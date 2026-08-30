"""Telegram. This is the only control surface.

The public Streamlit page is read-only on purpose -- anyone can open it, so
Pause, Cancel, Place and Mode must not live there.

Chat id is your own user id: message the bot once, then call getUpdates.
"""
from __future__ import annotations

import logging
import random
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


def _resolve_series(name: str) -> Optional[str]:
    """Accept the full ticker or a unique suffix, case-insensitive."""
    want = (name or "").strip().upper()
    if not want:
        return None
    keys = list(config.series_config().keys())
    if want in keys:
        return want
    hits = [k for k in keys if k.upper().endswith(want) or want in k.upper()]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------- reports ---

def status_text() -> str:
    paused = store.get_state("paused", "0") == "1"
    last_poll = store.get_state("last_poll")
    started = store.get_state("started_at")

    live = store.live_events()
    resting = store.resting_orders()
    rows = store.orders_for_dashboard(limit=3000)
    today = clock.ct_date(clock.now_utc())
    todays = [r for r in rows if clock.ct_date(r.get("placed_at")) == today]
    fills_today = [r for r in todays if r.get("status") == "filled"]

    lines = [
        f"Worker: {'PAUSED' if paused else 'running'}",
        f"Started: {clock.fmt_ct(clock.parse_iso(started))}",
        f"Last poll: {clock.fmt_ct(clock.parse_iso(last_poll))}",
        "Storage: postgres",
        "",
    ]

    needs_time = []
    for series, cfg in config.series_config().items():
        mode = config.mode_for(series)
        mine = [e for e in live if e["series"] == series]
        my_rest = [r for r in resting if r["series"] == series]
        lines.append(f"{series} [{mode}] NO {float(cfg['rest_price']):.2f} "
                     f"x {config.contracts_for(cfg):g} (${float(cfg['dollars']):.2f})")
        lines.append(f"  Open events: {len(mine)}   Resting orders: {len(my_rest)}")
        for e in sorted(mine, key=lambda x: (x.get("cancel_at") or clock.now_utc())):
            src = str(e.get("cancel_source") or "none")
            trusted = src.lower().startswith(("telegram", "milestone"))
            mark = "" if trusted else "  << needs /when"
            if not trusted:
                needs_time.append(e["event_ticker"])
            lines.append(f"  - {e['event_ticker']} [{e.get('mode') or '?'}] "
                         f"{e.get('markets_seen') or 0} mkts  "
                         f"cancel {clock.fmt_ct(e.get('cancel_at'))} ({src}){mark}")
        lines.append("")

    nxt = sorted([e for e in live if e.get("cancel_at")], key=lambda x: x["cancel_at"])
    if nxt:
        secs = (clock.to_utc(nxt[0]["cancel_at"]) - clock.now_utc()).total_seconds()
        lines.append(f"Next cancel: {nxt[0]['event_ticker']} in {clock.human_delta(secs)}")
    else:
        lines.append("Next cancel: nothing resting")

    rate = (len(fills_today) / len(todays)) if todays else None
    lines.append(f"Today: {len(todays)} orders, {len(fills_today)} fills ({clock.pct(rate)})")
    if needs_time:
        lines.append(f"\n{len(needs_time)} event(s) have no real showtime.")
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
        st = "cancelled" if e.get("cancelled_at") else "open"
        first = "first-list" if e.get("discovered_at_open") else "mid-event"
        out.append(f"{e['series']} [{e.get('mode') or '?'}] {e['event_ticker']} [{st}] "
                   f"{e.get('markets_seen') or 0} mkts, "
                   f"{e.get('orders_placed') or 0} orders, {first}, "
                   f"cancel {clock.fmt_ct(e.get('cancel_at'))} "
                   f"({e.get('cancel_source') or 'none'})")
    return "\n".join(out)


def modes_text() -> str:
    out = ["Series modes:"]
    for series, cfg in config.series_config().items():
        mode = config.mode_for(series)
        src = "db" if store.get_series_mode(series) else "config"
        out.append(f"  {series}: {mode} ({src})  "
                   f"NO {float(cfg['rest_price']):.2f} x {config.contracts_for(cfg):g}")
    out.append("")
    out.append("LIVE = real money   DRY = paper fills from the book")
    out.append("LOG  = record only  OFF = ignore the series")
    out.append("A change applies to the NEXT event discovered. Events already "
               "open keep the mode they started under.")
    out.append("Usage: /mode KXTRUMPMENTIONB DRY")
    return "\n".join(out)


# -------------------------------------------------------------------- mode ---

def _handle_mode(arg: str) -> None:
    parts = arg.split()
    if not parts:
        send(modes_text())
        return

    series = _resolve_series(parts[0])
    if not series:
        send(f"No series matching '{parts[0]}'.\n\n" + modes_text())
        return

    if len(parts) == 1:
        mode = config.mode_for(series)
        src = "db" if store.get_series_mode(series) else "config"
        send(f"{series}: {mode} ({src})")
        return

    want = parts[1].strip().upper()
    if want not in config.MODES:
        send(f"Mode must be one of: {', '.join(config.MODES)}")
        return

    current = config.mode_for(series)
    cfg = config.series_config().get(series, {})
    open_now = [e for e in store.live_events() if e["series"] == series]

    if want == current:
        send(f"{series} is already {current}.")
        return

    # Anything entering LIVE needs a second message.
    if want == config.MODE_LIVE:
        key = f"pending_live:{series}"
        if len(parts) >= 3:
            code = parts[2].strip()
            saved = store.get_state(key) or ""
            want_code, _, when = saved.partition("|")
            issued = clock.parse_iso(when)
            age = (clock.now_utc() - issued).total_seconds() if issued else 1e9
            if not saved or code != want_code:
                send("Wrong or expired code. Send /mode "
                     f"{series} LIVE again to get a new one.")
                return
            if age > config.LIVE_CONFIRM_TTL:
                store.delete_state(key)
                send("That code expired. Send /mode "
                     f"{series} LIVE again for a new one.")
                return
            store.delete_state(key)
            store.set_series_mode(series, config.MODE_LIVE)
            send(f"{series} is now LIVE.\n"
                 f"Price {float(cfg['rest_price']):.2f}, "
                 f"size {config.contracts_for(cfg):g} "
                 f"(${float(cfg['dollars']):.2f}/market) -- unchanged.\n"
                 f"Applies to the next event discovered. "
                 f"{len(open_now)} open event(s) keep their current mode.")
            return

        code = f"{random.randint(1000, 9999)}"
        store.set_state(key, f"{code}|{clock.now_utc().isoformat()}")
        send(
            f"CONFIRM LIVE for {series}\n\n"
            f"This places REAL orders with REAL money.\n"
            f"Price: NO {float(cfg['rest_price']):.2f}\n"
            f"Size: {config.contracts_for(cfg):g} contracts "
            f"(${float(cfg['dollars']):.2f}) per market\n"
            f"Every market in every new event of this series.\n"
            f"Price and size do not change when you go live.\n\n"
            f"Currently {current}. {len(open_now)} open event(s) stay as they are.\n\n"
            f"Send within {config.LIVE_CONFIRM_TTL}s:\n"
            f"/mode {series} LIVE {code}"
        )
        return

    store.set_series_mode(series, want)
    note = (f"\n{len(open_now)} open event(s) keep their current mode and their "
            f"cancel timers." if open_now else "")
    if want == config.MODE_OFF and open_now:
        note += "\nResting orders on those events will still be cancelled on time."
    send(f"{series}: {current} -> {want}.\nApplies to the next event discovered.{note}")


# -------------------------------------------------------------------- when ---

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

    fields = {"cancel_at": cancel_at,
              "cancel_source": "telegram /when",
              "occurrence_at": show}

    # Re-arm an event we closed out for having no clock, but only if it never
    # placed anything and the new time is still ahead of us.
    revived = False
    if ev.get("cancelled_at") and not (ev.get("orders_placed") or 0) \
            and cancel_at > clock.now_utc():
        fields["cancelled_at"] = None
        fields["notified_cancel"] = False
        revived = True

    store.mark_event(ticker, **fields)
    store.log_line("info", f"{ticker}: /when show {clock.fmt_ct(show)}, "
                           f"cancel {clock.fmt_ct(cancel_at)}")
    secs = (clock.to_utc(cancel_at) - clock.now_utc()).total_seconds()
    try:
        from . import kalshi
        page = kalshi.event_page_url(series, ev.get("title"), ticker)
    except Exception:
        page = ""
    send(
        f"WHEN set ({series}) [{ev.get('mode') or '?'}]\n{ticker}\n"
        + (f"{page}\n" if page else "")
        + f"Event time: {clock.fmt_ct(show)}\n"
        + f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {buffer_min}m)\n"
        + f"In: {clock.human_delta(secs)}\n"
        + "Resting orders were not moved."
        + ("\nEvent re-armed. Orders go out on the next poll." if revived else "")
    )
    if cancel_at <= clock.now_utc():
        send(f"{ticker}: that cancel time is already in the past. "
             f"Use /cancel {ticker} if you want orders pulled now.")


HELP = """Commands:
/status  - modes, open events, resting orders, next cancel, fills today
/mode    - show series modes; /mode SERIES LIVE|DRY|LOG|OFF to change
/today   - day-clustered fills and P/L, last 7 days
/events  - recent events seen, traded or not
/when EVENT_TICKER 8:00 PM central - set show time; cancel is that minus buffer
/pause   - stop placing (cancels and fill-watching keep running)
/resume  - start again
/cancel EVENT_TICKER - pull all orders for one event now
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
            elif cmd == "/mode":
                _handle_mode(arg)
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
                ev = store.get_event(arg.strip())
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
