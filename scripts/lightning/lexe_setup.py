"""Create an unfunded Lexe wallet and a receive-only credential, never a payment.

Run with the pinned optional SDK: uv run --with lexe-sdk==0.1.19 -m
scripts.lightning.lexe_setup --state-dir <private-directory> --create-wallet
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from scripts.lightning.lexe_preflight import PreflightError, check_credentials

CLIENT_LABEL = "LightningRouter receive only"


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PreflightError("private_owner_only_path_required")


def _write_new(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def setup(sdk: Any, state: Path) -> dict[str, Any]:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(state, directory=True)
    seed_path = state / "seedphrase.txt"
    credential_path = state / "receive-client.txt"
    # Persist the recovery key BEFORE registration or provisioning. Never
    # generate a replacement for a malformed seed or an ambiguous signup.
    if not seed_path.exists() and not seed_path.is_symlink():
        if any(state.iterdir()):
            raise PreflightError("seed_missing_from_existing_state")
        seed = sdk.RootSeed.generate()
        seed.write_to_path(str(seed_path))
        _private(seed_path)
        with seed_path.open("rb") as source:
            os.fsync(source.fileno())
        _sync_directory(state)
    _private(seed_path)
    seed = sdk.RootSeed.read_from_path(str(seed_path))
    config = sdk.WalletConfig.mainnet()
    if config.use_sgx is not True:
        raise PreflightError("attested_mainnet_required")
    root = sdk.LexeWallet.without_db(config, sdk.Credentials.from_root_seed(seed))
    root.signup(root_seed=seed, partner_pk=None)
    node = root.node_info()
    if node.balance_sats != 0 or node.num_channels != 0:
        raise PreflightError("wallet_not_empty_stop_setup")
    if credential_path.exists() or credential_path.is_symlink():
        _private(credential_path)
        cc = sdk.ClientCredentials.from_string(credential_path.read_text().strip())
    else:
        # If creation succeeded but its response/file write was lost, do not
        # multiply credentials. The root holder must reconcile that client.
        if any(client.label == CLIENT_LABEL for client in root.list_clients().values()):
            raise PreflightError("existing_client_requires_recovery")
        created = root.create_client(
            scopes=[sdk.Scope.READ_INFO, sdk.Scope.READ_PAYMENTS, sdk.Scope.RECEIVE],
            expires_at_ms=int(time.time() * 1000) + 90 * 86_400_000,
            label=CLIENT_LABEL, permissions=[],
        )
        cc = created.client_credentials
        _write_new(credential_path, cc.export_string() + "\n")
    receiver = sdk.LexeWallet.without_db(config, sdk.Credentials.from_client_credentials(cc))
    info = receiver.client_info()
    check_credentials({"kind": info.kind.name.lower(), "scopes": info.scopes,
                       "permissions": info.permissions, "effective_permissions": info.effective_permissions,
                       "expires_at": info.expires_at_ms}, int(time.time() * 1000))
    if receiver.node_info().user_pk != node.user_pk:
        raise PreflightError("wrong_wallet")
    result = {"status": "unfunded_wallet_created", "network": "mainnet", "user_pk": node.user_pk,
              "client_pk": info.client_pk, "credential_expires_at_ms": info.expires_at_ms,
              "payment_verified": False, "production_cutover_allowed": False,
              "recovery_backup_required": True, "sdk_version": "0.1.19"}
    receipt = state / "wallet.json"
    if not receipt.exists():
        _write_new(receipt, json.dumps(result, indent=2) + "\n")
    else:
        _private(receipt)
        prior = json.loads(receipt.read_text())
        if prior.get("user_pk") != node.user_pk or prior.get("client_pk") != info.client_pk:
            raise PreflightError("wallet_receipt_mismatch")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--create-wallet", action="store_true", required=True)
    args = parser.parse_args()
    try:
        from importlib.metadata import version

        if version("lexe-sdk") != "0.1.19":
            raise PreflightError("pinned_sdk_required")
        result = setup(importlib.import_module("lexe"), args.state_dir)
    except PreflightError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    except Exception as exc:
        # SDK errors can contain payment information or secret inputs.
        print(json.dumps({"status": "blocked", "reason": "wallet_setup_failed",
                          "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
