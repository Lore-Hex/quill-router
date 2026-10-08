"""Real native transaction conflict evidence; skips only without configured emulator."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from trusted_router.async_settle_shadow_evidence import CONTROL
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

pytestmark = pytest.mark.xdist_group('conformance-spanner-emulator')


@pytest.mark.parametrize('backend',['spanner-emulator'])
def test_shadow_native_cap_conflict(native_emulator_resources,backend):
    database,_ = native_emulator_resources
    day = '2099-11-28'
    with database.batch() as batch:
        batch.insert_or_update('tr_entities',columns=('kind','id','body','updated_at'),values=[
            (CONTROL,day+'/cap-v1',json.dumps(dict(v=1,limit=100000,reserved=99900,updated_at_us=0)),datetime.now(UTC))])
    def attempt(_):
        try:
            return EvidenceStore(database).reserve(day,time.monotonic()+1)
        except Exception:
            return 0  # Aborted permit is never reused; no retry by design.
    with ThreadPoolExecutor(max_workers=2) as executor:
        grants = list(executor.map(attempt,range(2)))
    assert sorted(grants) == [0, 100]
    rows = EvidenceStore(database).day(CONTROL,day)
    body = json.loads(dict(rows)[day+'/cap-v1'])
    assert set(body) == {'v', 'limit', 'reserved', 'updated_at_us'}
    assert (body['v'], body['limit'], body['reserved']) == (1, 100000, 100000)


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_shadow_native_sample_uniqueness(native_emulator_resources, backend):
    from uuid import uuid4

    from tests.test_async_settle_shadow_accounting import sample_row
    from trusted_router.async_settle_shadow_evidence import SAMPLE
    database, _ = native_emulator_resources
    row = sample_row()
    row['authorization_id'] = 'shadow-' + uuid4().hex
    identity = row['authorization_day'] + '/' + row['authorization_id']
    def insert(_):
        try:
            return EvidenceStore(database).insert_sample(identity, row, time.monotonic()+1)
        except Exception:
            return 'unavailable'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(insert, range(2)))
    assert sorted(results) in (['duplicate', 'inserted'], ['inserted', 'unavailable'])
    stored = dict(EvidenceStore(database).day(SAMPLE, row['authorization_day']))
    assert json.loads(stored[identity]) == row
