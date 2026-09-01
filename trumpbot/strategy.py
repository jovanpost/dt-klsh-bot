"""The engine.

One engine, N series across families. State is keyed by (series, event_ticker)
and lives in Postgres, never in memory and never by date.

Per event:
  1. On discovery, rest a NO limit at the series price on every open market.
     Placement is NEVER gated on knowing the show time.
  2. Keep joining late-added markets until that event's cancel time.
  3. Cancel all of that event's orders at cancel time.
  4. Hold fills to settlement. Buy-and-hold, never a round trip.

Cancel-time resolution, highest wins:
  1. /when          (cancel_source starts with "telegram") -- never overwritten
  2. milestones.start_date - buffer_min   <- the automatic clock
  3. min(occurrence, close) - buffer_min, usually the 14-day cap. Nag, but
     still place.
  4. No timestamp at all -- still nag, still place.
  5. Never close_time alone as a scheduled start.

MODE AND FAMILY ARE LOCKED TO THE EVENT AT DISCOVERY.
"""
from __future__ import annotations

import logging
import math
import time as _time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import analytics, clock, config, kalshi, store

log = logging.getLogger("trumpbot.strategy")

TRADEABLE_STATUS = {"active", "open"}

OCCURRENCE_FIELDS = ("occurrence_datetime", "occurrence_time", "event_start_time",
                     "expected_expiration_time", "latest_expiration_time")
CLOSE_FIELDS = ("close_time", "expected_expiration_time")
OPEN_FIELDS = ("open_time", "open_date")

TRUSTED_SOURCES = ("telegram", "milestone")


def _cutoff_listed_utc():
    raw = getattr(config, "CUTOFF_LISTED_CT", None)
    if not raw:
        return None
    dt = clock.parse_when_clock(str(raw), "central") if hasattr(clock, "parse_when_clock") else None
    if dt is not None:
        return clock.to_utc(dt)
    try:
        from zoneinfo import ZoneInfo
        naive = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=ZoneInfo("America/Chicago"))
    except Exception:
        return None

POLL_HOT_SECONDS = 5
POLL_WARM_SECONDS = 20
POLL_COLD_SECONDS = 120
HOT_WINDOW_MINUTES = 20
KILL_CHECK_SECONDS = 600


def taker_fee(count: float, price: float) -> float:
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
        self._series_swept: Dict[str, float] = {}
        self._event_polled: Dict[str, float] = {}
        self._last_tick_log: Dict[str, float] = {}
        self._last_kill_check = 0.0

    def _pick_time(self, blobs: List[Dict[str, Any]], fields) -> Tuple[Optional[Any], Optional[str]]:
        best = None
        best_field = None
        for blob in blobs:
            for f in fields:
                dt = clock.parse_iso(blob.get(f))
                if dt is None:
                    continue
                if best is None or dt < best:
                    best, best_field = dt, f
                break
        return best, best_field

    def cancel_anchor(self, markets: List[Dict[str, Any]], event: Dict[str, Any],
                      buffer_min: int):
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

    def _listed_at(self, markets: List[Dict[str, Any]], ev: Dict[str, Any]):
        blobs = list(markets) + [ev]
        opened, _ = self._pick_time(blobs, OPEN_FIELDS + ("created_time", "created_ts"))
        return opened

    def _first_list(self, markets: List[Dict[str, Any]]) -> bool:
        opened, _ = self._pick_time(list(markets), OPEN_FIELDS)
        if not opened:
            return False
        age = (clock.now_utc() - clock.to_utc(opened)).total_seconds()
        return 0 <= age <= config.FIRST_LIST_GRACE_SECONDS

    def due_series(self) -> List[str]:
        now = _time.time()
        due: List[Tuple[float, str]] = []
        for s, cfg in config.series_config().items():
            interval = config.DISCOVERY_INTERVAL_BY_MODE.get(cfg["mode"])
            if interval is None:
                continue
            last = self._series_swept.get(s, 0.0)
            waited = now - last
            if waited >= interval:
                due.append((waited / interval, s))
        due.sort(reverse=True)
        return [s for _, s in due[:config.DISCOVERY_MAX_SERIES_PER_TICK]]

    def discover(self) -> int:
        found = 0
        for series in self.due_series():
            self._series_swept[series] = _time.time()
            cfg = config.series_cfg(series)
            if not cfg:
                continue
            try:
                evs, miles = self.client.get_events_with_milestones(series, status="open")
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
                self._register_event(cfg, ev)
                found += 1
        return found

    def _maybe_apply_milestone(self, known: Dict[str, Any], ev: Dict[str, Any],
                               cfg: Dict[str, Any]) -> None:
        if str(known.get("cancel_source") or "").lower().startswith("telegram"):
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
        store.log_line("info", f"{ticker}: milestone {clock.fmt_ct(mile)}, "
                               f"cancel {clock.fmt_ct(new_cancel)}")
        page = kalshi.event_page_url(known.get("series") or "",
                                     known.get("title") or ev.get("title"), ticker)
        self.notify(
            f"TIME UPDATED ({known.get('series')}) [{known.get('mode')}]\n{ticker}\n"
            f"{page}\n"
            f"Event time: {clock.fmt_ct(mile)} (milestone)\n"
            f"Cancel at: {clock.fmt_ct(new_cancel)}\n"
            f"Resting orders were not moved."
        )

    def _register_event(self, cfg: Dict[str, Any], ev: Dict[str, Any]) -> None:
        series = cfg["series"]
        mode = cfg["mode"]
        family = cfg["family"]
        ticker = ev["event_ticker"]
        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Could not read markets for %s: %s", ticker, exc)
            markets = []

        listed = self._listed_at(markets, ev)
        cutoff = _cutoff_listed_utc()
        too_old = False
        if cutoff is not None:
            if listed is not None and listed < cutoff:
                too_old = True
            elif listed is None and clock.now_utc() < cutoff:
                too_old = True
        if too_old:
            store.upsert_event({
                "event_ticker": ticker,
                "series": series,
                "family": family,
                "mode": mode,
                "title": ev.get("title"),
                "discovered_at": clock.now_utc(),
                "discovered_at_open": False,
                "cancel_source": "pre_cutoff",
                "traded": False,
                "markets_seen": len(markets),
                "orders_placed": 0,
                "last_seen_at": clock.now_utc(),
                "cancelled_at": clock.now_utc(),
                "status": ev.get("status"),
            })
            store.log_line("info", f"{ticker}: listed {clock.fmt_ct(listed)} before cutoff; skipped")
            return

        cancel_at, source, occ, close = self.cancel_anchor(markets, ev, cfg["buffer_min"])
        now = clock.now_utc()
        page = kalshi.event_page_url(series, ev.get("title"), ticker)
        at_open = self._first_list(markets)

        store.upsert_event({
            "event_ticker": ticker, "series": series, "family": family,
            "mode": mode, "title": ev.get("title"),
            "subtitle": ev.get("sub_title") or ev.get("subtitle"),
            "discovered_at": now, "discovered_at_open": at_open,
            "occurrence_at": occ, "close_at": close,
            "cancel_at": cancel_at, "cancel_source": source,
            "traded": False, "markets_seen": len(markets), "orders_placed": 0,
            "last_seen_at": now, "status": ev.get("status"),
        })

        window = (cancel_at - now).total_seconds() if cancel_at else None
        trusted = is_trusted(source)
        store.log_line("info", f"[{series}/{mode}] new event {ticker}: "
                               f"{len(markets)} markets, cancel "
                               f"{clock.fmt_ct(cancel_at)} ({source})")

        placing = mode in config.PLACING_MODES and config.tradeable(cfg)

        self.notify(
            f"NEW EVENT ({series}) [{mode}/{family}]\n{ev.get('title') or ticker}\n"
            f"{ticker}\n{page}\n"
            f"Event time: {clock.fmt_ct(occ)} ({source or 'unknown'})"
            + ("" if trusted else "  <- fallback, not a real showtime") + "\n"
            f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {cfg['buffer_min']}m)\n"
            f"Markets now: {len(markets)}\n"
            f"First list: {'yes' if at_open else 'no (joined mid-event)'}\n"
            f"Resting window: {clock.human_delta(window)}\n"
            + (f"Resting NO {float(cfg['rest_price']):.2f} x "
               f"{config.contracts_for(cfg):g} (${float(cfg['dollars']):.2f})"
               if placing else "Recording only, no orders.")
            + ("" if trusted else f"\n\nSet the real time:\n/when {ticker} 8:00 PM central")
        )

        if not placing:
            return
        if cancel_at is None:
            store.log_line("warn", f"{ticker}: no time field of any kind; placing anyway")
            self.notify(
                f"{ticker}: no usable time field. Orders still go out.\n"
                f"Send /when {ticker} 8:00 PM central to set cancel."
            )

        if cancel_at is not None and cancel_at <= now:
            store.mark_event(ticker, cancelled_at=now, notified_cancel=True)
            store.log_line("warn", f"{ticker}: cancel time already past at discovery")
            self.notify(f"{ticker}: appeared inside the cancel buffer. Not traded.")
            return

        self.place_missing(cfg, ticker, markets, mode)

    def place_missing(self, cfg: Dict[str, Any], event_ticker: str,
                      markets: List[Dict[str, Any]], mode: str) -> int:
        if mode not in config.PLACING_MODES or not config.tradeable(cfg):
            return 0
        series = cfg["series"]
        family = cfg["family"]
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
            order_id, status = None, "resting"
            filled_now, fill_price = False, None

            if live:
                try:
                    resp = self.client.create_no_limit_order(ticker, count, price)
                    order_id = resp.get("order_id") or resp.get("id")
                    try:
                        rem_f = float(resp["remaining_count"]) \
                            if resp.get("remaining_count") is not None else None
                        fill_f = float(resp.get("fill_count") or 0)
                    except (TypeError, ValueError):
                        rem_f, fill_f = 0.0, 0.0
                    if rem_f is not None and rem_f <= 0 and fill_f > 0:
                        filled_now, status, fill_price = True, "filled", price
                except Exception as exc:
                    log.error("Order rejected on %s: %s", ticker, exc)
                    store.log_line("error", f"Order rejected on {ticker}: {exc}")
                    status = "rejected"

            store.record_order({
                "series": series, "family": family, "mode": mode,
                "event_ticker": event_ticker, "market_ticker": ticker,
                "market_title": m.get("yes_sub_title") or m.get("subtitle") or m.get("title"),
                "side": "no", "limit_price": price, "count": count,
                "dollars": float(cfg["dollars"]), "dry_run": not live,
                "took_at_open": bool(crosses), "quote_at_place": ask_now,
                "placed_at": clock.now_utc(), "order_id": order_id,
                "status": status,
                "filled_at": clock.now_utc() if filled_now else None,
                "fill_price": fill_price,
            })
            if status == "resting":
                placed += 1
            elif filled_now:
                placed += 1
                word = m.get("yes_sub_title") or m.get("subtitle") or ticker
                self.notify(f"LIVE FILL ({series}) {word}\n{ticker} NO {price:.2f} (at place)")

        if placed:
            ev = store.get_event(event_ticker) or {}
            store.mark_event(event_ticker, traded=True,
                             markets_seen=max(len(markets), ev.get("markets_seen") or 0),
                             orders_placed=(ev.get("orders_placed") or 0) + placed)
            store.log_line("info", f"[{series}/{mode}] {event_ticker}: placed {placed} "
                                   f"NO @ {price:.2f} x {count:g}")
        return placed

    def maybe_nag(self, ev: Dict[str, Any]) -> None:
        if is_trusted(ev.get("cancel_source")):
            return
        if config.normalize_mode(ev.get("mode")) not in config.PLACING_MODES:
            return
        now = clock.now_utc()
        last = clock.to_utc(ev.get("nagged_at")) if ev.get("nagged_at") else None
        if last and (now - last).total_seconds() < config.NAG_REPEAT_MINUTES * 60:
            return
        ticker = ev["event_ticker"]
        store.mark_event(ticker, nagged_at=now)
        page = kalshi.event_page_url(ev.get("series") or "", ev.get("title"), ticker)
        self.notify(
            f"NEEDS A TIME ({ev.get('series')}) [{ev.get('mode')}]\n{ticker}\n{page}\n"
            f"Cancel currently: {clock.fmt_ct(ev.get('cancel_at'))} "
            f"({ev.get('cancel_source') or 'none'})\n"
            f"Orders resting: {len(store.resting_orders(ticker))}\n"
            f"Check the app clock, then:\n/when {ticker} 8:00 PM central")

    def _poll_interval(self, ev: Dict[str, Any]) -> int:
        mode = config.normalize_mode(ev.get("mode"))
        if mode not in config.PLACING_MODES:
            return POLL_COLD_SECONDS
        cancel_at = ev.get("cancel_at")
        if cancel_at:
            secs = (clock.to_utc(cancel_at) - clock.now_utc()).total_seconds()
            if secs <= HOT_WINDOW_MINUTES * 60:
                return POLL_HOT_SECONDS
        return POLL_WARM_SECONDS

    def poll_event(self, ev: Dict[str, Any]) -> None:
        series = ev["series"]
        cfg = config.series_cfg(series)
        if not cfg:
            return
        mode = config.normalize_mode(ev.get("mode") or cfg["mode"])
        ticker = ev["event_ticker"]
        now = clock.now_utc()

        try:
            markets = self.client.get_markets(event_ticker=ticker)
        except Exception as exc:
            log.warning("Market poll failed for %s: %s", ticker, exc)
            return

        if not str(ev.get("cancel_source") or "").lower().startswith("telegram"):
            event_blob = {
                "occurrence_datetime": ev.get("occurrence_at"),
                "close_time": ev.get("close_at"),
                "milestone_start": ev.get("occurrence_at")
                if str(ev.get("cancel_source") or "") == "milestone" else None,
            }
            new_cancel, source, occ, close = self.cancel_anchor(
                markets, event_blob, cfg["buffer_min"])
            if new_cancel and new_cancel != ev.get("cancel_at"):
                store.mark_event(ticker, cancel_at=new_cancel, cancel_source=source,
                                 occurrence_at=occ, close_at=close)
                ev["cancel_at"], ev["cancel_source"] = new_cancel, source

        store.mark_event(ticker, last_seen_at=now, markets_seen=len(markets))
        self.maybe_nag(ev)

        cancel_at = ev.get("cancel_at")
        if cancel_at is None:
            disc = clock.to_utc(ev.get("discovered_at")) if ev.get("discovered_at") else now
            if (now - disc).total_seconds() > config.ORPHAN_HOURS * 3600:
                self.cancel_event(ev, reason=f"no time set within {config.ORPHAN_HOURS}h")
                return
        elif clock.to_utc(cancel_at) <= now:
            self.cancel_event(ev, reason="cancel time reached")
            return

        if markets and not any((m.get("status") or "").lower() in TRADEABLE_STATUS
                               for m in markets):
            self.cancel_event(ev, reason="all markets closed")
            return

        self.place_missing(cfg, ticker, markets, mode)
        if mode in config.PLACING_MODES:
            self.check_fills(ev, markets, mode)
        self.sample_ticks(ev, markets)

    def check_fills(self, ev: Dict[str, Any], markets: List[Dict[str, Any]],
                    mode: str) -> None:
        if mode == config.MODE_LIVE:
            self.check_live_fills(ev)
        elif mode == config.MODE_DRY:
            self.check_paper_fills(ev, markets)

    def check_paper_fills(self, ev: Dict[str, Any], markets: List[Dict[str, Any]]) -> None:
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
                store.log_line("fill", f"[{ev['series']}/DRY] PAPER FILL "
                                       f"{row['market_ticker']} NO {limit:.2f} "
                                       f"x {float(row['count']):g} (book {ask:.2f})")
                word = row.get("market_title") or row["market_ticker"]
                self.notify(f"FILL ({ev['series']}) [DRY] {word}\n"
                            f"{row['market_ticker']}\n"
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
            store.log_line("fill", f"[{ev['series']}/LIVE] FILL {row['market_ticker']} "
                                   f"NO {price:.2f}")
            word = row.get("market_title") or row["market_ticker"]
            self.notify(f"LIVE FILL ({ev['series']}) {word}\n"
                        f"{row['market_ticker']} NO {price:.2f}")

    def cancel_event(self, ev: Dict[str, Any], reason: str = "") -> None:
        ticker = ev["event_ticker"]
        fresh = store.get_event(ticker) or ev
        if fresh.get("cancelled_at"):
            return
        mode = config.normalize_mode(fresh.get("mode") or ev.get("mode"))
        now = clock.now_utc()
        live_errors = 0

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

        rows = [r for r in store.orders_for_dashboard(limit=20000)
                if r["event_ticker"] == ticker]
        st = analytics.stats(rows)
        store.log_line("info", f"[{ev.get('series')}/{mode}] cancelled {ticker}: "
                               f"{n} pulled ({reason})")

        if mode not in config.PLACING_MODES and not rows:
            self.notify(f"EVENT CLOSED ({ev.get('series')}) [{mode}]\n{ticker}\n"
                        f"Reason: {reason}\nRecorded only, no orders.")
            return

        self.notify(
            f"CANCELLED ({ev.get('series')}) [{mode}]\n{ticker}\n"
            f"Reason: {reason}\n"
            f"Clock was: {fresh.get('cancel_source') or 'none'}\n"
            f"Orders pulled: {n}\n"
            f"Filled: {st['fills']} of {st['orders']} ({clock.pct(st['fill_rate'])})"
            + (f"\nLive cancel errors: {live_errors}" if live_errors else ""))

    def sample_ticks(self, ev: Dict[str, Any], markets: List[Dict[str, Any]]) -> None:
        key = ev["event_ticker"]
        if _time.time() - self._last_tick_log.get(key, 0.0) < config.TICK_LOG_SECONDS:
            return
        self._last_tick_log[key] = _time.time()
        for m in markets:
            try:
                store.record_tick({
                    "ts": clock.now_utc(), "series": ev["series"],
                    "event_ticker": key, "market_ticker": m.get("ticker"),
                    "yes_bid": kalshi.quote(m, "yes", "bid"),
                    "yes_ask": kalshi.quote(m, "yes", "ask"),
                    "no_bid": kalshi.quote(m, "no", "bid"),
                    "no_ask": kalshi.no_ask(m),
                    "last": kalshi.last_price(m), "volume": m.get("volume"),
                })
            except Exception:
                pass

    def kill_switch(self) -> None:
        families = {c["family"] for c in config.series_config().values()}
        for fam in sorted(families):
            for mode in (config.MODE_LIVE, config.MODE_DRY):
                k = analytics.kill_check(fam, mode)
                if k["state"] in ("no data", "ok", "watch"):
                    continue
                key = f"kill:{fam}:{mode}:{k['state']}"
                if store.get_state(key):
                    continue
                store.set_state(key, clock.now_utc().isoformat())
                body = (f"{fam} [{mode}]\n"
                        f"P(No|filled) {clock.pct(k['p_no'], 1)} vs breakeven "
                        f"{k['breakeven']:.2f}\n"
                        f"Over the last {k['n']} settled fills.")
                if k["state"] == "alert":
                    self.notify(f"KILL SWITCH WARNING\n{body}\n"
                                f"Watching. Reverts at {config.KILL_REVERT_FILLS} "
                                f"fills if it stays under.")
                    store.log_line("warn", f"kill alert {fam}/{mode}")
                elif k["state"] == "revert" and mode == config.MODE_LIVE:
                    demoted = [s for s, c in config.series_config().items()
                               if c["family"] == fam and c["mode"] == config.MODE_LIVE]
                    for s in demoted:
                        store.set_series_mode(s, config.MODE_LOG)
                    self.notify(f"KILL SWITCH TRIPPED\n{body}\n"
                                f"Reverted to LOG: {', '.join(demoted) or 'none'}\n"
                                f"Open events keep their locked mode and their "
                                f"cancel timers. Prices unchanged.")
                    store.log_line("error", f"kill revert {fam}: {demoted}")
                elif k["state"] == "revert":
                    self.notify(f"KILL SWITCH (DRY, no action)\n{body}\n"
                                f"Nothing reverted -- this family is not live.")

    def run_once(self, allow_place: bool = True) -> None:
        if allow_place:
            self.discover()
        now_s = _time.time()
        for ev in store.live_events():
            key = ev["event_ticker"]
            if now_s - self._event_polled.get(key, 0.0) < self._poll_interval(ev):
                continue
            self._event_polled[key] = now_s
            try:
                if allow_place:
                    self.poll_event(ev)
                else:
                    self.poll_event_paused(ev)
            except Exception as exc:
                log.exception("poll_event failed for %s", key)
                store.log_line("error", f"poll failed {key}: {exc}")

        if now_s - self._last_kill_check >= KILL_CHECK_SECONDS:
            self._last_kill_check = now_s
            try:
                self.kill_switch()
            except Exception as exc:
                log.warning("Kill switch check failed: %s", exc)

    def poll_event_paused(self, ev: Dict[str, Any]) -> None:
        mode = config.normalize_mode(ev.get("mode"))
        cancel_at = ev.get("cancel_at")
        if cancel_at and clock.to_utc(cancel_at) <= clock.now_utc():
            self.cancel_event(ev, reason="cancel time reached (paused)")
            return
        if mode not in config.PLACING_MODES:
            return
        try:
            markets = self.client.get_markets(event_ticker=ev["event_ticker"])
        except Exception:
            return
        if markets:
            self.check_fills(ev, markets, mode)
