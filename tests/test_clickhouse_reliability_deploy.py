from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_archive_lifecycle_keeps_live_raw_objects_for_seven_years() -> None:
    lifecycle = json.loads(
        (ROOT / "scripts/deploy/clickhouse-archive-lifecycle.json").read_text()
    )
    rules = lifecycle["rule"]
    assert any(
        rule["action"]["type"] == "Delete"
        and rule["condition"] == {"age": 2555, "isLive": True}
        for rule in rules
    )
    assert any(rule["condition"] == {"age": 30, "isLive": False} for rule in rules)


def test_reliability_script_provisions_private_archive_and_snapshots() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_reliability.sh").read_text()

    assert "--uniform-bucket-level-access" in script
    assert "--public-access-prevention" in script
    assert "--versioning" in script
    assert "roles/storage.objectUser" in script
    assert "create snapshot-schedule" in script
    assert "--max-retention-days=30" in script
    assert "--on-source-disk-delete=keep-auto-snapshots" in script
    assert "disks add-resource-policies" in script


def test_reliability_script_installs_disk_metrics_and_idempotent_alerts() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_reliability.sh").read_text()
    disk_policy = (ROOT / "scripts/deploy/clickhouse-alerts/disk-capacity.yaml").read_text()
    uptime_policy = (
        ROOT / "scripts/deploy/clickhouse-alerts/node-availability.yaml"
    ).read_text()

    assert "google-cloud-ops-agent" in script
    assert "roles/monitoring.metricWriter" in script
    assert "monitoring policies create" in script
    assert "monitoring policies update" in script
    assert "agent.googleapis.com/disk/percent_used" in disk_policy
    assert "thresholdValue: 75" in disk_policy
    assert "compute.googleapis.com/instance/uptime" in uptime_policy
    assert "__INSTANCE_FILTER__" in disk_policy
    assert "__INSTANCE_FILTER__" in uptime_policy
    assert "name~^tr-clickhouse-[0-9]+$" in script


def test_online_resize_snapshots_resumes_ingestion_and_validates_filesystem() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_resize_disk.sh").read_text()

    assert 'TR_CLICKHOUSE_DISK_GB:-500' in script
    assert "compute snapshots create" in script
    assert "systemctl stop tr-clickhouse-ingest.service" in script
    assert "trap resume_ingester EXIT" in script
    assert "compute disks resize" in script
    assert "growpart" in script
    assert "resize2fs" in script
    assert "xfs_growfs" in script
    assert "resuming at filesystem growth" in script
    assert "systemctl is-active tr-clickhouse-ingest.service" in script


def test_failover_smoke_has_remote_and_local_restore_guards() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_failover_smoke.sh").read_text()

    assert 'RESTORE_UNIT="tr-clickhouse-failover-restore-' in script
    assert "systemd-run --unit=${RESTORE_UNIT}" in script
    assert "--on-active=${RESTORE_AFTER}" in script
    assert "trap restore_target EXIT INT TERM HUP" in script
    assert "systemctl start clickhouse-server" in script
    assert "wait_for_health 2 3 exact" in script
    assert "wait_for_health 3 3 exact" in script
    assert "for _ in \\$(seq 1 20)" in script
    assert "SYSTEM SYNC REPLICA provider_benchmark_samples" in script
