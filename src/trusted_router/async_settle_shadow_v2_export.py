"""Dormant offline original-byte export; no database or credential construction.

The port owns bounded reads and records each page, including an empty terminal
page. Object keys must be supplied by an independent finite plan, never LIST.
Accounting is a supplemental file, not a new evidence/acknowledgement schema.
Native readers live in async_settle_shadow_v2_export_native, loaded only by the CLI.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from trusted_router.async_settle_shadow_v2_a1_evidence import KINDS
from trusted_router.detached_jws import canonical

DAY_KINDS = (*KINDS[:6], KINDS[-1])
OBJECT_KINDS = {'health-basis-batches': 'health_basis', 'publisher-traces': 'publisher_trace'}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def bounds(day: str) -> tuple[str, str]:
    parsed = dt.date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError('invalid day')
    return day + '/', (parsed + dt.timedelta(days=1)).isoformat() + '/'


@dataclass(frozen=True)
class OriginalRow:
    key: str
    body: bytes
    commit: Mapping[str, Any]


@dataclass(frozen=True)
class OriginalObject:
    key: str
    body: bytes
    generation: str
    metageneration: str | None = None


class StoragePort(Protocol):
    # Appended before yielding each page; final complete page proves EOF in the
    # port's fixed snapshot, not completeness of independent boot inventory.
    pages: list[dict[str, Any]]

    def rows(self, kind: str, day: str) -> Iterator[OriginalRow]: ...
    def objects(self, prefix: str) -> Iterator[OriginalObject]: ...


@dataclass
class MemoryPort:
    """Fixed offline snapshot with honest fake commit versions and generations.

    Tuples retain conflicting candidates and original order within one key.
    Sorting is the explicit simulated primary-key ORDER BY, recorded per page.
    No wall/server timestamps are invented from the fake database's version.
    """
    originals: Mapping[str, tuple[OriginalRow, ...]]
    blobs: Mapping[str, tuple[OriginalObject, ...]]
    object_keys: tuple[str, ...]
    page_size: int = 200
    rpc_deadline_ms: int = 200
    pages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 1 <= self.page_size <= 200 or not 0 < self.rpc_deadline_ms <= 200:
            raise ValueError('read bounds')
        if len(self.object_keys) > 10000 or len(set(self.object_keys)) != len(self.object_keys):
            raise ValueError('finite object plan')

    @classmethod
    def from_fake(cls, database: Any, objects: Mapping[str, OriginalObject], *,
                  object_keys: tuple[str, ...], page_size: int = 200) -> MemoryPort:
        originals: dict[str, list[OriginalRow]] = {}
        for (kind, key), row in database.rows.items():
            originals.setdefault(kind, []).append(OriginalRow(key, row.body.encode('utf-8'),
                {'fake_version': row.version}))
        return cls({k: tuple(v) for k, v in originals.items()},
            {k: (v,) for k, v in objects.items()}, object_keys, page_size)

    def rows(self, kind: str, day: str) -> Iterator[OriginalRow]:
        if kind not in DAY_KINDS:
            raise ValueError('NativeV2RangeRead: non-day kind')
        start, end = bounds(day)
        found = sorted((r for r in self.originals.get(kind, ()) if start <= r.key < end), key=lambda r: r.key)
        after = ''
        # Always record a terminal short/empty page, including exact multiples.
        for at in range(0, len(found) + 1, self.page_size):
            page = found[at:at + self.page_size]
            self.pages.append(dict(source='rows', kind=kind, start=start, end=end,
                after=after, first=page[0].key if page else None,
                last=page[-1].key if page else None, count=len(page),
                complete=len(page) < self.page_size, page_size=self.page_size,
                deadline_ms=self.rpc_deadline_ms, priority='LOW', retries=0,
                snapshot='fixed-offline-capture', status='ok',
                continuation=page[-1].key if len(page) == self.page_size else None))
            yield from page
            if len(page) < self.page_size:
                return
            after = page[-1].key

    def objects(self, prefix: str) -> Iterator[OriginalObject]:
        for key in self.object_keys:
            if not (key == prefix if prefix in self.object_keys else key.startswith(prefix)):
                continue
            candidates = self.blobs.get(key, ())
            self.pages.append(dict(source='object', key=key, count=len(candidates),
                complete=bool(candidates), deadline_ms=self.rpc_deadline_ms,
                retries=0, generation_pinned=True))
            if not candidates:
                raise FileNotFoundError(key)
            yield from candidates


class SpoolArray:
    """Append-only JSON fragments; iteration holds one record, never a day."""
    def __init__(self, path: Path) -> None:
        self.path, self.count = path, 0
        path.touch(exist_ok=False)

    def append(self, value: Any) -> None:
        with self.path.open('a', encoding='utf-8') as target:
            json.dump(value, target, ensure_ascii=True, separators=(',', ':'))
            target.write('\n')
        self.count += 1

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Any]:
        with self.path.open(encoding='utf-8') as source:
            for line in source:
                yield json.loads(line)


def spool_transport(path: Path, value: Mapping[str, Any]) -> None:
    with path.open('x', encoding='utf-8') as target:
        target.write('{')
        for index, (name, member) in enumerate(sorted(value.items())):
            if index:
                target.write(',')
            target.write(json.dumps(name) + ':')
            if isinstance(member, SpoolArray):
                target.write('[')
                with member.path.open(encoding='utf-8') as source:
                    for ordinal, line in enumerate(source):
                        if ordinal:
                            target.write(',')
                        target.write(line.rstrip('\n'))
                target.write(']')
            else:
                json.dump(member, target, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
        target.write('}')


@dataclass(frozen=True)
class Export:
    transport: bytes
    accounting: bytes
    gaps: tuple[dict[str, Any], ...]
    # Contains even non-UTF8/oversize originals rejected by the JSON transport.
    originals: tuple[tuple[str, bytes], ...]
    transport_path: Path | None = None


def export_day(port: StoragePort, day: str, *, completed_at_us: int,
               object_keys: tuple[str, ...], supplied: Mapping[str, Any] | None = None,
               max_objects: int = 10000, spool_dir: Path | None = None) -> Export:
    """Export one explicitly selected day. Supplied tool/collector files stay raw.

    `supplied` uses loader transport members (proof, preimages, ledgers, etc.).
    Original collector attachments may accompany it; they cannot replace observed
    rows or completion metadata. Failures are retained
    in export/gaps/*.json and in the tool exit status; G1 binding of these new
    diagnostics is the separately reviewed ExportAccountingBinding seam.
    """
    bounds(day)
    native = bool(getattr(port, 'native', False))
    if native and spool_dir is None:
        raise ValueError('native_export_requires_spool')
    if spool_dir is not None:
        spool_dir.mkdir(exist_ok=False)
        # Persist the finite plan BEFORE the first source operation.
        (spool_dir / 'plan.json').write_bytes(canonical(dict(day=day,
            ranges=[dict(kind=k, start=bounds(day)[0], end=bounds(day)[1]) for k in DAY_KINDS],
            object_keys=list(object_keys), max_objects=max_objects,
            generations=getattr(port, 'object_plan', {}), resources=getattr(port, 'resources', {}))))
    if not 1 <= max_objects <= 10000 or len(object_keys) != len(set(object_keys)):
        raise ValueError('finite export bound')
    if len(object_keys) > max_objects or completed_at_us < 0:
        raise ValueError('finite export bound')
    value: dict[str, Any] = dict(supplied or {})
    if set(value) - {'proof', 'ledgers', 'preimages', 'transport', 'fleet', 'pre', 'serving', 'window', 'attachments'}:
        raise ValueError('supplied transport member')
    supplied_attachments = value.pop('attachments', [])
    value.update(policy_version='shadow-v2', contract_revision='a1', days=[day],
                 completed_at_us=completed_at_us, rows=[], attachments=[])
    value['preimages'] = list(value.get('preimages', []))
    if spool_dir is not None:
        value['rows'] = SpoolArray(spool_dir / 'rows.jsonl')
        value['attachments'] = SpoolArray(spool_dir / 'attachments.jsonl')
    gaps: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    originals: list[tuple[str, bytes]] = []
    seen: dict[tuple[str, str], str] = {}
    pass_snapshot: Any = None
    start_page = len(port.pages)

    def gap(key: str, reason: str) -> None:
        gaps.append(dict(key=key, gap='artifact_gap', reason=reason))

    def retain(kind: str, key: str, raw: bytes, metadata: Mapping[str, Any], member: str) -> None:
        index = len(entries)
        path = 'originals/' + f'{index:05}' + '-' + digest(raw)
        if spool_dir is None:
            originals.append((path, raw))
        else:
            destination = spool_dir / path
            destination.parent.mkdir(exist_ok=True)
            with destination.open('xb') as target:
                target.write(raw)
        entries.append(dict(kind=kind, key=key, bytes=len(raw), sha256=digest(raw),
            key_bytes=len(kind.encode()) + len(key.encode()),
            key_sha256=digest(canonical([kind, key])), key_sha256_encoding='canonical-json-kind-key',
            metadata=dict(metadata),
            original=path, ordinal=index))
        previous = seen.get((kind, key))
        if previous is not None:
            gap(key, 'conflicting_originals' if previous != digest(raw) else 'duplicate_original')
        seen[kind, key] = digest(raw)
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            gap(key, 'non_utf8_original_retained_in_sidecar')
            return
        if len(raw) > 262144:
            gap(key, 'original_exceeds_FILE_REF_bound')
            return
        value[member].append(dict(kind=kind, id=key, raw=text))

    for attachment in supplied_attachments:
        retain(attachment['kind'], attachment['id'], attachment['raw'].encode('utf-8'),
            {'source': 'supplied_collector_original'}, 'attachments')
    for kind in DAY_KINDS:
        if len(entries) >= max_objects:
            gap(kind + '/' + day, 'range_not_read_after_bound')
            continue
        read_count = 0
        page_at = len(port.pages)
        previous = ''
        actual: dict[int, dict[str, Any]] = {}
        iterator = None
        try:
            iterator = port.rows(kind, day)
            for row in iterator:
                low, high = bounds(day)
                if not low <= row.key < high or row.key <= previous:
                    gap(kind + '/' + row.key, 'range_or_order_violation')
                original_kind = row.commit.get('kind', '' if native else kind)
                retain(original_kind, row.key, row.body, row.commit, 'rows')
                if len(port.pages) <= page_at:
                    gap(kind + '/' + row.key, 'row_without_page')
                else:
                    page = port.pages[-1]
                    row_snapshot = row.commit.get('read_snapshot_ns')
                    found = actual.setdefault(id(page), dict(count=0, first=row.key, last=None,
                        row_snapshot=row_snapshot, differing_row_snapshots=False))
                    found['count'] += 1
                    found['last'] = row.key
                    if row_snapshot != found['row_snapshot']:
                        found['differing_row_snapshots'] = True
                    if native and (original_kind != kind or not row.commit.get('read_snapshot_ns')
                            or row.commit.get('read_snapshot_ns') != page.get('snapshot')
                            or row.commit.get('received_before_failure')):
                        gap(kind + '/' + row.key, 'row_metadata_invalid')
                previous = row.key
                read_count += 1
                # Account the already fetched page before stopping continuation.
                if len(entries) >= max_objects and read_count >= sum(p['count'] for p in port.pages[page_at:]):
                    if not port.pages[-1].get('complete') or len(entries) > max_objects:
                        gap(kind + '/' + day, 'finite_export_bound_exhausted')
                    break
        except Exception as exc:
            gap(kind + '/' + day, type(exc).__name__ + ':' + ('NativeV2RangeRead' if isinstance(exc, NotImplementedError) else 'read_failed'))
        finally:
            try:
                close = getattr(iterator, 'close', None)
                if close is not None:
                    close()
            except Exception as exc:
                gap(kind + '/' + day, type(exc).__name__ + ':cleanup_failed')
        # Validate every exit, after cleanup has succeeded or recorded its gap.
        pages = port.pages[page_at:]
        if not pages or not pages[-1].get('complete'):
            gap(kind + '/' + day, 'range_continuation_unproven')
        if sum(p['count'] for p in actual.values()) != read_count or set(actual) - {id(p) for p in pages}:
            gap(kind + '/' + day, 'page_originals_disagree')
        cursor = ''
        for index, page in enumerate(pages):
            low, high = bounds(day)
            found = actual.get(id(page), dict(count=0, first=None, last=None))
            if any(page.get(field) != found[field] for field in ('count', 'first', 'last')):
                gap(kind + '/' + day, 'page_originals_disagree')
            if native and found['count'] and (found['differing_row_snapshots']
                    or page.get('snapshot') != found['row_snapshot']):
                gap(kind + '/' + day, 'page_originals_disagree')
            if (page.get('source') != 'rows' or page.get('kind') != kind
                    or page.get('start') != low or page.get('end') != high
                    or page.get('after') != cursor
                    or not 1 <= page.get('page_size', 0) <= 200
                    or not 0 <= page.get('count', -1) <= page.get('page_size', 0)
                    or not 0 < page.get('deadline_ms', 0) <= 200
                    or page.get('priority') != 'LOW' or page.get('retries') != 0
                    or page.get('status') != 'ok'
                    or not page.get('snapshot')
                    or (pass_snapshot is not None and page.get('snapshot') != pass_snapshot)
                    or bool(page.get('complete')) != (index == len(pages) - 1)
                    or bool(page.get('complete')) != (page.get('count', 0) < page.get('page_size', 0))
                    or page.get('continuation') != (None if page.get('complete') else found['last'])):
                gap(kind + '/' + day, 'page_coverage_invalid')
            if pass_snapshot is None:
                pass_snapshot = page.get('snapshot')
            cursor = found['last'] or cursor
    # One exact planned key per call: failure cannot erase its later siblings.
    for key in object_keys:
        if len(entries) >= max_objects:
            gap(key, 'object_not_read_after_bound')
            continue
        prefix = key.split('/', 1)[0]
        if prefix not in OBJECT_KINDS or not key.startswith(prefix + '/' + day + '/'):
            gap(key, 'object_outside_day_or_unknown_kind')
            continue
        count = 0
        try:
            for obj in port.objects(key):
                count += 1
                if obj.key != key:
                    gap(obj.key, 'object_outside_exact_plan')
                retain(OBJECT_KINDS[prefix], obj.key, obj.body, {'generation': obj.generation, 'metageneration': obj.metageneration}, 'attachments')
                if not obj.generation:
                    gap(key, 'object_generation_absent')
                if len(entries) > max_objects:
                    gap(key, 'finite_export_bound_exhausted')
            if not count:
                gap(key, 'planned_object_missing')
        except Exception as exc:
            gap(key, type(exc).__name__ + ':' + ('NativeGenerationRead' if isinstance(exc, NotImplementedError) else 'read_failed'))
    # Supplemental accounting is itself bounded and hash-linked. It never
    # substitutes for the contract's independently expected COVERAGE_PAGEs.
    accounting_files: list[dict[str, str]] = []
    details: dict[str, list[dict[str, Any]]] = {}

    def paginate(stream: str, items: list[Any]) -> None:
        references = []
        previous: str | None = None
        for at in range(0, len(items), 128):
            name = 'export/' + stream + '/' + f'{at // 128:05}' + '.json'
            raw = canonical(dict(stream=stream, sequence=at // 128,
                previous_sha256=previous, entries=items[at:at + 128]))
            if len(raw) > 262144:
                gap(name, 'accounting_page_bound_exhausted')
                continue
            previous = digest(raw)
            references.append(dict(path=name, sha256=previous, bytes=len(raw)))
            accounting_files.append(dict(name=name, raw=raw.decode()))
        details[stream] = references

    paginate('entries', entries)
    paginate('reads', port.pages[start_page:])
    paginate('plan', [*[dict(source='rows', kind=k, day=day,
        start=bounds(day)[0], end=bounds(day)[1]) for k in DAY_KINDS],
        *[dict(source='object', key=k, generation=getattr(port, 'object_plan', {}).get(k)) for k in object_keys]])
    paginate('gaps', list(gaps))
    total = len(value['rows']) + len(value['attachments']) + len(value.get('ledgers', [])) + len(value['preimages']) + len(accounting_files) + 2
    if total > 10000:
        gap('transport', 'combined_loader_object_bound_exhausted')
        paginate('final-gaps', [gaps[-1]])
    accounting = canonical(dict(format='shadow-v2-live-export-a3-n', day=day,
        scope='declared day ranges and independently supplied exact object keys',
        complete=not gaps, passes=1, details=details,
        source_body_bytes=sum(e['bytes'] for e in entries),
        source_key_bytes=sum(e['key_bytes'] for e in entries),
        source_operations=len(port.pages)-start_page, gap_count=len(gaps),
        native=native, resources=getattr(port, 'resources', {}), entry_count=len(entries),
        operations=dict(native_rpcs=sum(p.get('native_rpcs', 0) for p in port.pages[start_page:]),
            pages=sum(p.get('source') == 'rows' for p in port.pages[start_page:]),
            rows=sum(p.get('count', 0) for p in port.pages[start_page:] if p.get('source') == 'rows'),
            objects=sum(p.get('count', 0) for p in port.pages[start_page:] if p.get('source') == 'object'),
            body_bytes=sum(p.get('body_bytes', 0) for p in port.pages[start_page:]),
            elapsed_us=sum(p.get('elapsed_us', 0) for p in port.pages[start_page:])),
        buffer=dict(body_limit=262144, json_record_limit=6*262144+16384, spool_io_buffer_bytes=8192,
                    retained_bodies=1 if spool_dir else len(originals),
                    metadata_entry_limit=max_objects + 200, streaming=spool_dir is not None)))
    value['preimages'].extend([*accounting_files,
        dict(name='export/accounting.json', raw=accounting.decode())])
    if spool_dir is not None:
        (spool_dir / 'accounting.json').write_bytes(accounting)
        # Sidecar source pages are individually addressable by the next packager.
        for item in accounting_files:
            destination = spool_dir / item['name']
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(item['raw'])
        transport_path = spool_dir / 'bundle.json'
        spool_transport(transport_path, value)
        return Export(b'', accounting, tuple(gaps), (), transport_path)
    return Export(canonical(value), accounting, tuple(gaps), tuple(originals))
