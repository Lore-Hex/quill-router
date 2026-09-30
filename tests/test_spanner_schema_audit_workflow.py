from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_schema_audit_workflow_pins_schedule_identity_permissions_and_emulator():
    workflow = yaml.safe_load((ROOT / ".github/workflows/spanner-schema-drift.yml").read_text())
    typed = yaml.safe_load((ROOT / ".github/workflows/typed-audit.yml").read_text())
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow[True]  # YAML 1.1 parses the GitHub `on` key as True.
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 11 * * *"}]
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    job = workflow["jobs"]["spanner-schema-drift"]
    assert job["if"] == "github.repository == 'Lore-Hex/quill-router'"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 25
    assert job["services"]["spanner"] == ci["jobs"]["spanner-emulator"]["services"]["spanner"]
    assert "@sha256:" in job["services"]["spanner"]["image"]
    steps = job["steps"]
    auth = next(step for step in steps if step.get("uses") == "google-github-actions/auth@v3")
    typed_auth = next(step for step in typed["jobs"]["typed-billing-invariant-audit"]["steps"] if step.get("uses") == "google-github-actions/auth@v3")
    assert auth["with"] == typed_auth["with"] == {
        "workload_identity_provider": "projects/44325983244/locations/global/workloadIdentityPools/github-actions/providers/github",
        "service_account": "tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com",
    }
    assert any(step.get("run") == "uv sync --frozen" for step in steps)
    assert steps[-1]["run"] == "uv run python -m scripts.audit_spanner_schema --emulator-host 127.0.0.1:9010"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in steps)
    assert "SPANNER_EMULATOR_HOST" not in str(workflow)
    assert "secrets." not in str(workflow)
