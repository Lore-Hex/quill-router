"""Test-owned fake-port captures made by the real writer and C1 collectors."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests import test_async_settle_shadow_v2_pack_integration as real
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles


def writer_capture(path: Path) -> dict:
    path.mkdir(parents=True, exist_ok=True)
    captured = PackFiles(path / 'objects')
    original_sink = real.ObjectSink

    class Sink(original_sink):
        def put(self, *args, **kwargs):
            result = super().put(*args, **kwargs)
            for key, item in self.objects.items():
                captured.put(key, item.body)
            return result

    with pytest.MonkeyPatch.context() as change:
        change.setattr(real, 'ObjectSink', Sink)
        asyncio.run(real.test_real_writers_native_export_collect_and_pack_loader(change, path))
    from tests.test_async_settle_shadow_v2_export_native import Database, fake_http, native, reply
    from trusted_router.async_settle_shadow_v2_export import export_day
    bundle = json.loads((path / 'native/bundle.json').read_bytes())
    keys = sorted(captured.refs)
    db = Database([(row['kind'], row['id'], row['raw']) for row in bundle['rows']])
    with pytest.MonkeyPatch.context() as change:
        fake_http(change, [reply(captured.read(captured.refs[key])) for key in keys])
        exported = export_day(native(db, page_size=2, plan=dict.fromkeys(keys, '7')), bundle['days'][0],
            completed_at_us=bundle['completed_at_us'], object_keys=keys, spool_dir=path / 'native-complete')
    assert not exported.gaps
    return dict(root=path, native_root=path / 'native-complete',
        object_plan=dict.fromkeys(keys, '7'), bundle=json.loads(exported.transport_path.read_bytes()), objects=captured)


def collector_capture(files, capture, *, native_prefix='native/'):
    from dataclasses import asdict

    from tests.test_async_settle_shadow_v2_collect import (
        get_readiness,
        inventory,
        readiness_sources,
        review,
        setup,
        shutdown_sources,
    )
    from trusted_router import async_settle_shadow_v2_collect_shutdown as shut
    from trusted_router.async_settle_shadow_v2_collect_ports import Pass, Query
    counter = next(json.loads(r['raw']) for r in capture['bundle']['rows'] if 'counter' in r['kind'])
    day, boot = counter['day'], counter['boot']
    _, binding, fake, queries = setup(files.root, names=(boot,))
    # These are explicit fake platform records surrounding the actual writer.
    for row in fake.records['lifecycle']:
        payload = row['jsonPayload']
        payload['at_us'] = counter['started_at_us'] if payload['event'] == 'start' else counter['flushed_at_us']
    for row in fake.records['startup']:
        row.update(process_start_us=counter['started_at_us'], recorded_at_us=counter['started_at_us'] + 2)
    _, inv = inventory(files, binding, fake, queries)
    _, inv = inventory(files, binding, fake, queries, review(files, inv.deployment))
    pins, startup = readiness_sources(files, inv)
    # C1 must preserve the original reader basis emitted by the writer.
    basis = max((basis for key, ref in capture['objects'].refs.items()
        if key.startswith('health-basis-batches/') for basis in json.loads(capture['objects'].read(ref))['bases']),
        key=lambda b: b['read_at_ns'] or -1)
    for record in startup.records['readiness']:
        if record.get('criterion') == 'observer_freshness':
            record.update(health_basis=files.obj(native_prefix + 'actual-reader-basis', basis),
                completed_at_us=basis['read_at_ns'] // 1000 + 2,
                installed_at_ns=basis['read_at_ns'] + basis['read_elapsed_ns'],
                install_elapsed_us=basis['read_elapsed_ns'] // 1000)
        if record['kind'] == 'a3_traffic_ready':
            record['at_us'] = basis['read_at_ns'] // 1000 + 3
    _, ready = get_readiness(files, inv, pins, startup)
    assert ready.complete
    spans = shutdown_sources(inv)
    originals = {}
    for name, needle in (('original_close', 'closure'), ('close_result', 'close_result')):
        row = next(r for r in capture['bundle']['rows'] if needle in r['kind'])
        originals[name] = files.put(native_prefix + name, row['raw'].encode())
    result = json.loads(files.read(originals['close_result']))
    spans.records['shutdown'][0]['spans']['close'] = [500, 500 + result['elapsed_us']]
    from trusted_router.async_settle_shadow_v2_pack_originals import ScopedOutputs
    shutdown_files = ScopedOutputs(files.root, refs=files.refs, prefix=native_prefix.removesuffix('native/'))
    def shutdown(approved=None):
        run = Pass(shutdown_files)
        found = shut.collect(run, inventory=inv, logs=spans, query=Query('shutdown', 'named-service', day),
            originals={day + '/' + boot: originals}, review=approved)
        assert found.complete, run.missing
        return found
    draft = shutdown()
    closed = shutdown(review(files, draft.measurements))
    outputs = {name: files.obj('captures/' + name, value) for name, value in dict(
        Inventory=asdict(inv), Readiness=asdict(ready), Shutdown=asdict(closed)).items()}
    # Files emitted by the C1 Files instance are explicitly captured here.
    for path in sorted(files.root.rglob('*')):
        if path.is_file() and not path.name.endswith('.zip'):
            files.put(path.relative_to(files.root).as_posix(), path.read_bytes())
    return outputs, pins
