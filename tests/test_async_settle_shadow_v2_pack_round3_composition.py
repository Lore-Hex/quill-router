"""Source-driven producer joins and command sequence over real writer records."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.async_settle.round3.shadow_v2_window import collect_day
from tests.shadow_v2_pack_round3_captures import collector_capture, writer_capture
from tests.test_async_settle_shadow_v2_pack import DAY, REV, START, review
from tests.test_async_settle_shadow_v2_pack_round3_budget import budget_capture
from tests.test_async_settle_shadow_v2_pack_round3_sources import procedure_capture
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS
from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_procedures import REQUIRED_STAGES, procedure_result


def stage_capture(root, capture):
    files = DayFiles(root)
    captured, pins = collector_capture(files, capture)
    budget = budget_capture(files, capture)
    captured['Budget'] = files.obj('captures/Budget', budget)
    native_root = capture['native_root']
    for path in sorted(native_root.rglob('*')):
        if path.is_file() and path.name not in ('bundle.json', 'rows.jsonl', 'attachments.jsonl'):
            files.put('native-export/' + path.relative_to(native_root).as_posix(), path.read_bytes())
    (root / 'native-export/bundle.json').write_bytes((native_root / 'bundle.json').read_bytes())
    objects = []
    for key, ref in capture['objects'].refs.items():
        original = files.put('writer-objects/' + key, capture['objects'].read(ref))
        objects.append(dict(kind='health_basis' if key.startswith('health-basis-batches/') else 'publisher_trace',
            id=key, original=original))
    captured['NativeExport'] = files.obj('captures/NativeExport', dict(bundle='native-export/bundle.json',
        accounting=files.refs['native-export/accounting.json'], prefix='native-export/', object_plan=capture['object_plan'], objects=[]))
    procedures, approvals = [], {}
    for name in REQUIRED_STAGES['fleet-budgets-pre']:
        item = procedure_capture(files, name, 'fleet-budgets-pre')
        procedure_result(files, item, capture_root=root)
        path = 'external/fleet-budgets-pre/' + name + '/result.json'
        approvals['fleet-budgets-pre/' + name] = review(files, files.refs[path], 'reviews/external/' + name)
        procedures.append(item)
    captured['ExternalPack'] = files.obj('captures/ExternalPack', dict(procedures=procedures, approvals=approvals))
    old = files.obj('old/payload', dict(rows=[], days=[DAY], proof={}, resets=[], serving=[], manifests={}))
    reviewed = files.obj('old/reviewed', dict(rows=[], resets=[], resolutions=[]))
    history = dict(payload=old, source=files.put('old/source', Path('scripts/async_settle/shadow_report.py').read_bytes()),
        dependencies=[], reviewed_history=reviewed, approvals=[review(files, reviewed, 'reviews/history')])
    captured['History'] = files.obj('captures/History', history)
    config = files.obj('cohort/config', dict(workspace_count=1, eligible_only=True, supported_candidates=1,
        fallbacks=False, routes=['chat.completions', 'responses']))
    cohort = files.obj('cohort/policy', dict(**TAGS, workspace_fingerprints=['a'*64],
        regional_seed_policy='each_traffic_region', required_regions=['us-central1'], primary_region='us-central1',
        maximum_publishers=2, configuration=config, approval=review(files, config, 'reviews/cohort')))
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    fixtures = {key: files.put(key, Path(RELEASE_FILES[key]).read_bytes()) for key in contract.FILE_PREIMAGES
                if key in RELEASE_FILES}
    fixtures['manifest.config_sha256'] = pins['config']
    fixtures['manifest.fixture_sha256'] = pins['fixture']
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    ports = shadow_v2_ports(b'[]', b'[]')
    verifiers = {}
    for original in ports.approvals:
        files.put('verifiers/source/' + file_digest(original.source), original.source)
        files.put('verifiers/fixture/' + file_digest(original.fixture), original.fixture)
        files.put('verifiers/keys/' + file_digest(original.keyset), original.keyset)
        verifiers[original.entry_point] = [file_digest(original.source), file_digest(original.fixture),
            file_digest(original.keyset) if original.keys else None]
    # Fresh outputs are recomputed; only captured originals enter this list.
    refs = [ref for path, ref in files.refs.items() if not path.startswith('external/')]
    return dict(day=DAY, captures=captured, files=refs, fixtures=fixtures, cohort=cohort,
        source_shas=dict(router=REV, enclave=REV), complete_through_us=START + 172805000000,
        checkpoint='fleet-budgets-pre', required_stages={s: list(n) for s, n in REQUIRED_STAGES.items()},
        verifiers=verifiers, predecessors=[])


@pytest.fixture(scope='module')
def composed(tmp_path_factory):
    root = tmp_path_factory.mktemp('source-composition')
    capture = writer_capture(root / 'writer')
    plan = stage_capture(root / 'capture', capture)
    return root, plan


def test_source_composition_real_writer_and_collectors(tmp_path, composed):
    root, plan = composed
    output = tmp_path / 'output'
    result = collect_day(root / 'capture', output, plan)
    assert result['complete']
    readiness = json.loads((root / 'capture/captures/Readiness').read_bytes())
    assert 'reader_originals' not in readiness
    assert readiness['proofs']
    bundle = json.loads((output / 'stage/transport.json').read_bytes())
    assert len([r for r in bundle['rows'] if r['id'].endswith('/manifest-v2')]) == 1
    assert any(r['id'].endswith('/cap-v2') for r in bundle['rows'])


def test_missing_producer_cannot_be_replaced_by_recipe(tmp_path):
    import pytest

    from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS
    files = PackFiles(tmp_path / 'inputs')
    ref = files.obj('original', {'fake': 'capture'})
    for omitted in PRODUCERS:
        captures = dict.fromkeys(PRODUCERS, ref)
        del captures[omitted]
        plan = dict(captures=captures, assembly={'supplied': 'recipe'}, files=[ref])
        with pytest.raises(ValueError, match='MissingProducer:' + omitted):
            collect_day(files.root, tmp_path / omitted, plan)
        assert not (tmp_path / omitted / 'bundle.json').exists()


def test_window_command_sequence_from_captures(tmp_path, composed, capsys):
    from scripts.async_settle.round3.shadow_v2_window import main
    from trusted_router.detached_jws import canonical
    root, selected = composed
    selected = dict(selected)
    plan_path = root / 'capture/plan.json'
    plan_path.write_bytes(canonical(dict(stages={'pre': selected})))
    collected = tmp_path / 'collected'
    commands = [
        ['collect', '--plan', str(plan_path), '--stage', 'pre', '--out', str(collected)],
        ['review-inputs', '--plan', str(plan_path), '--stage', 'pre', '--input', str(collected),
            '--out', str(tmp_path / 'review')],
        ['pack', '--plan', str(plan_path), '--stage', 'pre', '--input', str(collected),
            '--out', str(tmp_path / 'pack')]]
    assert main(commands[0]) == 0, capsys.readouterr().out
    assert main(commands[1]) == 1, capsys.readouterr().out
    subjects = json.loads((collected / 'review/subjects.json').read_bytes())['subjects']
    files = PackFiles(collected)
    selected['daily_reviews'] = {day: review(files, ref, 'review/daily-' + day) for day, ref in subjects.items()}
    plan_path.write_bytes(canonical(dict(stages={'pre': selected})))
    assert main(commands[2]) == 0, capsys.readouterr().out
    assert (tmp_path / 'pack/bundle.json').exists()
    (tmp_path / 'commands.json').write_bytes(canonical(commands))


@pytest.mark.parametrize('producer', ['NativeExport', 'Inventory', 'Readiness', 'Shutdown', 'Budget', 'ExternalPack', 'History'])
def test_each_producer_capture_deletion_blocks_pack(tmp_path, composed, producer):
    import copy
    root, original = composed
    plan = copy.deepcopy(original)
    plan['assembly'] = {'supplied': 'old recipe'}
    import shutil
    source = tmp_path / 'capture'
    shutil.copytree(root / 'capture', source)
    (source / plan['captures'][producer]['path']).unlink()
    with pytest.raises(ValueError, match='MissingProducer:' + producer):
        collect_day(source, tmp_path / 'output', plan)
    assert not (tmp_path / 'output/stage/transport.json').exists()


def test_original_byte_matrix_inventory():
    from tests.shadow_v2_pack_round3_matrix import CASE_NAMES
    required = {'middle_range_page', 'last_range_page', 'range_eof', 'entire_kind', 'entire_day',
        'accounting_file', 'original_hash', 'original_bytes', 'object_generation', 'extra_native_rpc', 'extra_rerun',
        'idle_boot', 'killed_boot', 'false_shutdown_zero', 'pre_budget', 'post_budget', 'cap_only_manifest',
        'resolution_support', 'new_booking_region', 'required_criterion_moved', 'deferred_name_removed',
        'earlier_stage_result'}
    criteria = ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys')
    sources = {'transport': ('ci', 'regional_trial'), 'size': ('ci', 'sizing_trial'),
        'regional_load': ('budget_trial',), 'boot_transport': ('regional_trial',), 'observer_freshness': (),
        'marker_keys': ('ci',)}
    required.update('criterion_' + c for c in criteria)
    required.update('source_' + c + '_' + s for c, values in sources.items() for s in values)
    required.update('seed_' + c + '_' + edge for c in (*criteria, 'stage_transport', 'fleet_budgets_pre', 'fleet_budgets')
                    for edge in ('before', 'at'))
    assert set(CASE_NAMES) == required


@pytest.fixture(scope='module')
def assembled(composed):
    from tests.shadow_v2_pack_round3_matrix import stage_ports
    root, plan = composed
    output = root / 'matrix-graph'
    collect_day(root / 'capture', output, plan)
    original = json.loads((output / 'stage/transport.json').read_bytes())
    return original, stage_ports(original)


from tests.shadow_v2_pack_round3_matrix import CASE_NAMES  # noqa: E402


@pytest.mark.parametrize('case', CASE_NAMES)
def test_composed_original_byte_matrix_base_defect(tmp_path, assembled, case):
    from tests.shadow_v2_pack_round3_matrix import negative_bundle, requested_stage
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    from trusted_router.detached_jws import canonical
    original, ports = assembled
    raw = canonical(negative_bundle(original, case))
    (tmp_path / (case + '.json')).write_bytes(raw)
    loaded = load(raw, ports)
    result = evaluate(loaded, ports, gate=requested_stage(case))
    rendered = render(result)
    actual = dict(loading=loaded.a2_streams['loading_issues'], gaps=list(result.gaps),
        failures=[dict(gap=f.gap, rule=f.rule, subject=f.subject, count=f.count, days=list(f.days))
                  for f in result.failures], counts=result.gate_report['failure_counts'] if result.gate_report else None,
        unavailable=rendered.diagnostic() if isinstance(rendered, UnavailableGateOutput) else None)
    expected = json.loads(Path('tests/shadow_v2_pack_round3_base_results.json').read_bytes())
    # BASE DEFECT BLOCK: this is today's exact rendered result, including the
    # unavailable cap-only report. Adopt corrected-loader expectations deliberately.
    # Cap-only correction must retain the supplied proof and report the absent day
    # manifest at its original day/key; it must not lose all subjects at container.
    # D1 must consume original compared sample/receipt joins; no new file is made.
    # G1/G7 must consume original range/accounting changes; stage projections must
    # preserve pre/trial scope. Binary archive parts remain in the input plan.
    assert actual == expected['vectors'][expected['cases'][case]]


def test_composer_missing_producer_named(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_composition import compose_day
    from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS
    for omitted in PRODUCERS:
        producers = {name: {'supplied_recipe': True} for name in PRODUCERS if name != omitted}
        with pytest.raises(ValueError, match='MissingProducer:' + omitted):
            compose_day(PackFiles(tmp_path), day=DAY, producers=producers, fixtures={}, cohort={},
                source_shas=dict(router=REV, enclave=REV), complete_through_us=START)


def test_recipe_cannot_supply_omitted_capture(tmp_path):
    import copy
    capture = writer_capture(tmp_path / 'writer')
    source = tmp_path / 'captures'
    plan = stage_capture(source, capture)
    removed = plan['captures'].pop('Budget')
    plan['assembly'] = dict(captures={'Budget': copy.deepcopy(removed)})
    with pytest.raises(ValueError, match='MissingProducer:Budget'):
        collect_day(source, tmp_path / 'output', plan)
    assert not (tmp_path / 'output/stage/transport.json').exists()
