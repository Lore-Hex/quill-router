"""Evaluation seam for the validated A2 load segments omitted by Budget.

Y may supply segments through BudgetEvidence, or through the existing rooted
Bundle.a2_streams (segments/<v2_load.segments.root>). No wire schema is added.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.async_settle_shadow_v2_gate_inputs import Budget, Bundle


@dataclass(frozen=True)
class BudgetEvidence(Budget):
    load_segments: tuple[dict[str, Any], ...] = ()


def segments(bundle: Bundle, stage: str) -> tuple[dict[str, Any], ...] | None:
    if isinstance(bundle.budget, BudgetEvidence):
        return bundle.budget.load_segments
    envelope = bundle.a2_payloads.get('proof.post_budget_sha256' if stage != 'fleet-budgets-pre' else 'proof.pre_budget_sha256')
    if envelope is None:
        return None
    return tuple(bundle.a2_streams['segments/' + envelope['v2_load']['segments']['root']]) if 'segments/' + envelope['v2_load']['segments']['root'] in bundle.a2_streams else None
