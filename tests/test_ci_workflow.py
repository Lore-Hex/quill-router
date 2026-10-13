from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_service_uses_digest_pinned_official_public_mirror() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    postgres = jobs["test"]["services"]["postgres"]
    assert postgres["image"] == (
        "mirror.gcr.io/library/postgres:17@sha256:"
        "2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
    )
    assert "5432:5432" in postgres["ports"]
    assert "pg_isready" in postgres["options"]
    assert "continue-on-error" not in jobs["test"]


def test_ci_shard_matrices_cover_every_partition_with_bounded_jobs() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    for name in ("test", "test-post-cutover"):
        job = jobs[name]
        count = int(job["env"]["TEST_SHARDS"])
        assert job["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
        assert job["timeout-minutes"] == 25
        assert job["strategy"]["fail-fast"] is False
        assert "continue-on-error" not in job
    assert jobs["coverage"]["needs"] == "test"


def test_ci_runs_python_suite_once_with_coverage() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    # Still one coverage pass, now sharded. A second pass on the SAME clock
    # doubles CI time and delays every guarded rollout without checking any
    # additional behavior, which is why this file exists.
    assert "-n 4 --dist loadgroup" in workflow
    assert "--cov=trusted_router" in workflow
    assert workflow.count("--cov=trusted_router") == 1

    # The shards collect coverage with NO threshold: each sees only its own
    # slice, so a per-shard floor would fail on a suite that covers far more.
    # The floor moved to the `coverage` job and must still be enforced exactly
    # once -- dropping it entirely would leave every assertion here passing.
    # Comment lines stripped first: the workflow EXPLAINS why a per-shard
    # --cov-fail-under would be wrong, and matching that prose would make this
    # assertion fail on the documentation rather than on the command.
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--cov-fail-under=70" not in commands
    assert commands.count("--fail-under=70") == 1
    assert "coverage combine" in workflow

    # --dist loadgroup is load-bearing: the conformance backends are
    # xdist_group-marked because the Spanner PG emulator rejects concurrent
    # DDL. Sharding is safe only because each matrix job gets its own service
    # containers.
    assert "--splits" in workflow and "--group" in workflow


def test_ci_runs_the_suite_again_past_every_scheduled_cutover() -> None:
    """The second full-suite pass: same tests, different clock.

    provider_lifecycle schedules effective-dated retirements, so a test written
    on the near side of one goes red at the announced minute on a pull request
    that did not touch it (CI run 31980690855, Wafer, 2026-08-17 00:00 UTC).
    The post-cutover job moves that failure onto the pull request that writes
    the test. It earns its runtime only if it actually moves the clock, so the
    override has to be there.
    """
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow)["jobs"]
    # The native emulator and proof-oracle jobs run focused tests. Keep the
    # duplicate-full-pass guard for other jobs, including future additions,
    # and verify both focused exceptions separately below.
    commands = "\n".join(
        step.get("run", "")
        for name, job in jobs.items() if name not in {"spanner-emulator", "proof-oracle"}
        for step in job.get("steps", [])
    )

    assert commands.count("uv run pytest") == 2
    for name in ("test", "test-post-cutover"):
        assert sum(step.get("run", "").count("uv run pytest") for step in jobs[name]["steps"]) == 1
    cutover = "\n".join(step.get("run", "") for step in jobs["test-post-cutover"]["steps"])
    assert "TR_LIFECYCLE_CLOCK_OVERRIDE=" in cutover
    # Derived from _RETIREMENTS at run time, never a hard-coded date that would
    # silently stop being in the future.
    assert "latest_scheduled_cutover" in cutover


def test_native_spanner_job_runs_both_focused_server_gates() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    job = jobs["spanner-emulator"]
    assert job["env"]["TR_CONFORMANCE_EMULATOR_SCHEMA"] == "1"
    assert job["env"]["SPANNER_EMULATOR_HOST"] == "127.0.0.1:9010"
    assert job["services"]["spanner"]["image"] == "gcr.io/cloud-spanner-emulator/emulator@sha256:c6f3402f2599684f295a0fdefb6fbbbfb18a0e43e309ff5456ccb452a4570a79"
    assert "bigtable" not in job["services"]
    steps = [step for step in job["steps"] if "uv run pytest" in step.get("run", "")]
    assert len(steps) == 2
    assert "tests/conformance -k spanner-emulator" in steps[0]["run"]
    assert "tests/conformance/test_spanner_schema.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_sql_acceptance.py" in steps[1]["run"]
    assert "tests/conformance/test_spanner_schema_audit.py" in steps[1]["run"]
    assert steps[1]["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])


def test_provider_health_is_monitored_separately_from_release_correctness() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    monitor = (ROOT / ".github/workflows/provider-catalog-health.yml").read_text(
        encoding="utf-8",
    )
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert ci.count('-m "not provider_health and not proof_oracle"') == 2
    # The price refresh runs no local suite: it publishes only after the exact
    # generated SHA passes ci.yml, whose shards exclude provider_health, so one
    # provider delisting a model cannot stop every other provider's prices.
    assert "uv run pytest" not in refresh
    assert "gh workflow run ci.yml" in refresh
    assert "schedule:" in monitor
    assert "workflow_dispatch:" in monitor
    assert "-m provider_health" in monitor
    # Every provider_health check, wherever it lives.
    assert "uv run pytest -q -m provider_health\n" in monitor
    assert "continue-on-error" not in monitor
    assert "provider-catalog-health.yml" not in deploy
    assert "--workflow ci.yml" in deploy


def test_proof_oracle_ci_split(request, tmp_path):
    import shlex

    import pytest

    from tests.proof_oracle_ci import check_proof_oracle_paths, proof_oracle_command

    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    job = jobs['proof-oracle']
    command = proof_oracle_command(job)
    assert command == ['uv', 'run', 'pytest', '-q', '-n', '4', '--dist', 'loadgroup',
                       '-m', 'proof_oracle', 'tests/test_async_settle_proof_oracle.py',
                       '--splits', '4', '--group', '${{', 'matrix.shard', '}}',
                       '--splitting-algorithm', 'least_duration']
    assert job['runs-on'] == 'ubuntu-latest'
    assert job['timeout-minutes'] == 45
    assert job['strategy']['matrix'] == {'clock': ['default', 'post-cutover'], 'shard': [1, 2, 3, 4]}
    assert job['strategy']['fail-fast'] is False
    assert job['permissions'] == {'contents': 'read'}
    assert 'continue-on-error' not in job
    assert any(step.get('run') == 'uv sync --frozen' for step in job['steps'])
    assert all('google-github-actions/auth' not in step.get('uses', '') for step in job['steps'])
    assert all('continue-on-error' not in step for step in job['steps'])
    pin = next(step for step in job['steps'] if 'TR_LIFECYCLE_CLOCK_OVERRIDE=' in step.get('run', ''))
    existing_pin = next(step for step in jobs['test-post-cutover']['steps']
                        if 'TR_LIFECYCLE_CLOCK_OVERRIDE=' in step.get('run', ''))
    assert pin['run'] == existing_pin['run']
    assert pin['if'] == "matrix.clock == 'post-cutover'"
    for name in ('test', 'test-post-cutover'):
        commands = [shlex.split(step['run']) for step in jobs[name]['steps']
                    if 'uv run pytest' in step.get('run', '')]
        assert len(commands) == 1
        tokens = commands[0]
        assert tokens[tokens.index('-m') + 1] == 'not provider_health and not proof_oracle'
    check_proof_oracle_paths({item.path for item in request.session.items
                              if item.get_closest_marker('proof_oracle')})
    # Negative control: moving/adding a marked file cannot silently drop it.
    with pytest.raises(AssertionError, match='missing from dedicated CI job'):
        check_proof_oracle_paths({tmp_path / 'test_unrouted_oracle.py'})


def test_ci_and_production_use_the_same_python() -> None:
    # CI previously took whatever Python the runner image shipped; a 3.11 image
    # would silently skip PEP 695 cases that production (3.12) exercises.
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned == "3.12"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"uv:python{pinned}-" in dockerfile
    refresh = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    assert f'python-version: "{pinned}"' in refresh
    # Every CI job installs through uv, which honors .python-version.
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "actions/setup-python" not in ci
    assert "astral-sh/setup-uv" in ci
    assert_ci_uses_repository_python(ci)


# Exact NAME=value text only, with a justification for each exception. Empty:
# even comments mentioning UV_ are intentionally rejected, before YAML parsing.
UV_ENV_ALLOWLIST: dict[str, str] = {}


def assert_ci_uses_repository_python(document: object, location: str = "ci") -> None:
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    for setting in re.findall(r"UV_(?:[A-Za-z0-9_]+=[^\s]+)?", text):
        assert setting in UV_ENV_ALLOWLIST, f"{location}: interpreter override: forbidden text {setting}"
    assert_ci_python_sources(yaml.safe_load(text), location)


def assert_ci_python_sources(document: object, location: str) -> None:
    """Check every workflow/job/step, including env and action inputs."""
    if isinstance(document, dict):
        for key, value in document.items():
            here = f"{location}.{key}"
            assert key not in {"python-version", "python-version-file"}, (
                f"{here}: interpreter override bypasses .python-version"
            )
            if key == "uses" and isinstance(value, str):
                assert "setup-python" not in value.lower(), f"{here}: alternate interpreter source"
            if key == "run" and isinstance(value, str):
                assert_uv_lines_allowed(value, here)
                assert ".python-version" not in value, f"{here}: rewrites the interpreter pin"
            assert_ci_python_sources(value, here)
    elif isinstance(document, list):
        for i, value in enumerate(document):
            assert_ci_python_sources(value, f"{location}[{i}]")


# Whole-line TEXT rules, with expressions only after the tool name and one
# exact Python heredoc form. Redirect/tee targets cannot carry shell syntax.
UV_ACTIONS_EXPRESSION = r"\$\{\{ *(?:env|matrix)\.[A-Za-z0-9_]+ *\}\}"
UV_REDIRECT_TARGET = r"[^\s|&;<>`$()]+"
UV_TAIL = (
    rf"(?P<tail>( +(?:[12]?>>?|&>) *{UV_REDIRECT_TARGET}| +2>&1)*"
    rf"( *\| *tee( -a)? +{UV_REDIRECT_TARGET})?)"
)
UV_PYTHON_HEREDOC = re.compile(r"""uv run python - <<(?P<quote>['"]?)(?P<tag>[A-Za-z_]+)(?P=quote)""")
UV_LINE_RULES = {
    "sync": re.compile(r"uv sync --frozen( --no-dev)?" + UV_TAIL),
    "run": re.compile(
        r"uv run( --no-sync)? (python|pytest|mypy|ruff|coverage)"
        rf"( (?:[^|&;<>`$()]|{UV_ACTIONS_EXPRESSION})*)?" + UV_TAIL,
    ),
    "python-heredoc": UV_PYTHON_HEREDOC,
}


def uv_logical_lines(script: str) -> list[str]:
    lines = []
    pending = ""
    heredoc_tag = None
    for physical in script.splitlines():
        # Body text is not shell commands. Check the exact physical terminator
        # before stripping comments or joining any backslashes in the body.
        if heredoc_tag is not None:
            if physical == heredoc_tag:
                heredoc_tag = None
            continue
        # Remove comments BEFORE continuations: a backslash inside a comment
        # cannot hide the following command. Quoted/escaped hashes stay text.
        line = re.sub(
            r"""('[^']*'|"(?:\\.|[^"\\])*"|\\.)|(?<!\S)#[^\n]*""",
            lambda match: match[1] or "",
            physical,
        )
        continued = line.endswith("\\")
        if line != physical:
            line = line.rstrip()  # Whitespace before a removed comment.
        pending += line[:-1] if continued else line
        if not continued:
            logical = pending.lstrip()
            lines.append(logical)
            if heredoc := UV_PYTHON_HEREDOC.fullmatch(logical):
                heredoc_tag = heredoc["tag"]
            pending = ""
    assert heredoc_tag is None, "CI allowlist requires a terminated Python heredoc"
    if pending:
        lines.append(pending.lstrip())
    return lines


def assert_uv_lines_allowed(script: str, location: str) -> None:
    for line in uv_logical_lines(script):
        if re.search(r"\buvx?\b", line):
            assert not re.search(r"\buvx\b", line) and any(
                rule.fullmatch(line) for rule in UV_LINE_RULES.values()
            ), (
                f"{location}: uv line not on the CI allowlist: {line}"
            )


@pytest.mark.parametrize("scope", ["workflow", "job", "step"])
@pytest.mark.parametrize("variable", [
    "UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_NO_CONFIG", "UV_CONFIG_FILE", "UV_PYTHON_INSTALL_DIR",
    "UV_FUTURE_SETTING",
])
def test_ci_python_env_override_mutations_are_detected(scope: str, variable: str) -> None:
    original = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    mutated = deepcopy(original)
    job = mutated["jobs"]["lint"]
    target = {"workflow": mutated, "job": job, "step": job["steps"][0]}[scope]
    target.setdefault("env", {})[variable] = {
        "UV_PYTHON": "3.11", "UV_PYTHON_PREFERENCE": "only-system", "UV_NO_CONFIG": "true",
        "UV_CONFIG_FILE": "another-uv.toml", "UV_PYTHON_INSTALL_DIR": "another-python",
    }.get(variable, "true")
    with pytest.raises(AssertionError, match="interpreter override"):
        assert_ci_uses_repository_python(mutated)
    assert_ci_uses_repository_python(original)


@pytest.mark.parametrize("variable", ["UV_NO_CONFIG", "UV_CONFIG_FILE", "UV_PYTHON_INSTALL_DIR"])
@pytest.mark.parametrize("shell", [
    '{variable}=true uv sync --frozen',
    'export {variable}=true\nuv sync --frozen',
    'export {variable}\nuv sync --frozen',
    'echo "{variable}=true" >> "$GITHUB_ENV"',
    'printf "%s\\n" "{variable}<<EOF" true EOF >> "$GITHUB_ENV"',
])
def test_ci_uv_shell_env_mutations_are_detected(variable: str, shell: str) -> None:
    mutated = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    mutated["jobs"]["lint"]["steps"].append({"run": shell.format(variable=variable)})
    with pytest.raises(AssertionError, match="interpreter override"):
        assert_ci_uses_repository_python(mutated)


@pytest.mark.parametrize("run", [
    "uv sync --python 3.11", "uv run --python=3.11 pytest", "uv run -p 3.11 pytest",
    "uv run -p3.11 pytest", "uv --no-config run pytest", "uv run --no-config pytest",
    "uv run --project . --python 3.11 pytest", "uv run \\\n  --python 3.11 pytest",
    "UV_PYTHON=3.11 uv sync", 'echo "UV_PYTHON_PREFERENCE=only-system" >> "$GITHUB_ENV"',
    "uv run pytest # initial check\nuv run --python 3.11 pytest",
])
def test_ci_python_command_override_mutations_are_detected(run: str) -> None:
    mutated = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    mutated["jobs"]["lint"]["steps"].append({"run": run})
    with pytest.raises(AssertionError, match="override|allowlist"):
        assert_ci_uses_repository_python(mutated)


@pytest.mark.parametrize("step", [
    {"uses": "actions/setup-python@v6", "with": {"python-version": "3.11"}},
    {"uses": "astral-sh/setup-uv@v7", "with": {"python-version": "3.11"}},
    {"uses": "astral-sh/setup-uv@v7", "with": {"python-version-file": "another-pin"}},
])
def test_ci_alternate_python_source_mutations_are_detected(step: dict[str, object]) -> None:
    mutated = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    mutated["jobs"]["lint"]["steps"].append(step)
    with pytest.raises(AssertionError, match="interpreter"):
        assert_ci_uses_repository_python(mutated)


def test_ci_python_check_allows_unrelated_command_flags() -> None:
    assert_ci_uses_repository_python({"jobs": {"example": {"steps": [{"run": (
        "uv run --no-sync python -c '\"--python\"'\n"
        "go test -p 1 ./...\nuv run pytest -p no:cacheprovider\n"
    )}]}}})


@pytest.mark.parametrize("command", [
    "uvx --python 3.11 ruff check .",
    "uv python pin 3.11 && uv sync",
    "echo 3.11 > .python-version && uv sync",
    "uvx --from ruff --python 3.11 ruff check .",
    "uvx --with x --python 3.11 ruff",
    "uv --quiet python pin 3.11",
    "uvx -w x --python 3.11 ruff",
    "uv tool run --from ruff --python 3.11 ruff",
    "uv --directory . python pin 3.11",
    "uv run -w x --python 3.11 pytest",
    "uv run --env-file .env --python 3.11 pytest",
    "uv sync --frozen --python 3.11",
])
def test_ci_python_pin_bypass_mutations_are_detected(command: str) -> None:
    mutated = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    mutated["jobs"]["lint"]["steps"].append({"run": command})
    with pytest.raises(AssertionError):
        assert_ci_uses_repository_python(mutated)


def test_uv_run_tool_options_are_not_interpreter_flags() -> None:
    # Options after the tool belong to the tool (pytest -p plugins, ruff -p...).
    assert_uv_lines_allowed("uv run pytest -p no:cacheprovider", "ci")
    assert_uv_lines_allowed("uv run --no-sync python -c 'pass'", "ci")


@pytest.mark.parametrize("script", [
    "uv sync --frozen # install\necho done",
    "uv sync --frozen # don't parse 'comment quotes\necho done",
    "uv sync --frozen > /tmp/sync.log",  # noqa: S108 -- parsed shell text, never executed
    "uv run pytest 2>&1 | tee log",
    "uv sync --frozen >> log", "uv sync --frozen 2> log",
    "uv sync --frozen &> log", "uv sync --frozen 2>&1", "uv sync --frozen | tee log",
])
def test_ci_uv_allows_shell_comments_and_redirections(script: str) -> None:
    assert_ci_uses_repository_python({"run": script})


def test_ci_uv_rejects_override_after_inline_comment() -> None:
    with pytest.raises(AssertionError, match="allowlist") as caught:
        assert_ci_uses_repository_python({"run": "uv sync --frozen # c\nuv sync --python 3.11"})
    assert "uv sync --python 3.11" in str(caught.value)


def test_ci_uv_does_not_strip_quoted_hash_arguments() -> None:
    with pytest.raises(AssertionError, match="allowlist"):
        assert_ci_uses_repository_python({"run": 'uv sync --frozen "#argument"'})


@pytest.mark.parametrize("script", [
    "uv sync --frozen >/dev/null --python 3.11",
    "uv sync --frozen # install \\\nuv sync --frozen --python 3.11",
    "env 'UV_NO_CONFIG'=true uv sync --frozen",
    "uv sync --frozen; echo done",
    "uv sync --frozen && echo done",
    "uv sync --frozen || echo done",
    "(uv sync --frozen)",
    "uv run pytest `echo --python=3.11`",
    "uv run pytest $(echo --python=3.11)",
    "uv sync --frozen < input",
])
def test_ci_uv_strict_text_mutations_are_detected(script: str) -> None:
    with pytest.raises(AssertionError, match="override|allowlist"):
        assert_ci_uses_repository_python({"run": script})


@pytest.mark.parametrize("addition", [
    "# UV_NO_CONFIG is deliberately forbidden even in comments\n",
    "env:\n  SETTING: UV_NO_CONFIG\n",
    "env:\n  PREFIX_UV_NO_CONFIG: true\n",
    "name: UV_\n",
    "run: echo UV_NO_CONFIG\n",
])
def test_ci_rejects_uv_substrings_anywhere_in_workflow_text(addition: str) -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    with pytest.raises(AssertionError, match="interpreter override"):
        assert_ci_uses_repository_python(ci + "\n" + addition)


@pytest.mark.parametrize("script", [
    "uv run pytest --group ${{ matrix.shard }}",
    "uv run --no-sync pytest --splits ${{env.TEST_SHARDS}} --group ${{  matrix.shard  }}",
])
def test_ci_allows_github_expressions_only_in_tool_arguments(script: str) -> None:
    assert_ci_uses_repository_python({"run": script})


@pytest.mark.parametrize("script", [
    "uv run ${{ env.X }} pytest",
    "uv sync --frozen ${{ env.X }}",
    "uv run pytest $(echo x)",
    "uv run pytest $X",
    "uv run pytest --group ${{ secrets.X }}",
    "uv run pytest --group ${{ matrix.shard || 1 }}",
    "uv run python - <<'PY' extra",
    "uv sync --frozen >$(echo log)",
    "uv run pytest >log;echo",
    "uv run pytest >log&&echo",
    "uv run pytest >log||echo",
    "uv run pytest >`echo log`",
])
def test_ci_rejects_shell_syntax_outside_narrow_exceptions(script: str) -> None:
    with pytest.raises(AssertionError, match="allowlist"):
        assert_ci_uses_repository_python({"run": script})


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_ci_python_heredoc_exempts_only_body_lines(quote: str) -> None:
    script = f"uv run python - <<{quote}PY{quote}\nuv sync --python 3.11\nPY\n"
    assert_ci_uses_repository_python({"run": script})
    with pytest.raises(AssertionError, match="uv sync --python 3.11"):
        assert_ci_uses_repository_python({"run": script + "uv sync --python 3.11\n"})


def test_ci_python_heredoc_still_rejects_raw_uv_settings() -> None:
    with pytest.raises(AssertionError, match="interpreter override"):
        assert_ci_uses_repository_python({"run": "uv run python - <<'PY'\n# UV_NO_CONFIG\nPY\n"})


def test_ci_python_heredoc_body_backslash_cannot_hide_terminator() -> None:
    script = "uv run python - <<'PY'\n# body \\\nPY\nuv sync --python 3.11\n"
    with pytest.raises(AssertionError, match="uv sync --python 3.11"):
        assert_ci_uses_repository_python({"run": script})
