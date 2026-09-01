"""Configuration.

Series live in the database (tm_series), not in a 180-entry Python dict.
This module is the reader: it merges the DB rows over the bootstrap defaults
below and hands the engine one dict per series.

    LIVE  real orders, real money
    DRY   no orders; simulate fills by watching the book
    LOG   record events, markets and quotes only
    OFF   ignore the series entirely

Resolution order for any series field: tm_series row, then FAMILY_DEFAULTS,
then the hard defaults here. Mode always comes from the DB once a row exists,
so a Streamlit reboot cannot silently reset a series.

PRICE IS PER FAMILY, NEVER GLOBAL. Sports loses money at 0.30 and makes money
at 0.15-0.20; a single global price would be a losing bet on half the book.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Dict, Optional

# ------------------------------------------------------------------- modes ---

MODE_LIVE = "LIVE"
MODE_DRY = "DRY"
MODE_LOG = "LOG"
MODE_OFF = "OFF"
MODES = (MODE_LIVE, MODE_DRY, MODE_LOG, MODE_OFF)
PLACING_MODES = (MODE_LIVE, MODE_DRY)


# ---------------------------------------------------------------- families ---
# base_price is the family's centre; each series gets a deterministic offset
# inside +/- JITTER_SPREAD so the book is not a wall of identical orders.
# exp_fill / exp_p_no are the backtest yardsticks for the dashboard.

FAMILY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "POLITICIAN":      {"base_price": 0.30, "mode": MODE_DRY, "dollars": 1.00,
                        "exp_fill": 0.51, "exp_p_no": 0.603},
    "NEWS_SHOW":       {"base_price": 0.30, "mode": MODE_DRY, "dollars": 1.00,
                        "exp_fill": 0.52, "exp_p_no": 0.618},
    "ENTERTAINMENT":   {"base_price": 0.30, "mode": MODE_DRY, "dollars": 1.00,
                        "exp_fill": 0.55, "exp_p_no": 0.702},
    "EARNINGS":        {"base_price": 0.20, "mode": MODE_LOG, "dollars": 1.00,
                        "exp_fill": 0.52, "exp_p_no": 0.509},
    # Sports failed as a block, but the block is incoherent: MLB lost 47.9
    # while World Cup made 41.2 at 0.30. Split, both LOG, judge separately.
    "SPORTS_ANNOUNCER": {"base_price": None, "mode": MODE_LOG, "dollars": 1.00,
                         "exp_fill": None, "exp_p_no": None},
    "SPORTS_EVENT":     {"base_price": None, "mode": MODE_LOG, "dollars": 1.00,
                         "exp_fill": None, "exp_p_no": None},
    # Negative at every price. Do not trade.
    "HEARING":         {"base_price": None, "mode": MODE_LOG, "dollars": 1.00,
                        "exp_fill": None, "exp_p_no": 0.473},
    "BUSINESS":        {"base_price": None, "mode": MODE_LOG, "dollars": 1.00,
                        "exp_fill": None, "exp_p_no": None},
    "OTHER":           {"base_price": 0.30, "mode": MODE_LOG, "dollars": 1.00,
                        "exp_fill": None, "exp_p_no": None},
}
FAMILIES = tuple(FAMILY_DEFAULTS.keys())

JITTER_SPREAD = 0.02      # +/- 2 cents around the family base
JITTER_STEP = 0.01

# One stake regime. Every series in DRY or LOG rests $1 per market, whatever
# its family or price. Mixed stakes would make cross-family dollar P/L
# meaningless, so there is only one number here.
DRY_STAKE = 1.00

# First LIVE sizing. Not applied by anything -- recorded so the plan does not
# drift. Going live is a per-series decision plus an explicit stake change.
LIVE_FIRST_STAKE = 0.25
LIVE_FIRST_BANK = 150.00

# Seeded into tm_series on first boot only. After that the DB row wins and
# this dict is ignored, so editing it will not retune a running series.
SEED_SERIES: Dict[str, Dict[str, Any]] = {
    "KXTRUMPMENTIONB": {"family": "POLITICIAN", "mode": MODE_DRY, "dollars": 1.00},
    "KXTRUMPMENTION":  {"family": "POLITICIAN", "mode": MODE_DRY, "dollars": 1.00},
    "KXWORLDNEWSMENTION": {"family": "NEWS_SHOW", "mode": MODE_OFF, "dollars": 1.00,
                           "notes": "handled by wnt-nofade-bot; do not enable"},
}

# Poll cadences, seconds.
TICK_SECONDS = 5
DISCOVERY_SECONDS = 45
SETTLE_SECONDS = 3600
TICK_LOG_SECONDS = 60

DISCOVERY_INTERVAL_BY_MODE = {
    MODE_LIVE: 45,
    MODE_DRY: 45,
    MODE_LOG: 900,
    MODE_OFF: None,
}
DISCOVERY_MAX_SERIES_PER_TICK = 8

# Events Kalshi listed before this Chicago instant are ignored (no rest).
CUTOFF_LISTED_CT = "2026-09-01 00:00:00"

FIRST_LIST_GRACE_SECONDS = 180
NAG_REPEAT_MINUTES = 90
LIVE_CONFIRM_TTL = 60
ORPHAN_HOURS = 48

KILL_ALERT_FILLS = 20
KILL_REVERT_FILLS = 50

LIVE_COUNT_MODE = "raw"
TABLE_PREFIX = "tm_"

_CACHE: Dict[str, Any] = {"series": None, "at": 0.0}
_CACHE_TTL = 5.0
_lock = threading.Lock()


def _secrets() -> Dict[str, Any]:
    try:
        import streamlit as st
        return dict(st.secrets)
    except Exception:
        return {}


def get(name: str, default: Any = None) -> Any:
    s = _secrets()
    if name in s:
        return s[name]
    if name in os.environ:
        return os.environ[name]
    return default


def normalize_mode(value: Any) -> str:
    v = str(value or "").strip().upper()
    return v if v in MODES else MODE_OFF


def normalize_family(value: Any) -> str:
    v = str(value or "").strip().upper()
    return v if v in FAMILY_DEFAULTS else "OTHER"


def jittered_price(series: str, base: Optional[float]) -> Optional[float]:
    if base is None:
        return None
    n = int(round(JITTER_SPREAD / JITTER_STEP))
    h = int(hashlib.sha256(series.encode()).hexdigest()[:8], 16)
    offset = (h % (2 * n + 1)) - n
    return round(float(base) + offset * JITTER_STEP, 4)


def _row_to_cfg(row: Dict[str, Any]) -> Dict[str, Any]:
    family = normalize_family(row.get("family"))
    fam = FAMILY_DEFAULTS[family]
    price = row.get("rest_price")
    if price is None:
        price = jittered_price(row["series"], fam["base_price"])
    return {
        "series": row["series"],
        "family": family,
        "mode": normalize_mode(row.get("mode") or fam["mode"]),
        "rest_price": float(price) if price is not None else None,
        "dollars": float(row.get("dollars") if row.get("dollars") is not None
                         else fam["dollars"]),
        "buffer_min": int(row.get("buffer_min") or 5),
        "enabled": bool(row.get("enabled", True)),
        "notes": row.get("notes"),
        "exp_fill_rate": fam["exp_fill"],
        "exp_p_no_given_filled": fam["exp_p_no"],
    }


def series_config(force: bool = False) -> Dict[str, Dict[str, Any]]:
    with _lock:
        now = time.time()
        if not force and _CACHE["series"] is not None and now - _CACHE["at"] < _CACHE_TTL:
            return _CACHE["series"]
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from . import store
        for row in store.all_series():
            out[row["series"]] = _row_to_cfg(row)
    except Exception:
        pass
    if not out:
        for s, vals in SEED_SERIES.items():
            out[s] = _row_to_cfg({"series": s, **vals})
    with _lock:
        _CACHE["series"] = out
        _CACHE["at"] = time.time()
    return out


def invalidate() -> None:
    with _lock:
        _CACHE["series"] = None


def series_cfg(series: str) -> Optional[Dict[str, Any]]:
    return series_config().get(series)


def mode_for(series: str) -> str:
    cfg = series_config().get(series)
    if not cfg:
        return MODE_OFF
    if not cfg.get("enabled"):
        return MODE_OFF
    return cfg["mode"]


def family_for(series: str) -> str:
    cfg = series_config().get(series)
    return cfg["family"] if cfg else "OTHER"


def all_modes() -> Dict[str, str]:
    return {s: c["mode"] for s, c in series_config().items()}


def series_in_family(family: str) -> Dict[str, Dict[str, Any]]:
    fam = normalize_family(family)
    return {s: c for s, c in series_config().items() if c["family"] == fam}


def active_series() -> Dict[str, Dict[str, Any]]:
    return {s: c for s, c in series_config().items()
            if c["mode"] != MODE_OFF}


def enabled_series() -> Dict[str, Dict[str, Any]]:
    return active_series()


def tradeable(cfg: Dict[str, Any]) -> bool:
    return cfg.get("rest_price") is not None


def dry_run() -> bool:
    return not any(m == MODE_LIVE for m in all_modes().values())


def contracts_for(cfg: Dict[str, Any]) -> Optional[float]:
    price = cfg.get("rest_price")
    if not price:
        return None
    return round(float(cfg["dollars"]) / float(price), 6)


def seed_series() -> int:
    from . import store
    added = 0
    existing = {r["series"] for r in store.all_series()}
    for s, vals in SEED_SERIES.items():
        if s in existing:
            continue
        family = normalize_family(vals.get("family"))
        fam = FAMILY_DEFAULTS[family]
        legacy = store.get_state(f"mode:{s}")
        price = vals.get("rest_price")
        if price is None:
            price = jittered_price(s, fam["base_price"])
        store.upsert_series({
            "series": s,
            "family": family,
            "mode": normalize_mode(legacy or vals.get("mode") or fam["mode"]),
            "rest_price": price,
            "dollars": vals.get("dollars", fam["dollars"]),
            "buffer_min": vals.get("buffer_min", 5),
            "enabled": vals.get("enabled", True),
            "notes": vals.get("notes"),
        })
        added += 1
    if added:
        invalidate()
    return added
