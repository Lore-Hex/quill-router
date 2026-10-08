"""Review witness: real verified exclusions remain complete report evidence."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from starlette.background import BackgroundTasks
from starlette.datastructures import Headers

from scripts.async_settle.shadow_report import known_exclusion, report
from tests.test_async_settle_shadow import FIXTURE, NOW, context, endpoint, signer, wire
from tests.test_async_settle_shadow_accounting import synthetic_window
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_evidence import SAMPLE
from trusted_router.services import async_settle_shadow as shadow_module
from trusted_router.services.async_settle_shadow import Capture, Runtime


def real_exclusion_window(monkeypatch, phase="settle", *, monotonic=None):
    rows, days, proof = synthetic_window()
    first = rows[0]['body']
    now = NOW + 1
    written = []
    ctx = context()
    envelope = copy.deepcopy(FIXTURE)
    envelope['observed']['service_tier'] = 'unsupported'
    envelope.update(terminal=None, payload_hash=None, go_error='unsupported_observed')
    admission = copy.deepcopy(rows[-1]['body']['admission'])
    store = SimpleNamespace(
        reserve=lambda *a: 100, booking=lambda *a: Booking(2 if phase == 'settle' else 0, 'settled' if phase == 'settle' else 'refunded', True),
        insert_sample=lambda identity, body, deadline: written.append((identity, body)) or 'inserted',
        flush=lambda *a: None)
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40,
        async_settle_shadow_workspaces='ws-v1'), runtime(), store,
        SimpleNamespace(peek=lambda *a: admission,
            counts=dict.fromkeys(('workspace_reads', 'health_reads', 'read_failures', 'missed_ticks'), 0)))
    rt.signer = signer()
    rt.counters.instance = first['instance']
    rt.counters.clock = lambda: now
    monkeypatch.setattr(time, 'time', lambda: now)
    bg = BackgroundTasks()
    capture = Capture(rt, ctx.body, phase, now, (monotonic or shadow_module.time.monotonic)(), ctx.authorization, endpoint(), (endpoint(),))
    try:
        rt.submit(capture, SimpleNamespace(headers=Headers({'X-TR-Settlement-Shadow': wire(envelope)[0]})),
            {'data': {'settled': True}}, bg)
        asyncio.run(bg())
        actual = rt.counters.snapshot(closed=True)[0][1]
    finally:
        rt.executor.shutdown()
    for original, extra in zip(first['counts'], actual['counts'], strict=True):
        for key, value in extra.items():
            if key not in ('adapter', 'route_type', 'streamed'):
                original[key] += value
    for key in ('comparison_attempts', 'comparison_dropped', 'samples_inserted', 'duplicate_samples',
                'conflicting_samples', 'booking_pending', 'booking_unknown'):
        first[key] += actual[key]
    for key in ('terminal_counts', 'exclusions', 'rejections', 'drops'):
        if key == 'terminal_counts':
            for extra in actual[key]:
                original = next((r for r in first[key] if all(r[k] == extra[k] for k in ('phase', 'adapter', 'route_type', 'streamed'))), None)
                if original is None:
                    first[key].append(extra)
                else:
                    for field in extra.keys() - {'phase', 'adapter', 'route_type', 'streamed'}:
                        original[field] += extra[field]
        else:
            first[key].extend(actual[key])
    for key, value in actual['admission_observer'].items():
        first['admission_observer'][key] += value
    rows[-1]['body']['authorization_id'] = 'positive'
    rows[-1]['id'] = days[0] + '/positive'
    rows.extend(dict(kind=SAMPLE, id=identity, body=body) for identity, body in written)
    return rows, days, proof, actual, written


def test_actual_verified_cohort_exclusion_does_not_break_clean_window(monkeypatch):
    rows, days, proof, actual, written = real_exclusion_window(monkeypatch)
    assert len(written) == 1
    assert known_exclusion(written[0][1])
    assert actual['first_gap_at_us'] is None
    assert actual['comparison_attempts'] == actual['samples_inserted'] == 1
    assert sum(b['observed_ineligible'] for b in actual['counts']) == 1
    assert sum(b['observed_eligible'] for b in actual['counts']) == 0
    result = report(rows, days, proof)
    assert result['status'] == 'PASS' and result['continuous_seconds'] == 691199
    assert result['gaps'] == []


@pytest.mark.parametrize('damage', ['missing_exclusion', 'surplus_exclusion', 'unknown', 'extra_comparison'])
def test_real_exclusion_counter_mutation_blocks(monkeypatch, damage):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    counter = rows[0]['body']
    if damage in {'missing_exclusion', 'surplus_exclusion'}:
        counter['exclusions'][0]['count'] += -1 if damage == 'missing_exclusion' else 1
        suffix = ':ineligible_coverage_gap'
    elif damage == 'unknown':
        bucket = next(b for b in counter['counts'] if b['observed_ineligible'])
        bucket['observed_ineligible'] -= 1
        bucket['observed_unknown'] += 1
        suffix = ':comparison_observation_gap'
    else:
        counter['comparison_attempts'] += 1
        counter['duplicate_samples'] += 1  # Keep persistence accounting balanced.
        suffix = ':comparison_observation_gap'
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED'
    assert any(g.endswith(suffix) for g in result['gaps'])
