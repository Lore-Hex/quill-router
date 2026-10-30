"""A leased stream's heartbeat, forwarded to the fast path's front door
(docs/design/fast-admission-streams.md, sections 4.2 to 4.5).

The heartbeat route runs today's transaction on the authorization's row
first, unchanged; this module then sends the accepted heartbeat to a front
door, which hands it to the owner of the lease the envelope names, and reads
the answer as today's heartbeat contract: an accepted heartbeat with the
owner's deadline, a rejection with a reason from the enclave's closed set,
or a retry (503). Which authorizations are leased is the row's to say
(``fastpath_envelope``), never a setting's: a stream admitted under a lease
keeps heartbeating its owner whatever the switches do.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.services import fastpath_frontdoor as frontdoor
from trusted_router.stage_d import billing_pricing_snapshot, parse_pricing_snapshot
from trusted_router.storage_models import GatewayAuthorization

logger = logging.getLogger("trusted_router.fastpath")

BASIS_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The front door's answers that are rejections, by the reason the enclave
# is told (its closed set, quill-cloud-proxy internal/trustedrouter/
# stage_d.go). deadline_passed is already_terminal: the enclave has no reason
# for it, and a 503 would retry into the same answer (design section 11).
# rejected and invalid mean the owner refused what today's transaction
# accepted, or an envelope whose seal failed: a divergence, counted.
REJECTION_REASONS = {
    frontdoor.STALE: "stale_seq",
    frontdoor.DECIDED: "already_terminal",
    frontdoor.DEADLINE_PASSED: "already_terminal",
    frontdoor.REJECTED: "out_of_cohort",
    frontdoor.INVALID: "out_of_cohort",
}
DIVERGENCES = frozenset({frontdoor.REJECTED, frontdoor.INVALID})


@dataclass(frozen=True)
class Forwarded:
    """What the route answers: the deadline of an accepted heartbeat, or the
    reason of a rejection, or neither (retry, a 503)."""

    deadline: datetime | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.deadline is not None


def heartbeat_digest(canonical_body: bytes) -> bytes:
    """The heartbeat's hash as the owner takes it: the 32 bytes of the
    SHA-256 whose hex the row stores as heartbeat_hash."""
    return hashlib.sha256(canonical_body).digest()


def usage_total(counts: dict[str, int]) -> int:
    """The owner's scalar usage: input plus output tokens. Today's
    transaction refuses any count below the stored one first, so the sum
    never falls."""
    return int(counts["input_tokens"]) + int(counts["output_tokens"])


def _price_entry(authorization: GatewayAuthorization, endpoint_id: str) -> Any:
    document = billing_pricing_snapshot(authorization)
    if document is None and authorization.pricing_snapshot is not None:
        document = parse_pricing_snapshot(authorization.pricing_snapshot)
    for candidate in (document or {}).get("candidates") or ():
        if isinstance(candidate, dict) and candidate.get("endpoint_id") == endpoint_id:
            return candidate
    return None


def basis(authorization: GatewayAuthorization, *, selected_endpoint_id: str) -> bytes:
    """The reap's basis a hold's first heartbeat carries (design section
    4.5): the authorization's terms a reap needs to build its full record
    from the lease's own records. Canonical JSON, a pure function of the row
    and the first heartbeat's endpoint, so a replay of seq 1 sends the same
    bytes."""
    snapshot = authorization.pricing_snapshot
    document = {
        "v": BASIS_VERSION,
        "authorization_id": authorization.id,
        "workspace_id": authorization.workspace_id,
        "key_hash": authorization.key_hash,
        "model_id": authorization.model_id,
        "provider": authorization.provider,
        "endpoint_id": selected_endpoint_id,
        "price": _price_entry(authorization, selected_endpoint_id),
        "pricing_snapshot_sha256": (
            hashlib.sha256(snapshot.encode("utf-8")).hexdigest() if snapshot is not None else None
        ),
        "receipt_fee_basis_points": int(authorization.receipt_fee_basis_points or 0),
        "app_markup_basis_points": int(authorization.app_markup_basis_points or 0),
        "estimate": int(authorization.estimated_microdollars),
        "stage_d_prompt_tokens": authorization.stage_d_prompt_tokens,
        "stage_d_max_output_tokens": authorization.stage_d_max_output_tokens,
        "boot": base64.b64encode((authorization.stage_d_boot_kid or "").encode("utf-8")).decode("ascii"),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def epoch_millis(value: datetime) -> int:
    """A deadline as the answer's expires_at_ms, floored to the millisecond."""
    return (value - _EPOCH) // timedelta(milliseconds=1)


def forward(
    client: frontdoor.FrontDoorClient | None,
    authorization: GatewayAuthorization,
    *,
    seq: int,
    digest: bytes,
    counts: dict[str, int],
    running: int,
    selected_endpoint_id: str,
) -> Forwarded:
    """Send an accepted heartbeat (a replay too: the owner may not have its
    first attempt) and read the answer. No client, no answer in the budget,
    or `retry` is a retry."""
    if client is None:
        logger.warning("fastpath heartbeat without a front door authorization_id=%s", authorization.id)
        return Forwarded()
    try:
        first_basis = basis(authorization, selected_endpoint_id=selected_endpoint_id) if seq == 1 else None
    except Exception:  # noqa: BLE001 - the row has committed; the enclave retries a 503, never a 500's guess
        logger.exception("fastpath heartbeat basis failed authorization_id=%s seq=%d", authorization.id, seq)
        return Forwarded()
    try:
        answer = client.heartbeat(
            envelope=str(authorization.fastpath_envelope),
            seq=seq,
            digest=digest,
            usage=usage_total(counts),
            running=running,
            basis=first_basis,
        )
    except frontdoor.FrontDoorUnavailable as exc:
        logger.warning(
            "fastpath heartbeat unanswered authorization_id=%s seq=%d why=%s", authorization.id, seq, exc,
        )
        return Forwarded()
    if answer.status == frontdoor.ACCEPTED and answer.deadline is not None:
        return Forwarded(deadline=answer.deadline)
    reason = REJECTION_REASONS.get(answer.status)
    if answer.status in DIVERGENCES:
        logger.error(
            "fastpath.heartbeat_divergence authorization_id=%s seq=%d status=%s",
            authorization.id,
            seq,
            answer.status,
        )
    elif reason is None:
        logger.warning(
            "fastpath heartbeat %s authorization_id=%s seq=%d", answer.status, authorization.id, seq,
        )
    return Forwarded(reason=reason)
