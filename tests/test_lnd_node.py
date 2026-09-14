from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.lightning import lnd_node as node


@pytest.fixture(scope="module")
def recovery_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def public(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def unseal(blob: bytes, key: rsa.RSAPrivateKey) -> bytes:
    parsed = json.loads(blob)
    assert parsed["version"] == 1
    dek = key.decrypt(base64.b64decode(parsed["key"]), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=node.AAD,
    ))
    return AESGCM(dek).decrypt(base64.b64decode(parsed["nonce"]), base64.b64decode(parsed["ciphertext"]), node.AAD)


def test_recovery_encryption_roundtrips_offline_and_randomizes(recovery_key: rsa.RSAPrivateKey) -> None:
    original = b"private seed words and wallet password"
    first, second = [node.seal(original, public(recovery_key)) for _ in range(2)]
    assert first != second
    assert original not in first
    assert unseal(first, recovery_key) == original


def test_tampered_backup_fails_authentication(recovery_key: rsa.RSAPrivateKey) -> None:
    parsed = json.loads(node.seal(b"private", public(recovery_key)))
    ciphertext = bytearray(base64.b64decode(parsed["ciphertext"]))
    ciphertext[0] ^= 1
    parsed["ciphertext"] = base64.b64encode(ciphertext).decode()
    with pytest.raises(InvalidTag):
        unseal(json.dumps(parsed).encode(), recovery_key)


def test_recovery_rejects_small_public_key() -> None:
    with pytest.raises(ValueError, match="3072"):
        node.seal(b"secret", public(rsa.generate_private_key(public_exponent=65537, key_size=2048)))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "::1", "169.254.169.254", "1.2.3.4\nno-macaroons=true"])
def test_config_rejects_invalid_peer_address(ip: str) -> None:
    with pytest.raises(ValueError):
        node.config(ip, "safe-password", initialized=True)


@pytest.mark.parametrize("password", ["", "secret\nno-macaroons=true", "secret\x00"])
def test_rpc_credential_cannot_inject_config(password: str) -> None:
    with pytest.raises(ValueError):
        node.config("8.8.8.8", password, initialized=True)


def test_only_peer_listener_is_public_and_merchant_cannot_route() -> None:
    config = node.config("8.8.8.8", "credential", initialized=True)
    assert "rpclisten=127.0.0.1:10009" in config
    assert "restlisten=127.0.0.1:8080" in config
    assert "listen=0.0.0.0:9735" in config
    assert "rejectpush=true" in config
    assert "rejecthtlc=true" in config
    assert "wallet-unlock-password-file=/run/credentials/" in config
    assert "wallet-unlock-allow-create" not in config
    assert "no-macaroons" not in config
    assert "noseedbackup" not in config
    assert "wallet-unlock" not in node.config("8.8.8.8", "credential", initialized=False)


def test_atomic_secret_file_permissions_and_no_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "nested/secret"
    node.write_private(path, b"first", exclusive=True)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        node.write_private(path, b"second", exclusive=True)
    assert path.read_bytes() == b"first"
    node.write_private(path, b"replacement")
    assert path.read_bytes() == b"replacement"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.fixture
def wallet_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_key: rsa.RSAPrivateKey) -> None:
    for name, relative in {"DATA": "data", "ETC": "etc", "NETWORK": "network", "SEED": "data/recovery/seed",
                           "PUBLIC_KEY": "public.pem"}.items():
        monkeypatch.setattr(node, name, tmp_path / relative)
    node.ETC.mkdir()
    node.NETWORK.mkdir()
    node.PUBLIC_KEY.write_bytes(public(recovery_key))
    (node.ETC / "wallet.passwd").write_bytes(b"test-wallet-password")
    monkeypatch.setattr(node, "run", lambda *args: json.dumps({"chain": "main", "initialblockdownload": False,
                                                             "blocks": 967000, "headers": 967000}))


def test_seed_is_durable_and_uploaded_before_initializing(
    wallet_paths: None, monkeypatch: pytest.MonkeyPatch, recovery_key: rsa.RSAPrivateKey,
) -> None:
    events: list[str] = []

    def rpc(path: str, body: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if path == "/v1/genseed":
            return {"cipher_seed_mnemonic": ["test-word"] * 24}
        assert events == ["uploaded"]
        assert node.SEED.exists()
        assert (node.DATA / "recovery/seed-upload.json").exists()
        events.append("initialized")
        return {}

    def upload(blob: bytes, kind: str) -> str:
        assert blob == node.SEED.read_bytes()
        assert json.loads(unseal(blob, recovery_key))["seed"] == ["test-word"] * 24
        events.append("uploaded")
        return "encrypted-object"

    monkeypatch.setattr(node, "rpc", rpc)
    monkeypatch.setattr(node, "upload", upload)
    monkeypatch.setattr(node, "write_config", lambda **kwargs: events.append("auto-unlock"))
    node.initialize()
    assert events == ["uploaded", "initialized", "auto-unlock"]


def test_backup_failure_never_initializes_wallet(wallet_paths: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def rpc(path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        return {"cipher_seed_mnemonic": ["test-word"] * 24}

    def failed_upload(*args: Any) -> str:
        raise RuntimeError("storage offline")

    monkeypatch.setattr(node, "rpc", rpc)
    monkeypatch.setattr(node, "upload", failed_upload)
    with pytest.raises(RuntimeError, match="offline"):
        node.initialize()
    assert calls == ["/v1/genseed"]
    # A restart must stop for operator recovery, never overwrite the first seed.
    with pytest.raises(ValueError, match="refusing"):
        node.initialize()


def test_existing_wallet_never_regenerates_seed(wallet_paths: None) -> None:
    (node.NETWORK / "wallet.db").touch()
    with pytest.raises(ValueError, match="refusing"):
        node.initialize()


def test_unsynced_bitcoin_blocks_wallet_creation(wallet_paths: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node, "run", lambda *args: json.dumps({"chain": "main", "initialblockdownload": True}))
    with pytest.raises(ValueError, match="not synced"):
        node.initialize()
    assert not node.SEED.exists()


def test_channel_backup_retries_failed_upload_and_skips_unchanged(
    wallet_paths: None, monkeypatch: pytest.MonkeyPatch, recovery_key: rsa.RSAPrivateKey,
) -> None:
    node.write_private(node.SEED, b"encrypted-seed")
    node.write_private(node.DATA / "recovery/seed-upload.json", b'{}')
    (node.NETWORK / "channel.backup").write_bytes(b"static channel backup")
    uploaded: list[bytes] = []

    def upload(blob: bytes, kind: str) -> str:
        uploaded.append(blob)
        if len(uploaded) == 1:
            raise RuntimeError("offline")
        assert unseal(blob, recovery_key) == b"static channel backup"
        return "object"

    monkeypatch.setattr(node, "upload", upload)
    with pytest.raises(RuntimeError):
        node.backup()
    assert not (node.DATA / "recovery/channel-upload.json").exists()
    node.backup()
    node.backup()
    assert len(uploaded) == 2


def test_subprocess_error_does_not_expose_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "secret seed"))
    with pytest.raises(RuntimeError) as error:
        node.run("example")
    assert "secret seed" not in str(error.value)


def test_metadata_request_is_exact_allowlist() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        node.request_json("https://example.com")
