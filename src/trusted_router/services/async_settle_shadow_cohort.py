"""P4a request facade; installation awaits the explicit capability/receipt seams.

The caller supplies already available server facts. No function in this module
starts a task, reads storage, consults a catalog or issues an exclusion proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from trusted_router.async_settle_shadow_binding import AUDIENCE, LIFETIME, ShadowSigner
from trusted_router.async_settle_shadow_cohort import (
    prepare_authorization,
    receipt_bucket,
    replay_event,
    unresolved_authorization,
)
from trusted_router.async_settle_shadow_journal import Reservation
from trusted_router.async_settle_shadow_projection import snapshot_material
from trusted_router.async_settle_shadow_v2_contract import LOCAL_BYTES, RequestedFacts
from trusted_router.async_settle_shadow_v2_evidence import EVENT, committed_identity
from trusted_router.async_settle_shadow_v2_schema import POLICY, require
from trusted_router.async_settle_shadow_wire import snapshot_from_object
from trusted_router.config import Settings


class AuthorizePort(Protocol):
    def reserve(self, observed_us: int, *, terminal: bool = False) -> Reservation | None: ...
    def finalize(self, reservation: Reservation, event: dict[str, Any]) -> bytes | None: ...
    def complete(self, reservation: Reservation) -> bool: ...
    def fold_counts(self, day: str, bucket: dict[str, Any]) -> bool: ...


def is_v2_request(settings: Settings, workspace: str, capability: tuple[str, ...]) -> bool:
    """The one complete activation predicate; duplicate markers are not capable."""
    return (settings.async_settle_shadow_policy == POLICY
            and workspace in settings.async_settle_shadow_workspace_ids
            and not settings.async_settle_enabled and capability == (POLICY,))


@dataclass(frozen=True)
class AuthorizeInput:
    observed_us: int
    identity: dict[str, Any]
    revision: str
    dimensions: dict[str, Any]
    facts: RequestedFacts | None
    candidates: bytes | None
    complete_candidates: bool
    complete_supported_context: bool
    reason: str | None


def authorize(settings: Settings, capability: tuple[str, ...], port: AuthorizePort,
              inputs: AuthorizeInput, additions: dict[str, Any], signer: ShadowSigner | None,
              *, replay: bool = False, original_page: dict[str, Any] | None = None,
              original_locator: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return additive metadata only after the exact fitted slot is finalized.

    No mutation of caller additions or billing DTOs. A replay records its own
    report obligation and never calls the signer or reconstructs prices.
    """
    reservation = None
    prepared = None
    finalized = False
    try:
        if not is_v2_request(settings, inputs.identity["workspace_id"], capability):
            return {}
        reservation = port.reserve(inputs.observed_us)
        if reservation is None:
            return {}
        if replay:
            require(original_page is not None and original_locator is not None, "authorization_link_gap")
            assert original_page is not None and original_locator is not None
            raw = replay_event(reservation, original_page=original_page,
                               original_locator=original_locator, revision=inputs.revision)
            event = EVENT.parse(raw)
            require(event["identity"] == committed_identity(inputs.identity), "authorization_link_gap")
            require(port.finalize(reservation, event) == raw, "original_receipt_digest_gap")
            finalized = True
            port.complete(reservation)
            port.fold_counts(reservation.day, receipt_bucket(event))
            return {}
        prepared = prepare_authorization(reservation, identity=committed_identity(inputs.identity),
            revision=inputs.revision, dimensions=inputs.dimensions, facts=inputs.facts,
            candidates=inputs.candidates, complete_candidates=inputs.complete_candidates,
            complete_supported_context=inputs.complete_supported_context, reason=inputs.reason)
        metadata: dict[str, Any] = {}
        if prepared.decision.eligibility == "eligible":
            require(signer is not None, "missing_signer")
            assert signer is not None and inputs.facts is not None
            snapshot = snapshot_from_object(additions["billing_snapshot"])
            material, digest = snapshot_material(snapshot)
            require(len(material) <= LOCAL_BYTES, "snapshot_size")
            require(digest == additions["billing_snapshot_hash"], "hash")
            event = EVENT.parse(prepared.body)
            require({c.endpoint_id for c in snapshot.candidates}
                    == {c["endpoint_id"] for c in event["candidates"]}, "missing_context")
            require(all(c.provider == p["adapter"] for c in snapshot.candidates
                        for p in event["candidates"] if c.endpoint_id == p["endpoint_id"]), "missing_context")
            facts = inputs.facts.object()
            issued = inputs.observed_us // 1000000
            claims = dict(inputs.identity, route_type=facts["route"], streamed=facts["streamed"],
                snapshot_version=1, snapshot_hash=digest, async_eligible=False,
                iss=signer.trusted.iss, aud=AUDIENCE, iat=issued, exp=issued+LIFETIME)
            metadata = dict(billing_shadow_policy=POLICY,
                            billing_shadow_binding=signer.sign(claims, issued))
        event = EVENT.parse(prepared.body)
        require(port.finalize(reservation, event) == prepared.body, "original_receipt_digest_gap")
        finalized = True
        port.complete(reservation)
        port.fold_counts(reservation.day, receipt_bucket(event))
        return metadata
    except Exception as error:
        # A failed slot stays owned by P3 and blocks closure. A fitted receipt
        # can retain a bounded diagnostic in that same slot before exposure.
        if reservation is not None and prepared is not None and not finalized:
            try:
                reason = str(error) if isinstance(error, ValueError) and str(error) in {
                    "missing_signer", "snapshot_size", "hash", "original_receipt_digest_gap",
                } else "missing_context"
                event = EVENT.parse(unresolved_authorization(prepared, reason))
                if port.finalize(reservation, event) is not None:
                    port.complete(reservation)
                    port.fold_counts(reservation.day, receipt_bucket(event))
            except Exception:  # noqa: S110 - the journal keeps the unfinished slot
                pass
        return {}
