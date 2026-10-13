"""Native process boundaries. Child imports only standalone helpers, never test modules."""
import asyncio
import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shadow_observation_diagnostics import (  # noqa: E402
    announce_boundary,
    client_history,
    hold_boundary,
    status,
    track_sessions,
)
from shadow_observation_fixtures import (  # noqa: E402
    NOW_US,
    event_for,
    observation_service,
    saturate,
)


def transport_boundary(store, boundary, landed_commit):
    """Real gRPC request/response, held inside transport before SDK sees success."""
    import grpc
    from google.cloud.spanner_v1.services.spanner.transports.grpc import SpannerGrpcTransport
    from google.cloud.spanner_v1.types import CommitRequest

    from trusted_router.async_settle_shadow_journal import CLOSURE_KIND
    class Withhold(grpc.UnaryUnaryClientInterceptor):
        def intercept_unary_unary(self, continuation, details, request):
            response = continuation(details, request)  # Sends bytes to the emulator.
            if details.method.endswith("/Commit"):
                request = CommitRequest(request)
                kinds = [row[0] for mutation in request.mutations
                         for row in mutation.insert_or_update.values]
                if CLOSURE_KIND in kinds:
                    response.result()  # Require native success, withhold it from the SDK.
                    landed_commit(request)
                    boundary("request_sent_response_withheld")
            return response
    api = store.database.spanner_api
    original = api.transport
    api._transport = SpannerGrpcTransport(channel=grpc.intercept_channel(original.grpc_channel, Withhold()))


def child_diagnostic():
    return "service not yet constructed"


async def run():
    global child_diagnostic
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.spanner_v1 import Client

    from trusted_router.async_settle_shadow_journal import (
        CLOSE_RESULT_KIND,
        CLOSURE_KIND,
        COUNTER_KIND,
        Journal,
        Row,
    )
    from trusted_router.async_settle_shadow_v2_a1_evidence import validate_close_result
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore
    project, instance, database_id, boot, point, manifest_path = sys.argv[1:]
    manifest_file = Path(manifest_path)
    client = Client(project=project, credentials=AnonymousCredentials(), disable_builtin_metrics=True)
    database = client.instance(instance).database(database_id)
    origin = time.monotonic()
    history_elapsed = [0.]
    base = 520 if point == "full_queues" else 1000
    clock = lambda: time.monotonic()+history_elapsed[0]  # noqa: E731
    store = ObservationStore(database, clock=clock,
        wall=lambda: NOW_US/1e6+base+clock()-origin)
    key = "2026-10-12/"+boot
    if point == "recover":
        manifest = json.loads(manifest_file.read_text())
        with client_history(store) as history:
            found = {}
            for identity, expected in manifest["rows"].items():
                kind, row_key = identity.split("|", 1)
                row = store.read(kind, row_key, Budget(store.clock()+4, store.clock))
                if row is not None:
                    assert row.digest in expected, json.dumps(dict(identity=identity, history=history, error="changed durable bytes"))
                    found[identity] = row.digest
            closure = store.read(CLOSURE_KIND, key, Budget(store.clock()+4, store.clock))
            result = store.read(CLOSE_RESULT_KIND, key, Budget(store.clock()+4, store.clock))
            counter = store.read(COUNTER_KIND, key, Budget(store.clock()+4, store.clock))
            if result is not None:
                assert closure is not None, json.dumps(history)
                validate_close_result(result.value(), closure.value(), termination_applies=True)
            for identity, digest in manifest["landed"].items():
                assert found.get(identity) == digest, json.dumps(dict(missing_landed=identity, history=history))
            state = dict(missing_close=result is None, writer_closure_gap=result is None,
                original_acknowledgement=False, final_attempts=manifest.get("final_attempts", 0),
                durable=result is not None, closure=closure is not None,
                found=found, landed=manifest["landed"], result_attempts=manifest["result_attempts"],
                sticky_loss=bool(counter and counter.value()["loss_count"]),
                lost_memory=manifest.get("lost_memory", {}), lost_attachments=manifest.get("attachments", []),
                hwm=counter.value()["durable_event_hwm"] if counter else None,
                event_seq=counter.value()["event_seq"] if counter else None, operations=history)
            print(json.dumps(state), flush=True)
        return
    svc = observation_service(store)
    # No publisher transaction may compete with the evidence history. The
    # saturated history deliberately queues its publisher before transaction entry.
    svc.settings.async_settle_shadow_health_region = ""
    svc.journal = Journal(boot)
    svc.bases.boot = svc.traces.boot = boot
    svc.counters.clear()
    svc.port.fold_cells.clear()
    svc.legacy.days.clear()
    stamp = int(store.wall()*1e6)
    day = svc.ensure_day(stamp)
    if point == "full_queues":
        history_elapsed[0] = 480.  # Deterministic prior missed-slot history; no sleep.
        stamp = NOW_US+1000_000_000
    manifest = dict(rows={}, landed={}, result_attempts=0, sessions=[])
    def save():
        manifest_file.write_text(json.dumps(manifest))
    def remember(rows, landed=False):
        for row in rows:
            identity = row.kind+"|"+row.key
            manifest["rows"].setdefault(identity, []).append(row.digest)
            if landed:
                manifest["landed"][identity] = row.digest
        save()
    # Persist only exact emulator session names for parent cleanup AFTER death.
    # Killing an open transaction does not promptly release the emulator's
    # database-wide RW lock; deleting that dead child's session does.
    api = store.database.spanner_api
    track_sessions(api, manifest, save)
    with client_history(store) as history:
        def diagnostic():
            return status(svc, history)
        child_diagnostic = diagnostic
        boundary_seen = threading.Event()
        def boundary(label):
            hold_boundary(svc, history, label, sys.stdout, boundary_seen, threading.Event())
        # Model a host handler, then exercise the actual Service.start/trap_sigterm chain.
        host_called = []
        def host_stop(*_):
            host_called.append(True)
            if point.startswith("request_pending"):
                # This history needs death while the RPC is outstanding. Chain
                # a terminating host handler; graceful shutdown is a separate
                # native history, with the full production grace interval.
                os._exit(0)
        signal.signal(signal.SIGTERM, host_stop)
        svc.loop = asyncio.get_running_loop()
        svc.next_flush = float("inf")
        if point == "full_queues":
            retained = saturate(svc, stamp)
            svc.publisher_executor.submit(lambda: threading.Event().wait(25))
            svc.submit_publisher(stamp//2_000_000, stamp)
            pages = svc.journal.freeze(day)
            svc.journal.checkpoint(day)
            counter = svc.counter_row(day, int(store.wall()*1e6), closed=False)
            remember([*pages, counter, *retained])
            store.flush(pages, counter, None, Budget(store.clock()+4, store.clock))
            remember([*pages, counter], landed=True)
            store.retain((retained[0],), Budget(store.clock()+4, store.clock))
            remember([retained[0]], landed=True)
            manifest["lost_memory"] = dict(pages=len(pages), retained=len(retained),
                basis=len(svc.bases.queue), trace=len(svc.traces.queue), observers=32, publishers=1,
                journal_bytes=svc.journal.retained_bytes, work_bytes=svc.journal.work_bytes,
                attachment_bytes=[svc.bases.retained_bytes, svc.traces.retained_bytes])
            manifest["attachments"] = [dict(key=o.key, digest=hashlib.sha256(o.body).hexdigest())
                for lane in (svc.bases, svc.traces) for o in lane.queue]
            save()
        else:
            svc.journal.grant(day, 100)
            for _ in range(4):
                reservation = svc.port.reserve(stamp)
                assert reservation is not None, diagnostic()
                assert svc.port.finalize(reservation, event_for(reservation)), diagnostic()
                assert svc.port.complete(reservation), diagnostic()
                svc.journal.freeze(day)
        remember(svc.journal.freeze(day))
        write, flush, result_write = store.write, store.flush, store.close_result
        def observe(tx, rows, budget):
            remember(rows)
            return write(tx, rows, budget)
        store.write = observe
        api = store.database.spanner_api
        commit = api.commit
        def landed_commit(request):
            from google.cloud.spanner_v1.types import CommitRequest
            request = CommitRequest(request)
            rows = [Row(row[0], row[1], row[2].encode()) for mutation in request.mutations
                    for values in (mutation.insert_or_update.values, mutation.insert.values) for row in values]
            remember(rows, landed=True)
        def original_commit(*args, **kwargs):
            from google.cloud.spanner_v1.types import CommitRequest
            request = CommitRequest(kwargs["request"])
            selected = any(row[0] == CLOSURE_KIND for mutation in request.mutations for row in mutation.insert_or_update.values)
            if selected and point.startswith("request_pending"):
                # All earlier RPCs have returned. Arm the parent's transport gate
                # before allowing this synchronous commit to leave the client.
                import select
                manifest["final_attempts"] = manifest.get("final_attempts", 0)+1
                save()
                print("GATE_ARM", flush=True)
                assert select.select([sys.stdin], [], [], 3)[0], "gate arm deadline"
                assert sys.stdin.buffer.read(1) == b"1", "gate arm acknowledgement"
            if selected and point == "during_commit":
                boundary("before_commit_send")
            try:
                result = commit(*args, **kwargs)
            finally:
                if selected and point.startswith("request_pending"):
                    manifest["pending_call_returned"] = True
                    save()
            landed_commit(request)
            if selected and point == "before_ack":
                boundary("before_ack")
            return result
        api.commit = original_commit
        if point == "request_sent":
            transport_boundary(store, boundary, landed_commit)
        def original_flush(pages, counter, closure, budget):
            flush(pages, counter, closure, budget)
            remember([*pages, counter, *([closure] if closure else [])], landed=True)
            if closure is not None and point == "after_ack":
                boundary("after_ack")
        store.flush = original_flush
        def handoff(row, budget):
            remember([row])
            if point == "before_result":
                boundary("before_result")
            manifest["result_attempts"] += 1
            save()
            result_write(row, budget)
            remember([row], landed=True)
            if point == "after_result":
                boundary("after_result")
        store.close_result = handoff
        svc.start()
        assert svc.signal_handler is not None, diagnostic()
        # No unrelated observer reads compete with controlled crash boundaries.
        await svc.observer.close()
        if point in {"before_first_flush", "full_queues"}:
            svc.flush_job = asyncio.create_task(asyncio.to_thread(boundary, point))
        elif point == "graceful":
            announce_boundary(svc, history, "graceful", sys.stdout)
            boundary_seen.set()
        else:
            svc.close_task = asyncio.create_task(svc.close())
        try:
            async with asyncio.timeout(12):
                while not host_called:
                    for job in (svc.flush_job, svc.close_task):
                        if job is not None and job.done() and not boundary_seen.is_set():
                            job.result()
                            raise AssertionError("crash boundary was not reached: "+diagnostic())
                    await asyncio.sleep(.01)
                await svc.close_done.wait()
            assert svc.close_finished, diagnostic()
            print(json.dumps(dict(shutdown=True, handler_called=bool(host_called), status=json.loads(diagnostic()))), flush=True)
            sys.stdout.flush()
            os._exit(0)  # End the held worker after the installed bounded close returned.
        except BaseException:
            print("CHILD_STATUS "+diagnostic(), file=sys.stderr, flush=True)
            print("CHILD_FAILURE "+type(sys.exception()).__name__, file=sys.stderr, flush=True)
            sys.stderr.flush()
            os._exit(1)


if __name__ == "__main__":
    watchdog = threading.Timer(25, lambda: os._exit(2))
    watchdog.daemon = True
    watchdog.start()
    try:
        asyncio.run(run())
    except BaseException:
        print(json.dumps(dict(failure=True, status=child_diagnostic())), flush=True)
        print("CHILD_STATUS "+child_diagnostic(), file=sys.stderr, flush=True)
        print("CHILD_FAILURE "+type(sys.exception()).__name__, file=sys.stderr, flush=True)
        sys.stderr.flush()
        os._exit(1)
