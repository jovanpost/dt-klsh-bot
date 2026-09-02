"""Kalshi REST client.

Scars baked in from the WNT build:
  * Read the *_dollars quote fields first. yes_bid / no_ask are empty on live
    markets and reading them reported zero fills for a whole afternoon.
  * The order book is bids only. A YES bid at 0.74 is someone selling NO at
    0.26. Do not look for an "asks" array.
  * get_markets needs cursor pagination, and Kalshi keeps adding markets to an
    event after the event first appears. One pass is not enough.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config

log = logging.getLogger("trumpbot.kalshi")

# Recommended host. api.elections.kalshi.com is still accepted as a fallback.
BASE = "https://external-api.kalshi.com"
PREFIX = "/trade-api/v2"


# ------------------------------------------------------------ price helpers ---

def to_dollars(value: Any) -> Optional[float]:
    """Accept '0.0500', 0.05, '5', or 5 and return dollars.

    Anything above 1.5 is assumed to be cents. 0 and None mean "no quote",
    not "free", so both come back as None.
    """
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1.5:
        v = v / 100.0
    return round(v, 4)


def quote(market: Dict[str, Any], side: str, kind: str) -> Optional[float]:
    """side is 'yes' or 'no'; kind is 'bid' or 'ask'. Dollar fields win."""
    for key in (f"{side}_{kind}_dollars", f"{side}_{kind}"):
        v = to_dollars(market.get(key))
        if v is not None:
            return v
    return None


def no_ask(market: Dict[str, Any]) -> Optional[float]:
    """Cheapest price someone will sell you NO at.

    Falls back to 1 - yes_bid, because a YES bid IS a NO offer.
    """
    direct = quote(market, "no", "ask")
    if direct is not None:
        return direct
    yb = quote(market, "yes", "bid")
    if yb is not None:
        return round(1.0 - yb, 4)
    return None


def fp_count(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def yes_bid_size(market: Dict[str, Any]) -> Optional[float]:
    """Contracts at the best YES bid — that size is what fills a resting NO."""
    for key in ("yes_bid_size_fp", "yes_bid_size", "no_ask_size_fp", "no_ask_size"):
        v = fp_count(market.get(key))
        if v is not None:
            return v
    return None


def last_price(market: Dict[str, Any]) -> Optional[float]:
    return to_dollars(market.get("last_price_dollars") or market.get("last_price"))


def event_page_url(series: str, title: Optional[str], ticker: str) -> str:
    """Public Kalshi event page. Deep-links into the app on a phone."""
    import re
    series_slug = re.sub(r"[^a-z0-9]+", "", (series or "").lower())
    raw = (title or ticker or "").lower()
    title_slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    title_slug = re.sub(r"-{2,}", "-", title_slug)
    return f"https://kalshi.com/markets/{series_slug}/{title_slug}/{ticker}"


# ------------------------------------------------------------------ client ---

class KalshiClient:
    def __init__(self, key_id: Optional[str] = None, private_key_pem: Optional[str] = None):
        self.key_id = key_id or config.get("KALSHI_KEY_ID")
        pem = private_key_pem or config.get("KALSHI_PRIVATE_KEY")
        self._key = None
        if pem:
            if isinstance(pem, str):
                pem = pem.encode()
            try:
                self._key = serialization.load_pem_private_key(pem, password=None)
            except Exception as exc:  # bad paste in Secrets is common
                log.error("Could not load Kalshi private key: %s", exc)
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.min_interval = 0.12  # ~8 requests/second ceiling

    # -- plumbing -------------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self._key)

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        if not self.authenticated:
            return {"Accept": "application/json"}
        ts = str(int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}".encode()
        sig = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": str(self.key_id),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _throttle(self) -> None:
        with self._lock:
            gap = time.time() - self._last_call
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last_call = time.time()

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None, retries: int = 2) -> Dict[str, Any]:
        """path is the part after /trade-api/v2, e.g. '/markets'.

        Signing uses the path WITHOUT the query string.
        """
        full_path = PREFIX + path
        url = BASE + full_path
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                resp = self.session.request(
                    method.upper(), url,
                    headers=self._headers(method, full_path),
                    params=params, json=body, timeout=20,
                )
                if resp.status_code == 429:
                    time.sleep(1.0 + attempt)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"{method} {path} -> {resp.status_code} {resp.text[:300]}")
                if not resp.content:
                    return {}
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt >= retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(str(last_exc))

    def _paged(self, path: str, params: dict, key: str, limit: int = 200,
               max_pages: int = 25) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            p["limit"] = limit
            if cursor:
                p["cursor"] = cursor
            data = self.request("GET", path, params=p)
            batch = data.get(key) or []
            out.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        return out

    # -- reads ---------------------------------------------------------------

    def get_events(self, series_ticker: str, status: str = "open") -> List[Dict[str, Any]]:
        events, _ = self.get_events_with_milestones(series_ticker, status=status)
        return events

    def get_events_with_milestones(self, series_ticker: str, status: str = "open"
                                   ) -> tuple:
        """Events plus the milestones blob (show start lives here, not on the event)."""
        events: List[Dict[str, Any]] = []
        milestones: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(25):
            params: Dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "with_milestones": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self.request("GET", "/events", params=params)
            events.extend(data.get("events") or [])
            milestones.extend(data.get("milestones") or [])
            cursor = data.get("cursor")
            if not cursor or not (data.get("events") or []):
                break
        return events, milestones

    def milestone_for(self, event_ticker: str,
                      milestones: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        want = (event_ticker or "").upper()
        for m in milestones:
            names = list(m.get("primary_event_tickers") or [])
            names.extend(m.get("related_event_tickers") or [])
            if any(str(t).upper() == want for t in names):
                return m
        return None

    def get_event(self, event_ticker: str, nested: bool = True) -> Dict[str, Any]:
        data = self.request("GET", f"/events/{event_ticker}",
                            params={"with_nested_markets": str(nested).lower()})
        return data.get("event") or data

    def get_markets(self, event_ticker: Optional[str] = None,
                    series_ticker: Optional[str] = None,
                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._paged("/markets", params, "markets")

    def get_market(self, ticker: str) -> Dict[str, Any]:
        data = self.request("GET", f"/markets/{ticker}")
        return data.get("market") or data

    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        data = self.request("GET", f"/markets/{ticker}/orderbook",
                            params={"depth": depth})
        return data.get("orderbook") or data

    # -- writes (live only) --------------------------------------------------

    def create_no_limit_order(self, ticker: str, count: float, no_price: float,
                              expiration_ts: Optional[int] = None) -> Dict[str, Any]:
        """Buy NO at a limit via Create Order V2.

        V2 quotes the YES book only: side=ask is sell-YES, which is buy-NO
        at (1 - price). The June 2026 cut removed POST /portfolio/orders
        (action/side/yes/no + integer cents).
        """
        if config.LIVE_COUNT_MODE == "floor":
            send_count = f"{max(1, int(count)):.2f}"
        else:
            send_count = f"{float(count):.6f}".rstrip("0").rstrip(".") or "1"
        yes_price = max(0.01, min(0.99, round(1.0 - float(no_price), 4)))
        body: Dict[str, Any] = {
            "ticker": ticker,
            "side": "ask",  # sell YES == buy NO
            "count": send_count,
            "price": f"{yes_price:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "client_order_id": str(uuid.uuid4()),
        }
        if expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        data = self.request("POST", "/portfolio/events/orders", body=body)
        return data.get("order") or data

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        try:
            return self.request("DELETE", f"/portfolio/events/orders/{order_id}")
        except Exception as exc:
            if "404" in str(exc) or "410" in str(exc):
                return self.request("DELETE", f"/portfolio/orders/{order_id}")
            raise

    def get_orders(self, event_ticker: Optional[str] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        return self._paged("/portfolio/orders", params, "orders")

    def get_fills(self, ticker: Optional[str] = None,
                  order_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        return self._paged("/portfolio/fills", params, "fills")
