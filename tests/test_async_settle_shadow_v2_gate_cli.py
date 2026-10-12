"""Policy dispatch, fail-closed loader/render seams and v1 separation."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.async_settle import pilot_enablement, shadow_report
from tests.shadow_v2_gate_fixture_generator import pre_enable_bundle
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.detached_jws import canonical

pytestmark = pytest.mark.timeout(method='signal')

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


def test_v1_rejects_v2_bundle(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    data = pre_enable_bundle(tmp_path)
    data.update(policy_version='shadow-v2', contract_revision='a1')
    path = tmp_path / 'v2.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, 'argv', ['pilot', 'fleet-budgets-pre', '--bundle', str(path)])
    with pytest.raises(SystemExit) as exc:
        pilot_enablement.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'BLOCKED'
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
