"""Ordinary traced predicate witnesses and lossless gate regression tests."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from tests.shadow_v2_gate_fixture_generator import START, build, change_row, ports
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, decode_bundle
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Row
from trusted_router.async_settle_shadow_v2_gate_result import Subject, render
from trusted_router.async_settle_shadow_v2_gates import GAP_GROUPS, RULES, clock, evaluate
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
        return counter(lambda c: c['admission_observer'].update(prediction_yes=0))
    if gap == 'prediction_unknown_gap':
        return counter(lambda c: c['admission_observer'].update(prediction_yes=0, prediction_unknown=1))
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
    if gap == 'exclusion_conflict':
        return counter(lambda c: c['diagnostics'].update(exclusion_conflict=1))
    if gap == 'missing_support':
        return replace(bundle, reset_support=None)
    if gap in GAP_GROUPS['G9']:
        assert bundle.reset_support
        return replace(bundle, reset_support=replace(bundle.reset_support,
            resets=(dict(at_us=START + 1, revision='a' * 40, reason=gap),)))
    if gap == 'positive_seed_gap':
        gate = json.loads(bundle.transport)
        gate['completed_at_us'] = START + 2000000
        return replace(bundle, transport=canonical(gate))
    if gap == 'regional_seed_gap':
        assert bundle.inventory
        return replace(bundle, inventory=replace(bundle.inventory,
            boots=(*bundle.inventory.boots, replace(bundle.inventory.boots[0], boot='new', region='europe-west1'))))
    if gap == 'continuous_window_gap':
        return bundle
    if gap == 'external_proofs_missing':
        assert bundle.external
        return replace(bundle, external=replace(bundle.external, proofs=()))
    if gap == 'pilot_prerequisite_gap':
        assert bundle.external
        return replace(bundle, external=replace(bundle.external, signer_checked=False))
    raise AssertionError(gap)


GAPS = tuple(rule.gap for rule in RULES)
pytestmark = pytest.mark.timeout(method='signal')


@pytest.fixture(scope='module')
def positive_results(passing: Bundle) -> dict[str, Any]:
    return {stage: evaluate(passing, ports(), gate=stage) for stage in ('shadow-serving', 'pre-flip')}


@pytest.mark.parametrize('gap', GAPS)
def test_gap_negative(passing: Bundle, gap: str) -> None:
    result = evaluate(damage(passing, gap), ports())
    assert result.verdict == 'BLOCKED'
    assert gap in result.gaps, (gap, result.gaps)
    assert any(f.gap == gap for f in result.failures)


@pytest.mark.parametrize('gap', GAPS)
def test_gap_positive(positive_results: dict[str, Any], gap: str) -> None:
    # Seven-day duration has a separate full-bundle witness; shadow-serving is
    # intentionally allowed to pass before the seven-day deadline.
    result = positive_results['shadow-serving' if gap == 'continuous_window_gap' else 'pre-flip']
    assert gap not in result.gaps, (gap, result.gaps)


def test_one_producer_and_both_directions() -> None:
    names = [r.gap for r in RULES]
    assert len(names) == len(set(names))
    assert set(names) == {g for gaps in GAP_GROUPS.values() for g in gaps}
    assert len({r.decide for r in RULES}) == len(RULES)
    assert all(r.decide.__name__ == 'decide_' + r.gap for r in RULES)
    assert set(test_gap_positive.pytestmark[0].args[1]) == set(names)
    assert set(test_gap_negative.pytestmark[0].args[1]) == set(names)


def test_lossless_more_than_128_subjects() -> None:
    context = Context(Bundle(), None, 'pre-flip')
    for i in range(200):
        context.issue('writer_closure_gap', 'absent', Subject('2026-10-06', f'boot{i}'), tuple(f'receipt{j:04}' for j in range(300)))
    rule = next(r for r in RULES if r.gap == 'writer_closure_gap')
    failures = rule.decide(context)
    assert len(failures) == 200
    assert all(f.count == 300 and len(f.object_ids) == 300 for f in failures)
    assert len({f.subject.boot for f in failures}) == 200


def test_evaluates_later_rules_after_failure(passing: Bundle) -> None:
    bad = damage(damage(passing, 'policy_version_gap'), 'external_proofs_missing')
    result = evaluate(bad, ports())
    assert {'policy_version_gap', 'evidence_schema_gap', 'external_proofs_missing'} <= set(result.gaps)


def test_missing_verifier_is_artifact_gap(passing: Bundle) -> None:
    assert 'artifact_gap' in evaluate(passing).gaps


def test_internal_pass_and_render_seam(passing: Bundle) -> None:
    result = evaluate(passing, ports(), gate='shadow-serving')
    assert result.verdict == 'PASS' and result.gaps == ()
    assert result.report.writers[1].prediction_state == 'not_exercised'
    assert result.report.writers[1].unknown_ratio is None
    with pytest.raises(ValueError, match='undefined preimage'):
        render(result)


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
    import re
    from pathlib import Path
    design = (Path(__file__).parents[1] / 'docs/design/async-settle-shadow-v2.md').read_text()
    table = design.split('## 8. Gate v2', 1)[1].split('For G6 cadence', 1)[0]
    names = {name for line in table.splitlines() if line.startswith('| G') or line.startswith('| **A1 AFTER G')
             for name in re.findall(r'`([^`]+)`', line.split('|')[-2])}
    assert names <= set(GAPS)
    assert set(GAPS) - names == {'hash', 'identity', 'normalization', 'evaluator_disagreement', 'counter_mismatch', 'reviewed_resolution'}


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
