"""Dedicated receiving-only LND node. Never sends money or enables web deposits.

Recovery encryption uses an operator RSA public key. The private recovery key
must stay off the VM. GCS identity is objectCreator on the backup bucket only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import pwd
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BIN = Path("/opt/lnd-v0.21.3-beta")
DATA = Path("/srv/lnd")
ETC = Path("/etc/lnd")
DEVICE = Path("/dev/disk/by-id/google-tr-lightning-data")
NETWORK = DATA / "data/chain/bitcoin/mainnet"
SEED = DATA / "recovery/seed.encrypted.json"
PUBLIC_KEY = ETC / "recovery-public.pem"
AAD = b"lightningrouter-recovery-v1"


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)  # noqa: S603
    if result.returncode:
        # A subprocess may include a credential in its error. Never relay it.
        raise RuntimeError("node command failed: " + Path(args[0]).name)
    return result.stdout


def write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if exclusive:
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return
    temporary = path.with_name(path.name + ".next")
    with temporary.open("wb") as output:
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def seal(data: bytes, public_pem: bytes) -> bytes:
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("recovery needs an RSA public key of at least 3072 bits")
    dek, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    wrapped = key.encrypt(dek, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD))
    return json.dumps({"version": 1, **{
        name: base64.b64encode(value).decode("ascii")
        for name, value in {"key": wrapped, "nonce": nonce, "ciphertext": AESGCM(dek).encrypt(nonce, data, AAD)}.items()
    }}, sort_keys=True).encode()


def request_json(url: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    if url != "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token":
        raise ValueError("unexpected metadata URL")
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers or {})  # noqa: S310 - exact URL allowlist
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:  # noqa: S310 - callers supply fixed metadata URL
        result: dict[str, Any] = json.loads(response.read(1_000_000))
        return result


def upload(blob: bytes, kind: str) -> str:
    settings = json.loads((ETC / "node.json").read_bytes())
    bucket = settings["backup_bucket"]
    if bucket != "quill-cloud-proxy-lightning-recovery":
        raise ValueError("unexpected backup bucket")
    token = request_json("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                         headers={"Metadata-Flavor": "Google"})["access_token"]
    name = f"tr-bitcoin-1/{kind}/{int(time.time())}-{hashlib.sha256(blob).hexdigest()}.json"
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?" + urllib.parse.urlencode(
        {"uploadType": "media", "ifGenerationMatch": "0", "name": name},
    )
    req = urllib.request.Request(url, data=blob, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/octet-stream",
    })
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as response:  # noqa: S310
        result = json.loads(response.read(1_000_000))
    if result.get("name") != name or int(result.get("size", -1)) != len(blob):
        raise RuntimeError("backup upload was not acknowledged")
    return name


def rpc(path: str, body: dict[str, Any] | None = None, *, authenticated: bool = True) -> dict[str, Any]:
    import ssl

    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Grpc-Metadata-macaroon"] = (NETWORK / "admin.macaroon").read_bytes().hex()
    context = ssl.create_default_context(cafile=str(DATA / "tls.cert"))
    request = urllib.request.Request("https://127.0.0.1:8080" + path,
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=120) as response:  # noqa: S310 - fixed loopback TLS
        result: dict[str, Any] = json.loads(response.read(10_000_000))
        return result


def config(ip: str, rpc_password: str, *, initialized: bool) -> str:
    address = ipaddress.ip_address(ip)
    if not address.is_global or address.version != 4:
        raise ValueError("peer address must be public IPv4")
    if not rpc_password or any(not (c.isalnum() or c in "-_") for c in rpc_password):
        raise ValueError("invalid RPC credential")
    return (
        "[Application Options]\nalias=LightningRouter\ncolor=#164c67\n"
        "listen=0.0.0.0:9735\nrpclisten=127.0.0.1:10009\nrestlisten=127.0.0.1:8080\n"
        f"externalip={address}:9735\n"
        "rejectpush=true\nrejecthtlc=true\nmaxpendingchannels=1\nmaxchansize=10000000\n"
        "debuglevel=warn\nlogging.file.max-files=3\nlogging.file.max-file-size=10\n"
        + ("wallet-unlock-password-file=/run/credentials/tr-lnd.service/wallet-password\n" if initialized else "")
        + "[Bitcoin]\nbitcoin.mainnet=true\nbitcoin.node=bitcoind\nbitcoin.defaultchanconfs=3\n"
        "[Bitcoind]\nbitcoind.rpchost=127.0.0.1:8332\nbitcoind.rpcuser=tr_lnd\n"
        f"bitcoind.rpcpass={rpc_password}\n"
        "bitcoind.zmqpubrawblock=tcp://127.0.0.1:28332\nbitcoind.zmqpubrawtx=tcp://127.0.0.1:28333\n"
        "[db]\ndb.bolt.auto-compact=true\n"
    )


def write_config(*, initialized: bool) -> None:
    settings = json.loads((ETC / "node.json").read_bytes())
    password = (ETC / "bitcoin-password").read_text().strip()
    path = ETC / "lnd.conf"
    write_private(path, config(settings["peer_ip"], password, initialized=initialized).encode())
    os.chown(path, 0, pwd.getpwnam("lnd").pw_gid)
    path.chmod(0o640)


def mount_data() -> None:
    if not DEVICE.is_block_device():
        raise ValueError("dedicated Lightning disk is missing")
    DATA.mkdir(exist_ok=True)
    mounted = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--mountpoint", str(DATA)],  # noqa: S603,S607
                             capture_output=True, text=True, check=False)
    if mounted.returncode == 0:
        if Path(mounted.stdout.strip()).resolve() != DEVICE.resolve():
            raise ValueError("unexpected Lightning mount")
        return
    if any(DATA.iterdir()):
        raise ValueError("refusing to hide existing Lightning files")
    signatures = json.loads(run("wipefs", "--json", str(DEVICE))).get("signatures", [])
    if not signatures:
        run("mkfs.ext4", "-L", "tr-lightning", str(DEVICE))
    elif len(signatures) != 1 or signatures[0].get("type") != "ext4":
        raise ValueError("refusing to format existing disk signatures")
    uuid = run("blkid", "-s", "UUID", "-o", "value", str(DEVICE)).strip()
    if not uuid or any(c not in "0123456789abcdef-" for c in uuid):
        raise ValueError("invalid disk UUID")
    fstab = Path("/etc/fstab")
    entry = f"UUID={uuid} {DATA} ext4 defaults,nodev,nosuid,noexec 0 2"
    old = fstab.read_text()
    if entry not in old.splitlines():
        if any(len(parts := line.split()) > 1 and parts[1] == str(DATA) for line in old.splitlines()):
            raise ValueError("unexpected existing Lightning fstab entry")
        fstab.write_text(old.rstrip() + "\n" + entry + "\n")
    run("mount", str(DATA))


def install(ip: str, bucket: str) -> None:
    if bucket != "quill-cloud-proxy-lightning-recovery":
        raise ValueError("unexpected backup bucket")
    config(ip, "validate", initialized=False)
    # Validate backup recipient before generating any wallet material.
    seal(b"preflight", PUBLIC_KEY.read_bytes())
    mount_data()
    try:
        user = pwd.getpwnam("lnd")
    except KeyError:
        run("useradd", "--system", "--home-dir", str(DATA), "--shell", "/usr/sbin/nologin", "lnd")
        user = pwd.getpwnam("lnd")
    os.chown(DATA, user.pw_uid, user.pw_gid)
    DATA.chmod(0o700)
    ETC.chmod(0o750)
    os.chown(ETC, 0, user.pw_gid)
    for name in ("bitcoin-password", "wallet.passwd"):
        path = ETC / name
        if not path.exists():
            if (NETWORK / "wallet.db").exists():
                raise ValueError("existing wallet requires its original credentials")
            write_private(path, secrets.token_urlsafe(48).encode(), exclusive=True)
    settings = {"peer_ip": ip, "backup_bucket": bucket}
    write_private(ETC / "node.json", json.dumps(settings).encode())
    salt = secrets.token_hex(16)
    password = (ETC / "bitcoin-password").read_bytes()
    digest = hmac.new(salt.encode(), password, "sha256").hexdigest()
    fragment = Path("/etc/bitcoin/lightning.conf")
    write_private(fragment, (
        f"rpcauth=tr_lnd:{salt}${digest}\nzmqpubrawblock=tcp://127.0.0.1:28332\n"
        "zmqpubrawtx=tcp://127.0.0.1:28333\n"
    ).encode())
    os.chown(fragment, 0, pwd.getpwnam("bitcoin").pw_gid)
    fragment.chmod(0o640)
    write_config(initialized=(NETWORK / "wallet.db").exists())
    Path("/etc/systemd/system/tr-lnd.service").write_text(
        "[Unit]\nDescription=LightningRouter receiving node\nAfter=network-online.target tr-bitcoin.service\n"
        "Wants=network-online.target\nRequires=tr-bitcoin.service\nRequiresMountsFor=/srv/lnd\n"
        "StartLimitIntervalSec=600\nStartLimitBurst=5\n"
        "[Service]\nUser=lnd\nGroup=lnd\nUMask=0077\n"
        "LoadCredential=wallet-password:/etc/lnd/wallet.passwd\n"
        f"ExecStart={BIN}/lnd --lnddir={DATA} --configfile={ETC}/lnd.conf\n"
        "Restart=on-failure\nRestartSec=15\nTimeoutStopSec=300\nLimitNOFILE=65536\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nPrivateDevices=true\nProtectSystem=strict\nProtectHome=true\n"
        "ProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\n"
        "RestrictSUIDSGID=true\nLockPersonality=true\nCapabilityBoundingSet=\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\nReadWritePaths=/srv/lnd\nMemoryMax=2G\n"
        "LimitCORE=0\n[Install]\nWantedBy=multi-user.target\n"
    )
    Path("/etc/systemd/system/tr-lnd-backup.service").write_text(
        "[Unit]\nDescription=Encrypted off-node Lightning recovery backup\nAfter=tr-lnd.service\n"
        "RequiresMountsFor=/srv/lnd\n[Service]\nType=oneshot\nUMask=0077\n"
        "ExecStart=/usr/bin/python3 /opt/tr-lightning/lnd_node.py backup\nTimeoutStartSec=90\n"
        "NoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\nProtectSystem=strict\n"
        "ReadWritePaths=/srv/lnd/recovery\nLimitCORE=0\n"
    )
    Path("/etc/systemd/system/tr-lnd-backup.timer").write_text(
        "[Unit]\nDescription=Check Lightning channel backups every minute\n"
        "[Timer]\nOnBootSec=90\nOnUnitInactiveSec=60\nAccuracySec=5\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    run("systemctl", "daemon-reload")
    print("Lightning configuration installed; wallet and listener not activated")


def initialize() -> None:
    if (NETWORK / "wallet.db").exists() or SEED.exists():
        raise ValueError("wallet or recovery material exists; refusing to initialize again")
    info = json.loads(run("runuser", "-u", "bitcoin", "--", "/opt/bitcoin-31.1/bin/bitcoin-cli",
                          "-conf=/etc/bitcoin/bitcoin.conf", "-datadir=/srv/bitcoin", "getblockchaininfo"))
    if info.get("chain") != "main" or info.get("initialblockdownload") is not False or info["blocks"] != info["headers"]:
        raise ValueError("Bitcoin not synced")
    seed = rpc("/v1/genseed", authenticated=False)["cipher_seed_mnemonic"]
    if not isinstance(seed, list) or len(seed) != 24 or not all(isinstance(word, str) for word in seed):
        raise ValueError("invalid seed response")
    password = (ETC / "wallet.passwd").read_bytes()
    recovery = json.dumps({"version": 1, "network": "mainnet", "seed": seed,
                           "wallet_password": password.decode(), "created_at": int(time.time())}).encode()
    blob = seal(recovery, PUBLIC_KEY.read_bytes())
    write_private(SEED, blob, exclusive=True)
    name = upload(blob, "seed")
    write_private(DATA / "recovery/seed-upload.json", json.dumps({"object": name, "at": int(time.time())}).encode())
    # Persist and acknowledge the encrypted off-host seed BEFORE creating a wallet.
    rpc("/v1/initwallet", {"wallet_password": base64.b64encode(password).decode(),
                          "cipher_seed_mnemonic": seed, "recovery_window": 0}, authenticated=False)
    write_config(initialized=True)
    print("Wallet initialized; encrypted seed uploaded; automatic unlock configured")


def backup() -> None:
    if not SEED.is_file() or not (DATA / "recovery/seed-upload.json").is_file():
        raise ValueError("seed backup missing")
    scb = NETWORK / "channel.backup"
    if not scb.is_file():
        raise ValueError("static channel backup missing")
    content = scb.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    marker = DATA / "recovery/channel-upload.json"
    previous = json.loads(marker.read_bytes()) if marker.exists() else {}
    if previous.get("sha256") != digest:
        blob = seal(content, PUBLIC_KEY.read_bytes())
        name = upload(blob, "channels")
        write_private(DATA / "recovery/channel.encrypted.json", blob)
        write_private(marker, json.dumps({"object": name, "sha256": digest, "at": int(time.time())}).encode())
    print(json.dumps({"event": "lightning.backup", "ok": True, "changed": previous.get("sha256") != digest}))


def status() -> None:
    info = rpc("/v1/getinfo")
    balance = rpc("/v1/balance/channels")
    safe = {key: info.get(key) for key in (
        "identity_pubkey", "alias", "synced_to_chain", "synced_to_graph", "block_height", "uris",
        "num_peers", "num_active_channels", "num_pending_channels", "num_inactive_channels",
    )}
    safe["remote_balance_sat"] = balance.get("remote_balance", {}).get("sat", "0")
    safe["customer_payments_enabled"] = False
    print(json.dumps(safe, sort_keys=True))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "initialize", "backup", "status"))
    parser.add_argument("--peer-ip")
    parser.add_argument("--backup-bucket", default="quill-cloud-proxy-lightning-recovery")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("root required on dedicated node")
    if args.command == "install":
        install(args.peer_ip, args.backup_bucket)
    else:
        {"initialize": initialize, "backup": backup, "status": status}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Lightning operation failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
