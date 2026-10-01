"""Request-local gateway wall-clock phases; never records request contents.

The async entrypoint owns the clock (including worker queue time). ContextVars
carry the same timing object into its serial worker; direct sync callers get
an independent scope. Background tasks run after the response snapshot.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
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


_OUTCOME_TIMING: ContextVar[dict[str, int] | None] = ContextVar("shadow_outcome_timing", default=None)


def _outcome_settings(args: Any, kwargs: Any) -> Any:
    return kwargs.get("settings") if "settings" in kwargs else (args[2] if len(args) > 2 else None)


def _outcome_request_identity(args: Any, kwargs: Any) -> object:
    request = kwargs.get("request") if "request" in kwargs else (args[0] if args else None)
    state = getattr(request, "state", None)
    if state is None:
        return object()  # Direct callers without a request are independent.
    # Log IDs can be supplied by callers and reused on another request. Keep an
    # opaque per-request token instead, shared with this request's worker call.
    identity = getattr(state, "_shadow_request_identity", None)
    if identity is None:
        identity = object()
        state._shadow_request_identity = identity
    return identity


@contextmanager
def _authorize_outcome(name: str, args: Any, kwargs: Any) -> Iterator[None]:
    from trusted_router.services import speculation_shadow as shadow
    settings = None
    with shadow.isolate("arguments"):
        settings = _outcome_settings(args, kwargs)
    if name not in {"authorize_gateway", "_authorize_gateway_sync"} or settings is None:
        yield
        return
    stack = ExitStack()
    observation = None
    token = None
    previous_timing = None
    error = None
    failures: list[str] = []
    finalized = False

    def cleanup(callback: Callable[[], Any]) -> None:
        with shadow.isolate("scope-cleanup", observation, deferred=failures):
            callback()

    def finish() -> None:
        nonlocal finalized
        finalized = True

    def cleanup_timing() -> None:
        if token is not None:
            shadow.restore_context(_OUTCOME_TIMING, token, previous_timing)

    try:
        try:
            with shadow.isolate("scope-setup"):
                observation = stack.enter_context(shadow.outcome_scope(settings, _outcome_request_identity(args, kwargs)))
                if observation is not None:
                    previous_timing = _OUTCOME_TIMING.get()
                    token = _OUTCOME_TIMING.set({})
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                with shadow.isolate("completion", observation, deferred=failures):
                    if observation is not None:
                        timing = _OUTCOME_TIMING.get() or {}
                        if isinstance(error, Exception):
                            detail = getattr(error, "detail", {})
                            data = detail.get("data", {}) if isinstance(detail, dict) else {}
                            timing = data.get("timing", getattr(error, "gateway_timing_data", {}).get("timing", {}))
                        shadow.complete(observation, timing, error, deferred=failures)
            finally:
                try:
                    cleanup(stack.close)
                finally:
                    cleanup(cleanup_timing)
            cleanup(finish)
    finally:
        # Even an interrupted setup, completion or cleanup closes coverage.
        # Recording runs only after both independent restoration attempts.
        try:
            for site in failures:
                shadow.record_loss(site, observation)
        finally:
            if not finalized or (error is not None and not isinstance(error, Exception)):
                shadow.record_loss("aborted", observation)


def _save_outcome_timing(response: dict[str, Any]) -> None:
    target = _OUTCOME_TIMING.get()
    if target is not None:
        target.update(response["data"].get("timing", {}))


def timed_gateway_sync(
    func: Callable[P, dict[str, Any]],
) -> Callable[P, dict[str, Any]]:
    authorize = func.__name__ == "_authorize_gateway_sync"
    @wraps(func)
    def timed(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        settings: Any = kwargs.get("settings") if "settings" in kwargs else (args[2] if len(args) > 2 else None)
        if not authorize or settings is None or not settings.speculative_provider_shadow_enabled:
            with _scope() as timing:
                response = func(*args, **kwargs)
                if timing is not None:
                    response["data"]["timing"] = timing.snapshot()
                return response
        with _authorize_outcome(func.__name__, args, kwargs):
            with _scope() as timing:
                response = func(*args, **kwargs)
                if timing is not None:
                    response["data"]["timing"] = timing.snapshot()
                from trusted_router.services.speculation_shadow import isolate
                with isolate("timing"):
                    _save_outcome_timing(response)
                return response
    return timed


def timed_gateway_async(
    func: Callable[P, Awaitable[dict[str, Any]]],
) -> Callable[P, Awaitable[dict[str, Any]]]:
    authorize = func.__name__ == "authorize_gateway"
    @wraps(func)
    async def timed(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        settings: Any = kwargs.get("settings") if "settings" in kwargs else (args[2] if len(args) > 2 else None)
        if not authorize or settings is None or not settings.speculative_provider_shadow_enabled:
            with _scope() as timing:
                response = await func(*args, **kwargs)
                if timing is not None:
                    response["data"]["timing"] = timing.snapshot()
                return response
        with _authorize_outcome(func.__name__, args, kwargs):
            with _scope() as timing:
                response = await func(*args, **kwargs)
                if timing is not None:
                    response["data"]["timing"] = timing.snapshot()
                from trusted_router.services.speculation_shadow import isolate
                with isolate("timing"):
                    _save_outcome_timing(response)
                return response
    return timed
