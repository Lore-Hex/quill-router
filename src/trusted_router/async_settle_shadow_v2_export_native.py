"""Dormant A3-N readers. Only finite PK ranges and planned exact object GETs.

Spanner uses the v2-owned deadline/no-retry transport, never the money client.
GCS parallels GCSAttachments.get because it discards generation headers; its
unchanged Lifetime/AuthRequest supply the same byte, socket and auth discipline.
One raw row (<=256 KiB) is handed to a temporary page spool at a time.
Pages finish cleanup before being streamed to the bundle; never buffer a day.
"""
from __future__ import annotations

import json
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import quote, urlencode

from trusted_router.async_settle_shadow_v2_a1_evidence import KINDS
from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH
from trusted_router.async_settle_shadow_v2_export import (
    DAY_KINDS,
    OBJECT_KINDS,
    OriginalObject,
    OriginalRow,
    bounds,
)

RANGE_SQL = ("SELECT id, body, updated_at FROM tr_entities "
             "WHERE kind=@kind AND id>=@day_start AND id<@next_day_start "
             "AND id>@after_id ORDER BY id LIMIT @page_size")
EXACT_SQL = "SELECT body, updated_at FROM tr_entities WHERE kind=@kind AND id=@id"
BODY_LIMIT = 262144
HEALTH_KIND = KINDS[6]


@dataclass(frozen=True)
class NativeRecord:
    """Delivered key/body with deferred metadata, so metadata cannot erase it."""
    key: str
    body: bytes | str
    metadata: Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class NativePage:
    records: Iterator[NativeRecord]
    snapshot_ns: Callable[[], int]


@dataclass(frozen=True)
class NativeIO:
    """Read-only binding supplied by the release tool, without SDK objects here."""
    owned_database: Callable[..., Any]
    open_page: Callable[..., Any]
    timestamp_ns: Callable[..., int]
    lifetime: Callable[..., Any]
    authorize: Callable[..., Any]


def spool_row(spool: BinaryIO, row: OriginalRow) -> None:
    header = json.dumps(dict(key=row.key, metadata=dict(row.commit))).encode()
    spool.write(struct.pack('!II', len(header), len(row.body)))
    spool.write(header)
    spool.write(row.body)


def spool_rows(spool: BinaryIO) -> Iterator[OriginalRow]:
    while sizes := spool.read(8):
        header_bytes, body_bytes = struct.unpack('!II', sizes)
        header = json.loads(spool.read(header_bytes))
        yield OriginalRow(header['key'], spool.read(body_bytes), header['metadata'])


class NativeV2RangeRead:
    def __init__(self, database: Any, *, io: NativeIO, page_size: int = 200,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError('page_size')
        self.io = io
        self.database = io.owned_database(database)
        self.page_size, self.clock = page_size, clock
        self.snapshot_ns: int | None = None
        self.pages: list[dict[str, Any]] = []

    def _read(self, kind: str, day: str | None, key: str = '', after: str = '') -> Iterator[OriginalRow]:
        low, high = bounds(day) if day is not None else ('', '')
        params = (dict(kind=kind, day_start=low, next_day_start=high, after_id=after,
                       page_size=self.page_size) if day is not None else dict(kind=kind, id=key))
        page: dict[str, Any] = dict(source='rows' if day is not None else 'exact', kind=kind,
            start=low, end=high, after=after, key=key, first=None, last=None, count=0,
            complete=False, continuation=None, page_size=self.page_size if day is not None else 1,
            deadline_ms=200, priority='LOW', retries=0, snapshot=self.snapshot_ns,
            native_rpcs=0, body_bytes=0, key_bytes=0, status='started')
        self.pages.append(page)
        started = self.clock()
        deadline = started + .2

        def rpc(elapsed_us: int) -> None:
            page['native_rpcs'] += 1
            page['rpc_elapsed_us'] = page.get('rpc_elapsed_us', 0) + elapsed_us

        failure: Exception | None = None
        with tempfile.TemporaryFile() as spool:
            try:
                with self.io.open_page(self.database, RANGE_SQL if day is not None else EXACT_SQL,
                        params, self.snapshot_ns, deadline, self.clock, rpc,
                        self.io.timestamp_ns) as source:
                    previous = after
                    for record in source.records:
                        identity, body = record.key, record.body
                        invalid_range = (page['count'] >= page['page_size'] or (day is not None and
                                (not low <= identity < high or identity <= previous)))
                        raw = body.encode('utf-8') if isinstance(body, str) else body
                        if not isinstance(raw, bytes):
                            raise ValueError('body_encoding')
                        page['count'] += 1
                        page['body_bytes'] += len(raw)
                        page['key_bytes'] += len(kind.encode()) + len(identity.encode())
                        page['first'] = page['first'] or identity
                        page['last'] = identity
                        previous = identity
                        metadata: dict[str, Any] = dict(kind=kind, received_before_failure=True)
                        try:
                            metadata.update(record.metadata())
                            self._snapshot(metadata['read_snapshot_ns'], page)
                            if self.clock() >= deadline:
                                raise TimeoutError('range_iteration_deadline')
                            if invalid_range:
                                raise ValueError('range_or_continuation')
                            if len(raw) > BODY_LIMIT:
                                raise ValueError('body_bound_exhausted')
                        except Exception as exc:
                            metadata['metadata_or_read_failure'] = type(exc).__name__
                            raise
                        else:
                            del metadata['received_before_failure']
                        finally:
                            # Every delivered original survives even invalid metadata,
                            # an expired deadline, or invalid order/size.
                            spool_row(spool, OriginalRow(identity, raw, metadata))
                    # An empty page MUST also return valid snapshot metadata.
                    self._snapshot(source.snapshot_ns(), page)
                if self.clock() >= deadline:
                    raise TimeoutError('range_cleanup_deadline')
                page.update(status='ok', complete=page['count'] < page['page_size'],
                            continuation=page['last'] if page['count'] == page['page_size'] else None)
                if day is None:
                    page.update(complete=True, continuation=None)
            except Exception as exc:
                page.update(status=type(exc).__name__, complete=False)
                failure = exc
            finally:
                page['elapsed_us'] = max(0, round((self.clock()-started)*1e6))

            # Release SDK body references before reading our one-record spool buffer.
            if page['count']:
                del record, body, raw
            spool.seek(0)
            yield from spool_rows(spool)
            if failure is not None:
                raise failure

    def _snapshot(self, ns: int, page: dict[str, Any]) -> None:
        if ns <= 0 or (self.snapshot_ns is not None and ns != self.snapshot_ns):
            raise ValueError('snapshot_metadata')
        if self.snapshot_ns is None:
            self.snapshot_ns = ns
        page['snapshot'] = ns

    def rows(self, kind: str, day: str) -> Iterator[OriginalRow]:
        if kind not in DAY_KINDS:
            raise ValueError('non_day_kind')
        after = ''
        while True:
            yield from self._read(kind, day, after=after)
            page = self.pages[-1]
            if page['complete']:
                return
            cursor = page['continuation']
            if not cursor or cursor <= after:
                raise ValueError('continuation_lost')
            after = cursor

    def exact(self, kind: str, key: str) -> Iterator[OriginalRow]:
        if kind not in KINDS or not key:
            raise ValueError('exact_kind_key')
        yield from self._read(kind, None, key)


class NativeGenerationRead:
    def __init__(self, bucket: str, plan: Mapping[str, str | None], *, io: NativeIO,
                 clock: Callable[[], float] = time.monotonic,
                 authenticated: bool = True) -> None:
        if not bucket or len(plan) > 10000:
            raise ValueError('finite_object_plan')
        for key, generation in plan.items():
            parts = key.split('/')
            if (len(parts) != 4 or parts[0] not in OBJECT_KINDS or not parts[2]
                    or not parts[3].isascii() or not parts[3].isdigit()
                    or (parts[0] == 'health-basis-batches' and len(parts[3]) != 20)):
                raise ValueError('object_key')
            bounds(parts[1])
            if generation is not None and (not generation.isascii() or not generation.isdigit()):
                raise ValueError('object_generation')
        self.io = io
        self.bucket, self.plan = quote(bucket, safe=''), dict(plan)
        self.clock, self.authenticated = clock, authenticated
        self.pages: list[dict[str, Any]] = []
        self.active = threading.Lock()

    def objects(self, key: str) -> Iterator[OriginalObject]:
        if key not in self.plan:
            raise ValueError('object_outside_exact_plan')
        page: dict[str, Any] = dict(source='object', key=key, count=0, complete=False,
            deadline_ms=200, retries=0, native_rpcs=0, generation_pinned=True,
            planned_generation=self.plan[key], status='started', body_bytes=0)
        self.pages.append(page)
        started = self.clock()
        lifetime = self.io.lifetime(started + .2, self.clock)
        if not self.active.acquire(blocking=False):
            page['status'] = 'busy'
            raise TimeoutError('object_worker_busy')
        done = threading.Event()
        result: list[OriginalObject] = []
        errors: list[BaseException] = []
        exchange = lifetime.exchange

        def counted(method: str, url: str, body: bytes | None,
                    headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
            page['native_rpcs'] += 1
            return exchange(method, url, body, headers)

        lifetime.exchange = counted

        def run() -> None:
            try:
                params: dict[str, Any] = {'alt': 'media'}
                expected = self.plan[key]
                if expected is not None:
                    params['ifGenerationMatch'] = expected
                url = ('https://storage.googleapis.com/storage/v1/b/' + self.bucket + '/o/'
                       + quote(key, safe='') + '?' + urlencode(params))
                headers: dict[str, str] = {}
                if self.authenticated:
                    self.io.authorize(lifetime, url, headers)
                lifetime.remaining()
                status, received, data = lifetime.exchange('GET', url, None, headers)
                page.update(response_status=status, body_bytes=len(data))
                lifetime.remaining()
                if status == 404:
                    raise FileNotFoundError(key)
                if status != 200:
                    raise ValueError('object_response_or_generation_changed')
                if len(data) > HEALTH_BASIS_BATCH.limit:
                    raise ValueError('object_body_bound')
                generation, meta = received.get('x-goog-generation'), received.get('x-goog-metageneration')
                if (not generation or not meta or not generation.isdigit() or not meta.isdigit()
                        or (expected is not None and generation != expected)):
                    raise ValueError('object_generation_metadata')
                page.update(generation=generation, metageneration=meta)
                result.append(OriginalObject(key, data, generation, meta))
            except BaseException as error:
                errors.append(error)
            finally:
                self.active.release()
                done.set()

        try:
            try:
                threading.Thread(target=run, name='v2-export-object', daemon=True).start()
            except BaseException:
                self.active.release()
                raise
            if not done.wait(lifetime.remaining()):
                raise TimeoutError('object_deadline')
            lifetime.remaining()
            if errors:
                raise errors[0]
            obj = result[0]
            self.plan[key] = obj.generation
            page.update(count=1, complete=True, status='ok')
            yield obj
        except BaseException as exc:
            page.update(status=type(exc).__name__, complete=False)
            raise
        finally:
            lifetime.abort()
            page['elapsed_us'] = max(0, round((self.clock()-started)*1e6))


class NativePort:
    native = True

    def __init__(self, database: Any, bucket: str, plan: Mapping[str, str | None], *,
                 io: NativeIO, page_size: int = 200, resources: Mapping[str, str] | None = None) -> None:
        self.object_plan = dict(plan)
        self.resources = dict(resources or {})
        self.range = NativeV2RangeRead(database, io=io, page_size=page_size)
        self.generation = NativeGenerationRead(bucket, plan, io=io)
        self.pages: list[dict[str, Any]] = []
        self.range.pages = self.generation.pages = self.pages

    def rows(self, kind: str, day: str) -> Iterator[OriginalRow]:
        return self.range.rows(kind, day)

    def objects(self, key: str) -> Iterator[OriginalObject]:
        return self.generation.objects(key)
