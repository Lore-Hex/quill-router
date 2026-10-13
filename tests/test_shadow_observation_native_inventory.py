"""Checked-in node inventory: native qualification cannot disappear by deletion."""
import ast
import itertools
import json
from pathlib import Path

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401


def test_owned_native_node_inventory():
    root = Path(__file__).parents[1]
    expected = json.loads((root/"tests/shadow_observation_native_nodes.json").read_text())
    collected = set()
    for filename in ("tests/conformance/test_shadow_observation_native.py", "tests/test_shadow_attachment_deadline.py"):
        for node in ast.parse((root/filename).read_text()).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            parameters = []
            for decorator in reversed(node.decorator_list):
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "parametrize":
                    parameters.append(ast.literal_eval(decorator.args[1]))
            for values in itertools.product(*parameters):
                suffix = "["+"-".join(str(v) for v in values)+"]" if parameters else ""
                collected.add(filename+"::"+node.name+suffix)
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
