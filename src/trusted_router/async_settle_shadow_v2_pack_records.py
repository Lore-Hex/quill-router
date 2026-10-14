"""Repeatable captured record streams backed by the original-file spool."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON, OriginalSpool
from trusted_router.detached_jws import canonical


class RecordSpool:
    """Retain record bodies and lookup lists on disk, with one record per read."""

    def __init__(self, root: Path):
        self.originals = OriginalSpool(root)
        self.root = root
        self.count = 0

    def append(self, value: Any, *, keys: tuple[str, ...] = ()) -> None:
        path = f'records/{self.count:012d}'
        self.originals.put(path, canonical(value), producer='composition')
        for key in keys:
            index = self.root / ('lookup-' + hashlib.sha256(key.encode()).hexdigest())
            with index.open('a') as stream:
                stream.write(path + '\n')
        self.count += 1

    def __iter__(self) -> Iterator[Any]:
        for index in range(self.count):
            yield json.loads((self.root / f'records/{index:012d}').read_bytes())

    def lookup(self, key: str) -> Iterator[Any]:
        index = self.root / ('lookup-' + hashlib.sha256(key.encode()).hexdigest())
        if index.exists():
            with index.open() as stream:
                for path in stream:
                    yield json.loads((self.root / path.rstrip('\n')).read_bytes())


def captured_native(path: Path, root: Path) -> dict[str, Any]:
    """Read transport arrays without retaining their bodies in the producer."""
    arrays = {name: RecordSpool(root / name) for name in ('rows', 'attachments', 'ledgers', 'preimages')}
    metadata = {}
    with path.open() as stream:
        for name, value in CapturedJSON(stream).members():
            if name in arrays:
                arrays[name].append(value)
            else:
                metadata[name] = value
    return {**metadata, **arrays}


class RecordMap:
    """A disk key index over small JSON records; iteration reads one value."""

    def __init__(self, root: Path):
        self.records = RecordSpool(root)
        self.root = root
        self.names = root / '.map-keys'
        self.names.touch()

    def _path(self, key: Any) -> Path:
        return self.root / ('.map-' + hashlib.sha256(canonical(key)).hexdigest())

    def __contains__(self, key: Any) -> bool:
        return self._path(key).exists()

    def __setitem__(self, key: Any, value: Any) -> None:
        target = self._path(key)
        if not target.exists():
            with self.names.open('a') as names:
                names.write(canonical(key).decode() + '\n')
        index = self.records.count
        self.records.append(value)
        target.write_text(str(index))

    def __getitem__(self, key: Any) -> Any:
        target = self._path(key)
        if not target.exists():
            raise KeyError(key)
        index = int(target.read_text())
        return json.loads((self.root / f'records/{index:012d}').read_bytes())

    def get(self, key: Any, default: Any = None) -> Any:
        return self[key] if key in self else default

    def items(self) -> Iterator[tuple[Any, Any]]:
        with self.names.open() as names:
            for line in names:
                key = json.loads(line)
                yield tuple(key) if isinstance(key, list) else key, self[key]

    def values(self) -> Iterator[Any]:
        for _, value in self.items():
            yield value


class RecordGroups:
    """Append-only lists indexed on disk, including repeated coordinates."""

    def __init__(self, root: Path):
        self.records = RecordSpool(root)
        self.keys = RecordMap(root / '.groups')

    def append(self, key: Any, value: Any) -> None:
        encoded = canonical(key).decode()
        self.records.append(value, keys=(encoded,))
        if key not in self.keys:
            self.keys[key] = True

    def get(self, key: Any, default: Any = ()) -> Any:
        return RecordView(lambda: self.records.lookup(canonical(key).decode()))

    def values(self) -> Iterator[Any]:
        for key, _ in self.keys.items():
            yield self.records.lookup(canonical(key).decode())


class RecordView:
    """Repeat an indexed read without keeping decoded records between reads."""

    def __init__(self, read: Callable[[], Iterator[Any]]):
        self.read = read

    def __iter__(self) -> Iterator[Any]:
        return self.read()

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __getitem__(self, index: int) -> Any:
        for offset, value in enumerate(self):
            if offset == index:
                return value
        raise IndexError(index)


def sorted_operations(values: Iterator[dict[str, Any]], root: Path) -> Path:
    """Sort bounded runs with sixteen input files per merge, preserving repeats."""
    import heapq
    from contextlib import ExitStack
    root.mkdir(parents=True, exist_ok=True)
    runs: list[Path] = []
    batch: list[dict[str, Any]] = []
    def emit() -> None:
        path = root / f'run-{len(runs):06d}'
        with path.open('wb') as output:
            for value in sorted(batch, key=lambda value: value['at_us']):
                output.write(canonical(value) + b'\n')
        runs.append(path)
        batch.clear()
    for value in values:
        batch.append(value)
        if len(batch) == 512:
            emit()
    if batch or not runs:
        emit()
    level = 0
    while len(runs) > 1:
        merged = []
        for offset in range(0, len(runs), 16):
            target = root / f'merge-{level}-{offset}'
            sources = runs[offset:offset + 16]
            with ExitStack() as stack, target.open('wb') as output:
                streams = [map(json.loads, stack.enter_context(path.open())) for path in sources]
                for value in heapq.merge(*streams, key=lambda value: value['at_us']):
                    output.write(canonical(value) + b'\n')
            for path in sources:
                path.unlink()
            merged.append(target)
        runs = merged
        level += 1
    return runs[0]
