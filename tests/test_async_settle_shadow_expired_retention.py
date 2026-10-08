"""Review witness: rejected replay cannot recreate a retired evidence day."""
import asyncio
import contextlib
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest
from starlette.background import BackgroundTasks
from starlette.datastructures import Headers

from tests.test_async_settle_shadow import NOW, context, endpoint, signer, wire
from tests.test_async_settle_shadow_accounting import Database, sample_row
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_binding import LIFETIME
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import COUNTER, SAMPLE, day_at
from trusted_router.services import async_settle_shadow as shadow_module
from trusted_router.services.async_settle_shadow import Capture, Runtime
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore


class CleanupDatabase(Database):
    def snapshot(self):
        return contextlib.nullcontext(self)

    def execute_sql(self, sql, *, params, **kwargs):
        if 'day_start' in params:
            return sorted((identity, body) for (kind, identity), body in self.rows.items()
                if kind == params['kind'] and params['day_start'] <= identity < params['next_day_start']
                and identity > params['after_id'])[:params['page_size']]
        return super().execute_sql(sql, params=params, **kwargs)

    def delete(self, table, keyset):
        for key in keyset.keys:
            self.rows.pop(tuple(key), None)


@pytest.mark.parametrize('age, malformed', [(LIFETIME-1, False), (LIFETIME, False), (31*86400, False), (31*86400, True)])
def test_expired_replay_cannot_repopulate_deleted_authorization_day(monkeypatch, age, malformed):
    ctx = context()
    # The fixture proof is issued at authorization creation, one second before NOW.
    now = int(dt.datetime.fromisoformat(ctx.authorization.created_at).timestamp()) + age
    monkeypatch.setattr(time, 'time', lambda: now)
    real_datetime = dt.datetime
    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(now, tz)
    monkeypatch.setattr(dt, 'datetime', Clock)
    db = CleanupDatabase()
    store = EvidenceStore(db)
    old_day = day_at(NOW)
    identity = old_day + '/auth-v1'
    if age > 30*86400:
        # Run the actual cleanup adapter before attempting the late write.
        db.rows[SAMPLE, identity] = json.dumps(sample_row())
        cursor = store.cleanup(SAMPLE, old_day)
        assert cursor == identity
        assert store.day(SAMPLE, old_day) == []
    monkeypatch.setattr(store, 'booking', lambda *args: Booking(2, 'settled', True))
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40,
        async_settle_shadow_workspaces='ws-v1'), runtime(), store)
    rt.signer = signer()
    rt.counters.clock = lambda: now
    bg = BackgroundTasks()
    try:
        rt.submit(Capture(rt, ctx.body, 'settle', now, shadow_module.time.monotonic(), ctx.authorization, endpoint(), (endpoint(),)),
            SimpleNamespace(headers=Headers({'X-TR-Settlement-Shadow': '!' if malformed else wire()[0]})),
            {'data': {'already_settled': True, 'finalization_outcome': 'settled'}}, bg)
        asyncio.run(bg())
    finally:
        rt.executor.shutdown()
    if age < LIFETIME:
        assert json.loads(db.rows[SAMPLE, identity])['classification'] == 'exact'
        return
    assert not [key for key in db.rows if key[0] == SAMPLE]
    assert not [key for key in db.rows if key[1].startswith(old_day + '/')]
    assert store.day(SAMPLE, old_day) == []
    counter = json.loads(db.rows[COUNTER, day_at(now) + '/' + rt.counters.instance])
    assert counter['comparison_attempts'] == counter['comparison_dropped'] == 1
    assert counter['samples_inserted'] == 0
    assert [(r['reason'], r['count']) for r in counter['rejections']] == [('base64' if malformed else 'proof_expired', 1)]
    assert counter['first_gap_at_us'] == now*1000000
