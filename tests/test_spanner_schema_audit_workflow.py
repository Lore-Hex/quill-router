import re
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WIF_PROVIDER = "projects/44325983244/locations/global/workloadIdentityPools/github-actions/providers/github"
KNOWN_WIF_ALLOWLIST_EXCEPTIONS = {
    ".github/workflows/deploy-growth-sync.yml": (
        "Uses the provider as tr-deploy but is not admitted; the IAM fix requires "
        "an operator decision (Lore-Hex/quill-router#1416)."
    ),
}


def test_schema_audit_workflow_pins_schedule_identity_permissions_and_emulator():
    workflow = yaml.safe_load((ROOT / ".github/workflows/typed-audit.yml").read_text())
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow[True]  # YAML 1.1 parses the GitHub `on` key as True.
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "43 11 * * *"}]
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert set(workflow["jobs"]) == {"typed-billing-invariant-audit", "spanner-schema-drift"}
    assert all("needs" not in job for job in workflow["jobs"].values())
    job = workflow["jobs"]["spanner-schema-drift"]
    assert "permissions" not in job  # Inherit the pinned workflow permissions.
    assert job["if"] == "github.repository == 'Lore-Hex/quill-router'"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 25
    assert job["services"]["spanner"] == ci["jobs"]["spanner-emulator"]["services"]["spanner"]
    assert "@sha256:" in job["services"]["spanner"]["image"]
    steps = job["steps"]
    auth = next(step for step in steps if step.get("uses") == "google-github-actions/auth@v3")
    typed_auth = next(step for step in workflow["jobs"]["typed-billing-invariant-audit"]["steps"] if step.get("uses") == "google-github-actions/auth@v3")
    assert auth["with"] == typed_auth["with"] == {
        "workload_identity_provider": WIF_PROVIDER,
        "service_account": "tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com",
    }
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert any(step.get("uses") == "google-github-actions/setup-gcloud@v3" for step in steps)
    assert any(step.get("uses") == "astral-sh/setup-uv@v7" and step.get("with") == {"version": "latest"} for step in steps)
    assert any(step.get("run") == "uv sync --frozen" for step in steps)
    assert steps[-1]["run"] == "uv run python -m scripts.audit_spanner_schema --emulator-host 127.0.0.1:9010"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in steps)
    assert "SPANNER_EMULATOR_HOST" not in str(workflow)
    assert "secrets." not in str(workflow)


def _assert_workflows_using_gcp_wif_provider_are_allowlisted(root: Path) -> None:
    terraform = (root / "infra/gcp_wif.tf").read_text()
    match = re.search(r"\bquill_router_workflow_refs\s*=\s*\[(.*?)\]", terraform, re.DOTALL)
    assert match is not None, "Missing quill_router_workflow_refs in infra/gcp_wif.tf"
    allowed_refs = set(re.findall(r'^\s*"([^"\n]+)"\s*,?\s*(?:#.*)?$', match[1], re.MULTILINE))
    stale_exceptions = {
        path: reason
        for path, reason in KNOWN_WIF_ALLOWLIST_EXCEPTIONS.items()
        if f"${{local.github_owner}}/quill-router/{path}@refs/heads/main" in allowed_refs
    }
    assert not stale_exceptions, (
        "Remove stale GCP WIF allowlist exceptions: workflows are now admitted: "
        f"{stale_exceptions}"
    )
    missing = []
    for path in sorted((root / ".github/workflows").iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        workflow = yaml.safe_load(path.read_text())
        uses_provider = any(
            step.get("uses", "").startswith("google-github-actions/auth@")
            and step.get("with", {}).get("workload_identity_provider") == WIF_PROVIDER
            for job in workflow.get("jobs", {}).values()
            for step in job.get("steps", [])
        )
        workflow_ref = f"${{local.github_owner}}/quill-router/.github/workflows/{path.name}@refs/heads/main"
        workflow_path = path.relative_to(root).as_posix()
        if uses_provider and workflow_ref not in allowed_refs and workflow_path not in KNOWN_WIF_ALLOWLIST_EXCEPTIONS:
            missing.append(workflow_path)
    assert not missing, (
        "Workflows using the GCP WIF provider are missing from "
        f"infra/gcp_wif.tf quill_router_workflow_refs: {missing}"
    )


def test_all_workflows_using_gcp_wif_provider_are_allowlisted():
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(ROOT)


@pytest.fixture
def wif_repository_copy(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    (tmp_path / "infra").mkdir()
    shutil.copy2(ROOT / "infra/gcp_wif.tf", tmp_path / "infra/gcp_wif.tf")
    return tmp_path


def test_wif_allowlist_rejects_another_unlisted_workflow(wif_repository_copy: Path):
    workflows = wif_repository_copy / ".github/workflows"
    shutil.copy2(workflows / "deploy-growth-sync.yml", workflows / "unlisted-workflow.yml")

    with pytest.raises(AssertionError, match=r"missing from .*\.github/workflows/unlisted-workflow\.yml"):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_allowlist_rejects_an_admitted_known_exception(wif_repository_copy: Path):
    terraform = wif_repository_copy / "infra/gcp_wif.tf"
    terraform.write_text(terraform.read_text().replace(
        "quill_router_workflow_refs = [",
        'quill_router_workflow_refs = [\n'
        '    "${local.github_owner}/quill-router/.github/workflows/deploy-growth-sync.yml@refs/heads/main",',
        1,
    ))

    with pytest.raises(AssertionError, match=r"Remove stale .*\.github/workflows/deploy-growth-sync\.yml"):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
