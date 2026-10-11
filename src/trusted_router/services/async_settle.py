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
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.async_settle_ticket import PURPOSE, TicketSigner
from trusted_router.billing_snapshot import (
    BillingSnapshot,
    Eligibility,
    build_snapshot,
    canonical_hash,
    require_eligible,
)
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.storage_models import GatewayAuthorization, generation_id_for_authorization

CACHE_SECONDS = 5.0
TIER_CAPS = {2: 25_000_000, 3: 100_000_000}


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

    def eligible(self, workspace_id: str, pilot_cap: int) -> bool:
        # Lock coalesces concurrent misses, including failures. Timestamp at
        # read START, so a slow read cannot turn old evidence into fresh data.
        if not self.lock.acquire(timeout=0.2):
            return False
        try:
            now = self.clock()
            if self.health_read is not None and not 0 <= now - self.health_read_at < 1:
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
            if cached is None or not 0 <= now - cached[0] < CACHE_SECONDS:
                if workspace_id not in self.entries and len(self.entries) >= 4096:
                    self.entries = {ws: entry for ws, entry in self.entries.items()
                                    if 0 <= now - entry[0] < CACHE_SECONDS}
                    if len(self.entries) >= 4096:
                        return False  # Capacity never evicts still-fresh evidence.
                value = None
                try:
                    value = self.read(workspace_id)
                except Exception:
                    value = None  # Never log exception text or credentials.
                cached = (now, value)
                self.entries[workspace_id] = cached
            now = self.clock()
            value, health = cached[1], self.health
            return bool(
                0 <= now - cached[0] < CACHE_SECONDS
                and value is not None and type(value.tier) is int and value.tier in TIER_CAPS
                and type(value.pending_micro) is int and value.pending_micro >= 0
                and value.pending_micro <= (pilot_cap or TIER_CAPS[value.tier])
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


def decode_health(value: dict[str, Any] | None, *, now: float, wall: float) -> DrainHealth | None:
    """Translate durable wall time to monotonic evidence time without rejuvenation."""
    if not valid_health_record(value) or value is None or not value["complete"]:
        return None
    observed, heartbeat = value["observed_at"], value["worker_heartbeat"]
    if (not all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in (now, wall))
            or not (0 <= wall - observed < CACHE_SECONDS and 0 <= wall - heartbeat < CACHE_SECONDS)
            or value["p95_age_seconds"] > 5):
        return None
    return DrainHealth(now - (wall - observed), value["p95_age_seconds"])


@dataclass
class Runtime:
    signer: TicketSigner | None
    admission: AdmissionCache | None
    region: str
    epoch: int
    # Journal authority stays primary even when observation is regional.
    # Direct constructors predating serving-region labels use region for both.
    journal_region: str | None = None

    @property
    def effective_journal_region(self) -> str:
        return self.region if self.journal_region is None else self.journal_region


def load_runtime(settings: Settings, backend: Any) -> Runtime:
    """Application construction only. Invalid/missing mounted key disables tickets."""
    signer = None
    try:
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
            # Recheck at use, so disabling admission also stops health RPCs.
            def health_read() -> dict[str, Any] | None:
                return read_health(backend._database) if settings.async_settle_admission_enabled else None
        admission = AdmissionCache(lambda ws: read_admission(backend._database, ws), health_read=health_read)
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
        if not settings.async_settle_admission_enabled:
            result.update(billing_snapshot=snapshot.model_dump(mode="json"), billing_snapshot_hash=digest)
            return result
        eligible = bool(settings.async_settle_pilot_allows(authorization.workspace_id)
                        and runtime.admission is not None
                        and runtime.admission.eligible(authorization.workspace_id,
                                                       settings.async_settle_pilot_cap_micro))
        issued = int(time.time()) if now is None else now
        signer = runtime.signer
        claims = dict(
            authorization_id=authorization.id,
            generation_id=generation_id_for_authorization(authorization.id),
            workspace_id=authorization.workspace_id, key_id=authorization.key_hash,
            invocation_nonce=authorization.invocation_nonce, billing_authority="local",
            journal_region=runtime.effective_journal_region, epoch=runtime.epoch, snapshot_version=1,
            snapshot_hash=digest, route_type=requested.route_type, streamed=requested.streamed,
            reservation_id=authorization.credit_reservation_id, settle_origin="typed",
            async_eligible=eligible, iss=signer.trusted.iss, aud=signer.trusted.aud,
            iat=issued, exp=issued + settings.async_settle_ticket_ttl_seconds,
        )
        ticket = signer.sign(claims, issued)
        result.update(billing_snapshot=snapshot.model_dump(mode="json"),
                      billing_snapshot_hash=digest, settlement_ticket=ticket,
                      async_eligible=eligible,
                      settlement_status_url="/v1/settlements/" + quote(authorization.id + ".settle", safe=""))
    except Exception:
        return {"async_eligible": False}  # Never invalidate a committed hold.
    return result


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
        if replay or body.model_extra or body.route_type not in {"chat.completions", "responses"}:
            return {"async_eligible": False}
        # Recognizing the enclave's modality field must not expand the
        # text-only settlement optimization's previous eligibility boundary.
        if "input_modalities" in body.model_fields_set:
            return {"async_eligible": False}
        # Unknown adapter features must not silently acquire an exact total.
        ordinary_parameters = {
            "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
            "stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias",
            "logprobs", "top_logprobs", "response_format", "reasoning_effort", "stream",
        }
        if set(body.requested_parameters or ()) - ordinary_parameters:
            return {"async_eligible": False}
        requested = Eligibility(
            typed=typed, usage_type=authorization.usage_type.value,
            authority="federated" if federated else authorization.settlement,
            route_type=body.route_type, streamed=body.stream is True,
            service_tier=body.service_tier,
            app_markup=authorization.app_markup_basis_points,
            custom_markup=authorization.custom_model_markup_basis_points,
            receipt_fee=max(int(body.inference_receipt), authorization.receipt_fee_basis_points),
            custom_model=bool(authorization.custom_model_id),
            user_model=bool(authorization.user_provided_model_id),
            tool_cost=bool(authorization.additional_cost_reservation_microdollars),
            native_batch=authorization.native_batch_eligible,
            # Only direct ordinary model names; aliases/programs require proof.
            partner=not body.model.startswith(("openai/", "anthropic/")),
        )
        return projection(authorization=authorization,
                          endpoints=[effective_endpoint(e, at=effective_at) for e in endpoints],
                          requested=requested, settings=settings,
                          runtime=getattr(request.app.state, "async_settle", None))
    except Exception:
        return {"async_eligible": False}
