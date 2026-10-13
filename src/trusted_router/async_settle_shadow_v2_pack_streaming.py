"""Bounded original-file spool, input pages and captured transport splitting."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS, Files
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, input_plan
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


class OriginalSpool:
    """Keep bytes on disk and sort the finite original index in SQLite."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=False)
        self.root = root
        self.files = Files(root)
        self.db = sqlite3.connect(root / 'index.sqlite')
        self.db.execute('PRAGMA cache_size=-512')
        self.db.execute('CREATE TABLE originals (path TEXT PRIMARY KEY, digest TEXT, size INTEGER, producer TEXT)')

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> OriginalSpool:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def capture(self, source: Files, references: Iterator[dict[str, Any]], *, producer: str) -> int:
        count = 0
        for ref in references:
            self.put(ref['path'], source.read(ref), producer=producer)
            count += 1
        return count

    def put(self, path: str, raw: bytes, *, producer: str) -> dict[str, Any]:
        require(len(raw) <= 262144, 'artifact_gap')
        target = (self.root / path).resolve()
        require(target.is_relative_to(self.root.resolve()) and path != 'index.sqlite', 'artifact_gap')
        old = self.db.execute('SELECT digest, size FROM originals WHERE path=?', (path,)).fetchone()
        digest = hashlib.sha256(raw).hexdigest()
        require(old is None or old == (digest, len(raw)), 'artifact_gap')
        ref = self.files.put(path, raw)
        if old is None:
            self.db.execute('INSERT INTO originals VALUES (?, ?, ?, ?)', (path, digest, len(raw), producer))
        return ref

    def references(self) -> Iterator[dict[str, Any]]:
        for path, digest, size in self.db.execute('SELECT path, digest, size FROM originals ORDER BY path'):
            yield dict(path=path, sha256=digest, bytes=size)

    def leaves(self, out: PackFiles) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        batch: list[dict[str, Any]] = []
        empty = len(canonical(dict(**TAGS, keys=[], complete=True)))
        size = empty

        def emit() -> None:
            nonlocal size
            ref = out.obj(f'inputs/leaves/{len(entries):05d}.json',
                          dict(**TAGS, keys=list(batch), complete=True), a2.INPUT_LEAF)
            entries.append(dict(bundle=ref, first_key=batch[0]['path'], last_key=batch[-1]['path'],
                                objects=len(batch)))
            batch.clear()
            size = empty

        for ref in self.references():
            self.files.read(ref)
            needed = len(canonical(ref))
            if len(batch) == 10000 or size + needed + bool(batch) > a2.INPUT_LEAF.limit:
                emit()
            size += needed + bool(batch)
            batch.append(ref)
        if batch:
            emit()
        return entries

    def validate_leaves(self, out: Files, entries: list[dict[str, Any]]) -> int:
        count, previous = 0, ''
        expected = iter(self.references())
        for entry in entries:
            a2.INPUT_INVENTORY_ENTRY(entry)
            leaf = a2.INPUT_LEAF.parse(out.read(entry['bundle']))
            keys = leaf['keys']
            require(leaf['complete'] and bool(keys), 'artifact_gap')
            require((keys[0]['path'], keys[-1]['path'], len(keys)) ==
                    (entry['first_key'], entry['last_key'], entry['objects']), 'artifact_gap')
            for ref in keys:
                require(ref == next(expected, None) and ref['path'] > previous, 'artifact_gap')
                self.files.read(ref)
                previous = ref['path']
                count += 1
        require(next(expected, None) is None, 'artifact_gap')
        return count


class CapturedJSON:
    """Incremental JSON member reader; one bounded transport child at a time."""

    def __init__(self, source: TextIO):
        self.source = source
        self.buffer = ''
        self.decoder = json.JSONDecoder(object_pairs_hook=self.unique)

    @staticmethod
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            require(key not in value, 'evidence_schema_gap')
            value[key] = item
        return value

    def fill(self) -> bool:
        raw = self.source.read(65536)
        self.buffer += raw
        require(len(self.buffer) <= 2 * 1024 * 1024, 'artifact_gap')
        return bool(raw)

    def char(self) -> str:
        self.buffer = self.buffer.lstrip()
        while not self.buffer:
            require(self.fill(), 'artifact_gap')
            self.buffer = self.buffer.lstrip()
        return self.buffer[0]

    def consume(self, char: str) -> None:
        require(self.char() == char, 'evidence_schema_gap')
        self.buffer = self.buffer[1:]

    def value(self) -> Any:
        self.char()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                require(self.fill(), 'evidence_schema_gap')
                continue
            # Numbers may end at a read boundary, so fetch the next delimiter.
            if end == len(self.buffer) and self.fill():
                continue
            self.buffer = self.buffer[end:]
            return value

    def members(self) -> Iterator[tuple[str, Any]]:
        self.consume('{')
        seen = set()
        while self.char() != '}':
            key = self.value()
            require(type(key) is str and key not in seen, 'evidence_schema_gap')
            seen.add(key)
            self.consume(':')
            if key in ('rows', 'attachments', 'ledgers', 'preimages'):
                self.consume('[')
                while self.char() != ']':
                    yield key, self.value()
                    if self.char() == ']':
                        break
                    self.consume(',')
                    require(self.char() != ']', 'evidence_schema_gap')
                self.consume(']')
            else:
                yield key, self.value()
            if self.char() == '}':
                break
            self.consume(',')
            require(self.char() != '}', 'evidence_schema_gap')
        self.consume('}')
        require(not self.buffer.strip() and not self.source.read(1), 'evidence_schema_gap')


def split_transport(source: Path, out: Path, *, max_objects: int = 9999,
                    max_bytes: int = 262144) -> dict[str, Any]:
    """Split original transport children through the reviewed multi-bundle plan.

    Parts are original-input transport pages, not independently evaluable stages.
    The complete plan remains required at the loader's multi-bundle boundary.
    """
    require(0 < max_objects <= 9999 and 1024 <= max_bytes <= 262144, 'artifact_gap')
    out.mkdir(parents=True, exist_ok=False)
    files = PackFiles(out)
    batch: list[dict[str, Any]] = []
    used, total = 2, 0
    parts: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}

    def emit() -> None:
        nonlocal used
        if batch:
            parts.append(files.put(f'transport/parts/{len(parts):05d}.json', canonical(batch)))
            batch.clear()
            used = 2

    with source.open(encoding='utf-8') as stream:
        for name, value in CapturedJSON(stream).members():
            if name not in ('rows', 'attachments', 'ledgers', 'preimages'):
                require(name in ('v', 'policy_version', 'contract_revision', 'proof', 'transport',
                    'fleet', 'pre', 'serving', 'completed_at_us', 'days', 'window'), 'artifact_gap')
                metadata[name] = value
                continue
            child = dict(member=name, value=value)
            raw = canonical(child)
            if len(raw) + 2 > max_bytes:
                pieces = [files.put(f'transport/children/{total:08d}/{offset // max_bytes:05d}',
                                   raw[offset:offset + max_bytes]) for offset in range(0, len(raw), max_bytes)]
                child = dict(member=name, encoded=pieces)
                raw = canonical(child)
                require(len(raw) + 2 <= max_bytes, 'CapturedTransportChildCapacity')
            if len(batch) == max_objects or used + len(raw) + bool(batch) > max_bytes:
                emit()
            used += len(raw) + bool(batch)
            batch.append(child)
            total += 1
    emit()
    require(metadata.get('policy_version') == 'shadow-v2' and metadata.get('contract_revision') == 'a1',
            'policy_version_gap')
    meta = files.obj('transport/metadata.json', metadata)
    plan = input_plan(files, list(files.refs.values()), days=metadata['days'], predecessors=[])
    result = dict(format='shadow-v2-captured-transport-1', metadata=meta, parts=parts,
                  objects=total, inputs=plan)
    files.obj('transport-plan.json', result)
    return result


def transport_members(files: Files, plan: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    from trusted_router.async_settle_shadow_v2_pack_plan import check_plan
    check_plan(files, plan['inputs'])
    count = 0
    for part in plan['parts']:
        for child in json.loads(files.read(part)):
            if 'encoded' in child:
                raw = b''.join(files.read(ref) for ref in child['encoded'])
                require(len(raw) <= 2 * 1024 * 1024, 'artifact_gap')
                decoded = json.loads(raw)
                require(decoded['member'] == child['member'], 'artifact_gap')
                child = decoded
            require(set(child) == {'member', 'value'}, 'artifact_gap')
            count += 1
            yield child['member'], child['value']
    require(count == plan['objects'], 'artifact_gap')


def restore_transport(files: Files, plan: dict[str, Any], out: Path) -> Path:
    from trusted_router.async_settle_shadow_v2_export import SpoolArray, spool_transport
    out.mkdir(parents=True, exist_ok=False)
    metadata = json.loads(files.read(plan['metadata']))
    arrays = {name: SpoolArray(out / (name + '.jsonl'))
              for name in ('rows', 'attachments', 'ledgers', 'preimages')}
    for name, value in transport_members(files, plan):
        require(name in arrays, 'artifact_gap')
        arrays[name].append(value)
    target = out / 'bundle.json'
    spool_transport(target, {**metadata, **arrays})
    return target
