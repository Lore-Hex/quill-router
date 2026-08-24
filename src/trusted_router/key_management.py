"""Envelope-key wrapping, decoupled from any one cloud KMS.

BYOK secrets are envelope-encrypted: a random per-secret DEK encrypts the
customer's provider key, and that DEK is then *wrapped*. Only the wrapping
step is cloud-specific, so it is the only thing behind this port.

Before this module, `byok_crypto` called `google.cloud.kms_v1` inline and
`routes/byok.py` caught `google.api_core.exceptions` to decide HTTP status —
which put a GCP SDK dependency in the request path of an endpoint that has
nothing to do with GCP. Adding AWS KMS or Azure Key Vault means adding a class
here; no caller changes.

Errors are translated at this boundary into `KeyAccessDenied` /
`KeyUnavailable` so route code can map them to HTTP status without knowing
which cloud produced them.

Compatibility note (deliberate): `unwrap` dispatches on the CURRENT settings,
matching the behaviour this replaced. That is wrong in the long run — the
envelope records the `key_ref` it was wrapped with, so a cloud migration or
key rotation should dispatch on `envelope.key_ref` or every existing envelope
becomes undecryptable. Fixing it changes production crypto behaviour and needs
its own migration (re-wrap all DEKs), so it is called out in
docs/storage-portability/README.md rather than smuggled in here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyManagementError(Exception):
    """Base for envelope-key wrapping failures."""


class KeyAccessDenied(KeyManagementError):
    """This principal may not use the wrapping key.

    Distinct because it is not retryable and not transient: it means the
    deployment is misconfigured (or the endpoint genuinely lacks permission),
    and the caller should say so rather than invite a retry.
    """


class KeyUnavailable(KeyManagementError):
    """The key service could not be reached or failed transiently."""


@runtime_checkable
class KeyWrapper(Protocol):
    """Wraps and unwraps a data encryption key."""

    @property
    def key_ref(self) -> str:
        """Stable identifier recorded in the envelope for auditability."""

    def wrap(self, dek: bytes, *, nonce: bytes, aad: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, *, nonce: bytes, aad: bytes) -> bytes: ...


class KeyWrapperSettings(Protocol):
    """The small configuration surface envelope wrapping actually needs."""

    environment: str
    byok_kms_key_name: str | None
    byok_envelope_key_b64: str | None
    byok_envelope_key_ref: str


@dataclass(frozen=True)
class KeyWrapperConfig:
    """Standalone key configuration for offline operator tools.

    Operator migrations must not instantiate the full web application settings
    object: doing so couples a KMS-only job to OAuth, email, gateway, and every
    other production startup validator. The explicit non-local environment also
    keeps the development wrapping-key fallback unavailable.
    """

    environment: str = "production"
    byok_kms_key_name: str | None = None
    byok_envelope_key_b64: str | None = None
    byok_envelope_key_ref: str = "trustedrouter/byok-envelope-key/v1"


class LocalAesKeyWrapper:
    """AES-GCM wrapping with a locally-held 32-byte key.

    Used for local/test, and for deployments that supply their own key rather
    than delegating to a cloud KMS.
    """

    def __init__(self, key: bytes, key_ref: str) -> None:
        if len(key) != 32:
            raise ValueError("envelope wrapping key must be 32 bytes")
        self._key = key
        self._key_ref = key_ref

    @property
    def key_ref(self) -> str:
        return self._key_ref

    def wrap(self, dek: bytes, *, nonce: bytes, aad: bytes) -> bytes:
        return AESGCM(self._key).encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, *, nonce: bytes, aad: bytes) -> bytes:
        return AESGCM(self._key).decrypt(nonce, wrapped, aad)


class GcpKmsKeyWrapper:
    """Wrapping delegated to Google Cloud KMS.

    The client and the Google exception types are imported lazily so that a
    non-GCP deployment does not need the Google libraries installed at all.
    """

    def __init__(self, key_name: str) -> None:
        self._key_name = key_name

    @property
    def key_ref(self) -> str:
        return self._key_name

    def _client(self):  # type: ignore[no-untyped-def]
        from google.cloud import kms_v1

        return kms_v1.KeyManagementServiceClient()

    def wrap(self, dek: bytes, *, nonce: bytes, aad: bytes) -> bytes:
        try:
            response = self._client().encrypt(
                request={
                    "name": self._key_name,
                    "plaintext": dek,
                    "additional_authenticated_data": aad,
                }
            )
        except Exception as exc:
            raise _translate_kms_error(exc) from exc
        return bytes(response.ciphertext)

    def unwrap(self, wrapped: bytes, *, nonce: bytes, aad: bytes) -> bytes:
        try:
            response = self._client().decrypt(
                request={
                    "name": self._key_name,
                    "ciphertext": wrapped,
                    "additional_authenticated_data": aad,
                }
            )
        except Exception as exc:
            raise _translate_kms_error(exc) from exc
        return bytes(response.plaintext)


def _translate_kms_error(exc: Exception) -> Exception:
    """Map a Google KMS exception onto the neutral taxonomy.

    Returns the original exception unchanged when it is not a recognised KMS
    failure, so genuine bugs keep their own type and traceback instead of being
    laundered into a misleading "key unavailable".

    The Google exception types are imported here rather than at module scope so
    that importing this module never pulls in a cloud SDK.
    """
    try:
        from google.api_core import exceptions as gcp_exceptions
    except ImportError:  # pragma: no cover - only without GCP deps
        return exc
    if isinstance(exc, gcp_exceptions.PermissionDenied):
        return KeyAccessDenied(str(exc))
    if isinstance(exc, gcp_exceptions.GoogleAPICallError):
        return KeyUnavailable(str(exc))
    return exc


def key_wrapper_for(settings: KeyWrapperSettings) -> KeyWrapper:
    """Select the wrapper for this deployment.

    Precedence matches the behaviour this replaced: an explicit local key wins
    over KMS, and outside local/test one of the two must be configured — a
    silent dev-key fallback in production would be a security hole.
    """
    if settings.byok_envelope_key_b64:
        from trusted_router.byok_crypto import _unb64

        return LocalAesKeyWrapper(
            _unb64(settings.byok_envelope_key_b64), settings.byok_envelope_key_ref
        )
    if settings.byok_kms_key_name:
        return GcpKmsKeyWrapper(settings.byok_kms_key_name)
    if settings.environment.lower() in {"local", "test"}:
        from trusted_router.byok_crypto import _DEV_WRAPPING_KEY

        return LocalAesKeyWrapper(_DEV_WRAPPING_KEY, settings.byok_envelope_key_ref)
    raise ValueError("TR_BYOK_ENVELOPE_KEY_B64 is required outside local/test")
