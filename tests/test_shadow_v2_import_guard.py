import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.shadow_v2_import_guard import validate_live_imports


@pytest.mark.parametrize("extra", [
    'import builtins\ngetattr(builtins, "__import__")("x")\n',
    'import builtins as b\ngetattr(b, "__import__")("x")\n',
    'getattr(__builtins__, "__import__")("x")\n',
    '__builtins__["__import__"]("x")\n',
    'import sys as s\ns.modules["importlib"].import_module("x")\n',
    'import sys as s\ngetattr(s, "modules")["builtins"]\n',
    'getattr(receiver, unresolved)("x")\n',
    'from builtins import getattr as hidden\n',
    'from sys import modules as loaded\n',
    'import sys\nread = getattr\nread(sys, "modules")\n',
    'import importlib as lib\nlib.import_module("x")\n',
    'exec("pass")\n',
    'eval("1")\n',
    "from builtins import __import__ as hidden\nhidden(\"x\")\n",
    "load = __import__\nload(\"x\")\n",
    "from importlib import import_module as hidden\nhidden(\"x\")\n",
    "builtins.__import__(\"x\")\n",
    "from trusted_router.async_settle_shadow_v2_a1_health import validate_publication\n",
    "from trusted_router.async_settle_shadow_v2_evidence import validate_completion\n",
    "importlib.import_module('trusted_router.async_settle_shadow_v2_evidence')\n",
    "importlib.import_module('trusted_router.async_settle_shadow_v2_a1_health')\n",
    "from trusted_router.async_settle_shadow_v2_a1_health import HEALTH as hidden\n",
    "from trusted_router import async_settle_shadow_v2_a1_health as hidden\n",
])
def test_exact_import_guard_negative_controls(extra):
    source = "from trusted_router.async_settle_shadow_v2_a1_health import HEALTH\n"
    allowed = {"trusted_router.async_settle_shadow_v2_a1_health": ["HEALTH"]}
    names = {"async_settle_shadow_v2_a1_health", "async_settle_shadow_v2_evidence"}
    validate_live_imports(source, allowed, names)
    with pytest.raises(AssertionError):
        validate_live_imports(extra+source, allowed, names)


def test_review_getattr_mutant_rejected_by_actual_dormancy_guard(monkeypatch):
    from pathlib import Path

    from tests.test_async_settle_shadow_v2_evidence import (
        test_no_runtime_imports_or_calls_to_new_modules,
    )
    test_no_runtime_imports_or_calls_to_new_modules()
    source = Path("src/trusted_router/storage_gcp_shadow_attachments.py").resolve()
    mutant = source.read_text() + '''
def _review_dynamic_load(module_name):
    import builtins
    return getattr(builtins, "__import__")(module_name, fromlist=["*"])
'''
    compile(mutant, str(source), "exec")
    read = Path.read_text
    def overlay(path, *args, **kwargs):
        return mutant if path.resolve() == source else read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", overlay)
    with pytest.raises(AssertionError):
        test_no_runtime_imports_or_calls_to_new_modules()


@pytest.mark.parametrize("loader", ["importlib", "importlib.util", "pkgutil", "runpy", "zipimport", "imp"])
@pytest.mark.parametrize("spelling", ["import {loader}", "import {loader} as hidden", "from {loader} import resolve_name", "from {loader} import resolve_name as hidden"])
def test_closed_loader_import_inventory(loader, spelling):
    prefix = "from trusted_router.async_settle_shadow_v2_a1_health import HEALTH\n"
    allowed = {"trusted_router.async_settle_shadow_v2_a1_health": ["HEALTH"]}
    source = prefix + spelling.format(loader=loader) + "\n"
    compile(source, "loader-control", "exec")
    validate_live_imports(prefix, allowed, {"async_settle_shadow_v2_a1_health"})
    with pytest.raises(AssertionError):
        validate_live_imports(source, allowed, {"async_settle_shadow_v2_a1_health"})


@pytest.mark.parametrize("extra", ["compile('1', 'x', 'eval')", "from builtins import compile as hidden",
    "import sys as s; s.modules", "import sys as s; s.__dict__['modules']", "import sys as s; vars(s)['modules']"])
def test_closed_loader_access_inventory(extra):
    prefix = "from trusted_router.async_settle_shadow_v2_a1_health import HEALTH\n"
    allowed = {"trusted_router.async_settle_shadow_v2_a1_health": ["HEALTH"]}
    compile(prefix+extra, "loader-control", "exec")
    with pytest.raises(AssertionError):
        validate_live_imports(prefix+extra, allowed, {"async_settle_shadow_v2_a1_health"})
