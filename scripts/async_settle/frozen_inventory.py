"""Regenerate the frozen execution inventory from one or more oracle output directories."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / 'tests/fakes/frozen_main'
DOCUMENT = ROOT / 'docs/async-settle-frozen-main-inventory.md'
SCOPE = ('Guarded frozen setup and HTTP/drain/state-capture executions in PR G CPython 3.12.3 '
         'full proof_oracle runs, default and post-cutover clocks (union of eight worker JSON files); negative controls excluded')


def render(data: dict) -> str:
    lines = [
        '# Frozen-main execution inventory', '',
        f"Observed inventory: **{data['modules']} modules, {data['callables']} distinct qualified names, "
        f"{data['execution_entries']} execution entries**.", '',
        f"BASE: `{data['baseline']}` plus the source changes in `worktree-pins.json`. CPython 3.12.3.", '',
        data['scope'] + '. This inventory is evidence, **not an allowlist**. Fixture seed preparation is excluded; '
        'module/class bodies and comprehensions are included. No observed live router call is permitted.', '',
        f"Archive SHA-256: `{data['archive_sha256']}`.", '',
        '[Machine-readable records](../tests/fakes/frozen_main/execution-inventory.json) · '
        '[all file pins](../tests/fakes/frozen_main/pins.json) · '
        '[regeneration instructions](../tests/fakes/frozen_main/README.md) · '
        '[guard coverage and scope](design/async-settle-outbox-v1.md#frozen-main-coverage)', '',
        '`Code line` means `co_firstlineno`; generated dataclass methods use their generated code line '
        'and are attributed to the owning class and pinned source file. Standard-library/third-party machinery '
        'and the shared fake IO engine are outside this router inventory.', '',
    ]
    module = None
    for row in data['records']:
        if row['module'] != module:
            module = row['module']
            lines.extend(['', f'## `{module}`', '',
                          f"Frozen alias: `{row['frozen_module']}`. Member: `{row['frozen_member']}`.", '',
                          f"SHA-256: `{row['sha256']}`.", '',
                          '| Callable / executed code unit | Code line |', '|---|---|'])
        lines.append(f"| `{row['qualname']}` | {row['code_firstlineno']} |")
        # Blank lines between table rows are intentionally omitted.
    return '\n'.join(lines) + '\n'


def check_inventory() -> None:
    data = json.loads((SNAPSHOT / 'execution-inventory.json').read_text())
    pins = json.loads((SNAPSHOT / 'pins.json').read_text())
    assert data['baseline'] == (SNAPSHOT / 'BASE').read_text().strip()
    assert data['archive_sha256'] == hashlib.sha256((SNAPSHOT / 'package.tar.gz').read_bytes()).hexdigest()
    assert all(pins[row['frozen_member']] == row['sha256'] for row in data['records'])
    assert data['modules'] == len({r['module'] for r in data['records']})
    assert data['callables'] == len({(r['module'], r['qualname']) for r in data['records']})
    assert data['execution_entries'] == len(data['records'])
    assert DOCUMENT.read_text() == render(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', type=Path, nargs='+')
    args = parser.parse_args()
    observed = {tuple(row) for directory in args.directories for path in directory.glob('*.json')
                for row in json.loads(path.read_text())}
    assert observed, 'No execution evidence found'
    records = [dict(module=module, qualname=qualname, code_firstlineno=line,
                    frozen_module=module.replace('trusted_router', 'frozen_main', 1),
                    frozen_member=member, sha256=digest)
               for module, qualname, line, member, digest in sorted(observed)]
    data = dict(baseline=(SNAPSHOT / 'BASE').read_text().strip(),
                archive='tests/fakes/frozen_main/package.tar.gz',
                archive_sha256=hashlib.sha256((SNAPSHOT / 'package.tar.gz').read_bytes()).hexdigest(),
                scope=SCOPE, modules=len({r['module'] for r in records}),
                execution_entries=len(records), callables=len({(r['module'], r['qualname']) for r in records}),
                records=records)
    (SNAPSHOT / 'execution-inventory.json').write_text(json.dumps(data, indent=2) + '\n')
    DOCUMENT.write_text(render(data))
    check_inventory()
    print(f"Inventory: {data['modules']} modules, {data['callables']} qualified names, {len(records)} entries")


if __name__ == '__main__':
    main()
