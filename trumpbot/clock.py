"""Time. All decisions in US Central via ZoneInfo, never a fixed UTC offset.

Also holds the safe formatters. The WNT bot died at 5:29 on an f-string like
f"({rate:.0%})" -- the percent sign next to the paren blew up the format
specifier. Use pct() everywhere instead.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Optional, Tuple

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


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_ZONES = {
    "central": CT,
    "ct": CT,
    "cst": CT,
    "cdt": CT,
    "chicago": CT,
    "eastern": ZoneInfo("America/New_York"),
    "et": ZoneInfo("America/New_York"),
    "est": ZoneInfo("America/New_York"),
    "edt": ZoneInfo("America/New_York"),
    "utc": UTC,
    "gmt": UTC,
    "z": UTC,
}

_TICKER_DATE = re.compile(r"-(\d{2})([A-Za-z]{3})(\d{2})$")
_TIME = re.compile(
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?",
    re.I,
)


def date_from_ticker(ticker: str) -> Optional[date]:
    """KXTRUMPMENTION-26AUG30 -> 2026-08-30."""
    if not ticker:
        return None
    m = _TICKER_DATE.search(str(ticker).strip())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2).upper(), m.group(3)
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None


def parse_when_clock(text: str, on_date: date) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse '8:00 PM central' onto on_date. Default zone is Central."""
    if not text or on_date is None:
        return None, "no time"
    raw = text.strip()
    zone = CT
    leftover = raw
    for name, tz in sorted(_ZONES.items(), key=lambda x: -len(x[0])):
        pat = re.compile(rf"(?:^|\s){re.escape(name)}(?:\s|$)", re.I)
        if pat.search(leftover):
            zone = tz
            leftover = pat.sub(" ", leftover)
            break
    leftover = leftover.strip(" ,")
    m = _TIME.search(leftover)
    if not m:
        return None, f"could not read a time in '{raw}'"
    h = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm.startswith("p") and h < 12:
        h += 12
    elif ampm.startswith("a") and h == 12:
        h = 0
    if h > 23 or minute > 59:
        return None, "hour or minute out of range"
    try:
        local = datetime.combine(on_date, time(h, minute), tzinfo=zone)
    except ValueError as exc:
        return None, str(exc)
    return to_utc(local), None


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