"""Read-only status page.

No buttons. Anyone with the URL can open this, so every control lives in
Telegram. Keep the page thin: it should not explain the strategy.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from trumpbot import clock, config, runner, settle, store

st.set_page_config(page_title="Mention bot", layout="wide")

# Keep-alive ping from the GitHub Action. Answer and stop.
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

st.title("Mention bot")

if r.error:
    st.error(f"Worker error: {r.error}")

# ------------------------------------------------------------------ header ---
try:
    started = clock.parse_iso(store.get_state("started_at"))
    last_poll = clock.parse_iso(store.get_state("last_poll"))
    paused = store.get_state("paused", "0") == "1"
except Exception as exc:
    st.error(f"Database unavailable: {exc}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Mode", "Dry run" if config.dry_run() else "Live")
c2.metric("Worker", "Paused" if paused else ("Running" if r.is_alive() else "Stopped"))
c3.metric("Last poll", clock.fmt_ct_short(last_poll))
c4.metric("Started", clock.fmt_ct_short(started))

rows = store.orders_for_dashboard(limit=5000)
live = store.live_events()

# ------------------------------------------------------------ open events ---
st.subheader("Open events")
if not live:
    st.caption("Nothing resting. New events appear within about a minute of listing.")
else:
    rest = store.resting_orders()
    data = []
    for e in sorted(live, key=lambda x: (x.get("cancel_at") or clock.now_utc())):
        secs = None
        if e.get("cancel_at"):
            secs = (clock.to_utc(e["cancel_at"]) - clock.now_utc()).total_seconds()
        data.append({
            "Series": e["series"],
            "Event": e["event_ticker"],
            "Markets": e.get("markets_seen") or 0,
            "Orders": e.get("orders_placed") or 0,
            "Resting": len([x for x in rest if x["event_ticker"] == e["event_ticker"]]),
            "Cancel at": clock.fmt_ct(e.get("cancel_at")),
            "In": clock.human_delta(secs),
            "Anchor": e.get("cancel_source") or "--",
        })
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)

# ------------------------------------------------- live vs backtest, by series ---
st.subheader("Live vs backtest")
comp = []
for series, cfg in config.series_config().items():
    mine = [x for x in rows if x["series"] == series and x["status"] != "rejected"]
    fills = [x for x in mine if x["status"] == "filled"]
    settled = [x for x in fills if x.get("result")]
    wins = [x for x in settled if x["result"] == "no"]
    comp.append({
        "Series": series,
        "Price": f"{float(cfg['rest_price']):.2f}",
        "Size": f"{config.contracts_for(cfg):g}",
        "Orders": len(mine),
        "Fill rate": clock.pct(len(fills) / len(mine) if mine else None),
        "Expected fill": clock.pct(cfg.get("exp_fill_rate")),
        "P(No|filled)": clock.pct(len(wins) / len(settled) if settled else None),
        "Expected P(No)": clock.pct(cfg.get("exp_p_no_given_filled")),
        "Settled": len(settled),
        "P/L": f"${sum(float(x.get('pnl') or 0) for x in settled):.2f}",
    })
st.dataframe(pd.DataFrame(comp), use_container_width=True, hide_index=True)
st.caption("Small samples move a lot. One day is not evidence.")

# -------------------------------------------------------- day-clustered P/L ---
st.subheader("By day (Central)")
days = settle.day_clustered(rows)
if not days:
    st.caption("No orders yet.")
else:
    table = []
    for d in days[:30]:
        table.append({
            "Day": d["day"],
            "Orders": d["orders"],
            "Fills": d["fills"],
            "Fill rate": clock.pct(d["fills"] / d["orders"] if d["orders"] else None),
            "Settled": d["settled"],
            "P(No|filled)": clock.pct(d["no_wins"] / d["settled"] if d["settled"] else None),
            "P/L": round(d["pnl"], 2),
        })
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    total = sum(d["pnl"] for d in days)
    n_days = len([d for d in days if d["settled"]])
    st.caption(f"Total ${total:.2f} across {n_days} settled days. "
               f"Day is the unit the research is calibrated on, not event.")

# --------------------------------------------------------------- order tape ---
st.subheader("Orders")
if rows:
    tape = pd.DataFrame([{
        "Placed": clock.fmt_ct(x.get("placed_at")),
        "Series": x["series"],
        "Market": x["market_ticker"],
        "Name": x.get("market_title") or "",
        "Price": float(x["limit_price"]) if x.get("limit_price") is not None else None,
        "Count": f"{float(x['count']):g}" if x.get("count") is not None else "",
        "Status": x.get("status"),
        "Crossed": "yes" if x.get("took_at_open") else "",
        "Fill": clock.fmt_ct_short(x.get("filled_at")) if x.get("filled_at") else "",
        "Result": x.get("result") or "",
        "P/L": float(x["pnl"]) if x.get("pnl") is not None else None,
    } for x in rows[:400]])
    st.dataframe(tape, use_container_width=True, hide_index=True)
else:
    st.caption("No orders yet.")

# -------------------------------------------------------------------- log ---
with st.expander("Recent activity"):
    for line in store.recent_log(80):
        st.text(f"{clock.fmt_ct(line['ts'])}  [{line['level']}]  {line['message']}")

st.caption(f"Page rendered {clock.fmt_ct(clock.now_utc())}. Controls are in Telegram.")
