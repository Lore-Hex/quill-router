from __future__ import annotations

import io
import json
import posixpath
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.ci.commit_scope import (
    LIFECYCLE_MODULE,
    PATH_FLAGS,
    PATH_SCOPED_JOBS,
    SKIPPED_FOR_DATA_ONLY,
    Change,
    changed_lines,
    classify,
    is_data_change,
    is_data_path,
    main,
    plain_change,
    plain_changes,
    scheduled_entities,
    scope_jobs,
    summarize,
)

ROOT = Path(__file__).resolve().parents[1]

SNAPSHOT = "src/trusted_router/data/openrouter_snapshot.json"
MANIFEST = "src/trusted_router/data/provider_models/fireworks.json"
CARD = "src/trusted_router/static/og/providers/fireworks.png"
CARDS = "src/trusted_router/static/og/providers/manifest.json"
HISTORY = "src/trusted_router/data/model_changes.jsonl"
GO = "fastpath/internal/store/ids.go"


def raw(path: str, status: str = "M", old: str = "100644", new: str = "100644") -> str:
    """One record of `git diff --no-renames --raw` without -z."""
    return f":{old} {new} 1a2b3c4 5d6e7f8 {status}\t{path}"


def added(path: str, mode: str = "100644") -> str:
    return raw(path, "A", "000000", mode)


def deleted(path: str, mode: str = "100644") -> str:
    return raw(path, "D", mode, "000000")


@pytest.mark.parametrize("path", [
    SNAPSHOT, MANIFEST, CARD, CARDS, HISTORY,
    "src/trusted_router/data/model_changes_state.json",
    "src/trusted_router/data/speech_pricing.json",
    "src/trusted_router/data/transcription_pricing.json",
    "src/trusted_router/data/provider_routing_evidence.json",
])
def test_published_catalog_files_are_data(path: str) -> None:
    assert is_data_path(path)
    assert is_data_change(raw(path))
    assert is_data_change(added(path))
    assert is_data_change(deleted(path))


@pytest.mark.parametrize("path", [
    "scripts/pricing/parsers/fireworks.py",
    "fastpath/testdata/model_alias_vectors_v1.json",
    "src/trusted_router/catalog_data.py",
    "src/trusted_router/data/provider_models/nested/fireworks.json",
    "src/trusted_router/data/provider_models/fireworks.py",
    "src/trusted_router/data/other.json",
    "src/trusted_router/static/og/providers/sub/x.png",
    "src/trusted_router/static/og/providers/new.js",
    "src/trusted_router/static/og/providers/ids.txt",
    "src/trusted_router/static/og/providers/other.json",
    ".github/workflows/ci.yml",
    "tests/test_catalog_routing_contracts.py",
    "/src/trusted_router/data/openrouter_snapshot.json",
    "src/trusted_router/data/../data/openrouter_snapshot.json",
    '"src/trusted_router/data/openrouter_snapshot.json"',
    "",
])
def test_everything_else_is_code(path: str) -> None:
    assert not is_data_path(path)
    assert not is_data_change(raw(path))


@pytest.mark.parametrize(
    "path",
    [
        "src/trusted_router/data/openrouter_snapshot.json ",
        " src/trusted_router/data/openrouter_snapshot.json",
        "src/trusted_router/data/openrouter_snapshot.json\t",
        "src/trusted_router/data/provider_models/a b.json",
    ],
)
def test_a_path_with_whitespace_is_not_the_data_file(path: str) -> None:
    """Git prints a name with a trailing space unquoted; stripping it would
    let a file that is not the allowlisted one count as data."""
    assert is_data_path(path) is False
    record = f":000000 100644 0000000 1111111 A\t{path}"
    assert is_data_change(record) is False


def test_only_regular_file_add_modify_delete_records_are_data_changes() -> None:
    # A rename or copy record (only produced without --no-renames) is never
    # data: its source may be code, and the record names the destination.
    assert not is_data_change(f":100644 100644 1a2b3c4 5d6e7f8 R100\t{GO}\t{CARD}")
    assert not is_data_change(f":100644 100644 1a2b3c4 5d6e7f8 C090\t{SNAPSHOT}\t{MANIFEST}")
    assert not is_data_change(raw(SNAPSHOT, "T"))
    assert not is_data_change(raw(SNAPSHOT, "U"))
    # Symlinks, submodules and executables at a data path are not data.
    assert not is_data_change(added(SNAPSHOT, "120000"))
    assert not is_data_change(raw(SNAPSHOT, "T", "100644", "120000"))
    assert not is_data_change(raw(SNAPSHOT, "M", "120000", "120000"))
    assert not is_data_change(added(CARD, "160000"))
    assert not is_data_change(raw(MANIFEST, "M", "100644", "100755"))
    assert not is_data_change(deleted(MANIFEST, "100755"))
    # Only a raw record is accepted: a bare path (the --name-only form) is not.
    assert not is_data_change(SNAPSHOT)
    assert not is_data_change(f"M\t{SNAPSHOT}")
    assert not is_data_change(f":100644 100644 1a2b3c4 5d6e7f8 M {SNAPSHOT}")
    # Full and abbreviated object ids both appear in raw output.
    assert is_data_change(f":100644 100644 {'a' * 40} {'b' * 40} M\t{SNAPSHOT}")
    assert is_data_change(f":100644 100644 1a2b3c4... 5d6e7f8... M\t{SNAPSHOT}")


def test_classify_requires_a_non_empty_all_data_diff() -> None:
    assert classify([raw(SNAPSHOT), added(MANIFEST), deleted(CARD), raw(HISTORY)])
    assert classify([raw(SNAPSHOT), "", "  "])
    assert not classify([])
    assert not classify(["", "\n"])
    assert not classify([raw(SNAPSHOT), raw("scripts/pricing/parsers/fireworks.py")])
    assert not classify([raw(SNAPSHOT), raw("fastpath/testdata/model_alias_vectors_v1.json")])
    assert not classify([raw(SNAPSHOT), raw("src/trusted_router/catalog_data.py")])
    # A code file moved onto a data path, as --no-renames reports it: the
    # deletion of the code path keeps the commit off the data-only run.
    assert not classify([deleted(GO), added(CARD)])
    assert not classify([raw(SNAPSHOT), f":100644 100644 1a2b3c4 5d6e7f8 R100\t{GO}\t{CARD}"])




def needs(outputs: dict[str, str] | None = None, **results: str) -> dict[str, dict[str, object]]:
    """The summary job's NEEDS. The scope job's flags default to `true`, so
    only the data-only rule can excuse a skip unless a test sets one."""
    base: dict[str, dict[str, object]] = {name: {"result": "success"} for name in
                                          ("lint", "production-import", "test", "test-post-cutover", "browser", "browser-post-cutover",
                                           *SKIPPED_FOR_DATA_ONLY)}
    base["scope"] = {"result": "success",
                     "outputs": {"data_only": "false", **dict.fromkeys(PATH_FLAGS, "true"), **(outputs or {})}}
    for name, result in results.items():
        job = name.replace("_", "-")
        base[job] = {**base.get(job, {}), "result": result}
    return base


def test_every_job_succeeding_is_success_on_either_commit() -> None:
    assert summarize(needs(), "false") == []
    assert summarize(needs(), "true") == []


def test_heavy_jobs_may_skip_only_on_a_data_only_commit() -> None:
    skipped = {name: "skipped" for name in SKIPPED_FOR_DATA_ONLY}
    assert summarize(needs(**skipped), "true") == []
    failures = summarize(needs(**skipped), "false")
    assert set(failures) == {f"{name}: skipped" for name in SKIPPED_FOR_DATA_ONLY}
    assert summarize(needs(**skipped), "") != []
    assert summarize(needs(proof_oracle="skipped"), "TRUE") == ["proof-oracle: skipped"]


@pytest.mark.parametrize("job", ["lint", "production-import", "test", "browser"])
def test_a_job_that_reads_the_data_may_never_skip(job: str) -> None:
    every_flag_false = dict.fromkeys(PATH_FLAGS, "false")
    assert summarize(needs(every_flag_false, **{job: "skipped"}), "true") == [f"{job}: skipped"]


def test_the_post_cutover_suite_reads_the_data_and_skips_only_on_its_flag() -> None:
    # Not a data-only job: a data-only commit does not excuse its skip.
    assert "test-post-cutover" not in SKIPPED_FOR_DATA_ONLY
    assert summarize(needs(test_post_cutover="skipped"), "true") == ["test-post-cutover: skipped"]
    assert summarize(needs({"run_post_cutover": "false"}, test_post_cutover="skipped"), "false") == []


def test_base_ci_may_skip_only_when_the_skips_do_not_rest_on_a_running_base() -> None:
    # base-ci waits for the base's own push run when the scope job took the
    # data-only skips before it concluded (base_pending). Its skip is a
    # failure then: the induction the skips rely on was never checked.
    assert summarize(needs({"base_pending": "false"}, base_ci="skipped"), "true") == []
    assert summarize(needs(base_ci="skipped"), "true") == []  # an older scope job sets no flag
    assert summarize(needs({"base_pending": "true"}, base_ci="skipped"), "true") == ["base-ci: skipped"]
    assert summarize(needs({"base_pending": "true"}, base_ci="success"), "true") == []
    for result in ("failure", "cancelled", None):
        assert summarize(needs({"base_pending": "true"}, base_ci=result), "true") == [f"base-ci: {result}"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_scope_must_succeed(result: str | None) -> None:
    assert summarize(needs(scope=result), "true") == [f"scope: {result}"]
    assert summarize(needs(scope=result), "false") == [f"scope: {result}"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "timed_out", None])
def test_any_other_result_fails_on_either_commit(result: str | None) -> None:
    every_flag_false = dict.fromkeys(PATH_FLAGS, "false")
    for data_only in ("true", "false"):
        assert summarize(needs(test=result), data_only) == [f"test: {result}"]
        assert summarize(needs(proofs=result), data_only) == [f"proofs: {result}"]
        # A flag that lets a job skip never excuses a job that ran and failed.
        assert summarize(needs(every_flag_false, proofs=result), data_only) == [f"proofs: {result}"]


def test_a_skipped_job_outside_the_list_fails_even_on_a_data_only_commit() -> None:
    assert summarize({**needs(), "new-job": {"result": "skipped"}}, "true") == ["new-job: skipped"]


@pytest.mark.parametrize(("job", "flag"), sorted(PATH_SCOPED_JOBS.items()))
def test_a_path_scoped_job_may_skip_only_when_its_own_flag_is_false(job: str, flag: str) -> None:
    assert PATH_FLAGS == ("run_fastpath", "run_heavy", "run_post_cutover", "run_proof_oracle", "run_proofs")
    assert summarize(needs({flag: "false"}, **{job: "skipped"}), "false") == []
    for value in ("true", "", "FALSE", "False", "no", "0"):
        assert summarize(needs({flag: value}, **{job: "skipped"}), "false") == [f"{job}: skipped"], value
    # Another job's flag does not excuse it.
    others = {other: "false" for other in PATH_FLAGS if other != flag}
    assert summarize(needs(others, **{job: "skipped"}), "false") == [f"{job}: skipped"]
    # No output at all (a scope job that wrote none) excuses nothing.
    bare = needs(**{job: "skipped"})
    bare["scope"] = {"result": "success"}
    assert summarize(bare, "false") == [f"{job}: skipped"]


def test_a_push_excuses_only_the_post_cutover_suite() -> None:
    # The owner's trade-off: the post-cutover suite runs on pull requests and
    # dispatches, not on a push to main. Every other path-scoped job, the
    # model checks among them, skips on a push only when its flag is false.
    from scripts.ci.commit_scope import SKIPPED_ON_PUSH

    assert SKIPPED_ON_PUSH == {"test-post-cutover"}
    assert summarize(needs(test_post_cutover="skipped"), "false", "push") == []
    for event in ("pull_request", "workflow_dispatch", "schedule", "", "PUSH"):
        assert summarize(needs(test_post_cutover="skipped"), "false", event) == ["test-post-cutover: skipped"], event
    for job in ("proofs", "proof-oracle", "fastpath", "fastpath-spanner", "fastpath-walks", "test", "lint"):
        assert summarize(needs(**{job: "skipped"}), "false", "push") == [f"{job}: skipped"], job
    # A push excuses a skip, never a failure.
    for result in ("failure", "cancelled", "timed_out", None):
        assert summarize(needs(test_post_cutover=result), "false", "push") == [f"test-post-cutover: {result}"]


@pytest.mark.parametrize("separator", [" ", " ", "\u0085"])
def test_a_unicode_line_separator_in_a_path_is_not_the_data_file(monkeypatch, capsys, separator: str) -> None:
    """git prints these literally when core.quotePath is off; splitting on
    them would strip the separator and leave the allowlisted name."""
    record = f":000000 100644 0000000 0967ef4 A\tsrc/trusted_router/data/openrouter_snapshot.json{separator}\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(record))
    assert main(["classify"]) == 0
    assert capsys.readouterr().out == "false\n"


def test_classify_command_reads_raw_records_from_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw(SNAPSHOT)}\n{added(MANIFEST)}\n"))
    assert main(["classify"]) == 0
    assert capsys.readouterr().out == "true\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw(SNAPSHOT)}\n{raw('src/trusted_router/main.py')}\n"))
    assert main(["classify"]) == 0
    assert capsys.readouterr().out == "false\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["classify"]) == 0
    assert capsys.readouterr().out == "false\n"


def test_summarize_command_exits_non_zero_with_an_annotation(monkeypatch, capsys) -> None:
    skipped = {name: "skipped" for name in SKIPPED_FOR_DATA_ONLY}
    monkeypatch.setenv("NEEDS", json.dumps(needs(**skipped)))
    monkeypatch.setenv("DATA_ONLY", "true")
    assert main(["summarize"]) == 0
    assert "::error" not in capsys.readouterr().out
    monkeypatch.setenv("DATA_ONLY", "false")
    assert main(["summarize"]) == 1
    out = capsys.readouterr().out
    assert "::error title=CI job did not succeed::proof-oracle: skipped" in out
    monkeypatch.delenv("DATA_ONLY")
    monkeypatch.setenv("NEEDS", json.dumps(needs(test="failure")))
    assert main(["summarize"]) == 1
    assert "::error title=CI job did not succeed::test: failure" in capsys.readouterr().out
    # The event reaches the summary: a push excuses the post-cutover skip.
    monkeypatch.setenv("NEEDS", json.dumps(needs(test_post_cutover="skipped")))
    monkeypatch.setenv("EVENT_NAME", "push")
    assert main(["summarize"]) == 0
    assert "event_name: push" in capsys.readouterr().out
    monkeypatch.delenv("EVENT_NAME")
    # The scope job's flags reach the summary through NEEDS.
    monkeypatch.setenv("NEEDS", json.dumps(needs(test_post_cutover="skipped")))
    assert main(["summarize"]) == 1
    assert "test-post-cutover: skipped" in capsys.readouterr().out
    monkeypatch.setenv("NEEDS", json.dumps(needs({"run_post_cutover": "false"}, test_post_cutover="skipped")))
    assert main(["summarize"]) == 0
    out = capsys.readouterr().out
    assert "::error" not in out and "run_post_cutover: false" in out


# ---------------------------------------------------------------------------
# Path-scoped flags

NOW = datetime(2026, 11, 16, tzinfo=UTC)
LIFECYCLE = '''
from datetime import UTC, datetime

PAST_AT = datetime(2026, 10, 1, tzinfo=UTC)
AHEAD_AT = datetime(2026, 12, 11, tzinfo=UTC)
_MODEL = "sakana/namazu"
_OFF_PEAK = {"tencent/hy4-turbo": 1}

_RETIREMENTS = (
    _Retirement(
        provider="oldcloud",
        model_ids=frozenset({"oldcloud/gone-1"}),
        upstream_ids=frozenset({"gone-1-upstream"}),
        effective_at=PAST_AT,
    ),
    _Retirement(
        provider="nextcloud",
        model_ids=frozenset({"vendor/soon-2"}),
        upstream_ids=frozenset({"soon-2-upstream"}),
        effective_at=AHEAD_AT,
        replacement_model_ids=("vendor/after-3",),
    ),
    _Retirement(
        provider="mystery",
        model_ids=frozenset({"vendor/unknown-4"}),
        upstream_ids=frozenset(),
        effective_at=_compute(),
    ),
)


def provider_price_microdollars(provider_slug, model_id, *, at=None):
    if provider_slug == "deepseek":
        return 1
    if model_id == _MODEL or model_id in _OFF_PEAK:
        return 2
    if period == "peak":
        return 3
'''

ORACLE_FILE, GATE_FILE = "tests/test_async_settle_proof_oracle.py", "tests/test_async_settle_shadow_v2_gate_window.py"


def write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small repository with each file a rule reads from HEAD."""
    write(tmp_path, {
        "proofs/manifest.toml": (
            '[Spec]\nstate = "implemented"\ncode = ["fastpath/internal/spec", "src/trusted_router/spec_code.py"]\n'
            'tests = ["tests/test_spec_code.py"]\n[[Spec.mutants]]\nname = "m"\n'
            '[Planned]\nstate = "planned"\ncode = ["src/trusted_router/planned.py"]\n'),
        "src/trusted_router/provider_lifecycle.py": LIFECYCLE,
        "tests/__init__.py": "",
        "tests/conftest.py": "import tests.harness_helper\n",
        "tests/harness_helper.py": "from tests.fakes import store\n",
        "tests/fakes/__init__.py": "",
        "tests/fakes/store.py": "",
        ORACLE_FILE: "from tests.oracle_helper import CASES\nfrom . import relative_helper\n",
        GATE_FILE: "import importlib\nNAMES = ('tests.string_named',)\n",
        "tests/oracle_helper.py": "from scripts.oracle_tool import run\nfrom tests.removed_module import x\n",
        "tests/relative_helper.py": "",
        "tests/string_named.py": "",
        "tests/unrelated_test.py": "import trusted_router.main\n",
        "scripts/oracle_tool.py": "",
    })
    return tmp_path


def scope(changes: list[str], root: Path, *, event: str = "pull_request", lines: str | None = "",
          now: datetime = NOW) -> dict[str, bool]:
    flags, _, _ = scope_jobs(changes, root=root, event=event, lines=lines, now=now)
    assert set(flags) == set(PATH_FLAGS)
    return flags


def ordinary(changes: list[str]) -> str:
    """A patch giving each changed file one line that names nothing."""
    return "".join(patch(record.split("\t", 1)[1], "+an ordinary line") for record in changes)


def selected(changes: list[str], root: Path, *, lines: str | None = None,
             read: bool = True) -> tuple[bool, tuple[str, ...]]:
    """Whether the post-cutover suite runs, and the test files it is limited
    to (empty: the whole suite). Without `lines`, every changed file's patch
    has one line that names nothing; read=False passes no patch at all."""
    if lines is None and read:
        lines = ordinary(changes)
    flags, _, tests = scope_jobs(changes, root=root, event="pull_request", lines=lines, now=NOW)
    assert not tests or flags["run_post_cutover"]
    return flags["run_post_cutover"], tests


def patch(path: str, *lines: str) -> str:
    """A `git diff -U0` section for path whose changed lines are `lines`."""
    body = "\n".join(lines)
    return f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}\n"


def test_plain_changes_are_regular_file_adds_modifies_and_deletes() -> None:
    assert plain_change(raw(GO)) == Change("M", GO)
    assert plain_change(raw(GO, "M", "100755", "100755")) == Change("M", GO)
    assert plain_change(added(GO)) == Change("A", GO)
    assert plain_change(added(GO, "100755")) == Change("A", GO)
    assert plain_change(deleted(GO)) == Change("D", GO)
    assert plain_change(f":100644 100644 {'a' * 40} {'b' * 40} M\t{GO}") == Change("M", GO)
    assert plain_change("  ") is None


@pytest.mark.parametrize("record", [
    f":100644 100644 1a2b3c4 5d6e7f8 R100\t{GO}\tfastpath/x.go",
    f":100644 100644 1a2b3c4 5d6e7f8 C090\t{GO}\tfastpath/x.go",
    raw(GO, "T", "100644", "120000"),
    raw(GO, "M", "100644", "100755"),
    raw(GO, "M", "100755", "100644"),
    raw(GO, "U"),
    raw(GO, "X"),
    added(GO, "120000"),
    added(GO, "160000"),
    deleted(GO, "120000"),
    raw(GO, "M", "120000", "120000"),
    raw('"fastpath/\\303\\251.go"'),
    raw(GO, "A"),
    raw(GO, "D"),
    GO,
    f"M\t{GO}",
    f":100644 100644 1a2b3c4 5d6e7f8 M {GO}",
])
def test_anything_but_a_plain_change_runs_every_path_scoped_job(tree: Path, record: str) -> None:
    assert plain_change(record) is None
    assert plain_changes([raw("docs/a.md"), record]) is None
    flags, notes, tests = scope_jobs([raw("docs/a.md"), record], root=tree, event="push", lines="", now=NOW)
    assert flags == dict.fromkeys(PATH_FLAGS, True) and tests == ()
    assert "not an add, modify or delete of a regular file" in notes[0]


def test_an_empty_comparison_runs_every_path_scoped_job(tree: Path) -> None:
    for records in ([], [""], ["", "  "]):
        flags, notes, tests = scope_jobs(records, root=tree, event="push", lines="", now=NOW)
        assert flags == dict.fromkeys(PATH_FLAGS, True) and tests == ()
        assert "empty" in notes[0]


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/ci/commit_scope.py",
                                  "scripts/ci/new_helper.py", "scripts/ci/sub/x.sh",
                                  ".gitattributes", "docs/.gitattributes", "fastpath/testdata/.gitattributes"])
def test_a_change_to_how_ci_decides_runs_every_path_scoped_job(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        assert scope([raw("docs/a.md"), record], tree) == dict.fromkeys(PATH_FLAGS, True)


@pytest.mark.parametrize("path", [".github/workflows/post-cutover-daily.yml", "scripts/cix/x.py",
                                  "docs/ci.yml", "tests/test_ci_workflow.py", "docs/gitattributes.md",
                                  "docs/x.gitattributes"])
def test_other_ci_files_do_not_run_everything(tree: Path, path: str) -> None:
    assert not all(scope([raw(path)], tree).values())


def test_a_change_no_rule_names_skips_every_path_scoped_job(tree: Path) -> None:
    # Prose and workflow files that name nothing scheduled.
    changes = [raw("docs/design/x.md"), added("README.md"), raw(".github/workflows/deploy.yml"),
               deleted(".github/CODEOWNERS")]
    lines = (patch("docs/design/x.md", "+A paragraph about latency.") + patch("README.md", "+Hello")
             + patch(".github/workflows/deploy.yml", "+  timeout-minutes: 30")
             + patch(".github/CODEOWNERS", "-* @owner"))
    assert scope(changes, tree, lines=lines) == dict.fromkeys(PATH_FLAGS, False)


@pytest.mark.parametrize("path", [
    "docs/catalog_expectations.json", "docs/models.txt", "infra/expected.yaml", "config/limits.yml",
    "data/routes.jsonl", "pricing/table.csv", "pricing/table.tsv", "settings.toml", "probe.ini",
    "probe.cfg", "feed.xml", "docs/UPPER.JSON",
])
def test_a_data_file_runs_the_whole_post_cutover_suite(tree: Path, path: str) -> None:
    # A test may load expectations from it through a path it builds: the
    # review's case is docs/catalog_expectations.json going from 659 to 679,
    # which names nothing scheduled.
    lines = patch(path, '-{"models": 659}', '+{"models": 679}')
    for record in (raw(path), added(path), deleted(path)):
        assert selected([record], tree, lines=lines) == (True, ()), record


@pytest.mark.parametrize("path", [
    "infra/main.tf", "scripts/deploy/rollout.sh", "fastpath/internal/edge/edge.go", "proofs/CreditDebt.tla",
    "frontend/src/old.ts", "sites/token-exchange/new.html", "sites/token-exchange/site.css",
    "src_static/x.js", "Dockerfile", "docs/catalog.md", ".github/workflows/synthetic.yml",
    ".github/dependabot.yml",
])
def test_code_and_config_run_it_only_when_they_name_the_lifecycle(tree: Path, path: str) -> None:
    # An expectation that changes past the cutovers without naming anything
    # scheduled comes from catalog-shaped data; code and configuration name
    # their models, and the line scan sees those. Workflow YAML is
    # configuration, not a data file a test loads expectations from.
    quiet = patch(path, "-count = 659", "+count = 679")
    for record in (raw(path), added(path), deleted(path)):
        assert selected([record], tree, lines=quiet) == (False, ()), record
        assert selected([record], tree, lines=patch(path, "+model = vendor/soon-2")) == (True, ()), record
        assert selected([record], tree, lines=patch(path, "+# retires at the cutover")) == (True, ()), record


def test_markdown_and_github_files_run_it_only_when_they_name_the_lifecycle(tree: Path) -> None:
    # Prose does not feed the catalog expectations tests assert: the accepted
    # exception. A Markdown or .github/ line naming something scheduled still
    # runs the whole suite.
    for path in ("docs/catalog.md", ".github/workflows/synthetic.yml"):
        assert selected([raw(path)], tree, lines=patch(path, "-659 models", "+679 models")) == (False, ()), path
        assert selected([raw(path)], tree, lines=patch(path, "+vendor/soon-2")) == (True, ()), path


@pytest.mark.parametrize("path", [
    "fastpath/internal/store/ids.go", "fastpath/go.mod", "fastpath/testdata/load-mix.json",
    "proofs/CreditDebt.tla", "proofs/tla2tools.jar.sha256",
    "scripts/deploy/migrate_fastpath.sh", "scripts/deploy/rollout.sh",
    "tests/fixtures/usage_cost_vectors.json",
])
def test_the_fast_path_jobs_run_on_what_the_go_tests_read(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        assert scope([raw("docs/a.md"), record], tree)["run_fastpath"], record


@pytest.mark.parametrize("path", [
    "scripts/deploy/lib/x.sh", "scripts/deploy/cloud_rollout.py", "scripts/fastpath/model_alias_vectors.py",
    "tests/fixtures/other_vectors.json", "src/trusted_router/main.py", "fastpathx/a.go", "docs/fastpath.md",
])
def test_the_fast_path_jobs_skip_what_the_go_tests_do_not_read(tree: Path, path: str) -> None:
    assert not scope([raw(path)], tree)["run_fastpath"]


@pytest.mark.parametrize("path", [
    "proofs/README.md", "proofs/manifest.toml", "proofs/check.sh", "proofs/New.tla",
    "fastpath/internal/spec/spec.go", "fastpath/internal/spec/deep/spec_test.go",
    "src/trusted_router/spec_code.py", "tests/test_spec_code.py", "src/trusted_router/planned.py",
])
def test_the_model_checks_run_on_proofs_and_what_the_manifest_names(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        assert scope([record], tree)["run_proofs"], record


@pytest.mark.parametrize("path", [
    "fastpath/internal/specx/a.go", "fastpath/internal/store/ids.go", "src/trusted_router/spec_code.pyc",
    "tests/test_spec_code_extra.py", "src/trusted_router/main.py", "docs/proofs.md",
])
def test_the_model_checks_skip_what_the_manifest_does_not_name(tree: Path, path: str) -> None:
    assert not scope([raw(path)], tree)["run_proofs"]


@pytest.mark.parametrize("path", [
    "src/trusted_router/main.py", "src/trusted_router/storage_gcp_settle_outbox.py",
    "src/trusted_router/data/model_routing_evidence.json", "src/trusted_router/templates/x.html",
    "src/sibling_package/module.py", "src/README.md", "pytest.ini", "tox.ini", "setup.cfg",
    ORACLE_FILE, GATE_FILE, "tests/conftest.py", "tests/harness_helper.py", "tests/fakes/store.py",
    "tests/fakes/__init__.py", "tests/__init__.py", "tests/oracle_helper.py", "tests/relative_helper.py",
    "tests/string_named.py", "scripts/oracle_tool.py", "tests/removed_module.py",
    "tests/fakes/frozen_main/BASE", "tests/fixtures/async_settlement/billing_v1.json",
    "pyproject.toml", "uv.lock", ".python-version", "conftest.py", ".test_durations",
])
def test_the_oracle_runs_on_the_application_and_what_its_tests_load(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        assert scope([raw("docs/a.md"), record], tree)["run_proof_oracle"], record


@pytest.mark.parametrize("path", [
    SNAPSHOT, MANIFEST, CARD, CARDS, HISTORY, "tests/unrelated_test.py", "tests/test_spec_code.py",
    "scripts/other_tool.py", ".test_durations-post-cutover", "fastpath/internal/store/ids.go",
    "proofs/CreditDebt.tla", "docs/a.md", "infra/main.tf",
])
def test_the_oracle_skips_catalog_data_and_tests_it_does_not_load(tree: Path, path: str) -> None:
    assert not scope([raw(path)], tree)["run_proof_oracle"]


def test_the_oracle_follows_imports_from_heads_sources(tree: Path) -> None:
    # A test the oracle did not load joins its closure once an oracle helper
    # imports it, and the deletion of a module it imports is a change to it.
    assert not scope([raw("tests/unrelated_test.py")], tree)["run_proof_oracle"]
    write(tree, {"tests/oracle_helper.py": "import tests.unrelated_test\n"})
    assert scope([raw("tests/unrelated_test.py")], tree)["run_proof_oracle"]
    assert scope([deleted("tests/removed_module.py")], tree)["run_proof_oracle"] is False
    write(tree, {"tests/oracle_helper.py": "from tests.removed_module import x\n"})
    assert scope([deleted("tests/removed_module.py")], tree)["run_proof_oracle"]


@pytest.mark.parametrize("path", [
    "src/trusted_router/main.py", SNAPSHOT, MANIFEST, CARD, CARDS, HISTORY,
    "src/trusted_router/provider_lifecycle.py", "src/trusted_router/catalog_registry.py",
    "scripts/pricing/parsers/fireworks.py", "scripts/pricing/refresh_inputs.py", "scripts/update_model_changes.py",
    "fastpath/testdata/model_alias_vectors_v1.json", ".test_durations-post-cutover",
    "pyproject.toml", "uv.lock", ".python-version", "conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
    "tests/conftest.py", "tests/harness_helper.py", "tests/fakes/store.py", "tests/fixtures/vehicle_routes.json",
    "src/sibling_package/module.py", "src/README.md",
])
def test_the_post_cutover_suite_runs_whole_on_the_application_its_data_and_harness(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        # No changed lines name anything: the path alone decides, and it is
        # the whole suite, not a selection.
        lines = patch("docs/a.md", "+notes") + patch(path, "+an ordinary line")
        assert selected([raw("docs/a.md"), record], tree, lines=lines) == (True, ()), record
        _, notes, _ = scope_jobs([raw("docs/a.md"), record], root=tree, event="pull_request", lines=lines, now=NOW)
        assert f"run_post_cutover: runs, {path}" in notes, (record, notes)


def test_every_catalog_data_path_runs_the_post_cutover_suite(tree: Path) -> None:
    # The price refresh's data-only commits keep running it.
    from scripts.ci.commit_scope import DATA_ONLY_PATHS

    for pattern in DATA_ONLY_PATHS:
        path = pattern.replace("*", "example")
        assert is_data_path(path)
        assert scope([raw(path)], tree, event="push")["run_post_cutover"], path


@pytest.mark.parametrize("line", [
    "+from trusted_router.provider_lifecycle import provider_model_retired",
    "+    if catalog_predates(cutover):",
    "+TR_LIFECYCLE_CLOCK_OVERRIDE=2027-01-01",
    "-    effective_at=AHEAD_AT,",
    "+the model retires on December 11",
    "+    body = {'model': 'vendor/soon-2'}",
    "+    upstream = 'soon-2-upstream'",
    "+    provider = 'nextcloud'",
    "-    'route': 'vendor/soon-2:nextcloud'",
    "+    model = 'vendor/unknown-4'",
    "+    model = 'sakana/namazu'",
    "+    model = 'tencent/hy4-turbo'",
    "+    provider = 'deepseek'",
    "+    model = 'DeepSeek/deepseek-v4'",
])
def test_the_post_cutover_suite_runs_on_a_line_naming_the_lifecycle(tree: Path, line: str) -> None:
    # In a file that is not Python (a document, a config file, a fixture
    # elsewhere): the whole suite, since any test may read it.
    for path in ("docs/models.md", "infra/monitors.tf", ".github/workflows/synthetic.yml"):
        assert selected([raw(path)], tree, lines=patch(path, line)) == (True, ()), path


@pytest.mark.parametrize("line", [
    "+    body = {'model': 'oldcloud/gone-1'}",
    "+    upstream = 'gone-1-upstream'",
    "+    model = 'vendor/after-3'",
    "+    model = 'vendor/soon-20'",
    "+    model = 'xvendor/soon-2'",
    "+    peak = 'peak'",
    "+def test_routing_prefers_the_cheapest_route():",
    " context line naming vendor/soon-2 is not a change",
])
def test_the_post_cutover_suite_skips_lines_that_name_nothing_scheduled(tree: Path, line: str) -> None:
    path = "docs/models.md"
    assert not scope([raw(path)], tree, lines=patch(path, line))["run_post_cutover"]


def test_a_file_whose_lines_the_patch_does_not_show_runs_the_whole_suite(tree: Path) -> None:
    path = "docs/models.md"
    # A binary file, an empty file added: a header with no hunk.
    header_only = f"diff --git a/{path} b/{path}\nindex 1..2 100644\nBinary files a/{path} and b/{path} differ\n"
    assert selected([raw(path)], tree, lines=header_only) == (True, ())
    # An empty file added or deleted has no hunk but nothing to hide either.
    empty = f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..e69de29\n"
    assert selected([added(path)], tree, lines=empty) == (False, ())
    gone = f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex e69de29..0000000\n"
    assert selected([deleted(path)], tree, lines=gone) == (False, ())
    # A changed file the patch leaves out entirely.
    assert selected([raw(path)], tree, lines=patch("docs/other.md", "+x")) == (True, ())
    _, notes, _ = scope_jobs([raw(path)], root=tree, event="push", lines=patch("docs/other.md", "+x"), now=NOW)
    assert "run_post_cutover: runs, docs/models.md has no lines the diff shows" in notes
    # Inside a hunk, a line whose content starts with "++" is a change.
    assert scope([raw(path)], tree, lines=patch(path, "+++ retire"))["run_post_cutover"]
    assert not scope([raw(path)], tree, lines=patch(path, "+++ notes"))["run_post_cutover"]


def test_the_post_cutover_suite_runs_without_lines_to_read_and_on_every_dispatch(tree: Path) -> None:
    assert selected([raw("docs/a.md")], tree, read=False) == (True, ())
    _, notes, _ = scope_jobs([raw("docs/a.md")], root=tree, event="push", lines=None, now=NOW)
    assert "run_post_cutover: runs, docs/a.md (no diff text to read)" in notes
    # A dispatch runs it whatever changed; the same change on a push does not.
    innocuous = patch("docs/a.md", "+x")
    flags, notes, tests = scope_jobs([raw("docs/a.md")], root=tree, event="workflow_dispatch", lines=innocuous, now=NOW)
    assert flags["run_post_cutover"] and tests == ()
    assert any("a dispatch (the price refresh's publication gate)" in note for note in notes)
    assert not scope([raw("docs/a.md")], tree, event="push", lines=innocuous)["run_post_cutover"]
    # Application changes alone need no lines.
    assert not scope([raw("src/trusted_router/main.py")], tree, lines=None)["run_proofs"]


def test_a_retirement_now_past_stops_counting(tree: Path) -> None:
    path = "docs/models.md"
    lines = patch(path, "+    body = {'model': 'vendor/soon-2'}")
    assert scope([raw(path)], tree, lines=lines, now=NOW)["run_post_cutover"]
    later = datetime(2026, 12, 12, tzinfo=UTC)
    assert not scope([raw(path)], tree, lines=lines, now=later)["run_post_cutover"]


def test_a_rule_that_cannot_read_its_inputs_runs_its_jobs(tree: Path) -> None:
    (tree / "proofs/manifest.toml").write_text("not = [toml")
    (tree / "src/trusted_router/provider_lifecycle.py").write_text("_RETIREMENTS = load()\n")
    (tree / "tests/oracle_helper.py").write_text("def broken(:\n")
    flags, notes, _ = scope_jobs([raw("docs/a.md")], root=tree, event="push", lines=patch("docs/a.md", "+x"), now=NOW)
    assert flags == {"run_fastpath": False, "run_heavy": True, "run_post_cutover": True, "run_proof_oracle": True,
                     "run_proofs": True}
    warnings = [note for note in notes if note.startswith("::warning")]
    assert len(warnings) == 4 and all("could not be derived" in note for note in warnings)
    assert any("run_heavy" in note and "oracle_helper.py does not parse" in note for note in warnings)


def test_scheduled_entities_are_retirements_still_ahead_and_date_switched_prices() -> None:
    entities = scheduled_entities(LIFECYCLE, NOW)
    assert entities == {"nextcloud", "vendor/soon-2", "soon-2-upstream", "mystery", "vendor/unknown-4",
                        "deepseek", "sakana/namazu", "tencent/hy4-turbo"}
    assert scheduled_entities(LIFECYCLE, datetime(2026, 9, 1, tzinfo=UTC)) >= {"oldcloud", "oldcloud/gone-1",
                                                                              "gone-1-upstream"}


@pytest.mark.parametrize("source", [
    "x = 1\n",
    "_RETIREMENTS = ()\n",
    "_RETIREMENTS = load()\n",
    "_RETIREMENTS = (other(),)\n",
    "_RETIREMENTS = (_Retirement('p', frozenset(), frozenset(), None),)\n",
    "_RETIREMENTS = (_Retirement(provider='p', model_ids=frozenset(), effective_at=A, notes='x'),)\n",
    "_RETIREMENTS = (_Retirement(provider='p', effective_at=A),)\n",
])
def test_an_unknown_lifecycle_structure_is_refused(source: str) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        scheduled_entities(source, NOW)


def test_the_real_lifecycle_module_yields_every_scheduled_entity() -> None:
    # Against the runtime truth: every retirement still ahead names its
    # provider, models and upstream ids in the source-derived set, and every
    # catalog endpoint whose price or retirement differs between the two
    # clocks CI pins is named by provider, model or upstream id.
    from trusted_router import provider_lifecycle
    from trusted_router.catalog_registry import MODEL_ENDPOINTS

    now = datetime.now(UTC)
    post = max(provider_lifecycle.latest_scheduled_cutover(), now) + timedelta(days=1)
    entities = scheduled_entities((ROOT / LIFECYCLE_MODULE).read_text(), now)
    ahead = [retirement for retirement in provider_lifecycle._RETIREMENTS if retirement.effective_at > now]

    def unnamed_retirements(named: frozenset[str]) -> list[str]:
        return [f"{r.provider}:{item}" for r in ahead
                for item in (r.provider, *r.model_ids, *r.upstream_ids) if item not in named]

    assert unnamed_retirements(entities) == []
    if ahead:
        # The check can fail: drop one id and it is reported.
        dropped = next(iter(ahead[0].model_ids))
        reported = unnamed_retirements(entities - {dropped})
        assert f"{ahead[0].provider}:{dropped}" in reported
        assert all(item.endswith(f":{dropped}") for item in reported)
    unnamed = []
    for endpoint in MODEL_ENDPOINTS.values():
        upstream = getattr(endpoint, "upstream_id", None)
        differs = (
            provider_lifecycle.provider_price_microdollars(endpoint.provider, endpoint.model_id, at=now)
            != provider_lifecycle.provider_price_microdollars(endpoint.provider, endpoint.model_id, at=post)
            or provider_lifecycle.provider_model_retired(endpoint.provider, endpoint.model_id, upstream, at=now)
            != provider_lifecycle.provider_model_retired(endpoint.provider, endpoint.model_id, upstream, at=post)
        )
        if differs and not {endpoint.provider, endpoint.model_id, upstream} & entities:
            unnamed.append(endpoint.id)
    assert unnamed == []


def test_the_real_tree_scopes_typical_changes() -> None:
    # The rules against this repository's own manifest, closure and lifecycle.
    def flags(*changes: str, lines: str | None = None) -> dict[str, bool]:
        # Each changed file's patch section, a line that names nothing.
        if lines is None:
            lines = "".join(patch(record.split("\t", 1)[1], "+an ordinary line") for record in changes)
        return scope(list(changes), ROOT, lines=lines, now=datetime.now(UTC))

    assert flags(raw("docs/design/ci-deploy-latency.md")) == dict.fromkeys(PATH_FLAGS, False)
    # Terraform naming nothing scheduled: nothing.
    infra = patch("infra/fastpath.tf", "+  machine_type = \"c3-standard-8\"")
    assert flags(raw("infra/fastpath.tf"), lines=infra) == dict.fromkeys(PATH_FLAGS, False)
    # A test file: the post-cutover suite on the tests it reaches.
    test = "tests/test_enclave_security_guard.py"
    flags_, _, tests = scope_jobs([raw(test)], root=ROOT, event="pull_request",
                                  lines=patch(test, "+    assert True"), now=datetime.now(UTC))
    assert flags_ == dict.fromkeys(PATH_FLAGS, False) | {"run_post_cutover": True}
    assert test in tests and 0 < len(tests) <= 10
    assert flags(raw("fastpath/internal/store/commit.go")) == {
        "run_fastpath": True, "run_heavy": False, "run_post_cutover": False, "run_proof_oracle": False,
        "run_proofs": True}
    assert flags(raw("src/trusted_router/services/auto_refill.py")) == dict.fromkeys(PATH_FLAGS, False) | {
        "run_heavy": True, "run_post_cutover": True, "run_proof_oracle": True, "run_proofs": True}
    assert flags(raw(SNAPSHOT), raw(MANIFEST)) == dict.fromkeys(PATH_FLAGS, False) | {
        "run_heavy": True, "run_post_cutover": True}
    assert flags(raw("tests/test_billing_snapshot.py"))["run_proof_oracle"]
    assert flags(raw("tests/fakes/frozen_main/execution-inventory.json"))["run_proof_oracle"]
    assert flags(raw("scripts/deploy/migrate_typed_counters.sh"))["run_fastpath"]
    assert flags(raw("tests/test_public_surface_deploy.py"))["run_proofs"]


def test_the_oracle_closure_reaches_what_its_tests_import() -> None:
    from scripts.ci.commit_scope import ORACLE_TESTS, TEST_HARNESS, python_closure

    closure = python_closure(ROOT, (*ORACLE_TESTS, *TEST_HARNESS))
    for path in ("tests/test_billing_snapshot.py", "tests/test_async_settle_handler.py",
                 "tests/test_async_settle_proof.py", "tests/fakes/frozen_package.py", "tests/fakes/spanner.py",
                 "tests/settle_drain_trace.py", "tests/test_async_settle_ticket.py", "tests/proof_oracle_ci.py",
                 "tests/catalog_vehicles.py", "tests/lifecycle_freeze.py",
                 "tests/test_async_settle_shadow_v2_gate_loading.py", "tests/__init__.py"):
        assert path in closure, path
    assert "tests/test_ci_workflow.py" not in closure


def test_python_closure_resolves_relative_and_string_imports(tmp_path: Path) -> None:
    from scripts.ci.commit_scope import python_closure

    write(tmp_path, {
        "tests/__init__.py": "", "tests/pkg/__init__.py": "", "tests/pkg/a.py": "from . import b\nfrom ..c import d\n",
        "tests/pkg/b.py": "", "tests/c.py": "X = 'scripts.tool.run'\n", "scripts/tool/run.py": "",
        "tests/top.py": "from tests.pkg import a\nimport json\nimport trusted_router.main\n",
    })
    closure = python_closure(tmp_path, ["tests/top.py"])
    assert {"tests/top.py", "tests/__init__.py", "tests/pkg/__init__.py", "tests/pkg/a.py", "tests/pkg/b.py",
            "tests/c.py", "scripts/tool/run.py"} <= closure
    assert not any(path.startswith(("src/", "json")) for path in closure)


def test_changed_lines_reads_each_files_hunks() -> None:
    text = patch("a.py", "+one", "-two") + "diff --git a/b.bin b/b.bin\nBinary files a/b.bin and b/b.bin differ\n"
    # A file with no hunk has no lines to read: None, not an empty list.
    assert changed_lines(text) == {"a/a.py b/a.py": ["one", "two"], "a/b.bin b/b.bin": None}


def test_the_jobs_command_prints_one_line_per_flag(monkeypatch, capsys, tmp_path: Path) -> None:
    assert main(["jobs", "--full"]) == 0
    out = capsys.readouterr()
    assert out.out == "".join(f"{flag}=true\n" for flag in PATH_FLAGS) + "post_cutover_tests=\n"
    assert "complete matrix" in out.err
    monkeypatch.chdir(ROOT)
    lines = tmp_path / "lines.patch"
    lines.write_text(patch("docs/a.md", "+x = 1"))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw('docs/a.md')}\n"))
    assert main(["jobs", "--event", "push", "--lines", str(lines)]) == 0
    out = capsys.readouterr()
    assert out.out == "".join(f"{flag}=false\n" for flag in PATH_FLAGS) + "post_cutover_tests=\n"
    assert "run_proofs: skips" in out.err
    # Unreadable lines are not an error; the post-cutover rule runs instead.
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw('docs/a.md')}\n"))
    assert main(["jobs", "--event", "push", "--lines", str(tmp_path / "missing.patch")]) == 0
    out = capsys.readouterr()
    assert "run_post_cutover=true\n" in out.out and "unreadable" in out.err


GO_JOIN = re.compile(r"filepath\.Join\(([^()]*)\)")
GO_STRING = re.compile(r'"((?:\.\./)+[^"]*)"')


def go_external_reads() -> set[str]:
    """Repository paths outside fastpath/ that a Go file names relative to its
    package directory (go test's working directory): the literal leading
    arguments of a filepath.Join, or a "../" string. A trailing argument that
    is not a literal makes the read a directory, written as `dir/<any>`."""
    found: set[str] = set()
    for file in sorted((ROOT / "fastpath").rglob("*.go")):
        source = file.read_text()
        package = file.parent.relative_to(ROOT).as_posix()
        candidates = []
        for match in GO_JOIN.finditer(source):
            parts = []
            for argument in (piece.strip() for piece in match.group(1).split(",")):
                if not (argument.startswith('"') and argument.endswith('"')):
                    parts.append("<any>")
                    break
                parts.append(argument[1:-1])
            candidates.append(parts)
        candidates += [[match.group(1)] for match in GO_STRING.finditer(source)]
        for parts in candidates:
            if not parts or parts[0] not in ("..",) and not parts[0].startswith("../"):
                continue
            path = posixpath.normpath(posixpath.join(package, *parts))
            assert not path.startswith(".."), (file, parts)
            if not path.startswith("fastpath/") and path != "fastpath":
                found.add(path)
        if "tla2tools.jar" in source:
            found.add("proofs/<any>")
    return found


def covered(path: str, patterns: tuple[str, ...]) -> bool:
    from scripts.ci.commit_scope import matches_any

    if path.endswith("/<any>"):
        directory = path.removesuffix("/<any>")
        return any(pattern in (f"{directory}/**", f"{directory}/*") or pattern.startswith(f"{directory}/*")
                   for pattern in patterns)
    return matches_any(path, patterns) or path in patterns


def test_every_file_the_go_tests_read_outside_the_module_runs_the_fast_path_jobs() -> None:
    from scripts.ci.commit_scope import FASTPATH_INPUTS

    reads = go_external_reads()
    # The reads this scan must find, so a scan that found nothing could not pass.
    assert {"tests/fixtures/usage_cost_vectors.json", "scripts/deploy/migrate_typed_counters.sh",
            "scripts/deploy/migrate_fastpath.sh", "scripts/deploy/*.sh", "scripts/deploy/<any>",
            "proofs/<any>"} <= reads
    assert [path for path in sorted(reads) if not covered(path, FASTPATH_INPUTS)] == []
    # And the check can fail: without scripts/deploy/*.sh those reads are uncovered.
    narrowed = tuple(pattern for pattern in FASTPATH_INPUTS if pattern != "scripts/deploy/*.sh")
    assert {path for path in reads if not covered(path, narrowed)} == {
        "scripts/deploy/migrate_typed_counters.sh", "scripts/deploy/migrate_fastpath.sh",
        "scripts/deploy/*.sh", "scripts/deploy/<any>"}


# ---------------------------------------------------------------------------
# The post-cutover suite on the tests a change outside src/ reaches

FILLERS = 24


@pytest.fixture
def suite(tree: Path) -> Path:
    """`tree` with a small test suite: helpers, a sub-directory conftest,
    scripts the tests import or run, and filler tests that reach none."""
    write(tree, {
        "tests/helper_x.py": "X = 1\n",
        "tests/mid.py": "from tests.helper_x import X\n",
        "tests/test_a.py": "from tests.helper_x import X\n\n\ndef test_a():\n    assert X\n",
        "tests/test_b.py": "import tests.mid\n\n\ndef test_b():\n    pass\n",
        "tests/test_c.py": "def test_c():\n    pass\n",
        "tests/test_d.py": "import subprocess\n\n\ndef test_d():\n    subprocess.run(['python', 'scripts/tool.py'])\n",
        "tests/test_e.py": "from scripts.lib import f\n\n\ndef test_e():\n    f()\n",
        "tests/helper_y.py": "",
        "tests/conformance/conftest.py": "from tests.helper_y import *\n",
        "tests/conformance/test_f.py": "def test_f():\n    pass\n",
        "tests/conformance/test_g.py": "def test_g():\n    pass\n",
        "tests/many.py": "",
        "scripts/tool.py": "print('tool')\n",
        "scripts/lib.py": "def f():\n    pass\n",
        "scripts/standalone.py": "print('alone')\n",
        **{f"tests/filler/test_filler_{n:02d}.py": f"def test_{n}():\n    pass\n" for n in range(FILLERS)},
        **{f"tests/test_many_{n}.py": "import tests.many\n" for n in range(10)},
    })
    return tree


def test_a_new_test_that_reads_the_catalog_runs_post_cutover_on_that_file(suite: Path) -> None:
    # The review's counterexample: today's clock gives 679 models and the
    # post-cutover clock 659, and the test names nothing scheduled.
    path = "tests/test_catalog_model_count.py"
    write(suite, {path: "from trusted_router.catalog_registry import MODELS\n\n\n"
                        "def test_catalog_has_679_models():\n    assert len(MODELS) == 679\n"})
    assert selected([added(path)], suite) == (True, (path,))
    # Modified, too.
    assert selected([raw(path)], suite) == (True, (path,))


def test_a_helper_change_selects_the_tests_that_import_it(suite: Path) -> None:
    # Directly (test_a) and through another module (test_b, via mid).
    assert selected([raw("tests/helper_x.py")], suite) == (True, ("tests/test_a.py", "tests/test_b.py"))
    assert selected([raw("tests/mid.py")], suite) == (True, ("tests/test_b.py",))


def test_an_unrelated_test_change_selects_only_itself(suite: Path) -> None:
    assert selected([raw("tests/test_c.py")], suite) == (True, ("tests/test_c.py",))
    assert selected([raw("tests/test_c.py"), raw("tests/test_a.py")], suite) == (
        True, ("tests/test_a.py", "tests/test_c.py"))


def test_a_directory_conftest_counts_as_imported_by_its_tests(suite: Path) -> None:
    expected = (True, ("tests/conformance/test_f.py", "tests/conformance/test_g.py"))
    assert selected([raw("tests/conformance/conftest.py")], suite) == expected
    assert selected([raw("tests/helper_y.py")], suite) == expected


def test_a_script_change_selects_the_tests_that_import_or_name_it(suite: Path) -> None:
    assert selected([raw("scripts/lib.py")], suite) == (True, ("tests/test_e.py",))
    assert selected([raw("scripts/tool.py")], suite) == (True, ("tests/test_d.py",))


@pytest.mark.parametrize("path", ["scripts/standalone.py", "tests/loaded_by_name.py", "tests/fakes/unused.py",
                                  "proofs/check_tool.py"])
def test_python_that_reaches_no_test_runs_the_whole_suite(suite: Path, path: str) -> None:
    # A test may load it by a computed name, importlib.import_module("tests."
    # + name), or run it by a built path, ROOT / "scripts" / (name + ".py"):
    # the review's cases. Neither import nor name finds those tests.
    write(suite, {path: "VALUE = 1\n"})
    assert selected([raw(path)], suite) == (True, ())
    # Even beside a change that does reach tests.
    assert selected([raw(path), raw("tests/test_c.py")], suite) == (True, ())


def test_a_deleted_helper_selects_the_tests_that_imported_it(suite: Path) -> None:
    (suite / "tests/helper_x.py").unlink()
    assert selected([deleted("tests/helper_x.py")], suite) == (True, ("tests/test_a.py", "tests/test_b.py"))
    # A deleted test file is no test to run, and no reason for the suite.
    (suite / "tests/test_c.py").unlink()
    assert selected([deleted("tests/test_c.py")], suite) == (False, ())
    assert selected([deleted("tests/test_c.py"), raw("tests/test_a.py")], suite) == (True, ("tests/test_a.py",))
    # A deleted helper nothing imported any more: the whole suite.
    (suite / "tests/helper_y.py").unlink()
    (suite / "tests/conformance/conftest.py").write_text("")
    assert selected([deleted("tests/helper_y.py")], suite) == (True, ())


def test_too_many_reached_tests_run_the_whole_suite(suite: Path) -> None:
    # Ten of 49 test files is more than an eighth: eight shards beat one.
    assert selected([raw("tests/many.py")], suite) == (True, ())


def test_a_path_the_job_cannot_pass_safely_runs_the_whole_suite(suite: Path) -> None:
    path = "tests/test_we$ird.py"
    write(suite, {path: "def test_weird():\n    pass\n"})
    assert selected([added(path)], suite) == (True, ())


def test_other_changes_decide_between_a_selection_and_the_whole_suite(suite: Path) -> None:
    test = patch("tests/test_c.py", "+    pass")
    lines = patch("docs/a.md", "+notes") + test
    assert selected([raw("tests/test_c.py"), raw("docs/a.md")], suite, lines=lines) == (True, ("tests/test_c.py",))
    # A document naming a scheduled model, any other data file, the
    # application, a fixture or the harness: the whole suite.
    named = patch("docs/a.md", "+vendor/soon-2 retires") + test
    assert selected([raw("tests/test_c.py"), raw("docs/a.md")], suite, lines=named) == (True, ())
    for other in ("src/trusted_router/main.py", "tests/fixtures/x.json", "tests/harness_helper.py", "uv.lock",
                  "docs/catalog_expectations.json"):
        assert selected([raw("tests/test_c.py"), raw(other)], suite, lines=lines + patch(other, "+x")) == (
            True, ()), other
    # A Python line naming the lifecycle runs the whole suite too, in a test
    # or a helper; and no patch to read runs it.
    for path in ("tests/test_c.py", "tests/helper_x.py", "scripts/lib.py"):
        for line in ("+    # past the cutover", "+from tests.lifecycle_clock import catalog_predates",
                     "-    model = 'vendor/soon-2'", "+RETIRED = True"):
            assert selected([raw(path)], suite, lines=patch(path, line)) == (True, ()), (path, line)
    assert selected([raw("tests/test_c.py")], suite, read=False) == (True, ())


def test_the_jobs_command_prints_the_selected_tests(monkeypatch, capsys, suite: Path) -> None:
    monkeypatch.chdir(suite)
    (suite / "lines.patch").write_text(patch("tests/helper_x.py", "+X = 2"))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw('tests/helper_x.py')}\n"))
    assert main(["jobs", "--event", "pull_request", "--lines", "lines.patch"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == "post_cutover_tests=tests/test_a.py tests/test_b.py"
    assert "run_post_cutover=true" in out.out
    assert "post-cutover test: tests/test_b.py" in out.err


def test_tests_reaching_against_this_repository() -> None:
    from scripts.ci.commit_scope import test_files, tests_reaching

    files = test_files(ROOT)
    assert "tests/test_commit_scope.py" in files and "tests/conformance/test_spanner_schema.py" in files
    assert all(path.startswith("tests/") and path.rsplit("/", 1)[-1].startswith("test_") for path in files)
    reached = tests_reaching(ROOT, ["tests/deploy_script_harness.py"])
    assert "tests/test_aws_ecs_release.py" in reached and "tests/test_ci_scope_step.py" not in reached
    # This file names the script too, so it is reached by name.
    assert set(tests_reaching(ROOT, ["scripts/check_enclave_sentry.py"])) == {
        "tests/test_enclave_security_guard.py", "tests/test_stubs_and_security.py", "tests/test_commit_scope.py"}


# ---------------------------------------------------------------------------
# The heavy lane (run_heavy)

HEAVY = "tests/test_heavy_lane.py"
HEAVY_SOURCE = '''\
import importlib
from pathlib import Path

from tests.test_helpers import make
from tests import imported_helper

ROOT = Path(__file__).parents[1]
TABLE = ROOT / "scripts/data/table.csv"
BASE = ROOT / "scripts" / "basedir"
CONF = ROOT / "scripts" / "conf"


def scan(directory):
    return sorted(directory.glob("*.cfg"))


def test_reads():
    for name in ("alpha", "beta"):
        importlib.import_module("tests.plugins." + name)
    assert (ROOT / "docs" / "named.md").read_text()
    assert (BASE / "one.txt").read_text()
    for path in (ROOT / "scripts" / "globbed").glob("*.py"):
        path.read_text()
    program = """
import sys
import tests.in_code
"""
    assert program and make()
'''


@pytest.fixture
def heavy_tree(tmp_path: Path, monkeypatch) -> Path:
    import scripts.ci.commit_scope as commit_scope

    monkeypatch.setattr(commit_scope, "HEAVY_TESTS", (HEAVY,))
    write(tmp_path, {
        "tests/__init__.py": "", "tests/conftest.py": "import tests.harness\n", "tests/harness.py": "",
        HEAVY: HEAVY_SOURCE,
        # A test module the lane imports a helper from: its own tests do not
        # run there, so what only their bodies read is not the lane's.
        "tests/test_helpers.py": "def make():\n    return 1\n\n\ndef test_helper_reads():\n"
                                 "    open('docs/test_only.md').read()\n    import tests.test_only_import\n",
        "tests/imported_helper.py": "", "tests/in_code.py": "", "tests/test_only_import.py": "",
        "tests/plugins/__init__.py": "", "tests/plugins/alpha.py": "", "tests/plugins/beta.py": "",
        "tests/plugins/gamma.py": "", "tests/unrelated.py": "",
        "scripts/data/table.csv": "a,b\n", "scripts/basedir/one.txt": "1\n", "scripts/basedir/two.txt": "2\n",
        "scripts/globbed/x.py": "", "scripts/globbed/y.py": "", "scripts/conf/a.cfg": "", "scripts/conf/b.txt": "",
        "scripts/other.py": "", "docs/named.md": "x\n", "docs/test_only.md": "x\n", "docs/other.md": "x\n",
        "src/trusted_router/x.py": "", "tests/fixtures/case.json": "{}\n",
    })
    return tmp_path


def test_the_heavy_closure_follows_imports_strings_and_named_paths(heavy_tree: Path) -> None:
    from scripts.ci.commit_scope import heavy_closure

    closure = heavy_closure(heavy_tree)
    for path in (HEAVY, "tests/conftest.py", "tests/harness.py", "tests/__init__.py", "tests/test_helpers.py",
                 "tests/imported_helper.py", "tests/in_code.py", "tests/plugins/alpha.py", "tests/plugins/beta.py",
                 "tests/plugins/__init__.py", "scripts/data/table.csv", "docs/named.md", "scripts/basedir/one.txt",
                 "scripts/globbed/x.py", "scripts/globbed/y.py", "scripts/conf/a.cfg"):
        assert path in closure, path
    # Only the plugins the loop names; a directory used as a base is not all
    # of its files; an unplaced glob lists the directories the file names;
    # nothing names the rest.
    for path in ("tests/plugins/gamma.py", "scripts/basedir/two.txt", "scripts/conf/b.txt", "tests/unrelated.py",
                 "scripts/other.py", "docs/other.md",
                 # Read only in the body of a test the lane does not run.
                 "docs/test_only.md", "tests/test_only_import.py"):
        assert path not in closure, path


def test_a_test_body_counts_once_something_in_the_lane_names_it(heavy_tree: Path) -> None:
    from scripts.ci.commit_scope import heavy_closure

    write(heavy_tree, {HEAVY: HEAVY_SOURCE.replace("import make", "import make, test_helper_reads")})
    closure = heavy_closure(heavy_tree)
    assert {"docs/test_only.md", "tests/test_only_import.py"} <= closure
    # Read as a file (not imported), the module is read whole too.
    write(heavy_tree, {HEAVY: HEAVY_SOURCE + "\nSOURCE = ROOT / 'tests/test_helpers.py'\n"})
    assert {"docs/test_only.md", "tests/test_only_import.py"} <= heavy_closure(heavy_tree)


def test_the_heavy_lane_runs_on_src_its_closure_fixtures_and_toolchain(heavy_tree: Path) -> None:
    def runs(*paths: str) -> bool:
        return scope([raw(path) for path in paths], heavy_tree)["run_heavy"]

    for path in ("src/trusted_router/x.py", "src/trusted_router/data/openrouter_snapshot.json", "src/other/y.txt",
                 HEAVY, "tests/conftest.py", "tests/harness.py", "tests/test_helpers.py", "tests/plugins/beta.py",
                 "scripts/data/table.csv", "docs/named.md", "tests/fixtures/case.json", "tests/fakes/x.txt",
                 ".test_durations", "pyproject.toml", "uv.lock", ".python-version"):
        assert runs(path), path
    for path in ("docs/other.md", "docs/test_only.md", "tests/unrelated.py", "tests/plugins/gamma.py",
                 "scripts/other.py", "tests/test_new.py", ".test_durations-post-cutover", "fastpath/main.go"):
        assert not runs(path), path
    # A deleted module the lane imports is a change to it.
    assert scope([deleted("tests/imported_helper.py")], heavy_tree)["run_heavy"]


def test_deleting_or_renaming_a_file_the_lane_reads_runs_it(heavy_tree: Path) -> None:
    # HEAD's sources still name the file a change deleted or moved away: its
    # deletion record runs the lane, wherever in the repository it was.
    (heavy_tree / "docs/named.md").unlink()
    for changes in ([deleted("docs/named.md")], [deleted("docs/named.md"), added("docs/renamed.md")]):
        assert scope(changes, heavy_tree)["run_heavy"], changes
    (heavy_tree / "scripts/data/table.csv").unlink()
    assert scope([deleted("scripts/data/table.csv")], heavy_tree)["run_heavy"]
    # A document nothing names still runs nothing.
    assert not scope([deleted("docs/other.md")], heavy_tree)["run_heavy"]


def test_a_heavy_file_that_does_not_parse_runs_the_lane(heavy_tree: Path) -> None:
    write(heavy_tree, {"tests/imported_helper.py": "def broken(:\n"})
    flags, notes, _ = scope_jobs([raw("docs/other.md")], root=heavy_tree, event="push", lines="", now=NOW)
    assert flags["run_heavy"]
    assert any("run_heavy" in note and "does not parse" in note for note in notes)


def test_the_real_heavy_closure_reaches_what_the_lane_imports_and_reads() -> None:
    from scripts.ci.commit_scope import HEAVY_TESTS, heavy_closure, heavy_covers

    closure = heavy_closure(ROOT)
    assert set(HEAVY_TESTS) <= closure
    for path in ("tests/conftest.py", "tests/test_async_settle_handler.py", "tests/test_billing_snapshot.py",
                 "tests/shadow_v2_gate_fixture_generator.py", "tests/shadow_v2_import_guard.py",
                 "scripts/async_settle/shadow_report.py", "tests/test_resold_claude_cache_accounting.py",
                 "tests/fixtures/async_settlement/p4a_lifecycle_recorder.py"):
        assert path in closure, path
    assert all(heavy_covers(path, closure) for path in ("src/trusted_router/main.py", ".test_durations",
                                                         "tests/fixtures/async_settlement/billing_v1.json"))
    for path in ("tests/test_ci_workflow.py", "tests/test_decide_models.py", "docs/design/ci-deploy-latency.md",
                 "scripts/pricing/parsers/fireworks.py", "fastpath/internal/store/ids.go"):
        assert not heavy_covers(path, closure), path
