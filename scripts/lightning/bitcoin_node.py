"""Isolated, wallet-disabled Bitcoin Core bootstrap and serial sync reporting.

Executed as root only on the dedicated, newly provisioned Bitcoin VM. The data
disk must have the exact GCE device name below. Existing unknown data is never
formatted. No cloud credentials, Lightning seeds or payment keys are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

VERSION = "31.1"
ARCHIVE = f"bitcoin-{VERSION}-x86_64-linux-gnu.tar.gz"
ARCHIVE_SHA256 = "b80d9c3e04da78fb6f0569685673418cf686fadba9042d926d13fb87ff503f9e"
BUILD_KEYS_REVISION = "3b667ee3ebb3dcd9e1990cf03e38a0935eec1683"
SIGNERS = {
    "fanquake": "E777299FC265DD04793070EB944D35F9AC3DB76A",
    "achow101": "152812300785C96444D3334D17565732E08E5E41",
}
DEVICE = Path("/dev/disk/by-id/google-tr-bitcoin-data")
DATA = Path("/srv/bitcoin")
CONFIG = Path("/etc/bitcoin/bitcoin.conf")
BIN = Path(f"/opt/bitcoin-{VERSION}/bin")
STATE = Path("/var/lib/tr-bitcoin")


def run(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    # All callers supply fixed executables and structured arguments; no shell.
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=timeout)  # noqa: S603


def valid_signers(status: str) -> set[str]:
    result = set()
    for line in status.splitlines():
        fields = line.split()
        # VALIDSIG includes the signing subkey followed by the primary key.
        if len(fields) == 12 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            result.add(fields[-1])
    return result


def download(url: str, path: Path, limit: int = 100_000_000) -> None:
    if not url.startswith("https://"):
        raise ValueError("release downloads require HTTPS")
    with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as output:  # noqa: S310 - HTTPS checked above; pinned digests/signatures below
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise ValueError("oversized release download")
            output.write(block)


def verify_release(directory: Path) -> None:
    manifest = directory / "SHA256SUMS"
    home = directory / "gnupg"
    home.mkdir(mode=0o700)
    for name in SIGNERS:
        key = directory / f"{name}.gpg"
        download(
            f"https://raw.githubusercontent.com/bitcoin-core/guix.sigs/"
            f"{BUILD_KEYS_REVISION}/builder-keys/{name}.gpg", key, 1_000_000,
        )
        run("gpg", "--homedir", str(home), "--batch", "--import", str(key))
    verification = run(
        "gpg", "--homedir", str(home), "--batch", "--status-fd", "1",
        "--verify", str(directory / "SHA256SUMS.asc"), str(manifest), check=False,
    )
    # Other builders' unknown public keys can make gpg exit 2. Require BOTH
    # pinned primary fingerprints, not its exit status or human-readable text.
    if not set(SIGNERS.values()).issubset(valid_signers(verification.stdout)):
        raise ValueError("Bitcoin release does not have both required valid signatures")
    entries = [line.split() for line in manifest.read_text().splitlines()]
    if [ARCHIVE_SHA256, ARCHIVE] not in entries:
        raise ValueError("pinned Bitcoin checksum is absent from signed manifest")
    with (directory / ARCHIVE).open("rb") as archive:
        checksum = hashlib.file_digest(archive, "sha256").hexdigest()
    if checksum != ARCHIVE_SHA256:
        raise ValueError("Bitcoin archive checksum mismatch")


def install_binaries() -> None:
    marker = BIN.parent / "verified-sha256"
    if marker.exists() and marker.read_text().strip() == ARCHIVE_SHA256:
        if all((BIN / name).is_file() for name in ("bitcoind", "bitcoin-cli")):
            return
    with tempfile.TemporaryDirectory(prefix="bitcoin-release-") as temp:
        directory = Path(temp)
        for name in ("SHA256SUMS", "SHA256SUMS.asc", ARCHIVE):
            download(f"https://bitcoincore.org/bin/bitcoin-core-{VERSION}/{name}", directory / name)
        verify_release(directory)
        BIN.mkdir(parents=True, exist_ok=True)
        with tarfile.open(directory / ARCHIVE) as archive:
            for name in ("bitcoind", "bitcoin-cli"):
                member = archive.getmember(f"bitcoin-{VERSION}/bin/{name}")
                if not member.isfile():
                    raise ValueError("release binary is not a regular file")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("missing release binary")
                with source, (BIN / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
                (BIN / name).chmod(0o755)
        marker.write_text(ARCHIVE_SHA256 + "\n")


def mount_data() -> None:
    if not DEVICE.is_block_device():
        raise ValueError("dedicated tr-bitcoin-data block device is missing")
    DATA.mkdir(exist_ok=True)
    mounted = run("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(DATA), check=False)
    if mounted.returncode == 0:
        if Path(mounted.stdout.strip()).resolve() != DEVICE.resolve():
            raise ValueError("unexpected disk already mounted on Bitcoin data path")
        return
    if any(DATA.iterdir()):
        raise ValueError("refusing to hide existing unmounted data")
    filesystem = run("blkid", "-p", "-s", "TYPE", "-o", "value", str(DEVICE), check=False)
    if filesystem.returncode == 2:
        signatures = json.loads(run("wipefs", "--json", str(DEVICE)).stdout)
        if signatures.get("signatures"):
            raise ValueError("refusing to format a disk with existing signatures")
        run("mkfs.ext4", "-L", "tr-bitcoin", str(DEVICE))
    elif filesystem.returncode != 0 or filesystem.stdout.strip() != "ext4":
        raise ValueError("unexpected Bitcoin disk filesystem")
    uuid = run("blkid", "-s", "UUID", "-o", "value", str(DEVICE)).stdout.strip()
    if not uuid or any(character not in "0123456789abcdef-" for character in uuid):
        raise ValueError("invalid data disk UUID")
    fstab = Path("/etc/fstab")
    entry = f"UUID={uuid} {DATA} ext4 defaults,nodev,nosuid,noexec 0 2"
    text = fstab.read_text()
    if entry not in text.splitlines():
        if any(len(parts := line.split()) > 1 and parts[1] == str(DATA) for line in text.splitlines()):
            raise ValueError("unexpected Bitcoin fstab entry")
        fstab.write_text(text.rstrip() + "\n" + entry + "\n")
    run("mount", str(DATA))


def memory_config(total_mib: int) -> tuple[int, int, int]:
    if total_mib < 3000:
        raise ValueError("Bitcoin VM needs at least 3 GiB usable memory")
    if total_mib >= 24000:
        return 16000, 4, 24000
    return 512, 2, 2560


def runtime_options() -> tuple[int, int, int]:
    memory = next(line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:"))
    return memory_config(int(memory.split()[1]) // 1024)


def serve() -> None:
    # Recompute before EVERY daemon start, including the first boot after a
    # resize. The guest startup script can run later than enabled services.
    cache, threads, _ = runtime_options()
    os.execv(str(BIN / "bitcoind"), [  # noqa: S606 - pinned, verified absolute executable; fixed arguments
        str(BIN / "bitcoind"), f"-conf={CONFIG}", f"-datadir={DATA}",
        f"-dbcache={cache}", f"-par={threads}",
    ])


def write_config() -> None:
    runtime_options()
    CONFIG.parent.mkdir(mode=0o750, exist_ok=True)
    CONFIG.write_text(
        "chain=main\nserver=1\ndisablewallet=1\nlisten=0\nnatpmp=0\n"
        "prune=30000\ntxindex=0\nmaxmempool=100\nrpcthreads=2\n"
        "maxuploadtarget=512\nlogips=0\nnodebuglogfile=1\nprinttoconsole=1\n"
        "dbcache=512\npar=2\n"
        "[main]\nrpcbind=127.0.0.1\nrpcallowip=127.0.0.1\n"
    )
    CONFIG.chmod(0o640)
    account = pwd.getpwnam("bitcoin")
    os.chown(CONFIG.parent, 0, account.pw_gid)
    os.chown(CONFIG, 0, account.pw_gid)
    os.chown(DATA, account.pw_uid, account.pw_gid)
    DATA.chmod(0o700)
    Path("/etc/systemd/system/tr-bitcoin.service").write_text(
        "[Unit]\nDescription=TrustedRouter isolated Bitcoin full node\n"
        "Wants=network-online.target\nAfter=network-online.target\n"
        f"RequiresMountsFor={DATA}\nStartLimitIntervalSec=600\nStartLimitBurst=5\n"
        "[Service]\nType=simple\nUser=bitcoin\nGroup=bitcoin\nUMask=0077\n"
        "ExecStart=/usr/bin/python3 /opt/tr-bitcoin/node.py serve\n"
        "KillSignal=SIGINT\nTimeoutStopSec=600\nRestart=on-failure\nRestartSec=30\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nPrivateDevices=true\nProtectHome=true\n"
        "ProtectSystem=strict\nProtectKernelTunables=true\nProtectKernelModules=true\n"
        "ProtectControlGroups=true\nRestrictSUIDSGID=true\nLockPersonality=true\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\nCapabilityBoundingSet=\n"
        f"ReadWritePaths={DATA}\nMemoryMax=80%\n"
        "Environment=MALLOC_ARENA_MAX=2\nLimitNOFILE=65536\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    journal_dir = Path("/etc/systemd/journald.conf.d")
    journal_dir.mkdir(exist_ok=True)
    (journal_dir / "bitcoin-bounded.conf").write_text(
        "[Journal]\nSystemMaxUse=512M\nRuntimeMaxUse=128M\nMaxRetentionSec=7day\n"
        "ForwardToSyslog=no\n"
    )
    Path("/etc/systemd/system/tr-bitcoin-status.service").write_text(
        "[Unit]\nDescription=Bitcoin sync progress (public chain metadata only)\n"
        "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /opt/tr-bitcoin/node.py status\n"
        "TimeoutStartSec=30\nStandardOutput=journal+console\nStandardError=journal+console\n"
    )
    Path("/etc/systemd/system/tr-bitcoin-status.timer").write_text(
        "[Unit]\nDescription=Report Bitcoin sync progress every five minutes\n"
        "[Timer]\nOnBootSec=60\nOnUnitActiveSec=300\nAccuracySec=10\n"
        "[Install]\nWantedBy=timers.target\n"
    )


def sync_ready(info: dict[str, Any], now: int) -> bool:
    blocks, headers = info.get("blocks"), info.get("headers")
    timestamp = info.get("time")
    return (
        info.get("chain") == "main"
        and info.get("initialblockdownload") is False
        and isinstance(blocks, int) and blocks > 0 and blocks == headers
        and isinstance(timestamp, int) and 0 <= now - timestamp < 7200
        and float(info.get("verificationprogress", 0)) >= 0.99999
        and not info.get("warnings")
    )


def status() -> None:
    info = json.loads(run(
        "runuser", "-u", "bitcoin", "--", str(BIN / "bitcoin-cli"),
        f"-conf={CONFIG}", f"-datadir={DATA}", "getblockchaininfo", timeout=15,
    ).stdout)
    payload = {key: info.get(key) for key in (
        "chain", "blocks", "headers", "time", "verificationprogress",
        "initialblockdownload", "size_on_disk", "pruned", "warnings",
    )}
    payload.update(
        event="bitcoin.sync", checked_at=int(time.time()),
        synced=sync_ready(info, int(time.time())),
        free_disk_gib=shutil.disk_usage(DATA).free // (1024 ** 3),
        lightning_payments_ready=False,
    )
    STATE.mkdir(mode=0o755, exist_ok=True)
    temporary = STATE / "status.next.json"
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
    temporary.replace(STATE / "status.json")
    print("TR_BITCOIN_STATUS " + json.dumps(payload, sort_keys=True), flush=True)


def install() -> None:
    if os.geteuid() != 0:
        raise ValueError("bootstrap must run as root on the dedicated VM")
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    run("apt-get", "update", timeout=600)
    run("apt-get", "install", "-y", "ca-certificates", "gnupg", "e2fsprogs", timeout=600)
    try:
        pwd.getpwnam("bitcoin")
    except KeyError:
        run("useradd", "--system", "--home-dir", str(DATA), "--shell", "/usr/sbin/nologin", "bitcoin")
    mount_data()
    install_binaries()
    write_config()
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "systemd-journald")
    run("systemctl", "enable", "--now", "tr-bitcoin.service")
    run("systemctl", "enable", "--now", "tr-bitcoin-status.timer")
    print("TR_BITCOIN_BOOTSTRAP_OK: signature-verified Core 31.1; sync started; no wallet", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "status", "serve"))
    arguments = parser.parse_args()
    try:
        {"install": install, "status": status, "serve": serve}[arguments.command]()
    except Exception as error:
        print(f"TR_BITCOIN_ERROR {type(error).__name__}: {error}", flush=True)
        raise
