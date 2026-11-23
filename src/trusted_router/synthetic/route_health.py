from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_error_detail import parse_error_message
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    scrub_provider_error_message,
)
from trusted_router.store_protocol import Store
from trusted_router.synthetic.probes import rotation_candidates

logger = logging.getLogger(__name__)

_SAMPLES_PER_ROUTE_LIMIT = 48
_BATCH_SAMPLE_LIMIT = 100_000
_DEGRADATION_WINDOW_HOURS = 24
# Partial degradation (a flaky route: >=25% failures, not a full outage) is
# reported once a day as one grouped digest, not every hourly pass. Chronic
# flappers (near-ai, fortanix glm-5.3-flash at 25-30% for days) paged hourly
# with nothing to act on. Full outages (kind="availability") still page every
# pass. The digest fingerprint carries the date, so each day's digest is one
# new Sentry issue (one email) and every pass in the digest hour folds into it.
DEGRADATION_DIGEST_HOUR_UTC = 14
_CONFIGURATION_ERROR_TYPES = frozenset({
    "provider_auth_config", "unsupported_route", "probe_config_error",
})
# A provider refusing OUR managed account (HTTP 401/403 on Credits traffic):
# a revoked key, an exhausted plan allowance or billing, or withdrawn model
# access. Configured-route probes reach one route only a few times a day, so
# the six-probe streak below alerted on Maple's exhausted allowance about 8.5
# hours after real traffic began failing (2026-11-20). Real Credits traffic
# and probes both count here; BYOK samples never do, because a 403 there is
# the customer's own key.
ACCOUNT_REFUSAL_STATUSES = frozenset({401, 403})
# provider_auth_config: probes; provider_auth_error: inline inference
# (services/inference_errors.py); provider_error: other organic writers.
ACCOUNT_REFUSAL_ERROR_TYPES = frozenset({
    "provider_auth_config", "provider_auth_error", "provider_error",
})
ACCOUNT_REFUSAL_MIN_SAMPLES = 3
# A streak qualifies when it spans this long, or when every one of a route's
# newest ACCOUNT_REFUSAL_SATURATED samples is a refusal (a busy route refused
# 20 times in a row is no burst, whatever the span).
ACCOUNT_REFUSAL_MIN_SPAN = dt.timedelta(minutes=10)
ACCOUNT_REFUSAL_SATURATED = 20
ACCOUNT_REFUSAL_MAX_AGE = dt.timedelta(hours=1)
# Total rows: one full per-route window for every candidate route, bounded.
ACCOUNT_REFUSAL_READ_LIMIT_MAX = 50_000
# The fast pass reports a refused route once per hour per process; the hourly
# sweep keeps re-reporting on its own cadence while the refusal lasts.
ACCOUNT_REFUSAL_FAST_COOLDOWN = dt.timedelta(hours=1)
_FAST_REFUSAL_REPORTED: dict[tuple[str, str], dt.datetime] = {}
_FAST_REFUSAL_LOCK = threading.Lock()
# Same bound the sample write paths apply (probes._rotation_error_sample and
# the internal benchmark ingest), re-applied here so an alert never carries
# more than a stored sample could.
_ALERT_ERROR_MESSAGE_MAX_CHARS = 300
# error_type can echo a provider's own error code; admit only short
# identifier-shaped values into availability alerts.
_ALERT_ERROR_TYPE_MAX_CHARS = 64
_ALERT_ERROR_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")

# A route-health alert means "this route is structurally broken — quarantine
# it". Transient/capacity failures (rate limits, gateway/no-upstream, timeouts,
# dropped connections) are NOT actionable that way: the model may recover, and
# quarantining it would stop us ever re-probing it. They still count toward the
# public leaderboard's uptime display. Sustained outages page separately and
# must never become remediator quarantine decisions; the only automatic action
# on an outage is the bounded, self-expiring hold of evaluate_unserved_routes
# (UNAVAILABLE_MIN_SPAN of uninterrupted failure, proven per-route by a sibling
# success, lifted after 24h so the route is probed again).
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
    # HTTP status of the newest failure, when the probe recorded one. Sustained
    # availability alerts carry this and the error class, never provider text.
    newest_error_status: int | None = None
    # A configuration flag raised by the account-refusal rule: every sample in
    # its streak was an HTTP 401/403 refusal of TR's managed account.
    account_refusal: bool = False
    # Configured-route rule only: the streak did not yet qualify one sweep
    # ago (CONFIGURATION_REPORT_INTERVAL), so this pass is the episode's onset.
    onset: bool = True
    # Configured-route rule only: a failure arrived since the previous sweep.
    # False means the evidence is the same stale streak as last pass.
    fresh: bool = True


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
    refusals_by_route = _account_refusals_by_route(store, route_set, now)

    flags: list[RouteHealthFlag] = []
    for provider, model in routes:
        route_samples = samples_by_route[(provider, model)]
        refusal = None if refusals_by_route is None else _account_refusal_flag(
            provider, model,
            _unique_samples(route_samples + refusals_by_route.get((provider, model), [])),
            now, cutoff,
        )
        if refusal is not None:
            flags.append(refusal)
            continue
        configuration = _configuration_flag(
            provider, model, route_samples, now, cutoff, min_samples,
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
# A route whose every probe has failed, of any provider-side class (5xx, 429,
# timeouts and 4xx included), for this long is held the same way. Chutes
# answered HTTP 502 for qwen/qwen3.5-27b for over 13 hours while serving its
# other models (Sentry QUILL-ROUTER-HR): the route stayed advertised, customer
# requests tried it first, and route-availability paged every hour. Transient
# classes are excluded from the model-not-served streak above because one
# capacity blip proves nothing; six hours of nothing else is an outage.
# Router- and monitor-origin failures and probe_config_error still break the
# streak: they are our failures, not the provider's.
UNAVAILABLE_MIN_SPAN = dt.timedelta(hours=6)


@dataclass(frozen=True)
class UnservedRoute:
    provider: str
    model: str
    samples: int
    first_failure_at: str
    last_failure_at: str
    error_types: tuple[str, ...]
    # "unserved": a model-not-served streak; "unavailable": a sustained outage.
    kind: str = "unserved"


def _breaks_outage_streak(sample: ProviderBenchmarkSample) -> bool:
    from trusted_router.synthetic.components import is_router_origin_error

    error_type = sample.error_type
    if sample.status == "unsupported":
        # A model-not-served answer is a provider refusal; an outage that
        # alternates it with 5xx is still an outage of this route.
        return error_type not in _UNSERVED_ERROR_TYPES
    return (
        sample.status != "error"
        or error_type == "probe_config_error"
        or is_router_origin_error(error_type)
    )


def evaluate_unserved_routes(
    store: Store,
    *,
    routes: list[tuple[str, str]] | None = None,
    now: dt.datetime | None = None,
    window_hours: int = 48,
) -> list[UnservedRoute]:
    """Routable routes the provider lists but has refused for hours.

    Two kinds qualify: a model-not-served streak (UNSERVED_MIN_SPAN) and a
    sustained outage of every probe (UNAVAILABLE_MIN_SPAN). Both need a fresh
    sibling success, so a provider-wide or TrustedRouter-side failure is never
    held one route at a time.

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
        outage: list[tuple[dt.datetime, ProviderBenchmarkSample]] = []
        for created, sample in recent:
            if _breaks_outage_streak(sample) or (
                outage and outage[-1][0] - created > UNSERVED_MAX_PROBE_GAP
            ):
                break
            outage.append((created, sample))

        def qualifies(
            run: list[tuple[dt.datetime, ProviderBenchmarkSample]],
            min_span: dt.timedelta,
            *,
            _provider: str = provider,
            _model: str = model,
        ) -> bool:
            return not (
                len(run) < UNSERVED_MIN_SAMPLES
                or now - run[0][0] > UNSERVED_MAX_AGE
                or run[0][0] - run[-1][0] < min_span
                # The sibling success must come after this route began failing:
                # a success from before a provider-wide break proves nothing.
                or not any(
                    sibling != _model and created >= run[-1][0]
                    for created, sibling in successes.get(_provider, [])
                )
            )

        if qualifies(streak, UNSERVED_MIN_SPAN):
            evidence, kind = streak, "unserved"
        elif qualifies(outage, UNAVAILABLE_MIN_SPAN) and (
            # Refusal answers may sit inside an outage, but a streak that is
            # mostly refusals is judged by the model-not-served rule above
            # (where a transient head deliberately restarts it).
            sum(sample.status == "error" for _, sample in outage) >= UNSERVED_MIN_SAMPLES
        ):
            evidence, kind = outage, "unavailable"
        else:
            continue
        unserved.append(
            UnservedRoute(
                provider=provider,
                model=model,
                samples=len(evidence),
                first_failure_at=_iso_z(evidence[-1][0]),
                last_failure_at=_iso_z(evidence[0][0]),
                error_types=tuple(sorted({str(sample.error_type) for _, sample in evidence})),
                kind=kind,
            )
        )
    return unserved


def _iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# The hourly sweep's cadence. A configured-route streak qualifies for up to six
# hours after its newest failure, because one route is probed only a few times
# a day. Paging on every sweep of it re-raised the same evidence hourly: on
# 2026-11-21 Maple's 403 streak (newest sample 01:15Z) regressed the resolved
# Sentry issue at 03:01Z and 04:01Z while Maple was serving again. So the rule
# pages at onset only. A streak that keeps getting new failures is re-paged
# once a day at the digest hour; a stale one is only logged.
CONFIGURATION_REPORT_INTERVAL = dt.timedelta(hours=1)
_CONFIGURATION_MAX_AGE = dt.timedelta(hours=6)
_CONFIGURATION_MIN_SPAN = dt.timedelta(minutes=30)


def _configuration_streak_qualifies(
    streak: list[tuple[dt.datetime, ProviderBenchmarkSample]],
    at: dt.datetime,
    min_samples: int,
) -> bool:
    return (
        len(streak) >= min_samples
        and at - streak[0][0] <= _CONFIGURATION_MAX_AGE
        and streak[0][0] - streak[-1][0] >= _CONFIGURATION_MIN_SPAN
    )


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
    if not _configuration_streak_qualifies(streak, now, min_samples):
        return None
    # The same streak as the previous sweep saw it: a success breaks a streak
    # wherever it falls, so the streak then is this one's older part.
    previous = now - CONFIGURATION_REPORT_INTERVAL
    earlier = [pair for pair in streak if pair[0] <= previous]
    return RouteHealthFlag(
        provider=provider, model=model, samples=len(streak), failures=len(streak),
        failure_rate=1.0, newest_error_type=streak[0][1].error_type,
        newest_error_message=_alert_error_message(streak[0][1].error_message),
        kind="configuration",
        onset=not _configuration_streak_qualifies(earlier, previous, min_samples),
        fresh=streak[0][0] > previous,
    )


def evaluate_account_refusals(
    store: Store,
    *,
    routes: list[tuple[str, str]] | None = None,
    now: dt.datetime | None = None,
) -> list[RouteHealthFlag]:
    """The account-refusal rule alone, from its own small read.

    The synthetic worker runs this on every pass between the hourly full
    sweeps, so a provider refusing TR's account alerts within minutes. Only
    organic evidence is read here; the hourly sweep adds the probes.
    """
    now = now or dt.datetime.now(dt.UTC)
    if routes is None:
        routes = [
            (provider, model)
            for provider, models in rotation_candidates().items()
            for model in models
        ]
    route_set = set(routes)
    by_route = _account_refusals_by_route(store, route_set, now, strict=True)
    assert by_route is not None  # strict re-raises instead
    cutoff = now - ACCOUNT_REFUSAL_MAX_AGE - ACCOUNT_REFUSAL_MIN_SPAN
    flags = []
    for provider, model in sorted(by_route):
        flag = _account_refusal_flag(provider, model, by_route[(provider, model)], now, cutoff)
        if flag is not None:
            flags.append(flag)
    return flags


def unreported_account_refusals(
    flags: list[RouteHealthFlag], *, now: dt.datetime | None = None,
) -> list[RouteHealthFlag]:
    """The fast pass's flags not already reported within the cooldown.

    Without this every pass (about every three minutes) re-emitted the same
    refusal. A route that recovers and is refused again after the cooldown
    alerts again.
    """
    now = now or dt.datetime.now(dt.UTC)
    fresh = []
    with _FAST_REFUSAL_LOCK:
        for flag in flags:
            route = (flag.provider, flag.model)
            last = _FAST_REFUSAL_REPORTED.get(route)
            if last is not None and now - last < ACCOUNT_REFUSAL_FAST_COOLDOWN:
                continue
            _FAST_REFUSAL_REPORTED[route] = now
            fresh.append(flag)
    return fresh


def _account_refusals_by_route(
    store: Store, routes: set[tuple[str, str]], now: dt.datetime, *, strict: bool = False,
) -> dict[tuple[str, str], list[ProviderBenchmarkSample]] | None:
    """Newest Credits samples (organic and probe) of each recently refused route.

    In the hourly sweep a failed read only loses the fast path; the synthetic
    configured-route rule still alerts on its own evidence. The every-pass
    scope has no other evidence, so there (strict) the error propagates and
    the endpoint reports the pass as not evaluated.
    """
    cutoff = now - ACCOUNT_REFUSAL_MAX_AGE - ACCOUNT_REFUSAL_MIN_SPAN
    try:
        samples = store.provider_account_refusal_samples(
            cutoff=cutoff.isoformat().replace("+00:00", "Z"),
            per_route_limit=ACCOUNT_REFUSAL_SATURATED,
            limit=min(ACCOUNT_REFUSAL_READ_LIMIT_MAX,
                      ACCOUNT_REFUSAL_SATURATED * max(1, len(routes))),
        )
    except Exception:
        if strict:
            raise
        logger.warning("route_health.account_refusal_read_failed", exc_info=True)
        # None, not {}: without this read the rule has no recovery evidence,
        # so the sweep skips it rather than judge refusals from probes alone.
        return None
    by_route: dict[tuple[str, str], list[ProviderBenchmarkSample]] = {}
    for sample in samples:
        route = (sample.provider, sample.model)
        if route in routes:
            by_route.setdefault(route, []).append(sample)
    return by_route


def _unique_samples(samples: list[ProviderBenchmarkSample]) -> list[ProviderBenchmarkSample]:
    # The refusal read and the synthetic window both carry probe samples.
    seen: set[str] = set()
    unique = []
    for sample in samples:
        if sample.id not in seen:
            seen.add(sample.id)
            unique.append(sample)
    return unique


def _account_refusal_flag(
    provider: str,
    model: str,
    samples: list[ProviderBenchmarkSample],
    now: dt.datetime,
    cutoff: dt.datetime,
) -> RouteHealthFlag | None:
    """A fresh streak of the provider refusing TR's managed account."""
    # The rule's own window, whatever the caller's: the organic read covers
    # only this long, so older probe evidence must not bridge an organic
    # recovery the read never returned.
    cutoff = max(cutoff, now - ACCOUNT_REFUSAL_MAX_AGE - ACCOUNT_REFUSAL_MIN_SPAN)
    recent = sorted(
        (
            (created, sample) for sample in samples
            if sample.usage_type == "Credits"
            and (created := _parse_created_at(sample.created_at)) is not None
            and cutoff <= created <= now
        ),
        key=lambda pair: pair[0], reverse=True,
    )
    streak = []
    for pair in recent:
        sample = pair[1]
        if (
            sample.status not in {"unsupported", "error"}
            or sample.error_status not in ACCOUNT_REFUSAL_STATUSES
            or sample.error_type not in ACCOUNT_REFUSAL_ERROR_TYPES
        ):
            break
        streak.append(pair)
    if (
        len(streak) < ACCOUNT_REFUSAL_MIN_SAMPLES
        or now - streak[0][0] > ACCOUNT_REFUSAL_MAX_AGE
        or (
            streak[0][0] - streak[-1][0] < ACCOUNT_REFUSAL_MIN_SPAN
            and len(streak) < ACCOUNT_REFUSAL_SATURATED
        )
    ):
        return None
    return RouteHealthFlag(
        provider=provider, model=model, samples=len(streak), failures=len(streak),
        failure_rate=1.0, newest_error_type="provider_auth_config",
        newest_error_message=_alert_error_message(streak[0][1].error_message),
        kind="configuration", account_refusal=True,
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


def _error_detail_context(message: str | None) -> tuple[dict[str, str], dict[str, str]]:
    # Sentry QUILL-ROUTER-J1 (a 400 "That request was rejected") could not name
    # the rejected field: only the provider's words survived. Probes now store
    # the provider's own type/code/param and the request ID as a bounded,
    # identifier-only prefix on the message; surface them as tags (searchable)
    # and the request IDs as extra (per-event). Never in the fingerprint, so
    # grouping stays per route.
    detail, _ = parse_error_message(message)
    tags = {
        f"upstream_error_{name}": value
        for name, value in (
            ("type", detail.type), ("code", detail.code), ("param", detail.param),
        )
        if value
    }
    extra = {
        name: value
        for name, value in (
            ("upstream_request_id", detail.request_id),
            ("provider_request_id", detail.provider_request_id),
        )
        if value
    }
    return tags, extra


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
    newest = errors[0][1]
    return RouteHealthFlag(
        provider=provider, model=model, samples=len(measured), failures=len(errors),
        failure_rate=len(errors) / len(measured), newest_error_type=newest.error_type,
        newest_error_message=None, kind=kind,
        newest_error_status=getattr(newest, "error_status", None),
    )


def _availability_cause(flag: RouteHealthFlag) -> str:
    # Sentry QUILL-ROUTER-GC paged for hours with no cause: the availability
    # alert dropped the error class the flag already held, so a 402, a 5xx and
    # a failed attestation all read the same. Carry only our own vocabulary:
    # an identifier-shaped error class and an HTTP status, never provider text.
    parts = []
    error_type = flag.newest_error_type
    if (
        isinstance(error_type, str)
        and 0 < len(error_type) <= _ALERT_ERROR_TYPE_MAX_CHARS
        and _ALERT_ERROR_TYPE_RE.fullmatch(error_type)
    ):
        parts.append(error_type)
    status = flag.newest_error_status
    if type(status) is int and 100 <= status <= 599:
        parts.append(f"HTTP {status}")
    return f" (latest: {', '.join(parts)})" if parts else ""


def _degradation_detail(flag: RouteHealthFlag) -> str:
    return (
        f"failed {flag.failures}/{flag.samples} probes ({flag.failure_rate:.0%}) "
        f"over at least 30 minutes within the last {_DEGRADATION_WINDOW_HOURS} hours"
    )


def report_degradation_digest(
    flags: list[RouteHealthFlag], *, now: dt.datetime | None = None,
) -> bool:
    """Log degraded routes every pass; page them as one daily digest.

    Returns True when this pass sent the digest to Sentry.
    """
    degraded = sorted(
        (flag for flag in flags if flag.kind == "degradation"),
        key=lambda flag: (flag.provider, flag.model),
    )
    if not degraded:
        return False
    for flag in degraded:
        logger.warning(
            "route-degradation: %s/%s %s%s",
            flag.provider, flag.model, _degradation_detail(flag), _availability_cause(flag),
        )
    now = now or dt.datetime.now(dt.UTC)
    if now.astimezone(dt.UTC).hour != DEGRADATION_DIGEST_HOUR_UTC:
        return False
    from trusted_router.synthetic.alerts import ops_alert

    day = now.astimezone(dt.UTC).date().isoformat()
    lines = "; ".join(
        f"{flag.provider}/{flag.model} {flag.failures}/{flag.samples} "
        f"({flag.failure_rate:.0%}){_availability_cause(flag)}"
        for flag in degraded
    )
    ops_alert(
        f"route-degradation daily digest {day}: {len(degraded)} flaky route"
        f"{'' if len(degraded) == 1 else 's'} failed at least 25% of probes within the last "
        f"{_DEGRADATION_WINDOW_HOURS} hours: {lines}",
        fingerprint=["route-degradation-digest", day],
        # A peer that sees a different set of flaky routes sends its own digest.
        claim_detail=hashlib.sha256(
            "\n".join(f"{flag.provider}/{flag.model}" for flag in degraded).encode()
        ).hexdigest()[:16],
        tags={"degraded_routes": str(len(degraded))},
    )
    return True


def report_route_health(
    flags: list[RouteHealthFlag], *, now: dt.datetime | None = None,
) -> None:
    """Emit one grouped Sentry message for each unhealthy route.

    Partial degradation goes to the daily digest instead (report_degradation_digest).
    """
    if not flags:
        return
    report_degradation_digest(flags, now=now)
    try:
        import sentry_sdk
    except ImportError:
        return

    for flag in flags:
        if flag.kind == "configuration":
            from trusted_router.synthetic.alerts import ops_alert

            latest = _alert_error_message(flag.newest_error_message)
            if flag.account_refusal:
                # Name the account, not our configuration: the provider refused
                # TR's own key, and the fix is on the provider's side.
                summary = (
                    f"route-configuration: {flag.provider} refused TR's account for "
                    f"{flag.model} ({flag.failures} consecutive HTTP 401/403): check the "
                    "provider account for a revoked key, an exhausted plan allowance or "
                    "billing, or withdrawn model access"
                )
            else:
                summary = (
                    f"route-configuration: {flag.provider}/{flag.model} failed "
                    f"{flag.failures} consecutive configured-route probes over at least 30 minutes "
                    f"({flag.newest_error_type})"
                )
                reminder = (
                    flag.fresh
                    and (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).hour
                    == DEGRADATION_DIGEST_HOUR_UTC
                )
                if not flag.onset and not reminder:
                    # Already paged at onset (CONFIGURATION_REPORT_INTERVAL).
                    logger.warning(
                        "%s; ongoing, paged at onset%s", summary,
                        "" if flag.fresh else "; no new failure since the last sweep",
                    )
                    continue
            detail_tags, detail_extra = _error_detail_context(latest)
            ops_alert(
                summary + "; excluded from provider uptime, not operations"
                + (f"; latest: {latest}" if latest else ""),
                fingerprint=["route-configuration", flag.provider, flag.model],
                tags={"route_provider": flag.provider, "route_model": flag.model, **detail_tags},
                extra=detail_extra or None,
            )
            continue
        if flag.kind == "degradation":
            continue  # reported by report_degradation_digest above
        if flag.kind == "availability":
            from trusted_router.synthetic.alerts import ops_alert

            detail = f"failed {flag.failures} consecutive probes over at least 30 minutes"
            ops_alert(
                f"route-{flag.kind}: {flag.provider}/{flag.model} {detail}"
                + _availability_cause(flag),
                fingerprint=["route-availability", flag.provider, flag.model],
                tags={"route_provider": flag.provider, "route_model": flag.model},
            )
            continue
        latest_message = _alert_error_message(flag.newest_error_message)
        latest = (
            " ".join(part for part in (flag.newest_error_type, latest_message) if part)
            or "unknown error"
        )
        detail_tags, detail_extra = _error_detail_context(latest_message)
        message = (
            f"route-health: {flag.provider}/{flag.model} {flag.failure_rate:.0%} failure "
            f"over {flag.samples} samples (latest: {latest})"
        )
        from trusted_router.synthetic.alerts import alert_claimed

        fingerprint = ["route-health", flag.provider, flag.model]
        if not alert_claimed(fingerprint):
            continue
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = fingerprint
            scope.set_tag("route_provider", flag.provider)
            scope.set_tag("route_model", flag.model)
            scope.set_tag("failure_rate", f"{flag.failure_rate:.4f}")
            for key, value in detail_tags.items():
                scope.set_tag(key, value)
            for key, value in detail_extra.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_message(message, level="error")


# Flag kinds a hold supersedes. Configuration flags keep their own alert (auth
# and probe setup stay observable) and degradation goes to the daily digest.
_HOLD_SUPERSEDES_KINDS = frozenset({"availability", "structural"})


def split_held_flags(
    flags: list[RouteHealthFlag], held: Iterable[tuple[str, str]],
) -> tuple[list[RouteHealthFlag], list[RouteHealthFlag]]:
    """Split flags into (still paging, superseded by a pending hold)."""
    held_routes = frozenset(held)
    paging: list[RouteHealthFlag] = []
    holding: list[RouteHealthFlag] = []
    for flag in flags:
        if flag.kind in _HOLD_SUPERSEDES_KINDS and (flag.provider, flag.model) in held_routes:
            holding.append(flag)
        else:
            paging.append(flag)
    return paging, holding


def report_route_holds(flags: list[RouteHealthFlag]) -> None:
    """One grouped alert per route that the next price refresh will hold.

    Replaces the hourly route-availability / route-health page for a route
    already proven down long enough to hold: the hold, not another page, is
    the action. The route leaves rotation once the hold deploys, so this
    stops on its own; if the route still fails after the 24h hold lifts, the
    same issue re-opens.
    """
    from trusted_router.synthetic.alerts import ops_alert

    for flag in flags:
        ops_alert(
            f"route-hold: {flag.provider}/{flag.model} has failed every probe for hours "
            f"while {flag.provider} serves other models{_availability_cause(flag)}; the hourly "
            "price refresh holds it from routing and the catalog "
            "(provider-route-unserved) for 24h, then re-probes it",
            fingerprint=["route-hold", flag.provider, flag.model],
            tags={"route_provider": flag.provider, "route_model": flag.model},
        )


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
