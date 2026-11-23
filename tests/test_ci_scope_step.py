"""ci.yml's scope step, run verbatim against local repositories and a fake gh.

The step decides whether a commit may skip the jobs that read no catalog
data, and which path-scoped jobs run. These tests drive the committed shell
with real git (file transport only) and tests/fixtures/ci_scope_gh.sh, and
read the data_only output and the path-scoped flags.
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
DOC = "docs/notes.md"
SPEC_CODE = "fastpath/internal/spec/spec.go"
MANIFEST_TOML = '[Spec]\nstate = "implemented"\ncode = ["fastpath/internal/spec"]\ntests = ["tests/test_spec.py"]\n'
LIFECYCLE = (
    "from datetime import UTC, datetime\n"
    "_RETIREMENTS = (\n"
    "    _Retirement(provider='nextcloud', model_ids=frozenset({'vendor/soon-1'}), upstream_ids=frozenset(),\n"
    "                effective_at=datetime(2099, 1, 1, tzinfo=UTC)),\n"
    ")\n"
)
ALL = {"run_fastpath": True, "run_heavy": True, "run_post_cutover": True, "run_proof_oracle": True,
       "run_proofs": True}
NONE = dict.fromkeys(ALL, False)
# Catalog data: the post-cutover suite, and the heavy lane (all of src/).
CATALOG = NONE | {"run_heavy": True, "run_post_cutover": True}


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
            runs: list[dict[str, object]] | None = None, mutation: str | None = None,
            dispatch_scope: str = "paths", labels: list[str] | None = None,
            labels_error: bool = False) -> str:
        """data_only; the path-scoped flags are in self.flags afterwards.
        dispatch_scope defaults to the price refresh's scope=paths; labels
        are the pull request's labels when the step runs."""
        (self.root / "scenario.json").write_text(json.dumps(
            {"runs": runs or [], "labels": labels or [], "labels_error": labels_error}))
        script = self.script.replace(
            "python3 scripts/ci/commit_scope.py",
            f"python3 {shlex.quote(str(ROOT / 'scripts/ci/commit_scope.py'))}",
        )
        if mutation == "renames":
            assert script.count("git -c core.quotePath=true diff --no-renames --raw") == 2
            script = script.replace("git -c core.quotePath=true diff --no-renames --raw", "git -c core.quotePath=true diff --raw")
        elif mutation == "no_text":
            # The patch without --text: git shows a -diff file as binary.
            assert script.count(" --text ") == 1
            script = script.replace(" --text ", " ")
        elif mutation == "no_widening":
            # REF is always the base, validated or not.
            widening = ('if [ "${EVENT_NAME}" = pull_request ] || [ "${base_validated}" = true ] '
                        '|| [ "${base_pending}" = true ]; then\n    ref="${base}"\n  else')
            assert script.count(widening) == 1
            script = script.replace(widening, 'if true; then\n    ref="${base}"\n  else')
        elif mutation is not None:
            raise AssertionError(f"Unknown mutation: {mutation}")
        output = self.root / "github-output"
        output.write_text("")
        env = {**self.env, "GITHUB_SHA": head, "EVENT_NAME": event, "PUSH_BEFORE": before,
               "PR_BASE_SHA": pr_base, "GITHUB_OUTPUT": str(output),
               "DISPATCH_SCOPE": dispatch_scope if event == "workflow_dispatch" else "",
               "PR_NUMBER": "42" if event == "pull_request" else ""}
        (self.root / "scope.sh").write_text(script)
        self.git("checkout", "-q", head)
        result = subprocess.run(  # noqa: S603 -- real workflow step, local git and fake gh only
            [str(self.root / "bin/bash"), "--noprofile", "--norc", str(self.root / "scope.sh")],
            cwd=self.clone, env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        lines = [line for line in output.read_text().splitlines() if line.startswith("data_only=")]
        assert len(lines) == 1, output.read_text()
        flags = dict(line.split("=", 1) for line in output.read_text().splitlines() if line.startswith("run_"))
        assert set(flags) == set(ALL) and set(flags.values()) <= {"true", "false"}, output.read_text()
        self.flags = {flag: value == "true" for flag, value in flags.items()}
        tests = [line for line in output.read_text().splitlines() if line.startswith("post_cutover_tests=")]
        assert len(tests) == 1, output.read_text()
        self.post_cutover_tests = tests[0].removeprefix("post_cutover_tests=").split()
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
    for name in ("sed", "tee", "grep"):
        tool = shutil.which(name)
        assert tool, f"the scope step needs {name} on PATH"
        (bin_dir / name).symlink_to(tool)
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
                      GO: "package store\n", TS: "export const x = 1;\n",
                      "proofs/manifest.toml": MANIFEST_TOML, SPEC_CODE: "package spec\n",
                      "src/trusted_router/provider_lifecycle.py": LIFECYCLE, "tests/conftest.py": "",
                      DOC: "notes\n",
                      **{f"tests/filler/test_filler_{n}.py": "def test_filler():\n    pass\n" for n in range(16)}})
    repo.push()
    repo.reset_events()
    return repo


def test_a_data_only_push_on_a_validated_base_is_data_only(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n', MANIFEST: '{"m": 1}\n'})
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "true"
    assert "changed paths" in scope_repo.last.stdout
    assert any(f"runs?head_sha={a}" in arg for call in scope_repo.gh_calls for arg in call)
    # Catalog data runs the post-cutover suite and the heavy lane, nothing else path-scoped.
    assert scope_repo.flags == CATALOG


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


def output_value(repo: ScopeRepo, name: str) -> str:
    lines = [line for line in (repo.root / "github-output").read_text().splitlines()
             if line.startswith(f"{name}=")]
    assert len(lines) == 1, lines
    return lines[0].split("=", 1)[1]


def test_a_refresh_dispatch_on_a_base_whose_push_run_is_running_takes_the_data_only_skips(
    scope_repo: ScopeRepo,
) -> None:
    # The price refresh carries its snapshot onto a main commit merged
    # minutes earlier. Its skips stand on that base, and base-ci makes the run
    # wait for the base's own push run to succeed (2026-11-20: without this
    # the snapshot ran the complete matrix, 45-60 minutes, and lost the race).
    a = scope_repo.shas["A"]
    b = scope_repo.commit("B", {DOC: "notes 2\n"})
    scope_repo.push()
    d = scope_repo.commit("D", {SNAPSHOT: '{"v": 2}\n'})
    scope_repo.push("automation/price-refresh-42")
    running = [run_record(a), run_record(b, status="in_progress", conclusion=None)]
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=running) == "true"
    assert output_value(scope_repo, "base_pending") == "true"
    assert output_value(scope_repo, "base") == b
    # The path-scoped jobs compare with the base itself, which base-ci checks.
    assert f"({b}..{d})" in scope_repo.last.stdout
    assert scope_repo.flags == CATALOG
    # A validated base needs no wait.
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[run_record(b)]) == "true"
    assert output_value(scope_repo, "base_pending") == "false"


@pytest.mark.parametrize(("event", "runs"), [
    # A push never waits: its run is the one main's deploy gate reads.
    ("push", [run_record("B", status="in_progress", conclusion=None)]),
    # Only the base's own push run counts as running.
    ("workflow_dispatch", [run_record("B", status="in_progress", conclusion=None, event="workflow_dispatch")]),
    ("workflow_dispatch", [run_record("B", status="in_progress", conclusion=None, event="pull_request")]),
    # Concluded without success: nothing to wait for.
    ("workflow_dispatch", [run_record("B", conclusion="failure")]),
])
def test_no_data_only_skip_without_a_running_or_validated_base(
    scope_repo: ScopeRepo, event: str, runs: list[dict[str, object]],
) -> None:
    b = scope_repo.commit("B", {DOC: "notes 2\n"})
    scope_repo.push()
    d = scope_repo.commit("D", {SNAPSHOT: '{"v": 2}\n'})
    scope_repo.push("automation/price-refresh-42")
    runs = [{**run, "head_sha": b} for run in runs]
    before = b if event == "push" else ""
    assert scope_repo.run(event=event, head=d, before=before, runs=runs) == "false"
    assert output_value(scope_repo, "base_pending") == "false"


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


def test_a_code_change_is_not_data_only_and_runs_the_jobs_that_read_it(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    cases: list[tuple[str, dict[str, str | None], dict[str, bool]]] = [
        ("C1", {SNAPSHOT: '{"v": 2}\n', GO: "package store // x\n"},
         CATALOG | {"run_fastpath": True}),
        ("C2", {"src/trusted_router/static/og/providers/new.js": "x\n"},
         CATALOG | {"run_proof_oracle": True}),
        ("C3", {"scripts/pricing/parsers/fireworks.py": "def parse(h): ...\n"}, NONE | {"run_post_cutover": True}),
        ("C4", {"fastpath/testdata/model_alias_vectors_v1.json": "[]\n"},
         NONE | {"run_fastpath": True, "run_post_cutover": True}),
        ("C5", {SPEC_CODE: "package spec // x\n"}, NONE | {"run_fastpath": True, "run_proofs": True}),
        # Go naming a scheduled model: the post-cutover suite too.
        ("C8", {SPEC_CODE: "package spec // vendor/soon-1\n"},
         NONE | {"run_fastpath": True, "run_proofs": True, "run_post_cutover": True}),
        # A data file a test could load expectations from.
        ("C9", {"docs/catalog_expectations.json": '{"models": 679}\n'}, NONE | {"run_post_cutover": True}),
        ("C6", {DOC: "notes about vendor/soon-1\n"}, NONE | {"run_post_cutover": True}),
        ("C7", {DOC: "notes about latency\n"}, NONE),
    ]
    for name, files, flags in cases:
        scope_repo.git("checkout", "-q", a)
        c = scope_repo.commit(name, files)
        scope_repo.reset_events()
        assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false", name
        assert scope_repo.flags == flags, name
        # The base passed, so it is REF: no listing of other runs.
        assert not any("status=success" in arg for call in scope_repo.gh_calls for arg in call), name


def test_a_code_file_renamed_onto_a_data_path_is_not_data(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", rename=(GO, "src/trusted_router/static/og/providers/ids.png"))
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false"
    assert f"D\t{GO}" in scope_repo.last.stdout
    assert "R100" not in scope_repo.last.stdout
    assert scope_repo.flags["run_fastpath"]
    # Two guards. With rename detection the diff reports one R100 record
    # naming the destination (with --name-only it would have been the bare
    # destination path, and the commit would have skipped nine jobs); the
    # classifier rejects a rename record too, so the mutant still runs the
    # complete matrix. --no-renames keeps the step's log an honest deletion.
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)],
                          mutation="renames") == "false"
    assert f"R100\t{GO}\t" in scope_repo.last.stdout
    # A rename record runs every path-scoped job.
    assert scope_repo.flags == ALL


def test_a_symlink_or_executable_at_a_data_path_is_not_data(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", symlink=(SNAPSHOT, "../../../fastpath/internal/store/ids.go"))
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false"
    assert scope_repo.flags == ALL
    scope_repo.git("checkout", "-q", a)
    scope_repo.commit("C2", {MANIFEST: '{"m": 2}\n'})
    scope_repo.git("update-index", "--chmod=+x", MANIFEST)
    scope_repo.git("commit", "-qm", "C2 executable")
    c2 = scope_repo.git("rev-parse", "HEAD")
    assert scope_repo.run(event="push", head=c2, before=a, runs=[run_record(a)]) == "false"
    # A mode change runs every path-scoped job.
    assert scope_repo.flags == ALL


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
    assert scope_repo.flags == ALL


def test_an_unfetchable_base_runs_the_complete_matrix(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    missing = "deadbeef" * 5
    assert scope_repo.run(event="push", head=c, before=missing, runs=[run_record(a)]) == "false"
    assert "is not fetchable" in scope_repo.last.stdout
    assert scope_repo.gh_calls == []
    assert scope_repo.flags == ALL


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
    assert scope_repo.flags == ALL
    # A dispatched branch carrying unmerged code and a final data commit.
    scope_repo.git("checkout", "-q", a)
    scope_repo.commit("F", {GO: "package store // f\n"})
    g = scope_repo.commit("G", {SNAPSHOT: '{"v": 3}\n'})
    scope_repo.push("feature")
    assert scope_repo.run(event="workflow_dispatch", head=g, runs=[run_record(a)]) == "false"


def test_a_red_base_widens_the_comparison_to_its_newest_validated_ancestor(scope_repo: ScopeRepo) -> None:
    # B changes the fast path and its run fails; C changes a document on top
    # of B. Compared with B alone, C would skip the fast path jobs and go
    # green over B's failure. Compared with A, B's newest validated
    # ancestor, C runs them until a run passes them.
    a = scope_repo.shas["A"]
    b = scope_repo.commit("B", {GO: "package store // broken\n"})
    scope_repo.push()
    c = scope_repo.commit("C", {DOC: "more notes\n"})
    red = [run_record(b, conclusion="failure"), run_record(a)]
    widened = NONE | {"run_fastpath": True}
    assert scope_repo.run(event="push", head=c, before=b, runs=red) == "false"
    assert scope_repo.flags == widened
    assert f"compare with {a}" in scope_repo.last.stdout
    assert any(f"compare/{a}...{b}" in arg for call in scope_repo.gh_calls for arg in call)
    # A base still running widens the same way.
    running = [run_record(b, status="in_progress", conclusion=None), run_record(a)]
    assert scope_repo.run(event="push", head=c, before=b, runs=running) == "false"
    assert scope_repo.flags == widened
    # Once B's own run passed, C is compared with B and skips them.
    assert scope_repo.run(event="push", head=c, before=b, runs=[run_record(b), run_record(a)]) == "false"
    assert scope_repo.flags == NONE
    # The widening is what catches it: compared with the red base, C skips.
    assert scope_repo.run(event="push", head=c, before=b, runs=red, mutation="no_widening") == "false"
    assert scope_repo.flags == NONE


def test_a_validated_commit_that_is_not_an_ancestor_is_passed_over(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    x = scope_repo.commit("X", {GO: "package store // side branch\n"})
    scope_repo.push("feature")
    scope_repo.git("checkout", "-q", a)
    b = scope_repo.commit("B", {DOC: "b\n"})
    scope_repo.push()
    c = scope_repo.commit("C", {DOC: "c\n"})
    # Newest first: the side branch's commit, then A.
    runs = [run_record(x, event="workflow_dispatch"), run_record(a)]
    assert scope_repo.run(event="push", head=c, before=b, runs=runs) == "false"
    assert f"compare with {a}" in scope_repo.last.stdout
    assert scope_repo.flags == NONE
    # A pull_request run never validates a commit.
    runs = [run_record(b, event="pull_request"), run_record(a)]
    assert scope_repo.run(event="push", head=c, before=b, runs=runs) == "false"
    assert f"compare with {a}" in scope_repo.last.stdout


@pytest.mark.parametrize("runs", [[], [run_record("0" * 40, conclusion="failure")]])
def test_no_validated_ancestor_runs_every_path_scoped_job(scope_repo: ScopeRepo,
                                                         runs: list[dict[str, object]]) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {DOC: "c\n"})
    assert scope_repo.run(event="push", head=c, before=a, runs=runs) == "false"
    assert scope_repo.flags == ALL
    assert "no recent successful ci.yml run is on an ancestor" in scope_repo.last.stdout


@pytest.mark.parametrize("dispatch_scope", ["", "full", "FULL", "path"])
def test_a_dispatch_without_scope_paths_runs_the_complete_matrix(scope_repo: ScopeRepo,
                                                                 dispatch_scope: str) -> None:
    # A data-only commit on a validated base: with scope=paths (the price
    # refresh) it skips the data-only jobs; any other dispatch runs them all.
    a = scope_repo.shas["A"]
    d = scope_repo.commit("D", {SNAPSHOT: '{"v": 2}\n'})
    scope_repo.push("automation/price-refresh-42")
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[run_record(a)], dispatch_scope="paths") == "true"
    assert scope_repo.flags == CATALOG
    assert scope_repo.run(event="workflow_dispatch", head=d, runs=[run_record(a)],
                          dispatch_scope=dispatch_scope) == "false"
    assert scope_repo.flags == ALL
    assert "unless it asks for scope=paths" in scope_repo.last.stdout


def test_the_ci_full_label_runs_the_complete_matrix(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {SNAPSHOT: '{"v": 2}\n'})
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)]) == "true"
    assert scope_repo.flags == CATALOG
    assert any("issues/42/labels" in arg for call in scope_repo.gh_calls for arg in call)
    # Another label changes nothing; ci:full, among others, runs everything,
    # the data-only skips included.
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)],
                          labels=["bug", "ci:fullish", "full"]) == "true"
    assert scope_repo.flags == CATALOG
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)],
                          labels=["bug", "ci:full"]) == "false"
    assert scope_repo.flags == ALL
    assert "labeled ci:full" in scope_repo.last.stdout


def test_a_rerun_reads_the_labels_as_they_are_now(scope_repo: ScopeRepo) -> None:
    # A re-run keeps the original event payload, so the step asks the API for
    # the labels each time it runs: the same event and commits, run again
    # after the label is added, run the complete matrix.
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {DOC: "notes for review\n"})
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)]) == "false"
    assert scope_repo.flags == NONE
    scope_repo.reset_events()
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)],
                          labels=["ci:full"]) == "false"
    assert scope_repo.flags == ALL
    assert [call for call in scope_repo.gh_calls if any("labels" in arg for arg in call)]


def test_unreadable_labels_run_the_complete_matrix(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {DOC: "notes for review\n"})
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)], labels_error=True) == "false"
    assert scope_repo.flags == ALL
    assert "labels could not be read" in scope_repo.last.stdout


def test_only_a_pull_request_reads_labels(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    c = scope_repo.commit("C", {DOC: "notes for review\n"})
    scope_repo.reset_events()
    assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)], labels=["ci:full"]) == "false"
    assert scope_repo.flags == NONE
    assert not any("labels" in arg for call in scope_repo.gh_calls for arg in call)


def test_a_change_to_how_ci_decides_runs_every_path_scoped_job(scope_repo: ScopeRepo) -> None:
    a = scope_repo.shas["A"]
    for name, path in (("W", ".github/workflows/ci.yml"), ("S", "scripts/ci/commit_scope.py")):
        scope_repo.git("checkout", "-q", a)
        c = scope_repo.commit(name, {path: "x\n"})
        assert scope_repo.run(event="push", head=c, before=a, runs=[run_record(a)]) == "false"
        assert scope_repo.flags == ALL, path


def test_a_pull_request_compares_with_its_base_validated_or_not(scope_repo: ScopeRepo) -> None:
    # Nothing ships from a pull request's run, and what it skips it did not
    # change; the push that merges it compares with the newest validated
    # commit. So a pull request never widens, and never asks for other runs.
    a = scope_repo.shas["A"]
    b = scope_repo.commit("B", {GO: "package store // red on main\n"})
    scope_repo.push()
    c = scope_repo.commit("C", {DOC: "pull request\n"})
    for runs in ([], [run_record(b, conclusion="failure"), run_record(a)]):
        scope_repo.reset_events()
        assert scope_repo.run(event="pull_request", head=c, pr_base=b, runs=runs) == "false"
        assert scope_repo.flags == NONE
        assert not any("status=success" in arg or "compare/" in arg
                       for call in scope_repo.gh_calls for arg in call)
    # The push of the same change does widen, and runs the fast path jobs.
    assert scope_repo.run(event="push", head=c, before=b, runs=[run_record(b, conclusion="failure"),
                                                               run_record(a)]) == "false"
    assert scope_repo.flags == NONE | {"run_fastpath": True}


def test_a_test_only_change_runs_the_post_cutover_suite_on_the_tests_it_reaches(scope_repo: ScopeRepo) -> None:
    # A new test that depends on the post-cutover catalog and names nothing
    # scheduled (the review's model count): the suite runs on that file.
    a = scope_repo.shas["A"]
    path = "tests/test_catalog_model_count.py"
    c = scope_repo.commit("C", {path: "from trusted_router.catalog_registry import MODELS\n\n\n"
                                      "def test_count():\n    assert len(MODELS) == 679\n"})
    assert scope_repo.run(event="pull_request", head=c, pr_base=a, runs=[run_record(a)]) == "false"
    assert scope_repo.flags == NONE | {"run_post_cutover": True}
    assert scope_repo.post_cutover_tests == [path]
    # The whole suite has no list.
    scope_repo.git("checkout", "-q", a)
    d = scope_repo.commit("D", {"src/trusted_router/x.py": "X = 1\n"})
    assert scope_repo.run(event="pull_request", head=d, pr_base=a, runs=[run_record(a)]) == "false"
    assert scope_repo.flags["run_post_cutover"] and scope_repo.post_cutover_tests == []


def test_a_file_gitattributes_marks_binary_still_shows_its_lines(scope_repo: ScopeRepo) -> None:
    # B marks Markdown as binary for diffs; C changes a document that names a
    # scheduled model. --text shows the line, which names it. (Any other
    # data file runs the suite whatever its lines say.)
    b = scope_repo.commit("B", {".gitattributes": "*.md -diff\n"})
    scope_repo.push()
    c = scope_repo.commit("C", {"docs/models.md": "vendor/soon-1\n"})
    assert scope_repo.run(event="push", head=c, before=b, runs=[run_record(b)]) == "false"
    assert scope_repo.flags == NONE | {"run_post_cutover": True}
    assert "docs/models.md names 'vendor/soon-1'" in scope_repo.last.stderr
    # A document naming nothing scheduled, read through --text, runs nothing.
    scope_repo.git("checkout", "-q", b)
    quiet = scope_repo.commit("Q", {"docs/models.md": "latency notes\n"})
    assert scope_repo.run(event="push", head=quiet, before=b, runs=[run_record(b)]) == "false"
    assert scope_repo.flags == NONE
    # Without --text the patch shows no lines for it, and that alone runs
    # the whole suite.
    assert scope_repo.run(event="push", head=c, before=b, runs=[run_record(b)], mutation="no_text") == "false"
    assert scope_repo.flags == NONE | {"run_post_cutover": True}
    assert "docs/models.md has no lines the diff shows" in scope_repo.last.stderr
    assert scope_repo.post_cutover_tests == []
    # A change to .gitattributes itself runs the complete matrix.
    scope_repo.git("checkout", "-q", b)
    d = scope_repo.commit("D", {".gitattributes": "*.md -diff\n*.dat -diff\n"})
    assert scope_repo.run(event="push", head=d, before=b, runs=[run_record(b)]) == "false"
    assert scope_repo.flags == ALL
