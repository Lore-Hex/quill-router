import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("actions,allowed", [
    (["no-op"], True), (["read"], True), (["create"], True), (["update"], True),
    (["delete"], False), (["delete", "create"], False), (["create", "delete"], False),
    (["unknown"], False), ([], False),
])
def test_plan_guard(actions: list[str], allowed: bool) -> None:
    plan = {"format_version": "1.2", "resource_changes": [
        {"address": "example.protected", "change": {"actions": actions}},
    ]}
    result = subprocess.run(  # noqa: S603 -- repository script with fixture input
        [sys.executable, str(ROOT / "scripts/deploy/check_terraform_plan.py")],
        input=json.dumps(plan), text=True, capture_output=True,
    )
    assert (result.returncode == 0) == allowed
    if "delete" in actions:
        assert "example.protected" in result.stderr


@pytest.mark.parametrize("payload", ["", "{}", "null", '{"format_version":"1.2","errored":true}'])
def test_plan_guard_fails_closed(payload: str) -> None:
    result = subprocess.run(  # noqa: S603 -- repository script with fixture input
        [sys.executable, str(ROOT / "scripts/deploy/check_terraform_plan.py")],
        input=payload, text=True, capture_output=True,
    )
    assert result.returncode != 0


def test_workflow_guards_the_saved_plan_before_apply() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-apply.yml").read_text())
    step = next(s for s in workflow["jobs"]["apply"]["steps"] if s.get("name") == "Apply reviewed plan")
    assert step["shell"] == "bash"
    assert "if" not in step and "continue-on-error" not in step
    command = step["run"]
    assert "set -euo pipefail" in command
    assert command.index("show -json tfplan | python3 src/scripts/deploy/check_terraform_plan.py") < command.index("apply tfplan")
