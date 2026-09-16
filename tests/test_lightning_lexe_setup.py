"""Setup is restartable and cannot pay, open channels, or export secrets."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.lightning import lexe_setup
from scripts.lightning.lexe_preflight import PERMISSIONS, PreflightError


def fake_sdk(state):
    calls = []
    clients = {}
    token = "private-receive-token"  # noqa: S105 - deliberately fake test credential
    credential = SimpleNamespace(export_string=lambda: token)
    node = SimpleNamespace(balance_sats=0, num_channels=0, user_pk="a" * 64)
    info = SimpleNamespace(kind=SimpleNamespace(name="CLIENT_CREDENTIALS"),
                           scopes=["read_info", "read_payments", "receive"], permissions=[],
                           effective_permissions=sorted(PERMISSIONS), expires_at_ms=None, client_pk="b" * 64)

    class Seed:
        def write_to_path(self, path):
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write("fake-test-seed")

    def signup(**kwargs):
        assert (state / "seedphrase.txt").read_text() == "fake-test-seed"
        calls.append("signup")

    def create_client(**kwargs):
        assert kwargs["scopes"] == ["read_info", "read_payments", "receive"]
        assert kwargs["permissions"] == []
        assert kwargs["expires_at_ms"] > 0
        calls.append("create_client")
        clients["b" * 64] = info
        info.label = kwargs["label"]
        return SimpleNamespace(client_credentials=credential)

    root = SimpleNamespace(signup=signup, node_info=lambda: node,
                           list_clients=lambda: clients, create_client=create_client)
    receiver = SimpleNamespace(client_info=lambda: info, node_info=lambda: node)

    def load_seed(path):
        assert Path(path).read_text() == "fake-test-seed"
        return Seed()

    sdk = SimpleNamespace(
        RootSeed=SimpleNamespace(generate=Seed, read_from_path=load_seed),
        WalletConfig=SimpleNamespace(mainnet=lambda: SimpleNamespace(use_sgx=True)),
        Credentials=SimpleNamespace(from_root_seed=lambda _: "root", from_client_credentials=lambda _: "receiver"),
        ClientCredentials=SimpleNamespace(from_string=lambda value: credential if value == token else pytest.fail("bad token")),
        LexeWallet=SimpleNamespace(without_db=lambda config, credentials: root if credentials == "root" else receiver),
        Scope=SimpleNamespace(READ_INFO="read_info", READ_PAYMENTS="read_payments", RECEIVE="receive"),
    )
    return sdk, calls, node, info


def test_empty_wallet_setup_persists_before_signup_and_resumes(tmp_path):
    state = tmp_path / "private"
    sdk, calls, _, _ = fake_sdk(state)
    result = lexe_setup.setup(sdk, state)
    assert calls == ["signup", "create_client"]
    assert result["production_cutover_allowed"] is False
    assert result["recovery_backup_required"] is True
    assert "private-receive-token" not in json.dumps(result)
    assert "fake-test-seed" not in json.dumps(result)
    assert state.stat().st_mode & 0o077 == 0
    for file in state.iterdir():
        assert file.stat().st_mode & 0o077 == 0
    assert lexe_setup.setup(sdk, state) == result
    assert calls == ["signup", "create_client", "signup"]


def test_lost_credential_file_does_not_create_another_client(tmp_path):
    state = tmp_path / "private"
    sdk, calls, _, _ = fake_sdk(state)
    lexe_setup.setup(sdk, state)
    (state / "receive-client.txt").unlink()
    with pytest.raises(PreflightError, match="recovery"):
        lexe_setup.setup(sdk, state)
    assert calls.count("create_client") == 1


def test_lost_seed_never_generates_a_replacement(tmp_path):
    state = tmp_path / "private"
    sdk, calls, _, _ = fake_sdk(state)
    lexe_setup.setup(sdk, state)
    (state / "seedphrase.txt").unlink()
    with pytest.raises(PreflightError, match="seed_missing"):
        lexe_setup.setup(sdk, state)
    assert not (state / "seedphrase.txt").exists()
    assert calls == ["signup", "create_client"]


@pytest.mark.parametrize("field,value", [("balance_sats", 1), ("num_channels", 1)])
def test_never_changes_a_funded_wallet(tmp_path, field, value):
    sdk, calls, node, _ = fake_sdk(tmp_path)
    setattr(node, field, value)
    with pytest.raises(PreflightError, match="not_empty"):
        lexe_setup.setup(sdk, tmp_path)
    assert calls == ["signup"]


def test_no_sgx_bypass(tmp_path):
    sdk, calls, _, _ = fake_sdk(tmp_path)
    sdk.WalletConfig.mainnet = lambda: SimpleNamespace(use_sgx=False)
    with pytest.raises(PreflightError, match="attested"):
        lexe_setup.setup(sdk, tmp_path)
    assert not calls


def test_broad_credentials_rejected(tmp_path):
    sdk, _, _, info = fake_sdk(tmp_path)
    info.scopes.append("spend")
    with pytest.raises(PreflightError, match="receive_only"):
        lexe_setup.setup(sdk, tmp_path)


def test_public_directory_rejected(tmp_path):
    tmp_path.chmod(0o755)
    sdk, calls, _, _ = fake_sdk(tmp_path)
    with pytest.raises(PreflightError, match="owner_only"):
        lexe_setup.setup(sdk, tmp_path)
    assert not calls


def test_seed_symlink_rejected(tmp_path):
    target = tmp_path / "seedphrase.txt"
    target.symlink_to(tmp_path / "nonexistent")
    sdk, calls, _, _ = fake_sdk(tmp_path)
    with pytest.raises(PreflightError, match="owner_only"):
        lexe_setup.setup(sdk, tmp_path)
    assert not calls


def test_private_write_is_exclusive(tmp_path):
    target = tmp_path / "test.txt"
    lexe_setup._write_new(target, "original")
    with pytest.raises(FileExistsError):
        lexe_setup._write_new(target, "replacement")
    assert target.read_text() == "original"
