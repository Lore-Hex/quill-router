import pytest

from tests.shadow_v2_import_guard import validate_live_imports


@pytest.mark.parametrize("extra", [
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
