"""P4a compared core and nonblocking authorize facade witnesses."""
from __future__ import annotations

import ast
import copy
import json
import sys
from dataclasses import replace
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_async_settle_shadow import NOW, context, signer
from trusted_router.async_settle_shadow_binding import LIFETIME, verify_binding
from trusted_router.async_settle_shadow_cohort import (
    prepare_authorization,
    receipt_bucket,
    receipt_demand,
    sample_retry,
    terminal_receipt,
    verify_terminal,
)
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_journal import Journal, Reservation
from trusted_router.async_settle_shadow_v2_a1_reconciliation import (
    authorization_obligation,
    event_locator,
)
from trusted_router.async_settle_shadow_v2_contract import (
    COHORT_EXCLUSIONS,
    FEATURES,
    NEVER_EXCLUSIONS,
    RequestedFacts,
    generation_id,
)
from trusted_router.async_settle_shadow_v2_evidence import EVENT, committed_identity
from trusted_router.async_settle_shadow_v2_schema import day_at
from trusted_router.config import Settings, in_v2_cohort
from trusted_router.detached_jws import b64encode, canonical
from trusted_router.services.async_settle_shadow_cohort import (
    AuthorizeInput,
    authorize,
    has_v2_capability,
)

# Importing the shared decorator also constructs P1's unrelated exhaustive
# mutation inventories. Keep that collection work out of line tracing, just as
# its exhaustive tests do; all P4 imports above and traced witnesses stay traced.
_collection_trace = sys.gettrace()
sys.settrace(None)
try:
    from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
finally:
    sys.settrace(_collection_trace)


def fixture(name: str) -> list[dict[str, Any]]:
    path = Path(__file__).parent/'fixtures/async_settlement/shadow_v2'/(name+'.json')
    return json.loads(path.read_bytes())['variants']


def inputs() -> AuthorizeInput:
    ctx = context()
    auth = ctx.authorization
    f = copy.deepcopy(fixture('receipt_pages')[1]['object']['facts'])
    f.update(settle_origin='typed', usage_class='Credits', billing_authority='local',
        route='chat.completions', streamed=False, service_tier=None, app_markup=0,
        custom_markup=0, receipt_requested=False, receipt_fee=0, commercial_program='ordinary',
        unknown_features=[])
    f['features'] = dict.fromkeys(f['features'], False)
    identity = dict(authorization_id=auth.id, generation_id=generation_id(auth.id),
        workspace_id=auth.workspace_id, key_id=auth.key_hash, invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id, billing_authority=auth.settlement,
        journal_region=ctx.region, epoch=ctx.epoch, settle_origin='typed')
    return AuthorizeInput(NOW*1000000, identity, 'a'*40,
        dict(adapter='openai', route_type='chat.completions', streamed=False),
        RequestedFacts.from_object(f), canonical([dict(endpoint_id=ctx.selected_endpoint,
        adapter='openai', features=[])]), True, True, None)


def config(**changes: Any) -> Settings:
    return Settings(environment='test', **dict(async_settle_shadow_policy='shadow-v2',
        async_settle_shadow_workspaces='ws-v1', async_settle_enabled=False,
        async_settle_protection=changes.get('async_settle_enabled', False)) | changes)


def additions() -> dict[str, Any]:
    from trusted_router.async_settle_shadow_wire import snapshot_from_object
    from trusted_router.billing_snapshot import canonical_hash
    snapshot = fixture('authorize_compared')[0]['object']['billing_snapshot']
    return dict(billing_snapshot=snapshot, billing_snapshot_hash=canonical_hash(snapshot_from_object(snapshot)), async_eligible=False)


class Port:
    def __init__(self, *, permits: bool = True) -> None:
        self.journal = Journal('writer')
        if permits:
            self.journal.grant(day_at(NOW*1000000), 100)
        self.buckets: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def reserve(self, observed_us: int, *, terminal: bool = False) -> Reservation | None:
        self.calls.append('reserve')
        return self.journal.reserve(observed_us)

    def finalize(self, reservation: Reservation, event: dict[str, Any]) -> bytes:
        self.calls.append('finalize')
        return self.journal.finalize(reservation, event)

    def complete(self, reservation: Reservation) -> bool:
        self.calls.append('complete')
        return self.journal.complete(reservation)

    def fold_counts(self, day: str, bucket: dict[str, Any]) -> bool:
        self.calls.append('fold')
        self.buckets.append(bucket)
        return True


def issued(port: Port | None = None, value: AuthorizeInput | None = None) -> tuple[Port, dict[str, Any]]:
    port = port or Port()
    result = authorize(config(), ('shadow-v2',), port, value or inputs(), additions(), signer())
    return port, result


def original() -> tuple[dict[str, Any], dict[str, Any]]:
    port, result = issued()
    assert set(result) == {'billing_shadow_policy', 'billing_shadow_binding'}
    page = port.journal.freeze(day_at(NOW*1000000))[0].value()
    return page, event_locator(page, page['first_seq'])


def terminal(**changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(fixture('receipt_pages')[2]['object'])
    value.update(phase='settle', partition='A', eligibility='eligible', proof_kind='compared',
        comparison='exact', original_receipt=None, certificate_sha256=None,
        payload_sha256='a'*64, prediction=copy.deepcopy(fixture('sample')[1]['object']),
        booking=dict(outcome='settled', winner='settle', reference='b'*64))
    value.update(changes)
    EVENT(value)
    return value


def _case_gate_cartesian_zero_effects(pin: str, member: bool, money: bool, marker: tuple[str, ...]) -> None:
    cfg = config(async_settle_shadow_policy=pin, async_settle_shadow_workspaces='ws-v1' if member else 'other', async_settle_enabled=money)
    expected = pin == 'shadow-v2' and member and not money and marker == ('shadow-v2',)
    cohort = in_v2_cohort(pin, 'ws-v1', money, cfg.async_settle_shadow_workspace_ids)
    assert cohort is (pin == 'shadow-v2' and member and not money)
    assert has_v2_capability(marker) is (marker == ('shadow-v2',))
    port = Port()
    before = additions()
    result = authorize(cfg, marker, port, inputs(), before, signer())
    assert bool(result) is expected
    assert before == additions()
    if not cohort:
        assert port.calls == [] and port.journal.days[day_at(NOW*1000000)].next_seq == 1


@exhaustive
def test_gate_cartesian_zero_effects() -> None:
    for pin, member, money, marker in product(['', 'shadow-v1', 'shadow-v2', 'SHADOW-V2'], [False, True], [False, True], [(), ('shadow-v2',), ('wrong',), ('shadow-v2', 'shadow-v2')]):
        _case_gate_cartesian_zero_effects(pin, member, money, marker)


def test_gate_traced_branches() -> None:
    for pin, member, money, marker in (
        ('', True, False, ('shadow-v2',)),
        ('shadow-v2', False, False, ('shadow-v2',)),
        ('shadow-v2', True, True, ('shadow-v2',)),
        ('shadow-v2', True, False, ()),
        ('shadow-v2', True, False, ('shadow-v2',)),
    ):
        _case_gate_cartesian_zero_effects(pin, member, money, marker)


def test_binding_bytes_and_finalize_before_exposure(monkeypatch: Any) -> None:
    from tests.test_async_settle_ticket import endpoint, runtime
    from trusted_router.services import async_settle_shadow as v1
    monkeypatch.setattr(v1, 'prewarm_catalog', lambda: None)
    monkeypatch.setattr(v1.time, 'time', lambda: NOW)
    legacy = v1.Runtime(config(), runtime())
    legacy.signer = signer()
    old = additions()
    try:
        legacy.authorize(context().authorization, old, replay=False, header=True,
                         route='chat.completions', streamed=False, endpoints=[endpoint()])
    finally:
        legacy.executor.shutdown()
    port, result = issued()
    assert port.calls == ['reserve', 'finalize', 'complete', 'fold']
    assert result['billing_shadow_binding'] == old['billing_shadow_binding']
    assert additions()['billing_snapshot'] == old['billing_snapshot']
    slot = port.journal.pages[0].slots[0]
    assert slot.body is not None and slot.completion_requested
    assert port.journal.days[slot.reservation.day].hwm == 0
    claims = verify_binding(result['billing_shadow_binding'], [signer().trusted], NOW)
    assert claims.exp-claims.iat == LIFETIME
    assert 'billing_shadow_exclusion' not in result
    assert port.buckets[0]['requested_eligible'] == 1


def _case_authorize_failure_isolation(failure: str, monkeypatch: Any) -> None:
    port = Port(permits=failure != 'reserve')
    data = additions()
    value = inputs()
    if failure == 'finalize':
        monkeypatch.setattr(port, 'finalize', lambda *a: None)
    if failure == 'bytes':
        finalize = port.finalize
        monkeypatch.setattr(port, 'finalize', lambda *a: finalize(*a)+b' ')
    if failure == 'snapshot':
        data['billing_snapshot_hash'] = '0'*64
    if failure == 'context':
        value = replace(value, complete_supported_context=False)
    before = copy.deepcopy(data)
    try:
        result: Any = authorize(config(), ('shadow-v2',), port, value, data, None if failure == 'signer' else signer())
    except Exception:
        result = 'escaped'
    assert result == {}
    assert data == before


@exhaustive
def test_authorize_failure_isolation(monkeypatch: Any) -> None:
    for failure in ['reserve', 'finalize', 'bytes', 'signer', 'snapshot', 'context']:
        _case_authorize_failure_isolation(failure, monkeypatch)


def _case_every_noneligible_reason_never_emits(reason: str) -> None:
    value = inputs()
    assert value.facts is not None
    facts = value.facts.object()
    if reason in facts['features']:
        facts['features'][reason] = True
    elif reason in ('partner', 'unsupported_adapter'):
        facts['commercial_program'] = 'partner'
    elif reason in ('app_markup', 'custom_markup'):
        facts[reason] = 1
    elif reason == 'receipt_fee':
        facts['receipt_requested'] = True
    elif reason == 'service_tier':
        facts['service_tier'] = 'priority'
    elif reason == 'unsupported_route':
        facts['route'] = 'embeddings'
    elif reason == 'untyped':
        facts['settle_origin'] = 'legacy'
    elif reason == 'non_credits':
        facts['usage_class'] = 'BYOK'
    elif reason == 'settlement_authority':
        facts['billing_authority'] = 'federated'
    value = replace(value, reason=reason, facts=RequestedFacts.from_object(facts))
    port, result = issued(value=value)
    assert result == {}
    event = EVENT.parse(port.journal.pages[0].slots[0].body)
    assert (event['partition'], event['eligibility']) == ('C', 'unknown')
    assert event['diagnostic']['reason'] == ('missing_context' if reason == 'unknown_parameters' else reason)
    assert not hasattr(port, 'certificate')
    # P2 receives no v2 fields for this decision and sends no usable variant.
    assert verify_terminal((), context(), [signer().trusted], local_bytes=1).partition == 'C'


@exhaustive
def test_every_noneligible_reason_never_emits() -> None:
    for reason in sorted(COHORT_EXCLUSIONS | NEVER_EXCLUSIONS):
        _case_every_noneligible_reason_never_emits(reason)


def _case_missing_fact_never_defaults(missing: str | None) -> None:
    value = inputs()
    assert value.facts is not None
    if missing is not None:
        facts = value.facts.object()
        del facts['features'][missing]
        with pytest.raises(ValueError):
            RequestedFacts.from_object(facts)
    else:
        port, result = issued(value=replace(value, facts=None))
        assert result == {}
        assert EVENT.parse(port.journal.pages[0].slots[0].body)['eligibility'] == 'unknown'
        prepared = prepare_authorization(Reservation(day_at(value.observed_us), 'writer', 1, 1, value.observed_us),
            identity=committed_identity(value.identity), revision=value.revision,
            dimensions=value.dimensions, facts=None, candidates=value.candidates,
            complete_candidates=True, complete_supported_context=True, reason=None)
        assert prepared.decision.eligibility == 'unknown'


@exhaustive
def test_missing_fact_never_defaults() -> None:
    for missing in [None, *FEATURES]:
        _case_missing_fact_never_defaults(missing)


def test_mixed_candidates_without_supported_context() -> None:
    value = inputs()
    candidates = [dict(endpoint_id='unsupported', adapter='other', features=[]),
                  dict(endpoint_id=context().selected_endpoint, adapter='openai', features=[])]
    port, result = issued(value=replace(value, candidates=canonical(candidates), complete_supported_context=False))
    assert result == {}
    assert EVENT.parse(port.journal.pages[0].slots[0].body)['eligibility'] == 'unknown'
    reserved = Reservation(day_at(value.observed_us), 'writer', 1, 1, value.observed_us)
    prepared = prepare_authorization(reserved, identity=committed_identity(value.identity),
        revision=value.revision, dimensions=value.dimensions, facts=value.facts,
        candidates=canonical(candidates), complete_candidates=True,
        complete_supported_context=False, reason=None)
    assert prepared.decision.eligibility == 'unknown'


def test_replay_links_original_and_never_signs_or_refreezes() -> None:
    page, locator = original()
    port = Port()
    value = replace(inputs(), observed_us=(NOW+1)*1000000)
    assert authorize(config(), ('shadow-v2',), port, value, {}, None, replay=True,
                     original_page=page, original_locator=locator) == {}
    replay_page = port.journal.freeze(day_at(value.observed_us))[0].value()
    replay = replay_page['events'][0]
    assert replay['phase'] == 'replay' and replay['decision'] == page['events'][0]['decision']
    obligation = authorization_obligation(replay_page, replay['seq'], original_page=page, original_event_seq=locator['event_seq'])
    assert obligation['status'] == 'open'
    assert obligation['horizon_at_us'] == (NOW+LIFETIME)*1000000
    assert port.buckets[0]['authorize_replay'] == 1


def _case_p2_bytes_delegate_to_unchanged_comparator(variant: str) -> None:
    checked = verify_terminal((fixture(variant)[0]['header'],), context(), [signer().trusted],
                              local_bytes=1000)
    assert (checked.partition, checked.eligibility, checked.proof_kind) == ('A', 'eligible', 'compared')
    assert checked.comparison is not None and checked.comparison.python_micro == 2


@exhaustive
def test_p2_bytes_delegate_to_unchanged_comparator() -> None:
    for variant in ['terminal_compared_full', 'terminal_compared_hash_only']:
        _case_p2_bytes_delegate_to_unchanged_comparator(variant)


def _case_compared_failures_remain_c(damage: str) -> None:
    ctx = context()
    keys = [signer().trusted]
    headers = (fixture('terminal_compared_full')[0]['header'],)
    size = 1000
    if damage == 'missing_header':
        headers = ()
    elif damage == 'bad_header':
        headers = ('!',)
    elif damage == 'local_cap':
        size = 65537
    elif damage == 'unverified':
        keys = []
    elif damage == 'booking_unknown':
        ctx = replace(ctx, attempted_kind='refund', booking=Booking())
    elif damage == 'opposite_winner':
        ctx = replace(ctx, booking=Booking(0, 'refunded', True))
    elif damage == 'excluded':
        headers = (fixture('terminal_excluded_settle')[0]['header'],)
    elif damage in ('hash', 'amount'):
        wire = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
        wire['terminal']['charge_micro'] += 1
        if damage == 'amount':
            import hashlib
            wire['payload_hash'] = hashlib.sha256(canonical(wire['terminal'])).hexdigest()
        headers = (b64encode(canonical(wire)),)
    checked = verify_terminal(headers, ctx, keys, local_bytes=size)
    assert checked.partition == 'C'
    assert checked.eligibility in ('unknown', 'eligible')
    if damage == 'amount':
        assert checked.comparison is not None and checked.comparison.classification == 'evaluator_disagreement'


@exhaustive
def test_compared_failures_remain_c() -> None:
    for damage in ['missing_header', 'bad_header', 'local_cap', 'unverified', 'booking_unknown', 'opposite_winner', 'excluded', 'hash', 'amount']:
        _case_compared_failures_remain_c(damage)


def _case_original_binding_lifetime(offset: int, valid: bool) -> None:
    _, response = issued()
    if valid:
        verify_binding(response['billing_shadow_binding'], [signer().trusted], NOW+offset)
    else:
        with pytest.raises(ValueError, match='proof_expired'):
            verify_binding(response['billing_shadow_binding'], [signer().trusted], NOW+offset)


@exhaustive
def test_original_binding_lifetime() -> None:
    for offset, valid in [(-1, False), (0, True), (1, True), (LIFETIME - 1, True), (LIFETIME, False), (LIFETIME + 1, False)]:
        _case_original_binding_lifetime(offset, valid)


def _case_receipt_demand_fraction(count: int, unknowns: int) -> None:
    events = [terminal() for _ in range(count)]
    for event in events[:unknowns]:
        event['prediction']['prediction'] = 'unknown'
    demand = receipt_demand(events)
    assert demand.eligible == count
    assert demand.unknown == unknowns
    assert ('prediction_unknown_gap' in demand.gaps) is (100*unknowns > 2*count)
    assert ('prediction_demand_gap' in demand.gaps) is (count > 0 and count == unknowns)


@exhaustive
def test_receipt_demand_fraction() -> None:
    for count, unknowns in [(0, 0), (1, 0), (1, 1), (49, 1), (50, 1)]:
        _case_receipt_demand_fraction(count, unknowns)


def test_demand_includes_failed_eligible_not_predictions() -> None:
    event = terminal(partition='C', booking=dict(outcome='unknown', winner=None, reference=None))
    bucket = receipt_bucket(event)
    assert bucket['eligible_demand'] == bucket['C'] == bucket['settle_attempts'] == 1
    demand = receipt_demand([event])
    assert demand.eligible == 1 and 'terminal_coverage_gap' in demand.gaps
    with pytest.raises(ValueError):
        receipt_demand([dict(event, prediction=None)])
    unknown = terminal(partition='C', eligibility='unknown', prediction=None)
    assert receipt_demand([unknown]).eligible is None
    assert receipt_bucket(unknown)['eligible_demand'] is None


def test_writer_zero_demand_rows() -> None:
    page, _ = original()
    excluded = terminal(partition='B', eligibility='excluded', prediction=None,
                        proof_kind='observed_binding', comparison='unevaluable')
    for events in ([], page['events'], [excluded]):
        result = receipt_demand(events)
        assert (result.eligible, result.yes, result.no, result.unknown, result.gaps) == (0, 0, 0, 0, ())


def test_retry_rules_call_retained_contract() -> None:
    sample = copy.deepcopy(fixture('sample')[0]['object'])
    retry = copy.deepcopy(sample)
    retry['deployment']['instance'] = 'fresh-worker'
    assert sample_retry(sample, retry) == 'duplicate'
    retry['payload_hash'] = '0'*64
    assert sample_retry(sample, retry) == 'conflict'
    retry = copy.deepcopy(sample)
    retry['booking']['attempted_kind'] = 'refund'
    retry.update(classification='unevaluable', python_micro=0, go_micro=0,
        legacy_frozen_micro=0, frozen_micro=0, rebuilt_micro=0, python_minus_go=0,
        booked_minus_frozen=2, rebuilt_minus_frozen=0, booked_minus_rebuilt=2)
    assert sample_retry(sample, retry) == 'winner_polarity'


def test_every_request_path_import_is_cohort_gated() -> None:
    root = Path(__file__).parents[1]/'src/trusted_router'
    found = []
    for path in (root/'routes').rglob('*.py'):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and 'shadow_cohort' in node.module:
                current = node
                guarded = False
                while current in parents:
                    current = parents[current]
                    if isinstance(current, ast.If) and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == 'in_v2_cohort' for n in ast.walk(current.test)):
                        guarded = True
                assert guarded, (path, node.lineno)
                found.extend(alias.name for alias in node.names)
    assert sorted(found) == ['authorize_http', 'capture_prices', 'capture_prices', 'terminal_http', 'terminal_http']


def test_zero_certificate_issuance_calls() -> None:
    root = Path(__file__).parents[1]/'src/trusted_router'
    paths = [root/'async_settle_shadow_cohort.py', *root.glob('services/async_settle_shadow_cohort*.py')]
    for path in paths:
        source = path.read_text()
        assert 'ExclusionSigner' not in source and 'async_settle_shadow_v2_certificate' not in source
        tree = ast.parse(source)
        assert not any(isinstance(n, ast.Call) and 'certificate' in ast.unparse(n.func).lower() for n in ast.walk(tree))


def test_complete_event_size_checked_after_coordinates() -> None:
    value = inputs()
    reserve = Reservation(day_at(value.observed_us), 'writer', 1, 1, value.observed_us)
    assert value.facts is not None
    identity = committed_identity(value.identity)
    candidates = [dict(endpoint_id='endpoint-'+str(i)+'x'*90, adapter='openai', features=[]) for i in range(32)]
    prepared = prepare_authorization(reserve, identity=identity, revision=value.revision,
        dimensions=value.dimensions, facts=value.facts, candidates=canonical(candidates),
        complete_candidates=True, complete_supported_context=True, reason=None)
    event = EVENT.parse(prepared.body)
    assert len(prepared.body) <= 2048
    assert prepared.decision.eligibility == event['eligibility'] == 'unknown'
    assert event['diagnostic']['required_bytes'] > 2048
    assert event['diagnostic']['candidate_count'] == 32
    assert event['candidates'] is None


def _case_exact_authorize_event_fit(target: int) -> None:
    value = inputs()
    reserve = Reservation(day_at(value.observed_us), 'writer', 1, 1, value.observed_us)
    identity = committed_identity(value.identity)
    def build() -> Any:
        return prepare_authorization(reserve, identity=identity, revision=value.revision,
            dimensions=value.dimensions, facts=value.facts, candidates=value.candidates,
            complete_candidates=True, complete_supported_context=True, reason=None)
    first = build()
    needed = target-len(first.body)
    extension = min(needed, 128-len(identity['journal_region']))
    identity['journal_region'] += 'x'*extension
    needed -= extension
    extension = min(needed, 64-len(reserve.boot))
    reserve = replace(reserve, boot=reserve.boot+'x'*extension)
    needed -= extension
    candidates = json.loads(value.candidates)
    assert 0 <= needed <= 128-len(candidates[0]['endpoint_id'])
    candidates[0]['endpoint_id'] += 'x'*needed
    value = replace(value, candidates=canonical(candidates))
    prepared = build()
    if target == 2048:
        assert len(prepared.body) == target and prepared.decision.eligibility == 'eligible'
    else:
        assert prepared.decision.eligibility == 'unknown'
        assert prepared.decision.required_bytes == target
        assert EVENT.parse(prepared.body)['diagnostic']['required_bytes'] == target


@exhaustive
def test_exact_authorize_event_fit() -> None:
    for target in [2048, 2049]:
        _case_exact_authorize_event_fit(target)


def test_every_eligible_terminal_receipt_keeps_prediction() -> None:
    page, locator = original()
    prediction = copy.deepcopy(fixture('sample')[1]['object'])
    for booking in (Booking(), Booking(2, 'settled', True)):
        verified = verify_terminal((fixture('terminal_compared_full')[0]['header'],),
            context(booking=booking), [signer().trusted], local_bytes=1000)
        reservation = Reservation(page['day'], 'writer', 2, 2, (NOW+1)*1000000)
        event = EVENT.parse(terminal_receipt(reservation, original=page['events'][0],
            revision='a'*40, phase='settle', dimensions=page['events'][0]['dimensions'],
            result=verified, booking=dict(outcome=booking.outcome, winner=booking.kind,
                reference='a'*64 if booking.confirmed else None), prediction=prediction))
        assert len(EVENT.serialize(event)) <= 2048
        assert event['prediction'] == prediction
        assert receipt_bucket(event)['eligible_demand'] == 1
        assert event['partition'] == ('A' if booking.confirmed else 'C')


def test_observed_binding_contract_gap_is_explicit() -> None:
    envelope = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
    envelope['observed']['service_tier'] = 'unsupported'
    envelope.update(terminal=None, payload_hash=None, go_error='unsupported_observed')
    result = verify_terminal((b64encode(canonical(envelope)),), context(), [signer().trusted],
        local_bytes=1000)
    assert result.comparison is not None
    assert result.comparison.binding_verified and result.comparison.raw_matches_body
    assert result.comparison.observed_eligible is False
    assert (result.partition, result.reason) == ('C', 'original_receipt_backing_gap')
    # Baseline EVENT cannot represent that valid v1 observed exclusion as B.
    with pytest.raises(ValueError):
        terminal(partition='B', eligibility='excluded', prediction=None,
            proof_kind='observed_binding', comparison='unevaluable', payload_sha256=None)


def test_p3_partition_reclassification_is_forbidden() -> None:
    from trusted_router.async_settle_shadow_v2_a1_evidence import COUNTER
    from trusted_router.async_settle_shadow_v2_evidence import COUNTS
    from trusted_router.storage_gcp_shadow_observation import monotonic_counter
    path = Path(__file__).parent/'fixtures/async_settlement/shadow_v2_a1/counter.json'
    before = json.loads(path.read_bytes())['variants'][0]['object']
    bucket = receipt_bucket(terminal(partition='C',
        dimensions=dict(adapter='openai', route_type='chat.completions', streamed=False)))
    before.update(closed=False, eligible_demand=1, buckets=[bucket])
    before.update({k: bucket[k] for k in COUNTS})
    row = next(v for v in before['counts'] if all(v[k] == n for k, n in bucket['dimensions'].items()))
    row.update(settle_attempts=1, observed_attempts=1, observed_eligible=1)
    after = copy.deepcopy(before)
    after.update(C=0, A=1)
    after['buckets'][0].update(C=0, A=1)
    COUNTER(before)
    COUNTER(after)
    with pytest.raises(ValueError, match='counter_regression'):
        monotonic_counter(before, after)


def test_verifier_does_not_consult_catalog(monkeypatch: Any) -> None:
    from trusted_router import catalog
    calls: list[str] = []
    class Catalog(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            calls.append(key)
            return default
        def __getitem__(self, key: str) -> Any:
            calls.append(key)
            return super().__getitem__(key)
    monkeypatch.setattr(catalog, 'MODEL_ENDPOINTS', Catalog())
    result = verify_terminal((fixture('terminal_compared_full')[0]['header'],), context(),
        [signer().trusted], local_bytes=1000)
    assert result.partition == 'A' and calls == []


def _case_refund_requires_actual_confirmed_booking(confirmed: bool) -> None:
    import hashlib
    value = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
    value['terminal'].update(terminal_kind='refund', charge_micro=0)
    value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
    ctx = context(attempted_kind='refund', booking=Booking(0, 'refunded', True) if confirmed else Booking())
    result = verify_terminal((b64encode(canonical(value)),), ctx, [signer().trusted],
        local_bytes=1000)
    assert result.partition == ('A' if confirmed else 'C')


@exhaustive
def test_refund_requires_actual_confirmed_booking() -> None:
    for confirmed in [False, True]:
        _case_refund_requires_actual_confirmed_booking(confirmed)


def test_real_p3_port_local_durability_never_discharges_authorization() -> None:
    from trusted_router.services.async_settle_shadow_health import Service
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('request attempted evidence I/O')
    cfg = config(source_revision='a'*40)
    service = Service(cfg, SimpleNamespace(clock=lambda: 10., wall=lambda: NOW,
        workspace_database=None, read_shadow_health=forbidden))
    value = inputs()
    day = day_at(value.observed_us)
    service.journal.grant(day, 100)
    try:
        response = authorize(cfg, ('shadow-v2',), service.port, value, additions(), signer())
        assert response['billing_shadow_policy'] == 'shadow-v2'
        assert service.counters[day]['authorize_attempts'] == 1
        assert service.counters[day]['requested_eligible'] == 1
        reservation = service.journal.pages[0].slots[0].reservation
        pages = service.journal.freeze(day)
        assert service.journal.days[day].hwm == 0
        service.journal.acknowledge(pages)
        assert service.journal.days[day].hwm == 0
        assert service.journal.verify_complete(reservation, pages)
        assert service.journal.days[day].hwm == 1
        obligation = authorization_obligation(pages[0].value(), f'{reservation.event_seq:020}')
        assert obligation['status'] == 'open' and obligation['terminal_reference'] is None
    finally:
        service.evidence_executor.shutdown()
        service.publisher_executor.shutdown()


def test_traced_core_branches(monkeypatch: Any) -> None:
    # Same case helpers, under tracing: branch witnesses are not fixture grids.
    for failure in ('reserve', 'finalize', 'bytes', 'signer', 'snapshot', 'context'):
        _case_authorize_failure_isolation(failure, monkeypatch)
    for reason in ('service_tier', 'missing_context', 'unknown_parameters'):
        _case_every_noneligible_reason_never_emits(reason)
    for missing in (None, FEATURES[0]):
        _case_missing_fact_never_defaults(missing)
    for variant in ('terminal_compared_full', 'terminal_compared_hash_only'):
        _case_p2_bytes_delegate_to_unchanged_comparator(variant)
    for damage in ('missing_header', 'bad_header',
                   'local_cap', 'unverified', 'booking_unknown', 'opposite_winner', 'excluded', 'hash', 'amount'):
        _case_compared_failures_remain_c(damage)
    for offset, valid in ((-1, False), (0, True)):
        _case_original_binding_lifetime(offset, valid)
    for count, unknowns in ((0, 0), (1, 0), (1, 1), (49, 1), (50, 1)):
        _case_receipt_demand_fraction(count, unknowns)
    for target in (2048, 2049):
        _case_exact_authorize_event_fit(target)
    for confirmed in (False, True):
        _case_refund_requires_actual_confirmed_booking(confirmed)
