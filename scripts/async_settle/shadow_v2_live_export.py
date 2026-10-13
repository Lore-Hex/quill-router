"""Dormant read-only native export, or an explicitly supplied offline capture.

Native object plan: a JSON array of {key, generation}, generation may be null on
its first GET. A returned generation is pinned for subsequent reads. No LIST.
Each pass requires a fresh bundle path; retained failed passes are charged too.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_export import (
    DAY_KINDS,
    MemoryPort,
    OriginalObject,
    OriginalRow,
    bounds,
    export_day,
)


def native_io() -> Any:
    """Release-only wiring; no import of the exporter from a runtime module."""
    from trusted_router.async_settle_shadow_v2_export_native import NativeIO
    from trusted_router.storage_gcp_shadow_attachments import AuthRequest, Lifetime
    from trusted_router.storage_gcp_shadow_io import (
        configure,
        observe_spanner_rpcs,
        owned_database,
        rpc_scope,
        timestamp_ns,
    )
    return NativeIO(owned_database, configure, rpc_scope, observe_spanner_rpcs,
                    timestamp_ns, Lifetime, AuthRequest)


def live_port(args: Any, plan: dict[str, str | None]) -> Any:
    # Imported/constructed only after explicit resource and read-only checks.
    from google.cloud.spanner_v1 import Client

    from trusted_router.async_settle_shadow_v2_export_native import NativePort
    database = Client(project=args.project, disable_builtin_metrics=True).instance(args.instance).database(args.database)
    return NativePort(database, args.bucket, plan, io=native_io(), page_size=args.page_size,
        resources={name: getattr(args, name) for name in ("project", "instance", "database", "bucket")})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--project')
    parser.add_argument('--instance')
    parser.add_argument('--database')
    parser.add_argument('--bucket')
    parser.add_argument('--read-only-confirmed', action='store_true')
    parser.add_argument('--object-plan', type=Path)
    parser.add_argument('--day', required=True)
    parser.add_argument('--completed-at-us', type=int, required=True)
    parser.add_argument('--inputs', type=Path, help='original operator/collector transport members')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--page-size', type=int, default=200)
    args = parser.parse_args()
    low, high = bounds(args.day)
    if not 1 <= args.page_size <= 200:
        parser.error('--page-size must be 1..200')
    if args.bundle.exists():
        raise FileExistsError(args.bundle)
    native = args.capture is None
    if native:
        if not all((args.project, args.instance, args.database, args.bucket, args.object_plan)):
            parser.error('--live native reads require --project --instance --database --bucket --object-plan')
        if not args.read_only_confirmed:
            parser.error('--live native reads require --read-only-confirmed')
        declared = json.loads(args.object_plan.read_bytes())
        plan = {item['key']: item['generation'] for item in declared}
        if len(plan) != len(declared) or len(plan) > 10000:
            parser.error('object plan must contain unique keys, at most 10000')
        keys = tuple(plan)
        print(json.dumps(dict(planned_ranges=[dict(kind=k, start=low, end=high) for k in DAY_KINDS],
                              object_keys_count=len(keys))), flush=True)
        port = live_port(args, plan)
    else:
        capture = json.loads(args.capture.read_bytes())
        rows: dict[str, list[OriginalRow]] = {}
        objects: dict[str, list[OriginalObject]] = {}
        for row in capture['rows']:
            rows.setdefault(row['kind'], []).append(OriginalRow(row['key'], row['raw'].encode(), row['commit']))
        for obj in capture['objects']:
            objects.setdefault(obj['key'], []).append(OriginalObject(obj['key'], obj['raw'].encode(), obj['generation']))
        keys = tuple(capture['object_keys'])
        port = MemoryPort({k: tuple(v) for k, v in rows.items()}, {k: tuple(v) for k, v in objects.items()}, keys, args.page_size)
    directory = args.bundle.with_name(args.bundle.name + '.sources')
    result = export_day(port, args.day, completed_at_us=args.completed_at_us, object_keys=keys,
        supplied=json.loads(args.inputs.read_bytes()) if args.inputs else None,
        spool_dir=directory if native else None)
    with args.bundle.open('xb') as target:
        if result.transport_path:
            with result.transport_path.open('rb') as source:
                shutil.copyfileobj(source, target, length=65536)
        else:
            target.write(result.transport)
    if not native:
        directory.mkdir()
        for name, raw in result.originals:
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            with (directory / name).open('xb') as target:
                target.write(raw)
        (directory / 'accounting.json').write_bytes(result.accounting)
    print(json.dumps(dict(bundle=str(args.bundle), export_complete=not result.gaps, gaps=result.gaps)))
    return int(bool(result.gaps))


if __name__ == '__main__':
    raise SystemExit(main())
