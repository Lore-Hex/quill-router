"""Native child-process crash and exact-key same-writer recovery probe.

Recovery reads original rows; it never synthesizes a missing acknowledgement.
"""
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.spanner_v1 import Client
    from shadow_observation_fixtures import NOW_US, event_for

    from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND, CLOSURE_KIND, Journal
    from trusted_router.async_settle_shadow_v2_a1_evidence import validate_close_result
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore
    project, instance, database_id, boot, point = sys.argv[1:]
    client = Client(project=project, credentials=AnonymousCredentials(), disable_builtin_metrics=True)
    database = client.instance(instance).database(database_id)
    store = ObservationStore(database, wall=lambda: NOW_US/1e6)
    key = "2026-10-12/"+boot
    if point == "recover":
        closure = store.read(CLOSURE_KIND, key, Budget(time.monotonic()+4))
        result = store.read(CLOSE_RESULT_KIND, key, Budget(time.monotonic()+4))
        if result is not None:
            assert closure is not None
            validate_close_result(result.value(), closure.value(), termination_applies=False)
        print(json.dumps(dict(missing_close=result is None, durable=result is not None,
                              closure=closure is not None)), flush=True)
        return
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)
    svc.journal = Journal(boot)
    svc.counters.clear()
    svc.legacy.days.clear()
    svc.ensure_day(NOW_US)
    svc.journal.grant("2026-10-12", 100)
    for _ in range(4):
        reservation = svc.port.reserve(NOW_US)
        assert reservation is not None
        assert svc.port.finalize(reservation, event_for(reservation))
        assert svc.port.complete(reservation)
        svc.journal.freeze(reservation.day)
    assert len(svc.journal.pages) == 4
    def boundary():
        print("BOUNDARY", flush=True)
        while True:
            signal.pause()
    if point == "before_first_flush":
        boundary()
    write = store.write
    selected = [False]
    def observe(tx, rows, budget):
        selected[0] = any(r.kind == CLOSURE_KIND for r in rows)
        return write(tx, rows, budget)
    store.write = observe
    api = store.database.spanner_api
    commit = api.commit
    def original_commit(*args, **kwargs):
        if selected[0] and point == "during_commit":
            boundary()
        result = commit(*args, **kwargs)
        if selected[0] and point == "before_ack":
            boundary()
        return result
    api.commit = original_commit
    flush = store.flush
    def original_flush(pages, counter, closure, budget):
        flush(pages, counter, closure, budget)
        if closure is not None and point == "after_ack":
            boundary()
    store.flush = original_flush
    handoff = store.close_result
    def result_write(row, budget):
        if point == "before_result":
            boundary()
        handoff(row, budget)
        if point == "after_result":
            boundary()
    store.close_result = result_write
    svc.closing_reason = "shutdown"
    svc.termination_deadline = time.monotonic()+9
    svc.flush(time.monotonic()+4)
    raise AssertionError("crash boundary was not reached")


if __name__ == "__main__":
    main()
