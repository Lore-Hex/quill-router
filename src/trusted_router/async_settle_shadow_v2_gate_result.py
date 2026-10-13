"""Lossless internal gate results; the output attachment seam is deliberately closed."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Literal

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router.async_settle_shadow_v2_schema import require


@dataclass(frozen=True, order=True)
class Dimensions:
    adapter: str | None
    route_type: str | None
    streamed: bool | None


@dataclass(frozen=True)
class Subject:
    day: str | None = None
    boot: str | None = None
    phase: str | None = None
    dimensions: Dimensions | None = None
    object_id: str | None = None
    wire: dict[str, Any] | None = None


@dataclass(frozen=True)
class Failure:
    gap: str
    rule: str
    subject: dict[str, Any]
    count: int
    object_ids: tuple[str, ...]
    detail: str
    days: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureSummary:
    count: int
    day_counts: dict[str, int]


class FailureSet(tuple[Failure, ...]):
    """Only canonical prefixes are retained; exact totals travel separately."""
    summaries: dict[str, FailureSummary]

    def __new__(cls, prefix: Iterable[Failure], summaries: dict[str, FailureSummary]) -> FailureSet:
        result = super().__new__(cls, prefix)
        result.summaries = summaries
        return result


@dataclass(frozen=True)
class WriterReport:
    day: str
    boot: str
    a: int | None
    b: int | None
    c: int | None
    eligible: int | None
    yes: int | None
    no: int | None
    unknown: int | None
    prediction_state: Literal['exercised', 'not_exercised', 'unknown']
    unknown_ratio: float | None
    failure_ratio: float | None
    degraded_ratio: float | None
    original_count: int | None
    retry_count: int | None
    checkpoint_failures: int | None
    checkpoint_recoveries: int | None
    evidence_loss: int | None


@dataclass(frozen=True)
class Metric:
    name: str
    region: str | None
    count: int | None
    null_count: int | None
    minimum: int | None
    maximum: int | None
    total: int | None


@dataclass(frozen=True)
class Peak:
    region: str | None
    simultaneous: int
    reads: Fraction
    pending: Fraction
    trust: Fraction
    health: Fraction
    publisher_reads: Fraction
    publisher_writes: Fraction
    publisher_simultaneous: int


@dataclass(frozen=True)
class Report:
    comparator_covered: int | None
    comparator_demand: int | None
    admission_covered: int | None
    admission_demand: int | None
    writers: tuple[WriterReport, ...]
    peaks: tuple[Peak, ...] | None
    actual_regions: tuple[str, ...] | None
    timing: tuple[Metric, ...]
    deltas: tuple[Metric, ...]
    first_seed_us: int | None
    reset_timestamps_us: tuple[int, ...]
    gap_timestamps_us: tuple[int, ...]
    clock_start_us: int | None
    elapsed_seconds: int | None
    open_obligation_count: int | None


@dataclass(frozen=True)
class Result:
    gate: str
    completed_at_us: int
    proof_manifest_sha256: str | None
    prerequisite_ledger_root: str | None
    gaps: tuple[str, ...]
    failures: tuple[Failure, ...]
    report: Report
    gate_report: dict[str, Any] | None = None
    detail_streams: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    input_leaves: dict[str, bytes] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return 'BLOCKED' if self.gaps else 'PASS'


@dataclass(frozen=True)
class UnavailableGateOutput:
    """A diagnostic value, never an envelope, report, or successful verdict."""
    condition: str
    error: str = 'unavailable_required_report_commitments'

    def diagnostic(self) -> dict[str, str]:
        return dict(error=self.error, condition=self.condition, status='BLOCKED', policy_version='shadow-v2')



def render(result: Result) -> tuple[bytes, tuple[bytes, ...]] | UnavailableGateOutput:
    """Emit the frozen envelope/report/flat streams, validating the final graph."""
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_failures import listing
    from trusted_router.async_settle_shadow_v2_schema import sha256
    from trusted_router.detached_jws import canonical

    report = result.gate_report
    if report is None:
        return UnavailableGateOutput('proof_manifest_absent_or_undecodable' if result.prerequisite_ledger_root is None
            else 'observation_scope_artifacts_absent_or_undecodable')
    tallies, prefix = listing(result.failures, tuple(report['days']))
    require(tallies == report['failure_counts'], 'evidence_schema_gap')
    streams = result.detail_streams
    require(prefix == [entry for page in streams['failures'] for entry in page['entries']], 'evidence_schema_gap')
    output = dict(v=2, policy_version='shadow-v2', contract_revision='a1',
        gate=result.gate, verdict=result.verdict, completed_at_us=result.completed_at_us,
        proof_manifest_sha256=result.proof_manifest_sha256, prerequisite_ledger_root=result.prerequisite_ledger_root,
        gaps=list(result.gaps), clock_start_us=report['clock_start_us'],
        elapsed_seconds=report['elapsed_seconds'] if report['elapsed_seconds'] is not None else 0,
        coverage_sha256=sha256(report))
    raw = a1.GATE_OUTPUT.serialize(output)
    attachments = (a2.GATE_REPORT.serialize(report), *(canonical(page) for name in
        ('writers', 'regions', 'failures', 'input_inventory', 'reset_history') for page in streams[name]), *(raw for _, raw in sorted(result.input_leaves.items())))
    # This is deliberately the last operation. Failure is a tool error, even
    # when all predicates had previously accepted the input.
    contract.validate_gate_output(output, report, failures=streams['failures'], writers=streams['writers'],
        regions=streams['regions'], inputs=streams['input_inventory'], reset_history=streams['reset_history'],
        verifiers=report['verifiers'])
    return raw, attachments


# Recovery serializes a deliberately minimal report, independently of the public
# render dispatch. It still runs the unchanged final contract graph validator.
_render_blocked = render


def run(raw: bytes, ports: Any = None, *, gate: str = 'pre-flip',
        damaged_inputs: tuple[str, ...] = ()) -> tuple[Result | None, tuple[bytes, tuple[bytes, ...]] | UnavailableGateOutput]:
    """Outermost original-byte library boundary shared by both offline tools.

    Unexpected load/evaluate/render exceptions cannot certify success or leak
    exception text. Recovery reads only surviving original commitments and scope;
    it never retries the failed semantic pipeline or invents a timestamp.
    """
    from dataclasses import replace

    from trusted_router import async_settle_shadow_v2_gate_loader as loader
    from trusted_router import async_settle_shadow_v2_gates as gates
    try:
        bundle = loader.load(raw, ports)
        if damaged_inputs:
            bundle = replace(bundle, undefined_preimages=(*bundle.undefined_preimages, *damaged_inputs))
        result = gates.evaluate(bundle, ports, gate=gate)
        return result, render(result)
    except Exception:
        return _internal_error(raw, gate)


def _internal_error(raw: bytes, gate: str) -> tuple[Result | None, tuple[bytes, tuple[bytes, ...]] | UnavailableGateOutput]:
    """Small recovery seam: commitments, diagnostic scope, fixed G1 failure."""
    from dataclasses import replace

    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_a2_contract import REQUIRED_RULES
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context
    from trusted_router.async_settle_shadow_v2_gate_failures import TAG, detail, listing
    from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key
    from trusted_router.async_settle_shadow_v2_gate_loader import MAX_BUNDLE_BYTES
    from trusted_router.async_settle_shadow_v2_gate_loading import transport_children
    from trusted_router.async_settle_shadow_v2_schema import Schema, day, integer
    try:
        value, _ = transport_children(raw, MAX_BUNDLE_BYTES)
        proof_raw = value['proof'].encode('utf-8')
        proof = a1.PROOF_MANIFEST.parse(proof_raw)
    except (ValueError, KeyError, AttributeError, TypeError, UnicodeError):
        return None, UnavailableGateOutput('proof_manifest_absent_or_undecodable')
    days = []
    for selected in value.get('days', []):
        try:
            day(selected)
            days.append(selected)
        except (ValueError, TypeError):
            continue
    completed = value.get('completed_at_us')
    try:
        integer(completed)
    except ValueError:
        completed = None
    bundle = Bundle(proof=proof_raw, days=tuple(sorted(set(days))[:32]), completed_at_us=completed or 0)
    ctx = Context(bundle, None, gate, proof=proof)
    for row in value.get('rows', []):
        if row['kind'] != p1.KINDS[5]:
            continue
        try:
            ctx.rows[Key(row['kind'], row['id'])] = a1.validate_row(row['kind'], row['id'], row['raw'].encode())
        except (ValueError, UnicodeError):
            continue
    # Independent inventory is also a diagnostic source when no requested day,
    # proof roster or manifest survives. Validate the original envelope and its
    # exact serving FILE_REF; this supplies scope, never semantic authority.
    from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
    files = {entry['name']: entry['raw'].encode() for entry in value.get('preimages', [])}
    independent = []
    for name, original in files.items():
        if not name.endswith('manifest.independent_inventory_sha256'):
            continue
        try:
            envelope = a2.INDEPENDENT_INVENTORY.parse(original)
            reference = envelope['serving']
            serving = files[reference['path']]
            require(len(serving) == reference['bytes'] and file_digest(serving) == reference['sha256'])
            rows = retained_json(serving)
            require(type(rows) is list)
            for row in rows:
                if row['role'] != 'router':
                    continue
                for interval in row['intervals']:
                    left, right = interval['started_at_us'], interval['flushed_at_us']
                    p1.INTERVAL(dict(start_us=left, end_us=right))
                    require(left < right)
                    independent.append((left, right))
        except (ValueError, KeyError, TypeError, UnicodeError):
            continue
    # Recovery does not call the semantic scope selector: a bug in that stage
    # must not recur while building its diagnostic.
    import datetime as dt

    from trusted_router.async_settle_shadow_v2_schema import day_at
    intervals = list(independent)
    writers = [roster.get('router', {}) for roster in proof['rosters_by_day'].values()]
    writers.extend(manifest['router_writers'] for manifest in ctx.kind(p1.KINDS[5]) if 'router_writers' in manifest)
    intervals.extend((writer['lifetime']['start_us'], writer['lifetime']['end_us'])
        for roster in writers for writer in roster.values())
    surviving = []
    for left, right in intervals:
        try:
            p1.INTERVAL(dict(start_us=left, end_us=right))
            require(left < right)
            day_at(left)
            day_at(right - 1)
            surviving.append((left, right))
        except (ValueError, OverflowError):
            continue
    requested = []
    for selected in days:
        left = (dt.date.fromisoformat(selected) - dt.date(1970, 1, 1)).days * 86400000000
        if left >= 0:
            requested.append((selected, left, left + 86400000000))
    if not surviving:
        surviving = [(left, right) for _, left, right in requested]
    days_tuple = tuple(sorted({selected for selected, _, _ in requested})[:32])
    if not days_tuple:
        days_tuple = tuple(sorted({day_at(at) for left, right in surviving
            for at in range(left // 86400000000 * 86400000000,
                            min(right, left + 32 * 86400000000), 86400000000)})[:32])
    scope = dict(start_us=min(left for left, _ in surviving), end_us=max(right for _, right in surviving)) if surviving else None
    if scope is None:
        return None, UnavailableGateOutput('observation_scope_artifacts_absent_or_undecodable')
    ctx.bundle = replace(bundle, days=days_tuple, completed_at_us=scope['end_us'] if completed is None else completed)
    subject = a2.O('a2_file', 'internal-error')
    failures = (Failure('artifact_gap', 'G1', subject, 1, (), 'internal gate error'),)
    metrics = Report(None, None, None, None, (), None, None, (), (), None, (), (), None, None, None)
    tallies, listed = listing(failures, days_tuple)
    streams, references = {}, {}
    for stream in ('writers', 'regions', 'failures', 'input_inventory', 'reset_history'):
        references[stream], streams[stream] = detail(listed if stream == 'failures' else [], stream)
    def unknown(schema: Any) -> dict[str, Any]:
        # Recursion is over frozen schema definitions, never input-controlled.
        return {name: unknown(check) if isinstance(check, Schema) else None for name, check in schema.fields.items()}
    groups = ('comparator', 'admission', 'partition', 'attempts', 'checkpoint', 'handoff',
              'publisher', 'fleet_peaks', 'timing', 'deltas', 'null_counts')
    aggregates = {name: unknown(a2.GATE_REPORT.fields[name]) for name in groups}
    for name in ('comparator', 'admission'):
        aggregates[name]['coverage']['state'] = 'unevaluable' if 'G5' in REQUIRED_RULES[gate] else 'not_required'
    report = dict(**TAG, gate=gate, completed_at_us=ctx.bundle.completed_at_us,
        proof_manifest_sha256=file_digest(proof_raw), prerequisite_ledger_root=proof['ledger_root'],
        days=list(days_tuple), window=scope, **aggregates, **references,
        first_seed_at_us=None, first_reset_at_us=None, first_gap_at_us=None, clock_start_us=None,
        elapsed_seconds=None, failure_counts=tallies, verifiers=dict(certificate=None, binding=None, retained_tools=[]),
        complete=False, rules={rule: 'fail' if rule == 'G1' else 'unevaluable' if rule in REQUIRED_RULES[gate]
            else 'not_required' for rule in (f'G{i}' for i in range(1, 12))})
    leaves: dict[str, bytes] = {}
    result = Result(gate, ctx.bundle.completed_at_us, file_digest(proof_raw), proof['ledger_root'],
                    ('artifact_gap',), failures, metrics, report, streams, leaves)
    return result, _render_blocked(result)
