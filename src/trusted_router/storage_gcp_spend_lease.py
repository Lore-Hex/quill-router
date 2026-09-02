"""Inert native-Spanner data access for spend-lease unit 2.

This module contains only statement-sized persistence primitives.  It does not
select protocol outcomes, run recovery, consume feature flags, or participate
in the gateway path.  Callers own transaction boundaries and interpret every
returned modified-row count.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from trusted_router import spend_leases
from trusted_router.services.spend_lease_settlement import derive_spend_lease_settlement
from trusted_router.spend_lease_state import BindingTuple

RegistrationKind = Literal["BOUND", "CLAIM"]
OpenPhase = Literal["candidate", "recovering", "open", "done"]

AUTHORIZATION_TYPED_COLUMNS = (
    "spend_lease_id",
    "spend_lease_gen",
    "spend_lease_allocated_micro",
    "spend_lease_token",
    "spend_lease_status",
    "spend_lease_exp",
    "idempotency_fingerprint",
    "finalization_outcome",
    "finalized_cost_microdollars",
)

ARBITRATION_COLUMNS = (
    "registration_kind",
    "authorization_id",
    "spend_lease_id",
    "spend_lease_gen",
    "spend_lease_allocated_micro",
    "provisional_id",
)

OPEN_COLUMNS = (
    "lease_id",
    "phase",
    "gen",
    "key_hash",
    "boot_kid",
    "cap_micro",
    "skew_seconds",
    "workspace_id",
    "region",
    "creating_authorization_id",
    "idempotency_scope",
    "expires_at",
    "next_attempt_at",
    "attempts",
    "last_error",
    "dead",
    "close_eligible_since",
    "global_closed_at",
    "local_closed_at",
    "recovering_at",
    "created_at",
)


class SpendLeaseDataError(ValueError):
    """A persisted spend-lease value violates the unit-2 storage contract."""


class SpendLeaseDmlError(RuntimeError):
    """A primary-key DML statement returned an impossible modified-row count."""


@dataclass(frozen=True, slots=True)
class Registration:
    """The sole arbitration registration for one idempotency scope."""

    kind: RegistrationKind
    authorization_id: str | None
    lease: BindingTuple | None
    provisional_id: str | None


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Immutable identity persisted before any candidate-local write."""

    lease_id: str
    gen: int
    key_hash: str
    boot_kid: str
    cap_micro: int
    skew_seconds: int
    workspace_id: str
    region: str
    creating_authorization_id: str
    idempotency_scope: str
    expires_at: Any

    def __post_init__(self) -> None:
        required = (
            self.lease_id,
            self.key_hash,
            self.boot_kid,
            self.workspace_id,
            self.region,
            self.creating_authorization_id,
            self.idempotency_scope,
        )
        if not all(required):
            raise SpendLeaseDataError("candidate identity fields must be non-empty")
        if self.gen <= 0 or self.cap_micro <= 0 or self.skew_seconds < 0:
            raise SpendLeaseDataError(
                "candidate gen and cap must be positive and skew must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class OpenRow:
    """Full durable spend-lease work row, including recovery ownership proof."""

    lease_id: str
    phase: OpenPhase
    gen: int
    key_hash: str
    boot_kid: str
    cap_micro: int
    skew_seconds: int
    workspace_id: str
    region: str
    creating_authorization_id: str
    idempotency_scope: str
    expires_at: Any
    next_attempt_at: Any | None
    attempts: int
    last_error: str | None
    dead: bool
    close_eligible_since: Any | None
    global_closed_at: Any | None
    local_closed_at: Any | None
    recovering_at: Any | None
    created_at: Any

    @property
    def identity(self) -> CandidateIdentity:
        return CandidateIdentity(
            lease_id=self.lease_id,
            gen=self.gen,
            key_hash=self.key_hash,
            boot_kid=self.boot_kid,
            cap_micro=self.cap_micro,
            skew_seconds=self.skew_seconds,
            workspace_id=self.workspace_id,
            region=self.region,
            creating_authorization_id=self.creating_authorization_id,
            idempotency_scope=self.idempotency_scope,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True, slots=True)
class LagInputs:
    """Oldest timestamps used to publish both decision-38 lag numbers."""

    close_eligible_since: Any | None
    expired_open_created_at: Any | None
    dead_rows: int


def register_bound(
    transaction: Any,
    param_types: Any,
    scope: str,
    authorization_id: str,
    lease_id: str,
    gen: int,
    allocated_micro: int,
) -> int:
    """Insert the scope's BOUND registration, returning exactly 1 or 0."""
    BindingTuple(lease_id, gen, allocated_micro)
    if not scope or not authorization_id:
        raise SpendLeaseDataError("BOUND scope and authorization id must be non-empty")
    return _single_row_count(
        "register_bound",
        transaction.execute_update(
            "INSERT OR IGNORE INTO spend_lease_scope_arbitration ("
            "scope_salt, idempotency_scope, registration_kind, authorization_id, "
            "spend_lease_id, spend_lease_gen, spend_lease_allocated_micro, "
            "provisional_id, created_at, terminal_at) VALUES ("
            "@scope_salt, @scope, 'BOUND', @authorization_id, @lease_id, @gen, "
            "@allocated_micro, NULL, CURRENT_TIMESTAMP(), NULL)",
            params={
                "scope_salt": spend_leases.spend_lease_scope_salt(scope),
                "scope": scope,
                "authorization_id": authorization_id,
                "lease_id": lease_id,
                "gen": int(gen),
                "allocated_micro": int(allocated_micro),
            },
            param_types={
                "scope_salt": param_types.STRING,
                "scope": param_types.STRING,
                "authorization_id": param_types.STRING,
                "lease_id": param_types.STRING,
                "gen": param_types.INT64,
                "allocated_micro": param_types.INT64,
            },
        ),
    )


def register_claim(
    transaction: Any,
    param_types: Any,
    scope: str,
    provisional_id: str,
) -> int:
    """Insert an already-retention-armed CLAIM, returning exactly 1 or 0."""
    if not scope or not provisional_id:
        raise SpendLeaseDataError("CLAIM scope and provisional id must be non-empty")
    return _single_row_count(
        "register_claim",
        transaction.execute_update(
            "INSERT OR IGNORE INTO spend_lease_scope_arbitration ("
            "scope_salt, idempotency_scope, registration_kind, authorization_id, "
            "spend_lease_id, spend_lease_gen, spend_lease_allocated_micro, "
            "provisional_id, created_at, terminal_at) VALUES ("
            "@scope_salt, @scope, 'CLAIM', NULL, NULL, NULL, NULL, "
            "@provisional_id, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())",
            params={
                "scope_salt": spend_leases.spend_lease_scope_salt(scope),
                "scope": scope,
                "provisional_id": provisional_id,
            },
            param_types={
                "scope_salt": param_types.STRING,
                "scope": param_types.STRING,
                "provisional_id": param_types.STRING,
            },
        ),
    )


def read_registration(
    transaction: Any,
    param_types: Any,
    scope: str,
) -> Registration | None:
    """Point-read the registration selected by the salted primary key."""
    rows = list(
        transaction.execute_sql(
            f"SELECT {', '.join(ARBITRATION_COLUMNS)} "  # noqa: S608 - fixed columns
            "FROM spend_lease_scope_arbitration "
            "WHERE scope_salt=@scope_salt AND idempotency_scope=@scope",
            params={
                "scope_salt": spend_leases.spend_lease_scope_salt(scope),
                "scope": scope,
            },
            param_types={
                "scope_salt": param_types.STRING,
                "scope": param_types.STRING,
            },
        )
    )
    if not rows:
        return None
    kind, authorization_id, lease_id, gen, allocated_micro, provisional_id = rows[0]
    if kind == "BOUND":
        if authorization_id is None or lease_id is None or gen is None or allocated_micro is None:
            raise SpendLeaseDataError("corrupt BOUND arbitration registration")
        return Registration(
            kind="BOUND",
            authorization_id=str(authorization_id),
            lease=BindingTuple(str(lease_id), int(gen), int(allocated_micro)),
            provisional_id=None,
        )
    if kind == "CLAIM":
        if provisional_id is None:
            raise SpendLeaseDataError("corrupt CLAIM arbitration registration")
        return Registration(
            kind="CLAIM",
            authorization_id=None,
            lease=None,
            provisional_id=str(provisional_id),
        )
    raise SpendLeaseDataError(f"unknown arbitration registration kind: {kind!r}")


def delete_bound(
    transaction: Any,
    param_types: Any,
    scope: str,
    authorization_id: str,
) -> int:
    """Delete only this attempt's BOUND registration during an exact inverse."""

    return _single_row_count(
        "delete_bound",
        transaction.execute_update(
            "DELETE FROM spend_lease_scope_arbitration "
            "WHERE scope_salt=@scope_salt AND idempotency_scope=@scope "
            "AND registration_kind='BOUND' AND authorization_id=@authorization_id",
            params={
                "scope_salt": spend_leases.spend_lease_scope_salt(scope),
                "scope": scope,
                "authorization_id": authorization_id,
            },
            param_types={
                "scope_salt": param_types.STRING,
                "scope": param_types.STRING,
                "authorization_id": param_types.STRING,
            },
        ),
    )


def arm_bound_retention(
    transaction: Any,
    param_types: Any,
    authorization_id: str,
    terminal_at: Any,
) -> int:
    """Set BOUND retention through the authorization index; return row count."""
    return int(
        transaction.execute_update(
            "UPDATE spend_lease_scope_arbitration"
            "@{FORCE_INDEX=spend_lease_scope_arbitration_by_authorization} "
            "SET terminal_at=@terminal_at WHERE authorization_id=@authorization_id "
            "AND registration_kind='BOUND'",
            params={
                "authorization_id": authorization_id,
                "terminal_at": terminal_at,
            },
            param_types={
                "authorization_id": param_types.STRING,
                "terminal_at": param_types.TIMESTAMP,
            },
        )
    )


def insert_candidate(
    transaction: Any,
    param_types: Any,
    identity: CandidateIdentity,
    *,
    created_at: Any,
) -> int:
    """Step 1: insert the durable candidate before any local ledger write."""
    return _single_row_count(
        "insert_candidate",
        transaction.execute_update(
            "INSERT OR IGNORE INTO spend_lease_open ("
            "lease_id, phase, gen, key_hash, boot_kid, cap_micro, skew_seconds, "
            "workspace_id, region, creating_authorization_id, idempotency_scope, "
            "expires_at, next_attempt_at, attempts, last_error, dead, "
            "close_eligible_since, global_closed_at, local_closed_at, recovering_at, "
            "created_at) VALUES (@lease_id, 'candidate', @gen, @key_hash, @boot_kid, "
            "@cap_micro, @skew_seconds, @workspace_id, @region, @creator, @scope, "
            "@expires_at, TIMESTAMP_ADD(@expires_at, INTERVAL @skew_seconds SECOND), "
            "0, NULL, false, NULL, NULL, NULL, NULL, @created_at)",
            params={
                "lease_id": identity.lease_id,
                "gen": int(identity.gen),
                "key_hash": identity.key_hash,
                "boot_kid": identity.boot_kid,
                "cap_micro": int(identity.cap_micro),
                "skew_seconds": int(identity.skew_seconds),
                "workspace_id": identity.workspace_id,
                "region": identity.region,
                "creator": identity.creating_authorization_id,
                "scope": identity.idempotency_scope,
                "expires_at": identity.expires_at,
                "created_at": created_at,
            },
            param_types={
                "lease_id": param_types.STRING,
                "gen": param_types.INT64,
                "key_hash": param_types.STRING,
                "boot_kid": param_types.STRING,
                "cap_micro": param_types.INT64,
                "skew_seconds": param_types.INT64,
                "workspace_id": param_types.STRING,
                "region": param_types.STRING,
                "creator": param_types.STRING,
                "scope": param_types.STRING,
                "expires_at": param_types.TIMESTAMP,
                "created_at": param_types.TIMESTAMP,
            },
        ),
    )


def upgrade_candidate_to_open(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    creating_authorization_id: str,
    expires_at: Any,
    skew_seconds: int,
) -> int:
    """Step 3: guarded candidate-to-open handoff; zero means MINT_LOST."""
    return _single_row_count(
        "upgrade_candidate_to_open",
        transaction.execute_update(
            "UPDATE spend_lease_open SET phase='open', "
            "next_attempt_at=TIMESTAMP_ADD(@expires_at, INTERVAL @skew_seconds SECOND) "
            "WHERE lease_id=@lease_id AND phase='candidate' "
            "AND creating_authorization_id=@creator",
            params={
                "lease_id": lease_id,
                "creator": creating_authorization_id,
                "expires_at": expires_at,
                "skew_seconds": int(skew_seconds),
            },
            param_types={
                "lease_id": param_types.STRING,
                "creator": param_types.STRING,
                "expires_at": param_types.TIMESTAMP,
                "skew_seconds": param_types.INT64,
            },
        ),
    )


def take_recovery_ownership(
    transaction: Any,
    param_types: Any,
    lease_id: str,
) -> int:
    """Step 4a: fence mint by moving candidate to recovering.

    The caller must end the transaction immediately after this statement and
    its row-count check.  ``recovering_at`` uses PENDING_COMMIT_TIMESTAMP(), and
    Spanner forbids later access to ``spend_lease_open`` in this transaction.
    """
    return _single_row_count(
        "take_recovery_ownership",
        transaction.execute_update(
            "UPDATE spend_lease_open SET phase='recovering', "
            "recovering_at=PENDING_COMMIT_TIMESTAMP() "
            "WHERE lease_id=@lease_id AND phase='candidate'",
            params={"lease_id": lease_id},
            param_types={"lease_id": param_types.STRING},
        ),
    )


def complete_candidate(
    transaction: Any,
    param_types: Any,
    lease_id: str,
) -> int:
    """Step 4c: finish owned recovery and remove it from the due index."""
    return _single_row_count(
        "complete_candidate",
        transaction.execute_update(
            "UPDATE spend_lease_open SET phase='done', next_attempt_at=NULL "
            "WHERE lease_id=@lease_id AND phase='recovering'",
            params={"lease_id": lease_id},
            param_types={"lease_id": param_types.STRING},
        ),
    )


def read_open_row(
    reader: Any,
    param_types: Any,
    lease_id: str,
) -> OpenRow | None:
    """Strong point read when called with a transaction or strong snapshot."""
    rows = list(
        reader.execute_sql(
            f"SELECT {', '.join(OPEN_COLUMNS)} FROM spend_lease_open "  # noqa: S608
            "WHERE lease_id=@lease_id",
            params={"lease_id": lease_id},
            param_types={"lease_id": param_types.STRING},
        )
    )
    return _open_row(rows[0]) if rows else None


def due_candidates(
    snapshot: Any,
    param_types: Any,
    limit: int,
) -> list[OpenRow]:
    """Ordered candidate/recovering recovery work from the NULL-filtered index."""
    return _due_rows(snapshot, param_types, limit, phases=("candidate", "recovering"))


def due_open(snapshot: Any, param_types: Any, limit: int) -> list[OpenRow]:
    """Ordered open-lease work from the NULL-filtered due index."""
    return _due_rows(snapshot, param_types, limit, phases=("open",))


def defer_open_row(
    transaction: Any,
    param_types: Any,
    row: OpenRow,
    *,
    next_attempt_at: Any,
    error: str | None,
    increment_attempts: bool,
    dead: bool = False,
) -> int:
    """Guarded per-row retry update; closeability polling does not count."""

    attempts = row.attempts + int(increment_attempts)
    return _single_row_count(
        "defer_open_row",
        transaction.execute_update(
            "UPDATE spend_lease_open SET attempts=@attempts, last_error=@error, "
            "next_attempt_at=@next_attempt_at, dead=@dead "
            "WHERE lease_id=@lease_id AND phase=@phase AND attempts=@expected_attempts",
            params={
                "attempts": attempts,
                "error": None if error is None else error[:1000],
                "next_attempt_at": None if dead else next_attempt_at,
                "dead": dead,
                "lease_id": row.lease_id,
                "phase": row.phase,
                "expected_attempts": row.attempts,
            },
            param_types={
                "attempts": param_types.INT64,
                "error": param_types.STRING,
                "next_attempt_at": param_types.TIMESTAMP,
                "dead": param_types.BOOL,
                "lease_id": param_types.STRING,
                "phase": param_types.STRING,
                "expected_attempts": param_types.INT64,
            },
        ),
    )


def set_close_eligible_once(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    observed_at: Any,
) -> int:
    """Set the monotonic close proof timestamp without ever clearing it."""

    return _single_row_count(
        "set_close_eligible_once",
        transaction.execute_update(
            "UPDATE spend_lease_open SET close_eligible_since=@observed_at "
            "WHERE lease_id=@lease_id AND phase='open' AND close_eligible_since IS NULL",
            params={"lease_id": lease_id, "observed_at": observed_at},
            param_types={
                "lease_id": param_types.STRING,
                "observed_at": param_types.TIMESTAMP,
            },
        ),
    )


def mark_global_closed(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    closed_at: Any,
) -> int:
    return _single_row_count(
        "mark_global_closed",
        transaction.execute_update(
            "UPDATE spend_lease_open SET global_closed_at=@closed_at, "
            "next_attempt_at=@closed_at WHERE lease_id=@lease_id AND phase='open' "
            "AND global_closed_at IS NULL",
            params={"lease_id": lease_id, "closed_at": closed_at},
            param_types={
                "lease_id": param_types.STRING,
                "closed_at": param_types.TIMESTAMP,
            },
        ),
    )


def mark_local_closed(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    closed_at: Any,
) -> int:
    return _single_row_count(
        "mark_local_closed",
        transaction.execute_update(
            "UPDATE spend_lease_open SET local_closed_at=@closed_at, "
            "next_attempt_at=TIMESTAMP_ADD(@closed_at, INTERVAL 30 DAY) "
            "WHERE lease_id=@lease_id AND phase='open' "
            "AND global_closed_at IS NOT NULL AND local_closed_at IS NULL",
            params={"lease_id": lease_id, "closed_at": closed_at},
            param_types={
                "lease_id": param_types.STRING,
                "closed_at": param_types.TIMESTAMP,
            },
        ),
    )


def delete_open_row(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    *,
    phase: OpenPhase,
) -> int:
    return _single_row_count(
        "delete_open_row",
        transaction.execute_update(
            "DELETE FROM spend_lease_open WHERE lease_id=@lease_id AND phase=@phase",
            params={"lease_id": lease_id, "phase": phase},
            param_types={"lease_id": param_types.STRING, "phase": param_types.STRING},
        ),
    )


def requeue_dead(
    transaction: Any,
    param_types: Any,
    lease_id: str,
    next_attempt_at: Any,
) -> int:
    return _single_row_count(
        "requeue_dead",
        transaction.execute_update(
            "UPDATE spend_lease_open SET attempts=0, last_error=NULL, dead=false, "
            "next_attempt_at=@next_attempt_at "
            "WHERE lease_id=@lease_id AND phase='open' AND dead=true",
            params={"lease_id": lease_id, "next_attempt_at": next_attempt_at},
            param_types={
                "lease_id": param_types.STRING,
                "next_attempt_at": param_types.TIMESTAMP,
            },
        ),
    )


def dead_rows(snapshot: Any, param_types: Any, limit: int) -> list[OpenRow]:
    rows = list(
        snapshot.execute_sql(
            f"SELECT {', '.join(OPEN_COLUMNS)} FROM spend_lease_open "  # noqa: S608
            "WHERE phase='open' AND dead=true ORDER BY created_at LIMIT @limit",
            params={"limit": int(limit)},
            param_types={"limit": param_types.INT64},
        )
    )
    return [_open_row(row) for row in rows]


def retained_done_candidates(
    snapshot: Any,
    param_types: Any,
    cutoff: Any,
    limit: int,
) -> list[OpenRow]:
    rows = list(
        snapshot.execute_sql(
            f"SELECT {', '.join(OPEN_COLUMNS)} FROM spend_lease_open "  # noqa: S608
            "WHERE phase='done' AND created_at<=@cutoff "
            "ORDER BY created_at LIMIT @limit",
            params={"cutoff": cutoff, "limit": int(limit)},
            param_types={
                "cutoff": param_types.TIMESTAMP,
                "limit": param_types.INT64,
            },
        )
    )
    return [_open_row(row) for row in rows]


def lag_inputs(snapshot: Any, param_types: Any, now: Any) -> LagInputs:
    rows = list(
        snapshot.execute_sql(
            "SELECT MIN(close_eligible_since), "
            "MIN(IF(TIMESTAMP_ADD(expires_at, INTERVAL skew_seconds SECOND)<=@now, "
            "created_at, NULL)), "
            "(SELECT COUNTIF(dead) FROM spend_lease_open) "
            "FROM spend_lease_open WHERE phase='open' AND local_closed_at IS NULL",
            params={"now": now},
            param_types={"now": param_types.TIMESTAMP},
        )
    )
    if not rows:
        return LagInputs(None, None, 0)
    return LagInputs(rows[0][0], rows[0][1], int(rows[0][2] or 0))


def authorization_typed_columns(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map JSON payload lease/finalization facts to the nine typed columns.

    ``spend_lease_exp`` is an epoch second in the JSON payload and a TIMESTAMP
    in Spanner.  ``spend_lease_allocated_micro`` is deliberately distinct from
    the payload's lease-wide ``spend_lease_cap_micro``; callers must supply the
    former explicitly once a request allocation is bound.
    """
    values = {column: payload.get(column) for column in AUTHORIZATION_TYPED_COLUMNS}
    allocated = values["spend_lease_allocated_micro"]
    if allocated is not None and int(allocated) <= 0:
        raise SpendLeaseDataError("spend_lease_allocated_micro must be NULL or positive")
    expires = values["spend_lease_exp"]
    if expires is not None:
        values["spend_lease_exp"] = _timestamp_from_payload(expires)
    return values


def merge_authorization_typed_columns(
    payload: Mapping[str, Any] | None,
    typed_columns: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge mixed-revision authorization facts, one field at a time.

    A non-NULL typed value wins; a typed NULL falls back to the JSON payload.
    This preserves rows written payload-only by an older rolling revision.
    """
    merged = dict(payload or {})
    for column in AUTHORIZATION_TYPED_COLUMNS:
        value = typed_columns.get(column)
        if value is None:
            continue
        merged[column] = _payload_expiry(value) if column == "spend_lease_exp" else value
    allocated = merged.get("spend_lease_allocated_micro")
    if allocated is not None and int(allocated) <= 0:
        raise SpendLeaseDataError("spend_lease_allocated_micro must be NULL or positive")
    derive_spend_lease_settlement(payload, typed_columns, merged)
    return merged


def authorization_typed_param_types(param_types: Any) -> dict[str, Any]:
    """Return the Spanner type map paired with :func:`authorization_typed_columns`."""
    return {
        "spend_lease_id": param_types.STRING,
        "spend_lease_gen": param_types.INT64,
        "spend_lease_allocated_micro": param_types.INT64,
        "spend_lease_token": param_types.STRING,
        "spend_lease_status": param_types.STRING,
        "spend_lease_exp": param_types.TIMESTAMP,
        "idempotency_fingerprint": param_types.STRING,
        "finalization_outcome": param_types.STRING,
        "finalized_cost_microdollars": param_types.INT64,
    }


def _due_rows(
    snapshot: Any,
    param_types: Any,
    limit: int,
    *,
    phases: tuple[OpenPhase, ...],
) -> list[OpenRow]:
    if limit <= 0:
        return []
    phase_sql = ", ".join(f"'{phase}'" for phase in phases)
    rows = list(
        snapshot.execute_sql(
            f"SELECT {', '.join(OPEN_COLUMNS)} FROM spend_lease_open"  # noqa: S608
            "@{FORCE_INDEX=spend_lease_open_due} "
            "WHERE next_attempt_at IS NOT NULL "
            f"AND phase IN ({phase_sql}) "  # noqa: S608 - enum literals only
            "AND next_attempt_at <= CURRENT_TIMESTAMP() "
            "ORDER BY next_attempt_at LIMIT @limit",
            params={"limit": int(limit)},
            param_types={"limit": param_types.INT64},
        )
    )
    return [_open_row(row) for row in rows]


def _open_row(values: Any) -> OpenRow:
    row = dict(zip(OPEN_COLUMNS, values, strict=True))
    phase = row["phase"]
    if phase not in ("candidate", "recovering", "open", "done"):
        raise SpendLeaseDataError(f"unknown spend-lease open phase: {phase!r}")
    return OpenRow(
        lease_id=str(row["lease_id"]),
        phase=phase,
        gen=int(row["gen"]),
        key_hash=str(row["key_hash"]),
        boot_kid=str(row["boot_kid"]),
        cap_micro=int(row["cap_micro"]),
        skew_seconds=int(row["skew_seconds"]),
        workspace_id=str(row["workspace_id"]),
        region=str(row["region"]),
        creating_authorization_id=str(row["creating_authorization_id"]),
        idempotency_scope=str(row["idempotency_scope"]),
        expires_at=row["expires_at"],
        next_attempt_at=row["next_attempt_at"],
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        dead=bool(row["dead"]),
        close_eligible_since=row["close_eligible_since"],
        global_closed_at=row["global_closed_at"],
        local_closed_at=row["local_closed_at"],
        recovering_at=row["recovering_at"],
        created_at=row["created_at"],
    )


def _timestamp_from_payload(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool):
        raise SpendLeaseDataError("spend_lease_exp must be an epoch second or timestamp")
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise SpendLeaseDataError(
            "spend_lease_exp must be an epoch second or timestamp"
        ) from exc


def _payload_expiry(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(normalized.astimezone(UTC).timestamp())


def _single_row_count(statement: str, count: Any) -> int:
    modified = int(count)
    if modified not in (0, 1):
        raise SpendLeaseDmlError(
            f"{statement} modified impossible row count: {modified}"
        )
    return modified
