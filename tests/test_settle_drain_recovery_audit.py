"""Watchdog claim witnesses and frozen-main claiming/health-gate equivalence."""
from __future__ import annotations

import copy
import logging
import queue
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.test_settle_drain_worker_audit import HASH, LEASE, NEW, OLD, PRIOR, REF
from tests.test_settle_outbox_drain import _bare_authorization, _row
from trusted_router import storage_gcp_async_admission as health
from trusted_router import storage_gcp_settle_outbox as storage
from trusted_router.services import settle_outbox_audit as audit
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services import settle_outbox_worker as worker

# Exact _recover_async from main b20084cb40f9bd03e69b402cd38d6a7ebbc02007.
# Execute its unchanged claiming/health control flow against the same fake inputs.
_BASELINE_RECOVERY = r'''
def _recover_async(outbox: SpannerSettleOutbox, *, limit: int, deadline: float,
                   settings: Settings | None) -> Iterator[SettleOutboxRow]:
    """Last-resort watchdog: short leases, immediately consumed, never health writes."""
    from trusted_router.services.async_settle import CACHE_SECONDS
    from trusted_router.storage_gcp_async_admission import read_health

    protection = outbox._async_fence
    lease = min(30, settings.settle_outbox_lease_seconds if settings is not None else 30)
    health_checked = False
    claimed = blocked = 0
    # One sweep when empty. Under backlog repeat bounded sweeps within this
    # pass's remaining budget; a yielded row is applied before the next claim.
    try:
        while claimed < limit:
            sweep_claimed = 0
            for shard in range(16):
                if _monotonic() >= deadline or claimed >= limit:
                    return
                try:
                    candidates = outbox.due_shard(shard=shard, async_only=True)
                    if not candidates:
                        continue
                    if not protection:
                        blocked += len(candidates)
                        continue
                    if not health_checked:
                        health_checked = True
                        try:
                            health = read_health(outbox._database)
                        except Exception as exc:
                            logger.warning("async_recovery.health_read_failed error_class=%s", type(exc).__name__)
                            health = None
                        now = time.time()
                        if health is not None and all(
                            0 <= now - health[field] < CACHE_SECONDS
                            for field in ("observed_at", "worker_heartbeat")
                        ):
                            return
                    if _monotonic() >= deadline:
                        return
                    rows = outbox.claim_candidates(candidates, lease_seconds=lease)
                except Exception as exc:
                    logger.warning("async_recovery.claim_failed error_class=%s", type(exc).__name__)
                    continue
                for row in rows:
                    claimed += 1
                    sweep_claimed += 1
                    yield row
            if not sweep_claimed:
                return
    finally:
        if blocked:
            logger.warning("async_recovery.protection_disabled due_count_at_least=%s", blocked)
'''


def candidate(*, prior=True, missing_hash=False):
    row = _row(_bare_authorization('SENTINEL_AUTHORIZATION'))
    row.workspace_id = 'SENTINEL_WORKSPACE'
    row.reservation_id = 'SENTINEL_RESERVATION'
    row.model_id = 'SENTINEL_MODEL'
    row.selected_endpoint_id = 'SENTINEL_PROVIDER'
    row.selected_usage_type = 'SENTINEL_USAGE'
    row.settle_body = 'SENTINEL_KEY SENTINEL_GENERATION SENTINEL_USAGE'
    row.actual_cost_micro = 987654321
    row.payload_hash = None if missing_hash else HASH
    row.lease_owner, row.leased_until = (OLD, PRIOR) if prior else (None, None)
    return row


def box_for(monkeypatch, rows, *, winner=0, protection=True):
    monkeypatch.setattr(storage.uuid, 'uuid4', lambda: SimpleNamespace(hex='b' * 32))
    monkeypatch.setattr(storage, '_iso_after_seconds', lambda seconds: LEASE)
    monkeypatch.setattr(drain, '_monotonic', lambda: 0.)
    monkeypatch.setattr(health, 'read_health', lambda database: None)
    class Box(storage.SpannerSettleOutbox):
        def __init__(self):
            super().__init__(None, None, async_fence=protection)
            self.trace = []

        def due_shard(self, *, shard, async_only=False):
            self.trace.append(('due', shard, async_only))
            return rows if shard == 0 else []

        def _claim_one(self, row, *, owner, lease_until):
            self.trace.append(('claim', row.authorization_id, owner, lease_until))
            if winner == 'exception':
                raise TimeoutError('SENTINEL_AUTHORIZATION SENTINEL_KEY')
            return winner is not None and row is rows[winner]

        def claim_candidates(self, candidates, *, lease_seconds):
            self.trace.append(('lease', lease_seconds))
            return super().claim_candidates(candidates, lease_seconds=lease_seconds)
    return Box()


@pytest.mark.parametrize('prior', [False, True])
@pytest.mark.parametrize('missing_hash', [False, True])
def test_exact_watchdog_claim_reaches_router_outputs(monkeypatch, caplog, capsys, prior, missing_hash):
    from trusted_router import main
    from trusted_router.axiom_config import _AxiomNoiseFilter, _DroppingQueueHandler

    app_logger = logging.getLogger('trusted_router')
    for name in ('handlers', 'level', 'disabled', 'propagate'):
        monkeypatch.setattr(app_logger, name, [] if name == 'handlers' else getattr(app_logger, name))
    main._configure_application_logging()
    row = candidate(prior=prior, missing_hash=missing_hash)
    box = box_for(monkeypatch, [row])
    ref = 'none' if missing_hash else REF
    expected = (f'async_recovery.claim ref={ref} prior_owner={OLD if prior else "none"} '
                f'prior_leased_until={PRIOR if prior else "none"} owner={NEW} leased_until={LEASE}')
    with caplog.at_level(logging.INFO):
        recovery = drain._recover_async(box, limit=1, deadline=10., settings=None)
        assert next(recovery) is row
        # The witness must exist BEFORE the consumer can apply the yielded row.
        assert caplog.messages == [expected]
        assert list(recovery) == []
    assert capsys.readouterr().err == f'INFO {drain.__name__} {expected}\n'
    record, = caplog.records
    assert record.name == drain.__name__
    assert _AxiomNoiseFilter().filter(record)
    prepared = _DroppingQueueHandler(queue.Queue()).prepare(record)
    assert prepared.getMessage() == record.getMessage() == expected
    assert prepared.args is None and prepared.exc_info is None
    output = caplog.text + repr(prepared.__dict__)
    assert 'SENTINEL_' not in output and '987654321' not in output and HASH not in output
    for forbidden in ('authorization_id', 'workspace', 'generation', 'model', 'provider', 'usage', 'cost', 'key='):
        assert forbidden not in output


@pytest.mark.parametrize('winner', [0, 1, None, 'exception'])
@pytest.mark.parametrize('gate', ['missing', 'fresh', 'stale', 'error', 'protection_off'])
def test_watchdog_claiming_matches_frozen_main(monkeypatch, caplog, winner, gate):
    # Both implementations must receive identical inputs even across a second boundary.
    original = [candidate(prior=False), candidate()]
    original[0].payload_hash = 'f' * 64
    outputs = []
    for baseline in (True, False):
        rows = copy.deepcopy(original)
        box = box_for(monkeypatch, rows, winner=winner, protection=gate != 'protection_off')
        health_reads = []
        def read(database, health_reads=health_reads):
            health_reads.append(database)
            if gate == 'error':
                raise TimeoutError('SENTINEL_KEY')
            return (dict(observed_at=100., worker_heartbeat=100.) if gate == 'fresh' else
                    dict(observed_at=0., worker_heartbeat=0.) if gate == 'stale' else None)
        monkeypatch.setattr(health, 'read_health', read)
        monkeypatch.setattr(drain, 'time', SimpleNamespace(time=lambda: 100.))
        namespace = dict(vars(drain))
        exec(compile(_BASELINE_RECOVERY, '<b20084cb4 _recover_async>', 'exec'), namespace)  # noqa: S102 - frozen main
        recover = namespace['_recover_async'] if baseline else drain._recover_async
        caplog.clear()
        with caplog.at_level(logging.INFO):
            result = list(recover(box, limit=1, deadline=10., settings=None))
        outputs.append(copy.deepcopy((box.trace, rows, result, health_reads)))
        claims = [m for m in caplog.messages if m.startswith(('async_recovery.claim ', 'async_drain.claim '))]
        if baseline or not result:
            assert claims == []  # Includes lost/throwing claims and suppressed recovery.
        else:
            chosen = REF if winner == 1 else 'f' * 16
            assert claims == [(f'async_recovery.claim ref={chosen} prior_owner={OLD if winner == 1 else "none"} '
                               f'prior_leased_until={PRIOR if winner == 1 else "none"} '
                               f'owner={NEW} leased_until={LEASE}')]
        assert 'SENTINEL_' not in caplog.text
    assert outputs[0] == outputs[1]


def test_watchdog_logging_failure_still_yields_claim(monkeypatch):
    row = candidate()
    box = box_for(monkeypatch, [row])
    def fail(*args, **kwargs):
        raise RuntimeError('SENTINEL_KEY')
    monkeypatch.setattr(drain.logger, 'log', fail)
    assert list(drain._recover_async(box, limit=1, deadline=10., settings=None)) == [row]
    assert row.lease_owner == NEW and row.leased_until == LEASE


def test_every_claim_in_admitted_recovery_pass_is_logged(monkeypatch, caplog):
    rows = [candidate(), candidate(prior=False)]
    rows[1].payload_hash = 'f' * 64
    box = box_for(monkeypatch, rows)
    monkeypatch.setattr(box, 'due_shard', lambda *, shard, async_only: [rows[shard]] if shard < 2 else [])
    monkeypatch.setattr(box, '_claim_one', lambda *args, **kwargs: True)
    reads = []
    def read(database):
        reads.append(database)
        return None  # Once admitted, the existing iterator does not recheck health.
    monkeypatch.setattr(health, 'read_health', read)
    with caplog.at_level(logging.INFO):
        recovery = drain._recover_async(box, limit=2, deadline=10., settings=None)
        assert next(recovery) is rows[0]
        assert len(caplog.messages) == 1
        monkeypatch.setattr(drain, 'time', SimpleNamespace(time=lambda: 100.))
        # Freshness returning elsewhere cannot suppress the already-admitted pass.
        assert next(recovery) is rows[1]
        assert len(caplog.messages) == 2
        assert list(recovery) == []
    assert reads == [None]
    assert caplog.messages == [
        f'async_recovery.claim ref={REF} prior_owner={OLD} prior_leased_until={PRIOR} '
        f'owner={NEW} leased_until={LEASE}',
        f'async_recovery.claim ref={"f" * 16} prior_owner=none prior_leased_until=none '
        f'owner={NEW} leased_until={LEASE}',
    ]


def test_shared_reference_does_not_import_worker_on_router_path():
    assert drain._row_ref is worker._row_ref is audit.row_ref
    result = subprocess.run([sys.executable, '-c',
        'import sys; from trusted_router.services import settle_outbox_drain; '
        'assert "trusted_router.services.settle_outbox_worker" not in sys.modules'],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
