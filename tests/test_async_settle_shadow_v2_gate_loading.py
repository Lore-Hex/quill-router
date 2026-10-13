"""Original-byte payload closure; small witnesses independent of gate rendering."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
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


@pytest.fixture(scope='module')
def round9_originals(tmp_path_factory: Any) -> Any:
    from tests.shadow_v2_gate_a2_fixture import real_fixture, transport
    original, ports, expected = real_fixture(tmp_path_factory.mktemp('round9-originals'))
    return original, transport(original), ports, expected


def round9_render(raw: bytes, ports: Any) -> Any:
    import json

    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    result = evaluate(load(raw, ports), ports, gate='shadow-serving')
    output, attachments = render(result)
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert json.loads(attachments[0]) == result.gate_report
    return result


@pytest.mark.parametrize('mode', ['content', 'source', 'reference', 'key', 'extra'])
def test_shutdown_alias_original_bytes(round9_originals: Any, mode: str) -> None:
    import json
    _, raw, ports, _ = round9_originals
    value = json.loads(raw)
    kind = 'shutdown_entry' if mode == 'reference' else 'shutdown_measurement'
    row = next(r for r in value['attachments'] if r['kind'] == kind)
    body = json.loads(row['raw'])
    if mode == 'content':
        body['result_us'] = 1000000000
    elif mode == 'source':
        body['source']['sha256'] = '0' * 64
    elif mode == 'reference':
        body['measurement']['sha256'] = '0' * 64
    elif mode == 'key':
        row['id'] += '-wrong'
    else:
        value['attachments'].append({**row, 'id': row['id'] + '-extra'})
    row['raw'] = canonical(body).decode()
    result = round9_render(canonical(value), ports)
    assert 'close_handoff_budget_gap' in result.gaps
    assert any('shutdown alias' in f.detail for f in result.failures)
    assert result.gate_report['timing']['handoff_us']['count'] == 1
    assert result.gate_report['timing']['handoff_us']['max'] == 1000000


@pytest.mark.parametrize('name', ['transport', 'fleet', 'pre'])
@pytest.mark.parametrize('field', ['coverage_sha256', 'proof_manifest_sha256', 'prerequisite_ledger_root'])
def test_unresolved_prior_graph_is_never_authority(round9_originals: Any, name: str, field: str) -> None:
    import json
    _, raw, ports, _ = round9_originals
    value = json.loads(raw)
    prior = json.loads(value[name])
    prior[field] = '0' * 64
    value[name] = canonical(prior).decode()
    result = round9_render(canonical(value), ports)
    assert 'artifact_gap' in result.gaps and 'positive_seed_gap' in result.gaps
    assert result.report.clock_start_us is None
    assert any('prior_gate_original_graph:' in f.detail for f in result.failures)


@pytest.mark.parametrize('scope', ['fleet', 'region'])
@pytest.mark.parametrize('field', ['startup_operations_1s', 'close_operations_1s', 'export_operations_1s',
                                  'export_operations_per_second', 'export_bytes_per_second'])
def test_approved_peak_limits_original_bytes(round9_originals: Any, scope: str, field: str) -> None:
    import json
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import commit_originals, transport
    from trusted_router.async_settle_shadow_v2_gate_failures import detail as pages_for
    original, _, ports, expected = round9_originals
    files = {p.name: p.raw for p in original.preimages}
    segments = copy.deepcopy(expected['segments'])
    for segment in segments:
        if segment['role'] == 'router':
            segment['peaks'][field] = 1000000000 if field.endswith('_1s') else dict(numerator=1000000000, denominator=1)
    ref, pages = pages_for(segments, 'segments')
    for path, raw in list(files.items()):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and value.get('entries') == expected['segments']:
            files[path] = canonical(pages[0])
    post = json.loads(files['proof.post_budget_sha256'])
    post['v2_load']['segments'] = ref
    files['proof.post_budget_sha256'] = a2.BUDGET_ENVELOPE.serialize(post)
    pins = {path: roots.file_digest(files[path]) for path in ports.release.artifacts}
    plan = canonical({**json.loads(ports.release.raw), 'artifacts': pins})
    files[ports.release.path] = plan
    ports = replace(ports, release=replace(ports.release, raw=plan, artifacts=pins))
    result = round9_render(transport(commit_originals(original, files)), ports)
    subject = a2.K('post', None if scope == 'fleet' else 'us-central1')
    assert any(f.subject == subject and 'approved peak exceeded: ' + field in f.detail for f in result.failures)
    assert result.gate_report['rules']['G7'] in ('fail', 'unevaluable')


@pytest.mark.parametrize('field', list(a2.PEAKS.fields))
def test_every_peak_limit_component_is_consumed(round9_originals: Any, field: str) -> None:
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_gate_bundle import Context
    from trusted_router.async_settle_shadow_v2_gate_load import compare_peak_limits, sweep
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    _, raw, ports, _ = round9_originals
    bundle = load(raw, ports)
    limits = copy.deepcopy(bundle.budget.peak_limits)
    limits['post']['fleet'][field] = None
    ctx = Context(replace(bundle, budget=replace(bundle.budget, peak_limits=limits)), ports, 'shadow-serving')
    compare_peak_limits(ctx, sweep(bundle.inventory.boots, bundle.budget.rates, bundle.cohort.workspace_count, bundle.completed_at_us))
    assert any(i.unavailable and i.detail == 'post unavailable peak or limit: ' + field for i in ctx.issues)


@pytest.mark.parametrize('mode', ['future', 'early', 'days', 'gap'])
def test_requested_window_cannot_certify_time(round9_originals: Any, mode: str) -> None:
    import json
    original, raw, ports, _ = round9_originals
    value = json.loads(raw)
    start = min(b.start_us for b in original.inventory.boots)
    value['window'] = dict(start_us=start, end_us=original.completed_at_us)
    if mode == 'future':
        value['window']['end_us'] += 604800000000
    elif mode == 'early':
        value['window']['start_us'] -= 1
    elif mode == 'days':
        value['window']['start_us'] -= 86400000000
    else:
        row = next(r for r in value['rows'] if r['kind'] == 'async_settle_shadow_control_v2')
        body = json.loads(row['raw'])
        body['complete'] = False
        body['gaps'] = [dict(interval=dict(start_us=start + 10000000, end_us=start + 11000000), reason='writer_closure_gap')]
        row['raw'] = canonical(body).decode()
    result = round9_render(canonical(value), ports)
    assert result.gate_report['elapsed_seconds'] == result.report.elapsed_seconds
    assert result.gate_report['window']['end_us'] <= original.completed_at_us
    if mode != 'gap':
        assert 'continuous_window_gap' in result.gaps


@pytest.mark.parametrize('mode', ['missing-manifest', 'conflicting-manifest', 'days-string', 'days-date',
    'window-list', 'window-equal', 'completion-null', 'completion-overflow', 'completion-bool'])
def test_damaged_scope_renders_blocked(round9_originals: Any, mode: str) -> None:
    import json
    _, raw, ports, _ = round9_originals
    value = json.loads(raw)
    if mode.endswith('manifest'):
        row = next(r for r in value['rows'] if r['kind'] == 'async_settle_shadow_control_v2')
        if mode == 'missing-manifest':
            value['rows'].remove(row)
        else:
            body = json.loads(row['raw'])
            body['complete'] = False
            value['rows'].append({**row, 'raw': canonical(body).decode()})
    elif mode.startswith('days'):
        value['days'] = 'bad' if mode == 'days-string' else ['not-a-date']
    elif mode.startswith('window'):
        value['window'] = [] if mode == 'window-list' else dict(start_us=1, end_us=1)
    else:
        value['completed_at_us'] = {'completion-null': None, 'completion-overflow': 2**63,
                                    'completion-bool': True}[mode]
    result = round9_render(canonical(value), ports)
    assert 'artifact_gap' in result.gaps
    assert result.gate_report['complete'] is False
    if mode.endswith('manifest'):
        assert any(f.subject == a2.O('async_settle_shadow_control_v2', '2026-10-06/manifest-v2')
                   for f in result.failures if f.gap == 'artifact_gap')


def test_every_gate_input_field_has_provenance_row() -> None:
    import dataclasses
    import re
    from pathlib import Path

    from trusted_router import async_settle_shadow_v2_gate_inputs as inputs
    expected = {name + '.' + field.name for name, cls in vars(inputs).items()
        if isinstance(cls, type) and dataclasses.is_dataclass(cls) and cls.__module__ == inputs.__name__
        for field in dataclasses.fields(cls)}
    table = (Path(__file__).parents[1] / 'docs/design/async-settle-shadow-v2-p5-inputs.md').read_text()
    rows = re.findall(r'^\| `([A-Za-z]+\.[a-z_0-9]+)` \| ([^|]+) \| ([^|]+) \|$', table, re.M)
    assert len(rows) == len(expected)
    assert {row[0] for row in rows} == expected
    assert all(source.strip() and validator.strip() for _, source, validator in rows)


def test_shutdown_population_requires_original_proof_commitment(round9_originals: Any) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_loader import load
    _, raw, ports, _ = round9_originals
    value = json.loads(raw)
    proof = json.loads(value['proof'])
    proof['shutdown_inventory_sha256'] = '0' * 64
    value['proof'] = canonical(proof).decode()
    damaged = canonical(value)
    assert load(damaged, ports).shutdown is None
    result = round9_render(damaged, ports)
    assert result.gate_report['timing']['handoff_us']['count'] is None


@pytest.mark.parametrize('name', ['transport', 'pre', 'fleet'])
@pytest.mark.parametrize('damage', ['wrong-stage', 'wrong-revision', 'wrong-cohort', 'wrong-policy',
    'missing-ledger', 'detail-absent', 'detail-altered', 'detail-reordered', 'preimage-root', 'completion-envelope'])
def test_prior_original_graph_damage(round9_originals: Any, name: str, damage: str) -> None:
    import json
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import alter_prior_inventory, transport
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    original, _, ports, _ = round9_originals
    envelope = json.loads(getattr(original, name))
    files = {p.name: p.raw for p in original.preimages}
    by_hash = {file_digest(raw): raw for raw in files.values()}
    report = json.loads(by_hash[envelope['coverage_sha256']])
    if damage in ('wrong-stage', 'completion-envelope'):
        envelope.update(gate='post-flip') if damage == 'wrong-stage' else envelope.update(completed_at_us=0)
        candidate = replace(original, **{name: canonical(envelope)})
    elif damage in ('wrong-revision', 'wrong-cohort', 'wrong-policy'):
        def change(raw: bytes) -> bytes:
            proof = json.loads(raw)
            if damage == 'wrong-revision':
                proof['source_shas']['router'] = 'b' * 40
            elif damage == 'wrong-cohort':
                proof['cohort_seed_policy_sha256'] = '0' * 64
            else:
                proof['policy_version'] = 'shadow-v1'
                return canonical(proof)
            return a1.PROOF_MANIFEST.serialize(proof)
        if damage == 'wrong-policy':
            # An undecodable proof remains original input; inventory commits it.
            digest = report['proof_manifest_sha256']
            files = {path: (change(raw) if file_digest(raw) == digest else raw) for path, raw in files.items()}
            candidate = replace(original, preimages=tuple(Preimage(k, v) for k, v in files.items()))
        else:
            candidate = alter_prior_inventory(original, name, lambda path: path == 'proof_manifest', change)
    elif damage == 'preimage-root':
        def change(raw: bytes) -> bytes:
            manifest = json.loads(raw)
            manifest['counter_root'] = '0' * 64
            return a1.MANIFEST.serialize(manifest)
        candidate = alter_prior_inventory(original, name,
            lambda path: path.startswith('rows/' + a1.p1.KINDS[5] + '/'), change)
    else:
        if damage == 'missing-ledger':
            from tests.shadow_v2_gate_a2_fixture import prior_inventory_entries
            leaves = prior_inventory_entries(report, by_hash)
            refs = [ref for leaf in leaves for ref in a2.INPUT_LEAF.parse(by_hash[leaf['bundle']['sha256']])['keys']]
            digest = next(ref['sha256'] for ref in refs if ref['path'].startswith('ledgers/'))
        else:
            digest = report['writers']['tail_sha256']
        raw = by_hash[digest]
        if damage == 'detail-altered':
            page = json.loads(raw)
            page['entries'][0]['revision'] = 'b' * 40
            replacement = canonical(page)
        elif damage == 'detail-reordered':
            page = json.loads(raw)
            page['entries'].reverse()
            replacement = canonical(page)
            assert replacement != raw
        else:
            replacement = None
        files = {path: (replacement if file_digest(value) == digest else value)
                 for path, value in files.items() if replacement is not None or file_digest(value) != digest}
        candidate = replace(original, preimages=tuple(Preimage(k, v) for k, v in files.items()))
    result = round9_render(transport(candidate), ports)
    assert 'artifact_gap' in result.gaps and 'positive_seed_gap' in result.gaps
    assert any(f.gap == 'artifact_gap' and f.subject == a2.O('prior_gate', name) for f in result.failures)


def test_prior_fabricated_pass_over_blocked_report(round9_originals: Any) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import original_prior_chain, transport
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, _, ports, _ = round9_originals
    prior, prior_ports, _ = original_prior_chain()
    loaded = load(transport(prior), prior_ports)
    blocked = evaluate(replace(loaded, budget=None), prior_ports, gate='fleet-budgets')
    assert blocked.verdict == 'BLOCKED'
    output, artifacts = render(blocked)
    envelope = a1.GATE_OUTPUT.parse(output)
    envelope.update(verdict='PASS', gaps=[])
    files = {p.name: p.raw for p in original.preimages}
    for raw in artifacts:
        files['prior-artifacts/' + file_digest(raw)] = raw
    candidate = replace(original, fleet=canonical(envelope), preimages=tuple(Preimage(k, v) for k, v in files.items()))
    result = round9_render(transport(candidate), ports)
    assert 'positive_seed_gap' in result.gaps


def test_prior_fleet_requires_own_predecessor(round9_originals: Any) -> None:
    from tests.shadow_v2_gate_a2_fixture import alter_prior_inventory, transport
    original, _, ports, _ = round9_originals
    candidate = alter_prior_inventory(original, 'fleet', lambda path: path == 'prior-gates/pre', lambda raw: b'{}')
    result = round9_render(transport(candidate), ports)
    assert 'boot_prerequisite_gap' in result.gaps


def test_prior_budget_linkage(round9_originals: Any) -> None:
    import json

    from tests.shadow_v2_gate_a2_fixture import alter_prior_inventory, transport
    original, _, ports, _ = round9_originals
    def change(raw: bytes) -> bytes:
        value = json.loads(raw)
        value['pre_budget_sha256'] = '0' * 64
        return canonical(value)
    candidate = alter_prior_inventory(original, 'fleet', lambda path: path == 'proof_manifest', change)
    result = round9_render(transport(candidate), ports)
    assert 'boot_prerequisite_gap' in result.gaps


@pytest.mark.parametrize('name,replacement', [('transport', 'pre'), ('pre', 'transport'), ('fleet', 'pre')])
def test_prior_wrong_stage_valid_graph(round9_originals: Any, name: str, replacement: str) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import transport
    original, _, ports, _ = round9_originals
    candidate = replace(original, **{name: getattr(original, replacement)})
    result = round9_render(transport(candidate), ports)
    assert 'positive_seed_gap' in result.gaps


def test_prior_budget_linkage_to_current_approval(round9_originals: Any) -> None:
    import json
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import transport
    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    original, _, ports, _ = round9_originals
    proof = json.loads(original.proof)
    proof['pre_budget_sha256'] = '0' * 64
    candidate = replace(original, proof=canonical(proof))
    ctx = decode_bundle(load(transport(candidate), ports), ports, 'shadow-serving')
    # Even though all prior artifacts are authentic, they belong to a different
    # approval and must never enter the current prerequisite/clock authority set.
    assert 'fleet' not in ctx.gates
    result = round9_render(transport(candidate), ports)
    assert 'positive_seed_gap' in result.gaps


@pytest.mark.parametrize('scope', ['exact', 'seed'])
def test_round11_explicit_observation_scope(round9_originals: Any, scope: str) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    _, raw, ports, _ = round9_originals
    baseline = evaluate(load(raw, ports), ports, gate='shadow-serving')
    assert baseline.verdict == 'PASS'
    value = json.loads(raw)
    value['window'] = dict(baseline.gate_report['window'])
    if scope == 'seed':
        value['window']['start_us'] = baseline.report.first_seed_us
    explicit = evaluate(load(canonical(value), ports), ports, gate='shadow-serving')
    assert explicit.verdict == 'PASS'
    assert explicit.report.elapsed_seconds == baseline.report.elapsed_seconds == 18
    assert render(explicit) == render(baseline)


@pytest.mark.parametrize('change', ['earlier', 'later', 'tally', 'metric', 'rule'])
def test_round11_coherent_prior_replay(round9_originals: Any, change: str) -> None:
    import json

    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_prior import _VERIFIED
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    _, raw, ports, _ = round9_originals
    _VERIFIED.clear()
    for _ in range(2):
        assert evaluate(load(raw, ports), ports, gate='shadow-serving').verdict == 'PASS'
    value = json.loads(raw)
    output = json.loads(value['transport'])
    report = json.loads(next(f['raw'] for f in value['preimages']
        if roots.file_digest(f['raw'].encode()) == output['coverage_sha256']))
    if change in ('earlier', 'later'):
        report['completed_at_us'] += -2000000 if change == 'earlier' else 22000000
        output['completed_at_us'] = report['completed_at_us']
    elif change == 'metric':
        report['fleet_peaks']['cpu_percent']['numerator'] += 1
    elif change == 'rule':
        report['rules']['G11'] = 'pass'
    else:
        # A self-consistent zero tally with changed completeness is still a lie
        # about what rendering these original inputs produced.
        report['failure_counts']['artifact_gap']['complete'] = False
    report_raw = canonical(report)
    output['coverage_sha256'] = roots.file_digest(report_raw)
    value['preimages'].append(dict(name='prior-artifacts/' + output['coverage_sha256'], raw=report_raw.decode()))
    value['transport'] = canonical(output).decode()
    for _ in range(2):
        result = round9_render(canonical(value), ports)
        assert {'artifact_gap', 'boot_prerequisite_gap'} <= set(result.gaps)
        assert any('prior_gate_original_graph: transport' in f.detail for f in result.failures)


def test_round11_cache_restores_every_function_identity() -> None:
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    from trusted_router.async_settle_shadow_v2_schema import Schema
    targets = [(contract, 'validate_preimages'), (contract, 'validate_gate_output'),
        (contract, '_object_bytes'), (Schema, '_validate'), (Schema, 'parse'),
        (payloads, 'retained_json'), (payloads.PayloadGraph, 'detail')]
    originals = [getattr(owner, key) for owner, key in targets]
    fixture = payloadgraph_shared_lookup_cache.__wrapped__()
    next(fixture)
    assert all(getattr(owner, key) is not original for (owner, key), original in zip(targets, originals, strict=True))
    with pytest.raises(StopIteration):
        next(fixture)
    assert all(getattr(owner, key) is original for (owner, key), original in zip(targets, originals, strict=True))


def test_round11_module_teardown_before_contract_subprocess() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:xdist', '-p', 'no:cacheprovider',
        'tests/test_async_settle_shadow_v2_gate_complete.py::test_original_bytes_stage_pass[transport]',
        'tests/test_async_settle_shadow_v2_evidence.py::test_transport_enforcing_routes[trusted_router.async_settle_shadow_v2_contract.COMPARED_FULL:raw-key]',
        'tests/test_async_settle_shadow_v2_evidence.py::test_transport_enforcing_routes[trusted_router.async_settle_shadow_v2_contract.COMPARED_FULL:raw-value]'],
        cwd=root, env={**os.environ, 'PYTEST_ADDOPTS': ''}, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr


def test_round11_prior_evaluated_once_per_content(round9_originals: Any, monkeypatch: Any) -> None:
    from collections import Counter

    from trusted_router import async_settle_shadow_v2_gates as gates
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_prior import _VERIFIED
    _, raw, ports, _ = round9_originals
    original = gates.evaluate
    calls: Any = Counter()
    def counted(bundle: Any, ports: Any = None, *, gate: str = 'pre-flip') -> Any:
        calls[gate] += 1
        return original(bundle, ports, gate=gate)
    _VERIFIED.clear()
    monkeypatch.setattr(gates, 'evaluate', counted)
    for _ in range(2):
        assert original(load(raw, ports), ports, gate='shadow-serving').verdict == 'PASS'
    assert calls == {'transport': 1, 'fleet-budgets-pre': 1, 'fleet-budgets': 1}


@pytest.mark.parametrize('field', ['policy_version', 'ledgers'])
def test_round11_container_damage_retains_commitments(round9_originals: Any, field: str) -> None:
    import json
    _, raw, ports, _ = round9_originals
    data = json.loads(raw)
    data[field] = 'wrong-policy' if field == 'policy_version' else ['{}'] * 4097
    result = round9_render(canonical(data), ports)
    assert result.proof_manifest_sha256 == roots.file_digest(data['proof'].encode())
    if field == 'policy_version':
        assert 'policy_version_gap' in result.gaps


def test_round11_prior_cache_includes_approval_pins(round9_originals: Any) -> None:
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_prior import _VERIFIED
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    _, raw, ports, _ = round9_originals
    _VERIFIED.clear()
    assert evaluate(load(raw, ports), ports, gate='shadow-serving').verdict == 'PASS'
    damaged = replace(ports, approvals=tuple(replace(a, source_pin='0' * 64)
        if a.entry_point.endswith('.verify_certificate') else a for a in ports.approvals))
    result = round9_render(raw, damaged)
    assert any('prior_gate_original_graph: transport' in f.detail for f in result.failures)


def test_round11_invalid_completion_uses_original_scope_bound(round9_originals: Any) -> None:
    import json
    original, raw, ports, _ = round9_originals
    data = json.loads(raw)
    data['completed_at_us'] = float('inf')
    result = round9_render(json.dumps(data).encode(), ports)
    assert result.completed_at_us == original.completed_at_us
    assert 'evidence_schema_gap' in result.gaps
