"""Native-port boundary harness: same SDK metadata/stream/RPC and HTTP paths."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from scripts.async_settle.shadow_v2_live_export import native_io
from trusted_router.async_settle_shadow_journal import HEALTH_KIND
from trusted_router.async_settle_shadow_v2_export import (
    DAY_KINDS,
    MemoryPort,
    OriginalRow,
    export_day,
)
from trusted_router.async_settle_shadow_v2_export_native import (
    EXACT_SQL,
    RANGE_SQL,
    NativeGenerationRead,
    NativePort,
    NativeV2RangeRead,
)
from trusted_router.storage_gcp_shadow_attachments import Lifetime

DAY = '2026-10-12'
STAMP = DatetimeWithNanoseconds.from_rfc3339('2026-10-13T00:00:00.123456789Z')
UPDATED = DatetimeWithNanoseconds.from_rfc3339('2026-10-12T01:02:03.987654321Z')
RAW = ' {"original":"São Paulo 雪"}\n'.encode()
pytestmark = pytest.mark.timeout(15)


class Database:
    def __init__(self, rows=(), *, fault=None):
        self.rows = list(rows)
        self.fault = fault
        self.calls, self.snapshots = [], []
        self.now = 1.
        self.spanner_api = SimpleNamespace(execute_streaming_sql=self.query, create_session=self.checkout)

    def checkout(self, **kwargs):
        self.calls.append(('checkout', kwargs))

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        assert kwargs['retry'] is None and 0 < kwargs['timeout'] <= .2 + 1e-12
        assert kwargs['request_options'] == {'priority': 'PRIORITY_LOW'}
        params = kwargs['params']
        matching = sorted((key, body, UPDATED) for kind, key, body in self.rows if kind == params['kind'])
        if sql == RANGE_SQL:
            matching = [r for r in matching if params['day_start'] <= r[0] < params['next_day_start']
                        and r[0] > params['after_id']][:params['page_size']]
        else:
            assert sql == EXACT_SQL
            matching = [(body, stamp) for key, body, stamp in matching if key == params['id']]
        for index, row in enumerate(matching):
            if self.fault == 'iteration' and params['kind'] == DAY_KINDS[0] and index == 1:
                self.now += .3
            yield row

    @contextmanager
    def snapshot(self, **kwargs):
        self.snapshots.append(kwargs)
        self.spanner_api.create_session()
        db = self

        class Snapshot:
            def execute_sql(self, sql, **params):
                class Stream:
                    metadata = SimpleNamespace(transaction=SimpleNamespace(read_timestamp=(
                        None if db.fault == 'empty_metadata' else STAMP)))

                    def __iter__(self):
                        yield from db.spanner_api.execute_streaming_sql(sql, **params)
                return Stream()
        try:
            yield Snapshot()
        finally:
            if self.fault == 'cleanup' and len(self.snapshots) == 1:
                self.now += .3


def native(db, *, page_size=200, plan=None):
    port = NativePort(db, 'test-bucket', plan or {}, io=native_io(), page_size=page_size)
    port.range.clock = lambda: db.now
    port.generation.authenticated = False
    return port


@pytest.mark.parametrize('count', [0, 199, 200, 201])
def test_native_pages_snapshot_bytes_and_actual_rpcs(count, tmp_path):
    db = Database([(DAY_KINDS[0], DAY + f'/{i:04}', RAW.decode()) for i in range(count)])
    port = native(db)
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert not result.gaps
    assert result.transport == b'' and result.originals == ()
    value = json.loads(result.transport_path.read_bytes())
    assert [r['raw'].encode() for r in value['rows']] == [RAW] * count
    pages = [p for p in port.pages if p.get('kind') == DAY_KINDS[0]]
    assert [p['count'] for p in pages] == ([count] if count < 200 else [200, count-200])
    assert pages[-1]['complete'] and pages[-1]['continuation'] is None
    assert all(p['snapshot'] == 1791849600123456789 for p in port.pages)
    assert db.snapshots == [{}] + [{'read_timestamp': STAMP}] * (len(db.snapshots)-1)
    accounting = json.loads(result.accounting)
    assert accounting['operations']['native_rpcs'] == len(db.calls) == 2*len(port.pages)
    assert accounting['operations']['body_bytes'] == count * len(RAW)
    entries = [entry for path in (tmp_path/'sources/export/entries').glob('*.json')
               for entry in json.loads(path.read_bytes())['entries']] if count else []
    for entry in entries:
        assert (tmp_path/'sources'/entry['original']).read_bytes() == RAW
        assert entry['metadata'] == dict(kind=DAY_KINDS[0], updated_at_ns=1791766923987654321,
            updated_at_semantics='writer_updated_at', read_snapshot_ns=1791849600123456789)


def test_two_kinds_neighbouring_days_and_health_timestamp_semantics():
    rows = [(kind, day+'/a', RAW.decode()) for kind in DAY_KINDS[:2]
            for day in ('2026-10-11', DAY, '2026-10-13')]
    rows.append((HEALTH_KIND, 'local/fleet', '{}'))
    db = Database(rows)
    port = NativeV2RangeRead(db, io=native_io(), clock=lambda: db.now)
    for kind in DAY_KINDS[:2]:
        found = list(port.rows(kind, DAY))
        assert [(r.key, r.body) for r in found] == [(DAY+'/a', RAW)]
        assert found[0].commit['updated_at_semantics'] == 'writer_updated_at'
    health = list(port.exact(HEALTH_KIND, 'local/fleet'))[0]
    assert health.commit['updated_at_semantics'] == 'store_commit_timestamp'
    assert health.commit['updated_at_ns'] == 1791766923987654321
    assert all(p['snapshot'] == port.snapshot_ns for p in port.pages)
    assert list(port.exact(HEALTH_KIND, 'missing')) == []
    assert port.pages[-1]['complete']


@pytest.mark.parametrize('fault', ['iteration', 'cleanup', 'empty_metadata'])
def test_deadline_and_empty_metadata_are_gaps_not_eof_keep_siblings(fault, tmp_path):
    db = Database([(k, DAY + f'/{i}', RAW.decode()) for k in DAY_KINDS[:2] for i in range(2)], fault=fault)
    port = native(db)
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert result.gaps
    assert not port.pages[0]['complete']
    value = json.loads(result.transport_path.read_bytes())
    if fault != 'empty_metadata':
        assert len([r for r in value['rows'] if r['kind'] == DAY_KINDS[1]]) == 2
    assert len(port.pages) == len(DAY_KINDS)
    assert not json.loads(result.accounting)['complete']


@pytest.mark.parametrize('fault', ['continuation', 'snapshot', 'empty_snapshot', 'count'])
def test_broken_page_metadata_is_gap_and_keeps_siblings(fault):
    class Lost(MemoryPort):
        def rows(self, kind, day):
            yield from super().rows(kind, day)
            if kind == DAY_KINDS[0]:
                if fault == 'continuation':
                    self.pages.pop()
                elif fault == 'snapshot':
                    self.pages[-1]['snapshot'] = 'changed'
                elif fault == 'empty_snapshot':
                    self.pages[-1]['snapshot'] = None
                else:
                    self.pages[-1]['count'] = 201
    row = OriginalRow(DAY+'/a', RAW, {})
    port = Lost({k: (row,) for k in DAY_KINDS[:2]}, {}, (), page_size=1)
    result = export_day(port, DAY, completed_at_us=1, object_keys=())
    assert result.gaps and len(json.loads(result.transport)['rows']) == 2


@pytest.mark.parametrize('bad', [b'\xff', b'other', b'x'*262145])
def test_conflicts_nonutf8_and_bounds_retain_originals_in_spool(bad, tmp_path):
    row = OriginalRow(DAY+'/a', RAW, {})
    other = OriginalRow(row.key, bad, {})
    port = MemoryPort({DAY_KINDS[0]: (row, other), DAY_KINDS[1]: (row,)}, {}, ())
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert result.gaps
    assert sorted(p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()) == sorted([RAW, bad, RAW])
    assert any(r['kind'] == DAY_KINDS[1] for r in json.loads(result.transport_path.read_bytes())['rows'])


def test_native_bound_exhaustion_is_charged_not_eof(tmp_path):
    db = Database([(DAY_KINDS[0], DAY+f'/{i}', RAW.decode()) for i in range(4)])
    port = native(db, page_size=2)
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), max_objects=1, spool_dir=tmp_path/'sources')
    assert result.gaps and len(port.pages) == 1
    assert not port.pages[0]['complete']
    assert json.loads(result.accounting)['operations']['native_rpcs'] == len(db.calls)


def fake_http(monkeypatch, replies):
    calls = []

    def exchange(self, method, url, body, headers):
        assert method == 'GET' and body is None
        calls.append(url)
        self.remaining()
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(Lifetime, 'exchange', exchange)
    return calls


def reply(data=RAW, generation='7'):
    return 200, {'x-goog-generation': generation, 'x-goog-metageneration': '2'}, data


def test_generation_exact_condition_and_subsequent_pin(monkeypatch):
    key = f'publisher-traces/{DAY}/boot/1'
    calls = fake_http(monkeypatch, [reply(), reply()])
    reader = NativeGenerationRead('bucket', {key: None}, io=native_io(), authenticated=False)
    assert list(reader.objects(key))[0].body == RAW
    obj = list(reader.objects(key))[0]
    assert (obj.generation, obj.metageneration) == ('7', '2')
    assert 'ifGenerationMatch' not in parse_qs(urlsplit(calls[0]).query)
    assert parse_qs(urlsplit(calls[1]).query)['ifGenerationMatch'] == ['7']
    assert all('/o/publisher-traces%2F' in url for url in calls)
    assert sum(p['native_rpcs'] for p in reader.pages) == 2
    with pytest.raises(ValueError, match='outside_exact_plan'):
        list(reader.objects('unknown'))


@pytest.mark.parametrize('failure', [(404, {}, b''), (412, {}, b''), reply(generation='8'),
                                    (200, {}, b'{}'), TimeoutError('iteration_or_cleanup')])
def test_object_missing_mutation_timeout_keeps_sibling(monkeypatch, tmp_path, failure):
    keys = tuple(f'publisher-traces/{DAY}/boot/{i}' for i in (1, 2))
    calls = fake_http(monkeypatch, [failure, reply()])
    db = Database()
    port = native(db, plan=dict.fromkeys(keys, '7'))
    result = export_day(port, DAY, completed_at_us=1, object_keys=keys, spool_dir=tmp_path/'sources')
    assert result.gaps and len(calls) == 2
    value = json.loads(result.transport_path.read_bytes())
    assert [a['id'] for a in value['attachments']] == [keys[1]]
    assert value['attachments'][0]['raw'].encode() == RAW
    assert port.pages[-1]['complete']


@pytest.mark.parametrize('missing', ['project', 'instance', 'database', 'bucket', 'read-only-confirmed'])
def test_live_cli_requires_explicit_resources_before_client(monkeypatch, tmp_path, missing):
    from scripts.async_settle import shadow_v2_live_export as cli
    plan = tmp_path/'plan.json'
    plan.write_text('[]')
    args = ['cli', '--live', '--day', DAY, '--completed-at-us', '1', '--bundle', str(tmp_path/'bundle'),
            '--object-plan', str(plan)]
    for name in ('project', 'instance', 'database', 'bucket', 'read-only-confirmed'):
        if name != missing:
            args += ['--'+name] + ([] if name == 'read-only-confirmed' else ['test'])
    monkeypatch.setattr('sys.argv', args)
    monkeypatch.setattr(cli, 'live_port', lambda *args: pytest.fail('client constructed'))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_live_cli_prints_plan_before_read_and_nonzero_on_gap(monkeypatch, tmp_path, capsys):
    from scripts.async_settle import shadow_v2_live_export as cli
    plan = tmp_path/'plan.json'
    plan.write_text('[]')
    args = ['cli', '--live', '--day', DAY, '--completed-at-us', '1', '--bundle', str(tmp_path/'bundle'),
            '--object-plan', str(plan), '--read-only-confirmed']
    for name in ('project', 'instance', 'database', 'bucket'):
        args += ['--'+name, 'test']
    monkeypatch.setattr('sys.argv', args)

    def construct(*args):
        value = json.loads(capsys.readouterr().out)
        assert len(value['planned_ranges']) == 7 and value['object_keys_count'] == 0
        return native(Database(fault='empty_metadata'))
    monkeypatch.setattr(cli, 'live_port', construct)
    assert cli.main() == 1
    assert (tmp_path/'bundle').exists()
    assert not json.loads(capsys.readouterr().out)['export_complete']


def test_streaming_memory_does_not_scale_with_day_bodies(tmp_path):
    import tracemalloc
    payload = 'x'*65536
    db = Database([(DAY_KINDS[0], DAY+f'/{i:04}', payload) for i in range(201)])
    port = native(db)
    tracemalloc.start()
    try:
        result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not result.gaps
    assert result.transport_path.stat().st_size > 13_000_000
    assert peak < 4_000_000, peak  # 13 MiB of bodies, one record at a time.


def test_native_snapshot_change_on_empty_page_is_failure():
    db = Database()
    reader = NativeV2RangeRead(db, io=native_io(), clock=lambda: db.now)
    list(reader.rows(DAY_KINDS[0], DAY))
    reader.snapshot_ns += 1
    with pytest.raises(ValueError, match='snapshot_metadata'):
        list(reader.rows(DAY_KINDS[1], DAY))
    assert not reader.pages[-1]['complete']


def test_cleanup_failure_at_export_iterator_boundary_keeps_sibling():
    class Cleanup(MemoryPort):
        def rows(self, kind, day):
            try:
                yield from super().rows(kind, day)
            finally:
                if kind == DAY_KINDS[0]:
                    raise TimeoutError('cleanup')
    row = OriginalRow(DAY+'/a', RAW, {})
    port = Cleanup({k: (row,) for k in DAY_KINDS[:2]}, {}, ())
    result = export_day(port, DAY, completed_at_us=1, object_keys=())
    assert result.gaps
    assert [r['kind'] for r in json.loads(result.transport)['rows']] == list(DAY_KINDS[:2])


def test_native_conflicting_and_nonutf8_rows_are_retained(tmp_path):
    db = Database([(DAY_KINDS[0], DAY+'/a', b'one'), (DAY_KINDS[0], DAY+'/a', b'two'),
                   (DAY_KINDS[1], DAY+'/b', b'\xff'), (DAY_KINDS[2], DAY+'/c', '{}')])
    result = export_day(native(db), DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert {p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()} == {b'one', b'two', b'\xff', b'{}'}
    assert {'conflicting_originals', 'non_utf8_original_retained_in_sidecar'} <= {g['reason'] for g in result.gaps}
    assert any(r['kind'] == DAY_KINDS[2] for r in json.loads(result.transport_path.read_bytes())['rows'])


def test_real_writer_conformance_inputs_are_valid():
    import datetime as dt

    from tests.conformance.test_shadow_v2_export_native import originals
    from tests.test_async_settle_shadow_observation import fixture
    from trusted_router.async_settle_shadow_journal import CLOSURE_KIND, COUNTER_KIND, Row
    day, counters = originals(2)
    for row in counters:
        assert row.value()['region'] == 'São Paulo 雪'
    closure = fixture('closure')[0]['object']
    closure.update(day=day, boot=counters[0].key.split('/')[1],
                   receipt_chain_sha256=hashlib.sha256(b'').hexdigest())
    Row.build(CLOSURE_KIND, counters[0].key, closure).value()
    for delta in (-1, 1):
        value = counters[0].value()
        value['day'] = (dt.date.fromisoformat(day)+dt.timedelta(days=delta)).isoformat()
        for field in ('started_at_us', 'flushed_at_us'):
            value[field] += delta * 86400 * 1000000
        Row.build(COUNTER_KIND, value['day']+'/'+value['boot'], value).value()


@pytest.mark.parametrize('phase', ['iteration', 'cleanup'])
def test_object_lifetime_checks_late_iteration_and_cleanup(monkeypatch, tmp_path, phase):
    keys = tuple(f'publisher-traces/{DAY}/boot/{i}' for i in (1, 2))
    clock = [1.]
    count = [0]

    def exchange(self, method, url, body, headers):
        count[0] += 1
        if count[0] == 1:
            try:
                if phase == 'iteration':
                    clock[0] += .3
                    self.remaining()
                return reply()
            finally:
                if phase == 'cleanup':
                    clock[0] += .3
                    self.remaining()
        return reply()
    monkeypatch.setattr(Lifetime, 'exchange', exchange)
    port = native(Database(), plan=dict.fromkeys(keys, '7'))
    port.generation.clock = lambda: clock[0]
    result = export_day(port, DAY, completed_at_us=1, object_keys=keys, spool_dir=tmp_path/'sources')
    assert len(result.gaps) == 1
    assert [a['id'] for a in json.loads(result.transport_path.read_bytes())['attachments']] == [keys[1]]


@pytest.mark.parametrize('fault', ['changed_snapshot', 'missing_snapshot', 'updated_at', 'late'])
def test_received_row_survives_metadata_and_deadline_failure(fault, tmp_path):
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_export_native import NativePage, NativeRecord

    def make():
        clock = [1.]

        @contextmanager
        def read(database, sql, params, snapshot_ns, deadline, now, rpc, convert):
            broken = params['kind'] == DAY_KINDS[0]

            def metadata():
                if broken and fault in ('missing_snapshot', 'updated_at'):
                    raise ValueError(fault)
                if broken and fault == 'late':
                    clock[0] += .3
                return dict(kind=params['kind'], updated_at_ns=42,
                    updated_at_semantics='writer_updated_at',
                    read_snapshot_ns=2 if broken and fault == 'changed_snapshot' else 1)
            yield NativePage(iter([NativeRecord(DAY+'/a', RAW, metadata)]), lambda: 1)

        io = replace(native_io(), owned_database=lambda db: db, open_page=read)
        port = NativePort(None, 'bucket', {}, io=io)
        port.range.clock = lambda: clock[0]
        port.range.snapshot_ns = 1
        return port

    reader = make().range
    received = []
    with pytest.raises((ValueError, TimeoutError)):
        for row in reader.rows(DAY_KINDS[0], DAY):
            received.append(row)
    assert [(r.key, r.body) for r in received] == [(DAY+'/a', RAW)]
    assert received[0].commit['received_before_failure'] is True
    assert reader.pages[0]['count'] == 1 and reader.pages[0]['body_bytes'] == len(RAW)
    assert not reader.pages[0]['complete']
    assert [(r.key, r.body) for r in reader.rows(DAY_KINDS[1], DAY)] == [(DAY+'/a', RAW)]
    assert reader.pages[-1]['complete']

    port = make()
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert result.gaps and not port.pages[0]['complete']
    account = json.loads(result.accounting)
    assert account['source_body_bytes'] == account['operations']['body_bytes'] == len(RAW)*len(DAY_KINDS)
    entries = json.loads((tmp_path/'sources/export/entries/00000.json').read_bytes())['entries']
    assert entries[0]['metadata']['received_before_failure'] is True
    assert [(e['key'], (tmp_path/'sources'/e['original']).read_bytes()) for e in entries] == [(DAY+'/a', RAW)]*len(DAY_KINDS)


@pytest.mark.parametrize('fault', ['dropped', 'first', 'last', 'kind', 'snapshot', 'status'])
def test_page_claims_reconcile_with_returned_originals(fault, tmp_path):
    from dataclasses import replace

    port = native(Database([(DAY_KINDS[0], DAY+'/a', RAW), (DAY_KINDS[0], DAY+'/b', b' second\n')]))
    original = port.rows

    def rows(kind, day):
        for row in original(kind, day):
            if kind == DAY_KINDS[0]:
                if fault == 'dropped' and row.key.endswith('/a'):
                    continue
                if fault in ('kind', 'snapshot'):
                    field, value = ('kind', DAY_KINDS[1]) if fault == 'kind' else ('read_snapshot_ns', 1)
                    row = replace(row, commit={**row.commit, field: value})
            yield row
        if kind == DAY_KINDS[0] and fault in ('first', 'last', 'status'):
            port.pages[-1][fault] = 'changed'
    port.rows = rows
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert not json.loads(result.accounting)['complete']
    expected_reason = ('page_originals_disagree' if fault in ('dropped', 'first', 'last') else
                       'page_coverage_invalid' if fault == 'status' else 'row_metadata_invalid')
    assert expected_reason in {g['reason'] for g in result.gaps}
    value = json.loads(result.transport_path.read_bytes())
    assert [(r['id'], r['raw'].encode()) for r in value['rows']] == (
        [(DAY+'/b', b' second\n')] if fault == 'dropped' else [(DAY+'/a', RAW), (DAY+'/b', b' second\n')])
    entries = json.loads((tmp_path/'sources/export/entries/00000.json').read_bytes())['entries']
    assert [(e['key'], (tmp_path/'sources'/e['original']).read_bytes()) for e in entries] == [
        (r['id'], r['raw'].encode()) for r in value['rows']]
    if fault == 'kind':
        assert all(e['kind'] == e['metadata']['kind'] == DAY_KINDS[1] for e in entries)
        assert all(r['kind'] == DAY_KINDS[1] for r in value['rows'])


@pytest.mark.parametrize('fault', ['snapshot', 'cleanup', None])
def test_exact_cap_final_range_validates_page_and_cleanup(fault, tmp_path):
    port = native(Database([(DAY_KINDS[-1], DAY+'/z', RAW)]))
    original = port.rows
    cleaned = []

    def rows(kind, day):
        try:
            for row in original(kind, day):
                if kind == DAY_KINDS[-1] and fault == 'snapshot':
                    port.pages[-1]['snapshot'] += 1
                yield row
        finally:
            cleaned.append(kind)
            if kind == DAY_KINDS[-1] and fault == 'cleanup':
                raise RuntimeError('cleanup')
    port.rows = rows
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), max_objects=1, spool_dir=tmp_path/'sources')
    assert cleaned == list(DAY_KINDS)
    assert json.loads(result.accounting)['complete'] is (fault is None)
    assert [p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()] == [RAW]
    if fault:
        reason = 'page_coverage_invalid' if fault == 'snapshot' else 'RuntimeError:cleanup_failed'
        assert reason in {g['reason'] for g in result.gaps}


def test_plan_persisted_before_first_native_operation_and_failure(tmp_path):
    from trusted_router.async_settle_shadow_v2_export import bounds
    from trusted_router.detached_jws import canonical

    directory = tmp_path/'sources'
    inspected = []
    expected = canonical(dict(day=DAY,
        ranges=[dict(kind=k, start=bounds(DAY)[0], end=bounds(DAY)[1]) for k in DAY_KINDS],
        object_keys=[], max_objects=17, generations={}, resources={'project': 'explicit-project'}))

    class Fail(Database):
        def checkout(self, **kwargs):
            inspected.append((directory/'plan.json').read_bytes() if (directory/'plan.json').exists() else None)
            raise OSError('first operation failed')
    port = native(Fail())
    port.resources = {'project': 'explicit-project'}
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), max_objects=17, spool_dir=directory)
    assert inspected == [expected]*len(DAY_KINDS)
    assert (directory/'plan.json').read_bytes() == expected
    assert 'RuntimeError:read_failed' in {g['reason'] for g in result.gaps}
    assert not json.loads((directory/'accounting.json').read_bytes())['complete']
    assert all(p['status'] == 'RuntimeError' and not p['complete'] for p in port.pages)
    assert json.loads(result.transport_path.read_bytes())['rows'] == []


@pytest.mark.parametrize('ids', [[DAY+'/a', DAY+'/a'], [DAY+'/b', DAY+'/a'], ['2026-10-13/a']])
def test_native_range_order_failure_retains_originals_and_later_range(ids, tmp_path):
    class Unordered(Database):
        def query(self, sql, **kwargs):
            keys = ids if kwargs['params']['kind'] == DAY_KINDS[0] else [DAY+'/later']
            for index, key in enumerate(keys):
                yield key, RAW + str(index).encode(), UPDATED

    reader = native(Unordered()).range
    found = []
    with pytest.raises(ValueError, match='range_or_continuation'):
        for row in reader.rows(DAY_KINDS[0], DAY):
            found.append(row)
    expected = [(key, RAW + str(i).encode()) for i, key in enumerate(ids)]
    assert [(r.key, r.body) for r in found] == expected
    assert not reader.pages[0]['complete'] and reader.pages[0]['count'] == len(ids)
    assert [(r.key, r.body) for r in reader.rows(DAY_KINDS[1], DAY)] == [(DAY+'/later', RAW+b'0')]
    assert reader.pages[-1]['complete']

    port = native(Unordered())
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert not port.pages[0]['complete'] and result.gaps
    entries = json.loads((tmp_path/'sources/export/entries/00000.json').read_bytes())['entries']
    assert [(e['key'], (tmp_path/'sources'/e['original']).read_bytes()) for e in entries[:len(ids)]] == expected
    assert port.pages[1]['complete']


@pytest.mark.parametrize('size', [262144, 262145])
def test_native_body_bound_retains_original(size, tmp_path):
    body = b' '*(size-1)+b'\n'
    rows = [(DAY_KINDS[0], DAY+'/a', body), (DAY_KINDS[1], DAY+'/b', RAW)]
    reader = native(Database(rows)).range
    found = []
    if size > 262144:
        with pytest.raises(ValueError, match='body_bound_exhausted'):
            for row in reader.rows(DAY_KINDS[0], DAY):
                found.append(row)
    else:
        found = list(reader.rows(DAY_KINDS[0], DAY))
    assert [(r.key, r.body) for r in found] == [(DAY+'/a', body)]
    assert reader.pages[0]['complete'] is (size == 262144)
    assert reader.pages[0]['body_bytes'] == size
    assert [(r.key, r.body) for r in reader.rows(DAY_KINDS[1], DAY)] == [(DAY+'/b', RAW)]
    port = native(Database(rows))
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert port.pages[0]['complete'] is (size == 262144)
    assert bool(result.gaps) is (size > 262144)
    assert sorted(p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()) == sorted([body, RAW])


@pytest.mark.parametrize('page_index', [1, 2])
def test_only_middle_or_terminal_cursor_changed(page_index, tmp_path):
    port = native(Database([(DAY_KINDS[0], DAY+f'/{i}', RAW) for i in range(2)]), page_size=1)
    original = port.rows

    def rows(kind, day):
        yield from original(kind, day)
        if kind == DAY_KINDS[0]:
            port.pages[page_index]['after'] = DAY+'/wrong'
    port.rows = rows
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert {g['reason'] for g in result.gaps} == {'page_coverage_invalid'}
    assert [p['count'] for p in port.pages[:3]] == [1, 1, 0]
    assert sorted(p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()) == [RAW, RAW]
    assert [(r['id'], r['raw'].encode()) for r in json.loads(result.transport_path.read_bytes())['rows']] == [
        (DAY+f'/{i}', RAW) for i in range(2)]


def test_object_overflow_retains_both_candidates_and_bound_gap(tmp_path):
    from trusted_router.async_settle_shadow_v2_export import OriginalObject

    key = f'publisher-traces/{DAY}/boot/1'
    port = MemoryPort({}, {key: (OriginalObject(key, RAW, '7'), OriginalObject(key, b' other\n', '8'))}, (key,))
    result = export_day(port, DAY, completed_at_us=1, object_keys=(key,), max_objects=1, spool_dir=tmp_path/'sources')
    assert {g['reason'] for g in result.gaps} == {'finite_export_bound_exhausted', 'conflicting_originals'}
    assert sorted(p.read_bytes() for p in (tmp_path/'sources/originals').iterdir()) == sorted([RAW, b' other\n'])
    assert [(a['id'], a['raw'].encode()) for a in json.loads(result.transport_path.read_bytes())['attachments']] == [
        (key, RAW), (key, b' other\n')]


def test_adapter_client_requests_data_scope_and_owned_client_preserves_it(monkeypatch):
    from google.auth.credentials import Credentials
    from google.cloud.spanner_v1 import Client

    from trusted_router.storage_gcp_shadow_v2_export import (
        DATA_SCOPE,
        database_for_export,
        owned_database,
    )

    class Credential(Credentials):
        def refresh(self, request):
            raise AssertionError('no live authentication')

    credential = Credential()
    requests = []

    def default(**kwargs):
        requests.append(kwargs)
        return credential, 'explicit-project'
    monkeypatch.delenv('SPANNER_EMULATOR_HOST', raising=False)
    monkeypatch.setattr('google.auth.default', default)
    database = database_for_export('explicit-project', 'explicit-instance', 'explicit-database')
    assert requests == [{'scopes': [DATA_SCOPE]}]
    owned = owned_database(database)
    first, second = database._instance._client, owned._instance._client
    assert isinstance(first, Client) and isinstance(second, Client) and first is not second
    assert first._client_options.scopes == second._client_options.scopes == [DATA_SCOPE]
    assert first.credentials is second.credentials is credential
    assert owned._instance.instance_id == 'explicit-instance' and owned.database_id == 'explicit-database'


@pytest.mark.parametrize('fault', ['changed_snapshot', 'missing_snapshot', 'updated_at', 'late'])
def test_adapter_delivered_failure_retains_bytes_and_finishes_rpc_count(fault, tmp_path):
    from dataclasses import replace

    db = Database([(DAY_KINDS[0], DAY+'/a', RAW)])
    io = native_io()

    def convert(value):
        if value is UPDATED:
            if fault == 'updated_at':
                return io.timestamp_ns('not a timestamp')
            if fault == 'late':
                db.now += .3
        if value is STAMP:
            if fault == 'missing_snapshot':
                return io.timestamp_ns(None)
            if fault == 'changed_snapshot':
                return io.timestamp_ns(value)+1
        return io.timestamp_ns(value)

    port = NativePort(db, 'bucket', {}, io=replace(io, timestamp_ns=convert))
    port.range.clock = lambda: db.now
    port.range.snapshot_ns = io.timestamp_ns(STAMP)
    result = export_day(port, DAY, completed_at_us=1, object_keys=(), spool_dir=tmp_path/'sources')
    assert result.gaps and not port.pages[0]['complete']
    account = json.loads(result.accounting)
    assert account['source_body_bytes'] == account['operations']['body_bytes'] == len(RAW)
    assert account['operations']['native_rpcs'] == len(db.calls) == 2*len(DAY_KINDS)
    entries = json.loads((tmp_path/'sources/export/entries/00000.json').read_bytes())['entries']
    assert len(entries) == 1 and entries[0]['metadata']['received_before_failure'] is True
    assert entries[0]['key'] == DAY+'/a' and (tmp_path/'sources'/entries[0]['original']).read_bytes() == RAW
