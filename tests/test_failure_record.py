"""scripts/ci/failure_record.py on a small synthetic project, run as ci.yml's
test shards run it: in GitHub Actions (GITHUB_ACTIONS, RUNNER_TEMP), under
xdist, loaded the way tests/conftest.py loads it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CONFTEST = '''
import os

if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_TEMP"):
    pytest_plugins = ["scripts.ci.failure_record"]
'''

TESTS = '''
import subprocess
import sys

import pytest


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


def test_runs_pytest_in_process(tmp_path):
    # As tests/test_lock_order_guard.py does: an inner session whose failure
    # is that test's business, not a failure of this run.
    (tmp_path / "conftest.py").write_text(open(__file__.replace("test_sample.py", "conftest.py")).read())
    (tmp_path / "test_inner.py").write_text("def test_inner():\\n    assert False\\n")
    assert pytest.main([str(tmp_path / "test_inner.py"), "-q", "-p", "no:cacheprovider",
                        "--confcutdir=" + str(tmp_path), "-o", "addopts="]) == 1


def test_runs_pytest_in_a_subprocess(tmp_path):
    (tmp_path / "test_child.py").write_text("def test_child():\\n    assert False\\n")
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                          capture_output=True, text=True)
    assert done.returncode == 1
'''


def _run(root: Path, temp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")}
    env = {**base, "PYTHONPATH": str(REPO), "GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(temp)}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",  # noqa: S603 -- fixed argv
                           "-o", "addopts=", "--rootdir", str(root), "tests", *args],
                          cwd=root, env=env, capture_output=True, text=True, timeout=300)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests/conftest.py").write_text(CONFTEST)
    (root / "tests/test_sample.py").write_text(TESTS)
    (root / "tests/test_broken.py").write_text("import not_a_module_anywhere\n")
    return root


def test_a_shard_records_every_failure_by_its_exact_node_id(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = _run(root, tmp_path / "runner-temp", "-n", "2")
    assert result.returncode == 1, result.stdout + result.stderr
    record = json.loads((tmp_path / "runner-temp/test-failures/failures.json").read_text())
    assert record["complete"] is True and record["exitstatus"] == 1
    # Exact IDs, spaces included; a setup error counts; an expected failure
    # does not; a pytest a test ran (in-process or as a child) is not this run.
    assert record["failed"] == ["tests/test_sample.py::test_fails", "tests/test_sample.py::test_param[a b]",
                                "tests/test_sample.py::test_setup_error"]
    assert record["collection_errors"] == ["tests/test_broken.py"]


def test_a_shard_that_stops_early_leaves_an_incomplete_record(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "tests/test_sample.py").write_text("import os\n\n\ndef test_dies():\n    os._exit(3)\n")
    (root / "tests/test_broken.py").unlink()
    result = _run(root, tmp_path / "runner-temp")
    assert result.returncode == 3
    record = json.loads((tmp_path / "runner-temp/test-failures/failures.json").read_text())
    assert record["complete"] is False and record["failed"] == []


def test_outside_github_actions_nothing_is_recorded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")}
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",  # noqa: S603 -- fixed argv
                    "--rootdir", str(root), "tests"], cwd=root, capture_output=True, text=True, timeout=300,
                   env={**base, "PYTHONPATH": str(REPO), "RUNNER_TEMP": str(tmp_path / "runner-temp")})
    assert not (tmp_path / "runner-temp").exists()
