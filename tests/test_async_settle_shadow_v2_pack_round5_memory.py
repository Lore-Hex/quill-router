"""Allocation boundary around one complete day-original dictionary."""
from __future__ import annotations

import copy
import tracemalloc

from scripts.async_settle import shadow_v2_window as command
from tests.test_async_settle_shadow_v2_pack_round3_composition import composed  # noqa: F401
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.detached_jws import canonical


def test_day_originals_materialize_inside_day_call(tmp_path, composed, monkeypatch):  # noqa: F811
    import gc
    import shutil

    from trusted_router import async_settle_shadow_v2_pack_composition as composition
    from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles

    root, original_plan = composed
    source = tmp_path / 'source'
    shutil.copytree(root / 'capture', source)
    plan = copy.deepcopy(original_plan)
    files = PackFiles(source)
    for index in range(256):
        plan['files'].append(files.put(f'memory/{index:05d}', ('x' * 65536).encode()))
    active = False
    original = DayFiles.originals
    assemble = composition.assemble_day
    peaks = {'producer': 0, 'day': 0, 'stage': 0}

    def originals(self):
        assert active, 'day originals accumulated outside the day call'
        return original(self)

    def measured(label, callback):
        def call(*args, **kwargs):
            nonlocal active
            gc.collect()
            peaks['producer'] = max(peaks['producer'], tracemalloc.get_traced_memory()[1])
            tracemalloc.reset_peak()
            active = True
            try:
                return callback(*args, **kwargs)
            finally:
                peaks[label] = tracemalloc.get_traced_memory()[1]
                active = False
                tracemalloc.reset_peak()
        return call

    monkeypatch.setattr(DayFiles, 'originals', originals)
    monkeypatch.setattr(composition, 'assemble_day', measured('day', assemble))
    # Stage loading has separate end-to-end coverage. This regression traces
    # the producer and the unchanged day validation call only.
    monkeypatch.setattr(command, 'evaluate_stage', lambda *args, **kwargs: dict(missing=[]))
    tracemalloc.start()
    try:
        command.collect_day(source, tmp_path / 'out', plan)
        peaks['producer'] = max(peaks['producer'], tracemalloc.get_traced_memory()[1])
    finally:
        tracemalloc.stop()
    (tmp_path / 'peaks.json').write_bytes(canonical(peaks))
    assert peaks['day'] > 16_000_000
    assert peaks['producer'] < 12_000_000, peaks



def test_midnight_writer_terminal_is_retained(tmp_path):
    import json

    from tests.shadow_v2_pack_round5_captures import midnight_writer
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_pack_references import TerminalReconciliation

    captures = midnight_writer(tmp_path / 'midnight')
    pages, samples = [], []
    for capture in captures.values():
        for row in capture['bundle']['rows']:
            if row['kind'] == p1.KINDS[2]:
                pages.append(json.loads(row['raw']))
            elif row['kind'] == p1.KINDS[0]:
                samples.append(json.loads(row['raw']))
    joined = TerminalReconciliation(pages, samples, spool=tmp_path / 'joins')
    from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
    page, event = next((page, event) for page in pages for event in page['events']
        if event['phase'] == 'authorize' and event['authorization_id'] == 'auth-2')
    terminal = joined.terminal(joins.authorization_obligation(page, event['seq']))
    assert terminal is not None and terminal[0]['day'] == '2026-10-13'
    assert joined.support_days(['2026-10-12']) == ('2026-10-12', '2026-10-13', '2026-10-14')
