from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.deploy import coordinate_secondaries as coordinator
from scripts.deploy.coordinate_secondaries import REGIONS, coordinate


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TR_DEPLOY_MUTEX_OPERATION", "operation1")
    monkeypatch.setenv("TR_DEPLOY_MUTEX_GENERATION", "42")
    for region, key in REGIONS.items():
        monkeypatch.setenv(key, region + "-new")
    monkeypatch.setenv("EVENTS", str(tmp_path / "events"))
    # Real processes and handoff files, no cloud calls and no fake wait().
    program = tmp_path / "worker.py"
    program.write_text('''import json, os, pathlib, sys, time
region = sys.argv[1]
ready = pathlib.Path(os.environ['TR_SECONDARY_READY_FILE'])
abort = pathlib.Path(os.environ['TR_SECONDARY_ABORT_FILE'])
def event(name):
    with open(os.environ['EVENTS'], 'a') as stream:
        stream.write(json.dumps([region, name]) + '\\n')
def wait_for(condition):
    deadline = time.monotonic() + float(os.environ.get('WAIT', '20'))
    while not condition():
        if time.monotonic() > deadline:
            raise RuntimeError('test worker handshake timed out')
        time.sleep(.005)
event('ramp')
(ready.parent / (region + '.started')).touch()
if region == os.environ.get('FAIL_EARLY'):
    sys.exit(1)
if os.environ.get('SIBLING_ABORT'):
    # staged_traffic.sh: a shift after a sibling's failure rolls back instead.
    wait_for(abort.exists)
    time.sleep(.2)  # the rollback itself takes time; a kill would cut it short
    event('rolled_back')
    sys.exit(1)
if os.environ.get('BARRIER'):
    # No region canaries until every region has begun its ramp.
    wait_for(lambda: all((ready.parent / (name + '.started')).exists()
                         for name in ('europe-west4', 'us-east4', 'southamerica-east1')))
if os.environ.get('HANG') or region == os.environ.get('HANG_REGION'):
    time.sleep(60)
if os.environ.get('FAIL_LATE') and region == 'us-east4':
    wait_for(abort.exists)
    event('retained')
    event('billing')
    sys.exit(0)
event('canary')
if not os.environ.get('NO_HANDOFF'):
    # Match staged_traffic.sh: readers must never observe a partial handoff.
    pending = pathlib.Path(str(ready) + '.tmp')
    pending.write_text(region + '\\n' + region + '-new\\n' +
                       os.environ.get('HANDOFF_OPERATION', 'operation1') + '\\n')
    pending.replace(ready)
event('drain')
if region == os.environ.get('FAIL_LATE'):
    wait_for((ready.parent / 'us-east4.started').exists)
    sys.exit(1)
if not os.environ.get('NO_HANDOFF'):
    wait_for((ready.parent / 'southamerica-east1.started').exists)
event('retained')
event('billing')
''')
    script = tmp_path / "worker.sh"
    script.write_text(f'#!/bin/bash\nexec {sys.executable} "{program}" "$2"\n')
    return script


def events(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]


def test_ramps_are_serial_but_full_drains_overlap(worker: Path, tmp_path: Path) -> None:
    assert coordinate(worker, tmp_path, poll_seconds=.005) == 0
    log = events(tmp_path)
    regions = list(REGIONS)
    for first, second in zip(regions, regions[1:], strict=False):
        assert log.index([first, "canary"]) < log.index([second, "ramp"])
        assert log.index([second, "ramp"]) < log.index([first, "retained"])
    for region in regions:
        assert [region, "retained"] in log and [region, "billing"] in log


def test_failure_before_handoff_stops_other_regions(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAIL_EARLY", "europe-west4")
    assert coordinate(worker, tmp_path, poll_seconds=.005) == 1
    assert events(tmp_path) == [["europe-west4", "ramp"]]


def test_late_failure_stops_admission_and_joins_retention(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAIL_LATE", "europe-west4")
    assert coordinate(worker, tmp_path, poll_seconds=.005) == 1
    log = events(tmp_path)
    assert ["us-east4", "retained"] in log
    assert ["southamerica-east1", "ramp"] not in log


def test_no_handoff_falls_back_to_serial_completion(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_HANDOFF", "1")
    assert coordinate(worker, tmp_path, poll_seconds=.005) == 0
    log = events(tmp_path)
    assert log.index(["europe-west4", "retained"]) < log.index(["us-east4", "ramp"])


def test_wrong_operation_cannot_admit_next_region(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HANDOFF_OPERATION", "old-operation")
    with pytest.raises(ValueError, match="invalid retention handoff"):
        coordinate(worker, tmp_path, poll_seconds=.005)
    assert ["us-east4", "ramp"] not in events(tmp_path)


def test_timeout_reaps_workers_without_starting_another(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HANG", "1")
    children = []
    popen = coordinator.subprocess.Popen

    def record_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(coordinator.subprocess, "Popen", record_child)
    with pytest.raises(TimeoutError, match="retaining reservation"):
        coordinate(worker, tmp_path, poll_seconds=.005, timeout=1)
    assert len(children) == 1 and children[0].poll() is not None
    if (tmp_path / "events").exists():
        assert events(tmp_path) == [["europe-west4", "ramp"]]


def test_ramps_start_together_by_default(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BARRIER", "1")
    assert coordinate(worker, tmp_path, poll_seconds=.005, serial=False) == 0
    log = events(tmp_path)
    regions = list(REGIONS)
    last_ramp = max(log.index([region, "ramp"]) for region in regions)
    for region in regions:
        assert log.index([region, "canary"]) > last_ramp
        assert [region, "retained"] in log and [region, "billing"] in log


def test_serial_ramps_never_overlap_a_sibling_ramp(worker: Path, tmp_path: Path, monkeypatch) -> None:
    # The barrier needs every ramp begun before any canary: serial cannot.
    monkeypatch.setenv("BARRIER", "1")
    monkeypatch.setenv("WAIT", "1")
    assert coordinate(worker, tmp_path, poll_seconds=.005) == 1
    assert ["us-east4", "ramp"] not in events(tmp_path)


def test_together_a_failure_rolls_back_in_flight_siblings(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAIL_EARLY", "southamerica-east1")
    monkeypatch.setenv("SIBLING_ABORT", "1")
    assert coordinate(worker, tmp_path, poll_seconds=.005, serial=False) == 1
    log = events(tmp_path)
    assert ["europe-west4", "rolled_back"] in log and ["us-east4", "rolled_back"] in log
    assert not [event for event in log if event[1] == "canary"]


def test_together_a_wrong_operation_handoff_fails_like_a_worker(worker: Path, tmp_path: Path, monkeypatch) -> None:
    # Siblings may be mid-ramp: they roll back at their next shift rather
    # than being killed part way.
    monkeypatch.setenv("HANDOFF_OPERATION", "old-operation")
    assert coordinate(worker, tmp_path, poll_seconds=.005, serial=False) == 1
    assert (tmp_path / "abort").exists()


def test_together_a_timeout_lets_siblings_roll_back_before_the_kill(worker: Path, tmp_path: Path,
                                                                      monkeypatch) -> None:
    monkeypatch.setenv("HANG_REGION", "europe-west4")
    monkeypatch.setenv("SIBLING_ABORT", "1")
    with pytest.raises(TimeoutError, match="retaining reservation"):
        coordinate(worker, tmp_path, poll_seconds=.005, timeout=1, serial=False, abort_grace=10)
    log = events(tmp_path)
    assert ["us-east4", "rolled_back"] in log and ["southamerica-east1", "rolled_back"] in log


def test_together_held_regions_finish_without_a_handoff(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_HANDOFF", "1")
    assert coordinate(worker, tmp_path, poll_seconds=.005, serial=False) == 0
    assert sorted(region for region, name in events(tmp_path) if name == "billing") == sorted(REGIONS)


@pytest.mark.parametrize(("value", "serial"), [("1", True), (" 1 ", True), ("", False), ("0", False), ("true", False)])
def test_the_repository_variable_restores_serial_ramps(monkeypatch, value: str, serial: bool) -> None:
    monkeypatch.setenv("TR_DEPLOY_SECONDARIES_SERIAL", value)
    seen = {}
    monkeypatch.setattr(coordinator.signal, "signal", lambda *_: None)
    monkeypatch.setattr(coordinator, "coordinate", lambda *_, **kwargs: seen.update(kwargs) or 0)
    assert coordinator.main() == 0
    assert seen == {"serial": serial}


def test_outer_fence_required(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TR_DEPLOY_MUTEX_GENERATION")
    with pytest.raises(ValueError, match="outer deployment fence"):
        coordinate(worker, tmp_path)
    assert not (tmp_path / "events").exists()
