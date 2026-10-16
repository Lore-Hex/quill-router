"""Dormant A3-C1 read-only capture ports and bounded immutable packaging.

NativeClients is deliberately absent: no network, retries or runtime
imports. A capture records a call, including its failure; EOF is explicit and is
not a delivery-completeness claim. Source adapters must be independently reviewed.
"""
from __future__ import annotations

import re
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


RECORD_LIMITS = dict(a3_startup_correlation=2048, a3_boot_check=16384,
                    a3_traffic_ready=1024, a3_opted_attempt=2048, a3_shutdown_span=4096)
CHECKS = {
    'transport': ('actual_hops', 'both_routes_variants', 'compared_outcome'),
    'size': ('event_snapshot', 'headers_fragments', 'native_memory', 'transactions'),
    'regional_load': ('cpu_reads', 'envelope', 'publisher_export_shutdown'),
    'boot_transport': ('local_dependencies', 'regional_hops'),
    'observer_freshness': ('role_warm_path', 'slot_freshness'),
    'marker_keys': ('deployed_pins', 'public_epoch', 'separation_negatives'),
}
SOURCES = {
    'transport': ('ci', 'regional_trial'), 'size': ('ci', 'sizing_trial'),
    'regional_load': ('budget_trial',), 'boot_transport': ('regional_trial',),
    'observer_freshness': (), 'marker_keys': ('ci',),
}
RECORD_FIELDS = {
    'a3_startup_correlation': 'kind instance launch boot role revision region process_start_us recorded_at_us',
    'a3_boot_check': 'kind boot role revision region boot_at_us criterion completed_at_us checks config_sha256 inputs',
    'a3_traffic_ready': 'kind boot at_us',
    'a3_opted_attempt': 'kind boot at_us result receipt',
    'a3_shutdown_span': 'kind boot instance launch revision region day interval_end_us complete clock clock_origin spans outcome',
}
ROLE_FIELDS = {
    'boot_transport': {'router': 'dependencies clock_basis', 'enclave': 'dependencies clock_basis'},
    'observer_freshness': {
        'router': 'health_basis install_elapsed_us contender installed_at_ns clock_basis',
        'enclave': 'router_path configured_router_region local_transport_ready echo_ready clock_basis'},
    'marker_keys': {
        'router': 'effective_pins source_revision authority_epoch public_descriptors policy_sha256 fixture_sha256',
        'enclave': 'effective_pins source_revision authority_epoch public_descriptors policy_sha256 fixture_sha256 attested_image attested_revision'},
}
ATTEMPT_RESULTS = ('accepted', 'failed', 'rejected', 'timeout')
# No non-lifecycle kinds have been reviewed for these projected runtime channels.
CHANNEL_KINDS = {
    'startup': ('a3_startup_correlation',),
    'readiness': ('a3_boot_check', 'a3_traffic_ready', 'a3_opted_attempt'),
    'shutdown': ('a3_shutdown_span',),
}

# Runtime coordinates replace only the listed captured FILE_REF fields. The
# offline adapter resolves against a supplied original index; no source lookup.
EMITTED_COORDINATE_FIELDS = 'kind sha256 bytes'
EMITTED_COORDINATES: dict[str, dict[str, Any]] = {
    'clock_basis.original': dict(kind='boot_clock', max_bytes=262144, producer='boot_clock_recorder'),
    'health_basis': dict(kind='health_basis', max_bytes=6144, producer='health_reader'),
    'claim': dict(kind='health_claim', max_bytes=1024, producer='health_publisher'),
    'publication': dict(kind='health', max_bytes=4096, producer='health_publisher'),
    'public_descriptors': dict(kind='public_descriptors', max_bytes=16384, producer='release_loader'),
    'attested_image': dict(kind='image_record', max_bytes=16384, producer='image_record_loader'),
    'receipt.original': dict(kind='receipt_page', max_bytes=65536, producer='receipt_page_writer'),
    'originals.original_close': dict(kind='closure', max_bytes=4096, producer='daily_close_writer'),
    'originals.close_result': dict(kind='close_result', max_bytes=4096, producer='close_result_writer'),
}
EMITTED_COORDINATE_TYPES = dict(kind='field_kind', sha256='lower_hex_64', bytes='positive_integer_to_max_bytes')
LAUNCH_DESCRIPTOR_FIELDS = 'instance launch role revision region'
LAUNCH_COORDINATE_FIELDS = {
    'cloud_run_instance': 'project location service revision_name instance_id',
    'launcher_start': 'project zone instance_id vm_boot_id start_insert_id',
    'supervisor_start': 'project location instance_id supervisor_boot_id child_start_sequence',
}


def resolve_emitted_reference(files: Files, field: str, coordinate: dict[str, Any],
                              originals: Mapping[tuple[str, str, int], dict[str, Any]]) -> dict[str, Any]:
    """Join bounded emitted coordinates to an indexed, retained original file."""
    require(field in EMITTED_COORDINATES)
    spec = EMITTED_COORDINATES[field]
    fields(coordinate, EMITTED_COORDINATE_FIELDS)
    require(coordinate['kind'] == spec['kind'])
    require(type(coordinate['sha256']) is str and re.fullmatch('[0-9a-f]{64}', coordinate['sha256']) is not None)
    integer(coordinate['bytes'])
    require(0 < coordinate['bytes'] <= spec['max_bytes'])
    ref = originals[(coordinate['kind'], coordinate['sha256'], coordinate['bytes'])]
    source_ref(ref)
    require(all(coordinate[k] == ref[k] for k in ('sha256', 'bytes')))
    files.read(ref)
    return ref


def identifier(value: Any) -> None:
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value) is not None)


def integer(value: Any) -> None:
    require(type(value) is int and 0 <= value <= 9223372036854775807)


def fields(value: Any, names: str) -> None:
    require(type(value) is dict and set(value) == set(names.split()))


def source_ref(value: Any) -> None:
    a2.FILE_REF(value)
    require(len(value['path']) <= 256 and value['bytes'] <= 262144)


def runtime_record(record: dict[str, Any]) -> None:
    kind = record['kind']
    require(kind in RECORD_LIMITS)
    require(len(canonical(record)) <= RECORD_LIMITS[kind], 'runtime_record_bytes')
    names = RECORD_FIELDS[kind]
    if kind == 'a3_boot_check':
        criterion, role = record['criterion'], record['role']
        require(criterion in CHECKS and role in ('router', 'enclave'))
        names += ' ' + ROLE_FIELDS.get(criterion, {}).get(role, '')
        if criterion == 'observer_freshness' and role == 'router' and record.get('contender') is True:
            names += ' claim publication publication_elapsed_us claim_at_ns published_at_ns'
        fields(record['checks'], ' '.join(CHECKS[criterion]))
        require(all(v in ('pass', 'fail') for v in record['checks'].values()))
        fields(record['inputs'], ' '.join(SOURCES[criterion]))
        for value in record['inputs'].values():
            identifier(value)
    fields(record, names)
    for key, value in record.items():
        if key in ('boot', 'instance', 'launch', 'region', 'configured_router_region'):
            identifier(value)
        elif key in ('revision', 'source_revision', 'attested_revision'):
            a2.revision(value)
        elif key.endswith('_sha256'):
            require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)
        elif key.endswith(('_us', '_ns')) or key == 'authority_epoch':
            integer(value)
        elif key in ('health_basis', 'claim', 'publication', 'public_descriptors', 'attested_image'):
            source_ref(value)
    if 'role' in record:
        require(record['role'] in ('router', 'enclave'))
    if 'clock_basis' in record:
        basis = record['clock_basis']
        fields(basis, 'clock boot boot_at_us original')
        require(basis['clock'] == 'utc_ns' and basis['boot'] == record['boot'])
        integer(basis['boot_at_us'])
        source_ref(basis['original'])
    if 'dependencies' in record:
        require(type(record['dependencies']) is list and 0 < len(record['dependencies']) <= 16)
        for row in record['dependencies']:
            fields(row, 'boot region config_sha256 operation resource attempts marker_matches elapsed_us deadline_us observed_at_us')
            for key in ('boot', 'region', 'resource'):
                identifier(row[key])
            require(row['operation'] in ('transport', 'read'))
            for key in ('attempts', 'elapsed_us', 'deadline_us', 'observed_at_us'):
                integer(row[key])
            require(type(row['marker_matches']) is bool)
    for key in ('contender', 'local_transport_ready', 'echo_ready'):
        if key in record:
            require(type(record[key]) is bool)
    if 'router_path' in record:
        row = record['router_path']
        fields(row, 'ready resource router_boot region observed_at_us')
        for key in ('resource', 'router_boot', 'region'):
            identifier(row[key])
        integer(row['observed_at_us'])
        require(type(row['ready']) is bool)
    if 'effective_pins' in record:
        names = ('TR_ASYNC_SETTLE_ENABLED TR_ASYNC_SETTLE_PROTECTION TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED'
                 if record['role'] == 'router' else 'TR_ASYNC_SETTLE_NEGOTIATE TR_ASYNC_SETTLE_SHADOW')
        fields(record['effective_pins'], names)
        require(all(v in ('false', 'true', 'on', 'off') for v in record['effective_pins'].values()))
    if kind == 'a3_opted_attempt':
        require(record['result'] in ATTEMPT_RESULTS)
        receipt = record['receipt']
        fields(receipt, 'day boot sequence original')
        a2.day(receipt['day'])
        identifier(receipt['boot'])
        integer(receipt['sequence'])
        source_ref(receipt['original'])
    if kind == 'a3_shutdown_span':
        a2.day(record['day'])
        require(record['clock'] == 'monotonic_us' and type(record['complete']) is bool)
        require(record['outcome'] in ('graceful', 'hard_kill'))
        fields(record['clock_origin'], 'boot monotonic_at_boot_us')
        require(record['clock_origin']['boot'] == record['boot'])
        integer(record['clock_origin']['monotonic_at_boot_us'])
        fields(record['spans'], 'preparation export_wait final_pass close result')
        for span in record['spans'].values():
            require(type(span) is list and len(span) == 2)
            for coordinate in span:
                integer(coordinate)
            require(record['clock_origin']['monotonic_at_boot_us'] <= span[0] <= span[1])


def classified_records(run: Pass, records: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
    valid = []
    for record in records:
        if record.get('kind') not in CHANNEL_KINDS[channel]:
            run.gap(channel, 'missing_record_classification')
            continue
        try:
            runtime_record(record)
            valid.append(record)
        except (ValueError, KeyError, TypeError):
            run.gap(channel, 'malformed_runtime_record')
    return valid
