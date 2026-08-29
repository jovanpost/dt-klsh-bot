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

from . import clock, kalshi, store
from .strategy import taker_fee

log = logging.getLogger("trumpbot.settle")


def pnl_for(row: Dict, result: str) -> float:
    """Buy NO at p for n contracts. NO wins -> n * (1 - p). YES wins -> -n * p.

    Fee is zero: neither series is in Kalshi's Non-Standard Fees table, so the
    maker multiplier is 0 and a resting order that fills pays nothing.
    """
    if row.get("status") != "filled":
        return 0.0
    n = float(row.get("count") or 0)
    p = float(row.get("fill_price") or row.get("limit_price") or 0)
    return round(n * (1.0 - p), 4) if result == "no" else round(-n * p, 4)


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
    for row in rows:
        res = results.get(row["market_ticker"])
        if not res:
            continue
        pnl = pnl_for(row, res)
        fee = 0.0
        if row.get("status") == "filled" and row.get("took_at_open"):
            # Only crossed-at-placement orders would have paid a taker fee.
            # Stored separately so it never silently rewrites the maker P/L
            # the research is calibrated on.
            fee = taker_fee(float(row.get("count") or 0),
                            float(row.get("fill_price") or row.get("limit_price") or 0))
        store.update_order(row["id"], result=res, settled_at=clock.now_utc(),
                           pnl=pnl, fee_est=fee)
        settled += 1

    if settled:
        store.log_line("info", f"Settled {settled} rows")
    return settled


def day_clustered(rows: List[Dict]) -> List[Dict]:
    """P/L per Central-time calendar day, which is the basis the research uses.

    Per-event numbers are overconfident: series A and B fire on the same days
    and correlate at +0.589, so running both is substantially the same bet
    twice. Day is the honest unit.
    """
    buckets: Dict[str, Dict] = {}
    for r in rows:
        anchor = r.get("filled_at") or r.get("cancelled_at") or r.get("placed_at")
        day = clock.ct_date(anchor)
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
