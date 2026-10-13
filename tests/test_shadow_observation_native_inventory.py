"""Checked-in node inventory: native qualification cannot disappear by deletion."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401

NATIVE_MODULES = ("tests/conformance/test_shadow_observation_native.py", "tests/test_shadow_attachment_deadline.py")


def collect_native(root):
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",  # noqa: S603
        "--no-cov", "-o", "addopts=", *NATIVE_MODULES], cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root)+os.pathsep+str(root/"src")),
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout+result.stderr
    return {line.split("@", 1)[0] for line in result.stdout.splitlines()
            if line.startswith(NATIVE_MODULES) and "::" in line}


@pytest.fixture(scope="session")
def native_collected(tmp_path_factory, worker_id):
    # One real collection for the entire session, including all xdist workers.
    shared = tmp_path_factory.getbasetemp()
    if worker_id != "master":
        shared = shared.parent
    cache = shared/"p3-native-collection.json"
    with FileLock(str(cache)+".lock", timeout=190):
        if not cache.exists():
            cache.write_text(json.dumps(sorted(collect_native(Path(__file__).resolve().parents[1]))))
        return set(json.loads(cache.read_text()))


def check_inventory(root, collected):
    expected = json.loads((root/"tests/shadow_observation_native_nodes.json").read_text())
    owned = {node for node in collected if node.startswith("tests/conformance/test_shadow_observation_native.py::")
             or "::test_http_absolute_deadline_keeps_unknown[" in node
             or "::test_authenticated_native_cancellation[" in node}
    assert owned == set(expected), "native collected-node mismatch: "+repr(owned ^ set(expected))
    assert all("spanner-emulator" in node for node in owned if node.startswith("tests/conformance/"))


def test_owned_native_node_inventory(native_collected):
    check_inventory(Path(__file__).resolve().parents[1], native_collected)


def test_hidden_native_case_fails_inventory(tmp_path, native_collected):
    root = Path(__file__).resolve().parents[1]
    check_inventory(root, native_collected)
    # Only the native source is changed; the scratch tree uses the same plugins,
    # configuration, helpers and conftests as the real collection.
    def overlay(source, target):
        target.mkdir()
        for item in source.iterdir():
            if item.name in {".git", ".venv", "__pycache__", ".pytest_cache"}:
                continue
            dest = target/item.name
            if item == root/"tests" or item == root/"tests/conformance":
                overlay(item, dest)
            elif item == root/NATIVE_MODULES[0]:
                dest.write_text(item.read_text()+"\ntest_shadow_observation_native_six_rows.__test__ = False\n")
            else:
                dest.symlink_to(item, target_is_directory=item.is_dir())
    scratch = tmp_path/"hidden"
    overlay(root, scratch)
    hidden = collect_native(scratch)
    assert not any("::test_shadow_observation_native_six_rows[" in node for node in hidden)
    with pytest.raises(AssertionError, match="native collected-node mismatch"):
        check_inventory(scratch, hidden)


def test_configured_emulator_skip_is_failure(monkeypatch):
    from contextlib import contextmanager

    import pytest

    from tests.conformance import spanner_emulator
    from tests.conformance.test_shadow_observation_native import shadow_emulator_resources
    @contextmanager
    def unavailable():
        pytest.skip("injected unavailable emulator")
        yield
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "127.0.0.1:9010")
    monkeypatch.setenv("TR_CONFORMANCE_EMULATOR_SCHEMA", "1")
    monkeypatch.setattr(spanner_emulator, "emulator_resources", unavailable)
    with pytest.raises(pytest.fail.Exception, match="configured native case skipped"):
        next(shadow_emulator_resources.__wrapped__(
            SimpleNamespace(node=SimpleNamespace(name="process_restart")),
            None, (None, []), None))


def test_ci_loopback_skip_is_failure(monkeypatch, tmp_path):
    import pytest

    from tests import test_shadow_attachment_deadline as native
    def forbidden(*args):
        raise PermissionError("injected loopback denial")
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(native, "ThreadingHTTPServer", forbidden)
    with pytest.raises(pytest.fail.Exception, match="CI must permit"):
        native.test_http_absolute_deadline_keeps_unknown("headers", tmp_path)
    with pytest.raises(pytest.fail.Exception, match="CI must permit"):
        native.test_authenticated_native_cancellation("credential", monkeypatch)
