"""Original-byte integration witnesses for all five loading deliverables."""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

import pytest

from tests.shadow_v2_gate_a2_fixture import real_fixture, transport
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_gate_fixture_generator import (
    exhaustive,
    untraced,  # noqa: F401
)
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, decode_bundle
from trusted_router.async_settle_shadow_v2_gate_inputs import Key, Preimage, Row
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_preimages import preimages
from trusted_router.async_settle_shadow_v2_gate_verifiers import approval, check_calls, invoke
from trusted_router.async_settle_shadow_v2_schema import sha256


@pytest.fixture(scope='module')
def real(tmp_path_factory: Any) -> Any:
    return real_fixture(tmp_path_factory.mktemp('real-loading'))


def diagnostics(bundle: Any) -> set[str]:
    return {i['gap'] for name in ('loading_issues', 'loading_validation') for i in bundle.a2_streams.get(name, [])}


def test_load_real_original_bytes_all_views_field_by_field(real: Any) -> None:
    original, ports, expected = real
    bundle = load(transport(original), ports)
    assert bundle.undefined_preimages == ()
    assert diagnostics(bundle) == set()
    assert bundle.inventory == original.inventory
    assert bundle.cohort == original.cohort
    assert bundle.budget is not None and original.budget is not None
    wanted = asdict(original.budget)
    pre = a2.BUDGET_ENVELOPE.parse(next(p.raw for p in original.preimages if p.name == 'proof.pre_budget_sha256'))
    wanted.update(pre_input=expected['pre_input'], pre_digest=sha256(pre), post_pre_digest=sha256(pre),
        post_completed_us=original.completed_at_us, cpu_increment=5., close_elapsed_us=2000000,
        rates=tuple({**rate, 'publisher_reads': 2 * (rate['end_us'] - rate['start_us']) // 1000000,
            'publisher_writes': (rate['end_us'] - rate['start_us']) // 1000000} for rate in expected['rates']))
    wanted['load_segments'] = tuple(expected['segments'])
    wanted['projected_load_segments'] = tuple(expected['projected_segments'])
    from trusted_router.async_settle_shadow_v2_gate_loading import LIMITS
    approved = LIMITS.parse(next(p.raw for p in original.preimages if p.name == 'budgets/limits.json'))
    wanted['peak_limits'] = dict(pre=approved, post=approved)
    actual = asdict(bundle.budget)
    for field in wanted:
        assert actual[field] == wanted[field], field
    assert bundle.reset_support is not None
    assert asdict(bundle.reset_support) == dict(rows=(), proof=dict(resolved_mismatches=[]), resets=(), support_files={})
    assert bundle.external is not None
    assert bundle.external.required == tuple(sorted(ports.release.required))
    assert set(bundle.external.required) == {p.name for p in bundle.external.proofs}
    for proof in bundle.external.proofs:
        original_proof = next(p.raw for p in original.preimages if p.name == 'proofs/' + proof.name)
        assert asdict(proof) == dict(name=proof.name, router_revision='a' * 40, enclave_revision='a' * 40,
            raw=original_proof, sha256=roots.file_digest(original_proof), passed=True, stage='shadow-serving')
    assert (bundle.external.drain_selected, bundle.external.signer_checked,
            bundle.external.attestation_checked, bundle.external.exposure_accepted, bundle.external.post_input) == (False, False, False, False, None)


@exhaustive
@pytest.mark.parametrize('path', [
    'proof.pre_budget_sha256', 'proof.post_budget_sha256', 'proof.external_proofs_sha256',
    'proof.resolution_dag_sha256', 'proof.shutdown_inventory_sha256', 'proof.cohort_seed_policy_sha256',
    'budgets/pre/input.json', 'budgets/post/input.json', 'budgets/pre/output.json',
    'budgets/post/output.json', 'budgets/limits.json', 'loading/budget-measurements.json',
    'loading/release-plan.json', 'sources/pilot_enablement.py', 'sources/shadow_report.py',
])
@exhaustive
@pytest.mark.parametrize('mutation', ['absent', 'wrong-byte', 'conflicting', 'oversize'])
def test_each_payload_original_mutation_blocks(real: Any, path: str, mutation: str) -> None:
    original, ports, _ = real
    files = list(original.preimages)
    index = next(i for i, p in enumerate(files) if p.name == path)
    if mutation == 'absent':
        files.pop(index)
    elif mutation == 'conflicting':
        files.append(Preimage(path, files[index].raw + b'\n'))
    else:
        raw = files[index].raw
        if mutation == 'oversize':
            raw = b' ' * 262145
        elif path.startswith('proof.'):
            # C(object) deliberately tolerates a fixture LF. Change a value.
            raw = raw.replace(b'fixture-reviewer', b'fixture-reviewez', 1) if b'fixture-reviewer' in raw else raw.replace(b'inputs', b'inputz', 1).replace(b'/input.json', b'/input.jsoz', 1)
        else:
            raw += b'\n'
        files[index] = Preimage(path, raw)
    loaded = load(transport(replace(original, preimages=tuple(files))), ports)
    assert 'artifact_gap' in diagnostics(loaded), (path, mutation)


@exhaustive
@pytest.mark.parametrize('field', [
    'pre_budget_sha256', 'post_budget_sha256', 'external_proofs_sha256', 'resolution_dag_sha256',
    'shutdown_inventory_sha256', 'cohort_seed_policy_sha256', 'ledger_root', 'publisher_slot_trace_sha256',
    'evaluator_fixture_sha256', 'binding_fixture_sha256', 'wire_fixture_sha256',
    'close_result_root', 'publisher_trace_root', 'health_basis_root', 'handoff_accounting_root', 'authorization_obligation_root',
])
def test_each_proof_digest_has_defined_original_preimage(real: Any, field: str) -> None:
    original, ports, _ = real
    proof = a1.PROOF_MANIFEST.parse(original.proof)
    proof[field] = roots.file_digest(b'wrong digest preimage')
    loaded = load(transport(replace(original, proof=a1.PROOF_MANIFEST.serialize(proof))), ports)
    assert 'artifact_gap' in diagnostics(loaded)


@exhaustive
@pytest.mark.parametrize('kind,gap', [(p1.KINDS[3], 'phase_coverage_gap'), (p1.KINDS[4], 'writer_closure_gap'),
    (p1.KINDS[2], 'authorize_coverage_gap'), (a1.KINDS[-1], 'close_result_identity_gap'),
    ('publisher_trace', 'publisher_trace_gap'), ('health_basis', 'shadow_health_gap')])
def test_deleting_every_observed_source_preserves_obligation(real: Any, kind: str, gap: str) -> None:
    original, ports, _ = real
    damaged = replace(original, rows=tuple(r for r in original.rows if r.key.kind != kind),
                      attachments=tuple(r for r in original.attachments if r.key.kind != kind))
    loaded = load(transport(damaged), ports)
    assert gap in diagnostics(loaded)
    assert 'artifact_gap' in diagnostics(loaded)


def test_missing_trace_is_a_missing_entry_not_an_empty_root(real: Any) -> None:
    original, ports, _ = real
    loaded = load(transport(original), ports)
    damaged = replace(loaded, attachments=tuple(r for r in loaded.attachments if r.key.kind != 'publisher_trace'))
    ctx = decode_bundle(damaged, ports, 'pre-flip')
    manifest = ctx.kind(p1.KINDS[5])[0]
    _, inputs = preimages(ctx, manifest)
    expected = inputs['proof.publisher_slot_trace_sha256']
    assert len(expected) == 1
    assert roots._root_entry(expected[0]['key'], expected[0]['bodies'])['state'] == 'missing'


def test_unexpected_receipt_does_not_expand_expected_root(real: Any) -> None:
    original, ports, _ = real
    loaded = load(transport(original), ports)
    receipt = next(r for r in loaded.rows if r.key.kind == p1.KINDS[2])
    unexpected = Row(Key(receipt.key.kind, receipt.key.id.replace('boot0', 'outsider')), receipt.raw)
    ctx = decode_bundle(replace(loaded, rows=loaded.rows + (unexpected,)), ports, 'pre-flip')
    _, inputs = preimages(ctx, ctx.kind(p1.KINDS[5])[0])
    assert all('outsider' not in e['key'] for e in inputs['manifest.receipts_root'])


@exhaustive
@pytest.mark.parametrize('mutation', ['source-pin', 'fixture-pin', 'key-pin', 'review', 'plan', 'port', 'callable', 'key-order'])
def test_independent_release_authority_blocks_substitutions(real: Any, mutation: str) -> None:
    original, ports, _ = real
    loaded = load(transport(original), ports)
    if mutation in ('source-pin', 'fixture-pin', 'key-pin'):
        release = ports.release
        pins = dict(release.verifiers)
        entry = ports.approvals[1].entry_point
        value = list(pins[entry])
        value[('source-pin', 'fixture-pin', 'key-pin').index(mutation)] = roots.file_digest(b'substitution')
        pins[entry] = tuple(value)
        ports = replace(ports, release=replace(release, verifiers=pins))
    elif mutation == 'review':
        plan = ports.release
        pins = {k: v for k, v in plan.artifacts.items() if not k.startswith('reviews/')}
        ports = replace(ports, release=replace(plan, artifacts=pins))
        assert 'artifact_gap' in diagnostics(load(transport(original), ports))
        return
    elif mutation == 'plan':
        ports = replace(ports, release=None)
    elif mutation == 'port':
        ports = replace(ports, verify_binding=None)
    elif mutation == 'callable':
        ports = replace(ports, verify_binding=lambda *args, **kwargs: True)
    else:
        approved = ports.approvals[1]
        ctx = Context(loaded, ports, 'pre-flip')
        with pytest.raises(ValueError, match='evidence_schema_gap'):
            invoke(ctx, 'binding', 'irrelevant', approved.keys + approved.keys, 0)
        return
    ctx = Context(loaded, ports, 'pre-flip')
    with pytest.raises(ValueError):
        approval(ctx, 'binding')


def test_suppressed_verifier_call_blocks(real: Any) -> None:
    original, ports, _ = real
    ctx = decode_bundle(load(transport(original), ports), ports, 'pre-flip')
    check_calls(ctx)
    assert any(i.gap == 'evidence_schema_gap' and 'missing applicable binding' in i.detail for i in ctx.issues)


def test_release_plan_requires_external_pin(real: Any) -> None:
    from scripts.async_settle.async_settle_shadow_v2_gate_tools import read_release_plan
    _, ports, _ = real
    plan = read_release_plan(ports.release.raw, roots.file_digest(ports.release.raw))
    assert plan.verifiers == ports.release.verifiers
    with pytest.raises(ValueError, match='artifact_gap'):
        read_release_plan(ports.release.raw, roots.file_digest(b'not approved'))


@exhaustive
@pytest.mark.parametrize('field', ['counter_root', 'closure_root', 'receipts_root', 'prerequisite_ledger_root',
    'independent_inventory_sha256', 'close_result_root', 'publisher_trace_root', 'health_basis_root',
    'config_sha256', 'fixture_sha256', 'proof_manifest_sha256'])
def test_each_manifest_digest_uses_its_declared_preimage(real: Any, field: str) -> None:
    original, ports, _ = real
    rows = []
    for row in original.rows:
        if row.key.kind == p1.KINDS[5]:
            value = a1.MANIFEST.parse(row.raw)
            value[field] = roots.file_digest(b'undefined preimage')
            row = Row(row.key, a1.MANIFEST.serialize(value))
        rows.append(row)
    assert 'artifact_gap' in diagnostics(load(transport(replace(original, rows=tuple(rows))), ports))


def test_every_declared_file_ref_requires_exact_original_bytes(real: Any) -> None:
    from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    original, _, _ = real
    files = {p.name: p.raw for p in original.preimages}
    references: dict[str, Any] = {}
    def visit(value: Any) -> None:
        if type(value) is dict:
            if set(value) == set(a2.FILE_REF.fields):
                references[value['path']] = value
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)
    for raw in files.values():
        try:
            visit(retained_json(raw))
        except ValueError:
            pass
    graph = PayloadGraph(files)
    assert len(references) > 50
    for path, reference in references.items():
        expected = files[path]
        assert graph.file(reference) == expected
        files[path] = expected + b'\n'
        with pytest.raises(ValueError, match='artifact_gap'):
            graph.file(reference)
        del files[path]
        with pytest.raises(ValueError, match='artifact_gap'):
            graph.file(reference)
        files[path] = expected


@exhaustive
@pytest.mark.parametrize('mutation', ['empty-checks', 'unknown-check', 'wrong-revision', 'future', 'wrong-boot'])
def test_prerequisite_provenance_is_contextual(real: Any, mutation: str) -> None:
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    from trusted_router.async_settle_shadow_v2_gate_provenance import prerequisite
    original, ports, _ = real
    files = {p.name: p.raw for p in original.preimages}
    ledger = a1.PREREQUISITE_LEDGER.parse(original.ledgers[0])
    field = 'boot_transport'
    evidence = a2.PROOF_RESULT.parse(files['ledger/' + ledger['boot'] + '/' + field])
    if mutation == 'empty-checks':
        evidence['checks'] = []
    elif mutation == 'unknown-check':
        evidence['checks'][0]['name'] = 'not_in_release_inventory'
    elif mutation == 'wrong-revision':
        ledger['revision'] = 'b' * 40
    elif mutation == 'future':
        evidence['completed_at_us'] = original.completed_at_us + 1
    else:
        ledger['boot'] = 'other-boot'
    with pytest.raises(ValueError, match='evidence_schema_gap'):
        prerequisite(PayloadGraph(files, original.completed_at_us, ports=ports), evidence, ledger, field)


def test_loaded_signature_material_uses_approved_key_sequence(real: Any) -> None:
    original, ports, _ = real
    loaded = load(transport(original), ports)
    assert loaded.signatures == original.signatures
    from trusted_router.async_settle_shadow_v2_gate_bundle import verify_samples
    ctx = decode_bundle(loaded, ports, 'pre-flip')
    verify_samples(ctx)
    assert ctx.verifier_calls['binding'] == 1
    assert loaded.signatures[0].object_id in ctx.verified


def test_conflicting_file_exports_keep_both_original_candidates() -> None:
    from trusted_router.async_settle_shadow_v2_gate_preimages import original_files
    originals = (Preimage('source.json', b'{"a":1}'), Preimage('source.json', b'{"a":2}'))
    files = original_files(originals)
    assert 'source.json' not in files
    assert set(files.values()) == {p.raw for p in originals}
    assert all(path.endswith(roots.file_digest(raw)) for path, raw in files.items())


@pytest.fixture(scope='module')
def pilot_real(tmp_path_factory: Any) -> Any:
    return real_fixture(tmp_path_factory.mktemp('real-pilot'), pilot_stage='pre-flip')


@pytest.mark.parametrize('gate', ['transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving'])
def test_original_bytes_stage_pass(real: Any, gate: str) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = real
    loaded = load(transport(original), ports)
    assert not diagnostics(loaded)
    result = evaluate(loaded, ports, gate=gate)
    raw, attachments = render(result)
    assert json.loads(raw)['verdict'] == 'PASS', [(f.gap, f.detail) for f in result.failures]
    assert json.loads(attachments[0])['complete'] is True


def test_original_bytes_post_flip_pass(tmp_path: Any) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = real_fixture(tmp_path, pilot_stage='post-flip')
    loaded = load(transport(original), ports)
    assert not diagnostics(loaded)
    result = evaluate(loaded, ports, gate='post-flip')
    assert json.loads(render(result)[0])['verdict'] == 'PASS', [(f.gap, f.detail) for f in result.failures]


def test_original_bytes_empty_load_segment_is_legal(tmp_path: Any) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = real_fixture(tmp_path, empty_segment=True)
    loaded = load(transport(original), ports)
    assert not diagnostics(loaded)
    assert loaded.budget is not None and loaded.budget.load_segments is not None
    assert any(s['start_us'] == s['end_us'] for s in loaded.budget.load_segments)
    result = evaluate(loaded, ports, gate='fleet-budgets')
    assert result.gaps == (), [(f.gap, f.detail) for f in result.failures]
    assert result.gate_report['fleet_peaks']['publication_operations_per_second'] == dict(numerator=1, denominator=2)
    assert json.loads(render(result)[0])['verdict'] == 'PASS'


def test_lookup_cache_keeps_mutation_isolation_and_schema_rejections(request: Any) -> None:
    from trusted_router.async_settle_shadow_v2_schema import Schema, integer, literal
    from trusted_router.detached_jws import canonical
    caches = request.getfixturevalue('payloadgraph_shared_lookup_cache')
    schema = Schema({'value': integer}, 128)
    other_schema = Schema({'value': literal(2)}, 128)
    raw = canonical({'value': 1})
    before = caches['schema'].cache_info()
    first = schema.parse(raw)
    first['value'] = 99
    assert schema.parse(raw) == {'value': 1}
    assert schema.parse(canonical({'value': 2})) == {'value': 2}
    for _ in range(2):
        with pytest.raises(ValueError, match='evidence_schema_gap'):
            schema.parse(canonical({'value': True}))
        with pytest.raises(ValueError, match='evidence_schema_gap'):
            other_schema.parse(raw)
    after = caches['schema'].cache_info()
    assert after.hits >= before.hits + 3
    assert after.misses >= before.misses + 4


def test_cached_loading_reexecutes_retained_calls(real: Any) -> None:
    import sys
    original, ports, _ = real
    raw = transport(original)
    code = ports.retained.__code__
    calls = 0
    previous = sys.getprofile()
    def observed(frame: Any, event: str, arg: Any) -> None:
        nonlocal calls
        if event == 'call' and frame.f_code is code:
            calls += 1
        if callable(previous):
            previous(frame, event, arg)
    sys.setprofile(observed)
    try:
        first = load(raw, ports)
        first_calls = calls
        second = load(raw, ports)
    finally:
        sys.setprofile(previous)
    assert first_calls > 0 and calls == 2 * first_calls
    assert not diagnostics(first) and not diagnostics(second)
    assert first.a2_streams['retained_calls'] == second.a2_streams['retained_calls']
