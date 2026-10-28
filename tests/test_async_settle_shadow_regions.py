"""Production-shaped region scope: installations, request gates and dormant defaults."""
from __future__ import annotations

import ast
import datetime as dt
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from trusted_router.config import Settings

REGIONS = "us-central1,us-east4,europe-west4"
WORKSPACE = "162e441a-5d13-414f-92bd-7c6cd805faa3"


def pins(**changes):
    values = dict(
        environment="test", service_surface="internal", source_revision="a" * 40,
        async_settle_shadow_policy="shadow-v2", async_settle_shadow_workspaces=WORKSPACE,
        async_settle_shadow_health_region="us-central1",
        async_settle_shadow_attachment_bucket="tr-async-settle-shadow-attachments-quill-cloud-proxy",
        async_settle_shadow_expires_at=dt.datetime.fromtimestamp(time.time() + 600, dt.UTC).isoformat(),
        async_settle_enabled=False, async_settle_protection=False,
        settle_outbox_fast_drain_enabled=False,
    )
    return Settings(**(values | changes))


@pytest.mark.parametrize("raw,expected", [
    ("", frozenset()), (" , , ", frozenset()),
    (REGIONS, frozenset(REGIONS.split(","))),
    (" us-central1,us-east4,us-central1, ", frozenset({"us-central1", "us-east4"})),
    ("a" * 32, frozenset({"a" * 32})),
])
def test_parse_regions_like_fastpath(monkeypatch, raw, expected):
    monkeypatch.setenv("TR_ASYNC_SETTLE_SHADOW_REGIONS", raw)
    cfg = Settings(environment="test", fastpath_pilot_regions=raw)
    assert cfg.async_settle_shadow_region_ids == expected == cfg.fastpath_pilot_region_ids


@pytest.mark.parametrize("raw", ["US-central1", "us_central1", "us-central1/a", "us central1", "a" * 33, "é"])
def test_invalid_region_ids(raw):
    with pytest.raises(ValueError, match="invalid async settle shadow region set"):
        pins(async_settle_shadow_regions=raw)


def test_health_publisher_must_be_in_explicit_set():
    with pytest.raises(ValueError, match="HEALTH_REGION must be in TR_ASYNC_SETTLE_SHADOW_REGIONS.*publisher.*excluded"):
        pins(async_settle_shadow_regions="us-east4")
    assert pins(async_settle_shadow_regions="us-east4", async_settle_shadow_health_region="").async_settle_shadow_region_ids
    assert pins(async_settle_shadow_regions="").async_settle_shadow_live()


@pytest.mark.parametrize("region,allowed", [("us-central1", True), ("us-east4", True),
    ("europe-west4", True), ("southamerica-east1", False), ("", False)])
def test_scope_uses_own_serving_pin_not_primary_or_request_region(region, allowed):
    cfg = pins(async_settle_shadow_regions=REGIONS, serving_region=region, primary_region="us-central1")
    assert cfg.async_settle_shadow_region_allowed() is allowed
    assert cfg.async_settle_shadow_live() is allowed
    assert cfg.effective_async_settle_shadow_policy() == ("shadow-v2" if allowed else "")
    assert cfg.async_settle_shadow_policy == "shadow-v2"
    assert not cfg.async_settle_admission_enabled
    assert not cfg.async_settle_protection


@pytest.mark.parametrize("region", ["us-central1", "us-east4", "europe-west4", "southamerica-east1", ""])
def test_empty_set_keeps_liveness_and_expiry(region):
    cfg = pins(serving_region=region)
    assert cfg.async_settle_shadow_region_allowed() and cfg.async_settle_shadow_live()
    assert cfg.effective_async_settle_shadow_policy() == "shadow-v2"
    assert cfg.regional_async_settle_shadow_policy() == "shadow-v2"
    assert not cfg.async_settle_shadow_live(time.time() + 601)
    cfg.stop_async_settle_shadow()
    assert not cfg.async_settle_shadow_live()
    # Outer hooks historically use raw pins; expiry/stop remains in the inner
    # runtime. Preserve that ordering when the regional setting is dormant.
    assert cfg.regional_async_settle_shadow_policy() == "shadow-v2"


@pytest.mark.parametrize("regions,region,active", [
    (REGIONS, "us-central1", True), (REGIONS, "us-east4", True),
    (REGIONS, "europe-west4", True), (REGIONS, "southamerica-east1", False),
    (REGIONS, "", False), ("", "southamerica-east1", True), ("", "", True),
])
def test_create_app_real_install_inventory(monkeypatch, regions, region, active):
    from tests.test_async_settle_shadow_observation import fake_store
    from tests.test_async_settle_ticket import runtime
    from trusted_router import main, storage
    from trusted_router.services import (
        async_settle,
        async_settle_shadow,
    )
    from trusted_router.services import (
        async_settle_shadow_cohort_runtime as cohort,
    )
    from trusted_router.services import (
        async_settle_shadow_health as health,
    )
    from trusted_router.services import (
        async_settle_shadow_startup_harness as startup,
    )

    # Real installers/Service/Runtime/RuntimeStop, with only external storage
    # and the unrelated admission runtime replaced. Never start cloud IO.
    store, database = fake_store()
    observation = Mock(return_value=store)
    evidence = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(health, "ObservationStore", observation)
    monkeypatch.setattr(cohort, "EvidenceStore", evidence)
    backend = SimpleNamespace(_database=database, request_record_write_mode="typed")
    monkeypatch.setattr(storage, "typed_billing_store", lambda _: backend)
    admission = runtime()
    monkeypatch.setattr(async_settle, "load_runtime", Mock(return_value=admission))
    v1 = Mock(side_effect=AssertionError("v2 must never fall back to v1"))
    monkeypatch.setattr(async_settle_shadow, "install", v1)
    # Even a provisioned manifest must not create a harness on excluded boots.
    monkeypatch.setenv(startup.MANIFEST_ENV, "/must-not-read/regional-manifest.json")
    cfg = pins(async_settle_shadow_regions=regions, serving_region=region)
    cfg = cfg.model_copy(update={"environment": "production"})
    app = main.create_app(cfg, configure_store_arg=False, init_observability=False)
    worker = getattr(app.state, "async_settle_shadow_cohort", None)
    service = getattr(app.state, "async_settle_shadow_observation", None)
    try:
        inventory = sorted(name for name in vars(app.state)["_state"] if name.startswith("async_settle_shadow"))
        # Inventory recorded on main ddfbd1e62 with these production pins.
        assert inventory == (["async_settle_shadow_cohort", "async_settle_shadow_observation",
                              "async_settle_shadow_startup", "async_settle_shadow_startup_begin"] if active else [])
        handlers = [getattr(f, "__qualname__", "") for f in app.router.on_startup + app.router.on_shutdown
                    if "shadow" in f.__module__]
        assert handlers == (["install.<locals>.start", "Runtime.start", "install.<locals>.stop", "Runtime.close"] if active else [])
        assert app.state.async_settle is admission
        v1.assert_not_called()
        assert observation.call_count == evidence.call_count == int(active)
        if active:
            assert service.runtime_stop is not None
            assert service.publisher_eligible() is (region in {"us-central1", ""})
            assert worker.startup_admission is app.state.async_settle_shadow_startup.admission
        else:
            # Neither the listener nor the direct install entry points may act.
            startup.listener_started(app)
            health.install(app, cfg, backend)
            cohort.install(app, cfg, backend)
            main._v2_install_hook(app, cfg, backend, WORKSPACE)
            observation.assert_not_called()
            evidence.assert_not_called()
            assert not hasattr(app.state, "async_settle_shadow_startup")
            direct = FastAPI()
            direct.state.async_settle_shadow_observation = object()
            cohort.install(direct, cfg, backend)
            startup.install(direct, cfg)
            assert not hasattr(direct.state, "async_settle_shadow_cohort")
            assert not hasattr(direct.state, "async_settle_shadow_startup")
    finally:
        if worker is not None:
            worker.close()
            worker.worker.join(2)
        if service is not None:
            for executor in (service.session_executor, service.publisher_executor, service.evidence_executor):
                executor.shutdown(wait=True, cancel_futures=True)


def test_non_v2_startup_and_real_admission_are_unchanged():
    from trusted_router.services import async_settle_shadow_startup_harness as startup

    cfg = pins(async_settle_shadow_regions=REGIONS, serving_region="southamerica-east1",
               async_settle_shadow_policy="", async_settle_enabled=True, async_settle_protection=True)
    app = FastAPI()
    startup.install(app, cfg)
    assert hasattr(app.state, "async_settle_shadow_startup")
    assert cfg.async_settle_admission_enabled and cfg.async_settle_protection


def test_every_production_cohort_call_uses_region_filtered_policy():
    root = Path(__file__).resolve().parents[1] / "src/trusted_router"
    calls = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "in_v2_cohort":
                calls.append((path, node.lineno))
                assert ast.unparse(node.args[0]) in {
                    "settings.effective_async_settle_shadow_policy()",
                    "settings.regional_async_settle_shadow_policy()",
                }, (path, node.lineno)
    assert len(calls) == 9
