import copy
import datetime as dt
import json
import threading

import pytest

from tests.test_async_settle_shadow_accounting import sample_row, synthetic_window
from tests.test_async_settle_shadow_expired_retention import CleanupDatabase
from trusted_router.async_settle_shadow_evidence import CONTROL, COUNTER, SAMPLE
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore


@pytest.mark.parametrize("kind", ["counter", "manifest", "cap"])
@pytest.mark.parametrize("clock_rewinds", [False, True])
def test_retired_day_cannot_be_repopulated_by_other_writes(monkeypatch, kind, clock_rewinds, shadow_deadline_clock):
    rows, days, _ = synthetic_window()
    day = days[0]
    real = dt.datetime
    now = real.fromisoformat(day).replace(tzinfo=dt.UTC) + dt.timedelta(days=31)

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(dt, "datetime", Clock)
    db = CleanupDatabase()
    store = EvidenceStore(db)
    if kind == "counter":
        row = copy.deepcopy(next(r for r in rows if r["kind"] == COUNTER))
        identity = row["id"]
        body = row["body"]
        rowkind = COUNTER
        def action():
            return store.flush(identity, body, shadow_deadline_clock.monotonic() + 1)
    elif kind == "manifest":
        row = copy.deepcopy(next(r for r in rows if r["kind"] == CONTROL))
        identity = row["id"]
        body = row["body"]
        rowkind = CONTROL
        def action():
            return store.publish_manifest(body, shadow_deadline_clock.monotonic() + 1)
    else:
        identity = day + "/cap-v1"
        body = dict(v=1, limit=100000, reserved=100, updated_at_us=1)
        rowkind = CONTROL
        def action():
            return store.reserve(day, shadow_deadline_clock.monotonic() + 1)
    db.rows[rowkind, identity] = json.dumps(body)
    store.cleanup(rowkind, day)
    assert (rowkind, identity) not in db.rows
    if clock_rewinds:
        now -= dt.timedelta(days=1)
    try:
        action()
    except ValueError:
        pass
    print(kind, "retired_key_recreated", (rowkind, identity) in db.rows)
    assert (rowkind, identity) not in db.rows
    assert store.rejections[rowkind, "proof_expired"] == 1


def test_cleanup_can_finish_before_old_sample_mutation(monkeypatch, shadow_deadline_clock):
    row = sample_row()
    day = row["authorization_day"]
    identity = day + "/auth-v1"
    real = dt.datetime
    wall = [real.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp() + 31 * 86400 - 0.001]

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromtimestamp(wall[0], tz)

    monkeypatch.setattr(dt, "datetime", Clock)
    db = CleanupDatabase()
    store = EvidenceStore(db)
    db.lock = threading.RLock()  # Allow the witness to interleave cleanup transactions.
    insert = db.insert_or_update
    events = []

    def interleave(**kwargs):
        if kwargs["values"][0][0] != SAMPLE:
            return insert(**kwargs)
        wall[0] += 0.002
        shadow_deadline_clock.now += 0.002
        cursor = store.cleanup(SAMPLE, day)
        events.append(("cleanup_completed", cursor, list(db.rows)))
        insert(**kwargs)

    monkeypatch.setattr(db, "insert_or_update", interleave)
    outcome = store.insert_sample(identity, row, shadow_deadline_clock.monotonic() + 1)
    print(outcome, events, "recreated", (SAMPLE, identity) in db.rows)
    assert (SAMPLE, identity) not in db.rows, (
        "cleanup completed after retirement then old-day insert committed"
    )


@pytest.mark.parametrize('kind', ['sample', 'counter', 'manifest', 'cap'])
def test_every_write_rejects_retirement_budget_boundary(monkeypatch, kind, shadow_deadline_clock):
    rows, days, _ = synthetic_window()
    day = days[0]
    real = dt.datetime
    boundary = real.fromisoformat(day).replace(tzinfo=dt.UTC) + dt.timedelta(days=31)
    clock_reads = []
    sample = sample_row()

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            clock_reads.append(boundary - dt.timedelta(milliseconds=1))
            return clock_reads[-1]

    monkeypatch.setattr(dt, 'datetime', Clock)
    db = CleanupDatabase()
    store = EvidenceStore(db)
    try:
        if kind == 'sample':
            store.insert_sample(day+'/auth-v1', sample, shadow_deadline_clock.monotonic()+1)
        elif kind == 'counter':
            row = rows[0]
            store.flush(row['id'], row['body'], shadow_deadline_clock.monotonic()+1)
        elif kind == 'manifest':
            store.publish_manifest(rows[1]['body'], shadow_deadline_clock.monotonic()+1)
        else:
            store.reserve(day, shadow_deadline_clock.monotonic()+1)
    except ValueError as error:
        assert str(error) == 'proof_expired'
    assert clock_reads == [boundary - dt.timedelta(milliseconds=1)]
    assert db.rows == {}
    assert sum(store.rejections.values()) == 1


def test_cleanup_fence_aborts_delayed_insert_even_after_empty_scan(monkeypatch, shadow_deadline_clock):
    """Optimistic transaction adapter: validate complete-key reads on commit."""
    from trusted_router.storage_gcp_async_settle_shadow import RETENTION_FENCE, WRITE_BUDGET_SECONDS

    # Start outside the early-rejection window, then deschedule past retention.
    delay = dt.timedelta(seconds=WRITE_BUDGET_SECONDS + 1)

    class Conflict(Exception):
        pass

    class Transactions(CleanupDatabase):
        def run_in_transaction(self, callback, **kwargs):
            database = self
            reads, writes = {}, {}

            class Tx:
                def execute_sql(self, sql, *, params, **options):
                    key = (params['kind'], params['id'])
                    reads[key] = database.rows.get(key)
                    return [] if reads[key] is None else [(reads[key],)]

                def insert_or_update(self, *, table, columns, values):
                    for kind, identity, body, _ in values:
                        if kind == SAMPLE:
                            # Descheduling can exceed a client deadline. Cleanup
                            # completes while this mutation is still uncommitted.
                            now[0] += delay
                            assert store.cleanup(SAMPLE, day) == ''
                            assert (SAMPLE, identity) not in database.rows
                        writes[kind, identity] = body

            result = callback(Tx())
            if any(self.rows.get(key) != value for key, value in reads.items()):
                raise Conflict('retention fence changed')
            self.rows.update(writes)
            return result

    row = sample_row()
    day = row['authorization_day']
    real = dt.datetime
    now = [real.fromisoformat(day).replace(tzinfo=dt.UTC) + dt.timedelta(days=31) - delay]

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(dt, 'datetime', Clock)
    db = Transactions()
    store = EvidenceStore(db)
    conflict = None
    try:
        store.insert_sample(day+'/auth-v1', row, shadow_deadline_clock.monotonic()+1)
    except Conflict as error:
        conflict = str(error)
    assert conflict == 'retention fence changed'
    assert (SAMPLE, day+'/auth-v1') not in db.rows
    assert json.loads(db.rows[CONTROL, RETENTION_FENCE]) == dict(v=1, retired_before='2026-10-07')
