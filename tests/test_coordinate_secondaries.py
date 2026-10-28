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
    deadline = time.monotonic() + 20
    while not condition():
        if time.monotonic() > deadline:
            raise RuntimeError('test worker handshake timed out')
        time.sleep(.005)
event('ramp')
(ready.parent / (region + '.started')).touch()
if region == os.environ.get('FAIL_EARLY'):
    sys.exit(1)
if os.environ.get('HANG'):
    time.sleep(60)
if os.environ.get('FAIL_LATE') and region == 'us-east4':
    wait_for(abort.exists)
    event('retained')
    event('billing')
    sys.exit(0)
event('canary')
if not os.environ.get('NO_HANDOFF'):
    ready.write_text(region + '\\n' + region + '-new\\n' +
                     os.environ.get('HANDOFF_OPERATION', 'operation1') + '\\n')
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


def test_outer_fence_required(worker: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TR_DEPLOY_MUTEX_GENERATION")
    with pytest.raises(ValueError, match="outer deployment fence"):
        coordinate(worker, tmp_path)
    assert not (tmp_path / "events").exists()
