"""Expected populations, exact captured page reads, and unresolved tails."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import Schema, require, sha256

TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a1')


@dataclass
class ExactPages:
    """One read of each enumerated source per pass, including absent sources."""
    get: Callable[[str], tuple[bytes, ...]]
    expected: set[str] = field(default_factory=set)
    cached: dict[str, tuple[bytes, ...]] = field(default_factory=dict)

    @classmethod
    def from_files(cls, files: Files, references: Mapping[str, list[dict[str, Any]]]) -> ExactPages:
        return cls(lambda key: tuple(files.read(ref) for ref in references.get(key, [])))

    def expect(self, keys: list[str]) -> None:
        self.expected.update(keys)

    def read(self, key: str) -> tuple[bytes, ...]:
        require(key in self.expected, 'artifact_gap')
        if key not in self.cached:
            self.cached[key] = self.get(key)
        return self.cached[key]


@dataclass(frozen=True)
class Tail:
    key: str
    first: int
    last: int
    reason: str


@dataclass(frozen=True)
class Chain:
    keys: tuple[str, ...]
    pages: tuple[dict[str, Any], ...]
    tail: Tail | None


def chain(source: ExactPages, *, first: int, last: int, key: Callable[[int], str],
          schema: Schema, first_field: str, last_field: str,
          previous_field: str) -> Chain:
    """An absent predecessor leaves the entire remaining interval outstanding."""
    require(0 <= first <= last + 1, 'artifact_gap')
    pages: list[dict[str, Any]] = []
    keys = []
    previous = None
    while first <= last:
        path = key(first)
        source.expect([path])
        keys.append(path)
        candidates = source.read(path)
        distinct = set(candidates)
        if len(distinct) != 1:
            return Chain(tuple(keys), tuple(pages), Tail(path, first, last, 'missing_or_conflicting_predecessor'))
        try:
            page = schema.parse(next(iter(distinct)))
            require(int(page[first_field]) == first and page[previous_field] == previous)
            following = int(page[last_field]) + 1
            require(first < following <= last + 1)
        except ValueError:
            return Chain(tuple(keys), tuple(pages), Tail(path, first, last, 'invalid_predecessor'))
        pages.append(page)
        first, previous = following, sha256(page)
    return Chain(tuple(keys), tuple(pages), None)


def receipt_chain(source: ExactPages, counter: dict[str, Any]) -> Chain:
    a1.COUNTER(counter)
    prefix = counter['day'] + '/' + counter['boot'] + '/'
    result = chain(source, first=1, last=counter['durable_event_hwm'],
        key=lambda first: prefix + f'{first:020d}', schema=p1.PAGE,
        first_field='first_seq', last_field='last_seq', previous_field='previous_page_sha256')
    if result.tail is None:
        actual = sha256(result.pages[-1]) if result.pages else None
        require(actual == counter['receipt_chain_sha256'], 'checkpoint_reconciliation_gap')
    return result


def contender_chain(source: ExactPages, *, day: str, boot: str, start_us: int, end_us: int) -> Chain:
    require(start_us < end_us, 'artifact_gap')
    return chain(source, first=(start_us + 1999999) // 2000000, last=(end_us - 1) // 2000000,
        key=lambda first: f'publisher-traces/{day}/{boot}/{first}', schema=health.PUBLISHER_TRACE_PAGE,
        first_field='first_slot', last_field='last_slot', previous_field='previous_sha256')


def coverage(files: PackFiles, source: ExactPages, *, day: str,
             expected: Mapping[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    require(set(expected) == set(a1.COVERAGE_STREAMS), 'artifact_gap')
    # Enumerate all populations before the first lookup of any stream.
    for stream, keys in expected.items():
        require(len(keys) == len(set(keys)), 'artifact_gap')
        source.expect([stream + '/' + key for key in keys])
    streams = {}
    for stream in a1.COVERAGE_STREAMS:
        entries = []
        for key in sorted(expected[stream]):
            bodies = source.read(stream + '/' + key)
            distinct = {roots.file_digest(raw): raw for raw in bodies}
            for digest, raw in sorted(distinct.items()):
                files.put(f'candidates/{digest}', raw)
            entry = dict(key=key, sha256=None if not distinct else next(iter(distinct)) if len(distinct) == 1
                         else sha256(sorted(distinct)), bytes=sum(len(raw) for raw in distinct.values()),
                         state='missing' if not distinct else 'present' if len(distinct) == 1 else 'conflict')
            a1.COVERAGE_ENTRY(entry)
            entries.append(entry)
        # Use smaller pages when entry bytes exhaust the bound before 1000.
        pages: list[dict[str, Any]] = []
        batch: list[dict[str, Any]] = []
        def emit(stream: str = stream, pages: list[dict[str, Any]] = pages,
                 batch: list[dict[str, Any]] = batch) -> None:
            page = dict(**TAG, day=day, stream=stream, page_seq=f'{len(pages) + 1:020d}',
                        previous_sha256=sha256(pages[-1]) if pages else None, entries=list(batch))
            files.obj(f'coverage/{day}/{stream}/{len(pages) + 1:05d}.json', page, a1.COVERAGE_PAGE)
            pages.append(page)
            batch.clear()
        for entry in entries:
            if len(batch) == 128:
                emit()
            batch.append(entry)
        if batch or not pages:
            emit()
        a1.coverage_root(pages)
        streams[stream] = pages
    return streams


def handoff(day: str, boot: str, result: dict[str, Any] | None) -> dict[str, Any]:
    if result is not None:
        a1.CLOSE_RESULT(result)
        require((result['day'], result['boot']) == (day, boot), 'close_result_identity_gap')
    value = dict(**TAG, day=day, boot=boot, expected_handoffs=1, attempts_lower=int(result is not None),
                 attempts_upper=1, landed=result is not None, reply_status='not_proven', failures_lower=0,
                 failures_upper=1, unknown_reply_upper=1, result_sha256=sha256(result) if result else None)
    a1.HANDOFF_ACCOUNTING(value)
    return value


def obligations(files: PackFiles, closure: dict[str, Any], pages: list[dict[str, Any]], *,
                original: Callable[[dict[str, Any]], tuple[dict[str, Any], str] | None],
                terminal: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]] | None],
                complete_through_us: int, closure_present: bool = True
                ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Issue-day horizons survive report selection and require forward originals.

    TerminalReconciliation is a caller seam: the callback supplies only terminals
    whose backing records were independently compared. This function additionally
    runs the frozen contextual join. An unavailable replay origin blocks output.
    """
    entries = []
    missing = []
    for page in pages:
        p1.PAGE(page)
        for event in page['events']:
            if event['phase'] not in ('authorize', 'replay'):
                continue
            args: dict[str, Any] = {}
            if event['phase'] == 'replay':
                found = original(event['retry_original'])
                if found is None:
                    raise ValueError('authorization_link_gap')
                args = dict(original_page=found[0], original_event_seq=found[1])
            entry = joins.authorization_obligation(page, event['seq'], **args)
            found_terminal = terminal(entry)
            if found_terminal is not None:
                end_page, locator = found_terminal
                joins.validate_terminal_join(entry, page, end_page, locator, **args)
                entry.update(status='terminal_reconciled', terminal_reference=locator)
            if complete_through_us < entry['horizon_at_us'] or entry['status'] != 'terminal_reconciled':
                missing.append('authorization_horizon_gap')
            entries.append(entry)
    require(not missing or bool(entries) or not pages, 'authorization_link_gap')
    ordered = sorted(entries, key=joins._obligation_key)
    result: list[dict[str, Any]] = []
    for offset in range(0, len(ordered), 64):
        value = dict(**TAG, day=closure['day'], boot=closure['boot'], page_seq=f'{len(result) + 1:020d}',
                     previous_sha256=sha256(result[-1]) if result else None, entries=ordered[offset:offset + 64])
        files.obj(f"obligations/{closure['day']}/{closure['boot']}/{len(result) + 1:05d}.json",
                  value, joins.AUTHORIZATION_OBLIGATION_PAGE)
        result.append(value)
    if not closure_present:
        missing.append('writer_closure_gap')
        return result, {}, missing
    projection = joins.obligation_projection(closure, ordered)
    files.obj(f"obligations/{closure['day']}/{closure['boot']}/projection.json", projection, joins.OBLIGATION_PROJECTION)
    return result, projection, missing


@dataclass(frozen=True)
class Population:
    expected: dict[str, list[str]]
    receipt_keys: tuple[str, ...]
    tails: tuple[Tail, ...]
    missing: tuple[str, ...]


def populations(source: ExactPages, *, day: str, boots: tuple[dict[str, Any], ...],
                contender_segments: list[dict[str, Any]], basis_keys: list[str],
                obligation_keys: list[str]) -> Population:
    """Derive writer and slot demand before looking at original row presence.

    BasisReferenceIndex and obligation page keys are retained explicit inputs;
    missing receipt predecessors remain tails with their original HWM, so this
    result cannot declare a shorter closed population.
    """
    from trusted_router.async_settle_shadow_v2_collect_ports import day_bounds

    left, right = day_bounds(day)
    writers = sorted(b['boot'] for b in boots if b['role'] == 'router'
                     and b['start_us'] < right and b['end_us'] > left)
    require(len(writers) == len(set(writers)), 'writer_roster_gap')
    counter_keys = [day + '/' + boot for boot in writers]
    source.expect(['counter/' + key for key in counter_keys])
    expected: dict[str, list[str]] = {stream: [] for stream in a1.COVERAGE_STREAMS}
    expected['close_result'] = list(counter_keys)
    expected['handoff_accounting'] = list(counter_keys)
    expected['health_basis'] = sorted(basis_keys)
    expected['authorization_obligation'] = sorted(obligation_keys)
    receipts: list[str] = []
    tails = []
    missing = []
    for key in counter_keys:
        candidates = set(source.read('counter/' + key))
        if len(candidates) != 1:
            missing.append('counter/' + key)
            continue
        counter = a1.COUNTER.parse(next(iter(candidates)))
        require(counter['day'] + '/' + counter['boot'] == key, 'artifact_gap')
        found = receipt_chain(source, counter)
        receipts.extend(found.keys)
        if found.tail is not None:
            tails.append(found.tail)
    for segment in contender_segments:
        from trusted_router.async_settle_shadow_v2_a2_evidence import LOAD_SEGMENT
        LOAD_SEGMENT(segment)
        if segment['role'] != 'router' or not segment['contender'] or segment['day'] != day:
            continue
        require(segment['boot'] in writers, 'writer_roster_gap')
        found = contender_chain(source, day=day, boot=segment['boot'],
            start_us=max(left, segment['start_us']), end_us=min(right, segment['end_us']))
        expected['publisher_trace'].extend(found.keys)
        if found.tail is not None:
            tails.append(found.tail)
    require(len(expected['publisher_trace']) == len(set(expected['publisher_trace'])), 'artifact_gap')
    return Population(expected, tuple(receipts), tuple(tails), tuple(missing))
