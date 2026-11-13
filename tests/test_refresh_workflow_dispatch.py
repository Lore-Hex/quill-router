import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from scripts.deploy.price_snapshot_deploy import SNAPSHOT_SUBJECT_PREFIX
from scripts.pricing.refresh_inputs import refresh_inputs

ROOT = Path(__file__).resolve().parents[1]
REFRESH_INPUTS = refresh_inputs()
DEPLOY_DISPATCH = 'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}" -f track_main=true'
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


def test_catalog_refresh_regenerates_and_publishes_model_alias_vectors():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = workflow["jobs"]["refresh"]["steps"]
    model_changes = next(step for step in steps if step.get("name") == "Record model route changes and upcoming schedules")
    publication = next(step for step in steps if step.get("name") == "Commit, verify, and push if changed")
    vector_steps = [step for step in steps if "uv run python scripts/fastpath/model_alias_vectors.py" in step.get("run", "")]
    assert len(vector_steps) == 1
    vectors = vector_steps[0]
    assert steps.index(model_changes) < steps.index(vectors) < steps.index(publication)
    assert not vectors.get("continue-on-error")
    assert "if" not in vectors
    assert "fastpath/testdata/model_alias_vectors_v1.json" in publication["run"]


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
        before = 'if [ "$(git rev-parse origin/main)" != "${PUBLISH_BASE}" ]; then'
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
    elif guard == "poll_by_listing":
        # Keep re-reading the branch listing after the run was found.
        before = 'if [ -n "${ci_run_id}" ]; then\n      # Once the listing has found the run'
        after = 'if false; then\n      # Once the listing has found the run'
    elif guard == "skip_branch_push":
        before = 'git push --force origin "HEAD:refs/heads/${BRANCH}"'
        after = f"if false; then {before}; fi"
    elif guard == "deploy_dispatch":
        # Put back the deploy dispatch publication made after its main push.
        before = "# GITHUB_TOKEN pushes do not start"
        after = f"{DEPLOY_DISPATCH}\n{before}"
    elif guard == "stale_attempt":
        # Read the failed attempt, still visible just after the rerun request,
        # as the rerun's own answer.
        before = 'if [ -n "${ci_rerun_attempt}" ] && [ "${attempt}" -lt "${ci_rerun_attempt}" ]; then'
        after = "if false; then"
    elif guard == "unbounded_rerun":
        before = 'if [ "${conclusion}" = "failure" ] && [ -z "${ci_rerun_attempt}" ]; then'
        after = 'if [ "${conclusion}" = "failure" ]; then'
    elif guard == "input_check":
        # Carry a snapshot whatever main changed.
        before = 'if ! git diff --quiet "${REFRESH_BASE}" "${target}" -- "${refresh_inputs[@]}"; then'
        after = "if false; then"
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
        shas = self.dispatched_shas
        assert len(shas) == 1, "Expected one resolved CI dispatch"
        return shas[0]

    @property
    def dispatched_shas(self) -> list[str]:
        dispatches = [event for event in self.events if event[0] == "ci-dispatch"]
        assert all(event[1] == "automation/price-refresh-42" for event in dispatches)
        return [event[2] for event in dispatches]

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
        advance_main: dict[str, object] | None = None, premove: str | None = None,
        inputs: bool = True,
        run_states: list[tuple[str, str | None] | tuple[str, str | None, int]] | None = None,
        status_fault: str | None = None,
        status_missing_reads: int = 0,
        polls: list[list[dict[str, object]]] | None = None,
        rerun_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if changed:
            snapshot = self.clone / "src/trusted_router/data/openrouter_snapshot.json"
            snapshot.write_text('{"generated": true}\n')
            self.git("add", str(snapshot))
        states = run_states if run_states is not None else [("completed", conclusion)]
        # `polls` is the listing at each poll, verbatim; otherwise one run per poll.
        # A third element is the run attempt; gh reports 1 before any rerun.
        scenario: dict[str, object] = {"polls": polls if polls is not None else [[{
            "databaseId": 1234, "headSha": self.base if wrong_sha else "$DISPATCHED_SHA",
            "status": state[0], "conclusion": state[1], **({"attempt": state[2]} if len(state) == 3 else {}),
        }] for state in states]}
        if rerun_fault:
            scenario["rerun_fault"] = rerun_fault
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
        if advance_main is not None or premove is not None:
            peer = self.root / "peer"
            self.git("clone", "-q", str(self.root / "origin.git"), str(peer))
            if premove is not None:
                # Main moved while the refresh ran, before publication began.
                self.peer_commit(peer, premove or None)
                self.moved = self.git("rev-parse", "HEAD", cwd=peer)
            if advance_main is not None:
                scenario["advance_main"] = {"clone": str(peer), **advance_main}
        inputs_path = self.root / "refresh-inputs.txt"
        inputs_path.write_text("".join(f"{spec}\n" for spec in REFRESH_INPUTS) if inputs else "")
        if status_fault:
            scenario["status_fault"] = status_fault
        scenario["status_missing_reads"] = status_missing_reads
        (self.root / "scenario.json").write_text(json.dumps(scenario))
        script = remove_publication_guard(self.script, mutation) if mutation else self.script
        # Relocate only the workflow's fixed scratch paths. Gate logic and all
        # polling iterations run verbatim; fake sleep removes the delay.
        for name in ("spike-summary.txt", "refresh-summary.txt", "commit-msg.txt", "refresh-inputs.txt"):
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

    def peer_commit(self, peer: Path, path: str | None) -> None:
        if path:
            with (peer / path).open("a") as handle:
                handle.write("concurrent change\n")
            self.git("add", "--", path, cwd=peer)
            self.git("commit", "-qm", "Concurrent main update", cwd=peer)
        else:
            self.git("commit", "-qm", "Concurrent main update", "--allow-empty", cwd=peer)
        self.git("push", "-q", "origin", "HEAD:main", cwd=peer)
        (self.root / "events.jsonl").write_text("")  # Not the step's own push.


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
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
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
        "src/trusted_router/data/openrouter_reasoning.json",
        "src/trusted_router/data/speech_pricing.json",
        "src/trusted_router/data/transcription_pricing.json",
        "src/trusted_router/data/provider_routing_evidence.json",
        "src/trusted_router/data/model_routing_evidence.json",
        "src/trusted_router/data/provider_models/fixture.json",
        "src/trusted_router/data/model_changes.jsonl",
        "src/trusted_router/data/model_changes_state.json",
        "src/trusted_router/static/og/providers/fixture.png",
        "fastpath/testdata/model_alias_vectors_v1.json",
        "scripts/pricing/parsers/fixture.py",
        "scripts/pricing/providers/fixture.py",
        "src/trusted_router/money.py",
        "docs/unrelated.md",
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


def assert_snapshot_published(
    repo: PublicationRepo, result: subprocess.CompletedProcess[str], *, status_gets: int = 5,
    rounds: int = 1, reruns: tuple[str, ...] = (),
) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(repo.env["GITHUB_OUTPUT"]).read_text() == "".join(
        f"ci_rerun={rerun}\n" for rerun in reruns) + "validated=true\n"
    shas = repo.dispatched_shas
    assert len(shas) == rounds, f"Expected {rounds} exact-SHA CI dispatches, got {len(shas)}"
    dispatched_sha = shas[-1]
    assert dispatched_sha != repo.base
    answers = [json.loads(event[2]) for event in repo.events if event[0] == "poll" and event[2]]
    successful = [answer for answer in answers
                  if answer["status"] == "completed" and answer["conclusion"] == "success"]
    assert successful, "Expected completed successful CI before publication"
    # Each round's run answers for the SHA that round dispatched; the last
    # successful run is the published SHA's own.
    assert all(answer["headSha"] in shas for answer in successful), (
        "Successful CI must use the dispatched SHA"
    )
    assert successful[-1]["headSha"] == dispatched_sha, "Successful CI must use the dispatched SHA"
    # A published snapshot rides the next rollout: publication dispatches its
    # exact-SHA CI runs and nothing else. The job's last step decides about a
    # deploy (tests/test_price_snapshot_deploy.py).
    assert not any(call[:3] == ["workflow", "run", "deploy.yml"] for call in repo.calls), (
        "Publication must not dispatch deploy.yml"
    )
    assert repo.dispatches == [repo.dispatch("ci.yml", "automation/price-refresh-42")] * rounds
    status_calls = [call for call in repo.calls if call[0] == "api"]
    posts = [call for call in status_calls if "POST" in call]
    gets = [call for call in status_calls if "--method" not in call]
    endpoint = f'repos/{repo.env["GITHUB_REPOSITORY"]}/statuses/{dispatched_sha}'
    assert len(posts) == 5
    assert len(gets) == status_gets
    assert all(endpoint in call for call in status_calls), "Status endpoints must use the dispatched SHA"
    assert repo.main == dispatched_sha, "Remote main must equal the dispatched SHA"
    assert [next(arg for arg in call if arg.startswith("context=")) for call in posts] == [
        "context=lint", "context=test (1)", "context=test (2)", "context=test (3)", "context=CI",
    ]
    assert all("state=success" in call for call in posts)
    assert all(
        f'target_url={repo.env["GITHUB_SERVER_URL"]}/{repo.env["GITHUB_REPOSITORY"]}/actions/runs/1234'
        in call for call in posts
    )
    assert_status_bridge_precedes_main_push(repo)


def assert_status_bridge_precedes_main_push(repo: PublicationRepo) -> None:
    events = repo.events
    pushes = [i for i, event in enumerate(events)
              if event[0] == "git" and "HEAD:refs/heads/main" in event]
    assert len(pushes) == 1, "Expected exactly one generated main push"
    posts = [i for i, event in enumerate(events) if event[:2] == ["gh", "api"] and "POST" in event]
    gets = [i for i, event in enumerate(events) if event[:2] == ["gh", "api"] and "--method" not in event]
    assert len(posts) == 5, "Every required status needs exactly one POST"
    assert all(index < pushes[0] for index in posts + gets), (
        "All status POSTs and verifications must precede the main push"
    )
    for status_id, (post, next_post) in enumerate(zip(posts, posts[1:] + pushes, strict=True), 1):
        readbacks = [index for index in gets if post < index < next_post]
        assert 1 <= len(readbacks) <= 5, "Each status needs bounded verification before advancing"
        for index in readbacks:
            event = events[index]
            assert event[event.index("--jq") + 1] == f"map(select(.id == {status_id}))[0] // empty"


def assert_ci_completed_before_publication(
    repo: PublicationRepo, states: list[tuple[str, str | None]] | list[tuple[str, str | None, int]],
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
    assert [(answer["status"], answer["conclusion"]) for answer in answers] == [state[:2] for state in states], (
        "Must keep polling until CI completes"
    )
    assert all(event[3] == repo.base for _, event in polls), "Main changed before CI completed"


def assert_snapshot_withheld(
    repo: PublicationRepo, result: subprocess.CompletedProcess[str], *, rounds: int = 1,
) -> None:
    output = Path(repo.env["GITHUB_OUTPUT"])
    assert not output.exists() or "validated=true" not in output.read_text()
    assert len(repo.dispatched_shas) == rounds
    assert repo.base not in repo.dispatched_shas
    # A concurrent merge per CI run records main's latest head in main-moved.
    moved_file = repo.root / "main-moved"
    if moved_file.exists():
        repo.moved = moved_file.read_text().strip()
    assert repo.main == (repo.moved or repo.base), result.stdout + result.stderr
    assert not any(call[0] == "api" for call in repo.calls)
    expected = [repo.dispatch("ci.yml", "automation/price-refresh-42")] * rounds
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


def test_publication_step_has_no_deploy_dispatch(publication_script: str) -> None:
    commands = [line for line in publication_script.splitlines() if not line.lstrip().startswith("#")]
    assert not any("deploy.yml" in line for line in commands), "Only a comment may name deploy.yml here"
    assert "gh workflow run deploy.yml" not in publication_script
    # Other workflows are still dispatched from here, so the check above is not vacuous.
    assert any("gh workflow run ci.yml" in line for line in commands)


def test_deploy_dispatch_from_publication_is_detected(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(mutation="deploy_dispatch")
    assert result.returncode == 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.dispatched_sha != publication_repo.base
    assert publication_repo.dispatches[-1] == publication_repo.dispatch("deploy.yml", "main") + [
        "-f", "track_main=true",
    ]
    with pytest.raises(AssertionError, match="Publication must not dispatch deploy.yml"):
        assert_snapshot_published(publication_repo, result)


@pytest.mark.parametrize("carried", [False, True])
def test_published_snapshot_subject_is_the_one_the_deploy_check_looks_for(
    publication_repo: PublicationRepo, carried: bool,
) -> None:
    # scripts/deploy/price_snapshot_deploy.py finds waiting snapshots on main
    # by this subject, also after the snapshot was carried onto a newer main.
    result = publication_repo.run(premove="docs/unrelated.md" if carried else None)
    assert_snapshot_published(publication_repo, result)
    origin = publication_repo.root / "origin.git"
    subject = publication_repo.git("log", "-1", "--format=%s", "refs/heads/main", cwd=origin)
    assert re.fullmatch(re.escape(SNAPSHOT_SUBJECT_PREFIX) + r": \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", subject), subject
    if carried:
        assert_carried_onto(publication_repo, publication_repo.moved)


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
    # A failure gets one rerun of its failed jobs; a rerun that fails again,
    # or any other unsuccessful conclusion, withholds the snapshot.
    states: list[tuple[str, str | None, int]] = [("completed", conclusion, 1)]
    if conclusion == "failure":
        states.append(("completed", "failure", 2))
    result = publication_repo.run(run_states=states)
    assert_snapshot_withheld(publication_repo, result)
    assert f"CI concluded '{conclusion}'" in result.stdout
    assert len(reruns(publication_repo)) == (1 if conclusion == "failure" else 0)


def reruns(repo: PublicationRepo) -> list[list[str]]:
    return [call for call in repo.calls if call[:2] == ["run", "rerun"]]


def flaky_then_green() -> list[tuple[str, str | None, int]]:
    # The second answer is the failed attempt, still reported just after the
    # rerun request; the rerun's attempt 2 then runs and passes.
    return [("completed", "failure", 1), ("completed", "failure", 1),
            ("in_progress", None, 2), ("completed", "success", 2)]


def test_a_single_flaky_ci_failure_is_rerun_once_then_published(publication_repo: PublicationRepo) -> None:
    states = flaky_then_green()
    result = publication_repo.run(run_states=states)
    assert_snapshot_published(publication_repo, result, reruns=("1234:2",))
    assert_ci_completed_before_publication(publication_repo, states)
    repo = publication_repo.env["GITHUB_REPOSITORY"]
    # Only the failed jobs of the exact run that failed, once.
    assert reruns(publication_repo) == [["run", "rerun", "1234", "--failed", "--repo", repo]]
    events = publication_repo.events
    rerun_at = next(i for i, event in enumerate(events) if event[:3] == ["gh", "run", "rerun"])
    published_at = [i for i, event in enumerate(events)
                    if event[:2] == ["gh", "api"] or (event[0] == "git" and "HEAD:refs/heads/main" in event)]
    assert published_at and all(rerun_at < i for i in published_at)
    assert "rerunning its failed jobs once" in result.stdout


def test_the_failed_attempt_is_never_read_as_the_rerun(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(run_states=flaky_then_green(), mutation="stale_attempt")
    # Without the attempt check the stale failure ends the gate before the
    # rerun reports: the snapshot a green rerun would publish is discarded.
    assert_snapshot_withheld(publication_repo, result)
    assert len(reruns(publication_repo)) == 1


@pytest.mark.parametrize("mutation", [None, "unbounded_rerun"])
def test_only_one_rerun_per_snapshot_ci_run(publication_repo: PublicationRepo, mutation: str | None) -> None:
    states = [("completed", "failure", 1), ("completed", "failure", 2), ("completed", "failure", 3)]
    result = publication_repo.run(run_states=states, mutation=mutation)
    assert_snapshot_withheld(publication_repo, result)
    # The bound, not the scenario, is what stops a second rerun.
    if mutation is None:
        assert len(reruns(publication_repo)) == 1
    else:
        assert len(reruns(publication_repo)) == 3  # one per failed attempt


def test_a_failed_rerun_request_withholds_the_snapshot(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(run_states=flaky_then_green(), rerun_fault="error")
    assert_snapshot_withheld(publication_repo, result)
    assert "could not rerun the failed jobs of ci run 1234" in result.stdout


def test_the_rerun_shares_the_round_poll_budget(publication_repo: PublicationRepo) -> None:
    # A rerun that never finishes still stops at the 120-minute (480-poll)
    # limit, and the run it waited for is cancelled by id.
    result = publication_repo.run(run_states=[("completed", "failure", 1), ("in_progress", None, 2)])
    assert_snapshot_withheld(publication_repo, result)
    assert "CI did not pass within 120 minutes" in result.stdout
    calls = publication_repo.calls
    assert len([call for call in calls if call[:2] in (["run", "list"], ["run", "view"])]) == 480
    assert len(reruns(publication_repo)) == 1
    assert calls[-1] == ["run", "cancel", "1234", "--repo", publication_repo.env["GITHUB_REPOSITORY"]]


def test_publication_times_out_when_only_another_sha_passed(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(wrong_sha=True)
    assert_snapshot_withheld(publication_repo, result)
    assert "CI did not pass within" in result.stdout
    assert len([call for call in publication_repo.calls if call[:2] == ["run", "list"]]) == 480
    # No run was ever found for the SHA, so there is nothing to cancel.
    assert not any(call[:2] == ["run", "cancel"] for call in publication_repo.calls)


def listing_with_a_second_run() -> list[list[dict[str, object]]]:
    """Our run (1234) first; a second dispatch (5678) on the same branch and SHA
    appears ahead of it in later listings and concludes success before ours."""
    def ours(status: str, conclusion: str | None) -> dict[str, object]:
        return {"databaseId": 1234, "headSha": "$DISPATCHED_SHA",
                "status": status, "conclusion": conclusion}
    other = {"databaseId": 5678, "headSha": "$DISPATCHED_SHA",
             "status": "completed", "conclusion": "success"}
    rerun_failed = dict(ours("completed", "failure"), attempt=2)
    return [[ours("in_progress", None)], [other, ours("in_progress", None)],
            [other, ours("completed", "failure")], [other, rerun_failed]]


def test_poll_sticks_to_the_run_the_listing_first_selected(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(polls=listing_with_a_second_run())
    assert_snapshot_withheld(publication_repo, result)
    assert "CI concluded 'failure'" in result.stdout
    answers = [json.loads(event[2]) for event in publication_repo.events if event[0] == "poll"]
    assert [answer["databaseId"] for answer in answers] == [1234, 1234, 1234, 1234]
    repo = publication_repo.env["GITHUB_REPOSITORY"]
    assert [call for call in publication_repo.calls if call[:2] == ["run", "view"]] == [
        ["run", "view", "1234", "--repo", repo, "--json", "databaseId,headSha,status,conclusion,attempt"],
    ] * 3
    assert reruns(publication_repo) == [["run", "rerun", "1234", "--failed", "--repo", repo]]
    assert len([call for call in publication_repo.calls if call[:2] == ["run", "list"]]) == 1


def test_poll_by_listing_mutation_is_detected(publication_repo: PublicationRepo) -> None:
    # Re-reading the listing lets the other run's success publish our SHA.
    result = publication_repo.run(polls=listing_with_a_second_run(), mutation="poll_by_listing")
    assert result.returncode == 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.dispatched_sha
    answers = [json.loads(event[2]) for event in publication_repo.events if event[0] == "poll"]
    assert [answer["databaseId"] for answer in answers] == [1234, 5678]


def test_publication_cancels_the_dispatched_run_it_stops_waiting_for(
    publication_repo: PublicationRepo,
) -> None:
    # Deleting the temporary branch does not cancel its run. A run still
    # queued when the gate gives up kept its jobs in the organization's
    # queue after nothing could use its result (2026-10-26 starvation).
    result = publication_repo.run(run_states=[("in_progress", None)])
    assert_snapshot_withheld(publication_repo, result)
    assert "CI did not pass within" in result.stdout
    calls = publication_repo.calls
    # Found by the listing once, then polled and cancelled by its id.
    assert len([call for call in calls if call[:2] == ["run", "list"]]) == 1
    assert len([call for call in calls if call[:2] == ["run", "view"]]) == 479
    assert calls[-1] == ["run", "cancel", "1234", "--repo", publication_repo.env["GITHUB_REPOSITORY"]]


def test_publication_recomputes_when_main_rewound(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(move_main="backward")
    assert (publication_repo.root / "main-moved").read_text() == publication_repo.moved
    assert publication_repo.moved != publication_repo.base
    assert_snapshot_withheld(publication_repo, result, rounds=1)
    assert "does not descend from" in result.stdout


def assert_carried_onto(repo: PublicationRepo, parent: str) -> None:
    """Main is the snapshot commit on `parent`: parent's tree plus the snapshot."""
    assert repo.git("rev-parse", "refs/heads/main^", cwd=repo.root / "origin.git") == parent
    changed = repo.git("diff", "--name-only", parent, repo.main, cwd=repo.root / "origin.git")
    assert changed.splitlines() == ["src/trusted_router/data/openrouter_snapshot.json"]
    snapshot = repo.git("show", f"{repo.main}:src/trusted_router/data/openrouter_snapshot.json",
                        cwd=repo.root / "origin.git")
    assert snapshot == '{"generated": true}'


@pytest.mark.parametrize("path", [None, "docs/unrelated.md"])
def test_snapshot_is_carried_over_main_changes_that_are_not_refresh_inputs(
    publication_repo: PublicationRepo, path: str | None,
) -> None:
    # A merge lands while the first exact-SHA CI runs. It touches nothing the
    # refresh reads, so the snapshot moves onto it and its new SHA gets its
    # own complete CI run before statuses and the push (2026-11-07 starvation).
    result = publication_repo.run(advance_main={"dispatches": 1, "path": path})
    moved = (publication_repo.root / "main-moved").read_text().strip()
    assert_snapshot_published(publication_repo, result, rounds=2)
    first, second = publication_repo.dispatched_shas
    assert publication_repo.git("rev-parse", f"{first}^") == publication_repo.base
    assert publication_repo.main == second != first
    assert_carried_onto(publication_repo, moved)
    assert f"carried the snapshot onto {moved}" in result.stdout


def test_snapshot_is_carried_before_the_first_ci_run(publication_repo: PublicationRepo) -> None:
    # Main moved while the refresh itself ran: carry before dispatching CI,
    # so the first exact-SHA run already checks the publishable SHA.
    result = publication_repo.run(premove="docs/unrelated.md")
    assert_snapshot_published(publication_repo, result, rounds=1)
    assert_carried_onto(publication_repo, publication_repo.moved)


@pytest.mark.parametrize("path", [
    "src/trusted_router/data/provider_models/fixture.json",  # a refresh output
    "scripts/pricing/providers/fixture.py",  # a dynamically imported adapter
    "src/trusted_router/money.py",  # catalog code in the import closure
])
def test_snapshot_is_recomputed_when_main_changes_a_refresh_input(
    publication_repo: PublicationRepo, path: str,
) -> None:
    assert covered_by_refresh_inputs(path)
    result = publication_repo.run(advance_main={"dispatches": 1, "path": path})
    assert_snapshot_withheld(publication_repo, result, rounds=1)
    assert "main gained changes to refresh inputs:" in result.stdout
    assert path in result.stdout


def covered_by_refresh_inputs(path: str) -> bool:
    return any(path == spec or (spec.endswith("/") and path.startswith(spec)) for spec in REFRESH_INPUTS)


def test_snapshot_is_recomputed_without_the_refresh_input_list(publication_repo: PublicationRepo) -> None:
    # No list, no proof that the snapshot is current: recompute as before.
    result = publication_repo.run(premove="docs/unrelated.md", inputs=False)
    assert_snapshot_withheld(publication_repo, result, rounds=0)
    assert "refresh input list unavailable" in result.stdout


def test_carrying_stops_after_bounded_ci_rounds(publication_repo: PublicationRepo) -> None:
    # Main moves during every exact-SHA CI run: three rounds, then recompute.
    result = publication_repo.run(advance_main={"dispatches": 3, "path": None})
    assert_snapshot_withheld(publication_repo, result, rounds=3)
    assert "main advanced during each of 3 exact-SHA CI runs" in result.stdout
    assert len(set(publication_repo.dispatched_shas)) == 3


def test_input_check_mutation_is_detected(publication_repo: PublicationRepo) -> None:
    # Without the input comparison a snapshot computed from the old catalog
    # code would be published over the change. The same safety assertion used
    # above must reject it.
    path = "src/trusted_router/money.py"
    result = publication_repo.run(advance_main={"dispatches": 1, "path": path}, mutation="input_check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(publication_repo.dispatched_shas) == 2
    assert publication_repo.git("rev-parse", "refs/heads/main^", cwd=publication_repo.root / "origin.git") == (
        publication_repo.root / "main-moved").read_text().strip()
    with pytest.raises(AssertionError):
        assert_snapshot_withheld(publication_repo, result, rounds=1)


def test_refresh_inputs_step_feeds_publication() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-prices.yml").read_text())
    steps = workflow["jobs"]["refresh"]["steps"]
    record = next(step for step in steps if step.get("id") == "refresh_inputs")
    publication = next(step for step in steps if step.get("id") == "publication")
    assert steps.index(record) == steps.index(publication) - 1
    assert record["run"].strip() == "uv run python -m scripts.pricing.refresh_inputs > /tmp/refresh-inputs.txt"
    # A failed listing falls back to recomputation; it never blocks publication.
    assert record["continue-on-error"] is True
    assert "mapfile -t refresh_inputs < /tmp/refresh-inputs.txt" in publication["run"]


def test_publication_does_nothing_without_staged_changes(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(changed=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(publication_repo.env["GITHUB_OUTPUT"]).read_text() == "validated=true\n"
    assert publication_repo.main == publication_repo.base
    assert publication_repo.calls == []
    assert publication_repo.events == []  # Includes even no-op pushes.


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_publication_waits_for_completed_ci(publication_repo: PublicationRepo, conclusion: str) -> None:
    states: list[tuple[str, str | None, int]] = [
        ("in_progress", None, 1), ("in_progress", None, 1), ("completed", conclusion, 1)]
    if conclusion == "failure":
        states += [("in_progress", None, 2), ("completed", "failure", 2)]
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
        'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}" -f track_main=true'
    )

    assert 'BRANCH="automation/price-refresh-${GITHUB_RUN_ID}"' in workflow
    assert "SNAPSHOT_SHA=$(git rev-parse HEAD)" in workflow
    assert 'git push --force origin "HEAD:refs/heads/${BRANCH}"' in workflow
    assert branch_ci_dispatch in workflow
    assert '--ref "${BRANCH}"' in workflow
    assert '--branch "${BRANCH}"' in workflow
    assert 'select(.headSha == \\"${SNAPSHOT_SHA}\\")' in workflow
    assert 'if [ "${conclusion}" = "success" ]' in workflow
    assert "statuses: write" in workflow
    assert 'ci_run_id=$(jq -r .databaseId <<<"${run_json}")' in workflow
    assert 'if [ "$(git rev-parse origin/main)" != "${PUBLISH_BASE}" ]' in workflow
    assert "git rebase" not in workflow
    status_bridge = '"repos/${GITHUB_REPOSITORY}/statuses/${SNAPSHOT_SHA}"'
    assert status_bridge in workflow
    assert 'for context in "lint" "test (1)" "test (2)" "test (3)" "CI"; do' in workflow
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
        'if [ "$(git rev-parse origin/main)" != "${PUBLISH_BASE}" ]'
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
    # required exact-SHA CI includes every default/post-cutover proof shard
    # for a commit that can change the oracle's result. That CI is the
    # refresh's dispatch run, which keeps both clocks; only a push to main
    # runs the default clock alone (ci.yml's test-post-cutover says why). The oracle builds its
    # catalog from each case's fixture body, so a commit that touches only
    # published catalog data cannot; ci.yml skips the shards for exactly that
    # commit and runs them for a refresh that also rewrote a parser.
    from scripts.ci.commit_scope import DATA_ONLY_CONDITION

    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["proof-oracle"]
    assert job["if"] == DATA_ONLY_CONDITION
    assert job["needs"] == "scope"
    assert "continue-on-error" not in job
    matrix = job["strategy"]["matrix"]
    assert set(matrix) == {"clock", "shard"} and matrix["shard"] == [1, 2, 3, 4, 5, 6]
    clocks = re.fullmatch(r"\$\{\{ fromJSON\(github\.event_name == 'push' && '(\[.*?\])' \|\| '(\[.*?\])'\) \}\}",
                          matrix["clock"])
    assert clocks, matrix["clock"]
    # Every event but a push, the refresh's dispatch among them, runs both.
    assert json.loads(clocks.group(2)) == ["default", "post-cutover"]
    assert json.loads(clocks.group(1)) == ["default"]
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
    # A required-model gap is provider-scoped: annotated and tracked, never a
    # global publication block that ages every other manifest to fail-closed.
    assert "--strict-model-discovery" not in workflow
    assert "--annotate-model-discovery-gaps" in coverage
    assert "'^::error title=Model discovery coverage gap::'" in coverage
    assert "gap_attention=true" in coverage
    assert "tee /tmp/coverage-summary.txt" in coverage

    # Audio sources hold independently of refresh.py; all three summaries
    # feed the held-price issue.
    refresh_step = workflow[
        workflow.index("- name: Refresh provider-direct prices (with self-heal)") :
        workflow.index("- name: Sanity-check for price spikes + record deltas")
    ]
    assert "python -m scripts.pricing.speech | tee /tmp/speech-summary.txt" in refresh_step
    assert "python -m scripts.pricing.transcription | tee /tmp/transcription-summary.txt" in refresh_step
    held_issue = workflow[workflow.index("- name: Reconcile held-price issue") :]
    assert "for summary in /tmp/refresh-summary.txt /tmp/speech-summary.txt /tmp/transcription-summary.txt; do" in held_issue
    assert 'for summary in ${summaries}; do' in held_issue
    assert "sed -n '/^Held for price review/,/^$/p' \"${summary}\"" in held_issue
    # The slice ends where the job's last step begins. That step is no issue
    # alert: it decides about a deploy and fails on purpose when it cannot
    # (tests/test_price_snapshot_deploy.py).
    deploy_check = "- name: Deploy a price snapshot no rollout has carried"
    held_price = workflow.index("- name: Reconcile held-price issue")
    assert workflow.index(final_alert) < held_price < workflow.index(deploy_check)
    alert = workflow[workflow.index(final_alert) : workflow.index(deploy_check)]
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


@pytest.mark.parametrize("fault", [
    "creator", "post_url", "get_empty", "get_url", "get_context", "get_state", "get_target", "get_error",
])
def test_publication_withholds_snapshot_when_status_verification_fails(
    publication_repo: PublicationRepo, fault: str,
) -> None:
    # A status GitHub did not create as github-actions[bot], a POST response for
    # another URL, or a status the SHA-scoped GET cannot find must stop the push.
    result = publication_repo.run(status_fault=fault)
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
    assert not any(call[:3] == ["workflow", "run", "deploy.yml"] for call in publication_repo.calls)
    gets = [call for call in publication_repo.calls if call[0] == "api" and "--method" not in call]
    assert len(gets) == (0 if fault == "post_url" else 5 if fault == "get_empty" else 1)


@pytest.mark.parametrize("missing_reads", [1, 4])
def test_publication_waits_only_for_the_exact_missing_status(
    publication_repo: PublicationRepo, missing_reads: int,
) -> None:
    result = publication_repo.run(status_missing_reads=missing_reads)
    assert_snapshot_published(publication_repo, result, status_gets=5 + missing_reads)
    # Only the last status is read again: the first four readbacks name ids 1
    # to 4, and every later one names the fifth.
    gets = [call for call in publication_repo.calls if call[0] == "api" and "--method" not in call]
    ids = [next(int(part.split(".id == ")[1].split(")")[0]) for part in call if ".id == " in part)
           for call in gets]
    assert ids == [1, 2, 3, 4] + [5] * (missing_reads + 1)


def test_publication_stops_when_exact_status_stays_missing(publication_repo: PublicationRepo) -> None:
    result = publication_repo.run(status_missing_reads=5)
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
    assert publication_repo.dispatches == [publication_repo.dispatch("ci.yml", "automation/price-refresh-42")]
    output = Path(publication_repo.env["GITHUB_OUTPUT"])
    assert not output.exists() or "validated=true" not in output.read_text()
    assert "not visible after 5 reads" in result.stdout
    status_calls = [call for call in publication_repo.calls if call[0] == "api"]
    assert len([call for call in status_calls if "POST" in call]) == 5
    assert len([call for call in status_calls if "--method" not in call]) == 9


@pytest.mark.parametrize("mutation", ["poll_workflow", "poll_event"])
def test_poll_must_select_the_exact_ci_dispatch(publication_repo: PublicationRepo, mutation: str) -> None:
    # Polling another workflow or event could let an unrelated successful run
    # gate publication; the fake gh only answers the exact ci.yml dispatch query.
    result = publication_repo.run(mutation=mutation)
    assert result.returncode != 0, result.stdout + result.stderr
    assert publication_repo.main == publication_repo.base
