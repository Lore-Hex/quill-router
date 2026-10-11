from __future__ import annotations

import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as errors

from trusted_router import storage_gcp_async_admission as admission
from trusted_router import storage_gcp_io as io


@pytest.fixture
def clock(monkeypatch, shadow_deadline_clock):
    monkeypatch.setattr(admission, 'time', shadow_deadline_clock, raising=False)
    monkeypatch.setattr(admission, '_read_failure_logs', {}, raising=False)
    return shadow_deadline_clock


def read(db, operation):
    return (admission.read_admission(db, 'private-workspace') if operation == 'admission'
            else admission.read_health(db))


@pytest.mark.parametrize('operation', ['admission', 'drain_health'])
@pytest.mark.parametrize('during_iteration', [False, True])
@pytest.mark.parametrize('error_type,reason', [
    (errors.DeadlineExceeded, 'deadline_exceeded'),
    (errors.Cancelled, 'cancelled'),
    (errors.Aborted, 'aborted'),
    (errors.ServiceUnavailable, 'unavailable'),
    (errors.InternalServerError, 'internal'),
    (errors.PermissionDenied, 'permission_denied'),
    (errors.ResourceExhausted, 'resource_exhausted'),
    (RuntimeError, 'other'),
])
def test_failed_reads_keep_exception_and_log_only_fixed_metadata(
        clock, caplog, operation, during_iteration, error_type, reason):
    calls = []
    error = error_type('private prompt, key, SQL parameters and user email')

    def rows():
        clock.now += .04
        raise error
        yield  # noqa: B018

    def execute(sql, **kwargs):
        calls.append(kwargs)
        if during_iteration:
            return rows()
        clock.now += .04
        raise error

    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=execute)))
    with caplog.at_level(logging.WARNING), pytest.raises(error_type) as caught:
        read(db, operation)
    assert caught.value is error
    assert calls[0]['request_options'] == {
        'priority': 'PRIORITY_LOW', 'request_tag': 'tr_async_' + operation,
    }
    assert calls[0]['retry'] is None and calls[0]['timeout'] == .5
    assert [record.getMessage() for record in caplog.records] == [
        f'async admission read failed operation={operation} reason={reason} elapsed_ms=40 failures=1',
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    assert 'private' not in caplog.text
    assert io._SPANNER_RPC_DEADLINE.get() is None


def test_failure_logs_are_bounded_and_do_not_hide_different_operations_or_causes(clock, caplog):
    error = errors.DeadlineExceeded('private')

    def execute(*args, **kwargs):
        raise error

    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=execute)))
    for _ in range(5):
        with pytest.raises(errors.DeadlineExceeded):
            read(db, 'admission')
    with pytest.raises(errors.DeadlineExceeded):
        read(db, 'drain_health')
    error = errors.PermissionDenied('private')
    with pytest.raises(errors.PermissionDenied):
        read(db, 'admission')
    assert len(caplog.records) == 3
    clock.now += 60
    error = errors.DeadlineExceeded('private')
    with pytest.raises(errors.DeadlineExceeded):
        read(db, 'admission')
    assert len(caplog.records) == 4
    assert caplog.records[-1].getMessage().endswith('failures=5')


@pytest.mark.parametrize('operation', ['admission', 'drain_health'])
def test_snapshot_acquisition_failure_is_diagnosed(clock, caplog, operation):
    def snapshot():
        clock.now += .1
        raise errors.ServiceUnavailable('private credentials')

    with pytest.raises(errors.ServiceUnavailable):
        read(SimpleNamespace(snapshot=snapshot), operation)
    assert f'operation={operation} reason=unavailable elapsed_ms=100 failures=1' in caplog.text
    assert 'private' not in caplog.text


@pytest.mark.parametrize('operation', ['admission', 'drain_health'])
def test_successful_read_is_quiet_and_restores_outer_budget(clock, caplog, operation):
    calls = []

    def execute(sql, **kwargs):
        calls.append(kwargs)
        return [[0, None, 2]] if operation == 'admission' else []

    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=execute)))
    with io.spanner_rpc_deadline(clock.now + .2):
        with caplog.at_level(logging.DEBUG):
            value = read(db, operation)
        assert io.remaining_rpc_budget(10) == pytest.approx(.2)
    assert value == (admission.Admission(0, 2) if operation == 'admission' else None)
    assert calls[0]['timeout'] == pytest.approx(.2)
    assert caplog.records == []
    assert io._SPANNER_RPC_DEADLINE.get() is None


def test_expired_caller_budget_does_not_issue_a_read(clock, caplog):
    db = SimpleNamespace(snapshot=lambda: pytest.fail('read after deadline'))
    with io.spanner_rpc_deadline(clock.now + .1):
        clock.now += .2
        with pytest.raises(errors.DeadlineExceeded):
            read(db, 'admission')
    assert 'reason=deadline_exceeded' in caplog.text


def test_closed_admission_validation_still_rejects_without_rpc_error(clock, caplog):
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(
        execute_sql=lambda *args, **kwargs: [[1001, 0, 2]])))
    with pytest.raises(ValueError, match='admission unavailable'):
        read(db, 'admission')
    assert caplog.records == []
