"""Real Service/transport controls for fleet stop and its independent retry lane."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import Aborted, Cancelled, DeadlineExceeded

from scripts.async_settle.shadow_v2_stop import execute
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_expiry import owner, pins
from trusted_router import async_settle_shadow_lifecycle as lifecycle
from trusted_router.async_settle_shadow_journal import CONTROL_KIND
from trusted_router.storage_gcp_shadow_io import configure, rpc_scope


def stop(store):
    return execute(store, action='stop', actor='reviewed-operator', reason='API incident', incident='incident-123')


def cleanup(svc):
    for executor in (svc.session_executor, svc.publisher_executor, svc.evidence_executor):
        executor.shutdown(wait=True, cancel_futures=True)


def test_cli_one_row_first_stop_wins_and_clear(monkeypatch):
    svc, _ = owner(pins())
    try:
        store = svc.store
        transaction = Mock(wraps=store.transaction)
        write = Mock(wraps=store.write)
        monkeypatch.setattr(store, "transaction", transaction)
        monkeypatch.setattr(store, "write", write)
        first = stop(store)
        assert transaction.call_count == 1 and write.call_count == 1
        assert first['row']['source'] == 'operator'
        assert first['row']['incident'] == 'incident-123'
        assert first['row']['actor'] == 'reviewed-operator'
        assert list(store.database.rows) == [(CONTROL_KIND, 'stop-v2')]
        second = execute(store, action='stop', actor='different', reason='retry', incident='different')
        assert first['row'] == second['row']
        assert transaction.call_count == 2 and write.call_count == 1
        assert len(store.database.rows) == 1
        assert execute(store, action='clear', actor='reviewer', reason='incident resolved')['row'] is None
        assert not store.database.rows
        assert execute(store, action='clear', actor='reviewer', reason='retry')['row'] is None
    finally:
        cleanup(svc)


@pytest.mark.asyncio
async def test_marker_stops_real_service_within_cadence_and_closes_opt_out():
    svc, wall = owner(pins())
    try:
        assert svc.runtime_stop.poll()
        assert any(isinstance(e, lifecycle.StartPublisherCycle) for e in svc.tick(wall[0]))
        generation = svc.generation
        svc.journal.grant(lifecycle.day_name(wall[0]), 100)
        assert svc.port.reserve(wall[0]) is not None
        stop(svc.store)
        wall[0] += 3_999_999
        assert svc.runtime_stop.poll() and svc.policy_enabled()
        wall[0] += 1
        assert not svc.runtime_stop.poll()
        assert not svc.settings.async_settle_shadow_live(svc.store.wall())
        assert svc.port.reserve(wall[0]) is None
        assert not svc.contender() and not svc.observer.enabled()
        assert svc.generation == generation+1
        assert not any(isinstance(e, lifecycle.StartPublisherCycle) for e in svc.tick(wall[0]))
        # Preserve the real run closure dispatch while avoiding unrelated final
        # receipt closure for the deliberately outstanding reservation above.
        reasons = []
        async def close(reason, **kwargs):
            reasons.append(reason)
        svc.close = close
        await svc.run()
        assert reasons == ['opt_out']
        execute(svc.store, action='clear', actor='reviewer', reason='resolved')
        assert not svc.policy_enabled()  # clear cannot resurrect this process
    finally:
        await svc.runtime_stop.close()
        cleanup(svc)


def test_startup_marker_installs_nothing(monkeypatch):
    from trusted_router.services import async_settle_shadow_health as shell
    svc, _ = owner(pins())
    try:
        stop(svc.store)
        monkeypatch.setattr(shell, 'ObservationStore', lambda _: svc.store)
        app = Mock()
        shell.install(app, pins(), SimpleNamespace(_database=object()))
        assert not app.mock_calls
    finally:
        cleanup(svc)


def transport(svc, method, error, *, stream=False):
    def fail(*args, **kwargs):
        assert kwargs['retry'] is None and 0 < kwargs['timeout'] <= 1
        if stream:
            def responses():
                yield object()
                raise error('synthetic')
            return responses()
        raise error('synthetic')
    api = SimpleNamespace(**{method: fail})
    db = SimpleNamespace(spanner_api=api, _shadow_v2_failure=svc.runtime_stop.outcome)
    configure(db)
    with rpc_scope(svc.store.clock()+1, 1, svc.store.clock), pytest.raises(RuntimeError):
        result = getattr(api, method)()
        if stream:
            list(result)


@pytest.mark.parametrize('method,stream', [('commit', False), ('execute_streaming_sql', False), ('execute_streaming_sql', True)])
def test_transport_breaker_counts_alert_outcomes_and_never_aborted(method, stream):
    svc, wall = owner(pins())
    try:
        for _ in range(5):
            transport(svc, method, Aborted, stream=stream)
        assert svc.policy_enabled() and svc.runtime_stop.tripped is None
        transport(svc, method, DeadlineExceeded, stream=stream)
        assert svc.policy_enabled()
        wall[0] += 1_000_000
        transport(svc, method, Cancelled, stream=stream)
        assert not svc.policy_enabled() and svc.revocation_epoch == 1
        svc.tick(wall[0])
        assert svc.counters[lifecycle.day_name(wall[0])]['diagnostics']['runtime_breaker_trips'] == 1
        svc.runtime_stop.retry()
        marker = svc.store.read_runtime_stop().value()
        assert marker['source'] == 'breaker' and marker['failure_count'] == 2
        assert marker['window_seconds'] == 120 and marker['incident'].startswith('breaker:')
    finally:
        cleanup(svc)


def test_sliding_window_and_failed_marker_write_backoff(monkeypatch):
    svc, wall = owner(pins())
    try:
        breaker = svc.runtime_stop
        breaker.outcome('commit', 'DeadlineExceeded')
        wall[0] += 120_000_000
        breaker.outcome('execute_streaming_sql', 'Cancelled')
        assert svc.policy_enabled()  # first failure aged out exactly at boundary
        breaker.outcome('commit', 'Cancelled')
        assert not svc.policy_enabled()
        original = svc.store.runtime_stop
        def failed_write(*args, **kwargs):
            wall[0] += 2_000_000  # A full timeout still needs a delay after failure.
            raise RuntimeError('unavailable')
        write = Mock(side_effect=failed_write)
        monkeypatch.setattr(svc.store, 'runtime_stop', write)
        breaker.retry()
        breaker.retry()
        assert write.call_count == 1 and not breaker.persisted and not svc.policy_enabled()
        wall[0] += 1_000_000
        breaker.retry()
        assert write.call_count == 2
        monkeypatch.setattr(svc.store, 'runtime_stop', original)
        wall[0] += 2_000_000
        breaker.retry()
        assert breaker.persisted and not svc.policy_enabled()
    finally:
        cleanup(svc)


@pytest.mark.asyncio
async def test_retry_task_survives_optout(monkeypatch):
    svc, _ = owner(pins())
    try:
        svc.runtime_stop.outcome('commit', 'DeadlineExceeded')
        svc.runtime_stop.outcome('commit', 'Cancelled')
        async def close(*args, **kwargs):
            pass
        monkeypatch.setattr(svc, 'close', close)
        await svc.run()
        for _ in range(100):
            if svc.runtime_stop.persisted:
                break
            await asyncio.sleep(.01)
        assert svc.runtime_stop.persisted
    finally:
        await svc.runtime_stop.close()
        cleanup(svc)


def test_create_app_and_cohort_gate_are_dormant_with_marker(monkeypatch):
    from tests.test_async_settle_shadow_cohort import Port, additions, inputs, signer
    from trusted_router import main, storage
    from trusted_router.services import async_settle, async_settle_shadow_health
    from trusted_router.services.async_settle_shadow_cohort import authorize
    svc, _ = owner(pins())
    try:
        stop(svc.store)
        cfg = pins()
        monkeypatch.setattr(storage, 'typed_billing_store', lambda _: SimpleNamespace(_database=object()))
        monkeypatch.setattr(async_settle, 'load_runtime', lambda *args: None)
        monkeypatch.setattr(async_settle_shadow_health, 'ObservationStore', lambda _: svc.store)
        app = main.create_app(cfg, configure_store_arg=False, init_observability=False)
        assert not hasattr(app.state, 'async_settle_shadow_observation')
        assert not hasattr(app.state, 'async_settle_shadow_cohort')
        port = Port()
        assert not authorize(cfg, ('shadow-v2',), port, inputs(), additions(), signer())
        assert port.calls == []
    finally:
        cleanup(svc)


def test_failed_control_read_fails_closed_and_has_no_catchup_reads(monkeypatch):
    svc, wall = owner(pins())
    try:
        read = Mock(side_effect=RuntimeError('unavailable'))
        monkeypatch.setattr(svc.store, 'read_runtime_stop', read)
        assert not svc.runtime_stop.poll() and not svc.policy_enabled()
        for _ in range(100):
            svc.runtime_stop.poll()
        assert read.call_count == 1
        wall[0] += 4_000_000
        svc.runtime_stop.poll()
        assert read.call_count == 2 and not svc.policy_enabled()
    finally:
        cleanup(svc)


@pytest.mark.parametrize('clear', ['clear', '--clear'])
def test_cli_argument_path_prints_result_without_other_mutations(monkeypatch, capsys, clear):
    import json

    from google.cloud import spanner_v1

    from scripts.async_settle import shadow_v2_stop as cli
    svc, _ = owner(pins())
    try:
        client = Mock()
        monkeypatch.setattr(spanner_v1, 'Client', client)
        monkeypatch.setattr(cli, 'ObservationStore', lambda _: svc.store)
        prefix = ['--project', 'p', '--instance', 'i', '--database', 'd', '--actor', 'reviewer']
        assert cli.main([*prefix, 'stop', '--reason', 'API incident', '--incident', '123']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['row']['incident'] == '123' and result['row']['actor'] == 'reviewer'
        assert list(svc.store.database.rows) == [(CONTROL_KIND, 'stop-v2')]
        assert cli.main([*prefix, clear, '--reason', 'resolved']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['row'] is None and result['reason'] == 'resolved'
        assert not svc.store.database.rows
    finally:
        cleanup(svc)


@pytest.mark.asyncio
async def test_controller_shutdown_ends_polling_but_optout_keeps_pending_write(monkeypatch):
    svc, _ = owner(pins())
    try:
        svc.runtime_stop.outcome('commit', 'DeadlineExceeded')
        svc.runtime_stop.outcome('execute_streaming_sql', 'Cancelled')
        monkeypatch.setattr(svc.store, 'runtime_stop', Mock(side_effect=RuntimeError('unavailable')))
        svc.runtime_stop.start(lambda: svc.policy_enabled() and not svc.stopped.is_set())
        await svc.close('opt_out')
        assert not svc.policy_enabled() and not svc.runtime_stop.persisted
        assert not svc.runtime_stop.task.done()
        await svc.runtime_stop.close()
        assert svc.runtime_stop.task.done()
    finally:
        await svc.runtime_stop.close()
        cleanup(svc)


@pytest.mark.asyncio
async def test_breaker_during_optout_drain_still_writes_marker():
    svc, _ = owner(pins())
    try:
        svc.latch_stop()
        svc.runtime_stop.start(svc.policy_enabled, lambda: not svc.close_finished)
        await asyncio.sleep(.06)
        assert not svc.runtime_stop.task.done()
        def drain():
            yield lambda: transport(svc, 'commit', DeadlineExceeded)
            yield lambda: transport(svc, 'execute_streaming_sql', Cancelled, stream=True)
        await svc.drive_async(drain())
        assert svc.counters[lifecycle.day_name(int(svc.store.wall()*1e6))]['diagnostics']['runtime_breaker_trips'] == 1
        for _ in range(100):
            if svc.runtime_stop.persisted:
                break
            await asyncio.sleep(.01)
        assert svc.runtime_stop.persisted and not svc.policy_enabled()
    finally:
        await svc.runtime_stop.close()
        cleanup(svc)
