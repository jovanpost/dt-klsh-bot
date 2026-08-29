# Setup

Deployment notes only.

## 1. Database

Run `schema.sql` in the Supabase SQL editor first. Do this before the first
deploy, and re-run any new `ALTER` before rebooting after a schema change.

Connection string: session or transaction pooler at
`aws-0-us-east-1.pooler.supabase.com`. Port `6543` is the transaction pooler,
`5432` is the session pooler. The app tries the port you give it and falls back
to the other one automatically. Do not use `db.*.supabase.co:5432` — this host
is IPv4-only.

## 2. Secrets

Streamlit → Manage app → Settings → Secrets. Nothing below goes in the repo.

```toml
DRY_RUN = true

KALSHI_KEY_ID = "..."
KALSHI_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"""

DATABASE_URL = "postgresql://postgres.PROJECT:PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres"

TELEGRAM_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
```

The private key must be triple-quoted. A raw paste fails TOML parsing.

Chat id is your own user id: message the bot once, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

Optional per-series overrides:

```toml
[series.EXAMPLE]
rest_price = 0.00
dollars = 3.00
buffer_min = 5
enabled = true
```

## 3. Deploy

Main file is `app.py`. Repo must be public for Streamlit Community Cloud.

Repo layout the Streamlit app expects:

```
app.py
trumpbot/clock.py
trumpbot/config.py
...
.github/workflows/keepalive.yml
```

Creating files through the GitHub web UI: type the full path with slashes
(`trumpbot/clock.py`) and the folder appears. Replace whole files rather than
editing a few lines — partial web edits are the most common cause of an
`unexpected indent` on deploy.

After any code or secrets change: Manage app → Reboot.

## 4. Keep-alive

`.github/workflows/keepalive.yml` pings every 10 minutes. Add a repo secret
`APP_URL` with the app's base URL. Without it the app sleeps after ~15 minutes
of no traffic.

## 5. Health check

Telegram `/status`. It should show the mode, the last poll time, open events
per series, resting orders, the next cancel time, and today's fills.

Other commands: `/today`, `/events`, `/pause`, `/resume`,
`/cancel EVENT_TICKER`, `/help`.

The web page is read-only. All controls are in Telegram.

## 6. Adding a series

One entry in `DEFAULT_SERIES` in `trumpbot/config.py`, or one `[series.X]`
block in Secrets. No other code changes.
