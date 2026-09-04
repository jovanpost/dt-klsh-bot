"""Telegram. The only control surface.

The public Streamlit page is read-only on purpose -- anyone can open it, so
Pause, Cancel, Place and Mode must not live there.

Chat id is your own user id: message the bot once, then call getUpdates.
This bot must have its own token, separate from the WNT bot -- sharing one
sent /status to the wrong bot once already.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

import requests

from . import analytics, clock, config, store

log = logging.getLogger("trumpbot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3800


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
    chunks = [message[i:i + MAX_LEN] for i in range(0, len(message), MAX_LEN)] or [""]
    for c in chunks:
        try:
            requests.post(API.format(token=token, method="sendMessage"),
                          json={"chat_id": chat, "text": c,
                                "disable_web_page_preview": True},
                          timeout=15)
        except Exception as exc:
            log.warning("Telegram send failed: %s", exc)


def log_notify_on() -> bool:
    return store.get_state("notify_log", "0") == "1"


def fills_notify_on() -> bool:
    """Per-fill Telegram. Default muted — missing key means off."""
    return store.get_state("notify_fills", "0") == "1"


def notify_summary() -> str:
    fills = "on" if fills_notify_on() else "muted"
    logs = "on" if log_notify_on() else "muted"
    return f"Telegram: fills {fills}, LOG {logs}"


def _resolve_series(name: str) -> Optional[str]:
    want = (name or "").strip().upper()
    if not want:
        return None
    keys = list(config.series_config().keys())
    if want in keys:
        return want
    hits = [k for k in keys if k.upper().endswith(want) or want in k.upper()]
    return hits[0] if len(hits) == 1 else None


def _resolve_family(name: str) -> Optional[str]:
    want = (name or "").strip().upper()
    return want if want in config.FAMILY_DEFAULTS else None


def _orders_and_events():
    rows = store.orders_for_dashboard(limit=20000)
    events = store.all_events(limit=2000)
    return rows, events, analytics.event_index(events)


def status_text() -> str:
    paused = store.get_state("paused", "0") == "1"
    live = store.live_events()
    resting = store.resting_orders()
    rows, events, idx = _orders_and_events()
    today = clock.ct_date(clock.now_utc())
    todays = [r for r in rows if clock.ct_date(r.get("placed_at")) == today]
    fills_today = [r for r in todays if r.get("status") == "filled"]

    cfgs = config.series_config()
    by_mode: Dict[str, int] = {}
    for c in cfgs.values():
        by_mode[c["mode"]] = by_mode.get(c["mode"], 0) + 1

    lines = [
        f"Worker: {'PAUSED' if paused else 'running'}",
        notify_summary(),
        f"Last poll: {clock.fmt_ct(clock.parse_iso(store.get_state('last_poll')))}",
        f"Series: {len(cfgs)}  (" + ", ".join(f"{k} {v}" for k, v in sorted(by_mode.items())) + ")",
        f"Open events: {len(live)}   Resting orders: {len(resting)}",
        "",
        "BY FAMILY (first-list only)",
    ]

    for fam in sorted({c["family"] for c in cfgs.values()}):
        members = [s for s, c in cfgs.items() if c["family"] == fam]
        modes = {}
        for s in members:
            modes[cfgs[s]["mode"]] = modes.get(cfgs[s]["mode"], 0) + 1
        frows = analytics.filter_orders(rows, family=fam, first_list_only=True,
                                        events_by_ticker=idx)
        fs = analytics.stats(frows)
        if not members:
            continue
        lines.append(f"{fam}: {len(members)} series ("
                     + " ".join(f"{k}:{v}" for k, v in sorted(modes.items())) + ")")
        if fs["orders"]:
            cush = ("--" if fs["cushion"] is None
                    else f"{100 * fs['cushion']:+.1f}pts")
            lines.append(f"   {fs['orders']} ord, fills {clock.pct(fs['fill_rate'])}, "
                         f"P(No|f) {clock.pct(fs['p_no'], 1)}, cushion {cush}, "
                         f"P/L ${fs['pnl']:.2f}")

    lines.append("")
    lines.append("OPEN EVENTS")
    needs = []
    for e in sorted(live, key=lambda x: (x.get("cancel_at") or clock.now_utc()))[:15]:
        src = str(e.get("cancel_source") or "none")
        trusted = src.lower().startswith(("telegram", "milestone"))
        if not trusted:
            needs.append(e["event_ticker"])
        secs = ((clock.to_utc(e["cancel_at"]) - clock.now_utc()).total_seconds()
                if e.get("cancel_at") else None)
        lines.append(f"  {e['series']} [{e.get('mode')}] {e['event_ticker']}  "
                     f"{e.get('markets_seen') or 0} mkts, "
                     f"{len([r for r in resting if r['event_ticker'] == e['event_ticker']])} resting, "
                     f"cancel {clock.fmt_ct_short(e.get('cancel_at'))} "
                     f"in {clock.human_delta(secs)} ({src})"
                     + ("  << needs /when" if not trusted else ""))
    if len(live) > 15:
        lines.append(f"  ...and {len(live) - 15} more")

    rate = (len(fills_today) / len(todays)) if todays else None
    lines.append("")
    lines.append(f"Today: {len(todays)} orders, {len(fills_today)} fills "
                 f"({clock.pct(rate)})")
    if needs:
        lines.append(f"{len(needs)} event(s) on a fallback clock.")
    return "\n".join(lines)


def ready_text(target: Optional[str] = None) -> str:
    rows, events, idx = _orders_and_events()
    cfgs = config.series_config()
    picks = [target] if target else [s for s, c in cfgs.items()
                                     if c["mode"] == config.MODE_DRY]
    out = []
    for s in sorted(picks):
        cfg = cfgs.get(s)
        if not cfg:
            continue
        srows = analytics.filter_orders(rows, series=s, mode=cfg["mode"],
                                        first_list_only=True, events_by_ticker=idx)
        rd = analytics.readiness(s, srows, events)
        smoke = "SMOKE ok" if rd.get("smoke_ready") else "smoke no"
        size = "SIZE ok" if rd.get("size_ready") else "size no"
        tag = "SIX" if rd.get("is_six") else "fam"
        out.append(f"{s} [{rd['mode']}/{rd['family']}/{tag}] "
                   f"{rd['passed']}/{rd['total']} {smoke} / {size}")
        if target:
            out.append(f"   path: {rd.get('path')}")
            out.append("   $0.25 smoke gates:")
            for g in rd["gates"]:
                out.append(f"   {'OK ' if g['ok'] else '.. '}{g['name']}: "
                           f"{g['have']} / {g['need']}")
            out.append("   family size-up (real dollars):")
            for g in rd.get("family_gates") or []:
                out.append(f"   {'OK ' if g['ok'] else '.. '}{g['name']}: "
                           f"{g['have']} / {g['need']}")
    if not out:
        return "No DRY series."
    out.append("")
    out.append("SMOKE ok = talk $0.25 LIVE on that ticker only.")
    out.append("SIZE ok = family tape allows real size. Smoke never implies size.")
    out.append("/mode SERIES LIVE still needs the confirm code.")
    return "\n".join(out)


def logstatus_text() -> str:
    counts = analytics.log_family_counts()
    due = {d["family"]: d for d in analytics.log_reviews_due()}
    out = ["LOG review (research chat, not /mode DRY):"]
    if not counts:
        out.append("  no LOG events logged yet")
    for fam, n in sorted(counts.items()):
        mark = due.get(fam)
        extra = (f"  << due ({mark['reason']}, +{mark['added']})"
                 if mark else "")
        out.append(f"  {fam}: {n} events{extra}")
    out.append("")
    out.append(f"Ping at +{config.LOG_REVIEW_EVENTS} events or "
               f"{config.LOG_REVIEW_DAYS}d with "
               f"+{config.LOG_REVIEW_MIN_EVENTS}.")
    out.append("/logreviewed FAMILY after you run the backtest.")
    return "\n".join(out)


def today_text() -> str:
    rows, events, idx = _orders_and_events()
    frows = analytics.filter_orders(rows, first_list_only=False, events_by_ticker=idx)
    ds = analytics.day_summary(frows)
    if not ds["buckets"]:
        return "No orders yet."
    out = ["Day-clustered (CT):"]
    for d in ds["buckets"][:7]:
        pno = (d["no_wins"] / d["settled"]) if d["settled"] else None
        fr = (d["fills"] / d["orders"]) if d["orders"] else None
        out.append(f"{d['day']}  {d['orders']} ord, {d['fills']} fills "
                   f"({clock.pct(fr)}), P(No|f) {clock.pct(pno)}, "
                   f"P/L ${d['pnl']:.2f}")
    if ds["boot_low"] is not None:
        out.append("")
        out.append(f"Mean ${ds['mean_per_day']:.2f}/day, 95% low "
                   f"${ds['boot_low']:.2f} over {ds['settled_days']} settled days.")
    return "\n".join(out)


def events_text() -> str:
    evs = store.all_events(limit=20)
    if not evs:
        return "No events logged yet."
    out = ["Recent events seen:"]
    for e in evs:
        st = "closed" if e.get("cancelled_at") else "open"
        out.append(f"{e['series']} [{e.get('mode')}] {e['event_ticker']} [{st}] "
                   f"{e.get('markets_seen') or 0} mkts, "
                   f"{e.get('orders_placed') or 0} ord, "
                   f"{'first-list' if e.get('discovered_at_open') else 'mid-event'}, "
                   f"{e.get('cancel_source') or 'no clock'}")
    return "\n".join(out)


def series_text(family: Optional[str] = None) -> str:
    cfgs = config.series_config()
    out = [f"Series ({family or 'all'}):"]
    for s, c in sorted(cfgs.items()):
        if family and c["family"] != family:
            continue
        price = "--" if c.get("rest_price") is None else f"{float(c['rest_price']):.2f}"
        size = config.contracts_for(c)
        out.append(f"  {s} [{c['mode']}/{c['family']}] NO {price} "
                   f"x {'--' if size is None else f'{size:g}'} "
                   f"(${float(c['dollars']):.2f})")
    return "\n".join(out)


def kill_text() -> str:
    out = ["Kill switch:"]
    for fam in sorted({c["family"] for c in config.series_config().values()}):
        for m in (config.MODE_LIVE, config.MODE_DRY):
            k = analytics.kill_check(fam, m)
            if k["n"] == 0:
                continue
            out.append(f"  {fam} [{m}] {k['n']} fills, "
                       f"P(No|f) {clock.pct(k['p_no'], 1)} vs "
                       f"{k['breakeven']:.2f} -> {k['state']}")
    if len(out) == 1:
        out.append("  no settled fills yet")
    out.append(f"Warns at {config.KILL_ALERT_FILLS}, reverts LIVE to LOG "
               f"at {config.KILL_REVERT_FILLS}.")
    return "\n".join(out)


def _apply_mode(targets: List[str], want: str, label: str) -> None:
    cfgs = config.series_config()
    changed = []
    for s in targets:
        if cfgs[s]["mode"] != want:
            store.set_series_mode(s, want)
            changed.append(s)
    open_now = [e for e in store.live_events() if e["series"] in targets]
    if not changed:
        send(f"{label}: already {want}.")
        return
    note = ""
    if open_now:
        note = (f"\n{len(open_now)} open event(s) keep the mode they started "
                f"under and their cancel timers.")
        if want == config.MODE_OFF:
            note += "\nTheir resting orders will still be cancelled on time."
    send(f"{label} -> {want}\nChanged: {', '.join(changed)}\n"
         f"Applies to the next event discovered.{note}")


def _handle_mode(arg: str) -> None:
    parts = arg.split()
    if not parts:
        send(series_text())
        return

    fam = _resolve_family(parts[0])
    series = None if fam else _resolve_series(parts[0])
    if not fam and not series:
        send(f"No series or family matching '{parts[0]}'.\n"
             f"Families: {', '.join(config.FAMILIES)}")
        return

    targets = (list(config.series_in_family(fam).keys()) if fam else [series])
    label = fam or series

    if len(parts) == 1:
        cfgs = config.series_config()
        send("\n".join(f"{s}: {cfgs[s]['mode']}" for s in targets))
        return

    want = parts[1].strip().upper()
    if want not in config.MODES:
        send(f"Mode must be one of: {', '.join(config.MODES)}")
        return

    if want != config.MODE_LIVE:
        _apply_mode(targets, want, label)
        return

    cfgs = config.series_config()
    key = f"pending_live:{label}"
    if len(parts) >= 3:
        saved = store.get_state(key) or ""
        want_code, _, when = saved.partition("|")
        issued = clock.parse_iso(when)
        age = (clock.now_utc() - issued).total_seconds() if issued else 1e9
        if not saved or parts[2].strip() != want_code:
            send(f"Wrong or expired code. Send /mode {label} LIVE again.")
            return
        if age > config.LIVE_CONFIRM_TTL:
            store.delete_state(key)
            send(f"That code expired. Send /mode {label} LIVE again.")
            return
        store.delete_state(key)
        _apply_mode(targets, config.MODE_LIVE, label)
        return

    untradeable = [s for s in targets if not config.tradeable(cfgs[s])]
    if untradeable:
        send(f"{label} cannot go LIVE: no price set on "
             f"{', '.join(untradeable)}. Set rest_price in tm_series first.")
        return

    code = f"{random.randint(1000, 9999)}"
    store.set_state(key, f"{code}|{clock.now_utc().isoformat()}")
    detail = "\n".join(
        f"  {s}: NO {float(cfgs[s]['rest_price']):.2f} x "
        f"{config.contracts_for(cfgs[s]):g} (${float(cfgs[s]['dollars']):.2f}/market)"
        for s in targets)
    send(f"CONFIRM LIVE for {label}\n\n"
         f"REAL orders with REAL money on every market of every new event.\n"
         f"{detail}\n\n"
         f"Price and size do not change when you go live.\n"
         f"First live test should be one tiny order plus a cancel smoke test, "
         f"not a full event.\n\n"
         f"Send within {config.LIVE_CONFIRM_TTL}s:\n/mode {label} LIVE {code}")


def _handle_when(arg: str) -> None:
    if not arg:
        send("Usage: /when KXTRUMPMENTION-26AUG30 8:00 PM central")
        return
    tokens = arg.replace(",", " ").split()
    ticker, other = None, []
    for tok in tokens:
        up = tok.strip().upper()
        if up.startswith("KX") or "-" in up and any(ch.isdigit() for ch in up):
            ticker = re.sub(r"[.,;]+$", "", up)
        else:
            other.append(tok)
    if not ticker:
        send("Need an event ticker, e.g. /when KXTRUMPMENTION-26AUG30 8:00 PM central")
        return
    ev = store.get_event(ticker)
    if not ev:
        hits = [e for e in store.all_events(limit=500)
                if e["event_ticker"].upper() == ticker
                or e["event_ticker"].upper().endswith("-" + ticker)]
        if len(hits) == 1:
            ev, ticker = hits[0], hits[0]["event_ticker"]
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
    cfg = config.series_cfg(series) or {}
    buffer_min = int(cfg.get("buffer_min") or 5)
    cancel_at = show - timedelta(minutes=buffer_min)

    fields: Dict[str, Any] = {"cancel_at": cancel_at,
                              "cancel_source": "telegram /when",
                              "occurrence_at": show}
    revived = False
    if ev.get("cancelled_at") and not (ev.get("orders_placed") or 0) \
            and cancel_at > clock.now_utc():
        fields["cancelled_at"] = None
        fields["notified_cancel"] = False
        revived = True

    store.mark_event(ticker, **fields)
    store.log_line("info", f"{ticker}: /when {clock.fmt_ct(show)}, "
                           f"cancel {clock.fmt_ct(cancel_at)}")
    secs = (clock.to_utc(cancel_at) - clock.now_utc()).total_seconds()
    try:
        from . import kalshi
        page = kalshi.event_page_url(series, ev.get("title"), ticker)
    except Exception:
        page = ""
    send(f"WHEN set ({series}) [{ev.get('mode') or '?'}]\n{ticker}\n"
         + (f"{page}\n" if page else "")
         + f"Event time: {clock.fmt_ct(show)}\n"
         + f"Cancel at: {clock.fmt_ct(cancel_at)} (event minus {buffer_min}m)\n"
         + f"In: {clock.human_delta(secs)}\n"
         + "Resting orders were not moved."
         + ("\nEvent re-armed. Orders go out on the next poll." if revived else ""))
    if cancel_at <= clock.now_utc():
        send(f"{ticker}: that cancel time is already past. "
             f"Use /cancel {ticker} to pull orders now.")


def _handle_mute(arg: str, on: bool) -> None:
    """FILLS is the default target so /mute with no arg quiets fill spam."""
    target = (arg or "FILLS").strip().upper()
    if target in ("FILL", "FILLS"):
        store.set_state("notify_fills", "1" if on else "0")
        if on:
            send("Fill Telegram unmuted. You will get FILL / LIVE FILL on each slice.")
        else:
            send("Fills muted. NEW EVENT, NEEDS A TIME, TIME UPDATED, "
                 "CANCELLED, SETTLED, and issues still ping.")
        return
    if target == "LOG":
        store.set_state("notify_log", "1" if on else "0")
        if on:
            send("LOG Telegram unmuted. You will get NEW EVENT on LOG series.")
        else:
            send("LOG Telegram muted. DRY/LIVE and RESEARCH REVIEW still ping.")
        return
    send("Usage: /mute FILLS | /mute LOG\n       /unmute FILLS | /unmute LOG")


HELP = """Commands:
/status  - family rollup, open events, next cancels, fills today
/ready   - $0.25 smoke + family size-up; /ready SERIES for the detail
/logstatus - LOG families: when to re-run research for DRY
/logreviewed FAMILY - mark that research review done
/mute FILLS - silence per-fill pings (default). Alias: /mute
/unmute FILLS - turn FILL / LIVE FILL back on
/mute LOG - silence NEW EVENT / TIME / CLOSED on LOG series (default)
/unmute LOG - turn those back on
/mode    - series and family modes; /mode <SERIES|FAMILY> LIVE|DRY|LOG|OFF
/series  - every series with mode, price and size; /series FAMILY to filter
/kill    - kill-switch state per family
/today   - day-clustered fills and P/L
/events  - recent events seen, traded or not
/when EVENT_TICKER 8:00 PM central - set show time; cancel is that minus buffer
/pause   - stop placing (cancels and fill-watching keep running)
/resume  - start again
/cancel EVENT_TICKER - pull all orders for one event now
/help"""


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
            elif cmd == "/ready":
                send(ready_text(_resolve_series(arg) if arg else None))
            elif cmd == "/logstatus":
                send(logstatus_text())
            elif cmd == "/logreviewed":
                fam = _resolve_family(arg) if arg else None
                if not fam:
                    send("Usage: /logreviewed EARNINGS")
                    return
                n = analytics.mark_log_reviewed(fam)
                send(f"{fam}: review marked. Baseline {n} LOG events.")
            elif cmd == "/mute":
                _handle_mute(arg, on=False)
            elif cmd == "/unmute":
                _handle_mute(arg, on=True)
            elif cmd == "/mode":
                _handle_mode(arg)
            elif cmd == "/series":
                send(series_text(_resolve_family(arg) if arg else None))
            elif cmd == "/kill":
                send(kill_text())
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
