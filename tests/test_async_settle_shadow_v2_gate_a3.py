"""A3 corrections through original bytes, the library, and both public tools."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from tests import test_async_settle_shadow_cohort_runtime as runtime_originals
from tests.shadow_v2_gate_a2_fixture import real_fixture, transport
from tests.shadow_v2_gate_cache import (  # noqa: F401
    default_dependency_lookup_cache,
    payloadgraph_shared_lookup_cache,
)
from tests.shadow_v2_gate_live_originals import captured
from tests.shadow_v2_gate_totality import library, tool
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_gate_native_accounting as consumer
from trusted_router import async_settle_shadow_v2_pack_accounting_stream as bundle_accounting
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_result import run
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def originals(tmp_path_factory: Any) -> Any:
    return real_fixture(tmp_path_factory.mktemp('a3-originals'), chain=False)


def through_all(raw: bytes, ports: Any, tmp_path: Path, gate: str = 'transport') -> dict[str, Any]:
    from tests.shadow_v2_gate_totality import BoundaryCache
    # One exact-byte evaluation per comparison; each tool still reads its
    # original files, writes its graph, and runs the output validator.
    with BoundaryCache().installed():
        expected = library(raw, ports, gate)
        assert 'error' not in expected
        for which in ('report', 'pilot'):
            assert tool(raw, ports, tmp_path / which, which, gate=gate) == expected
    return expected


@pytest.mark.parametrize('manifest', [True, False])
def test_original_writer_cap_never_becomes_manifest(originals: Any, tmp_path: Path, manifest: bool) -> None:
    original, ports, _ = originals
    value = json.loads(transport(original))
    cap = next(row for row in captured()['rows'] if row['id'].endswith('/cap-v2'))
    value['rows'].append(cap)
    if not manifest:
        value['rows'] = [r for r in value['rows'] if not r['id'].endswith('/manifest-v2')]
    raw = canonical(value)
    bundle = load(raw, ports, gate='transport')
    assert bundle.proof == original.proof
    assert any(row.raw == cap['raw'].encode() for row in bundle.rows)
    output = through_all(raw, ports, tmp_path)
    assert output['verdict'] == ('PASS' if manifest else 'BLOCKED')
    own = [i for i in bundle.a2_streams['loading_validation'] if i['subject'] == a2.O(p1.KINDS[5], cap['id'])]
    assert own == []
    if not manifest:
        assert any(i['gap'] == 'artifact_gap' and i['subject'] == a2.O(p1.KINDS[5], original.days[0] + '/manifest-v2')
            for i in bundle.a2_streams['loading_validation'])


def test_live_cap_only_preserves_original_proof_and_siblings(originals: Any, tmp_path: Path) -> None:
    _, ports, _ = originals
    value = captured()
    value['rows'] = [r for r in value['rows'] if not r['id'].endswith('/manifest-v2')]
    raw = canonical(value)
    bundle = load(raw, ports, gate='transport')
    assert bundle.proof == value['proof'].encode()
    assert [(r.key.kind, r.key.id, r.raw) for r in bundle.rows] == [(r['kind'], r['id'], r['raw'].encode()) for r in value['rows']]
    assert through_all(raw, ports, tmp_path)['verdict'] == 'BLOCKED'
    assert any(issue['gap'] == 'artifact_gap' and issue['subject'] == a2.O(p1.KINDS[5], day + '/manifest-v2')
        for day in value['days'] for issue in bundle.a2_streams['loading_validation'])


def test_downstream_error_keeps_proof(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    original, ports, _ = originals
    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise KeyError('private implementation key')
    monkeypatch.setattr(loader, 'validate_original_bytes', broken)
    raw = transport(original)
    bundle = load(raw, ports, gate='transport')
    assert bundle.proof == original.proof and bundle.rows == original.rows
    assert through_all(raw, ports, tmp_path)['verdict'] == 'BLOCKED'
    assert 'private implementation key' not in json.dumps(bundle.a2_streams)


@pytest.mark.parametrize('body', ['{}', '{'])
def test_unknown_control_is_scoped_to_its_original_row(originals: Any, tmp_path: Path, body: str) -> None:
    original, ports, _ = originals
    value = json.loads(transport(original))
    unknown = dict(kind=p1.KINDS[5], id=original.days[0] + '/unknown-v2', raw=body)
    value['rows'].append(unknown)
    bundle = load(canonical(value), ports, gate='transport')
    own = [i for i in bundle.a2_streams['loading_validation'] if i['gap'] == 'evidence_schema_gap']
    assert own == [dict(gap='evidence_schema_gap', detail='evidence_schema_gap', subject=a2.O(unknown['kind'], unknown['id']))]
    assert through_all(canonical(value), ports, tmp_path)['verdict'] == 'BLOCKED'


def control_readers(source: str) -> dict[str, int]:
    """Resolve imported and assigned aliases before enumerating row decoders."""
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    def resolve(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return resolve(node.value) + '.' + node.attr
        if isinstance(node, ast.Subscript):
            return resolve(node.value) + '[' + resolve(node.slice) + ']'
        if isinstance(node, ast.Constant):
            return str(node.value)
        return ast.unparse(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module or '') + '.' + alias.name
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = resolve(node.value)
    imported = dict(aliases)
    readers: dict[str, int] = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        aliases = dict(imported)
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        aliases[target.id] = resolve(node.value)
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                call = resolve(node.func)
                if call.endswith(('.MANIFEST', '.MANIFEST.parse', '.validate_row')):
                    readers[function.name] = readers.get(function.name, 0) + 1
                if call.endswith('.kind') and any(resolve(arg).endswith('KINDS[5]') or resolve(arg) == p1.KINDS[5] for arg in node.args):
                    readers[function.name] = readers.get(function.name, 0) + 1
            if isinstance(node, ast.Compare):
                if any(resolve(child).endswith('KINDS[5]') or resolve(child) == p1.KINDS[5]
                       for child in ast.walk(node)):
                    readers[function.name] = readers.get(function.name, 0) + 1
    return readers


def test_every_control_population_reader_uses_shared_selector() -> None:
    root = Path(__file__).parents[1] / 'src/trusted_router'
    actual = {path.stem.removeprefix('async_settle_shadow_v2_gate_'): control_readers(path.read_text())
              for path in root.glob('async_settle_shadow_v2_gate*.py')}
    # Generic row decoders are enumerated too, even when they currently only
    # decode counters or receipts. A new reader must be reviewed explicitly.
    assert {path: readers for path, readers in actual.items() if readers} == {
        'controls': {'manifests': 3, 'traffic_regions': 1}, 'bundle': {'decode_bundle': 1},
        'prior': {'_resolve': 1}, 'views': {'budget_view': 1, 'signature_views': 1}}


@pytest.mark.parametrize('reader', [
    'return [a1.validate_row(r.key.kind, r.key.id, r.raw) for r in rows if r.key.kind == "async_settle_shadow_control_v2"]',
    'kind = p1.KINDS[5]; return [r for r in rows if r.key.kind == kind]',
    'parser = a1.MANIFEST; return parser.parse(rows[0].raw)',
    'parser = a1.MANIFEST; return parser(rows[0])',
    'return ctx.kind("async_settle_shadow_control_v2")',
    'return [r for r in rows if r.key.kind in {"async_settle_shadow_control_v2"}]',
    'from trusted_router.async_settle_shadow_v2_a1_evidence import MANIFEST as parser; return parser.parse(rows[0].raw)',
    'parser = a1.validate_row; return parser(rows[0].key.kind, rows[0].key.id, rows[0].raw)',
])
def test_added_control_reader_is_noticed(tmp_path: Path, reader: str) -> None:
    source = Path(__file__).parents[1] / 'src/trusted_router/async_settle_shadow_v2_gate_expected.py'
    copy = tmp_path / source.name
    copy.write_text(source.read_text() + '\n\ndef added_reader(rows):\n    ' + reader + '\n')
    assert control_readers(source.read_text()) == {}
    assert set(control_readers(copy.read_text())) == {'added_reader'}


ROOT_DEPENDENCIES = tuple(name for name in __import__(
    'trusted_router.async_settle_shadow_v2_a2_contract', fromlist=['OBJECT_PREIMAGES']).OBJECT_PREIMAGES if name.startswith('proof.'))



def test_compared_branch_has_no_token_file_or_offline_calls(originals: Any, tmp_path: Path) -> None:
    original, ports, _ = originals
    assert not any(p.name == 'loading/signatures.json' for p in original.preimages)
    raw = transport(original)
    assert load(raw, ports, gate='fleet-budgets').a2_streams['loading_issues'] == []
    result, rendered = run(raw, ports, gate='fleet-budgets')
    assert result is not None and isinstance(rendered, tuple)
    report = json.loads(rendered[1][0])
    assert report['rules']['G5'] == 'pass'
    assert report['verifiers']['binding']['calls'] == 0
    assert report['verifiers']['certificate']['calls'] == 0
    assert not any('retained compared sample join failed' in f.detail for f in result.failures)
    through_all(raw, ports, tmp_path, 'fleet-budgets')


@pytest.mark.parametrize('damage', ['sample_absent', 'retry_mismatch'])
def test_unbacked_compared_outcome_blocks_all_dependent_rules(originals: Any, tmp_path: Path, damage: str) -> None:
    original, ports, _ = originals
    value = json.loads(transport(original))
    if damage == 'sample_absent':
        value['rows'] = [r for r in value['rows'] if r['kind'] != p1.KINDS[0]]
    else:
        row = next(r for r in value['rows'] if r['kind'] == p1.KINDS[2])
        page = json.loads(row['raw'])
        terminal = next(e for e in page['events'] if e['phase'] in ('settle', 'refund'))
        terminal['retry_original'] = dict(day=page['day'], boot=page['boot'], event_seq=terminal['seq'])
        terminal.update(partition='C', proof_kind='none', comparison='mismatch')
        row['raw'] = canonical(page).decode()
    raw = canonical(value)
    result, _ = run(raw, ports, gate='shadow-serving')
    assert result is not None
    assert {'G3', 'G4', 'G5', 'G8', 'G9'} <= {f.rule for f in result.failures}
    assert through_all(raw, ports, tmp_path, 'shadow-serving')['verdict'] == 'BLOCKED'


def test_future_proof_objects_are_not_required_at_transport(originals: Any, tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a2_fixture import commit_originals
    original, ports, _ = originals
    files = {p.name: p.raw for p in original.preimages}
    external = json.loads(files['proof.external_proofs_sha256'])
    future = [r for r in external['requirements'] if r['stage'] != 'transport']
    for item in future:
        del files[item['evidence']['path']]
    external['requirements'] = [r for r in external['requirements'] if r['stage'] == 'transport']
    files['proof.external_proofs_sha256'] = canonical(external)
    changed = commit_originals(original, files)
    assert through_all(transport(changed), ports, tmp_path)['verdict'] == 'PASS'
    loaded = load(transport(changed), ports, gate='transport')
    assert loaded.budget is None and loaded.reset_support is None
    assert loaded.external is not None and all(p.stage == 'transport' for p in loaded.external.proofs)


def weaken_plan(raw: bytes, damage: str) -> bytes:
    value = json.loads(raw)
    if damage == 'move_current':
        value['required_stages']['transport'].remove('p4a_fail_closed')
        value['required_stages']['pre-flip'].append('p4a_fail_closed')
    elif damage == 'inventory_only':
        value['required'].remove('d2_d3_capacity_slo')
    elif damage == 'drop_deferred':
        value['required'].remove('d2_d3_capacity_slo')
        value['required_stages']['pre-flip'].remove('d2_d3_capacity_slo')
    else:
        value['criteria']['pre-flip.policy_rollback_status'] = value['criteria']['transport.policy_rollback_status']
    return canonical(value)


@pytest.mark.parametrize('damage', ['move_current', 'drop_deferred', 'shadow_for_money', 'inventory_only'])
def test_release_plan_cannot_weaken_policy(originals: Any, tmp_path: Path, damage: str) -> None:
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
    original, ports, _ = originals
    raw_plan = weaken_plan(ports.release.raw, damage)
    # The shared parser checks the file format; gate loading checks policy.
    if damage == 'inventory_only':
        with pytest.raises(ValueError, match='evidence_schema_gap'):
            decode_release_plan(raw_plan, file_digest(raw_plan))
    else:
        decoded = decode_release_plan(raw_plan, file_digest(raw_plan))
        assert decoded.raw == raw_plan
    bad = json.loads(raw_plan)
    from trusted_router.async_settle_shadow_v2_gate_policy import validate_policy
    with pytest.raises(ValueError, match='evidence_schema_gap'):
        validate_policy(tuple(bad['required']), bad['required_stages'], bad['criteria'])
    bad_ports = replace(ports, release=replace(ports.release, raw=raw_plan, required=tuple(bad['required']),
        required_stages={k: tuple(v) for k, v in bad['required_stages'].items()},
        criteria={k: tuple(v) for k, v in bad['criteria'].items()}))
    original = replace(original, preimages=tuple(Preimage(p.name, raw_plan if p.name == ports.release.path else p.raw) for p in original.preimages))
    raw = transport(original)
    assert library(raw, bad_ports, 'transport')['verdict'] == 'BLOCKED'
    for which in ('report', 'pilot'):
        assert tool(raw, bad_ports, tmp_path / which, which, gate='transport')['verdict'] == 'BLOCKED'


@pytest.mark.parametrize('damage', ['move_current', 'drop_deferred', 'shadow_for_money'])
def _oracle_test_prior_release_plan_cannot_weaken_policy(originals: Any, tmp_path: Path, damage: str) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import alter_prior_inventory, attach_prior_chain
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    original, ports, _ = originals
    original, ports = attach_prior_chain(original, ports)
    changed_plans = []
    def change(raw: bytes) -> bytes:
        modified = weaken_plan(raw, damage)
        changed_plans.append(modified)
        return modified
    original = alter_prior_inventory(original, 'transport', lambda path: path == ports.release.path, change)
    # Independently approve the altered original plan so rejection must come
    # from policy, not a failure to approve its bytes in the current release.
    plan = json.loads(ports.release.raw)
    for raw_plan in changed_plans:
        plan['artifacts']['prior-artifacts/' + file_digest(raw_plan)] = file_digest(raw_plan)
    raw_plan = canonical(plan)
    ports = replace(ports, release=replace(ports.release, raw=raw_plan, artifacts=plan['artifacts']))
    original = replace(original, preimages=tuple(Preimage(p.name, raw_plan if p.name == ports.release.path else p.raw) for p in original.preimages))
    raw = transport(original)
    result, _ = run(raw, ports, gate='shadow-serving')
    assert result is not None and any(f.subject == a2.O('prior_gate', 'transport') for f in result.failures)
    assert through_all(raw, ports, tmp_path, 'shadow-serving')['verdict'] == 'BLOCKED'


def test_pre_enable_keeps_dormant_production_and_closed_trial_sources(originals: Any, tmp_path: Path) -> None:
    original, ports, _ = originals
    raw = transport(original)
    bundle = load(raw, ports, gate='fleet-budgets-pre')
    assert bundle.budget is not None
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'PASS'
    production = bundle.budget.pre_input['serving']
    assert all(row['pins']['TR_ASYNC_SETTLE_SHADOW'] == 'off' for row in production if row['role'] == 'enclave')
    assert all(row['pins']['TR_ASYNC_SETTLE_SHADOW_WORKSPACES'] == '' for row in production if row['role'] == 'router')
    # The loader does not edit any supplied original to make the trial pass.
    assert bundle.preimages == original.preimages
    value = json.loads(raw)
    value['preimages'] = [p for p in value['preimages'] if p['name'] != 'loading/qualification-scope.json']
    assert through_all(canonical(value), ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'BLOCKED'
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    value = json.loads(raw)
    scope_file = next(p for p in value['preimages'] if p['name'] == 'loading/qualification-scope.json')
    scope = json.loads(scope_file['raw'])
    scope['trial_resource'] = scope['production_resource']
    scope_file['raw'] = canonical(scope).decode()
    plan = json.loads(ports.release.raw)
    plan['artifacts'][scope_file['name']] = file_digest(scope_file['raw'].encode())
    changed_plan = canonical(plan)
    next(p for p in value['preimages'] if p['name'] == ports.release.path)['raw'] = changed_plan.decode()
    changed_ports = replace(ports, release=replace(ports.release, raw=changed_plan, artifacts=plan['artifacts']))
    _, failures_by_entry = output_failures(canonical(value), changed_ports, tmp_path, 'fleet-budgets-pre')
    for failures in failures_by_entry:
        own = [f for f in failures if f['subject'] == a2.O('a2_file', scope_file['name'])]
        assert len(own) == 1 and own[0]['gap'] == 'evidence_schema_gap' and own[0]['count'] == 1


@pytest.mark.parametrize('damage', ['expired', 'wrong_purpose', 'foreign_identity', 'malformed', 'invalid_signature'])
def _oracle_test_pinned_runtime_calls_existing_verifier_and_retains_c(originals: Any, tmp_path: Path, monkeypatch: Any, damage: str) -> None:
    import copy

    from tests.test_async_settle_shadow import NOW, context, signer
    from tests.test_async_settle_shadow_cohort import fixture
    from tests.test_async_settle_ticket import endpoint
    from trusted_router import async_settle_shadow_binding as binding
    from trusted_router import async_settle_shadow_cohort as cohort
    from trusted_router.detached_jws import b64decode, b64encode
    from trusted_router.services import async_settle_shadow_cohort_runtime as runtime
    shell, service, _ = runtime_originals.installed()
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    envelope = copy.deepcopy(fixture('terminal_compared_full')[0]['object'])
    token = envelope['billing_shadow_binding']
    header, payload, signature = token.split('.')
    claims = json.loads(b64decode(payload))
    if damage == 'expired':
        claims.update(iat=claims['iat'] - binding.LIFETIME, exp=claims['exp'] - binding.LIFETIME)
        token = signer().sign(claims, claims['iat'])
    elif damage == 'wrong_purpose':
        metadata = json.loads(b64decode(header))
        metadata['typ'] = 'tr-async-settle-ticket-v1'
        material = b64encode(canonical(metadata)) + '.' + payload
        token = material + '.' + b64encode(signer().private.sign(material.encode()))
    elif damage == 'foreign_identity':
        claims['workspace_id'] = 'foreign-workspace'
        token = signer().sign(claims, NOW)
    elif damage == 'malformed':
        token = '!'  # noqa: S105 - malformed public fixture token
    else:
        bad = bytes([b64decode(signature)[0] ^ 1]) + b64decode(signature)[1:]
        token = header + '.' + payload + '.' + b64encode(bad)
    envelope['billing_shadow_binding'] = token
    original_function = runtime.verify_terminal
    assert original_function is cohort.verify_terminal
    calls = []
    def observed(*args: Any, **kwargs: Any) -> Any:
        result = original_function(*args, **kwargs)
        calls.append(result)
        return result
    monkeypatch.setattr(runtime, 'verify_terminal', observed)
    try:
        ctx = context()
        capture, size = shell.capture_terminal(runtime_originals.request([('X-TR-Settlement-Shadow', b64encode(canonical(envelope)))]),
            ctx.body, ctx.authorization, 'settle')
        assert capture is not None
        capture.endpoint = endpoint()
        shell.process(capture)
        assert len(calls) == 1 and calls[0].partition == 'C'
        raw_event = service.journal.slot(capture.reservation).body
        assert raw_event is not None
        event = p1.EVENT.parse(raw_event)
        assert event['partition'] == 'C' and event['proof_kind'] != 'compared'
        pages = service.journal.freeze(capture.reservation.day)
        assert pages
        original, ports, _ = originals
        value = json.loads(transport(original))
        # Original P3 page bytes carry the rejected outcome through both tools.
        value['rows'].extend(dict(kind=p.kind, id=p.key, raw=p.body.decode()) for p in pages)
        assert through_all(canonical(value), ports, tmp_path)['verdict'] == 'BLOCKED'
        shell.release(size)
    finally:
        shell.close()
        shell.worker.join(2)


@pytest.mark.parametrize('active,reviewed', [(False, False), (True, False), (True, True)])
def test_reviewed_traffic_subset_keeps_idle_writer_regions(originals: Any, tmp_path: Path, active: bool, reviewed: bool) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a2_fixture import commit_originals
    from tests.test_async_settle_shadow_v2_gate_loading import approval, reference
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    source = tmp_path / 'region-originals'
    source.mkdir()
    original, ports, _ = real_fixture(source, chain=False, churn=True)
    files = {p.name: p.raw for p in original.preimages}
    for path in list(files):
        if not path.endswith('manifest.independent_inventory_sha256'):
            continue
        envelope = json.loads(files[path])
        serving = json.loads(files[envelope['serving']['path']])
        for row in serving:
            if row['role'] == 'router' and (row['instance'] == 'boot0') == active:
                row['region'] = 'review-test-added-region'
        envelope['serving'] = reference(files, envelope['serving']['path'], canonical(serving))
        deployed = json.loads(files[envelope['deployment']['path']])
        deployed['serving'] = serving
        envelope['deployment'] = reference(files, envelope['deployment']['path'], canonical(deployed))
        envelope['approval'] = approval(files, envelope['deployment'])
        files[path] = canonical(envelope)
    if reviewed:
        policy = json.loads(files['proof.cohort_seed_policy_sha256'])
        policy['required_regions'] = ['review-test-added-region']
        files['proof.cohort_seed_policy_sha256'] = canonical(policy)
    plan = json.loads(ports.release.raw)
    plan['artifacts'].update({path: file_digest(raw) for path, raw in files.items() if path != ports.release.path and not path.startswith('proof.')})
    files[ports.release.path] = canonical(plan)
    ports = replace(ports, release=replace(ports.release, raw=files[ports.release.path], artifacts=plan['artifacts']))
    raw = transport(commit_originals(original, files))
    loaded = load(raw, ports, gate='transport')
    assert loaded.inventory is not None
    assert 'review-test-added-region' in {b.region for b in loaded.inventory.boots}
    own = [i for i in loaded.a2_streams['loading_issues'] if i['path'] == 'proof.cohort_seed_policy_sha256']
    assert bool(own) == (active and not reviewed)
    if active and not reviewed:
        assert own[0]['gap'] == 'evidence_schema_gap'
    # The unchanged manifests intentionally retain their original roster and
    # independently block the changed deployment. The traffic-policy diagnostic
    # above isolates whether an idle writer is incorrectly required to seed.
    assert through_all(raw, ports, tmp_path)['verdict'] == 'BLOCKED'


def output_failures(raw: bytes, ports: Any, tmp_path: Path, gate: str) -> tuple[Any, list[list[Any]]]:
    """Observe fixed details at each entry; tools also validate the written graph."""
    from dataclasses import asdict
    from unittest.mock import patch

    from trusted_router import async_settle_shadow_v2_gate_result as results
    observed = []
    original_run = results.run
    def capture(*args: Any, **kwargs: Any) -> Any:
        result, rendered = original_run(*args, **kwargs)
        assert result is not None and isinstance(rendered, tuple)
        observed.append((result.gate_report, [asdict(f) for f in result.failures]))
        return result, rendered
    with patch.object(results, 'run', capture):
        through_all(raw, ports, tmp_path, gate)
    assert len(observed) == 3
    assert all(report == observed[0][0] for report, _ in observed)
    return observed[0][0], [failures for _, failures in observed]


VIEW_BOUNDARIES = (
    ('budget_view', 'proof.pre_budget_sha256'),
    ('external_view', 'proof.external_proofs_sha256'),
    ('reset_view', 'proof.resolution_dag_sha256'),
    ('signature_views', 'loading/signatures.json'),
)
VIEW_ERRORS = (KeyError, ValueError, TypeError, OverflowError, RecursionError, OSError, AttributeError, IndexError)


@pytest.mark.parametrize('name,path', VIEW_BOUNDARIES)
@pytest.mark.parametrize('error', VIEW_ERRORS, ids=lambda cls: cls.__name__)
def _oracle_test_view_error_retains_originals_and_exact_diagnostic(originals: Any, tmp_path: Path,
        monkeypatch: Any, name: str, path: str, error: Any) -> None:
    from trusted_router import async_settle_shadow_v2_gate_views as views
    original, ports, _ = originals
    def changed(*args: Any, **kwargs: Any) -> Any:
        raise error('non-label changed view text')
    changed.__module__ = views.__name__
    changed.__name__ = name
    changed.__code__ = changed.__code__.replace(co_filename=views.__file__)
    monkeypatch.setattr(views, name, changed)
    raw = transport(original)
    loaded = load(raw, ports, gate='shadow-serving')
    assert loaded.proof == original.proof and loaded.rows == original.rows
    assert loaded.attachments == original.attachments and loaded.preimages == original.preimages
    own = [issue for issue in loaded.a2_streams['loading_issues'] if issue['path'] == path]
    assert own == [dict(gap='evidence_schema_gap', path=path, detail='evidence_schema_gap')]
    _, sets = output_failures(raw, ports, tmp_path, 'shadow-serving')
    for failures in sets:
        own = [f for f in failures if f['subject'] == a2.O('a2_file', path)]
        assert len(own) == 1 and own[0]['gap'] == 'evidence_schema_gap'
        assert own[0]['detail'] == 'evidence_schema_gap'


@pytest.mark.parametrize('field', ['observed_at_us', 'authorize_at_us', 'payload_hash'])
def test_compared_original_join_has_its_own_failure(originals: Any, tmp_path: Path, field: str) -> None:
    original, ports, _ = originals
    value = json.loads(transport(original))
    row = next(r for r in value['rows'] if r['kind'] == p1.KINDS[0])
    sample = json.loads(row['raw'])
    sample[field] = '0' * 64 if field == 'payload_hash' else sample[field] + 1
    row['raw'] = canonical(sample).decode()
    report, sets = output_failures(canonical(value), ports, tmp_path, 'shadow-serving')
    assert report['rules']['G5'] == 'fail'
    for failures in sets:
        own = [f for f in failures if f['gap'] == 'eligible_coverage_gap'
               and f['detail'] == 'retained compared sample join failed']
        assert len(own) == 1


@pytest.mark.parametrize('stage', ['transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip'])
@pytest.mark.parametrize('boundary', ['validation', 'materialize', 'inventory', 'cohort', 'release', 'evaluate', 'render'])
def _oracle_test_recovery_report_has_every_required_failure(originals: Any, tmp_path: Path,
        monkeypatch: Any, boundary: str, stage: str) -> None:
    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    from trusted_router import async_settle_shadow_v2_gate_release as release
    from trusted_router import async_settle_shadow_v2_gate_result as result_module
    from trusted_router import async_settle_shadow_v2_gates as gates
    from trusted_router.async_settle_shadow_v2_a2_contract import REQUIRED_RULES
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    owner, name = {'validation': (loader, 'validate_original_bytes'), 'release': (release, 'release_checks'),
        'evaluate': (gates, 'evaluate'), 'render': (result_module, 'render'),
        **{name: (PayloadGraph, name) for name in ('materialize', 'inventory', 'cohort')}}[boundary]
    def changed(*args: Any, **kwargs: Any) -> Any:
        raise OSError('non-label changed view text')
    monkeypatch.setattr(owner, name, changed)
    original, ports, _ = originals
    report, sets = output_failures(transport(original), ports, tmp_path, stage)
    for rule in REQUIRED_RULES[stage]:
        if report['rules'][rule] in ('fail', 'unevaluable'):
            for failures in sets:
                assert any(f['rule'] == rule for f in failures)
    assert all('non-label' not in json.dumps(failures) for failures in sets)


@pytest.mark.parametrize('changes,accepted', [
    ({}, True), ({'close_us': 2100000}, False), ({'final_pass_us': 4100000}, False),
    ({'preparation_us': 5600000}, False), ({'final_pass_us': 2400000}, False),
    pytest.param(dict(close_us=2000000, result_us=2000000, final_pass_us=4000000,
        preparation_us=2500000, export_wait_us=2500000, grace_us=9000000), True, id='exact-limits'),
    pytest.param(dict(grace_us=3999999), False, id='grace-one-short'),
    pytest.param(dict(close_us=250000, result_us=2100000), False, id='result-over'),
])
def _oracle_test_shutdown_transactions_and_total_use_each_measurement(originals: Any, tmp_path: Path,
        changes: Any, accepted: bool) -> None:
    from tests.shadow_v2_gate_a3_originals import shutdown_originals
    original, ports, _ = originals
    values = dict(close_us=1250000, result_us=1250000, final_pass_us=3000000,
                  preparation_us=500000, export_wait_us=500000, grace_us=10000000)
    values.update(changes)
    raw, ports = shutdown_originals(original, ports, **values)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert loaded.budget is not None
    assert loaded.budget.close_elapsed_us == values['close_us']
    assert loaded.budget.handoff_elapsed_us == values['result_us']
    _, sets = output_failures(raw, ports, tmp_path, 'fleet-budgets-pre')
    for failures in sets:
        assert (not any(f['gap'] == 'close_handoff_budget_gap' for f in failures)) == accepted


def test_view_error_diagnostic_smoke(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _oracle_test_view_error_retains_originals_and_exact_diagnostic(originals, tmp_path, monkeypatch,
        'budget_view', 'proof.pre_budget_sha256', KeyError)


def test_recovery_report_smoke(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _oracle_test_recovery_report_has_every_required_failure(originals, tmp_path, monkeypatch, 'render', 'shadow-serving')


@pytest.mark.parametrize('path', ['inventory/2026-10-06/serving.json', 'inventory/2026-10-06/deployment.json'])
@pytest.mark.parametrize('damage', ['delete', 'corrupt', '{}', 'null', '[]'])
def test_nested_original_diagnostic_smoke(originals: Any, tmp_path: Path, path: str, damage: str) -> None:
    original, ports, _ = originals
    value = json.loads(transport(original))
    if damage == 'delete':
        value['preimages'] = [p for p in value['preimages'] if p['name'] != path]
    else:
        next(p for p in value['preimages'] if p['name'] == path)['raw'] = '{' if damage == 'corrupt' else damage
    _, sets = output_failures(canonical(value), ports, tmp_path, 'transport')
    for failures in sets:
        own = [f for f in failures if f['subject'] == a2.O('a2_file', path)]
        gap = 'artifact_gap' if damage == 'delete' else 'evidence_schema_gap'
        assert len(own) == 1 and own[0]['gap'] == own[0]['detail'] == gap and own[0]['count'] == 1


@pytest.mark.parametrize('path', ['reviews/0e24c2175d11e9b52549aa3487b25aa832270b84477527718b90ffa70e3338f0', 'release/11/source'])
@pytest.mark.parametrize('damage', ['delete', 'relocate', 'corrupt'])
def test_unused_original_matches_boundary_oracle(originals: Any, tmp_path: Path, path: str, damage: str) -> None:
    from tests.shadow_v2_gate_totality import inert_damage
    original, ports, _ = originals
    baseline = json.loads(transport(original))
    baseline['window'] = dict(start_us=min(b.start_us for b in original.inventory.boots), end_us=original.completed_at_us)
    value = json.loads(canonical(baseline))
    if damage == 'delete':
        value['preimages'] = [p for p in value['preimages'] if p['name'] != path]
    else:
        entry = next(p for p in value['preimages'] if p['name'] == path)
        if damage == 'relocate':
            entry['name'] = 'unused/relocated'
        else:
            entry['raw'] = '{'
    raw = canonical(value)
    assert inert_damage('bundle', (), damage, None, {'bundle': baseline}, raw, None, library_call=True) == (damage != 'corrupt')
    assert through_all(raw, ports, tmp_path)['verdict'] == ('BLOCKED' if damage == 'corrupt' else 'PASS')


def test_view_overflow_smoke(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _oracle_test_view_error_retains_originals_and_exact_diagnostic(originals, tmp_path, monkeypatch,
        'budget_view', 'proof.pre_budget_sha256', OverflowError)


def test_boundary_cache_notices_changed_view(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from tests.shadow_v2_gate_totality import BoundaryCache
    with BoundaryCache().installed():
        for index, (name, path) in enumerate(VIEW_BOUNDARIES[:2]):
            directory = tmp_path / str(index)
            directory.mkdir()
            with monkeypatch.context() as changed:
                _oracle_test_view_error_retains_originals_and_exact_diagnostic(originals, directory, changed,
                    name, path, OverflowError)


def _oracle_test_shutdown_opposite_writer_durations(tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a3_originals import shutdown_originals
    (tmp_path / 'originals').mkdir()
    original, ports, _ = real_fixture(tmp_path / 'originals', chain=False, churn=True)
    boots = sorted({json.loads(row.raw)['boot'] for row in original.attachments if row.key.kind == 'shutdown_measurement'})
    assert len(boots) >= 2
    values = {boot: dict(close_us=2000000 if i % 2 else 250000,
        result_us=250000 if i % 2 else 2000000, final_pass_us=2250000,
        preparation_us=500000, export_wait_us=500000, grace_us=10000000)
        for i, boot in enumerate(boots)}
    raw, ports = shutdown_originals(original, ports, per_writer=values)
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'PASS'


def test_shutdown_original_remaining_deadline(tmp_path: Path, monkeypatch: Any) -> None:
    import inspect

    from tests import shadow_v2_gate_fixture_generator as generator
    source = inspect.getsource(generator.build)
    old = 'result.update(day=day, boot=boot.boot,'
    assert old in source
    namespace = dict(vars(generator))
    exec(source.replace(old, 'result.update(shutdown_remaining_us=1000000, day=day, boot=boot.boot,', 1), namespace)  # noqa: S102
    monkeypatch.setattr(generator, 'build', namespace['build'])
    (tmp_path / 'originals').mkdir()
    original, ports, _ = real_fixture(tmp_path / 'originals', chain=False)
    raw = transport(original)
    bundle = load(raw, ports, gate='fleet-budgets-pre')
    assert bundle.budget is not None and bundle.a2_streams['loading_issues'] == []
    _, sets = output_failures(raw, ports, tmp_path, 'fleet-budgets-pre')
    for failures in sets:
        assert any(f['gap'] == 'close_handoff_budget_gap' for f in failures)


def _oracle_test_shutdown_result_ceiling_independent_of_aggregate(originals: Any) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a3_originals import shutdown_originals
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = originals
    raw, ports = shutdown_originals(original, ports, close_us=250000, result_us=2100000,
        final_pass_us=3000000, preparation_us=0, export_wait_us=0, grace_us=9000000)
    bundle = load(raw, ports, gate='fleet-budgets-pre')
    assert bundle.budget is not None
    # Isolate the per-measurement predicate from the separately tested maximum.
    bundle = replace(bundle, budget=replace(bundle.budget, handoff_elapsed_us=0))
    result = evaluate(bundle, ports, gate='fleet-budgets-pre')
    assert any(f.gap == 'close_handoff_budget_gap' and f.detail == 'per-measurement final pass and remaining grace'
               for f in result.failures)


@pytest.mark.parametrize('contents', ['yaml', 'json-reference'])
def test_opaque_support_source_has_only_byte_checks(originals: Any, tmp_path: Path, contents: str) -> None:
    from tests.shadow_v2_gate_a3_originals import opaque_originals
    original, ports, _ = originals
    body = (Path('.github/workflows/ci.yml').read_bytes() if contents == 'yaml' else
            canonical(dict(path='unlisted/input.json', bytes=7, sha256='0' * 64)))
    raw, ports = opaque_originals(original, ports, body)
    assert through_all(raw, ports, tmp_path)['verdict'] == 'PASS'


def test_opaque_passes_support_has_only_byte_checks(originals: Any, tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a3_originals import opaque_originals
    original, ports, _ = originals
    body = b'{"passes":[{"bundle":"example.json","accounting":{"path":"unused-accounting.json"},"prefix":null,"object_plan":{}}]}'
    raw, ports = opaque_originals(original, ports, body)
    assert through_all(raw, ports, tmp_path)['verdict'] == 'PASS'


@pytest.mark.parametrize('gate', ['transport', 'fleet-budgets-pre'])
@pytest.mark.parametrize('two_files', [False, True], ids=['support-only', 'file-ref'])
def test_opaque_registration_support_has_only_byte_checks(originals: Any, tmp_path: Path, monkeypatch: Any,
                                                         gate: str, two_files: bool) -> None:
    from tests import shadow_v2_gate_a3_originals as helper
    from trusted_router.async_settle_shadow_v2_gate_dependencies import dependencies, original_bytes

    original, ports, _ = originals
    raw, ports = helper.accounted_originals(original, ports)
    original = load(raw, ports, gate=gate)
    support = 'ci/review/stage/producer-captures.json'
    notes = 'ci/review/notes.json'
    reference = helper.reference
    detail = helper.detail

    def support_reference(files: Any, path: str, body: bytes) -> Any:
        if path == 'ci/review/workflow.yml':
            if two_files:
                capture = reference(files, notes, body)
                return reference(files, support, canonical(dict(NativeExport=capture)))
            path = support
        return reference(files, path, body)

    def support_detail(files: Any, entries: Any, stream: str) -> Any:
        if two_files:
            entries = sorted([*entries, reference(files, notes, files[notes])], key=lambda row: row['path'])
        pages: dict[str, bytes] = {}
        descriptor = detail(pages, entries, stream)
        files.update({'review-support-' + path: body for path, body in pages.items()})
        return descriptor

    monkeypatch.setattr(helper, 'reference', support_reference)
    monkeypatch.setattr(helper, 'detail', support_detail)
    example = dict(passes=[dict(bundle='example.json',
        accounting=dict(path=f'accounting/{original.days[0]}/root.json'), prefix=None, object_plan={})])
    if not two_files:
        example['NativeExport'] = dict(path=support)
    for label, body in [('control', canonical(dict(source='workflow record'))), ('example', canonical(example))]:
        raw, bound = helper.opaque_originals(original, ports, body)
        (tmp_path / label).mkdir()
        assert through_all(raw, bound, tmp_path / label, gate)['verdict'] == 'PASS'
        loaded = load(raw, bound, gate=gate)
        assert loaded.a2_streams['loading_issues'] == []
        files = {p.name: p.raw for p in loaded.preimages}
        parsers = dependencies(files, dict(loaded.a2_payloads), bound, 'transport', loaded.ledgers)
        for path in (support, notes) if two_files else (support,):
            assert parsers[path] is original_bytes


def test_reviewed_multi_bundle_stage_and_missing_part(originals: Any, tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a3_originals import captured_plan
    original, ports, _ = originals
    planned = captured_plan(transport(original), tmp_path / 'plan', part_size=50)
    result, output = run(planned, ports, gate='transport')
    assert result is not None and result.verdict == 'PASS'
    assert isinstance(output, tuple)
    descriptor = json.loads(planned.raw)
    parts = descriptor['parts']
    assert len(parts) > 1
    for which in ('report', 'pilot'):
        assert tool(planned.raw, ports, tmp_path / which, which, gate='transport',
            options={'bundle': str(tmp_path / 'plan' / 'transport-plan.json')}) == json.loads(output[0])
    for part in [*parts, *(entry['bundle'] for entry in descriptor['inputs']['bundles'])]:
        path = tmp_path / 'plan' / part['path']
        raw = path.read_bytes()
        path.unlink()
        loaded = load(planned, ports, gate='transport')
        assert loaded.proof == original.proof
        assert any(i['path'] == 'transport/input-plan' and i['gap'] == 'artifact_gap' for i in loaded.a2_streams['loading_issues'])
        path.write_bytes(raw)
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput
    (tmp_path / 'plan' / descriptor['metadata']['path']).unlink()
    _, missing = run(planned, ports, gate='transport')
    assert isinstance(missing, UnavailableGateOutput)


def test_multi_bundle_exceeds_combined_transport_count(originals: Any, tmp_path: Path) -> None:
    from tests.shadow_v2_gate_a3_originals import captured_plan
    original, ports, _ = originals
    value = json.loads(transport(original))
    value['preimages'].extend(dict(name=f'additional/{index:05}', raw='{}') for index in range(10001))
    planned = captured_plan(canonical(value), tmp_path / 'plan')
    loaded = load(planned, ports, gate='transport')
    assert len(loaded.preimages) == len(value['preimages']) > 10000
    assert loaded.a2_streams['loading_issues'] == []
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    assert evaluate(loaded, ports, gate='transport').verdict == 'PASS'


def test_input_plan_reads_one_original_at_a_time(tmp_path: Path) -> None:
    import tracemalloc

    from tests.test_async_settle_shadow_v2_gate_loading import detail, reference
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_plan import PlanInput
    files: dict[str, bytes] = {}
    size, count = 250000, 128
    digest = file_digest(b'x' * size)
    keys = [dict(path=f'originals/{i:05}', bytes=size, sha256=digest) for i in range(count)]
    leaf = reference(files, 'leaf.json', a2.INPUT_LEAF.serialize(dict(
        v=2, policy_version='shadow-v2', contract_revision='a2', keys=keys, complete=True)))
    entries = [dict(bundle=leaf, first_key=keys[0]['path'], last_key=keys[-1]['path'], objects=count)]
    before = set(files)
    inventory = detail(files, entries, 'input_inventory')
    pages = [dict(path=k, bytes=len(files[k]), sha256=file_digest(files[k])) for k in files.keys() - before]
    plan = dict(format='shadow-v2-pack-plan-1', days=['2026-10-06'], predecessors=[],
        inventory=inventory, bundles=entries, objects=count, inventory_pages=pages)
    def read(path: str) -> bytes:
        return b'x' * size if path.startswith('originals/') else files[path]
    source = PlanInput(canonical(plan), read)
    tracemalloc.start()
    try:
        found = list(source.leaves(plan))
        _, peak = tracemalloc.get_traced_memory()
        assert found == keys
        assert peak < 4 * 1024 * 1024, peak
        (tmp_path / 'memory.json').write_text(json.dumps(dict(original_bytes=size * count, peak_bytes=peak)))
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('damage,gap', [('', None), ('missing', 'artifact_gap'),
    ('cursor', 'evidence_schema_gap'), ('root-shape', 'evidence_schema_gap'), ('page-shape', 'evidence_schema_gap'), ('unaccounted', 'evidence_schema_gap'),
    ('extra-rpc', 'evidence_export_budget_gap'), ('collector-object', 'evidence_schema_gap'),
    ('collector-row', 'evidence_schema_gap'), ('planned-collector', None)])
def test_export_accounting_originals_are_consumed(originals: Any, tmp_path: Path, damage: str, gap: str | None) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, damage=damage)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    if damage == 'missing':
        assert any(i['gap'] == gap and i['path'].endswith('/root.json') for i in loaded.a2_streams['loading_issues'])
    elif damage in ('cursor', 'root-shape', 'page-shape', 'unaccounted', 'collector-object', 'collector-row'):
        assert any(i['gap'] == gap for i in loaded.a2_streams['loading_issues'])
        if damage in ('root-shape', 'page-shape'):
            target = '/root.json' if damage == 'root-shape' else '/reads.json'
            assert [i['gap'] for i in loaded.a2_streams['loading_issues'] if i['path'].endswith(target)] == [gap]
    else:
        assert loaded.a2_streams['loading_issues'] == []
        assert loaded.a2_streams['export_accounting']
    report, sets = output_failures(raw, ports, tmp_path, 'fleet-budgets-pre')
    expected_g1 = 'pass' if damage in ('', 'extra-rpc', 'planned-collector') else 'unevaluable' if damage in ('root-shape', 'page-shape') else 'fail'
    assert report['rules']['G1'] == expected_g1
    for failures in sets:
        assert (not failures if gap is None else any(f['gap'] == gap for f in failures))
        if damage in ('root-shape', 'page-shape'):
            target = '/root.json' if damage == 'root-shape' else '/reads.json'
            own = [f for f in failures if f['subject'] == a2.O('a2_file', 'accounting/' + original.days[0] + target)]
            assert len(own) == 1 and own[0]['gap'] == gap and own[0]['count'] == 1


@pytest.fixture(scope='module')
def shutdown_context(originals: Any) -> Any:
    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle
    original, ports, _ = originals
    return decode_bundle(load(transport(original), ports, gate='fleet-budgets-pre'), ports, 'fleet-budgets-pre')


@pytest.mark.parametrize('case,accepted', [('exact', True), ('opposite', True),
    ('grace-short', False), ('result-over', False), ('close-over', False),
    ('final-over', False), ('final-short', False), ('total-over', False), ('actual-missing', False)])
def test_shutdown_scalar_predicates(shutdown_context: Any, case: str, accepted: bool) -> None:
    import copy
    from dataclasses import replace

    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_gate_inputs import Key
    from trusted_router.async_settle_shadow_v2_gate_load import load_budgets
    ctx = copy.copy(shutdown_context)
    ctx.issues, ctx.verifier_calls, ctx.verified = [], {}, set()
    bundle = ctx.bundle
    shutdown = bundle.shutdown
    values = dict(close_us=1250000, result_us=1250000, final_pass_us=3000000,
                  preparation_us=500000, export_wait_us=500000, grace_us=10000000)
    changes = {
        'actual-missing': {},
        'exact': dict(close_us=2000000, result_us=2000000, final_pass_us=4000000,
            preparation_us=2500000, export_wait_us=2500000, grace_us=9000000),
        'opposite': dict(close_us=2000000, result_us=250000, final_pass_us=2250000),
        'grace-short': dict(grace_us=3999999), 'result-over': dict(close_us=250000, result_us=2100000),
        'close-over': dict(close_us=2100000, result_us=250000), 'final-over': dict(final_pass_us=4100000),
        'final-short': dict(final_pass_us=2400000), 'total-over': dict(preparation_us=5600000),
    }
    values.update(changes[case])
    measurements = [{**row, **values} for row in shutdown.measurements]
    entries = [{**row, 'grace_us': values['grace_us']} for row in shutdown.entries]
    if case == 'actual-missing':
        ctx.rows = {k: {**v, 'shutdown_remaining_us': None} if k.kind == a1.KINDS[-1] else v for k, v in ctx.rows.items()}
    if case == 'opposite':
        measurements.append({**measurements[0], 'boot': 'second-writer', 'close_us': 250000, 'result_us': 2000000})
        entries.append({**entries[0], 'boot': 'second-writer'})
        original = next(v for k, v in ctx.rows.items() if k.kind == a1.KINDS[-1])
        ctx.rows = {**ctx.rows, Key(a1.KINDS[-1], original['day'] + '/second-writer'): {**original, 'boot': 'second-writer'}}
    ctx.bundle = replace(bundle, shutdown=replace(shutdown, measurements=tuple(measurements), entries=tuple(entries)))
    load_budgets(ctx)
    failures = [issue for issue in ctx.issues if issue.gap == 'close_handoff_budget_gap']
    assert (not failures) == accepted
    if not accepted:
        assert any(issue.detail == 'per-measurement final pass and remaining grace' for issue in failures)


def test_payload_page_lookup_does_not_retain_original_bodies(tmp_path: Path) -> None:
    import subprocess
    import sys
    script = '''
import json,tracemalloc
from collections.abc import Mapping
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
class Originals(Mapping):
    def __len__(self): return 128
    def __iter__(self): return (str(i) for i in range(128))
    def __getitem__(self,key): return json.dumps({'body': 'x'*250000, 'id': key}).encode()
graph=PayloadGraph(Originals())
tracemalloc.start()
assert graph.detail(dict(root=entry_root([],stream='files'), count=0,pages=0,tail_sha256=None),'files') == []
_,peak=tracemalloc.get_traced_memory()
assert peak < 4*1024*1024, peak
print(json.dumps(dict(original_bytes=128*250000,peak_bytes=peak)))
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=True)  # noqa: S603
    measured = json.loads(completed.stdout)
    assert measured['original_bytes'] > 30 * 1024 * 1024
    (tmp_path / 'memory.json').write_text(completed.stdout)


def test_required_support_bytes_report_the_original_path(originals: Any, tmp_path: Path) -> None:
    from trusted_router import async_settle_shadow_v2_a2_evidence as evidence
    original, ports, _ = originals
    path = 'ci/transport/vectors.json'
    value = json.loads(transport(original))
    next(entry for entry in value['preimages'] if entry['name'] == path)['raw'] = '{'
    _, sets = output_failures(canonical(value), ports, tmp_path, 'transport')
    for failures in sets:
        own = [failure for failure in failures if failure['subject'] == evidence.O('a2_file', path)]
        assert len(own) == 1 and own[0]['gap'] == own[0]['detail'] == 'artifact_gap' and own[0]['count'] == 1


def test_qualification_inventory_has_its_root_parser(originals: Any) -> None:
    from trusted_router.async_settle_shadow_v2_gate_dependencies import dependencies
    from trusted_router.async_settle_shadow_v2_gate_loading import error_gap
    original, ports, _ = originals
    files = {p.name: p.raw for p in original.preimages}
    loaded = load(transport(original), ports, gate='fleet-budgets-pre')
    parsers = dependencies(files, loaded.a2_payloads, ports, 'fleet-budgets-pre', original.ledgers)
    paths = [path for path in parsers if path.endswith('manifest.independent_inventory_sha256')]
    assert paths
    for path in paths:
        assert parsers[path](files[path]) == loaded.a2_payloads[path]
        for raw in (b'{', b'{}', b'null', b'[]'):
            with pytest.raises(ValueError) as error:
                parsers[path](raw)
            assert error_gap(error.value) == 'evidence_schema_gap'


@pytest.mark.parametrize('damage', ['', 'missing', 'cursor', 'root-shape', 'page-shape',
    'unaccounted', 'extra-rpc', 'collector-object', 'collector-row', 'planned-collector', 'object-rows'])
def test_accounting_reference_differential(originals: Any, tmp_path: Path, monkeypatch: Any, damage: str) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    from tests.shadow_v2_gate_accounting import assert_accounting_equal
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import DAY_KINDS
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, damage='' if damage == 'object-rows' else damage)
    current = consumer.compare_export_accounting_streamed
    reference = bundle_accounting.compare_export_accounting_streamed
    comparisons = []

    def compare(files: Any, ref: Any, bundle: Path, **kwargs: Any) -> Any:
        captured = json.loads(bundle.read_bytes())
        assert all(row['kind'] in DAY_KINDS for row in captured['rows'])
        assert all(row['kind'] not in DAY_KINDS for row in captured['attachments'])
        if damage == 'object-rows':
            captured['rows'].extend(captured.pop('attachments'))
            bundle = tmp_path / 'object-rows.json'
            bundle.write_bytes(canonical(captured))
        actual = current(files, ref, bundle, **kwargs)
        records = [*captured.get('rows', []), *captured.get('attachments', [])]
        grouped = dict(rows=[row for row in records if row['kind'] in DAY_KINDS],
                       attachments=[row for row in records if row['kind'] not in DAY_KINDS])
        if damage == 'object-rows':
            grouped['rows'].extend(grouped.pop('attachments'))
        reference_path = tmp_path / 'bundle-comparison.json'
        reference_path.write_bytes(canonical(grouped))
        expected = reference(files, ref, reference_path, **kwargs)
        assert_accounting_equal(actual, expected)
        comparisons.append(actual)
        return actual

    with monkeypatch.context() as patch:
        patch.setattr(consumer, 'compare_export_accounting_streamed', compare)
        actual = library(raw, ports, 'fleet-budgets-pre')
    with monkeypatch.context() as patch:
        patch.setattr(consumer, 'compare_export_accounting_streamed', reference)
        expected = library(raw, ports, 'fleet-budgets-pre')
    if damage != 'object-rows':
        assert actual == expected  # Includes every emitted loading/report diagnostic.
    if damage in ('missing', 'root-shape', 'page-shape'):
        # These originals fail the shared file/header checks before comparison.
        assert not comparisons and actual['verdict'] == 'BLOCKED'
    else:
        assert comparisons
        if damage in ('', 'planned-collector'):
            assert all(item.complete for item in comparisons)
            assert actual['verdict'] == 'PASS'
        elif damage != 'extra-rpc':
            assert all(not item.complete for item in comparisons)
            assert actual['verdict'] == 'BLOCKED'
        if damage == 'object-rows':
            assert all(item.missing == ('native_key_totals',) for item in comparisons)


def test_accounting_differential_compares_diagnostics() -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_accounting import assert_accounting_equal
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import AccountingResult
    expected = AccountingResult('2026-10-06', (), ('planned_collector_attachments', 'native_body_totals'), 0, 0, 0)
    assert_accounting_equal(expected, expected)
    with pytest.raises(AssertionError):
        assert_accounting_equal(replace(expected, missing=('planned_collector_attachments',)), expected)


def test_accounting_support_definitions_match() -> None:
    from trusted_router import async_settle_shadow_v2_collect_ports as collectors
    from trusted_router import async_settle_shadow_v2_export as live_export
    from trusted_router import async_settle_shadow_v2_gate_native_accounting as native
    from trusted_router import async_settle_shadow_v2_pack_daypack as daypack
    from trusted_router import async_settle_shadow_v2_pack_streaming as streaming

    assert native.Files is collectors.Files
    assert native.DAY_KINDS is live_export.DAY_KINDS
    assert native.OBJECT_KINDS is live_export.OBJECT_KINDS
    assert native.bounds is live_export.bounds
    assert native.AccountingResult is daypack.AccountingResult
    assert native.CapturedJSON is streaming.CapturedJSON


@pytest.mark.parametrize('value', ['2026-10-06', '2024-02-29', '2026-12-31', '20261006',
    '2026-W41-2', '2026-02-29', '2026-13-01', '', '2026-1-1', '9999-12-31', None, 123])
def test_accounting_bounds_reference(value: Any) -> None:
    from trusted_router.async_settle_shadow_v2_export import bounds as original_bounds
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import bounds

    def outcome(fn: Any) -> Any:
        try:
            return fn(value)
        except (ValueError, TypeError, OverflowError) as exc:
            return type(exc), str(exc)

    assert outcome(bounds) == outcome(original_bounds)


def test_accounting_files_reference(tmp_path: Path, monkeypatch: Any) -> None:
    from io import BytesIO

    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_collect_ports import Files as OriginalFiles
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import Files

    recorded = []
    for cls in (Files, OriginalFiles):
        folder = tmp_path / str(len(recorded))
        files = cls(folder)
        ref = files.put('original.json', b'{}')
        assert files.put('original.json', b'{}') == ref
        assert files.read(ref) == b'{}'
        obj = files.obj('object.json', {'a': 1})
        assert files.read(obj) == b'{"a":1}'
        detail = files.detail('pages', [ref, obj], 'files')
        assert detail['count'] == 2 and detail['pages'] == 1
        outcomes = []
        for bad in ({**ref, 'bytes': 3}, {**ref, 'sha256': '0' * 64}, {**ref, 'bytes': 262145}):
            with pytest.raises(ValueError) as error:
                files.read(bad)
            outcomes.append((type(error.value), str(error.value)))
        calls = []

        class Reader(BytesIO):
            def read(self, size: int = -1) -> bytes:
                calls.append(size)  # noqa: B023 - used within this iteration
                return super().read(size)

        # A small declared reference backed by an oversized file still reads once,
        # with the same finite size as the supplied disk reader.
        body = b'x' * 300000
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', lambda *args, body=body, **kwargs: Reader(body))
            with pytest.raises(ValueError) as error:
                files.read(dict(path='oversize.json', bytes=1, sha256=file_digest(b'x')))
            outcomes.append((type(error.value), str(error.value)))
        assert calls == [262145]
        recorded.append((ref, obj, detail, outcomes))
    assert recorded[0] == recorded[1]


def test_accounting_comparison_is_bundle_function() -> None:

    assert consumer.compare_export_accounting_streamed is bundle_accounting.compare_export_accounting_streamed


@pytest.mark.parametrize('body', [b'{}', b'{"passes":42}', b'42', b'{', b'{"passes":[{"bundle":"native.json","accounting":{},"prefix":null}]}'])
@pytest.mark.parametrize('declared', [True, False], ids=['declared', 'historical'])
def test_round13_capture_format(originals: Any, tmp_path: Path, body: bytes, declared: bool) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, registration_path='stage/producer-captures.json', capture_body=body)
    value = json.loads(raw)
    if not declared:
        value.pop('producer_registrations')
    raw = canonical(value)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert dict(gap='evidence_schema_gap', path='review/capture.data', detail='evidence_schema_gap') in loaded.a2_streams['loading_issues']
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'BLOCKED'


@pytest.mark.parametrize('declared', [True, False], ids=['declared', 'historical'])
def test_round13_complete_scope(originals: Any, tmp_path: Path, declared: bool) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, prefix='review-spool/', registration_path='stage/producer-captures.json')
    value = json.loads(raw)
    if not declared:
        value.pop('producer_registrations')
    raw = canonical(value)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert loaded.a2_streams['loading_issues'] == []
    assert loaded.a2_streams['export_accounting'][0]['native_rpcs'] == 9
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'PASS'


def test_round13_byte_only_content_is_never_decoded(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from tests.shadow_v2_gate_a3_originals import opaque_originals
    from trusted_router import async_settle_shadow_v2_gate_dependencies as dependencies
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    original, ports, _ = originals
    body = b'{"passes":[],"entries":[],"NativeExport":{"path":"example"},"round13":"byte-only"}'
    raw, ports = opaque_originals(original, ports, body)
    for module in (dependencies, payloads, consumer):
        parser = module.retained_json
        def checked(raw: bytes, parser: Any = parser) -> Any:
            assert raw != body, 'byte-only source was decoded'
            return parser(raw)
        monkeypatch.setattr(module, 'retained_json', checked)
    assert through_all(raw, ports, tmp_path)['verdict'] == 'PASS'


def test_round13_measurement_role(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from dataclasses import replace

    from tests import shadow_v2_gate_a3_originals as helper
    from trusted_router.async_settle_shadow_v2_gate_dependencies import dependencies, original_bytes
    from trusted_router.async_settle_shadow_v2_gate_release import measurement_parsers
    original, ports, _ = originals
    reference = helper.reference
    path = 'sources/measurement/workflow.yml'
    def renamed(files: Any, name: str, body: bytes) -> Any:
        return reference(files, path if name == 'ci/review/workflow.yml' else name, body)
    monkeypatch.setattr(helper, 'reference', renamed)
    raw, ports = helper.opaque_originals(original, ports, b'name: review\non: push\n')
    ports = replace(ports, measurements=measurement_parsers(ports.release))
    assert through_all(raw, ports, tmp_path)['verdict'] == 'PASS'
    loaded = load(raw, ports, gate='transport')
    parsers = dependencies({p.name: p.raw for p in loaded.preimages}, dict(loaded.a2_payloads), ports, 'transport', loaded.ledgers)
    assert parsers[path] is original_bytes
    declared = {value['source']['path'] for value in loaded.shutdown.measurements}
    assert declared and path not in declared
    assert all(parsers[name] is ports.measurements[name] for name in declared)


@pytest.mark.parametrize('empty', [False, True], ids=['scoped', 'empty'])
def test_round13_prior_registration_propagation(originals: Any, tmp_path: Path, monkeypatch: Any, empty: bool) -> None:
    from dataclasses import replace

    from tests.shadow_v2_gate_a3_originals import accounted_originals
    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    from trusted_router import async_settle_shadow_v2_gate_prior as prior
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, prefix='' if empty else 'review-spool/',
        registration_path='stage/producer-captures.json' if empty else 'review/registration.data', capture_body=b'{}' if empty else None)
    if empty:
        value = json.loads(raw)
        value['producer_registrations'] = []
        raw = canonical(value)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    result = evaluate(loaded, ports, gate='fleet-budgets-pre')
    assert result.verdict == 'PASS'
    output, attachments = render(result)
    assert json.loads(attachments[0])['producer_registrations'] == list(loaded.producer_registrations)
    archive = [*attachments, *(p.raw for p in loaded.preimages),
               *(row.raw for row in (*loaded.rows, *loaded.attachments)), *loaded.ledgers, loaded.proof]
    current = replace(loaded, pre=output, preimages=(*loaded.preimages,
        *(Preimage('prior-artifacts/' + file_digest(body), body) for body in archive)))
    seen = []
    actual_load = loader.load
    def recorded(raw: Any, *args: Any, **kwargs: Any) -> Any:
        value = json.loads(raw)
        if kwargs.get('gate') == 'fleet-budgets-pre':
            assert value['producer_registrations'] == list(loaded.producer_registrations)
            replay = actual_load(raw, *args, **kwargs)
            assert replay.a2_streams['export_accounting'] == loaded.a2_streams['export_accounting']
            seen.append(value)
            return replay
        return actual_load(raw, *args, **kwargs)
    monkeypatch.setattr(loader, 'load', recorded)
    prior._VERIFIED.clear()
    assert through_all(transport(current), ports, tmp_path, 'fleet-budgets')['verdict'] == 'PASS'
    assert seen


@pytest.mark.parametrize('target', ['registration', 'capture'])
def test_round13_unreadable_declared_original(originals: Any, tmp_path: Path, target: str) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, registration_path='review/registration.data')
    value = json.loads(raw)
    path = 'review/' + target + '.data'
    value['preimages'] = [p for p in value['preimages'] if p['name'] != path]
    raw = canonical(value)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert dict(gap='evidence_schema_gap', path=path, detail='evidence_schema_gap') in loaded.a2_streams['loading_issues']
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'BLOCKED'


@pytest.mark.parametrize('path', [None, 42, []])
def test_round13_capture_reference_path_format(originals: Any, tmp_path: Path, path: Any) -> None:
    from tests.shadow_v2_gate_a3_originals import accounted_originals
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    original, ports, _ = originals
    raw, ports = accounted_originals(original, ports, registration_path='review/registration.data')
    value = json.loads(raw)
    registration = next(p for p in value['preimages'] if p['name'] == 'review/registration.data')
    body = canonical(dict(NativeExport=dict(path=path, bytes=1, sha256='0' * 64)))
    registration['raw'] = body.decode()
    value['producer_registrations'] = [dict(path=registration['name'], bytes=len(body), sha256=file_digest(body))]
    raw = canonical(value)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert dict(gap='evidence_schema_gap', path=registration['name'], detail='evidence_schema_gap') in loaded.a2_streams['loading_issues']
    assert through_all(raw, ports, tmp_path, 'fleet-budgets-pre')['verdict'] == 'BLOCKED'


def _round15_prior(originals: Any, monkeypatch: Any, declared: bool) -> Any:
    from dataclasses import replace

    from tests import shadow_v2_gate_a3_originals as helper
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate

    original, ports, _ = originals
    path, body = 'loading/producer-registrations.json', b'name: review\non: push\n'
    reference = helper.reference
    def renamed(files: Any, name: str, raw: bytes) -> Any:
        return reference(files, path if name == 'ci/review/workflow.yml' else name, raw)
    with monkeypatch.context() as patch:
        patch.setattr(helper, 'reference', renamed)
        raw, ports = helper.opaque_originals(original, ports, body)
    loaded = load(raw, ports, gate='fleet-budgets-pre')
    if declared:
        raw, ports = helper.accounted_originals(loaded, ports, prefix='review-spool/',
                                               registration_path='review/registration.data')
        loaded = load(raw, ports, gate='fleet-budgets-pre')
    assert loaded.a2_streams['loading_issues'] == []
    result = evaluate(loaded, ports, gate='fleet-budgets-pre')
    assert result.verdict == 'PASS'
    output, attachments = render(result)
    archive = [*attachments, *(p.raw for p in loaded.preimages),
               *(row.raw for row in (*loaded.rows, *loaded.attachments)), *loaded.ledgers, loaded.proof]
    current = replace(loaded, pre=output, preimages=(*loaded.preimages,
        *(Preimage('prior-artifacts/' + file_digest(raw), raw) for raw in archive)))
    return raw, ports, loaded, attachments, transport(current), path, body


@pytest.mark.parametrize('declared', [False, True], ids=['source-only', 'explicit-registration'])
def test_round15_prior_byte_only_metadata_name(originals: Any, tmp_path: Path, monkeypatch: Any, declared: bool) -> None:
    from trusted_router import async_settle_shadow_v2_gate_prior as prior
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest

    raw, ports, loaded, attachments, replay, path, body = _round15_prior(originals, monkeypatch, declared)
    (tmp_path / 'initial').mkdir()
    (tmp_path / 'prior').mkdir()
    assert through_all(raw, ports, tmp_path / 'initial', 'fleet-budgets-pre')['verdict'] == 'PASS'
    refs = [ref for item in attachments for value in [json.loads(item)]
            if isinstance(value, dict) and 'keys' in value for ref in value['keys'] if ref['path'] == path]
    assert refs == [dict(path=path, bytes=22, sha256=file_digest(body))]
    assert json.loads(attachments[0])['producer_registrations'] == list(loaded.producer_registrations)
    prior._VERIFIED.clear()
    assert through_all(replay, ports, tmp_path / 'prior', 'fleet-budgets')['verdict'] == 'PASS'


def test_round15_prior_never_decodes_byte_only_original(originals: Any, tmp_path: Path, monkeypatch: Any) -> None:
    from trusted_router import async_settle_shadow_v2_gate_dependencies as dependencies
    from trusted_router import async_settle_shadow_v2_gate_loading as loading
    from trusted_router import async_settle_shadow_v2_gate_payloads as payloads
    from trusted_router import async_settle_shadow_v2_gate_prior as prior

    _, ports, _, _, replay, _, body = _round15_prior(originals, monkeypatch, False)
    attempted = []
    for module in (loading, dependencies, payloads, consumer):
        parser = module.retained_json
        def recorded(raw: bytes, parser: Any = parser) -> Any:
            if raw == body:
                attempted.append(raw)
            return parser(raw)
        monkeypatch.setattr(module, 'retained_json', recorded)
    prior._VERIFIED.clear()
    result = through_all(replay, ports, tmp_path, 'fleet-budgets')
    assert attempted == []
    assert result['verdict'] == 'PASS'


def test_round15_report_registration_field_schema(originals: Any, monkeypatch: Any) -> None:
    _, _, loaded, attachments, _, _, _ = _round15_prior(originals, monkeypatch, True)
    report = json.loads(attachments[0])
    assert report['producer_registrations'] == list(loaded.producer_registrations)
    assert a2.GATE_REPORT.parse(canonical(report)) == report
    ref = report['producer_registrations'][0]
    for value in (None, {}, [42], [ref] * 33, [{'path': ref['path']}], [{**ref, 'bytes': -1}],
                  [{**ref, 'sha256': 'bad'}], [{**ref, 'path': '../registration.json'}]):
        with pytest.raises(ValueError):
            a2.GATE_REPORT.parse(canonical({**report, 'producer_registrations': value}))
