"""Background worker.

One thread, one heartbeat. Streamlit re-runs the page constantly, so nothing
that matters may live in page code -- it lives here and in Postgres.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import clock, config, kalshi, settle, store, telegram_bot
from .strategy import Engine

log = logging.getLogger("trumpbot.runner")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


class Runner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="runner")
        self.client = kalshi.KalshiClient()
        self.engine = Engine(self.client, notify=telegram_bot.send)
        self.listener = telegram_bot.Listener(engine=self.engine)
        self._stop = threading.Event()
        self._last_settle = 0.0
        self.error: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()
        self.listener.stop()

    def run(self) -> None:
        try:
            store.init()
        except Exception as exc:
            self.error = str(exc)
            log.error("Startup failed: %s", exc)
            telegram_bot.send(f"STARTUP FAILED\n{exc}")
            return

        store.set_state("started_at", clock.now_utc().isoformat())
        store.log_line("info", "Bot started")

        enabled = ", ".join(config.enabled_series().keys()) or "none"
        telegram_bot.send(
            f"Bot started.\n"
            f"Mode: {'DRY RUN' if config.dry_run() else 'LIVE'}\n"
            f"Series: {enabled}\n"
            f"Kalshi auth: {'ok' if self.client.authenticated else 'MISSING KEY'}"
        )

        self.listener.start()

        while not self._stop.is_set():
            started = time.time()
            try:
                paused = store.get_state("paused", "0") == "1"
                self.engine.run_once(allow_place=not paused)
                store.set_state("last_poll", clock.now_utc().isoformat())

                if started - self._last_settle >= config.SETTLE_SECONDS:
                    self._last_settle = started
                    try:
                        settle.run(self.client, telegram_bot.send)
                    except Exception as exc:
                        log.warning("Settlement pass failed: %s", exc)
            except Exception as exc:
                self.error = str(exc)
                log.exception("Tick failed")
                store.log_line("error", f"Tick failed: {exc}")

            elapsed = time.time() - started
            self._stop.wait(max(0.5, config.TICK_SECONDS - elapsed))


_runner: Optional[Runner] = None
_lock = threading.Lock()


def get_runner() -> Runner:
    global _runner
    with _lock:
        if _runner is None or not _runner.is_alive():
            _runner = Runner()
            _runner.start()
        return _runner
