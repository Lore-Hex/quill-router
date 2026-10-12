"""Lossless internal gate results; the output attachment seam is deliberately closed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


@dataclass(frozen=True)
class Failure:
    gap: str
    rule: str
    subject: Subject
    count: int
    object_ids: tuple[str, ...]
    detail: str


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
    reads: float
    pending: float
    trust: float
    health: float
    publisher_reads: float
    publisher_writes: float
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

    @property
    def verdict(self) -> str:
        return 'BLOCKED' if self.gaps else 'PASS'


def render(result: Result) -> tuple[bytes, tuple[bytes, ...]]:
    """A2 seam: cannot invent coverage preimage or paginate failures as GATE_OUTPUT.

    Validate every currently expressible field, then raise the contract's own
    error. Even an internally passing bundle cannot be published as a PASS yet.
    """
    fields = dict(v=2, policy_version='shadow-v2', contract_revision='a1',
                  gate=result.gate, verdict=result.verdict,
                  completed_at_us=result.completed_at_us,
                  proof_manifest_sha256=result.proof_manifest_sha256,
                  prerequisite_ledger_root=result.prerequisite_ledger_root,
                  gaps=list(result.gaps), clock_start_us=result.report.clock_start_us,
                  elapsed_seconds=result.report.elapsed_seconds)
    for name, value in fields.items():
        a1.GATE_OUTPUT.fields[name](value)
    require(False, 'artifact_gap: undefined preimage: coverage_sha256; failure pages')
    raise AssertionError('unreachable')
