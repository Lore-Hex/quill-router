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
