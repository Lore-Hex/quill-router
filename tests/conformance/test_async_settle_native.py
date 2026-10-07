"""GoogleSQL semantics for the additive row and bounded exposure query."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trusted_router.services.async_settle import Admission
from trusted_router.storage_gcp_async_admission import read_admission

pytestmark = pytest.mark.xdist_group('conformance-spanner-emulator')


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_admission_counts_leased_and_dead_excludes_terminal_and_null(native_emulator_resources, backend):
    database, _ = native_emulator_resources
    suffix = uuid4().hex
    with database.batch() as batch:
        batch.insert_or_update('tr_credit_balance', columns=('workspace_id', 'shard', 'total_credits',
                                                  'total_usage', 'reserved', 'trust_tier'),
                     values=[('async-admit-' + suffix, 0, 100000000, 0, 0, 2)])
        batch.insert_or_update('tr_settle_outbox', columns=('authorization_id', 'intent_kind', 'settle_origin',
                                                'actual_cost_micro', 'workspace_id', 'status', 'leased_until'),
                     values=[('async-pending-' + suffix, 'settle', 'typed', 100, 'async-admit-' + suffix, 'pending', None),
                             ('async-leased-' + suffix, 'settle', 'typed', 200, 'async-admit-' + suffix, 'pending', datetime.now(UTC) + timedelta(seconds=300)),
                             ('async-dead-' + suffix, 'settle', 'typed', 300, 'async-admit-' + suffix, 'dead', None),
                             ('async-done-' + suffix, 'settle', 'typed', 10000, 'async-admit-' + suffix, 'done', None),
                             ('async-other-' + suffix, 'settle', 'typed', 10000, 'other-' + suffix, 'pending', None),
                             ('async-legacy-' + suffix, 'settle', 'typed', 10000, None, 'pending', None)])
    assert read_admission(database, 'async-admit-' + suffix) == Admission(600, 2)
    with database.batch() as batch:
        batch.insert_or_update('tr_settle_outbox', columns=('authorization_id', 'intent_kind', 'settle_origin',
                                                'actual_cost_micro', 'workspace_id'),
                     values=[(f'async-overflow-{suffix}-{i}', 'settle', 'typed', 0, 'async-admit-' + suffix) for i in range(1001)])
    with pytest.raises(ValueError, match='admission unavailable'):
        read_admission(database, 'async-admit-' + suffix)


@pytest.mark.parametrize('backend', ['spanner-emulator'])
@pytest.mark.parametrize('open_reservation', [True, False])
def test_native_async_admission_is_atomic(native_emulator_resources, backend, open_reservation):
    import time

    from google.cloud.spanner_v1 import param_types

    from trusted_router.storage_gcp_async_settle import ReservationNotOpen, enqueue
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    from trusted_router.storage_models import SettleOutboxRow

    database, _ = native_emulator_resources
    suffix = uuid4().hex
    rid, aid = 'async-native-res-' + suffix, 'async-native-auth-' + suffix
    with database.batch() as batch:
        batch.insert_or_update('tr_reservation', columns=('reservation_id', 'workspace_id', 'authorization_id',
                                                'settled', 'credit_reserved_micro', 'key_reserved_micro'),
                     values=[(rid, 'async-native-ws', aid, not open_reservation, 3, 3)])
    outbox = SpannerSettleOutbox(database, param_types)
    row = SettleOutboxRow(authorization_id=aid, intent_kind='settle', settle_origin='typed',
                         actual_cost_micro=2, reservation_id=rid, async_version=1,
                         workspace_id='async-native-ws', snapshot_hash='a'*64, payload_hash='b'*64)
    if open_reservation:
        enqueue(outbox, row, time.monotonic()+.5)
        accepted = outbox.get(aid, 'settle')
        assert accepted is not None and accepted.actual_cost_micro == 2
        assert accepted.async_version == 1 and accepted.terminal_at is None
    else:
        with pytest.raises(ReservationNotOpen):
            enqueue(outbox, row, time.monotonic()+.5)
        assert outbox.get(aid, 'settle') is None


@pytest.mark.parametrize('backend', ['spanner-emulator'])
@pytest.mark.parametrize('competitor', ['claim', 'enqueue'])
def test_native_overlapping_async_transactions(native_emulator_resources, backend, competitor):
    """Both read-write transactions begin before either writes; SDK retries losers."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from google.api_core.exceptions import AlreadyExists
    from google.cloud.spanner_v1 import param_types as pt

    from trusted_router.storage_gcp_async_settle import ReservationNotOpen, enqueue
    from trusted_router.storage_gcp_counter_dml import claim_reservation_statement
    from trusted_router.storage_gcp_io import run_in_transaction_with_retry
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    from trusted_router.storage_models import SettleOutboxRow

    database, _ = native_emulator_resources
    suffix = uuid4().hex
    rid, aid = 'race-res-' + suffix, 'race-auth-' + suffix
    with database.batch() as batch:
        batch.insert_or_update(
            'tr_reservation',
            columns=('reservation_id', 'workspace_id', 'authorization_id', 'settled',
                     'credit_reserved_micro', 'key_reserved_micro'),
            values=[(rid, suffix, aid, False, 3, 3)],
        )
    ready = Barrier(2)

    class RacingDatabase:
        def __init__(self):
            self.first = True
            self.counts = []

        def run_in_transaction(self, callback, **kwargs):
            def run(tx):
                if self.first:
                    self.first = False
                    tx._begin_transaction()
                    ready.wait(timeout=20)
                self.counts = []
                owner = self

                class RecordingTransaction:
                    def __getattr__(self, name):
                        return getattr(tx, name)

                    def batch_update(self, statements):
                        status, counts = tx.batch_update(statements)
                        owner.counts = list(counts)
                        return status, counts

                return callback(RecordingTransaction())
            return database.run_in_transaction(run, **kwargs)

    def attempt(kind):
        racing = RacingDatabase()
        try:
            if kind == 'enqueue':
                row = SettleOutboxRow(
                    authorization_id=aid, intent_kind='settle', settle_origin='typed',
                    actual_cost_micro=2, reservation_id=rid, async_version=1,
                    workspace_id=suffix, snapshot_hash='a'*64, payload_hash='b'*64,
                )
                # Generous test-only budget tolerates emulator abort backoff;
                # handler tests separately pin the production 500 ms deadline.
                enqueue(SpannerSettleOutbox(racing, pt), row, time.monotonic()+30)
                assert racing.counts == [1, 0, 0, 1]
            else:
                def claim(tx):
                    sql, params, types = claim_reservation_statement(
                        pt, rid, actual_micro=7, settled_usage_type='Credits',
                        defer_retention=True, async_fence=True,
                    )
                    count = tx.execute_update(sql, params=params, param_types=types)
                    if count == 0:
                        raise ReservationNotOpen('async intent won')
                    assert count == 1
                run_in_transaction_with_retry(racing, claim, total_budget_seconds=30)
            return kind
        except (AlreadyExists, ReservationNotOpen):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, 'enqueue')
        second = pool.submit(attempt, competitor)
        winners = [winner for winner in (first.result(timeout=45), second.result(timeout=45)) if winner]
    assert len(winners) == 1
    row = SpannerSettleOutbox(database, pt).get(aid, 'settle')
    with database.snapshot() as snapshot:
        from google.cloud.spanner_v1 import KeySet

        reservations = list(snapshot.read('tr_reservation', ('settled', 'actual_micro'), KeySet(keys=[[rid]])))
    if winners == ['enqueue']:
        assert row is not None and row.actual_cost_micro == 2
        assert reservations == [[False, None]]
    else:
        assert row is None
        assert reservations == [[True, 7]]


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_null_created_at_is_indexed_and_unhealthy(native_emulator_resources, backend):
    """Real GoogleSQL checks the typed literal, generated value and sparse membership."""
    import time

    from google.cloud.spanner_v1 import KeySet, param_types

    from trusted_router.services.async_settle import decode_health
    from trusted_router.storage_gcp_async_admission import publish_health, read_health

    database, _ = native_emulator_resources
    suffix = uuid4().hex
    statuses = ('pending', 'dead', 'done', 'release_approved')
    keys = [[f'null-age-{status}-{suffix}', 'settle'] for status in statuses]
    try:
        with database.batch() as batch:
            batch.insert('tr_settle_outbox',
                columns=('authorization_id', 'intent_kind', 'settle_origin', 'actual_cost_micro', 'status', 'created_at'),
                values=[(*key, 'typed', 7, status, None) for key, status in zip(keys, statuses, strict=True)])
        for key, status in zip(keys, statuses, strict=True):
            # One single-use snapshot per query: the emulator client refuses reuse.
            with database.snapshot() as snapshot:
                rows = list(snapshot.execute_sql(
                    'SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox'
                    '@{FORCE_INDEX=tr_settle_outbox_unresolved} '
                    'WHERE authorization_id=@aid AND intent_kind=@kind AND unresolved_at IS NOT NULL',
                    params={'aid': key[0], 'kind': key[1]},
                    param_types={'aid': param_types.STRING, 'kind': param_types.STRING}))
                assert rows == ([[datetime(1970, 1, 1, tzinfo=UTC), 7, status]]
                                if status in ('pending', 'dead') else [])
        value = publish_health(database)
        assert value['backlog_count'] >= 2 and value['frozen_micro'] >= 14
        assert value['oldest_unresolved_age_seconds'] >= value['observed_at']
        assert value['complete'] is False
        assert decode_health(read_health(database), now=time.monotonic(), wall=time.time()) is None
    finally:
        with database.batch() as batch:
            batch.delete('tr_settle_outbox', KeySet(keys=keys))
