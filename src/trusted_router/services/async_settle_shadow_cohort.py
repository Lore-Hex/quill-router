"""P4a nonblocking request facade with exact fitted-byte exposure.

The caller supplies already available server facts. No function in this module
starts a task, reads storage, consults a catalog or issues an exclusion proof.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from trusted_router.async_settle_shadow_binding import AUDIENCE, LIFETIME, ShadowSigner
from trusted_router.async_settle_shadow_cohort import (
    prepare_authorization,
    receipt_bucket,
    replay_decision,
    replay_event,
    replay_object,
)
from trusted_router.async_settle_shadow_journal import Reservation
from trusted_router.async_settle_shadow_projection import snapshot_material
from trusted_router.async_settle_shadow_v2_contract import LOCAL_BYTES, RequestedFacts
from trusted_router.async_settle_shadow_v2_evidence import EVENT, committed_identity
from trusted_router.async_settle_shadow_v2_schema import POLICY, require
from trusted_router.async_settle_shadow_wire import snapshot_from_object
from trusted_router.config import Settings, in_v2_cohort


class AuthorizePort(Protocol):
    def reserve(self, observed_us: int, *, terminal: bool = False) -> Reservation | None: ...
    def finalize(self, reservation: Reservation, event: dict[str, Any], *, fold_owed: bool = False) -> bytes | None: ...
    def complete(self, reservation: Reservation) -> bool: ...
    def fold_counts(self, day: str, bucket: dict[str, Any]) -> bool: ...


def has_v2_capability(headers: tuple[str, ...]) -> bool:
    """Ruling 1: exactly one marker is capable, independently of cohort opt-in."""
    return headers == (POLICY,)


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
    server_facts: dict[str, Any] | None = None
    server_candidates: list[dict[str, Any]] | None = None
    validated_snapshot: bool = False


def authorize(settings: Settings, capability: tuple[str, ...], port: AuthorizePort,
              inputs: AuthorizeInput, additions: dict[str, Any], signer: ShadowSigner | None,
              *, replay: bool = False, original_page: dict[str, Any] | None = None,
              original_locator: dict[str, Any] | None = None,
              original_decision: dict[str, Any] | None = None,
              retained_original: bool = False,
              finalized_event: Callable[[bytes], None] | None = None,
              finish_obligation: Callable[[Reservation, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Return additive metadata only after the exact fitted slot is finalized.

    No mutation of caller additions or billing DTOs. A replay records its own
    report obligation and never calls the signer or reconstructs prices.
    """
    def finish(reservation: Reservation, event: dict[str, Any]) -> None:
        if finish_obligation is not None:
            finish_obligation(reservation, event)
        else:
            require(port.fold_counts(reservation.day, receipt_bucket(event)), "counter_overflow")
            port.complete(reservation)
    reservation = None
    prepared = None
    finalized = False
    try:
        if not in_v2_cohort(settings.async_settle_shadow_policy, inputs.identity["workspace_id"],
                            settings.async_settle_enabled, settings.async_settle_shadow_workspace_ids):
            return {}
        reservation = port.reserve(inputs.observed_us)
        if reservation is None:
            return {}
        if replay and has_v2_capability(capability) and (original_page is not None or original_decision is not None):
            require(original_locator is not None, "authorization_link_gap")
            assert original_locator is not None
            if original_decision is not None and retained_original:
                event = replay_object(reservation, original=original_decision,
                    original_locator=original_locator, revision=inputs.revision)
            elif original_decision is not None:
                raw = replay_decision(reservation, original=original_decision,
                    original_locator=original_locator, revision=inputs.revision)
                event = EVENT.parse(raw)
            else:
                assert original_page is not None
                raw = replay_event(reservation, original_page=original_page,
                    original_locator=original_locator, revision=inputs.revision)
                event = EVENT.parse(raw)
            require(event["identity"] == committed_identity(inputs.identity), "authorization_link_gap")
            require(port.finalize(reservation, event, fold_owed=finish_obligation is not None) is not None, "unfinished_slot")
            finalized = True
            finish(reservation, event)
            return {}
        prepared = prepare_authorization(reservation, identity=committed_identity(inputs.identity),
            revision=inputs.revision, dimensions=inputs.dimensions, facts=inputs.facts,
            candidates=inputs.candidates, complete_candidates=inputs.complete_candidates,
            complete_supported_context=inputs.complete_supported_context,
            reason="header_absent" if not has_v2_capability(capability) else "replay" if replay else inputs.reason,
            server_facts=inputs.server_facts, server_candidates=inputs.server_candidates,
            native_request=finish_obligation is not None)
        metadata: dict[str, Any] = {}
        if prepared.decision.eligibility == "eligible":
            require(signer is not None, "missing_signer")
            assert signer is not None
            if inputs.validated_snapshot:
                # authorize_additions owns this server-produced pricing projection.
                # Keep hash/context agreement without reconstructing its DTO.
                import hashlib

                from trusted_router.detached_jws import canonical
                snapshot = additions["billing_snapshot"]
                material = canonical(snapshot)
                digest = hashlib.sha256(material).hexdigest()
                candidates = snapshot["candidates"]
                require({c["endpoint_id"] for c in candidates}
                        == {c["endpoint_id"] for c in prepared.event["candidates"]}, "missing_context")
                require(all(c["provider"] == p["adapter"] for c in candidates
                            for p in prepared.event["candidates"] if c["endpoint_id"] == p["endpoint_id"]), "missing_context")
            else:
                supplied = snapshot_from_object(additions["billing_snapshot"])
                material, digest = snapshot_material(supplied)
                require({c.endpoint_id for c in supplied.candidates}
                        == {c["endpoint_id"] for c in prepared.event["candidates"]}, "missing_context")
                require(all(c.provider == p["adapter"] for c in supplied.candidates
                            for p in prepared.event["candidates"] if c.endpoint_id == p["endpoint_id"]), "missing_context")
            require(len(material) <= LOCAL_BYTES, "snapshot_size")
            require(digest == additions["billing_snapshot_hash"], "hash")
            event = prepared.event
            # The fitted EVENT already owns this validated projection.
            facts = event["facts"]
            issued = inputs.observed_us // 1000000
            claims = dict(inputs.identity, route_type=facts["route"], streamed=facts["streamed"],
                snapshot_version=1, snapshot_hash=digest, async_eligible=False,
                iss=signer.trusted.iss, aud=AUDIENCE, iat=issued, exp=issued+LIFETIME)
            metadata = dict(billing_shadow_policy=POLICY,
                            billing_shadow_binding=signer.sign(claims, issued))
        event = prepared.event
        if replay:
            event.update(phase="replay", receipt_locator=None, facts=None, candidates=None,
                         original_receipt=None, certificate_sha256=None, payload_sha256=None,
                         proof_kind="none", comparison=None, prediction=None, retry_original=None,
                         completion_of=None, booking=dict(outcome="not_attempted", reference=None, winner=None))
            prepared = type(prepared)(prepared.decision, event)
        finalized_body = port.finalize(reservation, event, fold_owed=finish_obligation is not None)
        require(finalized_body is not None, "unfinished_slot")
        assert finalized_body is not None
        finalized = True
        finish(reservation, event)
        if finalized_event is not None:
            finalized_event(finalized_body)
        return metadata
    except Exception as error:
        loss = getattr(port, "lost", None)
        if loss is not None:
            loss.set()
        # A failed slot stays owned by P3 and blocks closure. A fitted receipt
        # can retain a bounded diagnostic in that same slot before exposure.
        if reservation is not None and prepared is not None and not finalized:
            try:
                reason = str(error) if isinstance(error, ValueError) and str(error) in {
                    "missing_signer", "snapshot_size", "hash", "original_receipt_digest_gap",
                } else "missing_context"
                event = {**prepared.event, "eligibility": "unknown", "facts": None, "candidates": None,
                         "diagnostic": dict(reason=reason, required_bytes=0, candidate_count=0)}
                if port.finalize(reservation, event, fold_owed=finish_obligation is not None) is not None:
                    finish(reservation, event)
            except Exception:  # noqa: S110 - the journal keeps the unfinished slot
                pass
        return {}
