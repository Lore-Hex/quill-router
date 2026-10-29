"""Transaction outcomes and bounded publisher admission through the real owner."""
from __future__ import annotations

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_health_watchdog import accounting, failed, owner
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    sdk_database,  # noqa: F401
)
from tests.test_async_settle_shadow_observation_r7 import entries
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.services import async_settle_shadow_health as shell
from trusted_router.storage_gcp_shadow_observation import ObservationStore


def test_expiry_between_scope_entry_and_native_dispatch_blocks_new_rpc():
    from types import SimpleNamespace

    from trusted_router import storage_gcp_shadow_io as io

    now, calls = [0.], []
    db = SimpleNamespace(spanner_api=SimpleNamespace(commit=lambda **kw: calls.append(kw)))
    assert io.configure(db) is not None
    with pytest.raises(TimeoutError):
        with io.finish_transaction_rpcs(), io.rpc_scope(.5, .5, lambda: now[0]):
            now[0] = .6
            db.spanner_api.commit()
    assert not calls, "Completion allowance must not admit a new RPC after expiry"


def native_owner(native):
    db, clock, sleeps = native
    svc, wall, _, job = owner()
    clock.now = job.earliest_start_mono
    svc.store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    db.commit_duration = .02
    # These tests isolate the incumbent read-write transaction outcome.
    svc.incumbent_slot = job.slot-1
    return svc, job, db, clock, sleeps


def outcome(svc, job, expected, unknown=False):
    trace = next(e for e in entries(svc) if e["slot"] == job.slot)
    assert (trace["outcome"], trace["unknown_outcome"]) == (expected, unknown)
    assert svc.state.job is None
    accounting(svc)
    counts = svc.counters[trace["day"]]["publisher"]
    assert counts["scheduled_slots"] == counts["missed_slots"] + counts["claim_attempts"]
    assert counts["claim_attempts"] == counts["lost_claims"] + counts["publications"] + counts["failures"]
    return trace


def test_aborted_claim_commit_is_lost(sdk_database):  # noqa: F811
    svc, job, db, _, sleeps = native_owner(sdk_database)
    db.errors = [Aborted("claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "commit"]
    assert not sleeps


@pytest.mark.parametrize("streaming", [False, True])
def test_aborted_claim_read_is_lost_without_timestamp(sdk_database, monkeypatch, streaming):  # noqa: F811
    db, _, _ = sdk_database
    # The actual bounded wrapper sees both call and stream errors.
    original = db.spanner_api.execute_streaming_sql
    def aborted(**kwargs):
        if not streaming:
            raise Aborted("claim read aborted")
        def consume():
            yield from original(**kwargs)
            raise Aborted("claim stream aborted")
        return consume()
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", aborted)
    svc, job, db, _, sleeps = native_owner(sdk_database)
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["claim_at_ns"] is None
    assert not any(name == "commit" for name, _ in db.calls)
    assert not sleeps


@pytest.mark.parametrize("rollback_error", [DeadlineExceeded, ServiceUnavailable])
def test_aborted_claim_stream_survives_rollback_failure(sdk_database, monkeypatch, rollback_error):  # noqa: F811
    db, _, _ = sdk_database
    original = db.spanner_api.execute_streaming_sql
    def aborted(**kwargs):
        def consume():
            yield from original(**kwargs)  # Give the SDK the transaction ID for rollback.
            raise Aborted("claim stream aborted")
        return consume()
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", aborted)
    db.rollback_errors = [rollback_error("rollback failed")]
    svc, job, db, _, sleeps = native_owner(sdk_database)
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["claim_at_ns"] is None
    assert trace["cycle_us"] == 210_000
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "rollback"]
    assert not db.rollback_errors and not sleeps
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == counts["lost_claims"] == 1
    assert counts["failures"] == counts["unknown_outcomes"] == counts["publications"] == 0


def test_claim_commit_deadline_without_abort_stays_unknown(sdk_database):  # noqa: F811
    svc, job, db, _, sleeps = native_owner(sdk_database)
    db.errors = [DeadlineExceeded("claim commit reply deadline")]
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "failed", unknown=True)
    assert trace["claim_at_ns"] is not None
    assert trace["cycle_us"] == 30_000
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "commit"]
    assert not db.errors and not sleeps
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == counts["failures"] == counts["unknown_outcomes"] == 1
    assert counts["lost_claims"] == counts["publications"] == 0


@pytest.mark.parametrize("stage", ["commit", "read"])
def test_sdk_reentry_is_lost_without_second_attempt(sdk_database, monkeypatch, stage):  # noqa: F811
    from google.cloud.spanner_v1 import session
    svc, job, db, _, sleeps = native_owner(sdk_database)
    reentries = []
    # Exercise the installed Session retry branch when ABORTED reaches it,
    # bypassing only the transport's conversion and SDK backoff sleep.
    def abort(**kwargs):
        raise Aborted("SDK abort", errors=[object()])
    monkeypatch.setattr(db.spanner_api, "commit" if stage == "commit" else "execute_streaming_sql", abort)
    monkeypatch.setattr(session, "_delay_until_retry", lambda *a, **kw: reentries.append(1))
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert reentries == [1] and not sleeps
    assert sum(name == "read" for name, _ in db.calls) == int(stage == "commit")


@pytest.mark.parametrize("stage", ["claim", "publish"])
def test_direct_aborted_transaction_is_known(monkeypatch, stage):
    svc, wall, _, job = owner()
    db = svc.store.database
    commit = db._try_commit
    commits = []
    def aborted(tx):
        commits.append(tx)
        if len(commits) == (1 if stage == "claim" else 2):
            raise Aborted("transaction aborted")
        return commit(tx)
    monkeypatch.setattr(db, "_try_commit", aborted)
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, "lost_claim" if stage == "claim" else "failed")
    assert len(commits) == (1 if stage == "claim" else 2)
    assert db.commits == int(stage == "publish")


def test_aborted_claim_then_commit_deadline_stays_unknown(sdk_database):  # noqa: F811
    svc, job, db, clock, sleeps = native_owner(sdk_database)
    db.errors = [Aborted("first claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    # A later cycle's commit deadline has a different, unknown outcome.
    stamp = NOW_US + m.SLOT_US
    clock.now = 102.
    svc.store.wall = lambda: stamp/1e6
    db.server_ns = stamp*1000
    db.errors = [DeadlineExceeded("commit reply deadline")]
    svc.tick(stamp)
    next_job = svc.state.job
    assert next_job is not None
    svc.incumbent_slot = next_job.slot-1
    svc.submit_publisher(next_job.slot, stamp)
    outcome(svc, next_job, "failed", unknown=True)
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == 2
    assert counts["lost_claims"] == counts["failures"] == counts["unknown_outcomes"] == 1
    assert [name for name, _ in db.calls] == ["read", "commit", "read", "commit"]
    assert not sleeps


def test_admission_lock_wait_is_bounded_and_releases(monkeypatch):
    svc, wall, mono, job = owner()
    lock = svc.admission_lock
    waits = []
    class Held:
        def __enter__(self):
            raise AssertionError("unbounded admission wait")
        def acquire(self, *, timeout):
            waits.append(timeout)
            mono[0] += timeout
            wall[0] += round(timeout*1e6)
            return False
        def release(self):
            raise AssertionError("unowned admission lock released")
    monkeypatch.setattr(svc, "admission_lock", Held())
    reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                               svc.settings.source_revision, job, NOW_US, svc)
    monkeypatch.setattr(svc, "admission_lock", lock)
    svc.publisher_reply(reply)
    assert waits == [.5]
    outcome(svc, job, "missed")
    assert not svc.store.database.commits
    wall[0], mono[0] = NOW_US+m.SLOT_US, 102.
    svc.store.database.server_ns = wall[0]*1000
    svc.publish_cycle(wall[0]//m.SLOT_US, wall[0])
    assert entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


def test_released_vetoed_day_enumerates_remaining_slots():
    # Three opportunities remain in this day, including the outstanding job.
    stamp = NOW_US + m.DAY_US - 3*m.SLOT_US
    svc, wall, mono, job = owner(stamp)
    day = m.day_name(stamp)
    svc.accept(m.ObserverEnqueued(stamp, "read"))
    svc.accept(m.ObserverFolded(stamp, day, "read", success=False))
    svc.accept(m.PublisherFailed(stamp, job.slot, job.generation, "publisher_watchdog"))
    failed(svc, job)
    trace = entries(svc)[0]
    assert (trace["cycle_us"], trace["rpc_count"], trace["unknown_outcome"], trace["claim_at_ns"]) == (0, 0, True, None)
    wall[0], mono[0] = NOW_US+m.DAY_US, 106.
    svc.tick(wall[0])
    old = next(d for d in svc.state.days if d.day == day)
    assert old.veto and old.cursor == old.last_slot
    assert old.scheduled == 3 and old.missed == 2 and old.attempted == old.failed == 1
    assert [e["outcome"] for e in entries(svc) if e["day"] == day] == ["failed", "missed", "missed"]
    accounting(svc)


@pytest.mark.parametrize("gap", [0, 1])
def test_losing_claim_rolls_back_without_empty_commit(gap):
    from trusted_router.storage_gcp_shadow_observation import Budget, ContentionLoss
    svc, wall, _, job = owner()
    db = svc.store.database
    db.server_ns = (job.slot-gap)*m.SLOT_US*1000
    assert svc.store.claim("incumbent", job.slot-gap, wall[0], Budget(101., svc.store.clock))
    db.server_ns = wall[0]*1000
    commits = db.commits
    with pytest.raises(ContentionLoss, match="slot_already_claimed"):
        svc.store.claim("follower", job.slot, wall[0], Budget(101., svc.store.clock))
    assert db.commits == commits


def test_one_aborted_cycle_keeps_recent_accepted_incumbent(sdk_database):  # noqa: F811
    svc, job, db, _, _ = native_owner(sdk_database)
    db.errors = [Aborted("claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert svc.is_incumbent(job.slot+1)
    assert svc.is_incumbent(job.slot+2)  # Third slot since the accepted publication.
    assert not svc.is_incumbent(job.slot+3)


@pytest.mark.parametrize("rollback_error", [None, DeadlineExceeded, ServiceUnavailable])
def test_claim_already_taken_uses_sdk_rollback_not_commit(sdk_database, monkeypatch, rollback_error):  # noqa: F811
    import json

    from tests.test_async_settle_shadow_health_stagger import claim
    svc, job, db, _, sleeps = native_owner(sdk_database)
    original = db.spanner_api.execute_streaming_sql
    def claimed(**kwargs):
        for response in original(**kwargs):
            response._pb.values[0].string_value = json.dumps(claim("other", job.slot))
            yield response
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", claimed)
    if rollback_error is not None:
        db.rollback_errors = [rollback_error("rollback response failed")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert [name for name, _ in db.calls] == ["read", "rollback"]
    assert not sleeps


@pytest.mark.parametrize("stage", ["read", "stream", "commit"])
def test_late_claim_rpc_finishes_and_sdk_rolls_back_without_server_error(sdk_database, monkeypatch, stage):  # noqa: F811
    from trusted_router import storage_gcp_shadow_io as io
    from trusted_router.storage_gcp_shadow_observation import (
        Budget,
        ObservationStore,
        UnknownCommit,
    )

    db, clock, sleeps = sdk_database
    db.commit_duration = .02
    if stage == "read":
        db.read_duration = 10.
    elif stage == "commit":
        db.commit_duration = 10.
    else:
        original = db.spanner_api.execute_streaming_sql
        def slow_stream(**kwargs):
            yield from original(**kwargs)  # SDK has a transaction ID before the pause.
            clock.now += 10.
        monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", slow_stream)
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    failures = []
    store.transport.failure = lambda *args: failures.append(args)
    with pytest.raises(UnknownCommit if stage == "commit" else TimeoutError):
        store.claim("issuer", NOW_US//2_000_000, NOW_US, Budget(clock.now+.5, clock.monotonic))
    assert not failures and not sleeps
    assert [name for name, _ in db.calls] == ["read", "commit" if stage == "commit" else "rollback"]
    assert all(timeout == (io.PUBLISHER_ROLLBACK_SECONDS if name == "rollback"
                           else io.PUBLISHER_TRANSACTION_RPC_SECONDS) for name, timeout in db.calls)
    assert store.claim_unknown == (stage == "commit")
    assert io._SCOPE.get() is None and not io._FINISH_TRANSACTION.get()


@pytest.mark.parametrize("operation", ["claim", "publish"])
def test_expired_publisher_transaction_never_starts_rpc(sdk_database, operation):  # noqa: F811
    from tests.test_async_settle_shadow_observation import health
    from trusted_router import storage_gcp_shadow_io as io
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

    db, clock, _ = sdk_database
    store = ObservationStore(db, clock=clock.monotonic)
    budget = Budget(clock.now, clock.monotonic)
    with pytest.raises(TimeoutError):
        if operation == "claim":
            store.claim("issuer", NOW_US//2_000_000, NOW_US, budget)
        else:
            store.publish(health(), budget)
    assert not db.calls
    assert io._SCOPE.get() is None and not io._FINISH_TRANSACTION.get()


@pytest.mark.parametrize("stage", ["fence", "commit"])
def test_late_publication_rpc_drains_without_extending_acceptance(sdk_database, monkeypatch, stage):  # noqa: F811
    from tests.test_async_settle_shadow_observation import health
    from trusted_router.async_settle_shadow_journal import CLAIM_KIND, Row
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

    db, clock, sleeps = sdk_database
    value = health()
    fence = dict(v=2, policy_version=value["policy_version"], contract_revision="a1",
        authority="local", slot=value["slot"], claim_at_ns=value["observed_at_ns"],
        owner_boot_id=value["publisher_boot_id"])
    original = db.spanner_api.execute_streaming_sql
    def read(**kwargs):
        kind = kwargs["request"].params["kind"]
        db.rows = [Row.build(CLAIM_KIND, "local/publish", fence).body.decode()] if kind == CLAIM_KIND else []
        return original(**kwargs)
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", read)
    db.read_duration = 10. if stage == "fence" else .01
    db.commit_duration = 10.
    store = ObservationStore(db, clock=clock.monotonic)
    failures = []
    store.transport.failure = lambda *args: failures.append(args)
    with pytest.raises(TimeoutError if stage == "fence" else shell.UnknownCommit):
        store.publish(value, Budget(clock.now+.5, clock.monotonic))
    assert [name for name, _ in db.calls] == (["read", "rollback"] if stage == "fence" else ["read", "read", "commit"])
    assert not failures and not sleeps


def test_real_server_deadline_is_still_reported_to_breaker(sdk_database):  # noqa: F811
    from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

    db, clock, sleeps = sdk_database
    db.read_duration = 13.
    store = ObservationStore(db, clock=clock.monotonic)
    failures = []
    store.transport.failure = lambda *args: failures.append(args)
    with pytest.raises(RuntimeError):
        store.claim("issuer", NOW_US//2_000_000, NOW_US, Budget(clock.now+.5, clock.monotonic))
    assert failures == [("execute_streaming_sql", "DeadlineExceeded")]
    assert not sleeps


def shutdown_native_witness(stage, blocked, path):
    """Child process: real GAPIC/Session/Transaction, controlled gRPC responses."""
    import asyncio
    import datetime as dt
    import json
    import signal
    import threading
    import time
    from types import SimpleNamespace

    import grpc
    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.cloud.spanner_v1.services.spanner.transports.grpc import SpannerGrpcTransport
    from google.cloud.spanner_v1.types import CommitResponse, PartialResultSet, Transaction
    from google.protobuf.empty_pb2 import Empty

    from tests.test_async_settle_shadow_observation import health, service
    from trusted_router.storage_gcp_shadow_io import Sessions

    def record(event, **fields):
        with open(path, 'a') as output:
            output.write(json.dumps(dict(event=event, at=time.monotonic(), **fields))+'\n')

    churn = stage == 'churn'
    epoch = [time.monotonic()]
    def wall():
        return NOW_US/1e6+(time.monotonic()-epoch[0] if churn else 0.)
    transactions, rows = set(), {}
    serial = [0]
    ready = threading.Event()

    class CancelledCall(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.CANCELLED
        def details(self):
            return 'shutdown witness'
        def trailing_metadata(self):
            return ()
        def initial_metadata(self):
            return ()

    class Call:
        def code(self):
            return grpc.StatusCode.CANCELLED if self.cancelled_event.is_set() else grpc.StatusCode.OK
        def details(self):
            return ''
        def initial_metadata(self):
            return ()
        def trailing_metadata(self):
            return ()
        def __init__(self, method, request):
            self.method, self.request = method, request
            self.cancelled_event = threading.Event()
            self.yielded = False
            self.hold = (method == ('Commit' if stage.endswith('commit') else 'ExecuteStreamingSql')
                         and serial[0] == (2 if stage.startswith('publish') else 1))
            self.release_at = time.monotonic()+10
            if self.hold:
                record('active', method=method, transactions=len(transactions), blocked=blocked)
                ready.set()
        def cancel(self):
            if not self.cancelled_event.is_set():
                record('cancel', method=self.method)
                self.cancelled_event.set()
            return True
        def result(self):
            if self.hold:
                assert self.cancelled_event.wait(15), 'native call never cancelled'
                raise grpc.FutureCancelledError()
            request = self.request
            if self.method == 'CreateSession':
                from google.cloud.spanner_v1.types import Session
                time.sleep(1.9 if churn else .01)
                record('prepared')
                return Session(name=db.name+'/sessions/replacement')
            if self.method == 'BeginTransaction':
                serial[0] += 1
                ident = str(serial[0]).encode()
                transactions.add(ident)
                record('begin', transaction=serial[0])
                return Transaction(id=ident)
            if self.method == 'Commit':
                for mutation in request.mutations:
                    for values in mutation.insert_or_update.values:
                        rows[(values[0], values[1])] = values[2]
                transactions.discard(request.transaction_id)
                record('commit')
                return CommitResponse(commit_timestamp=dt.datetime.fromtimestamp(wall(), dt.UTC))
            if self.method == 'Rollback':
                time.sleep(.05)
                transactions.discard(request.transaction_id)
                record('rollback', remaining=len(transactions))
                return Empty()
            if self.method == 'DeleteSession':
                assert not transactions, 'deletion must follow transaction rollback'
                time.sleep(4.9 if churn and not stopping[0] else .05)
                record('delete', remaining=len(transactions))
                return Empty()
            raise AssertionError(self.method)
        def __iter__(self):
            return self
        def __next__(self):
            if self.hold and (blocked or self.yielded):
                if stage.endswith('python'):
                    time.sleep(15)  # Worker cannot resume even when the native call is cancelled.
                    raise CancelledCall()
                elif churn:
                    assert not self.cancelled_event.wait(max(0, self.release_at-time.monotonic()))
                else:
                    assert self.cancelled_event.wait(15)
                    raise CancelledCall()
            if self.yielded:
                raise StopIteration
            self.yielded = True
            request = self.request
            claim = 'CURRENT_TIMESTAMP() AS claim_at' in request.sql
            response = PartialResultSet(metadata=dict(transaction=dict(id=str(serial[0]).encode()),
                row_type=dict(fields=[dict(name='body', type=dict(code='STRING'))]+
                    ([dict(name='claim_at', type=dict(code='TIMESTAMP'))] if claim else []))))
            body = rows.get((request.params['kind'], request.params['id']))
            if body is not None:
                response._pb.values.add(string_value=body)
            elif claim:
                response._pb.values.add(null_value=0)
            if claim:
                response._pb.values.add(string_value=dt.datetime.fromtimestamp(wall(), dt.UTC).isoformat())
            return response

    class Unary:
        def __init__(self, method):
            self.method = method
        def future(self, request, **kwargs):
            record('dispatch', method=self.method, timeout=kwargs.get('timeout'))
            return Call(self.method, request)
        def __call__(self, request, **kwargs):
            return self.future(request, **kwargs).result()
        def with_call(self, request, **kwargs):
            call = self.future(request, **kwargs)
            return call.result(), call
    class Stream:
        def __init__(self, method):
            self.method = method
        def __call__(self, request, **kwargs):
            record('dispatch', method=self.method, timeout=kwargs.get('timeout'))
            return Call(self.method, request)
    class Channel(grpc.Channel):
        def subscribe(self, *args, **kwargs):
            pass
        def unsubscribe(self, *args, **kwargs):
            pass
        def stream_unary(self, *args, **kwargs):
            raise AssertionError('unexpected streaming request')
        def stream_stream(self, *args, **kwargs):
            raise AssertionError('unexpected streaming request')
        def unary_unary(self, method, *args, **kwargs):
            return Unary(method.rsplit('/', 1)[-1])
        def unary_stream(self, method, *args, **kwargs):
            return Stream(method.rsplit('/', 1)[-1])
        def close(self):
            record('lane_closed')

    async def run():
        nonlocal db
        patch = pytest.MonkeyPatch()
        fake_clock = SimpleNamespace(now=0.)
        fake_clock.monotonic = lambda: fake_clock.now
        db, _, _ = sdk_database.__wrapped__(patch, fake_clock)
        db.spanner_api = SpannerClient(transport=SpannerGrpcTransport(channel=Channel()))
        lane = ObservationStore(db, clock=time.monotonic, wall=wall)
        manager = Sessions(db, managed=True, lane='publisher')
        manager.session = db.session
        db._sessions_manager = manager
        def transaction(callback, **kwargs):
            session = manager.get_session(None)
            try:
                return session.run_in_transaction(callback, **kwargs)
            finally:
                manager.put_session(session)
        db.run_in_transaction = transaction
        lane.owns_publisher_database = True
        lane.observe = lambda boot, region, revision, slot, budget, **kw: dict(health(now_us=round(wall()*1e6)), publisher_boot_id=boot)
        svc = service()
        svc.store.clock, svc.store.wall = time.monotonic, wall
        svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
        svc.loop, svc.supervised = asyncio.get_running_loop(), True
        svc.publisher_store, svc.publisher_lane_open = lane, True
        failures = []
        lane.transport.failure = lambda *args: failures.append(args)
        original_flush = svc._flush
        def flush(deadline):
            record('flush', remaining=deadline-time.monotonic())
            yield from original_flush(deadline)
        svc._flush = flush
        if churn:
            def replacement():
                other, _, _ = sdk_database.__wrapped__(patch, fake_clock)
                other.database_role = None
                other.spanner_api = SpannerClient(transport=SpannerGrpcTransport(channel=Channel()))
                result = ObservationStore(other, clock=time.monotonic, wall=wall)
                other._sessions_manager = Sessions(other, managed=True, lane='publisher')
                def transaction(callback, **kwargs):
                    session = other._sessions_manager.get_session(None)
                    try:
                        return session.run_in_transaction(callback, **kwargs)
                    finally:
                        other._sessions_manager.put_session(session)
                other.run_in_transaction = transaction
                result.owns_publisher_database = True
                result.read_shadow_claim = lambda budget: None
                result.read_shadow_publication = lambda budget: None
                result.observe = lane.observe
                result.transport.failure = lambda *args: failures.append(args)
                return result
            svc.store.publisher_lane = replacement
            svc.observer.start = lambda: None
            svc.next_flush = float('inf')
            epoch[0] = time.monotonic()
        signal.signal(signal.SIGTERM, lambda *args: record('sigterm'))
        svc.trap_sigterm()
        job = next(e.job for e in svc.tick(NOW_US) if isinstance(e, m.StartPublisherCycle))
        svc.incumbent_slot = job.slot-1
        svc.submit_publisher(job.slot, NOW_US)
        assert await asyncio.to_thread(ready.wait, 5), 'publisher did not enter held RPC'
        record('ready')
        if stage.endswith('owner'):
            time.sleep(7)  # SIGTERM runs, but its queued owner callback cannot run.
        if churn:
            owner = asyncio.create_task(svc.run())
            try:
                until = epoch[0]+24
                while time.monotonic() < until:
                    svc.publisher_inventory()
                    assert len(svc.abandoned_publishers) <= 1
                    from trusted_router.storage_gcp_shadow_observation import HEALTH_KIND
                    if rows.get((HEALTH_KIND, 'local/fleet')) and not svc.abandoned_publishers:
                        break
                    await asyncio.sleep(.01)
                assert rows.get((HEALTH_KIND, 'local/fleet')), rows
                assert not svc.abandoned_publishers
                assert svc.publisher_lane_generation >= 1
                assert not failures, failures
                record('recovered', elapsed=time.monotonic()-epoch[0], faults=svc.publisher_faults)
            finally:
                stopping[0] = True
                await svc.close()
                await owner
            patch.undo()
            return
        while svc.close_task is None:
            await asyncio.sleep(.01)
        await svc.close_task
        assert not transactions
        assert len(failures) == (0 if stage.endswith('python') else 1), failures
        # No new native transaction can be admitted after the terminal latch.
        from trusted_router.storage_gcp_shadow_observation import Budget
        with pytest.raises(TimeoutError, match='publisher_stopping'):
            lane.claim('stopping', job.slot+1, NOW_US+2_000_000, Budget(time.monotonic()+.5, time.monotonic))
        record('drained', failures=failures, remaining=len(transactions))
        await asyncio.sleep(100)  # The parent enforces the real ten-second kill.
    db, stopping = None, [False]
    asyncio.run(run())


@pytest.mark.parametrize('stage,blocked', [('claim-read', False), ('claim-read', True),
    ('publish-read', True), ('publish-commit', False), ('claim-python', False), ('claim-owner', False)])
def test_sigterm_rolls_back_and_deletes_native_publisher_before_hard_kill(tmp_path, stage, blocked):
    import json
    import os
    import signal
    import subprocess
    import sys
    import time

    output = tmp_path/'shutdown.jsonl'
    command = ('from tests.test_async_settle_shadow_health_contention import shutdown_native_witness; '
               'import sys; shutdown_native_witness(sys.argv[1], sys.argv[2] == "True", sys.argv[3])')
    with (tmp_path/'child.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-c', command, stage, str(blocked), str(output)],  # noqa: S603 - fixed child witness
                                   stdout=log, stderr=log)
        def events():
            return [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
        try:
            until = time.monotonic()+120  # Module collection is outside the signal/kill clock.
            while not any(e['event'] == 'ready' for e in events()) and process.poll() is None and time.monotonic() < until:
                time.sleep(.02)
            log.seek(0)
            assert any(e['event'] == 'ready' for e in events()), log.read()
            os.kill(process.pid, signal.SIGTERM)
            kill_at = time.monotonic()+10
            while time.monotonic() < kill_at and process.poll() is None:
                time.sleep(min(.02, max(0, kill_at-time.monotonic())))
            assert process.poll() is None, 'child exited before the hard-kill witness'
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            found = events()
            by_name = {e['event']: e for e in found}
            start = by_name['sigterm']['at']
            log.seek(0)
            assert 'drained' in by_name, log.read()
            assert len([e for e in found if e['event'] == 'cancel']) == 1
            assert 3.9 <= by_name['cancel']['at']-start < 5
            assert start < by_name['rollback']['at'] < by_name['delete']['at'] < by_name['lane_closed']['at'] < start+9
            assert by_name['drained']['remaining'] == 0
            if stage.endswith('owner'):
                assert by_name['lane_closed']['at'] < by_name['flush']['at']
            else:
                assert by_name['flush']['at'] < by_name['cancel']['at']
                assert by_name['flush']['remaining'] > 3.5
            assert not [e for e in found if e['event'] == 'begin' and e['at'] > start]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


@pytest.mark.parametrize('blocked', [False, True], ids=['stalled-reader', 'abandoned-lock'])
def test_supervised_native_churn_recovers_with_cleanup_within_twelve_slots(tmp_path, blocked):
    import json
    import subprocess
    import sys

    events = tmp_path/'supervised.jsonl'
    command = ('from tests.test_async_settle_shadow_health_contention import shutdown_native_witness; '
               'import sys; shutdown_native_witness("churn", sys.argv[1] == "True", sys.argv[2])')
    result = subprocess.run([sys.executable, '-c', command, str(blocked), str(events)],  # noqa: S603 - fixed witness
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stderr
    found = [json.loads(line) for line in events.read_text().splitlines()]
    recovered = next(e for e in found if e['event'] == 'recovered')
    assert recovered['elapsed'] <= 24
    assert recovered['faults'] >= 1  # Actual Service.tick watchdog quarantined the lane.
    assert not [e for e in found if e['event'] == 'cancel']
    assert any(e['event'] == 'prepared' for e in found)
    assert any(e['event'] == 'delete' and e['at'] < recovered['at'] for e in found)
