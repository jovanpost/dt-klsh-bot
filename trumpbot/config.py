"""Configuration.

Per-series run modes replace the global DRY_RUN boolean.

    LIVE  place real orders, real fills, real money
    DRY   place nothing real; simulate fills by watching the book
    LOG   record events, markets and prices only; no orders, no fills
    OFF   ignore the series entirely

Mode resolution order, highest wins:
    1. the DB (set by Telegram /mode) -- survives a Streamlit reboot
    2. Secrets  [series.X] mode = "DRY"
    3. DEFAULT_SERIES below

Prices and sizes are NOT touched by a mode change. Moving a series to LIVE
never implicitly retunes anything.

Secrets shape (TOML):

    KALSHI_KEY_ID = "..."
    KALSHI_PRIVATE_KEY = \"\"\"-----BEGIN RSA PRIVATE KEY-----
    ...
    -----END RSA PRIVATE KEY-----\"\"\"
    DATABASE_URL = "postgresql://postgres.xxxx:PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    TELEGRAM_TOKEN = "..."
    TELEGRAM_CHAT_ID = "..."

    [series.KXTRUMPMENTIONB]
    mode = "DRY"
    rest_price = 0.35
    dollars = 3.00
    buffer_min = 5
"""
from __future__ import annotations

import os
from typing import Any, Dict

# ------------------------------------------------------------------- modes ---

MODE_LIVE = "LIVE"
MODE_DRY = "DRY"
MODE_LOG = "LOG"
MODE_OFF = "OFF"
MODES = (MODE_LIVE, MODE_DRY, MODE_LOG, MODE_OFF)

# Modes that create order rows of any kind.
PLACING_MODES = (MODE_LIVE, MODE_DRY)


# ---------------------------------------------------------------- defaults ---

DEFAULT_SERIES: Dict[str, Dict[str, Any]] = {
    "KXTRUMPMENTIONB": {
        "mode": MODE_DRY,
        "rest_price": 0.35,
        "dollars": 3.00,
        "buffer_min": 5,
        # Clean-clock expectations (handoff v2). The old 0.657 / 0.605 came
        # from a cancel anchor that ran through the appearance and counted
        # on-air fills the bot could never take.
        "exp_fill_rate": 0.522,
        "exp_p_no_given_filled": 0.579,
    },
    "KXTRUMPMENTION": {
        "mode": MODE_DRY,
        "rest_price": 0.25,
        "dollars": 3.00,
        "buffer_min": 5,
        # No clean-clock fill rate or P(No|filled) was published for A.
        # Left as None rather than carrying the discredited 0.671 / 0.318.
        "exp_fill_rate": None,
        "exp_p_no_given_filled": None,
    },
}

# Poll cadences, seconds.
TICK_SECONDS = 5              # main loop heartbeat
DISCOVERY_SECONDS = 45        # look for brand-new events
EVENT_POLL_SECONDS = 5        # quotes + late-market join per open event
SETTLE_SECONDS = 3600         # settlement hunter, hourly
TICK_LOG_SECONDS = 60         # how often to save a quote sample

# An event counts as "discovered at open" if we saw it within this many
# seconds of its markets opening. Only these events belong in the clean
# fill-rate test against 52%.
FIRST_LIST_GRACE_SECONDS = 180

# Telegram nag when an open event has neither /when nor a milestone.
NAG_REPEAT_MINUTES = 90

# Seconds a /mode LIVE confirmation code stays valid.
LIVE_CONFIRM_TTL = 60

# Live placement only. "floor" sends int(count); "raw" sends the fraction.
LIVE_COUNT_MODE = "raw"

TABLE_PREFIX = "tm_"          # keeps these tables apart from the WNT bot's


# ----------------------------------------------------------------- loading ---

def _secrets() -> Dict[str, Any]:
    try:
        import streamlit as st
        return dict(st.secrets)
    except Exception:
        return {}


def get(name: str, default: Any = None) -> Any:
    """Secrets first, then environment, then the supplied default."""
    s = _secrets()
    if name in s:
        return s[name]
    if name in os.environ:
        return os.environ[name]
    return default


def series_config() -> Dict[str, Dict[str, Any]]:
    """DEFAULT_SERIES merged with any [series.X] blocks in Secrets.

    This does NOT consult the DB. Use mode_for() for the effective mode.
    """
    merged = {k: dict(v) for k, v in DEFAULT_SERIES.items()}
    override = _secrets().get("series", {})
    try:
        override = dict(override)
    except Exception:
        override = {}
    for ticker, vals in override.items():
        base = merged.get(ticker, {})
        base.update(dict(vals))
        # Migration: an old `enabled = false` means OFF.
        if "mode" not in base and base.get("enabled") is False:
            base["mode"] = MODE_OFF
        merged[ticker] = base
    return merged


def normalize_mode(value: Any) -> str:
    v = str(value or "").strip().upper()
    return v if v in MODES else MODE_OFF


def mode_for(series: str) -> str:
    """Effective mode: DB override, then Secrets/defaults.

    store is imported lazily -- store imports config at module level, so a
    top-level import here would be circular.
    """
    try:
        from . import store
        db = store.get_series_mode(series)
        if db:
            return normalize_mode(db)
    except Exception:
        pass
    cfg = series_config().get(series)
    if not cfg:
        return MODE_OFF
    if "mode" not in cfg and cfg.get("enabled") is False:
        return MODE_OFF
    return normalize_mode(cfg.get("mode", MODE_DRY))


def all_modes() -> Dict[str, str]:
    return {s: mode_for(s) for s in series_config()}


def active_series() -> Dict[str, Dict[str, Any]]:
    """Series we still discover events for. OFF is excluded."""
    return {k: v for k, v in series_config().items()
            if mode_for(k) != MODE_OFF}


def enabled_series() -> Dict[str, Dict[str, Any]]:
    """Kept so older call sites do not break."""
    return active_series()


def dry_run() -> bool:
    """Legacy shim. Modes are per series now -- prefer mode_for(series).

    True only if no series is LIVE, so anything still calling this behaves
    conservatively rather than assuming real money.
    """
    return not any(m == MODE_LIVE for m in all_modes().values())


def contracts_for(cfg: Dict[str, Any]) -> float:
    """Dollars / price, rounded to 6 decimals. Fractional on purpose."""
    return round(float(cfg["dollars"]) / float(cfg["rest_price"]), 6)
