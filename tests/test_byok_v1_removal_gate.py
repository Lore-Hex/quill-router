"""Permanent Step 4 guards after BYOK AAD v1 was retired.

The production migration is intentionally one-way: a V1 row cannot be opened
by a Step 4 build after the old AAD encoder is removed. These tests keep the
post-migration contract explicit:

* every standalone cloud attested zero V1 before removal;
* the production module contains no V1 encoder or dispatch path; and
* a byte-compatible V1 fixture is rejected before any unwrap succeeds.

The fixture carries the retired wire format only in test code. Production must
never regain a V1 constant, encoder, or fallback branch.
"""

from __future__ import annotations

import ast
import secrets
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from trusted_router import byok_crypto
from trusted_router.byok_v1_attestations import (
    STANDALONE_CLOUDS,
    load_ledger,
    zero_v1_blockers,
)
from trusted_router.key_management import KeyWrapperConfig
from trusted_router.storage_models import EncryptedSecretEnvelope

REPO = Path(__file__).resolve().parents[1]
PRODUCTION_SRC = REPO / "src" / "trusted_router"
BYOK_CRYPTO = REPO / "src" / "trusted_router" / "byok_crypto.py"
NAMESPACE_PROPERTY_TEST = REPO / "tests" / "test_byok_aad_namespace_property.py"
V1_ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"
PROBE_SETTINGS = KeyWrapperConfig(environment="test")


def _v1_aad(workspace_id: str, context: str) -> bytes:
    return f"trustedrouter:byok:{workspace_id}:{context}".encode()


def _v1_envelope(*, workspace_id: str, context: str) -> EncryptedSecretEnvelope:
    """Seal an opaque fixture exactly as the retired implementation did."""
    plaintext = b"retired-v1-fixture"  # noqa: S105 - synthetic crypto fixture
    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    dek_nonce = secrets.token_bytes(12)
    aad = _v1_aad(workspace_id, context)
    return EncryptedSecretEnvelope(
        algorithm=V1_ALGORITHM,
        key_ref=byok_crypto._key_ref(PROBE_SETTINGS),
        encrypted_dek=byok_crypto._b64(
            byok_crypto._wrap_dek(dek, dek_nonce, aad, PROBE_SETTINGS)
        ),
        dek_nonce=byok_crypto._b64(dek_nonce),
        ciphertext=byok_crypto._b64(AESGCM(dek).encrypt(nonce, plaintext, aad)),
        nonce=byok_crypto._b64(nonce),
    )


@pytest.mark.parametrize("family", ("provider", "control", "user_model"))
def test_retired_v1_envelopes_are_rejected_before_unwrap(family: str) -> None:
    workspace_id = "workspace-step4-fixture"
    context = {
        "provider": "openai",
        "control": "broadcast:fixture:api_key",
        "user_model": "user_model_signing",
    }[family]
    envelope = _v1_envelope(workspace_id=workspace_id, context=context)

    expected = (
        "user-model secrets are always v2 envelopes"
        if family == "user_model"
        else "unsupported BYOK envelope algorithm"
    )
    with pytest.raises(ValueError, match=expected):
        if family == "provider":
            byok_crypto.decrypt_byok_secret(
                envelope,
                PROBE_SETTINGS,
                workspace_id=workspace_id,
                provider=context,
            )
        elif family == "control":
            byok_crypto.decrypt_control_secret(
                envelope,
                PROBE_SETTINGS,
                workspace_id=workspace_id,
                purpose=context,
            )
        else:
            byok_crypto.decrypt_user_model_secret(
                envelope,
                PROBE_SETTINGS,
                workspace_id=workspace_id,
                purpose=context,
            )


def test_retired_v1_envelopes_cannot_be_forwarded_to_the_enclave() -> None:
    envelope = _v1_envelope(
        workspace_id="workspace-step4-fixture",
        context="openai",
    )

    with pytest.raises(ValueError, match="unsupported encrypted secret envelope algorithm"):
        byok_crypto.encrypted_secret_payload(envelope)


def test_production_crypto_contains_only_the_v2_format() -> None:
    tree = ast.parse(BYOK_CRYPTO.read_text())
    assignments: dict[str, Any] = {}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif isinstance(node, ast.FunctionDef):
            functions.add(node.name)

    assert "ALGORITHM" not in assignments
    assert "_aad" not in functions
    assert V1_ALGORITHM not in BYOK_CRYPTO.read_text()
    assert byok_crypto.ALGORITHM_V2 == "TR-BYOK-ENVELOPE-AES-256-GCM-V2"


def test_every_production_secret_decryptor_is_covered_by_the_v1_rejection_fixture() -> None:
    actual: set[tuple[str, Path]] = set()
    for path in PRODUCTION_SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("decrypt_")
                and node.name.endswith("_secret")
            ):
                actual.add((node.name, path))
    expected = {
        ("decrypt_byok_secret", BYOK_CRYPTO),
        ("decrypt_control_secret", BYOK_CRYPTO),
        ("decrypt_user_model_secret", BYOK_CRYPTO),
    }
    assert actual == expected, actual


def test_retired_v1_identifier_is_confined_to_the_read_only_census() -> None:
    allowed_symbol_files = {
        PRODUCTION_SRC / "byok_v1_attestations.py",
        PRODUCTION_SRC / "byok_aad_backfill.py",
    }
    for path in PRODUCTION_SRC.rglob("*.py"):
        body = path.read_text()
        if V1_ALGORITHM in body:
            assert path == PRODUCTION_SRC / "byok_v1_attestations.py", path
        if "V1_ALGORITHM_LITERAL" in body:
            assert path in allowed_symbol_files, path


def test_committed_fleet_ledger_authorizes_step4() -> None:
    assert zero_v1_blockers(load_ledger()) == []


def test_every_cloud_remains_required_by_the_fleet_gate() -> None:
    for absent in STANDALONE_CLOUDS:
        ledger = load_ledger()
        del ledger["attestations"][absent]
        blockers = zero_v1_blockers(ledger)
        assert any(absent in blocker for blocker in blockers)


def test_a_new_encrypted_surface_invalidates_old_attestations() -> None:
    ledger = load_ledger()
    ledger["attestations"]["gcp"]["surface_fingerprint"] = "stale-fingerprint"
    assert any("surface fingerprint" in blocker for blocker in zero_v1_blockers(ledger))


def test_the_v1_collision_preservation_test_was_removed_with_v1() -> None:
    names = {
        node.name
        for node in ast.walk(ast.parse(NAMESPACE_PROPERTY_TEST.read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    assert "test_aad_encoding_is_not_injective_in_general" not in names
