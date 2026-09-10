from __future__ import annotations

import dataclasses
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from trusted_router.receipt_key_backfill_cli import run
from trusted_router.receipt_keys import b64url_encode, receipt_attestation_sha256, receipt_kid
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_models import ReceiptKey


def _legacy(seed: bytes) -> ReceiptKey:
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(hashlib.sha256(seed).digest()),
    }
    return ReceiptKey(
        kid=receipt_kid(jwk),
        jwk=jwk,
        att=f"legacy-attestation-{seed.hex()}",
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen="2026-08-26T00:00:00Z",
        last_seen="2026-08-26T00:01:00Z",
    )


class _Transaction:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.writes = 0

    def execute_sql(self, sql: str, *, params, param_types) -> list[tuple[str, str]]:
        assert "kind=@kind AND id>@after" in sql
        assert "kid IS NULL OR att_sha256 IS NULL" in sql
        del param_types
        ids = sorted(
            entity_id
            for entity_id, row in self.rows.items()
            if entity_id > params["after"]
            and (row["kid"] is None or row["att_sha256"] is None)
        )[: params["limit"]]
        return [(entity_id, self.rows[entity_id]["body"]) for entity_id in ids]

    def insert_or_update(self, *, table: str, columns, values) -> None:
        assert table == "tr_entities"
        values_by_column = dict(zip(columns, values[0], strict=True))
        row = self.rows[values_by_column["id"]]
        row.update(values_by_column)
        self.writes += 1


def _store(transaction: _Transaction) -> SpannerBigtableStore:
    store = SpannerBigtableStore.__new__(SpannerBigtableStore)
    store._param_types = SimpleNamespace(STRING="STRING", INT64="INT64")
    store._spanner = SimpleNamespace(COMMIT_TIMESTAMP="commit-timestamp")
    store._run_in_transaction = lambda operation: operation(transaction)  # type: ignore[method-assign]
    return store


def _row(record: ReceiptKey) -> dict[str, Any]:
    body = dataclasses.asdict(record)
    body.pop("att_sha256")
    return {
        "kind": "receipt_key",
        "id": record.kid,
        "body": json.dumps(body),
        "kid": None,
        "att_sha256": None,
    }


def test_backfill_converts_legacy_projection_once_and_second_run_is_noop() -> None:
    legacy = _legacy(b"backfill-once")
    rows = {legacy.kid: _row(legacy)}
    transaction = _Transaction(rows)
    store = _store(transaction)

    assert run(store, batch_size=1) == 1
    assert rows[legacy.kid]["kid"] == legacy.kid
    assert rows[legacy.kid]["att_sha256"] == receipt_attestation_sha256(
        legacy.att, legacy.att_kind
    )
    assert json.loads(rows[legacy.kid]["body"])["att_sha256"] == rows[legacy.kid][
        "att_sha256"
    ]
    assert transaction.writes == 1

    assert run(store, batch_size=1) == 0
    assert transaction.writes == 1


def test_backfill_resume_cursor_starts_after_completed_entity() -> None:
    records = sorted((_legacy(b"resume-a"), _legacy(b"resume-b")), key=lambda row: row.kid)
    rows = {record.kid: _row(record) for record in records}

    assert run(_store(_Transaction(rows)), batch_size=1, after=records[0].kid) == 1
    assert rows[records[0].kid]["kid"] is None
    assert rows[records[1].kid]["kid"] == records[1].kid
