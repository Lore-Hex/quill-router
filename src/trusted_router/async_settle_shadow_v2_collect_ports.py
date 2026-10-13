"""Dormant A3-C1 read-only capture ports and bounded immutable packaging.

NativeClients is deliberately absent: no credentials, network, retries or runtime
imports. A capture records a call, including its failure; EOF is explicit and is
not a delivery-completeness claim. Source adapters must be independently reviewed.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_schema import Schema, decode, require, sha256
from trusted_router.detached_jws import canonical

TAGS = dict(v=2, policy_version='shadow-v2', contract_revision='a2')


def day_bounds(day: str) -> tuple[int, int]:
    a2.day(day)
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC)
    return int(start.timestamp()) * 1_000_000, int((start + timedelta(days=1)).timestamp()) * 1_000_000


@dataclass(frozen=True)
class Query:
    source: str
    resource: str
    day: str
    page_size: int = 200
    deadline_seconds: int = 10

    def __post_init__(self) -> None:
        a2.name(self.source)
        require(type(self.resource) is str and 0 < len(self.resource) <= 1024 and '*' not in self.resource)
        day_bounds(self.day)
        require(type(self.page_size) is int and type(self.deadline_seconds) is int)
        require(0 < self.page_size <= 200 and 0 < self.deadline_seconds <= 10)


@dataclass(frozen=True)
class Page:
    """Original bounded API response plus capture metadata, never a short-page EOF."""
    raw: bytes
    next_token: str | None
    eof: bool
    elapsed_us: int
    complete_through_us: int | None
    truncated: bool = False
    error: str | None = None


class PlatformPort(Protocol):
    def read(self, query: Query, token: str | None) -> Page: ...


class LifecyclePort(Protocol):
    def read(self, query: Query, token: str | None) -> Page: ...


class MonitoringPort(Protocol):
    def read(self, query: Query, token: str | None) -> Page: ...


class CIPort(Protocol):
    def read(self, query: Query, token: str | None) -> Page: ...


@dataclass
class Files:
    """Disk spool: one bounded decode buffer; no whole capture held in memory."""
    root: Path

    def put(self, path: str, raw: bytes, schema: Schema | None = None) -> dict[str, Any]:
        ref = dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw))
        a2.FILE_REF(ref)
        if schema is not None:
            schema.parse(raw)
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(target.read_bytes() == raw, 'artifact_gap')
        else:
            with target.open('xb') as output:
                output.write(raw)
        return ref

    def obj(self, path: str, value: Any, schema: Schema | None = None) -> dict[str, Any]:
        return self.put(path, schema.serialize(value) if schema else canonical(value), schema)

    def read(self, ref: dict[str, Any]) -> bytes:
        a2.FILE_REF(ref)
        target = (self.root / ref['path']).resolve()
        require(target.is_relative_to(self.root.resolve()), 'artifact_gap')
        with target.open('rb') as source:
            raw = source.read(262145)
        require(len(raw) == ref['bytes'] and roots.file_digest(raw) == ref['sha256'], 'artifact_gap')
        return raw

    def detail(self, prefix: str, entries: list[dict[str, Any]], stream: str) -> dict[str, Any]:
        # Contract constructors define ordering, cardinality, chaining and root.
        _, schema, limit, _ = roots.STREAMS[stream]
        assert schema is not None
        ordered = sorted(entries, key=lambda entry: roots._key(entry, stream))
        pages: list[dict[str, Any]] = []
        tail = None
        for offset in range(0, len(ordered), limit):
            page = dict(**TAGS, page_seq=f'{len(pages) + 1:020d}', previous_sha256=tail,
                        entries=ordered[offset:offset + limit])
            self.obj(f'{prefix}/{len(pages) + 1:05d}.json', page, schema)
            pages.append(page)
            tail = sha256(page)
        detail = dict(root=roots.entry_root(ordered, stream=stream), count=len(ordered),
                      pages=len(pages), tail_sha256=tail)
        roots.validate_detail(detail, pages, stream=stream)
        return detail


@dataclass
class Pass:
    files: Files
    limit: int = 10_000
    records: int = 0
    calls: int = 0
    missing: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    _seen: set[tuple[str, str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        require(0 < self.limit <= 10_000)

    def gap(self, source: str, reason: str, gap: str = 'artifact_gap') -> None:
        item = dict(source=source, reason=reason, gap=gap)
        if item not in self.missing:
            self.missing.append(item)

    def scan(self, port: PlatformPort | LifecyclePort | MonitoringPort | CIPort,
             query: Query) -> Iterator[dict[str, Any]]:
        key = query.source, query.resource, query.day
        require(key not in self._seen, 'artifact_gap')
        self._seen.add(key)
        token = None
        tokens: set[str] = set()
        # Empty nonterminal responses also consume finite call budget.
        for _ in range(10_000):
            if self.records >= self.limit:
                self.gap(query.source, 'records_per_pass')
                return
            request_query = replace(query, page_size=min(query.page_size, self.limit - self.records))
            self.calls += 1
            prefix = f'sources/capture/{query.source}/{self.calls:05d}'
            request = dict(source=query.source, resource=query.resource, day=query.day,
                           page_size=request_query.page_size, deadline_seconds=query.deadline_seconds, token=token)
            self.sources.append(self.files.obj(prefix + '-request.json', request))
            try:
                page = port.read(request_query, token)
            except (TimeoutError, OSError, ValueError) as exc:
                self.sources.append(self.files.obj(prefix + '-failure.json',
                    dict(request=request, error=type(exc).__name__, elapsed_us=None, eof=False)))
                self.gap(query.source, type(exc).__name__)
                return
            try:
                ref = self.files.put(prefix + '.json', page.raw)
                self.sources.append(ref)
                self.sources.append(self.files.obj(prefix + '-call.json', dict(source=query.source, resource=query.resource,
                    day=query.day, page_size=request_query.page_size, deadline_seconds=query.deadline_seconds,
                    token=token, next_token=page.next_token, eof=page.eof, elapsed_us=page.elapsed_us,
                    complete_through_us=page.complete_through_us, truncated=page.truncated,
                    error=page.error, original=ref)))
                value = decode(page.raw, 262144, retained_strings=True)
                require(type(value) is dict and type(value.get('records')) is list)
                records = value['records']
                require(len(records) <= request_query.page_size and all(type(r) is dict for r in records))
                require(type(page.elapsed_us) is int and 0 <= page.elapsed_us <= query.deadline_seconds * 1_000_000)
                require(type(page.eof) is bool and type(page.truncated) is bool)
                require(page.complete_through_us is None or type(page.complete_through_us) is int)
                require(not page.error and not page.truncated)
                require((page.eof and page.next_token is None) or
                        (not page.eof and type(page.next_token) is str and bool(page.next_token)))
            except ValueError:
                self.gap(query.source, 'invalid_truncated_or_deadline_page')
                return
            if self.records + len(records) > self.limit:
                self.gap(query.source, 'records_per_pass')
                return
            self.records += len(records)
            yield from records
            if page.eof:
                if page.complete_through_us is None or page.complete_through_us < day_bounds(query.day)[1]:
                    self.gap(query.source, 'delivery_boundary_missing_or_delayed')
                return
            assert page.next_token is not None
            if page.next_token in tokens:
                self.gap(query.source, 'continuation_cycle')
                return
            tokens.add(page.next_token)
            token = page.next_token
        self.gap(query.source, 'calls_per_pass')

    def status(self, path: str) -> dict[str, Any]:
        return self.files.obj(path, dict(complete=not self.missing, calls=self.calls,
            records=self.records, missing=sorted(self.missing, key=canonical), originals=self.sources))


def approval(files: Files, value: Mapping[str, Any] | None, subject: dict[str, Any]) -> dict[str, Any] | None:
    """Consume original human review; never fabricate or self-approve one."""
    if value is None:
        return None
    result = dict(value)
    a2.APPROVAL(result)
    review = a2.REVIEW_RECORD.parse(files.read(result['record']))
    require(result['subject_sha256'] == subject['sha256'], 'artifact_gap')
    require(all(review[key] == result[key] for key in
                ('subject_sha256', 'reviewer', 'reviewed_at_us', 'decision')), 'artifact_gap')
    return result


@dataclass
class CapturedPort:
    """Exact finite file map. Missing captured calls fail; no native fallback."""
    files: Files
    captures: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]]

    def read(self, query: Query, token: str | None) -> Page:
        try:
            capture = self.captures[query.source, query.resource, query.day, token]
        except KeyError as exc:
            raise OSError('missing captured call') from exc
        require(capture['page_size'] == query.page_size and capture['deadline_seconds'] == query.deadline_seconds)
        return Page(raw=self.files.read(capture['original']), next_token=capture['next_token'],
                    eof=capture['eof'], elapsed_us=capture['elapsed_us'],
                    complete_through_us=capture['complete_through_us'],
                    truncated=capture['truncated'], error=capture['error'])
