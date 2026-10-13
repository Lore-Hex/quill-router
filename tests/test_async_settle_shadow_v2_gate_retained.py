"""Real-function delegation, exact outputs, finite reads and reviewed deltas."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.async_settle import pilot_enablement as pilot
from scripts.async_settle import shadow_report
from scripts.async_settle.async_settle_shadow_v2_gate_tools import run_retained
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_gate_fixture_generator import pre_enable_bundle
from tests.test_async_settle_shadow_v2_gate_loading import TAG, detail, reference
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_retained import delegate, unchanged_checks
from trusted_router.detached_jws import canonical


def retained_pre(tmp_path: Path) -> tuple[PayloadGraph, dict[str, Any], dict[str, Any]]:
    data = pre_enable_bundle(tmp_path)
    files: dict[str, bytes] = {}
    deps = []
    for key in ('deployment', 'monitoring', 'read_health'):
        raw = Path(data[key]['path']).read_bytes()
        ref = reference(files, 'inputs/' + key, raw)
        deps.append(ref)
        data[key] = dict(path=ref['path'], sha256=ref['sha256'])
    ports = shadow_report.shadow_v2_ports()
    approved = next(a for a in ports.approvals if a.function is pilot.fleet_budgets_pre)
    envelope = dict(**TAG, entry_point='pilot_enablement.fleet_budgets_pre',
        payload=reference(files, 'input.json', canonical(data)),
        source=reference(files, 'source.py', approved.source),
        dependencies=detail(files, sorted(deps, key=lambda d: d['path']), 'files'))
    result = run_retained(envelope['entry_point'], data, {ref['path']: files[ref['path']] for ref in deps})
    output = reference(files, 'output.json', result.output)
    return PayloadGraph(files, ports=ports), envelope, output


def test_real_pre_function_and_exact_bytes(tmp_path: Path) -> None:
    graph, envelope, output = retained_pre(tmp_path)
    payload, result = delegate(graph, envelope, output)
    unchanged_checks(envelope['entry_point'], payload, result)
    assert result['status'] == 'PASS'
    assert graph.streams['retained_calls'][0]['calls'] == 1
    assert payload['read_health']['path'] == 'inputs/read_health'


@pytest.mark.parametrize('mutation', ['output', 'file', 'source', 'call', 'port'])
def test_retained_rejects_substitution(tmp_path: Path, mutation: str) -> None:
    graph, envelope, output = retained_pre(tmp_path)
    if mutation in ('output', 'file', 'source'):
        key = {'output': 'output.json', 'file': 'inputs/read_health', 'source': 'source.py'}[mutation]
        graph.files[key] += b'\n'
    else:
        assert graph.ports is not None
        if mutation == 'port':
            graph.ports = replace(graph.ports, retained=None)
        else:
            def suppressed(*args: Any) -> Any:
                return replace(run_retained(*args), calls={})
            graph.ports = replace(graph.ports, retained=suppressed)
    with pytest.raises(ValueError):
        delegate(graph, envelope, output)


def test_hidden_file_and_absolute_path_are_not_opened(tmp_path: Path) -> None:
    data = pre_enable_bundle(tmp_path)
    with pytest.raises(ValueError):
        run_retained('pilot_enablement.fleet_budgets_pre', data, {})


@pytest.mark.parametrize('key', ['artifacts', 'workspace_count', 'reviewed_pins', 'fleet_capacity'])
def test_untouched_pre_check_cannot_be_masked(tmp_path: Path, key: str) -> None:
    graph, envelope, output = retained_pre(tmp_path)
    payload, result = delegate(graph, envelope, output)
    result['checklist'][key] = 'BLOCKED'
    with pytest.raises(ValueError):
        unchanged_checks(envelope['entry_point'], payload, result)


def test_post_uses_deltas_without_requiring_v1_pass() -> None:
    payload = dict(rows=[dict(id='day/idle')], regional_budgets={'region': {}})
    result = dict(status='BLOCKED', checklist=dict(combined_health_age='BLOCKED',
        maximum_instance_headroom='BLOCKED', pre_enable_budget='PASS', **{'day/idle': 'BLOCKED'}))
    unchanged_checks('pilot_enablement.fleet_budgets', payload, result)
    result['checklist']['pre_enable_budget'] = 'BLOCKED'
    with pytest.raises(ValueError):
        unchanged_checks('pilot_enablement.fleet_budgets', payload, result)


@pytest.mark.parametrize('entry', [
    'pilot_enablement.fleet_budgets_pre', 'pilot_enablement.fleet_budgets',
    'pilot_enablement.shadow_serving', 'pilot_enablement.pre_flip', 'pilot_enablement.post_flip',
    'pilot_enablement.counter_coverage_report', 'pilot_enablement.roster_coverage',
    'shadow_report.collect_rollbacks',
])
def test_all_eight_entry_points_match_real_retained_functions(tmp_path: Path, monkeypatch: Any, entry: str) -> None:
    import copy
    import json

    from tests import test_async_settle_pilot_tools as fixtures

    monkeypatch.chdir(tmp_path)
    original_artifact = fixtures.artifact
    def relative_artifact(directory: Path, name: str, value: Any) -> Any:
        result = original_artifact(directory, name, value)
        return {**result, 'path': name}
    monkeypatch.setattr(fixtures, 'artifact', relative_artifact)
    data = fixtures.fleet_bundle(tmp_path)
    if entry.endswith('.fleet_budgets_pre'):
        data = fixtures.pre_enable_bundle(tmp_path, data['serving'])
    elif entry.endswith('.roster_coverage'):
        data = data['serving']
    elif entry.endswith('.pre_flip'):
        source = fixtures.artifact(tmp_path, 'export.json', [])
        selected = fixtures.select([fixtures.trial()])
        selected['exports'] = {source['path']: source['sha256']}
        data.update(drain=fixtures.artifact(tmp_path, 'drain.json', selected),
            ci={name: fixtures.artifact(tmp_path, name, {'passed': True}) for name in ('F1', 'F2b', 'F2c')},
            signer=dict(epoch=1, audience='router-settlement', kid='kid', issuer='issuer', mounted=True,
                public_key=fixtures.FIXTURE['public_key'], verification=fixtures.artifact(tmp_path, 'signer.json', {'passed': True})))
        data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_TICKET_KID='kid', TR_ASYNC_SETTLE_TICKET_ISSUER='issuer',
            TR_ASYNC_SETTLE_TICKET_AUDIENCE='router-settlement', TR_ASYNC_SETTLE_AUTHORITY_EPOCH='1',
            TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE='/mounted/key.pem')
    elif entry.endswith('.post_flip'):
        data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_ENABLED='true', TR_ASYNC_SETTLE_PROTECTION='true',
            TR_ASYNC_SETTLE_PILOT_WORKSPACES=pilot.PILOT, TR_ASYNC_SETTLE_PILOT_CAP_MICRO='5000000')
        data['serving'][1]['pins'].update(TR_ASYNC_SETTLE_NEGOTIATE='on', TR_ASYNC_SETTLE_SHADOW='off')
        data.update(rollback=fixtures.artifact(tmp_path, 'rollback.json', dict(status='PASS', states=[
            'admission_on', 'pending', 'admission_off', 'drained', 'protection_off'])),
            outbox=[dict(authorization_id='a', intent_kind='settle', workspace_id=pilot.PILOT,
                status='done', created_at=100, terminal_at=102, async_version=1)],
            handoff={'a.settle': dict(region='us-central1', handoff_us=3000)}, observed_peak_concurrency=4)
    elif entry.endswith('.collect_rollbacks'):
        data = dict(rows=data['rows'], days=data['days'], proof=data['proof'], resets=[], serving=[], manifests={})
    files = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    source_files = dict(files)
    deps = [reference(source_files, key, raw) for key, raw in sorted(files.items())]
    ports = shadow_report.shadow_v2_ports()
    approved = next(a for a in ports.approvals if a.entry_point.endswith('.' + entry))
    envelope = dict(**TAG, entry_point=entry, payload=reference(source_files, 'payload', canonical(data)),
        source=reference(source_files, 'source', approved.source), dependencies=detail(source_files, deps, 'files'))
    if entry.endswith('.collect_rollbacks'):
        resets = copy.deepcopy(data['resets'])
        shadow_report.collect_rollbacks(resets, data['serving'], data['proof'].get('resolved_mismatches', []), data['manifests'])
        direct = dict(resets=resets, report=shadow_report.report(data['rows'], data['days'], data['proof']))
    else:
        direct = getattr(pilot, entry.split('.')[1])(copy.deepcopy(data))
        if isinstance(direct, set):
            direct = sorted(direct)
    output = reference(source_files, 'output', canonical(direct))
    graph = PayloadGraph(source_files, ports=ports)
    payload, result = delegate(graph, envelope, output)
    assert canonical(result) == canonical(direct)
    unchanged_checks(entry, payload, result)
    assert json.loads(output_bytes := source_files['output']) == json.loads(canonical(result))
    assert output_bytes == canonical(result)


def test_retained_reset_report_cannot_hide_missing_raw_support() -> None:
    with pytest.raises(ValueError, match='evidence_schema_gap'):
        unchanged_checks('shadow_report.collect_rollbacks', {},
            dict(report=dict(gaps=['resolution:1:revision:missing_support'])))


def test_rehashed_forged_output_is_not_the_retained_result(tmp_path: Path) -> None:
    graph, envelope, _ = retained_pre(tmp_path)
    output = reference(graph.files, 'forged-output', canonical(dict(status='PASS', fabricated=True)))
    with pytest.raises(ValueError, match='artifact_gap'):
        delegate(graph, envelope, output)
