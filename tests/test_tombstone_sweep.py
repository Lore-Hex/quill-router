"""scripts/tombstone_sweep.py records a run that did not happen as a crash, and
resumes a sweep only with the groups it was written with. Either mistake would
report providers as safe to delist that were never tested."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import tombstone_sweep as sweep


def _pytest_exits(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, failed: tuple[str, ...] = ()
) -> None:
    def run(args: list[str], *, env: dict[str, str], **_: Any) -> subprocess.CompletedProcess[str]:
        Path(env["TOMBSTONE_SWEEP_FAILURES"]).write_text(
            "".join(f"{node}\n" for node in failed), encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(sweep.subprocess, "run", run)


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (3, "INTERNALERROR: worker failed during import\n=== no tests ran in 0.12s ===\n"),
        (5, "=== no tests ran in 0.01s ===\n"),
        (2, "!!! Interrupted: 1 error during collection !!!\n=== 1 error in 0.30s ===\n"),
        (1, "=== 3 passed in 1.00s ===\n"),
        (1, "ImportError while loading conftest\n"),
    ],
)
def test_a_run_that_did_not_happen_is_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stdout: str
) -> None:
    _pytest_exits(monkeypatch, returncode, stdout)

    failures, summary = sweep.run_pytest([], 1, tmp_path / "pytest.log")

    assert failures == {sweep.SESSION_CRASH}
    assert summary.startswith(f"session crashed (exit {returncode}): ")


@pytest.mark.parametrize(
    ("returncode", "stdout", "failed"),
    [
        (0, "=== 5 passed in 1.00s ===\n", ()),
        (1, "=== 1 failed, 4 passed in 1.00s ===\n", ("tests/test_x.py::test_y[a b]",)),
    ],
)
def test_a_finished_run_reports_exactly_its_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    failed: tuple[str, ...],
) -> None:
    _pytest_exits(monkeypatch, returncode, stdout, failed)

    failures, summary = sweep.run_pytest([], 1, tmp_path / "pytest.log")

    assert failures == set(failed)
    assert summary == stdout.strip()


def _state(out: Path, providers: list[str], baseline_failures: tuple[str, ...] = ()) -> None:
    out.mkdir()
    group = {"providers": providers, "counts": {}, "summary": "done", "failures": []}
    baseline = {"failures": list(baseline_failures), "summary": "done"}
    state = {"baseline": baseline, "groups": {"group00": group}}
    (out / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    delisted: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "delist", lambda group: delisted.append(group) or {})
    monkeypatch.setattr(sweep, "run_pytest", lambda *_: (set(), "1 passed"))
    return delisted


def test_a_sweep_resumes_only_with_the_groups_it_was_written_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    _state(tmp_path / "out", providers[:8])
    delisted = _nothing_runs(monkeypatch)

    with pytest.raises(SystemExit, match="group00 holds"):
        sweep.sweep(tmp_path / "out", group_size=16, workers=1)
    assert delisted == []


def test_a_sweep_resumes_with_the_same_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    _state(tmp_path / "out", providers[:8])
    delisted = _nothing_runs(monkeypatch)

    sweep.sweep(tmp_path / "out", group_size=8, workers=1)

    # group00 is not run again, and every later provider is.
    assert [provider for group in delisted for provider in group] == providers[8:]


FAILING_BASELINES = [
    pytest.param((sweep.SESSION_CRASH,), id="crashed"),
    # A module that fails to import is recorded under its file's node id.
    pytest.param(("tests/test_x.py",), id="collection-error"),
    pytest.param(("tests/test_x.py::test_y",), id="failed-test"),
]


@pytest.mark.parametrize("baseline_failures", FAILING_BASELINES)
def test_a_sweep_starts_only_from_a_passing_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, baseline_failures: tuple[str, ...]
) -> None:
    delisted = _nothing_runs(monkeypatch)
    monkeypatch.setattr(sweep, "run_pytest", lambda *_: (set(baseline_failures), "1 failed"))

    with pytest.raises(SystemExit, match="must pass before a sweep"):
        sweep.sweep(tmp_path / "out", group_size=8, workers=1)
    assert delisted == []
    assert not (tmp_path / "out" / "state.json").exists()


@pytest.mark.parametrize("baseline_failures", FAILING_BASELINES)
def test_a_sweep_does_not_resume_from_a_failing_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, baseline_failures: tuple[str, ...]
) -> None:
    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    _state(tmp_path / "out", providers[:8], baseline_failures)
    delisted = _nothing_runs(monkeypatch)

    with pytest.raises(SystemExit, match="baseline did not pass"):
        sweep.sweep(tmp_path / "out", group_size=8, workers=1)
    assert delisted == []


def test_a_group_failure_is_attributed_to_the_provider_that_causes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Delisting the first provider breaks a module's import, which pytest
    # records under the file's node id; the suite passes otherwise.
    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    delisted: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "delist", lambda group: delisted.append(group) or {})

    def run(*_: Any) -> tuple[set[str], str]:
        if delisted and providers[0] in delisted[-1]:
            return {"tests/test_x.py"}, "1 failed"
        return set(), "1 passed"

    monkeypatch.setattr(sweep, "run_pytest", run)

    sweep.sweep(tmp_path / "out", group_size=8, workers=1)

    state = json.loads((tmp_path / "out" / "state.json").read_text(encoding="utf-8"))
    assert state["groups"]["group00"]["failures"] == ["tests/test_x.py"]
    assert {provider: single["failures"] for provider, single in state["single"].items()} == {
        provider: ["tests/test_x.py"] if provider == providers[0] else [] for provider in providers[:8]
    }
