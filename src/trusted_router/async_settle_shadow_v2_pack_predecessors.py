"""Recursive local stage graphs with explicit source roots and original results."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan
from trusted_router.async_settle_shadow_v2_pack_stage import references
from trusted_router.async_settle_shadow_v2_schema import require

STAGES = ('transport', 'fleet-budgets-pre', 'fleet-budgets')


def production_predecessors(source: Path, output: PackFiles, roots: list[dict[str, Any]],
                            evaluate: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Copy and rerun every graph, retaining all incomplete inner results."""
    source_files = Files(source)
    visited: dict[str, dict[str, Any]] = {}
    active: set[str] = set()
    missing: list[str] = []

    def visit(ref: dict[str, Any]) -> None:
        path = ref['path']
        require(path not in active, 'PredecessorCycle')
        if path in visited:
            require(visited[path]['descriptor'] == ref, 'PredecessorConflict')
            return
        active.add(path)
        stage = None
        try:
            graph = retained_json(source_files.read(ref))
            require(graph['format'] == 'shadow-v2-production-predecessor-1', 'PredecessorPlan')
            stage = graph['stage']
            require(stage in STAGES, 'PredecessorStage')
            for child in graph['predecessors']:
                visit(child)
            namespace = graph['source_root']
            root = (source / namespace).resolve()
            require(namespace != '.' and root.is_relative_to(source.resolve()) and root.is_dir(),
                    'MissingPredecessorGraph:' + path)
            original = Files(root)
            members = check_plan(original, graph['inputs'])
            require(all(part in members for part in graph['transport']['parts'])
                    and graph['result'] in members, 'PredecessorPopulation')
            children = {visited[r['path']]['stage'] for r in graph['predecessors'] if r['path'] in visited}
            required = set(STAGES[:STAGES.index(stage)])
            require(required <= children, 'PredecessorStages:' + stage)
            target = output.root / namespace
            copied = PackFiles(target)
            copied.capture(original, list({r['path']: r for r in
                [*members, *references(graph['inputs']), graph['release_plan']]}.values()))
            output.capture(source_files, [ref])
            for name, member in copied.refs.items():
                output.refs[namespace + '/' + name] = {**member, 'path': namespace + '/' + name}
            import hashlib
            transport = target / 'graph-transport.json'
            digest = hashlib.sha256()
            size = 0
            with transport.open('wb') as stream:
                for part in graph['transport']['parts']:
                    raw = copied.read(part)
                    digest.update(raw)
                    size += len(raw)
                    stream.write(raw)
            require(size == graph['transport']['bytes'] and digest.hexdigest() == graph['transport']['sha256'],
                    'PredecessorTransport')
            checked = evaluate(transport, target, stage, graph['release_sha256'])
            from trusted_router import async_settle_shadow_v2_a1_evidence as a1
            registered = {r['path']: r for r in members}
            day = graph['inputs']['days'][0]
            manifest = a1.MANIFEST.parse(copied.read(registered[day + '/manifest-v2.json']))
            proof = a1.PROOF_MANIFEST.parse(copied.read(registered['proof_manifest']))
            require((graph['manifest_complete'], graph['proof_complete']) ==
                    (manifest['complete'], proof['complete']), 'PredecessorCompletion')
            recorded = retained_json(original.read(graph['result']))
            require(checked == recorded, 'PredecessorResult:' + stage)
            complete = not checked['missing'] and checked['verdict'] == 'PASS'
            if not complete:
                missing.append('IncompletePredecessor:' + stage)
            visited[path] = dict(descriptor=ref, stage=stage, complete=complete, gate=checked,
                manifest_complete=graph['manifest_complete'], proof_complete=graph['proof_complete'])
        except (OSError, ValueError, KeyError) as exc:
            missing.append('MissingPredecessorGraph:' + path)
            visited[path] = dict(descriptor=ref, stage=stage, complete=False,
                manifest_complete=False, proof_complete=False, detail=str(exc))
        finally:
            active.remove(path)

    for ref in roots:
        visit(ref)
    require({v['stage'] for v in visited.values() if v['stage']} <= set(STAGES), 'PredecessorStages')
    missing.extend('MissingPredecessorStage:' + stage for stage in STAGES
                   if stage not in {v['stage'] for v in visited.values()})
    return dict(complete=not missing, missing=sorted(set(missing)), graphs=visited,
                manifest_complete=not missing, proof_complete=not missing)
