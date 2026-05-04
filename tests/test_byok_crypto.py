from __future__ import annotations

from dataclasses import dataclass

from trusted_router.byok_crypto import decrypt_byok_secret, encrypt_byok_secret
from trusted_router.config import Settings


@dataclass
class _KmsResponse:
    ciphertext: bytes = b""
    plaintext: bytes = b""


class _FakeKmsClient:
    calls: list[dict] = []

    def encrypt(self, *, request: dict) -> _KmsResponse:
        _FakeKmsClient.calls.append({"op": "encrypt", **request})
        aad = bytes(request["additional_authenticated_data"])
        plaintext = bytes(request["plaintext"])
        return _KmsResponse(ciphertext=b"kmswrap:" + plaintext[::-1] + aad)

    def decrypt(self, *, request: dict) -> _KmsResponse:
        _FakeKmsClient.calls.append({"op": "decrypt", **request})
        aad = bytes(request["additional_authenticated_data"])
        ciphertext = bytes(request["ciphertext"])
        assert ciphertext.startswith(b"kmswrap:")
        assert ciphertext.endswith(aad)
        wrapped = ciphertext[len(b"kmswrap:") : -len(aad)]
        return _KmsResponse(plaintext=wrapped[::-1])


def test_byok_envelope_uses_kms_wrap_without_plaintext_in_envelope(monkeypatch) -> None:
    _FakeKmsClient.calls.clear()
    monkeypatch.setattr(
        "trusted_router.byok_crypto.kms_v1.KeyManagementServiceClient",
        _FakeKmsClient,
    )
    key_name = "projects/test/locations/us-central1/keyRings/tr/cryptoKeys/byok"
    settings = Settings(environment="test", byok_kms_key_name=key_name)
    raw = "sk-user-owned-provider-key-1234"

    envelope = encrypt_byok_secret(
        raw,
        settings,
        workspace_id="ws_1",
        provider="openai",
    )

    assert envelope.key_ref == key_name
    assert raw not in str(envelope)
    assert _FakeKmsClient.calls[0]["op"] == "encrypt"
    assert _FakeKmsClient.calls[0]["name"] == key_name
    assert _FakeKmsClient.calls[0]["additional_authenticated_data"] == b"trustedrouter:byok:ws_1:openai"
    assert decrypt_byok_secret(
        envelope,
        settings,
        workspace_id="ws_1",
        provider="openai",
    ) == raw
    assert _FakeKmsClient.calls[1]["op"] == "decrypt"
