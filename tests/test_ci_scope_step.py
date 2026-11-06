"""ci.yml's scope step, run verbatim against local repositories and a fake gh.

The step decides whether a commit may skip the jobs that read no catalog
data. These tests drive the committed shell with real git (file transport
only) and tests/fixtures/ci_scope_gh.sh, and read the data_only output.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from tests.test_refresh_workflow_dispatch import publication_prerequisites

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0" * 40
SNAPSHOT = "src/trusted_router/data/openrouter_snapshot.json"
MANIFEST = "src/trusted_router/data/provider_models/fireworks.json"
CARD = "src/trusted_router/static/og/providers/fireworks.png"
GO = "fastpath/internal/store/ids.go"
TS = "frontend/src/dashboard.ts"


def scope_script() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(step for step in workflow["jobs"]["scope"]["steps"] if step.get("id") == "scope")
    return step["run"]


def run_record(sha: str, *, conclusion: str | None = "success", status: str = "completed",
               event: str = "push") -> dict[str, object]:
    return {"head_sha": sha, "status": status, "conclusion": conclusion, "event": event}


@dataclass
class ScopeRepo:
    root: Path
    env: dict[str, str]
    script: str
    shas: dict[str, str] = field(default_factory=dict)

    @property
    def clone(self) -> Path:
        return self.root / "clone"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(  # noqa: S603 -- isolated local repositories; file protocol only
            [self.env["REAL_GIT"], *args], cwd=cwd or self.clone, env=self.env,
            text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, f"git {args}: {result.stdout}{result.stderr}"
        return result.stdout.strip()

    def commit(self, name: str, files: dict[str, str | None] | None = None, *,
               rename: tuple[str, str] | None = None, symlink: tuple[str, str] | None = None) -> str:
        for path, content in (files or {}).items():
            target = self.clone / path
            if content is None:
                self.git("rm", "-q", path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            self.git("add", path)
        if rename:
            (self.clone / rename[1]).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", *rename)
        if symlink:
            link_path, link_target = symlink
            link = self.clone / link_path
            if link.exists():
                self.git("rm", "-q", link_path)
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, link)
            self.git("add", link_path)
        self.git("commit", "-qm", name)
        self.shas[name] = self.git("rev-parse", "HEAD")
        return self.shas[name]

    def push(self, ref: str = "main") -> None:
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{ref}")

    def run(self, *, event: str, head: str, before: str = "", pr_base: str = "",
            runs: list[dict[str, object]] | None = None, mutation: str | None = None) -> str:
        (self.root / "scenario.json").write_text(json.dumps({"runs": runs or []}))
        script = self.script.replace(
            "python3 scripts/ci/commit_scope.py",
            f"python3 {shlex.quote(str(ROOT / 'scripts/ci/commit_scope.py'))}",
        )
        if mutation == "renames":
            assert script.count("git -c core.quotePath=true diff --no-renames --raw") == 1
            script = script.replace("git -c core.quotePath=true diff --no-renames --raw", "git -c core.quotePath=true diff --raw")
        elif mutation is not None:
            raise AssertionError(f"Unknown mutation: {mutation}")
        output = self.root / "github-output"
        output.write_text("")
        env = {**self.env, "GITHUB_SHA": head, "EVENT_NAME": event, "PUSH_BEFORE": before,
               "PR_BASE_SHA": pr_base, "GITHUB_OUTPUT": str(output)}
        (self.root / "scope.sh").write_text(script)
        self.git("checkout", "-q", head)
        result = subprocess.run(  # noqa: S603 -- real workflow step, local git and fake gh only
            [str(self.root / "bin/bash"), "--noprofile", "--norc", str(self.root / "scope.sh")],
            cwd=self.clone, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lines = [line for line in output.read_text().splitlines() if line.startswith("data_only=")]
        assert len(lines) == 1, output.read_text()
        self.last = result
        return lines[0].removeprefix("data_only=")

    @property
    def gh_calls(self) -> list[list[str]]:
        log = self.root / "events.jsonl"
        events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [event[1:] for event in events if event[0] == "gh"]

    def reset_events(self) -> None:
        (self.root / "events.jsonl").write_text("")


@pytest.fixture
def scope_repo(tmp_path: Path) -> ScopeRepo:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = publication_prerequisites()
    git_exec_path = subprocess.run(  # noqa: S603 -- discover installed git locally
        [commands["git"], "--exec-path"], text=True, capture_output=True, check=True, timeout=30,
    ).stdout.strip()
    real_git = Path(git_exec_path) / "git"
    commands["git"] = str(real_git.resolve()) if real_git.is_file() else commands["git"]
    for name in ("bash", "jq", "git"):
        (bin_dir / name).symlink_to(commands[name])
    sed = shutil.which("sed")
    assert sed, "the scope step needs sed on PATH"
    (bin_dir / "sed").symlink_to(sed)
    (bin_dir / "python3").symlink_to(sys.executable)
    gh = bin_dir / "gh"
    fixture = (ROOT / "tests/fixtures/ci_scope_gh.sh").read_text()
    gh.write_text(f"#!{commands['bash']}\n" + fixture.split("\n", 1)[1])
    gh.chmod(0o700)
    shutil.copyfile(ROOT / "tests/fixtures/refresh_publish_events.sh", tmp_path / "record-event.sh")
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {
        "PATH": str(bin_dir), "TMPDIR": str(tmp_path), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TEMPLATE_DIR": str(empty), "GIT_ALLOW_PROTOCOL": "file",
        "GIT_EXEC_PATH": git_exec_path, "REAL_GIT": commands["git"],
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(empty), "GIT_CONFIG_KEY_1": "commit.gpgsign",
        "GIT_CONFIG_VALUE_1": "false", "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GITHUB_REPOSITORY": "fixture/quill-router", "GH_TOKEN": "offline-fixture",  # noqa: S105
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    repo = ScopeRepo(tmp_path, env, scope_script())
    repo.git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    # The step fetches the base by its sha at depth 1, as it does from GitHub.
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=tmp_path / "origin.git")
    repo.git("clone", "-q", f"file://{tmp_path / 'origin.git'}", "clone", cwd=tmp_path)
    repo.commit("A", {SNAPSHOT: '{"v": 1}\n', MANIFEST: "{}\n", CARD: "png\n",
                      GO: "package store\n", TS: "export const x = 1;\n"})
    repo.push()
    repo.reset_events()
    return repo


def test_a_data_only_push_on_a_validated_base_is_data_only(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n', MANIFEST: '{"m": 1}\n'})
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "true"
    assert "changed paths" in scope_repo.last.stdout
    assert any(f"runs?head_sha={a}" in arg for call in scope_repo.gh_calls for arg in call)


@pytest.mark.parametrize("runs", [
    [],
    [run_record("0" * 40)],
    [run_record("A", status="in_progress", conclusion=None)],
    [run_record("A", conclusion="failure")],
    [run_record("A", conclusion="cancelled")],
    [run_record("A", event="pull_request")],
    [run_record("A", status="in_progress", conclusion=None), run_record("A", conclusion="failure")],
])
def test_a_data_only_push_on_an_unvalidated_base_runs_the_complete_matrix(
    scope_repo: ScopeRepo, runs: list[dict[str, object]],
) -> None:
    # The induction: the skipped jobs' inputs were validated at the base only
    # if the base's own tree concluded success in a push or dispatch run.
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    runs = [{**run, "head_sha": a if run["head_sha"] == "A" else run["head_sha"]} for run in runs]
    assert scope_repo.run(event="push", head=c, before=a, runs=runs) == "false"
    assert "has no successful ci.yml run" in scope_repo.last.stdout


def test_inherited_broken_code_on_an_unvalidated_base_is_not_skipped(scope_repo: ScopeRepo) -> None:
    # B breaks the frontend and its run fails; C changes only data on top of
    # B. C must not skip the frontend job that would catch B.
    a = scope_repo.shas["A"]
    b = scope_repo.commit("B", {TS: "const broken = ;\n"})
    scope_repo.push()
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    assert scope_repo.run(event="push", head=c, before=b,
                          runs=[run_record(a), run_record(b, conclusion="failure")]) == "false"
    # A force push of N (B's break plus data) over B: B..N is data-only, but
    # B was never validated, so N runs the complete matrix.
    scope_repo.git("checkout", "-q", a)
    n = scope_repo.commit("N", {TS: "const broken = ;\n", SNAPSHOT: '{"v": 2}\n'})
    scope_repo.git("push", "-q", "--force", "origin", f"{n}:refs/heads/main")
    assert scope_repo.run(event="push", head=n, before=b, runs=[run_record(a)]) == "false"


def test_a_code_change_runs_the_complete_matrix_without_asking_about_the_base(
    scope_repo: ScopeRepo,
) -> None:
    a = scope_repo.shas["A"]
    cases: list[tuple[str, dict[str, str | None]]] = [
        ("C1", {SNAPSHOT: '{"v": 2}\n', GO: "package store // x\n"}),
        ("C2", {"src/trusted_router/static/og/providers/new.js": "x\n"}),
        ("C3", {"scripts/pricing/parsers/fireworks.py": "def parse(h): ...\n"}),
        ("C4", {"fastpath/testdata/model_alias_vectors_v1.json": "[]\n"}),
    ]
    for name, files in cases:
        scope_repo.git("checkout", "-q", a)
        c = scope_repo.commit(name, files)
        scope_repo.reset_events()
        assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false", name
        assert not any("actions/workflows" in arg for call in scope_repo.gh_calls for arg in call)


def test_a_code_file_renamed_onto_a_data_path_is_not_data(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", rename=(GO, "src/trusted_router/static/og/providers/ids.png"))
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false"
    assert f"D\t{GO}" in scope_repo.last.stdout
    assert "R100" not in scope_repo.last.stdout
    # Two guards. With rename detection the diff reports one R100 record
    # naming the destination (with --name-only it would have been the bare
    # destination path, and the commit would have skipped nine jobs); the
    # classifier rejects a rename record too, so the mutant still runs the
    # complete matrix. --no-renames keeps the step's log an honest deletion.
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)],
                          mutation="renames") == "false"
    assert f"R100\t{GO}\t" in scope_repo.last.stdout


def test_a_symlink_or_executable_at_a_data_path_is_not_data(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", symlink=(SNAPSHOT, "../../../fastpath/internal/store/ids.go"))
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false"
    scope_repo.git("checkout", "-q", a)
    scope_repo.commit("C2", {MANIFEST: '{"m": 2}\n'})
    scope_repo.git("update-index", "--chmod=+x", MANIFEST)
    scope_repo.git("commit", "-qm", "C2 executable")
    c2 = scope_repo.git("rev-parse", "HEAD")
    assert scope_repo.run(event="push", head=c2, before=a, runs=[run_record(a)]) == "false"


def test_a_deleted_data_file_is_data(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {MANIFEST: None})
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "true"


@pytest.mark.parametrize("before", ["", ZERO])
def test_a_push_without_a_base_runs_the_complete_matrix(scope_repo: ScopeRepo, before: str) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    assert scope_repo.run(event="push", head=c, before=before, runs=[run_record(a)]) == "false"
    assert "no base commit" in scope_repo.last.stdout
    assert scope_repo.gh_calls == []


def test_an_unfetchable_base_runs_the_complete_matrix(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    missing = "deadbeef" * 5
    assert scope_repo.run(event="push", head=c, before=missing, runs=[run_record(a)]) == "false"
    assert "is not fetchable" in scope_repo.last.stdout
    assert scope_repo.gh_calls == []


def test_a_pull_request_compares_its_base_with_the_checked_out_merge(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)]) == "true"
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[]) == "false"
    assert not any("compare" in arg for call in scope_repo.gh_calls for arg in call)


def test_a_dispatch_compares_with_the_merge_base_of_main(scope_repo: ScopeRepo) -> None:
    # refresh-prices.yml dispatches a branch of main plus one generated commit.
    a = scope_repo.shas["A"]
    d = scope_repo.commit("D", {SNAPSHOT: '{"v": 2}\n', CARD: "png2\n"})
    scope_repo.push("automation/price-refresh-42")
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[run_record(a)]) == "true"
    assert any(f"compare/main...{d}" in arg for call in scope_repo.gh_calls for arg in call)
    # A dispatch run for the base commit counts: bot commits reach main
    # without a push-triggered run and carry only their dispatch run.
    assert scope_repo.run(event="workflow_dispatch", head=d,
                          runs=[run_record(a, event="workflow_dispatch")]) == "true"
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[]) == "false"
    # Main moved under the branch: the merge base is still A, the diff is
    # still D's own, and A's validation still holds.
    scope_repo.git("checkout", "-q", a)
    scope_repo.commit("E", {GO: "package store // e\n"})
    scope_repo.push()
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[run_record(a)]) == "true"
    # A dispatch on main's own tip is its own base: nothing to compare.
    e = scope_repo.shas["E"]
    assert scope_repo.run(event="workflow_dispatch", head=e, runs=[run_record(a), run_record(e)]) == "false"
    assert "is its own base" in scope_repo.last.stdout
    # A dispatched branch carrying unmerged code and a final data commit.
    scope_repo.git("checkout", "-q", a)
    scope_repo.commit("F", {GO: "package store // f\n"})
    g = scope_repo.commit("G", {SNAPSHOT: '{"v": 3}\n'})
    scope_repo.push("feature")
    assert scope_repo.run(event="workflow_dispatch", head=g, runs=[run_record(a)]) == "false"
