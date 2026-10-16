"""Named producer context omitted by the frozen generic PROOF_RESULT wrapper.

A prerequisite is for a specific boot, not just a revision. These independently
approved original CI source records establish that context; generic daily gate
outputs continue to fail at the named contextual-parser seam.
"""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_gate_inputs import PREREQUISITE_CONTEXT
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_release import validate_proof
from trusted_router.async_settle_shadow_v2_schema import (
    require,
)


def prerequisite(graph: PayloadGraph, proof: dict[str, Any], ledger: dict[str, Any], field: str) -> None:
    validate_proof(graph, proof)
    contexts = []
    for reference in graph.detail(proof['sources'], 'files'):
        try:
            context = graph.object(reference, PREREQUISITE_CONTEXT)
        except ValueError:
            continue
        contexts.append(context)
    require(len(contexts) == 1, 'evidence_schema_gap')
    context = contexts[0]
    require(all(context[k] == ledger[k] for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')),
            'evidence_schema_gap')
    require(context['criterion'] == field and context['completed_at_us'] == proof['completed_at_us'], 'evidence_schema_gap')
