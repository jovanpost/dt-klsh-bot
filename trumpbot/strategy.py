"""The engine.

One engine, N series. State is keyed by (series, event_ticker) and lives in
Postgres, never in memory and never by date. Several events from both series
are live at once -- that is the normal case, not an edge case.

Per event:
  1. On discovery, rest a NO limit at the series price on every open market.
  2. Keep joining late-added markets until that event's cancel time.
  3. Cancel everything for the event at
     min(occurrence_datetime, close_time) - buffer_min.
  4. Hold fills to settlement. Never exit early.

Buying NO at p is selling YES at (1 - p). The order sits on the YES ask side
and fills when someone buys YES at or above (1 - p).
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clock, config, kalshi, store

log = logging.getLogger("trumpbot.strategy")

TRADEABLE_STATUS = {"active", "open"}

# Order of preference when reading the appearance time off a market or event.
OCCURRENCE_FIELDS = ("occurrence_datetime", "occurrence_time", "event_start_time",
                     "expected_expiration_time", "latest_expiration_time")
CLOSE_FIELDS = ("close_time", "expected_expiration_time")


def taker_fee(count: float, price: float) -> float:
    """Kalshi taker fee, dollars: ceil(0.07 * C * P * (1-P)) to the cent.

    Only charged on orders that crossed the spread at placement. Resting
    orders that fill pay nothing on these series -- neither appears in the
    Non-Standard Fees table, so the maker multiplier is 0.
    """
    raw = 0.07 * float(count) * float(price) * (1.0 - float(price))
    return math.ceil(raw * 100.0) / 100.0


class Engine:
    def __init__(self, client: kalshi.KalshiClient,
                 notify: Optional[Callable[[str], None]] = None):
        self.client = client
        self.notify = notify or (lambda msg: None)
        self._last_discovery = 0.0
        self._last_tick_log: Dict[str, float] = {}
        self._milestones: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------- helpers ---

    def _pick_time(self, blobs: List[Dict[str, Any]], fields) -> Tuple[Optional[Any], Optional[str]]:
        """Earliest usable timestamp across markets, plus which field gave it."""
        best = None
        best_field = None
        for blob in blobs:
            for f in fields:
                dt = clock.parse_iso(blob.get(f))
                if dt is None:
                    continue
                if best is None or dt < best:
                    best, best_field = dt, f
                break  # first field that exists on this blob wins for this blob
        return best, best_field

    def cancel_anchor(self, markets: List[Dict[str, Any]], event: Dict[str, Any],
                      buffer_min: int):
        """min(occurrence, close) - buffer.

        Never close_time alone: close_time moves when the appearance actually
        happens, so using it by itself leaks the outcome into the decision.
        Some B events publish a close_time that is EARLIER than the stated
        occurrence, which is why we take the minimum of the two.
        """
        blobs = list(markets) + [event]
        mile = clock.parse_iso(event.get("milestone_start") or event.get("start_date"))
        occ, occ_field = self._pick_time(blobs, OCCURRENCE_FIELDS)
        close, close_field = self._pick_time(blobs, CLOSE_FIELDS)

        if mile:
            occ, occ_field = mile, "milestone"
            cancel_at = mile - timedelta(minutes=int(buffer_min))
            return cancel_at, "milestone", occ, close

        candidates = [(t, f) for t, f in ((occ, occ_field), (close, close_field)) if t]
        if not candidates:
            return None, None, occ, close
        anchor, source = min(candidates, key=lambda x: x[0])
        return anchor - timedelta(minutes=int(buffer_min)), source, occ, close

    # ----------------------------------------------------------- discovery ---

    def discover(self) -> int:
        """Find new events. Log every event seen, traded or not."""
        found = 0
        for series, cfg in config.enabled_series().items():
            try:
                evs, miles = self.client.get_events_with_milestones(series, status="open")
                self._milestones[series] = miles
            except Exception as exc:
                log.warning("Event discovery failed for %s: %s", series, exc)
                store.log_line("warn", f"Discovery failed for {series}: {exc}")
                continue
            for ev in evs:
                ticker = ev.get("event_ticker")
                if not ticker:
                    continue
                mile = self.client.milestone_for(ticker, miles)
                if mile:
                    ev["milestone_start"] = mile.get("start_date")
                    ev["milestone_end"] = mile.get("end_date")
                    ev["milestone_title"] = mile.get("title")
                known = store.get_event(ticker)
                if known:
                    self._maybe_apply_milestone(known, ev, cfg)
                    store.mark_event(ticker, last_seen_at=clock.now_utc(),
                                     status=ev.get("status") or known.get("status"))
                    continue
                self._register_event(series, ev, cfg)
                found += 1
        return found

    def _maybe_apply_milestone(self, known: Dict[str, Any], ev: Dict[str, Any],
                               cfg: Dict[str, Any]) -> None:
        """If a milestone start appears after first list, tighten cancel_at.

        Never overwrites /when. Never cancels or replaces resting orders.
        """
        if str(known.get("cancel_source") or "").startswith("telegram"):
            return
        mile = clock.parse_iso(ev.get("milestone_start"))
        if not mile:
            return
        new_cancel = mile - timedelta(minutes=int(cfg["buffer_min"]))
        old = known.get("cancel_at")
        if old and clock.to_utc(old) == clock.to_utc(new_cancel):
            return
        ticker = known["event_ticker"]
        store.mark_event(ticker, cancel_at=new_cancel, cancel_source="milestone",
                         occurrence_at=mile)
        store.log_line("info", f"{ticker}: milestone start {clock.fmt_ct(mile)}, "
                               f"cancel {clock.fmt_ct(new_cancel)}")
        page = kalshi.event_page_url(known.get("series") or ev.get("series_ticker") or "",
                                     known.get("title") or ev.get("title"), ticker)
        self.notify(
            f"TIME UPDATED ({known.get('series')})\n{ticker}\n"
            f"{page}\n"
            f"Event time: {clock.fmt_ct(mile)} (milestone)\n"
            f"Cancel at: {clock.fmt_ct(new_cancel)}\n"
            f"Resting orders were not moved."
        )

    def _register_event(self, series: str, ev: Dict[str, Any], cfg: Dict[str, Any]) -> None:
        ticker = ev["event_ticker"]
        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Could not read markets for %s: %s", ticker, exc)
            markets = []

        cancel_at, source, occ, close = self.cancel_anchor(markets, ev, cfg["buffer_min"])
        now = clock.now_utc()
        page = kalshi.event_page_url(series, ev.get("title"), ticker)

        store.upsert_event({
            "event_ticker": ticker,
            "series": series,
            "title": ev.get("title"),
            "subtitle": ev.get("sub_title") or ev.get("subtitle"),
            "discovered_at": now,
            "occurrence_at": occ,
            "close_at": close,
            "cancel_at": cancel_at,
            "cancel_source": source,
            "traded": False,
            "markets_seen": len(markets),
            "orders_placed": 0,
            "last_seen_at": now,
            "status": ev.get("status"),
        })

        window = None
        if cancel_at:
            window = (cancel_at - now).total_seconds()

        store.log_line("info", f"[{series}] new event {ticker}: {len(markets)} markets, "
                               f"cancel {clock.fmt_ct(cancel_at)}")
        self.notify(
            f"NEW EVENT ({series})\n{ev.get('title') or ticker}\n"
            f"{ticker}\n"
            f"{page}\n"
            f"Event time: {clock.fmt_ct(occ)} ({source})\n"
            f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {cfg['buffer_min']}m)\n"
            f"Markets now: {len(markets)}\n"
            f"Resting window: {clock.human_delta(window)}"
        )

        if cancel_at is None:
            store.mark_event(ticker, cancelled_at=now, notified_cancel=True)
            store.log_line("warn", f"{ticker}: no usable occurrence or close time, not trading")
            self.notify(f"{ticker}: no usable time fields. Logged, not traded.")
            return

        if cancel_at <= now:
            store.mark_event(ticker, cancelled_at=now, notified_cancel=True)
            store.log_line("warn", f"{ticker}: cancel time already passed at discovery, not trading")
            self.notify(f"{ticker}: appeared inside the cancel buffer. Logged, not traded.")
            return

        self.place_missing(series, cfg, ticker, markets)

    # ----------------------------------------------------------- placement ---

    def place_missing(self, series: str, cfg: Dict[str, Any], event_ticker: str,
                      markets: List[Dict[str, Any]]) -> int:
        """Place on any tradeable market in this event we have no order for."""
        dry = config.dry_run()
        already = store.existing_market_tickers(event_ticker, dry)
        price = float(cfg["rest_price"])
        count = config.contracts_for(cfg)
        placed = 0

        for m in markets:
            ticker = m.get("ticker")
            if not ticker or ticker in already:
                continue
            if (m.get("status") or "").lower() not in TRADEABLE_STATUS:
                continue

            ask_now = kalshi.no_ask(m)
            crosses = ask_now is not None and ask_now <= price
            order_id = None
            status = "resting"
            filled_now = False
            fill_price = None

            if not dry:
                try:
                    resp = self.client.create_no_limit_order(ticker, count, price)
                    order_id = resp.get("order_id") or resp.get("id")
                    rem = resp.get("remaining_count")
                    fill_n = resp.get("fill_count")
                    try:
                        rem_f = float(rem) if rem is not None else None
                        fill_f = float(fill_n) if fill_n is not None else 0.0
                    except (TypeError, ValueError):
                        rem_f, fill_f = None, 0.0
                    if rem_f is not None and rem_f <= 0 and fill_f > 0:
                        filled_now = True
                        status = "filled"
                        fill_price = price
                except Exception as exc:
                    log.error("Order rejected on %s: %s", ticker, exc)
                    store.log_line("error", f"Order rejected on {ticker}: {exc}")
                    status = "rejected"

            store.record_order({
                "series": series,
                "event_ticker": event_ticker,
                "market_ticker": ticker,
                "market_title": m.get("yes_sub_title") or m.get("subtitle") or m.get("title"),
                "side": "no",
                "limit_price": price,
                "count": count,
                "dollars": float(cfg["dollars"]),
                "dry_run": dry,
                "took_at_open": bool(crosses),
                "quote_at_place": ask_now,
                "placed_at": clock.now_utc(),
                "order_id": order_id,
                "status": status,
                "filled_at": clock.now_utc() if filled_now else None,
                "fill_price": fill_price,
            })
            if status == "resting":
                placed += 1
                log.info("[%s] rest NO %.2f x %g on %s", series, price, count, ticker)
            elif filled_now:
                placed += 1
                log.info("[%s] crossed NO %.2f x %g on %s", series, price, count, ticker)
                self.notify(f"LIVE FILL ({series}) {ticker} NO {price:.2f} (at place)")

        if placed:
            ev = store.get_event(event_ticker) or {}
            store.mark_event(event_ticker,
                             traded=True,
                             markets_seen=max(len(markets), ev.get("markets_seen") or 0),
                             orders_placed=(ev.get("orders_placed") or 0) + placed)
            store.log_line("info", f"[{series}] {event_ticker}: placed {placed} "
                                   f"NO @ {price:.2f} x {count:g}")
        return placed

    # --------------------------------------------------------------- polling ---

    def poll_event(self, ev: Dict[str, Any]) -> None:
        series = ev["series"]
        cfg = config.series_config().get(series)
        if not cfg:
            return
        ticker = ev["event_ticker"]
        now = clock.now_utc()

        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Market poll failed for %s: %s", ticker, exc)
            return

        # Kalshi can revise the appearance time after the event is listed.
        # Keep the event blob so occurrence_datetime on the event (not just
        # the markets) still participates in the min() after discovery.
        event_blob = {
            "occurrence_datetime": ev.get("occurrence_at"),
            "close_time": ev.get("close_at"),
            "event_ticker": ticker,
            "milestone_start": ev.get("occurrence_at") if str(ev.get("cancel_source") or "") == "milestone" else None,
        }
        new_cancel, source, occ, close = self.cancel_anchor(markets, event_blob, cfg["buffer_min"])
        manual = str(ev.get("cancel_source") or "").startswith("telegram")
        if manual:
            pass
        elif new_cancel and new_cancel != ev.get("cancel_at"):
            store.mark_event(ticker, cancel_at=new_cancel, cancel_source=source,
                             occurrence_at=occ, close_at=close)
            if ev.get("cancel_at"):
                store.log_line("info", f"{ticker}: cancel time moved to {clock.fmt_ct(new_cancel)}")
            ev["cancel_at"] = new_cancel

        store.mark_event(ticker, last_seen_at=now, markets_seen=len(markets))

        cancel_at = ev.get("cancel_at")
        if cancel_at and clock.to_utc(cancel_at) <= now:
            self.cancel_event(ev, reason="cancel time reached")
            return

        # every market is closed already -> nothing left to do
        if markets and not any((m.get("status") or "").lower() in TRADEABLE_STATUS for m in markets):
            self.cancel_event(ev, reason="all markets closed")
            return

        self.place_missing(series, cfg, ticker, markets)
        self.check_paper_fills(ev, markets)
        self.sample_ticks(ev, markets)

    # ------------------------------------------------------------- fills ---

    def check_paper_fills(self, ev: Dict[str, Any], markets: List[Dict[str, Any]]) -> None:
        """Dry-run fills come from watching the book, not from the fill API.

        Our resting NO limit at p fills when someone is willing to sell NO at
        p or lower -- equivalently, when the YES bid reaches (1 - p). Our fill
        price is our own limit, because we are the resting side.
        """
        if not config.dry_run():
            self.check_live_fills(ev)
            return

        by_ticker = {m.get("ticker"): m for m in markets}
        for row in store.resting_orders(ev["event_ticker"]):
            m = by_ticker.get(row["market_ticker"])
            if not m:
                continue
            ask = kalshi.no_ask(m)
            if ask is None:
                continue
            limit = float(row["limit_price"])
            if ask <= limit:
                store.update_order(row["id"], status="filled",
                                   filled_at=clock.now_utc(), fill_price=limit)
                store.log_line("fill", f"[{ev['series']}] PAPER FILL {row['market_ticker']} "
                                       f"NO {limit:.2f} x {float(row['count']):g} "
                                       f"(book offered {ask:.2f})")
                self.notify(f"FILL ({ev['series']}) {row['market_ticker']}\n"
                            f"NO {limit:.2f} x {float(row['count']):g}\n"
                            f"Book was offering NO at {ask:.2f}")

    def check_live_fills(self, ev: Dict[str, Any]) -> None:
        for row in store.resting_orders(ev["event_ticker"]):
            if not row.get("order_id"):
                continue
            try:
                fills = self.client.get_fills(order_id=row["order_id"])
            except Exception:
                continue
            if not fills:
                continue
            f = fills[0]
            price = kalshi.to_dollars(f.get("no_price_dollars") or f.get("no_price")) \
                or float(row["limit_price"])
            store.update_order(row["id"], status="filled",
                               filled_at=clock.parse_iso(f.get("created_time")) or clock.now_utc(),
                               fill_price=price)
            store.log_line("fill", f"[{ev['series']}] LIVE FILL {row['market_ticker']} "
                                   f"NO {price:.2f}")
            self.notify(f"LIVE FILL ({ev['series']}) {row['market_ticker']} NO {price:.2f}")

    # ------------------------------------------------------------- cancel ---

    def cancel_event(self, ev: Dict[str, Any], reason: str = "") -> None:
        """Guarded: if cancelled_at is already set we do nothing.

        The WNT bot re-cancelled every five minutes from 5:29 until 6:15 and
        spammed Telegram, because dry-run cancel never wrote cancelled_at.
        """
        ticker = ev["event_ticker"]
        fresh = store.get_event(ticker) or ev
        if fresh.get("cancelled_at"):
            return

        now = clock.now_utc()
        live_errors = 0
        if not config.dry_run():
            for row in store.resting_orders(ticker):
                if not row.get("order_id"):
                    continue
                try:
                    self.client.cancel_order(row["order_id"])
                except Exception as exc:
                    live_errors += 1
                    log.error("Cancel failed for %s: %s", row["order_id"], exc)

        n = store.cancel_resting_for_event(ticker, now)
        store.mark_event(ticker, cancelled_at=now, notified_cancel=True)

        rows = [r for r in store.orders_for_dashboard(limit=5000)
                if r["event_ticker"] == ticker]
        filled = [r for r in rows if r["status"] == "filled"]
        placed = [r for r in rows if r["status"] != "rejected"]
        rate = (len(filled) / len(placed)) if placed else None

        store.log_line("info", f"[{ev['series']}] cancelled {ticker}: {n} orders pulled "
                               f"({reason})")
        self.notify(
            f"CANCELLED ({ev['series']})\n{ticker}\n"
            f"Reason: {reason}\n"
            f"Orders pulled: {n}\n"
            f"Filled: {len(filled)} of {len(placed)} ({clock.pct(rate)})"
            + (f"\nLive cancel errors: {live_errors}" if live_errors else "")
        )

    # -------------------------------------------------------------- ticks ---

    def sample_ticks(self, ev: Dict[str, Any], markets: List[Dict[str, Any]]) -> None:
        import time as _t
        key = ev["event_ticker"]
        last = self._last_tick_log.get(key, 0.0)
        if _t.time() - last < config.TICK_LOG_SECONDS:
            return
        self._last_tick_log[key] = _t.time()
        for m in markets:
            try:
                store.record_tick({
                    "ts": clock.now_utc(),
                    "series": ev["series"],
                    "event_ticker": key,
                    "market_ticker": m.get("ticker"),
                    "yes_bid": kalshi.quote(m, "yes", "bid"),
                    "yes_ask": kalshi.quote(m, "yes", "ask"),
                    "no_bid": kalshi.quote(m, "no", "bid"),
                    "no_ask": kalshi.no_ask(m),
                    "last": kalshi.last_price(m),
                    "volume": m.get("volume"),
                })
            except Exception:
                pass

    # --------------------------------------------------------------- tick ---

    def run_once(self, allow_place: bool = True) -> None:
        import time as _t
        now_s = _t.time()
        if allow_place and now_s - self._last_discovery >= config.DISCOVERY_SECONDS:
            self._last_discovery = now_s
            self.discover()
        for ev in store.live_events():
            try:
                if allow_place:
                    self.poll_event(ev)
                else:
                    self.poll_event_paused(ev)
            except Exception as exc:
                log.exception("poll_event failed for %s", ev.get("event_ticker"))
                store.log_line("error", f"poll failed {ev.get('event_ticker')}: {exc}")

    def poll_event_paused(self, ev: Dict[str, Any]) -> None:
        """While paused: still honor cancel time and watch fills. No new orders."""
        ticker = ev["event_ticker"]
        now = clock.now_utc()
        cancel_at = ev.get("cancel_at")
        if cancel_at and clock.to_utc(cancel_at) <= now:
            self.cancel_event(ev, reason="cancel time reached (paused)")
            return
        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception:
            markets = []
        if markets:
            self.check_paper_fills(ev, markets)