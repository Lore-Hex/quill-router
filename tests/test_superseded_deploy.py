"""A superseded deploy ships when main moved only outside the deploy paths."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"


def _module():
    spec = importlib.util.spec_from_file_location(
        "superseded_deploy", ROOT / "scripts/deploy/superseded_deploy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


superseded = _module()


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_script_and_the_trigger_list_the_same_deploy_paths() -> None:
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    trigger = workflow.get("on", workflow.get(True))
    assert tuple(trigger["push"]["paths"]) == superseded.DEPLOY_PATHS


def test_every_image_input_is_a_deploy_path() -> None:
    # A change to anything the image copies must start a deploy; otherwise a
    # superseded run would ship an image main no longer matches.
    sources: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == "COPY" and not any(part.startswith("--from") for part in parts):
            sources.extend(parts[1:-1])
    assert "src" in sources and "scripts/entrypoint.sh" in sources
    for source in sources:
        probe = source if (ROOT / source).is_file() else f"{source}/probe"
        assert superseded.touches_deploy_paths([probe]), source


@pytest.mark.parametrize(
    ("files", "relevant"),
    [
        (["docs/design/x.md"], False),
        (["tests/test_x.py", "clickhouse/023_x.sql"], False),
        (["src/trusted_router/main.py"], True),
        (["scripts/deploy/rollout.sh"], True),
        (["scripts/entrypoint.sh"], True),
        (["scripts/other_tool.sh"], False),
        (["frontend/src/app.ts"], True),
        (["frontend/package.json"], False),
        (["Dockerfile"], True),
        (["uv.lock"], True),
        ([".github/workflows/deploy.yml"], True),
        ([".github/workflows/ci.yml"], False),
        (["srcx/file.py"], False),
    ],
)
def test_touches_deploy_paths(files: list[str], relevant: bool) -> None:
    assert superseded.touches_deploy_paths(files) is relevant


def test_a_rename_counts_both_paths() -> None:
    compare = {
        "status": "ahead",
        "files": [{"filename": "docs/moved.py", "previous_filename": "src/trusted_router/moved.py"}],
    }
    assert superseded.changed_files(compare) == ["docs/moved.py", "src/trusted_router/moved.py"]
    assert superseded.may_deploy(compare) is False


@pytest.mark.parametrize(
    "compare",
    [
        {"status": "diverged", "files": [{"filename": "docs/x.md"}]},
        {"status": "behind", "files": []},
        {"status": "identical", "files": []},
        {"status": "ahead"},
        {"status": "ahead", "files": [{"filename": f"docs/{i}.md"} for i in range(300)]},
    ],
    ids=["diverged", "behind", "identical", "no-file-list", "truncated"],
)
def test_an_uncertain_comparison_never_deploys(compare: dict) -> None:
    assert superseded.changed_files(compare) is None
    assert superseded.may_deploy(compare) is False


def test_docs_only_changes_deploy() -> None:
    compare = {"status": "ahead", "files": [{"filename": "docs/a.md"}, {"filename": "tests/test_a.py"}]}
    assert superseded.may_deploy(compare) is True


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stdout: str, exit_code: int = 0) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (tmp_path / "gh.out").write_text(stdout)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'cat "{tmp_path / "gh.out"}"\n'
        f"exit {exit_code}\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_main_deploys_when_only_docs_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_gh(
        tmp_path, monkeypatch, stdout=json.dumps({"status": "ahead", "files": [{"filename": "docs/a.md"}]})
    )
    assert superseded.main(["--repo", "Lore-Hex/quill-router", "--base", "aaa", "--head", "bbb"]) == 0
    assert log.read_text().strip() == "api repos/Lore-Hex/quill-router/compare/aaa...bbb"


def test_main_skips_when_a_newer_commit_touches_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(
        tmp_path,
        monkeypatch,
        stdout=json.dumps({"status": "ahead", "files": [{"filename": "src/trusted_router/x.py"}]}),
    )
    assert superseded.main(["--repo", "o/r", "--base", "aaa", "--head", "bbb"]) == 1


@pytest.mark.parametrize(("stdout", "exit_code"), [("", 1), ("not json", 0)], ids=["gh-fails", "garbage"])
def test_main_skips_when_the_comparison_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, exit_code: int
) -> None:
    _fake_gh(tmp_path, monkeypatch, stdout=stdout, exit_code=exit_code)
    assert superseded.main(["--repo", "o/r", "--base", "aaa", "--head", "bbb"]) == 1


def test_the_workflow_asks_the_script_before_skipping() -> None:
    job = _workflow()["jobs"]["confirm-current-main"]
    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["sparse-checkout"] == "scripts/deploy/superseded_deploy.py"
    run = job["steps"][1]["run"]
    current = run.index('if [ "${latest_sha}" = "${GITHUB_SHA}" ]')
    script = run.index("python3 scripts/deploy/superseded_deploy.py")
    skip = run.index('echo "proceed=false" >> "$GITHUB_OUTPUT"')
    assert current < script < skip
    assert '--base "${GITHUB_SHA}" --head "${latest_sha}"' in run


def test_the_admit_wait_outlasts_a_gateway_rollout() -> None:
    job = _workflow()["jobs"]["admit-cloud"]
    acquire = next(step for step in job["steps"] if step.get("id") == "acquire_mutex")
    wait = int(acquire["env"]["TR_DEPLOY_WAIT_SECONDS"])
    # Gateway rollouts held the gcp lease for about 2 h 20 min (2026-10-01/02).
    assert wait >= 3 * 3600
    # The job must not time out before the wait does, and the lease must last
    # longer than the job that holds it.
    assert job["timeout-minutes"] * 60 > wait
    assert int(acquire["env"]["TR_DEPLOY_MUTEX_TTL_SECONDS"]) > job["timeout-minutes"] * 60
