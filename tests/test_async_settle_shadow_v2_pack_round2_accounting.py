"""Streamed NativeExport comparisons."""
from __future__ import annotations

import tracemalloc

import pytest

from tests import test_async_settle_shadow_v2_pack_accounting as previous
from tests.test_async_settle_shadow_v2_export_native import DAY, Database, native
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_export import DAY_KINDS, export_day
from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
    compare_export_accounting_streamed,
)
from trusted_router.detached_jws import canonical


@pytest.mark.parametrize('damage', ['middle_page', 'last_page', 'eof', 'kind', 'day',
    'snapshot', 'cursor', 'hash', 'byte', 'extra_rpc', 'operation_sum', 'body_sum',
    'failure', 'continuation', 'record', 'accounting'])
def test_streamed_accounting_same_original_negatives(monkeypatch, tmp_path, damage):
    def streamed(files, reference, bundle, **kwargs):
        path = tmp_path / 'streamed-capture.json'
        path.write_bytes(bundle)
        return compare_export_accounting_streamed(files, reference, path, **kwargs)
    monkeypatch.setattr(previous, 'compare_export_accounting', streamed)
    previous.test_accounting_each_original_change_blocks(tmp_path, damage)


def test_streamed_accounting_large_day_memory(tmp_path):
    raw = canonical(dict(original='x' * 250000)).decode()
    db = Database([(DAY_KINDS[0], DAY + f'/{i:04d}', raw) for i in range(128)])
    exported = export_day(native(db, page_size=2), DAY, completed_at_us=1,
                          object_keys=(), spool_dir=tmp_path / 'native')
    assert not exported.gaps
    assert exported.transport_path.stat().st_size > 30 * 1024 * 1024
    tracemalloc.start()
    try:
        found = compare_export_accounting_streamed(Files(tmp_path / 'native'),
            previous.reference('accounting.json', exported.accounting), exported.transport_path,
            day=DAY, object_plan={})
        _, peak = tracemalloc.get_traced_memory()
        assert found.complete, found.missing
        assert peak < 6 * 1024 * 1024, peak
    finally:
        tracemalloc.stop()
