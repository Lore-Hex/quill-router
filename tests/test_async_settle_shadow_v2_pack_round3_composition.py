"""Source-driven producer joins and command sequence over real writer records."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.async_settle.round3.shadow_v2_window import collect_day
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round3_captures import collector_capture, writer_capture
from tests.test_async_settle_shadow_v2_pack import DAY, REV, START, review
from tests.test_async_settle_shadow_v2_pack_round3_budget import budget_capture
from tests.test_async_settle_shadow_v2_pack_round3_sources import procedure_capture
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS
from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_procedures import REQUIRED_STAGES, procedure_result


def stage_capture(root, capture, *, checkpoint='fleet-budgets-pre', budget_prefix=''):
    files = DayFiles(root)
    captured, pins = collector_capture(files, capture, native_prefix=budget_prefix + 'native/')
    from trusted_router.async_settle_shadow_v2_pack_originals import ScopedOutputs
    budget_files = ScopedOutputs(root, refs=files.refs, prefix=budget_prefix) if budget_prefix else files
    budget = budget_capture(budget_files, capture)
    if budget_prefix:
        budget['output_prefix'] = budget_prefix + 'generated/'
    for name in ('pre_allowances', 'post_allowances'):
        allowance = json.loads(files.read(budget[name]))
        allowance.update(export_bytes=10000000, export_write_bytes=10000000, export_operations=10000)
        budget[name] = budget_files.obj('trial/composed-' + name, allowance)
    closed = json.loads(files.read(captured['Shutdown']))
    if budget_prefix:
        captured['Shutdown'] = files.obj(budget_prefix + 'captures/Shutdown', closed)
    budget['shutdown'] = [dict(measurement=entry['measurement'],
        original=files.refs[budget_prefix + 'shutdown/' + entry['day'] + '/' + entry['boot'] + '/span-original.json'])
        for entry in closed['entries']]
    trial = json.loads(files.read(budget['trial_record']))
    trial.update(shutdown=budget['shutdown'], shutdown_inventory=captured['Shutdown'])
    lifecycle = json.loads(files.read(trial['lifecycle_work']))
    lifecycle['shutdown'] = budget['shutdown']
    trial['lifecycle_work'] = budget_files.obj('trial/composed-lifecycle', lifecycle)
    budget['trial_record'] = budget_files.obj('trial/composed-record', trial)
    budget['trial_review'] = review(budget_files, budget['trial_record'], 'reviews/composed-trial')
    values = json.loads(files.read(budget['pre_allowances']))
    budget['export_ceilings'] = budget_files.obj('trial/export-ceilings', dict(native_rpcs=10000, **{k: values[k]
        for k in ('export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')}))
    budget['export_review'] = review(budget_files, budget['export_ceilings'], 'reviews/export-ceilings')
    native_root = capture['native_root']
    native_prefix = budget_prefix + 'native-export/'
    for path in sorted(native_root.rglob('*')):
        if path.is_file() and path.name not in ('bundle.json', 'rows.jsonl', 'attachments.jsonl'):
            files.put(native_prefix + path.relative_to(native_root).as_posix(), path.read_bytes())
    (root / native_prefix / 'bundle.json').write_bytes((native_root / 'bundle.json').read_bytes())
    objects = []
    for key, ref in capture['objects'].refs.items():
        original = files.put('writer-objects/' + key, capture['objects'].read(ref))
        objects.append(dict(kind='health_basis' if key.startswith('health-basis-batches/') else 'publisher_trace',
            id=key, original=original))
    passed = dict(id='initial', bundle=native_prefix + 'bundle.json',
        accounting=files.refs[native_prefix + 'accounting.json'], prefix=native_prefix, object_plan=capture['object_plan'])
    from tests.shadow_v2_pack_round8_work import timed_export
    timed_export(budget_files, budget, passed)
    captured['Budget'] = files.obj('captures/Budget', budget)
    captured['NativeExport'] = files.obj('captures/NativeExport', dict(**passed, passes=[passed], output_receipts=budget['output_receipts'], objects=[]))
    from trusted_router.async_settle_shadow_v2_pack_daypack import bind_accounting
    inventory_source = json.loads(files.read(captured['Inventory']))
    bound = bind_accounting(files, json.loads(files.read(inventory_source['deployment'])),
        day=DAY, accounting=[passed['accounting']])
    bound_ref = files.obj(DAY + '/deployment-with-accounting.json', bound)
    bound_review = review(files, bound_ref, 'reviews/deployment-accounting')
    procedures, approvals = [], {}
    reached = ('transport', 'fleet-budgets-pre', 'fleet-budgets') if checkpoint == 'fleet-budgets' else (checkpoint,)
    for procedure_stage in reached:
        for name in REQUIRED_STAGES[procedure_stage]:
            item = procedure_capture(files, name, procedure_stage)
            procedure_result(files, item, capture_root=root)
            path = 'external/' + procedure_stage + '/' + name + '/result.json'
            review_path = 'reviews/external/' + (procedure_stage + '/' if len(reached) > 1 else '') + name
            approvals[procedure_stage + '/' + name] = review(files, files.refs[path], review_path)
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
    completion = files.obj('captures/completion', dict(command='collect', complete=True,
        completed_at_us=START + 86400000010))
    # Fresh outputs are recomputed; only captured originals enter this list.
    refs = [ref for path, ref in files.refs.items() if not path.startswith('external/')]
    return dict(day=DAY, days=[DAY], completion=completion, pass_ids=['initial'], deployment_accounting_review=bound_review, captures=captured, files=refs, fixtures=fixtures, cohort=cohort,
        source_shas=dict(router=REV, enclave=REV), complete_through_us=START + 172805000000,
        checkpoint=checkpoint, required_stages={s: list(n) for s, n in REQUIRED_STAGES.items()},
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
    assert not result['complete']
    assert 'IncompleteDay:' + DAY in result['missing']
    assert result['gate']['verdict'] == 'BLOCKED'
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
        ['review-inputs', '--plan', str(plan_path), '--input', str(collected)],
        ['pack', '--plan', str(plan_path), '--input', str(collected),
            '--out', str(tmp_path / 'pack')]]
    assert main(commands[0]) == 1, capsys.readouterr().out
    assert main(commands[1]) == 1, capsys.readouterr().out
    subjects = json.loads((collected / 'review/subjects.json').read_bytes())['subjects']
    files = PackFiles(collected)
    selected['pack_completion'] = files.obj('captures/pack-completion', dict(command='pack', complete=True, completed_at_us=START + 86400000011))
    selected['daily_reviews'] = {day: review(files, ref, 'review/daily-' + day) for day, ref in subjects.items()}
    plan_path.write_bytes(canonical(dict(stages={'pre': selected})))
    # The independent reviewer uses the final release list, including daily records.
    assert main(['review-inputs', '--plan', str(plan_path), '--input', str(collected),
                 '--out', str(tmp_path / 'final-review')]) == 1
    import hashlib
    selected['release_plan_sha256'] = hashlib.sha256(
        (tmp_path / 'final-review/loading/release-plan.json').read_bytes()).hexdigest()
    plan_path.write_bytes(canonical(dict(stages={'pre': selected})))
    assert main(commands[2]) == 1, capsys.readouterr().out
    from trusted_router.async_settle_shadow_v2_pack_plan import check_plan
    output = PackFiles(tmp_path / 'pack')
    result = json.loads((output.root / 'result.json').read_bytes())
    refs = {r['path'] for r in check_plan(output, result['inputs'])}
    final_bundle = json.loads((output.root / 'bundle.json').read_bytes())
    collected_bundle = json.loads((collected / 'stage/transport.json').read_bytes())
    assert final_bundle['completed_at_us'] == START + 86400000011
    assert final_bundle['completed_at_us'] > collected_bundle['completed_at_us']
    bundle_names = {r['name'] for r in final_bundle['preimages']}
    released = json.loads((output.root / 'loading/release-plan.json').read_bytes())['artifacts']
    for decision in selected['daily_reviews'].values():
        path = decision['record']['path']
        assert path in refs and path in bundle_names and path in released
    assert (output.root / 'review/sealed.json').exists()
    assert not result['complete'] and 'IncompleteDay:' + DAY in result['missing']

    assert (tmp_path / 'pack/bundle.json').exists()
    assert json.loads((tmp_path / 'pack/bundle.json').read_bytes())['completed_at_us'] == START + 86400000011
    assert not any('shutdown_inventory' in str(issue) and 'schema' in str(issue)
        for issue in result['gate']['loading'])
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
    expected = json.loads(Path('tests/shadow_v2_pack_round8_base_results.json').read_bytes())
    # Exact current results after generated transport records and aligned launch times.
    # The projection-page mismatch remains an existing loader result.
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
