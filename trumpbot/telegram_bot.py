"""Telegram. This is the only control surface.

The public Streamlit page is read-only on purpose -- anyone can open it, so
Pause, Cancel and Place must not live there.

Chat id is your own user id: message the bot once, then call getUpdates.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import timedelta
from typing import Optional

import requests

from . import clock, config, settle, store

log = logging.getLogger("trumpbot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> Optional[str]:
    t = config.get("TELEGRAM_TOKEN")
    return str(t) if t else None


def _chat_id() -> Optional[str]:
    c = config.get("TELEGRAM_CHAT_ID")
    return str(c) if c else None


def send(message: str) -> None:
    token, chat = _token(), _chat_id()
    if not token or not chat:
        log.info("Telegram not configured, message dropped: %s", message[:80])
        return
    try:
        requests.post(API.format(token=token, method="sendMessage"),
                      json={"chat_id": chat, "text": message,
                            "disable_web_page_preview": True},
                      timeout=15)
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ----------------------------------------------------------------- reports ---

def status_text() -> str:
    dry = config.dry_run()
    paused = store.get_state("paused", "0") == "1"
    last_poll = store.get_state("last_poll")
    started = store.get_state("started_at")

    live = [e for e in store.live_events()]
    resting = store.resting_orders()
    rows = store.orders_for_dashboard(limit=3000)
    today = clock.ct_date(clock.now_utc())
    todays = [r for r in rows if clock.ct_date(r.get("placed_at")) == today]
    fills_today = [r for r in todays if r.get("status") == "filled"]

    lines = [
        f"Mode: {'DRY RUN' if dry else 'LIVE'}{'  (PAUSED)' if paused else ''}",
        f"Started: {clock.fmt_ct(clock.parse_iso(started))}",
        f"Last poll: {clock.fmt_ct(clock.parse_iso(last_poll))}",
        f"Storage: postgres",
        "",
    ]

    for series, cfg in config.series_config().items():
        mine = [e for e in live if e["series"] == series]
        my_rest = [r for r in resting if r["series"] == series]
        flag = "on" if cfg.get("enabled") else "OFF"
        lines.append(f"{series} [{flag}] NO {float(cfg['rest_price']):.2f} "
                     f"x {config.contracts_for(cfg):g} (${float(cfg['dollars']):.2f})")
        lines.append(f"  Open events: {len(mine)}   Resting orders: {len(my_rest)}")
        for e in sorted(mine, key=lambda x: (x.get("cancel_at") or clock.now_utc())):
            lines.append(f"  - {e['event_ticker']}  {e.get('markets_seen') or 0} mkts  "
                         f"cancel {clock.fmt_ct(e.get('cancel_at'))}")
        lines.append("")

    nxt = sorted([e for e in live if e.get("cancel_at")],
                 key=lambda x: x["cancel_at"])
    if nxt:
        secs = (clock.to_utc(nxt[0]["cancel_at"]) - clock.now_utc()).total_seconds()
        lines.append(f"Next cancel: {nxt[0]['event_ticker']} in {clock.human_delta(secs)}")
    else:
        lines.append("Next cancel: nothing resting")

    rate = (len(fills_today) / len(todays)) if todays else None
    lines.append(f"Today: {len(todays)} orders, {len(fills_today)} fills ({clock.pct(rate)})")
    return "\n".join(lines)


def today_text() -> str:
    rows = store.orders_for_dashboard(limit=3000)
    days = settle.day_clustered(rows)[:7]
    if not days:
        return "No orders yet."
    out = ["Day-clustered (CT):"]
    for d in days:
        settled = d["settled"]
        pno = (d["no_wins"] / settled) if settled else None
        fr = (d["fills"] / d["orders"]) if d["orders"] else None
        out.append(f"{d['day']}  {d['orders']} ord, {d['fills']} fills ({clock.pct(fr)}), "
                   f"P(No|filled) {clock.pct(pno)}, P/L ${d['pnl']:.2f}")
        for s, v in sorted(d["by_series"].items()):
            out.append(f"   {s}: {v['fills']}/{v['orders']} fills, ${v['pnl']:.2f}")
    return "\n".join(out)


def events_text() -> str:
    evs = store.all_events(limit=25)
    if not evs:
        return "No events logged yet."
    out = ["Recent events seen (traded or not):"]
    for e in evs:
        state = "cancelled" if e.get("cancelled_at") else "open"
        out.append(f"{e['series']} {e['event_ticker']} [{state}] "
                   f"{e.get('markets_seen') or 0} mkts, "
                   f"{e.get('orders_placed') or 0} orders, "
                   f"cancel {clock.fmt_ct(e.get('cancel_at'))}")
    return "\n".join(out)


def _handle_when(arg: str) -> None:
    if not arg:
        send("Usage: /when KXTRUMPMENTION-26AUG30 8:00 PM central")
        return
    tokens = arg.replace(",", " ").split()
    ticker = None
    other = []
    for tok in tokens:
        up = tok.strip().upper()
        if up.startswith("KX") or "TRUMPMENTION" in up:
            ticker = re.sub(r"[.,;]+$", "", up)
        else:
            other.append(tok)
    if not ticker:
        send("Need an event ticker, e.g. /when KXTRUMPMENTION-26AUG30 8:00 PM central")
        return
    ev = store.get_event(ticker)
    if not ev:
        hits = [e for e in store.all_events(limit=200)
                if e["event_ticker"].upper() == ticker
                or e["event_ticker"].upper().endswith("-" + ticker)]
        if len(hits) == 1:
            ev = hits[0]
            ticker = ev["event_ticker"]
        else:
            send(f"No event called {ticker}.")
            return
    day = clock.date_from_ticker(ticker)
    if day is None:
        send(f"Could not read a date out of {ticker}.")
        return
    show, err = clock.parse_when_clock(" ".join(other), day)
    if err or show is None:
        send(f"Could not parse time. {err or ''}".strip())
        return
    series = ev.get("series") or ""
    cfg = config.series_config().get(series) or {}
    buffer_min = int(cfg.get("buffer_min") or 5)
    cancel_at = show - timedelta(minutes=buffer_min)
    store.mark_event(ticker,
                     cancel_at=cancel_at,
                     cancel_source="telegram /when",
                     occurrence_at=show)
    store.log_line("info", f"{ticker}: /when show {clock.fmt_ct(show)}, "
                           f"cancel {clock.fmt_ct(cancel_at)}")
    secs = (clock.to_utc(cancel_at) - clock.now_utc()).total_seconds()
    page = ""
    try:
        from . import kalshi
        page = kalshi.event_page_url(series, ev.get("title"), ticker)
    except Exception:
        page = ""
    send(
        f"WHEN set ({series})\n{ticker}\n"
        + (f"{page}\n" if page else "")
        + f"Event time: {clock.fmt_ct(show)}\n"
        + f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {buffer_min}m)\n"
        + f"In: {clock.human_delta(secs)}\n"
        + "Resting orders were not moved."
    )
    if cancel_at <= clock.now_utc():
        send(f"{ticker}: that cancel time is already in the past. "
             f"Use /cancel {ticker} if you want orders pulled now.")


HELP = """Commands:
/status  - mode, open events per series, resting orders, next cancel, fills today
/today   - day-clustered fills and P/L, last 7 days
/events  - recent events seen, traded or not
/pause   - stop placing and cancelling (polling continues)
/resume  - start again
/cancel EVENT_TICKER - pull all orders for one event now
/when EVENT_TICKER 8:00 PM central - set show time; cancel is that minus buffer
/help"""


# ---------------------------------------------------------------- listener ---

class Listener(threading.Thread):
    def __init__(self, engine=None):
        super().__init__(daemon=True, name="telegram")
        self.engine = engine
        self.offset = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        token = _token()
        if not token:
            log.info("No Telegram token, listener not started")
            return
        while not self._stop.is_set():
            try:
                resp = requests.get(API.format(token=token, method="getUpdates"),
                                    params={"offset": self.offset + 1, "timeout": 25},
                                    timeout=40)
                for upd in resp.json().get("result", []):
                    self.offset = max(self.offset, upd["update_id"])
                    self.handle(upd)
            except Exception as exc:
                log.debug("Telegram poll: %s", exc)
                time.sleep(3)

    def handle(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id") or "")
        if not text:
            return
        if _chat_id() and chat != _chat_id():
            return

        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()

        try:
            if cmd in ("/start", "/help"):
                send(HELP)
            elif cmd == "/status":
                send(status_text())
            elif cmd == "/today":
                send(today_text())
            elif cmd == "/events":
                send(events_text())
            elif cmd == "/pause":
                store.set_state("paused", "1")
                send("Paused. No new orders. Cancels and fill-watching still run.")
            elif cmd == "/resume":
                store.set_state("paused", "0")
                send("Resumed.")
            elif cmd == "/when":
                _handle_when(arg)
            elif cmd == "/cancel":
                if not arg:
                    send("Give an event ticker: /cancel KXTRUMPMENTIONB-26AUG29")
                    return
                ev = store.get_event(arg)
                if not ev:
                    send(f"No event called {arg}.")
                    return
                if self.engine:
                    self.engine.cancel_event(ev, reason="manual /cancel")
                else:
                    send("Engine not attached.")
            else:
                send(HELP)
        except Exception as exc:
            log.exception("Telegram command failed")
            send(f"Command failed: {exc}")