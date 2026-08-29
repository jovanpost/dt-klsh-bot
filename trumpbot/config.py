"""Configuration.

Everything here can be overridden from Streamlit Secrets. The dict below is
only a fallback so a fresh deploy still boots. If you want the numbers off the
public GitHub tree, delete the values in DEFAULT_SERIES and set them in
Secrets instead -- the loader already prefers Secrets.

Secrets shape (TOML):

    DRY_RUN = true
    KALSHI_KEY_ID = "..."
    KALSHI_PRIVATE_KEY = \"\"\"-----BEGIN RSA PRIVATE KEY-----
    ...
    -----END RSA PRIVATE KEY-----\"\"\"
    DATABASE_URL = "postgresql://postgres.xxxx:PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
    TELEGRAM_TOKEN = "..."
    TELEGRAM_CHAT_ID = "..."

    [series.KXTRUMPMENTIONB]
    rest_price = 0.35
    dollars = 3.00
    buffer_min = 5
    enabled = true
"""
from __future__ import annotations

import os
from typing import Any, Dict

# ---------------------------------------------------------------- defaults ---

DEFAULT_SERIES: Dict[str, Dict[str, Any]] = {
    "KXTRUMPMENTIONB": {
        "rest_price": 0.35,
        "dollars": 3.00,
        "buffer_min": 5,
        "enabled": True,
        # backtest expectations, used only for the dashboard comparison
        "exp_fill_rate": 0.657,
        "exp_p_no_given_filled": 0.605,
    },
    "KXTRUMPMENTION": {
        "rest_price": 0.25,
        "dollars": 3.00,
        "buffer_min": 5,
        "enabled": True,
        "exp_fill_rate": 0.671,
        "exp_p_no_given_filled": 0.318,
    },
}

# Poll cadences, seconds.
TICK_SECONDS = 5              # main loop heartbeat
DISCOVERY_SECONDS = 45        # look for brand-new events
EVENT_POLL_SECONDS = 5        # quotes + late-market join per open event
SETTLE_SECONDS = 3600         # settlement hunter, hourly
TICK_LOG_SECONDS = 60         # how often to save a quote sample

# Live placement only. Kalshi's REST API historically wants an integer count.
# "floor" sends int(count); "raw" sends the fraction. Dry-run always uses the
# exact fraction so the paper P/L matches the research sizing.
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


def dry_run() -> bool:
    v = get("DRY_RUN", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off")
    return bool(v)


def series_config() -> Dict[str, Dict[str, Any]]:
    """Merge DEFAULT_SERIES with any [series.X] blocks in Secrets."""
    merged = {k: dict(v) for k, v in DEFAULT_SERIES.items()}
    override = _secrets().get("series", {})
    try:
        override = dict(override)
    except Exception:
        override = {}
    for ticker, vals in override.items():
        base = merged.get(ticker, {})
        base.update(dict(vals))
        merged[ticker] = base
    return merged


def enabled_series() -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in series_config().items() if v.get("enabled")}


def contracts_for(cfg: Dict[str, Any]) -> float:
    """Dollars / price, rounded to 6 decimals. Fractional on purpose."""
    return round(float(cfg["dollars"]) / float(cfg["rest_price"]), 6)
