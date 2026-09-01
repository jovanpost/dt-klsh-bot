"""Analytics shared by the dashboard, Telegram and the kill switch.

One place computes these numbers so the page and the bot can never disagree.

Two rules the research paid for:
  * Report the day-clustered bootstrap lower bound, never the per-event mean.
    Events on the same day are correlated; per-event confidence is fiction.
  * Never mix modes, and never mix first-list events with mid-event joins.
    A mid-join tape is a different measurement, not a bad day.
"""
from __future__ import annotations

import random
from typing import Any, Dict, Iterable, List, Optional

from . import clock, config

FIRST_LIST_ONLY_DEFAULT = True


def event_index(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {e["event_ticker"]: e for e in events}


def filter_orders(rows: List[Dict[str, Any]],
                  mode: Optional[str] = None,
                  family: Optional[str] = None,
                  series: Optional[str] = None,
                  price: Optional[float] = None,
                  first_list_only: bool = False,
                  events_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None
                  ) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("status") == "rejected":
            continue
        if mode and r.get("mode") != mode:
            continue
        if family and r.get("family") != family:
            continue
        if series and r.get("series") != series:
            continue
        if price is not None and r.get("limit_price") is not None \
                and abs(float(r["limit_price"]) - float(price)) > 1e-9:
            continue
        if first_list_only:
            ev = (events_by_ticker or {}).get(r.get("event_ticker"))
            if not ev or not ev.get("discovered_at_open"):
                continue
        out.append(r)
    return out


def stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    orders = len(rows)
    fills = [r for r in rows if r.get("status") == "filled"]
    settled = [r for r in fills if r.get("result")]
    wins = [r for r in settled if r["result"] == "no"]

    staked = sum(float(r.get("dollars") or 0) for r in settled)
    pnl = sum(float(r.get("pnl") or 0) for r in settled)
    contracts = sum(float(r.get("count") or 0) for r in settled)

    prices = [float(r["limit_price"]) for r in rows
              if r.get("limit_price") is not None]
    avg_price = sum(prices) / len(prices) if prices else None
    p_no = (len(wins) / len(settled)) if settled else None

    return {
        "orders": orders,
        "fills": len(fills),
        "fill_rate": (len(fills) / orders) if orders else None,
        "settled": len(settled),
        "pending": len(fills) - len(settled),
        "no_wins": len(wins),
        "p_no": p_no,
        "pnl": pnl,
        "staked": staked,
        "roi": (pnl / staked) if staked else None,
        "edge_per_contract": (pnl / contracts) if contracts else None,
        "avg_price": avg_price,
        "cushion": (p_no - avg_price) if (p_no is not None and avg_price) else None,
        "days": len({clock.ct_date(r.get("filled_at") or r.get("placed_at"))
                     for r in rows if r.get("placed_at")}),
    }


def day_pnl(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        anchor = r.get("filled_at") or r.get("cancelled_at") or r.get("placed_at")
        day = clock.ct_date(anchor)
        if not day:
            continue
        b = buckets.setdefault(day, {"day": day, "orders": 0, "fills": 0,
                                     "settled": 0, "no_wins": 0,
                                     "pnl": 0.0, "staked": 0.0})
        b["orders"] += 1
        if r.get("status") == "filled":
            b["fills"] += 1
            if r.get("result"):
                b["settled"] += 1
                if r["result"] == "no":
                    b["no_wins"] += 1
                b["pnl"] += float(r.get("pnl") or 0)
                b["staked"] += float(r.get("dollars") or 0)
    return buckets


def bootstrap_low(values: List[float], iters: int = 2000,
                  pct: float = 2.5, seed: int = 7) -> Optional[float]:
    vals = [float(v) for v in values]
    if len(vals) < 5:
        return None
    rng = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    idx = max(0, min(len(means) - 1, int(len(means) * pct / 100.0)))
    return means[idx]


def day_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets = day_pnl(rows)
    settled_days = [b for b in buckets.values() if b["settled"] > 0]
    vals = [b["pnl"] for b in settled_days]
    mean = (sum(vals) / len(vals)) if vals else None
    return {
        "days": len(buckets),
        "settled_days": len(settled_days),
        "mean_per_day": mean,
        "boot_low": bootstrap_low(vals),
        "total": sum(vals) if vals else 0.0,
        "buckets": sorted(buckets.values(), key=lambda x: x["day"], reverse=True),
    }


MIN_EVENTS_DRY = 25
MIN_EVENTS_LIVE = 50
MIN_SETTLED_FILLS = 50
MIN_SETTLED_DAYS = 10
MIN_CUSHION = 0.03
FILL_RATE_FLOOR = 0.70


def readiness(series: str, rows: List[Dict[str, Any]],
              events: List[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = config.series_cfg(series) or {}
    mine = [e for e in events if e.get("series") == series]
    first_list = [e for e in mine if e.get("discovered_at_open")]

    st = stats(rows)
    ds = day_summary(rows)
    exp_fill = cfg.get("exp_fill_rate")

    gates: List[Dict[str, Any]] = []

    def gate(name, ok, have, need, note=""):
        gates.append({"name": name, "ok": bool(ok), "have": have,
                      "need": need, "note": note,
                      "progress": None if not isinstance(have, (int, float))
                      or not isinstance(need, (int, float)) or not need
                      else max(0.0, min(1.0, float(have) / float(need)))})

    gate("Mode is DRY", cfg.get("mode") == config.MODE_DRY,
         cfg.get("mode"), "DRY",
         "LOG series record only; LIVE is the thing we are gating")
    gate("First-list events", len(first_list), len(first_list), MIN_EVENTS_LIVE,
         "mid-event joins do not count")
    gate("Settled fills", st["settled"], st["settled"], MIN_SETTLED_FILLS)
    gate("Settled days", ds["settled_days"], ds["settled_days"], MIN_SETTLED_DAYS)

    cushion = st["cushion"]
    gate("P(No|filled) clears price",
         cushion is not None and cushion >= MIN_CUSHION,
         None if cushion is None else round(cushion, 3), MIN_CUSHION,
         "P(No|filled) minus the price paid")

    if exp_fill and st["fill_rate"] is not None:
        ratio = st["fill_rate"] / exp_fill
        gate("Fill rate vs backtest", ratio >= FILL_RATE_FLOOR,
             round(ratio, 2), FILL_RATE_FLOOR,
             "low means you are behind other resting orders in the queue")
    else:
        gate("Fill rate vs backtest", False, "no baseline", FILL_RATE_FLOOR,
             "family has no published expectation")

    low = ds["boot_low"]
    gate("Day bootstrap low > 0", low is not None and low > 0,
         None if low is None else round(low, 3), 0.0,
         "95% lower bound on mean $/day, resampling whole days")

    passed = sum(1 for g in gates if g["ok"])
    return {
        "series": series,
        "family": cfg.get("family"),
        "mode": cfg.get("mode"),
        "price": cfg.get("rest_price"),
        "dollars": cfg.get("dollars"),
        "gates": gates,
        "passed": passed,
        "total": len(gates),
        "ready": passed == len(gates),
        "stats": st,
        "day": ds,
        "events_seen": len(mine),
        "events_first_list": len(first_list),
    }


def kill_check(family: str, mode: str = config.MODE_LIVE) -> Dict[str, Any]:
    from . import store
    rows = store.recent_family_fills(family, mode,
                                     limit=config.KILL_REVERT_FILLS)
    n = len(rows)
    if n == 0:
        return {"family": family, "n": 0, "state": "no data",
                "p_no": None, "breakeven": None, "cushion": None}
    wins = sum(1 for r in rows if r.get("result") == "no")
    p_no = wins / n
    prices = [float(r["limit_price"]) for r in rows if r.get("limit_price")]
    breakeven = sum(prices) / len(prices) if prices else None
    cushion = (p_no - breakeven) if breakeven is not None else None

    state = "ok"
    if breakeven is not None and p_no < breakeven:
        if n >= config.KILL_REVERT_FILLS:
            state = "revert"
        elif n >= config.KILL_ALERT_FILLS:
            state = "alert"
        else:
            state = "watch"
    return {"family": family, "n": n, "p_no": p_no, "breakeven": breakeven,
            "cushion": cushion, "state": state}
