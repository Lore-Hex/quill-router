"""Every trust-gate cost proof mutation must still apply to the current source.

scripts/prove_trust_gate_cost.py refuses a mutation whose anchor does not occur
exactly once, but nothing runs the script routinely, so a refactor of
lease_eligibility left one anchor matching nothing and the proof stopped before
its last two controls. This check runs in the normal suite; the mutation loop
itself stays a manual command.
"""

from __future__ import annotations

import ast

import pytest

from scripts import prove_trust_gate_cost as proof

TEST_NAMES = {
    node.name
    for node in ast.parse((proof.ROOT / proof.TESTS).read_text(encoding="utf-8")).body
    if isinstance(node, ast.FunctionDef)
}


@pytest.mark.parametrize(
    ("name", "relative", "before", "after", "description"),
    proof.MUTATIONS,
    ids=[f"{name}:{description}" for name, _, _, _, description in proof.MUTATIONS],
)
def test_mutation_anchor_occurs_exactly_once_and_names_a_real_test(
    name: str, relative: str, before: str, after: str, description: str
) -> None:
    assert (proof.ROOT / relative).read_text(encoding="utf-8").count(before) == 1
    assert before != after
    assert name in TEST_NAMES
