"""Companion loading ports; the frozen evaluation interface is unchanged.

Release authority is supplied independently of the untrusted export. Exact
review records and CI artifacts must have been approved in that release plan.
The loader cannot manufacture approval from a reviewer name or its own source.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from trusted_router.async_settle_shadow_v2_gate_inputs import Ports


@dataclass(frozen=True)
class RetainedResult:
    output: bytes
    opened: tuple[str, ...]
    calls: Mapping[str, int]
    components: Mapping[str, Any] = field(default_factory=dict)


class RetainedRunner(Protocol):
    def __call__(self, entry_point: str, payload: Any,
                 files: Mapping[str, bytes]) -> RetainedResult: ...


@dataclass(frozen=True)
class ReleasePlan:
    """Tool-approved pins, not assertions copied out of the evidence bundle.

    ``artifacts`` includes the exact review/CI files and transitive dependencies.
    ``criteria`` freezes the check inventory for each external/prerequisite proof.
    ``verifiers`` pins source, fixture and ordered public descriptor bytes.
    The original plan is itself an inventoried dependency, independently pinned
    by the trusted tool's release input.
    """
    path: str
    raw: bytes
    artifacts: Mapping[str, str]
    criteria: Mapping[str, tuple[str, ...]]
    verifiers: Mapping[str, tuple[str, str, str | None]]
    required: tuple[str, ...]


@dataclass(frozen=True)
class LoadingPorts(Ports):
    retained: RetainedRunner | None = None
    gate_roster: Callable[[list[dict[str, Any]], list[dict[str, Any]]], bool] | None = None
    release: ReleasePlan | None = None
    # Original producer-specific parsers. No unknown opaque format passes.
    measurements: Mapping[str, Callable[[bytes], Mapping[str, Any]]] = field(default_factory=dict)
