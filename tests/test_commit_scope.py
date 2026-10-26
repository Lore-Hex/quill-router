from __future__ import annotations

import io
import json

import pytest

from scripts.ci.commit_scope import (
    SKIPPED_FOR_DATA_ONLY,
    classify,
    is_data_change,
    is_data_path,
    main,
    summarize,
)

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


def needs(**results: str) -> dict[str, dict[str, object]]:
    base = {name: {"result": "success"} for name in
            ("scope", "lint", "production-import", "test", "test-post-cutover", "browser",
             *SKIPPED_FOR_DATA_ONLY)}
    for name, result in results.items():
        base[name.replace("_", "-")] = {"result": result}
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


@pytest.mark.parametrize("job", ["lint", "production-import", "test", "test-post-cutover", "browser"])
def test_a_job_that_reads_the_data_may_never_skip(job: str) -> None:
    assert summarize(needs(**{job: "skipped"}), "true") == [f"{job}: skipped"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_scope_must_succeed(result: str | None) -> None:
    assert summarize(needs(scope=result), "true") == [f"scope: {result}"]
    assert summarize(needs(scope=result), "false") == [f"scope: {result}"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "timed_out", None])
def test_any_other_result_fails_on_either_commit(result: str | None) -> None:
    for data_only in ("true", "false"):
        assert summarize(needs(test=result), data_only) == [f"test: {result}"]
        assert summarize(needs(proofs=result), data_only) == [f"proofs: {result}"]


def test_a_skipped_job_outside_the_list_fails_even_on_a_data_only_commit() -> None:
    assert summarize({**needs(), "new-job": {"result": "skipped"}}, "true") == ["new-job: skipped"]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
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
