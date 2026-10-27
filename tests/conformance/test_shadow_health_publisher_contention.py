"""Real SDK bounds and eight independent publisher processes on native Spanner.

Selected by the ordinary conformance emulator job; no production credentials or
resources. The parent's shared counter measures overlapping claim calls, while
SDK transaction histories expose ABORTED/re-entry instead of masking retries.
"""
from __future__ import annotations

import multiprocessing
import os
import time
from collections import Counter
from contextlib import contextmanager

import pytest

from tests.conformance.spanner_emulator import emulator_resources, emulator_sdk_shim
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401

pytestmark = [pytest.mark.xdist_group("conformance-spanner-emulator"), pytest.mark.timeout(180)]


@pytest.fixture
def publisher_database():
    with emulator_resources() as resources:
        try:
            yield resources
        finally:
            resources[0].drop()


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_min_read_timestamp_after_nanosecond_claim(publisher_database, backend):
    from trusted_router.storage_gcp_shadow_io import timestamp_ns
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

    database, _ = publisher_database
    store = ObservationStore(database)
    assert store.prepare_session("bound-probe")
    time.sleep(2-time.time() % 2+.005)
    slot = int(time.time())//2
    budget = Budget(time.monotonic()+.5)
    assert store.claim("bound-probe", slot, slot*2_000_000, budget)
    claim = store.last_claim
    assert claim is not None
    bounds = []
    snapshot = store.database.snapshot
    @contextmanager
    def capture(**kwargs):
        if "min_read_timestamp" in kwargs:
            bounds.append(timestamp_ns(kwargs["min_read_timestamp"]))
        # The shim preserves min_read_timestamp; a real RPC must accept it.
        with snapshot(**kwargs) as reader:
            yield reader
    store.database.snapshot = capture
    value = store.observe("bound-probe", "us-central1", "a"*40, slot, budget,
                          claim_at_ns=claim["claim_at_ns"])
    assert bounds == [((claim["claim_at_ns"]+999)//1000)*1000]
    assert value["observed_at_ns"] >= bounds[0] >= claim["claim_at_ns"]
    assert store.publish(value, budget)
    print("NATIVE_MIN_READ_TIMESTAMP", dict(claim=claim["claim_at_ns"], bound=bounds[0],
                                           observed=value["observed_at_ns"]))


def _publisher_process(instance_id, index, commands, results, active, peak):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner

    from tests.shadow_observation_diagnostics import client_history
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    from trusted_router.storage_gcp_shadow_observation import ObservationStore

    svc = None
    try:
        with emulator_sdk_shim():
            client = spanner.Client(project="tr-conformance", credentials=AnonymousCredentials(),
                                    disable_builtin_metrics=True)
            database = client.instance(instance_id).database("conformance")
            store = ObservationStore(database)
            boot = f"contention-boot-{index}"
            assert store.prepare_session(boot)
            settings = Settings(environment="test", async_settle_shadow_policy="shadow-v2",
                                async_settle_shadow_workspaces="ws", source_revision="a"*40)
            settings.async_settle_shadow_health_region = settings.effective_serving_region
            svc = Service(settings, store)
            svc.journal.boot = boot
            traces, claims = [], []
            svc.traces.append = lambda day, trace: traces.append(dict(trace))
            claim = store.claim
            def counted(owner, slot, stamp, budget):
                claims.append(slot)
                with active.get_lock():
                    active.value += 1
                    peak.value = max(peak.value, active.value)
                try:
                    return claim(owner, slot, stamp, budget)
                finally:
                    with active.get_lock():
                        active.value -= 1
            store.claim = counted
            results.put(("ready", index, os.getpid()))
            first, health = commands.get(timeout=30)
            svc.observer.health = {"body": health}
            observe = store.observe
            def sometimes_fail(*args, **kwargs):
                if index == 0 and args[3] in (first+1, first+2):
                    raise TimeoutError("two forced incumbent observation failures")
                return observe(*args, **kwargs)
            store.observe = sometimes_fail
            with client_history(store) as history:
                for offset in range(5):
                    slot = first+offset
                    time.sleep(max(0, slot*2+.005-time.time()))
                    svc.publish_cycle(slot, int(time.time()*1e6))
                    # Observer install off the claim critical path, after all
                    # publishers' 500ms budgets, exactly as a separate reader.
                    time.sleep(max(0, slot*2+.8-time.time()))
                    health_read = store.read_shadow_health()
                    if health_read is not None:
                        svc.observer.health = health_read
                errors = [h for h in history if h.get("statement") == "transaction_error"]
            results.put(("done", index, dict(pid=os.getpid(), claims=claims, traces=traces,
                                             transaction_errors=errors)))
    except BaseException as error:
        results.put(("error", index, repr(error)))
        raise
    finally:
        if svc is not None:
            svc.publisher_executor.shutdown(wait=True, cancel_futures=True)
            svc.evidence_executor.shutdown(wait=True, cancel_futures=True)
            svc.session_executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_eight_processes_two_failed_cycles_have_one_claim_writer(publisher_database, backend):
    import datetime as dt

    from trusted_router.async_settle_shadow_health_state import summarize
    from trusted_router.async_settle_shadow_journal import CLAIM_KIND, HEALTH_KIND, Row
    from trusted_router.async_settle_shadow_v2_schema import POLICY

    database, instance = publisher_database
    ctx = multiprocessing.get_context("spawn")  # Never fork live gRPC threads.
    results = ctx.Queue()
    active, peak = ctx.Value("i", 0), ctx.Value("i", 0)
    commands = [ctx.Queue() for _ in range(8)]
    children = [ctx.Process(target=_publisher_process,
                           args=(instance, i, commands[i], results, active, peak)) for i in range(8)]
    try:
        for child in children:
            child.start()
        ready = [results.get(timeout=40) for _ in children]
        assert all(message[0] == "ready" for message in ready), ready
        assert len({message[2] for message in ready}) == 8
        first = int(time.time())//2+3
        prior = first-1
        # Seed a warm holder so this case isolates renewal, not cold election.
        health = summarize([], boot="contention-boot-0", region="us-central1", revision="a"*40,
                           slot=prior, observed_ns=prior*2_000_000_000,
                           age_seconds=prior*2., row_limit=10000)
        claim = dict(v=2, policy_version=POLICY, contract_revision="a1", authority="local",
                     slot=prior, claim_at_ns=prior*2_000_000_000, owner_boot_id="contention-boot-0")
        with database.batch() as batch:
            batch.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                values=[(r.kind, r.key, r.body.decode(), dt.datetime.now(dt.UTC)) for r in (
                    Row.build(CLAIM_KIND, "local/publish", claim), Row.build(HEALTH_KIND, "local/fleet", health))])
        for command in commands:
            command.put((first, health))
        replies = [results.get(timeout=25) for _ in children]
        assert all(message[0] == "done" for message in replies), replies
        data = [message[2] for message in replies]
        counts = Counter(slot for d in data for slot in d["claims"])
        assert counts == dict.fromkeys(range(first, first+5), 1), data
        assert peak.value == 1 and active.value == 0, data
        assert not any(d["transaction_errors"] for d in data), data
        traces = [t for d in data for t in d["traces"]]
        assert len(traces) == 40, data
        published = Counter(t["slot"] for t in traces if t["outcome"] == "published")
        assert published == {first: 1, first+3: 1, first+4: 1}, data
        failures = [t for t in traces if t["outcome"] == "failed"]
        assert len(failures) == 2 and all(t["boot"] == "contention-boot-0" for t in failures), data
        print("NATIVE_PUBLISHER_CONTENTION", dict(processes=8, claims=dict(counts),
              max_concurrent_claims=peak.value, transaction_errors=0, publications=dict(published)))
        for child in children:
            child.join(timeout=5)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.pid is not None:
                if child.is_alive():
                    child.terminate()
                child.join(timeout=5)
        for queue in [results, *commands]:
            queue.close()
