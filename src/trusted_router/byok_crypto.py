from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.cloud import kms_v1

from trusted_router.config import Settings
from trusted_router.storage_models import EncryptedSecretEnvelope

ALGORITHM = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"
_DEV_WRAPPING_KEY = b"trustedrouter-dev-byok-envelope!"  # 32 bytes.


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
    aad = _aad(workspace_id, provider)

    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    encrypted_dek = _wrap_dek(dek, dek_nonce, aad, settings)
    return EncryptedSecretEnvelope(
        algorithm=ALGORITHM,
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
    if envelope.algorithm != ALGORITHM:
        raise ValueError("unsupported BYOK envelope algorithm")
    aad = _aad(workspace_id, provider)
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
    if settings.byok_kms_key_name:
        response = kms_v1.KeyManagementServiceClient().encrypt(
            request={
                "name": settings.byok_kms_key_name,
                "plaintext": dek,
                "additional_authenticated_data": aad,
            }
        )
        return bytes(response.ciphertext)
    return AESGCM(_wrapping_key(settings)).encrypt(dek_nonce, dek, aad)


def _unwrap_dek(
    envelope: EncryptedSecretEnvelope,
    aad: bytes,
    settings: Settings,
) -> bytes:
    encrypted_dek = _unb64(envelope.encrypted_dek)
    if settings.byok_kms_key_name:
        response = kms_v1.KeyManagementServiceClient().decrypt(
            request={
                "name": settings.byok_kms_key_name,
                "ciphertext": encrypted_dek,
                "additional_authenticated_data": aad,
            }
        )
        return bytes(response.plaintext)
    return AESGCM(_wrapping_key(settings)).decrypt(_unb64(envelope.dek_nonce), encrypted_dek, aad)


def _key_ref(settings: Settings) -> str:
    return settings.byok_kms_key_name or settings.byok_envelope_key_ref


def _aad(workspace_id: str, provider: str) -> bytes:
    return f"trustedrouter:byok:{workspace_id}:{provider}".encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
