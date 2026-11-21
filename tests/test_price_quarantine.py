"""Provider quarantine: one provider's bad data must not block every price update."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import quarantine, refresh
from tests.test_pricing_failure_isolation import refresh_run  # noqa: F401 -- shared fixture

NOW = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)


def write_state(path: Path, entries: list[tuple[str, datetime]]) -> Path:
    quarantine.dump_state(path, [quarantine.Entry(slug, "fixture reason", until) for slug, until in entries])
    return path


# --- state -----------------------------------------------------------------

def test_state_round_trips_and_only_unexpired_entries_are_active(tmp_path: Path) -> None:
    path = write_state(tmp_path / "q.json", [
        ("expired", NOW - timedelta(minutes=1)),
        ("live", NOW + timedelta(hours=1)),
        ("live", NOW + timedelta(hours=5)),  # the later expiry wins
    ])
    entries = quarantine.load_state(path)
    assert len(entries) == 3
    # An expiry beyond one quarantine from now was not written by diagnose.
    forged = quarantine.Entry("forged", "r", NOW + timedelta(days=30))
    assert quarantine.active([forged], NOW) == []
    assert [(e.slug, e.until) for e in quarantine.active(entries, NOW)] == [("live", NOW + timedelta(hours=5))]


@pytest.mark.parametrize("body", [
    "", "{", "[]", '{"quarantined": [{"slug": "x"}]}', '{"quarantined": null}', '{"quarantined": 3}',
    '{"quarantined": [1, true, "x"]}', '{"quarantined": {"slug": "x"}}',
    '{"quarantined": [{"slug": "x", "reason": "r", "until": "2026-11-21T00:00:00"}]}',  # naive time
])
def test_unreadable_state_quarantines_nothing(tmp_path: Path, body: str) -> None:
    path = tmp_path / "q.json"
    path.write_text(body)
    assert quarantine.load_state(path) == []


def test_quarantine_is_read_from_the_named_state_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TR_PRICE_QUARANTINE", raising=False)
    assert quarantine.quarantined_providers(NOW) == {}
    path = write_state(tmp_path / "q.json", [("gmi", NOW + timedelta(hours=1))])
    monkeypatch.setenv("TR_PRICE_QUARANTINE", str(path))
    assert quarantine.quarantined_providers(NOW) == {"gmi": "fixture reason"}
    assert quarantine.quarantined_providers(NOW + timedelta(hours=2)) == {}
    # Within a run, activity is judged at the time the workflow loaded it.
    monkeypatch.setenv("TR_PRICE_QUARANTINE_AT", NOW.isoformat())
    assert quarantine.quarantined_providers() == {"gmi": "fixture reason"}
    monkeypatch.setenv("TR_PRICE_QUARANTINE_AT", (NOW + timedelta(hours=2)).isoformat())
    assert quarantine.quarantined_providers() == {}


# --- culprit search ---------------------------------------------------------

def oracle(*culprits: str, flaky: bool = False):
    calls: list[frozenset[str]] = []

    def fails(held: frozenset[str]) -> bool:
        calls.append(held)
        if flaky:
            return False
        return not set(culprits) <= held

    return fails, calls


UNITS = [f"p{i:02d}" for i in range(40)]


@pytest.mark.parametrize("culprits", [("p00",), ("p17",), ("p39",), ("p03", "p31"), ("p05", "p06", "p07")])
def test_search_finds_exactly_the_providers_that_break_the_tests(culprits: tuple[str, ...]) -> None:
    fails, calls = oracle(*culprits)
    result = quarantine.find_culprits(UNITS, fails)
    assert result.status == "culprits"
    assert result.culprits == sorted(culprits)
    assert len(calls) == result.probes <= quarantine.MAX_PROBES
    # Every probe is distinct: results are cached, never re-run.
    assert len(set(calls)) == len(calls)


def test_a_failure_that_does_not_reproduce_quarantines_nothing() -> None:
    fails, calls = oracle(flaky=True)
    assert quarantine.find_culprits(UNITS, fails).status == "not_reproduced"
    assert calls == [frozenset()]


def test_a_failure_no_provider_explains_quarantines_nothing() -> None:
    # Restoring every changed provider still fails: code, OpenRouter's feed or
    # the environment, never one provider's data.
    result = quarantine.find_culprits(UNITS, lambda _held: True)
    assert result.status == "not_provider_data"
    assert result.culprits == []
    assert quarantine.find_culprits([], lambda _held: True).status == "not_provider_data"


def test_too_many_culprits_is_a_systemic_failure_not_a_quarantine() -> None:
    fails, _ = oracle("p01", "p02", "p03", "p04")
    result = quarantine.find_culprits(UNITS, fails, max_culprits=3)
    assert result.status == "too_many"


def test_the_probe_budget_bounds_the_search() -> None:
    fails, calls = oracle("p01", "p20", "p38")
    result = quarantine.find_culprits(UNITS, fails, max_probes=5, max_culprits=10)
    assert result.status == "budget"
    assert len(calls) == 5


# --- failure records --------------------------------------------------------

def test_failed_tests_union_every_shard_record(tmp_path: Path) -> None:
    for shard, failed, errors in (("1", ["tests/d.py::t@p3-dormancy", "tests/e.py::t[a@b]"], []),
                                  ("3", ["tests/a.py::t[x y]"], []),
                                  ("heavy-1", ["tests/b.py::t"], ["tests/c.py"])):
        (tmp_path / f"test-failures-{shard}").mkdir()
        (tmp_path / f"test-failures-{shard}" / "failures.json").write_text(
            json.dumps({"failed": failed, "collection_errors": errors}))
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "failures.json").write_text("{")
    # xdist group suffixes are dropped so a serial pytest finds the test.
    assert quarantine.failed_tests(tmp_path) == (
        ["tests/a.py::t[x y]", "tests/b.py::t", "tests/d.py::t", "tests/e.py::t[a@b]"], ["tests/c.py"])


def test_collection_errors_quarantine_nothing(tmp_path: Path) -> None:
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "failures.json").write_text(json.dumps({"failed": [], "collection_errors": ["tests/x.py"]}))
    out = tmp_path / "out.json"
    code = quarantine.main(["diagnose", "--baseline", str(tmp_path), "--failures", str(tmp_path / "f"),
                            "--state", str(tmp_path / "none.json"), "--out", str(out), "--ci-run", "1"])
    assert code == 3
    assert not out.exists()


# --- the refresh honours a quarantine ---------------------------------------

def test_a_quarantined_provider_keeps_its_committed_state_while_the_rest_refresh(
    refresh_run: dict[str, Any],  # noqa: F811
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    state = write_state(tmp_path / "quarantine.json", [
        ("good_c", datetime.now(UTC) + timedelta(hours=1)),
        ("good_b", datetime.now(UTC) - timedelta(hours=1)),  # expired: refreshed again
        ("not_a_refreshed_provider", datetime.now(UTC) + timedelta(hours=1)),
    ])
    monkeypatch.setenv("TR_PRICE_QUARANTINE", str(state))
    # A quarantine counts toward the majority-failure rule like any failure;
    # keep this fixture's other failures below it.
    refresh_run["fetch_failures"].discard("bad_b")
    assert refresh.main([]) == 0

    root = refresh_run["root"]
    before = refresh_run["before"]
    assert (root / "provider_models/good_c.json").read_bytes() == before[Path("provider_models/good_c.json")]
    assert (root / "parsers/good_c.py").read_bytes() == before[Path("parsers/good_c.py")]
    # Only a stale recovery from its committed manifest may stand in for it.
    kept = refresh_run["results"].get("good_c")
    assert kept is None or kept.source.startswith("stale_")
    for slug in ("good_a", "good_b", "good_d"):
        assert json.loads((root / f"provider_models/{slug}.json").read_text())["generated_at"] == "fresh"
    models = {row["id"] for row in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]}
    # good_c had no committed row: it stays absent, never priced from the
    # quarantined fresh data.
    assert "acme/good_c" not in models
    assert {"acme/good_b", "acme/good_d"} <= models
    out = capsys.readouterr().out
    assert refresh.HELD_FOR_REVIEW_HEADING in out
    assert "  good_c:\n    refresh failed: quarantined: fixture reason" in out
    assert "good_b:\n    refresh failed: quarantined" not in out


# --- diagnosis on a repository tree ------------------------------------------

def test_diagnosis_names_the_provider_and_leaves_the_tree_as_it_found_it(
    refresh_run: dict[str, Any],  # noqa: F811
    tmp_path: Path,
) -> None:
    root = refresh_run["root"]
    baseline = tmp_path / "baseline"
    for relative, data in refresh_run["before"].items():
        target = baseline / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    assert refresh.main([]) == 0  # good_* refreshed; bad_* held
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
             if p.is_file() and baseline not in p.parents}

    def good_c_fresh_breaks_a_test(_tests: list[str]) -> bool:
        raw = json.loads((refresh.PROVIDER_MANIFEST_DIR / "good_c.json").read_text())
        return raw.get("generated_at") == "fresh"

    assert quarantine.changed_providers(baseline) == ["good_a", "good_b", "good_c", "good_d"]
    result = quarantine.diagnose(baseline, ["tests/x.py::t"], run_tests=good_c_fresh_breaks_a_test)
    assert (result.status, result.culprits) == ("culprits", ["good_c"])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and baseline not in p.parents} == after
    shutil.rmtree(baseline)


def test_diagnose_cli_records_the_culprits_for_the_next_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "failures.json").write_text(json.dumps({"failed": ["tests/x.py::t"], "collection_errors": []}))
    previous = write_state(tmp_path / "in.json", [("older", datetime.now(UTC) + timedelta(hours=2))])
    monkeypatch.setattr(quarantine, "diagnose",
                        lambda _baseline, tests: quarantine.Diagnosis("culprits", ["gmi"], 7))
    out = tmp_path / "out.json"
    assert quarantine.main(["diagnose", "--baseline", str(tmp_path), "--failures", str(tmp_path / "f"),
                            "--state", str(previous), "--out", str(out), "--ci-run", "44811033112"]) == 0
    entries = quarantine.load_state(out)
    assert [e.slug for e in entries] == ["gmi", "older"]
    gmi = entries[0]
    assert "snapshot CI run 44811033112" in gmi.reason and "tests/x.py::t" in gmi.reason
    hours = (gmi.until - datetime.now(UTC)).total_seconds() / 3600
    assert quarantine.QUARANTINE_HOURS - 0.1 < hours <= quarantine.QUARANTINE_HOURS
    assert "::error title=Provider quarantined from price refresh::gmi" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["not_reproduced", "not_provider_data", "too_many", "budget", "no_tests"])
def test_diagnose_cli_writes_no_state_without_a_clean_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "failures.json").write_text(json.dumps({"failed": ["tests/x.py::t"], "collection_errors": []}))
    monkeypatch.setattr(quarantine, "diagnose", lambda _baseline, tests: quarantine.Diagnosis(status, ["x"]))
    out = tmp_path / "out.json"
    assert quarantine.main(["diagnose", "--baseline", str(tmp_path), "--failures", str(tmp_path / "f"),
                            "--state", str(tmp_path / "in.json"), "--out", str(out), "--ci-run", "1"]) == 3
    assert not out.exists()


def test_probes_run_without_the_refresh_jobs_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs, command=command)
        return type("Done", (), {"returncode": 1})()

    monkeypatch.setenv("TOGETHER_API_KEY", "secret-value")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(quarantine.subprocess, "run", fake_run)
    assert quarantine._run_tests(["tests/x.py::t"]) is True
    assert "TOGETHER_API_KEY" not in seen["env"] and "GITHUB_ACTIONS" not in seen["env"]
    assert seen["env"]["TR_STORAGE_BACKEND"] == "memory"
    # Not the job's home: its gcloud login and credential files stay out of reach.
    import os
    assert seen["env"]["HOME"] != os.environ.get("HOME")
    assert seen["env"]["CLOUDSDK_CONFIG"].startswith(seen["env"]["HOME"])
    assert seen["command"][-2:] == ["--", "tests/x.py::t"]
    assert quarantine.PROBE_MARKERS in seen["command"]


def test_prune_and_active_cli_keep_only_unexpired_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    state = write_state(tmp_path / "in.json", [("gone", now - timedelta(hours=1)), ("kept", now + timedelta(hours=1))])
    assert quarantine.main(["active", str(state)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("kept: fixture reason (until ") and "gone" not in out
    assert quarantine.main(["prune", str(state), str(tmp_path / "out.json")]) == 0
    assert [e.slug for e in quarantine.load_state(tmp_path / "out.json")] == ["kept"]
    # A missing state saves an empty one, so the chain of artifacts continues.
    assert quarantine.main(["prune", str(tmp_path / "missing.json"), str(tmp_path / "empty.json")]) == 0
    assert json.loads((tmp_path / "empty.json").read_text()) == {"quarantined": []}


# --- workflow wiring ---------------------------------------------------------

def workflow_steps() -> list[dict[str, Any]]:
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/refresh-prices.yml").read_text())
    return workflow["jobs"]["refresh"]["steps"]


def step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in steps if item.get("name") == name)


def test_the_refresh_reads_the_quarantine_before_any_provider_refresh() -> None:
    steps = workflow_steps()
    load = step(steps, "Load provider quarantine")
    refresh_step = step(steps, "Refresh provider-direct prices (with self-heal)")
    assert steps.index(load) < steps.index(refresh_step)
    # A broken load never blocks prices; it points the refresh at an empty state.
    assert load["continue-on-error"] is True
    run = load["run"]
    assert run.index('echo "TR_PRICE_QUARANTINE=/tmp/price-quarantine.json" >> "$GITHUB_ENV"') < run.index("gh run")
    assert "printf '{\"quarantined\": []}\\n' > /tmp/price-quarantine.json" in run
    assert "-n price-quarantine" in run and ".workflow_run.id != ${GITHUB_RUN_ID}" in run
    # Only the newest artifact: no fallback that could revive a quarantine.
    assert "sort_by(.created_at) | last" in run and "for run in" not in run
    # Only this workflow's own scheduled or dispatched runs on main may write it.
    assert '--workflow refresh-prices.yml' in run and 'select(.event == "schedule" or .event == "workflow_dispatch")' in run
    assert r"\$trusted | index(\$id)" in run
    assert 'echo "TR_PRICE_QUARANTINE_AT=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" >> "$GITHUB_ENV"' in run


def test_quarantine_runs_only_after_a_failed_snapshot_ci_and_resaves_state() -> None:
    steps = workflow_steps()
    names = [item.get("name") for item in steps]
    publication = names.index("Commit, verify, and push if changed")
    diagnose = step(steps, "Quarantine providers that failed snapshot CI")
    prune = step(steps, "Keep the active provider quarantine")
    save = step(steps, "Save the provider quarantine for the next run")
    recompute = step(steps, "Recompute without the quarantined providers")
    assert publication < steps.index(diagnose) < steps.index(prune) < steps.index(save) < steps.index(recompute)
    assert diagnose["if"] == "failure() && steps.publication.outputs.snapshot_ci_failed != ''"
    # The tree CI ran, its parent as the published baseline, offline probes.
    assert diagnose["run"].index("git reset -q --hard HEAD") < diagnose["run"].index("quarantine diagnose")
    assert "git archive HEAD^" in diagnose["run"]
    assert "-p 'test-failures-*'" in diagnose["run"]
    assert diagnose["timeout-minutes"] <= 45
    for kept in (prune, save):
        assert kept["if"] == "always()" and kept["continue-on-error"] is True
    assert save["with"]["name"] == "price-quarantine"
    assert recompute["if"] == "always() && steps.quarantine.outputs.quarantined == 'true'"
    assert recompute["run"].startswith("gh workflow run refresh-prices.yml --ref main")
