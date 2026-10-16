"""Policy dispatch, fail-closed loader/render seams and v1 separation."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.async_settle import pilot_enablement, shadow_report
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_gate_fixture_generator import pre_enable_bundle
from trusted_router import async_settle_shadow_v2_gate_loader as loader
from trusted_router import async_settle_shadow_v2_gates as gates
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle
from trusted_router.detached_jws import canonical

pytestmark = pytest.mark.timeout(method='signal')

def load(*args: Any, **kwargs: Any) -> Any:
    return loader.load(*args, **kwargs)

def evaluate(*args: Any, **kwargs: Any) -> Any:
    return gates.evaluate(*args, **kwargs)

def test_no_policy_does_not_dispatch_v2(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('default entered v2')
    monkeypatch.setattr(shadow_report, 'shadow_v2_main', forbidden)
    data = pre_enable_bundle(tmp_path)
    path = tmp_path / 'bundle.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, 'argv', ['pilot', 'fleet-budgets-pre', '--bundle', str(path)])
    with pytest.raises(SystemExit) as exc:
        pilot_enablement.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PASS'
    rows, days, proof = [], ['2026-10-06'], {}
    (tmp_path / 'proof.json').write_text(json.dumps(proof))
    (tmp_path / 'rows.json').write_text(json.dumps(rows))
    argv = ['report', '--proof-manifest', str(tmp_path / 'proof.json'), '--evidence-json', str(tmp_path / 'rows.json')]
    for day in days:
        argv.extend(['--day', day])
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        shadow_report.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'BLOCKED'


@pytest.mark.parametrize('tool', ['report', 'pilot'])
def test_explicit_v2_rejects_v1_bundle(tmp_path: Path, monkeypatch: Any, capsys: Any, tool: str) -> None:
    path = tmp_path / 'v1.json'
    path.write_text(json.dumps(pre_enable_bundle(tmp_path)))
    argv = [tool, '--policy', 'shadow-v2', '--bundle', str(path)]
    if tool == 'pilot':
        argv.insert(1, 'fleet-budgets-pre')
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        (shadow_report.main if tool == 'report' else pilot_enablement.main)()
    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'BLOCKED' and output['policy_version'] == 'shadow-v2'


@pytest.mark.parametrize('extras', [{'contract_revision': 'a1'}, {'policy_version': 'shadow-v2'},
    {'unrecognized': {'revision': 9, 'labels': [True, None, 'ignored']}}, {'new_flag': False}])
def test_no_policy_preserves_ignored_metadata(tmp_path: Path, monkeypatch: Any, capsys: Any, extras: Any) -> None:
    data = pre_enable_bundle(tmp_path)
    data.update(extras)
    path = tmp_path / 'v2.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, 'argv', ['pilot', 'fleet-budgets-pre', '--bundle', str(path)])
    with pytest.raises(SystemExit) as exc:
        pilot_enablement.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PASS'
    with pytest.raises(ValueError):
        shadow_report.report([dict(kind='async_settle_shadow_counter_v2', id='x', body={})], ['2026-10-06'], {})


@pytest.mark.parametrize('raw', [b'null', b'[]', b'{}', b'{"v":2,"v":2}', b'{"policy_version":"shadow-v1"}'])
def test_loader_is_total_and_undefined_preimages_block(raw: bytes) -> None:
    bundle = load(raw)
    assert isinstance(bundle, Bundle)
    assert bundle.undefined_preimages
    assert evaluate(bundle).verdict == 'BLOCKED'


def test_loader_retains_original_bytes() -> None:
    raw = '{"v": 2,"policy_version":"shadow-v2"}'
    bundle = load(canonical(dict(policy_version='shadow-v2', contract_revision='a1', rows=[dict(kind='unknown', id='key', raw=raw)])))
    assert bundle.rows[0].raw == raw.encode()
    assert 'evidence_schema_gap' in evaluate(bundle).gaps


def test_loader_v1_rejection_marker() -> None:
    value = load(b'{"policy_version":"shadow-v1","contract_revision":"a1"}')
    assert 'invalid_or_cross_version_bundle' in value.undefined_preimages


@pytest.fixture(scope='module')
def cli_original(tmp_path_factory: Any) -> Any:
    from tests.shadow_v2_gate_a2_fixture import real_fixture
    from tests.shadow_v2_gate_totality import BoundaryCache
    original = real_fixture(tmp_path_factory.mktemp('cli-original'))
    with BoundaryCache().installed():
        yield original


@pytest.mark.parametrize('tool', ['report', 'pilot'])
@pytest.mark.parametrize('gate', ['transport', 'fleet-budgets-pre'])
def test_v2_tools_render_the_shared_original_byte_result(cli_original: Any, tmp_path: Path,
        monkeypatch: Any, capsys: Any, tool: str, gate: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_result import render
    original, ports, _ = cli_original
    expected, attachments = render(evaluate(load(transport(original), ports), ports, gate=gate))
    assert json.loads(expected)['verdict'] == 'PASS'
    path = tmp_path / 'bundle.json'
    path.write_bytes(transport(original))
    plan = tmp_path / 'release.json'
    plan.write_bytes(ports.release.raw)
    argv = [tool]
    if tool == 'pilot':
        argv.append(gate)
    argv += ['--policy', 'shadow-v2', '--bundle', str(path), '--release-plan', str(plan),
             '--release-plan-sha256', file_digest(plan.read_bytes())]
    if tool == 'report':
        argv += ['--gate', gate]
    for name in ('certificate', 'binding'):
        keyfile = tmp_path / (name + '.json')
        keyfile.write_bytes(next(a.keyset for a in ports.approvals if a.entry_point.endswith('.verify_' + name)))
        argv += ['--' + name + '-keys', str(keyfile)]
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        (shadow_report.main if tool == 'report' else pilot_enablement.main)()
    assert exc.value.code == 0
    assert capsys.readouterr().out.encode() == expected + b'\n'
    assert (tmp_path / 'gate-output/output.json').read_bytes() == expected
    assert (tmp_path / 'gate-output/report.json').read_bytes() == attachments[0]


@pytest.mark.parametrize('proof', [None, '', '{}', '{', 'null', '{"ledger_root":null}'])
@pytest.mark.parametrize('tool', ['report', 'pilot'])
def test_unavailable_commitment_value_and_one_line(cli_original: Any, tmp_path: Path,
        monkeypatch: Any, capsys: Any, proof: Any, tool: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    original, ports, _ = cli_original
    data = json.loads(transport(original))
    data['proof'] = proof
    raw = canonical(data)
    value = render(evaluate(load(raw, ports), ports, gate='shadow-serving'))
    assert isinstance(value, UnavailableGateOutput)
    assert value.condition == 'proof_manifest_absent_or_undecodable'
    path = tmp_path / 'unavailable.json'
    path.write_bytes(raw)
    monkeypatch.setattr(shadow_report, 'shadow_v2_ports', lambda *args, **kwargs: ports)
    argv = [tool] + (['shadow-serving'] if tool == 'pilot' else [])
    argv += ['--policy', 'shadow-v2', '--bundle', str(path)]
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        (shadow_report.main if tool == 'report' else pilot_enablement.main)()
    assert exc.value.code != 0
    stdout = capsys.readouterr().out
    assert stdout == json.dumps(value.diagnostic(), sort_keys=True, separators=(',', ':')) + '\n'
    assert not (tmp_path / 'gate-output').exists()


@pytest.mark.parametrize('field', ['rows', 'attachments', 'preimages', 'ledgers', 'transport', 'pre', 'fleet'])
def test_generated_damaged_inputs_always_render(cli_original: Any, field: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    original, ports, _ = cli_original
    data = json.loads(transport(original))
    # Retain the report's proof commitment while independently deleting each
    # evidence population, including all scope sources except the proof roster.
    data[field] = [] if isinstance(data[field], list) else None
    rendered = render(evaluate(load(canonical(data), ports), ports, gate='shadow-serving'))
    assert not isinstance(rendered, UnavailableGateOutput)
    output, attachments = rendered
    assert json.loads(output)['verdict'] == 'BLOCKED'
    assert len(attachments) > 0


@pytest.mark.parametrize('tool', ['report', 'pilot'])
def test_unavailable_scope_artifacts_value_and_one_line(cli_original: Any, tmp_path: Path,
        monkeypatch: Any, capsys: Any, tool: str) -> None:
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    original, ports, _ = cli_original
    proof = a1.PROOF_MANIFEST.parse(original.proof)
    proof.update(rosters_by_day={}, instance_regions_by_day={})
    raw = canonical(dict(policy_version='shadow-v2', contract_revision='a1', proof=a1.PROOF_MANIFEST.serialize(proof).decode()))
    value = render(evaluate(load(raw, ports), ports))
    assert isinstance(value, UnavailableGateOutput)
    assert value.condition == 'observation_scope_artifacts_absent_or_undecodable'
    path = tmp_path / 'unavailable.json'
    path.write_bytes(raw)
    monkeypatch.setattr(shadow_report, 'shadow_v2_ports', lambda *args, **kwargs: ports)
    argv = [tool] + (['pre-flip'] if tool == 'pilot' else [])
    argv += ['--policy', 'shadow-v2', '--bundle', str(path)]
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        (shadow_report.main if tool == 'report' else pilot_enablement.main)()
    assert exc.value.code != 0
    assert capsys.readouterr().out == json.dumps(value.diagnostic(), sort_keys=True, separators=(',', ':')) + '\n'
    assert not (tmp_path / 'gate-output').exists()


@pytest.mark.parametrize('field', ['rows', 'attachments', 'preimages', 'ledgers'])
@pytest.mark.parametrize('value', [None, 3, [None], [{'raw': 'x'}]])
def test_damaged_sibling_never_erases_original_proof(cli_original: Any, field: str, value: Any) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    original, ports, _ = cli_original
    data = json.loads(transport(original))
    data[field] = value
    loaded = load(canonical(data), ports)
    assert loaded.proof == original.proof
    rendered = render(evaluate(loaded, ports, gate='shadow-serving'))
    assert not isinstance(rendered, UnavailableGateOutput)
    assert json.loads(rendered[0])['verdict'] == 'BLOCKED'


@pytest.mark.parametrize('tool', ['report', 'pilot'])
@pytest.mark.parametrize('damage', ['bundle-missing', 'release-missing', 'release-unapproved', 'keys-missing', 'keys-invalid'])
def test_tool_input_damage_has_one_result(cli_original: Any, tmp_path: Path, monkeypatch: Any,
        capsys: Any, tool: str, damage: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    original, _, _ = cli_original
    path = tmp_path / 'bundle.json'
    if damage != 'bundle-missing':
        path.write_bytes(transport(original))
    argv = [tool] + (['transport'] if tool == 'pilot' else [])
    argv += ['--policy', 'shadow-v2', '--bundle', str(path)]
    if tool == 'report':
        argv += ['--gate', 'transport']
    if damage.startswith('release'):
        argv += ['--release-plan', str(tmp_path / 'release.json')]
        if damage == 'release-missing':
            argv += ['--release-plan-sha256', '0' * 64]
    if damage.startswith('keys'):
        key = tmp_path / 'keys.json'
        if damage == 'keys-invalid':
            key.write_text('{}')
        argv += ['--binding-keys', str(key)]
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit) as exc:
        (shadow_report.main if tool == 'report' else pilot_enablement.main)()
    assert exc.value.code != 0
    stdout = capsys.readouterr().out
    assert len(stdout.splitlines()) == 1
    result = json.loads(stdout)
    if damage == 'bundle-missing':
        assert result['condition'] == 'proof_manifest_absent_or_undecodable'
        assert not (tmp_path / 'gate-output').exists()
    else:
        assert result['verdict'] == 'BLOCKED'
        assert (tmp_path / 'gate-output/report.json').exists()


@pytest.mark.parametrize('field', ['rows', 'preimages'])
@pytest.mark.parametrize('damage', ['surrogate', 'nonfinite', 'domain', 'depth', 'object', 'scalar'])
def test_round11_sibling_damage(cli_original: Any, tmp_path: Path, field: str, damage: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from tests.shadow_v2_gate_totality import tool
    from trusted_router.async_settle_shadow_v2_gate_result import render
    original, ports, _ = cli_original
    value = json.loads(transport(original))
    bad: Any = {'surrogate': '\ud800', 'nonfinite': float('nan'), 'domain': 2**100,
                'object': {}, 'scalar': False}.get(damage)
    if damage == 'depth':
        bad = 0
        for _ in range(24):
            bad = [bad]
    if field in ('rows', 'attachments', 'preimages', 'ledgers'):
        # Damage a single original child; all its siblings must survive.
        child = value[field][0]
        subject_path = child['name'] if field == 'preimages' else 'rows/' + child['kind'] + '/' + child['id']
        if isinstance(child, dict):
            child['raw'] = bad
        else:
            value[field][0] = bad
    else:
        value[field] = bad
    raw = json.dumps(value).encode()
    loaded = load(raw, ports)
    assert loaded.proof == original.proof
    result = evaluate(loaded, ports, gate='shadow-serving')
    expected = json.loads(render(result)[0])
    assert expected['verdict'] == 'BLOCKED'
    assert any(f.gap == 'evidence_schema_gap' and f.subject.get('object', {})
        and f.subject['object']['kind'] == 'a2_file' and f.subject['object']['key'] == subject_path
        for f in result.failures)
    for which in ('report', 'pilot'):
        assert tool(raw, ports, tmp_path / which, which) == expected


@pytest.mark.parametrize('sources', ['days+inventory', 'days+roster', 'days+manifest',
    'inventory+roster', 'inventory+manifest', 'roster+manifest', 'days+inventory+roster'])
def test_round11_scope_source_deletions(cli_original: Any, tmp_path: Path, sources: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from tests.shadow_v2_gate_totality import library, tool
    original, ports, _ = cli_original
    data = json.loads(transport(original))
    if 'days' in sources:
        data['days'] = []
    if 'inventory' in sources:
        data['preimages'] = []
    if 'roster' in sources:
        proof = json.loads(data['proof'])
        proof.update(rosters_by_day={}, instance_regions_by_day={})
        data['proof'] = json.dumps(proof)
    if 'manifest' in sources:
        data['rows'] = [r for r in data['rows'] if r['kind'] != 'async_settle_shadow_control_v2']
    raw = canonical(data)
    expected = library(raw, ports)
    assert expected['verdict'] == 'BLOCKED'
    for which in ('report', 'pilot'):
        assert tool(raw, ports, tmp_path / which, which) == expected


@pytest.mark.parametrize('field', ['artifacts', 'criteria', 'verifiers'])
@pytest.mark.parametrize('bad', [[]])
def test_round11_release_collection_shape(cli_original: Any, tmp_path: Path, field: str, bad: Any) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from tests.shadow_v2_gate_totality import tool
    original, ports, _ = cli_original
    plan = json.loads(ports.release.raw)
    if field.endswith('_member'):
        if field == 'criterion_member':
            plan['criteria'][next(iter(plan['criteria']))] = [bad]
        elif field == 'verifier_member':
            plan['verifiers'][next(iter(plan['verifiers']))] = [bad, bad, bad]
        elif field == 'required_member':
            plan['required'] = [bad]
        else:
            plan['required_stages'] = {'transport': [bad]}
    else:
        plan[field] = bad
    for which in ('report', 'pilot'):
        output = tool(transport(original), ports, tmp_path / which, which, release=json.dumps(plan).encode())
        assert output['verdict'] == 'BLOCKED'


def test_round11_decoder_depth_does_not_erase_proof(cli_original: Any, tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from tests.shadow_v2_gate_totality import library, tool
    original, ports, _ = cli_original
    raw = transport(original)[:-1] + b',"unused":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    expected = library(raw, ports)
    assert expected['verdict'] == 'BLOCKED'
    for which in ('report', 'pilot'):
        assert tool(raw, ports, tmp_path / which, which) == expected


@pytest.mark.parametrize('field', ['kind', 'id'])
def test_round11_oversized_identity_preserves_report(cli_original: Any, tmp_path: Path, field: str) -> None:
    from tests.shadow_v2_gate_a2_fixture import transport
    from tests.shadow_v2_gate_totality import library, tool
    original, ports, _ = cli_original
    data = json.loads(transport(original))
    data['rows'][0][field] = 'x' * 2000
    raw = canonical(data)
    expected = library(raw, ports)
    assert expected['verdict'] == 'BLOCKED'
    for which in ('report', 'pilot'):
        assert tool(raw, ports, tmp_path / which, which) == expected
