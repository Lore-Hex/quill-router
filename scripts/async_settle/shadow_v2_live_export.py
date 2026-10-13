"""Offline captured-store export. --live selects v2 export, never credentials.

Capture: {rows:[{kind,key,raw,commit}],objects:[{key,raw,generation}],
object_keys:[...]} using original UTF-8 strings, not parsed evidence bodies.
A separately reviewed collector produces this capture; native reads are a seam.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from trusted_router.async_settle_shadow_v2_export import (
    MemoryPort,
    OriginalObject,
    OriginalRow,
    export_day,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--completed-at-us', type=int, required=True)
    parser.add_argument('--inputs', type=Path, help='original operator/collector transport members')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--page-size', type=int, default=200)
    args = parser.parse_args()
    capture = json.loads(args.capture.read_bytes())
    rows: dict[str, list[OriginalRow]] = {}
    objects: dict[str, list[OriginalObject]] = {}
    for row in capture['rows']:
        rows.setdefault(row['kind'], []).append(OriginalRow(row['key'], row['raw'].encode(), row['commit']))
    for obj in capture['objects']:
        objects.setdefault(obj['key'], []).append(OriginalObject(obj['key'], obj['raw'].encode(), obj['generation']))
    keys = tuple(capture['object_keys'])
    port = MemoryPort({k: tuple(v) for k, v in rows.items()}, {k: tuple(v) for k, v in objects.items()}, keys, args.page_size)
    result = export_day(port, args.day, completed_at_us=args.completed_at_us, object_keys=keys,
        supplied=json.loads(args.inputs.read_bytes()) if args.inputs else None)
    # Exclusive creation protects earlier passes; a rerun is a new charged pass.
    with args.bundle.open('xb') as target:
        target.write(result.transport)
    directory = args.bundle.with_name(args.bundle.name + '.sources')
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
