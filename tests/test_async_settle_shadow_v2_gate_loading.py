"""Original-byte payload closure; small witnesses independent of gate rendering."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.shadow_v2_gate_fixture_generator import (
    exhaustive,
    untraced,  # noqa: F401
)
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_loading import LIMITS, retained_json
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_schema import sha256
from trusted_router.detached_jws import canonical

TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a2')
START = 1791763200000000


def reference(files: dict[str, bytes], path: str, raw: bytes) -> dict[str, Any]:
    files[path] = raw
    return dict(path=path, bytes=len(raw), sha256=roots.file_digest(raw))


def approval(files: dict[str, bytes], subject: dict[str, Any]) -> dict[str, Any]:
    value = dict(subject_sha256=subject['sha256'], reviewer='reviewer', reviewed_at_us=START, decision='approved')
    return {**value, 'record': reference(files, 'reviews/' + subject['sha256'], a2.REVIEW_RECORD.serialize({**TAG, **value}))}


def detail(files: dict[str, bytes], values: list[dict[str, Any]], stream: str) -> dict[str, Any]:
    if not values:
        return dict(root=roots.entry_root([], stream=stream), count=0, pages=0, tail_sha256=None)
    page = {**TAG, 'page_seq': f'{1:020}', 'previous_sha256': None, 'entries': values}
    raw = a2.ATTACHMENT_PAGE.serialize(page)
    reference(files, 'details/' + stream, raw)
    result = dict(root=roots.entry_root(values, stream=stream), count=len(values), pages=1, tail_sha256=sha256(page))
    roots.validate_detail(result, [page], stream=stream)
    return result


def budget_graph() -> tuple[dict[str, bytes], dict[str, Any]]:
    files: dict[str, bytes] = {}
    limits = reference(files, 'limits.json', LIMITS.serialize(dict(regions=[], fleet=dict.fromkeys(a2.PEAKS.fields))))
    payload = reference(files, 'pre.json', b'{"cpu":20.5}\n')
    source = reference(files, 'source.py', b'# retained callable\n')
    output = reference(files, 'output.json', b'{"status":"BLOCKED"}\n')
    delegated = dict(**TAG, entry_point='pilot_enablement.fleet_budgets_pre', payload=payload,
        source=source, dependencies=detail(files, [], 'files'))
    load = dict(**TAG, interval=dict(start_us=START, end_us=START + 1000000),
        segments=detail(files, [], 'segments'), operations=detail(files, [], 'operations'),
        limits=limits, approval=approval(files, limits))
    pre = dict(**TAG, stage='pre', retained=delegated, output=output, pre_envelope_sha256=None,
        approval=approval(files, payload), v2_load=load)
    a2.BUDGET_ENVELOPE(pre)
    return files, pre


@exhaustive
@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\\ud800"}', b'[' * 1000])
def test_retained_transport_rejects_ambiguous_or_nonfinite(raw: bytes) -> None:
    with pytest.raises(ValueError):
        retained_json(raw)


def test_retained_transport_preserves_binary64_and_original_lf() -> None:
    files, pre = budget_graph()
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert graph.issues == []
    assert graph.decoded['pre.json'] == {'cpu': 20.5}
    assert graph.file(pre['retained']['payload']) == b'{"cpu":20.5}\n'
    assert pre['retained']['payload']['sha256'] != roots.file_digest(b'{"cpu":20.5}')


@exhaustive
@pytest.mark.parametrize('path', ['limits.json', 'pre.json', 'source.py', 'output.json'])
@exhaustive
@pytest.mark.parametrize('mutation', ['absent', 'wrong-byte'])
def test_every_delegated_file_requires_original_bytes(path: str, mutation: str) -> None:
    files, pre = budget_graph()
    if mutation == 'absent':
        del files[path]
    else:
        files[path] = bytes([files[path][0] ^ 1]) + files[path][1:]
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [(issue.gap, issue.path) for issue in graph.issues] == [('artifact_gap', path)]


@exhaustive
@pytest.mark.parametrize('mutation', ['extra', 'duplicate-region', 'float', 'missing', 'duplicate-key'])
def test_limits_original_schema_is_not_a_hash_only_check(mutation: str) -> None:
    files, pre = budget_graph()
    limits = dict(regions=[], fleet=dict.fromkeys(a2.PEAKS.fields))
    if mutation == 'extra':
        limits['extra'] = 0
    elif mutation == 'duplicate-region':
        limits['regions'] = [dict(region='r', approved=dict.fromkeys(a2.PEAKS.fields))] * 2
    elif mutation == 'float':
        limits['fleet']['configured_routers'] = 1.5
    elif mutation == 'missing':
        del limits['fleet']['configured_routers']
    raw = canonical(limits)
    if mutation == 'duplicate-key':
        raw = raw[:-1] + b',"regions":[]}'
    pre['v2_load']['limits'] = reference(files, 'limits.json', raw)
    pre['v2_load']['approval'] = approval(files, pre['v2_load']['limits'])
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [issue.gap for issue in graph.issues] == ['evidence_schema_gap']


@exhaustive
@pytest.mark.parametrize('mutation', ['wrong-subject', 'future', 'rejected', 'reviewer', 'missing-record'])
def test_review_binding_and_time(mutation: str) -> None:
    files, pre = budget_graph()
    approved = pre['approval']
    record_path = approved['record']['path']
    if mutation == 'wrong-subject':
        other = reference(files, 'other', b'other')
        pre['approval'] = approval(files, other)
    elif mutation == 'future':
        approved['reviewed_at_us'] += 1
        value = {**TAG, **{k: v for k, v in approved.items() if k != 'record'}}
        approved['record'] = reference(files, record_path, a2.REVIEW_RECORD.serialize(value))
    elif mutation in ('rejected', 'reviewer'):
        value = retained_json(files[record_path])
        value['decision' if mutation == 'rejected' else 'reviewer'] = 'rejected' if mutation == 'rejected' else 'imposter'
        approved['record'] = reference(files, record_path, a2.REVIEW_RECORD.serialize(value))
    else:
        del files[record_path]
    graph = PayloadGraph(files, START)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [issue.gap for issue in graph.issues] == [
        'artifact_gap' if mutation in ('wrong-subject', 'missing-record') else 'evidence_schema_gap']


def test_materialize_does_not_stop_after_first_bad_payload() -> None:
    files, pre = budget_graph()
    second = copy.deepcopy(pre)
    second.update(stage='post', pre_envelope_sha256=sha256(pre))
    second['retained']['entry_point'] = 'pilot_enablement.fleet_budgets'
    del files['output.json']
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre, 'proof.post_budget_sha256': second})
    assert [(issue.gap, issue.path) for issue in graph.issues] == [
        ('artifact_gap', 'output.json'), ('artifact_gap', 'output.json')]


@exhaustive
@pytest.mark.parametrize('mutation', ['overlap', 'wrong-day', 'outside-interval'])
def test_load_segment_identity_and_interval(mutation: str) -> None:
    files, pre = budget_graph()
    segment = dict(day='2026-10-12', boot='router', region='r', start_us=START, end_us=START + 100,
        role='router', contender=True, workspace_count=1, peaks=dict.fromkeys(a2.PEAKS.fields))
    values = [segment]
    if mutation == 'overlap':
        values.append({**segment, 'start_us': START + 1})
    elif mutation == 'wrong-day':
        segment['day'] = '2026-10-11'
    else:
        segment['end_us'] = START + 2000000
    if mutation == 'overlap':
        ref = detail(files, values[:1], 'segments')
        page = dict(**TAG, page_seq=f'{1:020}', previous_sha256=None, entries=values)
        files['details/segments'] = a2.ATTACHMENT_PAGE.serialize(page)
        ref.update(root=sha256(values), count=2, tail_sha256=sha256(page))
        pre['v2_load']['segments'] = ref
    else:
        pre['v2_load']['segments'] = detail(files, values, 'segments')
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [issue.gap for issue in graph.issues] == ['evidence_schema_gap']


def test_post_links_exact_pre_envelope() -> None:
    files, pre = budget_graph()
    post = copy.deepcopy(pre)
    post.update(stage='post', pre_envelope_sha256=roots.file_digest(b'foreign'))
    post['retained']['entry_point'] = 'pilot_enablement.fleet_budgets'
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre, 'proof.post_budget_sha256': post})
    assert [(issue.gap, issue.path) for issue in graph.issues] == [('artifact_gap', 'proof.post_budget_sha256')]


def test_shutdown_close_rows_are_parsed_not_only_hashed() -> None:
    from tests.shadow_v2_gate_fixture_generator import fixture
    files: dict[str, bytes] = {}
    close = fixture('close_result')
    closure = fixture('closure')
    # Deliberately invalid original bytes, with valid FILE_REF digests.
    close_ref = reference(files, 'close.json', canonical({**close, 'extra': True}))
    closure_ref = reference(files, 'closure.json', a1.CLOSURE.serialize(closure))
    measurement = dict(**TAG, day=close['day'], boot=close['boot'], grace_us=4000000, final_pass_us=2,
        close_us=1, result_us=1, export_wait_us=0, preparation_us=0, outcome='graceful', source=close_ref)
    measured = reference(files, 'measurement.json', a2.SHUTDOWN_MEASUREMENT.serialize(measurement))
    entry = dict(day=close['day'], boot=close['boot'], termination_applies=True, reason='shutdown',
        interval_end_us=START, grace_us=4000000, original_close=closure_ref, close_result=close_ref, measurement=measured)
    measurements = reference(files, 'measurements.json', canonical([measured]))
    envelope = dict(**TAG, writers=detail(files, [entry], 'shutdown_writers'), measurements=measurements,
        approval=approval(files, measurements), complete=True)
    graph = PayloadGraph(files)
    graph.materialize({'proof.shutdown_inventory_sha256': envelope})
    assert [issue.gap for issue in graph.issues] == ['evidence_schema_gap']


PAYLOADS = [
    ('manifest.independent_inventory_sha256', 'independent_inventory', 'inventory'),
    ('proof.shutdown_inventory_sha256', 'shutdown_inventory', 'inventory'),
    ('proof.cohort_seed_policy_sha256', 'cohort_seed_policy', 'policy'),
    ('proof.pre_budget_sha256', 'budget_envelopes', 'pre'),
    ('proof.post_budget_sha256', 'budget_envelopes', 'post'),
    ('proof.external_proofs_sha256', 'external_proofs', 'requirements'),
    ('proof.resolution_dag_sha256', 'resolution_dag', 'dag'),
]


@exhaustive
@pytest.mark.parametrize('qualified,file,variant', PAYLOADS)
@exhaustive
@pytest.mark.parametrize('mutation', ['missing-field', 'extra', 'wrong-revision', 'duplicate-key', 'oversize'])
def test_every_top_level_payload_uses_original_contract_schema(qualified: str, file: str, variant: str, mutation: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import fixture_bytes
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    value = retained_json(fixture_bytes(file, variant))
    if mutation == 'missing-field':
        del value['v']
    elif mutation == 'extra':
        value['extra'] = True
    elif mutation == 'wrong-revision':
        value['contract_revision'] = 'a1'
    raw = canonical(value)
    if mutation == 'duplicate-key':
        raw = raw[:-1] + b',"v":2}'
    elif mutation == 'oversize':
        raw += b' ' * (262145 - len(raw))
    bundle = load(canonical(dict(v=2, policy_version='shadow-v2', contract_revision='a1',
        preimages=[dict(name=qualified, raw=raw.decode())])))
    assert qualified not in bundle.a2_payloads
    own = [issue for issue in bundle.a2_streams['loading_issues'] if issue['path'] == qualified]
    assert own and own[0]['gap'] == ('artifact_gap' if mutation == 'oversize' else 'evidence_schema_gap')


@exhaustive
@pytest.mark.parametrize('conflicting', [False, True])
def test_repeated_file_exports_coalesce_only_identical_original_bytes(conflicting: bool) -> None:
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    values = [dict(name='some/file.json', raw='{}'), dict(name='some/file.json', raw='{}\n' if conflicting else '{}')]
    bundle = load(canonical(dict(v=2, policy_version='shadow-v2', contract_revision='a1', preimages=values)))
    assert len(bundle.preimages) == (2 if conflicting else 1)
    own = [issue for issue in bundle.a2_streams['loading_issues'] if issue['path'] == 'some/file.json']
    assert bool(own) == conflicting
    if conflicting:
        assert own == [dict(gap='artifact_gap', path='some/file.json', detail='conflicting file candidates')]


def test_load_checks_row_bytes_before_returning() -> None:
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    raw = canonical(dict(v=2, policy_version='shadow-v2', contract_revision='a1',
        rows=[dict(kind='async_settle_shadow_counter_v2', id='2026-10-12/router', raw='{}')]))
    bundle = load(raw)
    assert bundle.rows[0].raw == b'{}'
    assert any(issue['gap'] == 'evidence_schema_gap' for issue in bundle.a2_streams['loading_validation'])


def test_committed_invalid_page_is_schema_gap_not_missing_file() -> None:
    files, pre = budget_graph()
    segment = dict(day='2026-10-12', boot='router', region='r', start_us=START, end_us=START + 100,
        role='router', contender=True, workspace_count=1, peaks=dict.fromkeys(a2.PEAKS.fields))
    ref = detail(files, [segment], 'segments')
    page = retained_json(files['details/segments'])
    page['entries'][0]['extra'] = True
    files['details/segments'] = canonical(page)
    ref['tail_sha256'] = sha256(page)
    pre['v2_load']['segments'] = ref
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [(i.gap, i.path) for i in graph.issues] == [('evidence_schema_gap', 'details/segments')]


def test_missing_page_is_artifact_gap() -> None:
    files, pre = budget_graph()
    segment = dict(day='2026-10-12', boot='router', region='r', start_us=START, end_us=START + 100,
        role='router', contender=True, workspace_count=1, peaks=dict.fromkeys(a2.PEAKS.fields))
    pre['v2_load']['segments'] = detail(files, [segment], 'segments')
    del files['details/segments']
    graph = PayloadGraph(files)
    graph.materialize({'proof.pre_budget_sha256': pre})
    assert [i.gap for i in graph.issues] == ['artifact_gap']


@exhaustive
@pytest.mark.parametrize('mutation', ['absent', 'wrong-byte', 'wrong-time', 'undefined-parser', 'valid'])
def test_ledger_artifacts_require_defined_original_preimages(mutation: str) -> None:
    from tests.shadow_v2_gate_fixture_generator import fixture
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context
    from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Preimage
    from trusted_router.async_settle_shadow_v2_gate_preimages import ledger_preimages
    ledger = fixture('prerequisite_ledger')
    gate = fixture('gate_outputs')
    gate['completed_at_us'] = ledger['transport']['completed_at_us']
    raw = a1.GATE_OUTPUT.serialize(gate)
    artifact_files = []
    for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
        result = dict(**TAG, criterion=field, router_revision=ledger['revision'], enclave_revision=ledger['revision'],
            completed_at_us=gate['completed_at_us'], producer='ci', checks=[],
            sources=dict(root=roots.entry_root([], stream='files'), count=0, pages=0, tail_sha256=None))
        raw = a2.PROOF_RESULT.serialize(result)
        artifact_files.append(Preimage('prerequisites/' + field, raw))
        ledger[field] = dict(sha256=roots.file_digest(raw), completed_at_us=gate['completed_at_us'])
    opaque = b'{"completed_at_us":0}'
    if mutation == 'undefined-parser':
        ledger['transport']['sha256'] = roots.file_digest(opaque)
    if mutation == 'wrong-time':
        ledger['transport']['completed_at_us'] += 1
    files = [Preimage('config', b'config'), Preimage('policy', b'policy'), Preimage('fixture', b'fixture')]
    for field, source in zip(('config_sha256', 'policy_sha256', 'fixture_sha256'), files, strict=True):
        ledger[field] = roots.file_digest(source.raw)
    ledger['previous_ledger_sha256'] = None
    if mutation != 'absent':
        files.extend(Preimage(f.name, f.raw + b' ' if mutation == 'wrong-byte' else f.raw) for f in artifact_files)
    files.append(Preimage('prerequisites/opaque.json', opaque))
    ctx = Context(Bundle(preimages=tuple(files)), None, 'pre-flip', ledgers=[ledger])
    ledger_preimages(ctx)
    if mutation == 'valid':
        assert ctx.issues == []
    elif mutation == 'undefined-parser':
        assert [i.gap for i in ctx.issues] == ['evidence_schema_gap']
        assert ctx.issues[0].detail == 'ledger_artifact_parser_unbound:transport'
    else:
        assert ctx.issues and all(i.gap == 'artifact_gap' for i in ctx.issues)


def test_gate_family_import_and_construction_stays_offline() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).parents[1]
    program = '''
import builtins
import importlib
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("trusted_router.storage", "trusted_router.async_settle_shadow_binding", "google.cloud", "httpx", "requests")):
        raise AssertionError("impure import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
for path in Path("src/trusted_router").glob("async_settle_shadow_v2_gate_*.py"):
    importlib.import_module("trusted_router." + path.stem)
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
assert load(b"{}").undefined_preimages
assert not PayloadGraph({}).files
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=root,  # noqa: S603 - literal offline import guard
        env={**os.environ, 'PYTHONPATH': str(root / 'src')}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
