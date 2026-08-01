from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.store_protocol import Store
from trusted_router.synthetic.probes import rotation_candidates

_SAMPLES_PER_ROUTE_LIMIT = 48

# A route-health alert means "this route is structurally broken — quarantine
# it". Transient/capacity failures (rate limits, gateway/no-upstream, timeouts,
# dropped connections) are NOT actionable that way: the model may recover, and
# quarantining it would stop us ever re-probing it. They still count toward the
# public leaderboard's uptime display (a separate path); they just don't page.
# Structural failures — 4xx model-not-found / bad-request / auth (except 429) —
# do page.
_TRANSIENT_ERROR_TYPES = frozenset(
    {
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
    }
)
_TRANSIENT_ERROR_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


def _is_transient_failure(sample: object) -> bool:
    status = getattr(sample, "error_status", None)
    if status in _TRANSIENT_ERROR_STATUSES:
        return True
    return getattr(sample, "error_type", None) in _TRANSIENT_ERROR_TYPES


@dataclass(frozen=True)
class RouteHealthFlag:
    provider: str
    model: str
    samples: int
    failures: int
    failure_rate: float
    newest_error_type: str | None
    newest_error_message: str | None


def evaluate_route_health(
    store: Store,
    *,
    routes: list[tuple[str, str]] | None = None,
    window_hours: int = 48,
    min_samples: int = 6,
    failure_threshold: float = 0.95,
) -> list[RouteHealthFlag]:
    """Return provider/model routes whose recent failure rate is too high."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_hours)
    if routes is None:
        routes = [
            (provider, model)
            for provider, models in rotation_candidates().items()
            for model in models
        ]

    flags: list[RouteHealthFlag] = []
    for provider, model in routes:
        sample_count = 0
        failure_count = 0
        newest_error: tuple[dt.datetime, str | None, str | None] | None = None
        samples = store.provider_benchmark_samples(
            date=None,
            provider=provider,
            model=model,
            limit=_SAMPLES_PER_ROUTE_LIMIT,
        )
        for sample in samples:
            if sample.source != "synthetic":
                continue
            created_at = _parse_created_at(sample.created_at)
            if created_at is None or created_at < cutoff or sample.status == "unsupported":
                continue
            if sample.status not in {"error", "success"}:
                continue
            # Transient/capacity failures don't page (and don't dilute the
            # denominator) — they aren't a "quarantine me" signal.
            if sample.status == "error" and _is_transient_failure(sample):
                continue

            sample_count += 1
            if sample.status == "error":
                failure_count += 1
                if newest_error is None or created_at > newest_error[0]:
                    newest_error = (
                        created_at,
                        sample.error_type,
                        sample.error_message,
                    )

        if sample_count < min_samples:
            continue
        failure_rate = failure_count / sample_count
        if failure_rate < failure_threshold:
            continue
        flags.append(
            RouteHealthFlag(
                provider=provider,
                model=model,
                samples=sample_count,
                failures=failure_count,
                failure_rate=failure_rate,
                newest_error_type=newest_error[1] if newest_error else None,
                newest_error_message=newest_error[2] if newest_error else None,
            )
        )
    return flags


def report_route_health(flags: list[RouteHealthFlag]) -> None:
    """Emit one grouped Sentry message for each unhealthy route."""
    if not flags:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    for flag in flags:
        latest = " ".join(
            part for part in (flag.newest_error_type, flag.newest_error_message) if part
        ) or "unknown error"
        message = (
            f"route-health: {flag.provider}/{flag.model} {flag.failure_rate:.0%} failure "
            f"over {flag.samples} samples (latest: {latest})"
        )
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = ["route-health", flag.provider, flag.model]
            scope.set_tag("route_provider", flag.provider)
            scope.set_tag("route_model", flag.model)
            scope.set_tag("failure_rate", f"{flag.failure_rate:.4f}")
            sentry_sdk.capture_message(message, level="error")


def report_image_generation_failures(samples: list[SyntheticProbeSample]) -> None:
    """Report only image routes whose full confirmation batch failed."""
    grouped: dict[tuple[str, str], list[SyntheticProbeSample]] = {}
    for sample in samples:
        if sample.probe_type != "image_generation":
            continue
        provider = sample.selected_provider or sample.provider or "unknown"
        model = sample.selected_model or sample.model or "unknown"
        grouped.setdefault((provider, model), []).append(sample)

    confirmed_failures = [
        route_samples[-1]
        for route_samples in grouped.values()
        if route_samples and all(sample.status != "up" for sample in route_samples)
    ]
    if not confirmed_failures:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    for sample in confirmed_failures:
        provider = sample.selected_provider or sample.provider or "unknown"
        model = sample.selected_model or sample.model or "unknown"
        error_type = sample.error_type or "unknown"
        message = (
            f"image-generation-canary: {provider}/{model} failed "
            f"({error_type}, HTTP {sample.http_status or 'none'})"
        )
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = ["image-generation-canary", provider, model]
            scope.set_tag("route_provider", provider)
            scope.set_tag("route_model", model)
            scope.set_tag("probe_error_type", error_type)
            sentry_sdk.capture_message(message, level="error")


def report_video_generation_failures(samples: list[SyntheticProbeSample]) -> None:
    """Emit at most one grouped alert for each failed daily video canary."""
    failures = [
        sample
        for sample in samples
        if sample.probe_type == "video_generation" and sample.status != "up"
    ]
    if not failures:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    for sample in failures:
        provider = sample.selected_provider or sample.provider or "unknown"
        model = sample.selected_model or sample.model or "unknown"
        error_type = sample.error_type or "unknown"
        message = (
            f"video-generation-canary: {provider}/{model} failed "
            f"({error_type}, HTTP {sample.http_status or 'none'})"
        )
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = ["video-generation-canary", provider, model]
            scope.set_tag("route_provider", provider)
            scope.set_tag("route_model", model)
            scope.set_tag("probe_error_type", error_type)
            sentry_sdk.capture_message(message, level="error")


def _parse_created_at(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
