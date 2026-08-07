from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_load_balanced_control_plane_region_is_deployed() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    cold_step = "- name: Deploy cold regions (no canary, scale-to-zero)"
    monitor_step = "- name: Deploy synthetic monitor Cloud Run Job"

    assert "deploy_cold_regions:" not in workflow
    assert "steps.optional.outputs.deploy_cold_regions" not in workflow
    assert "TR_DEPLOY_TARGET_REGIONS: southamerica-east1" in workflow
    assert workflow.index(cold_step) < workflow.index(monitor_step)


def test_prod_smoke_checks_each_control_plane_region_directly() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert (
        "for region in us-central1 us-east4 europe-west4 southamerica-east1; do"
        in workflow
    )
    assert 'check_url "ready_${region}" "${service_url}/ready"' in workflow
    assert 'check_url "status_${region}" "${service_url}/status.json"' in workflow
    assert 'active_revision=$(jq -r' in workflow
    assert '[ "${active_count}" != "1" ]' in workflow
    assert '[ "${active_revision}" != "${latest_ready}" ]' in workflow
    assert '[ "${latest_ready}" != "${latest_created}" ]' in workflow
    assert 'gcloud run revisions describe "${active_revision}"' in workflow
    assert '[ "${active_release}" != "${SHA}" ]' in workflow
    assert '[ "${retired_secret_count}" != "0" ]' in workflow


def test_warm_secondary_regions_roll_in_parallel_after_primary_canary() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    primary_canary = workflow.index("- name: Canary gate — watch us-central1 only")
    start = workflow.index("- name: Roll secondary warm regions in parallel")
    end = workflow.index("- name: Deploy cold regions", start)
    rollout = workflow[start:end]

    assert primary_canary < start
    assert "regions=(europe-west4 us-east4)" in rollout
    assert 'for region in "${regions[@]}"; do' in rollout
    assert '(deploy_secondary "${region}")' in rollout
    assert 'if ! TR_DEPLOY_TARGET_REGIONS="${region}"' in rollout
    assert 'if ! bash scripts/deploy/staged_traffic.sh \\' in rollout
    assert 'active_traffic="$(gcloud run services describe' in rollout
    assert '[ "${active_traffic}" != "1" ]' in rollout
    assert "pids=()" in rollout
    assert 'pids+=("$!")' in rollout
    assert 'if wait "${pids[$idx]}"; then' in rollout
    assert 'rollback_region "${region}"' in rollout
    assert 'TR_DEPLOY_RECONCILE_LB: "0"' in rollout


def test_superseded_push_stops_before_production_mutation() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    confirm = workflow.index("confirm-current-main:")
    deploy = workflow.index("\n  deploy:", confirm)
    mutation = workflow.index("- name: Capture pre-deploy revisions", deploy)

    assert confirm < deploy < mutation
    assert 'git/ref/heads/main" --jq' in workflow[confirm:deploy]
    assert 'echo "proceed=false" >> "$GITHUB_OUTPUT"' in workflow[confirm:deploy]
    assert "if: ${{ needs.confirm-current-main.outputs.proceed == 'true' }}" in workflow


def test_runtime_secret_validation_is_parallel_and_does_not_restore_stale_ses_keys() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    sync = workflow.index("sync-runtime-secrets:")
    confirm = workflow.index("confirm-current-main:", sync)
    section = workflow[sync:confirm]

    assert "needs: [gate-on-ci]" in section
    assert "trustedrouter-aws-access-key-id" in section
    assert "gcloud secrets versions access latest" in section
    assert "secrets.TR_AWS_ACCESS_KEY_ID" not in section
    assert "secrets.TR_AWS_SECRET_ACCESS_KEY" not in section
    assert "trustedrouter-clickhouse-provider-read-password" in section
    assert "run: bash scripts/deploy/secrets.sh" not in workflow


def test_shared_load_balancer_is_reconciled_once_per_workflow() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert workflow.count('TR_DEPLOY_RECONCILE_LB: "1"') == 1
    assert workflow.count('TR_DEPLOY_RECONCILE_LB: "0"') == 2
