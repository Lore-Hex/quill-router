"""Flag-gated Spanner/Bigtable coordinator for bound spend-lease authorize.

The gateway imports this module only when ``TR_SPEND_LEASE_BINDING_ENABLED`` is
true.  Candidate creation is deliberately outside the billing transaction;
all cross-store authority is represented by the candidate work row, BOUND or
CLAIM proof, and the post-commit bind seam.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from trusted_router.spend_lease_authorize import (
    FenceView,
    SpendLeaseArbitrationConflict,
    SpendLeaseContractError,
    SpendLeaseMintLost,
    classify_fence_loss,
    derive_candidate_lease_id,
)
from trusted_router.spend_lease_ledger import SpendLeaseLedger
from trusted_router.spend_lease_state import (
    AbsenceObservation,
    BindingAbsenceProof,
    BoundProof,
    ClaimProof,
    Created,
    ExistingLocal,
    Mismatch,
    SpendLease,
)
from trusted_router.spend_leases import (
    FrozenSpendLeaseCatalog,
    SpendLeaseArtifact,
    SpendLeaseSigner,
)
from trusted_router.storage_gcp_counter_dml import (
    insert_entity_dml,
    read_reservation_by_idempotency,
    release_credit,
    reserve_credit,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_spend_lease import (
    CandidateIdentity,
    delete_bound,
    insert_candidate,
    read_registration,
    register_bound,
    register_claim,
    upgrade_candidate_to_open,
)

SPEND_LEASE_KIND = "spend_lease"
FENCE_KIND = "spend_lease_active_grant"
PREDECESSOR_LIMIT = 3


class SpendLeaseReuseLost(RuntimeError):
    def __init__(self, reason: Literal["lease_transferred", "lease_expired"]) -> None:
        super().__init__(reason)
        self.reason = reason


class SpendLeaseExistingLocal(RuntimeError):
    """A different local creator owns this scope; use ordinary authorize."""


@dataclass(frozen=True, slots=True)
class BindingPlan:
    ledger: SpendLeaseLedger
    scope: str
    fence_id: str
    region: str
    provisional_id: str
    artifact: SpendLeaseArtifact
    allocation_micro: int
    admission_deadline: datetime
    mode: Literal["reuse", "mint"]
    candidate: CandidateIdentity | None
    observed_gen: int
    incumbent_lease_id: str | None
    incumbent_window_closed: bool
    authoritative_exhaustion: bool
    remaining_micro: int | None = None

    def transaction_hook(self, transaction: Any, param_types: Any, workspace_id: str, shard: int) -> dict[str, Any]:
        if self.mode == "reuse":
            return self._reuse_hook(transaction, param_types)
        return self._mint_hook(transaction, param_types, workspace_id, shard)

    def _register(self, transaction: Any, param_types: Any) -> bool:
        count = register_bound(
            transaction,
            param_types,
            self.scope,
            self.provisional_id,
            self.artifact.lease_id,
            self.artifact.gen,
            self.allocation_micro,
        )
        if count == 1:
            return True
        winner = read_registration(transaction, param_types, self.scope)
        if winner is None:
            raise SpendLeaseContractError("BOUND insert returned zero without a winner")
        if winner.kind == "CLAIM":
            return False
        if winner.authorization_id != self.provisional_id:
            raise SpendLeaseArbitrationConflict("foreign BOUND won scope arbitration")
        return True

    def _reuse_hook(self, transaction: Any, param_types: Any) -> dict[str, Any]:
        if not self._register(transaction, param_types):
            return _unbound("scope_arbitrated", "scope_claimed")
        fence = _read_fence(transaction, param_types, self.fence_id, self.artifact.lease_id)
        if fence is None or fence["gen"] > self.observed_gen:
            raise SpendLeaseReuseLost("lease_transferred")
        now = datetime.now(UTC)
        if fence["gen"] != self.observed_gen or now >= self.admission_deadline:
            raise SpendLeaseReuseLost("lease_expired")
        return _bound("reuse_bound")

    def _mint_hook(self, transaction: Any, param_types: Any, workspace_id: str, shard: int) -> dict[str, Any]:
        if not reserve_credit(transaction, param_types, workspace_id, self.artifact.cap_micro, shard=shard):
            return _unbound("escrow_headroom", "escrow_refused")
        if not self._register(transaction, param_types):
            _require_one(
                release_credit(
                    transaction,
                    param_types,
                    workspace_id,
                    self.artifact.cap_micro,
                    0,
                    shard=shard,
                ),
                "CLAIM escrow inverse",
            )
            return _unbound("scope_arbitrated", "scope_claimed")

        mark_count = _mark_incumbent(
            transaction,
            param_types,
            self.incumbent_lease_id,
        )
        fence_count = _advance_fence(
            transaction,
            param_types,
            artifact=self.artifact,
            fence_id=self.fence_id,
            observed_gen=self.observed_gen,
            mark_count=mark_count,
            window_closed=self.incumbent_window_closed,
            authoritative_exhaustion=self.authoritative_exhaustion,
        )
        if fence_count == 0:
            current = _read_fence(
                transaction, param_types, self.fence_id, self.artifact.lease_id
            )
            decision = classify_fence_loss(
                observed_gen=self.observed_gen,
                incumbent_mark_count=mark_count,
                predecessor_limit=PREDECESSOR_LIMIT,
                window_closed=self.incumbent_window_closed,
                authoritative_exhaustion=self.authoritative_exhaustion,
                statement_window_open=datetime.now(UTC) < _artifact_expiry(self.artifact),
                current=None if current is None else FenceView(
                    gen=current.get("gen"),
                    open_predecessor_count=current.get("open_predecessor_count"),
                    active_lease_id=current.get("lease_id"),
                    active_lease_valid=current.get("active_valid"),
                ),
            )
            _require_one(delete_bound(transaction, param_types, self.scope, self.provisional_id), "BOUND inverse")
            if mark_count:
                _require_one(_unmark_incumbent(transaction, param_types, self.incumbent_lease_id), "incumbent inverse")
            _require_one(
                release_credit(transaction, param_types, workspace_id, self.artifact.cap_micro, 0, shard=shard),
                "fence escrow inverse",
            )
            if decision.reason is None:
                raise SpendLeaseContractError(f"fence loss: {decision.outcome}")
            outcomes = {
                "lease_transferred": "fence_lost_race",
                "stale_advisory": "fence_stale_advisory",
                "predecessor_limit": "fence_count_exhausted",
                "window_open": "fence_window_open",
            }
            return _unbound(decision.reason, outcomes[decision.reason])

        assert self.candidate is not None
        global_lease = {
            "state": "ACTIVE",
            "lease_id": self.artifact.lease_id,
            "gen": self.artifact.gen,
            "key_hash": self.candidate.key_hash,
            "boot_kid": self.candidate.boot_kid,
            "workspace_id": self.candidate.workspace_id,
            "region": self.candidate.region,
            "cap_micro": self.artifact.cap_micro,
            "expires_at": self.candidate.expires_at.isoformat(),
            "skew_seconds": self.candidate.skew_seconds,
            "credit_shard": shard,
            "frozen_local_version": None,
            "holds_predecessor_slot": False,
            "closing_at": None,
            "last_error": None,
        }
        if self.artifact.local_admission_allowed:
            global_lease.update(
                token=self.artifact.token,
                iat=self.artifact.iat,
                exp=self.artifact.exp,
                issuer_kid=self.artifact.issuer_kid,
                catalog_version=self.artifact.catalog_version,
                routing_policy_hash=self.artifact.routing_policy_hash,
                catalog=self.artifact.catalog,
                local_admission_allowed=True,
            )
        insert_entity_dml(
            transaction,
            param_types,
            SPEND_LEASE_KIND,
            self.artifact.lease_id,
            json.dumps(global_lease, separators=(",", ":"), sort_keys=True),
        )
        if upgrade_candidate_to_open(
            transaction,
            param_types,
            self.candidate.lease_id,
            self.provisional_id,
            self.candidate.expires_at,
            self.candidate.skew_seconds,
        ) != 1:
            raise SpendLeaseMintLost("candidate recovery won the phase handoff")
        return _bound("mint_bound")

    def bind_after_commit(self) -> None:
        proof = BoundProof(
            self.scope,
            self.provisional_id,
            self.artifact.lease_id,
            self.artifact.gen,
            self.allocation_micro,
        )
        # TODO(decision 21(f)): PR 4's reconciler binds from the surviving open
        # row if this process crashes after Spanner commit and before this CAS.
        self.ledger.bind(
            self.artifact.lease_id,
            region=self.region,
            expected_provisional_id=self.provisional_id,
            proof=proof,
        )

    def compensate_with_claim(self, database: Any, param_types: Any) -> None:
        def claim_txn(transaction: Any) -> None:
            if register_claim(transaction, param_types, self.scope, self.provisional_id) == 0:
                registration = read_registration(transaction, param_types, self.scope)
                if registration is None or registration.kind != "CLAIM" or registration.provisional_id != self.provisional_id:
                    raise SpendLeaseContractError("compensation CLAIM was not authoritative")

        run_in_transaction_with_retry(database, claim_txn, transaction_tag="tr_spend_lease_claim")
        self.ledger.compensate(
            self.artifact.lease_id,
            region=self.region,
            idempotency_scope=self.scope,
            expected_provisional_id=self.provisional_id,
            claim=ClaimProof(self.scope, self.provisional_id),
            absence=BindingAbsenceProof(self.scope, self.provisional_id, AbsenceObservation.NON_BINDING_ROW),
        )


def reservation_exists(database: Any, param_types: Any, scope: str) -> bool:
    """Decision 31's one strong, reservation-only pre-read."""
    with database.snapshot() as snapshot:
        return read_reservation_by_idempotency(snapshot, param_types, scope) is not None


def ensure_initial_fence(database: Any, param_types: Any, fence_id: str) -> None:
    """Idempotently create generation zero before the first bound mint."""

    body = json.dumps(
        {
            "lease_id": "unminted",
            "gen": 0,
            "open_predecessor_count": 0,
            "lease_status": "terminal",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    def txn(transaction: Any) -> None:
        transaction.execute_update(
            "INSERT OR IGNORE INTO tr_entities (kind, id, body, updated_at) "
            "VALUES (@kind, @id, @body, PENDING_COMMIT_TIMESTAMP())",
            params={"kind": FENCE_KIND, "id": fence_id, "body": body},
            param_types={
                "kind": param_types.STRING,
                "id": param_types.STRING,
                "body": param_types.STRING,
            },
        )

    run_in_transaction_with_retry(database, txn, transaction_tag="tr_spend_lease_fence_init")


def prepare_candidate(
    *,
    database: Any,
    param_types: Any,
    ledger: SpendLeaseLedger,
    signer: SpendLeaseSigner,
    scope: str,
    fence_id: str,
    provisional_id: str,
    workspace_id: str,
    key_hash: str,
    boot_kid: str,
    region: str,
    gen: int,
    cap_micro: int,
    allocation_micro: int,
    ttl_seconds: int,
    skew_seconds: int,
    request_fingerprint: str,
    catalog: dict[str, Any],
    observed_gen: int,
    observed_predecessor_count: int,
    incumbent_lease_id: str | None,
    incumbent_window_closed: bool,
    authoritative_exhaustion: bool,
    local_admission_allowed: bool = False,
    routing_policy_hash: str | None = None,
) -> BindingPlan:
    now = datetime.now(UTC)
    lease_id = derive_candidate_lease_id(key_hash, boot_kid, gen, provisional_id)
    expires_at = now + timedelta(seconds=ttl_seconds)
    claims = {
        "v": 1, "typ": "spend-lease+jws", "authoritative": True,
        "lease_id": lease_id, "key_hash": key_hash, "workspace_id": workspace_id,
        "cap_micro": cap_micro, "gen": gen, "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()), "boot_kid": boot_kid, "catalog": catalog,
    }
    if local_admission_allowed:
        if routing_policy_hash is None:
            raise SpendLeaseContractError("local admission requires a routing policy hash")
        claims.update(
            local_admission_allowed=True,
            routing_policy_hash=routing_policy_hash,
        )
    artifact = SpendLeaseArtifact(
        token=signer.sign(claims), lease_id=lease_id, cap_micro=cap_micro, gen=gen,
        iat=int(now.timestamp()), exp=int(expires_at.timestamp()), issuer_kid=signer.kid,
        boot_kid=boot_kid, catalog_version=str(catalog["version"]),
        open_predecessor_count=observed_predecessor_count,
        local_admission_allowed=local_admission_allowed,
        routing_policy_hash=routing_policy_hash,
        catalog=(
            cast(FrozenSpendLeaseCatalog, dict(catalog))
            if local_admission_allowed
            else None
        ),
    )
    identity = CandidateIdentity(
        lease_id, gen, key_hash, boot_kid, cap_micro, skew_seconds, workspace_id,
        region, provisional_id, scope, expires_at,
    )
    run_in_transaction_with_retry(
        database,
        lambda transaction: insert_candidate(transaction, param_types, identity, created_at=now),
        transaction_tag="tr_spend_lease_candidate",
    )
    ledger.initialize(
        SpendLease(
            lease_id, gen, key_hash, boot_kid, workspace_id, provisional_id,
            cap_micro, expires_at, timedelta(seconds=skew_seconds), 0,
        ),
        region=region,
    )
    result = ledger.allocate(
        None, lease_id, region=region, idempotency_scope=scope,
        provisional_authorization_id=provisional_id,
        request_fingerprint=request_fingerprint, allocated_micro=allocation_micro,
        abandon_after=expires_at + timedelta(seconds=skew_seconds), now=now,
    )
    if isinstance(result, Mismatch):
        raise SpendLeaseContractError("candidate allocation mismatched its creating attempt")
    if isinstance(result, ExistingLocal):
        raise SpendLeaseExistingLocal("different local creator owns the allocation")
    if not isinstance(result, Created):
        raise SpendLeaseContractError(f"unexpected new-candidate result: {type(result).__name__}")
    return BindingPlan(
        ledger, scope, fence_id, region, provisional_id, artifact, allocation_micro,
        expires_at + timedelta(seconds=skew_seconds), "mint", identity,
        observed_gen, incumbent_lease_id, incumbent_window_closed, authoritative_exhaustion,
    )


def _bound(outcome: str) -> dict[str, Any]:
    return {"bound": True, "no_lease_reason": None, "spend_lease_outcome": outcome}


def _unbound(reason: str, outcome: str) -> dict[str, Any]:
    return {"bound": False, "no_lease_reason": reason, "spend_lease_outcome": outcome}


def _require_one(count: int, inverse: str) -> None:
    if count != 1:
        raise SpendLeaseContractError(f"{inverse} modified {count} rows")


def _read_fence(
    transaction: Any,
    param_types: Any,
    fence_id: str,
    candidate_lease_id: str,
) -> dict[str, Any] | None:
    rows = list(transaction.execute_sql(
        "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
        params={"kind": FENCE_KIND, "id": fence_id},
        param_types={"kind": param_types.STRING, "id": param_types.STRING},
    ))
    if not rows:
        return None
    body = json.loads(rows[0][0])
    active_id = str(body.get("lease_id") or "")
    if body.get("lease_status") == "terminal" or active_id == "unminted":
        body["active_valid"] = False
    else:
        global_rows = list(
            transaction.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
                params={"kind": SPEND_LEASE_KIND, "id": active_id},
                param_types={"kind": param_types.STRING, "id": param_types.STRING},
            )
        )
        if not global_rows:
            body["active_valid"] = None
        else:
            global_lease = json.loads(global_rows[0][0])
            consistent = (
                global_lease.get("lease_id") == active_id
                and int(global_lease.get("gen", -1)) == int(body.get("gen", -2))
            )
            expires_at = _global_expiry(global_lease)
            body["active_valid"] = bool(
                consistent
                and global_lease.get("state") == "ACTIVE"
                and expires_at is not None
                and expires_at > datetime.now(UTC)
            )
    return {str(key): value for key, value in body.items()}


def _mark_incumbent(transaction: Any, param_types: Any, lease_id: str | None) -> int:
    if lease_id is None:
        return 0
    return int(transaction.execute_update(  # noqa: S608 - fixed trusted suffix
        "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
        "'$.holds_predecessor_slot', true)) "
        "WHERE kind=@kind AND id=@id AND JSON_VALUE(body, '$.state') IN ('ACTIVE','DRAINING','TOMBSTONED') "
        "AND JSON_VALUE(body, '$.holds_predecessor_slot')='false'",
        params={"kind": SPEND_LEASE_KIND, "id": lease_id},
        param_types={"kind": param_types.STRING, "id": param_types.STRING},
    ))


def _unmark_incumbent(
    transaction: Any,
    param_types: Any,
    lease_id: str | None,
    *,
    frozen_only: bool = False,
) -> int:
    if lease_id is None:
        return 0
    if frozen_only:
        statement = (
            "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
            "'$.holds_predecessor_slot', false)) "
            "WHERE kind=@kind AND id=@id "
            "AND JSON_VALUE(body, '$.holds_predecessor_slot')='true' "
            "AND JSON_VALUE(body, '$.state') IN ('DRAINING','TOMBSTONED')"
        )
    else:
        statement = (
            "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
            "'$.holds_predecessor_slot', false)) "
            "WHERE kind=@kind AND id=@id "
            "AND JSON_VALUE(body, '$.holds_predecessor_slot')='true'"
        )
    return int(transaction.execute_update(
        statement,
        params={"kind": SPEND_LEASE_KIND, "id": lease_id},
        param_types={"kind": param_types.STRING, "id": param_types.STRING},
    ))


def _mark_closing(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    closing_at: datetime,
) -> int:
    """Flag-false close guard; callers abort the transaction on zero."""

    return int(
        transaction.execute_update(
            "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
            "'$.closing_at', @closing_at_text)) "
            "WHERE kind=@kind AND id=@id "
            "AND JSON_VALUE(body, '$.state') IN ('DRAINING','TOMBSTONED') "
            "AND JSON_VALUE(body, '$.holds_predecessor_slot')='false'",
            params={
                "kind": SPEND_LEASE_KIND,
                "id": lease_id,
                "closing_at_text": closing_at.isoformat(),
            },
            param_types={
                "kind": param_types.STRING,
                "id": param_types.STRING,
                "closing_at_text": param_types.STRING,
            },
        )
    )


def _decrement_fence(
    transaction: Any,
    param_types: Any,
    fence_id: str,
) -> int:
    """Release one predecessor slot, guarded against count underflow."""

    return int(
        transaction.execute_update(
            "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
            "'$.open_predecessor_count', "
            "CAST(COALESCE(JSON_VALUE(body, '$.open_predecessor_count'),'0') AS INT64)-1)) "
            "WHERE kind=@kind AND id=@id "
            "AND CAST(COALESCE(JSON_VALUE(body, '$.open_predecessor_count'),'0') AS INT64)>0",
            params={"kind": FENCE_KIND, "id": fence_id},
            param_types={"kind": param_types.STRING, "id": param_types.STRING},
        )
    )


def _close_global_lease(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    body: str,
) -> int:
    """Publish CLOSED only after every earlier close-step guard succeeded."""

    return int(
        transaction.execute_update(
            "UPDATE tr_entities SET body=@body WHERE kind=@kind AND id=@id "
            "AND JSON_VALUE(body, '$.state') IN ('DRAINING','TOMBSTONED')",
            params={"body": body, "kind": SPEND_LEASE_KIND, "id": lease_id},
            param_types={
                "body": param_types.STRING,
                "kind": param_types.STRING,
                "id": param_types.STRING,
            },
        )
    )


def _advance_fence(
    transaction: Any,
    param_types: Any,
    *,
    artifact: SpendLeaseArtifact,
    fence_id: str,
    observed_gen: int,
    mark_count: int,
    window_closed: bool,
    authoritative_exhaustion: bool,
) -> int:
    next_artifact = dataclasses.replace(
        artifact,
        open_predecessor_count=artifact.open_predecessor_count + mark_count,
    )
    body = json.dumps(
        dataclasses.asdict(next_artifact), separators=(",", ":"), sort_keys=True
    )
    return int(transaction.execute_update(
        "UPDATE tr_entities SET body=@body WHERE kind=@kind AND id=@id "
        "AND CAST(JSON_VALUE(body, '$.gen') AS INT64)=@observed_gen "
        "AND CAST(COALESCE(JSON_VALUE(body, '$.open_predecessor_count'),'0') AS INT64)+@mark_count<=@limit "
        "AND (@window_closed OR @authoritative_exhaustion) AND CURRENT_TIMESTAMP()<@candidate_expires_at",
        params={
            "body": body, "kind": FENCE_KIND, "id": fence_id, "observed_gen": observed_gen,
            "mark_count": mark_count, "limit": PREDECESSOR_LIMIT,
            "window_closed": window_closed, "authoritative_exhaustion": authoritative_exhaustion,
            "candidate_expires_at": _artifact_expiry(artifact),
        },
        param_types={
            "body": param_types.STRING, "kind": param_types.STRING, "id": param_types.STRING,
            "observed_gen": param_types.INT64, "mark_count": param_types.INT64,
            "limit": param_types.INT64, "window_closed": param_types.BOOL,
            "authoritative_exhaustion": param_types.BOOL, "candidate_expires_at": param_types.TIMESTAMP,
        },
    ))


def _artifact_expiry(artifact: SpendLeaseArtifact) -> datetime:
    return datetime.fromtimestamp(artifact.exp, tz=UTC)


def _global_expiry(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("expires_at")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    legacy_exp = payload.get("exp")
    if isinstance(legacy_exp, int):
        return datetime.fromtimestamp(legacy_exp, tz=UTC)
    return None
