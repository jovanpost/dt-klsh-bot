"""The engine.

One engine, N series. State is keyed by (series, event_ticker) and lives in
Postgres, never in memory and never by date. Several events from both series
are live at once -- that is the normal case, not an edge case.

Per event:
  1. On discovery, rest a NO limit at the series price on every open market.
     Placement is never gated on knowing the show time.
  2. Keep joining late-added markets until that event's cancel time.
  3. Cancel all of that event's orders at cancel time.
  4. Hold fills to settlement. Buy-and-hold, never a round trip.

Cancel-time resolution, highest wins:
  1. /when          (cancel_source starts with "telegram") -- never overwritten
  2. milestone start_date - buffer_min
  3. min(occurrence, close) - buffer_min, which may be the 14-day cap
  4. never close_time alone as a scheduled start; it is partly an outcome

MODE IS LOCKED TO THE EVENT AT DISCOVERY. A series flip takes effect on the
next event discovered. An event that opened under DRY stays DRY through its
own cancel, so one event never contains both DRY and LIVE rows.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clock, config, kalshi, store

log = logging.getLogger("trumpbot.strategy")

TRADEABLE_STATUS = {"active", "open"}

# Order of preference when reading a time off a market or event blob.
OCCURRENCE_FIELDS = ("occurrence_datetime", "occurrence_time", "event_start_time",
                     "expected_expiration_time", "latest_expiration_time")
CLOSE_FIELDS = ("close_time", "expected_expiration_time")
OPEN_FIELDS = ("open_time", "open_date")

# Cancel sources we treat as a real appearance time rather than a fallback cap.
TRUSTED_SOURCES = ("telegram", "milestone")

# If an event never gets a cancel time at all, stop polling it after this.
ORPHAN_HOURS = 48


def taker_fee(count: float, price: float) -> float:
    """Kalshi taker fee, dollars: ceil(0.07 * C * P * (1-P)) to the cent.

    Only charged on orders that crossed the spread at placement. Resting
    orders that fill pay nothing on these series -- neither appears in the
    Non-Standard Fees table, so the maker multiplier is 0.
    """
    raw = 0.07 * float(count) * float(price) * (1.0 - float(price))
    return math.ceil(raw * 100.0) / 100.0


def is_trusted(source: Optional[str]) -> bool:
    s = str(source or "").lower()
    return any(s.startswith(t) for t in TRUSTED_SOURCES)


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
        """Milestone if we have one, else min(occurrence, close), minus buffer.

        Never close_time alone: close_time moves when the appearance actually
        happens, so using it by itself leaks the outcome into the decision.
        """
        blobs = list(markets) + [event]
        mile = clock.parse_iso(event.get("milestone_start") or event.get("start_date"))
        occ, occ_field = self._pick_time(blobs, OCCURRENCE_FIELDS)
        close, close_field = self._pick_time(blobs, CLOSE_FIELDS)

        if mile:
            return mile - timedelta(minutes=int(buffer_min)), "milestone", mile, close

        candidates = [(t, f) for t, f in ((occ, occ_field), (close, close_field)) if t]
        if not candidates:
            return None, None, occ, close
        anchor, source = min(candidates, key=lambda x: x[0])
        return anchor - timedelta(minutes=int(buffer_min)), source, occ, close

    def _first_list(self, markets: List[Dict[str, Any]]) -> bool:
        """True if we caught this event within the grace window of its open.

        Only these events belong in the clean fill-rate test against 52%.
        Mid-event joins are a different measurement and must not be averaged in.
        """
        opened, _ = self._pick_time(list(markets), OPEN_FIELDS)
        if not opened:
            return False
        age = (clock.now_utc() - clock.to_utc(opened)).total_seconds()
        return 0 <= age <= config.FIRST_LIST_GRACE_SECONDS

    # ----------------------------------------------------------- discovery ---

    def discover(self) -> int:
        """Find new events in every series that is not OFF."""
        found = 0
        for series, cfg in config.series_config().items():
            mode = config.mode_for(series)
            if mode == config.MODE_OFF:
                continue
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
                self._register_event(series, ev, cfg, mode)
                found += 1
        return found

    def _maybe_apply_milestone(self, known: Dict[str, Any], ev: Dict[str, Any],
                               cfg: Dict[str, Any]) -> None:
        """If a milestone start appears after first list, tighten cancel_at.

        Never overwrites /when. Never cancels or replaces resting orders --
        queue position is not spent on a clock update.
        """
        if is_trusted(known.get("cancel_source")) and \
                str(known.get("cancel_source") or "").lower().startswith("telegram"):
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
            f"TIME UPDATED ({known.get('series')}) [{known.get('mode')}]\n{ticker}\n"
            f"{page}\n"
            f"Event time: {clock.fmt_ct(mile)} (milestone)\n"
            f"Cancel at: {clock.fmt_ct(new_cancel)}\n"
            f"Resting orders were not moved."
        )

    def _register_event(self, series: str, ev: Dict[str, Any], cfg: Dict[str, Any],
                        mode: str) -> None:
        ticker = ev["event_ticker"]
        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Could not read markets for %s: %s", ticker, exc)
            markets = []

        cancel_at, source, occ, close = self.cancel_anchor(markets, ev, cfg["buffer_min"])
        now = clock.now_utc()
        page = kalshi.event_page_url(series, ev.get("title"), ticker)
        at_open = self._first_list(markets)

        store.upsert_event({
            "event_ticker": ticker,
            "series": series,
            "mode": mode,
            "title": ev.get("title"),
            "subtitle": ev.get("sub_title") or ev.get("subtitle"),
            "discovered_at": now,
            "discovered_at_open": at_open,
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

        window = (cancel_at - now).total_seconds() if cancel_at else None
        trusted = is_trusted(source)

        store.log_line("info", f"[{series}/{mode}] new event {ticker}: {len(markets)} markets, "
                               f"cancel {clock.fmt_ct(cancel_at)} ({source})")
        self.notify(
            f"NEW EVENT ({series}) [{mode}]\n{ev.get('title') or ticker}\n"
            f"{ticker}\n"
            f"{page}\n"
            f"Event time: {clock.fmt_ct(occ)} ({source or 'unknown'})"
            + ("" if trusted else "  <- fallback, not a real showtime") + "\n"
            f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {cfg['buffer_min']}m)\n"
            f"Markets now: {len(markets)}\n"
            f"First list: {'yes' if at_open else 'no (joined mid-event)'}\n"
            f"Resting window: {clock.human_delta(window)}"
            + ("" if trusted else
               f"\n\nSet the real time:\n/when {ticker} 8:00 PM central")
        )

        if mode == config.MODE_LOG:
            store.log_line("info", f"[{series}] {ticker}: LOG mode, no orders placed")
            return

                if cancel_at is None:
            store.log_line("warn", f"{ticker}: no time field of any kind; placing anyway")
            self.notify(
                f"{ticker}: no usable time field. Orders still go out.\n"
                f"Send /when {ticker} 8:00 PM central to set cancel.\n"
                f"{page}"
            )

        if cancel_at is not None and cancel_at <= now:
            store.mark_event(ticker, cancelled_at=now, notified_cancel=True)
            store.log_line("warn", f"{ticker}: cancel time already passed at discovery")
            self.notify(f"{ticker}: appeared inside the cancel buffer. Logged, not traded.")
            return

        self.place_missing(series, cfg, ticker, markets, mode)

    # ----------------------------------------------------------- placement ---

    def place_missing(self, series: str, cfg: Dict[str, Any], event_ticker: str,
                      markets: List[Dict[str, Any]], mode: str) -> int:
        """Place on any tradeable market in this event we have no order for."""
        if mode not in config.PLACING_MODES:
            return 0
        live = (mode == config.MODE_LIVE)
        already = store.existing_market_tickers(event_ticker, mode)
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

            if live:
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
                "mode": mode,
                "event_ticker": event_ticker,
                "market_ticker": ticker,
                "market_title": m.get("yes_sub_title") or m.get("subtitle") or m.get("title"),
                "side": "no",
                "limit_price": price,
                "count": count,
                "dollars": float(cfg["dollars"]),
                "dry_run": not live,
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
                log.info("[%s/%s] rest NO %.2f x %g on %s", series, mode, price, count, ticker)
            elif filled_now:
                placed += 1
                log.info("[%s/%s] crossed NO %.2f x %g on %s", series, mode, price, count, ticker)
                self.notify(f"LIVE FILL ({series}) {ticker} NO {price:.2f} (at place)")

        if placed:
            ev = store.get_event(event_ticker) or {}
            store.mark_event(event_ticker,
                             traded=True,
                             markets_seen=max(len(markets), ev.get("markets_seen") or 0),
                             orders_placed=(ev.get("orders_placed") or 0) + placed)
            store.log_line("info", f"[{series}/{mode}] {event_ticker}: placed {placed} "
                                   f"NO @ {price:.2f} x {count:g}")
        return placed

    # --------------------------------------------------------------- nag ---

    def maybe_nag(self, ev: Dict[str, Any]) -> None:
        """Tell Jovan an open event has no real showtime. Nag, never hold."""
        if is_trusted(ev.get("cancel_source")):
            return
        now = clock.now_utc()
        last = clock.to_utc(ev.get("nagged_at")) if ev.get("nagged_at") else None
        if last and (now - last).total_seconds() < config.NAG_REPEAT_MINUTES * 60:
            return
        ticker = ev["event_ticker"]
        store.mark_event(ticker, nagged_at=now)
        resting = len(store.resting_orders(ticker))
        page = kalshi.event_page_url(ev.get("series") or "", ev.get("title"), ticker)
        self.notify(
            f"NEEDS A TIME ({ev.get('series')}) [{ev.get('mode')}]\n{ticker}\n"
            f"{page}\n"
            f"Cancel currently: {clock.fmt_ct(ev.get('cancel_at'))} "
            f"({ev.get('cancel_source') or 'none'})\n"
            f"Orders resting: {resting}\n"
            f"Check the app clock, then:\n/when {ticker} 8:00 PM central"
        )

    # --------------------------------------------------------------- polling ---

    def poll_event(self, ev: Dict[str, Any]) -> None:
        series = ev["series"]
        cfg = config.series_config().get(series)
        if not cfg:
            return
        # Mode is the one locked on the event, not the series' current mode.
        mode = config.normalize_mode(ev.get("mode") or config.mode_for(series))
        ticker = ev["event_ticker"]
        now = clock.now_utc()

        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Market poll failed for %s: %s", ticker, exc)
            return

        manual = str(ev.get("cancel_source") or "").lower().startswith("telegram")
        if not manual:
            event_blob = {
                "occurrence_datetime": ev.get("occurrence_at"),
                "close_time": ev.get("close_at"),
                "event_ticker": ticker,
                "milestone_start": ev.get("occurrence_at")
                if str(ev.get("cancel_source") or "") == "milestone" else None,
            }
            new_cancel, source, occ, close = self.cancel_anchor(markets, event_blob,
                                                               cfg["buffer_min"])
            if new_cancel and new_cancel != ev.get("cancel_at"):
                store.mark_event(ticker, cancel_at=new_cancel, cancel_source=source,
                                 occurrence_at=occ, close_at=close)
                if ev.get("cancel_at"):
                    store.log_line("info", f"{ticker}: cancel time moved to "
                                           f"{clock.fmt_ct(new_cancel)}")
                ev["cancel_at"] = new_cancel
                ev["cancel_source"] = source

        store.mark_event(ticker, last_seen_at=now, markets_seen=len(markets))
        self.maybe_nag(ev)

        cancel_at = ev.get("cancel_at")

                if cancel_at is None:
            disc = clock.to_utc(ev.get("discovered_at")) if ev.get("discovered_at") else now
            if (now - disc).total_seconds() > ORPHAN_HOURS * 3600:
                self.cancel_event(ev, reason=f"no time set within {ORPHAN_HOURS}h")
                return
        elif clock.to_utc(cancel_at) <= now:
            self.cancel_event(ev, reason="cancel time reached")
            return

        # every market is closed already -> nothing left to do
        if markets and not any((m.get("status") or "").lower() in TRADEABLE_STATUS
                               for m in markets):
            self.cancel_event(ev, reason="all markets closed")
            return

        self.place_missing(series, cfg, ticker, markets, mode)
        if mode in config.PLACING_MODES:
            self.check_fills(ev, markets, mode)
        self.sample_ticks(ev, markets)

    # ------------------------------------------------------------- fills ---

    def check_fills(self, ev: Dict[str, Any], markets: List[Dict[str, Any]],
                    mode: str) -> None:
        if mode == config.MODE_LIVE:
            self.check_live_fills(ev)
        elif mode == config.MODE_DRY:
            self.check_paper_fills(ev, markets)

    def check_paper_fills(self, ev: Dict[str, Any], markets: List[Dict[str, Any]]) -> None:
        """DRY fills come from watching the book, not from the fill API.

        Our resting NO limit at p fills when someone is willing to sell NO at
        p or lower -- equivalently, when the YES bid reaches (1 - p). Our fill
        price is our own limit, because we are the resting side.
        """
        by_ticker = {m.get("ticker"): m for m in markets}
        for row in store.resting_orders(ev["event_ticker"], mode=config.MODE_DRY):
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
                store.log_line("fill", f"[{ev['series']}/DRY] PAPER FILL {row['market_ticker']} "
                                       f"NO {limit:.2f} x {float(row['count']):g} "
                                       f"(book offered {ask:.2f})")
                self.notify(f"FILL ({ev['series']}) [DRY] {row['market_ticker']}\n"
                            f"NO {limit:.2f} x {float(row['count']):g}\n"
                            f"Book was offering NO at {ask:.2f}")

    def check_live_fills(self, ev: Dict[str, Any]) -> None:
        for row in store.resting_orders(ev["event_ticker"], mode=config.MODE_LIVE):
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
            store.log_line("fill", f"[{ev['series']}/LIVE] LIVE FILL {row['market_ticker']} "
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

        mode = config.normalize_mode(fresh.get("mode") or ev.get("mode"))
        now = clock.now_utc()
        live_errors = 0

        # Only LIVE rows have real orders on the exchange. A series turned OFF
        # mid-flight still gets its resting orders pulled here.
        if mode == config.MODE_LIVE:
            for row in store.resting_orders(ticker, mode=config.MODE_LIVE):
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

        store.log_line("info", f"[{ev.get('series')}/{mode}] cancelled {ticker}: "
                               f"{n} orders pulled ({reason})")

        if mode == config.MODE_LOG and not placed:
            self.notify(f"EVENT CLOSED ({ev.get('series')}) [LOG]\n{ticker}\n"
                        f"Reason: {reason}\nLogged only, no orders.")
            return

        self.notify(
            f"CANCELLED ({ev.get('series')}) [{mode}]\n{ticker}\n"
            f"Reason: {reason}\n"
            f"Clock was: {fresh.get('cancel_source') or 'none'}\n"
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
        """While paused: still honour cancel time and watch fills. No new orders.

        Pausing must never orphan a resting order through its cancel time.
        """
        ticker = ev["event_ticker"]
        mode = config.normalize_mode(ev.get("mode"))
        now = clock.now_utc()
        cancel_at = ev.get("cancel_at")
        if cancel_at and clock.to_utc(cancel_at) <= now:
            self.cancel_event(ev, reason="cancel time reached (paused)")
            return
        if mode not in config.PLACING_MODES:
            return
        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception:
            markets = []
        if markets:
            self.check_fills(ev, markets, mode)
