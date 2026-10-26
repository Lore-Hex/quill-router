from __future__ import annotations

import io
import json

import pytest

from scripts.ci.commit_scope import (
    SKIPPED_FOR_DATA_ONLY,
    classify,
    is_data_path,
    main,
    summarize,
)

SNAPSHOT = "src/trusted_router/data/openrouter_snapshot.json"
MANIFEST = "src/trusted_router/data/provider_models/fireworks.json"
CARD = "src/trusted_router/static/og/providers/fireworks.png"
HISTORY = "src/trusted_router/data/model_changes.jsonl"


@pytest.mark.parametrize("path", [
    SNAPSHOT, MANIFEST, CARD, HISTORY,
    "src/trusted_router/data/model_changes_state.json",
    "src/trusted_router/data/speech_pricing.json",
    "src/trusted_router/data/transcription_pricing.json",
    "src/trusted_router/data/provider_routing_evidence.json",
])
def test_published_catalog_files_are_data(path: str) -> None:
    assert is_data_path(path)
    assert is_data_path(f"  {path}\n")


@pytest.mark.parametrize("path", [
    "scripts/pricing/parsers/fireworks.py",
    "fastpath/testdata/model_alias_vectors_v1.json",
    "src/trusted_router/catalog_data.py",
    "src/trusted_router/data/provider_models/nested/fireworks.json",
    "src/trusted_router/data/provider_models/fireworks.py",
    "src/trusted_router/data/other.json",
    "src/trusted_router/static/og/providers/sub/x.png",
    ".github/workflows/ci.yml",
    "tests/test_catalog_routing_contracts.py",
    "/src/trusted_router/data/openrouter_snapshot.json",
    "src/trusted_router/data/../data/openrouter_snapshot.json",
    "",
])
def test_everything_else_is_code(path: str) -> None:
    assert not is_data_path(path)


def test_classify_requires_a_non_empty_all_data_set() -> None:
    assert classify([SNAPSHOT, MANIFEST, CARD, HISTORY])
    assert classify([SNAPSHOT, "", "  "])
    assert not classify([])
    assert not classify(["", "\n"])
    assert not classify([SNAPSHOT, "scripts/pricing/parsers/fireworks.py"])
    assert not classify([SNAPSHOT, "fastpath/testdata/model_alias_vectors_v1.json"])
    assert not classify([SNAPSHOT, "src/trusted_router/catalog_data.py"])


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


def test_classify_command_reads_paths_from_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SNAPSHOT}\n{MANIFEST}\n"))
    assert main(["classify"]) == 0
    assert capsys.readouterr().out == "true\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SNAPSHOT}\nsrc/trusted_router/main.py\n"))
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
