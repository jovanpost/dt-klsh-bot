"""Analytics shared by the dashboard, Telegram and the kill switch."""
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
MIN_EVENTS_LIVE = config.SIZE_EVENTS
MIN_SETTLED_FILLS = config.SIZE_FILLS
MIN_SETTLED_DAYS = config.SIZE_DAYS
MIN_CUSHION = config.SIZE_CUSHION
FILL_RATE_FLOOR = config.SIZE_FILL_RATIO


def _gate(name, ok, have, need, note=""):
    prog = None
    if isinstance(have, (int, float)) and isinstance(need, (int, float)) and need:
        prog = max(0.0, min(1.0, float(have) / float(need)))
    return {"name": name, "ok": bool(ok), "have": have, "need": need,
            "note": note, "progress": prog}


def events_with_settled_fills(rows: List[Dict[str, Any]]) -> int:
    return len({r.get("event_ticker") for r in rows
                if r.get("status") == "filled" and r.get("result") and r.get("event_ticker")})


def family_first_list_rows(family: str, rows: List[Dict[str, Any]],
                           events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    idx = event_index(events)
    return filter_orders(rows, family=family, first_list_only=True,
                         events_by_ticker=idx)


def family_unlocked(family: str, rows: List[Dict[str, Any]],
                    events: List[Dict[str, Any]]) -> Dict[str, Any]:
    frows = [r for r in family_first_list_rows(family, rows, events)
             if r.get("mode") == config.MODE_DRY]
    fl_events = [e for e in events
                 if e.get("family") == family and e.get("discovered_at_open")
                 and e.get("mode") == config.MODE_DRY]
    if not fl_events:
        members = set(config.series_in_family(family).keys())
        fl_events = [e for e in events
                     if e.get("series") in members and e.get("discovered_at_open")
                     and e.get("mode") == config.MODE_DRY]
        frows = filter_orders(rows, first_list_only=True,
                              events_by_ticker=event_index(events))
        frows = [r for r in frows if r.get("series") in members
                 and r.get("mode") == config.MODE_DRY]
    st = stats(frows)
    ds = day_summary(frows)
    fam = config.FAMILY_DEFAULTS.get(family, {})
    exp_fill = fam.get("exp_fill")
    cushion = st["cushion"]
    ratio = (st["fill_rate"] / exp_fill) if (exp_fill and st["fill_rate"] is not None) else None
    low = ds["boot_low"]
    gates = [
        _gate("Family first-list events", len(fl_events) >= config.SIZE_EVENTS,
              len(fl_events), config.SIZE_EVENTS, "pooled DRY first-list"),
        _gate("Family settled fills", st["settled"] >= config.SIZE_FILLS,
              st["settled"], config.SIZE_FILLS),
        _gate("Family settled days", ds["settled_days"] >= config.SIZE_DAYS,
              ds["settled_days"], config.SIZE_DAYS),
        _gate("Family cushion >= 0.03",
              cushion is not None and cushion >= config.SIZE_CUSHION,
              None if cushion is None else round(cushion, 3), config.SIZE_CUSHION),
        _gate("Family fill vs backtest",
              ratio is not None and ratio >= config.SIZE_FILL_RATIO,
              None if ratio is None else round(ratio, 2), config.SIZE_FILL_RATIO),
        _gate("Family day bootstrap > 0", low is not None and low > 0,
              None if low is None else round(low, 3), 0.0),
    ]
    return {
        "family": family,
        "unlocked": all(g["ok"] for g in gates),
        "gates": gates,
        "stats": st,
        "day": ds,
        "events_first_list": len(fl_events),
    }


def readiness(series: str, rows: List[Dict[str, Any]],
              events: List[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = config.series_cfg(series) or {}
    family = cfg.get("family") or "OTHER"
    mine = [e for e in events if e.get("series") == series]
    first_list = [e for e in mine if e.get("discovered_at_open")]
    st = stats(rows)
    ds = day_summary(rows)
    exp_fill = cfg.get("exp_fill_rate")
    price = cfg.get("rest_price")
    p_no = st["p_no"]
    cushion = st["cushion"]
    n_ev_fills = events_with_settled_fills(rows)
    is_six = series in config.SMOKE_SIX
    from . import store
    all_rows = store.orders_for_dashboard(limit=20000)
    all_events = store.all_events(limit=2000)
    fam_state = family_unlocked(family, all_rows, all_events)

    gates: List[Dict[str, Any]] = []
    gates.append(_gate("Mode is DRY", cfg.get("mode") == config.MODE_DRY,
                       cfg.get("mode"), "DRY",
                       "LOG records only. LIVE is gated here."))

    if is_six:
        path = "six-name smoke (BUSINESS)"
        gates.append(_gate("First-list events", len(first_list) >= config.SMOKE_SIX_EVENTS,
                           len(first_list), config.SMOKE_SIX_EVENTS,
                           "mid-joins do not count"))
        gates.append(_gate("Settled fills", st["settled"] >= config.SMOKE_SIX_FILLS,
                           st["settled"], config.SMOKE_SIX_FILLS))
        gates.append(_gate("Settled days", ds["settled_days"] >= config.SMOKE_SIX_DAYS,
                           ds["settled_days"], config.SMOKE_SIX_DAYS))
        gates.append(_gate("Events that produced fills",
                           n_ev_fills >= config.SMOKE_SIX_EVENTS_WITH_FILLS,
                           n_ev_fills, config.SMOKE_SIX_EVENTS_WITH_FILLS,
                           "not 12 fills from one show"))
        need_cush = config.SMOKE_SIX_CUSHION
        gates.append(_gate("Cushion >= price+0.05",
                           cushion is not None and cushion >= need_cush,
                           None if cushion is None else round(cushion, 3), need_cush,
                           "BUSINESS filter; 12 fills are not an edge test"))
        if exp_fill and st["fill_rate"] is not None:
            ratio = st["fill_rate"] / exp_fill
            gates.append(_gate("Fill rate vs backtest",
                               ratio >= config.SMOKE_SIX_FILL_RATIO,
                               round(ratio, 2), config.SMOKE_SIX_FILL_RATIO))
        else:
            gates.append(_gate("Fill rate vs backtest", False, "no baseline",
                               config.SMOKE_SIX_FILL_RATIO))
    else:
        path = "series smoke after family unlock (BUSINESS)"
        gates.append(_gate("Family unlocked for smoke", fam_state["unlocked"],
                           "yes" if fam_state["unlocked"] else "no", "yes",
                           "family 50/50/10 + cushion + fill + boot>0"))
        gates.append(_gate("First-list events", len(first_list) >= config.SMOKE_OTHER_EVENTS,
                           len(first_list), config.SMOKE_OTHER_EVENTS,
                           "full tape since Sep 1, no reset at unlock"))
        gates.append(_gate("Settled fills", st["settled"] >= config.SMOKE_OTHER_FILLS,
                           st["settled"], config.SMOKE_OTHER_FILLS))
        gates.append(_gate("Settled days", ds["settled_days"] >= config.SMOKE_OTHER_DAYS,
                           ds["settled_days"], config.SMOKE_OTHER_DAYS))
        gates.append(_gate("Events that produced fills",
                           n_ev_fills >= config.SMOKE_OTHER_EVENTS_WITH_FILLS,
                           n_ev_fills, config.SMOKE_OTHER_EVENTS_WITH_FILLS))
        ok_be = p_no is not None and price is not None and p_no >= float(price)
        gates.append(_gate("P(No|filled) >= price",
                           ok_be,
                           None if p_no is None else round(p_no, 3),
                           None if price is None else float(price),
                           "not losing on this ticker"))

    smoke_ready = all(g["ok"] for g in gates)
    return {
        "series": series,
        "family": family,
        "mode": cfg.get("mode"),
        "price": price,
        "dollars": cfg.get("dollars"),
        "path": path,
        "is_six": is_six,
        "gates": gates,
        "passed": sum(1 for g in gates if g["ok"]),
        "total": len(gates),
        "ready": smoke_ready,
        "smoke_ready": smoke_ready,
        "size_ready": fam_state["unlocked"] and cfg.get("mode") == config.MODE_DRY,
        "family_unlocked": fam_state["unlocked"],
        "family_gates": fam_state["gates"],
        "stats": st,
        "day": ds,
        "events_seen": len(mine),
        "events_first_list": len(first_list),
        "events_with_fills": n_ev_fills,
    }


def log_family_counts(events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    if events is None:
        from . import store
        events = store.all_events(limit=5000)
    log_series = {s for s, c in config.series_config().items()
                  if c["mode"] == config.MODE_LOG}
    out: Dict[str, int] = {}
    for e in events:
        s = e.get("series")
        if s not in log_series:
            continue
        fam = (config.series_cfg(s) or {}).get("family") or e.get("family") or "OTHER"
        out[fam] = out.get(fam, 0) + 1
    return out


def log_reviews_due(events: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    from . import store
    counts = log_family_counts(events)
    now = clock.now_utc()
    due = []
    for fam, n in sorted(counts.items()):
        raw = store.get_state(f"log_review:{fam}") or ""
        last_n, _, when = raw.partition("|")
        try:
            last_n_i = int(last_n)
        except ValueError:
            last_n_i = 0
        last_at = clock.parse_iso(when) if when else None
        added = n - last_n_i
        age_days = ((now - last_at).total_seconds() / 86400.0) if last_at else None
        hit_events = added >= config.LOG_REVIEW_EVENTS
        hit_time = (last_at is not None
                    and age_days >= config.LOG_REVIEW_DAYS
                    and added >= config.LOG_REVIEW_MIN_EVENTS)
        if hit_events or hit_time:
            due.append({"family": fam, "events": n, "added": added,
                        "days": None if last_at is None else round(age_days, 1),
                        "reason": "25 events" if hit_events else "90 days"})
    return due


def mark_log_reviewed(family: str, events: Optional[List[Dict[str, Any]]] = None) -> int:
    from . import store
    n = log_family_counts(events).get(family, 0)
    store.set_state(f"log_review:{family}", f"{n}|{clock.now_utc().isoformat()}")
    return n


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
