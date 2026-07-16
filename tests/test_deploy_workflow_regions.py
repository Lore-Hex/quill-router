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
    assert 'check_url "status_${region}" "${service_url}/status.json"' in workflow
