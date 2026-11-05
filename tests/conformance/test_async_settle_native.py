"""GoogleSQL semantics for the additive row and bounded exposure query."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.conformance.spanner_sql_builders import NOW
from tests.conformance.test_rpc_c1_native import c1_database  # noqa: F401 - isolated native database
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


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_sibling_refund_first_claimant(native_emulator_resources, backend):
    """Two accepted kinds overlap their claim transactions on one reservation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from google.cloud.spanner_v1 import KeySet, param_types

    from trusted_router.async_settle_fence import APPLY_PAYLOAD
    from trusted_router.storage_gcp_counter_dml import claim_reservation_statement

    database, _ = native_emulator_resources
    suffix = uuid4().hex
    aid, rid = 'f1-auth-'+suffix, 'f1-res-'+suffix
    with database.batch() as batch:
        batch.insert('tr_reservation',
                     columns=('reservation_id', 'workspace_id', 'authorization_id', 'settled',
                              'credit_reserved_micro', 'key_reserved_micro'),
                     values=[(rid, suffix, aid, False, 3, 3)])
        batch.insert('tr_settle_outbox',
                     columns=('authorization_id', 'intent_kind', 'settle_origin', 'reservation_id',
                              'actual_cost_micro', 'async_version', 'payload_hash', 'status'),
                     values=[(aid, kind, 'typed', rid, amount, 1, kind+'-hash', 'pending')
                             for kind, amount in [('settle', 2), ('refund', 0)]])
    barrier = Barrier(2)
    def attempt(kind, amount):
        first = True
        def transaction(tx):
            nonlocal first
            if first:
                first = False
                tx._begin_transaction()
                barrier.wait(timeout=20)
            token = APPLY_PAYLOAD.set((kind+'-hash', kind))
            try:
                sql, params, types = claim_reservation_statement(param_types, rid,
                    actual_micro=amount, settled_usage_type='Credits', defer_retention=True,
                    async_fence=True)
                return tx.execute_update(sql, params=params, param_types=types)
            finally:
                APPLY_PAYLOAD.reset(token)
        return database.run_in_transaction(transaction)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            settle = pool.submit(attempt, 'settle', 2)
            refund = pool.submit(attempt, 'refund', 0)
            counts = (settle.result(timeout=45), refund.result(timeout=45))
        assert counts in ((1, 0), (0, 1))
        with database.snapshot() as snapshot:
            values = list(snapshot.read('tr_reservation', ('settled', 'actual_micro'), KeySet(keys=[[rid]])))
        assert values == [[True, 2 if counts[0] else 0]]
    finally:
        with database.batch() as batch:
            batch.delete('tr_reservation', KeySet(keys=[[rid]]))
            batch.delete('tr_settle_outbox', KeySet(keys=[[aid, 'settle'], [aid, 'refund']]))


def test_native_drain_version_partition(c1_database, monkeypatch):  # noqa: F811 - shared fixture
    """Execute the real bulk/shard queries and lease claims against two native rows."""
    from google.cloud.spanner_v1 import param_types as pt

    from trusted_router import storage_gcp_settle_outbox as outbox_module

    database = c1_database
    now = NOW.isoformat().replace('+00:00', 'Z')
    monkeypatch.setattr(outbox_module, '_iso_now', lambda: now)
    # Seventeen fixed keys guarantee a pair in one of the sixteen generated
    # shards. Ask GoogleSQL for the hash; don't approximate FARM_FINGERPRINT.
    with database.snapshot() as snapshot:
        shards = list(snapshot.execute_sql(
            "SELECT id, MOD(MOD(FARM_FINGERPRINT(CONCAT(id, '#settle')), 16) + 16, 16) "
            "FROM UNNEST(@ids) AS id ORDER BY id",
            params={'ids': [f'drain-partition-{i:02}' for i in range(17)]},
            param_types={'ids': pt.Array(pt.STRING)},
        ))
    seen = {}
    for aid, shard in shards:
        if shard in seen:
            legacy_id, async_id = seen[shard], aid
            break
        seen[shard] = aid
    else:
        pytest.fail('17 keys must collide across 16 shards')
    with database.batch() as batch:
        batch.insert('tr_settle_outbox',
            columns=('authorization_id', 'intent_kind', 'settle_origin', 'actual_cost_micro',
                     'async_version', 'created_at', 'next_attempt_at'),
            values=[(legacy_id, 'settle', 'typed', 1, None, NOW - timedelta(seconds=3), NOW - timedelta(seconds=1)),
                    (async_id, 'settle', 'typed', 1, 1, NOW - timedelta(seconds=3), NOW - timedelta(seconds=2))])
    outbox = outbox_module.SpannerSettleOutbox(database, pt)
    def ids(rows):
        return [row.authorization_id for row in rows]

    # Async is first in due order: a legacy LIMIT must apply after the SQL
    # partition, not before a Python filter. Both default scans include both.
    assert ids(outbox.due()) == [async_id, legacy_id]
    assert ids(outbox.due(limit=1, legacy_only=True)) == [legacy_id]
    assert ids(outbox.due_shard(shard=shard)) == [async_id, legacy_id]
    assert ids(outbox.due_shard(shard=shard, async_only=True)) == [async_id]
    assert ids(outbox.claim(limit=2, lease_seconds=300)) == [legacy_id]
    legacy, accepted = outbox.get(legacy_id, 'settle'), outbox.get(async_id, 'settle')
    assert legacy.lease_owner and legacy.leased_until == (NOW + timedelta(seconds=300)).isoformat().replace('+00:00', 'Z')
    assert accepted.lease_owner is None and accepted.leased_until is None
    # Recovery uses the same advisory candidates with the short lease.
    candidates = outbox.due_shard(shard=shard, async_only=True)
    assert ids(outbox.claim_candidates(candidates, lease_seconds=30)) == [async_id]
    assert outbox.get(async_id, 'settle').leased_until == (NOW + timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')


def test_native_release_claim_frees_only_its_own_pending_claim(c1_database, monkeypatch):  # noqa: F811 - shared fixture
    """Run release_claim on GoogleSQL: the fake checks its predicates only as substrings."""
    from google.cloud.spanner_v1 import param_types as pt

    from trusted_router import storage_gcp_settle_outbox as outbox_module

    database = c1_database
    clock = [NOW]
    monkeypatch.setattr(outbox_module, '_iso_now', lambda: clock[0].isoformat().replace('+00:00', 'Z'))
    keys = [('release-own', 'settle'), ('release-own', 'refund'), ('release-stale', 'settle'),
            ('release-dead', 'settle'), ('release-idle', 'settle')]
    with database.batch() as batch:
        batch.insert('tr_settle_outbox',
            columns=('authorization_id', 'intent_kind', 'settle_origin', 'actual_cost_micro',
                     'created_at', 'next_attempt_at'),
            values=[(aid, kind, 'typed', 1, NOW - timedelta(seconds=3), NOW - timedelta(seconds=1))
                    for aid, kind in keys])
    outbox = outbox_module.SpannerSettleOutbox(database, pt)

    def claim(aid, kind):
        [row] = outbox.claim_candidates([outbox.get(aid, kind)], lease_seconds=30)
        return row

    def state(aid, kind):
        row = outbox.get(aid, kind)
        return row.status, row.lease_owner, row.leased_until

    # Its own live claim goes back at once and is due again. Both intents of the
    # authorization carry one owner here, as a batch claim's rows do, so only
    # the key keeps each release to its own intent.
    settle = claim('release-own', 'settle')
    database.run_in_transaction(lambda transaction: transaction.execute_update(
        "UPDATE tr_settle_outbox SET lease_owner=@owner, leased_until=@lease "
        "WHERE authorization_id=@aid AND intent_kind=@kind",
        params={'owner': settle.lease_owner, 'lease': NOW + timedelta(seconds=30),
                'aid': 'release-own', 'kind': 'refund'},
        param_types={'owner': pt.STRING, 'lease': pt.TIMESTAMP, 'aid': pt.STRING, 'kind': pt.STRING}))
    refund = outbox.get('release-own', 'refund')
    assert (refund.lease_owner, refund.leased_until) == (settle.lease_owner, settle.leased_until)
    assert outbox.release_claim(refund) is True
    assert state('release-own', 'refund') == ('pending', None, None)
    assert state('release-own', 'settle') == ('pending', settle.lease_owner, settle.leased_until)
    assert outbox.release_claim(settle) is True
    assert state('release-own', 'settle') == ('pending', None, None)
    assert claim('release-own', 'refund').lease_owner != settle.lease_owner

    # A claim another worker took after this one's lease lapsed stays with it.
    stale = claim('release-stale', 'settle')
    clock[0] += timedelta(seconds=31)
    current = claim('release-stale', 'settle')
    assert outbox.release_claim(stale) is False
    assert state('release-stale', 'settle') == ('pending', current.lease_owner, current.leased_until)

    # A row that is no longer pending is untouched, even with the owner still on it.
    dead = claim('release-dead', 'settle')
    database.run_in_transaction(lambda transaction: transaction.execute_update(
        "UPDATE tr_settle_outbox SET status='dead' WHERE authorization_id=@aid AND intent_kind=@kind",
        params={'aid': 'release-dead', 'kind': 'settle'},
        param_types={'aid': pt.STRING, 'kind': pt.STRING}))
    assert outbox.release_claim(dead) is False
    assert state('release-dead', 'settle') == ('dead', dead.lease_owner, dead.leased_until)

    # No other row was written: the never-claimed one was never updated at all.
    assert state('release-idle', 'settle') == ('pending', None, None)
    assert outbox.get('release-idle', 'settle').updated_at is None


def test_native_inline_grace_counts_from_due_time(c1_database, monkeypatch):  # noqa: F811 - shared fixture
    """Run the grace read and the publisher on GoogleSQL: the fake checks predicates only as substrings."""
    from google.cloud.spanner_v1 import KeySet

    from trusted_router import storage_gcp_async_admission as admission

    database = c1_database
    now = datetime.now(UTC).replace(microsecond=0)
    columns = ('authorization_id', 'intent_kind', 'settle_origin', 'actual_cost_micro', 'status',
               'attempts', 'async_version', 'last_error', 'created_at', 'next_attempt_at')

    def row(aid, age, due, attempts=0, version=None, status='pending', note=None):
        created = now - timedelta(seconds=age)
        return (aid, 'settle', 'typed', 1, status, attempts, version, note, created,
                None if due is None else created + timedelta(seconds=due))

    def iso(value):
        return value.isoformat().replace('+00:00', 'Z')

    # Alone, a row in its inline grace keeps admission open; its creation age is still the oldest.
    with database.batch() as batch:
        batch.insert('tr_settle_outbox', columns=columns,
                     values=[row('grace', 30, 60), row('young', 1, 0, version=1)])
    value = admission.publish_health(database)
    assert value['backlog_count'] == 2 and value['p95_age_seconds'] <= 5
    assert value['oldest_unresolved_age_seconds'] >= 30

    # Rows that are not in their grace keep their creation age, each enough to close it.
    others = [row('no-due', 31, None), row('async', 32, 60, version=1), row('retried', 33, 60, attempts=1),
              row('dead', 34, None, status='dead'), row('done', 35, None, status='done'), row('old', 90, 60),
              row('parked', 36, 3600, note='typed store unavailable')]
    for other in others:
        with database.batch() as batch:
            batch.insert('tr_settle_outbox', columns=columns, values=[other])
        value = admission.publish_health(database)
        assert value['p95_age_seconds'] > 5 or other[0] == 'done', other[0]
        with database.batch() as batch:
            batch.delete('tr_settle_outbox', KeySet(keys=[[other[0], 'settle']]))

    # The read itself: non-async pending rows the drain has neither attempted nor parked,
    # created in [since, until), oldest first, bounded. Rows sit on both bounds.
    edges = [row('at-since', 66, 60), row('at-until', 5, 60), row('young-legacy', 1, 60)]
    with database.batch() as batch:
        batch.insert('tr_settle_outbox', columns=columns, values=others + edges)
    sql, params, types = admission.grace_statement(iso(now - timedelta(seconds=66)), iso(now - timedelta(seconds=5)))
    with database.snapshot() as snapshot:
        found = list(snapshot.execute_sql(sql, params=params, param_types=types))
    assert found == [[now - timedelta(seconds=66), now - timedelta(seconds=6)],
                     [now - timedelta(seconds=31), None],
                     [now - timedelta(seconds=30), now + timedelta(seconds=30)]]
    monkeypatch.setattr(admission, 'GRACE_ROW_LIMIT', 1)
    sql, params, types = admission.grace_statement(iso(now - timedelta(seconds=66)), iso(now - timedelta(seconds=5)))
    with database.snapshot() as snapshot:
        assert list(snapshot.execute_sql(sql, params=params, param_types=types)) == [
            [now - timedelta(seconds=66), now - timedelta(seconds=6)]]
