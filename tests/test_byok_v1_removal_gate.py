"""The gate on step 4: v1 envelope support may not be deleted unattested.

THE LAW
    If a v1-shaped envelope — sealed exactly as the stored rows were sealed —
    no longer opens through `decrypt_byok_secret` and `decrypt_control_secret`,
    then `docs/design/byok-aad-v2-attestations.json` must carry a passing
    zero-v1 attestation for **every** standalone cloud, covering the surfaces
    this repository writes today. Otherwise CI fails and says what to run.

    While a v1 envelope still opens, this gate asserts nothing about the
    ledger.

WHY THE TEST IS BEHAVIOURAL AND THE AST PROBE IS ONLY THE EXPLANATION
    An earlier revision of this file decided by reading `byok_crypto.py`'s AST
    for three named pieces: the `ALGORITHM` constant, the `_aad` builder, and
    the v1 arm of `_envelope_aad`. That got it wrong in both directions.

    It stayed **quiet** on the removal the migration doc's own step-4 bullet
    invites — rejecting v1 at `decrypt_byok_secret`/`decrypt_control_secret`
    while leaving all three pieces physically in place. Every stored v1
    envelope is permanently unopenable after that edit, and the gate returned
    no verdict at all.

    It **fired** on edits that changed nothing: annotating the constant as
    `ALGORITHM: Final[str]` (an `ast.AnnAssign`, which the probe did not match)
    and extracting the v1 arm into a helper that `_envelope_aad` calls. Both
    preserve v1 completely. A gate that cries wolf on a type annotation is a
    gate somebody deletes, and deleting it is the whole failure.

    So the verdict now comes from sealing a v1 envelope the way a stored row
    was sealed and asking the public API to open it. Refactors are invisible to
    that; anything that stops a stored row opening is not, wherever the change
    lives. The AST probe survives only to name which pieces went missing in the
    failure message, and it no longer decides anything on its own.

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
    * The behavioural probe opens a v1 envelope wrapped by the local/test key
      wrapper (`KeyWrapperConfig(environment="test")`). It establishes that the
      v1 AAD path and the algorithm dispatch still work; it does not exercise
      the KMS wrapper, so a v1 regression that lives only in `GcpKmsKeyWrapper`
      is out of scope here.
    * It probes the two public decrypt entry points. A caller that reaches a v1
      envelope by some other route is not covered, and neither is a v1 branch
      that some *other* module reimplements privately — but note that a v1 row
      which those two functions refuse is already unopenable in production,
      which is the condition the gate cares about.
    * The v1 wire format (`V1_ALGORITHM` and `_v1_aad` below) is pinned here
      rather than imported, because it has to stay expressible in the tree
      where step 4 has deleted the originals. A pin can drift; while `_aad`
      still exists, `test_the_pinned_v1_format_is_the_one_the_module_seals`
      holds them equal.
    * Nothing stops someone deleting this file along with v1. No in-repo guard
      can prevent that; what it can do is make the deliberate version of the
      edit cost a sentence in a pull request instead of nothing at all.
"""

from __future__ import annotations

import ast
import secrets as secrets_module
import sys
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trusted_router.byok_v1_attestations import (
    OUTCOME_CLEAN,
    STANDALONE_CLOUDS,
    empty_ledger,
    load_ledger,
    surface_fingerprint,
    zero_v1_blockers,
)
from trusted_router.key_management import KeyWrapperConfig
from trusted_router.storage_models import EncryptedSecretEnvelope

REPO = Path(__file__).resolve().parents[1]
BYOK_CRYPTO = REPO / "src" / "trusted_router" / "byok_crypto.py"
NAMESPACE_PROPERTY_TEST = REPO / "tests" / "test_byok_aad_namespace_property.py"
CHECK_SCRIPT = "scripts/check_no_v1_envelopes.py"

#: Pinned rather than imported. This module must still load — and still explain
#: itself — in the tree where `ALGORITHM` has just been deleted, which is
#: precisely the tree it is here to judge.
V1_ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"

#: The local/test wrapping path, so the probe needs no KMS. See the scope limit.
PROBE_SETTINGS = KeyWrapperConfig(environment="test")


def _v1_aad(workspace_id: str, context: str) -> bytes:
    """The v1 associated data, as it was sealed into the rows that exist.

    Pinned, not imported, for the same reason as `V1_ALGORITHM`: a stored row
    carries these bytes whatever the module later calls the function that
    produced them, and the probe has to keep working after step 4 deletes
    `_aad`. `test_the_pinned_v1_format_is_the_one_the_module_seals` holds this
    equal to the real `_aad` while the real one exists.
    """
    return f"trustedrouter:byok:{workspace_id}:{context}".encode()


#: The migration doc's step 4 says to delete this test along with v1. Until
#: then it is the standing record of the v1 collision, and deleting it early
#: would erase the reason any of this exists.
V1_COLLISION_RECORD = "test_aad_encoding_is_not_injective_in_general"


@dataclass(frozen=True)
class V1Support:
    """What `byok_crypto.py`'s SOURCE still mentions of the v1 envelope format.

    Descriptive only. `present` here is not the gate's verdict — a refactor can
    move any of these and keep v1 working perfectly, and an entry-point guard
    can leave all three in place and break it. `v1_envelope_opens()` decides;
    this explains.
    """

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

    `ast.AnnAssign` is matched as well as `ast.Assign`: `ALGORITHM: Final[str] =
    "…"` is the same constant, and reading it as an absence used to turn a type
    annotation into a CI failure about permanently unreadable customer keys.
    """
    tree = ast.parse(source)
    algorithm_constant: str | None = None
    has_aad_builder = False
    dispatcher: ast.FunctionDef | None = None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
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


@dataclass(frozen=True)
class V1Behaviour:
    """Whether a stored v1 envelope still opens through the public API."""

    #: True: it opened. False: it did not. None: the probe could not be built,
    #: so this says nothing and the AST probe is all there is.
    opens: bool | None
    reason: str

    @property
    def removed(self) -> bool:
        """True only for a definite refusal. `None` is never treated as removal.

        An indeterminate probe must not fire the gate: the message it prints is
        a multi-line warning about unrecoverable customer keys, and printing it
        because an import failed is how a guard gets deleted.
        """
        return self.opens is False


def load_module(source: str, name: str = "byok_crypto_under_probe") -> types.ModuleType:
    """Execute `source` as a module, so the probe can run against a mutation.

    The mutation tests below edit the real `byok_crypto.py` text and need the
    edited version to be *callable*, not merely parseable. Registered in
    `sys.modules` under a scratch name while it executes, because dataclass and
    typing machinery resolves `__module__` through there.

    The `exec` is the point rather than a shortcut: the input is always this
    repository's own source file, read from disk, optionally with a mutation
    this file wrote. Nothing external reaches it.
    """
    module = types.ModuleType(name)
    module.__file__ = str(BYOK_CRYPTO)
    sys.modules[name] = module
    try:
        exec(compile(source, str(BYOK_CRYPTO), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


def seal_v1(module: types.ModuleType, secret: str, *, workspace_id: str, context: str) -> Any:
    """Build the envelope a pre-migration row holds, using only v2-era helpers.

    Deliberately does not touch `ALGORITHM` or `_aad`: the algorithm string and
    the AAD bytes are pinned above, exactly as a stored row carries them. So
    retargeting the constant, renaming the builder, or deleting either is
    invisible to the sealing side and shows up only where it matters — in
    whether the module can still open the result.
    """
    dek = secrets_module.token_bytes(32)
    nonce = secrets_module.token_bytes(12)
    dek_nonce = secrets_module.token_bytes(12)
    aad = _v1_aad(workspace_id, context)
    return EncryptedSecretEnvelope(
        algorithm=V1_ALGORITHM,
        key_ref=module._key_ref(PROBE_SETTINGS),
        encrypted_dek=module._b64(module._wrap_dek(dek, dek_nonce, aad, PROBE_SETTINGS)),
        dek_nonce=module._b64(dek_nonce),
        ciphertext=module._b64(AESGCM(dek).encrypt(nonce, secret.encode(), aad)),
        nonce=module._b64(nonce),
    )


def v1_envelope_opens(module: types.ModuleType) -> V1Behaviour:
    """The gate's actual question: can a stored v1 row still be decrypted?

    Both public entry points are tried, because they dispatch differently —
    `decrypt_control_secret` picks the namespace from the algorithm — and
    because rejecting v1 at either one is enough to make a stored row
    unopenable in production.
    """
    workspace_id = str(uuid.uuid4())
    try:
        provider_envelope = seal_v1(
            module, "v1-provider", workspace_id=workspace_id, context="openai"
        )
        control_envelope = seal_v1(
            module, "v1-control", workspace_id=workspace_id, context="broadcast:bdst_1:api_key"
        )
    except Exception as exc:
        return V1Behaviour(
            opens=None,
            reason=(
                f"could not seal a v1 envelope with this module's own helpers "
                f"({type(exc).__name__}: {exc}), so the behavioural probe says nothing"
            ),
        )

    try:
        opened_provider = module.decrypt_byok_secret(
            provider_envelope, PROBE_SETTINGS, workspace_id=workspace_id, provider="openai"
        )
    except Exception as exc:
        return V1Behaviour(
            opens=False,
            reason=(
                f"decrypt_byok_secret refused a stored v1 envelope: {type(exc).__name__}: {exc}"
            ),
        )
    try:
        opened_control = module.decrypt_control_secret(
            control_envelope,
            PROBE_SETTINGS,
            workspace_id=workspace_id,
            purpose="broadcast:bdst_1:api_key",
        )
    except Exception as exc:
        return V1Behaviour(
            opens=False,
            reason=(
                f"decrypt_control_secret refused a stored v1 envelope: {type(exc).__name__}: {exc}"
            ),
        )
    if (opened_provider, opened_control) != ("v1-provider", "v1-control"):
        return V1Behaviour(
            opens=False,
            reason="a v1 envelope decrypted to the wrong plaintext, which is worse than a refusal",
        )
    return V1Behaviour(opens=True, reason="a v1 envelope still opens through both entry points")


def gate(source: str, ledger: dict[str, Any]) -> str | None:
    """The CI verdict: a failure message, or None when there is nothing to say.

    Behaviour decides. The AST probe only supplies the "what changed" lines,
    and is consulted for a verdict solely when the behavioural probe could not
    run at all.
    """
    support = probe_v1_support(source)
    behaviour = v1_envelope_opens(load_module(source))
    if behaviour.opens is True:
        return None
    if behaviour.opens is None and support.present:
        return None
    blockers = zero_v1_blockers(ledger)
    if not blockers:
        return None
    gaps = list(support.missing) or ["nothing in the module's source text, but:"]
    removed = "\n".join(f"  - {gap}" for gap in gaps + [behaviour.reason])
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
        "Every cloud must attest, not just the one you are looking at. Each is a\n"
        "standalone deployment with its own database, AND an AWS or Azure enclave falls\n"
        "over to the home control plane when its own cannot be dialled — so a v1 envelope\n"
        "left in any one database can be handed to any cloud's enclave, during an outage.\n"
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

    The gate above is quiet today because a v1 envelope still opens. If the
    probe had silently stopped being able to ask — an import that no longer
    resolves, a helper it needs renamed — the gate would be quiet for the
    opposite reason and would never fire again. This asserts the reason.
    """
    behaviour = v1_envelope_opens(load_module(BYOK_CRYPTO.read_text()))
    assert behaviour.opens is True, behaviour.reason

    support = probe_v1_support(BYOK_CRYPTO.read_text())
    assert support.algorithm_constant == V1_ALGORITHM
    assert support.has_aad_builder
    assert support.dispatch_selects_v1
    assert support.present


def test_the_pinned_v1_format_is_the_one_the_module_seals() -> None:
    """The pin above must be the real thing while the real thing exists.

    `_v1_aad` and `V1_ALGORITHM` are copies of a wire format, kept so the probe
    outlives step 4's deletions. A copy can drift, and a drifted copy makes the
    behavioural probe report a refusal that never happened — a false CI failure
    on the scariest message in the repository. Held equal here, and this test
    is the one that becomes unrunnable (and should be deleted) at step 4.
    """
    from trusted_router import byok_crypto

    assert byok_crypto.ALGORITHM == V1_ALGORITHM
    assert byok_crypto._aad("ws", "ctx") == _v1_aad("ws", "ctx")


# ------------------------------------------- the edits it must not permit ---


@pytest.fixture(name="v1_source")
def _v1_source() -> str:
    """The module as committed, while it still has v1 support to remove.

    The tests below work by performing step 4's edits on the real source. Once
    step 4 has actually been performed there is nothing left to mutate, and the
    tests above are the ones that speak. Skipping here keeps that moment
    legible: a failing gate plus a handful of "already removed" skips, rather
    than mutation helpers failing about text they cannot find.
    """
    source = BYOK_CRYPTO.read_text()
    if v1_envelope_opens(load_module(source)).removed:
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


def _rejected_at_the_entry_points(source: str) -> str:
    """The staged removal the migration doc's own step-4 bullet invites.

    "Keep a v1-shaped envelope in a test fixture asserting it is now rejected"
    describes exactly this edit: refuse v1 where callers arrive, leave
    `ALGORITHM`, `_aad` and the dispatch arm physically in place for now. Every
    stored v1 row is permanently unopenable the moment it ships, and the
    source-reading version of this gate had nothing to say about it.
    """
    guard = (
        "    if envelope.algorithm != ALGORITHM_V2:\n"
        '        raise ValueError("v1 BYOK envelopes are no longer supported")\n'
    )
    mutated = source.replace(
        "    aad = _envelope_aad(envelope.algorithm, NAMESPACE_PROVIDER, workspace_id, provider)",
        guard
        + "    aad = _envelope_aad(envelope.algorithm, NAMESPACE_PROVIDER, workspace_id, provider)",
    )
    return mutated.replace(
        "    namespace = NAMESPACE_CONTROL if envelope.algorithm == ALGORITHM_V2 "
        "else NAMESPACE_PROVIDER",
        guard + "    namespace = NAMESPACE_CONTROL",
    )


def _rejected_only_for_control_secrets(source: str) -> str:
    """Half of the above: BYOK keeps working, broadcast secrets stop opening.

    A partial removal is the likelier accident, and it is just as unrecoverable
    for the rows it touches.
    """
    return source.replace(
        "    namespace = NAMESPACE_CONTROL if envelope.algorithm == ALGORITHM_V2 "
        "else NAMESPACE_PROVIDER",
        "    if envelope.algorithm != ALGORITHM_V2:\n"
        '        raise ValueError("v1 control-plane envelopes are no longer supported")\n'
        "    namespace = NAMESPACE_CONTROL",
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
        (_rejected_at_the_entry_points, "decrypt_byok_secret refused"),
        (_rejected_only_for_control_secrets, "decrypt_control_secret refused"),
    ],
)
def test_the_gate_fires_on_each_half_of_the_v1_deletion(
    v1_source: str, mutate: Any, expected_gap: str
) -> None:
    """Every way v1 stops opening, one at a time, on the real module.

    Partial removals matter as much as the whole edit: deleting the dispatch
    branch alone already makes every stored v1 envelope unreadable, and it is
    the smallest diff that does so. The last two entries are the edits that
    leave the source text looking untouched — the gate's previous shape passed
    them both.
    """
    mutated = mutate(v1_source)
    assert mutated != v1_source, "the mutation did not apply"

    behaviour = v1_envelope_opens(load_module(mutated))
    verdict = gate(mutated, load_ledger())

    assert behaviour.removed, behaviour.reason
    reported = list(probe_v1_support(mutated).missing) + [behaviour.reason]
    assert any(expected_gap in line for line in reported), reported
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
    ledger = _full_ledger()
    assert zero_v1_blockers(ledger) == []

    assert gate(_without_v1_dispatch(v1_source), ledger) is None
    assert gate(_rejected_at_the_entry_points(v1_source), ledger) is None


def _full_ledger() -> dict[str, Any]:
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
            "missing_envelopes": 0,
            "census_migrated_kind_counts": {"byok": 12},
            "census_sampled_kinds": ["byok", "workspace"],
            "census_v1_literal_rows": 0,
            "census_source": (
                f"spanner:projects/tr-{cloud}/instances/i/databases/d"
                " (from CLI arguments, not asked of the server)"
            ),
            "operator": "release-engineer@lorehex.co",
            "note": "step 4 precondition",
        }
    return ledger


def test_one_cloud_short_of_a_full_ledger_still_refuses(v1_source: str) -> None:
    """There is no per-cloud version of this permission.

    An AWS enclave that can no longer read v1 is broken by a v1 envelope in the
    GCP database, because it falls over to the home control plane whenever its
    own is undialable (byok_v1_attestations, "WHY EVERY CLOUD"). So dropping
    every cloud but one from the ledger must still refuse the edit, whichever
    cloud is the one left owing.
    """
    for absent in STANDALONE_CLOUDS:
        ledger = _full_ledger()
        del ledger["attestations"][absent]

        verdict = gate(_without_v1_dispatch(v1_source), ledger)

        assert verdict is not None, f"the gate cleared with {absent} unattested"
        assert absent in verdict


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "an ordinary comment",
            lambda source: source.replace(
                "def _b64(value: bytes) -> str:",
                "def _b64(value: bytes) -> str:\n    # an ordinary comment\n",
            ),
        ),
        (
            "a Final[str] annotation on ALGORITHM",
            lambda source: source.replace(
                "import secrets\n", "import secrets\nfrom typing import Final\n", 1
            ).replace(f'ALGORITHM = "{V1_ALGORITHM}"', f'ALGORITHM: Final[str] = "{V1_ALGORITHM}"'),
        ),
        (
            "the v1 arm extracted into a helper",
            lambda source: source.replace(
                "    if algorithm == ALGORITHM:\n        return _aad(workspace_id, context)",
                "    if algorithm == ALGORITHM:\n        return _legacy_aad(workspace_id, context)",
            ).replace(
                "def _aad_v2(",
                "def _legacy_aad(workspace_id: str, context: str) -> bytes:\n"
                "    return _aad(workspace_id, context)\n\n\ndef _aad_v2(",
            ),
        ),
    ],
)
def test_an_unrelated_edit_to_byok_crypto_keeps_the_gate_quiet(
    v1_source: str, name: str, mutate: Any
) -> None:
    """The annoyance check, which is a safety check.

    The gate's failure message is a multi-line warning about customer keys
    nothing will ever decrypt again. Printing it for a type annotation or a
    helper extraction — both of which leave every stored v1 envelope opening
    exactly as before — teaches the reader to ignore it, and the next reader
    deletes it. The source-reading gate fired on the last two of these.
    """
    source = mutate(v1_source)
    assert source != v1_source, f"the {name} mutation did not apply"
    assert v1_envelope_opens(load_module(source)).opens is True

    assert gate(source, load_ledger()) is None, name


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
    v1_supported = not v1_envelope_opens(load_module(BYOK_CRYPTO.read_text())).removed
    record_kept = _defines(NAMESPACE_PROPERTY_TEST, V1_COLLISION_RECORD)

    assert v1_supported == record_kept, (
        f"{V1_COLLISION_RECORD} is "
        f"{'present' if record_kept else 'absent'} in {NAMESPACE_PROPERTY_TEST.name} while v1 "
        f"envelope support is {'present' if v1_supported else 'absent'} in byok_crypto.py. They "
        "are removed together, in step 4, after every cloud attests zero v1 envelopes — never "
        "before. If the test was renamed rather than deleted, update V1_COLLISION_RECORD here."
    )
