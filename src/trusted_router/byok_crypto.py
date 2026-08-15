from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from trusted_router.config import Settings
from trusted_router.key_management import key_wrapper_for
from trusted_router.storage_models import EncryptedSecretEnvelope

# v1 associated data is colon-joined with no escaping or length prefix, so
# component boundaries are ambiguous: _aad("a:b", "c") == _aad("a", "b:c").
# Kept because envelopes written before the migration are still v1 and must
# keep decrypting. See docs/design/byok-aad-v2-migration.md.
ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"

# v2 length-prefixes every AAD component and adds a namespace, making the
# encoding injective. Written only because every enclave region can already
# read it (quill-cloud-proxy#162 shipped first) — writing v2 before that would
# break BYOK for every customer using it.
ALGORITHM_V2 = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"

# The two secret families. A purpose can no longer collide with a provider slug
# even when the strings match, because the namespace is a separate AAD
# component rather than a prefix on the same one.
NAMESPACE_PROVIDER = "provider"
NAMESPACE_CONTROL = "control"

# Local/test fallback wrapping key — derived via HKDF from a label, not
# a literal byte string. Anyone who reads this file can still
# reconstruct the same key (the HKDF inputs are public), but the bytes
# don't appear verbatim in source. The real defense is the gate in
# `_wrapping_key()` which refuses this path outside `local`/`test`
# environments — production MUST set TR_BYOK_KMS_KEY_NAME (preferred)
# or TR_BYOK_ENVELOPE_KEY_B64.
def _derive_dev_wrapping_key() -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"trustedrouter:byok:dev:salt:v1",
        info=b"trustedrouter:byok:dev:envelope-wrapping-key:v1",
    ).derive(b"trustedrouter-dev-only-do-not-use-in-production")


_DEV_WRAPPING_KEY = _derive_dev_wrapping_key()


def encrypt_byok_secret(
    raw_secret: str,
    settings: Settings,
    *,
    workspace_id: str,
    provider: str,
) -> EncryptedSecretEnvelope:
    """Envelope-encrypt a user-supplied BYOK provider key.

    The raw provider key is encrypted with a random per-secret DEK. That DEK is
    then wrapped with the configured BYOK envelope key. At large scale this
    means millions of user keys are ordinary encrypted rows, not millions of
    Secret Manager objects.
    """
    plaintext = raw_secret.strip().encode("utf-8")
    if not plaintext:
        raise ValueError("raw BYOK key is empty")

    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    dek_nonce = secrets.token_bytes(12)
    aad = _aad_v2(NAMESPACE_PROVIDER, workspace_id, provider)

    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    encrypted_dek = _wrap_dek(dek, dek_nonce, aad, settings)
    return EncryptedSecretEnvelope(
        algorithm=ALGORITHM_V2,
        key_ref=_key_ref(settings),
        encrypted_dek=_b64(encrypted_dek),
        dek_nonce=_b64(dek_nonce),
        ciphertext=_b64(ciphertext),
        nonce=_b64(nonce),
    )


def decrypt_byok_secret(
    envelope: EncryptedSecretEnvelope,
    settings: Settings,
    *,
    workspace_id: str,
    provider: str,
) -> str:
    aad = _envelope_aad(envelope.algorithm, NAMESPACE_PROVIDER, workspace_id, provider)
    dek = _unwrap_dek(envelope, aad, settings)
    plaintext = AESGCM(dek).decrypt(_unb64(envelope.nonce), _unb64(envelope.ciphertext), aad)
    return plaintext.decode("utf-8")


def encrypt_control_secret(
    raw_secret: str,
    settings: Settings,
    *,
    workspace_id: str,
    purpose: str,
) -> EncryptedSecretEnvelope:
    """Envelope-encrypt a control-plane secret (broadcast keys, headers).

    Deliberately no longer delegates through encrypt_byok_secret's `provider`
    parameter. That put a control purpose into the same AAD slot as a provider
    slug, so a BYOK key and a control secret in one workspace could share
    associated data and decrypt each other. They now differ in the namespace
    component, which no choice of purpose or provider string can collapse.
    """
    plaintext = raw_secret.strip().encode("utf-8")
    if not plaintext:
        raise ValueError("raw control secret is empty")

    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    dek_nonce = secrets.token_bytes(12)
    aad = _aad_v2(NAMESPACE_CONTROL, workspace_id, purpose)

    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    encrypted_dek = _wrap_dek(dek, dek_nonce, aad, settings)
    return EncryptedSecretEnvelope(
        algorithm=ALGORITHM_V2,
        key_ref=_key_ref(settings),
        encrypted_dek=_b64(encrypted_dek),
        dek_nonce=_b64(dek_nonce),
        ciphertext=_b64(ciphertext),
        nonce=_b64(nonce),
    )


def decrypt_control_secret(
    envelope: EncryptedSecretEnvelope,
    settings: Settings,
    *,
    workspace_id: str,
    purpose: str,
) -> str:
    """Decrypt a control-plane secret.

    A v1 envelope predates the namespace split, so it is opened with the v1 AAD
    that sealed it — which is the same shape a BYOK key used. That collision is
    exactly what the backfill (step 3) removes; until then v1 rows keep working
    as they always did.
    """
    namespace = NAMESPACE_CONTROL if envelope.algorithm == ALGORITHM_V2 else NAMESPACE_PROVIDER
    aad = _envelope_aad(envelope.algorithm, namespace, workspace_id, purpose)
    dek = _unwrap_dek(envelope, aad, settings)
    plaintext = AESGCM(dek).decrypt(_unb64(envelope.nonce), _unb64(envelope.ciphertext), aad)
    return plaintext.decode("utf-8")


def encrypted_secret_payload(envelope: EncryptedSecretEnvelope | None) -> dict[str, str] | None:
    if envelope is None:
        return None
    return {
        "algorithm": envelope.algorithm,
        "key_ref": envelope.key_ref,
        "encrypted_dek": envelope.encrypted_dek,
        "dek_nonce": envelope.dek_nonce,
        "ciphertext": envelope.ciphertext,
        "nonce": envelope.nonce,
    }


def byok_cache_key(
    envelope: EncryptedSecretEnvelope | None,
    *,
    workspace_id: str,
    provider: str,
) -> str | None:
    """Stable, non-secret cache key for one encrypted BYOK envelope version.

    Gateways use this to cache decrypted BYOK material briefly in enclave
    memory. A raw-key rotation creates a new ciphertext/DEK, therefore a new
    cache key; deleting BYOK stops returning an envelope at authorization time.
    """
    if envelope is None:
        return None
    digest = hashlib.sha256()
    for part in (
        workspace_id,
        provider,
        envelope.algorithm,
        envelope.key_ref,
        envelope.encrypted_dek,
        envelope.dek_nonce,
        envelope.ciphertext,
        envelope.nonce,
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return "byokcache:v1:" + digest.hexdigest()


def _wrapping_key(settings: Settings) -> bytes:
    if settings.byok_envelope_key_b64:
        key = _unb64(settings.byok_envelope_key_b64)
        if len(key) != 32:
            raise ValueError("TR_BYOK_ENVELOPE_KEY_B64 must decode to 32 bytes")
        return key
    if settings.environment.lower() in {"local", "test"}:
        return _DEV_WRAPPING_KEY
    raise ValueError("TR_BYOK_ENVELOPE_KEY_B64 is required outside local/test")


def _wrap_dek(dek: bytes, dek_nonce: bytes, aad: bytes, settings: Settings) -> bytes:
    return key_wrapper_for(settings).wrap(dek, nonce=dek_nonce, aad=aad)


def _unwrap_dek(
    envelope: EncryptedSecretEnvelope,
    aad: bytes,
    settings: Settings,
) -> bytes:
    encrypted_dek = _unb64(envelope.encrypted_dek)
    return key_wrapper_for(settings).unwrap(
        encrypted_dek, nonce=_unb64(envelope.dek_nonce), aad=aad
    )


def _key_ref(settings: Settings) -> str:
    return key_wrapper_for(settings).key_ref


def _aad(workspace_id: str, provider: str) -> bytes:
    """v1 associated data. Not injective — see the module header."""
    return f"trustedrouter:byok:{workspace_id}:{provider}".encode()


def _aad_v2(namespace: str, workspace_id: str, context: str) -> bytes:
    """v2 associated data: length-prefixed, so it is injective.

    Each component is a 4-byte big-endian length followed by its UTF-8 bytes.
    No choice of component values can produce the same byte string from a
    different tuple, which is the property v1 lacks.

    Must stay byte-identical to aadV2 in quill-cloud-proxy's
    enclave-go/internal/byokcache/cache.go. Both sides pin the same hex vector;
    a divergence is not a test failure but every BYOK key in that family
    failing to open on the other plane.
    """
    parts = (
        b"trustedrouter/byok/v2",
        namespace.encode("utf-8"),
        workspace_id.encode("utf-8"),
        context.encode("utf-8"),
    )
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _envelope_aad(
    algorithm: str, namespace: str, workspace_id: str, context: str
) -> bytes:
    """Select the associated data for an envelope's declared format.

    Dispatching on the stored algorithm rather than trying v2 and falling back
    to v1: a permanent fallback would keep the substitution weakness alive and
    make the migration impossible to declare finished.
    """
    if algorithm == ALGORITHM:
        return _aad(workspace_id, context)
    if algorithm == ALGORITHM_V2:
        return _aad_v2(namespace, workspace_id, context)
    raise ValueError("unsupported BYOK envelope algorithm")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
