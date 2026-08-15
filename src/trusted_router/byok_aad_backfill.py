"""Resumable BYOK envelope AAD v1-to-v2 backfill primitives.

The migration touches secret-bearing rows, so the storage adapters expose only
the generic entity body and an optimistic compare-and-swap. KMS work happens
outside database transactions; the final write succeeds only when the row is
still byte-for-byte (Spanner) or JSON-equivalent (Postgres) to the version that
was decrypted. A concurrent key rotation therefore wins and is never
overwritten by the backfill.

The non-apply audit mode of `BackfillRunner` is also the measurement half of
the step-4 precondition: `check_no_v1_envelopes` below wraps it with the one
thing an audit cannot tell you about itself — whether it looked at anything.
See `trusted_router.byok_v1_attestations` for the law and the ledger.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from trusted_router.byok_crypto import (
    ALGORITHM,
    ALGORITHM_V2,
    decrypt_byok_secret,
    decrypt_control_secret,
    encrypt_byok_secret,
    encrypt_control_secret,
)
from trusted_router.byok_v1_attestations import (
    MIGRATED_KINDS,
    MIGRATED_SURFACES,
    OUTCOME_CLEAN,
    OUTCOME_DIRTY,
    OUTCOME_EMPTY_WITNESSED,
    OUTCOME_SCAN_DISAGREES,
    OUTCOME_V1_REMAINS,
    OUTCOME_ZERO_SCAN,
    PASSING_OUTCOMES,
    Attestation,
    surface_fingerprint,
    utc_now,
)
from trusted_router.key_management import KeyWrapperSettings
from trusted_router.storage_models import EncryptedSecretEnvelope


@dataclass(frozen=True)
class EntityRow:
    kind: str
    entity_id: str
    body: dict[str, Any]
    original_body: Any


@dataclass(frozen=True)
class EntityCensus:
    """A second, differently shaped question about the same table.

    `migrated_kind_counts` is an aggregate count per migrated kind; the scan is
    a paged cursor walk. They are computed by different SQL and can therefore
    disagree, which is the point: a cursor bug returns no rows while the count
    still sees them. `sampled_kinds` is a bounded peek at the table's contents
    of any kind at all, so that "the audit found nothing" can be distinguished
    from "the audit could not have found anything".
    """

    migrated_kind_counts: dict[str, int]
    sampled_kinds: tuple[str, ...]

    @property
    def reachable(self) -> bool:
        """True when the table answered with real rows of some kind.

        A credentials failure raises rather than reaching here; a wrong table
        name raises; an empty answer leaves this False, which is never a pass.
        """
        return bool(self.sampled_kinds)


class EntityStore(Protocol):
    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]: ...

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool: ...


class CensusStore(Protocol):
    def census(self, *, sample_limit: int = 1000) -> EntityCensus: ...


class AuditableStore(EntityStore, CensusStore, Protocol):
    """A store that can be both walked and counted. Required by the precondition."""


@dataclass
class BackfillStats:
    rows_scanned: int = 0
    envelopes_seen: int = 0
    v1_envelopes: int = 0
    v2_envelopes: int = 0
    missing_envelopes: int = 0
    rows_updated: int = 0
    envelopes_migrated: int = 0
    conflicts: int = 0
    failures: int = 0
    unsupported_algorithms: int = 0
    # Per-kind scan counts. A single total cannot be cross-checked against a
    # per-kind census, and "we scanned 4000 rows" says nothing about whether
    # any of them were the kind that holds envelopes.
    rows_scanned_by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class KmsOperationRateLimiter:
    """Bound the average KMS operation rate without introducing concurrency."""

    def __init__(
        self,
        operations_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if operations_per_second <= 0:
            raise ValueError("KMS operations per second must be positive")
        self._operations_per_second = operations_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_operation_at = 0.0

    def acquire(self, operations: int) -> None:
        if operations <= 0:
            return
        now = self._monotonic()
        start = max(now, self._next_operation_at)
        if start > now:
            self._sleep(start - now)
        self._next_operation_at = start + operations / self._operations_per_second


class BackfillRunner:
    """Audit or migrate every known encrypted field in stable key order."""

    def __init__(
        self,
        store: EntityStore,
        *,
        settings: KeyWrapperSettings | None = None,
        apply: bool = False,
        kms_operations_per_second: float = 5.0,
        reporter: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if apply and settings is None:
            raise ValueError("settings are required when applying the backfill")
        self._store = store
        self._settings = settings
        self._apply = apply
        self._report = reporter
        self._limiter = KmsOperationRateLimiter(
            kms_operations_per_second,
            monotonic=monotonic,
            sleep=sleep,
        )

    def run(
        self,
        *,
        batch_size: int = 100,
        after: tuple[str, str] | None = None,
    ) -> BackfillStats:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch size must be between 1 and 1000")
        stats = BackfillStats()
        cursor = after
        while True:
            rows = self._store.scan(after=cursor, limit=batch_size)
            if not rows:
                return stats
            for row in rows:
                self._process_row(row, stats)
                cursor = (row.kind, row.entity_id)
            last_row = rows[-1]
            self._report(
                "checkpoint "
                f"kind={last_row.kind} row={_row_ref(last_row.kind, last_row.entity_id)} "
                f"rows_scanned={stats.rows_scanned}"
            )

    def _process_row(self, row: EntityRow, stats: BackfillStats) -> None:
        stats.rows_scanned += 1
        stats.rows_scanned_by_kind[row.kind] = stats.rows_scanned_by_kind.get(row.kind, 0) + 1
        new_body = copy.deepcopy(row.body)
        migrations = 0
        row_failed = False
        for field_name, envelope_family in _fields_for_kind(row.kind):
            raw_envelope = row.body.get(field_name)
            if raw_envelope is None:
                stats.missing_envelopes += 1
                continue
            stats.envelopes_seen += 1
            if not isinstance(raw_envelope, dict):
                stats.failures += 1
                row_failed = True
                self._error(row, field_name, "invalid_envelope_shape")
                continue
            algorithm = raw_envelope.get("algorithm")
            if algorithm == ALGORITHM_V2:
                stats.v2_envelopes += 1
                continue
            if algorithm != ALGORITHM:
                stats.unsupported_algorithms += 1
                row_failed = True
                self._error(row, field_name, "unsupported_algorithm")
                continue
            stats.v1_envelopes += 1
            if not self._apply:
                continue
            try:
                # One v1 unwrap, one v2 wrap, and one v2 verification unwrap.
                self._limiter.acquire(3)
                new_body[field_name] = self._migrate_envelope(
                    row,
                    field=field_name,
                    envelope_family=envelope_family,
                    raw_envelope=raw_envelope,
                )
                migrations += 1
            except Exception as exc:  # fail closed; never expose secret material
                stats.failures += 1
                row_failed = True
                self._error(row, field_name, type(exc).__name__)

        if not self._apply or row_failed or migrations == 0:
            return
        if self._store.compare_and_swap(row, new_body):
            stats.rows_updated += 1
            stats.envelopes_migrated += migrations
            return
        stats.conflicts += 1
        self._error(row, "*", "concurrent_change")

    def _migrate_envelope(
        self,
        row: EntityRow,
        *,
        field: str,
        envelope_family: str,
        raw_envelope: dict[str, Any],
    ) -> dict[str, str]:
        assert self._settings is not None
        workspace_id = _required_string(row.body, "workspace_id")
        envelope = EncryptedSecretEnvelope(**raw_envelope)
        if envelope_family == "provider":
            provider = _required_string(row.body, "provider")
            plaintext = decrypt_byok_secret(
                envelope,
                self._settings,
                workspace_id=workspace_id,
                provider=provider,
            )
            migrated = encrypt_byok_secret(
                plaintext,
                self._settings,
                workspace_id=workspace_id,
                provider=provider,
            )
            verified = decrypt_byok_secret(
                migrated,
                self._settings,
                workspace_id=workspace_id,
                provider=provider,
            )
        else:
            purpose = _broadcast_context(row.entity_id, field)
            plaintext = decrypt_control_secret(
                envelope,
                self._settings,
                workspace_id=workspace_id,
                purpose=purpose,
            )
            migrated = encrypt_control_secret(
                plaintext,
                self._settings,
                workspace_id=workspace_id,
                purpose=purpose,
            )
            verified = decrypt_control_secret(
                migrated,
                self._settings,
                workspace_id=workspace_id,
                purpose=purpose,
            )
        if verified != plaintext or migrated.algorithm != ALGORITHM_V2:
            raise ValueError("v2 verification failed")
        return asdict(migrated)

    def _error(self, row: EntityRow, field: str, reason: str) -> None:
        self._report(
            "ERROR "
            f"kind={row.kind} row={_row_ref(row.kind, row.entity_id)} "
            f"field={field} reason={reason}"
        )


class SpannerEntityStore:
    def __init__(self, database: Any, param_types: Any) -> None:
        self._database = database
        self._param_types = param_types

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        after_kind, after_id = after or ("", "")
        sql = (
            "SELECT kind, id, body FROM tr_entities "
            "WHERE kind IN ('broadcast_destination', 'byok') "
            "AND (kind > @after_kind OR (kind = @after_kind AND id > @after_id)) "
            "ORDER BY kind, id LIMIT @limit"
        )
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                sql,
                params={
                    "after_kind": after_kind,
                    "after_id": after_id,
                    "limit": limit,
                },
                param_types={
                    "after_kind": self._param_types.STRING,
                    "after_id": self._param_types.STRING,
                    "limit": self._param_types.INT64,
                },
            )
            return [
                EntityRow(
                    kind=kind,
                    entity_id=entity_id,
                    body=json.loads(body),
                    original_body=body,
                )
                for kind, entity_id, body in rows
            ]

    def census(self, *, sample_limit: int = 1000) -> EntityCensus:
        """Aggregate counts for the migrated kinds, plus a bounded liveness peek.

        Deliberately not `SELECT kind, COUNT(*) ... GROUP BY kind` over the
        whole table: `tr_entities` holds every entity in the deployment and a
        full group-by is an expensive scan on a live database. The peek is a
        `LIMIT` read, so its cost does not grow with the table.
        """
        counts: dict[str, int] = {}
        sampled: set[str] = set()
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT kind, COUNT(*) FROM tr_entities WHERE kind IN UNNEST(@kinds) GROUP BY kind",
                params={"kinds": list(MIGRATED_KINDS)},
                param_types={"kinds": self._param_types.Array(self._param_types.STRING)},
            )
            for kind, count in rows:
                counts[kind] = int(count)
            sample = snapshot.execute_sql(
                "SELECT kind FROM tr_entities LIMIT @limit",
                params={"limit": sample_limit},
                param_types={"limit": self._param_types.INT64},
            )
            for (kind,) in sample:
                sampled.add(kind)
        return EntityCensus(migrated_kind_counts=counts, sampled_kinds=tuple(sorted(sampled)))

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool:
        new_body_json = _json_body(new_body)

        def update(transaction: Any) -> bool:
            changed = transaction.execute_update(
                "UPDATE tr_entities SET body=@new_body, "
                "updated_at=PENDING_COMMIT_TIMESTAMP() "
                "WHERE kind=@kind AND id=@id AND body=@old_body",
                params={
                    "new_body": new_body_json,
                    "kind": row.kind,
                    "id": row.entity_id,
                    "old_body": row.original_body,
                },
                param_types={
                    "new_body": self._param_types.STRING,
                    "kind": self._param_types.STRING,
                    "id": self._param_types.STRING,
                    "old_body": self._param_types.STRING,
                },
            )
            return changed == 1

        return bool(self._database.run_in_transaction(update))


class PostgresEntityStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN is required")
        self._dsn = dsn

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        import psycopg

        after_kind, after_id = after or ("", "")
        with psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT kind, id, body FROM tr_entities "
                "WHERE kind = ANY(%s) "
                "AND (kind > %s OR (kind = %s AND id > %s)) "
                "ORDER BY kind, id LIMIT %s",
                (
                    list(MIGRATED_KINDS),
                    after_kind,
                    after_kind,
                    after_id,
                    limit,
                ),
            ).fetchall()
        result: list[EntityRow] = []
        for kind, entity_id, raw_body in rows:
            body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
            result.append(
                EntityRow(
                    kind=kind,
                    entity_id=entity_id,
                    body=body,
                    original_body=copy.deepcopy(body),
                )
            )
        return result

    def census(self, *, sample_limit: int = 1000) -> EntityCensus:
        """See SpannerEntityStore.census. Same two questions, same reasons."""
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            counted = conn.execute(
                "SELECT kind, COUNT(*) FROM tr_entities WHERE kind = ANY(%s) GROUP BY kind",
                (list(MIGRATED_KINDS),),
            ).fetchall()
            sampled = conn.execute(
                "SELECT DISTINCT kind FROM (SELECT kind FROM tr_entities LIMIT %s) AS peek",
                (sample_limit,),
            ).fetchall()
        return EntityCensus(
            migrated_kind_counts={kind: int(count) for kind, count in counted},
            sampled_kinds=tuple(sorted(kind for (kind,) in sampled)),
        )

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool:
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            changed = conn.execute(
                "UPDATE tr_entities SET body=%s::jsonb, updated_at=CURRENT_TIMESTAMP "
                "WHERE kind=%s AND id=%s AND body=%s::jsonb",
                (
                    _json_body(new_body),
                    row.kind,
                    row.entity_id,
                    _json_body(row.original_body),
                ),
            ).rowcount
        return changed == 1


def _fields_for_kind(kind: str) -> tuple[tuple[str, str], ...]:
    """Derived from MIGRATED_SURFACES so the surface list has exactly one home.

    A new encrypted surface added there is walked by the backfill AND changes
    the attestation fingerprint, which invalidates every zero-v1 attestation
    recorded before the surface existed. Two copies of this list is how open
    question #2 in the migration doc turns into an outage.
    """
    fields = tuple(
        (field_name, family)
        for surface_kind, field_name, family in MIGRATED_SURFACES
        if surface_kind == kind
    )
    if not fields:
        raise ValueError(f"unsupported entity kind: {kind}")
    return fields


def _broadcast_context(destination_id: str, field: str) -> str:
    suffix = {"encrypted_api_key": "api_key", "encrypted_headers": "headers"}[field]
    # Byte-identical to services.broadcast.broadcast_secret_context. Keeping
    # this tiny helper here avoids importing the application-global STORE into
    # an offline secret migration process.
    return f"broadcast:{destination_id}:{suffix}"


def _required_string(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {field}")
    return value


def _row_ref(kind: str, entity_id: str) -> str:
    return hashlib.sha256(f"{kind}\x00{entity_id}".encode()).hexdigest()[:12]


def _json_body(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


# ------------------------------------------------- the step-4 precondition ---


@dataclass(frozen=True)
class PreconditionResult:
    """One cloud's answer to "may v1 be deleted?", with its working shown."""

    cloud: str
    outcome: str
    detail: str
    stats: BackfillStats
    census: EntityCensus

    @property
    def passed(self) -> bool:
        return self.outcome in PASSING_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "cloud": self.cloud,
            "outcome": self.outcome,
            "detail": self.detail,
            "audit": self.stats.as_dict(),
            "census": {
                "migrated_kind_counts": dict(sorted(self.census.migrated_kind_counts.items())),
                "sampled_kinds": list(self.census.sampled_kinds),
            },
        }


def check_no_v1_envelopes(
    store: AuditableStore,
    *,
    cloud: str,
    batch_size: int = 100,
    sample_limit: int = 1000,
    reporter: Callable[[str], None] = print,
) -> PreconditionResult:
    """Audit one cloud's database and say whether it attests zero v1 envelopes.

    The audit alone cannot do this. `BackfillRunner(apply=False)` reports
    `v1_envelopes == 0` just as happily for a migrated database as for a run
    that scanned nothing at all — a bad resume cursor, a renamed kind, a
    read-only credential on the wrong project. Both render as a green check,
    and on AWS and Azure a green check is exactly what a zero-row audit
    produced. So this function pairs the walk with a census computed by
    different SQL and refuses to collapse the two cases.

    The census is taken FIRST, on purpose. Taken last, a BYOK key registered
    while the scan was running would be counted by the census and missed by the
    walk, and would report as a scan disagreement — a false alarm on the most
    ordinary event there is. Taken first, that same registration is scanned but
    not counted, which is harmless. The remaining false-alarm mode is a row
    deleted mid-run; the answer to that one is to re-run.

    There is no resume cursor here, unlike the backfill. Resuming is what makes
    a long mutating job survivable and it is exactly what makes an audit
    unfalsifiable: a precondition that starts halfway through cannot claim to
    have covered the beginning. This walks the whole table or reports nothing.
    """
    census = store.census(sample_limit=sample_limit)
    stats = BackfillRunner(store, apply=False, reporter=reporter).run(batch_size=batch_size)

    undercounted = {
        kind: (counted, stats.rows_scanned_by_kind.get(kind, 0))
        for kind, counted in sorted(census.migrated_kind_counts.items())
        if counted > stats.rows_scanned_by_kind.get(kind, 0)
    }
    if undercounted:
        detail = ", ".join(
            f"{kind}: census counted {counted} rows, the scan returned {scanned}"
            for kind, (counted, scanned) in undercounted.items()
        )
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_SCAN_DISAGREES,
            detail=(
                f"the scan did not see rows the census can count ({detail}). A resume cursor, "
                "filter, or ordering bug — or a row deleted mid-run. Re-run before believing "
                "anything else this reported."
            ),
            stats=stats,
            census=census,
        )
    if stats.failures or stats.unsupported_algorithms:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_DIRTY,
            detail=(
                f"{stats.failures} unreadable and {stats.unsupported_algorithms} "
                "unknown-algorithm envelopes. Something other than the migration is wrong; "
                "a row that cannot be classified cannot be attested as v2."
            ),
            stats=stats,
            census=census,
        )
    if stats.v1_envelopes:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_V1_REMAINS,
            detail=(
                f"{stats.v1_envelopes} v1 envelopes are still stored. Run the backfill "
                "(scripts/backfill_byok_aad_v2.py --apply) before attempting step 4."
            ),
            stats=stats,
            census=census,
        )
    if stats.envelopes_seen == 0 and not census.reachable:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_ZERO_SCAN,
            detail=(
                "the audit saw no envelopes AND the census found no rows of any kind, so "
                "nothing distinguishes a migrated deployment from a broken query, a wrong "
                "database, or a credential that can read nothing. This is not zero v1 "
                "envelopes; it is zero evidence."
            ),
            stats=stats,
            census=census,
        )
    if stats.envelopes_seen == 0:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_EMPTY_WITNESSED,
            detail=(
                f"no envelopes exist on this deployment. The census reached "
                f"{len(census.sampled_kinds)} entity kinds in the same table with the same "
                "credentials and counts zero rows of every migrated kind, so the empty result "
                "is the deployment's state and not the query's failure."
            ),
            stats=stats,
            census=census,
        )
    return PreconditionResult(
        cloud=cloud,
        outcome=OUTCOME_CLEAN,
        detail=(
            f"{stats.envelopes_seen} envelopes examined across {stats.rows_scanned} rows; "
            f"all {stats.v2_envelopes} are v2."
        ),
        stats=stats,
        census=census,
    )


def attestation_for(
    result: PreconditionResult,
    *,
    backend: str,
    operator: str,
    note: str = "",
    recorded_at: str | None = None,
) -> Attestation:
    """Turn a passing precondition run into a ledger entry.

    Refuses non-passing outcomes here rather than only at write time, so that
    no caller can construct a green-looking Attestation from a zero scan and
    then hand it to something less careful than `record_attestation`.
    """
    if not result.passed:
        raise ValueError(
            f"outcome {result.outcome!r} does not attest zero v1 envelopes: {result.detail}"
        )
    return Attestation(
        cloud=result.cloud,
        outcome=result.outcome,
        recorded_at=utc_now() if recorded_at is None else recorded_at,
        backend=backend,
        surface_fingerprint=surface_fingerprint(),
        rows_scanned=result.stats.rows_scanned,
        rows_scanned_by_kind=dict(result.stats.rows_scanned_by_kind),
        envelopes_seen=result.stats.envelopes_seen,
        v1_envelopes=result.stats.v1_envelopes,
        v2_envelopes=result.stats.v2_envelopes,
        census_migrated_kind_counts=dict(result.census.migrated_kind_counts),
        census_sampled_kinds=list(result.census.sampled_kinds),
        operator=operator,
        note=note,
    )
