from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_clickhouse_worker_projection_import_does_not_require_pydantic() -> None:
    script = """
import builtins

real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "pydantic" or name.startswith("pydantic."):
        raise ModuleNotFoundError("pydantic is intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import trusted_router.storage_operational_analytics
"""

    result = subprocess.run(  # noqa: S603 - fixed interpreter and inert test script.
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_operational_schema_is_replicated_bounded_and_content_free() -> None:
    schema = (ROOT / "clickhouse/004_operational_analytics_replicated.sql").read_text()
    assert schema.count("ENGINE = ReplicatedReplacingMergeTree") == 4
    assert "INTERVAL 400 DAY" in schema
    assert "INTERVAL 14 DAY" in schema
    assert "INTERVAL 24 MONTH" in schema
    for forbidden_column in (
        "prompt_content",
        "output_content",
        "workspace_id ",
        "key_hash ",
        "api_key ",
        "authorization_header",
    ):
        assert forbidden_column not in schema.lower()


def test_provider_rollup_schema_replicates_all_published_granularities() -> None:
    schema = (ROOT / "clickhouse/005_provider_rollups_replicated.sql").read_text()
    assert schema.count("ENGINE = ReplicatedMergeTree") == 3
    for granularity in ("hourly", "daily", "monthly"):
        assert f"provider_analytics_{granularity}_replicated" in schema


def test_operational_deploy_backfills_before_starting_live_ingest() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    backfill = script.index("clickhouse.backfill_operational_analytics --apply")
    start = script.index(
        "systemctl start tr-clickhouse-operational-ingest.service",
        backfill,
    )
    assert backfill < start
    assert "SYSTEM SYNC REPLICA" in script
    assert "clickhouse_replicate_rollups.sh" in script
    assert "clickhouse_operational_analytics_finalize.sh --apply" in script
    assert "systemctl start tr-clickhouse-operational-parity.service" not in script
    assert "008_client_events_replicated.sql" in script
    assert "tr-clickhouse-client-rollup.service" in script
    assert "tr-clickhouse-client-rollup.timer" in script
    assert "systemctl enable" in script
    assert 'id_column="event_id"' in script


def test_client_telemetry_single_node_schema_is_applied_with_operational_schema() -> None:
    script = (ROOT / "scripts/deploy/aws_eu_north_clickhouse.sh").read_text()

    assert "006_operational_analytics_single_node.sql" in script
    assert "009_client_events_single_node.sql" in script
    assert "${CLIENT_SCHEMA}" in script


def test_operational_finalize_requires_live_outbox_before_closing_gap() -> None:
    script = (
        ROOT / "scripts/deploy/clickhouse_operational_analytics_finalize.sh"
    ).read_text()
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in script
    assert "--skip-synthetic --skip-rollups" in script
    assert "--recent-limit 20000 --skip-activity --skip-rollups" in script
    replay = script.index("replaying activity after the outbox producer is live")
    parity = script.index("systemctl start tr-clickhouse-operational-parity.service")
    assert replay < parity


def test_control_reader_is_private_read_only_and_cannot_read_secrets() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_control_reader.sh").read_text()
    assert "<readonly>1</readonly>" in script
    assert "<ip>10.0.0.0/8</ip>" in script
    assert "GRANT SELECT ON tr.activity_generations" in script
    assert "GRANT SELECT ON tr.synthetic_probe_samples" in script
    assert "GRANT SELECT ON tr.synthetic_status_rollups" in script
    assert "GRANT SELECT ON tr.public_analytics_snapshots" in script
    assert "GRANT SELECT ON tr.tr_entities" not in script
    assert "GRANT ALL" not in script


def test_rollout_preserves_dual_read_mode_and_uses_distinct_reader_secret() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()
    assert "TR_ANALYTICS_READ_MODE" in rollout
    assert 'serving_env_value TR_ANALYTICS_READ_MODE bigtable' in rollout
    assert 'TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}' in rollout
    assert "TR_ANALYTICS_DUAL_READ_STARTED_AT" in rollout
    assert "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=tr_control_read" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in secrets


def test_cutover_requires_soak_logs_queue_replica_and_positive_parity() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_analytics_cutover.sh").read_text()
    assert "604800" in script
    assert "analytics_dual_read_mismatch" in script
    assert "tr_operational_analytics_outbox" in script
    assert "TR_ANALYTICS_MAX_OUTBOX_ROWS" in script
    assert "TR_ANALYTICS_MAX_OUTBOX_AGE_SECONDS" in script
    assert "oldest_age_seconds" in script
    assert "system.replicas" in script
    assert "operational-parity.jsonl" in script
    assert "verify_operational_parity_history.py" in script
    assert "TR_ANALYTICS_DEPLOY_CREDENTIAL_FILE" in script
    assert "refusing to deploy with the read-only operations identity" in script
    assert "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" in script
    assert "TR_ANALYTICS_READ_MODE=clickhouse" in script


def test_operational_parity_worker_has_a_bounded_runtime() -> None:
    service = (
        ROOT / "clickhouse/tr-clickhouse-operational-parity.service"
    ).read_text()
    assert "TimeoutStartSec=5m" in service


def test_generation_record_migration_has_ttl_and_delivery_audit_index() -> None:
    script = (ROOT / "scripts/deploy/migrate_generation_records.sh").read_text()
    assert "ROW DELETION POLICY" in script
    assert "INTERVAL 30 DAY" in script
    assert "tr_generation_by_terminal_at" in script
    assert "STORING (payload)" in script


def test_spanner_delivery_verifier_is_installed_and_bounded() -> None:
    deploy = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    service = (ROOT / "clickhouse/tr-clickhouse-spanner-delivery.service").read_text()
    assert "tr-clickhouse-spanner-delivery.timer" in deploy
    assert "TimeoutStartSec=5m" in service
    assert "verify_spanner_delivery" in service


def test_final_bigtable_retirement_is_two_soak_gated_and_non_destructive() -> None:
    script = (ROOT / "scripts/deploy/retire_bigtable_runtime.sh").read_text()
    assert "604800" in script
    assert "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" in script
    assert "operational-parity.jsonl" in script
    assert "spanner-delivery.jsonl" in script
    assert "archive-restore.json" in script
    assert "archive-backfill-complete.json" in script
    assert "tr_analytics_outbox" in script
    assert "tr_operational_analytics_outbox" in script
    assert "tr_settle_outbox" in script
    assert "TR_STORAGE_BACKEND=spanner-clickhouse" in script
    assert "TR_ANALYTICS_READ_MODE=clickhouse-only" in script
    assert "TR_BIGTABLE_MIRROR_WRITES_ENABLED=false" in script
    assert "verify_deployment.sh" in script
    assert "delete-instance" not in script
    assert "delete-table" not in script


def test_retirement_preparation_backfills_and_restore_verifies_every_dataset() -> None:
    script = (ROOT / "scripts/deploy/prepare_bigtable_retirement.sh").read_text()
    assert "clickhouse_operational_analytics.sh" in script
    assert "clickhouse.archive_daily --backfill" in script
    assert "clickhouse.verify_archive_restore" in script
    assert "clickhouse.verify_archive_backfill" in script
    assert "printf" not in script
    assert "clickhouse.verify_spanner_delivery" not in script
    assert "tr-clickhouse-spanner-delivery.service" in script
    assert "would not change production read mode" in script


def test_capacity_probe_is_disposable_and_uses_a_conservative_gate() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_capacity_smoke.sh").read_text()
    assert "trap cleanup EXIT" in script
    assert "DROP TABLE IF EXISTS" in script
    assert "SYSTEM SYNC REPLICA" in script
    assert "throughput * 0.25" in script
    assert "add_shard_now" in script
