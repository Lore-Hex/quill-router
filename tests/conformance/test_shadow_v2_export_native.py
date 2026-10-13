"""A3-N native readback. Selected by CI's -k spanner-emulator command.

No application service/executor or package inventory is installed by these
cases. A configured emulator failing setup is a failure, never a skip.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import time
from uuid import uuid4

import pytest

from scripts.async_settle.shadow_v2_live_export import native_io
from tests.test_async_settle_shadow_observation import fixture
from trusted_router.async_settle_shadow_journal import CLOSURE_KIND, COUNTER_KIND, Row
from trusted_router.async_settle_shadow_v2_export_native import NativeV2RangeRead
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

pytestmark = [pytest.mark.xdist_group('conformance-spanner-emulator'), pytest.mark.timeout(30)]


@pytest.fixture
def export_emulator():
    from tests.conformance.spanner_emulator import emulator_resources
    from tests.shadow_observation_fixtures import native_rpc_deadlines
    if 'SPANNER_EMULATOR_HOST' in os.environ:
        assert os.environ.get('TR_CONFORMANCE_EMULATOR_SCHEMA') == '1'
    try:
        with native_rpc_deadlines(), emulator_resources() as resources:
            try:
                yield resources[0]
            finally:
                resources[0].drop()
    except pytest.skip.Exception as error:
        if 'SPANNER_EMULATOR_HOST' in os.environ:
            pytest.fail(f'configured export emulator skipped: {error}')
        raise


def originals(count):
    base = fixture('counter')[0]['object']
    boot = uuid4().hex
    rows = []
    for index in range(count):
        value = copy.deepcopy(base)
        value.update(boot=boot+f'{index:04}', region='São Paulo 雪')
        row = Row.build(COUNTER_KIND, value['day']+'/'+value['boot'], value)
        # Real writer validation accepts this whitespace and canonical escaped
        # Unicode; it rejects literal non-ASCII JSON transport spelling.
        rows.append(Row(row.kind, row.key, b' '+row.body+b'\n'))
    return base['day'], rows


def write(store, rows):
    # Same serialized mutation helper as real observation writers. Small batches
    # keep fixture work bounded; each transaction and RPC has an explicit budget.
    for at in range(0, len(rows), 10):
        batch = rows[at:at+10]
        budget = Budget(time.monotonic()+2)
        store.transaction(lambda tx, batch=batch, budget=budget: store.write(tx, batch, budget), budget, 2.)


@pytest.mark.parametrize('backend', ['spanner-emulator'])
@pytest.mark.parametrize('count', [0, 199, 200, 201])
def test_native_export_page_edges(export_emulator, backend, count):
    day, rows = originals(count)
    store = ObservationStore(export_emulator)
    write(store, rows)
    reader = NativeV2RangeRead(export_emulator, io=native_io())
    found = list(reader.rows(COUNTER_KIND, day))
    assert [(r.key, r.body) for r in found] == [(r.key, r.body) for r in rows]
    assert [p['count'] for p in reader.pages] == ([count] if count < 200 else [200, count-200])
    assert reader.pages[-1]['complete'] and reader.pages[-1]['continuation'] is None
    assert len({p['snapshot'] for p in reader.pages}) == 1
    assert reader.pages[0]['snapshot'] > 0
    assert all(p['native_rpcs'] >= 1 for p in reader.pages)
    if rows:
        assert json.loads(found[0].body)['region'] == 'São Paulo 雪'
        assert found[0].commit['updated_at_semantics'] == 'writer_updated_at'
        assert found[0].commit['read_snapshot_ns'] == reader.snapshot_ns
    if count >= 200:
        assert reader.pages[1]['after'] == rows[199].key


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_export_kinds_day_boundaries_and_fixed_snapshot(export_emulator, backend):
    day, counters = originals(2)
    store = ObservationStore(export_emulator)
    closure = fixture('closure')[0]['object']
    closure.update(day=day, boot=counters[0].key.split('/')[1],
                   receipt_chain_sha256=hashlib.sha256(b'').hexdigest())
    closing = Row.build(CLOSURE_KIND, counters[0].key, closure)
    neighbours = []
    for delta in (-1, 1):
        value = counters[0].value()
        value['day'] = (dt.date.fromisoformat(day)+dt.timedelta(days=delta)).isoformat()
        for field in ('started_at_us', 'flushed_at_us'):
            value[field] += delta * 86400 * 1000000
        neighbours.append(Row.build(COUNTER_KIND, value['day']+'/'+value['boot'], value))
    write(store, [*counters, closing, *neighbours])
    reader = NativeV2RangeRead(export_emulator, io=native_io(), page_size=1)
    found = list(reader.rows(COUNTER_KIND, day))
    # A later write to the other kind must not change this pass's view.
    old = closing.body
    changed = Row(closing.kind, closing.key, old+b' ')
    write(store, [changed])
    assert [r.body for r in found] == [r.body for r in counters]
    assert [(r.key, r.body) for r in reader.rows(CLOSURE_KIND, day)] == [(closing.key, old)]
    assert [p['count'] for p in reader.pages if p['kind'] == CLOSURE_KIND] == [1, 0]
    assert [p['count'] for p in reader.pages if p['kind'] == COUNTER_KIND] == [1, 1, 0]
    assert len({p['snapshot'] for p in reader.pages}) == 1
    assert all(p['snapshot'] > 0 for p in reader.pages)


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_native_export_preserves_literal_utf8_corruption(export_emulator, backend):
    day, rows = originals(1)
    store = ObservationStore(export_emulator)
    write(store, rows)
    # The real writer permits escaped Unicode but intentionally rejects literal
    # UTF-8 JSON spelling. Corrupt its stored bytes at the native mutation
    # boundary; the exporter must preserve, rather than repair, that original.
    body = json.dumps(rows[0].value(), ensure_ascii=False, indent=1)+'\n'
    with export_emulator.batch() as batch:
        batch.update(table='tr_entities', columns=('kind', 'id', 'body', 'updated_at'),
            values=[(COUNTER_KIND, rows[0].key, body, dt.datetime.now(dt.UTC))])
    found = list(NativeV2RangeRead(export_emulator, io=native_io()).rows(COUNTER_KIND, day))
    assert len(found) == 1 and found[0].body == body.encode('utf-8')
    assert b'\xc3\xa3' in found[0].body and b'\xe9\x9b\xaa' in found[0].body
