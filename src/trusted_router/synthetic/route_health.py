from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trusted_router.store_protocol import Store
from trusted_router.synthetic.probes import rotation_candidates

_SAMPLES_PER_ROUTE_LIMIT = 48


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


def _parse_created_at(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)
