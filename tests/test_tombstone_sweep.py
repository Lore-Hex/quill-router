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


def _data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Two providers' manifests and a snapshot, and a tests/ tree, under tmp_path."""
    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    rows = {
        "alpha": [{"id": "maker/model-a", "routable": True}, {"id": "maker/model-b"}],
        "beta": [
            {"id": "maker/model-a"},
            {"id": "maker/model-a-fast", "routable": True},
            {"id": "maker/gone", "routable": False, "routable_reason": "delisted-upstream"},
        ],
    }
    for provider, provider_rows in rows.items():
        (manifests / f"{provider}.json").write_text(json.dumps({"models": provider_rows}), encoding="utf-8")
    snapshot = tmp_path / "openrouter_snapshot.json"
    snapshot.write_text(json.dumps({"models": [
        {"id": "maker/model-a", "endpoints": [{"tr_provider_slug": "alpha"}]},
        {"id": "maker/model-a-fast", "endpoints": [{"tr_provider_slug": "beta"}]},
    ]}), encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text('MODEL = "maker/model-a"\n', encoding="utf-8")
    (tests / "test_route.py").write_text('ROUTE = "maker/model-a@alpha/prepaid"\n', encoding="utf-8")
    (tests / "test_page.py").write_text('PATH = "/models/maker/model-a-fast"\n', encoding="utf-8")
    (tests / "test_preset.py").write_text('PRESET = "trustedrouter/preset"\n', encoding="utf-8")
    monkeypatch.setattr(sweep, "MANIFESTS", manifests)
    monkeypatch.setattr(sweep, "SNAPSHOT", snapshot)
    monkeypatch.setattr(sweep, "ROOT", tmp_path)
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "catalog_dependents", lambda: {"maker/model-a": ["trustedrouter/preset"]})
    return tmp_path


def test_a_model_vanishing_tombstones_it_on_every_host_and_drops_it_from_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _data(monkeypatch, tmp_path)

    counts = sweep.delist_model("maker/model-a")

    assert counts == {"rows": 2, "snapshot_models": 1}
    for provider in ("alpha", "beta"):
        rows = json.loads((root / "provider_models" / f"{provider}.json").read_text())["models"]
        model_rows = [row for row in rows if row["id"] == "maker/model-a"]
        assert [(row["routable"], row["routable_reason"]) for row in model_rows] == [
            (False, "delisted-upstream")
        ]
    # A model with a longer id sharing the prefix is untouched.
    snapshot = json.loads((root / "openrouter_snapshot.json").read_text())
    assert [model["id"] for model in snapshot["models"]] == ["maker/model-a-fast"]
    assert sweep.delist_model("maker/gone") == {"rows": 0, "snapshot_models": 0}


def test_a_model_is_swept_over_the_files_naming_it_or_a_model_depending_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _data(monkeypatch, tmp_path)

    assert sweep.models_to_sweep() == {
        # The id, an endpoint id and a preset that depends on it; not the page
        # of the longer id that shares its prefix.
        "maker/model-a": ["tests/test_a.py", "tests/test_preset.py", "tests/test_route.py"],
        "maker/model-a-fast": ["tests/test_page.py"],
    }


def test_the_models_sweep_attributes_each_failure_to_the_model_that_vanished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _data(monkeypatch, tmp_path)
    vanished: list[str] = []
    monkeypatch.setattr(sweep, "delist_model", lambda model_id: vanished.append(model_id) or {"rows": 1})

    def run(files: list[str], *_: Any) -> tuple[set[str], str]:
        if vanished and vanished[-1] == "maker/model-a" and "tests/test_route.py" in files:
            return {"tests/test_route.py::test_route"}, "1 failed"
        return set(), "1 passed"

    monkeypatch.setattr(sweep, "run_pytest", run)

    sweep.models(root / "out", workers=2, only=[])

    state = json.loads((root / "out" / "models.json").read_text(encoding="utf-8"))
    assert {model_id: result["failures"] for model_id, result in state["models"].items()} == {
        "maker/model-a": ["tests/test_route.py::test_route"],
        "maker/model-a-fast": [],
    }


def test_the_models_sweep_starts_only_from_passing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _data(monkeypatch, tmp_path)
    vanished: list[str] = []
    monkeypatch.setattr(sweep, "delist_model", lambda model_id: vanished.append(model_id) or {"rows": 1})
    monkeypatch.setattr(sweep, "run_pytest", lambda *_: ({"tests/test_a.py::test_x"}, "1 failed"))

    with pytest.raises(SystemExit, match="must pass before a sweep"):
        sweep.models(root / "out", workers=2, only=[])
    assert vanished == []


def test_a_resumed_models_sweep_refuses_files_its_baseline_never_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _data(monkeypatch, tmp_path)
    runs: list[list[str]] = []
    monkeypatch.setattr(sweep, "delist_model", lambda model_id: {"rows": 1})
    monkeypatch.setattr(sweep, "run_pytest", lambda files, *_: runs.append(files) or (set(), "1 passed"))

    sweep.models(root / "out", workers=2, only=["maker/model-a-fast"])
    assert runs == [["tests/test_page.py"], ["tests/test_page.py"]]  # the baseline, then the model

    with pytest.raises(SystemExit, match="baseline did not run"):
        sweep.models(root / "out", workers=2, only=["maker/model-a"])
    assert len(runs) == 2
    # The selection the baseline covered resumes, with nothing left to run.
    sweep.models(root / "out", workers=2, only=["maker/model-a-fast"])
    assert len(runs) == 2


def test_a_value_change_scales_every_price_and_limit_of_the_chosen_provider_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    row = {
        "id": "maker/m", "status": 1, "input_token_price_per_m": 1_000_000,
        "output_token_price_per_m": 3_000_000, "cached_input_token_price_per_m": 0,
        "context_length": 131_072, "max_output_tokens": 8_192,
        "fixed_output_price_microdollars": {"1k": 1_364},
        "price_tiers": [
            {"max_prompt_tokens": 199_999, "input_token_price_per_m": 1_000_000},
            {"max_prompt_tokens": None, "input_token_price_per_m": 2_000_000},
        ],
    }
    for provider in ("alpha", "beta"):
        (manifests / f"{provider}.json").write_text(json.dumps({"models": [row]}), encoding="utf-8")
    snapshot = tmp_path / "openrouter_snapshot.json"
    endpoints = [
        {"tr_provider_slug": "alpha", "context_length": 131_072, "max_completion_tokens": None,
         "pricing": {"prompt": "0.000001", "completion": "0", "discount": 0}},
        {"tr_provider_slug": "beta", "context_length": 131_072, "pricing": {"prompt": "0.000001"}},
    ]
    snapshot.write_text(json.dumps({"models": [{"id": "maker/m", "endpoints": endpoints}]}), encoding="utf-8")
    monkeypatch.setattr(sweep, "MANIFESTS", manifests)
    monkeypatch.setattr(sweep, "SNAPSHOT", snapshot)

    counts = sweep.perturb(["alpha"])

    # Prices x1.07 and limits x0.9, rounded half up; zero and absent stay so.
    assert json.loads((manifests / "alpha.json").read_text())["models"] == [{
        **row, "input_token_price_per_m": 1_070_000, "output_token_price_per_m": 3_210_000,
        "context_length": 117_965, "max_output_tokens": 7_373,
        "fixed_output_price_microdollars": {"1k": 1_459},
        "price_tiers": [
            {"max_prompt_tokens": 179_999, "input_token_price_per_m": 1_070_000},
            {"max_prompt_tokens": None, "input_token_price_per_m": 2_140_000},
        ],
    }]
    assert json.loads((manifests / "beta.json").read_text())["models"] == [row]
    snapshot_endpoints = json.loads(snapshot.read_text())["models"][0]["endpoints"]
    assert snapshot_endpoints == [
        {"tr_provider_slug": "alpha", "context_length": 117_965, "max_completion_tokens": None,
         "pricing": {"prompt": "0.00000107", "completion": "0", "discount": 0}},
        endpoints[1],
    ]
    assert counts == {"rows": 1, "endpoints": 1, "fields": 10}


def test_the_values_sweep_changes_values_not_listings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    changed: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "delist", lambda group: pytest.fail("a value sweep delisted"))
    monkeypatch.setattr(sweep, "perturb", lambda group, down=False: changed.append(group) or {})
    monkeypatch.setattr(sweep, "run_pytest", lambda *_: (set(), "1 passed"))

    sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True, thorough=True)

    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    assert [provider for group in changed for provider in group] == providers
    assert (tmp_path / "out" / "values.json").exists()
    assert not (tmp_path / "out" / "state.json").exists()


def test_a_value_change_moves_snapshot_price_tiers_and_runs_either_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    (manifests / "alpha.json").write_text(json.dumps({"models": []}), encoding="utf-8")
    endpoint = {
        "tr_provider_slug": "alpha",
        "context_length": 1_000_000,
        "pricing": {
            "prompt": "0.00000125",
            "prompt_tiers": [
                {"max_prompt_tokens": 200_000, "prompt": "0.00000125", "input_cache_read": "0.000000125"},
                {"max_prompt_tokens": None, "prompt": "0.0000025"},
            ],
            "completion_tiers": [{"max_prompt_tokens": 200_000, "completion": "0.00001"}],
        },
    }
    snapshot = tmp_path / "openrouter_snapshot.json"
    monkeypatch.setattr(sweep, "MANIFESTS", manifests)
    monkeypatch.setattr(sweep, "SNAPSHOT", snapshot)

    def changed(**direction: bool) -> dict[str, Any]:
        snapshot.write_text(json.dumps({"models": [{"id": "maker/m", "endpoints": [endpoint]}]}), encoding="utf-8")
        sweep.perturb(["alpha"], **direction)
        return json.loads(snapshot.read_text())["models"][0]["endpoints"][0]

    # Prices x1.07 and limits x0.9, tier prices and thresholds too; an open-ended tier stays open.
    assert changed() == {
        "tr_provider_slug": "alpha",
        "context_length": 900_000,
        "pricing": {
            "prompt": "0.0000013375",
            "prompt_tiers": [
                {"max_prompt_tokens": 180_000, "prompt": "0.0000013375", "input_cache_read": "0.00000013375"},
                {"max_prompt_tokens": None, "prompt": "0.000002675"},
            ],
            "completion_tiers": [{"max_prompt_tokens": 180_000, "completion": "0.0000107"}],
        },
    }
    # Down: prices x0.93 and limits x1.1.
    down = changed(down=True)
    assert down["context_length"] == 1_100_000
    assert down["pricing"]["prompt"] == "0.0000011625"
    assert down["pricing"]["prompt_tiers"][0]["max_prompt_tokens"] == 220_000
    assert down["pricing"]["completion_tiers"][0]["completion"] == "0.0000093"


def test_a_values_sweep_resumes_only_in_the_mode_it_was_written_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "perturb", lambda group, down=False: {})
    monkeypatch.setattr(sweep, "run_pytest", lambda files, *_: runs.append(files) or (set(), "1 passed"))

    sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True)
    ran = len(runs)
    for other_mode in ({"thorough": True}, {"down": True}):
        with pytest.raises(SystemExit, match="sweep into a new OUT"):
            sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True, **other_mode)
    assert len(runs) == ran
    # The mode it was written in resumes, with nothing left to run.
    sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True)
    assert len(runs) == ran


def test_the_values_sweep_changes_everything_once_then_attributes_on_the_failed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    providers = sorted(path.stem for path in sweep.MANIFESTS.glob("*.json"))
    changed: list[list[str]] = []
    runs: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "perturb", lambda group, down=False: changed.append(group) or {})

    def run(files: list[str], *_: Any) -> tuple[set[str], str]:
        runs.append(files)
        if changed and providers[0] in changed[-1] and len(runs) > 1:
            return {"tests/test_x.py::test_pin"}, "1 failed"
        return set(), "1 passed"

    monkeypatch.setattr(sweep, "run_pytest", run)

    sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True)

    state = json.loads((tmp_path / "out" / "values.json").read_text(encoding="utf-8"))
    assert changed[0] == providers  # every provider at once, over the full suite
    assert runs[:2] == [[], []]  # the baseline, then that run
    assert all(files == ["tests/test_x.py"] for files in runs[2:])  # only the failed file after
    assert state["single"][providers[0]]["failures"] == ["tests/test_x.py::test_pin"]
    assert all(not single["failures"] for provider, single in state["single"].items() if provider != providers[0])


def test_a_values_sweep_with_nothing_pinned_stops_after_one_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    changed: list[list[str]] = []
    monkeypatch.setattr(sweep, "restore", lambda: None)
    monkeypatch.setattr(sweep, "perturb", lambda group, down=False: changed.append(group) or {})
    monkeypatch.setattr(sweep, "run_pytest", lambda *_: (set(), "1 passed"))

    sweep.sweep(tmp_path / "out", group_size=8, workers=1, values=True)

    assert len(changed) == 1
    assert "groups" not in json.loads((tmp_path / "out" / "values.json").read_text(encoding="utf-8"))
