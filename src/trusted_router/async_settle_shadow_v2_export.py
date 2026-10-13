"""Dormant offline original-byte export; no database or credential construction.

The port owns bounded reads and records each page, including an empty terminal
page. Object keys must be supplied by an independent finite plan, never LIST.
Accounting is a supplemental file, not a new evidence/acknowledgement schema.
NativeV2RangeRead and NativeGenerationRead remain explicit unavailable seams.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
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
                snapshot='fixed-offline-capture'))
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


class NativePort:
    """Unavailable until reviewed readers expose metadata (see the source map).

    No new SQL or network fallback is hidden here. Existing DAY_SQL is v1-only;
    existing GCSAttachments.get drops generation. A partial native export must
    remain a gap, not silently delegate to either incompatible reader.
    """
    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []

    def rows(self, kind: str, day: str) -> Iterator[OriginalRow]:
        raise NotImplementedError('NativeV2RangeRead')

    def objects(self, prefix: str) -> Iterator[OriginalObject]:
        raise NotImplementedError('NativeGenerationRead')


@dataclass(frozen=True)
class Export:
    transport: bytes
    accounting: bytes
    gaps: tuple[dict[str, Any], ...]
    # Contains even non-UTF8/oversize originals rejected by the JSON transport.
    originals: tuple[tuple[str, bytes], ...]


def export_day(port: StoragePort, day: str, *, completed_at_us: int,
               object_keys: tuple[str, ...], supplied: Mapping[str, Any] | None = None,
               max_objects: int = 10000) -> Export:
    """Export one explicitly selected day. Supplied tool/collector files stay raw.

    `supplied` uses loader transport members (proof, preimages, ledgers, etc.).
    Original collector attachments may accompany it; they cannot replace observed
    rows or completion metadata. Failures are retained
    in export/gaps/*.json and in the tool exit status; G1 binding of these new
    diagnostics is the separately reviewed ExportAccountingBinding seam.
    """
    bounds(day)
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
    gaps: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    originals: list[tuple[str, bytes]] = []
    seen: dict[tuple[str, str], bytes] = {}
    start_page = len(port.pages)

    def gap(key: str, reason: str) -> None:
        gaps.append(dict(key=key, gap='artifact_gap', reason=reason))

    def retain(kind: str, key: str, raw: bytes, metadata: Mapping[str, Any], member: str) -> None:
        index = len(entries)
        path = 'originals/' + f'{index:05}' + '-' + digest(raw)
        originals.append((path, raw))
        entries.append(dict(kind=kind, key=key, bytes=len(raw), sha256=digest(raw),
            key_bytes=len(kind.encode()) + len(key.encode()), metadata=dict(metadata),
            original=path, ordinal=index))
        previous = seen.get((kind, key))
        if previous is not None:
            gap(key, 'conflicting_originals' if previous != raw else 'duplicate_original')
        seen[kind, key] = raw
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            gap(key, 'non_utf8_original_retained_in_sidecar')
            return
        if len(raw) > 262144:
            gap(key, 'original_exceeds_FILE_REF_bound')
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
        try:
            for row in port.rows(kind, day):
                low, high = bounds(day)
                if not low <= row.key < high or row.key < previous:
                    gap(kind + '/' + row.key, 'range_or_order_violation')
                retain(kind, row.key, row.body, row.commit, 'rows')
                previous = row.key
                read_count += 1
                # A page is already fetched: account all its bytes before
                # stopping continuation. Never discard its tail at a key cap.
                if len(entries) >= max_objects and read_count >= sum(p['count'] for p in port.pages[page_at:]):
                    if not port.pages[-1].get('complete') or len(entries) > max_objects:
                        gap(kind + '/' + day, 'finite_export_bound_exhausted')
                    break
            else:
                pages = port.pages[page_at:]
                if not pages or not pages[-1].get('complete'):
                    gap(kind + '/' + day, 'range_continuation_unproven')
                cursor = ''
                for index, page in enumerate(pages):
                    low, high = bounds(day)
                    if (page.get('source') != 'rows' or page.get('kind') != kind
                            or page.get('start') != low or page.get('end') != high
                            or page.get('after') != cursor
                            or not 1 <= page.get('page_size', 0) <= 200
                            or not 0 <= page.get('count', -1) <= page.get('page_size', 0)
                            or not 0 < page.get('deadline_ms', 0) <= 200
                            or page.get('priority') != 'LOW' or page.get('retries') != 0
                            or bool(page.get('complete')) != (index == len(pages) - 1)):
                        gap(kind + '/' + day, 'page_coverage_invalid')
                    cursor = page.get('last') or cursor
        except Exception as exc:
            gap(kind + '/' + day, type(exc).__name__ + ':' + ('NativeV2RangeRead' if isinstance(exc, NotImplementedError) else 'read_failed'))
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
                retain(OBJECT_KINDS[prefix], obj.key, obj.body, {'generation': obj.generation}, 'attachments')
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
    paginate('plan', list(object_keys))
    paginate('gaps', list(gaps))
    total = len(value['rows']) + len(value['attachments']) + len(value.get('ledgers', [])) + len(value['preimages']) + len(accounting_files) + 2
    if total > 10000:
        gap('transport', 'combined_loader_object_bound_exhausted')
        paginate('final-gaps', [gaps[-1]])
    accounting = canonical(dict(format='shadow-v2-live-export-round1', day=day,
        scope='declared day ranges and independently supplied exact object keys',
        complete=not gaps, passes=1, details=details,
        source_body_bytes=sum(e['bytes'] for e in entries),
        source_key_bytes=sum(e['key_bytes'] for e in entries),
        source_operations=len(port.pages)-start_page, gap_count=len(gaps),
        native=False, entry_count=len(entries)))
    value['preimages'].extend([*accounting_files,
        dict(name='export/accounting.json', raw=accounting.decode())])
    return Export(canonical(value), accounting, tuple(gaps), tuple(originals))
