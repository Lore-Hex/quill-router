"""scripts/ci/failure_record.py on a small synthetic project, run as ci.yml's
test shards run it: in GitHub Actions (GITHUB_ACTIONS, RUNNER_TEMP), under
xdist, loaded the way tests/conftest.py loads it. It runs in every CI pytest,
the deploy-gating shards among them: whatever happens to the record, the
session's outcome must not change."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

CONFTEST = '''
import os

if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_TEMP"):
    pytest_plugins = ["scripts.ci.failure_record"]
'''

# Read the record from inside the session: still open, and the controller's.
READ = '''
def read_record():
    import json, os
    with open(os.path.join(os.environ["RUNNER_TEMP"], "test-failures", "failures.json")) as handle:
        return json.load(handle)
'''

TESTS = READ + '''
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def test_passes():
    assert True


def test_fails():
    assert False


@pytest.mark.parametrize("value", ["a b", "c.d"])
def test_param(value):
    assert value == "c.d"


@pytest.fixture
def broken():
    raise RuntimeError("setup")


def test_setup_error(broken):
    pass


@pytest.mark.xfail(reason="known")
def test_expected_failure():
    assert False


def test_the_record_is_the_controllers_and_open():
    # Under xdist this runs in a worker: the record is the controller's
    # (its parent), written when the session started, not this worker's.
    record = read_record()
    assert record["pid"] == os.getppid() and record["complete"] is False


def test_runs_pytest_in_process(tmp_path):
    # As tests/test_lock_order_guard.py does: an inner session whose failure
    # is that test's business, not a failure of this run.
    (tmp_path / "conftest.py").write_text(open(os.path.join(HERE, "conftest.py")).read())
    (tmp_path / "test_inner.py").write_text("def test_inner():\\n    assert False\\n")
    assert pytest.main([str(tmp_path / "test_inner.py"), "-q", "-p", "no:cacheprovider",
                        "--confcutdir=" + str(tmp_path), "-o", "addopts="]) == 1
    assert read_record()["pid"] == os.getppid()


def test_runs_pytest_in_a_subprocess_with_a_filtered_environment(tmp_path):
    # The child loads the plugin (same conftest, GITHUB_ACTIONS and
    # RUNNER_TEMP kept) without the owner variable: it must not take the
    # record over, mid-session or at its end.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").write_text(open(os.path.join(HERE, "conftest.py")).read())
    (tmp_path / "tests/test_child.py").write_text("def test_child():\\n    assert False\\n")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "PYTHONPATH", "GITHUB_ACTIONS", "RUNNER_TEMP")
           if k in os.environ}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                           "--rootdir", str(tmp_path), "tests"], cwd=tmp_path, env=env, capture_output=True,
                          text=True)
    assert done.returncode == 1, done.stdout
    record = read_record()
    assert record["pid"] == os.getppid() and record["complete"] is False
    assert "tests/test_child.py::test_child" not in record["failed"]
'''


def _env(temp: Path | None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")}
    env = {**base, "PYTHONPATH": str(REPO), "GITHUB_ACTIONS": "true"}
    if temp is not None:
        env["RUNNER_TEMP"] = str(temp)
    return env


def _run(root: Path, temp: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",  # noqa: S603 -- fixed argv
                           "-o", "addopts=", "--rootdir", str(root), "tests", *args],
                          cwd=root, env=_env(temp), capture_output=True, text=True, timeout=300)


def _project(tmp_path: Path, tests: str = TESTS) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests/conftest.py").write_text(CONFTEST)
    (root / "tests/test_sample.py").write_text(tests)
    return root


def _record(temp: Path) -> dict:
    return json.loads((temp / "test-failures/failures.json").read_text())


def test_a_shard_records_every_failure_by_its_exact_node_id(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "tests/test_broken.py").write_text("import not_a_module_anywhere\n")
    (root / "tests/sub").mkdir()
    (root / "tests/sub/conftest.py").write_text("raise RuntimeError('a failing conftest')\n")
    (root / "tests/sub/test_under.py").write_text("def test_under():\n    pass\n")
    result = _run(root, tmp_path / "runner-temp", "-n", "2")
    assert result.returncode == 1, result.stdout + result.stderr
    record = _record(tmp_path / "runner-temp")
    # Exact IDs, spaces included; a setup error counts; an expected failure
    # does not; a pytest a test ran (in-process or as a child) is not this
    # run, and neither took the record over (the in-session tests passed).
    assert record["failed"] == ["tests/test_sample.py::test_fails", "tests/test_sample.py::test_param[a b]",
                                "tests/test_sample.py::test_setup_error"]
    # A module, and a directory whose conftest.py failed.
    assert record["collection_errors"] == ["tests/sub", "tests/test_broken.py"]
    # Every test it collected ran (the modules that did not collect are counted apart).
    assert record["complete"] is True and record["collected"] == record["reported"] == 9


def test_complete_means_every_collected_test_reported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run(root, tmp_path / "runner-temp", "-n", "2")
    assert result.returncode == 1, result.stdout + result.stderr
    record = _record(tmp_path / "runner-temp")
    assert record["complete"] is True and record["exitstatus"] == 1
    assert record["collected"] == record["reported"] == 9


@pytest.mark.parametrize(("tests", "args"), [
    # A KeyboardInterrupt in a test: pytest stops, exit 2.
    ("def test_a():\n    assert False\n\ndef test_b():\n    raise KeyboardInterrupt\n\n"
     "def test_c():\n    assert False\n", ()),
    # xdist's worker-restart limit: exit 1, most tests never ran.
    ("import os\n\ndef test_a():\n    os._exit(1)\n\n"
     + "".join(f"def test_c{i}():\n    assert False\n\n" for i in range(10)), ("-n", "1", "--max-worker-restart", "0")),
    # SIGINT to the xdist controller.
    ("import os, signal, time\n\ndef test_a():\n    assert False\n\n"
     "def test_b():\n    os.kill(int(os.environ['TR_FAILURE_RECORD_OWNER']), signal.SIGINT)\n    time.sleep(3)\n\n"
     + "".join(f"def test_c{i}():\n    import time; time.sleep(0.2); assert False\n\n" for i in range(20)), ("-n", "1")),
    # The session dies outright: the record stays as the start wrote it.
    ("import os\n\ndef test_dies():\n    os._exit(3)\n", ()),
    # Every test reported, then a KeyboardInterrupt in a session fixture's
    # teardown: exit 2, so not complete although reported == collected.
    ("import pytest\n\n@pytest.fixture(scope='session')\ndef held():\n    yield 1\n    raise KeyboardInterrupt\n\n"
     "def test_a(held):\n    assert False\n\ndef test_b(held):\n    assert held == 1\n", ()),
], ids=["keyboard-interrupt", "worker-restart-limit", "sigint-to-controller", "process-dies",
        "interrupt-in-session-teardown"])
def test_a_shard_that_did_not_run_its_tests_is_incomplete(tmp_path: Path, tests: str, args: tuple[str, ...]) -> None:
    root = _project(tmp_path, tests)
    _run(root, tmp_path / "runner-temp", *args)
    record = _record(tmp_path / "runner-temp")
    assert record["complete"] is False
    if "session" in tests:
        assert record["exitstatus"] == 2 and record["reported"] == record["collected"] == 2


GREEN = "def test_ok():\n    assert True\n"


def _assert_unchanged(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "INTERNALERROR" not in result.stdout + result.stderr
    assert (result.stderr + result.stdout).count("failure_record:") == 1


def test_runner_temp_that_is_a_file_changes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, GREEN)
    (tmp_path / "rt").write_text("not a directory")
    _assert_unchanged(_run(root, tmp_path / "rt"))


def test_an_unwritable_runner_temp_changes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path, GREEN)
    (tmp_path / "rt").mkdir()
    os.chmod(tmp_path / "rt", 0o500)
    try:
        _assert_unchanged(_run(root, tmp_path / "rt"))
    finally:
        os.chmod(tmp_path / "rt", 0o700)


def test_a_record_that_cannot_be_finished_changes_nothing(tmp_path: Path) -> None:
    # The directory turns unwritable mid-session (a full disk, say): the
    # session stays green, and the other plugins' session-end hooks still run
    # (pytest-split stores its durations).
    tests = ("import os\n\ndef test_ok():\n"
             "    os.chmod(os.path.join(os.environ['RUNNER_TEMP'], 'test-failures'), 0o500)\n")
    root = _project(tmp_path, tests)
    durations = tmp_path / "durations.json"
    try:
        result = _run(root, tmp_path / "rt", "--store-durations", f"--durations-path={durations}")
    finally:
        os.chmod(tmp_path / "rt/test-failures", 0o700)
    _assert_unchanged(result)
    assert "tests/test_sample.py::test_ok" in json.loads(durations.read_text())
    assert _record(tmp_path / "rt")["complete"] is False  # the start's record: unavailable, not zero


def test_outside_github_actions_nothing_is_recorded(tmp_path: Path) -> None:
    root = _project(tmp_path, GREEN)
    result = _run(root, None)
    assert result.returncode == 0
    assert not list(tmp_path.rglob("failures.json"))
