import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

from starlette.background import BackgroundTasks
from starlette.datastructures import Headers

from tests.test_async_settle_shadow import context, endpoint, signer
from tests.test_async_settle_shadow_accounting import sample_row
from tests.test_async_settle_shadow_expired_retention import CleanupDatabase
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import SAMPLE, day_at
from trusted_router.services.async_settle_shadow import Capture, Runtime
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore


def test_no_retired_sample_insert_across_midnight(monkeypatch):
    ctx = context()
    # Authorization is on the oldest retained day. Receipt occurs 100 ms
    # before that entire day becomes eligible for the operator cleanup.
    created = dt.datetime.fromisoformat(ctx.authorization.created_at).timestamp()
    now = [created + 31 * 86400 - 0.1]
    monkeypatch.setattr(time, "time", lambda: now[0])
    real = dt.datetime

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromtimestamp(now[0], tz)

    monkeypatch.setattr(dt, "datetime", Clock)
    db = CleanupDatabase()
    store = EvidenceStore(db)
    identity = day_at(created) + "/auth-v1"
    monkeypatch.setattr(store, "booking", lambda *args: Booking(2, "settled", True))
    clean = []

    def peek(_):
        # Worker was descheduled across midnight after its age check.
        now[0] += 0.2
        db.rows[SAMPLE, identity] = json.dumps(sample_row())
        clean.append(store.cleanup(SAMPLE, day_at(created)))
        clean.append((SAMPLE, identity) not in db.rows)
        return None

    rt = Runtime(
        settings(
            async_settle_enabled=False, release="a" * 40, async_settle_shadow_workspaces="ws-v1"
        ),
        runtime(),
        store,
        SimpleNamespace(peek=peek, counts={}),
    )
    rt.signer = signer()
    rt.counters.clock = lambda: now[0]
    bg = BackgroundTasks()
    try:
        rt.submit(
            Capture(
                rt,
                ctx.body,
                "settle",
                now[0],
                time.monotonic(),
                ctx.authorization,
                endpoint(),
                (endpoint(),),
            ),
            SimpleNamespace(headers=Headers({"X-TR-Settlement-Shadow": "!"})),
            {"data": {"already_settled": True, "finalization_outcome": "settled"}},
            bg,
        )
        asyncio.run(bg())
    finally:
        rt.executor.shutdown()
    print("cleanup", clean, "sample keys", [key for key in db.rows if key[0] == SAMPLE])
    assert clean == [identity, True]
    assert (SAMPLE, identity) not in db.rows, (
        "sample repopulated after cutoff advanced and cleanup completed"
    )


def test_insert_rechecks_clock_after_point_read(monkeypatch):
    row = sample_row()
    created = dt.datetime.fromtimestamp(row["authorize_at_us"] / 1e6, dt.UTC)
    now = [created.timestamp() + 31 * 86400 - 0.1]
    real = dt.datetime

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromtimestamp(now[0], tz)

    monkeypatch.setattr(dt, "datetime", Clock)
    db = CleanupDatabase()
    execute = db.execute_sql

    def cross_midnight(*args, **kwargs):
        result = execute(*args, **kwargs)
        now[0] += 0.2
        return result

    monkeypatch.setattr(db, "execute_sql", cross_midnight)
    error = None
    try:
        EvidenceStore(db).insert_sample(
            row["authorization_day"] + "/auth-v1", row, time.monotonic() + 1
        )
    except ValueError as exc:
        error = str(exc)
    assert error == "proof_expired"
    assert not [key for key in db.rows if key[0] == SAMPLE]


def test_retention_and_insert_share_one_timestamp(monkeypatch):
    row = sample_row()
    real = dt.datetime
    boundary = real.fromtimestamp(row['authorize_at_us']/1e6 + 31*86400, dt.UTC)
    calls = []
    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            result = boundary - dt.timedelta(microseconds=1) if not calls else boundary
            calls.append(result)
            return result
    monkeypatch.setattr(dt, 'datetime', Clock)
    db = CleanupDatabase()
    inserted_at = []
    insert = db.insert_or_update
    def record(**kwargs):
        inserted_at.extend(values[-1] for values in kwargs['values'])
        insert(**kwargs)
    monkeypatch.setattr(db, 'insert_or_update', record)
    outcome = EvidenceStore(db).insert_sample(row['authorization_day']+'/auth-v1', row, time.monotonic()+1)
    assert outcome == 'inserted'
    assert calls == inserted_at == [boundary - dt.timedelta(microseconds=1)]
