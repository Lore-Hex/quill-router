"""Offline window front over captured collection plans and original input files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.async_settle.shadow_v2_collect import collect
from scripts.async_settle.shadow_v2_pack import execute
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_stage import (
    StageAssemblyInputs,
    StreamingStageAssembly,
    validate_capacity,
)
from trusted_router.async_settle_shadow_v2_pack_streaming import split_transport
from trusted_router.async_settle_shadow_v2_schema import require

ACTION_SEAMS = {'trial': 'P6NativeQualification', 'release': 'P6ReviewedRelease', 'probe': 'P6DedicatedJob'}


def window(command: str, plan: dict[str, Any], *, source: Path, out: Path | None,
           stage: str | None = None) -> dict[str, Any]:
    require(command not in ACTION_SEAMS, ACTION_SEAMS.get(command, 'artifact_gap'))
    if command == 'review-inputs' and out is None:
        out = source / 'review-inputs'
    require(out is not None, 'artifact_gap')
    assert out is not None
    if command == 'collect':
        require(stage in ('pre', 'warmup', 'window'), 'artifact_gap')
        selected = plan['stages'][stage]
        out.mkdir(parents=True, exist_ok=False)
        results = []
        for index, capture in enumerate(selected.get('collections', [])):
            # Each C1 call is its existing bounded captured-input entry point.
            results.append(collect(source / capture, out / f'collect-{index:05d}'))
        for index, captured in enumerate(selected.get('transports', [])):
            split_transport(source / captured, out / f'export-{index:05d}')
        registered = StreamingStageAssembly.capture(Files(source), out / 'stage',
                                                    StageAssemblyInputs.parse(selected['inputs']))
        return dict(complete=bool(results) and all(r['complete'] for r in results),
                    input_objects=registered.objects, inventory=registered.inventory,
                    missing=[] if results else ['C1CapturedCollection'])
    if command == 'pack' and 'captured_transport' in plan:
        split = split_transport(source / plan['captured_transport'], out)
        return dict(complete=False, missing=['MultiBundleLoading'], transport_plan='transport-plan.json',
                    input_objects=split['objects'])
    if command == 'pack':
        # The base pack path is bounded before it can materialize original bytes.
        validate_capacity(Files(source), plan['files'])
    require(command in ('review-inputs', 'pack'), 'artifact_gap')
    return execute(command, plan, source, out, release_sha256=plan.get('release_plan_sha256'))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'review-inputs', 'pack', *ACTION_SEAMS))
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--stage', choices=('pre', 'warmup', 'window'))
    parser.add_argument('--phase')
    args = parser.parse_args(argv)
    try:
        result = window(args.command, retained_json(args.plan.read_bytes()),
            source=args.input or args.plan.parent, out=args.out, stage=args.stage)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = dict(complete=False, missing=[str(exc)])
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
