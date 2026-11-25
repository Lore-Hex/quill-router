from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from trusted_router import receipt_key_backfill_cli
from trusted_router.receipt_key_backfill_cli import BackfillDeadlineExceeded, run
from trusted_router.receipt_keys import b64url_encode, receipt_attestation_sha256, receipt_kid
from trusted_router.storage_gcp import SpannerStore
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

    def execute_sql(
        self, sql: str, *, params, param_types, timeout, request_options
    ) -> list[tuple[str, str]]:
        assert "kind=@kind AND id>@after" in sql
        assert "kid IS NULL OR att_sha256 IS NULL" in sql
        # Every page read is bounded: a read with no deadline held a deploy
        # for hours on 2026-11-24.
        assert 0 < timeout <= 60
        assert request_options == {"priority": "PRIORITY_LOW"}
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


def _store(transaction: _Transaction) -> SpannerStore:
    store = SpannerStore.__new__(SpannerStore)
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


class _PagedStore:
    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    def backfill_receipt_key_versions_page(
        self, *, after: str | None = None, limit: int = 100
    ) -> list[str]:
        del limit
        self.calls.append(after)
        return self.pages.pop(0) if self.pages else []


def test_backfill_stops_between_pages_at_deadline_with_resume_cursor() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    class _SlowStore(_PagedStore):
        def backfill_receipt_key_versions_page(
            self, *, after: str | None = None, limit: int = 100
        ) -> list[str]:
            now[0] += 40.0
            return super().backfill_receipt_key_versions_page(after=after, limit=limit)

    store = _SlowStore([["a", "b"], ["c"], ["d"]])
    with pytest.raises(BackfillDeadlineExceeded) as exc:
        run(store, batch_size=2, deadline_seconds=60, clock=clock)

    assert store.calls == [None, "b"]
    assert exc.value.updated == 3
    assert exc.value.cursor == "c"
    # The cursor resumes the remaining work only.
    assert run(store, batch_size=2, after=exc.value.cursor) == 1
    assert store.calls[-2:] == ["c", "d"]


def test_backfill_refuses_a_page_that_does_not_advance_the_cursor() -> None:
    store = _PagedStore([["b"], ["b"]])
    with pytest.raises(RuntimeError, match="did not advance"):
        run(store, batch_size=1)


def test_backfill_rejects_unbounded_or_oversized_deadlines() -> None:
    for bad in (0, -1, receipt_key_backfill_cli.MAX_DEADLINE_SECONDS + 1):
        with pytest.raises(ValueError, match="deadline"):
            run(_PagedStore([]), deadline_seconds=bad)


def test_hard_deadline_interrupts_a_call_that_never_returns() -> None:
    started = time.monotonic()
    with pytest.raises(BackfillDeadlineExceeded):
        with receipt_key_backfill_cli._hard_deadline(1):
            time.sleep(30)
    assert time.monotonic() - started < 10


def test_main_exits_with_deadline_code_and_restores_alarm(monkeypatch) -> None:
    import signal

    class _HangingStore:
        def backfill_receipt_key_versions_page(
            self, *, after: str | None = None, limit: int = 100
        ) -> list[str]:
            raise BackfillDeadlineExceeded(updated=-1, cursor=None)

    monkeypatch.setattr(receipt_key_backfill_cli, "get_settings", lambda: object())
    monkeypatch.setattr(receipt_key_backfill_cli, "create_store", lambda settings: _HangingStore())
    previous = signal.getsignal(signal.SIGALRM)

    assert receipt_key_backfill_cli.main(["--deadline-seconds", "5"]) == (
        receipt_key_backfill_cli.DEADLINE_EXIT_CODE
    )
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.alarm(0) == 0


def test_main_rejects_out_of_range_deadline() -> None:
    with pytest.raises(SystemExit):
        receipt_key_backfill_cli.main(["--deadline-seconds", "0"])


def test_deploy_backfill_step_is_bounded_well_inside_the_lease() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/deploy-production.yml").read_text())
    steps = workflow["jobs"]["migrate-schema"]["steps"]
    (step,) = [s for s in steps if s.get("name") == "Backfill legacy receipt attestation version projections"]
    run_line = step["run"]
    assert "python -m trusted_router.receipt_key_backfill_cli" in run_line
    assert "--deadline-seconds 600" in run_line
    # The step timeout is a backstop beyond the command's own deadline plus its
    # SIGALRM grace, and far below the 6 h control-plane lease TTL.
    timeout_minutes = step["timeout-minutes"]
    assert isinstance(timeout_minutes, int)
    assert 600 + 30 < timeout_minutes * 60 <= 20 * 60
