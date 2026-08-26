from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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

    assert "OnUnitActiveSec=30min" in timer
    assert "Persistent=true" in timer

    assert "tr-clickhouse-workspace-directory.service" in installer
    assert "tr-clickhouse-workspace-directory.timer" in installer
    assert "systemctl enable --now tr-clickhouse-workspace-directory.timer" in installer
