"""Ordinary traced predicate witnesses and lossless gate regression tests."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_gate_fixture_generator import (
    START,
    build,
    change_row,
    exhaustive,
    ports,
    untraced,  # noqa: F401
)
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_a2_evidence import FAILURE_RULES
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, decode_bundle
from trusted_router.async_settle_shadow_v2_gate_failures import PRODUCERS
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Row
from trusted_router.async_settle_shadow_v2_gate_result import Subject, render
from trusted_router.async_settle_shadow_v2_gates import clock, evaluate, names
from trusted_router.async_settle_shadow_v2_schema import sha256
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def passing(tmp_path_factory: Any) -> Bundle:
    return build(tmp_path_factory.mktemp('p5-bundle'))


def attached(bundle: Bundle, kind: str, change: Any) -> Bundle:
    rows = list(bundle.attachments)
    pos = next(i for i, r in enumerate(rows) if r.key.kind == kind)
    value = json.loads(rows[pos].raw)
    change(value)
    rows[pos] = Row(rows[pos].key, canonical(value))
    return replace(bundle, attachments=tuple(rows))


def basis_damage(bundle: Bundle, damage: str) -> Bundle:
    def alter(value: dict[str, Any]) -> None:
        basis = value['bases'][0]
        if damage == 'health_source_gap':
            value['boot'] = basis['reader_boot'] = 'foreign'
        elif damage == 'health_elapsed_gap':
            basis.update(read_elapsed_ns=250000001, source_state='unusable')
        else:
            basis.update(read_at_ns=basis['read_at_ns'] + 5000000000, source_state='unusable')
    result = attached(bundle, 'health_basis', alter)
    basis = json.loads(next(r.raw for r in result.attachments if r.key.kind == 'health_basis'))['bases'][0]
    return change_row(result, p1.KINDS[2], lambda p: p['events'][1]['prediction'].update(basis_sha256=sha256(basis)))


def with_reset(bundle: Bundle, reason: str, at_us: int = START + 1) -> Bundle:
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_failures import detail
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    raw = canonical(dict(at_us=at_us, revision='a' * 40, reason=reason))
    path = 'reset-facts/' + file_digest(raw)
    files = [dict(path=path, sha256=file_digest(raw), bytes=len(raw))]
    support, _ = detail(files, 'files')
    entry = dict(at_us=at_us, revision='a' * 40, reason=reason, support=support,
                 resolved_at_us=None, fixed_revision=None, invalidated_at_us=None)
    assert bundle.reset_support
    return replace(bundle, reset_support=replace(bundle.reset_support, resets=(entry,),
            support_files={**bundle.reset_support.support_files, support['root']: (Preimage(path, raw),)}),
        preimages=(*bundle.preimages, Preimage(path, raw)))


def damage(bundle: Bundle, gap: str) -> Bundle:
    def counter(fn: Any) -> Bundle:
        return change_row(bundle, p1.KINDS[3], fn)

    def receipt(fn: Any) -> Bundle:
        return change_row(bundle, p1.KINDS[2], fn)
    if gap == 'policy_version_gap':
        return counter(lambda c: c.update(policy_version='shadow-v1'))
    if gap == 'evidence_schema_gap':
        return counter(lambda c: c.update(unrecognized=1))
    if gap == 'artifact_gap':
        return replace(bundle, undefined_preimages=('undefined',))
    if gap == 'writer_roster_gap':
        assert bundle.inventory
        return replace(bundle, inventory=replace(bundle.inventory, boots=bundle.inventory.boots[1:]))
    if gap == 'regional_roster_unknown':
        proof = json.loads(bundle.proof)
        proof['instance_regions_by_day'] = {}
        return replace(bundle, proof=canonical(proof))
    if gap == 'legacy_region_label':
        return counter(lambda c: c.update(region='wrong'))
    if gap == 'legacy_sample_region':
        return change_row(bundle, p1.KINDS[0], lambda s: s['deployment'].update(region='wrong'))
    if gap == 'flag_interval_gap':
        return change_row(bundle, p1.KINDS[5], lambda m: m.update(disabled_flag_intervals=[]))
    if gap == 'boot_prerequisite_gap':
        return replace(bundle, ledgers=())
    if gap == 'authorize_coverage_gap':
        return receipt(lambda p: (p.update(first_seq=p['last_seq']), p['events'].pop(0)))
    if gap == 'authorization_link_gap':
        return receipt(lambda p: p['events'][0].update(facts=None))
    if gap == 'authorization_obligation_gap':
        return replace(bundle, attachments=tuple(r for r in bundle.attachments if r.key.kind != 'authorization_obligation'))
    if gap == 'authorization_terminal_conflict':
        return receipt(lambda p: p['events'][1]['identity'].update(epoch=2))
    if gap == 'authorization_horizon_gap':
        return receipt(lambda p: p['events'][1].update(receipt_at_us=START + 172802000000))
    if gap == 'observed_partition_gap':
        return receipt(lambda p: p['events'][1].update(partition='C', comparison='unevaluable'))
    if gap == 'terminal_coverage_gap':
        return replace(bundle, rows=tuple(r for r in bundle.rows if r.key.kind != p1.KINDS[2]))
    if gap == 'phase_coverage_gap':
        return counter(lambda c: c['buckets'][1]['dimensions'].update(adapter='anthropic'))
    if gap == 'exclusion_proof_gap':
        return receipt(lambda p: p['events'][1].update(partition='B', eligibility='excluded', proof_kind='observed_binding', prediction=None, comparison='unevaluable'))
    if gap == 'eligible_coverage_gap':
        return replace(bundle, rows=tuple(r for r in bundle.rows if r.key.kind != p1.KINDS[0]))
    if gap == 'prediction_demand_gap':
        return receipt(lambda p: p['events'][1]['prediction'].update(prediction='unknown'))
    if gap == 'prediction_unknown_gap':
        return receipt(lambda p: p['events'][1]['prediction'].update(prediction='unknown'))
    if gap == 'observer_cadence_gap':
        return counter(lambda c: c['admission_observer'].update(health_reads=0))
    if gap == 'observer_budget_gap':
        return counter(lambda c: c['admission_observer'].update(max_consecutive_failures=3))
    if gap == 'shadow_health_gap':
        return replace(bundle, attachments=tuple(r for r in bundle.attachments if r.key.kind != 'health_basis'))
    if gap in ('health_source_gap', 'health_timestamp_gap', 'health_elapsed_gap'):
        return basis_damage(bundle, gap)
    if gap == 'health_slot_gap':
        return receipt(lambda p: p['events'][1]['prediction'].update(health_slot=0))
    if gap == 'publisher_accounting_gap':
        return counter(lambda c: c['publisher'].update(claims=0))
    if gap == 'publisher_trace_gap':
        return replace(bundle, attachments=tuple(r for r in bundle.attachments if r.key.kind != 'publisher_trace'))
    assert bundle.budget is not None
    changes = {
        'simultaneous_capacity_gap': dict(fleet_cap=0),
        'pre_enable_budget': dict(post_workspace_count=2),
        'publisher_budget_gap': dict(publisher_cap=0),
        'fleet_headroom_gap': dict(cpu_peak=44.),
        'freshness_budget_gap': dict(component_seconds={}),
        'evidence_export_budget_gap': dict(export_operations=0),
        'close_handoff_budget_gap': dict(close_transactions=0),
    }
    if gap in changes:
        return replace(bundle, budget=replace(bundle.budget, **changes[gap]))
    if gap == 'checkpoint_reconciliation_gap':
        return counter(lambda c: c.update(durable_event_hwm=0))
    if gap == 'evidence_loss_gap':
        return counter(lambda c: c.update(loss_count=1))
    if gap == 'writer_closure_gap':
        return counter(lambda c: c.update(closed=False))
    if gap == 'retry_original_gap':
        return receipt(lambda p: p['events'][1].update(retry_original=dict(day=bundle.days[0], boot='absent', event_seq=f'{1:020}')))
    if gap == 'close_result_identity_gap':
        return replace(bundle, rows=tuple(r for r in bundle.rows if r.key.kind != a1.KINDS[-1]))
    if gap == 'close_result_conflict':
        row = next(r for r in bundle.rows if r.key.kind == a1.KINDS[-1])
        value = json.loads(row.raw)
        value['elapsed_us'] += 1
        return replace(bundle, rows=(*bundle.rows, Row(row.key, canonical(value))))
    if gap == 'handoff_coverage_gap':
        return replace(bundle, attachments=tuple(r for r in bundle.attachments if r.key.kind != 'handoff_accounting'))
    if gap == 'missing_support':
        return replace(bundle, reset_support=None)
    if gap in names('G9'):
        assert bundle.reset_support
        return with_reset(bundle, gap)
    if gap == 'positive_seed_gap':
        gate = json.loads(bundle.transport)
        gate['completed_at_us'] = START + 2000000
        return replace(bundle, transport=canonical(gate))
    if gap == 'regional_seed_gap':
        assert bundle.inventory
        return replace(bundle, inventory=replace(bundle.inventory,
            boots=(replace(bundle.inventory.boots[0], region='europe-west1'), *bundle.inventory.boots[1:])))
    if gap == 'continuous_window_gap':
        return bundle
    if gap == 'external_proofs_missing':
        assert bundle.external
        return replace(bundle, external=replace(bundle.external, proofs=()))
    if gap == 'pilot_prerequisite_gap':
        assert bundle.external
        return replace(bundle, external=replace(bundle.external, signer_checked=False))
    raise AssertionError(gap)


GAPS = tuple(rule.name for rule in FAILURE_RULES)
pytestmark = pytest.mark.timeout(method='signal')


@pytest.fixture(scope='module')
def positive_results(passing: Bundle) -> dict[str, Any]:
    return {stage: evaluate(passing, ports(), gate=stage) for stage in ('shadow-serving', 'pre-flip')}


# The same 58 predicates have traced original-byte witnesses; retain this
# independent typed-view differential grid under the exhaustive convention.
@exhaustive
@pytest.mark.parametrize('gap', GAPS)
def test_gap_negative(passing: Bundle, gap: str) -> None:
    result = evaluate(damage(passing, gap), ports())
    assert result.verdict == 'BLOCKED'
    assert gap in result.gaps, (gap, result.gaps)
    assert any(f.gap == gap for f in result.failures)
    output, attachments = render(result)
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report


def test_gap_positive(positive_results: dict[str, Any]) -> None:
    # All 58 memberships inspect the same two evaluated results.
    checked = set()
    for gap in GAPS:
        result = positive_results['shadow-serving' if gap == 'continuous_window_gap' else 'pre-flip']
        assert gap not in result.gaps, (gap, result.gaps)
        checked.add(gap)
    assert checked == set(PRODUCERS)


def test_one_producer_and_both_directions() -> None:
    expected = [r.name for r in FAILURE_RULES]
    assert len(expected) == len(set(expected)) == 58
    assert set(PRODUCERS) == set(expected)
    assert len(set(PRODUCERS.values())) == 58
    assert all(fn.__name__ == 'decide_' + name for name, fn in PRODUCERS.items())
    # The positive loop consumes GAPS directly; it verifies its executed set.
    assert set(GAPS) == set(expected)
    assert set(next(mark.args[1] for mark in test_gap_negative.pytestmark if mark.name == 'parametrize')) == set(expected)


def test_lossless_more_than_128_subjects() -> None:
    context = Context(Bundle(), None, 'pre-flip')
    for i in range(200):
        context.issue('writer_closure_gap', 'absent', Subject('2026-10-06', f'boot{i}'), tuple(f'receipt{j:04}' for j in range(300)))
    failures = PRODUCERS['writer_closure_gap'](context)
    assert len(failures) == 16
    assert failures.summaries['writer_closure_gap'].count == 200
    assert failures.summaries['writer_closure_gap'].day_counts == {'2026-10-06': 200}
    assert all(f.count == 1 and len(f.object_ids) == 300 for f in failures)
    assert len({f.subject['boot'] for f in failures}) == 16


def test_evaluates_later_rules_after_failure(passing: Bundle) -> None:
    bad = damage(damage(passing, 'policy_version_gap'), 'external_proofs_missing')
    result = evaluate(bad, ports())
    assert {'policy_version_gap', 'evidence_schema_gap', 'external_proofs_missing'} <= set(result.gaps)


def test_missing_verifier_is_artifact_gap(passing: Bundle) -> None:
    assert 'artifact_gap' in evaluate(passing).gaps


def test_internal_pass_and_render(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='shadow-serving')
    assert result.verdict == 'PASS' and result.gaps == ()
    assert result.report.writers[1].prediction_state == 'not_exercised'
    assert result.report.writers[1].unknown_ratio is None
    output, attachments = render(result)
    assert json.loads(output)['verdict'] == 'PASS'
    assert json.loads(attachments[0]) == result.gate_report


def test_bootstrap_uses_original_time_and_excludes_serving(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='shadow-serving')
    assert result.report.clock_start_us == START + 2000000
    assert result.report.elapsed_seconds == 18
    assert result.completed_at_us != result.report.clock_start_us
    assert evaluate(replace(passing, serving=None), ports(), gate='shadow-serving').report.clock_start_us == START + 2000000


@pytest.mark.parametrize('name', ['transport', 'fleet'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_seed_strict_prerequisite_boundary(passing: Bundle, name: str, offset: int) -> None:
    prerequisite = json.loads(getattr(passing, name))
    prerequisite['completed_at_us'] = START + 2000000 + offset
    result = evaluate(replace(passing, **{name: canonical(prerequisite)}), ports(), gate='shadow-serving')
    assert ('positive_seed_gap' in result.gaps) == (offset >= 0)


def test_fleet_bootstrap_does_not_require_seed(passing: Bundle) -> None:
    result = evaluate(damage(passing, 'positive_seed_gap'), ports(), gate='fleet-budgets')
    assert result.verdict == 'PASS'


@pytest.mark.parametrize('seconds', [604799, 604800, 604801])
def test_clock_exact_seven_day_boundary(passing: Bundle, seconds: int) -> None:
    ctx = decode_bundle(passing, ports(), 'pre-flip')
    ctx.verified.add(passing.signatures[0].object_id)
    end = START + 2000000 + seconds * 1000000
    ctx.bundle = replace(ctx.bundle, completed_at_us=end)
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key
    from trusted_router.async_settle_shadow_v2_schema import day_at
    template = next(v for v in ctx.rows.values() if 'disabled_flag_intervals' in v)
    for day_index in range(START // 86400000000, (end - 1) // 86400000000 + 1):
        day = day_at(day_index * 86400000000)
        value = json.loads(canonical(template))
        value['day'] = day
        value['disabled_flag_intervals'][0]['interval'] = dict(start_us=max(START, day_index * 86400000000), end_us=min(end, (day_index + 1) * 86400000000))
        ctx.rows[Key(p1.KINDS[5], day + '/manifest-v2')] = value
    ctx.bundle = replace(ctx.bundle, days=tuple(v['day'] for v in ctx.rows.values() if 'disabled_flag_intervals' in v))
    _, elapsed, _ = clock(ctx, ())
    assert elapsed == seconds
    assert any(i.gap == 'continuous_window_gap' for i in ctx.issues) == (seconds < 604800)


def test_gap_day_cannot_be_backfilled(passing: Bundle) -> None:
    bad = change_row(passing, p1.KINDS[5], lambda m: m.update(complete=False,
        gaps=[dict(interval=dict(start_us=START + 10000000, end_us=START + 11000000), reason='writer_closure_gap')]))
    result = evaluate(bad, ports(), gate='shadow-serving')
    assert result.report.clock_start_us is None
    assert result.report.gap_timestamps_us == (START + 10000000,)


@pytest.mark.parametrize('value', [None, False, 0, [], {}, 'shadow-v1', {'rows': None}])
def test_total_over_json_domain(value: Any) -> None:
    result = evaluate(value)
    assert result.verdict == 'BLOCKED' and 'artifact_gap' in result.gaps
    assert result.report.actual_regions is None and result.report.peaks is None


def test_no_unknown_zero_after_schema_damage(passing: Bundle) -> None:
    result = evaluate(damage(passing, 'evidence_schema_gap'), ports())
    assert result.report.comparator_covered is None
    assert result.report.admission_demand is None
    missing = replace(passing, rows=tuple(row for row in passing.rows if row.key.kind != p1.KINDS[3]))
    result = evaluate(missing, ports())
    assert result.report.comparator_covered is None
    assert result.report.admission_covered is None
    assert result.report.admission_demand is None


def test_different_boot_close_result_is_rejected(passing: Bundle) -> None:
    result = evaluate(change_row(passing, a1.KINDS[-1], lambda v: v.update(boot='foreign')), ports())
    assert 'close_result_identity_gap' in result.gaps


def test_writer_closure_cannot_discharge_authorization(passing: Bundle) -> None:
    bad = change_row(passing, p1.KINDS[2], lambda p: (p['events'].pop(), p.update(last_seq=p['first_seq'])))
    assert 'authorization_horizon_gap' in evaluate(bad, ports()).gaps


def test_changed_contract_revision(passing: Bundle) -> None:
    bad = change_row(passing, a1.KINDS[-1], lambda v: v.update(contract_revision='a2'))
    assert 'policy_version_gap' in evaluate(bad, ports()).gaps


def terminal_c(bundle: Bundle) -> Bundle:
    result = change_row(bundle, p1.KINDS[2], lambda p: p['events'][1].update(partition='C', comparison='unevaluable'))
    def update(c: dict[str, Any]) -> None:
        c.update(A=0, C=1, proof_compared=0)
        c['buckets'][1].update(A=0, C=1, proof_compared=0)
    return change_row(result, p1.KINDS[3], update)


def all_unknown(bundle: Bundle) -> Bundle:
    result = change_row(bundle, p1.KINDS[2], lambda p: p['events'][1]['prediction'].update(prediction='unknown'))
    return change_row(result, p1.KINDS[3], lambda c: c['admission_observer'].update(prediction_yes=0, prediction_unknown=1))


def late_publisher(bundle: Bundle) -> Bundle:
    def trace(value: dict[str, Any]) -> None:
        value['entries'][1].update(outcome='failed', late_cycle=True, cycle_us=800000)
    result = attached(bundle, 'publisher_trace', trace)
    def count(c: dict[str, Any]) -> None:
        c['publisher']['publications'] -= 1
        c['publisher'].update(failures=1, late_cycles=1)
    return change_row(result, p1.KINDS[3], count)


def test_nonzero_c_with_reconciled_counts_still_blocks(passing: Bundle) -> None:
    assert 'observed_partition_gap' in evaluate(terminal_c(passing), ports()).gaps


def test_nonzero_e_requires_known_prediction(passing: Bundle) -> None:
    assert 'prediction_demand_gap' in evaluate(all_unknown(passing), ports()).gaps


def test_two_percent_unknown_is_independent(passing: Bundle) -> None:
    assert 'prediction_unknown_gap' in evaluate(all_unknown(passing), ports()).gaps


def test_late_landed_health_is_not_timely_publication(passing: Bundle) -> None:
    result = evaluate(late_publisher(passing), ports())
    assert 'publisher_budget_gap' in result.gaps
    assert 'shadow_health_gap' not in result.gaps


def test_reader_success_cannot_erase_publisher_failure(passing: Bundle) -> None:
    result = evaluate(late_publisher(passing), ports())
    assert 'observer_budget_gap' in result.gaps
    assert 'shadow_health_gap' not in result.gaps


def test_foreign_close_result_with_consistent_key_still_blocks(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key
    rows = []
    for row in passing.rows:
        if row.key.kind == a1.KINDS[-1] and row.key.id.endswith('/boot0'):
            value = json.loads(row.raw)
            value['boot'] = 'foreign'
            row = Row(Key(row.key.kind, value['day'] + '/foreign'), canonical(value))
        rows.append(row)
    assert 'close_result_identity_gap' in evaluate(replace(passing, rows=tuple(rows)), ports()).gaps


def test_digest_without_preimage_cannot_pass(passing: Bundle) -> None:
    result = evaluate(replace(passing, preimages=()), ports(), gate='shadow-serving')
    assert result.verdict == 'BLOCKED' and 'artifact_gap' in result.gaps


def test_gap_inventory_matches_design_table() -> None:
    from pathlib import Path
    design = (Path(__file__).parents[1] / 'docs/design/async-settle-shadow-v2.md').read_text()
    table = design.split('| GAP_NAME | Single producer |', 1)[1].split('\n\n', 1)[0]
    inventory = {line.split('|')[1].strip() for line in table.splitlines()[2:]}
    assert inventory == set(GAPS)


def test_post_flip_delegates_retained_outcomes(passing: Bundle, tmp_path: Any) -> None:
    from scripts.async_settle import pilot_enablement as pilot
    from tests.shadow_v2_gate_fixture_generator import pre_enable_bundle
    data = pre_enable_bundle(tmp_path)
    def artifact(path: Any, name: str, value: Any) -> dict[str, Any]:
        import hashlib
        raw = canonical(value)
        target = path / name
        target.write_bytes(raw)
        return dict(path=str(target), sha256=hashlib.sha256(raw).hexdigest())
    data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_ENABLED='true', TR_ASYNC_SETTLE_PROTECTION='true',
        TR_ASYNC_SETTLE_PILOT_WORKSPACES=pilot.PILOT, TR_ASYNC_SETTLE_PILOT_CAP_MICRO='5000000')
    data['serving'][1]['pins'].update(TR_ASYNC_SETTLE_NEGOTIATE='on', TR_ASYNC_SETTLE_SHADOW='off')
    data.update(rollback=artifact(tmp_path, 'rollback.json', dict(status='PASS', states=[
        'admission_on', 'pending', 'admission_off', 'drained', 'protection_off'])),
        outbox=[dict(authorization_id='a', intent_kind='settle', workspace_id=pilot.PILOT,
            status='done', created_at=100, terminal_at=102, async_version=1)],
        handoff={'a.settle': dict(region='us-central1', handoff_us=3000)}, observed_peak_concurrency=4)
    assert passing.external
    candidate = replace(passing, external=replace(passing.external, post_input=data))
    assert evaluate(candidate, ports(), gate='post-flip').verdict == 'PASS'
    data['outbox'][0]['terminal_at'] = None
    assert 'pilot_prerequisite_gap' in evaluate(candidate, ports(), gate='post-flip').gaps


def test_pre_enable_does_not_invent_observer_demand(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='fleet-budgets-pre')
    assert result.verdict == 'PASS'
    assert result.report.clock_start_us is None


def test_plan_is_manifest_inventory_reference_derived(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_export import derive
    context = decode_bundle(passing, ports(), 'pre-flip')
    export = derive(context)
    assert export.source_gets == len(export.keys) + len(export.ranges)
    assert export.operations == export.source_gets
    assert export.actual_bytes > 0
    assert any(k.kind == 'proof_manifest' for k in export.keys)
    assert any(k.kind == 'prerequisite_ledger' for k in export.keys)
    assert any(k.kind == a1.KINDS[-1] for k in export.keys)
    assert export.ranges


def test_open_obligation_export_requires_forward_inventory(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_export import derive
    candidate = change_row(passing, p1.KINDS[2], lambda p: (p['events'].pop(), p.update(last_seq=p['first_seq'])))
    with pytest.raises(ValueError, match='authorization_horizon_gap'):
        derive(decode_bundle(candidate, ports(), 'pre-flip'))


def test_real_certificate_and_original_receipt_ports() -> None:
    from tests.shadow_v2_gate_fixture_generator import ROOT, fixture
    from trusted_router.async_settle_shadow_v2_gate_bundle import verify_samples
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key, Signature
    from trusted_router.detached_jws import TrustedKey
    vector = json.loads((ROOT / 'shadow_v2/exclusion_certificate.json').read_bytes())['variants'][0]
    page = vector['page']
    event = fixture('receipt_pages', a1_release=False, name='terminal-event')
    event['seq'] = f'{2:020}'
    terminal = {**page, 'first_seq': event['seq'], 'last_seq': event['seq'],
                'previous_page_sha256': sha256(page), 'events': [event]}
    attempt = fixture('terminal_excluded_settle', a1_release=False)['attempt']
    identity = event['authorization_day'] + '/' + event['authorization_id']
    signature = Signature(identity, vector['certificate'], (TrustedKey(**vector['key']),), vector['now'],
        vector['expected_identity'], attempt, attempt, vector['claims'])
    rows = tuple(Row(Key(p1.KINDS[2], '/'.join(p[k] for k in ('day', 'boot', 'first_seq'))), p1.PAGE.serialize(p)) for p in (page, terminal))
    value = Bundle(rows=rows, signatures=(signature,))
    ctx = decode_bundle(value, ports(), 'pre-flip')
    verify_samples(ctx)
    assert identity in ctx.verified
    assert not any(i.gap == 'exclusion_proof_gap' for i in ctx.issues)
    bad = replace(value, signatures=(replace(signature, token=signature.token[:-1] + '!'),))
    ctx = decode_bundle(bad, ports(), 'pre-flip')
    verify_samples(ctx)
    assert any(i.gap == 'exclusion_proof_gap' for i in ctx.issues)


def test_recovered_checkpoint_has_no_evidence_loss(tmp_path: Any) -> None:
    value = build(tmp_path, recovered_checkpoint=True)
    result = evaluate(value, ports(), gate='shadow-serving')
    assert result.verdict == 'PASS'
    assert result.report.writers[0].checkpoint_failures == 1
    assert result.report.writers[0].checkpoint_recoveries == 1
    assert result.report.writers[0].evidence_loss == 0


@pytest.mark.parametrize('name', ['same-day', 'cross-day-equal-sequence'])
def test_completion_reuses_contextual_contract(name: str) -> None:
    from tests.shadow_v2_gate_fixture_generator import fixture
    from trusted_router.async_settle_shadow_v2_gate_accounting import durability
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key
    value = fixture('completion_context', name=name)
    rows = tuple(Row(Key(p1.KINDS[2], '/'.join(page[k] for k in ('day', 'boot', 'first_seq'))), p1.PAGE.serialize(page))
                 for page in (value['original_page'], value['completion_page']))
    ctx = decode_bundle(Bundle(rows=rows), ports(), 'pre-flip')
    durability(ctx)
    assert not any(i.gap == 'retry_original_gap' for i in ctx.issues)
    ctx = decode_bundle(Bundle(rows=rows[1:]), ports(), 'pre-flip')
    durability(ctx)
    assert any(i.gap == 'retry_original_gap' for i in ctx.issues)


def test_all_required_peak_sources_and_missing_component(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import Budget
    assert isinstance(passing.budget, Budget)
    result = evaluate(passing, ports(), gate='fleet-budgets-pre')
    raw, _ = render(result)
    assert json.loads(raw)['verdict'] == 'PASS'
    assert result.gate_report['fleet_peaks']['cpu_percent'] == dict(numerator=20, denominator=1)
    assert result.gate_report['fleet_peaks']['configured_reads_per_second'] == dict(numerator=13, denominator=4)
    assert result.gate_report['fleet_peaks']['reads_per_second'] == dict(numerator=13, denominator=4)
    assert result.gate_report['fleet_peaks']['export_operations_per_second'] == dict(numerator=2, denominator=5)
    assert result.gate_report['publisher']['peaks']['export_operations_per_second'] is None
    assert_missing_peak(passing, 'publication_operations_per_second')


def assert_missing_peak(passing: Bundle, field: str) -> None:
    assert passing.budget is not None
    source = passing.budget.load_segments[0]
    bad = replace(passing, budget=replace(passing.budget,
        projected_load_segments=({**source, 'peaks': {**source['peaks'], field: None}}, *passing.budget.load_segments[1:])))
    blocked = evaluate(bad, ports(), gate='fleet-budgets-pre')
    assert blocked.gate_report['fleet_peaks'][field] is None
    assert blocked.gate_report['rules']['G7'] == 'unevaluable'
    assert json.loads(render(blocked)[0])['verdict'] == 'BLOCKED'


@exhaustive
def test_exhaustive_missing_peak_components(passing: Bundle) -> None:
    for field in ('startup_operations_1s', 'close_operations_1s', 'export_operations_1s',
                  'claim_operations_per_second', 'scan_operations_per_second',
                  'publication_operations_per_second', 'export_operations_per_second', 'export_bytes_per_second'):
        assert_missing_peak(passing, field)

# Normative singleton populations cannot contain three distinct subjects. This
# named seam records that contract limit rather than making up noncanonical IDs.
SINGLETON_POPULATIONS = frozenset(('pre_enable_budget', 'pilot_prerequisite_gap',
    'evidence_export_budget_gap', 'positive_seed_gap', 'continuous_window_gap'))


@exhaustive
@pytest.mark.parametrize('rule', FAILURE_RULES, ids=lambda row: row.name)
def test_all_failure_populations(rule, passing: Bundle, positive_results: dict[str, Any]) -> None:
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_failures import detail, listing, normalize
    from trusted_router.async_settle_shadow_v2_gate_resets import normalized
    from trusted_router.async_settle_shadow_v2_gate_result import Dimensions
    def one(index):
        day, boot = f'2026-10-{5 + index:02}', 'boot' + str(index)
        ctx = Context(Bundle(days=(day,)), None, 'pre-flip')
        code = rule.constructor[0]
        if code == 'W':
            sub, wire = Subject(day, boot), a2.W(day, 'router', boot)
        elif code == 'D':
            sub, wire = Subject(day), a2.D(day)
        elif code == 'B':
            dims = dict(adapter='openai', route_type='chat.completions', streamed=False)
            sub, wire = Subject(day, boot, 'settle', Dimensions(**dims)), a2.B(day, boot, 'settle', dims)
        elif code == 'A':
            sub, wire = Subject(), a2.A('pre')
        elif code == 'N':
            sub, wire = Subject(), a2.N()
        elif code == 'V':
            sub, wire = Subject(object_id='region' + str(index)), a2.V('region' + str(index))
        elif code == 'P':
            name = 'pilot' if rule.name == 'pilot_prerequisite_gap' else 'proof' + str(index)
            sub, wire = Subject(object_id=name), a2.P('pre-flip', name)
        elif code == 'K':
            # A diagnosed stage/scope is one assertion, not both stages.
            wire = a2.K('post', 'region' + str(index))
            sub = Subject(wire=wire)
        elif code == 'Q':
            entry = normalized(dict(at_us=START + index, revision='a' * 40,
                reason='hash' if rule.name == 'missing_support' else rule.name))
            ctx.reset_history = [entry]
            sub, wire = Subject(object_id=str(START + index)), a2.Q(entry)
        elif code == 'E':
            page = json.loads(next(r.raw for r in passing.rows if r.key.kind == p1.KINDS[2]))
            event = page['events'][0 if rule.rule == 'G3' else 1]
            page = {**page, 'boot': boot}
            event = {**event, 'seq': f'{index + 1:020}'}
            if event.get('receipt_locator') is not None:
                event['receipt_locator'] = dict(day=page['day'], boot=boot, first_seq=page['first_seq'], event_seq=event['seq'])
            page['events'] = [event]
            if rule.name == 'retry_original_gap':
                event['retry_original'] = dict(day=page['day'], boot='original', event_seq=f'{1:020}')
            ctx.rows[next(r.key for r in passing.rows if r.key.kind == p1.KINDS[2])] = page
            sub = Subject(page['day'], boot, object_id=event['seq'])
            wire = a2.E(page['day'], boot, page['first_seq'], event['seq'], event=event)
        elif rule.name.startswith('close_result_'):
            sub, wire = Subject(day, boot), a2.O(a1.KINDS[-1], day + '/' + boot)
        else:
            row = next(r for r in passing.rows if r.key.kind == p1.KINDS[0])
            from trusted_router.async_settle_shadow_v2_gate_inputs import Key
            row = Row(Key(row.key.kind, day + '/sample' + str(index)), row.raw)
            ctx.bundle = replace(ctx.bundle, rows=(row,))
            sub, wire = Subject(object_id=row.key.id), a2.O(row.key.kind, row.key.id)
        ctx.issue(rule.name, 'failed assertion', sub)
        normalize(ctx)
        produced = PRODUCERS[rule.name](ctx)
        assert len(produced) == 1 and canonical(produced[0].subject) == canonical(wire)
        return ctx, wire
    ctx, wire = one(0)
    tally, entries = listing(PRODUCERS[rule.name](ctx), ctx.bundle.days)
    assert tally[rule.name]['count'] == 1 and canonical(entries[0]['subject']) == canonical(wire)
    contexts = [one(i)[0] for i in (2, 0, 1)]
    merged = Context(Bundle(days=('2026-10-05', '2026-10-06', '2026-10-07')), None, 'pre-flip')
    expected = []
    for context in contexts:
        for failure in PRODUCERS[rule.name](context):
            merged.issue(rule.name, 'failed assertion', Subject(wire=failure.subject))
            expected.append(failure.subject)
    failures = PRODUCERS[rule.name](merged)
    tally, entries = listing(failures, merged.bundle.days)
    count = 1 if rule.name in SINGLETON_POPULATIONS else 3
    assert tally[rule.name]['count'] == count
    assert [canonical(e['subject']) for e in entries] == sorted(set(map(canonical, expected)))
    unknown = Context(Bundle(days=merged.bundle.days), None, 'pre-flip')
    unknown.issue(rule.name, 'population unavailable', Subject(wire=a2.N()), unavailable=True)
    normalize(unknown)
    assert PRODUCERS[rule.name](unknown)[0].subject == a2.N()
    assert PRODUCERS['artifact_gap'](unknown)
    # A singleton prefix is complete; the whole report must be incomplete.
    assert any(i.gap == 'artifact_gap' for i in unknown.issues)
    baseline = positive_results['pre-flip']
    failures = {canonical([f.gap, f.subject]): f for f in baseline.failures}
    for row in FAILURE_RULES:
        for failure in PRODUCERS[row.name](unknown):
            failures[canonical([failure.gap, failure.subject])] = failure
    selected = tuple(failures.values())
    tallies, entries = listing(selected, tuple(baseline.gate_report['days']))
    reference, pages = detail(entries, 'failures')
    report = {**baseline.gate_report, 'failure_counts': tallies, 'failures': reference,
        'complete': False,
        'rules': {r: 'fail' if any(f.rule == r for f in selected) else state
                  for r, state in baseline.gate_report['rules'].items()}}
    output = replace(baseline, failures=selected, gaps=tuple(sorted({f.gap for f in selected})),
                     gate_report=report, detail_streams={**baseline.detail_streams, 'failures': pages})
    _, attachments = render(output)
    assert json.loads(attachments[0])['complete'] is False


@pytest.mark.parametrize('reason', [r.name for r in FAILURE_RULES if r.rule == 'G9' and r.name != 'missing_support'])
def test_reset_subject_support_routing(passing: Bundle, reason: str) -> None:
    good = with_reset(passing, reason)
    result = evaluate(good, ports(), gate='shadow-serving')
    own = [f for f in result.failures if f.gap == reason]
    assert len(own) == 1 and own[0].subject['scope'] == 'reset'
    bad = replace(good, reset_support=replace(good.reset_support, support_files={}))
    result = evaluate(bad, ports(), gate='shadow-serving')
    assert reason not in result.gaps
    unsupported = [f for f in result.failures if f.gap == 'missing_support']
    assert unsupported[0].subject == own[0].subject
    assert 'artifact_gap' in result.gaps and not result.gate_report['complete']
    render(result)


def test_reset_history_and_writer_first_times(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
    bundle = with_reset(passing, 'hash', START + 1)
    bundle = change_row(bundle, p1.KINDS[5], lambda m: m.update(complete=False, gaps=[dict(
        interval=dict(start_us=START + 3, end_us=START + 4), reason='observation_missing')]))
    result = evaluate(bundle, ports(), gate='shadow-serving')
    raw, attachments = render(result)
    assert json.loads(raw)['verdict'] == 'BLOCKED'
    history = [e for page in result.detail_streams['reset_history'] for e in page['entries']]
    assert history == list(bundle.reset_support.resets)
    assert result.gate_report['reset_history']['root'] == entry_root(history, stream='reset_history')
    assert result.gate_report['first_reset_at_us'] == START + 1
    assert result.gate_report['first_gap_at_us'] == START + 3
    writers = [e for page in result.detail_streams['writers'] for e in page['entries']]
    first = next(w for w in writers if w['boot'] == 'boot0')
    second = next(w for w in writers if w['boot'] == 'boot1')
    assert first['first_reset_at_us'] == START + 1 and first['first_gap_at_us'] == START + 3
    assert second['first_reset_at_us'] is second['first_gap_at_us'] is None
    assert json.loads(attachments[0]) == result.gate_report


def test_metric_source_table_and_availability(passing: Bundle) -> None:
    from copy import deepcopy

    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_metrics import observations
    sample = json.loads(next(r.raw for r in passing.rows if r.key.kind == p1.KINDS[0]))
    terminal = json.loads(next(r.raw for r in passing.rows if r.key.kind == p1.KINDS[2]))['events'][1]
    samples = [deepcopy(sample) for _ in range(3)]
    for i, s in enumerate(samples):
        for j, key in enumerate(a2.TIMINGS[:8]):
            parent = 'admission' if key.endswith('_age_us') else 'timing'
            s[parent][key] = None if key == 'evidence_write_us' or i == 1 else 10 * j + i
        s['booked_minus_frozen'] = [-5, None, 11][i]
        s['booked_micro'] = None if i == 1 else 10
        s['eligibility']['observed'] = None if i == 2 else True
    terminals = [deepcopy(terminal) for _ in range(3)]
    terminals[0]['comparison'] = None
    terminals[1]['prediction'] = None
    terminals[2]['prediction']['prediction'] = 'unknown'
    inputs = dict(samples=samples, terminals=terminals,
        slots=[dict(cycle_us=2, rpc_elapsed_us=11, rpc_count=99), dict(cycle_us=None, rpc_elapsed_us=0, rpc_count=0)],
        closes=[dict(elapsed_us=3), dict(elapsed_us=7)],
        handoffs=[dict(result_us=4), dict(result_us=8)], terminations=[dict(final_pass_us=9)])
    result = observations(**inputs)
    for j, key in enumerate(a2.TIMINGS[:8]):
        stat = result['timing'][key]
        if key == 'evidence_write_us':
            assert stat == dict(count=0, null_count=3, min=None, p50=None, p95=None, p99=None, max=None)
        else:
            assert stat == dict(count=2, null_count=1, min=j * 10, p50=j * 10, p95=j * 10 + 2, p99=j * 10 + 2, max=j * 10 + 2)
    assert result['timing']['publisher_cycle_us']['count'] == 1
    assert result['timing']['publisher_cycle_us']['null_count'] == 1
    assert result['timing']['publisher_slot_rpc_elapsed_us'] == dict(count=2, null_count=0, min=0, p50=0, p95=11, p99=11, max=11)
    assert [result['timing'][k]['count'] for k in ('close_us', 'handoff_us', 'drain_us')] == [2, 2, 1]
    assert [result['timing'][k]['max'] for k in ('close_us', 'handoff_us', 'drain_us')] == [7, 8, 9]
    assert result['deltas'] == dict(count=2, null_count=1, min_micro=-5, max_micro=11,
        bins={k: int(k in ('-2-10', '+11-100')) for k in a2.BINS})
    assert result['null_counts'] == dict(eligibility=1, booking=1, comparison=1, prediction=1, timing=11, delta=1)
    # Each exact metric-source row is removed independently. Its dependants
    # become unavailable; unrelated observations retain their original values.
    sources = [(k, 'samples', ('admission' if k.endswith('_age_us') else 'timing', k)) for k in a2.TIMINGS[:8]] + [
        ('publisher_cycle_us', 'slots', ('cycle_us',)), ('publisher_slot_rpc_elapsed_us', 'slots', ('rpc_elapsed_us',)),
        ('close_us', 'closes', ('elapsed_us',)), ('handoff_us', 'handoffs', ('result_us',)), ('drain_us', 'terminations', ('final_pass_us',))]
    for key, population, path in sources:
        changed = deepcopy(inputs)
        parent = changed[population][0]
        for field in path[:-1]:
            parent = parent[field]
        del parent[path[-1]]
        observed = observations(**changed)
        assert all(v is None for v in observed['timing'][key].values())
        assert observed['null_counts']['timing'] is None
        assert all(observed['timing'][other] == result['timing'][other] for other in a2.TIMINGS if other != key)
    for population, path, field in (
        ('samples', ('eligibility', 'observed'), 'eligibility'),
        ('samples', ('booked_micro',), 'booking'),
        ('samples', ('booked_minus_frozen',), 'delta'),
        ('terminals', ('comparison',), 'comparison'),
        ('terminals', ('prediction',), 'prediction')):
        changed = deepcopy(inputs)
        parent = changed[population][0]
        for part in path[:-1]:
            parent = parent[part]
        del parent[path[-1]]
        observed = observations(**changed)
        assert observed['null_counts'][field] is None
        assert observed['timing'] == result['timing']
        if field == 'delta':
            assert observed['deltas']['count'] is None
            assert all(v is None for v in observed['deltas']['bins'].values())
    for population in inputs:
        absent = observations(**{**inputs, population: None})
        if population == 'samples':
            assert absent['deltas']['count'] is absent['null_counts']['delta'] is None
            assert all(v is None for v in absent['deltas']['bins'].values())
        elif population == 'terminals':
            assert absent['null_counts']['prediction'] is absent['null_counts']['comparison'] is None
        else:
            assert absent['null_counts']['timing'] is None


def test_overflow_propagates_and_rule_states(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_gate_metrics import counts, deltas, stat, total
    from trusted_router.async_settle_shadow_v2_schema import INT64
    assert total([INT64, 1]) is None
    assert total([0, None]) is None
    assert total([]) == 0
    assert all(v is None for v in stat([INT64 + 1]).values())
    assert deltas([INT64 + 1])['count'] is None
    ctx = decode_bundle(passing, ports(), 'shadow-serving')
    counters = [dict(c) for c in ctx.kind(p1.KINDS[3])]
    counters[0]['A'], counters[1]['A'] = INT64, 1
    observed = counts(ctx, counters, [e for _, e in ctx.events()], ('G4', 'G5'))
    assert observed['partition']['A'] is observed['partition']['T'] is None
    assert observed['comparator']['compared'] is None
    assert observed['comparator']['coverage']['state'] == 'unevaluable'
    result = evaluate(damage(passing, 'eligible_coverage_gap'), ports(), gate='shadow-serving')
    assert result.gate_report['rules']['G5'] == 'unevaluable'
    assert result.gate_report['rules']['G11'] == 'not_required'
    assert result.gate_report['rules']['G6'] == 'pass'
    assert result.gate_report['rules']['G1'] == 'unevaluable'
    render(result)


def test_regional_metric_partitions_and_hterm(passing: Bundle) -> None:
    assert passing.inventory
    bundle = replace(passing, inventory=replace(passing.inventory,
        boots=tuple(replace(b, region='region-two') if b.boot == 'boot1' else b for b in passing.inventory.boots)))
    result = evaluate(bundle, ports(), gate='shadow-serving')
    render(result)
    regions = [r for page in result.detail_streams['regions'] for r in page['entries']]
    assert {r['region'] for r in regions} == {'us-central1', 'region-two'}
    for key, fleet in result.gate_report['timing'].items():
        for field in ('count', 'null_count'):
            assert fleet[field] == sum(r['timing'][key][field] for r in regions)
    assert [r['timing']['publisher_slot_rpc_elapsed_us']['count'] for r in regions] == [5, 5]
    for timing in (result.gate_report['timing'], *(r['timing'] for r in regions)):
        assert timing['close_us']['count'] == timing['handoff_us']['count']
        assert timing['drain_us']['count'] <= timing['close_us']['count']


def test_reset_history_uses_common_64_entry_pages(passing: Bundle) -> None:
    bundle = passing
    resets = []
    for index in range(65):
        bundle = with_reset(bundle, 'hash', START + index + 1)
        resets.extend(bundle.reset_support.resets)
    bundle = replace(bundle, reset_support=replace(bundle.reset_support, resets=tuple(reversed(resets))))
    result = evaluate(bundle, ports(), gate='shadow-serving')
    render(result)
    assert result.gate_report['reset_history']['count'] == 65
    assert [len(page['entries']) for page in result.detail_streams['reset_history']] == [64, 1]
    history = [e for page in result.detail_streams['reset_history'] for e in page['entries']]
    assert [r['at_us'] for r in history] == list(range(START + 1, START + 66))


def test_failure_tally_streams_and_keeps_only_16_subjects() -> None:
    import weakref

    from trusted_router.async_settle_shadow_v2_a2_evidence import W
    from trusted_router.async_settle_shadow_v2_gate_failures import bounded, listing
    from trusted_router.async_settle_shadow_v2_gate_result import Failure
    live = peak = consumed = 0
    def released():
        nonlocal live
        live -= 1
    def source():
        nonlocal live, peak, consumed
        for index in range(2000, 0, -1):
            for day in ('2026-10-05', '2026-10-06'):
                value = Failure('writer_closure_gap', 'G8', W('2026-10-05', 'router', f'boot{index:05}'),
                                1, (), 'absent', (day,))
                live += 1
                consumed += 1
                peak = max(peak, live)
                weakref.finalize(value, released)
                yield value
    failures = bounded(source())
    assert consumed == 4000 and peak <= 20
    assert len(failures) == 16
    counts, prefix = listing(failures, ('2026-10-05', '2026-10-06'))
    assert counts['writer_closure_gap'] == dict(count=2000, listed=16, complete=False, day_counts=[2000, 2000])
    assert [f['subject']['boot'] for f in prefix] == [f'boot{i:05}' for i in range(1, 17)]


def test_peak_rationals_preserve_binary64(passing: Bundle) -> None:
    from fractions import Fraction
    assert passing.budget
    bundle = replace(passing, budget=replace(passing.budget, cpu_peak=0.1))
    result = evaluate(bundle, ports(), gate='shadow-serving')
    expected = Fraction(0.1)
    assert result.gate_report['fleet_peaks']['cpu_percent'] == dict(numerator=expected.numerator, denominator=expected.denominator)
    assert result.gate_report['fleet_peaks']['cpu_percent'] != dict(numerator=1, denominator=10)
    render(result)


@pytest.mark.parametrize('reason', [r.name for r in FAILURE_RULES if r.rule == 'G9' and r.name != 'missing_support'])
def test_unenumerable_reset_preserves_its_named_fallback(passing: Bundle, reason: str) -> None:
    from trusted_router.async_settle_shadow_v2_a2_evidence import N
    assert passing.reset_support
    bad = replace(passing, reset_support=replace(passing.reset_support,
        resets=(dict(at_us=START + 1, revision='invalid', reason=reason),)))
    result = evaluate(bad, ports(), gate='shadow-serving')
    assert [f.subject for f in result.failures if f.gap == reason] == [N()]
    assert {'artifact_gap', 'missing_support', reason} <= set(result.gaps)
    assert result.gate_report['complete'] is False
    render(result)


@pytest.mark.parametrize('residence', ['region', 'writer'])
def test_required_aggregate_availability_at_each_residence(passing: Bundle, monkeypatch: pytest.MonkeyPatch, residence: str) -> None:
    from trusted_router import async_settle_shadow_v2_gate_report as reports
    if residence == 'region':
        original = reports.peaks
        def incomplete_peak(value: Any, ctx: Any, *, publisher: bool = False) -> Any:
            result = original(value, ctx, publisher=publisher)
            if value is not None and value.region is not None:
                result['cpu_percent'] = None
            return result
        monkeypatch.setattr(reports, 'peaks', incomplete_peak)
        rule = 'G7'
    else:
        original_counts = reports.counts
        def incomplete_counts(ctx: Any, counters: Any, events: Any, required: Any, *, enclave: bool = False) -> Any:
            result = original_counts(ctx, counters, events, required, enclave=enclave)
            if counters is not None and len(counters) == 1 and not enclave:
                result['comparator']['exact'] = None
            return result
        monkeypatch.setattr(reports, 'counts', incomplete_counts)
        rule = 'G5'
    result = evaluate(passing, ports(), gate='shadow-serving')
    assert result.gate_report['rules'][rule] == 'unevaluable'
    assert result.gate_report['complete'] is False
    assert 'artifact_gap' in result.gaps
    assert json.loads(render(result)[0])['verdict'] == 'BLOCKED'


def test_external_stage_and_release_package_scope_are_retained(passing: Bundle) -> None:
    from trusted_router.async_settle_shadow_v2_a2_evidence import P
    from trusted_router.async_settle_shadow_v2_gates import external
    assert passing.external
    proof = passing.external.proofs[0]
    scoped = replace(passing.external, proofs=(replace(proof, stage='post-flip'),),
        required_stages={'pre-flip': (proof.name,)})
    ctx = decode_bundle(replace(passing, external=scoped), ports(), 'pre-flip')
    external(ctx)
    assert any(i.gap == 'external_proofs_missing' and i.subject.wire == P('pre-flip', proof.name)
               for i in ctx.issues)
    deferred = replace(scoped, required_stages={'post-flip': (proof.name,)})
    ctx = decode_bundle(replace(passing, external=deferred), ports(), 'pre-flip')
    external(ctx)
    assert not any(i.gap == 'external_proofs_missing' for i in ctx.issues)
