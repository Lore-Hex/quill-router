import json
import subprocess
import sys
from copy import deepcopy
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
    assert "[nyte-404-only]" in step["env"]["NYTE_404_ONLY"]
    assert command.index("src/infra/tools/check_nyte_404_plan.py") < command.index("apply tfplan")


@pytest.mark.parametrize("variant", ["valid", "host", "other", "unknown", "status", "path", "bucket"])
def test_nyte_release_scope(variant: str) -> None:
    before_matchers = [{"name": "exchange-new-york"}, {"name": "exchange-global"}]
    before = {"path_matcher": before_matchers, "fingerprint": "old"}
    rule = {"match_response_codes": ["404"], "path": "/new-york/404.html", "override_response_code": 404}
    policy = {"error_service": "projects/quill-cloud-proxy/global/backendBuckets/token-exchange-static",
              "error_response_rule": [rule]}
    after = {"path_matcher": [dict(before_matchers[0], default_custom_error_response_policy=[policy]),
                              deepcopy(before_matchers[1])]}
    unknown = {"fingerprint": True}
    changes = [{"address": "google_compute_url_map.control", "change": {
        "actions": ["update"], "before": before, "after": after, "after_unknown": unknown,
    }}]
    if variant == "host":
        after["path_matcher"][1]["name"] = "changed"
    elif variant == "other":
        changes.append({"address": "other.resource", "change": {"actions": ["update"]}})
    elif variant == "unknown":
        unknown["default_service"] = True
    elif variant == "status":
        rule["override_response_code"] = 200
    elif variant == "path":
        rule["path"] = "/404.html"
    elif variant == "bucket":
        policy["error_service"] = "wrong"
    result = subprocess.run(  # noqa: S603 -- repository script with fixture input
        [sys.executable, str(ROOT / "infra/tools/check_nyte_404_plan.py")],
        input=json.dumps({"format_version": "1.2", "resource_changes": changes}), text=True, capture_output=True,
    )
    assert (result.returncode == 0) == (variant == "valid")
