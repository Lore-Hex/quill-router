"""Recursive production graph copies use real producer and evaluator outputs."""
from __future__ import annotations

import json
import shutil

import pytest

from scripts.async_settle.shadow_v2_window import evaluate_stage
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round7_chain import production_chain
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_predecessors import STAGES, production_predecessors


@pytest.fixture(scope='module')
def chain_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp('production-chain')
    return root, production_chain(root)


def test_production_chain_recursively_recomputed(tmp_path, chain_sources):
    root, refs = chain_sources
    calls = []
    def evaluated(*args):
        calls.append(args[2])
        return evaluate_stage(*args)
    result = production_predecessors(root, PackFiles(tmp_path), [refs[-1]], evaluated)
    assert calls == list(STAGES)
    assert result['missing'] == ['IncompletePredecessor:' + stage for stage in sorted(STAGES)]
    assert not result['complete'] and not result['manifest_complete'] and not result['proof_complete']
    assert len(result['graphs']) == 3
    times = [json.loads((tmp_path / 'graphs' / stage / 'gate/output.json').read_bytes())['completed_at_us']
             for stage in STAGES]
    assert times == sorted(set(times))
    for graph in result['graphs'].values():
        assert graph['gate']['verdict'] == 'BLOCKED'
        assert graph['manifest_complete'] is False and graph['proof_complete'] is False
    pre = (tmp_path / 'graphs/fleet-budgets-pre/proof.pre_budget_sha256').read_bytes()
    warm = (tmp_path / 'graphs/fleet-budgets/proof.pre_budget_sha256').read_bytes()
    assert pre == warm
    from trusted_router.async_settle_shadow_v2_schema import sha256
    post = json.loads((tmp_path / 'graphs/fleet-budgets/proof.post_budget_sha256').read_bytes())
    assert post['pre_envelope_sha256'] == sha256(json.loads(pre))


@pytest.mark.parametrize('stage', STAGES)
def test_each_production_predecessor_graph_missing(tmp_path, chain_sources, stage):
    original, refs = chain_sources
    source = tmp_path / 'source'
    shutil.copytree(original, source)
    shutil.rmtree(source / 'graphs' / stage)
    result = production_predecessors(source, PackFiles(tmp_path / 'out'), [refs[-1]], evaluate_stage)
    assert not result['complete'] and not result['manifest_complete'] and not result['proof_complete']
    name = 'descriptors/' + stage
    assert result['missing'] == sorted(['MissingPredecessorGraph:' + name,
        *('IncompletePredecessor:' + other for other in STAGES if other != stage)])
    assert result['graphs'][name]['manifest_complete'] is False
    assert result['graphs'][name]['proof_complete'] is False


@pytest.fixture(scope='module')
def production_window(tmp_path_factory, chain_sources):
    from tests.shadow_v2_pack_round5_captures import captured_day
    from tests.shadow_v2_pack_round7_chain import link_production_day
    original, refs = chain_sources
    root = tmp_path_factory.mktemp('production-window')
    shutil.copytree(original, root / 'prior')
    files = PackFiles(root)
    # Source roots and descriptor edges are relative to this independent root.
    copied = []
    for ref in refs:
        value = json.loads((original / ref['path']).read_bytes())
        value['source_root'] = 'prior/' + value['source_root']
        value['predecessors'] = list(copied)
        copied.append(files.obj(ref['path'], value))
    days = [f'2026-10-{n:02d}' for n in range(7, 17)]
    plans = {}
    for day in days:
        plan = captured_day(root / day, day, checkpoint='fleet-budgets')
        plans[day] = link_production_day(root / day / 'capture', plan, original)
        plans[day]['source_root'] = day + '/capture'
    bundle = json.loads((root / days[0] / 'writer/native-complete/bundle.json').read_bytes())
    row = max((row for row in bundle['rows'] if 'sample' in row['kind']),
              key=lambda row: json.loads(row['raw'])['observed_at_us'])
    seed = files.put('window/seed', row['raw'].encode())
    start = json.loads(row['raw'])['observed_at_us']
    selected = dict(interval=dict(seed=seed, start_us=start, end_us=start + 7 * 86400000000),
        days=days[:8], day_plans=plans, predecessors=[], production_predecessors=[copied[-1]],
        checkpoint='shadow-serving', required_stages=plans[days[0]]['required_stages'],
        verifiers=plans[days[0]]['verifiers'])
    return root, dict(default_stage='window', stages=dict(window=selected))


def test_short_window_keeps_real_predecessors_and_blocks_coverage(tmp_path, production_window):
    from scripts.async_settle.shadow_v2_window import window
    root, plan = production_window
    result = window('collect', plan, source=root, out=tmp_path / 'out')
    assert result.get('kind') == 'window', result
    assert len(result['inputs']['days']) == 10
    assert len(result['predecessor_results']['graphs']) == 3
    interval = plan['stages']['window']['interval']
    assert interval['start_us'] % 86400000000 > 0
    assert interval['end_us'] - interval['start_us'] == 604800000000
    prior_times = [json.loads((root / 'prior/graphs' / stage / 'gate/output.json').read_bytes())['completed_at_us']
                   for stage in STAGES]
    assert interval['start_us'] > max(prior_times)
    assert result['manifest_complete'] is False and result['proof_complete'] is False
    assert 'WindowWriterCoverage:us-central1' in result['missing']
    assert 'FiniteWindowSchedule' in result['missing']
    assert not (tmp_path / 'out/day-inputs').exists()


@pytest.mark.parametrize('day', ['2026-10-15', '2026-10-16'])
def test_production_window_each_support_missing(tmp_path, production_window, day):
    import copy

    from scripts.async_settle.shadow_v2_window import window
    root, original = production_window
    plan = copy.deepcopy(original)
    del plan['stages']['window']['day_plans'][day]
    result = window('collect', plan, source=root, out=tmp_path / 'out')
    assert result['missing'] == sorted(['MissingLaterFilePlan', 'MissingSupportDay:' + day,
        *('IncompletePredecessor:' + stage for stage in sorted(STAGES))])
    assert result['manifest_complete'] is False and result['proof_complete'] is False
