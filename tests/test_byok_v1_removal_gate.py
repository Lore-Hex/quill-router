"""The gate on step 4: v1 envelope support may not be deleted unattested.

THE LAW
    If `src/trusted_router/byok_crypto.py` no longer supports v1 envelopes —
    the `ALGORITHM` constant holding the v1 literal, the `_aad` builder, and
    the v1 branch of `_envelope_aad` must all be present, or v1 is gone — then
    `docs/design/byok-aad-v2-attestations.json` must carry a passing zero-v1
    attestation for **every** standalone cloud, covering the surfaces this
    repository writes today. Otherwise CI fails and says what to run.

    While v1 support is present, this gate asserts nothing about the ledger. It
    is silent on every change except the one it exists for.

WHY THIS IS A PROOF AND NOT A TEST
    Step 4 of docs/design/byok-aad-v2-migration.md is the only irreversible
    step in that plan. A v1 envelope that outlives the deletion is not a
    revertable bug: the associated data that seals both the ciphertext and the
    KMS-wrapped DEK cannot be reconstructed once no implementation of the v1
    encoding exists, so the customer's provider key is gone. Everything else in
    the migration has a rollback row in §5. This one has "do not attempt this
    step until you are willing not to roll it back."

    What made it worth writing as a proof is the shape of the evidence. Until
    now the precondition for that irreversible step was a sentence in a
    markdown table: "clean audit: no BYOK or Broadcast secret rows existed".
    Nothing executes a sentence. Nothing dates it, re-runs it, or notices when
    it stops being true. This gate does not verify the migration — it verifies
    that somebody ran the precondition, on each cloud, and that the run
    established something.

THE REAL NEAR-MISS THIS ENCODES
    On 2026-08-15 the migration doc shows step 3 green on all three clouds. On
    AWS and Azure step 3 was a **read-only audit that found no rows to
    rewrite**. That is not the same claim as GCP's "7 migrated, 0 v1 after an
    independent audit", but the table renders them identically — and an audit
    that returns nothing looks exactly the same whether the database is
    migrated, empty, or unreachable behind a bad cursor or the wrong
    credentials. See tests/test_byok_v1_precondition.py for the outcome split
    that makes those cases distinguishable; this file is what refuses to let
    the ambiguous one authorise a deletion.

SCOPE LIMIT — what this gate does NOT establish
    * It does not prove no v1 envelope exists. It proves that a precondition
      run which could distinguish "none" from "did not look" was recorded for
      each cloud. The truth of those runs rests on the databases they queried,
      at the moment they queried them.
    * It cannot see `quill-cloud-proxy`. The enclave has its own `Algorithm`
      constant and its own v1 branch in `byokcache/cache.go`, and removing v1
      there is a separate decision this repository has no visibility into.
      Ordering across the two repos remains a human responsibility.
    * It reads the source text of one module. A v1 branch moved into a
      different module, or reimplemented inline, would leave this gate quiet.
      The probe below is deliberately narrow so that it fires on the edit the
      migration plan actually prescribes ("remove `_aad`, `ALGORITHM`, and the
      v1 branches"), and it says so rather than pretending to be exhaustive.
    * Nothing stops someone deleting this file along with v1. No in-repo guard
      can prevent that; what it can do is make the deliberate version of the
      edit cost a sentence in a pull request instead of nothing at all.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from trusted_router.byok_v1_attestations import (
    OUTCOME_CLEAN,
    STANDALONE_CLOUDS,
    empty_ledger,
    load_ledger,
    surface_fingerprint,
    zero_v1_blockers,
)

REPO = Path(__file__).resolve().parents[1]
BYOK_CRYPTO = REPO / "src" / "trusted_router" / "byok_crypto.py"
NAMESPACE_PROPERTY_TEST = REPO / "tests" / "test_byok_aad_namespace_property.py"
CHECK_SCRIPT = "scripts/check_no_v1_envelopes.py"

#: Pinned rather than imported. This module must still load — and still explain
#: itself — in the tree where `ALGORITHM` has just been deleted, which is
#: precisely the tree it is here to judge.
V1_ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"

#: The migration doc's step 4 says to delete this test along with v1. Until
#: then it is the standing record of the v1 collision, and deleting it early
#: would erase the reason any of this exists.
V1_COLLISION_RECORD = "test_aad_encoding_is_not_injective_in_general"


@dataclass(frozen=True)
class V1Support:
    """What `byok_crypto.py` still knows about the v1 envelope format."""

    algorithm_constant: str | None
    has_aad_builder: bool
    dispatch_selects_v1: bool

    @property
    def present(self) -> bool:
        return not self.missing

    @property
    def missing(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if self.algorithm_constant != V1_ALGORITHM:
            gaps.append(
                f"the ALGORITHM constant (found {self.algorithm_constant!r}, "
                f"expected {V1_ALGORITHM!r})"
            )
        if not self.has_aad_builder:
            gaps.append("the _aad v1 associated-data builder")
        if not self.dispatch_selects_v1:
            gaps.append("the v1 branch of _envelope_aad")
        return tuple(gaps)


def probe_v1_support(source: str) -> V1Support:
    """Read the module's AST for the three pieces step 4 removes.

    An AST walk rather than a grep because the v1 algorithm string also appears
    in this file's own prose, in the module docstring of `byok_crypto.py`, and
    in comments — a grep would keep reporting v1 as present long after the code
    implementing it was gone, which is the failure mode that makes a guard
    worthless.
    """
    tree = ast.parse(source)
    algorithm_constant: str | None = None
    has_aad_builder = False
    dispatcher: ast.FunctionDef | None = None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ALGORITHM":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        algorithm_constant = node.value.value
        elif isinstance(node, ast.FunctionDef):
            if node.name == "_aad":
                has_aad_builder = True
            elif node.name == "_envelope_aad":
                dispatcher = node

    dispatch_selects_v1 = False
    if dispatcher is not None:
        compares_algorithm = any(
            isinstance(node, ast.Compare)
            and any(
                isinstance(comparator, ast.Name) and comparator.id == "ALGORITHM"
                for comparator in node.comparators
            )
            for node in ast.walk(dispatcher)
        )
        calls_v1_builder = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_aad"
            for node in ast.walk(dispatcher)
        )
        dispatch_selects_v1 = compares_algorithm and calls_v1_builder

    return V1Support(
        algorithm_constant=algorithm_constant,
        has_aad_builder=has_aad_builder,
        dispatch_selects_v1=dispatch_selects_v1,
    )


def gate(source: str, ledger: dict[str, Any]) -> str | None:
    """The CI verdict: a failure message, or None when there is nothing to say."""
    support = probe_v1_support(source)
    if support.present:
        return None
    blockers = zero_v1_blockers(ledger)
    if not blockers:
        return None
    removed = "\n".join(f"  - {gap}" for gap in support.missing)
    still_blocking = "\n".join(f"  - {blocker}" for blocker in blockers)
    return (
        "v1 BYOK envelope support is being removed from src/trusted_router/byok_crypto.py:\n"
        f"{removed}\n"
        "\n"
        "but no zero-v1 attestation exists for every cloud. This is step 4 of\n"
        "docs/design/byok-aad-v2-migration.md and it cannot be rolled back: a v1 envelope\n"
        "that survives the deletion is a customer's BYOK provider key that nothing can\n"
        "decrypt again, because the AAD sealing both the ciphertext and the wrapped DEK\n"
        "cannot be rebuilt from a format no implementation carries.\n"
        "\n"
        "Each cloud is a standalone deployment with its own database, so each one needs\n"
        "its own run:\n"
        f"    uv run python {CHECK_SCRIPT} --backend <spanner|postgres> \\\n"
        "        --cloud <" + "|".join(STANDALONE_CLOUDS) + "> --record --operator <you>\n"
        "then commit docs/design/byok-aad-v2-attestations.json.\n"
        "\n"
        "Still blocking:\n"
        f"{still_blocking}"
    )


# --------------------------------------------------------------- the gate ---


def test_v1_support_is_not_removed_before_every_cloud_attests_zero_v1() -> None:
    """The gate itself, against the tree as committed."""
    verdict = gate(BYOK_CRYPTO.read_text(), load_ledger())

    if verdict is not None:
        # pytest.fail rather than assert: the verdict is a multi-line
        # instruction sheet, and assertion rewriting would print it as one
        # escaped string. The whole value of this failure is that it reads.
        pytest.fail(verdict, pytrace=False)


def test_the_probe_sees_v1_support_in_the_module_today() -> None:
    """Anti-vacuity, first half.

    The gate above is quiet today because v1 support is present. If the probe
    had silently stopped recognising it — a renamed helper, a restructured
    dispatch — the gate would be quiet for the opposite reason and would never
    fire again. This asserts the reason.
    """
    support = probe_v1_support(BYOK_CRYPTO.read_text())

    assert support.algorithm_constant == V1_ALGORITHM
    assert support.has_aad_builder
    assert support.dispatch_selects_v1
    assert support.present


# ------------------------------------------- the edits it must not permit ---


@pytest.fixture(name="v1_source")
def _v1_source() -> str:
    """The module as committed, while it still has v1 support to remove.

    The tests below work by performing step 4's edits on the real source. Once
    step 4 has actually been performed there is nothing left to mutate, and the
    two tests above are the ones that speak. Skipping here keeps that moment
    legible: a failing gate plus a handful of "already removed" skips, rather
    than four mutation helpers failing about text they cannot find.
    """
    source = BYOK_CRYPTO.read_text()
    if not probe_v1_support(source).present:
        pytest.skip("v1 support is already removed; the gate test above is the live one")
    return source


def _without_function(source: str, name: str) -> str:
    tree = ast.parse(source)
    target = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return _without_lines(source, target)


def _without_v1_dispatch(source: str) -> str:
    """Delete the `if algorithm == ALGORITHM:` arm of `_envelope_aad`.

    Located through the AST rather than by matching source text, so that
    reformatting `byok_crypto.py` cannot turn this into a mutation that
    silently stops mutating — a no-op mutation makes every test below pass for
    the wrong reason.
    """
    tree = ast.parse(source)
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_envelope_aad"
    )
    branch = next(
        node
        for node in dispatcher.body
        if isinstance(node, ast.If)
        and any(
            isinstance(comparator, ast.Name) and comparator.id == "ALGORITHM"
            for comparator in getattr(node.test, "comparators", [])
        )
    )
    return _without_lines(source, branch)


def _without_algorithm_constant(source: str) -> str:
    tree = ast.parse(source)
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "ALGORITHM" for target in node.targets
        )
    )
    return _without_lines(source, assignment)


def _algorithm_constant_retargeted(source: str) -> str:
    """The subtle one: the constant survives, pointing at the v2 literal.

    A stored v1 row then dispatches to the v2 AAD builder and fails to open,
    which surfaces as an InvalidTag that looks like corruption rather than like
    a migration mistake.
    """
    return source.replace(
        f'ALGORITHM = "{V1_ALGORITHM}"', 'ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"'
    )


def _without_lines(source: str, node: ast.stmt) -> str:
    lines = source.splitlines(keepends=True)
    assert node.end_lineno is not None
    del lines[node.lineno - 1 : node.end_lineno]
    return "".join(lines)


@pytest.mark.parametrize(
    ("mutate", "expected_gap"),
    [
        (_without_v1_dispatch, "the v1 branch of _envelope_aad"),
        (lambda source: _without_function(source, "_aad"), "the _aad v1 associated-data builder"),
        (_without_algorithm_constant, "the ALGORITHM constant"),
        (_algorithm_constant_retargeted, "the ALGORITHM constant"),
    ],
)
def test_the_gate_fires_on_each_half_of_the_v1_deletion(
    v1_source: str, mutate: Any, expected_gap: str
) -> None:
    """Every piece step 4 removes, removed one at a time, from the real module.

    Partial removals matter as much as the whole edit: deleting the dispatch
    branch alone already makes every stored v1 envelope unreadable, and it is
    the smallest diff that does so.
    """
    mutated = mutate(v1_source)
    assert mutated != v1_source, "the mutation did not apply"

    support = probe_v1_support(mutated)
    verdict = gate(mutated, load_ledger())

    assert not support.present
    assert any(expected_gap in gap for gap in support.missing), support.missing
    assert verdict is not None, "v1 was removed with no attestation and the gate stayed quiet"
    assert CHECK_SCRIPT in verdict
    for cloud in STANDALONE_CLOUDS:
        assert cloud in verdict, "the failure must name every cloud that still owes a run"


def test_the_gate_permits_the_removal_once_every_cloud_attests(v1_source: str) -> None:
    """Not a "never delete v1" test, which would simply be deleted.

    The gate exists to sequence the work, not to forbid it. Given a ledger in
    which all three deployments attest zero v1 envelopes, the same edit the
    test above rejects is allowed through without further argument.
    """
    ledger = empty_ledger()
    for cloud in STANDALONE_CLOUDS:
        ledger["attestations"][cloud] = {
            "cloud": cloud,
            "outcome": OUTCOME_CLEAN,
            "recorded_at": "2026-09-01T00:00:00+00:00",
            "backend": "spanner",
            "surface_fingerprint": surface_fingerprint(),
            "rows_scanned": 12,
            "rows_scanned_by_kind": {"byok": 12},
            "envelopes_seen": 12,
            "v1_envelopes": 0,
            "v2_envelopes": 12,
            "census_migrated_kind_counts": {"byok": 12},
            "census_sampled_kinds": ["byok", "workspace"],
            "operator": "release-engineer@lorehex.co",
            "note": "step 4 precondition",
        }
    assert zero_v1_blockers(ledger) == []

    assert gate(_without_v1_dispatch(v1_source), ledger) is None


def test_an_unrelated_edit_to_byok_crypto_keeps_the_gate_quiet(v1_source: str) -> None:
    """The annoyance check. Touching the file must not summon the gate."""
    source = v1_source.replace(
        "def _b64(value: bytes) -> str:",
        "def _b64(value: bytes) -> str:\n    # an ordinary comment\n",
    )
    assert source != v1_source

    assert gate(source, load_ledger()) is None


# ---------------------------------------------- the record step 4 deletes ---


def _defines(path: Path, name: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef) and node.name == name
        for node in ast.walk(ast.parse(path.read_text()))
    )


def test_the_v1_collision_record_lives_exactly_as_long_as_v1_does() -> None:
    """`test_aad_encoding_is_not_injective_in_general` and v1 go together.

    The migration doc lists deleting that test as a step-4 item, and it is
    correct to delete it then: it asserts `_aad("a:b", "c") == _aad("a", "b:c")`,
    which cannot even be expressed once `_aad` is gone. Deleting it EARLIER is
    the edit worth catching — it is the only standing record that the v1
    encoding is not injective, and losing it while v1 envelopes are still
    stored erases the reason the migration exists. Without this assertion that
    early deletion shows up as nothing at all; with it, the collection error
    that would otherwise appear becomes a sentence.
    """
    v1_supported = probe_v1_support(BYOK_CRYPTO.read_text()).present
    record_kept = _defines(NAMESPACE_PROPERTY_TEST, V1_COLLISION_RECORD)

    assert v1_supported == record_kept, (
        f"{V1_COLLISION_RECORD} is "
        f"{'present' if record_kept else 'absent'} in {NAMESPACE_PROPERTY_TEST.name} while v1 "
        f"envelope support is {'present' if v1_supported else 'absent'} in byok_crypto.py. They "
        "are removed together, in step 4, after every cloud attests zero v1 envelopes — never "
        "before. If the test was renamed rather than deleted, update V1_COLLISION_RECORD here."
    )
