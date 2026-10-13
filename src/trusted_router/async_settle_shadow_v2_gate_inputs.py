"""Internal decoded inputs and ports, not a new evidence wire contract.

A2 defines serialization/preimages; the P5 adapter must establish their semantic
binding before populating these internal views. A digest alone is insufficient.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from trusted_router.async_settle_shadow_v2_a2_contract import BindingVerifier, CertificateVerifier
from trusted_router.detached_jws import TrustedKey

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_certificate import ReceiptVerification


class ReceiptVerifier(Protocol):
    def __call__(self, claims: dict[str, Any], *, returned_key: tuple[str, str],
                 page: bytes | None) -> ReceiptVerification: ...


class ResetCollector(Protocol):
    def __call__(self, resets: list[dict[str, Any]], serving: list[tuple[int, int, str]],
                 resolutions: list[dict[str, Any]], manifests: dict[str, Any]) -> None: ...


class LegacyReport(Protocol):
    def __call__(self, rows: list[dict[str, Any]], days: list[str], proof: dict[str, Any],
                 *, not_before_us: int = 0) -> dict[str, Any]: ...


class RosterCoverage(Protocol):
    def __call__(self, rows: list[dict[str, Any]]) -> set[tuple[Any, ...]]: ...


@dataclass(frozen=True)
class VerifierApproval:
    """Trusted tool's imported callable and approved original public artifacts."""
    entry_point: str
    function: Callable[..., Any]
    source: bytes
    fixture: bytes
    keyset: bytes
    keys: tuple[TrustedKey, ...]
    source_pin: str
    fixture_pin: str


@dataclass(frozen=True)
class Ports:
    verify_certificate: CertificateVerifier
    verify_original_receipt: ReceiptVerifier
    verify_binding: BindingVerifier
    comparator_positive: Callable[[dict[str, Any]], bool]
    positive_sample: Callable[[dict[str, Any]], bool]
    known_exclusion: Callable[..., bool]
    fleet_budgets_pre: Callable[[dict[str, Any]], dict[str, Any]]
    collect_rollbacks: ResetCollector
    report: LegacyReport
    post_flip: Callable[[dict[str, Any]], dict[str, Any]]
    approvals: tuple[VerifierApproval, ...] = ()
    approved_files: Mapping[str, str] = field(default_factory=dict)
    read_release_file: Callable[[str], bytes] | None = None
    roster_coverage: RosterCoverage | None = None


@dataclass(frozen=True, order=True)
class Key:
    kind: str
    id: str


@dataclass(frozen=True)
class Row:
    key: Key
    raw: bytes


@dataclass(frozen=True)
class Boot:
    boot: str
    role: str
    revision: str
    region: str
    start_us: int
    end_us: int | None
    contender: bool = False
    termination_applies: bool = False


@dataclass(frozen=True)
class Inventory:
    boots: tuple[Boot, ...]


@dataclass(frozen=True)
class Cohort:
    workspace_count: int
    regional_seeds: bool
    primary_region: str


@dataclass(frozen=True)
class Rate:
    boot: str
    start_us: int
    end_us: int
    workspace_reads: int
    health_reads: int
    publisher_reads: int = 0
    publisher_writes: int = 0
    burst_reads: int = 0
    burst_pending: int = 0


@dataclass(frozen=True)
class Budget:
    pre_input: dict[str, Any]
    regional_caps: Mapping[str, int]
    fleet_cap: int
    surge: int
    publisher_cap: int
    regional_reads: Mapping[str, float]
    regional_pending: Mapping[str, float]
    fleet_reads: float
    publisher_reads: float
    publisher_writes: float
    cpu_peak: float
    cpu_increment: float
    component_seconds: Mapping[str, float]
    rates: tuple[Rate, ...]
    pre_completed_us: int
    post_completed_us: int
    pre_digest: str
    post_pre_digest: str
    post_workspace_count: int
    post_regional_caps: Mapping[str, int]
    export_operations: int
    export_bytes: int
    close_transactions: int
    close_rows: int
    close_elapsed_us: int
    handoff_elapsed_us: int
    grace_us: int
    publisher_unresolved_rows_per_second: float | None = None
    publisher_control_rows_per_second: float | None = None
    publisher_transactions_per_second: float | None = None
    publisher_rpc_per_second: float | None = None
    metadata_bytes_per_second: float | None = None
    export_passes: int = 1
    export_root_writes: int = 0
    export_write_bytes: int = 0
    cleanup_spanner_keys: int = 0
    cleanup_sink_keys: int = 0
    cleanup_seconds: int = 30


@dataclass(frozen=True)
class ExternalProof:
    name: str
    router_revision: str
    enclave_revision: str
    raw: bytes
    sha256: str
    passed: bool


@dataclass(frozen=True)
class External:
    proofs: tuple[ExternalProof, ...]
    required: tuple[str, ...]
    drain_selected: bool
    signer_checked: bool
    attestation_checked: bool
    exposure_accepted: bool
    post_input: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResetSupport:
    rows: tuple[dict[str, Any], ...]
    proof: dict[str, Any]
    resets: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Signature:
    """Ephemeral verification material; never serialized into a report."""
    object_id: str
    token: str
    keys: tuple[TrustedKey, ...]
    now: int
    expected: dict[str, Any] = field(default_factory=dict)
    attempt: dict[str, Any] = field(default_factory=dict)
    server_attempt: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Preimage:
    """Supplied original attachment bytes for contract decoding and P5 binding."""
    name: str
    raw: bytes


@dataclass(frozen=True)
class Bundle:
    rows: tuple[Row, ...] = ()
    proof: bytes | None = None
    ledgers: tuple[bytes, ...] = ()
    attachments: tuple[Row, ...] = ()
    inventory: Inventory | None = None
    cohort: Cohort | None = None
    budget: Budget | None = None
    external: External | None = None
    reset_support: ResetSupport | None = None
    signatures: tuple[Signature, ...] = ()
    transport: bytes | None = None
    fleet: bytes | None = None
    pre: bytes | None = None
    serving: bytes | None = None
    completed_at_us: int = 0
    days: tuple[str, ...] = ()
    undefined_preimages: tuple[str, ...] = ()
    preimages: tuple[Preimage, ...] = ()
    a2_payloads: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    window: Mapping[str, int] | None = None
    a2_streams: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
