from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_clickhouse_node_preserves_disk_and_blocks_accidental_vm_deletion() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_node.sh").read_text()

    assert "--no-boot-disk-auto-delete" in script
    assert "set-disk-auto-delete" in script
    assert "--no-auto-delete" in script
    assert script.count("--deletion-protection") >= 2
    assert 'DISK_GB="${DISK_GB:-500}"' in script


def test_clickhouse_password_never_enters_instance_metadata() -> None:
    node = (ROOT / "scripts/deploy/clickhouse_node.sh").read_text()
    startup = (ROOT / "scripts/deploy/clickhouse_startup.sh").read_text()

    assert "--metadata ch-password=" not in node
    assert "clickhouse-password-secret" in node
    assert "secretmanager.googleapis.com" in startup
    assert "instance/attributes/ch-password" not in startup


def test_live_deploy_installs_archive_and_rollup_timers() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    assert "002_provider_analytics_rollups.sql" in script
    assert "tr-clickhouse-archive.timer" in script
    assert "tr-clickhouse-rollup-hourly.timer" in script
    assert "tr-clickhouse-rollup-daily.timer" in script


def test_cluster_migration_is_parity_gated_and_keeps_local_backup() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_cluster.sh").read_text()

    assert "tr-clickhouse-2" in script
    assert "tr-clickhouse-3" in script
    assert "us-central1-b" in script
    assert "us-central1-c" in script
    assert "<keeper_server>" in script
    assert "pid_two" in script
    assert "pid_three" in script
    assert "SYSTEM SYNC REPLICA" in script
    assert "source and replicated fingerprints differ" in script
    assert "timedelta(minutes=5)" in script
    assert "service account did not become visible" in script
    assert "provider_benchmark_samples_local_backup" in script
    assert "RENAME TABLE provider_benchmark_samples TO" in script
    assert "DROP TABLE provider_benchmark_samples_local_backup" not in script


def test_cluster_load_balancer_is_private_global_access_and_three_backend() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_cluster.sh").read_text()

    assert "--load-balancing-scheme=INTERNAL" in script
    assert "--allow-global-access" in script
    assert "--ports=8123" in script
    assert "tr-clickhouse-health-check" in script
    assert "35.191.0.0/16,130.211.0.0/22" in script
    assert "http://${ip}:8123/ping" in script


def test_rollout_prefers_private_clickhouse_load_balancer() -> None:
    script = (ROOT / "scripts/deploy/rollout.sh").read_text()

    assert "compute addresses describe tr-clickhouse-ilb" in script
    assert 'TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL=${PROVIDER_ANALYTICS_CLICKHOUSE_URL}' in script
