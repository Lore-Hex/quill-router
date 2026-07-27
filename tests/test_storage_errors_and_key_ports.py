"""The two cloud ports: storage error classification and envelope-key wrapping.

Both replaced direct cloud SDK use in application code, so the thing worth
testing is that behaviour is UNCHANGED — a refactor that quietly narrowed the
retryable set would make the settle-outbox drain burn attempts on a transient
Spanner outage instead of parking, which is real money.
"""

from __future__ import annotations

import pytest

from trusted_router.key_management import (
    GcpKmsKeyWrapper,
    KeyAccessDenied,
    KeyManagementError,
    KeyUnavailable,
    LocalAesKeyWrapper,
    key_wrapper_for,
)
from trusted_router.storage_errors import (
    StoreConflict,
    StoreUnavailable,
    conflict_store_error_types,
    is_conflict_error,
    is_transient_store_error,
    transient_store_error_types,
)

# --------------------------------------------------------------------------
# Storage error taxonomy
# --------------------------------------------------------------------------

#: The exact set settle_outbox_apply parked on before the refactor.
ORIGINAL_GOOGLE_TRANSIENT = {
    "Aborted",
    "DeadlineExceeded",
    "InternalServerError",
    "ResourceExhausted",
    "RetryError",
    "ServiceUnavailable",
}


def test_transient_set_still_contains_every_original_google_type() -> None:
    """Regression guard on the outbox park/error policy.

    Dropping one of these would turn a transient backend outage into burned
    delivery attempts on rows carrying real settlements.
    """
    names = {t.__name__ for t in transient_store_error_types()}
    missing = ORIGINAL_GOOGLE_TRANSIENT - names
    assert not missing, f"transient set lost {missing}"


def test_aborted_is_the_only_google_conflict_type() -> None:
    """`Aborted` uniquely means "a concurrent writer won", not "backend unwell".

    main.py answers 503 + Retry-After on exactly that condition; widening it
    would start telling clients to retry unretryable failures.
    """
    names = {t.__name__ for t in conflict_store_error_types()}
    assert "Aborted" in names
    assert "DeadlineExceeded" not in names


def test_neutral_types_are_classified() -> None:
    assert is_transient_store_error(StoreConflict("lost a race"))
    assert is_transient_store_error(StoreUnavailable("backend down"))
    assert is_conflict_error(StoreConflict("lost a race"))
    # Unavailability is retryable but is NOT a write conflict.
    assert not is_conflict_error(StoreUnavailable("backend down"))


def test_unknown_errors_are_not_swallowed() -> None:
    """An unrecognised exception is a bug, not an infra blip.

    If this returned True the drain would retry a deterministic failure
    forever instead of surfacing it.
    """
    assert not is_transient_store_error(ValueError("bug"))
    assert not is_conflict_error(ValueError("bug"))


def test_google_types_resolve_when_the_sdk_is_installed() -> None:
    """The lazy import must actually find the SDK when present.

    Without this, the graceful ImportError fallback could silently mean "no
    Google types" in production and nothing would ever be classified transient.
    """
    names = {t.__name__ for t in transient_store_error_types()}
    assert ORIGINAL_GOOGLE_TRANSIENT <= names


# --------------------------------------------------------------------------
# Key wrapping port
# --------------------------------------------------------------------------


def test_local_wrapper_round_trips() -> None:
    wrapper = LocalAesKeyWrapper(b"\x01" * 32, "local:test")
    dek, nonce, aad = b"\x02" * 32, b"\x03" * 12, b"ws:provider"
    wrapped = wrapper.wrap(dek, nonce=nonce, aad=aad)
    assert wrapped != dek
    assert wrapper.unwrap(wrapped, nonce=nonce, aad=aad) == dek


def test_local_wrapper_binds_the_aad() -> None:
    """AAD binds the envelope to its workspace+provider.

    Without this check a ciphertext could be lifted from one workspace's row
    into another's and still decrypt.
    """
    from cryptography.exceptions import InvalidTag

    wrapper = LocalAesKeyWrapper(b"\x01" * 32, "local:test")
    dek, nonce = b"\x02" * 32, b"\x03" * 12
    wrapped = wrapper.wrap(dek, nonce=nonce, aad=b"ws-a:openai")
    # InvalidTag specifically: authentication must fail, not merely "some error".
    with pytest.raises(InvalidTag):
        wrapper.unwrap(wrapped, nonce=nonce, aad=b"ws-b:openai")


def test_local_wrapper_rejects_wrong_key_length() -> None:
    with pytest.raises(ValueError):
        LocalAesKeyWrapper(b"short", "local:test")


def test_kms_errors_are_translated_to_the_neutral_taxonomy() -> None:
    """Route code must not need Google's exception classes to pick a status.

    routes/byok.py distinguishes "this principal may not use the key" (a
    configuration problem, surfaced as an actionable 503) from any other key
    service failure, and that distinction has to survive the port.
    """
    from google.api_core import exceptions as gcp_exceptions

    wrapper = GcpKmsKeyWrapper("projects/p/locations/l/keyRings/r/cryptoKeys/k")

    def boom(exc: Exception):
        def _client():
            raise exc

        return _client

    wrapper._client = boom(gcp_exceptions.PermissionDenied("nope"))  # type: ignore[method-assign]
    with pytest.raises(KeyAccessDenied):
        wrapper.wrap(b"x" * 32, nonce=b"n" * 12, aad=b"aad")

    wrapper._client = boom(gcp_exceptions.ServiceUnavailable("later"))  # type: ignore[method-assign]
    with pytest.raises(KeyUnavailable):
        wrapper.wrap(b"x" * 32, nonce=b"n" * 12, aad=b"aad")

    # Both are catchable as the common base, which is what byok routes use.
    assert issubclass(KeyAccessDenied, KeyManagementError)
    assert issubclass(KeyUnavailable, KeyManagementError)


class _KeySettings:
    """Minimal stand-in for the three attributes `key_wrapper_for` reads.

    The real `Settings` is fail-closed for `environment="production"` — it
    refuses to construct without the full production secret set — which is
    correct behaviour but makes it unusable for isolating this one decision.
    """

    def __init__(
        self, *, environment: str, envelope_key_b64: str = "", kms_key_name: str = ""
    ) -> None:
        self.environment = environment
        self.byok_envelope_key_b64 = envelope_key_b64
        self.byok_kms_key_name = kms_key_name
        self.byok_envelope_key_ref = "test-ref"


def test_production_refuses_the_dev_wrapping_key() -> None:
    """The dev key is derivable from public inputs in this repo.

    Falling back to it outside local/test would wrap every customer's BYOK
    secret with a key anyone reading the source can reconstruct.
    """
    with pytest.raises(ValueError, match="required outside local/test"):
        key_wrapper_for(_KeySettings(environment="production"))


def test_local_environment_gets_the_dev_wrapper() -> None:
    wrapper = key_wrapper_for(_KeySettings(environment="test"))
    assert isinstance(wrapper, LocalAesKeyWrapper)


def test_explicit_local_key_wins_over_kms() -> None:
    """Precedence must match the behaviour this replaced.

    An operator who sets an explicit envelope key expects it to be used even
    if a KMS key name is also present in the environment.
    """
    import base64

    key_b64 = base64.urlsafe_b64encode(b"\x07" * 32).decode()
    wrapper = key_wrapper_for(
        _KeySettings(
            environment="production",
            envelope_key_b64=key_b64,
            kms_key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k",
        )
    )
    assert isinstance(wrapper, LocalAesKeyWrapper)


def test_kms_is_used_when_no_explicit_local_key() -> None:
    wrapper = key_wrapper_for(
        _KeySettings(
            environment="production",
            kms_key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k",
        )
    )
    assert isinstance(wrapper, GcpKmsKeyWrapper)
    assert wrapper.key_ref.endswith("cryptoKeys/k")
