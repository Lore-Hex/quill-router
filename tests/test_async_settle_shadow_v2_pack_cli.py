"""Offline file commands never overwrite an earlier result."""
import json

from scripts.async_settle.shadow_v2_pack import main
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles


def test_cli_finite_plan_and_no_overwrite(tmp_path, capsys):
    source = PackFiles(tmp_path / 'input')
    ref = source.put('one', b'original')
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps(dict(files=[ref], days=['2026-10-12'], predecessors=[])))
    args = ['plan', '--plan', str(plan), '--input', str(source.root), '--out', str(tmp_path / 'out')]
    assert main(args) == 0
    before = {str(path): path.read_bytes() for path in (tmp_path / 'out').rglob('*') if path.is_file()}
    assert main(args) == 1
    after = {str(path): path.read_bytes() for path in (tmp_path / 'out').rglob('*') if path.is_file()}
    assert before == after
    assert 'FileExistsError' in capsys.readouterr().out


def test_cli_missing_assembly_nonzero(tmp_path, capsys):
    source = PackFiles(tmp_path / 'input')
    ref = source.put('one', b'original')
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps(dict(files=[ref], days=['2026-10-12'], predecessors=[])))
    assert main(['pack', '--plan', str(plan), '--input', str(source.root), '--out', str(tmp_path / 'out')]) == 1
    assert 'StageAssemblyInputs' in capsys.readouterr().out


def test_cli_existing_directory_without_status_untouched(tmp_path):
    plan = tmp_path / 'plan.json'
    plan.write_text('{}')
    out = tmp_path / 'earlier'
    out.mkdir()
    assert main(['pack', '--plan', str(plan), '--input', str(tmp_path), '--out', str(out)]) == 1
    assert not list(out.iterdir())


def test_cli_stage_uses_real_loader_and_never_calls_missing_inputs_pass(tmp_path):
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from scripts.async_settle.shadow_v2_pack import evaluate_captured
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.detached_jws import canonical
    files = PackFiles(tmp_path)
    keys = files.put('loading/certificate-public.json', b'[]')
    binding = files.put('loading/binding-public.json', b'[]')
    ports = shadow_v2_ports(b'[]', b'[]')
    ref = files.obj('loading/release-plan.json', dict(artifacts={r['path']: r['sha256'] for r in (keys, binding)},
        criteria={}, verifiers={a.entry_point: [a.source_pin, a.fixture_pin, file_digest(a.keyset) if a.keys else None]
                               for a in ports.approvals}, required=[]))
    result = evaluate_captured(files, canonical(dict(policy_version='shadow-v2', contract_revision='a1',
        days=['2026-10-12'], completed_at_us=1)), stage='transport', release_sha256=ref['sha256'])
    assert result['complete'] is False
    assert result['missing'][0]['condition'] == 'proof_manifest_absent_or_undecodable'


def test_cli_assembly_requires_original_accounting(tmp_path):
    from scripts.async_settle.shadow_v2_pack import pack_captured
    result = pack_captured(PackFiles(tmp_path), dict(assembly={}))
    assert result == dict(complete=False, missing=['ExportAccountingInputs'])
