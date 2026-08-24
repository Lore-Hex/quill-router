from __future__ import annotations

import re
import subprocess
import sys
import tarfile
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


def test_operational_deploy_resumes_live_ingest_before_backfills() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    stop = script.index(
        "systemctl stop tr-clickhouse-operational-ingest.service"
    )
    migration = script.index('log "adding workspace attribution to benchmark samples"')
    start = script.index(
        "systemctl start tr-clickhouse-operational-ingest.service",
        migration,
    )
    replay = script.index("clickhouse.backfill_benchmark_samples")
    backfill = script.index("clickhouse.backfill_operational_analytics --apply")
    assert stop < migration < start < replay < backfill
    paused_section = script[stop:start]
    assert "backfill_" not in paused_section
    assert "SYSTEM SYNC REPLICA" not in paused_section
    assert "clickhouse_replicate_rollups.sh" not in paused_section
    assert "trap cleanup EXIT" in script
    assert 'if [ "$ingester_stopped" -eq 1 ]' in script
    assert "deployment exited during parser/schema cutover" in script
    assert "SYSTEM SYNC REPLICA" in script
    assert "clickhouse_replicate_rollups.sh" in script
    assert "clickhouse_operational_analytics_finalize.sh --apply" in script
    assert "systemctl start tr-clickhouse-operational-parity.service" not in script
    assert "008_client_events_replicated.sql" in script
    assert "tr-clickhouse-client-rollup.service" in script
    assert "tr-clickhouse-client-rollup.timer" in script
    assert "tr-clickhouse-synthetic-reconcile.service" in script
    assert "tr-clickhouse-synthetic-reconcile.timer" in script
    assert "systemctl enable" in script
    assert 'id_column="event_id"' in script


def test_clickhouse_manual_deploys_bundle_only_valid_committed_source() -> None:
    helper = (ROOT / "scripts/deploy/_clickhouse_bundle.sh").read_text()
    assert "git -C \"$root\" status --porcelain" in helper
    assert "git -C \"$root\" archive" in helper
    assert "provider bundle contains invalid JSON" in helper
    assert "path.read_text(encoding=\"utf-8\")" in helper

    for relative in (
        "scripts/deploy/clickhouse_live_ingestion.sh",
        "scripts/deploy/clickhouse_operational_analytics.sh",
    ):
        script = (ROOT / relative).read_text()
        assert "source \"${SCRIPT_DIR}/_clickhouse_bundle.sh\"" in script
        assert "build_clickhouse_bundle \"$ROOT\" \"$archive\"" in script
        assert 'tar -C "$ROOT" -czf "$archive" clickhouse src/trusted_router' not in script


def _committed_clickhouse_fixture(tmp_path: Path, *, manifest: bytes) -> Path:
    repo = tmp_path / "repo"
    (repo / "clickhouse").mkdir(parents=True)
    data = repo / "src/trusted_router/data/provider_models"
    data.mkdir(parents=True)
    (repo / "clickhouse/worker.py").write_text("VALUE = 1\n")
    (data / "provider.json").write_bytes(manifest)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@trustedrouter.com"],
        ["git", "config", "user.name", "TrustedRouter Test"],
        ["git", "add", "clickhouse", "src/trusted_router"],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)  # noqa: S603
    return repo


def _run_bundle_helper(repo: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "scripts/deploy/_clickhouse_bundle.sh"
    return subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            "-c",
            'source "$1"; build_clickhouse_bundle "$2" "$3"',
            "bundle-test",
            str(helper),
            str(repo),
            str(archive),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_clickhouse_bundle_is_a_valid_committed_snapshot(tmp_path: Path) -> None:
    repo = _committed_clickhouse_fixture(tmp_path, manifest=b'{"models": []}\n')
    archive = tmp_path / "bundle.tar.gz"

    result = _run_bundle_helper(repo, archive)

    assert result.returncode == 0, result.stderr
    with tarfile.open(archive) as bundle:
        assert "src/trusted_router/data/provider_models/provider.json" in bundle.getnames()


def test_clickhouse_bundle_rejects_dirty_or_invalid_source(tmp_path: Path) -> None:
    dirty_repo = _committed_clickhouse_fixture(
        tmp_path / "dirty",
        manifest=b'{"models": []}\n',
    )
    (dirty_repo / "src/trusted_router/data/provider_models/provider.json").write_bytes(
        b"\xa3"
    )
    dirty = _run_bundle_helper(dirty_repo, tmp_path / "dirty.tar.gz")
    assert dirty.returncode != 0
    assert "refusing ClickHouse deployment from modified worker source" in dirty.stderr

    invalid_repo = _committed_clickhouse_fixture(
        tmp_path / "invalid",
        manifest=b"\xa3",
    )
    invalid = _run_bundle_helper(invalid_repo, tmp_path / "invalid.tar.gz")
    assert invalid.returncode != 0
    assert "provider bundle contains invalid JSON" in invalid.stderr


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
    assert "GRANT SELECT ON tr.client_minute_counters" in script
    assert "GRANT SELECT ON tr.client_request_events" in script
    assert "GRANT SELECT ON tr.client_availability_rollups" in script
    assert "GRANT SELECT ON tr.tr_entities" not in script
    assert "GRANT ALL" not in script


def test_control_reader_grants_cover_operational_queries_and_all_client_tables() -> None:
    """Pin grants to control-plane reads plus all three tables introduced by 008."""

    source = (ROOT / "src/trusted_router/operational_analytics.py").read_text()
    script = (ROOT / "scripts/deploy/clickhouse_control_reader.sh").read_text()
    # Every table source the reader issues is `FROM <table> FINAL`. A qualified,
    # aliased, dynamic, or non-FINAL source must extend this discovery to pass.
    sources = re.findall(r"\bFROM\s+[a-z_]", source)
    queried = set(re.findall(r"\bFROM ([a-z_]+) FINAL", source))
    assert len(sources) == len(re.findall(r"\bFROM ([a-z_]+) FINAL", source))
    granted = set(re.findall(r"<query>GRANT SELECT ON tr\.(\w+)</query>", script))

    client_tables = {
        "client_minute_counters",
        "client_request_events",
        "client_availability_rollups",
    }
    assert client_tables <= granted
    # The three provider rollups predate operational_analytics.py and remain
    # intentionally available to the same private, read-only account.
    legacy = {
        "provider_analytics_hourly",
        "provider_analytics_daily",
        "provider_analytics_monthly",
    }
    assert granted == queried | legacy | {"client_minute_counters"}
    assert "operational_outbox_quarantine" not in script
    for table in sorted(client_tables):
        assert f"SELECT count() FROM tr.{table} FINAL LIMIT 1" in script  # noqa: S608
    # A failed self-check must survive the cleanup command and fail deployment.
    assert (
        r">/dev/null || status=\$?; rm -f /tmp/tr-control-reader.env; exit \$status" in script
    )


def test_rollout_preserves_dual_read_mode_and_uses_distinct_reader_secret() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()
    assert "TR_ANALYTICS_READ_MODE" in rollout
    assert "LIVE_ANALYTICS_READ_MODE" in rollout
    assert "TR_ANALYTICS_DUAL_READ_STARTED_AT" in rollout
    assert "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true" in rollout
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=tr_control_read" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in rollout
    assert "trustedrouter-clickhouse-control-read-password" in secrets


def test_cutover_requires_soak_logs_queue_replica_and_positive_parity() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_analytics_cutover.sh").read_text()
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
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
    assert 'region_image="$(read_image "$region")"' in script
    assert "latestCreatedRevisionName == $active[0]" in script
    assert "latestReadyRevisionName == $active[0]" in script
    assert "value(status.imageDigest)" in script
    assert "does not match the live release selected for cutover" in script
    assert 'IMAGE="$live_image"' in script
    assert 'TR_DEPLOY_RELEASE_ID="$live_release"' in script
    assert "TR_DEPLOY_RELEASE_ID:-" in rollout


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


def test_workspace_directory_is_on_demand_not_a_timer() -> None:
    """The directory refresh is an operator tool, not a scheduled unit.

    The hourly timer was removed on 2026-08-19: new activity rows carry
    workspace_id directly, so the map of tenant_id -> workspace_id only covers
    the CLOSED set of pre-change rows and never goes stale. A timer would be a
    standing moving part guarding against a drift that can no longer happen --
    and it was wired into this deploy script, so removing the units without
    removing the wiring would break the next deploy at install -m.
    """
    deploy = (ROOT / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    assert "tr-clickhouse-workspace-directory" not in deploy
    assert not (ROOT / "clickhouse/tr-clickhouse-workspace-directory.service").exists()
    assert not (ROOT / "clickhouse/tr-clickhouse-workspace-directory.timer").exists()
    # The schema applies stay: the directory remains queryable, and new rows
    # need the workspace_id column before the drain ships.
    assert "010_workspace_directory.sql" in deploy
    assert "012_activity_generations_workspace_id.sql" in deploy


def test_live_ingestion_restarts_every_daemon_whose_code_it_ships() -> None:
    """The deploy replaces /opt/tr-clickhouse wholesale, so every long-running
    daemon on that tree must be restarted, not only the benchmark drain.

    On 2026-08-23 this script shipped a new ACTIVITY_COLUMNS allowlist while
    tr-clickhouse-operational-ingest kept the old module in memory: it
    silently dropped the new workspace_id key from every payload, and
    systemctl reported "active" throughout -- a running process says nothing
    about which code it runs. Asserting only that the benchmark drain is
    restarted was exactly the under-assertion that let this through.

    Timer-driven oneshots (archive, rollups, reconcile) pick up new code on
    their next fire and need no restart.
    """
    script = (ROOT / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    assert "systemctl restart tr-clickhouse-ingest.service" in script
    # The operational drains restart through the guarded loop: both units are
    # named, and the loop both restarts and re-asserts activeness. The guard
    # exists because the postgres variant only exists on the AWS/Azure nodes.
    loop = script[script.index("for unit in") : script.index("done", script.index("for unit in"))]
    assert "tr-clickhouse-operational-ingest.service" in loop
    assert "tr-clickhouse-operational-ingest-postgres.service" in loop
    assert "systemctl restart" in loop
    assert "systemctl is-active" in loop
    # The restart must come AFTER the tree extraction that replaces the code,
    # or it restarts the daemons into the same stale module.
    assert script.index("tar -xzf - -C /opt/tr-clickhouse") < script.index("for unit in")
