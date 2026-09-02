from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_aws_control_plane_installs_uv_before_running_completeness_gate() -> None:
    workflow = (ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text()

    setup_uv = workflow.index("uses: astral-sh/setup-uv@v7")
    deploy = workflow.index("run: bash scripts/deploy/aws_eu_control_plane.sh")

    assert setup_uv < deploy
