"""Storage.

The Friday-open crash on the WNT bot was a schema drift: strategy.py wrote
kwargs that the SQLAlchemy Table did not define, so every insert raised
"Unconsumed column names" and the bot missed the open by 12 minutes.

Three things must always agree:
  1. schema.sql / alter.sql  (what Postgres actually has)
  2. the Table() objects below
  3. ORDER_FIELDS / EVENT_FIELDS, which is what record_* is allowed to write

assert_schema() checks all three at boot and refuses to start on a mismatch.
Add a field? Add it in all three places in the same change, and run the ALTER
in Supabase BEFORE you reboot the app.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, Numeric,
                        String, Table, Text, and_, create_engine, inspect,
                        select, text, update)
from sqlalchemy.engine import Engine

from . import clock, config

log = logging.getLogger("trumpbot.store")

P = config.TABLE_PREFIX
metadata = MetaData()

events = Table(
    f"{P}events", metadata,
    Column("event_ticker", Text, primary_key=True),
    Column("series", Text, nullable=False),
    Column("mode", Text),                 # locked at discovery, never rewritten
    Column("title", Text),
    Column("subtitle", Text),
    Column("discovered_at", DateTime(timezone=True)),
    Column("discovered_at_open", Boolean, default=False),
    Column("occurrence_at", DateTime(timezone=True)),
    Column("close_at", DateTime(timezone=True)),
    Column("cancel_at", DateTime(timezone=True)),
    Column("cancel_source", Text),
    Column("nagged_at", DateTime(timezone=True)),
    Column("traded", Boolean, default=False),
    Column("markets_seen", Integer, default=0),
    Column("orders_placed", Integer, default=0),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("notified_open", Boolean, default=False),
    Column("notified_cancel", Boolean, default=False),
    Column("status", Text),
)

orders = Table(
    f"{P}orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("series", Text, nullable=False),
    Column("mode", Text),                 # the mode this row was created under
    Column("event_ticker", Text, nullable=False),
    Column("market_ticker", Text, nullable=False),
    Column("market_title", Text),
    Column("side", Text, default="no"),
    Column("limit_price", Numeric),
    Column("count", Numeric),
    Column("dollars", Numeric),
    Column("dry_run", Boolean, default=True),   # kept for history; mode wins
    Column("took_at_open", Boolean, default=False),
    Column("quote_at_place", Numeric),
    Column("placed_at", DateTime(timezone=True)),
    Column("order_id", Text),
    Column("status", Text, default="resting"),
    Column("filled_at", DateTime(timezone=True)),
    Column("fill_price", Numeric),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("result", Text),
    Column("settled_at", DateTime(timezone=True)),
    Column("pnl", Numeric),
    Column("fee_est", Numeric),
)

ticks = Table(
    f"{P}ticks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True)),
    Column("series", Text),
    Column("event_ticker", Text),
    Column("market_ticker", Text),
    Column("yes_bid", Numeric),
    Column("yes_ask", Numeric),
    Column("no_bid", Numeric),
    Column("no_ask", Numeric),
    Column("last", Numeric),
    Column("volume", Numeric),
)

state = Table(
    f"{P}state", metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
    Column("updated_at", DateTime(timezone=True)),
)

applog = Table(
    f"{P}log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True)),
    Column("level", Text),
    Column("message", Text),
)

# The single source of truth for what record_* may write.
EVENT_FIELDS = [c.name for c in events.columns]
ORDER_FIELDS = [c.name for c in orders.columns if c.name != "id"]

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


# ------------------------------------------------------------------ engine ---

def _candidate_urls() -> List[str]:
    url = config.get("DATABASE_URL")
    if not url:
        return []
    url = str(url).strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    urls = [url]
    # Documented fallback: transaction pooler 6543 refuses connections during
    # Supabase incidents; session pooler 5432 usually still answers.
    if ":6543" in url:
        urls.append(url.replace(":6543", ":5432"))
    elif ":5432" in url:
        urls.append(url.replace(":5432", ":6543"))
    return urls


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        last: Optional[Exception] = None
        for url in _candidate_urls():
            try:
                kwargs: Dict[str, Any] = {"pool_pre_ping": True}
                if url.startswith("postgresql"):
                    kwargs.update(pool_size=3, max_overflow=2, pool_recycle=300,
                                  connect_args={"connect_timeout": 10})
                eng = create_engine(url, **kwargs)
                with eng.connect() as conn:
                    conn.execute(text("select 1"))
                _engine = eng
                log.info("Database connected on port %s", url.split(":")[-1].split("/")[0])
                return _engine
            except Exception as exc:
                last = exc
                log.warning("Database connect failed: %s", exc)
        raise RuntimeError(f"No usable DATABASE_URL. Last error: {last}")


def init() -> None:
    eng = get_engine()
    metadata.create_all(eng)   # creates missing TABLES only, never columns
    assert_schema()


def assert_schema() -> None:
    """Fail loudly at boot instead of quietly at the open."""
    eng = get_engine()
    insp = inspect(eng)
    problems: List[str] = []
    for table, fields in ((events, EVENT_FIELDS), (orders, ORDER_FIELDS),
                          (ticks, [c.name for c in ticks.columns]),
                          (state, [c.name for c in state.columns]),
                          (applog, [c.name for c in applog.columns])):
        if not insp.has_table(table.name):
            problems.append(f"table {table.name} does not exist")
            continue
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        model_cols = {c.name for c in table.columns}
        missing_in_db = model_cols - db_cols
        if missing_in_db:
            problems.append(
                f"{table.name}: Postgres is missing {sorted(missing_in_db)} "
                f"-- run alter.sql before starting")
        missing_in_model = set(fields) - model_cols
        if missing_in_model:
            problems.append(f"{table.name}: write list has {sorted(missing_in_model)} "
                            f"not defined on the Table")
    if problems:
        raise RuntimeError("Schema mismatch:\n  " + "\n  ".join(problems))


def _filtered(row: Dict[str, Any], allowed: List[str]) -> Dict[str, Any]:
    """Drop anything not in the allowed list, so a stray key cannot crash an
    insert with 'Unconsumed column names'."""
    extra = set(row) - set(allowed)
    if extra:
        log.warning("Dropping unknown fields %s", sorted(extra))
    return {k: v for k, v in row.items() if k in allowed}


def _pg_insert(table):
    from sqlalchemy.dialects.postgresql import insert as pg
    from sqlalchemy.dialects.sqlite import insert as lite
    name = get_engine().dialect.name
    return lite(table) if name == "sqlite" else pg(table)


# ------------------------------------------------------------------ events ---

def upsert_event(row: Dict[str, Any]) -> None:
    row = _filtered(row, EVENT_FIELDS)
    stmt = _pg_insert(events).values(**row)
    updates = {k: stmt.excluded[k] for k in row if k != "event_ticker"}
    stmt = stmt.on_conflict_do_update(index_elements=["event_ticker"], set_=updates)
    with get_engine().begin() as conn:
        conn.execute(stmt)


def get_event(event_ticker: str) -> Optional[Dict[str, Any]]:
    with get_engine().connect() as conn:
        r = conn.execute(select(events).where(events.c.event_ticker == event_ticker)).mappings().first()
    return dict(r) if r else None


def live_events() -> List[Dict[str, Any]]:
    """Events we still act on: not cancelled yet."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(events).where(events.c.cancelled_at.is_(None))
            .order_by(events.c.cancel_at.asc().nulls_last())
        ).mappings().all()
    return [dict(r) for r in rows]


def all_events(limit: int = 400) -> List[Dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(events).order_by(events.c.discovered_at.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def mark_event(event_ticker: str, **fields) -> None:
    fields = _filtered(fields, EVENT_FIELDS)
    if not fields:
        return
    with get_engine().begin() as conn:
        conn.execute(update(events).where(events.c.event_ticker == event_ticker).values(**fields))


# ------------------------------------------------------------------ orders ---

def record_order(row: Dict[str, Any]) -> None:
    row = _filtered(row, ORDER_FIELDS)
    with get_engine().begin() as conn:
        conn.execute(orders.insert().values(**row))


def existing_market_tickers(event_ticker: str, mode: str) -> set:
    """Markets in this event we already have a row for, under this mode.

    Keyed on mode, not dry_run: a DRY row and a LIVE row for the same market
    are different rows and must not shadow each other.
    """
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(orders.c.market_ticker).where(
                and_(orders.c.event_ticker == event_ticker,
                     orders.c.mode == mode))
        ).all()
    return {r[0] for r in rows}


def resting_orders(event_ticker: Optional[str] = None,
                   mode: Optional[str] = None) -> List[Dict[str, Any]]:
    q = select(orders).where(orders.c.status == "resting")
    if event_ticker:
        q = q.where(orders.c.event_ticker == event_ticker)
    if mode:
        q = q.where(orders.c.mode == mode)
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings().all()]


def update_order(order_row_id: int, **fields) -> None:
    fields = _filtered(fields, ORDER_FIELDS)
    if not fields:
        return
    with get_engine().begin() as conn:
        conn.execute(update(orders).where(orders.c.id == order_row_id).values(**fields))


def cancel_resting_for_event(event_ticker: str, when: datetime) -> int:
    with get_engine().begin() as conn:
        res = conn.execute(
            update(orders)
            .where(and_(orders.c.event_ticker == event_ticker,
                        orders.c.status == "resting"))
            .values(status="cancelled", cancelled_at=when))
    return res.rowcount or 0


def unsettled_orders() -> List[Dict[str, Any]]:
    """Everything without a result yet -- includes DRY AND unfilled rows.

    settle.py on the WNT bot skipped dry-run rows and the dashboard sat at
    $0.00 forever. Do not add a mode filter here.
    """
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(orders).where(and_(orders.c.result.is_(None),
                                      orders.c.status != "rejected"))
        ).mappings().all()
    return [dict(r) for r in rows]


def orders_for_dashboard(limit: int = 2000) -> List[Dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(orders).order_by(orders.c.placed_at.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


# ------------------------------------------------------- ticks / state / log ---

def record_tick(row: Dict[str, Any]) -> None:
    row = _filtered(row, [c.name for c in ticks.columns])
    with get_engine().begin() as conn:
        conn.execute(ticks.insert().values(**row))


def set_state(key: str, value: str) -> None:
    stmt = _pg_insert(state).values(key=key, value=str(value), updated_at=clock.now_utc())
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at})
    with get_engine().begin() as conn:
        conn.execute(stmt)


def get_state(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_engine().connect() as conn:
        r = conn.execute(select(state.c.value).where(state.c.key == key)).first()
    return r[0] if r else default


def delete_state(key: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(state.delete().where(state.c.key == key))


# -------------------------------------------------------------- series mode ---

def get_series_mode(series: str) -> Optional[str]:
    """DB override set by Telegram /mode. None means fall back to config."""
    return get_state(f"mode:{series}")


def set_series_mode(series: str, mode: str) -> None:
    set_state(f"mode:{series}", mode)
    log_line("info", f"{series} mode set to {mode}")


def log_line(level: str, message: str) -> None:
    try:
        with get_engine().begin() as conn:
            conn.execute(applog.insert().values(ts=clock.now_utc(), level=level,
                                                message=message[:2000]))
    except Exception:
        pass


def recent_log(limit: int = 60) -> List[Dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(applog).order_by(applog.c.id.desc()).limit(limit)).mappings().all()
    return [dict(r) for r in rows]
