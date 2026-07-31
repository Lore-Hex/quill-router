from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy"


def test_gateway_5xx_alert_is_immediate_and_debounced() -> None:
    policy = (DEPLOY / "gateway-alerts" / "billing-5xx.yaml").read_text(
        encoding="utf-8"
    )

    assert 'displayName: "TR Gateway: billing path 5xx"' in policy
    assert "conditionMatchedLog:" in policy
    assert 'resource.labels.service_name = "trusted-router"' in policy
    assert "httpRequest.status >= 500" in policy
    assert "httpRequest.status < 600" in policy
    assert "/internal/gateway/(authorize|settle|refund)" in policy
    assert "notificationRateLimit:" in policy
    assert "period: 1800s" in policy
    assert "autoClose: 1800s" in policy


def test_gateway_pressure_alert_only_tracks_the_billing_path() -> None:
    pressure = (DEPLOY / "gateway-alerts" / "billing-pressure.yaml").read_text(
        encoding="utf-8"
    )
    original = (DEPLOY / "spanner-alerts" / "contention.yaml").read_text(
        encoding="utf-8"
    )

    assert "trustedrouter_gateway_billing_slow" in pressure
    assert "thresholdValue: 2" in pressure
    assert pressure.count("duration: 180s") == 1
    assert pressure.count("crossSeriesReducer: REDUCE_SUM") == 1
    assert pressure.count("groupByFields:") == 0
    assert "EVALUATION_MISSING_DATA_INACTIVE" in pressure
    assert "commit_attempt_count" not in pressure
    assert 'resource.type = "spanner_instance"' not in pressure

    assert "thresholdValue: 1.666666667" in original
    assert "duration: 600s" in original


def test_control_plane_memory_alert_warns_before_oom() -> None:
    policy = (DEPLOY / "gateway-alerts" / "control-plane-memory.yaml").read_text(
        encoding="utf-8"
    )

    assert 'displayName: "TR Control Plane: memory pressure"' in policy
    assert 'resource.labels.service_name = "trusted-router"' in policy
    assert 'metric.type = "run.googleapis.com/container/memory/utilizations"' in policy
    assert "perSeriesAligner: ALIGN_PERCENTILE_99" in policy
    assert "perSeriesAligner: ALIGN_MAX" not in policy
    assert "crossSeriesReducer: REDUCE_MAX" in policy
    assert "resource.label.location" in policy
    assert "thresholdValue: 0.8" in policy
    assert "duration: 300s" in policy
    assert "EVALUATION_MISSING_DATA_INACTIVE" in policy


def test_gateway_alert_deploy_is_idempotent_and_dry_run_by_default() -> None:
    script = (DEPLOY / "gateway_reliability.sh").read_text(encoding="utf-8")

    assert 'if [ "${1:-}" = "--apply" ]' in script
    assert "logging metrics describe" in script
    assert "logging metrics create" in script
    assert "logging metrics update" in script
    assert "monitoring policies create" in script
    assert "monitoring policies update" in script
    assert "TrustedRouter Spanner on-call" in script
    assert 'httpRequest.status < 500' in script
    assert 'httpRequest.latency >= "10s"' in script


def test_gateway_alert_workflow_requires_explicit_apply() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "gateway-reliability.yml"
    ).read_text(encoding="utf-8")

    assert 'if [ "${CONFIRMATION}" != "APPLY" ]' in workflow
    assert "id-token: write" in workflow
    assert "tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com" in workflow
    assert "gateway_reliability.sh --apply" in workflow
