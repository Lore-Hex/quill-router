"""Rebuild the frozen legacy golden from Git objects or local source, without Git writes.

Run --base COMMIT to re-pin, or --check to verify the committed BASE locally.
CI's shallow checkout cannot run --check: it need not contain the BASE objects.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / 'tests/fakes/frozen_main'
SUFFIXES = {'.py', '.json', '.jsonl', '.html', '.txt', '.sql', '.css', '.js'}


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', *args], cwd=ROOT)  # noqa: S603, S607 - read-only Git commands


def derive(base: str, *, worktree: bool = False) -> dict[str, bytes]:
    commit = git('rev-parse', '--verify', f'{base}^{{commit}}').decode().strip()
    paths = git('ls-tree', '-r', '--name-only', commit, '--', 'src/trusted_router')
    pins = {}
    changes = {}
    selected = set(paths.decode().splitlines())
    if worktree:
        selected.update(str(path.relative_to(ROOT)) for path in (ROOT / 'src/trusted_router').rglob('*')
                        if path.is_file() and '__pycache__' not in path.parts)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(selected):
            if Path(path).suffix not in SUFFIXES:
                continue
            baseline = git('show', f'{commit}:{path}') if path in paths.decode().splitlines() else None
            if worktree and not (ROOT / path).is_file():
                changes[path] = None
                continue
            data = (ROOT / path).read_bytes() if worktree else baseline
            assert data is not None
            pins[path] = hashlib.sha256(data).hexdigest()
            if worktree and data != baseline:
                changes[path] = pins[path]
            member = tarfile.TarInfo(path)
            member.size = len(data)
            member.mode = 0o644
            member.mtime = member.uid = member.gid = 0
            member.uname = member.gname = ''
            archive.addfile(member, io.BytesIO(data))
    compressed = io.BytesIO()
    # GzipFile fixes the header OS byte across Python versions as well as time.
    with gzip.GzipFile(fileobj=compressed, mode='wb', filename='', mtime=0) as stream:
        stream.write(buffer.getvalue())
    files = {'BASE': (commit + '\n').encode(),
            'pins.json': (json.dumps(pins, sort_keys=True, indent=2) + '\n').encode(),
            'package.tar.gz': compressed.getvalue()}
    if worktree:
        files['worktree-pins.json'] = (json.dumps(changes, sort_keys=True, indent=2) + '\n').encode()
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--base', help='Commit whose Git objects become the frozen reference')
    mode.add_argument('--check', action='store_true', help='Re-derive BASE and compare every byte')
    parser.add_argument('--worktree', action='store_true', help='Freeze local source against BASE without Git writes')
    args = parser.parse_args()
    worktree = args.worktree or (args.check and (SNAPSHOT / 'worktree-pins.json').exists())
    files = derive((SNAPSHOT / 'BASE').read_text().strip() if args.check else args.base, worktree=worktree)
    if args.check:
        different = [name for name, data in files.items()
                     if not (SNAPSHOT / name).is_file() or (SNAPSHOT / name).read_bytes() != data]
        if different:
            parser.exit(1, 'Frozen reference differs: ' + ', '.join(different) + '\n')
        from frozen_inventory import check_inventory

        check_inventory()
    else:
        SNAPSHOT.mkdir(parents=True, exist_ok=True)
        if not worktree:
            (SNAPSHOT / 'worktree-pins.json').unlink(missing_ok=True)
        for name, data in files.items():
            (SNAPSHOT / name).write_bytes(data)
    print('Archive SHA-256: ' + hashlib.sha256(files['package.tar.gz']).hexdigest())
    print('Reference matches ' + ('worktree against BASE' if worktree else 'BASE') + ' byte-for-byte.' if args.check
          else 'Paste the archive SHA-256 into tests/fakes/frozen_package.py; regenerate execution inventory.')


if __name__ == '__main__':
    main()
