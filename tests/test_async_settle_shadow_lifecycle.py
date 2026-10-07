from __future__ import annotations

import asyncio
import json

from tests.test_async_settle_shadow_accounting import Database
from tests.test_async_settle_ticket import runtime, settings
from trusted_router.async_settle_shadow_evidence import COUNTER, dimensions
from trusted_router.services.async_settle_shadow import Runtime
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore


def test_timer_flushes_without_sample_work_and_keeps_idle_roster():
    async def run():
        db = Database()
        rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
        stop = asyncio.Event()
        original = rt.flush
        loop = asyncio.get_running_loop()
        def flush(deadline):
            original(deadline)
            loop.call_soon_threadsafe(stop.set)
        rt.flush = flush
        await rt.maintain_counters(stop)
        rt.executor.shutdown()
        return db
    db = asyncio.run(run())
    bodies = [json.loads(body) for (kind, _), body in db.rows.items() if kind == COUNTER]
    assert [(body['sequence'], body['closed'], body['comparison_attempts']) for body in bodies] == [(1, False, 0)]
    assert all(bucket['observed_attempts'] == 0 for bucket in bodies[0]['counts'])


def test_counter_timer_flushes_rate_drops_and_failure_is_sticky():
    db = Database()
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    dims = dimensions('openai', 'responses', True)
    rt.counters.reason(dims, 'settle', 'rate_limit')
    import time
    rt.flush(time.monotonic()+1)
    first = next(json.loads(body) for (kind, _), body in db.rows.items() if kind == COUNTER)
    assert first['drops'] == [dict(phase='settle', adapter='openai', route_type='responses', streamed=True, reason='rate_limit', count=1)]
    gap = first['first_gap_at_us']
    rt.last_flush = 0
    rt.flush(time.monotonic()+1, True)
    final = next(json.loads(body) for (kind, _), body in db.rows.items() if kind == COUNTER)
    assert final['closed'] is True and final['first_gap_at_us'] == gap and gap is not None
    rt.executor.shutdown()


def test_counter_integer_saturation_retains_sticky_unknown():
    from trusted_router.async_settle_shadow_evidence import Counters
    counters = Counters('us-central1','a'*40,clock=lambda:1791244801)
    dims = dimensions('openai','responses',True)
    counters.increment(dims,'comparison_attempts',(1 << 63)-1)
    counters.increment(dims,'comparison_attempts')
    body = counters.snapshot()[0][1]
    assert (body['comparison_attempts'],body['counter_overflow'],body['first_gap_at_us']) == ((1 << 63)-1, True, 1791244801000000)
    counters.increment(dims,'comparison_attempts')
    assert counters.snapshot()[0][1]['comparison_attempts'] == (1 << 63)-1


def test_hash_precedence_over_simultaneous_identity_disagreement():
    import copy

    from tests.test_async_settle_shadow import FIXTURE, context, signer, wire
    from trusted_router.async_settle_shadow_compare import compare
    value = copy.deepcopy(FIXTURE)
    value['terminal']['workspace_id'] = 'foreign'
    result = compare(wire(value), context(), [signer().trusted])
    assert (result.classification, result.reasons) == ('hash', {'hash'})


def test_manifest_write_uses_exact_control_schema():
    from tests.test_async_settle_shadow_accounting import synthetic_window
    from trusted_router.async_settle_shadow_evidence import CONTROL
    rows, _, _ = synthetic_window()
    body = rows[1]['body']
    db = Database()
    import time
    EvidenceStore(db).publish_manifest(body, time.monotonic()+1)
    assert json.loads(db.rows[CONTROL, body['day']+'/manifest-v1']) == body


def test_missing_sample_cannot_be_hidden_by_closed_counter():
    from scripts.async_settle.shadow_report import report
    from tests.test_async_settle_shadow_accounting import synthetic_window
    from trusted_router.async_settle_shadow_evidence import SAMPLE
    rows, days, proof = synthetic_window()
    result = report([row for row in rows if row['kind'] != SAMPLE], days, proof)
    assert result['clean_window_start_us'] is None and any(gap.endswith(':sample_count_gap') for gap in result['gaps'])


def test_rate_admission_precedes_real_comparator_and_refills(monkeypatch):
    import time
    from types import SimpleNamespace

    from starlette.datastructures import Headers

    from tests.test_async_settle_shadow import NOW, context, endpoint, signer, wire
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.services import async_settle_shadow as module
    from trusted_router.services.async_settle_shadow import Capture
    clock = [100.]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    cfg = settings(async_settle_enabled=False, release='a'*40, async_settle_shadow_workspaces='ws-v1')
    store = EvidenceStore(Database())
    monkeypatch.setattr(store, 'booking', lambda *args: Booking(2,'settled',True))
    rt = Runtime(cfg, runtime(), store)
    rt.signer = signer()
    original, calls = module.compare, []
    def compare(*args):
        calls.append(args[0])
        return original(*args)
    monkeypatch.setattr(module, 'compare', compare)
    class Background:
        def add_task(self, function, capture, headers, result, elapsed, dims, size):
            rt.process(capture, headers, result, elapsed, dims)
            rt.pending -= 1
            rt.queued_bytes -= size
    def submit():
        ctx = context()
        capture = Capture(rt,ctx.body,'settle',NOW,clock[0],ctx.authorization,endpoint(),(endpoint(),))
        rt.submit(capture,SimpleNamespace(headers=Headers({'X-TR-Settlement-Shadow':wire()[0]})),
                  {'data':{'settled':True}},Background())
    for _ in range(12):
        submit()
    assert calls == [wire()]*10
    clock[0] += 1
    submit()
    submit()
    submit()
    assert calls == [wire()]*12
    body = rt.counters.snapshot()[0][1]
    assert [row for row in body['drops'] if row['reason']=='rate_limit'] == [dict(phase='settle',adapter='openai',route_type='chat.completions',streamed=False,reason='rate_limit',count=3)]
    bucket = next(row for row in body['counts'] if (row['adapter'],row['route_type'],row['streamed']) == ('openai','chat.completions',False))
    assert (bucket['observed_attempts'],bucket['observed_eligible'],bucket['observed_unknown']) == (15,12,3)
    rt.executor.shutdown()


def test_queue_refuses_unused_oversize_alias_and_integer_before_copy():
    from types import SimpleNamespace

    from starlette.datastructures import Headers

    from tests.test_async_settle_shadow import NOW, context
    from trusted_router.services.async_settle_shadow import Capture
    rt = Runtime(settings(async_settle_enabled=False, async_settle_shadow_workspaces='ws-v1'), runtime())
    queued = []
    background = SimpleNamespace(add_task=lambda *args: queued.append(args))
    ctx = context()
    for changes in ({'endpoint':'x'*100000}, {'actual_input_tokens':1 << 10000}):
        body = ctx.body.model_copy(update=changes)
        rt.submit(Capture(rt,body,'settle',NOW,0,ctx.authorization),
                  SimpleNamespace(headers=Headers()), {'data':{'settled':True}}, background)
    assert queued == [] and (rt.pending,rt.queued_bytes) == (0,0)
    rows = rt.counters.snapshot()[0][1]['rejections']
    assert [(row['phase'],row['reason'],row['count']) for row in rows] == [('settle','identity',1),('settle','integer',1)]


def test_counter_unknown_prediction_blocks_duplicate_observation():
    from scripts.async_settle.shadow_report import report
    from tests.test_async_settle_shadow_accounting import synthetic_window
    rows, days, proof = synthetic_window()
    rows[0]['body']['admission_observer']['prediction_unknown'] = 1
    result = report(rows, days, proof)
    assert result['status'] == 'BLOCKED' and result['clean_window_start_us'] is None
    assert result['metrics']['prediction_known_fraction'] == .5


def test_verified_cohort_exclusion_cannot_seed_clock_or_hide_unknown():
    import copy

    from scripts.async_settle.shadow_report import known_exclusion, positive_sample
    from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
    from tests.test_async_settle_shadow_accounting import sample_row
    from trusted_router.async_settle_shadow_compare import compare
    from trusted_router.async_settle_shadow_evidence import Counters
    envelope = copy.deepcopy(FIXTURE)
    envelope['observed']['service_tier'] = 'flex'
    envelope.update(terminal=None, payload_hash=None, go_error='unsupported_observed')
    compared = compare(wire(envelope),context(),[signer().trusted])
    counters = Counters('us-central1','a'*40,clock=lambda:NOW)
    dims = dimensions('openai','chat.completions',False)
    for key in ('settle_attempts','observed_attempts','observed_unknown'):
        counters.increment(dims,key)
    counters.outcome(dims,compared)
    counters.reason(dims,'settle','service_tier','exclusions')
    assert counters.snapshot()[0][1]['first_gap_at_us'] is None
    row = sample_row()
    row.update(classification='unevaluable',reason_codes=['service_tier'])
    row['eligibility']['observed'] = False
    row['admission'].update(prediction='yes',workspace_age_us=1,health_age_us=1)
    assert known_exclusion(row) and not positive_sample(row)
    row['admission']['prediction'] = 'unknown'
    assert not known_exclusion(row)


def test_signed_evidence_delta_survives_strict_storage_decode():
    import pytest

    from trusted_router.async_settle_shadow_wire import Rejection, bounded_json
    raw = b'{"booked_minus_frozen":-17,"python_minus_go":0}'
    assert bounded_json(raw, signed=True) == {'booked_minus_frozen':-17,'python_minus_go':0}
    with pytest.raises(Rejection, match='integer'):
        bounded_json(raw)
    with pytest.raises(Rejection, match='json_duplicate'):
        bounded_json(b'{"booked_minus_frozen":-17,"booked_minus_frozen":0}', signed=True)


def test_cpu_budget_has_a_failing_witness(monkeypatch):
    import pytest

    from scripts.async_settle import shadow_benchmark
    # Fast cold requests cannot conceal a slow warm tail (and vice versa in
    # test_cpu_budget_rejects_only_cold_tail).
    ticks = iter(tick for index in range(6) for tick in (
        index*10_000_000, index*10_000_000+(100_000 if index < 5 else 6_000_000)))
    monkeypatch.setattr(shadow_benchmark.time,'thread_time_ns',lambda:next(ticks))
    with pytest.raises(AssertionError,match='warm shadow comparator exceeds 5 ms CPU budget'):
        shadow_benchmark.benchmark(iterations=6)


def test_explicit_exclusion_overflow_still_sets_sticky_gap():
    from trusted_router.async_settle_shadow_evidence import DIMENSIONS, Counters
    counters = Counters('us-central1','a'*40,clock=lambda:1791244801)
    expected = []
    for phase in ('authorize','settle','refund'):
        for adapter,route,streamed in DIMENSIONS:
            counters.reason((adapter,route,streamed),phase,'service_tier','exclusions')
            expected.append(dict(phase=phase,adapter=adapter,route_type=route,streamed=streamed,reason='service_tier',count=1))
    body = counters.snapshot()[0][1]
    assert body['exclusions'] == expected[:128]
    assert (body['dimension_overflow'],body['counter_overflow'],body['first_gap_at_us']) == (16,True,1791244801000000)


def test_local_worker_failure_and_storage_failure_keep_distinct_reasons(monkeypatch):
    from types import SimpleNamespace

    from tests.test_async_settle_shadow import NOW, context, signer, wire
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.services import async_settle_shadow as module
    from trusted_router.services.async_settle_shadow import Capture
    captured = []
    for error,reason in ((ValueError('evidence_size'),'evidence_size'),(RuntimeError('injected'),'worker_error')):
        store = SimpleNamespace(reserve=lambda *a:100,booking=lambda *a:Booking(2,'settled',True),
            insert_sample=lambda *a:captured.append(a),flush=lambda *a:None)
        rt = Runtime(settings(async_settle_enabled=False,release='a'*40,async_settle_shadow_workspaces='ws-v1'),runtime(),store)
        rt.signer = signer()
        def fail(*args, error=error, **kwargs):
            raise error
        monkeypatch.setattr(module,'sample',fail)
        dims = dimensions('openai','chat.completions',False)
        with rt.counters.day(NOW):
            for field in ('settle_attempts','observed_attempts','observed_unknown'):
                rt.counters.increment(dims,field)
        ctx = context()
        rt.process(Capture(rt,ctx.body,'settle',NOW,0,ctx.authorization),wire(),{'data':{'settled':True}},1,dims)
        drops = rt.counters.snapshot()[0][1]['drops']
        assert drops == [dict(phase='worker',adapter='openai',route_type='chat.completions',streamed=False,reason=reason,count=1)]
        rt.executor.shutdown()
    assert captured == []


def test_flush_failure_counts_drop_and_preserves_first_gap():
    import time
    from types import SimpleNamespace

    def fail(*args):
        raise RuntimeError('injected flush failure')
    rt = Runtime(settings(async_settle_enabled=False,release='a'*40),runtime(),SimpleNamespace(flush=fail))
    rt.counters.clock = lambda:1791244801
    rt.counters.reason(dimensions('openai','responses',False),'settle','usage_missing','exclusions')
    rt.counters.clock = lambda:1791244804
    rt.flush(time.monotonic()+1)
    body = rt.counters.snapshot()[0][1]
    assert body['first_gap_at_us'] == 1791244801000000
    assert body['drops'] == [dict(phase='worker',adapter='unknown',route_type='unknown',streamed=None,reason='store_unavailable',count=1)]
    rt.executor.shutdown()


def test_long_lived_writer_closes_seven_clean_days_before_new_writer():
    import time

    from scripts.async_settle.shadow_report import validate_counter
    from trusted_router.async_settle_shadow_evidence import day_at
    now = [1791244800.]
    db = Database()
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), EvidenceStore(db))
    rt.counters.clock = lambda: now[0]
    dims = dimensions('openai', 'responses', False)
    for n in range(8):
        now[0] = 1791244800. + 86400*n
        rt.last_flush = 0
        rt.flush(time.monotonic()+1)
        for prior in range(n):
            key = day_at(1791244800. + 86400*prior) + '/' + rt.counters.instance
            body = json.loads(db.rows[COUNTER, key])
            validate_counter(key, body)
            assert body['closed'] and body['first_gap_at_us'] is None
            assert body['flushed_at_us'] == int((1791244800. + 86400*(prior+1))*1e6)
        assert len(rt.counters.days) == 1
        rt.counters.increment(dims, 'authorize_attempts')
        rt.counters.increment(dims, 'authorize_fresh')
        now[0] += 10
        rt.last_flush = 0
        rt.flush(time.monotonic()+1)
    writes = [(params['id'], params) for _, params, _ in db.trace]
    # Each prior close's point read precedes the new day's first write/read.
    for n in range(1, 8):
        old = day_at(1791244800. + 86400*(n-1)) + '/' + rt.counters.instance
        new = day_at(1791244800. + 86400*n) + '/' + rt.counters.instance
        assert max(i for i, (key, _) in enumerate(writes) if key == old) < min(i for i, (key, _) in enumerate(writes) if key == new)
    rt.executor.shutdown()


def test_rollover_retains_inflight_receipt_and_failed_close(monkeypatch):
    import time

    from trusted_router.async_settle_shadow_evidence import day_at
    now = [1791244801.]
    db = Database()
    store = EvidenceStore(db)
    rt = Runtime(settings(async_settle_enabled=False, release='a'*40), runtime(), store)
    rt.counters.clock = lambda: now[0]
    received = now[0]
    rt.counters.retain(received)
    rt.flush(time.monotonic()+1)
    now[0] += 86400
    rt.last_flush = 0
    rt.flush(time.monotonic()+1)
    old = day_at(received) + '/' + rt.counters.instance
    assert not json.loads(db.rows[COUNTER, old])['closed']
    assert (COUNTER, day_at(now[0]) + '/' + rt.counters.instance) not in db.rows
    rt.counters.release(received)
    original = store.flush
    def fail_close(identity, body, deadline):
        if body['closed']:
            raise RuntimeError('lost close')
        return original(identity, body, deadline)
    monkeypatch.setattr(store, 'flush', fail_close)
    rt.last_flush = 0
    rt.flush(time.monotonic()+1)
    assert day_at(received) in rt.counters.days
    assert not json.loads(db.rows[COUNTER, old])['closed']
    monkeypatch.setattr(store, 'flush', original)
    rt.last_flush = 0
    rt.flush(time.monotonic()+1)
    assert json.loads(db.rows[COUNTER, old])['closed']
    assert day_at(received) not in rt.counters.days
    rt.executor.shutdown()


def test_cpu_budget_rejects_only_cold_tail(monkeypatch):

    from scripts.async_settle import shadow_benchmark
    calls, tick = 0, 0
    def cpu_clock():
        nonlocal calls, tick
        index = calls
        calls += 1
        if index % 2:
            tick += 6_000_000 if (index//2) % 6 < 5 else 100_000
        return tick
    monkeypatch.setattr(shadow_benchmark.time, 'thread_time_ns', cpu_clock)
    rejected = False
    try:
        shadow_benchmark.benchmark(iterations=6)
    except AssertionError as error:
        rejected = 'cold shadow comparator exceeds 5 ms CPU budget' in str(error)
    assert rejected, 'five 6 ms cold requests must fail even with a 0.1 ms warm p99'


def test_runtime_prewarms_only_when_shadow_opted_in(monkeypatch):
    from trusted_router.services import async_settle_shadow
    calls = []
    monkeypatch.setattr(async_settle_shadow, 'prewarm_catalog', lambda: calls.append('warm'))
    for workspaces, admission in (('', False), ('ws-v1', True), ('ws-v1', False)):
        rt = Runtime(settings(async_settle_enabled=admission, async_settle_shadow_workspaces=workspaces), runtime())
        rt.executor.shutdown()
    assert calls == ['warm']
