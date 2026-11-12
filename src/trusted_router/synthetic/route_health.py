from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    scrub_provider_error_message,
)
from trusted_router.store_protocol import Store
from trusted_router.synthetic.probes import rotation_candidates

_SAMPLES_PER_ROUTE_LIMIT = 48
_BATCH_SAMPLE_LIMIT = 100_000
_DEGRADATION_WINDOW_HOURS = 24
_CONFIGURATION_ERROR_TYPES = frozenset({
    "provider_auth_config", "unsupported_route", "probe_config_error",
})
# Same bound the sample write paths apply (probes._rotation_error_sample and
# the internal benchmark ingest), re-applied here so an alert never carries
# more than a stored sample could.
_ALERT_ERROR_MESSAGE_MAX_CHARS = 300

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
        "TimeoutError",
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
        configuration = _configuration_flag(
            provider, model, samples_by_route[(provider, model)], now, cutoff, min_samples,
        )
        if configuration is not None:
            flags.append(configuration)
            continue
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


# A listed route the provider refuses to serve. These thresholds feed the
# hourly price refresh's automatic route hold (scripts/pricing/
# unserved_route_holds.py), so they are deliberately stricter than the
# route-configuration alert above: at least six consecutive configured-route
# probes, all answered with a model-not-served class, spanning at least three
# hours, the newest within twelve. Transient classes (timeouts, 429/5xx) and
# probe_config_error break the streak, as does any success.
#
# The freshness and gap bounds are sized to the real probe cadence, not to a
# steady one. Configured-route probes are rotation picks: uniform over ~80
# providers, then uniform over that provider's models (probes.
# choose_rotation_target). One route is probed a few times a day at random
# intervals; gaps of 6-11 hours between two probes of the same route are
# routine (makora/meta-models/muse-spark-open-816b-a42b-nvfp4, 2026-11-11
# 19:00Z to 11-12 05:30Z). With a two-hour newest bound and a six-hour gap
# bound no real streak ever qualified (#2504 held nothing while two routes
# had failed every probe for over a day). Freshness is still proven: the
# sibling success must fall within UNSERVED_SIBLING_SUCCESS_WINDOW, so the
# monitors are probing this provider now and its account serves.
UNSERVED_MIN_SAMPLES = 6
UNSERVED_MIN_SPAN = dt.timedelta(hours=3)
UNSERVED_MAX_AGE = dt.timedelta(hours=12)
# provider_auth_config counts only together with the sibling rule below: a 403
# on one model while the same key serves another model is per-model access,
# not a broken credential.
_UNSERVED_ERROR_TYPES = frozenset({"unsupported_route", "provider_auth_config"})
# The provider must have served a different model since this route began
# failing, within the last six hours. Without that proof
# the failure may be ours (a broken adapter, a revoked key, a blocked egress)
# and holding routes one by one would hide a provider-wide problem.
UNSERVED_SIBLING_SUCCESS_WINDOW = dt.timedelta(hours=6)
# A held route is not probed for the whole hold (scripts/pricing/
# unserved_route_holds.HOLD_HOURS, 24h), so a gap this long separates two
# episodes. It must stay well under the hold length and well over the
# routine gap between two rotation picks of one route; a test pins both.
UNSERVED_MAX_PROBE_GAP = dt.timedelta(hours=16)


@dataclass(frozen=True)
class UnservedRoute:
    provider: str
    model: str
    samples: int
    first_failure_at: str
    last_failure_at: str
    error_types: tuple[str, ...]


def evaluate_unserved_routes(
    store: Store,
    *,
    routes: list[tuple[str, str]] | None = None,
    now: dt.datetime | None = None,
    window_hours: int = 48,
) -> list[UnservedRoute]:
    """Routable routes the provider lists but has refused for hours.

    Reads the same bounded per-route sample query as evaluate_route_health.
    Raises whatever the analytics read raises: callers must treat an error as
    "no evidence", never as "no unserved routes".
    """
    now = now or dt.datetime.now(dt.UTC)
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
    samples = store.provider_route_benchmark_samples(
        cutoff=cutoff.isoformat().replace("+00:00", "Z"),
        per_route_limit=_SAMPLES_PER_ROUTE_LIMIT,
        limit=min(_BATCH_SAMPLE_LIMIT, len(route_set) * _SAMPLES_PER_ROUTE_LIMIT),
    )
    by_route: dict[tuple[str, str], list[tuple[dt.datetime, ProviderBenchmarkSample]]] = {}
    successes: dict[str, list[tuple[dt.datetime, str]]] = {}
    sibling_cutoff = now - UNSERVED_SIBLING_SUCCESS_WINDOW
    for sample in samples:
        created = _parse_created_at(sample.created_at)
        if created is None or created < cutoff or created > now:
            continue
        if sample.status == "success":
            # A served probe proves the route (or, for a sibling, the provider
            # account) works at that moment. Every Store implementation of
            # provider_route_benchmark_samples returns synthetic probes only;
            # a non-probe success, should one ever arrive, is kept as the
            # conservative reading: it breaks this route's streak but is
            # never sibling proof, which only our own probe credential gives.
            if created >= sibling_cutoff and sample.source == "synthetic":
                successes.setdefault(sample.provider, []).append((created, sample.model))
        elif sample.source != "synthetic":
            continue
        route = (sample.provider, sample.model)
        if route in route_set:
            by_route.setdefault(route, []).append((created, sample))

    unserved: list[UnservedRoute] = []
    for provider, model in sorted(route_set):
        recent = sorted(by_route.get((provider, model), []), key=lambda pair: pair[0], reverse=True)
        streak: list[tuple[dt.datetime, ProviderBenchmarkSample]] = []
        for created, sample in recent:
            if (
                sample.status not in {"unsupported", "error"}
                or sample.error_type not in _UNSERVED_ERROR_TYPES
                # A passed-through 429/5xx keeps its model-ish error type;
                # it is still capacity, not a refusal to serve.
                or _is_transient_failure(sample)
                # Probes stop while a route is held. Failures from before a
                # gap belong to an earlier episode and must not re-arm a
                # freshly lifted route on its first new probe.
                or (streak and streak[-1][0] - created > UNSERVED_MAX_PROBE_GAP)
            ):
                break
            streak.append((created, sample))
        if (
            len(streak) < UNSERVED_MIN_SAMPLES
            or now - streak[0][0] > UNSERVED_MAX_AGE
            or streak[0][0] - streak[-1][0] < UNSERVED_MIN_SPAN
            # The sibling success must come after this route began failing:
            # a success from before a provider-wide break proves nothing.
            or not any(
                sibling != model and created >= streak[-1][0]
                for created, sibling in successes.get(provider, [])
            )
        ):
            continue
        unserved.append(
            UnservedRoute(
                provider=provider,
                model=model,
                samples=len(streak),
                first_failure_at=_iso_z(streak[-1][0]),
                last_failure_at=_iso_z(streak[0][0]),
                error_types=tuple(sorted({str(sample.error_type) for _, sample in streak})),
            )
        )
    return unserved


def _iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _configuration_flag(
    provider: str,
    model: str,
    samples: list[ProviderBenchmarkSample],
    now: dt.datetime,
    cutoff: dt.datetime,
    min_samples: int,
) -> RouteHealthFlag | None:
    # Exclusion from provider uptime is not exclusion from operational alerts.
    # Only current routable pairs reach this evaluator. Do not auto-quarantine:
    # auth and probe configuration must remain observable until recovery.
    recent = sorted(
        (
            (created, sample) for sample in samples
            if sample.source == "synthetic"
            and (created := _parse_created_at(sample.created_at)) is not None
            and cutoff <= created <= now
        ),
        key=lambda pair: pair[0], reverse=True,
    )
    streak = []
    for pair in recent:
        sample = pair[1]
        if sample.status not in {"unsupported", "error"} or (
            sample.error_type not in _CONFIGURATION_ERROR_TYPES
        ):
            break
        streak.append(pair)
    if (
        len(streak) < min_samples
        or now - streak[0][0] > dt.timedelta(hours=6)
        or streak[0][0] - streak[-1][0] < dt.timedelta(minutes=30)
    ):
        return None
    return RouteHealthFlag(
        provider=provider, model=model, samples=len(streak), failures=len(streak),
        failure_rate=1.0, newest_error_type=streak[0][1].error_type,
        newest_error_message=_alert_error_message(streak[0][1].error_message),
        kind="configuration",
    )


def _alert_error_message(message: str | None) -> str | None:
    # Configuration failures are probe/auth/route rejections, and the reason is
    # only in the provider's message (Sentry QUILL-ROUTER-FF carried just
    # probe_config_error). Stored messages are already scrubbed and bounded at
    # write time; scrub and bound again with the same helper so the alert can
    # never carry key-shaped material regardless of which writer stored it.
    if not message:
        return None
    return scrub_provider_error_message(message)[:_ALERT_ERROR_MESSAGE_MAX_CHARS] or None


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
        if flag.kind == "configuration":
            from trusted_router.synthetic.alerts import ops_alert

            latest = _alert_error_message(flag.newest_error_message)
            ops_alert(
                f"route-configuration: {flag.provider}/{flag.model} failed "
                f"{flag.failures} consecutive configured-route probes over at least 30 minutes "
                f"({flag.newest_error_type}); excluded from provider uptime, not operations"
                + (f"; latest: {latest}" if latest else ""),
                fingerprint=["route-configuration", flag.provider, flag.model],
                tags={"route_provider": flag.provider, "route_model": flag.model},
            )
            continue
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


ROUTE_HEALTH_UNEVALUABLE_FINGERPRINT = ["route-health-unevaluable", "analytics_unavailable"]


def report_route_health_unevaluable(exc: BaseException) -> None:
    """One grouped alert: route health could not be evaluated at all.

    Distinct from every per-route fingerprint on purpose. While operational
    analytics is unreachable no route was judged, so silence from the
    per-route alerts means "blind", never "healthy"; this issue is what says
    so, and it folds every pass on every monitor into one Sentry issue.
    """
    from trusted_router.synthetic.alerts import ops_alert

    ops_alert(
        "route-health: cannot evaluate route health; operational analytics "
        f"(ClickHouse) is unavailable ({type(exc).__name__}); no routes were evaluated",
        fingerprint=list(ROUTE_HEALTH_UNEVALUABLE_FINGERPRINT),
        tags={"error_class": type(exc).__name__},
    )


def report_catalog_freshness(
    *, endpoints: Iterable[ModelEndpoint] | None = None, now: dt.datetime | None = None,
) -> list[str]:
    """Alert before runtime expiry hides a provider, without reading request data."""
    from trusted_router.catalog import MODEL_ENDPOINTS
    from trusted_router.synthetic.alerts import ops_alert

    now = now or dt.datetime.now(dt.UTC)
    soon = now + dt.timedelta(hours=48)
    by_provider: dict[str, list[tuple[dt.datetime, ModelEndpoint]]] = {}
    for endpoint in MODEL_ENDPOINTS.values() if endpoints is None else endpoints:
        if endpoint.catalog_valid_until is not None:
            by_provider.setdefault(endpoint.provider, []).append((endpoint.catalog_valid_until, endpoint))
    flagged = []
    for provider, routes in sorted(by_provider.items()):
        expiring = [route for deadline, route in routes if deadline <= soon]
        if not expiring:
            continue
        deadline = min(deadline for deadline, _route in routes)
        expired = deadline <= now
        if len(expiring) == len(routes):
            state = "expired" if expired else "expires within 48 hours"
            message = (
                f"catalog-freshness: {provider} {state}; deadline={deadline.isoformat()}. "
                "Repair discovery before fail-closed routing removes its models."
            )
        else:
            # A row-level price deadline (one model's promotion) ends only
            # that model's routes; the provider's other routes keep serving.
            models = ", ".join(sorted({route.model_id for route in expiring}))
            message = (
                f"catalog-freshness: {provider} models {models} "
                f"{'expired' if expired else 'expire within 48 hours'}; "
                f"deadline={deadline.isoformat()}. Only these models' routes are affected; "
                "the next price refresh after the deadline restores them at the current price."
            )
        ops_alert(message, fingerprint=["catalog-freshness", provider], tags={"route_provider": provider})
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
