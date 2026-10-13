"""Prior gate graph boundary; an envelope alone is never a prerequisite.

The graph transport adapter is deliberately closed until original A2 reports,
input leaves and recursively verified preimages can be bound to this run.
Keeping this seam explicit prevents the former envelope-only bootstrap.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_gate_result import Subject

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


def verified_prior_gate(ctx: Context, name: str, value: dict[str, Any]) -> None:
    """Fail closed at the unresolved original-prior-graph adapter seam."""
    if ctx.gate not in ('shadow-serving', 'pre-flip') or name == 'serving':
        return
    stage = {'fleet': 'fleet-budgets', 'pre': 'fleet-budgets-pre',
             'serving': 'shadow-serving'}.get(name, name)
    ctx.issue('artifact_gap', 'prior_gate_original_graph_unavailable: ' + stage,
              Subject(wire=a2.O('prior_gate', name)))
    ctx.issue('pre_enable_budget' if name == 'pre' else 'boot_prerequisite_gap',
              'prior gate has no verified original graph')
