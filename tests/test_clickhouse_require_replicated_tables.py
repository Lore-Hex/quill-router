"""G7 in docs/design/clickhouse-high-availability.md: the worker installer applies
single-node DDL whose CREATE TABLE IF NOT EXISTS would give a freshly rebuilt
node NON-replicated canonical tables. It now refuses first unless every
canonical table already exists as a replica.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clickhouse import require_replicated_tables as guard

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/deploy/clickhouse_live_ingestion.sh"
REQUIRED = [
    "provider_benchmark_samples",
    "provider_analytics_hourly",
    "provider_analytics_daily",
    "provider_analytics_monthly",
]

# The `tr` inventory measured on tr-clickhouse-1 on 2026-10-01 (§1.2 of the
# design document): 17 replicated tables, two views and the eight node-1-only
# tables (backups, staging and a Join engine).
NODE_ONE = {
    **{
        name: "ReplicatedReplacingMergeTree"
        for name in (
            "activity_generations",
            "provider_benchmark_samples",
            "spend_lease_shadow",
            "synthetic_probe_samples",
            "synthetic_status_rollups",
            "public_analytics_snapshots",
            "client_request_events",
            "client_minute_counters",
            "client_availability_rollups",
            "workspace_directory",
            "tenant_workspace_map",
            "reservation_overruns",
            "analytics_workspace_backfill",
        )
    },
    **{
        name: "ReplicatedMergeTree"
        for name in (
            "provider_analytics_hourly",
            "provider_analytics_daily",
            "provider_analytics_monthly",
            "operational_outbox_quarantine",
        )
    },
    "growth_billing_owners": "View",
    "growth_daily_usage": "View",
    "provider_benchmark_samples_local_backup": "ReplacingMergeTree",
    "provider_analytics_hourly_local_backup": "MergeTree",
    "provider_analytics_daily_local_backup": "MergeTree",
    "provider_analytics_monthly_local_backup": "MergeTree",
    "provider_analytics_hourly_staging": "MergeTree",
    "provider_analytics_daily_staging": "MergeTree",
    "provider_analytics_monthly_staging": "MergeTree",
    "ws_backfill_map": "Join",
}


def test_the_measured_production_inventory_passes() -> None:
    # Positive control: the guard must not refuse the cluster as it is today.
    assert guard.replication_problems(NODE_ONE, REQUIRED) == []


def test_a_freshly_rebuilt_node_with_no_tables_is_refused() -> None:
    assert guard.replication_problems({}, REQUIRED) == [f"{name}: missing" for name in REQUIRED]


def test_single_node_ddl_applied_to_a_rebuilt_node_is_refused() -> None:
    # What 001/002 create on a node that has no tables yet.
    rebuilt = {
        "provider_benchmark_samples": "ReplacingMergeTree",
        "provider_analytics_hourly": "MergeTree",
        "provider_analytics_daily": "MergeTree",
        "provider_analytics_monthly": "MergeTree",
        "provider_analytics_hourly_staging": "MergeTree",
    }
    assert guard.replication_problems(rebuilt, REQUIRED) == [
        "provider_benchmark_samples: ReplacingMergeTree, not replicated",
        "provider_analytics_hourly: MergeTree, not replicated",
        "provider_analytics_daily: MergeTree, not replicated",
        "provider_analytics_monthly: MergeTree, not replicated",
    ]


def test_any_other_unreplicated_canonical_table_is_refused() -> None:
    drifted = {**NODE_ONE, "activity_generations": "ReplacingMergeTree"}
    assert guard.replication_problems(drifted, REQUIRED) == [
        "activity_generations: ReplacingMergeTree, not replicated"
    ]


def test_node_local_suffixes_must_be_suffixes() -> None:
    tables = {**NODE_ONE, "provider_staging_rollup": "MergeTree"}
    assert guard.replication_problems(tables, REQUIRED) == [
        "provider_staging_rollup: MergeTree, not replicated"
    ]


def test_parse_tables_reads_tab_separated_rows_and_rejects_malformed_ones() -> None:
    assert guard.parse_tables("a\tMergeTree\nb\tView\n\n") == {"a": "MergeTree", "b": "View"}
    with pytest.raises(ValueError, match="unexpected system.tables row"):
        guard.parse_tables("no-engine\n")


class _FakeClickHouse:
    answer = ""
    queries: list[str] = []

    def __init__(self, *, password: str) -> None:
        assert password == os.environ["CH_PASSWORD"]

    def query(self, sql: str, **_: object) -> str:
        self.queries.append(sql)
        return self.answer


@pytest.mark.parametrize(
    ("tables", "code"),
    [(NODE_ONE, 0), ({}, 1)],
    ids=["production-inventory", "rebuilt-node"],
)
def test_main_exits_nonzero_only_when_tables_would_not_replicate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tables: dict[str, str],
    code: int,
) -> None:
    _FakeClickHouse.answer = "".join(f"{name}\t{engine}\n" for name, engine in tables.items())
    _FakeClickHouse.queries = []
    monkeypatch.setattr(guard, "ClickHouse", _FakeClickHouse)
    monkeypatch.setenv("CH_PASSWORD", "fixture-clickhouse-credential")

    assert guard.main(REQUIRED) == code

    assert _FakeClickHouse.queries == [guard.TABLES_SQL]
    err = capsys.readouterr().err
    assert ("refusing" in err) is (code == 1)


def test_main_requires_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CH_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="CH_PASSWORD"):
        guard.main(REQUIRED)


def test_the_installer_runs_the_guard_before_any_single_node_ddl() -> None:
    script = INSTALLER.read_text()
    guard_at = script.index("python -m clickhouse.require_replicated_tables")
    assert guard_at < script.index("001_provider_benchmark_samples.sql")
    assert guard_at < script.index("002_provider_analytics_rollups.sql")
    # It runs after the password is loaded and before the drains are enabled.
    assert script.index(". /etc/tr-clickhouse-ingest.env") < guard_at
    assert guard_at < script.index("systemctl enable tr-clickhouse-ingest.service")
    invocation = script[guard_at : script.index(")", guard_at)]
    for table in REQUIRED:
        assert table in invocation
