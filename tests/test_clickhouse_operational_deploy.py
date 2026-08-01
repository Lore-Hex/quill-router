from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_operational_schema_is_replicated_bounded_and_content_free() -> None:
    schema = (ROOT / "clickhouse/004_operational_analytics_replicated.sql").read_text()
    assert schema.count("ENGINE = ReplicatedReplacingMergeTree") == 3
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


def test_control_reader_is_private_read_only_and_cannot_read_secrets() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_control_reader.sh").read_text()
    assert "<readonly>1</readonly>" in script
    assert "<ip>10.0.0.0/8</ip>" in script
    assert "GRANT SELECT ON tr.activity_generations" in script
    assert "GRANT SELECT ON tr.synthetic_probe_samples" in script
    assert "GRANT SELECT ON tr.synthetic_status_rollups" in script
    assert "GRANT SELECT ON tr.tr_entities" not in script
    assert "GRANT ALL" not in script


def test_rollout_preserves_dual_read_mode_and_uses_distinct_reader_secret() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()
    assert "TR_ANALYTICS_READ_MODE" in rollout
    assert "LIVE_ANALYTICS_READ_MODE" in rollout
    assert "TR_ANALYTICS_DUAL_READ_STARTED_AT" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=tr_control_read" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in secrets


def test_cutover_requires_soak_logs_queue_replica_and_positive_parity() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_analytics_cutover.sh").read_text()
    assert "604800" in script
    assert "analytics_dual_read_mismatch" in script
    assert "tr_operational_analytics_outbox" in script
    assert "system.replicas" in script
    assert "operational-parity.jsonl" in script
    assert "TR_ANALYTICS_READ_MODE=clickhouse" in script


def test_capacity_probe_is_disposable_and_uses_a_conservative_gate() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_capacity_smoke.sh").read_text()
    assert "trap cleanup EXIT" in script
    assert "DROP TABLE IF EXISTS" in script
    assert "SYSTEM SYNC REPLICA" in script
    assert "throughput * 0.25" in script
    assert "add_shard_now" in script
