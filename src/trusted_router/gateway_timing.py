"""Request-local gateway wall-clock phases; never records request contents.

The async entrypoint owns the clock (including worker queue time). ContextVars
carry the same timing object into its serial worker; direct sync callers get
an independent scope. Background tasks run after the response snapshot.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from typing import Any, ParamSpec

from starlette.exceptions import HTTPException

from trusted_router.storage_gcp_io import SpannerRpcCounter, count_spanner_rpcs

P = ParamSpec("P")
_PHASES = ("key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms")


class GatewayTiming:
    def __init__(self, rpc_counter: SpannerRpcCounter) -> None:
        self.rpc_counter = rpc_counter
        self.started = perf_counter()
        self.changed = self.started
        self.phase: str | None = None
        self.elapsed = dict.fromkeys(_PHASES, 0.0)

    def switch(self, phase: str | None) -> None:
        now = perf_counter()
        if self.phase is not None:
            self.elapsed[self.phase] += now - self.changed
        self.changed = now
        self.phase = phase

    def snapshot(self) -> dict[str, int]:
        self.switch(self.phase)
        return {
            "total_ms": int((self.changed - self.started) * 1000),
            "spanner_rpcs": self.rpc_counter.value(),
            **{key: int(value * 1000) for key, value in self.elapsed.items()},
        }


_CURRENT: ContextVar[GatewayTiming | None] = ContextVar("gateway_timing", default=None)


def gateway_timing_phase(phase: str) -> None:
    timing = _CURRENT.get()
    if timing is not None:
        timing.switch(phase)


@contextmanager
def gateway_phase(phase: str, *, after: str | None = None) -> Iterator[None]:
    timing = _CURRENT.get()
    previous = timing.phase if timing is not None else None
    if timing is not None:
        timing.switch(phase)
    try:
        yield
    finally:
        if timing is not None:
            timing.switch(after if after is not None else previous)


@contextmanager
def _scope() -> Iterator[GatewayTiming | None]:
    if _CURRENT.get() is not None:
        yield None
        return
    with count_spanner_rpcs() as rpc_counter:
        timing = GatewayTiming(rpc_counter)
        token = _CURRENT.set(timing)
        try:
            yield timing
        except Exception as exc:
            data = {"timing": timing.snapshot()}
            # Existing status, headers and error envelope remain intact.
            if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) and "error" in exc.detail:
                exc.detail = {**exc.detail, "data": data}
            else:
                # Storage exceptions are rendered by the app's portable 503 handlers.
                # Carry only the completed snapshot past the ContextVar reset.
                exc.gateway_timing_data = data  # type: ignore[attr-defined]
            raise
        finally:
            _CURRENT.reset(token)


def timed_gateway_sync(
    func: Callable[P, dict[str, Any]],
) -> Callable[P, dict[str, Any]]:
    @wraps(func)
    def timed(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        with _scope() as timing:
            response = func(*args, **kwargs)
            if timing is not None:
                response["data"]["timing"] = timing.snapshot()
            return response
    return timed


def timed_gateway_async(
    func: Callable[P, Awaitable[dict[str, Any]]],
) -> Callable[P, Awaitable[dict[str, Any]]]:
    @wraps(func)
    async def timed(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        with _scope() as timing:
            response = await func(*args, **kwargs)
            if timing is not None:
                response["data"]["timing"] = timing.snapshot()
            return response
    return timed
