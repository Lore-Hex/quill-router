"""Daily real-writer captures with the composition test's fake source ports."""
from __future__ import annotations

import importlib
from datetime import date

import pytest

from tests.shadow_v2_pack_round3_captures import writer_capture
from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture


def captured_day(root, day):
    shift = (date.fromisoformat(day) - date(2026, 10, 12)).days * 86400000000
    modules = [importlib.import_module('tests.' + name) for name in (
        'test_async_settle_shadow_v2_pack', 'test_async_settle_shadow_observation', 'test_async_settle_shadow_v2_collect',
        'test_async_settle_shadow_v2_live_export', 'test_async_settle_shadow_v2_pack_integration',
        'test_async_settle_shadow_v2_pack_round3_composition',
        'test_async_settle_shadow_v2_pack_round3_budget', 'test_async_settle_shadow_v2_pack_round3_sources')]
    with pytest.MonkeyPatch.context() as patch:
        for module in modules:
            if hasattr(module, 'DAY'):
                patch.setattr(module, 'DAY', day)
            for field in ('START', 'END', 'NOW_US'):
                if hasattr(module, field):
                    patch.setattr(module, field, getattr(module, field) + shift)
        capture = writer_capture(root / 'writer')
        return stage_capture(root / 'capture', capture)


def midnight_writer(root):
    import asyncio

    from tests import test_async_settle_shadow_v2_pack_integration as real
    from tests.test_async_settle_shadow_v2_export_native import Database, fake_http, native, reply
    from trusted_router.async_settle_shadow_v2_export import export_day
    from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles

    root.mkdir(parents=True)
    captured = dict(terminal_delay_us=2100000)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(real, 'START', real.START + 86394000000)
        asyncio.run(real.test_real_writers_native_export_collect_and_pack_loader(patch, root, captured))
    values = {}
    for day in ('2026-10-12', '2026-10-13'):
        objects = PackFiles(root / day / 'objects')
        for key, raw in captured['objects'].items():
            if '/' + day + '/' in key:
                objects.put(key, raw)
        keys = sorted(objects.refs)
        with pytest.MonkeyPatch.context() as patch:
            fake_http(patch, [reply(objects.read(objects.refs[key])) for key in keys])
            exported = export_day(native(Database(captured['rows']), page_size=2, plan=dict.fromkeys(keys, '7')),
                day, completed_at_us=captured['completed_at_us'], object_keys=keys, spool_dir=root / day / 'native')
        import json
        values[day] = dict(root=root / day, native_root=root / day / 'native',
            object_plan=dict.fromkeys(keys, '7'), bundle=json.loads(exported.transport_path.read_bytes()), objects=objects)
    return values


def lifecycle_capture(root, state):
    from tests.test_async_settle_shadow_v2_collect import inventory, setup
    name = 'idle' if state == 'idle' else 'killed'
    files, binding, source, queries = setup(root, names=(name,))
    result, found = inventory(files, binding, source, queries)
    return source.records['lifecycle'], result, found
