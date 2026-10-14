"""Round five source windows and bounded transport regressions."""
from __future__ import annotations

import copy
import json
import tracemalloc

import pytest

from scripts.async_settle import shadow_v2_window as command
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round5_captures import captured_day
from tests.test_async_settle_shadow_v2_pack import review
from tests.test_async_settle_shadow_v2_pack_window import make_predecessor
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan
from trusted_router.async_settle_shadow_v2_pack_records import RecordSpool, captured_native
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def eight_day_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp('eight-real-days')
    days = [f'2026-10-{n:02d}' for n in range(7, 15)]
    plans = {}
    for day in days:
        plans[day] = captured_day(root / day, day)
        plans[day]['source_root'] = day + '/capture'
    files = PackFiles(root)
    previous = []
    for stage in ('transport', 'fleet-budgets-pre', 'shadow-serving', 'fleet-budgets'):
        previous = [make_predecessor(files, 'prior/' + stage, previous)]
    selected = dict(days=days[:6], day_plans=plans, predecessors=previous,
        checkpoint='shadow-serving', required_stages=plans[days[0]]['required_stages'],
        verifiers=plans[days[0]]['verifiers'])
    plan = dict(default_stage='window', stages=dict(window=selected))
    (root / 'plan.json').write_bytes(canonical(plan))
    return root, plan


def test_real_eight_day_public_window_and_pack(tmp_path, eight_day_sources):
    source, plan = eight_day_sources
    plan = copy.deepcopy(plan)
    selected = plan['stages']['window']
    collected = tmp_path / 'collected'
    result = command.window('collect', plan, source=source, out=collected)
    assert result.get('kind') == 'window', result
    days = sorted(selected['day_plans'])
    assert len(days) == 8 and result['inputs']['days'] == days
    assert len(result['inputs']['predecessors']) == 4
    for day in days:
        child = json.loads((collected / 'day-inputs' / day / 'stage/result.json').read_bytes())
        assert 'gate' in child and child['gate']['verdict'] == 'BLOCKED'
        native = json.loads((collected / 'day-inputs' / day / 'stage/transport.json').read_bytes())
        assert native['days'] == [day]
        assert any('sample' in row['kind'] for row in native['rows'])
    files = PackFiles(collected)
    selected['daily_reviews'] = {day: review(files, ref, 'daily-reviews/' + day)
        for day, ref in result['review']['subjects'].items()}
    reviewed = command.window('review-inputs', plan, source=collected, out=tmp_path / 'review')
    selected['release_plan_sha256'] = reviewed['review']['release_plan']['sha256']
    packed = command.window('pack', plan, source=collected, out=tmp_path / 'pack')
    assert packed['inputs']['days'] == days
    refs = check_plan(PackFiles(tmp_path / 'pack'), packed['inputs'])
    assert all(any(ref['path'] == 'daily-reviews/' + day for ref in refs) for day in days)
    assert (tmp_path / 'pack/review/sealed.json').exists()
    released = json.loads((tmp_path / 'pack/loading/release-plan.json').read_bytes())['artifacts']
    assert all('window/days/' + day + '.json' in released for day in days)
    for day in days:
        child = json.loads((tmp_path / 'pack/window/days' / (day + '.json')).read_bytes())
        assert check_plan(PackFiles(tmp_path / 'pack' / child['root']), child['inputs'])


def test_real_window_missing_support_is_recorded(tmp_path, eight_day_sources):
    source, plan = eight_day_sources
    plan = copy.deepcopy(plan)
    del plan['stages']['window']['day_plans']['2026-10-14']
    result = command.window('collect', plan, source=source, out=tmp_path / 'out')
    assert not result['complete']
    assert 'MissingSupportDay:2026-10-14' in result['missing']
    assert '2026-10-14' in result['inputs']['days']


def test_record_spool_memory_and_repeatable_lookup(tmp_path):
    spool = RecordSpool(tmp_path / 'records')
    tracemalloc.start()
    try:
        for index in range(512):
            spool.append(dict(index=index, body=str(index) + 'x' * 32768), keys=('all',))
        assert sum(len(record['body']) for record in spool.lookup('all')) > 16_000_000
        assert [record['index'] for record in spool.lookup('all')] == list(range(512))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000, peak


def test_native_transport_is_repeatable_and_bounded(tmp_path):
    path = tmp_path / 'native.json'
    with path.open('w') as stream:
        stream.write('{"days":["2026-10-12"],"rows":[')
        for index in range(512):
            if index:
                stream.write(',')
            stream.write(canonical(dict(kind='sample', id=str(index), raw='x' * 32768)).decode())
        stream.write('],"attachments":[],"ledgers":[],"preimages":[]}')
    tracemalloc.start()
    try:
        native = captured_native(path, tmp_path / 'spool')
        assert sum(1 for _ in native['rows']) == 512
        assert sum(1 for _ in native['rows']) == 512
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000, peak


@pytest.mark.parametrize('day', [f'2026-10-{n:02d}' for n in range(7, 15)])
@pytest.mark.parametrize('producer', ['NativeExport', 'Inventory', 'Readiness', 'Shutdown', 'Budget', 'ExternalPack', 'History'])
def test_every_daily_producer_is_required(tmp_path, eight_day_sources, day, producer):
    source, plan = eight_day_sources
    plan = copy.deepcopy(plan)
    ref = plan['stages']['window']['day_plans'][day]['captures'][producer]
    ref['path'] = 'absent/' + ref['path']
    result = command.window('collect', plan, source=source, out=tmp_path / 'out')
    assert not result['complete']
    assert result['missing'] == ['MissingProducer:' + producer]


def test_distinct_idle_and_terminated_original_histories(tmp_path):
    from tests.shadow_v2_pack_round5_captures import lifecycle_capture
    populations = {}
    for state, name in [('idle', 'idle'), ('terminated', 'killed')]:
        original, result, found = lifecycle_capture(tmp_path / state, state)
        assert original[-1]['jsonPayload']['event'] == ('stop' if state == 'idle' else 'kill')
        assert found.complete and not result.missing
        populations[state] = found.boots
        assert len(found.boots) == 1 and found.boots[0]['boot'] == name
        (tmp_path / (state + '.json')).write_bytes(canonical(original))
    assert populations['idle'] != populations['terminated']


def test_corrected_vector_catalog_is_complete():
    from pathlib import Path

    from tests.shadow_v2_pack_round5_vectors import corrected_result
    catalog = json.loads(Path('tests/shadow_v2_pack_round5_corrected_results.json').read_bytes())['cases']
    expected = {'test_real_writer_loader_original_deletion_matrix_is_base_defect:' + str(i) for i in range(17)}
    expected.update(name + ':0' for name in ('test_current_loader_cap_result_is_expected_base_defect',
        'test_cap_survives_split_transport_with_expected_base_result',
        'test_real_writers_native_export_collect_and_pack_loader'))
    assert set(catalog) == expected
    for name in expected:
        test, case = name.rsplit(':', 1)
        value = corrected_result(test, int(case))
        assert value['test'] == catalog[name]['test'] and value['case'] == catalog[name]['case']
        assert value['failures'] and all(f['count'] > 0 and f['subject'] for f in value['failures'])
