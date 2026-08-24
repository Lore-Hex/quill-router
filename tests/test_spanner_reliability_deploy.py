from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy"


def test_every_spanner_alert_reduces_cross_series_fanout() -> None:
    policy_files = sorted((DEPLOY / "spanner-alerts").glob("*.yaml"))

    assert len(policy_files) == 6
    display_names: set[str] = set()
    for policy_file in policy_files:
        policy = policy_file.read_text(encoding="utf-8")
        aggregations = policy.count("      aggregations:")
        reducers = policy.count("          crossSeriesReducer:")

        assert aggregations > 0, policy_file.name
        assert reducers == aggregations, policy_file.name
        assert "groupByFields:" not in policy, policy_file.name
        assert "enabled: true" in policy, policy_file.name

        display_name = next(
            line for line in policy.splitlines() if line.startswith("displayName:")
        )
        assert display_name not in display_names
        display_names.add(display_name)


def test_api_failure_alert_excludes_retry_status_case_variants() -> None:
    policy = (DEPLOY / "spanner-alerts" / "api-failures.yaml").read_text(
        encoding="utf-8"
    )

    for status in ("Aborted", "aborted", "AlreadyExists", "already_exists"):
        assert f'metric.labels.status != "{status}"' in policy


def test_high_priority_cpu_alert_keeps_short_spikes_visible() -> None:
    policy = (DEPLOY / "spanner-alerts" / "high-priority-cpu.yaml").read_text(
        encoding="utf-8"
    )

    assert "alignmentPeriod: 300s" in policy
    assert "perSeriesAligner: ALIGN_MAX" in policy
    assert "thresholdValue: 0.45" in policy
    assert "duration: 300s" in policy


def test_backup_copy_workflow_is_deterministic_and_idempotent() -> None:
    workflow = (DEPLOY / "spanner_backup_copy_workflow.yaml").read_text(
        encoding="utf-8"
    )

    assert 'destination_id: ${"copy-" + source_id}' in workflow
    assert "condition: ${error.code == 409}" in workflow
    assert "status: already_copied" in workflow
    assert "pageSize: 1" in workflow
    assert "backupSchedules:default_daily_full_backup_schedule" in workflow


def test_spanner_reliability_baseline_is_production_safe() -> None:
    library = (DEPLOY / "_lib.sh").read_text(encoding="utf-8")
    reliability = (DEPLOY / "spanner_reliability.sh").read_text(encoding="utf-8")

    assert 'SPANNER_CONFIG="${TR_SPANNER_CONFIG:-nam6}"' in library
    assert 'SPANNER_EDITION="${TR_SPANNER_EDITION:-ENTERPRISE_PLUS}"' in library
    assert 'SPANNER_PROCESSING_UNITS="${TR_SPANNER_PROCESSING_UNITS:-300}"' in library
    assert "--enable-drop-protection" in reliability
    assert "version_retention_period = '7d'" in reliability
    assert "--backup-type=incremental-backup" in reliability
    assert "--cron='0 */4 * * *'" in reliability
    assert "--retention-duration=7d" in reliability
    assert "--update-headers=Content-Type=application/json" in reliability


def test_cross_project_backup_uses_an_isolated_project() -> None:
    reliability = (DEPLOY / "spanner_reliability.sh").read_text(encoding="utf-8")
    workflow = (DEPLOY / "spanner_backup_copy_workflow.yaml").read_text(
        encoding="utf-8"
    )

    assert 'DR_PROJECT_ID="${TR_SPANNER_DR_PROJECT_ID:-trustedrouter-dr}"' in reliability
    assert "projects/trustedrouter-dr/instances/trusted-router-backups" in workflow
    assert "--service-account=\"$BACKUP_SERVICE_ACCOUNT_EMAIL\"" in reliability
