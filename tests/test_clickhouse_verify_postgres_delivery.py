from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clickhouse import verify_postgres_delivery as verifier

ROLE_SQL = Path(__file__).resolve().parents[1] / "scripts/deploy/aws_eu_tr_verifier_role.sql"
START = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
END = START + dt.timedelta(days=1)


def _row(gid: str = "gen-1", **changes: Any) -> dict[str, Any]:
    return {
        "generation_id": gid,
        "total_cost_microdollars": 7,
        "tokens_prompt": 10,
        "tokens_completion": 2,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "model": "test/model",
        "provider": "test",
        "status": "success",
        "created_at": "2026-10-18T12:00:00.123Z",
        **changes,
    }


class FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = sorted(rows, key=lambda row: row["generation_id"])
        # Generations the window scan never sees but a point read finds: a
        # settle that landed after the scan passed its id.
        self.late: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def transaction(self) -> Any:
        return contextlib.nullcontext()

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        self.calls.append((sql, params))
        assert "tr_entities" not in sql
        if "id = ANY(%s)" in sql:
            assert sql == "SELECT id FROM tr_verifier_generations WHERE id = ANY(%s)"
            (ids,) = params
            present = {row["generation_id"] for row in self.rows} | set(self.late)
            return SimpleNamespace(fetchall=lambda: [(gid,) for gid in ids if gid in present])
        assert "FROM tr_verifier_generations WHERE id > %s ORDER BY id LIMIT" in sql
        after = params[0]
        limit = params[1] if len(params) == 2 else 1
        selected = [row for row in self.rows if row["generation_id"] > after][:limit]
        if sql.startswith("SELECT id FROM"):
            result = [(row["generation_id"],) for row in selected]
        else:
            result = [
                (
                    row["generation_id"],
                    *(
                        str(row[field]) if row.get(field) is not None else None
                        for field in verifier.FIELDS
                    ),
                    row.get("updated_at", row["created_at"]),
                )
                for row in selected
            ]
        return SimpleNamespace(fetchall=lambda: result)

    def close(self) -> None:
        self.closed = True


def _source(rows: list[dict[str, Any]]) -> tuple[verifier.PostgresGenerationSource, FakeConnection]:
    source = verifier.PostgresGenerationSource(dsn="host=localhost dbname=test")
    connection = FakeConnection(rows)
    source._connection = connection
    return source, connection


class FakeClickHouse:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[httpx.Request] = []
        self.ids: list[list[str]] = []
        self.client = verifier.ClickHouseGenerationSource(
            base_url="http://clickhouse.test:8123",
            user="verifier",
            password="",
            database="default",
            transport=httpx.MockTransport(self.handle),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        sql = request.content.decode()
        assert "activity_generations FINAL" in sql
        assert "max_rows_to_read = 1000000" in sql
        assert "read_overflow_mode = 'throw'" in sql
        assert request.url.params["database"] == "default"
        assert "password" not in request.url.params
        ids = [value for key, value in request.url.params.items() if key.startswith("param_id_")]
        if ids:
            self.ids.append(ids)
            assert "WHERE (tenant_id, created_at, generation_id) IN (" in sql
            assert "WHERE generation_id IN ({id_0:String}" in sql
            rows = [row for row in self.rows if row["generation_id"] in ids]
        else:
            assert "created_at >= parseDateTime64BestEffort({start:String}" in sql
            assert "created_at < parseDateTime64BestEffort({end:String}" in sql
            start = verifier._timestamp(request.url.params["param_start"])
            end = verifier._timestamp(request.url.params["param_end"])
            rows = [
                row for row in self.rows if start <= verifier._timestamp(row["created_at"]) < end
            ]
            rows = rows[: int(request.url.params["param_limit"])]
        return httpx.Response(200, json={"data": rows})


def _verify(
    rows: list[dict[str, Any]],
    stored: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    source, _ = _source(rows)
    return verifier.verify_delivery(
        source,
        FakeClickHouse(stored).client,
        start=START,
        end=END,
        now=END,
        limit=kwargs.pop("limit", 100),
        **kwargs,
    )


def test_matches_shared_fields_without_reporting_private_payloads() -> None:
    row = _row()
    result = _verify([row], [row | {"app": "uncompared", "ingest_version": 123}])

    assert result == {
        "sampled": 1,
        "found": 1,
        "missing": 0,
        "pending": 0,
        "extra": 0,
        "mismatched": 0,
        "duplicated": 0,
        "missing_ids": [],
        "pending_ids": [],
        "extra_ids": [],
        "mismatched_ids": [],
        "duplicated_ids": [],
        "mismatch_fields": {},
        "settled_after_scan": 0,
        "scanned": 1,
        "complete": True,
        "extra_checked": True,
        "ok": True,
    }
    assert "test/model" not in json.dumps(result)


@pytest.mark.parametrize("field", verifier.FIELDS)
def test_reports_each_shared_field_mismatch(field: str) -> None:
    row = _row()
    wrong = (
        "2026-10-18T12:00:01.124Z"
        if field == "created_at"
        else 999
        if isinstance(row[field], int)
        else "different"
    )
    result = _verify([row], [row | {field: wrong}])

    assert result["ok"] is False
    assert result["mismatched_ids"] == ["gen-1"]
    assert result["mismatch_fields"] == {field: 1}


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-18 12:00:00.123",
        "2026-10-18T05:00:00.123-07:00",
        "2026-10-18T12:00:01.123Z",
        "2026-10-18T11:59:59.123Z",
    ],
)
def test_accepts_utc_equivalents_and_one_second_tolerance(timestamp: str) -> None:
    assert _verify([_row()], [_row(created_at=timestamp)])["ok"] is True


def test_legacy_optional_token_defaults_and_nullable_provider() -> None:
    source = _row(provider=None)
    del source["cached_input_tokens"]
    del source["reasoning_tokens"]
    assert _verify([source], [_row(provider="")])["ok"] is True


def test_clickhouse_uint64_strings_compare_without_losing_precision() -> None:
    row = _row(total_cost_microdollars=2**60 + 1)
    actual = row | {field: str(row[field]) for field in verifier.FIELDS[:5]}
    assert _verify([row], [actual])["ok"] is True
    actual["total_cost_microdollars"] = str(2**60)
    assert _verify([row], [actual])["mismatch_fields"] == {"total_cost_microdollars": 1}
    actual["total_cost_microdollars"] = "invalid"
    assert _verify([row], [actual])["mismatch_fields"] == {"total_cost_microdollars": 1}


def test_missing_grace_uses_settle_time_even_for_an_old_created_at() -> None:
    old = _row("gen-old")
    recent = _row("gen-recent", created_at=(END - dt.timedelta(seconds=299)).isoformat())
    late = _row("gen-late", updated_at=END - dt.timedelta(seconds=1))
    boundary = _row("gen-boundary", updated_at=END - dt.timedelta(seconds=300))

    result = _verify([old, recent, late, boundary], [])

    assert result["missing_ids"] == ["gen-boundary", "gen-old"]
    assert result["pending_ids"] == ["gen-late", "gen-recent"]
    assert result["ok"] is False
    assert _verify([recent, late], [])["ok"] is True
    assert _verify([recent], [], grace_seconds=0)["missing"] == 1


def test_extra_ids_only_count_inside_the_half_open_window() -> None:
    before = _row("gen-before", created_at=(START - dt.timedelta(seconds=1)).isoformat())
    end = _row("gen-end", created_at=END.isoformat())
    start = _row("gen-start", created_at=START.isoformat())

    result = _verify([before, end], [before, end, start])

    assert result["sampled"] == 0
    assert result["extra_ids"] == ["gen-start"]
    assert result["ok"] is False
    assert _verify([], [before, end])["ok"] is True


def test_a_settle_that_landed_after_the_scan_passed_its_id_is_not_extra() -> None:
    """The scan walks ids, not time: a request settled inside the window
    after the scan passed its id is in ClickHouse and not in the scan. The
    point read finds it, so healthy delivery is not reported as a row from
    nowhere; a row Postgres has never had still is."""
    late = _row("gen-late")
    source, connection = _source([_row()])
    connection.late = ["gen-late"]

    result = verifier.verify_delivery(
        source, FakeClickHouse([_row(), late]).client, start=START, end=END, now=END, limit=100
    )

    assert result["extra_ids"] == []
    assert result["settled_after_scan"] == 1
    assert result["ok"] is True
    rechecks = [params for sql, params in connection.calls if "id = ANY(%s)" in sql]
    assert rechecks == [(["gen-late"],)]

    connection.late = []
    result = verifier.verify_delivery(
        source, FakeClickHouse([_row(), late]).client, start=START, end=END, now=END, limit=100
    )
    assert result["extra_ids"] == ["gen-late"]
    assert result["settled_after_scan"] == 0
    assert result["ok"] is False


def test_reports_duplicate_sort_keys_and_does_not_pick_one_arbitrarily() -> None:
    shifted = _row(created_at="2026-10-17T12:00:00Z")
    result = _verify([_row()], [_row(), shifted])

    assert result["duplicated_ids"] == ["gen-1"]
    assert result["mismatched"] == result["missing"] == 0
    assert result["ok"] is False


def test_a_postgres_id_outside_the_source_window_is_not_an_extra_id() -> None:
    old = _row(created_at="2025-10-18T12:00:00Z")
    result = _verify([old], [_row()])
    assert result["extra_ids"] == []
    assert result["sampled"] == 0


def test_empty_window_is_clean_and_does_not_issue_an_empty_in_lookup() -> None:
    source, _ = _source([])
    clickhouse = FakeClickHouse([])
    result = verifier.verify_delivery(source, clickhouse.client, start=START, end=END, limit=10)

    assert result["sampled"] == 0 and result["ok"] is True
    assert clickhouse.ids == []


def test_primary_key_pages_skip_old_rows_and_clickhouse_lookups_are_chunked() -> None:
    old = _row("gen-0000", created_at="2025-10-18T12:00:00Z")
    rows = [_row(f"gen-{index:04}") for index in range(1, 502)]
    source, connection = _source([old, *rows])
    clickhouse = FakeClickHouse(rows)
    result = verifier.verify_delivery(
        source,
        clickhouse.client,
        start=START,
        end=END,
        limit=1000,
        now=END,
    )

    assert result["ok"] is True
    assert result["sampled"] == 501 and result["scanned"] == 502
    assert [params for _, params in connection.calls] == [("", 500), ("gen-0499", 500)]
    assert [len(ids) for ids in clickhouse.ids] == [500, 1]
    assert "gen-0000" not in clickhouse.ids[0]


def test_scan_cap_cannot_claim_a_clean_empty_window_or_false_extras() -> None:
    old = _row("gen-0", created_at="2025-10-18T12:00:00Z")
    source, connection = _source([old, _row()])
    clickhouse = FakeClickHouse([_row()])
    result = verifier.verify_delivery(
        source,
        clickhouse.client,
        start=START,
        end=END,
        limit=10,
        scan_limit=1,
    )

    assert result["sampled"] == 0
    assert result["scanned"] == 1
    assert result["complete"] is result["extra_checked"] is result["ok"] is False
    assert clickhouse.calls == []
    assert connection.calls[-1][0].startswith("SELECT id FROM")


def test_exact_scan_cap_can_still_be_complete() -> None:
    assert _verify([_row()], [_row()], scan_limit=1)["complete"] is True


def _role_sql_view() -> str:
    """The CREATE VIEW statement from the reviewed operator SQL, for SQLite.

    SQLite has CREATE VIEW but not OR REPLACE; the body is otherwise the
    statement the operator runs, so the verifier is tested against it.
    """
    text = ROLE_SQL.read_text()
    start = text.index("CREATE OR REPLACE VIEW")
    return text[start : text.index(";", start)].replace("CREATE OR REPLACE VIEW", "CREATE VIEW")


def test_source_sql_reads_only_the_view_and_through_the_primary_key() -> None:
    database = sqlite3.connect(":memory:")
    database.execute(
        "CREATE TABLE tr_entities (kind TEXT, id TEXT, body TEXT, updated_at TEXT, "
        "PRIMARY KEY (kind, id))"
    )
    database.execute(_role_sql_view())
    row = _row()
    for kind, gid in (("generation", "gen-1"), ("api_key", "gen-2")):
        database.execute(
            "INSERT INTO tr_entities VALUES (?, ?, ?, ?)",
            (kind, gid, json.dumps(row | {"unrelated": "not projected"}), row["created_at"]),
        )

    class Connection:
        def transaction(self) -> Any:
            return contextlib.nullcontext()

        def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
            assert "tr_entities" not in sql
            return database.execute(sql.replace("%s", "?"), params)

    source = verifier.PostgresGenerationSource(dsn="host=localhost")
    source._connection = Connection()
    window = source.fetch(start=START, end=END, limit=10, scan_limit=10)

    # The other kind's row exists with an id the scan would pass; the view
    # alone keeps it out.
    assert window.complete
    assert window.known_ids == {"gen-1"}
    assert window.rows == {"gen-1": {field: row[field] for field in verifier.FIELDS}}
    view_columns = [
        column[1] for column in database.execute("PRAGMA table_info(tr_verifier_generations)")
    ]
    assert view_columns == ["id", *verifier.FIELDS, "updated_at"]
    for sql, params in (
        (verifier.SELECT_PAGE_SQL, ("", 500)),
        (verifier.CONTINUATION_SQL, ("",)),
    ):
        plan = database.execute("EXPLAIN QUERY PLAN " + sql.replace("%s", "?"), params).fetchall()
        # The view inlines: the planner seeks the (kind, id) key for both.
        assert any(
            detail.startswith("SEARCH tr_entities USING")
            and detail.endswith("(kind=? AND id>?)")
            for _, _, _, detail in plan
        ), plan
        assert all("SCAN tr_entities" not in detail for _, _, _, detail in plan)
    database.close()


def test_role_sql_grants_the_view_only_and_is_rerunnable() -> None:
    text = ROLE_SQL.read_text()
    statements = [line for line in text.splitlines() if line and not line.startswith("--")]
    grants = [line for line in statements if line.startswith("GRANT")]

    assert grants == ["GRANT SELECT ON tr_verifier_generations TO tr_verifier;"]
    assert "WHERE kind = 'generation';" in text
    assert "body ->> 'created_at' AS created_at" in text
    # Re-runnable: the view is replaced, the role and the IAM mapping are
    # created only when absent, and psql stops at the first error.
    assert "CREATE OR REPLACE VIEW tr_verifier_generations AS" in text
    assert "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tr_verifier') \\gexec" in text
    assert "FROM sys.iam_pg_role_mappings" in text
    assert "format('AWS IAM GRANT tr_verifier TO %L', :'node_role_arn')" in text
    assert "\\set ON_ERROR_STOP on" in text
    assert "tr_entities TO" not in text and "ALL PRIVILEGES" not in text


@pytest.mark.parametrize(
    "answer",
    [("tr_verifier", True, 0), ("tr_verifier", True, 3), ("admin", True, 40)],
)
def test_read_scope_reports_role_view_grant_and_other_readable_relations(
    answer: tuple[str, bool, int],
) -> None:
    calls: list[str] = []

    class Connection:
        def transaction(self) -> Any:
            return contextlib.nullcontext()

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
            calls.append(sql)
            return SimpleNamespace(fetchone=lambda: answer)

    source = verifier.PostgresGenerationSource(dsn="host=localhost")
    source._connection = Connection()

    assert source.read_scope() == answer
    assert calls == [verifier.READ_SCOPE_SQL]
    assert "has_table_privilege('public.tr_verifier_generations', 'SELECT')" in calls[0]
    assert "c.relname <> 'tr_verifier_generations'" in calls[0]


def test_source_result_limit_reports_incomplete_without_inventing_extra_ids() -> None:
    rows = [_row("gen-1"), _row("gen-2")]
    result = _verify(rows, rows, limit=1)
    assert result["sampled"] == 1
    assert result["complete"] is result["ok"] is result["extra_checked"] is False
    assert result["extra_ids"] == []


def test_clickhouse_window_limit_cannot_claim_complete_coverage() -> None:
    result = _verify([], [_row("gen-1"), _row("gen-2")], limit=1)
    assert result["complete"] is result["ok"] is False
    assert result["extra_checked"] is True
    assert result["extra_ids"] == ["gen-1"]


def test_report_id_lists_are_capped_but_counts_are_not() -> None:
    result = _verify([_row(f"gen-{index:03}") for index in range(25)], [])
    assert result["missing"] == 25
    assert len(result["missing_ids"]) == 20


@pytest.mark.parametrize("iam_auth", ["", "aws-dsql"])
def test_reuses_the_drains_password_and_scoped_iam_connections(
    monkeypatch: pytest.MonkeyPatch,
    iam_auth: str,
) -> None:
    source = verifier.PostgresGenerationSource(
        dsn="host=cluster.dsql.eu-west-3.on.aws user=tr_verifier dbname=postgres",
        iam_auth=iam_auth,
    )
    connection = FakeConnection([_row()])
    calls: list[dict[str, Any]] = []
    minted: list[dict[str, Any]] = []

    def connect(dsn: str, **kwargs: Any) -> FakeConnection:
        calls.append(kwargs)
        return connection

    def token(**kwargs: Any) -> str:
        minted.append(kwargs)
        return "fixture-token"

    monkeypatch.setattr(source, "_psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(
            client=lambda service, region_name: SimpleNamespace(
                generate_db_connect_auth_token=token
            ),
        ),
    )
    window = source.fetch(start=START, end=END, limit=10, scan_limit=10)
    source.close()

    assert list(window.rows) == ["gen-1"] and window.complete
    assert connection.closed
    assert calls[0]["connect_timeout"] == 10
    if iam_auth:
        assert calls[0]["password"] == "fixture-token"  # noqa: S105 - fake IAM token
        assert minted == [
            {
                "Hostname": "cluster.dsql.eu-west-3.on.aws",
                "Region": "eu-west-3",
                "ExpiresIn": 900,
            }
        ]
    else:
        assert "password" not in calls[0]
        assert calls[0]["options"] == "-c statement_timeout=60000"


@pytest.mark.parametrize("stored,expected_exit", [([], 1), ([_row()], 0)])
def test_cli_exit_codes_and_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stored: list[dict[str, Any]],
    expected_exit: int,
) -> None:
    source, connection = _source([_row()])
    clickhouse = FakeClickHouse(stored)
    history = tmp_path / "history.jsonl"
    monkeypatch.setenv("CH_PASSWORD", "fixture-value")
    monkeypatch.setattr(verifier, "PostgresGenerationSource", lambda **kwargs: source)
    monkeypatch.setattr(verifier, "ClickHouseGenerationSource", lambda **kwargs: clickhouse.client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_postgres_delivery",
            "--dsn",
            "host=localhost",
            "--start",
            START.isoformat(),
            "--end",
            END.isoformat(),
            "--grace-seconds",
            "0",
            "--history-file",
            str(history),
        ],
    )

    assert verifier.main() == expected_exit
    result = json.loads(capsys.readouterr().out)
    assert result == json.loads(history.read_text())
    assert result["grace_seconds"] == 0
    assert result["window_start"] == "2026-10-18T00:00:00Z"
    assert connection.closed


def test_cli_failure_never_prints_driver_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, connection = _source([])

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sensitive-driver-detail")

    monkeypatch.setenv("CH_PASSWORD", "fixture-value")
    monkeypatch.setattr(source, "fetch", fail)
    monkeypatch.setattr(verifier, "PostgresGenerationSource", lambda **kwargs: source)
    monkeypatch.setattr(sys, "argv", ["verify_postgres_delivery", "--dsn", "host=localhost"])

    assert verifier.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "RuntimeError"}
    assert "sensitive-driver-detail" not in output.out + output.err
    assert connection.closed


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--limit", "0"),
        ("--scan-limit", "-1"),
        ("--lookback-hours", "0"),
        ("--grace-seconds", "-1"),
    ],
)
def test_cli_rejects_invalid_limits_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_postgres_delivery", flag, value])
    with pytest.raises(SystemExit) as exc:
        verifier.main()
    assert exc.value.code == 2


def test_imports_without_google_libraries_as_on_the_aws_node() -> None:
    """The AWS node's venv has no Google client libraries. The verifier used to
    import its history writer from verify_spanner_delivery, which pulls in
    google.api_core transitively, so it could not even start there."""
    blocker = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'google' or name.startswith('google.'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import clickhouse.verify_postgres_delivery\n"
        "print('postgres verifier imported', flush=True)\n"
        "import clickhouse.verify_spanner_delivery\n"
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline program
        [sys.executable, "-c", blocker],
        cwd=root,
        env={"PYTHONPATH": f"{root}:{root / 'src'}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout == "postgres verifier imported\n", result.stderr
    # Positive control: the Spanner verifier really does need google, so the
    # blocker is live; only the Postgres import must survive it.
    assert "ImportError: google" in result.stderr
