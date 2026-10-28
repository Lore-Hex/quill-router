"""Container entry point: uvicorn with a listener that outlives SIGTERM briefly.

Cloud Run retires an instance by sending SIGTERM and then, ten seconds later,
SIGKILL. Stock uvicorn closes its listening socket the instant SIGTERM
arrives and only then drains in-flight connections, so a request the Cloud
Run front end has already dispatched to the instance is refused and logged as
a 500 with zero latency and no container output. The enclave's settle and
authorize calls hit that window at every rollout (five "billing path 5xx"
alerts in the week to 2026-09-25, all at rollouts).

This runner keeps accepting for ``TR_SHUTDOWN_DRAIN_SECONDS`` after the first
SIGTERM (default and maximum 3 s) and only then hands over to uvicorn's own
shutdown, which closes the socket and waits for in-flight requests without a
request timeout. Cloud Run's SIGKILL is the only hard deadline; neither request
completion nor process exit before it is guaranteed. A second SIGTERM, or any
SIGINT, starts ordinary graceful shutdown without waiting for the drain window.

It also exits a process whose event loop has stopped. On 2026-10-28 two
europe-west4 instances went silent for one and two minutes with CPU and memory
flat; every request routed to them hung for up to two minutes and failed
(two "billing path 5xx" alerts), and nothing said where the process was
waiting. With ``TR_LOOP_STALL_EXIT_SECONDS`` set, each second of uvicorn ticks
re-arms ``faulthandler``'s watchdog, a C thread that needs neither the loop nor
the GIL. If the loop goes that long without a tick, the watchdog writes every
thread's stack to stderr and exits the process, so Cloud Run replaces it and
the stacks name the call it was blocked in. Writing the stacks can itself block
on a backed-up stderr, so a process timer re-armed with it ends the process
``STALL_BACKSTOP_SECONDS`` later by SIGALRM's default action, which runs in the
kernel. Only set it for a service whose CPU stays allocated between requests
(``--no-cpu-throttling``): a throttled idle instance sees no ticks and would
exit when CPU returns.

Usage: ``python -m trusted_router.serve`` (host 0.0.0.0, port from ``PORT``).
"""

from __future__ import annotations

from trusted_router import launch_descriptor_state

if __name__ == "__main__":
    launch_descriptor_state.claim()

import faulthandler
import logging
import math
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
MAX_DRAIN_SECONDS = 3.0
LOOP_STALL_ENV = "TR_LOOP_STALL_EXIT_SECONDS"
# uvicorn ticks every 0.1 s; re-arming starts a new watchdog thread, so do it
# once a second. A stall budget must exceed that second.
_TICKS_PER_REARM = 10
MIN_LOOP_STALL_SECONDS = 2.0
# After the stack dump's deadline, time for the dump to drain before SIGALRM.
STALL_BACKSTOP_SECONDS = 10.0

# Named explicitly: the container runs this module as `python -m`, where
# `__name__` is "__main__" and a `__name__`-based logger would sit outside the
# "trusted_router" package logger that carries the console handler, so the
# drain lines would never reach Cloud Logging (they did not, on 2026-09-25).
logger = logging.getLogger("trusted_router.serve")


def drain_seconds_from_env(environ: dict[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    raw = env.get(DRAIN_SECONDS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_DRAIN_SECONDS
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("drain seconds must be finite")
    except ValueError:
        logger.warning("serve.invalid_drain_seconds value=%r using default", raw)
        return DEFAULT_DRAIN_SECONDS
    if value < 0:
        return 0.0
    return min(value, MAX_DRAIN_SECONDS)


def loop_stall_seconds_from_env(environ: dict[str, str] | None = None) -> float | None:
    """The stall budget, or None (no watchdog) when unset or invalid."""
    env = os.environ if environ is None else environ
    raw = env.get(LOOP_STALL_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value >= MIN_LOOP_STALL_SECONDS):
        logger.warning("serve.invalid_loop_stall_seconds value=%r watchdog off", raw)
        return None
    return value


class DrainingServer(uvicorn.Server):
    """uvicorn.Server that keeps its listener open for a drain window after SIGTERM."""

    def __init__(
        self, config: uvicorn.Config, *, drain_seconds: float, stall_seconds: float | None = None,
    ) -> None:
        super().__init__(config)
        self.drain_seconds = max(0.0, min(drain_seconds, MAX_DRAIN_SECONDS))
        self.drain_deadline: float | None = None
        self.stall_seconds = stall_seconds

    async def startup(self, sockets: list | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            try:
                from trusted_router.services.async_settle_shadow_startup_harness import (
                    listener_started,
                    serving_application,
                )
                listener_started(serving_application(self))
            except Exception:
                # Startup records must not interrupt ordinary serving.
                return

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
        if self.stall_seconds is not None and counter % _TICKS_PER_REARM == 0:
            if counter == 0:
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
            faulthandler.dump_traceback_later(self.stall_seconds, exit=True)
            signal.setitimer(signal.ITIMER_REAL, self.stall_seconds + STALL_BACKSTOP_SECONDS)
        if self.drain_deadline is not None and not self.should_exit:
            if monotonic() >= self.drain_deadline:
                logger.info("serve.sigterm_drain_complete")
                self.should_exit = True
        return await super().on_tick(counter)

    async def shutdown(self, sockets: list | None = None) -> None:
        # Ticks stop here while shutdown waits on in-flight requests; Cloud
        # Run's SIGKILL bounds that wait instead.
        if self.stall_seconds is not None:
            faulthandler.cancel_dump_traceback_later()
            signal.setitimer(signal.ITIMER_REAL, 0)
        await super().shutdown(sockets=sockets)


def build_config(environ: dict[str, str] | None = None) -> uvicorn.Config:
    env = os.environ if environ is None else environ
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw_workers = env.get(name, "").strip()
        if raw_workers and raw_workers != "1":
            raise ValueError(f"{name}={raw_workers!r}: drain runner requires one worker (no supervisor)")
    raw_proxy_headers = env.get("UVICORN_PROXY_HEADERS", "true").strip().lower()
    if raw_proxy_headers not in {"1", "true", "t", "yes", "y", "on", "0", "false", "f", "no", "n", "off"}:
        raise ValueError(f"Invalid UVICORN_PROXY_HEADERS={raw_proxy_headers!r}: expected a boolean")
    raw_port = env.get("PORT", "").strip()
    port = int(raw_port) if raw_port.isdigit() else DEFAULT_PORT
    return uvicorn.Config(
        APP,
        host="0.0.0.0",  # noqa: S104 - container listener
        port=port,
        workers=1,
        timeout_keep_alive=int(env.get("UVICORN_TIMEOUT_KEEP_ALIVE", "5")),
        proxy_headers=raw_proxy_headers in {"1", "true", "t", "yes", "y", "on"},
        forwarded_allow_ips=env.get(
            "UVICORN_FORWARDED_ALLOW_IPS", env.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
        ),
    )


def main() -> int:
    try:
        config = build_config()
        server = DrainingServer(
            config, drain_seconds=drain_seconds_from_env(), stall_seconds=loop_stall_seconds_from_env(),
        )
        server.run()
        return 0
    finally:
        launch_descriptor_state.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
