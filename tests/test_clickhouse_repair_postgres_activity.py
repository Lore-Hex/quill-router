"""clickhouse.repair_postgres_activity against the store's real SQL.

The generations are written by the real settle path
(`PostgresStore.finalize_gateway_authorization`) on the SQLite psycopg fake, so
"byte-equal to what settle enqueues" compares against the bytes settle actually
wrote, not against a second call to the same builder.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from clickhouse import repair_postgres_activity as repair_cli
from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.test_clickhouse_verify_postgres_delivery import FakeClickHouse, _row
from trusted_router.storage_codec import json_body
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import activity_payload
from trusted_router.storage_postgres_operational_analytics_outbox import (
    INSERT_EVENT_SQL,
    PostgresOperationalAnalyticsOutbox,
)
from trusted_router.types import UsageType

# Bound at import: the CLI tests replace the module attribute with a factory.
RepairSource = repair_cli.PostgresActivityRepairSource
ROLE_SQL = Path(__file__).resolve().parents[1] / "scripts/deploy/aws_eu_tr_verifier_role.sql"
START = repair_cli._timestamp("2026-08-11T00:00:00Z")
END = repair_cli._timestamp("2026-08-23T00:00:00Z")
LATER = repair_cli._timestamp("2030-01-01T00:00:00Z")
WS = "ws-repair-1"
KEY = "kh-repair-1"
OUTBOX_ROWS_SQL = (
    "SELECT shard, event_kind, event_id, payload FROM tr_operational_analytics_outbox "
    "ORDER BY event_id"
)


def _view_statements() -> list[str]:
    """The reviewed view from the verifier role SQL, minus psql and IAM steps."""
    text = ROLE_SQL.read_text()
    view = text[text.index("DROP VIEW") : text.index("SELECT 'CREATE ROLE")]
    return [statement for statement in view.split(";") if statement.strip()]


def _generation(generation_id: str, **changes: Any) -> Generation:
    values: dict[str, Any] = {
        "id": generation_id,
        "request_id": f"req-{generation_id}",
        "workspace_id": WS,
        "key_hash": KEY,
        "model": "test/model",
        "provider": "test",
        "provider_name": "Test",
        "app": "Test app",
        "app_id": "app-1",
        "app_markup_microdollars": 2,
        "custom_model_markup_microdollars": 3,
        "tokens_prompt": 12,
        "tokens_completion": 3,
        "total_cost_microdollars": 9,
        "usage_type": UsageType.CREDITS,
        "speed_tokens_per_second": 7.25,
        "finish_reason": "stop",
        "status": "success",
        "streamed": True,
        "usage_estimated": False,
        "cached_input_tokens": 2,
        "reasoning_tokens": 1,
        "tags": {"team": "a"},
        "app_categories": ["coding"],
        "created_at": "2026-08-12T10:00:00.123Z",
    }
    values.update(changes)
    return Generation(**values)


class Harness:
    def __init__(self) -> None:
        self.conn = sqlite_postgres_conn()
        for statement in _view_statements():
            self.conn.execute(statement)
        self.store = postgres_store_on(self.conn)
        self.store._operational_analytics_outbox = PostgresOperationalAnalyticsOutbox(
            self.store._run_transaction
        )
        self.conn.execute(
            "INSERT INTO tr_credit_balance"
            " (workspace_id, shard, total_credits, total_usage, reserved, updated_at)"
            " VALUES (%s, 0, %s, 0, 0, CURRENT_TIMESTAMP)",
            (WS, 1_000_000),
        )
        self.closed = 0
        self.source = self.new_source()

    def new_source(self, **_kwargs: Any) -> RepairSource:
        """A source on the shared SQLite connection; `close` is counted, not run."""
        source = RepairSource(dsn="host=unused")
        source._connection = self.conn

        def close() -> None:
            self.closed += 1

        source.close = close  # type: ignore[method-assign]
        return source

    def settle(self, generation: Generation) -> None:
        reservation = self.store.reserve(WS, KEY, 100)
        authorization = self.store.create_gateway_authorization(
            workspace_id=WS,
            key_hash=KEY,
            model_id=generation.model,
            provider="test",
            usage_type="Credits",
            estimated_microdollars=100,
            credit_reservation_id=reservation.id,
            key_reserved_microdollars=0,
        )
        assert self.store.finalize_gateway_authorization(
            authorization.id,
            success=True,
            actual_microdollars=generation.total_cost_microdollars,
            selected_usage_type="Credits",
            generation=generation,
        )

    def outbox(self) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.conn.execute(OUTBOX_ROWS_SQL).fetchall()]

    def lose_outbox(self) -> None:
        """The August outage: rows left the outbox without reaching ClickHouse."""
        self.conn.execute("DELETE FROM tr_operational_analytics_outbox")

    def snapshot(self) -> tuple[Any, ...]:
        """Every non-outbox table the repair must leave alone."""
        tables = ("tr_entities", "tr_credit_balance", "tr_credit_movement", "tr_key_limit")
        return tuple(
            sorted(map(tuple, self.conn.execute(f"SELECT * FROM {table}").fetchall()))  # noqa: S608
            for table in tables
        )

    def repair(self, stored_ids: list[str], **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("limit", 100)
        if "generation_ids" not in kwargs:
            kwargs.setdefault("start", START)
            kwargs.setdefault("end", END)
        return repair_cli.repair(
            self.source,
            FakeClickHouse([_row(gid) for gid in stored_ids]).client,
            grace_seconds=kwargs.pop("grace_seconds", 0),
            now=kwargs.pop("now", LATER),
            **kwargs,
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


def _settled(harness: Harness, ids: list[str]) -> list[tuple[Any, ...]]:
    for gid in ids:
        harness.settle(_generation(gid))
    rows = harness.outbox()
    assert [row[2] for row in rows] == sorted(ids)
    harness.lose_outbox()
    return rows


def test_dry_run_writes_nothing(harness: Harness) -> None:
    _settled(harness, ["gen-a", "gen-b", "gen-c"])
    before = harness.snapshot()
    harness.conn.statements.clear()

    result = harness.repair(["gen-b"])

    assert result["mode"] == "dry_run"
    assert result["missing"] == result["selected"] == 2
    assert result["selected_ids"] == ["gen-a", "gen-c"]
    assert result["enqueued"] == 0 and result["next_after_id"] is None
    assert result["ok"] is True and result["complete"] is True
    assert harness.outbox() == []
    assert harness.snapshot() == before
    assert all(sql.lstrip().startswith("SELECT") for sql, _ in harness.conn.statements)


def test_apply_enqueues_exactly_the_missing_ids_byte_equal_to_settle(harness: Harness) -> None:
    settled = _settled(harness, ["gen-a", "gen-b", "gen-c"])
    before = harness.snapshot()
    harness.conn.statements.clear()

    result = harness.repair(["gen-b"], apply=True)

    assert result["mode"] == "apply" and result["enqueued"] == 2 and result["ok"]
    assert harness.outbox() == [row for row in settled if row[2] != "gen-b"]
    # Only reads and the settle path's own outbox INSERT; no delete, and no
    # entity, balance, movement or key-limit write.
    writes = [sql for sql, _ in harness.conn.statements if not sql.lstrip().startswith("SELECT")]
    assert writes == [INSERT_EVENT_SQL, INSERT_EVENT_SQL]
    assert harness.snapshot() == before


def test_a_second_apply_is_idempotent(harness: Harness) -> None:
    settled = _settled(harness, ["gen-a", "gen-b"])

    first = harness.repair([], apply=True)
    second = harness.repair([], apply=True)

    assert first["enqueued"] == second["enqueued"] == 2
    assert harness.outbox() == settled
    # And once the drain has delivered them, nothing is selected at all.
    third = harness.repair(["gen-a", "gen-b"], apply=True)
    assert third["missing"] == third["enqueued"] == 0
    assert harness.outbox() == settled


def test_limit_bounds_each_run_and_after_id_resumes(harness: Harness) -> None:
    ids = [f"gen-{index}" for index in range(5)]
    settled = _settled(harness, ids)

    first = harness.repair([], apply=True, limit=2)
    assert first["selected_ids"] == ids[:2]
    assert first["missing"] == 5 and first["next_after_id"] == "gen-1"
    assert first["resume"] == "rerun with --after-id gen-1"
    assert [row[2] for row in harness.outbox()] == ids[:2]

    second = harness.repair([], apply=True, limit=2, after_id=first["next_after_id"])
    assert second["selected_ids"] == ids[2:4] and second["next_after_id"] == "gen-3"

    third = harness.repair([], apply=True, limit=2, after_id=second["next_after_id"])
    assert third["selected_ids"] == ids[4:]
    assert third["next_after_id"] is None and third["resume"] is None
    assert harness.outbox() == settled


def test_window_and_grace_match_the_verifier(harness: Harness) -> None:
    harness.settle(_generation("gen-in"))
    harness.settle(_generation("gen-before", created_at="2026-08-10T23:59:59Z"))
    harness.settle(_generation("gen-end", created_at="2026-08-23T00:00:00Z"))
    harness.lose_outbox()

    assert harness.repair([])["selected_ids"] == ["gen-in"]
    # A row written just now is still inside the drain's grace: pending, not missing.
    pending = harness.repair([], now=None, grace_seconds=3600)
    assert pending["missing"] == 0 and pending["pending"] == 1


def test_an_entity_from_before_the_app_and_markup_fields(harness: Harness) -> None:
    """August entities predate app_id (08-26), markup (08-27, 09-02) and the
    client_* fields (08-17). Their absent keys take the dataclass defaults, as
    on every other read of the entity, so the payload is the one settle builds
    for that stored Generation."""
    new_fields = {
        field.name
        for field in dataclasses.fields(Generation)
        if field.name in {"app_id", "app_markup_microdollars", "custom_model_markup_microdollars"}
        or field.name.startswith("client_")
        or field.name in {"gateway_request_id", "synthetic"}
    }
    body = {
        key: value
        for key, value in json.loads(json_body(_generation("gen-old"))).items()
        if key not in new_fields
    }
    harness.conn.execute(
        "INSERT INTO tr_entities (kind, id, body, updated_at) "
        "VALUES ('generation', %s, %s, CURRENT_TIMESTAMP)",
        ("gen-old", json.dumps(body)),
    )

    harness.repair([], apply=True)

    ((_, _, _, payload),) = harness.outbox()
    stored = harness.store.get_generation("gen-old")
    assert stored is not None
    assert payload == json_body(activity_payload(stored))
    decoded = json.loads(payload)
    assert decoded["app_id"] is None
    assert decoded["app_markup_microdollars"] == decoded["custom_model_markup_microdollars"] == 0
    assert decoded["synthetic"] is False and decoded["client_source"] is None


def test_explicit_generation_ids(harness: Harness) -> None:
    settled = _settled(harness, ["gen-a", "gen-b"])
    harness.settle(_generation("gen-outside", created_at="2026-10-01T00:00:00Z"))
    outside = harness.outbox()
    harness.lose_outbox()

    result = harness.repair(
        ["gen-b"],
        generation_ids=["gen-a", "gen-b", "gen-outside", "gen-unknown"],
        apply=True,
    )

    assert result["selected_ids"] == ["gen-a", "gen-outside"]
    assert result["not_found_ids"] == ["gen-unknown"] and result["ok"] is False
    assert harness.outbox() == sorted([settled[0], *outside], key=lambda row: row[2])


def test_cli_requires_a_window_or_ids_and_rejects_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CH_PASSWORD", "fixture-value")
    for argv in (
        ["--dsn", "host=x"],
        ["--dsn", "host=x", "--start", "2026-08-11T00:00:00Z"],
        ["--dsn", "host=x", "--generation-id", "gen-a", "--start", "2026-08-11T00:00:00Z"],
        ["--dsn", "host=x", "--generation-id", "bad id"],
        ["--dsn", "host=x", "--generation-id", "gen-a", "--limit", "0"],
    ):
        with pytest.raises(SystemExit) as exc:
            repair_cli.main(argv)
        assert exc.value.code == 2


def test_cli_dry_run_by_default_and_apply_on_request(
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _settled(harness, ["gen-a"])
    monkeypatch.setenv("CH_PASSWORD", "fixture-value")
    monkeypatch.setattr(repair_cli, "PostgresActivityRepairSource", harness.new_source)
    monkeypatch.setattr(
        repair_cli, "clickhouse_from_env", lambda password: FakeClickHouse([]).client
    )
    argv = [
        "--dsn",
        "host=x",
        "--start",
        "2026-08-11T00:00:00Z",
        "--end",
        "2026-08-23T00:00:00Z",
        "--grace-seconds",
        "0",
    ]

    assert repair_cli.main(argv) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry_run" and dry_run["selected_ids"] == ["gen-a"]
    assert harness.outbox() == []

    assert repair_cli.main([*argv, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 1
    assert [row[2] for row in harness.outbox()] == ["gen-a"]
    assert harness.closed == 2


def test_cli_failure_never_prints_driver_details(
    monkeypatch: pytest.MonkeyPatch,
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sensitive-driver-detail")

    monkeypatch.setenv("CH_PASSWORD", "fixture-value")
    monkeypatch.setattr(harness.source, "fetch", fail)
    monkeypatch.setattr(repair_cli, "PostgresActivityRepairSource", lambda **kwargs: harness.source)
    monkeypatch.setattr(
        repair_cli, "clickhouse_from_env", lambda password: FakeClickHouse([]).client
    )
    argv = ["--dsn", "host=x", "--start", "2026-08-11T00:00:00Z", "--end", "2026-08-23T00:00:00Z"]

    assert repair_cli.main(argv) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "RuntimeError"}
    assert "sensitive-driver-detail" not in output.out + output.err
    assert harness.closed == 1


def test_imports_in_the_aws_nodes_virtualenv() -> None:
    """The ClickHouse node's venv has no Google libraries and no psycopg_pool,
    so this tool must not reach PostgresStore (storage_postgres) or Spanner."""
    program = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('google', 'psycopg_pool'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import clickhouse.repair_postgres_activity\n"
        "print('repair imported', flush=True)\n"
        "import trusted_router.storage_postgres\n"
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline program
        [sys.executable, "-c", program],
        cwd=root,
        env={"PYTHONPATH": f"{root}:{root / 'src'}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout == "repair imported\n", result.stderr
    # Positive control: the blocker is live for the store this tool avoids.
    assert "ImportError: psycopg_pool" in result.stderr


# Real Postgres, when a throwaway superuser DSN is provided.
_PG_SUPERUSER_DSN = os.environ.get("TR_TEST_POSTGRES_SUPERUSER_DSN", "")


@pytest.fixture
def real_database() -> Any:
    if not _PG_SUPERUSER_DSN:
        pytest.skip("set TR_TEST_POSTGRES_SUPERUSER_DSN to a throwaway Postgres superuser")
    import psycopg
    from psycopg.conninfo import make_conninfo

    name = f"tr_repair_{os.getpid()}_{time.monotonic_ns()}"
    admin = psycopg.connect(_PG_SUPERUSER_DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    connection = psycopg.connect(make_conninfo(_PG_SUPERUSER_DSN, dbname=name), autocommit=True)
    schema = Path(__file__).resolve().parents[1] / "src/trusted_router/storage_postgres_schema.sql"
    try:
        connection.execute(schema.read_text())
        for statement in _view_statements():
            connection.execute(statement)
        yield connection
    finally:
        connection.close()
        admin.execute(f'DROP DATABASE "{name}"')
        admin.close()


def test_apply_on_real_postgres_matches_the_settle_write(real_database: Any) -> None:
    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(real_database))
    generations = [_generation("gen-a"), _generation("gen-b"), _generation("gen-c")]
    with real_database.transaction():
        for generation in generations:
            # The settle transaction's two statements for a generation.
            real_database.execute(
                "INSERT INTO tr_entities (kind, id, body, updated_at) "
                "VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP)",
                ("generation", generation.id, json_body(generation)),
            )
            outbox.enqueue_activity_tx(real_database, generation)
    settled = [tuple(row) for row in real_database.execute(OUTBOX_ROWS_SQL).fetchall()]
    real_database.execute("DELETE FROM tr_operational_analytics_outbox")
    source = RepairSource(dsn="host=unused")
    source._connection = real_database

    for _ in range(2):
        result = repair_cli.repair(
            source,
            FakeClickHouse([_row("gen-b")]).client,
            start=START,
            end=END,
            limit=100,
            apply=True,
            grace_seconds=0,
            now=LATER,
        )
        assert result["selected_ids"] == ["gen-a", "gen-c"] and result["ok"]
        rows = [tuple(row) for row in real_database.execute(OUTBOX_ROWS_SQL).fetchall()]
        assert rows == [row for row in settled if row[2] != "gen-b"]

    explicit = repair_cli.repair(
        source,
        FakeClickHouse([]).client,
        generation_ids=["gen-b", "gen-unknown"],
        limit=100,
        apply=True,
    )
    assert explicit["selected_ids"] == ["gen-b"] and explicit["not_found_ids"] == ["gen-unknown"]
    rows = [tuple(row) for row in real_database.execute(OUTBOX_ROWS_SQL).fetchall()]
    assert rows == settled
