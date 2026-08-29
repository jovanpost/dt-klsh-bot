"""Time. All decisions in US Central via ZoneInfo, never a fixed UTC offset.

Also holds the safe formatters. The WNT bot died at 5:29 on an f-string like
f"({rate:.0%})" -- the percent sign next to the paren blew up the format
specifier. Use pct() everywhere instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ct() -> datetime:
    return datetime.now(CT)


def to_ct(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CT)


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso(value) -> Optional[datetime]:
    """Kalshi timestamps. Accepts ISO strings (with Z), epoch seconds, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(float(value), tz=UTC)
    s = str(value).strip()
    if not s or s.startswith("0001-01-01"):
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return to_utc(dt)


def fmt_ct(dt: Optional[datetime]) -> str:
    """'Aug 29 2:05:00 PM CT'. No POSIX-only %-I."""
    if dt is None:
        return "--"
    d = to_ct(dt)
    hour = d.hour % 12 or 12
    return f"{d:%b %d} {hour}:{d:%M:%S %p} CT"


def fmt_ct_short(dt: Optional[datetime]) -> str:
    if dt is None:
        return "--"
    d = to_ct(dt)
    hour = d.hour % 12 or 12
    return f"{hour}:{d:%M %p}"


def ct_date(dt: Optional[datetime]) -> Optional[str]:
    """Calendar day in Central, as YYYY-MM-DD. This is the day-clustering key."""
    if dt is None:
        return None
    return to_ct(dt).strftime("%Y-%m-%d")


def pct(rate: Optional[float], digits: int = 0) -> str:
    """Safe percent. Never use {x:.0%} in this codebase."""
    if rate is None:
        return "--"
    return f"{100 * float(rate):.{digits}f}%"


def human_delta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}h {m}m"
    if m:
        return f"{sign}{m}m {s}s"
    return f"{sign}{s}s"
