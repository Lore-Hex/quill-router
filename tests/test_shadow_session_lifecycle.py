"""Server-side session inventory must stay bounded across native SDK faults."""
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import DeadlineExceeded, NotFound
from google.cloud.spanner_v1.types import Session as SessionPB

from tests.test_async_settle_shadow_sdk_retry import sdk_database
from trusted_router.storage_gcp_shadow_io import Sessions, configure, rpc_scope


@pytest.fixture
def inventory(monkeypatch, shadow_deadline_clock):
    db, clock, _ = sdk_database.__wrapped__(monkeypatch, shadow_deadline_clock)
    db.database_role = None
    state = SimpleNamespace(live=set(), creates=0, deletes=0, create_error=False,
                            delete_error=False, missing=False, late_create=False,
                            stream_error=False)

    def create(**kwargs):
        assert kwargs['retry'] is None
        state.creates += 1
        name = db.name + '/sessions/' + str(state.creates)
        state.live.add(name)
        if state.create_error:
            raise DeadlineExceeded('response lost after allocation')
        if state.late_create:
            clock.now += kwargs['timeout']
        return SessionPB(name=name)

    def delete(**kwargs):
        assert kwargs['retry'] is None
        state.deletes += 1
        if state.delete_error:
            raise DeadlineExceeded('cleanup unavailable')
        state.live.discard(kwargs['name'])
        if state.missing:
            raise NotFound('session already gone')

    db.spanner_api.create_session = create
    db.spanner_api.delete_session = delete
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
            assert session.labels == {"tr_component": "shadow-v2"}
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
