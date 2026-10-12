"""P3 native serialization and slot races, selected by CI's emulator command."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from tests.test_async_settle_shadow_observation import NOW_US, maximum_rows
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


@pytest.fixture(scope="module")
def shadow_emulator_resources():
    # Reuse the existing emulator/schema fixture in an isolated instance so the
    # empty snapshot witness cannot inherit outbox rows from adjacent suites.
    from tests.conformance.spanner_emulator import emulator_resources
    with emulator_resources() as resources:
        yield resources


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_six_rows(shadow_emulator_resources: Any, backend: str) -> None:
    database, _ = shadow_emulator_resources
    store = ObservationStore(database, wall=lambda: NOW_US/1e6)
    pages, counter, closure = maximum_rows(uuid4().hex*2)
    store.flush(pages, counter, closure, Budget(time.monotonic()+4))
    assert store.reconcile([*pages, counter, closure], Budget(time.monotonic()+4))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_slot_race(shadow_emulator_resources: Any, backend: str) -> None:
    database, _ = shadow_emulator_resources
    store = ObservationStore(database)
    other = ObservationStore(database)
    # Warm the independent lanes before aligning the race. The cold-session
    # deadline path is covered by the native fault/checkout harness.
    store.read_shadow_health()
    other.read_shadow_health()
    time.sleep(2-time.time()%2+.005)
    now_us = int(time.time()*1e6)
    slot = now_us//2_000_000
    def contender(boot: str, contender_store: ObservationStore) -> dict[str, Any] | None:
        budget = Budget(time.monotonic()+.5)
        try:
            if not contender_store.claim(boot, slot, now_us, budget):
                return None
            value = contender_store.observe(boot, "us-central1", "a"*40, slot, budget)
            assert contender_store.publish(value, budget)
            return value
        except Exception:
            return None
    # The winner observes immediately, without waiting for the loser's timeout.
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(contender, "a", store)
        b = pool.submit(contender, "b", other)
        winners = [value for value in (a.result(), b.result()) if value is not None]
    assert len(winners) == 1
    value = winners[0]
    read = store.read_shadow_health()
    assert read is not None and read["body"] == value
    assert value["observed_at_ns"] <= read["committed_at_ns"] <= read["read_at_ns"]
    with pytest.raises(ValueError, match="a1-server-slot"):
        store.claim("future", slot+100, now_us+200_000_000, Budget(time.monotonic()+.5))
    assert not store.publish(value, Budget(time.monotonic()+.5))  # Same slot cannot overwrite.


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_checkpoint_fences(shadow_emulator_resources: Any, backend: str) -> None:
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = shadow_emulator_resources
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
def test_shadow_observation_native_reference_readback(shadow_emulator_resources: Any, backend: str) -> None:
    from tests.test_async_settle_shadow_observation import event_for
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = shadow_emulator_resources
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database))
    svc.maintain()
    r = svc.port.reserve(int(time.time()*1e6))
    assert r is not None
    assert svc.port.finalize(r, event_for(r))
    assert svc.port.complete(r)
    svc.flush(time.monotonic()+4)
    assert svc.journal.days[r.day].hwm == 1


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_empty_nonempty_metadata(shadow_emulator_resources: Any, backend: str) -> None:
    import datetime as dt

    from google.cloud.spanner_v1 import KeySet
    database, _ = shadow_emulator_resources
    store = ObservationStore(database)
    slot = int(time.time())//2
    empty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
    assert empty["observed_at_ns"] > 0 and empty["backlog_count"] == 0
    identity = uuid4().hex
    with database.batch() as batch:
        batch.insert(table="tr_settle_outbox", columns=("authorization_id", "intent_kind", "settle_origin", "actual_cost_micro", "status", "created_at"),
            values=[(identity, "settle", "typed", 1, "dead", dt.datetime.now(dt.UTC))])
    try:
        nonempty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
        assert nonempty["observed_at_ns"] > empty["observed_at_ns"]
        assert nonempty["backlog_count"] == empty["backlog_count"]+1
        assert nonempty["dead_count"] == empty["dead_count"]+1
    finally:
        with database.batch() as batch:
            batch.delete("tr_settle_outbox", KeySet(keys=[(identity, "settle")]))
