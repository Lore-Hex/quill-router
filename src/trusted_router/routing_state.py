"""Bounded, process-local routing hints. Never an authorization authority.

Only eligible candidates enter this module. Cache misses, restarts and expiry
fall back to normal routing; no database or network IO runs on the hot path.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any

from trusted_router.errors import api_error
from trusted_router.types import ErrorType

if TYPE_CHECKING:
    from trusted_router.catalog import Model, ModelEndpoint
    from trusted_router.routing import RoutePreferences

Thresholds = tuple[tuple[str, float], ...]
SessionKey = tuple[str, str, str, str]
PERCENTILES = {"p50": 0.50, "p75": 0.75, "p90": 0.90, "p99": 0.99}


def parse_thresholds(value: Any, field: str) -> Thresholds:
    if value is None:
        return ()
    values = value if isinstance(value, dict) else {"p50": value}
    if not values or set(values) - PERCENTILES.keys():
        raise api_error(
            400, f"provider.{field} expects p50, p75, p90 or p99", ErrorType.BAD_REQUEST
        )
    parsed = []
    for percentile, number in sorted(values.items()):
        try:
            valid = (
                not isinstance(number, bool)
                and isinstance(number, (int, float))
                and math.isfinite(number)
                and number >= 0
            )
        except OverflowError:
            valid = False
        if not valid:
            raise api_error(
                400,
                f"provider.{field} must contain finite non-negative numbers",
                ErrorType.BAD_REQUEST,
            )
        parsed.append((percentile, float(number)))
    return tuple(parsed)


def _meets(values: list[float], thresholds: Thresholds, *, lower_bound: bool) -> bool:
    if not thresholds:
        return True
    if not values:
        return False
    values.sort(reverse=lower_bound)
    # Throughput p90 is the rate achieved by at least 90% of requests,
    # hence descending order. Latency p90 is an upper-tail duration.
    for percentile, threshold in thresholds:
        measured = values[max(0, math.ceil(PERCENTILES[percentile] * len(values)) - 1)]
        if (measured < threshold) if lower_bound else (measured > threshold):
            return False
    return True


class RoutingState:
    def __init__(self, *, max_sessions: int = 10_000, max_routes: int = 1_024) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[SessionKey, tuple[float, str]] = OrderedDict()
        self._measurements: OrderedDict[tuple[str, str], deque[tuple[float, float, float]]] = (
            OrderedDict()
        )
        self._max_sessions = max_sessions
        self._max_routes = max_routes

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def remember(self, session: SessionKey, endpoint: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sessions[session] = (now, endpoint)
            self._sessions.move_to_end(session)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)

    def observe(
        self,
        endpoint: str,
        region: str,
        *,
        latency: float,
        throughput: float,
        now: float | None = None,
    ) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (latency, throughput)):
            return
        now = time.monotonic() if now is None else now
        key = (endpoint, region)
        with self._lock:
            samples = self._measurements.setdefault(key, deque(maxlen=128))
            samples.append((now, latency, throughput))
            self._measurements.move_to_end(key)
            while len(self._measurements) > self._max_routes:
                self._measurements.popitem(last=False)

    def rank(
        self,
        candidates: list[tuple[Model, ModelEndpoint]],
        prefs: RoutePreferences,
        *,
        region: str,
        session: SessionKey | None = None,
        now: float | None = None,
    ) -> list[tuple[Model, ModelEndpoint]]:
        now = time.monotonic() if now is None else now
        performance = bool(prefs.preferred_max_latency or prefs.preferred_min_throughput)
        if not performance and (session is None or prefs.order):
            return candidates
        sticky = None
        snapshots = {}
        with self._lock:
            if session is not None and not prefs.order:
                previous = self._sessions.get(session)
                if previous is not None:
                    if now - previous[0] < 600:
                        sticky = previous[1]
                    else:
                        del self._sessions[session]
            if performance:
                for _, endpoint in candidates:
                    snapshots[endpoint.id] = tuple(
                        self._measurements.get((endpoint.id, region), ())
                    )
        model_order: dict[str, int] = {}
        order = {provider: index for index, provider in enumerate(prefs.order)}
        ranked = []
        for index, (model, endpoint) in enumerate(candidates):
            model_rank = (
                model_order.setdefault(model.id, len(model_order))
                if prefs.sort_partition == "model"
                else 0
            )
            samples = [
                sample for sample in snapshots.get(endpoint.id, ()) if 0 <= now - sample[0] < 300
            ]
            preferred = not performance or (
                _meets(
                    [sample[1] for sample in samples],
                    prefs.preferred_max_latency,
                    lower_bound=False,
                )
                and _meets(
                    [sample[2] for sample in samples],
                    prefs.preferred_min_throughput,
                    lower_bound=True,
                )
            )
            # Explicit order and model fallback order win. Performance takes
            # precedence over warm-cache locality when the caller requests it.
            rank = (
                model_rank,
                order.get(endpoint.provider, len(order)),
                not preferred,
                endpoint.id != sticky,
                index,
            )
            ranked.append((rank, (model, endpoint)))
        return [candidate for _, candidate in sorted(ranked, key=lambda row: row[0])]


ROUTING_STATE = RoutingState()
