"""Independent A2 release, contract, identity and commitment witnesses."""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import itertools
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests import shadow_v2_a2_fixture_generator as generator
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as c
from trusted_router import async_settle_shadow_v2_a2_evidence as e
from trusted_router import async_settle_shadow_v2_a2_roots as r
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router.detached_jws import canonical

# Importing the required baseline decorator eagerly builds its exhaustive seed
# grids. Collection is not a coverage witness: keep that pure setup untraced,
# then restore the exact tracer for every new module's explicit branch tests.
_collection_trace = sys.gettrace()
try:
    sys.settrace(None)
    from tests.test_async_settle_shadow_v2_contract import assert_round_trip, strict_equal
    from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
finally:
    sys.settrace(_collection_trace)

FIXTURES = generator.OUT
VECTORS = [(path.stem, v) for path in sorted(FIXTURES.glob('*.json')) if path.stem != 'manifest'
           for v in json.loads(path.read_bytes())['variants']]
SCHEMAS = {name: schema for name, schema in vars(e).items() if isinstance(schema, s.Schema)}


def vector(file: str, name: str) -> dict[str, Any]:
    return copy.deepcopy(next(v for f, v in VECTORS if f == file and v['name'] == name))


def obj(file: str, name: str) -> Any:
    return vector(file, name)['object']


def run_vector(v: dict[str, Any]) -> Any:
    value = copy.deepcopy(v['object'])
    target = v['target']
    if target in SCHEMAS:
        schema = SCHEMAS[target]
        raw = canonical(value)
        raw += b' ' * (v.get('transport_bytes', len(raw)) - len(raw))
        return schema.parse(raw)
    if target == 'root_entry':
        return r._root_entry(value['key'], [bytes.fromhex(b) for b in value['bodies_hex']])
    if target == 'coverage_root':
        return a1.coverage_root(value)
    if target == 'file_digest':
        return r.file_digest(bytes.fromhex(value['hex']))
    if target == 'validate_preimages':
        value['artifacts'] = {k: bytes.fromhex(v) for k, v in value.pop('artifacts_hex').items()}
        for candidates in value['roots'].values():
            for candidate in candidates:
                if 'bodies_hex' in candidate:
                    candidate['bodies'] = [bytes.fromhex(raw) for raw in candidate.pop('bodies_hex')]
        return c.validate_preimages(**value)
    if target == 'validate_gate_output':
        return c.validate_gate_output(**value)
    if target in ('entry_root', 'validate_detail'):
        return getattr(r, target)(**value)
    if target == 'metric_population':
        # Independent literal ownership only; P5 will evaluate S/T/J/H inputs.
        assert value['S'] == 1 and value['T'] == 2
        assert value['deltas']['bins']['-2-10'] == 1
        assert sum(value['deltas']['bins'].values()) == 1
        s1 = sorted(slot['rpc_elapsed_us'] for slot in value['slots'])
        assert value['publisher_slot_rpc_elapsed_us'] == dict(count=2, null_count=0, min=s1[0], p50=s1[0], p95=s1[1], p99=s1[1], max=s1[1])
        return None
    raise AssertionError(target)


@exhaustive
@pytest.mark.parametrize('file,v', VECTORS, ids=[f + ':' + v['name'] for f, v in VECTORS])
def test_frozen_vectors(file: str, v: dict[str, Any]) -> None:
    if v['expected'] == 'reject':
        with pytest.raises(ValueError):
            run_vector(v)
    else:
        result = run_vector(v)
        if 'result' in v:
            assert strict_equal(result, v['result'])
        if v['target'] in SCHEMAS:
            assert_round_trip(SCHEMAS[v['target']], v['object'])
        if 'subject_bytes' in v:
            value = v['object'].get('subject', v['object'])
            assert canonical(value) == v['subject_bytes'].encode()


def test_release_and_frozen_identities() -> None:
    raw = (FIXTURES / 'manifest.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == e.FIXTURE_MANIFEST_SHA256
    manifest = json.loads(raw)
    from trusted_router.async_settle_shadow_v2_contract import FIXTURE_MANIFEST_SHA256
    assert manifest['baseline_manifest_sha256'] == FIXTURE_MANIFEST_SHA256
    assert manifest['source_a1_manifest_sha256'] == a1.FIXTURE_MANIFEST_SHA256
    assert {p.name for p in FIXTURES.glob('*.json')} == {n + '.json' for n in generator.FILES} | {'manifest.json'}
    actual = {str(p.relative_to(FIXTURES)): p.read_bytes() for p in FIXTURES.rglob('*.json')}
    assert set(actual) == set(manifest['files']) | {'manifest.json'}
    for path, entry in manifest['files'].items():
        data = actual[path]
        assert data.endswith(b'\n') and not data.endswith(b'\n\n')
        assert hashlib.sha256(data).hexdigest() == entry['sha256']
        variants = json.loads(data).get('variants', [])
        assert entry['variants'] == [v['name'] for v in variants]
        assert entry['variant_sha256'] == {v['name']: hashlib.sha256(canonical(v)).hexdigest() for v in variants}
        if path.startswith('a1/'):
            assert data == (generator.A1 / path[3:]).read_bytes()
    assert generator.build() == actual


def test_single_name_table_and_constructor_vectors() -> None:
    assert len(e.FAILURE_RULES) == len({row.name for row in e.FAILURE_RULES}) == 58
    assert [Counter(row.rule for row in e.FAILURE_RULES)[f'G{i}'] for i in range(1, 12)] == [3, 6, 5, 4, 3, 9, 7, 7, 9, 3, 2]
    assert set(e.FAILURE_COUNTS.fields) == {row.name for row in e.FAILURE_RULES}
    vectors = {v['object']['gap']: v for f, v in VECTORS if f == 'gate_failures' and v['name'].startswith('constructor-')}
    assert set(vectors) == set(e.FAILURE_COUNTS.fields)
    for row in e.FAILURE_RULES:
        v = vectors[row.name]
        assert (v['producer'], v['population'], v['unevaluable']) == (row.rule, row.population, row.unevaluable)
        assert v['constructor'] in row.constructor
        e.GATE_FAILURE(v['object'])
    with pytest.raises(ValueError):
        e.GAP_NAME('fifty_ninth_name')
    tally = obj('gate_report', 'pre-enable-null-populations')['failure_counts']
    with pytest.raises(ValueError):
        e.FAILURE_COUNTS({**tally, 'fifty_ninth_name': next(iter(tally.values()))})


def test_constructor_bytes_and_no_enrichment() -> None:
    event = vector('gate_failures', 'subject-E')['event']
    constructors = {
        'N': e.N(), 'D': e.D(generator.DAY), 'W': e.W(generator.DAY, 'router', 'w1'),
        'B': e.B(generator.DAY, 'w1', 'settle', {'adapter': 'openai', 'route_type': 'responses', 'streamed': False}),
        'O': e.O('async_settle_shadow_close_result_v2', generator.DAY + '/w1'),
        'I': e.I(generator.DAY, 'w1', '00000000000000000001', 0),
        'V': e.V('us-central1'), 'K': e.K('pre', None), 'A': e.A('pre'), 'P': e.P('pre-flip', 'pilot'),
        'E': e.E(generator.DAY, 'w1', '00000000000000000001', event['seq'], event=event),
    }
    for code, value in constructors.items():
        assert canonical(value) == vector('gate_failures', 'subject-' + code)['subject_bytes'].encode()
        assert_round_trip(e.FAILURE_SUBJECT, value)
        for field in value:
            if value[field] is None:
                candidate = {**value, field: {'day': generator.DAY, 'role': 'router', 'boot': 'w2', 'phase': 'worker',
                    'dimensions': {'adapter': None, 'route_type': None, 'streamed': None}, 'object': {'kind': 'row', 'key': 'row', 'event_seq': None},
                    'at_us': 1, 'revision': generator.REV}[field]}
                with pytest.raises(ValueError):
                    e.FAILURE_SUBJECT(candidate)
    reset = obj('resolution_dag', 'reset')
    assert e.Q(reset) == obj('gate_failures', 'constructor-hash')['subject']
    reset.update(at_us=None, reason='counter_mismatch')
    assert e.Q(reset)['at_us'] is None
    with pytest.raises(ValueError):
        e.Q({**reset, 'reason': 'hash'})


def test_wrong_subject_for_name() -> None:
    failure = obj('gate_failures', 'constructor-writer_closure_gap')
    with pytest.raises(ValueError):
        e.GATE_FAILURE({**failure, 'subject': e.D(generator.DAY)})
    with pytest.raises(ValueError):
        e.GATE_FAILURE({**failure, 'subject': e.W(generator.DAY, 'enclave', 'w1')})


def test_display_is_not_identity() -> None:
    f = obj('gate_failures', 'invalid-first-event-claims-99')
    display = c.display_failure(f)
    assert display == ('gap=evidence_schema_gap;scope=object;day=2026-10-12;role=router;boot=w1;phase=~;adapter=~;route=~;streamed=~;kind=a2_invalid_receipt;key=34%3A2026-10-12%2Fw1%2F00000000000000000001%2F5%3Aevent%2F1%3A0;event_seq=~;at_us=~;revision=~')
    assert len(display) > 138
    assert c.display_failure(obj('gate_failures', 'constructor-prediction_demand_gap')).find('streamed=false') > 0


@pytest.mark.parametrize('label', ['wrong-report-hash', 'gap-without-count', 'count-without-gap', 'pass-nonzero', 'missing-port', 'null-as-zero', 'elapsed-zero-real', 'wrong-subject', 'dropped-page', 'reordered-list'])
def test_output_mutation_witnesses(label: str) -> None:
    with pytest.raises(ValueError):
        c.validate_gate_output(**obj('gate_outputs_a2', label))


def test_required_aggregate_and_rule_cannot_pass_unevaluated() -> None:
    args = obj('gate_outputs_a2', 'pre-enable-structural-pass')
    args['report']['rules']['G5'] = 'pass'
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)
    args['report']['rules']['G5'] = 'unevaluable'
    args['output']['coverage_sha256'] = s.sha256(args['report'])
    with pytest.raises(ValueError):
        c.validate_gate_output(**args)


def test_a1_delegation_and_root_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    assert e.ROOT_ENTRY is a1.COVERAGE_ENTRY
    assert e.GATE_OUTPUT is a1.GATE_OUTPUT
    calls = []
    original = s.Schema.__call__
    def spy(self: s.Schema, value: Any) -> None:
        if self is a1.GATE_OUTPUT:
            calls.append(value)
            raise ValueError('a1-delegate')
        original(self, value)
    monkeypatch.setattr(s.Schema, '__call__', spy)
    with pytest.raises(ValueError, match='a1-delegate'):
        c.validate_gate_output(**obj('gate_outputs_a2', 'pre-enable-structural-pass'))
    assert len(calls) == 1


def test_conflict_literals_permutations_and_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = obj('root_preimages', 'conflict-entry-2')
    for bodies in itertools.permutations([b'{}', b'{}\n']):
        assert r._root_entry('10:2026-10-12/2:w1', bodies) == expected
        assert r.entry_root([expected], stream='counter_root') == '129591526645105fa353f07120da3ca4905ce6ec7ac74914a96604ec9fb1e2f1'
    monkeypatch.setattr(r, 'file_digest', lambda raw: 'a' * 64)
    with pytest.raises(ValueError, match='artifact_gap'):
        r._root_entry('10:2026-10-12/2:w1', [b'{}', b'{}\n'])


def test_stream_enum_and_undefined_preimage() -> None:
    with pytest.raises(ValueError):
        run_vector(vector('root_preimages', 'undefined-preimage'))
    with pytest.raises(ValueError):
        r.entry_root([], stream='undefined')
    missing = vector('root_preimages', 'all-defined-preimages-empty')
    del missing['object']['roots']['manifest.counter_root']
    with pytest.raises(ValueError, match='artifact_gap'):
        run_vector(missing)


def test_ports_exact_signature_without_binding_import() -> None:
    from trusted_router.async_settle_shadow_binding import verify_binding
    from trusted_router.async_settle_shadow_v2_certificate import verify_certificate
    for port, actual in ((c.BindingVerifier, verify_binding), (c.CertificateVerifier, verify_certificate)):
        parameters = list(inspect.signature(port.__call__).parameters.values())[1:]
        expected = list(inspect.signature(actual).parameters.values())
        assert [(p.name, p.kind, p.default) for p in parameters] == [(p.name, p.kind, p.default) for p in expected]
    root = Path(__file__).parents[1]
    script = '''import importlib,pathlib,sys
for p in pathlib.Path("src/trusted_router").glob("async_settle_shadow_v2_a2_*.py"):
 importlib.import_module("trusted_router."+p.stem)
assert "trusted_router.async_settle_shadow_binding" not in sys.modules
assert not any(n.startswith(("trusted_router.storage", "trusted_router.services", "trusted_router.routes")) or n == "trusted_router.main" for n in sys.modules)
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env={**os.environ, 'PYTHONPATH': str(root / 'src')}, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert result.returncode == 0, result.stderr
    for path in (root / 'src/trusted_router').glob('async_settle_shadow_v2_a2_*.py'):
        tree = ast.parse(path.read_text())
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ('open', 'eval', 'exec') for n in ast.walk(tree))


def test_verifier_record_entrypoint_cannot_be_substituted() -> None:
    records = obj('verifier_ports', 'both-records')
    for port in ('certificate', 'binding'):
        bad = copy.deepcopy(records)
        bad[port]['entry_point'] = 'same_signature.always_true'
        with pytest.raises(ValueError):
            e.VERIFIERS(bad)
        bad = copy.deepcopy(records)
        bad[port]['keyset_sha256'] = None
        with pytest.raises(ValueError):
            e.VERIFIERS(bad)


def test_delegated_payload_has_no_local_validator_copy() -> None:
    pre = obj('budget_envelopes', 'pre')
    post = obj('budget_envelopes', 'post')
    with pytest.raises(ValueError):
        e.BUDGET_ENVELOPE({**pre, 'retained': post['retained']})
    dag = obj('resolution_dag', 'dag')
    with pytest.raises(ValueError):
        e.RESOLUTION_DAG({**dag, 'retained': pre['retained']})


def test_exact_public_function_surface() -> None:
    for module, names in ((r, {'file_digest', 'entry_root', 'validate_detail'}),
                          (c, {'validate_gate_output', 'validate_preimages', 'display_failure'})):
        assert {name for name, value in vars(module).items() if inspect.isfunction(value)
                and value.__module__ == module.__name__ and not name.startswith('_')} == names


# One small traced witness for each fixture target/verdict; the complete grid
# above exercises every literal without paying repeated line-tracing cost.
TRACED = {}
for _file, _vector in VECTORS:
    _key = (_vector['target'], _vector['expected'])
    if _key not in TRACED or len(json.dumps(_vector)) < len(json.dumps(TRACED[_key][1])):
        TRACED[_key] = (_file, _vector)


@pytest.mark.parametrize('file,v', list(TRACED.values()), ids=[':'.join(key) for key in TRACED])
def test_traced_fixture_branches(file: str, v: dict[str, Any]) -> None:
    test_frozen_vectors(file, v)


# Literal transcription of §7.5.2's constructor column, not letters in prose.
CONSTRUCTOR_TABLE = {
    'policy_version_gap': ('O', 'E', 'I'),
    'evidence_schema_gap': ('O', 'E', 'I'),
    'artifact_gap': ('O', 'N'),
    'writer_roster_gap': ('D',),
    'regional_roster_unknown': ('W',),
    'legacy_region_label': ('W',),
    'legacy_sample_region': ('O',),
    'flag_interval_gap': ('W',),
    'boot_prerequisite_gap': ('W',),
    'authorize_coverage_gap': ('W',),
    'authorization_link_gap': ('E',),
    'authorization_obligation_gap': ('W',),
    'authorization_terminal_conflict': ('E',),
    'authorization_horizon_gap': ('E',),
    'observed_partition_gap': ('B',),
    'terminal_coverage_gap': ('E',),
    'phase_coverage_gap': ('W',),
    'exclusion_proof_gap': ('E',),
    'eligible_coverage_gap': ('W',),
    'prediction_demand_gap': ('B',),
    'prediction_unknown_gap': ('B',),
    'observer_cadence_gap': ('W',),
    'observer_budget_gap': ('W',),
    'shadow_health_gap': ('W',),
    'health_source_gap': ('W',),
    'health_timestamp_gap': ('W',),
    'health_elapsed_gap': ('W',),
    'health_slot_gap': ('W',),
    'publisher_accounting_gap': ('W',),
    'publisher_trace_gap': ('W',),
    'simultaneous_capacity_gap': ('K',),
    'pre_enable_budget': ('A',),
    'publisher_budget_gap': ('W',),
    'fleet_headroom_gap': ('K',),
    'freshness_budget_gap': ('W',),
    'evidence_export_budget_gap': ('N',),
    'close_handoff_budget_gap': ('W',),
    'checkpoint_reconciliation_gap': ('W',),
    'evidence_loss_gap': ('W',),
    'writer_closure_gap': ('W',),
    'retry_original_gap': ('E',),
    'close_result_identity_gap': ('O',),
    'close_result_conflict': ('O',),
    'handoff_coverage_gap': ('W',),
    'hash': ('Q',),
    'identity': ('Q',),
    'normalization': ('Q',),
    'evaluator_disagreement': ('Q',),
    'counter_mismatch': ('Q',),
    'reviewed_resolution': ('Q',),
    'revision_rollback': ('Q',),
    'missing_support': ('Q',),
    'exclusion_conflict': ('Q',),
    'positive_seed_gap': ('N',),
    'regional_seed_gap': ('V',),
    'continuous_window_gap': ('N',),
    'external_proofs_missing': ('P',),
    'pilot_prerequisite_gap': ('P',),
}


@pytest.mark.parametrize('gap,expected', CONSTRUCTOR_TABLE.items())
def test_exact_constructor_table(gap: str, expected: tuple[str, ...]) -> None:
    assert {row.name for row in e.FAILURE_RULES} == set(CONSTRUCTOR_TABLE)
    row = next(row for row in e.FAILURE_RULES if row.name == gap)
    assert row.constructor == expected
    base = obj('gate_failures', 'constructor-' + gap)
    for code in 'NDWBOEIVKAQP':
        if code == 'Q':
            reset = obj('resolution_dag', 'reset')
            reset['reason'] = gap if gap in e.RESET_REASONS else 'hash'
            subject = e.Q(reset)
        else:
            subject = obj('gate_failures', 'subject-' + code)
        if code == 'E' and row.rule == 'G3':
            subject['phase'] = 'authorize'
        # N is the common unenumerable-population fallback. I is the explicit
        # position fallback for E. Neither widens the frozen constructor tuple.
        if code in expected or code == 'N' or code == 'I' and 'E' in expected:
            e.GATE_FAILURE({**base, 'subject': subject})
        else:
            with pytest.raises(ValueError):
                e.GATE_FAILURE({**base, 'subject': subject})


@pytest.mark.parametrize('gap', [name for name, codes in CONSTRUCTOR_TABLE.items() if codes == ('E',)])
def test_receipt_page_fallback_kind_and_key(gap: str) -> None:
    base = obj('gate_failures', 'constructor-' + gap)
    kind = 'async_settle_shadow_receipts_v2'
    key = '2026-10-12/w1/00000000000000000001'
    e.GATE_FAILURE({**base, 'subject': e.O(kind, key)})
    for wrong_kind, wrong_key in [('totally_unrelated', 'any'), ('wrong', key),
                                 (kind, 'any'), (kind, '2026-10-12/w1/1'),
                                 (kind, '2026-99-99/w1/00000000000000000001')]:
        with pytest.raises(ValueError):
            e.GATE_FAILURE({**base, 'subject': e.O(wrong_kind, wrong_key)})


@pytest.mark.parametrize('label', ['inventory', 'counter_root', 'closure_root', 'receipts_root'])
def test_preimages_bind_manifest_day(label: str) -> None:
    v = vector('root_preimages', 'wrong-day-' + label)
    if label != 'inventory':
        candidate = v['object']['roots']['manifest.' + label][0]
        raw = bytes.fromhex(candidate['bodies_hex'][0])
        schema = {'counter_root': a1.COUNTER, 'closure_root': a1.CLOSURE, 'receipts_root': a1.p1.PAGE}[label]
        assert schema.parse(raw)['day'] == '2026-10-11'
    with pytest.raises(ValueError):
        run_vector(v)


@pytest.mark.parametrize('role', ['router', 'enclave'])
def test_preimages_bind_external_revision(role: str) -> None:
    with pytest.raises(ValueError):
        run_vector(vector('root_preimages', 'wrong-external-' + role + '-revision'))


@pytest.mark.parametrize('component', sorted(e.PEAKS.fields))
def test_required_G7_component_unavailable(component: str) -> None:
    with pytest.raises(ValueError):
        run_vector(vector('gate_outputs_a2', 'missing-G7-' + component))


def test_blocked_report_cannot_claim_G5_pass_with_unavailable_aggregates() -> None:
    with pytest.raises(ValueError):
        run_vector(vector('gate_outputs_a2', 'blocked-G5-unavailable'))


@pytest.mark.parametrize('label', ['regional-contradiction-count', 'regional-contradiction-null_count', 'Hterm-exceeds-H'])
def test_regional_populations_reconcile(label: str) -> None:
    with pytest.raises(ValueError):
        run_vector(vector('gate_outputs_a2', label))


def test_listed_fallback_N_forces_incomplete() -> None:
    with pytest.raises(ValueError):
        run_vector(vector('gate_outputs_a2', 'complete-listed-fallback-N'))


def test_inventory_zero_objects_native_and_transport() -> None:
    v = obj('input_inventory', 'zero-objects')
    assert v['objects'] == 0
    assert_round_trip(e.INPUT_INVENTORY_ENTRY, v)
