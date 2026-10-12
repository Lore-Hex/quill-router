"""P3 native serialization and slot races, selected by CI's emulator command."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from tests.test_async_settle_shadow_observation import NOW_US, health, maximum_rows
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_six_rows(native_emulator_resources: Any, backend: str) -> None:
    database, _ = native_emulator_resources
    store = ObservationStore(database, wall=lambda: NOW_US/1e6)
    pages, counter, closure = maximum_rows(uuid4().hex*2)
    store.flush(pages, counter, closure, Budget(time.monotonic()+4))
    assert store.reconcile([*pages, counter, closure], Budget(time.monotonic()+4))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_slot_race(native_emulator_resources: Any, backend: str) -> None:
    database, _ = native_emulator_resources
    store = ObservationStore(database)
    now_us = int(time.time()*1e6)
    slot = now_us//2_000_000
    def contender(boot: str) -> bool:
        try:
            return store.claim(boot, slot, now_us, Budget(time.monotonic()+.5))
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(contender, ("a", "b")))
    assert sum(winners) == 1
    value = health(now_us=now_us)
    value["publisher_boot_id"] = "a" if winners[0] else "b"
    assert store.publish(value, Budget(time.monotonic()+.5))
    assert store.claim("new", slot+1, now_us+2_000_000, Budget(time.monotonic()+.5))
    assert not store.publish(value, Budget(time.monotonic()+.5))
    assert store.read_shadow_health() == value


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_checkpoint_fences(native_emulator_resources: Any, backend: str) -> None:
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = native_emulator_resources
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database))
    day = next(iter(svc.journal.days))
    svc.journal.checkpoint(day)
    older = svc.counter_row(day, int(time.time()*1e6), closed=False)
    svc.journal.checkpoint(day)
    newer = svc.counter_row(day, int(time.time()*1e6), closed=False)
    svc.store.flush([], newer, None, Budget(time.monotonic()+4))
    with pytest.raises(ValueError, match="checkpoint_regression"):
        svc.store.flush([], older, None, Budget(time.monotonic()+4))
    assert svc.store.reconcile([newer], Budget(time.monotonic()+4))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_reference_readback(native_emulator_resources: Any, backend: str) -> None:
    from tests.test_async_settle_shadow_observation import event_for
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = native_emulator_resources
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database))
    svc.maintain()
    r = svc.port.reserve(int(time.time()*1e6))
    assert r is not None
    assert svc.port.finalize(r, event_for(r))
    assert svc.port.complete(r)
    svc.flush(time.monotonic()+4)
    assert svc.journal.days[r.day].hwm == 1
