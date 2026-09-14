from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.lightning import bitcoin_node as node


def chain(**changes: Any) -> dict[str, Any]:
    return {
        "chain": "main", "initialblockdownload": False,
        "blocks": 960000, "headers": 960000,
        "time": 100000, "verificationprogress": 0.99999999, **changes,
    }


def test_synced_means_validated_tip_not_only_headers() -> None:
    assert node.sync_ready(chain(), 100500)
    assert not node.sync_ready(chain(blocks=0), 100500)
    assert not node.sync_ready(chain(blocks=400000), 100500)
    assert not node.sync_ready(chain(initialblockdownload=True), 100500)
    assert not node.sync_ready(chain(chain="test"), 100500)
    assert not node.sync_ready(chain(verificationprogress=0.99), 100500)
    assert not node.sync_ready(chain(), 108000)
    assert not node.sync_ready(chain(), 99000)
    assert not node.sync_ready(chain(warnings=["chain warning"]), 100500)
    assert not node.sync_ready({}, 100500)


@pytest.mark.parametrize("ram,expected", [(32000, (16000, 4, 24000)), (3900, (512, 2, 2560))])
def test_resize_uses_small_cache_without_resync(ram: int, expected: tuple[int, int, int]) -> None:
    assert node.memory_config(ram) == expected


def test_undersized_vm_fails_instead_of_oom_loop() -> None:
    with pytest.raises(ValueError, match="3 GiB"):
        node.memory_config(1900)


def test_signer_parser_requires_machine_status_and_primary_fingerprint() -> None:
    primary = node.SIGNERS["fanquake"]
    subkey = "CFB16E21C950F67FA95E558F2EEB9F5CC09526C1"
    assert node.valid_signers(
        f"gpg: Good signature from {primary}\n"
        f"[GNUPG:] GOODSIG {subkey} Builder\n"
        f"[GNUPG:] VALIDSIG {subkey} 2026-07-07 1783414423 0 4 0 1 8 00 {primary}\n"
    ) == {primary}
    assert node.valid_signers(f"[GNUPG:] BADSIG {primary}\ngpg: Good signature") == set()


@pytest.mark.parametrize("signatures", [set(), {node.SIGNERS["fanquake"]}, {"A" * 40}])
def test_unsigned_or_one_signer_release_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signatures: set[str],
) -> None:
    monkeypatch.setattr(node, "download", lambda *args: None)
    monkeypatch.setattr(node, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, "", ""))
    monkeypatch.setattr(node, "valid_signers", lambda status: signatures)
    with pytest.raises(ValueError, match="both required valid signatures"):
        node.verify_release(tmp_path)


@pytest.mark.parametrize("url", ["http://example.com/x", "file:///etc/passwd", "ftp://example.com/x"])
def test_release_download_rejects_non_https(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        node.download(url, tmp_path / "download")


def test_serve_recalculates_memory_and_preserves_data_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node, "runtime_options", lambda: (512, 2, 2560))
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(node.os, "execv", lambda executable, args: calls.append((executable, args)))
    node.serve()
    executable, args = calls[0]
    assert executable == str(node.BIN / "bitcoind")
    assert "-dbcache=512" in args
    assert f"-datadir={node.DATA}" in args
    assert not any("reindex" in argument for argument in args)


def test_bootstrap_is_not_payment_readiness() -> None:
    source = Path(node.__file__).read_text()
    assert "lightning_payments_ready=False" in source
    assert "disablewallet=1" in source
    assert "rpcbind=127.0.0.1" in source
    assert "listen=0" in source
    assert "NoNewPrivileges=true" in source
    assert "MemoryMax=80%" in source
    assert "ForwardToSyslog=no" in source
    # ZMQ needs netlink interface enumeration even with loopback publishers.
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in source


def test_startup_shell_has_valid_syntax() -> None:
    script = Path(node.__file__).with_name("bitcoin_startup.sh")
    subprocess.run(["/bin/bash", "-n", str(script)], check=True)  # noqa: S603


@pytest.mark.parametrize("filesystem,signatures", [("xfs", []), ("", [{"type": "gpt"}])])
def test_mount_never_formats_unknown_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem: str, signatures: list[dict[str, str]],
) -> None:
    import json

    device, data = tmp_path / "device", tmp_path / "mount"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(node, "DEVICE", device)
    monkeypatch.setattr(node, "DATA", data)
    monkeypatch.setattr(Path, "is_block_device", lambda path: path == device)

    def fake_run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "findmnt":
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[0] == "blkid":
            return subprocess.CompletedProcess(args, 0 if filesystem else 2, filesystem, "")
        if args[0] == "wipefs":
            return subprocess.CompletedProcess(args, 0, json.dumps({"signatures": signatures}), "")
        raise AssertionError(f"unsafe command: {args}")

    monkeypatch.setattr(node, "run", fake_run)
    with pytest.raises(ValueError, match="filesystem|existing signatures"):
        node.mount_data()
    assert not any(command[0] == "mkfs.ext4" for command in calls)


def test_mount_rejects_wrong_disk_at_data_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device, data = tmp_path / "device", tmp_path / "mount"
    monkeypatch.setattr(node, "DEVICE", device)
    monkeypatch.setattr(node, "DATA", data)
    monkeypatch.setattr(Path, "is_block_device", lambda path: path == device)
    monkeypatch.setattr(node, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "/dev/wrong", ""))
    with pytest.raises(ValueError, match="unexpected disk already mounted"):
        node.mount_data()
