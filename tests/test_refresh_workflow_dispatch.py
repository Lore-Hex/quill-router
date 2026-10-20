import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# External commands in the extracted step. Shell builtins/functions (echo,
# test/[ /[[, trap, cleanup_branch, etc.) need no executable. The allowlisted
# PATH also makes a newly introduced, unlisted command fail rather than escape.
PUBLICATION_COMMANDS = {
    "bash": "real", "git": "wrapped", "jq": "real", "cat": "real",
    "date": "real", "seq": "real", "mktemp": "real", "gh": "fake", "sleep": "fake",
}


def test_routing_evidence_refresh_uses_catalog_publication_gates():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = workflow["jobs"]["refresh"]["steps"]
    evidence = next(step for step in steps if step.get("id") == "routing_evidence")
    publication = next(step for step in steps if step.get("name") == "Commit, verify, and push if changed")
    assert evidence["continue-on-error"] is True
    assert "update_provider_throughput_rank.py --write --min-age-hours 24" in evidence["run"]
    assert steps.index(evidence) < steps.index(publication)
    assert "src/trusted_router/data/provider_routing_evidence.json" in publication["run"]
    assert any(step.get("if") == "steps.routing_evidence.outcome == 'failure'"
               and "GITHUB_STEP_SUMMARY" in step.get("run", "") for step in steps)


def publication_prerequisites() -> dict[str, str]:
    commands = {
        name: shutil.which(name) for name, kind in PUBLICATION_COMMANDS.items() if kind != "fake"
    }
    missing = [name for name, path in commands.items() if path is None]
    if missing:
        reason = f"Price-refresh publication tests require on PATH: {', '.join(missing)}"
        if any(os.environ.get(name, "").lower() not in {"", "0", "false"}
               for name in ("GITHUB_ACTIONS", "CI")):
            pytest.fail(reason)
        pytest.skip(reason)
    return {name: path for name, path in commands.items() if path is not None}


@pytest.fixture
def publication_script() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = [
        step for job in workflow["jobs"].values() for step in job.get("steps", [])
        if step.get("name") == "Commit, verify, and push if changed"
    ]
    assert len(steps) == 1, "Expected exactly one price-refresh publication step"
    script = steps[0].get("run")
    assert isinstance(script, str) and script.strip(), "Publication step has no run script"
    return script


def remove_publication_guard(script: str, guard: str) -> str:
    """Negative controls change only the in-memory script, never the workflow."""
    if guard == "conclusion":
        before, after = 'if [ "${conclusion}" = "success" ]; then', "if true; then"
    elif guard in {"completion", "accept_pending"}:
        before, after = 'if [ "${status}" = "completed" ]; then', "if true; then"
        if guard == "accept_pending":
            script = remove_publication_guard(script, "conclusion")
    elif guard == "late_bridge":
        start = script.index('ci_url="${GITHUB_SERVER_URL}')
        end = script.index("push_log=$(mktemp)", start)
        bridge = script[start:end]
        script = script[:start] + script[end:]
        after_push = script.index("# GITHUB_TOKEN pushes do not start")
        return script[:after_push] + bridge + script[after_push:]
    elif guard == "head_sha":
        before, after = r'map(select(.headSha == \"${SNAPSHOT_SHA}\"))', "."
    elif guard == "main_moved":
        # Only disable the pre-push guard; retain the push-race recovery guard.
        before = 'if [ "$(git rev-parse origin/main)" != "${GITHUB_SHA}" ]; then'
        after = "if false; then"
        assert script.count(before) == 2
        return script.replace(before, after, 1)
    elif guard == "poll_workflow":
        before, after = "--workflow ci.yml \\", "--workflow deploy.yml \\"
    elif guard == "poll_event":
        before, after = "--event workflow_dispatch \\", "--event push \\"
    elif guard == "unverified_commit":
        before = 'ci_url="${GITHUB_SERVER_URL}'
        after = ('git commit --allow-empty -m "Never ran CI"\n'
                 'SNAPSHOT_SHA=$(git rev-parse HEAD)\n' + before)
    elif guard == "unverified_commit_before_dispatch":
        before = "gh workflow run ci.yml"
        after = 'git commit --allow-empty -m "Never ran CI"\n' + before
    elif guard == "skip_branch_push":
        before = 'git push origin "HEAD:refs/heads/${BRANCH}"'
        after = f"if false; then {before}; fi"
    else:
        raise AssertionError(f"Unknown mutation: {guard}")
    assert script.count(before) == 1, f"Mutation target missing or ambiguous: {guard}"
    return script.replace(before, after, 1)


@dataclass
class PublicationRepo:
    root: Path
    env: dict[str, str]
    script: str
    base: str = ""
    previous: str = ""
    moved: str = ""

    @property
    def clone(self) -> Path:
        return self.root / "clone"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(  # noqa: S603 -- isolated local repositories; file protocol only
            [str(self.root / "bin/git"), *args], cwd=cwd or self.clone,
            env=self.env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, f"git {args}: {result.stdout}{result.stderr}"
        return result.stdout.strip()

    @property
    def main(self) -> str:
        return self.git("rev-parse", "refs/heads/main", cwd=self.root / "origin.git")

    @property
    def dispatched_sha(self) -> str:
        dispatches = [event for event in self.events if event[0] == "ci-dispatch"]
        assert len(dispatches) == 1, "Expected one resolved CI dispatch"
        assert dispatches[0][1] == "automation/price-refresh-42"
        return dispatches[0][2]

    @property
    def calls(self) -> list[list[str]]:
        return [event[1:] for event in self.events if event[0] == "gh"]

    @property
    def events(self) -> list[list[str]]:
        log = self.root / "events.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    @property
    def dispatches(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["workflow", "run"]]

    def dispatch(self, workflow: str, ref: str) -> list[str]:
        return ["workflow", "run", workflow, "--ref", ref, "--repo", self.env["GITHUB_REPOSITORY"]]

    def run(
        self, *, conclusion: str = "success", wrong_sha: bool = False,
        move_main: str | None = None, changed: bool = True, mutation: str | None = None,
        run_states: list[tuple[str, str | None]] | None = None,
        status_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if changed:
            snapshot = self.clone / "src/trusted_router/data/openrouter_snapshot.json"
            snapshot.write_text('{"generated": true}\n')
            self.git("add", str(snapshot))
        states = run_states if run_states is not None else [("completed", conclusion)]
        scenario: dict[str, object] = {"polls": [[{
            "databaseId": 1234, "headSha": self.base if wrong_sha else "$DISPATCHED_SHA",
            "status": status, "conclusion": outcome,
        }] for status, outcome in states]}
        if move_main:
            peer = self.root / "peer"
            self.git("clone", "-q", str(self.root / "origin.git"), str(peer))
            if move_main == "forward":
                self.git("commit", "-qm", "Concurrent main update", "--allow-empty", cwd=peer)
                self.moved = self.git("rev-parse", "HEAD", cwd=peer)
            else:
                assert move_main == "backward"
                self.moved = self.previous
            scenario["move_main"] = {"clone": str(peer), "sha": self.moved}
        if status_fault:
            scenario["status_fault"] = status_fault
        (self.root / "scenario.json").write_text(json.dumps(scenario))
        script = remove_publication_guard(self.script, mutation) if mutation else self.script
        # Relocate only the workflow's fixed scratch paths. Gate logic and all
        # polling iterations run verbatim; fake sleep removes the delay.
        for name in ("spike-summary.txt", "refresh-summary.txt", "commit-msg.txt"):
            script = script.replace(
                f"/tmp/{name}", shlex.quote(str(self.root / name)),  # noqa: S108 -- relocate workflow paths
            )
        script_path = self.root / "publish.sh"
        script_path.write_text(script)
        result = subprocess.run(  # noqa: S603 -- real workflow, local git and fake gh only
            [str(self.root / "bin/bash"), "--noprofile", "--norc", str(script_path)],
            cwd=self.clone, env=self.env, capture_output=True, text=True, timeout=60,
        )
        # Every exit path must leave only main on the remote, including the
        # no-change path (which must not create a temporary branch at all).
        assert self.git(
            "for-each-ref", "--format=%(refname)", "refs/heads", cwd=self.root / "origin.git",
        ) == "refs/heads/main", result.stdout + result.stderr
        return result


@pytest.fixture
def publication_repo(tmp_path: Path, publication_script: str) -> PublicationRepo:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_exec_path = ""
    commands = publication_prerequisites()
    for name, executable in commands.items():
        if name == "git":
            # macOS /usr/bin/git is an Xcode launcher; resolve the real binary
            # before stripping the environment to avoid repeated tool discovery.
            git_exec_path = subprocess.run(  # noqa: S603 -- discover installed git locally
                [executable, "--exec-path"], text=True, capture_output=True, check=True, timeout=30,
            ).stdout.strip()
            real_git = Path(git_exec_path) / "git"
            if real_git.is_file():
                executable = str(real_git.resolve())
            commands[name] = executable
        else:
            (bin_dir / name).symlink_to(executable)
    # An allowlisted PATH prevents accidentally calling a real gh or another
    # network client if the workflow changes. Git only permits local transports.
    for name in ("gh", "git"):
        path = bin_dir / name
        fixture = (ROOT / f"tests/fixtures/refresh_publish_{name}.sh").read_text()
        path.write_text(f"#!{commands['bash']}\n" + fixture.split("\n", 1)[1])
        path.chmod(0o700)
    shutil.copyfile(ROOT / "tests/fixtures/refresh_publish_events.sh", tmp_path / "record-event.sh")
    sleep = bin_dir / "sleep"
    sleep.write_text(f"#!{(bin_dir / 'bash').resolve()}\nexit 0\n")
    sleep.chmod(0o700)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {
        "PATH": str(bin_dir), "TMPDIR": str(tmp_path), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TEMPLATE_DIR": str(empty), "GIT_ALLOW_PROTOCOL": "file",
        "GIT_EXEC_PATH": git_exec_path,
        "REAL_GIT": commands["git"],
        "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(empty), "GIT_CONFIG_KEY_1": "commit.gpgsign",
        "GIT_CONFIG_VALUE_1": "false", "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GITHUB_RUN_ID": "42", "GITHUB_REPOSITORY": "fixture/quill-router",
        "GITHUB_SERVER_URL": "https://github.invalid", "GITHUB_API_URL": "https://api.github.invalid",
        "GH_TOKEN": "offline-fixture",  # noqa: S105 -- deliberately fake credential
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    repo = PublicationRepo(tmp_path, env, publication_script)
    repo.git("init", "-q", "--bare", "--initial-branch=main", "origin.git", cwd=tmp_path)
    repo.git("clone", "-q", str(tmp_path / "origin.git"), "clone", cwd=tmp_path)
    # Track one file in every git-add path so the real command cannot fail on
    # missing pathspecs before reaching the gate being tested.
    for name in (
        "src/trusted_router/data/openrouter_snapshot.json",
        "src/trusted_router/data/speech_pricing.json",
        "src/trusted_router/data/transcription_pricing.json",
        "src/trusted_router/data/provider_routing_evidence.json",
        "src/trusted_router/data/provider_models/fixture.json",
        "src/trusted_router/data/model_changes.jsonl",
        "src/trusted_router/data/model_changes_state.json",
        "src/trusted_router/static/og/providers/fixture.png",
        "scripts/pricing/parsers/fixture.py",
    ):
        path = repo.clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    repo.git("add", ".")
    repo.git("commit", "-qm", "Previous main")
    repo.previous = repo.git("rev-parse", "HEAD")
    repo.git("commit", "-qm", "Base main", "--allow-empty")
    repo.base = repo.git("rev-parse", "HEAD")
    repo.git("push", "-q", "origin", "main")
    (tmp_path / "events.jsonl").write_text("")  # Exclude fixture setup's seed push.
    env["GITHUB_SHA"] = repo.base
    for name in ("spike-summary.txt", "refresh-summary.txt"):
        (tmp_path / name).write_text("Fixture generated price changes\n")
    return repo


def assert_snapshot_published(repo: PublicationRepo, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    dispatched_sha = repo.dispatched_sha
    assert dispatched_sha != repo.base
    answers = [json.loads(event[2]) for event in repo.events if event[0] == "poll" and event[2]]
    successful = [answer for answer in answers
                  if answer["status"] == "completed" and answer["conclusion"] == "success"]
    assert successful, "Expected completed successful CI before publication"
    assert all(answer["headSha"] == dispatched_sha for answer in successful), (
        "Successful CI must use the dispatched SHA"
    )
    assert repo.dispatches == [
        repo.dispatch("ci.yml", "automation/price-refresh-42"), repo.dispatch("deploy.yml", "main"),
    ]
    status_calls = [call for call in repo.calls if call[0] == "api"]
    posts = [call for call in status_calls if "POST" in call]
    gets = [call for call in status_calls if "--method" not in call]
    endpoint = f'repos/{repo.env["GITHUB_REPOSITORY"]}/statuses/{dispatched_sha}'
    assert len(posts) == len(gets) == 4
    assert all(endpoint in call for call in status_calls), "Status endpoints must use the dispatched SHA"
    assert repo.main == dispatched_sha, "Remote main must equal the dispatched SHA"
    assert [next(arg for arg in call if arg.startswith("context=")) for call in posts] == [
        "context=lint", "context=test (1)", "context=test (2)", "context=test (3)",
    ]
    assert all("state=success" in call for call in posts)
    assert all(
        f'target_url={repo.env["GITHUB_SERVER_URL"]}/{repo.env["GITHUB_REPOSITORY"]}/actions/runs/1234'
        in call for call in posts
    )
    assert_status_bridge_precedes_main_push(repo)
    events = repo.events
    push = next(i for i, e in enumerate(events) if e[0] == "git" and "HEAD:refs/heads/main" in e)
    deploy = next(i for i, e in enumerate(events) if e[:4] == ["gh", "workflow", "run", "deploy.yml"])
    assert push < deploy, "deploy.yml must be dispatched only after main is pushed"


def assert_status_bridge_precedes_main_push(repo: PublicationRepo) -> None:
    events = repo.events
    pushes = [i for i, event in enumerate(events)
              if event[0] == "git" and "HEAD:refs/heads/main" in event]
    assert len(pushes) == 1, "Expected exactly one generated main push"
    posts = [i for i, event in enumerate(events) if event[:2] == ["gh", "api"] and "POST" in event]
    gets = [i for i, event in enumerate(events) if event[:2] == ["gh", "api"] and "--method" not in event]
    assert len(posts) == len(gets) == 4, "Every required status needs POST and verification"
    assert all(post < get < pushes[0] for post, get in zip(posts, gets, strict=True)), (
        "All status POSTs and verifications must precede the main push"
    )


def assert_ci_completed_before_publication(
    repo: PublicationRepo, states: list[tuple[str, str | None]],
) -> None:
    polls = [(i, event) for i, event in enumerate(repo.events) if event[0] == "poll"]
    answers = [json.loads(event[2]) for _, event in polls]
    successful_completions = [i for (i, _), answer in zip(polls, answers, strict=True)
                              if answer["status"] == "completed" and answer["conclusion"] == "success"]
    for i, event in enumerate(repo.events):
        if event[:2] == ["gh", "api"] or (event[0] == "git" and "HEAD:refs/heads/main" in event):
            assert any(completed < i for completed in successful_completions), (
                "Publication occurred before the completed successful CI answer"
            )
    assert [(answer["status"], answer["conclusion"]) for answer in answers] == states, (
        "Must keep polling until CI completes"
    )
    assert all(event[3] == repo.base for _, event in polls), "Main changed before CI completed"


def assert_snapshot_withheld(repo: PublicationRepo, result: subprocess.CompletedProcess[str]) -> None:
    assert repo.dispatched_sha != repo.base
    assert repo.main == (repo.moved or repo.base), result.stdout + result.stderr
    assert not any(call[0] == "api" for call in repo.calls)
    expected = [repo.dispatch("ci.yml", "automation/price-refresh-42")]
    if repo.moved:
        assert result.returncode == 0, result.stdout + result.stderr
        expected.append(repo.dispatch("refresh-prices.yml", "main"))
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    assert repo.dispatches == expected


def test_snapshot_waits_for_ci_beyond_45_minutes(publication_repo: PublicationRepo) -> None:
    states = [("in_progress", None)] * 181 + [("completed", "success")]
    result = publication_repo.run(run_states=states)
    assert result.returncode == 0, result.stdout + result.stderr
    assert_ci_completed_before_publication(publication_repo, states)


def test_publication_promotes_only_the_successful_generated_sha(publication_repo: PublicationRepo) -> None:
    assert_snapshot_published(publication_repo, publication_repo.run())


@pytest.mark.parametrize("mutation, message", [
    ("unverified_commit", "Status endpoints must use the dispatched SHA"),
    ("unverified_commit_before_dispatch", "Remote main must equal the dispatched SHA"),
])
def test_unverified_commit_mutation_is_detected(
    publication_repo: PublicationRepo, mutation: str, message: str,
) -> None:
    result = publication_repo.run(mutation=mutation)
    assert result.returncode == 0, result.stdout + result.stderr
    with pytest.raises(AssertionError, match=message):
        assert_snapshot_published(publication_repo, result)


def test_missing_remote_dispatch_ref_mutation_is_detected(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(mutation="skip_branch_push")
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
    assert not any(call[0] == "api" for call in publication_repo.calls)
    assert publication_repo.dispatches == [publication_repo.dispatch("ci.yml", "automation/price-refresh-42")]
    assert "Could not resolve dispatched ref" in result.stderr
    assert not any(event[0] == "poll" for event in publication_repo.events)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_publication_rejects_unsuccessful_ci(publication_repo: PublicationRepo, conclusion: str) -> None:
    result = publication_repo.run(conclusion=conclusion)
    assert_snapshot_withheld(publication_repo, result)
    assert f"CI concluded '{conclusion}'" in result.stdout


def test_publication_times_out_when_only_another_sha_passed(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(wrong_sha=True)
    assert_snapshot_withheld(publication_repo, result)
    assert "CI did not pass within" in result.stdout
    assert len([call for call in publication_repo.calls if call[:2] == ["run", "list"]]) == 480


@pytest.mark.parametrize("direction", ["forward", "backward"])
def test_publication_recomputes_when_main_moved(publication_repo: PublicationRepo, direction: str) -> None:
    result = publication_repo.run(move_main=direction)
    assert (publication_repo.root / "main-moved").read_text() == publication_repo.moved
    assert publication_repo.moved != publication_repo.base
    assert_snapshot_withheld(publication_repo, result)


def test_publication_does_nothing_without_staged_changes(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(changed=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
    assert publication_repo.calls == []
    assert publication_repo.events == []  # Includes even no-op pushes.


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_publication_waits_for_completed_ci(publication_repo: PublicationRepo, conclusion: str) -> None:
    states = [("in_progress", None), ("in_progress", None), ("completed", conclusion)]
    result = publication_repo.run(run_states=states)
    assert_ci_completed_before_publication(publication_repo, states)
    if conclusion == "success":
        assert_snapshot_published(publication_repo, result)
    else:
        assert_snapshot_withheld(publication_repo, result)


@pytest.mark.parametrize("mutation", ["completion", "accept_pending"])
def test_pending_ci_mutations_are_detected(publication_repo: PublicationRepo, mutation: str) -> None:
    states = [("in_progress", None), ("in_progress", None), ("completed", "success")]
    result = publication_repo.run(run_states=states, mutation=mutation)
    message = "Must keep polling" if mutation == "completion" else "Publication occurred before"
    with pytest.raises(AssertionError, match=message):
        assert_ci_completed_before_publication(publication_repo, states)
    if mutation == "completion":
        assert_snapshot_withheld(publication_repo, result)  # Incorrectly rejects a pending run.
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert publication_repo.main == publication_repo.dispatched_sha
        with pytest.raises(AssertionError, match="completed successful CI"):
            assert_snapshot_published(publication_repo, result)


def test_status_bridge_order_mutation_is_detected(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(mutation="late_bridge")
    assert result.returncode == 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.dispatched_sha != publication_repo.base
    with pytest.raises(AssertionError, match="must precede the main push"):
        assert_snapshot_published(publication_repo, result)


@pytest.mark.parametrize("missing", [name for name, kind in PUBLICATION_COMMANDS.items() if kind != "fake"])
@pytest.mark.parametrize("ci_marker", [None, "CI", "GITHUB_ACTIONS"])
def test_missing_publication_tools_fail_in_ci(
    monkeypatch: pytest.MonkeyPatch, missing: str, ci_marker: str | None,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    if ci_marker:
        monkeypatch.setenv(ci_marker, "true")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == missing else f"/fixture/{name}")
    expected = pytest.fail.Exception if ci_marker else pytest.skip.Exception
    assert_prerequisite_outcome(expected, missing)


def assert_prerequisite_outcome(expected: type[BaseException], missing: str) -> None:
    # An unexpected pytest.skip must become a failure, not silently skip the
    # test of this very policy (pytest.raises alone would let it escape).
    try:
        with pytest.raises(expected, match=f"require on PATH: {missing}"):
            publication_prerequisites()
    except pytest.skip.Exception as exc:
        raise AssertionError("CI prerequisites must fail instead of skipping") from exc


def test_missing_tool_skip_in_ci_mutation_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "jq" else f"/fixture/{name}")
    expected = pytest.fail.Exception
    # Mutate the failure action into the old unconditional skip behavior.
    monkeypatch.setattr(pytest, "fail", pytest.skip)
    with pytest.raises(AssertionError, match="CI prerequisites must fail instead of skipping"):
        assert_prerequisite_outcome(expected, "jq")


@pytest.mark.parametrize("guard", ["conclusion", "head_sha", "main_moved"])
def test_publication_guards_have_effect(publication_repo: PublicationRepo, guard: str) -> None:
    # A rewind isolates the main-moved guard: a divergent forward move would
    # also be rejected by Git's non-fast-forward rule, masking a missing guard.
    result = publication_repo.run(
        conclusion="failure" if guard == "conclusion" else "success",
        wrong_sha=guard == "head_sha",
        move_main="backward" if guard == "main_moved" else None,
        mutation=guard,
    )
    # The SAME safety assertion used above must reject every mutant, and the
    # remote ref must show an actual unsafe publication (not an unrelated error).
    with pytest.raises(AssertionError):
        assert_snapshot_withheld(publication_repo, result)
    assert result.returncode == 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.dispatched_sha != publication_repo.base
    if guard in {"conclusion", "head_sha"}:
        message = "completed successful CI" if guard == "conclusion" else "dispatched SHA"
        with pytest.raises(AssertionError, match=message):
            assert_snapshot_published(publication_repo, result)
    else:
        assert_snapshot_published(publication_repo, result)


def test_ci_accepts_explicit_dispatch_for_bot_commits() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch: {}" in workflow


def test_price_refresh_checks_exact_branch_sha_before_advancing_main() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    branch_ci_dispatch = "gh workflow run ci.yml \\\n"
    deploy_dispatch = (
        'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}"'
    )

    assert 'BRANCH="automation/price-refresh-${GITHUB_RUN_ID}"' in workflow
    assert "SNAPSHOT_SHA=$(git rev-parse HEAD)" in workflow
    assert 'git push origin "HEAD:refs/heads/${BRANCH}"' in workflow
    assert branch_ci_dispatch in workflow
    assert '--ref "${BRANCH}"' in workflow
    assert '--branch "${BRANCH}"' in workflow
    assert 'select(.headSha == \\"${SNAPSHOT_SHA}\\")' in workflow
    assert 'if [ "${conclusion}" = "success" ]' in workflow
    assert "statuses: write" in workflow
    assert 'ci_run_id=$(jq -r .databaseId <<<"${run_json}")' in workflow
    assert 'if [ "$(git rev-parse origin/main)" != "${GITHUB_SHA}" ]' in workflow
    assert "git rebase" not in workflow
    status_bridge = '"repos/${GITHUB_REPOSITORY}/statuses/${SNAPSHOT_SHA}"'
    assert status_bridge in workflow
    assert 'for context in "lint" "test (1)" "test (2)" "test (3)"' in workflow
    assert '-f target_url="${ci_url}"' in workflow
    assert 'expected_status_url="${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/statuses/${SNAPSHOT_SHA}"' in workflow
    assert "status_id=$(jq -r '.id // empty'" in workflow
    assert '$(jq -r .url <<<"${status_json}")' in workflow
    assert '$(jq -r .context <<<"${status_json}")' in workflow
    assert '$(jq -r .state <<<"${status_json}")' in workflow
    assert '--jq "map(select(.id == ${status_id}))[0] // empty"' in workflow
    assert '$(jq -r .creator.login <<<"${verified_status}")' in workflow
    assert '!= "github-actions[bot]"' in workflow
    main_push = 'git push origin "HEAD:refs/heads/main"'
    assert main_push in workflow
    assert deploy_dispatch in workflow
    assert workflow.index(branch_ci_dispatch) < workflow.index(main_push)
    assert workflow.index('if [ "${conclusion}" = "success" ]') < workflow.index(
        status_bridge
    )
    assert workflow.index(
        'if [ "$(git rev-parse origin/main)" != "${GITHUB_SHA}" ]'
    ) < workflow.index(status_bridge)
    assert workflow.index(status_bridge) < workflow.index(main_push)
    assert workflow.index(main_push) < workflow.index(deploy_dispatch)
    assert "deploy_synthetic_monitor" not in workflow
    assert "WARN: failed to dispatch deploy.yml" not in workflow


def test_price_refresh_publishes_only_after_exact_snapshot_ci() -> None:
    # The gate is the full ci.yml run on the exact generated SHA; a local
    # ruff/mypy/pytest pass duplicated a subset of it and cost ~2 h per run.
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    commit_step = "- name: Commit, verify, and push if changed"
    assert "- name: Validate generated catalog before commit" not in workflow
    before_commit = workflow[: workflow.index(commit_step)]
    assert "uv run pytest" not in before_commit
    publish = workflow[workflow.index(commit_step) :]
    dispatch = publish.index("gh workflow run ci.yml")
    exact_sha = publish.index('map(select(.headSha == \\"${SNAPSHOT_SHA}\\"))')
    success = publish.index('if [ "${conclusion}" = "success" ]; then')
    main_push = publish.index('git push origin "HEAD:refs/heads/main"')
    assert dispatch < exact_sha < success < main_push


def test_refresh_still_requires_billing_proofs_in_exact_snapshot_ci() -> None:
    # Excluding the redundant local oracle pass is safe only while the
    # required exact-SHA CI includes every default/post-cutover proof shard.
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["proof-oracle"]
    assert "if" not in job
    assert "continue-on-error" not in job
    assert job["strategy"]["matrix"] == {
        "clock": ["default", "post-cutover"], "shard": [1, 2, 3, 4, 5, 6],
    }
    proof_steps = [step for step in job["steps"] if "-m proof_oracle" in step.get("run", "")]
    assert len(proof_steps) == 1
    assert "if" not in proof_steps[0]
    assert "continue-on-error" not in proof_steps[0]


def test_model_discovery_gap_alerts_without_freezing_safe_provider_updates() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    coverage_step = "- name: Price-source coverage audit"
    commit_step = "- name: Commit, verify, and push if changed"
    final_alert = "- name: Reconcile model-discovery coverage issue"

    assert workflow.index(coverage_step) < workflow.index(commit_step)
    assert workflow.index(commit_step) < workflow.index(final_alert)
    coverage = workflow[
        workflow.index(coverage_step) : workflow.index(commit_step)
    ]
    assert "id: coverage_audit" in coverage
    assert "continue-on-error: true" in coverage
    assert "--strict-model-discovery" in coverage
    assert "tee /tmp/coverage-summary.txt" in coverage
    alert = workflow[workflow.index(final_alert) :]
    assert "COVERAGE_OUTCOME: ${{ steps.coverage_audit.outcome }}" in alert
    assert 'title="[bot] Model discovery coverage gaps"' in alert
    assert "gh issue create" in alert
    assert "gh issue edit" in alert
    assert "gh issue close" in alert
    assert "exit 1" not in alert
    assert "issues: write" in workflow

    spike_gate = workflow[
        workflow.index("- name: Sanity-check for price spikes + record deltas") :
        workflow.index("- name: Refresh provider social cards")
    ]
    assert "continue-on-error" not in spike_gate


@pytest.mark.parametrize("fault", ["creator", "post_url", "get_empty"])
def test_publication_withholds_snapshot_when_status_verification_fails(
    publication_repo: PublicationRepo, fault: str,
) -> None:
    # A status GitHub did not create as github-actions[bot], a POST response for
    # another URL, or a status the SHA-scoped GET cannot find must stop the push.
    result = publication_repo.run(status_fault=fault)
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
    assert not any(call[:3] == ["workflow", "run", "deploy.yml"] for call in publication_repo.calls)


@pytest.mark.parametrize("mutation", ["poll_workflow", "poll_event"])
def test_poll_must_select_the_exact_ci_dispatch(publication_repo: PublicationRepo, mutation: str) -> None:
    # Polling another workflow or event could let an unrelated successful run
    # gate publication; the fake gh only answers the exact ci.yml dispatch query.
    result = publication_repo.run(mutation=mutation)
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
