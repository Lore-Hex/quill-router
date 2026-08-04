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


def test_warm_secondary_regions_roll_sequentially() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    start = workflow.index(
        "- name: Roll secondary warm regions sequentially (europe-west4, then us-east4)"
    )
    end = workflow.index("- name: Deploy cold regions", start)
    rollout = workflow[start:end]

    assert "regions=(europe-west4 us-east4)" in rollout
    assert 'for region in "${regions[@]}"; do' in rollout
    assert 'deploy_secondary "${region}"' in rollout
    assert 'if ! TR_DEPLOY_TARGET_REGIONS="${region}"' in rollout
    assert 'if ! bash scripts/deploy/staged_traffic.sh \\' in rollout
    assert 'active_traffic="$(gcloud run services describe' in rollout
    assert '[ "${active_traffic}" != "1" ]' in rollout
    assert "pids=(" not in rollout
    assert " &" not in rollout
