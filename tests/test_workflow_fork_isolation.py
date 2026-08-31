from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CANONICAL_GUARD = "github.repository == 'Lore-Hex/quill-router'"
CLOUD_AUTH_ACTIONS = (
    "google-github-actions/auth@",
    "aws-actions/configure-aws-credentials@",
    "azure/login@",
)


def test_every_cloud_authenticated_job_is_disabled_in_forks() -> None:
    unguarded: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            steps = job.get("steps") or []
            uses = [str(step.get("uses", "")) for step in steps]
            if not any(
                action in use
                for use in uses
                for action in CLOUD_AUTH_ACTIONS
            ):
                continue
            if CANONICAL_GUARD not in str(job.get("if", "")):
                unguarded.append(f"{path.name}:{job_name}")

    assert unguarded == [], (
        "cloud-authenticated jobs must refuse fork execution before requesting "
        f"an OIDC token: {unguarded}"
    )
