"""Traced A2 gate identities, metric observations and output mutation witnesses."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.shadow_v2_gate_fixture_generator import (
    build,
    exhaustive,
    ports,
    untraced,  # noqa: F401
)
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, decode_bundle
from trusted_router.async_settle_shadow_v2_gate_failures import PRODUCERS, TAG, detail, listing
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Row
from trusted_router.async_settle_shadow_v2_gate_metrics import observations, ratio, stat
from trusted_router.async_settle_shadow_v2_gate_result import Subject, render
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.detached_jws import canonical

pytestmark = pytest.mark.timeout(method='signal')
FIXTURES = Path(__file__).parent / 'fixtures/async_settlement/shadow_v2_a2'


@pytest.fixture(scope='module')
def passing(tmp_path_factory: Any) -> Bundle:
    return build(tmp_path_factory.mktemp('p5-a2'))


def test_literal_writer_subject_counts_or_subchecks() -> None:
    ctx = Context(Bundle(days=('2026-10-12',)), None, 'pre-flip')
    expected = b'{"at_us":null,"boot":"w1","day":"2026-10-12","dimensions":null,"object":null,"phase":null,"revision":null,"role":"router","scope":"writer"}'
    for gap, size in (('health_timestamp_gap', 2), ('publisher_trace_gap', 3), ('publisher_accounting_gap', 3)):
        for i in reversed(range(size)):
            ctx.issue(gap, 'bad basis/slot ' + str(i), Subject('2026-10-12', 'w1', object_id=str(i)))
        failures = PRODUCERS[gap](ctx)
        assert len(failures) == 1 and failures[0].count == 1
        assert canonical(failures[0].subject) == expected
        tallies, listed = listing(failures, ctx.bundle.days)
        assert tallies[gap] == dict(count=1, listed=1, complete=True, day_counts=[1])
        assert canonical(listed[0]['subject']) == expected


@exhaustive
def test_each_contract_row_has_only_approved_producer_shape() -> None:
    vectors = json.loads((FIXTURES / 'gate_failures.json').read_bytes())['variants']
    for row in a2.FAILURE_RULES:
        seed = next(v['object'] for v in vectors if v['name'] == 'constructor-' + row.name)
        ctx = Context(Bundle(days=('2026-10-12',)), None, 'pre-flip')
        ctx.issue(row.name, 'fixture assertion', Subject(wire=seed['subject']))
        producing = [name for name, fn in PRODUCERS.items() if fn(ctx)]
        assert producing == [row.name]
        result = PRODUCERS[row.name](ctx)[0]
        assert result.rule == row.rule
        a2.GATE_FAILURE(dict(**TAG, gap=row.name, count=result.count, subject=result.subject))
    with pytest.raises(ValueError):
        ctx.issue('invented_gap', 'no producer')


def test_invalid_event_identity_uses_expected_position() -> None:
    from tests.shadow_v2_gate_fixture_generator import fixture
    page = fixture('receipt_pages', a1_release=False)
    page.update(day='2026-10-12', boot='w1')
    page['events'][0]['seq'] = '00000000000000000099'
    page['events'][0]['extra'] = 1
    key = Key(p1.KINDS[2], '2026-10-12/w1/00000000000000000001')
    ctx = decode_bundle(Bundle(rows=(Row(key, canonical(page)),), days=('2026-10-12',)), None, 'pre-flip')
    own = [f for f in PRODUCERS['evidence_schema_gap'](ctx) if f.subject['object'] and f.subject['object']['kind'] == 'a2_invalid_receipt']
    assert len(own) == 1
    assert canonical(own[0].subject) == b'{"at_us":null,"boot":"w1","day":"2026-10-12","dimensions":null,"object":{"event_seq":null,"key":"34:2026-10-12/w1/00000000000000000001/5:event/1:0","kind":"a2_invalid_receipt"},"phase":null,"revision":null,"role":"router","scope":"object"}'
    assert not any(f.subject == a2.O(key.kind, key.id) for f in PRODUCERS['evidence_schema_gap'](ctx))


def test_listing_exact_total_canonical_prefix_and_days() -> None:
    ctx = Context(Bundle(days=('2026-10-11', '2026-10-12')), None, 'pre-flip')
    for i in reversed(range(200)):
        ctx.issue('writer_closure_gap', 'missing', Subject('2026-10-12', f'w{i}'))
    failures = PRODUCERS['writer_closure_gap'](ctx)
    tallies, entries = listing(failures, ctx.bundle.days)
    assert tallies['writer_closure_gap'] == dict(count=200, listed=16, complete=False, day_counts=[0, 200])
    assert len(entries) == 16
    assert [canonical(v['subject']) for v in entries] == sorted(canonical(f.subject) for f in failures)[:16]
    ref, pages = detail(entries, 'failures')
    roots.validate_detail(ref, pages, stream='failures')
    with pytest.raises(ValueError):
        detail(list(reversed(entries)), 'failures')


def test_metric_literal_original_retry_completion_populations() -> None:
    sample = dict(timing=dict.fromkeys(a2.TIMINGS[:6]), admission=dict(workspace_age_us=4, health_age_us=5),
                  booked_minus_frozen=-3, booked_micro=100, eligibility=dict(observed=None))
    terminals = [dict(comparison=None, prediction=dict(prediction='unknown')),
                 dict(comparison='explained-by-catalog-change', prediction=dict(prediction='yes'))]
    # Completion is not in T; all three references point to one immutable S.
    slots = [dict(cycle_us=10, rpc_count=3, rpc_elapsed_us=150), dict(cycle_us=None, rpc_count=0, rpc_elapsed_us=0)]
    result = observations(samples=[sample], terminals=terminals, slots=slots, closes=[], handoffs=[], terminations=[])
    assert result['deltas'] == dict(count=1, null_count=0, min_micro=-3, max_micro=-3, bins={k: int(k == '-2-10') for k in a2.BINS})
    assert {k: result['null_counts'][k] for k in ('eligibility', 'comparison', 'prediction', 'booking', 'delta')} == dict(eligibility=1, comparison=1, prediction=0, booking=0, delta=0)
    assert result['timing']['publisher_slot_rpc_elapsed_us'] == dict(count=2, null_count=0, min=0, p50=0, p95=150, p99=150, max=150)
    assert result['timing']['evidence_write_us'] == dict(count=0, null_count=1, min=None, p50=None, p95=None, p99=None, max=None)
    assert stat(None) == dict.fromkeys(a2.STAT.fields)
    assert stat([])['count'] == 0
    assert stat([None])['null_count'] == 1


@pytest.mark.parametrize('numerator,denominator,required,closed,state', [
    (1, 2, True, False, 'evaluated'), (0, 0, True, True, 'not_exercised'),
    (0, 0, True, False, 'unevaluable'), (None, None, True, False, 'unevaluable'),
    (None, None, False, False, 'not_required'), (2, 1, True, False, 'unevaluable')])
def test_ratio_states(numerator: Any, denominator: Any, required: bool, closed: bool, state: str) -> None:
    assert ratio(numerator, denominator, required=required, closed_empty=closed)['state'] == state


def test_render_calls_final_validator(passing: Bundle, monkeypatch: pytest.MonkeyPatch) -> None:
    result = evaluate(passing, ports(), gate='shadow-serving')
    calls = []
    def reject(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        raise ValueError('final-validator-witness')
    monkeypatch.setattr(contract, 'validate_gate_output', reject)
    with pytest.raises(ValueError, match='final-validator-witness'):
        render(result)
    assert len(calls) == 1


def test_tally_cannot_disagree_with_evaluated_listing(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='pre-flip')
    report = copy.deepcopy(result.gate_report)
    assert report is not None
    report['failure_counts']['continuous_window_gap']['count'] += 1
    with pytest.raises(ValueError):
        render(replace(result, gate_report=report))


def test_null_aggregate_does_not_render_as_zero(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='fleet-budgets-pre')
    _, attachments = render(result)
    report = json.loads(attachments[0])
    assert report['admission']['E'] is None
    assert report['timing']['evidence_write_us']['count'] is None
    assert report['deltas']['count'] is None


@pytest.mark.parametrize('mode', ['unbound', 'substituted', 'changed-keys'])
def test_verifier_identity_and_keys_block(passing: Bundle, mode: str) -> None:
    bound = ports()
    if mode == 'unbound':
        bound = replace(bound, approvals=())
    elif mode == 'substituted':
        bound = replace(bound, verify_binding=lambda token, keys, now: None)
    else:
        sig = passing.signatures[0]
        passing = replace(passing, signatures=(replace(sig, keys=()),))
    result = evaluate(passing, bound, gate='shadow-serving')
    assert {'artifact_gap', 'evidence_schema_gap'} & set(result.gaps)
    assert result.verdict == 'BLOCKED'


def test_unexercised_certificate_still_bound(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='shadow-serving')
    assert result.gate_report is not None
    assert result.gate_report['verifiers']['certificate']['calls'] == 0
    assert result.gate_report['verifiers']['binding']['calls'] == 1


def test_missing_preimage_never_accepts_digest(passing: Bundle) -> None:
    result = evaluate(replace(passing, preimages=tuple(p for p in passing.preimages
        if p.name != 'proof.external_proofs_sha256')), ports(), gate='shadow-serving')
    assert result.verdict == 'BLOCKED' and 'artifact_gap' in result.gaps
    assert any(f.gap == 'artifact_gap' and f.subject == a2.O('a2_file', 'proof.external_proofs_sha256')
               and f.detail == 'A2 required original preimage absent' for f in result.failures)
    assert result.gate_report is not None and not result.gate_report['complete']


def test_conflict_entry_commits_both_candidates() -> None:
    value = roots._root_entry(a2.L('2026-10-12', 'w1'), [b'{}', b'{}\n', b'{}'])
    assert value == dict(key='10:2026-10-12/2:w1', bytes=5, state='conflict', sha256='0b896b4ad8e80334b52d5f5de239a1bee9f008464e9c5f019974159aea183a0c')
    assert roots.entry_root([value], stream='counter_root') == '129591526645105fa353f07120da3ca4905ce6ec7ac74914a96604ec9fb1e2f1'


def test_suppressed_call_is_not_a_transcript(passing: Bundle, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import async_settle_shadow_v2_gate_verifiers as v
    def suppressed(ctx: Context, name: str, *args: Any, **kwargs: Any) -> Any:
        return v.approval(ctx, name).function(*args, **kwargs)
    monkeypatch.setattr(v, 'invoke', suppressed)
    assert 'evidence_schema_gap' in evaluate(passing, ports(), gate='shadow-serving').gaps


def test_idle_router_region_does_not_need_seed(passing: Bundle) -> None:
    assert passing.inventory
    idle = replace(passing.inventory.boots[1], region='idle-region')
    bundle = replace(passing, inventory=replace(passing.inventory, boots=(passing.inventory.boots[0], idle, *passing.inventory.boots[2:])))
    result = evaluate(bundle, ports(), gate='pre-flip')
    assert not any(f.gap == 'regional_seed_gap' and f.subject == a2.V('idle-region') for f in result.failures)


def test_traffic_router_region_cannot_be_excused(passing: Bundle) -> None:
    assert passing.inventory
    active = replace(passing.inventory.boots[0], region='traffic-region')
    bundle = replace(passing, inventory=replace(passing.inventory, boots=(active, *passing.inventory.boots[1:])))
    assert 'regional_seed_gap' in evaluate(bundle, ports(), gate='pre-flip').gaps


def test_release_billing_fixture_is_not_a_file_ref() -> None:
    raw = (FIXTURES.parent / 'billing_v1.json').read_bytes()
    assert len(raw) == 699476
    with pytest.raises(ValueError):
        a2.FILE_REF(dict(path='fixtures/billing_v1.json', bytes=len(raw), sha256=roots.file_digest(raw)))


def test_release_files_are_checkout_inputs_outside_export_inventory(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    result = evaluate(passing, ports(), gate='shadow-serving')
    raw, _ = render(result)
    assert json.loads(raw)['verdict'] == 'PASS'
    assert result.gate_report is not None and result.gate_report['complete'] is True
    assert not set(RELEASE_FILES) & {source.name for source in passing.preimages}
    refs = [ref for leaf in result.input_leaves.values() for ref in json.loads(leaf)['keys']]
    assert not set(RELEASE_FILES) & {ref['path'] for ref in refs}
    assert all(ref['bytes'] <= 262144 for ref in refs)


@pytest.mark.parametrize('field', ['proof.evaluator_fixture_sha256', 'proof.binding_fixture_sha256', 'proof.wire_fixture_sha256'])
@pytest.mark.parametrize('damage', ['missing', 'changed', 'manifest', 'pin', 'unbound'])
def test_release_file_requires_checkout_and_both_pins(passing: Bundle, field: str, damage: str) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    reader = ports().read_release_file
    assert reader is not None
    path = RELEASE_FILES[field]
    def read(name: str) -> bytes:
        if name == path:
            if damage == 'missing':
                raise FileNotFoundError(name)
            if damage == 'changed':
                return reader(name) + b'\n'
        return reader(name)
    bound = replace(ports(), read_release_file=None if damage == 'unbound' else read)
    if damage == 'pin':
        bound = replace(bound, approved_files={**bound.approved_files, field: '0' * 64})
    if damage == 'manifest':
        proof = json.loads(passing.proof)
        proof[field.split('.')[1]] = '0' * 64
        passing = replace(passing, proof=canonical(proof))
    # An exported copy cannot rescue a missing/changed checkout input.
    passing = replace(passing, preimages=(*passing.preimages, Preimage(field, reader(path))))
    ctx = decode_bundle(passing, bound, 'shadow-serving')
    assert any(issue.gap == 'artifact_gap' and issue.subject.wire == a2.O('a2_file', path) for issue in ctx.issues)
    assert field not in ctx.release_files


def test_oversized_export_still_blocks(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    candidate = replace(passing, preimages=(*passing.preimages, Preimage('evidence/too-large', b'x' * 262145)))
    result = evaluate(candidate, ports(), gate='shadow-serving')
    assert result.verdict == 'BLOCKED'
    assert result.gate_report is not None and result.gate_report['complete'] is False
    assert any(f.gap == 'artifact_gap' and f.detail.startswith('exported_file_ref_bound:') for f in result.failures)


def test_loader_decodes_original_a2_payload_bytes() -> None:
    from tests.shadow_v2_gate_a2_fixture import fixture_bytes
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    raw = fixture_bytes('cohort_seed_policy', 'policy')
    source = canonical(dict(policy_version='shadow-v2', contract_revision='a1',
        preimages=[dict(name='proof.cohort_seed_policy_sha256', raw=raw.decode())]))
    loaded = load(source)
    assert loaded.preimages[0].raw == raw
    assert loaded.a2_payloads['proof.cohort_seed_policy_sha256'] == json.loads(raw)
    assert 'retained_payload_adapter_unbound' not in loaded.undefined_preimages
    assert any(issue['gap'] == 'artifact_gap' for issue in loaded.a2_streams['loading_issues'])


def test_preimage_conflict_candidates_are_not_selected() -> None:
    from trusted_router.async_settle_shadow_v2_gate_preimages import preimages
    day = '2026-10-12'
    key = Key(p1.KINDS[3], day + '/w1')
    ctx = Context(Bundle(rows=(Row(key, b'{}'), Row(key, b'{}\n'), Row(key, b'{}'))), None, 'pre-flip')
    _, values = preimages(ctx, dict(day=day, router_writers={'w1': {}}, enclave_writers={}))
    candidate = values['manifest.counter_root'][0]
    entry = roots._root_entry(candidate['key'], candidate['bodies'])
    assert entry['state'] == 'conflict' and entry['bytes'] == 5


def test_unexercised_port_cannot_be_left_unbound() -> None:
    from trusted_router.async_settle_shadow_v2_gate_verifiers import check_ports
    bound = ports()
    missing = replace(bound, approvals=tuple(v for v in bound.approvals if '.verify_certificate' not in v.entry_point))
    ctx = Context(Bundle(), missing, 'pre-flip')
    check_ports(ctx)
    assert any(i.gap == 'artifact_gap' and 'certificate' in i.detail for i in ctx.issues)


def test_substituted_port_is_not_approved_identity() -> None:
    from trusted_router.async_settle_shadow_v2_gate_verifiers import check_ports
    def impostor(token: str, keys: Any, now: int) -> Any:
        return None
    impostor.__module__ = ports().verify_binding.__module__
    impostor.__name__ = ports().verify_binding.__name__
    ctx = Context(Bundle(), replace(ports(), verify_binding=impostor), 'pre-flip')
    check_ports(ctx)
    assert any(i.gap == 'evidence_schema_gap' and 'binding' in i.detail for i in ctx.issues)


@exhaustive
def test_four_flat_failure_pages_from_all_producers() -> None:
    vectors = json.loads((FIXTURES / 'gate_failures.json').read_bytes())['variants']
    ctx = Context(Bundle(days=('2026-10-12',)), None, 'pre-flip')
    for row in a2.FAILURE_RULES:
        sub = next(v['object']['subject'] for v in vectors if v['name'] == 'constructor-' + row.name)
        for i in range(16):
            changed = copy.deepcopy(sub)
            if sub['scope'] in ('writer', 'bucket'):
                changed['boot'] = f'w{i:02}'
            elif sub['scope'] == 'reset':
                parts = list(a2.decode_key(sub['object']['key']))
                parts[0] = str(i)
                changed['at_us'] = i
                changed['object']['key'] = a2.L(*parts)
            elif sub['scope'] == 'object' and sub['object']['event_seq'] is not None:
                changed['object']['event_seq'] = f'{i + 1:020}'
            elif sub['scope'] == 'object' and not sub['object']['kind'].startswith('a2_'):
                changed['object']['key'] = f'2026-10-12/w{i:02}'
            elif sub['scope'] == 'day':
                changed = a2.D(f'2026-10-{i + 1:02}')
            elif sub['scope'] == 'object' and sub['object']['kind'] == 'a2_region':
                changed = a2.V(f'region{i}')
            elif sub['scope'] == 'object' and sub['object']['kind'] == 'a2_budget' and row.name != 'pre_enable_budget':
                changed = a2.K('post', f'region{i}')
            ctx.issue(row.name, 'fixture assertion', Subject(wire=changed))
    failures = tuple(f for fn in PRODUCERS.values() for f in fn(ctx))
    counts, entries = listing(failures, ctx.bundle.days)
    assert len(counts) == 58 and all(t['listed'] <= 16 for t in counts.values())
    assert 765 < len(entries) <= 928
    ref, pages = detail(entries, 'failures')
    assert len(pages) == 4 and all(len(p['entries']) == 255 for p in pages[:-1])
    roots.validate_detail(ref, pages, stream='failures')


@pytest.mark.parametrize('vector,gap,diagnostic,subchecks', [
    ('two-health-bases', 'health_timestamp_gap', Subject('2026-10-12', 'w1'), 2),
    ('three-slots-two-batches', 'publisher_trace_gap', Subject('2026-10-12', 'w1'), 3),
    ('same-slots-accounting', 'publisher_accounting_gap', Subject('2026-10-12', 'w1'), 3),
    ('invalid-first-event-claims-99', 'evidence_schema_gap',
        Subject(wire=a2.I('2026-10-12', 'w1', '00000000000000000001', 0)), 1),
    ('fleet-cap', 'simultaneous_capacity_gap', Subject(wire=a2.K('pre', None)), 1),
    ('region-cap', 'simultaneous_capacity_gap', Subject(wire=a2.K('pre', 'us-central1')), 1),
    ('approval', 'pre_enable_budget', Subject(), 1),
    ('expected-row-conflict', 'close_result_conflict', Subject('2026-10-12', 'w1'), 2),
])
def test_eight_worked_literal_vectors(vector: str, gap: str, diagnostic: Subject, subchecks: int) -> None:
    vectors = json.loads((FIXTURES / 'gate_failures.json').read_bytes())['variants']
    expected = next(v for v in vectors if v['name'] == vector)
    ctx = Context(Bundle(days=('2026-10-12',)), None, 'fleet-budgets-pre')
    for index in range(subchecks):
        ctx.issue(gap, f'subcheck {index}', diagnostic)
    failures = PRODUCERS[gap](ctx)
    assert len(failures) == 1
    assert canonical(failures[0].subject) == expected['subject_bytes'].encode()
    counts, listed = listing(failures, ctx.bundle.days)
    assert counts[gap] == expected['tally']
    assert listed == [expected['object']]


def test_render_rechecks_exact_unlisted_tally(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='pre-flip')
    assert result.gate_report is not None
    ctx = Context(Bundle(days=passing.days), None, 'pre-flip')
    for i in range(200):
        ctx.issue('writer_closure_gap', 'absent', Subject(passing.days[0], f'w{i}'))
    failures = (*result.failures, *PRODUCERS['writer_closure_gap'](ctx))
    tallies, entries = listing(failures, passing.days)
    report = copy.deepcopy(result.gate_report)
    report['failure_counts'] = tallies
    ref, pages = detail(entries, 'failures')
    report['failures'] = ref
    report['rules']['G8'] = 'fail'
    # This changed count satisfies A2 structural checks and leaves the prefix
    # unchanged. Only comparison with the evaluated complete assertions catches it.
    report['failure_counts']['writer_closure_gap']['count'] = 201
    with pytest.raises(ValueError):
        render(replace(result, failures=failures, gaps=tuple(sorted({f.gap for f in failures})),
            gate_report=report, detail_streams={**result.detail_streams, 'failures': pages}))


def test_missing_ledger_is_enumerated_from_inventory(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_preimages import preimages
    candidate = replace(passing, ledgers=())
    ctx = decode_bundle(candidate, ports(), 'shadow-serving')
    manifest = ctx.kind(p1.KINDS[5])[0]
    _, entries = preimages(ctx, manifest)
    assert passing.inventory is not None
    expected = sorted(a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us)) for b in passing.inventory.boots)
    assert entries['proof.ledger_root'] == [dict(key=key, bodies=[]) for key in expected]
    assert any(i.gap == 'artifact_gap' and i.detail == 'expected prerequisite ledger absent' for i in ctx.issues)


def loaded_fixture(passing: Bundle) -> Bundle:
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    value = dict(v=2, policy_version='shadow-v2', contract_revision='a1',
        rows=[dict(kind=r.key.kind, id=r.key.id, raw=r.raw.decode()) for r in passing.rows],
        attachments=[dict(kind=r.key.kind, id=r.key.id, raw=r.raw.decode()) for r in passing.attachments],
        preimages=[dict(name=p.name, raw=p.raw.decode()) for p in passing.preimages],
        proof=passing.proof.decode(), ledgers=[raw.decode() for raw in passing.ledgers],
        completed_at_us=passing.completed_at_us, days=list(passing.days))
    raw = canonical(value)
    assert len(raw) > 262144  # The container is not a single FILE_REF.
    return load(raw, ports())


def test_loader_derives_roster_cohort_and_shutdown_from_original_bytes(passing: Bundle) -> None:
    loaded = loaded_fixture(passing)
    assert loaded.inventory is not None and passing.inventory is not None
    def attributes(b: Any) -> tuple[Any, ...]:
        return b.boot, b.role, b.revision, b.region, b.start_us, b.end_us, b.termination_applies
    assert sorted(map(attributes, loaded.inventory.boots)) == sorted(map(attributes, passing.inventory.boots))
    assert loaded.cohort == passing.cohort
    shutdown = [entry for key, entries in loaded.a2_streams.items() if key.startswith('shutdown_writers/') for entry in entries]
    assert {entry['boot'] for entry in shutdown} == {'boot0', 'boot1'}
    assert 'retained_payload_adapter_unbound' not in loaded.undefined_preimages
    assert any(issue['path'] == 'loading/release-plan.json' and issue['gap'] == 'artifact_gap'
               for issue in loaded.a2_streams['loading_issues'])
    assert loaded.budget is None  # The older schema fixture has no approved loading port.


@pytest.mark.parametrize('missing', ['reviews/', 'budgets/deployment.json', 'details/shutdown/', 'measurements/'])
def test_loader_requires_original_dependency_bytes(passing: Bundle, missing: str) -> None:
    candidate = replace(passing, preimages=tuple(p for p in passing.preimages if not p.name.startswith(missing)))
    loaded = loaded_fixture(candidate)
    assert loaded.a2_streams['loading_issues']
    assert any(issue['gap'] in ('artifact_gap', 'evidence_schema_gap') for issue in loaded.a2_streams['loading_issues'])


def test_loader_rejects_changed_retained_roster_pins(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    changed = {}
    for source in passing.preimages:
        if source.name.endswith('/serving.json'):
            rows = json.loads(source.raw)
            rows[0]['pins']['TR_ASYNC_SETTLE_ENABLED'] = 'true'
            changed[source.name] = canonical(rows)
    sources = []
    for source in passing.preimages:
        if source.name in changed:
            source = Preimage(source.name, changed[source.name])
        elif source.name.endswith('/manifest.independent_inventory_sha256'):
            envelope = json.loads(source.raw)
            raw = changed[envelope['serving']['path']]
            envelope['serving'].update(sha256=roots.file_digest(raw), bytes=len(raw))
            source = Preimage(source.name, a2.INDEPENDENT_INVENTORY.serialize(envelope))
        sources.append(source)
    loaded = loaded_fixture(replace(passing, preimages=tuple(sources)))
    assert loaded.a2_streams['loading_issues']
    assert any(issue['gap'] in ('artifact_gap', 'evidence_schema_gap') for issue in loaded.a2_streams['loading_issues'])


def test_missing_first_receipt_has_expected_root_entry(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_preimages import preimages
    candidate = replace(passing, rows=tuple(r for r in passing.rows if r.key.kind != p1.KINDS[2]))
    ctx = decode_bundle(candidate, ports(), 'shadow-serving')
    manifest = ctx.kind(p1.KINDS[5])[0]
    _, entries = preimages(ctx, manifest)
    assert entries['manifest.receipts_root'] == [dict(key=a2.L(passing.days[0], 'boot0', f'{1:020}'), bodies=[])]


def test_independent_inventory_day_must_match_manifest(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    sources = []
    for source in passing.preimages:
        if source.name.endswith('/manifest.independent_inventory_sha256'):
            value = json.loads(source.raw)
            value['day'] = '2026-01-01'
            source = Preimage(source.name, a2.INDEPENDENT_INVENTORY.serialize(value))
        sources.append(source)
    ctx = decode_bundle(replace(passing, preimages=tuple(sources)), ports(), 'shadow-serving')
    assert any(i.gap == 'evidence_schema_gap' and i.detail == 'A2 manifest/proof preimage validation failed' for i in ctx.issues)


def test_traced_detail_pagination_chain() -> None:
    entries = [dict(path=f'inputs/{i:03}', bytes=0, sha256=roots.file_digest(b'')) for i in range(129)]
    ref, pages = detail(entries, 'files')
    assert [len(page['entries']) for page in pages] == [128, 1]
    assert pages[0]['previous_sha256'] is None
    assert pages[1]['previous_sha256'] == roots.file_digest(canonical(pages[0]))
    assert ref['tail_sha256'] == roots.file_digest(canonical(pages[1]))
    assert ref['count'] == 129
    roots.validate_detail(ref, pages, stream='files')
