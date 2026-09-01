"""Read-only status page.

No buttons. Anyone with the URL can open this, so every control is in Telegram.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from trumpbot import analytics, clock, config, runner, store

st.set_page_config(page_title="Mentions bot", layout="wide")

try:
    params = st.query_params
    ping = params.get("ping")
except Exception:
    params = st.experimental_get_query_params()
    ping = (params.get("ping") or [None])[0]

r = runner.get_runner()

if ping:
    st.write("ok")
    st.stop()

if r.error:
    st.error(f"Worker error: {r.error}")

try:
    started = clock.parse_iso(store.get_state("started_at"))
    last_poll = clock.parse_iso(store.get_state("last_poll"))
    paused = store.get_state("paused", "0") == "1"
    rows_all = store.orders_for_dashboard(limit=20000)
    events_all = store.all_events(limit=2000)
    series_cfg = config.series_config(force=True)
except Exception as exc:
    st.error(f"Database unavailable: {exc}")
    st.stop()

ev_index = analytics.event_index(events_all)
live_events = [e for e in events_all if not e.get("cancelled_at")]


def pct(x, digits=0):
    return clock.pct(x, digits)


def money(x):
    return "--" if x is None else f"${float(x):,.2f}"


def num(x, digits=3):
    return "--" if x is None else f"{float(x):.{digits}f}"


st.title("Mentions bot")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Worker", "Paused" if paused else ("Running" if r.is_alive() else "Stopped"))
c2.metric("Last poll", clock.fmt_ct_short(last_poll))
c3.metric("Open events", len(live_events))
c4.metric("Resting orders", len([x for x in rows_all if x.get("status") == "resting"]))
modes = config.all_modes()
c5.metric("Series", f"{len(modes)}",
          " / ".join(f"{m}:{sum(1 for v in modes.values() if v == m)}"
                     for m in config.MODES if any(v == m for v in modes.values())))

next_cancels = sorted([e for e in live_events if e.get("cancel_at")],
                      key=lambda x: x["cancel_at"])
if next_cancels:
    e = next_cancels[0]
    secs = (clock.to_utc(e["cancel_at"]) - clock.now_utc()).total_seconds()
    st.caption(f"Next cancel: {e['event_ticker']} in {clock.human_delta(secs)} "
               f"({e.get('cancel_source') or 'no clock'})")

needs_when = [e for e in live_events
              if not str(e.get("cancel_source") or "").lower()
              .startswith(("telegram", "milestone"))]
if needs_when:
    st.warning(f"{len(needs_when)} open event(s) are on a fallback clock and "
               f"need a /when: " + ", ".join(x["event_ticker"] for x in needs_when[:6]))

with st.container():
    f1, f2, f3 = st.columns([1, 1, 2])
    mode_filter = f1.selectbox("Mode", ["All"] + list(config.MODES), index=0)
    first_only = f2.checkbox("First-list events only", value=True,
                             help="Mid-event joins are a different measurement. "
                                  "Leave this on when comparing to backtest.")
    fam_options = ["All"] + sorted({c["family"] for c in series_cfg.values()})
    fam_filter = f3.selectbox("Family", fam_options, index=0)

base_rows = analytics.filter_orders(
    rows_all,
    mode=None if mode_filter == "All" else mode_filter,
    family=None if fam_filter == "All" else fam_filter,
    first_list_only=first_only,
    events_by_ticker=ev_index)

overall = analytics.stats(base_rows)
overall_day = analytics.day_summary(base_rows)

o1, o2, o3, o4, o5, o6 = st.columns(6)
o1.metric("Orders", overall["orders"])
o2.metric("Fill rate", pct(overall["fill_rate"]))
o3.metric("P(No | filled)", pct(overall["p_no"], 1))
o4.metric("Cushion", "--" if overall["cushion"] is None
          else f"{100 * overall['cushion']:+.1f} pts")
o5.metric("P/L", money(overall["pnl"]))
o6.metric("Mean $/day", money(overall_day["mean_per_day"]))

if overall_day["boot_low"] is not None:
    good = overall_day["boot_low"] > 0
    st.caption(f"Day-clustered 95% lower bound: **{money(overall_day['boot_low'])}/day** "
               f"across {overall_day['settled_days']} settled days. "
               + ("Above zero." if good else "Not above zero yet."))
else:
    st.caption(f"{overall_day['settled_days']} settled day(s). "
               f"A bootstrap lower bound needs at least 5.")

st.divider()

st.subheader("How close to LIVE")
st.caption("Every gate must pass before going live is even a conversation. "
           "Counts use first-list events only.")

ready_rows = []
detail: Dict[str, Any] = {}
for s, cfg in sorted(series_cfg.items()):
    if cfg["mode"] == config.MODE_OFF:
        continue
    srows = analytics.filter_orders(rows_all, series=s, mode=cfg["mode"],
                                    first_list_only=True,
                                    events_by_ticker=ev_index)
    rd = analytics.readiness(s, srows, events_all)
    detail[s] = rd
    ready_rows.append({
        "Series": s,
        "Family": rd["family"],
        "Mode": rd["mode"],
        "Price": num(rd["price"], 2),
        "Stake": money(rd["dollars"]),
        "Gates": f"{rd['passed']}/{rd['total']}",
        "Events": rd["events_first_list"],
        "Fills settled": rd["stats"]["settled"],
        "P(No|filled)": pct(rd["stats"]["p_no"], 1),
        "Cushion": "--" if rd["stats"]["cushion"] is None
        else f"{100 * rd['stats']['cushion']:+.1f}",
        "$/day low": num(rd["day"]["boot_low"], 2),
        "Ready": "yes" if rd["ready"] else "",
    })

if ready_rows:
    st.dataframe(pd.DataFrame(ready_rows), use_container_width=True, hide_index=True)

    pick = st.selectbox("Gate detail for", sorted(detail.keys()))
    rd = detail[pick]
    for g in rd["gates"]:
        mark = "PASS" if g["ok"] else "not yet"
        left, right = st.columns([3, 1])
        with left:
            if g["progress"] is not None:
                st.progress(g["progress"], text=f"{g['name']} — {mark}")
            else:
                st.write(f"**{g['name']}** — {mark}")
            if g["note"]:
                st.caption(g["note"])
        right.write(f"{g['have']} / {g['need']}")
else:
    st.caption("No series active.")

st.divider()

st.subheader("By family")
fam_rows = []
for fam in sorted({c["family"] for c in series_cfg.values()}):
    frows = analytics.filter_orders(rows_all, family=fam,
                                    mode=None if mode_filter == "All" else mode_filter,
                                    first_list_only=first_only,
                                    events_by_ticker=ev_index)
    fs = analytics.stats(frows)
    fd = analytics.day_summary(frows)
    famdef = config.FAMILY_DEFAULTS.get(fam, {})
    members = [s for s, c in series_cfg.items() if c["family"] == fam]
    modemix = {}
    for s in members:
        modemix[series_cfg[s]["mode"]] = modemix.get(series_cfg[s]["mode"], 0) + 1
    fam_rows.append({
        "Family": fam,
        "Series": len(members),
        "Modes": " ".join(f"{k}:{v}" for k, v in sorted(modemix.items())),
        "Base price": num(famdef.get("base_price"), 2),
        "Orders": fs["orders"],
        "Fill rate": pct(fs["fill_rate"]),
        "Exp fill": pct(famdef.get("exp_fill")),
        "P(No|filled)": pct(fs["p_no"], 1),
        "Exp P(No)": pct(famdef.get("exp_p_no"), 1),
        "Cushion": "--" if fs["cushion"] is None else f"{100 * fs['cushion']:+.1f}",
        "Settled": fs["settled"],
        "P/L": round(fs["pnl"], 2),
        "ROI": pct(fs["roi"], 1),
        "$/day": num(fd["mean_per_day"], 2),
        "$/day low": num(fd["boot_low"], 2),
    })
st.dataframe(pd.DataFrame(fam_rows), use_container_width=True, hide_index=True)
st.caption("Fill rate below expectation with P(No|filled) holding up means "
           "queue crowding. P(No|filled) falling means the mechanism is breaking.")

st.subheader("Kill switch")
kill_rows = []
for fam in sorted({c["family"] for c in series_cfg.values()}):
    for m in (config.MODE_LIVE, config.MODE_DRY):
        k = analytics.kill_check(fam, m)
        if k["n"] == 0:
            continue
        kill_rows.append({
            "Family": fam, "Mode": m, "Settled fills": k["n"],
            "P(No|filled)": pct(k["p_no"], 1),
            "Breakeven": num(k["breakeven"], 2),
            "Cushion": "--" if k["cushion"] is None else f"{100 * k['cushion']:+.1f}",
            "State": k["state"],
        })
if kill_rows:
    st.dataframe(pd.DataFrame(kill_rows), use_container_width=True, hide_index=True)
    st.caption(f"Warns at {config.KILL_ALERT_FILLS} settled fills below breakeven, "
               f"reverts the family to LOG at {config.KILL_REVERT_FILLS}. "
               f"Only LIVE reverts automatically.")
else:
    st.caption("No settled fills yet.")

st.divider()

st.subheader("By series")
ser_rows = []
for s, cfg in sorted(series_cfg.items()):
    srows = analytics.filter_orders(rows_all, series=s,
                                    mode=None if mode_filter == "All" else mode_filter,
                                    first_list_only=first_only,
                                    events_by_ticker=ev_index)
    ss = analytics.stats(srows)
    sd = analytics.day_summary(srows)
    evs = [e for e in events_all if e.get("series") == s]
    ser_rows.append({
        "Series": s,
        "Family": cfg["family"],
        "Mode": cfg["mode"],
        "Price": num(cfg.get("rest_price"), 2),
        "Stake": money(cfg.get("dollars")),
        "Size": "--" if config.contracts_for(cfg) is None
        else f"{config.contracts_for(cfg):g}",
        "Events": len(evs),
        "First-list": len([e for e in evs if e.get("discovered_at_open")]),
        "Orders": ss["orders"],
        "Fills": ss["fills"],
        "Fill rate": pct(ss["fill_rate"]),
        "Settled": ss["settled"],
        "Pending": ss["pending"],
        "P(No|filled)": pct(ss["p_no"], 1),
        "Cushion": "--" if ss["cushion"] is None else f"{100 * ss['cushion']:+.1f}",
        "P/L": round(ss["pnl"], 2),
        "ROI": pct(ss["roi"], 1),
        "$/day low": num(sd["boot_low"], 2),
    })
st.dataframe(pd.DataFrame(ser_rows), use_container_width=True, hide_index=True)

st.subheader("By mode")
mode_rows = []
for m in config.MODES:
    mrows = analytics.filter_orders(rows_all, mode=m, first_list_only=first_only,
                                    events_by_ticker=ev_index)
    ms = analytics.stats(mrows)
    md = analytics.day_summary(mrows)
    if not ms["orders"]:
        continue
    mode_rows.append({
        "Mode": m, "Orders": ms["orders"], "Fill rate": pct(ms["fill_rate"]),
        "Settled": ms["settled"], "P(No|filled)": pct(ms["p_no"], 1),
        "P/L": round(ms["pnl"], 2), "ROI": pct(ms["roi"], 1),
        "Days": md["settled_days"], "$/day": num(md["mean_per_day"], 2),
        "$/day low": num(md["boot_low"], 2),
    })
if mode_rows:
    st.dataframe(pd.DataFrame(mode_rows), use_container_width=True, hide_index=True)
st.caption("DRY and LIVE never share a statistic. Rows keep the mode they were "
           "created under.")

st.divider()

st.subheader("By day (Central)")
days = analytics.day_summary(base_rows)["buckets"]
if days:
    st.dataframe(pd.DataFrame([{
        "Day": d["day"], "Orders": d["orders"], "Fills": d["fills"],
        "Fill rate": pct(d["fills"] / d["orders"] if d["orders"] else None),
        "Settled": d["settled"],
        "P(No|filled)": pct(d["no_wins"] / d["settled"] if d["settled"] else None),
        "Staked": round(d["staked"], 2), "P/L": round(d["pnl"], 2),
    } for d in days[:60]]), use_container_width=True, hide_index=True)
    cum, series_pts, labels = 0.0, [], []
    for d in reversed(days):
        cum += d["pnl"]
        series_pts.append(round(cum, 2))
        labels.append(d["day"])
    if len(series_pts) > 1:
        st.line_chart(pd.DataFrame({"Cumulative P/L": series_pts}, index=labels))
else:
    st.caption("No orders yet in this slice.")

st.subheader("Open events")
if live_events:
    rest_by_ev: Dict[str, int] = {}
    for x in rows_all:
        if x.get("status") == "resting":
            rest_by_ev[x["event_ticker"]] = rest_by_ev.get(x["event_ticker"], 0) + 1
    st.dataframe(pd.DataFrame([{
        "Series": e["series"], "Family": e.get("family") or "",
        "Mode": e.get("mode") or "", "Event": e["event_ticker"],
        "Markets": e.get("markets_seen") or 0,
        "Orders": e.get("orders_placed") or 0,
        "Resting": rest_by_ev.get(e["event_ticker"], 0),
        "First list": "yes" if e.get("discovered_at_open") else "no",
        "Clock": e.get("cancel_source") or "none",
        "Cancel at": clock.fmt_ct(e.get("cancel_at")),
        "In": clock.human_delta(
            (clock.to_utc(e["cancel_at"]) - clock.now_utc()).total_seconds()
            if e.get("cancel_at") else None),
    } for e in sorted(live_events,
                      key=lambda x: (x.get("cancel_at") or clock.now_utc()))]),
        use_container_width=True, hide_index=True)
else:
    st.caption("Nothing resting.")

with st.expander("Order tape"):
    if rows_all:
        st.dataframe(pd.DataFrame([{
            "Placed": clock.fmt_ct(x.get("placed_at")),
            "Series": x["series"], "Mode": x.get("mode"),
            "Market": x["market_ticker"], "Word": x.get("market_title") or "",
            "Price": float(x["limit_price"]) if x.get("limit_price") is not None else None,
            "Count": f"{float(x['count']):g}" if x.get("count") is not None else "",
            "Stake": float(x["dollars"]) if x.get("dollars") is not None else None,
            "Status": x.get("status"),
            "Crossed": "yes" if x.get("took_at_open") else "",
            "Filled": clock.fmt_ct_short(x.get("filled_at")) if x.get("filled_at") else "",
            "Result": x.get("result") or "",
            "P/L": float(x["pnl"]) if x.get("pnl") is not None else None,
        } for x in rows_all[:800]]), use_container_width=True, hide_index=True)
    else:
        st.caption("No orders yet.")

with st.expander("Events seen"):
    st.dataframe(pd.DataFrame([{
        "Discovered": clock.fmt_ct(e.get("discovered_at")),
        "Series": e["series"], "Mode": e.get("mode"),
        "Event": e["event_ticker"], "Title": e.get("title") or "",
        "Markets": e.get("markets_seen") or 0,
        "Orders": e.get("orders_placed") or 0,
        "First list": "yes" if e.get("discovered_at_open") else "no",
        "Clock": e.get("cancel_source") or "none",
        "Cancel at": clock.fmt_ct(e.get("cancel_at")),
        "Closed": clock.fmt_ct(e.get("cancelled_at")) if e.get("cancelled_at") else "",
    } for e in events_all[:400]]), use_container_width=True, hide_index=True)

with st.expander("Recent activity"):
    for line in store.recent_log(120):
        st.text(f"{clock.fmt_ct(line['ts'])}  [{line['level']}]  {line['message']}")

st.caption(f"Rendered {clock.fmt_ct(clock.now_utc())}. Controls are in Telegram.")
