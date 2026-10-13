"""Live P4a shell witnesses using P3's real locked journal port."""
from __future__ import annotations

import copy
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from tests.test_async_settle_shadow import NOW, context, signer
from tests.test_async_settle_shadow_cohort import additions, config, fixture, inputs
from tests.test_async_settle_shadow_observation import event_for, fake_store
from tests.test_async_settle_ticket import endpoint, runtime
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_v2_contract import decide
from trusted_router.async_settle_shadow_v2_evidence import EVENT
from trusted_router.async_settle_shadow_v2_schema import day_at
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle_shadow_cohort_runtime import Runtime, map_requested_facts
from trusted_router.services.async_settle_shadow_health import Service


def installed():
    cfg = config(source_revision='a'*40)
    store, db = fake_store()
    store.wall = lambda: NOW
    service = Service(cfg, store)
    day = day_at(NOW*1000000)
    service.journal.grant(day, 100)
    service.journal.terminal_permits[day] = 100
    shell = Runtime(cfg, runtime(), service.port, SimpleNamespace(booking=lambda *_: Booking(2, 'settled', True)), typed=True)
    shell.signer = signer()
    return shell, service, db


def request(headers=()):
    return SimpleNamespace(headers=Headers(raw=[(k.lower().encode(), v.encode()) for k, v in headers]))


def mapped(**changes):
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    for key, value in changes.items():
        setattr(body, key, value)
    return map_requested_facts(body=body, authorization=ctx.authorization, endpoints=[endpoint()],
        typed=True, federated=False, runtime=runtime(), revision='a'*40, observed_us=NOW*1000000)


def test_mapping_probe_and_missing_facts():
    value = mapped()
    assert value.facts is not None and value.complete_candidates and value.complete_supported_context
    assert decide(value.facts, value.reason, value.candidates, complete_candidates=True).eligibility == 'eligible'
    for changes in ({'stream': None}, {'route_type': None}, {'requested_parameters': None},
                    {'requested_parameters': ['not-in-policy']}, {'model': 'alias'}, {'models': ['other']}):
        value = mapped(**changes)
        assert value.reason in {'missing_context', 'unknown_parameters'} and value.facts is None


@pytest.mark.parametrize('state', ['untyped', 'non_credits', 'settlement_authority', 'unsupported_route',
    'service_tier', 'app_markup', 'custom_markup', 'receipt_fee', 'custom_model', 'user_model',
    'native_batch', 'fusion', 'polyphemus', 'liberty', 'tool_cost', 'search_cost', 'image_cost',
    'video_cost', 'request_fee', 'private_tier_basis', 'partner', 'unsupported_adapter', 'unknown_parameters'])
def test_nonordinary_handler_facts_never_issue_through_mapper(state):
    from tests.test_async_settle_shadow_cohort import Port, issued
    ctx = context()
    auth = ctx.authorization
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=auth.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    endpoints = [endpoint()]
    fields = {'app_markup': 'app_markup_basis_points', 'custom_markup': 'custom_model_markup_basis_points',
              'receipt_fee': 'receipt_fee_basis_points', 'tool_cost': 'additional_cost_reservation_microdollars'}
    if state in fields:
        auth = replace(auth, **{fields[state]: 1})
    elif state in ('custom_model', 'user_model'):
        auth = replace(auth, **{'custom_model_id' if state == 'custom_model' else 'user_provided_model_id': 'custom'})
    elif state == 'non_credits':
        auth = replace(auth, usage_type='BYOK')
    elif state in ('service_tier', 'private_tier_basis'):
        body.service_tier = 'priority'
    elif state == 'native_batch':
        auth = replace(auth, native_batch_eligible=True)
    elif state in ('fusion', 'polyphemus', 'liberty', 'partner'):
        auth = replace(auth, model_id='trustedrouter/'+state)
        body.model = auth.model_id
    elif state in ('search_cost', 'unknown_parameters'):
        body.requested_parameters = ['web_search' if state == 'search_cost' else 'unrecognized']
    elif state in ('image_cost', 'video_cost', 'unsupported_route'):
        body.route_type = {'image_cost': 'images', 'video_cost': 'videos', 'unsupported_route': 'embeddings'}[state]
    elif state == 'request_fee':
        endpoints = [replace(endpoints[0], request_price_microdollars=1)]
    elif state == 'unsupported_adapter':
        endpoints = [replace(endpoints[0], provider='other')]
    value = map_requested_facts(body=body, authorization=auth, endpoints=endpoints,
        typed=state != 'untyped', federated=state == 'settlement_authority', runtime=runtime(),
        revision='a'*40, observed_us=NOW*1000000)
    port, response = issued(Port(), value)
    assert response == {}
    event = EVENT.parse(port.journal.pages[0].slots[0].body)
    assert event['eligibility'] == 'unknown' and event['diagnostic']['reason'] is not None


def test_request_thread_reserve_worker_thread_finalize_blocks_freeze_hwm_close():
    shell, service, _ = installed()
    try:
        slot = service.port.reserve(NOW*1000000, terminal=True)
        assert slot is not None
        day = service.journal.days[slot.day]
        assert service.journal.freeze(slot.day) == []
        service.journal.advance(slot.day)
        day.closing = True
        assert day.hwm == 0
        assert not service.journal.can_close(slot.day, inflight=0, pending=0, interval_end_us=0, folded_us=0)
        done = []
        thread = threading.Thread(target=lambda: done.append(service.port.finalize(slot, event_for(slot))))
        thread.start()
        thread.join()
        assert done[0] is not None
        assert service.journal.freeze(slot.day)
        assert day.hwm == 0
    finally:
        shell.close()


def test_p3_terminal_permit_exhaustion_has_no_slot_reproduction():
    shell, service, _ = installed()
    try:
        day = day_at(NOW*1000000)
        service.journal.terminal_permits[day] = 0
        assert service.port.reserve(NOW*1000000, terminal=True) is None
        assert service.port.lost.is_set()
        assert service.journal.days[day].next_seq == 1
    finally:
        shell.close()


def test_marker_absence_records_unresolved_without_response_fields(monkeypatch):
    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    monkeypatch.setattr(module, 'map_requested_facts', lambda **_: (_ for _ in ()).throw(AssertionError('marker absence must not map or project')))
    try:
        req = request()
        req.app = SimpleNamespace(state=SimpleNamespace())
        value = additions()
        before = copy.deepcopy(value)
        ctx = context()
        body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
            route_type='chat.completions', stream=False, requested_parameters=[])
        shell.authorize_http(req, body, ctx.authorization, value, [endpoint()], typed=True, federated=False, replay=False)
        assert value == before
        shell.join()
        event = EVENT.parse(service.journal.pages[0].slots[0].body)
        assert event['diagnostic']['reason'] == 'header_absent'
        assert service.counters[day_at(NOW*1000000)]['authorize_attempts'] == 1
    finally:
        shell.close()


def test_capture_is_reserved_unparsed_then_one_final_a_and_sample(monkeypatch):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    try:
        ctx = context()
        capture, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])]),
            ctx.body, ctx.authorization, 'settle')
        assert capture is not None and capture.prediction is None
        assert service.journal.slot(capture.reservation).body is None
        assert service.journal.freeze(capture.reservation.day) == []
        capture.endpoint = endpoint()
        shell.process(capture)
        event = EVENT.parse(service.journal.slot(capture.reservation).body)
        assert (event['partition'], event['eligibility']) == ('A', 'eligible')
        assert event['dimensions'] == inputs().dimensions
        assert len(service.journal.batches[capture.reservation]) == 1
        counter = service.counters[capture.reservation.day]
        assert (counter['A'], counter['C'], counter['settle_attempts'], counter['eligible_demand']) == (1, 0, 1, 1)
        assert counter['admission_observer']['prediction_unknown'] == 1
        shell.process(capture)
        assert service.counters[capture.reservation.day]['A'] == 1
        shell.release(size)
    finally:
        shell.close()


def test_typed_binding_cannot_certify_an_untyped_terminal(monkeypatch):
    shell, service, _ = installed()
    shell.typed = False
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    try:
        ctx = context()
        cap, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])]),
            ctx.body, ctx.authorization, 'settle')
        cap.endpoint = endpoint()
        shell.process(cap)
        event = EVENT.parse(service.journal.slot(cap.reservation).body)
        assert (event['partition'], event['eligibility'], event['diagnostic']['reason']) == ('C', 'unknown', 'untyped')
        assert event['identity']['settle_origin'] == 'legacy'
        shell.release(size)
    finally:
        shell.close()


def test_guard_refusal_inline_c_and_missing_header_worker_c(monkeypatch):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    try:
        ctx = context()
        shell.tokens = 0
        cap, _ = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'refund')
        assert cap is None
        shell.join()
        event = EVENT.parse(service.journal.pages[0].slots[0].body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == 'rate_limit'
        shell.tokens = 10
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'refund')
        assert cap is not None
        shell.process(cap)
        event = EVENT.parse(service.journal.slot(cap.reservation).body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == 'header_absent'
        shell.release(size)
    finally:
        shell.close()


def test_shadow_capture_failure_preserves_callback_error(monkeypatch):
    shell, _, _ = installed()
    try:
        monkeypatch.setattr(shell, 'capture_terminal', lambda *_: (_ for _ in ()).throw(ValueError('shadow')))
        ctx = context()
        assert shell.terminal(request(), ctx.body, ctx.authorization, 'refund', lambda: b'legacy') == b'legacy'
        with pytest.raises(RuntimeError, match='money'):
            shell.terminal(request(), ctx.body, ctx.authorization, 'refund', lambda: (_ for _ in ()).throw(RuntimeError('money')))
    finally:
        shell.close()


def test_terminal_http_failure_cannot_repeat_money_or_replace_response(monkeypatch):
    from trusted_router.services.async_settle_shadow_cohort_runtime import terminal_http
    shell, _, _ = installed()
    ctx = context()
    req = request()
    req.app = SimpleNamespace(state=SimpleNamespace(async_settle_shadow_cohort=shell))
    calls = []
    def money():
        calls.append(1)
        return b'legacy'
    try:
        for after in (False, True):
            def broken(req, body, auth, kind, callback, after=after):
                if after:
                    callback()
                raise ValueError('shadow')
            monkeypatch.setattr(shell, 'terminal', broken)
            calls.clear()
            assert terminal_http(req, ctx.body, ctx.authorization, 'settle', money) == b'legacy'
            assert calls == [1]
    finally:
        shell.close()


def test_missing_prediction_basis_retains_unfinished_eligible_slot(monkeypatch):
    from trusted_router.services.async_settle_shadow_cohort_runtime import receipt_prediction
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    monkeypatch.setattr(service.port, 'capture_prediction', lambda *_: None)
    try:
        ctx = context()
        cap, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])]),
            ctx.body, ctx.authorization, 'settle')
        assert cap is not None
        cap.endpoint = endpoint()
        from trusted_router.async_settle_shadow_cohort import VerifiedTerminal
        with pytest.raises(ValueError, match='prediction_demand_gap'):
            receipt_prediction(cap, VerifiedTerminal('C', 'eligible', 'none', 'booking_unknown'))
        shell.work(cap, size)
        assert service.journal.slot(cap.reservation).body is None
        assert service.journal.freeze(cap.reservation.day) == []
        assert service.port.lost.is_set()
    finally:
        shell.close()


def test_p3_nullable_dimension_fold_defect_reproduction():
    from tests.test_async_settle_shadow_cohort import terminal
    from trusted_router.async_settle_shadow_cohort import receipt_bucket
    shell, service, _ = installed()
    try:
        event = terminal(partition='C', eligibility='unknown', prediction=None,
            dimensions=dict(adapter='openai', route_type=None, streamed=None))
        with pytest.raises(StopIteration):
            service.port.fold_counts(day_at(NOW*1000000), receipt_bucket(event))
    finally:
        shell.close()


def test_runtime_replay_links_original_without_fact_mapping(monkeypatch):
    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    req = request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')])
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    try:
        shell.authorize_http(req, body, ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        monkeypatch.setattr(module, 'map_requested_facts', lambda **_: (_ for _ in ()).throw(AssertionError('refreeze')))
        second = {}
        shell.authorize_http(req, body, ctx.authorization, second, [endpoint()], typed=True, federated=False, replay=True)
        events = [EVENT.parse(s.body) for p in service.journal.pages for s in p.slots]
        assert len(events) == 2 and second == {}
        assert events[1]['phase'] == 'replay'
        assert events[1]['decision'] == events[0]['decision']
        assert events[1]['retry_original']['event_seq'] == events[0]['seq']
    finally:
        shell.close()


@pytest.mark.parametrize('durable', [False, True])
def test_worker_retry_keeps_original_sample_and_each_attempt(monkeypatch, durable):
    from trusted_router.storage_gcp_shadow_observation import Budget
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    req = request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])])
    try:
        first, size = shell.capture_terminal(req, ctx.body, ctx.authorization, 'settle')
        first.endpoint = endpoint()
        shell.process(first)
        rows = service.journal.batches[first.reservation]
        if durable:
            service.store.retain(rows, Budget(service.store.clock()+2, service.store.clock))
            del service.journal.batches[first.reservation]
        shell.release(size)
        second, size = shell.capture_terminal(req, ctx.body, ctx.authorization, 'settle')
        second.endpoint = endpoint()
        shell.process(second)
        event = EVENT.parse(service.journal.slot(second.reservation).body)
        assert event['partition'] == 'A'
        assert event['retry_original'] == rows[0].value()['original_terminal_receipt']
        assert service.journal.batches[second.reservation] == ()
        assert service.counters[first.reservation.day]['A'] == 2
        assert service.counters[first.reservation.day]['eligible_demand'] == 2
        shell.release(size)
    finally:
        shell.close()


@pytest.mark.parametrize('variant', ['conflict', 'winner_polarity'])
def test_worker_conflict_and_opposite_winner_preserve_first_sample(monkeypatch, variant):
    from trusted_router.async_settle_shadow_v2_schema import sha256
    from trusted_router.detached_jws import b64encode, canonical
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    try:
        value = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
        first, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', b64encode(canonical(value)))]),
            ctx.body, ctx.authorization, 'settle')
        first.endpoint = endpoint()
        shell.process(first)
        rows = service.journal.batches[first.reservation]
        shell.release(size)
        kind = 'refund' if variant == 'winner_polarity' else 'settle'
        value['terminal'].update(terminal_kind=kind, charge_micro=0 if kind == 'refund' else 3)
        value['payload_hash'] = sha256(value['terminal'])
        second, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', b64encode(canonical(value)))]),
            ctx.body, ctx.authorization, kind)
        second.endpoint = endpoint()
        shell.process(second)
        event = EVENT.parse(service.journal.slot(second.reservation).body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == variant
        assert event['retry_original'] == rows[0].value()['original_terminal_receipt']
        assert service.journal.batches[first.reservation] == rows
        assert service.journal.batches[second.reservation] == ()
        assert service.journal.days[first.reservation.day].resets == int(variant == 'conflict')
        shell.release(size)
    finally:
        shell.close()


def test_p3_later_booking_completion_backing_defect_reproduction(monkeypatch):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell.booking_store.booking = lambda *_: Booking()
    ctx = context()
    try:
        first, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])]),
            ctx.body, ctx.authorization, 'settle')
        first.endpoint = endpoint()
        shell.process(first)
        rows = service.journal.batches[first.reservation]
        original = service.journal.freeze(first.reservation.day)[0]
        event = EVENT.parse(service.journal.slot(first.reservation).body)
        later = service.port.reserve(NOW*1000000+1)
        completion = {**event, 'phase': 'completion', 'seq': f'{later.event_seq:020}',
            'event_at_us': later.observed_us, 'receipt_at_us': later.observed_us,
            'prediction': None, 'completion_of': event['seq'],
            'retry_original': rows[0].value()['original_terminal_receipt'],
            'booking': dict(outcome='settled', winner='settle', reference='a'*64)}
        # P3 invokes the shared P1-A1 completion-context function here. The
        # completion preserves the original C class and is contextually valid.
        assert service.port.complete_event(later, original, completion) is not None
        assert service.port.complete(later)
        pages = service.journal.freeze(later.day)
        with pytest.raises(ValueError, match='reference_conflict'):
            service.journal.verify_complete(later, [*pages, *rows])
        shell.release(size)
    finally:
        shell.close()


def test_later_retry_fact_is_c_without_reclassifying_original(monkeypatch):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    req = request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])])
    try:
        shell.booking_store.booking = lambda *_: Booking()
        first, size = shell.capture_terminal(req, ctx.body, ctx.authorization, 'settle')
        first.endpoint = endpoint()
        shell.process(first)
        original_bytes = service.journal.slot(first.reservation).body
        shell.release(size)
        shell.booking_store.booking = lambda *_: Booking(2, 'settled', True)
        second, size = shell.capture_terminal(req, ctx.body, ctx.authorization, 'settle')
        second.endpoint = endpoint()
        shell.process(second)
        event = EVENT.parse(service.journal.slot(second.reservation).body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == 'retry_original_gap'
        assert service.journal.slot(first.reservation).body == original_bytes
        assert service.counters[first.reservation.day]['C'] == service.counters[first.reservation.day]['eligible_demand'] == 2
        assert service.port.lost.is_set()
        shell.release(size)
    finally:
        shell.close()


def test_bounded_capture_drops_request_content(monkeypatch):
    shell, _, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    ctx.body.metadata = {'content': 'x'*100000}
    ctx.body.model_extra['private'] = 'x'*100000
    ctx.authorization.tags['private'] = 'x'*100000
    try:
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'refund')
        assert cap is not None and cap.body is not ctx.body
        assert cap.body.metadata is None and cap.body.model_extra == {}
        assert cap.authorization is not ctx.authorization and cap.authorization.tags == {}
        shell.release(size)
    finally:
        shell.close()


def test_compact_fact_overflow_finalizes_one_c_inline(monkeypatch):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    ctx.body.route_type = 'x'*129
    try:
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'settle')
        assert cap is None and size == 0
        shell.join()
        assert shell.pending == 0
        event = EVENT.parse(service.journal.pages[0].slots[0].body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == 'local_work_overflow'
        assert service.counters[day_at(NOW*1000000)]['settle_attempts'] == 1
        assert service.port.lost.is_set()
    finally:
        shell.close()


@pytest.mark.parametrize('failure', ['booking', 'sample_read'])
def test_worker_failures_keep_eligible_c_and_prediction(monkeypatch, failure):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    def failed(*_):
        raise OSError('unavailable')
    if failure == 'booking':
        shell.booking_store.booking = failed
    else:
        monkeypatch.setattr(service.store, 'read', failed)
    try:
        ctx = context()
        cap, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', fixture('terminal_compared_full')[0]['header'])]),
            ctx.body, ctx.authorization, 'settle')
        assert cap is not None
        cap.endpoint = endpoint()
        shell.work(cap, size)
        event = EVENT.parse(service.journal.slot(cap.reservation).body)
        assert event['partition'] == 'C' and event['eligibility'] == 'eligible'
        assert event['prediction'] is not None
        counter = service.counters[cap.reservation.day]
        assert counter['eligible_demand'] == counter['C'] == counter['settle_attempts'] == 1
        assert counter['A'] == counter['B'] == 0
    finally:
        shell.close()


@pytest.mark.parametrize('full', ['tasks', 'bytes', 'stopped'])
def test_shared_guard_refusals_each_finalize_one_c(monkeypatch, full):
    shell, service, _ = installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    if full == 'tasks':
        shell.pending = 32
    elif full == 'bytes':
        shell.queued_bytes = 256*1024
    else:
        shell.stopped = True
    try:
        ctx = context()
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'refund')
        assert cap is None and size == 0
        shell.join()
        event = EVENT.parse(service.journal.pages[0].slots[0].body)
        assert event['partition'] == 'C' and event['diagnostic']['reason'] == 'queue_full'
        assert service.counters[day_at(NOW*1000000)]['refund_attempts'] == 0
        assert service.port.lost.is_set()
    finally:
        shell.close()


@pytest.mark.parametrize('lock_name', ['guard', 'queue', 'port', 'cleanup'])
def test_terminal_lock_handshake_returns_before_exact_lock_release(monkeypatch, lock_name):
    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell, service, _ = installed()
    ctx = context()
    real = {'guard': shell.guard, 'queue': shell.queue.mutex,
            'port': service.lock, 'cleanup': shell.guard}[lock_name]
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    attempts, result, money = [], [], []
    class Handshake:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == 'request-handshake':
                attempts.append((args, kwargs))
                assert kwargs == {'blocking': False}
            return real.acquire(*args, **kwargs)
        def release(self):
            real.release()
    if lock_name in {'guard', 'cleanup'}:
        shell.guard = Handshake()
    elif lock_name == 'queue':
        shell.queue.mutex = Handshake()
    else:
        service.lock = Handshake()
    def holder():
        with real:
            held.set()
            assert release.wait(10)
    owner = threading.Thread(target=holder)
    if lock_name != 'cleanup':
        owner.start()
        assert held.wait(10)
    else:
        def broken(body):
            owner.start()
            assert held.wait(10)
            raise ValueError('compact failure after admission')
        monkeypatch.setattr(module, 'compact_terminal_facts', broken)
    def callback():
        money.append(1)
        return b'committed money response'
    def caller():
        try:
            result.append(shell.terminal(request(), ctx.body, ctx.authorization, 'settle', callback))
        finally:
            done.set()
    caller_thread = threading.Thread(target=caller, name='request-handshake')
    try:
        caller_thread.start()
        assert done.wait(10), 'request must return while the exact lock is still held'
        assert not release.is_set() and attempts
        assert result == [b'committed money response'] and money == [1]
        assert service.port.lost.is_set()
        assert all(not slot.completion_requested for page in service.journal.pages for slot in page.slots)
    finally:
        release.set()
        owner.join(10)
        caller_thread.join(10)
        # Restore the real worker locks before it drains queued obligations.
        if lock_name in {'guard', 'cleanup'}:
            shell.guard = real
        elif lock_name == 'queue':
            shell.queue.mutex = real
        else:
            service.lock = real
        shell.join()
        shell.close()


@pytest.mark.parametrize('failure', ['full', 'stopped'])
def test_refused_dispatch_retains_one_final_c_without_completion(monkeypatch, failure):
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell, service, _ = installed()
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        assert release.wait(10)
    shell.dispatch(block)
    assert entered.wait(10)
    try:
        if failure == 'full':
            for _ in range(32):
                shell.queue.put_nowait((lambda: None, ()))
        else:
            shell.close()
            release.set()
            shell.worker.join(10)
            assert not shell.worker.is_alive()
        ctx = context()
        calls = []
        assert shell.terminal(request(), ctx.body, ctx.authorization, 'refund',
                              lambda: calls.append(1) or b'ok') == b'ok'
        assert calls == [1]
        slots = [slot for page in service.journal.pages for slot in page.slots]
        assert len(slots) == 1 and EVENT.parse(slots[0].body)['partition'] == 'C'
        assert not slots[0].completion_requested and service.port.lost.is_set()
        assert service.journal.days[slots[0].reservation.day].hwm == 0
    finally:
        release.set()
        shell.join()
        shell.close()


def test_observed_exclusion_worker_has_no_eligible_demand_or_prediction(monkeypatch):
    from trusted_router.detached_jws import b64encode, canonical
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell, service, _ = installed()
    ctx = context()
    value = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
    value['observed']['service_tier'] = 'unsupported'
    value.update(terminal=None, payload_hash=None, go_error='unsupported_observed')
    try:
        cap, size = shell.capture_terminal(request([('X-TR-Settlement-Shadow', b64encode(canonical(value)))]),
            ctx.body, ctx.authorization, 'settle')
        cap.endpoint = endpoint()
        shell.process(cap)
        event = EVENT.parse(service.journal.slot(cap.reservation).body)
        assert cap.verified.comparison.observed_eligible is False
        assert (event['partition'], event['eligibility'], event['prediction']) == ('C', 'excluded', None)
        counter = service.counters[cap.reservation.day]
        assert counter['eligible_demand'] == 0 and counter['C'] == 1
        assert all(counter['admission_observer']['prediction_'+label] == 0 for label in ('yes', 'no', 'unknown'))
        shell.release(size)
    finally:
        shell.close()


@pytest.mark.parametrize('condition', ['fresh', 'nonmember', 'disabled_policy', 'money_async',
    'missing_capability', 'duplicate_capability', 'unusable_capability', 'unresolved', 'failed_exposure', 'replay'])
def test_nonce_echo_exact_erratum_conditions(monkeypatch, condition):
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell, service, _ = installed()
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    marker = ('X-TR-Settlement-Shadow-Policy', 'shadow-v2')
    headers = [marker]
    if condition == 'nonmember':
        shell.settings._async_settle_shadow_workspace_ids = frozenset({'other'})
    elif condition == 'disabled_policy':
        shell.settings.async_settle_shadow_policy = ''
    elif condition == 'money_async':
        shell.settings.async_settle_enabled = True
    elif condition == 'missing_capability':
        headers = []
    elif condition == 'duplicate_capability':
        headers = [marker, marker]
    elif condition == 'unusable_capability':
        headers = [(marker[0], 'shadow-v1')]
    elif condition == 'unresolved':
        body.requested_parameters = None
    elif condition == 'failed_exposure':
        monkeypatch.setattr(service.port, 'finalize', lambda *_: None)
    values = additions()
    try:
        shell.authorize_http(request(headers), body, ctx.authorization, values, [endpoint()],
            typed=True, federated=False, replay=condition == 'replay')
        shell.join()
        assert ('invocation_nonce' in values) is (condition == 'fresh')
        if condition == 'fresh':
            from trusted_router.async_settle_shadow_binding import verify_binding
            binding = verify_binding(values['billing_shadow_binding'], [shell.signer.trusted], NOW)
            assert values['invocation_nonce'] == binding.invocation_nonce == ctx.authorization.invocation_nonce
        else:
            assert 'billing_shadow_binding' not in values
    finally:
        shell.close()


def test_request_threads_never_fold_and_capture_never_validates_contract(monkeypatch):
    import sys

    from trusted_router.async_settle_shadow_v2_schema import Schema
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    shell, service, _ = installed()
    ctx = context()
    entered, release = threading.Event(), threading.Event()
    shell.dispatch(lambda: (entered.set(), release.wait(10)))
    assert entered.wait(10)
    calls = []
    codes = {Schema.__call__.__code__: 'contract', type(service.port).fold_counts.__code__: 'fold'}
    def recorder(frame, event, arg):
        if event == 'call' and frame.f_code in codes:
            calls.append(codes[frame.f_code])
    try:
        sys.setprofile(recorder)
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'settle')
        sys.setprofile(None)
        assert calls == []
        body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
            route_type='chat.completions', stream=False, requested_parameters=[])
        sys.setprofile(recorder)
        values = additions()
        calls.clear()
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]), body,
            ctx.authorization, values, [endpoint()], typed=True, federated=False, replay=False)
        sys.setprofile(None)
        assert 'fold' not in calls
        # Named remaining seam: the unchanged P3 finalize API mandates EVENT
        # validation/serialization before byte exposure. Do not bypass it.
        assert 'contract' in calls, 'P3_FINALIZE_VALIDATION seam changed; reassess the no-validation requirement'
        assert all(not slot.completion_requested for page in service.journal.pages for slot in page.slots)
        assert service.journal.days[cap.reservation.day].hwm == 0
        shell.release(size)
    finally:
        sys.setprofile(None)
        release.set()
        shell.join()
        shell.close()
