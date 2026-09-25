"""Container entry point: uvicorn with a listener that outlives SIGTERM briefly.

Cloud Run retires an instance by sending SIGTERM and then, ten seconds later,
SIGKILL. Stock uvicorn closes its listening socket the instant SIGTERM
arrives and only then drains in-flight connections, so a request the Cloud
Run front end has already dispatched to the instance is refused and logged as
a 500 with zero latency and no container output. The enclave's settle and
authorize calls hit that window at every rollout (five "billing path 5xx"
alerts in the week to 2026-09-25, all at rollouts).

This runner keeps accepting for ``TR_SHUTDOWN_DRAIN_SECONDS`` after the first
SIGTERM (default 3 s, never more than 8 s so the normal graceful shutdown
still fits before SIGKILL) and only then hands over to uvicorn's own
shutdown, which closes the socket and waits for in-flight requests. A second
SIGTERM, or any SIGINT, exits immediately so local runs and forced stops
behave as before.

Usage: ``python -m trusted_router.serve`` (host 0.0.0.0, port from ``PORT``).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from time import monotonic
from types import FrameType

import uvicorn

APP = "trusted_router.main:app"
DEFAULT_PORT = 8080
DRAIN_SECONDS_ENV = "TR_SHUTDOWN_DRAIN_SECONDS"
DEFAULT_DRAIN_SECONDS = 3.0
# Cloud Run sends SIGKILL 10 s after SIGTERM; leave room for the graceful
# shutdown that follows the drain.
MAX_DRAIN_SECONDS = 8.0

logger = logging.getLogger(__name__)


def drain_seconds_from_env(environ: dict[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    raw = env.get(DRAIN_SECONDS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_DRAIN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("serve.invalid_drain_seconds value=%r using default", raw)
        return DEFAULT_DRAIN_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_DRAIN_SECONDS)


class DrainingServer(uvicorn.Server):
    """uvicorn.Server that keeps its listener open for a drain window after SIGTERM."""

    def __init__(self, config: uvicorn.Config, *, drain_seconds: float) -> None:
        super().__init__(config)
        self.drain_seconds = max(0.0, min(drain_seconds, MAX_DRAIN_SECONDS))
        self.drain_deadline: float | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if sig == signal.SIGTERM and self.drain_deadline is None and self.drain_seconds > 0:
            # Keep accepting: the front end may still dispatch a request that
            # it queued before it learned this instance is going away.
            self.drain_deadline = monotonic() + self.drain_seconds
            self._captured_signals.append(sig)
            logger.info(
                "serve.sigterm_drain_started drain_seconds=%.1f", self.drain_seconds
            )
            return
        super().handle_exit(sig, frame)

    async def on_tick(self, counter: int) -> bool:
        if self.drain_deadline is not None and not self.should_exit:
            if monotonic() >= self.drain_deadline:
                logger.info("serve.sigterm_drain_complete")
                self.should_exit = True
        return await super().on_tick(counter)


def build_config(environ: dict[str, str] | None = None) -> uvicorn.Config:
    env = os.environ if environ is None else environ
    raw_port = env.get("PORT", "").strip()
    port = int(raw_port) if raw_port.isdigit() else DEFAULT_PORT
    return uvicorn.Config(APP, host="0.0.0.0", port=port)  # noqa: S104 - container listener


def main() -> int:
    config = build_config()
    server = DrainingServer(config, drain_seconds=drain_seconds_from_env())
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
