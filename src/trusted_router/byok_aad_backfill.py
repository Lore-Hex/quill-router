"""Resumable BYOK envelope AAD v1-to-v2 backfill primitives.

The migration touches secret-bearing rows, so the storage adapters expose only
the generic entity body and an optimistic compare-and-swap. KMS work happens
outside database transactions; the final write succeeds only when the row is
still byte-for-byte (Spanner) or JSON-equivalent (Postgres) to the version that
was decrypted. A concurrent key rotation therefore wins and is never
overwritten by the backfill.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from trusted_router.byok_crypto import (
    ALGORITHM,
    ALGORITHM_V2,
    decrypt_byok_secret,
    decrypt_control_secret,
    encrypt_byok_secret,
    encrypt_control_secret,
)
from trusted_router.key_management import KeyWrapperSettings
from trusted_router.storage_models import EncryptedSecretEnvelope

_MIGRATED_KINDS = ("broadcast_destination", "byok")


@dataclass(frozen=True)
class EntityRow:
    kind: str
    entity_id: str
    body: dict[str, Any]
    original_body: Any


class EntityStore(Protocol):
    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]: ...

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool: ...


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

    def as_dict(self) -> dict[str, int]:
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
        new_body = copy.deepcopy(row.body)
        migrations = 0
        row_failed = False
        for field, envelope_family in _fields_for_kind(row.kind):
            raw_envelope = row.body.get(field)
            if raw_envelope is None:
                stats.missing_envelopes += 1
                continue
            stats.envelopes_seen += 1
            if not isinstance(raw_envelope, dict):
                stats.failures += 1
                row_failed = True
                self._error(row, field, "invalid_envelope_shape")
                continue
            algorithm = raw_envelope.get("algorithm")
            if algorithm == ALGORITHM_V2:
                stats.v2_envelopes += 1
                continue
            if algorithm != ALGORITHM:
                stats.unsupported_algorithms += 1
                row_failed = True
                self._error(row, field, "unsupported_algorithm")
                continue
            stats.v1_envelopes += 1
            if not self._apply:
                continue
            try:
                # One v1 unwrap, one v2 wrap, and one v2 verification unwrap.
                self._limiter.acquire(3)
                new_body[field] = self._migrate_envelope(
                    row,
                    field=field,
                    envelope_family=envelope_family,
                    raw_envelope=raw_envelope,
                )
                migrations += 1
            except Exception as exc:  # fail closed; never expose secret material
                stats.failures += 1
                row_failed = True
                self._error(row, field, type(exc).__name__)

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
                "WHERE kind IN (%s, %s) "
                "AND (kind > %s OR (kind = %s AND id > %s)) "
                "ORDER BY kind, id LIMIT %s",
                (
                    _MIGRATED_KINDS[0],
                    _MIGRATED_KINDS[1],
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
    if kind == "byok":
        return (("encrypted_secret", "provider"),)
    if kind == "broadcast_destination":
        return (
            ("encrypted_api_key", "control"),
            ("encrypted_headers", "control"),
        )
    raise ValueError(f"unsupported entity kind: {kind}")


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
