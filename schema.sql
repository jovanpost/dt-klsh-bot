-- Run this in the Supabase SQL editor BEFORE the first deploy.
-- If you ever add a field to an order dict in strategy.py, add it here, add
-- the Column() in store.py, and run the ALTER before you reboot the app.
-- create_all() does not add columns to a table that already exists.

create table if not exists tm_events (
  event_ticker    text primary key,
  series          text not null,
  title           text,
  subtitle        text,
  discovered_at   timestamptz,
  occurrence_at   timestamptz,
  close_at        timestamptz,
  cancel_at       timestamptz,
  cancel_source   text,
  traded          boolean default false,
  markets_seen    integer default 0,
  orders_placed   integer default 0,
  last_seen_at    timestamptz,
  cancelled_at    timestamptz,
  notified_open   boolean default false,
  notified_cancel boolean default false,
  status          text
);

create table if not exists tm_orders (
  id             bigserial primary key,
  series         text not null,
  event_ticker   text not null,
  market_ticker  text not null,
  market_title   text,
  side           text default 'no',
  limit_price    numeric,
  count          numeric,          -- fractional on purpose (8.571429, 12.0)
  dollars        numeric,
  dry_run        boolean default true,
  took_at_open   boolean default false,
  quote_at_place numeric,
  placed_at      timestamptz,
  order_id       text,
  status         text default 'resting',
  filled_at      timestamptz,
  fill_price     numeric,
  cancelled_at   timestamptz,
  result         text,
  settled_at     timestamptz,
  pnl            numeric,
  fee_est        numeric
);

create unique index if not exists tm_orders_unique
  on tm_orders (event_ticker, market_ticker, dry_run);
create index if not exists tm_orders_status on tm_orders (status);
create index if not exists tm_orders_result on tm_orders (result);

create table if not exists tm_ticks (
  id            bigserial primary key,
  ts            timestamptz,
  series        text,
  event_ticker  text,
  market_ticker text,
  yes_bid       numeric,
  yes_ask       numeric,
  no_bid        numeric,
  no_ask        numeric,
  last          numeric,
  volume        numeric
);
create index if not exists tm_ticks_market on tm_ticks (market_ticker, ts);

create table if not exists tm_state (
  key        text primary key,
  value      text,
  updated_at timestamptz
);

create table if not exists tm_log (
  id      bigserial primary key,
  ts      timestamptz,
  level   text,
  message text
);
