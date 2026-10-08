"""Rebuild the frozen legacy golden from Git objects, without any Git writes.

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


def derive(base: str) -> dict[str, bytes]:
    commit = git('rev-parse', '--verify', f'{base}^{{commit}}').decode().strip()
    paths = git('ls-tree', '-r', '--name-only', commit, '--', 'src/trusted_router')
    pins = {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(paths.decode().splitlines()):
            if Path(path).suffix not in SUFFIXES:
                continue
            data = git('show', f'{commit}:{path}')
            pins[path] = hashlib.sha256(data).hexdigest()
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
    return {'BASE': (commit + '\n').encode(),
            'pins.json': (json.dumps(pins, sort_keys=True, indent=2) + '\n').encode(),
            'package.tar.gz': compressed.getvalue()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--base', help='Commit whose Git objects become the frozen reference')
    mode.add_argument('--check', action='store_true', help='Re-derive BASE and compare every byte')
    args = parser.parse_args()
    files = derive((SNAPSHOT / 'BASE').read_text().strip() if args.check else args.base)
    if args.check:
        different = [name for name, data in files.items()
                     if not (SNAPSHOT / name).is_file() or (SNAPSHOT / name).read_bytes() != data]
        if different:
            parser.exit(1, 'Frozen reference differs: ' + ', '.join(different) + '\n')
    else:
        SNAPSHOT.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (SNAPSHOT / name).write_bytes(data)
    print('Archive SHA-256: ' + hashlib.sha256(files['package.tar.gz']).hexdigest())
    print('Reference matches BASE byte-for-byte.' if args.check
          else 'Paste the archive SHA-256 into tests/fakes/frozen_package.py; regenerate execution inventory.')


if __name__ == '__main__':
    main()
