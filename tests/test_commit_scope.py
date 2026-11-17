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
                                          ("lint", "production-import", "test", "test-post-cutover", "browser",
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
    assert PATH_FLAGS == ("run_fastpath", "run_post_cutover", "run_proof_oracle", "run_proofs")
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


def test_no_event_excuses_a_skip_any_more() -> None:
    # A push to main used to excuse the post-cutover suite and the model
    # checks. Now the push applies the same flags as a pull request.
    for job in ("test-post-cutover", "proofs"):
        assert summarize(needs(**{job: "skipped"}), "false") == [f"{job}: skipped"]
    import inspect

    assert list(inspect.signature(summarize).parameters) == ["needs", "data_only"]


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
    flags, _ = scope_jobs(changes, root=root, event=event, lines=lines, now=now)
    assert set(flags) == set(PATH_FLAGS)
    return flags


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
    flags, notes = scope_jobs([raw("docs/a.md"), record], root=tree, event="push", lines="", now=NOW)
    assert flags == dict.fromkeys(PATH_FLAGS, True)
    assert "not an add, modify or delete of a regular file" in notes[0]


def test_an_empty_comparison_runs_every_path_scoped_job(tree: Path) -> None:
    for records in ([], [""], ["", "  "]):
        flags, notes = scope_jobs(records, root=tree, event="push", lines="", now=NOW)
        assert flags == dict.fromkeys(PATH_FLAGS, True)
        assert "empty" in notes[0]


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/ci/commit_scope.py",
                                  "scripts/ci/new_helper.py", "scripts/ci/sub/x.sh"])
def test_a_change_to_how_ci_decides_runs_every_path_scoped_job(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        assert scope([raw("docs/a.md"), record], tree) == dict.fromkeys(PATH_FLAGS, True)


@pytest.mark.parametrize("path", [".github/workflows/post-cutover-daily.yml", "scripts/cix/x.py",
                                  "docs/ci.yml", "tests/test_ci_workflow.py"])
def test_other_ci_files_do_not_run_everything(tree: Path, path: str) -> None:
    assert not all(scope([raw(path)], tree).values())


def test_a_change_no_rule_names_skips_every_path_scoped_job(tree: Path) -> None:
    changes = [raw("docs/design/x.md"), raw("infra/main.tf"), added("sites/token-exchange/new.html"),
               deleted("frontend/src/old.ts"), raw(".github/workflows/deploy.yml")]
    lines = patch("docs/design/x.md", "+A paragraph about latency.") + patch("infra/main.tf", "-a = 1", "+a = 2")
    assert scope(changes, tree, lines=lines) == dict.fromkeys(PATH_FLAGS, False)


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
    "pyproject.toml", "uv.lock", ".python-version", "conftest.py",
    "tests/conftest.py", "tests/harness_helper.py", "tests/fakes/store.py", "tests/fixtures/vehicle_routes.json",
])
def test_the_post_cutover_suite_runs_on_the_application_its_data_and_harness(tree: Path, path: str) -> None:
    for record in (raw(path), added(path), deleted(path)):
        # No changed lines name anything: the path alone decides.
        assert scope([raw("docs/a.md"), record], tree, lines="")["run_post_cutover"], record


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
    path = "tests/test_new_routes.py"
    assert scope([added(path)], tree, lines=patch(path, line))["run_post_cutover"]
    # Wherever the line is: a document or a config file too.
    assert scope([raw("docs/models.md")], tree, lines=patch("docs/models.md", line))["run_post_cutover"]


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
    path = "tests/test_new_routes.py"
    assert not scope([added(path)], tree, lines=patch(path, line))["run_post_cutover"]


def test_the_post_cutover_suite_reads_lines_only_inside_hunks(tree: Path) -> None:
    path = "tests/test_new_routes.py"
    header_only = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    assert not scope([added(path)], tree, lines=header_only)["run_post_cutover"]
    # Inside a hunk, a line whose content starts with "++" is a change.
    assert scope([added(path)], tree, lines=patch(path, "+++ retire"))["run_post_cutover"]


def test_the_post_cutover_suite_runs_without_lines_to_read_and_on_every_dispatch(tree: Path) -> None:
    assert scope([raw("docs/a.md")], tree, lines=None)["run_post_cutover"]
    assert scope([raw("docs/a.md")], tree, event="workflow_dispatch", lines="")["run_post_cutover"]
    assert not scope([raw("docs/a.md")], tree, event="push", lines="")["run_post_cutover"]
    # Application changes alone need no lines.
    assert not scope([raw("src/trusted_router/main.py")], tree, lines=None)["run_proofs"]


def test_a_retirement_now_past_stops_counting(tree: Path) -> None:
    path = "tests/test_new_routes.py"
    lines = patch(path, "+    body = {'model': 'vendor/soon-2'}")
    assert scope([added(path)], tree, lines=lines, now=NOW)["run_post_cutover"]
    later = datetime(2026, 12, 12, tzinfo=UTC)
    assert not scope([added(path)], tree, lines=lines, now=later)["run_post_cutover"]


def test_a_rule_that_cannot_read_its_inputs_runs_its_jobs(tree: Path) -> None:
    (tree / "proofs/manifest.toml").write_text("not = [toml")
    (tree / "src/trusted_router/provider_lifecycle.py").write_text("_RETIREMENTS = load()\n")
    (tree / "tests/oracle_helper.py").write_text("def broken(:\n")
    flags, notes = scope_jobs([raw("docs/a.md")], root=tree, event="push", lines=patch("docs/a.md", "+x"), now=NOW)
    assert flags == {"run_fastpath": False, "run_post_cutover": True, "run_proof_oracle": True, "run_proofs": True}
    warnings = [note for note in notes if note.startswith("::warning")]
    assert len(warnings) == 3 and all("could not be derived" in note for note in warnings)


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
    def flags(*changes: str, lines: str = "") -> dict[str, bool]:
        return scope(list(changes), ROOT, lines=lines, now=datetime.now(UTC))

    assert flags(raw("infra/fastpath.tf"), raw("tests/test_infra_fastpath.py"),
                 lines=patch("tests/test_infra_fastpath.py", "+    assert 'machine_type' in text")) == {
        "run_fastpath": False, "run_post_cutover": False, "run_proof_oracle": False, "run_proofs": False}
    assert flags(raw("fastpath/internal/store/commit.go")) == {
        "run_fastpath": True, "run_post_cutover": False, "run_proof_oracle": False, "run_proofs": True}
    assert flags(raw("src/trusted_router/services/auto_refill.py")) == dict.fromkeys(PATH_FLAGS, False) | {
        "run_post_cutover": True, "run_proof_oracle": True, "run_proofs": True}
    assert flags(raw(SNAPSHOT), raw(MANIFEST)) == dict.fromkeys(PATH_FLAGS, False) | {"run_post_cutover": True}
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
    assert changed_lines(text) == {"a/a.py b/a.py": ["one", "two"], "a/b.bin b/b.bin": []}


def test_the_jobs_command_prints_one_line_per_flag(monkeypatch, capsys, tmp_path: Path) -> None:
    assert main(["jobs", "--full"]) == 0
    out = capsys.readouterr()
    assert out.out == "".join(f"{flag}=true\n" for flag in PATH_FLAGS)
    assert "complete matrix" in out.err
    monkeypatch.chdir(ROOT)
    lines = tmp_path / "lines.patch"
    lines.write_text(patch("infra/a.tf", "+x = 1"))
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw('infra/a.tf')}\n"))
    assert main(["jobs", "--event", "push", "--lines", str(lines)]) == 0
    out = capsys.readouterr()
    assert out.out == "".join(f"{flag}=false\n" for flag in PATH_FLAGS)
    assert "run_proofs: skips" in out.err
    # Unreadable lines are not an error; the post-cutover rule runs instead.
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{raw('infra/a.tf')}\n"))
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
