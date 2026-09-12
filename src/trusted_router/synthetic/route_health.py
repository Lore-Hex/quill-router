from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.store_protocol import Store
from trusted_router.synthetic.probes import rotation_candidates

_SAMPLES_PER_ROUTE_LIMIT = 48
_BATCH_SAMPLE_LIMIT = 100_000
_DEGRADATION_WINDOW_HOURS = 24

# A route-health alert means "this route is structurally broken — quarantine
# it". Transient/capacity failures (rate limits, gateway/no-upstream, timeouts,
# dropped connections) are NOT actionable that way: the model may recover, and
# quarantining it would stop us ever re-probing it. They still count toward the
# public leaderboard's uptime display. Sustained outages page separately and
# must never become automatic quarantine decisions.
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
    kind: str = "structural"


def evaluate_route_health(
    store: Store,
    *,
    routes: list[tuple[str, str]] | None = None,
    window_hours: int = 48,
    min_samples: int = 6,
    failure_threshold: float = 0.95,
) -> list[RouteHealthFlag]:
    """Return provider/model routes whose recent failure rate is too high."""
    now = dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(hours=window_hours)
    if routes is None:
        routes = [
            (provider, model)
            for provider, models in rotation_candidates().items()
            for model in models
        ]
    if not routes:
        return []

    route_set = set(routes)
    samples_by_route: dict[tuple[str, str], list[ProviderBenchmarkSample]] = {
        route: [] for route in route_set
    }
    samples = store.provider_route_benchmark_samples(
        cutoff=cutoff.isoformat().replace("+00:00", "Z"),
        per_route_limit=_SAMPLES_PER_ROUTE_LIMIT,
        limit=min(_BATCH_SAMPLE_LIMIT, len(route_set) * _SAMPLES_PER_ROUTE_LIMIT),
    )
    for sample in samples:
        route = (sample.provider, sample.model)
        if route in samples_by_route:
            samples_by_route[route].append(sample)

    flags: list[RouteHealthFlag] = []
    for provider, model in routes:
        sample_count = 0
        failure_count = 0
        newest_error: tuple[dt.datetime, str | None, str | None] | None = None
        recent: list[tuple[dt.datetime, ProviderBenchmarkSample]] = []
        for sample in samples_by_route[(provider, model)]:
            if sample.source != "synthetic":
                continue
            created_at = _parse_created_at(sample.created_at)
            if created_at is None or created_at < cutoff or sample.status == "unsupported":
                continue
            if sample.status not in {"error", "success"}:
                continue
            if created_at > now:
                continue
            recent.append((created_at, sample))
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

        if sample_count < min_samples or failure_count / sample_count < failure_threshold:
            availability = _availability_flag(provider, model, recent, now, min_samples)
            if availability is not None:
                flags.append(availability)
            continue
        failure_rate = failure_count / sample_count
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


def _availability_flag(
    provider: str,
    model: str,
    recent: list[tuple[dt.datetime, ProviderBenchmarkSample]],
    now: dt.datetime,
    min_samples: int,
) -> RouteHealthFlag | None:
    recent.sort(key=lambda pair: pair[0], reverse=True)
    streak = []
    for pair in recent:
        if pair[1].status == "success":
            break
        streak.append(pair)
    if (
        len(streak) >= min_samples
        and now - streak[0][0] <= dt.timedelta(hours=6)
        and streak[0][0] - streak[-1][0] >= dt.timedelta(minutes=30)
    ):
        measured, errors, kind = streak, streak, "availability"
    else:
        # Hourly route probes cannot meet the sample floor in a two-hour window.
        # Reuse the bounded query; freshness and recovery checks still apply.
        measured = [
            pair for pair in recent
            if now - pair[0] <= dt.timedelta(hours=_DEGRADATION_WINDOW_HOURS)
        ]
        errors = [pair for pair in measured if pair[1].status == "error"]
        if (
            len(measured) < max(12, min_samples)
            or len(errors) < 4
            or len(errors) / len(measured) < 0.25
            or now - measured[0][0] > dt.timedelta(minutes=30)
            or measured[0][0] - measured[-1][0] < dt.timedelta(minutes=30)
            or all(pair[1].status == "success" for pair in measured[:6])
        ):
            return None
        kind = "degradation"
    return RouteHealthFlag(
        provider=provider, model=model, samples=len(measured), failures=len(errors),
        failure_rate=len(errors) / len(measured), newest_error_type=errors[0][1].error_type,
        newest_error_message=None, kind=kind,
    )


def report_route_health(flags: list[RouteHealthFlag]) -> None:
    """Emit one grouped Sentry message for each unhealthy route."""
    if not flags:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    for flag in flags:
        if flag.kind in {"availability", "degradation"}:
            from trusted_router.synthetic.alerts import ops_alert

            detail = (
                f"failed {flag.failures} consecutive probes over at least 30 minutes"
                if flag.kind == "availability"
                else f"failed {flag.failures}/{flag.samples} probes ({flag.failure_rate:.0%}) "
                f"over at least 30 minutes within the last {_DEGRADATION_WINDOW_HOURS} hours"
            )
            ops_alert(
                f"route-{flag.kind}: {flag.provider}/{flag.model} {detail}",
                fingerprint=["route-availability", flag.provider, flag.model],
                tags={"route_provider": flag.provider, "route_model": flag.model},
            )
            continue
        latest = (
            " ".join(part for part in (flag.newest_error_type, flag.newest_error_message) if part)
            or "unknown error"
        )
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


def report_catalog_freshness(
    *, endpoints: Iterable[ModelEndpoint] | None = None, now: dt.datetime | None = None,
) -> list[str]:
    """Alert before runtime expiry hides a provider, without reading request data."""
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.synthetic.alerts import ops_alert

    now = now or dt.datetime.now(dt.UTC)
    deadlines: dict[str, dt.datetime] = {}
    for endpoint in MODEL_ENDPOINTS.values() if endpoints is None else endpoints:
        deadline = endpoint.catalog_valid_until
        if deadline is not None:
            deadlines[endpoint.provider] = min(deadlines.get(endpoint.provider, deadline), deadline)
    flagged = []
    for provider, deadline in sorted(deadlines.items()):
        if deadline > now + dt.timedelta(hours=48):
            continue
        state = "expired" if deadline <= now else "expires within 48 hours"
        ops_alert(
            f"catalog-freshness: {provider} {state}; deadline={deadline.isoformat()}. "
            "Repair discovery before fail-closed routing removes its models.",
            fingerprint=["catalog-freshness", provider], tags={"route_provider": provider},
        )
        flagged.append(provider)
    return flagged


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
