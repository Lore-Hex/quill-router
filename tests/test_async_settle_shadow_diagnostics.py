from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import DeadlineExceeded

from trusted_router.async_settle_shadow_evidence import CONTROL, COUNTER, SAMPLE
from trusted_router.storage_gcp_async_settle_shadow import (
    RETENTION_FENCE,
    EvidenceStore,
    day_statement,
    finalization_statement,
    point_statement,
)


@pytest.mark.parametrize('statement, operation', [
    (point_statement(COUNTER, 'private-customer-id'), 'counter_read'),
    (point_statement(SAMPLE, 'private-customer-id'), 'sample_read'),
    (point_statement(CONTROL, RETENTION_FENCE), 'retention_fence_read'),
    (point_statement(CONTROL, 'private-customer-id'), 'control_read'),
    (finalization_statement('private-customer-id'), 'booking_read'),
    (day_statement(COUNTER, '2026-10-06'), 'day_read'),
])
@pytest.mark.parametrize('during_iteration', [False, True])
def test_shadow_failed_read_has_fixed_tag_and_redacted_operation(
        shadow_deadline_clock, caplog, statement, operation, during_iteration):
    calls = []
    error = DeadlineExceeded('private prompt, api key and customer identifiers')

    def iterate():
        yield ('unused',)
        raise error

    def execute(sql, **kwargs):
        calls.append(kwargs)
        if during_iteration:
            return iterate()
        raise error

    reader = SimpleNamespace(execute_sql=execute)
    db = SimpleNamespace(run_in_transaction=lambda callback, **kwargs: callback(reader))
    store = EvidenceStore(db)
    deadline = shadow_deadline_clock.monotonic() + 2
    with pytest.raises(DeadlineExceeded) as caught:
        store.transaction(lambda tx: store.query(tx, statement, deadline), deadline)
    assert caught.value is error
    assert len(calls) == 1
    assert calls[0]['request_options'] == {
        'priority': 'PRIORITY_LOW', 'request_tag': 'tr_shadow_' + operation,
    }
    assert calls[0]['retry'] is None
    assert 0 < calls[0]['timeout'] <= 1
    assert f'operation={operation}' in caplog.text
    assert 'stage=callback reason=deadline_exceeded' in caplog.text
    assert 'private' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_shadow_diagnostics_do_not_leak_operation_between_transactions(
        shadow_deadline_clock, caplog):
    error = DeadlineExceeded('secret')

    def fail(*args, **kwargs):
        raise error

    reader = SimpleNamespace(execute_sql=fail)
    db = SimpleNamespace(run_in_transaction=lambda callback, **kwargs: callback(reader))
    deadline = shadow_deadline_clock.monotonic() + 2
    with pytest.raises(DeadlineExceeded):
        EvidenceStore(db).transaction(
            lambda tx: EvidenceStore.query(tx, point_statement(COUNTER, 'private'), deadline), deadline)
    with pytest.raises(DeadlineExceeded):
        EvidenceStore(db).transaction(fail, deadline)
    assert 'operation=counter_read' in caplog.records[0].getMessage()
    assert 'operation=unknown' in caplog.records[1].getMessage()


def test_shadow_successful_read_is_not_blamed_for_later_callback_failure(
        shadow_deadline_clock, caplog):
    reader = SimpleNamespace(execute_sql=lambda *args, **kwargs: [])
    db = SimpleNamespace(run_in_transaction=lambda callback, **kwargs: callback(reader))
    store = EvidenceStore(db)
    deadline = shadow_deadline_clock.monotonic() + 2

    def callback(tx):
        store.query(tx, point_statement(COUNTER, 'private'), deadline)
        raise ValueError('secret')

    with pytest.raises(ValueError):
        store.transaction(callback, deadline)
    assert 'operation=unknown' in caplog.text
    assert 'operation=counter_read' not in caplog.text


def test_shadow_diagnostic_operations_are_thread_isolated(shadow_deadline_clock, caplog):
    barrier = Barrier(2)
    deadline = shadow_deadline_clock.monotonic() + 2

    def attempt(kind):
        def execute(*args, **kwargs):
            barrier.wait(timeout=5)
            raise DeadlineExceeded('secret')
        reader = SimpleNamespace(execute_sql=execute)
        db = SimpleNamespace(run_in_transaction=lambda callback, **kwargs: callback(reader))
        store = EvidenceStore(db)
        with pytest.raises(DeadlineExceeded):
            store.transaction(lambda tx: store.query(tx, point_statement(kind, 'private'), deadline), deadline)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, [COUNTER, SAMPLE]))
    messages = [record.getMessage() for record in caplog.records]
    assert sum('operation=counter_read' in message for message in messages) == 1
    assert sum('operation=sample_read' in message for message in messages) == 1
