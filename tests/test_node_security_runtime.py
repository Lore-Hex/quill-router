"""Keep build tooling on a patched, reproducible Node.js runtime."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SECURITY_FLOOR = (24, 22, 1)


def test_node_pin_includes_october_2026_security_fixes() -> None:
    version = tuple(int(part) for part in (ROOT / ".node-version").read_text().strip().split("."))
    assert len(version) == 3
    assert version[0] == 24
    assert version >= SECURITY_FLOOR


def test_every_node_workflow_uses_the_shared_patched_version() -> None:
    found = 0
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("actions/setup-node@"):
                    continue
                found += 1
                assert step["with"].get("node-version-file") == ".node-version", path
                assert "node-version" not in step["with"], path
    assert found >= 4


def test_lightning_build_image_includes_october_2026_security_fixes() -> None:
    image = (ROOT / "experiments/lightning_router/Dockerfile").read_text().splitlines()[0].split()[1]
    assert image.startswith("node:")
    version = tuple(int(part) for part in image.removeprefix("node:").split("-")[0].split("."))
    assert len(version) == 3
    assert version[0] == 24
    assert version >= SECURITY_FLOOR


def test_runtime_updates_trigger_all_consuming_workflows() -> None:
    for name in ("lightning-experiment", "token-exchange-sites"):
        workflow = yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
        triggers = workflow.get("on", workflow.get(True))
        for event in ("push", "pull_request"):
            assert ".node-version" in triggers[event]["paths"]
