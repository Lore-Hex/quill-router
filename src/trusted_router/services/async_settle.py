"""Authorize-only async v1 projection and fail-closed, bounded admission cache.

PR D must publish fleet-wide drain health; no local empty queue is evidence of
fleet health. This module adds no settlement writer, worker, or status handler.
"""
from __future__ import annotations

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
from trusted_router.speculation_protocol import TrustedKey, _b64encode
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
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.read, self.clock = read, clock
        self.lock = threading.Lock()
        self.entries: dict[str, tuple[float, Admission | None]] = {}
        self.health: DrainHealth | None = None

    def eligible(self, workspace_id: str, pilot_cap: int) -> bool:
        # Lock coalesces concurrent misses, including failures. Timestamp at
        # read START, so a slow read cannot turn old evidence into fresh data.
        if not self.lock.acquire(timeout=0.2):
            return False
        try:
            now = self.clock()
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


@dataclass
class Runtime:
    signer: TicketSigner | None
    admission: AdmissionCache | None
    region: str
    epoch: int


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
            _b64encode(private.public_key().public_bytes_raw()),
            settings.async_settle_ticket_issuer, settings.async_settle_ticket_audience))
    except Exception:
        signer = None
    admission = None
    if backend is not None and hasattr(backend, "_database"):
        from trusted_router.storage_gcp_async_admission import read_admission
        admission = AdmissionCache(lambda ws: read_admission(backend._database, ws))
    return Runtime(signer, admission, settings.primary_region, settings.async_settle_authority_epoch)


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
                or not runtime.region or not authorization.credit_reservation_id
                or authorization.settlement != "local" or authorization.settled):
            return result
        digest = canonical_hash(snapshot)
        eligible = bool(settings.async_settle_enabled and runtime.admission is not None
                        and runtime.admission.eligible(authorization.workspace_id,
                                                       settings.async_settle_pilot_cap_micro))
        issued = int(time.time()) if now is None else now
        signer = runtime.signer
        claims = dict(
            authorization_id=authorization.id,
            generation_id=generation_id_for_authorization(authorization.id),
            workspace_id=authorization.workspace_id, key_id=authorization.key_hash,
            invocation_nonce=authorization.invocation_nonce, billing_authority="local",
            journal_region=runtime.region, epoch=runtime.epoch, snapshot_version=1,
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
