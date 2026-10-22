"""Server-side session inventory must stay bounded across native SDK faults."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import DeadlineExceeded, NotFound
from google.cloud.spanner_v1.services.spanner.pagers import ListSessionsPager
from google.cloud.spanner_v1.types import ListSessionsRequest, ListSessionsResponse
from google.cloud.spanner_v1.types import Session as SessionPB

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import deterministic_scheduler  # noqa: F401
from tests.test_async_settle_shadow_sdk_retry import sdk_database
from trusted_router.storage_gcp_shadow_io import Sessions, configure, rpc_scope


@pytest.fixture
def inventory(monkeypatch, shadow_deadline_clock):
    db, clock, _ = sdk_database.__wrapped__(monkeypatch, shadow_deadline_clock)
    db.database_role = None
    state = SimpleNamespace(live=set(), creates=0, deletes=0, create_error=False,
                            delete_error=False, missing=False, late_create=False,
                            stream_error=False, create_timeouts=[], labels={}, events=[],
                            list_error=False, list_fail_page=None, list_duration=0.,
                            delete_duration=0., page_size=1000, listings=[])

    def record(operation):
        state.events.append((operation, set(state.live)))

    def create(**kwargs):
        assert kwargs['retry'] is None
        state.creates += 1
        state.create_timeouts.append(kwargs['timeout'])
        name = db.name + '/sessions/' + str(state.creates)
        state.live.add(name)
        state.labels[name] = dict(kwargs['request'].session.labels)
        record('create')
        if state.create_error:
            raise DeadlineExceeded('response lost after allocation')
        if state.late_create:
            clock.now += kwargs['timeout']
        return SessionPB(name=name)

    def delete(**kwargs):
        assert kwargs['retry'] is None
        state.deletes += 1
        clock.now += min(state.delete_duration, kwargs['timeout'])
        record('delete')
        if state.delete_error:
            raise DeadlineExceeded('cleanup unavailable')
        state.live.discard(kwargs['name'])
        record('deleted')
        if state.missing:
            raise NotFound('session already gone')

    def listing(**kwargs):
        assert kwargs['retry'] is None
        request = ListSessionsRequest(kwargs['request'])
        state.listings.append((request, kwargs['timeout'], clock.now))
        record('list')
        clock.now += min(state.list_duration, kwargs['timeout'])
        if state.list_error or request.page_token == state.list_fail_page:
            raise DeadlineExceeded('listing unavailable')
        assert request.database == db.name
        # Model server-side filtering; foreign boots and components stay live.
        component, boot = request.filter.split(' AND ')
        assert component == 'labels.tr_component:shadow-v2'
        assert boot.startswith('labels.tr_boot:')
        labels = dict(tr_component='shadow-v2', tr_boot=boot.removeprefix('labels.tr_boot:'))
        sessions = [SessionPB(name=n, labels=state.labels[n]) for n in sorted(state.live)
                    if state.labels[n] == labels]
        start = int(request.page_token or '0')
        end = start + state.page_size
        response = ListSessionsResponse(sessions=sessions[start:end],
                                       next_page_token=str(end) if end < len(sessions) else '')
        def forbidden_page(*args, **kwargs):
            raise AssertionError('pagination bypassed the bounded RPC wrapper')
        return ListSessionsPager(forbidden_page, request, response)

    db.spanner_api.create_session = create
    db.spanner_api.delete_session = delete
    db.spanner_api.list_sessions = listing
    query = db.spanner_api.execute_streaming_sql
    def stream(**kwargs):
        if state.stream_error:
            def iterate():
                raise DeadlineExceeded('stream interrupted')
                yield  # pragma: no cover
            return iterate()
        return query(**kwargs)
    db.spanner_api.execute_streaming_sql = stream
    db._sessions_manager = Sessions(db)
    configure(db)
    return db, clock, state


def failed_read(db, clock):
    with rpc_scope(clock.now + 1, 1, clock.monotonic):
        session = db._sessions_manager.get_session(None)
        try:
            db.read_errors = [DeadlineExceeded('read timeout')]
            with pytest.raises(RuntimeError, match='shadow_rpc_failed'):
                db.spanner_api.execute_streaming_sql(request=SimpleNamespace(sql='SELECT 1'))
        finally:
            db._sessions_manager.put_session(session)


def test_repeated_rpc_failures_do_not_accumulate_server_sessions(inventory):
    db, clock, state = inventory
    for _ in range(20):
        failed_read(db, clock)
        assert len(state.live) <= 1
    assert state.deletes == 19


@pytest.mark.parametrize('missing', [False, True])
def test_failed_cleanup_blocks_replacement_until_it_succeeds(inventory, missing):
    db, clock, state = inventory
    failed_read(db, clock)
    state.delete_error = True
    for _ in range(3):
        with rpc_scope(clock.now + 1, 1, clock.monotonic):
            with pytest.raises(RuntimeError):
                db._sessions_manager.get_session(None)
    assert state.creates == 1
    state.delete_error, state.missing = False, missing
    failed_read(db, clock)
    assert state.creates == 2 and len(state.live) == 1


def test_unknown_creation_result_does_not_loop_allocating_sessions(inventory):
    db, clock, state = inventory
    state.create_error = True
    for _ in range(5):
        with rpc_scope(clock.now + 1, 1, clock.monotonic):
            with pytest.raises(RuntimeError):
                db._sessions_manager.get_session(None)
    assert state.creates == 1 and len(state.live) == 1


def test_late_successful_creation_retains_identity_for_cleanup(inventory):
    db, clock, state = inventory
    state.late_create = True
    with pytest.raises((RuntimeError, TimeoutError)):
        with rpc_scope(clock.now + 1, 1, clock.monotonic):
            db._sessions_manager.get_session(None)
    state.late_create = False
    failed_read(db, clock)
    assert state.deletes == 1 and len(state.live) == 1


def test_healthy_checkouts_reuse_one_labeled_session(inventory):
    db, clock, state = inventory
    for _ in range(20):
        with rpc_scope(clock.now + 1, 1, clock.monotonic):
            session = db._sessions_manager.get_session(None)
            assert set(session.labels) == {"tr_component", "tr_boot"}
            assert session.labels == {"tr_component": "shadow-v2", "tr_boot": db._sessions_manager.boot}
            db._sessions_manager.put_session(session)
    assert state.creates == 1 and state.deletes == 0


def test_iteration_failure_retains_session_until_cleanup(inventory):
    db, clock, state = inventory
    state.stream_error = True
    for _ in range(3):
        with rpc_scope(clock.now + 1, 1, clock.monotonic):
            session = db._sessions_manager.get_session(None)
            try:
                with pytest.raises(RuntimeError, match='shadow_stream_failed'):
                    list(db.spanner_api.execute_streaming_sql())
            finally:
                db._sessions_manager.put_session(session)
        assert len(state.live) == 1
    assert state.deletes == 2


def test_install_prepares_session_under_own_five_second_budget(inventory, monkeypatch):
    from tests.test_async_settle_shadow_health_watchdog import owner
    from trusted_router.services import async_settle_shadow_health as shell
    from trusted_router.storage_gcp_shadow_observation import ObservationStore
    db, clock, state = inventory
    svc, wall, _, _ = owner()
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    monkeypatch.setattr(shell, 'ObservationStore', lambda database: store)
    app = SimpleNamespace(state=SimpleNamespace(), on_event=lambda name: lambda fn: fn)
    shell.install(app, svc.settings, SimpleNamespace(_database=db))
    assert state.creates == 1 and state.create_timeouts == [5.]
    assert db._sessions_manager.ready
    assert state.labels[next(iter(state.live))] == dict(
        tr_component='shadow-v2', tr_boot=app.state.async_settle_shadow_observation.journal.boot)
    assert app.state.async_settle_shadow_observation.state.job is None
    assert not db.calls


def test_quarantined_cycle_never_calls_storage(inventory, monkeypatch):
    from tests.test_async_settle_shadow_health_contention import outcome
    from tests.test_async_settle_shadow_health_watchdog import owner
    from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE
    from trusted_router.storage_gcp_shadow_observation import ObservationStore
    db, clock, state = inventory
    state.create_error = True
    assert not db._sessions_manager.prepare(clock.monotonic)
    svc, wall, _, job = owner()
    clock.now = job.earliest_start_mono
    svc.store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    def forbidden(*args, **kwargs):
        raise AssertionError('quarantined cycle reached storage')
    for method in ('read_shadow_claim', 'claim', 'observe', 'publish'):
        monkeypatch.setattr(svc.store, method, forbidden)
    svc.submit_publisher(job.slot, wall[0])
    trace = outcome(svc, job, 'missed')
    PUBLISHER_SLOT_TRACE(trace)
    assert trace['rpc_count'] == 0 and trace['claim_start_us'] is None
    assert trace['cycle_us'] is None and not trace['unknown_outcome']
    assert state.creates == 1


def test_managed_session_never_recreates_inside_cycle(inventory):
    db, clock, state = inventory
    manager = db._sessions_manager
    assert manager.prepare(clock.monotonic)
    manager.retire()
    with rpc_scope(clock.now+.5, .5, clock.monotonic):
        with pytest.raises(RuntimeError, match='shadow_session_unavailable'):
            manager.get_session(None)
        with pytest.raises(RuntimeError, match='shadow_session_prepare_in_cycle'):
            manager.prepare(clock.monotonic)
    assert state.creates == 1
    clock.now += 60
    assert manager.prepare(clock.monotonic)
    assert state.creates == 2 and state.deletes == 1 and len(state.live) == 1
    assert state.create_timeouts == [5., 5.]


def test_late_known_creation_recovers_once_after_sixty_seconds(inventory):
    db, clock, state = inventory
    manager = db._sessions_manager
    started = clock.now
    state.late_create = True
    assert not manager.prepare(clock.monotonic)
    assert manager.retired is not None and not manager.creation_unknown
    assert state.creates == len(state.live) == 1
    state.late_create = False
    clock.now = started+59.999
    assert not manager.prepare(clock.monotonic)
    assert state.creates == 1 and state.deletes == 0
    clock.now = started+60
    assert manager.prepare(clock.monotonic)
    assert manager.ready and manager.retired is None
    assert state.creates == 2 and state.deletes == 1 and len(state.live) == 1
    assert state.create_timeouts == [5., 5.]
    for _ in range(10):
        assert manager.prepare(clock.monotonic)
    assert state.creates == 2


def test_recovery_cleanup_failure_prevents_creation_and_is_rate_limited(inventory):
    db, clock, state = inventory
    manager = db._sessions_manager
    assert manager.prepare(clock.monotonic)
    manager.retire()
    clock.now += 60
    state.delete_error = True
    assert not manager.prepare(clock.monotonic)
    assert state.creates == 1 and len(state.live) == 1
    state.delete_error = False
    clock.now += 59.999
    assert not manager.prepare(clock.monotonic)
    assert state.deletes == 1 and state.creates == 1
    clock.now += .001
    assert manager.prepare(clock.monotonic)
    assert state.deletes == 2 and state.creates == 2 and len(state.live) == 1


def unknown_creation(inventory):
    db, clock, state = inventory
    manager = db._sessions_manager
    manager.boot = 'cold-start-boot'
    state.create_error = True
    assert not manager.prepare(clock.monotonic)
    assert manager.creation_unknown and not manager.ready
    state.create_error = False
    return manager


def seed_session(db, state, identity, boot, component='shadow-v2'):
    name = db.name+'/sessions/'+identity
    state.live.add(name)
    state.labels[name] = dict(tr_component=component, tr_boot=boot)
    return name


@pytest.mark.parametrize('missing', [False, True])
def test_unknown_creation_reconciles_before_replacement_and_publishes(inventory, monkeypatch, missing):
    from tests.test_async_settle_shadow_health_contention import outcome
    from tests.test_async_settle_shadow_health_stagger import advance, timed_owner
    from tests.test_async_settle_shadow_health_watchdog import accounting
    from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE

    db, clock, state = inventory
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    clock.now = mono[0]
    manager = unknown_creation(inventory)
    svc.store.database._sessions_manager = manager
    # Recovery and startup use the same boot; no identity may change mid-flight.
    manager_boot = manager.boot
    stale = seed_session(db, state, 'stale-retired', manager_boot)
    pending = set(state.live)
    foreign = {seed_session(db, state, 'other-boot', 'other'),
               seed_session(db, state, 'other-component', manager_boot, 'money')}
    state.events.clear()
    for offset in range(30):
        if offset:
            job = advance(svc, wall, mono, job.slot+1)
            clock.now = mono[0]
        assert not manager.prepare(clock.monotonic)
        svc.submit_publisher(job.slot, wall[0])
        trace = outcome(svc, job, 'missed')
        PUBLISHER_SLOT_TRACE(trace)
        assert trace['rpc_count'] == 0 and trace['claim_start_us'] is None
    assert state.creates == 1 and not state.listings and not state.deletes
    job = advance(svc, wall, mono, job.slot+1)
    clock.now = mono[0]
    state.missing = missing
    # Use the dedicated recovery executor, with no publisher RPC scope.
    with svc.session_executor as recovery:
        assert recovery.submit(svc.store.prepare_session).result(timeout=5)
    assert manager.ready and not manager.creation_unknown and manager.retired is None
    assert stale not in state.live and not (pending & state.live)
    assert state.creates == 2 and state.deletes == 2 and len(state.listings) == 1
    assert state.listings[0][0].filter == f'labels.tr_component:shadow-v2 AND labels.tr_boot:{manager_boot}'
    assert state.listings[0][1] == 5.
    assert state.create_timeouts == [5., 5.]
    assert state.live == foreign | {manager.session.name}
    # At every RPC boundary, the inventory is exactly held plus pending identities.
    for operation, live in state.events:
        owned = live-foreign
        assert owned <= pending if operation != 'create' else owned == {manager.session.name}
        assert len(owned) <= (2 if operation != 'create' else 1)
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, 'published')
    accounting(svc)
    counts = next(iter(svc.counters.values()))['publisher']
    assert counts['scheduled_slots'] == 31 and counts['missed_slots'] == 30
    assert counts['claim_attempts'] == counts['publications'] == 1
    assert counts['failures'] == counts['lost_claims'] == 0


@pytest.mark.parametrize('failure', ['list', 'delete', 'later-page'])
def test_unknown_reconciliation_failure_keeps_quarantine_and_retry_window(inventory, failure):
    db, clock, state = inventory
    manager = unknown_creation(inventory)
    seed_session(db, state, 'stale', manager.boot)
    state.list_error = failure == 'list'
    state.delete_error = failure == 'delete'
    if failure == 'later-page':
        state.page_size, state.list_fail_page = 1, '1'
    clock.now += 60
    assert not manager.prepare(clock.monotonic)
    if failure == 'later-page':
        assert len(state.listings) == 1 and not state.deletes
        clock.now += 60
        assert not manager.prepare(clock.monotonic)
    assert len(state.listings) == (2 if failure == 'later-page' else 1)
    assert manager.creation_unknown and not manager.ready
    assert state.creates == 1 and len(state.live) == 2
    assert state.deletes == (1 if failure == 'delete' else 0)
    calls = len(state.listings), state.creates, state.deletes
    clock.now += 59.999
    assert not manager.prepare(clock.monotonic)
    assert (len(state.listings), state.creates, state.deletes) == calls
    state.list_error = state.delete_error = False
    state.list_fail_page = None
    state.page_size = 1000
    clock.now += .001
    assert manager.prepare(clock.monotonic)
    assert manager.ready and len(state.live) == 1 and state.creates == 2


def test_repeated_unknown_replacements_never_accumulate_inventory(inventory):
    db, clock, state = inventory
    manager = unknown_creation(inventory)
    state.create_error = True
    for attempt in range(1, 6):
        clock.now += 60
        assert not manager.prepare(clock.monotonic)
        assert manager.creation_unknown and not manager.ready
        assert state.creates == attempt+1 and state.deletes == attempt
        assert len(state.listings) == attempt and len(state.live) == 1
    assert all(len(live) <= 1 for _, live in state.events)
    state.create_error = False
    clock.now += 60
    assert manager.prepare(clock.monotonic)
    assert len(state.live) == 1


@pytest.mark.parametrize('slow', ['list', 'delete'])
def test_reconciliation_and_creation_share_five_second_scope(inventory, slow):
    _, clock, state = inventory
    manager = unknown_creation(inventory)
    if slow == 'list':
        state.list_duration = 5.
    else:
        state.delete_duration = 5.
    clock.now += 60
    started = clock.now
    assert not manager.prepare(clock.monotonic)
    assert len(state.listings) == 1 and clock.now-started == 5.
    assert manager.creation_unknown and state.creates == 1
    state.list_duration = state.delete_duration = 0.
    clock.now = started+60
    assert manager.prepare(clock.monotonic)


def test_reconciliation_pages_wait_for_retry_window_and_deletes_share_budget(inventory):
    db, clock, state = inventory
    manager = unknown_creation(inventory)
    seed_session(db, state, 'stale', manager.boot)
    state.page_size = 1
    state.list_duration = .5
    state.delete_duration = .25
    clock.now += 60
    started = clock.now
    assert not manager.prepare(clock.monotonic)
    assert manager.creation_unknown and state.creates == 1 and not state.deletes
    clock.now = started+59.999
    assert not manager.prepare(clock.monotonic)
    assert len(state.listings) == 1
    clock.now = started+60
    assert manager.prepare(clock.monotonic)
    assert [timeout for _, timeout, _ in state.listings] == [5., 5.]
    assert state.listings[1][2]-state.listings[0][2] == 60
    assert [request.page_token for request, _, _ in state.listings] == ['', '1']
    assert state.create_timeouts == [5., 4.]
    assert state.deletes == 2 and len(state.live) == 1


def test_reconciliation_preserves_held_session(inventory):
    db, clock, state = inventory
    manager = db._sessions_manager
    manager.boot = 'held-boot'
    assert manager.prepare(clock.monotonic)
    held = manager.session
    seed_session(db, state, 'unknown', manager.boot)
    manager.creation_unknown = True
    clock.now += 60
    assert manager.prepare(clock.monotonic)
    assert manager.session is held and manager.ready
    assert state.live == {held.name} and state.creates == state.deletes == 1
    assert len(state.listings) == 1


@pytest.mark.asyncio
async def test_recovery_worker_waits_for_first_window_after_unknown_startup(inventory, monkeypatch):
    import asyncio

    from tests.test_async_settle_shadow_health_watchdog import owner
    from trusted_router.services import async_settle_shadow_health as shell
    from trusted_router.storage_gcp_shadow_observation import ObservationStore

    db, clock, state = inventory
    svc, wall, _, _ = owner()
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    monkeypatch.setattr(shell, 'ObservationStore', lambda database: store)
    app = SimpleNamespace(state=SimpleNamespace(), on_event=lambda name: lambda fn: fn)
    started = clock.now
    state.create_error = True
    shell.install(app, svc.settings, SimpleNamespace(_database=db))
    installed = app.state.async_settle_shadow_observation
    manager = db._sessions_manager
    assert not manager.ready and manager.creation_unknown
    assert state.create_timeouts == [5.]
    installed.next_flush = float('inf')
    monkeypatch.setattr(installed, 'tick', lambda now: ())

    async def one_turn(seconds):
        if installed.session_job is not None:
            await asyncio.wrap_future(installed.session_job)
        installed.stopped.set()

    monkeypatch.setattr(shell.asyncio, 'sleep', one_turn)
    with installed.session_executor:
        state.create_error = False
        clock.now = started+59.999
        await installed.run()
        assert installed.session_job is None and not state.listings and state.creates == 1
        installed.stopped.clear()
        clock.now = started+60
        await installed.run()
        assert installed.session_job.result() and manager.ready
        assert len(state.listings) == 1 and state.deletes == 1 and state.creates == 2
        assert len(state.live) == 1


def test_quarantine_between_readiness_and_checkout(inventory, monkeypatch):
    """A recovery owning the lane must still produce a zero-RPC missed cycle."""
    from tests.test_async_settle_shadow_health_contention import outcome
    from tests.test_async_settle_shadow_health_stagger import timed_owner
    from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE
    from trusted_router.services import async_settle_shadow_health as shell
    from trusted_router.storage_gcp_shadow_observation import ObservationStore
    db, clock, server = inventory
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    clock.now = mono[0]-60
    manager = db._sessions_manager
    assert manager.prepare(clock.monotonic)
    clock.now = mono[0]
    svc.store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    svc.incumbent_slot = job.slot-1
    # Mirror the production owned Database transaction checkout around the real SDK Session.
    def transaction(callback, **kwargs):
        session = manager.get_session(None)
        try:
            return session.run_in_transaction(callback, **kwargs)
        finally:
            manager.put_session(session)
    db.run_in_transaction = transaction
    recovery_entered, release = threading.Event(), threading.Event()
    real_create = db.spanner_api.create_session
    def held_create(**kwargs):
        recovery_entered.set()
        assert release.wait(3)
        return real_create(**kwargs)
    db.spanner_api.create_session = held_create
    checks = 0
    real_ready = svc.store.session_ready
    with ThreadPoolExecutor(max_workers=1) as background:
        def readiness():
            nonlocal checks
            checks += 1
            sampled = real_ready()
            if checks == 2:
                # The evidence lane fails after readiness was sampled; recovery
                # acquires the checkout lane before the publisher does.
                background.submit(failed_read, db, clock).result(timeout=2)
                background.submit(manager.prepare, clock.monotonic)
                assert recovery_entered.wait(2)
            return sampled
        monkeypatch.setattr(svc.store, 'session_ready', readiness)
        try:
            reply = shell.publish_cycle(svc.store, svc.journal.boot,
                svc.settings.effective_serving_region, svc.settings.source_revision,
                job, wall[0], svc, True, True)
            PUBLISHER_SLOT_TRACE(reply.trace)
            assert reply.trace['rpc_count'] == 0
            assert reply.trace['outcome'] == 'missed'
            assert reply.trace['claim_start_us'] is None
            assert reply.trace['claim_at_ns'] is None
            assert reply.trace['cycle_us'] is None
            svc.publisher_reply(reply)
            outcome(svc, job, 'missed')
        finally:
            release.set()



@pytest.mark.parametrize('after_rpc', [False, True])
def test_checkout_timeout_is_missed_only_before_first_rpc(inventory, monkeypatch, after_rpc):
    from tests.test_async_settle_shadow_health_contention import outcome
    from tests.test_async_settle_shadow_health_stagger import timed_owner
    from trusted_router.async_settle_shadow_v2_a1_health import PUBLISHER_SLOT_TRACE
    from trusted_router.storage_gcp_shadow_observation import ObservationStore

    db, clock, _ = inventory
    svc, wall, mono, job, _ = timed_owner(monkeypatch)
    clock.now = mono[0]
    manager = db._sessions_manager
    assert manager.prepare(clock.monotonic)
    svc.store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    svc.incumbent_slot = job.slot-1
    def blocked_claim(*args):
        if after_rpc:
            # A completed read is recorded even when its elapsed time is zero.
            db.read_duration = 0
            list(db.spanner_api.execute_streaming_sql(request=SimpleNamespace(sql='SELECT 1')))
        with manager.lock:
            manager.get_session(None)
    monkeypatch.setattr(svc.store, 'claim', blocked_claim)
    svc.submit_publisher(job.slot, wall[0])
    trace = outcome(svc, job, 'failed' if after_rpc else 'missed')
    PUBLISHER_SLOT_TRACE(trace)
    assert trace['rpc_count'] == int(after_rpc)
    assert (trace['claim_start_us'] is not None) is after_rpc
    assert (trace['cycle_us'] is not None) is after_rpc
    assert trace['claim_at_ns'] is None
    assert manager.ready and not manager.lock.locked()
