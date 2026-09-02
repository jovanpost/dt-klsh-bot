"""Settlement hunter.

Two WNT lessons live here:

  * The depth tape stopped at 6:15 PM and the bot assumed it was done. It was
    not -- Norway was still quoted 87/10 hours after the word was not said.
    The book at 6:15 is not settlement. Keep asking for the market until
    `result` is literally 'yes' or 'no'.
  * The original settle loop filtered out dry_run rows, so the dashboard read
    $0.00 forever. Unfilled rows get a result too (we want the base rate);
    they just get no P/L.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from . import analytics, clock, kalshi, store
from .strategy import taker_fee

log = logging.getLogger("trumpbot.settle")


def pnl_for(row: Dict, result: str) -> float:
    """Buy NO at p for n contracts. NO wins -> n * (1 - p). YES wins -> -n * p.

    Fee is zero: neither series is in Kalshi's Non-Standard Fees table, so the
    maker multiplier is 0 and a resting order that fills pays nothing.
    n is filled_count when we have a partial; otherwise the placed count.
    """
    n = float(row.get("filled_count") or 0)
    if n <= 0 and row.get("status") == "filled":
        n = float(row.get("count") or 0)
    if n <= 0:
        return 0.0
    p = float(row.get("fill_price") or row.get("limit_price") or 0)
    return round(n * (1.0 - p), 4) if result == "no" else round(-n * p, 4)


def _day_key(row: Dict) -> Optional[str]:
    anchor = row.get("filled_at") or row.get("cancelled_at") or row.get("placed_at")
    return clock.ct_date(anchor)


def _notify_days(notify: Callable[[str], None], batch: List[Dict]) -> None:
    """One Telegram per Central day that just got new results.

    Dollars and percent are the running day total (not only this batch),
    so a second settlement wave the same day updates the scoreboard.
    """
    days = sorted({d for d in (_day_key(r) for r in batch) if d})
    if not days:
        return
    tape = store.orders_for_dashboard(limit=20000)
    by_day: Dict[str, List[Dict]] = {}
    for r in tape:
        d = _day_key(r)
        if d in days:
            by_day.setdefault(d, []).append(r)
    for d in days:
        rows = by_day.get(d) or []
        st = analytics.stats(rows)
        pnl = float(st.get("pnl") or 0)
        staked = float(st.get("staked") or 0)
        roi = (pnl / staked) if staked else None
        batch_n = sum(1 for r in batch if _day_key(r) == d)
        batch_fills = sum(1 for r in batch if _day_key(r) == d
                          and r.get("status") == "filled")
        sign = "+" if pnl >= 0 else "-"
        body = (
            f"SETTLED {d}\n"
            f"This wave: {batch_n} rows ({batch_fills} fills)\n"
            f"Day P/L: {sign}${abs(pnl):.2f}"
        )
        if roi is not None:
            body += f"  ({sign}{abs(100 * roi):.1f}% on ${staked:.2f} staked)"
        elif staked:
            body += f"  (${staked:.2f} staked)"
        body += (
            f"\nDay tape: {st['fills']} fills / {st['orders']} orders, "
            f"P(No|filled) {clock.pct(st.get('p_no'), 1)}"
        )
        series_pnl: Dict[str, float] = {}
        for r in rows:
            if r.get("status") != "filled" or not r.get("result"):
                continue
            series_pnl[r["series"]] = series_pnl.get(r["series"], 0.0) + float(r.get("pnl") or 0)
        for s, v in sorted(series_pnl.items()):
            ssign = "+" if v >= 0 else "-"
            body += f"\n  {s}: {ssign}${abs(v):.2f}"
        notify(body)


def run(client: kalshi.KalshiClient,
        notify: Optional[Callable[[str], None]] = None) -> int:
    notify = notify or (lambda m: None)
    rows = store.unsettled_orders()
    if not rows:
        return 0

    tickers = sorted({r["market_ticker"] for r in rows})
    results: Dict[str, str] = {}
    for t in tickers:
        try:
            m = client.get_market(t)
        except Exception as exc:
            log.warning("Settlement read failed for %s: %s", t, exc)
            continue
        res = (m.get("result") or "").strip().lower()
        if res in ("yes", "no"):
            results[t] = res

    settled = 0
    batch: List[Dict] = []
    now = clock.now_utc()
    for row in rows:
        res = results.get(row["market_ticker"])
        if not res:
            continue
        pnl = pnl_for(row, res)
        fee = 0.0
        if (row.get("status") == "filled" or float(row.get("filled_count") or 0) > 0) \
                and row.get("took_at_open"):
            n = float(row.get("filled_count") or row.get("count") or 0)
            fee = taker_fee(n, float(row.get("fill_price") or row.get("limit_price") or 0))
        store.update_order(row["id"], result=res, settled_at=now,
                           pnl=pnl, fee_est=fee)
        updated = dict(row)
        updated["result"] = res
        updated["pnl"] = pnl
        updated["fee_est"] = fee
        updated["settled_at"] = now
        batch.append(updated)
        settled += 1

    if settled:
        store.log_line("info", f"Settled {settled} rows")
        try:
            _notify_days(notify, batch)
        except Exception as exc:
            log.warning("Settlement notify failed: %s", exc)
    return settled


def day_clustered(rows: List[Dict]) -> List[Dict]:
    """P/L per Central-time calendar day, which is the basis the research uses.

    Per-event numbers are overconfident: series A and B fire on the same days
    and correlate at +0.589, so running both is substantially the same bet
    twice. Day is the honest unit.
    """
    buckets: Dict[str, Dict] = {}
    for r in rows:
        day = _day_key(r)
        if not day:
            continue
        b = buckets.setdefault(day, {
            "day": day, "orders": 0, "fills": 0, "settled": 0,
            "no_wins": 0, "pnl": 0.0, "fees": 0.0,
            "by_series": {},
        })
        b["orders"] += 1
        s = b["by_series"].setdefault(r["series"], {"orders": 0, "fills": 0,
                                                    "no_wins": 0, "settled": 0,
                                                    "pnl": 0.0})
        s["orders"] += 1
        if r.get("status") == "filled":
            b["fills"] += 1
            s["fills"] += 1
            if r.get("result"):
                b["settled"] += 1
                s["settled"] += 1
                if r["result"] == "no":
                    b["no_wins"] += 1
                    s["no_wins"] += 1
                b["pnl"] += float(r.get("pnl") or 0)
                s["pnl"] += float(r.get("pnl") or 0)
                b["fees"] += float(r.get("fee_est") or 0)
    return sorted(buckets.values(), key=lambda x: x["day"], reverse=True)
