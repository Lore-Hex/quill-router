from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import clickhouse.refresh_workspace_directory as refresh_module
from clickhouse.refresh_workspace_directory import main, refresh_workspace_directory
from trusted_router.storage_operational_analytics import analytics_surrogate

ROOT = Path(__file__).parents[1]


class FakeSpannerSource:
    def __init__(self, bodies: list[Mapping[str, Any]]) -> None:
        self.bodies = bodies
        self.fetches = 0

    def fetch(self) -> list[Mapping[str, Any]]:
        self.fetches += 1
        return self.bodies


class FakeWriter:
    def __init__(self) -> None:
        self.directory_rows: list[dict[str, Any]] = []
        self.tenant_map_rows: list[dict[str, Any]] = []

    def upsert_directory(self, rows: list[dict[str, Any]]) -> None:
        self.directory_rows.extend(rows)

    def upsert_tenant_map(self, rows: list[dict[str, Any]]) -> None:
        self.tenant_map_rows.extend(rows)


def _source() -> FakeSpannerSource:
    return FakeSpannerSource(
        [
            {
                "id": "ws-one",
                "name": "First workspace",
                "deleted": False,
                "created_at": "2026-08-20T12:34:56Z",
                "email": "must-not-be-projected@example.test",
                "owner_email": "also-must-not-be-projected@example.test",
            },
            {
                "id": "ws-two",
                "name": "Renamed workspace",
                "deleted": True,
                "created_at": "2026-08-21T01:02:03+00:00",
            },
        ]
    )


def test_refresh_projects_only_directory_fields_and_updates_both_tables() -> None:
    source = _source()
    writer = FakeWriter()
    started_at = dt.datetime(2026, 8, 22, 9, 10, 11, tzinfo=dt.UTC)

    result = refresh_workspace_directory(source, writer, started_at=started_at)

    assert result.source_rows == result.directory_rows == result.tenant_map_rows == 2
    assert source.fetches == 1
    assert [row["deleted"] for row in writer.directory_rows] == [0, 1]
    assert all(
        set(row)
        == {
            "tenant_id",
            "workspace_id",
            "workspace_name",
            "billing_account_fingerprint",
            "deleted",
            "workspace_created_at",
            "refreshed_at",
        }
        for row in writer.directory_rows
    )
    assert all("email" not in key for row in writer.directory_rows for key in row)
    for row in writer.directory_rows:
        assert row["tenant_id"] == analytics_surrogate("workspace", row["workspace_id"])

    directory_pairs = {
        (row["tenant_id"], row["workspace_id"]) for row in writer.directory_rows
    }
    mapping_pairs = {
        (row["tenant_id"], row["workspace_id"]) for row in writer.tenant_map_rows
    }
    assert directory_pairs == mapping_pairs


def test_dry_run_reads_and_counts_but_writes_nothing(capsys: Any) -> None:
    source = _source()
    writer = FakeWriter()

    assert main(["--dry-run"], source=source, writer=writer) == 0

    assert source.fetches == 1
    assert writer.directory_rows == []
    assert writer.tenant_map_rows == []
    output = capsys.readouterr().out
    assert "workspace_directory_rows=2" in output
    assert "tenant_workspace_map_rows=2" in output
    assert "dry_run=true" in output


def test_refresh_source_contains_no_private_identity_queries() -> None:
    source = (ROOT / "clickhouse/refresh_workspace_directory.py").read_text().lower()
    assert "email" not in source
    assert "kind='user'" not in source


def test_workspace_directory_schemas_have_matching_columns_and_version_order() -> None:
    replicated = (ROOT / "clickhouse/010_workspace_directory.sql").read_text()
    single = (ROOT / "clickhouse/011_workspace_directory_single_node.sql").read_text()

    for column in (
        "tenant_id",
        "workspace_id",
        "workspace_name",
        "deleted",
        "workspace_created_at",
        "refreshed_at",
    ):
        assert column in replicated
        assert column in single
    assert "ON CLUSTER trustedrouter" in replicated
    assert (
        "'/trustedrouter/tables/{shard}/workspace_directory-v1',\n"
        "    '{replica}',\n"
        "    refreshed_at"
    ) in replicated
    assert "ENGINE = ReplacingMergeTree(refreshed_at)" in single


def test_the_refresh_is_scheduled_and_installed() -> None:
    """The directory went 685 workspaces stale between two manual runs.

    The module used to declare itself deliberately unscheduled, which was true
    for the tenant map and false for the directory: signup reporting joins new
    activity to workspace names here, and a dimension table that only updates
    when somebody remembers it goes quiet exactly when nobody is looking. These
    pins keep the timer pair present, hardened like its siblings, and actually
    installed and enabled by the node installer.
    """
    root = Path(__file__).resolve().parents[1]
    service = (root / "clickhouse/tr-clickhouse-workspace-directory.service").read_text()
    timer = (root / "clickhouse/tr-clickhouse-workspace-directory.timer").read_text()
    installer = (root / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    assert "python -m clickhouse.refresh_workspace_directory" in service
    assert "User=tr-clickhouse-ingest" in service
    assert "EnvironmentFile=/etc/tr-clickhouse-ingest.env" in service
    assert "ProtectSystem=strict" in service

    assert "OnCalendar=*-*-* *:00/30:00" in timer
    assert "Persistent=true" in timer

    assert "tr-clickhouse-workspace-directory.service" in installer
    assert "tr-clickhouse-workspace-directory.timer" in installer
    assert "systemctl enable --now tr-clickhouse-workspace-directory.timer" in installer


def _view_row(body: Mapping[str, Any]) -> list[str | None]:
    """What tr_workspace_directory_source returns for an entity body.

    JSON_QUERY yields each field's JSON text, or NULL when the key is absent or
    its value is JSON null.
    """
    keys = ("id", "name", "deleted", "owner_user_id", "federated_home", "created_at")
    return [
        None if body.get(key) is None else json.dumps(body[key], separators=(",", ":"))
        for key in keys
    ]


class _FakeSnapshot:
    def __init__(self, rows: list[list[str | None]], calls: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._calls = calls

    def __enter__(self) -> _FakeSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute_sql(self, sql: str, **kwargs: Any) -> list[list[str | None]]:
        self._calls.append({"sql": sql, **kwargs})
        return self._rows


def _view_source(rows: list[list[str | None]]) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    source = object.__new__(refresh_module.SpannerWorkspaceSource)
    source._database = SimpleNamespace(snapshot=lambda: _FakeSnapshot(rows, calls))
    return source, calls


_PARITY_BODIES: list[dict[str, Any]] = [
    {
        "id": "ws-owned",
        "name": 'Quote "and" unicode é',
        "owner_user_id": "user-1",
        "created_at": "2026-08-20T12:34:56Z",
        "email": "never-read@example.test",
    },
    {"id": "ws-deleted", "name": "Gone", "deleted": True, "owner_user_id": "user-2",
     "created_at": "2026-08-21T01:02:03+00:00"},
    {"id": "ws-federated", "name": "Fed", "owner_user_id": "user-3",
     "federated_home": {"region": "eu"}, "created_at": "2026-08-22T00:00:00Z"},
    {"id": "ws-empty-home", "name": "Empty", "owner_user_id": "user-4",
     "federated_home": "", "deleted": False, "created_at": "2026-08-23T00:00:00Z"},
    {"id": "ws-null-owner", "name": "Null", "owner_user_id": None,
     "created_at": "2026-08-24T00:00:00Z"},
]


def test_the_view_rows_project_exactly_as_the_entity_bodies_did() -> None:
    refreshed_at = dt.datetime(2026, 11, 12, 3, tzinfo=dt.UTC)
    source, calls = _view_source([_view_row(body) for body in _PARITY_BODIES])

    bodies = source.fetch()

    assert [
        refresh_module._project_workspace(body, refreshed_at=refreshed_at) for body in bodies
    ] == [
        refresh_module._project_workspace(body, refreshed_at=refreshed_at)
        for body in _PARITY_BODIES
    ]
    assert all("email" not in body for body in bodies)
    [call] = calls
    assert call["sql"] == (
        "SELECT workspace_id, name, deleted, owner_user_id, federated_home, created_at "
        "FROM tr_workspace_directory_source ORDER BY id LIMIT 50001"
    )
    assert "params" not in call


def test_a_field_the_body_lacks_stays_absent_and_a_required_one_still_fails() -> None:
    source, _ = _view_source([[None, '"No id"', None, None, None, '"2026-08-24T00:00:00Z"']])

    [body] = source.fetch()

    assert body == {"name": "No id", "created_at": "2026-08-24T00:00:00Z"}
    with pytest.raises(KeyError):
        refresh_module._project_workspace(body, refreshed_at=dt.datetime.now(dt.UTC))


def test_a_view_row_of_another_shape_is_refused() -> None:
    source, _ = _view_source([['"ws"', '"name"']])
    with pytest.raises(ValueError, match="unexpected row shape"):
        source.fetch()


def test_the_refresh_never_reads_entity_bodies_itself() -> None:
    source = (ROOT / "clickhouse/refresh_workspace_directory.py").read_text()
    code = source.split('"""', 2)[2]
    assert "FROM tr_entities" not in code
    assert "kind=@kind" not in code
