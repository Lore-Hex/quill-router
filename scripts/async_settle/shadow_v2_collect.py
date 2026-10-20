"""Explicit offline A3-C1 collection over captured files; no native client mode."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_collect_inventory as inventory
from trusted_router import async_settle_shadow_v2_collect_readiness as readiness
from trusted_router import async_settle_shadow_v2_collect_shutdown as shutdown
from trusted_router.async_settle_shadow_v2_collect_ports import CapturedPort, Files, Pass, Query
from trusted_router.async_settle_shadow_v2_schema import decode, require


def collect(capture: Path, out: Path) -> dict[str, Any]:
    with capture.open('rb') as source:
        plan = decode(source.read(262145), 262144, retained_strings=True)
    require(not out.exists(), 'artifact_gap')
    out.mkdir(parents=True)
    source_files, files = Files(capture.parent), Files(out)
    require(len(plan['files']) <= 10_000)
    for ref in plan['files']:
        files.put(ref['path'], source_files.read(ref))
    captures = {}
    for call in plan['calls']:
        key = call['source'], call['resource'], call['day'], call['token']
        require(key not in captures)
        captures[key] = call
    port = CapturedPort(files, captures)
    run = Pass(files, limit=plan.get('records_per_pass', 10_000))
    queries = {key: Query(**value) for key, value in plan['queries'].items()}
    inv = inventory.collect(run, platform=port, logs=port, monitoring=port,
        queries={k: queries[k] for k in ('platform', 'lifecycle', 'startup', 'monitoring')},
        binding_ref=plan['binding'], review=plan.get('inventory_review'))
    ready = readiness.collect(run, inventory=inv, ci=port, logs=port,
        artifact_query=queries['artifacts'], startup_query=queries['readiness'], pins=plan['pins'],
        previous=plan.get('previous'))
    shut = shutdown.collect(run, inventory=inv, logs=port, query=queries['shutdown'],
        originals=plan.get('shutdown_originals', {}), review=plan.get('shutdown_review'))
    status = run.status('collection-status.json')
    result = dict(inventory=asdict(inv), readiness=asdict(ready), shutdown=asdict(shut), status=status,
                  complete=not run.missing and inv.envelope is not None and shut.envelope is not None)
    files.obj('collection.json', result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True, help='Bounded plan; paths relative to this file')
    parser.add_argument('--out', type=Path, required=True, help='New immutable output directory')
    args = parser.parse_args()
    result = collect(args.capture, args.out)
    print('complete' if result['complete'] else 'BLOCKED: see collection-status.json and review subjects')
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
