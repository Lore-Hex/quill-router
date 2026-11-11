"""Authorize-only async v1 projection and fail-closed, bounded admission cache.

Fleet health comes from a complete durable observation; a local empty queue
is never evidence of fleet health.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import JSONResponse

from trusted_router import async_settle_timing as timing
from trusted_router.async_settle_ticket import PURPOSE, TicketSigner
from trusted_router.billing_snapshot import (
    BillingSnapshot,
    Eligibility,
    build_snapshot,
    canonical_hash,
    require_eligible,
)
from trusted_router.catalog_data import PARASAIL_LIBERTY_2_0_MODEL_ID, ModelEndpoint
from trusted_router.config import Settings, in_v2_cohort
from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.model_aliases import (
    ROUTER_MODEL_NAMESPACE,
    canonical_router_model_id,
    names_router_model,
)
from trusted_router.partner_billing import partner_billing_mode
from trusted_router.polyphemus import MODEL_ID as POLYPHEMUS_MODEL_ID
from trusted_router.provider_contracts import provider_model_has_private_price_tier_basis
from trusted_router.speech import SPEECH_MODELS
from trusted_router.storage_models import (
    GatewayAuthorization,
    SettleOutboxRow,
    generation_id_for_authorization,
)
from trusted_router.transcription import TRANSCRIPTION_MODELS
from trusted_router.web_fetch import FETCH_MODEL_ID

if TYPE_CHECKING:
    from trusted_router.routes.internal.fastpath_async_ticket import EdgeTicketRequest

CACHE_SECONDS = 5.0
# Admission thresholds on a workspace's unsettled async cost, in microdollars,
# by trust tier. A tier with no entry is never admitted.
TIER_CAPS = {1: 100_000, 2: 25_000_000, 3: 100_000_000}
# Tiers whose threshold the pilot override may lower and never raise.
PILOT_CAP_LOWERS_ONLY = frozenset({1})
# Enclaves reject future-issued tickets without clock tolerance. Backdate the
# whole window, preserving the 300s contract bound (240s remain at default TTL).
TICKET_CLOCK_SKEW_ALLOWANCE_SECONDS = 60


def admission_cap(tier: object, pilot_cap: int) -> int | None:
    """The unsettled-cost threshold for a trust tier, or None when not admitted.

    Every admission decision and every prediction of one takes its threshold
    here. A nonzero pilot override replaces the threshold of tiers 2 and 3,
    including with a larger value. For tier 1 it only ever lowers it.
    """
    if type(tier) is not int or tier not in TIER_CAPS:
        return None
    cap = TIER_CAPS[tier]
    if not pilot_cap:
        return cap
    return min(cap, pilot_cap) if tier in PILOT_CAP_LOWERS_ONLY else pilot_cap


@dataclass(frozen=True)
class Admission:
    pending_micro: int
    tier: int


@dataclass(frozen=True)
class DrainHealth:
    # Local monotonic receipt time of a fresh, complete fleet measurement.
    observed_at: float
    p95_age_seconds: float


class AdmissionCache:
    def __init__(self, read: Callable[[str], Admission], *,
                 clock: Callable[[], float] = time.monotonic,
                 health_read: Callable[[], dict[str, Any] | None] | None = None,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.read, self.clock = read, clock
        self.lock = threading.Lock()
        self.entries: dict[str, tuple[float, Admission | None]] = {}
        self.health: DrainHealth | None = None
        self.health_read, self.wall_clock = health_read, wall_clock
        self.health_read_at = float("-inf")
        self.last_evaluated_at = float("-inf")
        self.evaluated_at: dict[str, float] = {}

    def eligible(self, workspace_id: str, pilot_cap: int, *,
                 queued_micro: int = 0, cached_only: bool = False) -> bool:
        # Lock coalesces concurrent misses, including failures. Timestamp at
        # read START, so a slow read cannot turn old evidence into fresh data.
        if not self.lock.acquire(timeout=0.2):
            return False
        try:
            now = self.clock()
            self.last_evaluated_at = now
            # Cached-only admission uses the latest health observation younger
            # than CACHE_SECONDS, read here or by read-ahead, and admits only a
            # healthy one. A read-ahead read that fails, or returns an invalid,
            # incomplete or stale record, neither extends nor revokes it; a valid
            # record of an unhealthy drain replaces it. The full path keeps its
            # refresh and fail-closed rule below.
            if not cached_only and self.health_read is not None and not 0 <= now - self.health_read_at < 1:
                self.health_read_at = now
                self.health = None
                try:
                    self.health = decode_health(self.health_read(), now=self.clock(), wall=self.wall_clock())
                except Exception:
                    self.health = None
                logging.getLogger(__name__).info(
                    "async_drain.admission health_available=%s data_age_seconds=%s",
                    self.health is not None,
                    self.clock() - self.health.observed_at if self.health is not None else None,
                )
            cached = self.entries.get(workspace_id)
            if cached_only and (cached is None or not 0 <= now - cached[0] < CACHE_SECONDS):
                self.evaluated_at[workspace_id] = now
                # Register demand for read-ahead without inventing evidence or
                # reading on the request path. Full-path pruning stays below.
                if cached is None and len(self.entries) < 4096:
                    self.entries[workspace_id] = (float("-inf"), None)
                return False
            if cached is None or not 0 <= now - cached[0] < CACHE_SECONDS:
                if workspace_id not in self.entries and len(self.entries) >= 4096:
                    self.entries = {ws: entry for ws, entry in self.entries.items()
                                    if 0 <= now - entry[0] < CACHE_SECONDS}
                    self.evaluated_at = {ws: at for ws, at in self.evaluated_at.items()
                                         if ws in self.entries}
                    if len(self.entries) >= 4096:
                        return False  # Capacity never evicts still-fresh evidence.
                value = None
                try:
                    value = self.read(workspace_id)
                except Exception:
                    value = None  # Never log exception text or credentials.
                cached = (now, value)
                self.entries[workspace_id] = cached
            self.evaluated_at[workspace_id] = now
            now = self.clock()
            value, health = cached[1], self.health
            cap = admission_cap(value.tier, pilot_cap) if value is not None else None
            return bool(
                0 <= now - cached[0] < CACHE_SECONDS
                and value is not None and cap is not None
                and type(value.pending_micro) is int and value.pending_micro >= 0
                and value.pending_micro + queued_micro <= cap
                and health is not None and 0 <= now - health.observed_at < CACHE_SECONDS
                and 0 <= health.p95_age_seconds <= 5
            )
        finally:
            self.lock.release()


HEALTH_JSON_MAX_BYTES = 4096
HEALTH_INT_FIELDS = ("sample_count", "backlog_count", "frozen_micro", "dead_count")
HEALTH_NUMBER_FIELDS = ("observed_at", "worker_heartbeat", "p50_age_seconds",
                        "p95_age_seconds", "oldest_unresolved_age_seconds")
HEALTH_KEYS = frozenset(("v", "authority", "complete", *HEALTH_INT_FIELDS, *HEALTH_NUMBER_FIELDS))


def valid_health_record(value: Any) -> bool:
    """Exact bounded wire schema, shared by the publisher and admission reader.

    Incomplete observations are valid records but never eligible evidence.
    JSON numbers accept exact int/float types, never bool or coercible strings.
    """
    try:
        if type(value) is not dict or value.keys() != HEALTH_KEYS:
            return False
        if (type(value["v"]) is not int or value["v"] != 1
                or type(value["authority"]) is not str or value["authority"] != "local"
                or type(value["complete"]) is not bool):
            return False
        if any(type(value[k]) is not int or value[k] < 0 for k in HEALTH_INT_FIELDS):
            return False
        if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or value[k] < 0
               for k in HEALTH_NUMBER_FIELDS):
            return False
        if not (value["observed_at"] <= value["worker_heartbeat"]
                and 0 <= value["p50_age_seconds"] <= value["p95_age_seconds"]
                <= value["oldest_unresolved_age_seconds"]
                and value["dead_count"] <= value["backlog_count"] == value["sample_count"]):
            return False
        if value["backlog_count"] == 0 and any(value[k] != 0 for k in (
                "frozen_micro", "p50_age_seconds", "p95_age_seconds", "oldest_unresolved_age_seconds")):
            return False
        return len(json.dumps(value, allow_nan=False).encode("utf-8")) <= HEALTH_JSON_MAX_BYTES
    except (TypeError, ValueError, OverflowError):
        return False


def parse_health_record(body: Any) -> dict[str, Any] | None:
    """Bound JSON before parsing; reject duplicate keys as ambiguous evidence."""
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate health keys")
        return value

    try:
        if type(body) is not str or len(body.encode("utf-8")) > HEALTH_JSON_MAX_BYTES:
            return None
        value = json.loads(body, object_pairs_hook=unique_pairs)
        return value if valid_health_record(value) else None
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def observe_health(value: dict[str, Any] | None, *, now: float, wall: float) -> DrainHealth | None:
    """Evidence from a valid, complete and fresh record, whether or not the drain is healthy.

    Translates durable wall time to monotonic evidence time without rejuvenation.
    eligible() rejects an unhealthy observation, so installing one only closes admission.
    """
    if not valid_health_record(value) or value is None or not value["complete"]:
        return None
    observed, heartbeat = value["observed_at"], value["worker_heartbeat"]
    if (not all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in (now, wall))
            or not (0 <= wall - observed < CACHE_SECONDS and 0 <= wall - heartbeat < CACHE_SECONDS)):
        return None
    return DrainHealth(now - (wall - observed), value["p95_age_seconds"])


def decode_health(value: dict[str, Any] | None, *, now: float, wall: float) -> DrainHealth | None:
    """Healthy evidence only: observe_health() with the drain's p95 age at most 5 s."""
    health = observe_health(value, now=now, wall=wall)
    return health if health is not None and health.p95_age_seconds <= 5 else None


class HandoffQueue(Protocol):
    """Request-side queue interface, also resolvable by runtime type reflection."""

    def get(self, authorization_id: str, intent_kind: str) -> SettleOutboxRow | None: ...

    def pending(self, workspace_id: str) -> int: ...

    def accept(self, row: SettleOutboxRow) -> JSONResponse: ...


@dataclass
class Runtime:
    signer: TicketSigner | None
    admission: AdmissionCache | None
    region: str
    epoch: int
    # Journal authority stays primary even when observation is regional.
    # Direct constructors predating serving-region labels use region for both.
    journal_region: str | None = None
    memory: HandoffQueue | None = None

    @property
    def effective_journal_region(self) -> str:
        return self.region if self.journal_region is None else self.journal_region


def load_runtime(settings: Settings, backend: Any) -> Runtime:
    """Application construction only. Invalid key or lifetime disables tickets."""
    signer = None
    try:
        if settings.async_settle_ticket_ttl_seconds <= TICKET_CLOCK_SKEW_ALLOWANCE_SECONDS:
            raise ValueError("ticket lifetime must exceed clock skew allowance")
        path = settings.async_settle_ticket_private_key_file
        if not path or path == settings.speculation_shadow_private_key_file:
            raise ValueError("separate key required")
        private = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError("Ed25519 required")
        # Refuse even a second file containing the shadow private key.
        if settings.speculation_shadow_private_key_file:
            shadow = serialization.load_pem_private_key(
                Path(settings.speculation_shadow_private_key_file).read_bytes(), password=None)
            if isinstance(shadow, Ed25519PrivateKey) and shadow.public_key().public_bytes_raw() == private.public_key().public_bytes_raw():
                raise ValueError("separate key required")
        if settings.async_settle_ticket_kid == settings.speculation_shadow_kid:
            raise ValueError("separate kid required")
        signer = TicketSigner(private, TrustedKey(
            settings.async_settle_ticket_kid, PURPOSE,
            b64encode(private.public_key().public_bytes_raw()),
            settings.async_settle_ticket_issuer, settings.async_settle_ticket_audience))
    except Exception:
        signer = None
    admission = None
    if backend is not None and hasattr(backend, "_database"):
        from trusted_router.storage_gcp_async_admission import read_admission
        health_read: Callable[[], dict[str, Any] | None] | None = None
        if settings.async_settle_admission_enabled:
            from trusted_router.storage_gcp_async_admission import read_health
            timed_health_read = timing.read(read_health, "health")
            # Recheck at use, so disabling admission also stops health RPCs.
            def health_read() -> dict[str, Any] | None:
                return timed_health_read(backend._database) if settings.async_settle_admission_enabled else None
        admission = AdmissionCache(timing.read(lambda ws: read_admission(backend._database, ws), "workspace"),
                                   health_read=health_read)
        admission.lock = cast(Any, timing.AdmissionLock(admission.lock))
    return Runtime(signer, admission, settings.effective_serving_region,
                   settings.async_settle_authority_epoch, journal_region=settings.primary_region)


def projection(*, authorization: GatewayAuthorization, endpoints: Iterable[ModelEndpoint],
               requested: Eligibility, runtime: Runtime | None, settings: Settings,
               now: int | None = None) -> dict[str, Any]:
    """Best-effort detached metadata AFTER the successful durable authorization."""
    try:
        snapshot = build_snapshot(endpoints, requested)
        return snapshot_projection(authorization=authorization, snapshot=snapshot,
                                   requested=requested, runtime=runtime, settings=settings, now=now)
    except Exception:
        return {"async_eligible": False}


def snapshot_projection(*, authorization: GatewayAuthorization, snapshot: BillingSnapshot,
                        requested: Eligibility, runtime: Runtime | None, settings: Settings,
                        now: int | None = None) -> dict[str, Any]:
    """Sign a validated detached DTO; caller has already frozen effective prices."""
    result: dict[str, Any] = {"async_eligible": False}
    try:
        require_eligible(requested)
        # Missing authority facts must not be invented from defaults or routing.
        if (runtime is None or runtime.signer is None or runtime.epoch < 1
                or not runtime.effective_journal_region or not authorization.credit_reservation_id
                or authorization.settlement != "local" or authorization.settled):
            return result
        digest = canonical_hash(snapshot)
        # A workspace or enclave region outside negotiation gets the admission-off answer
        # byte for byte (shape B): no ticket, no status URL. The enclave
        # binds async settlement only from async_eligible true. The edge
        # composes this answer for non-pilots from admission_workspaces, and
        # from admission_excluded_workspaces for the workspaces
        # async_settle_pilot_allows leaves out whatever the pilot set says:
        # under the explicit list, the fast-path workspaces outside the
        # overlap list; in all mode with no pilot set, the reserved list
        # (fast-admission-widening.md section 9.1). Without edge tickets,
        # pilots pass to Python, which checks their stored enclave region here
        # (docs/design/fast-admission-e2-g15-async.md sections 4.1 and 4.5).
        # A request the pilot leased gets the same answer unless its
        # workspace is on the overlap list: its settle goes through the
        # lease (docs/design/fast-admission-widening.md section 9.1).
        if (not settings.async_settle_admission_enabled
                or not settings.async_settle_admits_request(
                    authorization.workspace_id, leased=authorization.fastpath_envelope is not None)
                or not settings.async_settle_negotiation_region_allowed(authorization.region)):
            result.update(billing_snapshot=snapshot.model_dump(mode="json"), billing_snapshot_hash=digest)
            return result
        eligible, ticket, status_url = sign_ticket(
            authorization_id=authorization.id, workspace_id=authorization.workspace_id,
            key_hash=authorization.key_hash, invocation_nonce=authorization.invocation_nonce,
            reservation_id=authorization.credit_reservation_id, requested=requested,
            digest=digest, runtime=runtime, settings=settings, now=now,
        )
        result.update(billing_snapshot=snapshot.model_dump(mode="json"),
                      billing_snapshot_hash=digest, settlement_ticket=ticket,
                      async_eligible=eligible,
                      settlement_status_url=status_url)
    except Exception:
        return {"async_eligible": False}  # Never invalidate a committed hold.
    return result


def sign_ticket(*, authorization_id: str, workspace_id: str, key_hash: str,
                invocation_nonce: str | None, reservation_id: str, requested: Eligibility,
                digest: str, runtime: Runtime, settings: Settings,
                now: int | None = None) -> tuple[bool, str, str]:
    """The shared admission answer and ticket claims for validated local facts."""
    eligible = bool(runtime.admission is not None
                    and timing.eligible(runtime.admission, workspace_id,
                                        settings.async_settle_pilot_cap_micro, projection=True))
    issued = int(time.time()) if now is None else now
    valid_from = issued - TICKET_CLOCK_SKEW_ALLOWANCE_SECONDS
    signer = runtime.signer
    if signer is None:
        raise ValueError("ticket authority")
    claims = dict(
        authorization_id=authorization_id,
        generation_id=generation_id_for_authorization(authorization_id),
        workspace_id=workspace_id, key_id=key_hash,
        invocation_nonce=invocation_nonce, billing_authority="local",
        journal_region=runtime.effective_journal_region, epoch=runtime.epoch, snapshot_version=1,
        snapshot_hash=digest, route_type=requested.route_type, streamed=requested.streamed,
        reservation_id=reservation_id, settle_origin="typed",
        async_eligible=eligible, iss=signer.trusted.iss, aud=signer.trusted.aud,
        iat=valid_from, exp=valid_from + settings.async_settle_ticket_ttl_seconds,
    )
    ticket = signer.sign(claims, issued)
    return eligible, ticket, "/v1/settlements/" + quote(authorization_id + ".settle", safe="")


def edge_ticket(*, body: EdgeTicketRequest, settings: Settings, runtime: Runtime | None,
                typed: bool, now: int | None = None) -> dict[str, Any]:
    """Sign catalog-verified edge facts; failure never changes the edge's hold."""
    started = time.monotonic()
    reason, outcome, eligible_label = "error", "refused", "na"
    refused: dict[str, Any] = {"async_eligible": False}
    try:
        if not settings.fastpath_edge_async_ticket_allows(body.workspace_id):
            reason = "workspace"
            return refused
        if not settings.async_settle_negotiation_region_allowed(body.region):
            reason = "region"
            return refused
        if (runtime is None or runtime.signer is None or runtime.epoch < 1
                or not runtime.effective_journal_region):
            reason = "authority"
            return refused
        if not typed:
            reason = "records"
            return refused
        issued = int(time.time()) if now is None else now
        if abs(issued - body.priced_at) > 300:
            reason = "price_instant"
            return refused
        from trusted_router.catalog import effective_endpoint, endpoint_for_id

        endpoints = []
        for endpoint_id in body.candidate_endpoint_ids:
            endpoint = endpoint_for_id(endpoint_id)
            if endpoint is None:
                reason = "endpoint"
                return refused
            endpoints.append(endpoint)
        at = datetime.fromtimestamp(body.priced_at, UTC)
        effective = [effective_endpoint(endpoint, at=at) for endpoint in endpoints]
        requested = Eligibility(route_type=body.route_type, streamed=body.streamed)
        try:
            require_eligible(requested)
            snapshot = build_snapshot(effective, requested)
        except ValueError:
            reason = "snapshot"
            return refused
        digest = canonical_hash(snapshot)
        if digest != body.billing_snapshot_hash:
            reason = "hash"
            return refused
        eligible, ticket, status_url = sign_ticket(
            authorization_id=body.authorization_id, workspace_id=body.workspace_id,
            key_hash=body.key_hash, invocation_nonce=body.invocation_nonce,
            reservation_id=body.reservation_id, requested=requested, digest=digest,
            runtime=runtime, settings=settings, now=now,
        )
        outcome, reason, eligible_label = "issued", "none", "yes" if eligible else "no"
        return {"async_eligible": eligible, "settlement_ticket": ticket,
                "settlement_status_url": status_url}
    except Exception:
        reason = "error"
        return refused
    finally:
        timing.logger.info(
            "metric=async_settle_edge_ticket outcome=%s reason=%s eligible=%s elapsed_ms=%d",
            outcome, reason, eligible_label, int((time.monotonic() - started) * 1000),
        )


# The requested parameters an async-v1 authorize may name; any other makes
# the answer async_eligible false only. The edge's authorize goldens carry
# this set (scripts/fastpath/authorize_vectors.py, authorize_async_cases_v1.json).
ORDINARY_PARAMETERS: frozenset[str] = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
    "stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias",
    "logprobs", "top_logprobs", "response_format", "reasoning_effort", "stream",
})


# The two prefixes names_program_model reads, for the edge's copy.
PROGRAM_MODEL_PREFIXES: tuple[str, ...] = (ROUTER_MODEL_NAMESPACE, PARASAIL_LIBERTY_2_0_MODEL_ID)


def names_program_model(model: str) -> bool:
    """Whether a requested model string names a TrustedRouter program,
    meta-router or private alias, or the partner model.

    The synchronous settle prices such a request, routed on a chat route, by
    the plain endpoint formula; async-v1 leaves it out all the same, as it
    always has. Every catalog id of those is in the router's own namespace
    (model_aliases.names_router_model, which authorize's native Batch rule
    reads too, and which routing also accepts spelled nyte/); the partner
    model's id is the one outside it. Each is a prefix, so a variant or dated
    suffix that routing strips stays covered. An ordinary model of any
    publisher, served by any provider, is not one. The edge mirrors this
    (fastpath/internal/gwauth/async.go, namesProgramModel), held to it by the
    authorize goldens' program_model_names.
    """
    canonical = canonical_router_model_id(model.strip().lower())
    return names_router_model(canonical) or canonical.startswith(PARASAIL_LIBERTY_2_0_MODEL_ID)


def requested_facts(*, body: Any, authorization: GatewayAuthorization, endpoints: Iterable[ModelEndpoint],
                    typed: bool, federated: bool) -> Eligibility:
    """The requested facts of one authorization.

    Each is read from what the synchronous settle itself reads to price a
    request by anything but the plain endpoint formula
    (routes/internal/gateway.py, _settle_gateway_authorization), so the two
    cannot disagree about which authorization is ordinary:

    usage_type, authority: the authorization's (a BYOK route, or a federated
        or deferred settlement, is not ordinary local Credits).
    app_markup, custom_markup, receipt_fee: the authorization's basis points;
        a requested receipt counts even when its fee is zero.
    custom_model, user_model: the authorization's creator ids, which
        _authorized_user_model_pair and billing_pricing_snapshot read.
    tool_cost: the authorization's additional cost reservation, or its model
        being one the settle path meters as a tool or media request (Fetch,
        transcription, speech).
    video_cost: the authorization's video pricing snapshot.
    native_batch: the authorization's native Batch eligibility.
    partner: partner_billing_mode, the settle path's own classifier, over the
        authorization's requested model and idempotency key and this
        request's route; or a requested program or alias name
        (names_program_model), which the contract has no fact of its own for.
    polyphemus: a candidate that is the Polyphemus selector's endpoint.
    private_tier_basis: a candidate whose provider reports its own price tier
        basis, by the predicate _provider_price_tier_input_tokens reads.

    tests/test_async_settle_settle_branches.py enumerates the settle path's
    cost branches and holds each to its fact here.
    """
    candidates = list(endpoints)
    return Eligibility(
        typed=typed, usage_type=authorization.usage_type.value,
        authority="federated" if federated else authorization.settlement,
        route_type=body.route_type, streamed=body.stream is True,
        service_tier=body.service_tier,
        app_markup=authorization.app_markup_basis_points,
        custom_markup=authorization.custom_model_markup_basis_points,
        receipt_fee=max(int(body.inference_receipt), authorization.receipt_fee_basis_points),
        custom_model=bool(authorization.custom_model_id),
        user_model=bool(authorization.user_provided_model_id),
        tool_cost=(
            bool(authorization.additional_cost_reservation_microdollars)
            or authorization.model_id == FETCH_MODEL_ID
            or authorization.model_id in TRANSCRIPTION_MODELS
            or authorization.model_id in SPEECH_MODELS
        ),
        video_cost=authorization.video_pricing_snapshot is not None,
        native_batch=authorization.native_batch_eligible,
        partner=(
            partner_billing_mode(
                requested_model_id=authorization.requested_model_id, route_type=body.route_type,
                idempotency_key=authorization.idempotency_key,
            ) is not None
            or any(names_program_model(name) for name in (body.model, *(body.models or ())))
        ),
        polyphemus=any(candidate.model_id == POLYPHEMUS_MODEL_ID for candidate in candidates),
        private_tier_basis=any(
            provider_model_has_private_price_tier_basis(candidate.provider, candidate.model_id)
            for candidate in candidates
        ),
    )


def has_authorize_feature_extras(body: Any, *, shadow_v2: bool = False) -> bool:
    """Separate the enclave's advisory lease echo from inference features.

    spend_lease.go adds this observation after chatAuthorizeBody. It supplies
    no billing authority or eligibility facts here. Consequential admission
    and every other unknown member remain unsupported. Keep v1 unchanged.
    """
    extras = body.model_extra
    if shadow_v2 and extras:
        return bool(extras.keys() - {"spend_lease_echo"})
    return bool(extras)


def authorize_additions(*, request: Any, body: Any, authorization: GatewayAuthorization,
                        endpoints: list[ModelEndpoint], settings: Settings, typed: bool,
                        federated: bool, replay: bool, effective_at: Any) -> dict[str, Any]:
    """Map server-observed authorize facts; never accept client eligibility flags."""
    if request is None or request.headers.getlist("X-TR-Settlement-Mode") != ["async-v1"]:
        return {}
    try:
        from trusted_router.catalog import effective_endpoint

        # Replays lack a persisted async snapshot in PR B. Never re-freeze a
        # newer catalog against an older authorization. PR C owns retry storage.
        shadow_v2 = (
            in_v2_cohort(settings.effective_async_settle_shadow_policy(), authorization.workspace_id,
                         settings.async_settle_enabled, settings.async_settle_shadow_workspace_ids)
            and request.headers.getlist("X-TR-Settlement-Shadow-Policy") == ["shadow-v2"]
        )
        if replay or has_authorize_feature_extras(body, shadow_v2=shadow_v2) or body.route_type not in {"chat.completions", "responses"}:
            return {"async_eligible": False}
        # Recognizing the enclave's modality field must not expand the
        # text-only settlement optimization's previous eligibility boundary.
        if "input_modalities" in body.model_fields_set:
            return {"async_eligible": False}
        # Unknown adapter features must not silently acquire an exact total.
        if set(body.requested_parameters or ()) - ORDINARY_PARAMETERS:
            return {"async_eligible": False}
        requested = requested_facts(body=body, authorization=authorization, endpoints=endpoints,
                                    typed=typed, federated=federated)
        return projection(authorization=authorization,
                          endpoints=[effective_endpoint(e, at=effective_at) for e in endpoints],
                          requested=requested, settings=settings,
                          runtime=getattr(request.app.state, "async_settle", None))
    except Exception:
        return {"async_eligible": False}
