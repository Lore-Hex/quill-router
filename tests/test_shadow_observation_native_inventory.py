"""Checked-in node inventory: native qualification cannot disappear by deletion."""
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401


def test_owned_native_node_inventory():
    root = Path(__file__).parents[1]
    expected = json.loads((root/"tests/shadow_observation_native_nodes.json").read_text())
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--noconftest", "--no-cov",
        "-n", "0", "-o", "addopts=", "tests/conformance/test_shadow_observation_native.py",
        "tests/test_shadow_attachment_deadline.py"]
    result = subprocess.run(command, cwd=root, env={**os.environ, "PYTHONPATH": str(root/"src")},  # noqa: S603
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr+result.stdout
    collected = {line.strip() for line in result.stdout.splitlines() if "::test_" in line}
    owned = {node for node in collected if node.startswith("tests/conformance/test_shadow_observation_native.py::")
             or "::test_http_absolute_deadline_keeps_unknown[" in node
             or "::test_authenticated_native_cancellation[" in node}
    assert owned == set(expected)
    assert all("spanner-emulator" in node for node in owned if node.startswith("tests/conformance/"))


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
        next(shadow_emulator_resources.__wrapped__())


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
