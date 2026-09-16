"""Deploy BTCPay separately, reusing the existing Bitcoin/LND node without restarting it."""

from __future__ import annotations

import argparse
import base64
import io
import json
import secrets
import shlex
import subprocess
import tarfile
from pathlib import Path

from scripts.lightning.activate_web import Operator
from scripts.lightning.btcpay.bootstrap import private_json

VM = "tr-btcpay-1"
ZONE = "us-central1-a"
REGION = "us-central1"
SUBNET = "tr-btcpay-us-central1"
PRIVATE_IP = "10.92.2.2"
HOST = "btcpay.lightningrouter.ai."
SOURCE = "scripts/lightning/btcpay"
RPCS = (
    "/lnrpc.Lightning/GetInfo", "/lnrpc.Lightning/ListChannels",
    "/lnrpc.Lightning/WalletBalance", "/lnrpc.Lightning/ChannelBalance",
    "/lnrpc.Lightning/ListInvoices", "/lnrpc.Lightning/LookupInvoice",
    "/lnrpc.Lightning/SubscribeInvoices", "/lnrpc.Lightning/AddInvoice",
)

NODE_SCRIPT = r'''
import json, os, ssl, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
allowed = json.load(sys.stdin)["permissions"]
macaroon = Path("/etc/lnd/btcpay.macaroon")
cli = ["/opt/lnd-v0.21.3-beta/lncli", "--lnddir=/srv/lnd"]
if not macaroon.exists():
    ids = json.loads(subprocess.check_output(cli + ["listmacaroonids"]))["root_key_ids"]
    if "31" in [str(i) for i in ids]:
        raise SystemExit("BTCPay root key ID already used; inspect before proceeding")
    subprocess.run(cli + ["bakemacaroon", "--root_key_id=31", "--save_to=" + str(macaroon)] + ["uri:" + p for p in allowed], check=True, stdout=subprocess.DEVNULL)
os.chmod(macaroon, 0o600)
decoded = json.loads(subprocess.check_output(cli + ["printmacaroon", "--macaroon_file=" + str(macaroon)]))
permissions = set(decoded["permissions"])
if permissions != {"uri:" + p for p in allowed}:
    raise SystemExit("Macaroon permissions differ from exact URI allowlist")
cert = Path("/srv/lnd/tls.cert")
context = ssl.create_default_context(cafile=str(cert))
def read(path):
    request = urllib.request.Request("https://10.92.0.2:8080" + path, headers={"Grpc-Metadata-macaroon": macaroon.read_bytes().hex()})
    try:
        with urllib.request.urlopen(request, context=context, timeout=10) as result:
            return result.status, json.load(result)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())
status, info = read("/v1/getinfo")
if status != 200 or not info.get("synced_to_chain"):
    raise SystemExit("Existing LND node is not healthy")
status, denial = read("/v1/peers")
if status != 500 or denial.get("message") != "permission denied":
    raise SystemExit("Macaroon failed negative permission check")
print(json.dumps({"macaroon":macaroon.read_bytes().hex(), "certificate":cert.read_text(), "node_pubkey":info["identity_pubkey"]}))
'''


def ssh(operator: Operator, command: str, *, data: str | None = None, node: str = VM) -> str:
    return operator.gc("compute", "ssh", node, "--zone=" + ZONE, "--tunnel-through-iap",
                       "--ssh-key-expire-after=30m", "--command=" + command, data=data)


def committed_bundle() -> dict[str, str]:
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", SOURCE], text=True)  # noqa: S603,S607
    if dirty:
        raise ValueError("Commit the reviewed BTCPay source before deployment")
    archive = subprocess.check_output(["git", "archive", "HEAD", SOURCE])  # noqa: S603,S607
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for entry in tar:
            if not entry.isfile():
                continue
            stream = tar.extractfile(entry)
            if stream is None:
                raise ValueError("Missing archive entry")
            files[Path(entry.name).name] = base64.b64encode(stream.read()).decode()
    required = {"compose.yaml", "Caddyfile", "bootstrap.py", "install.sh"}
    if not required <= files.keys():
        raise ValueError("Incomplete committed BTCPay bundle")
    return files


def provision(operator: Operator) -> str:
    subnets = operator.gc("compute", "networks", "subnets", "list", "--filter=name=" + SUBNET, "--format=value(name)")
    if not subnets.strip():
        operator.gc("compute", "networks", "subnets", "create", SUBNET, "--network=tr-lightning",
                    "--region=" + REGION, "--range=10.92.2.0/28")
    addresses = operator.gc("compute", "addresses", "list", "--filter=name=" + VM, "--format=value(name)")
    if not addresses.strip():
        operator.gc("compute", "addresses", "create", VM, "--region=" + REGION)
    ip = operator.gc("compute", "addresses", "describe", VM, "--region=" + REGION, "--format=value(address)").strip()
    rules = set(operator.gc("compute", "firewall-rules", "list", "--format=value(name)").splitlines())
    for name, source, tag, ports in (
        ("tr-btcpay-iap", "35.235.240.0/20", "tr-btcpay", "tcp:22"),
        ("tr-btcpay-lnd", PRIVATE_IP + "/32", "tr-lightning", "tcp:8080"),
    ):
        if name not in rules:
            operator.gc("compute", "firewall-rules", "create", name, "--network=tr-lightning", "--direction=INGRESS",
                        "--source-ranges=" + source, "--target-tags=" + tag, "--allow=" + ports, "--enable-logging")
    vms = operator.gc("compute", "instances", "list", "--filter=name=" + VM, "--format=value(name)")
    if not vms.strip():
        operator.gc("compute", "instances", "create", VM, "--zone=" + ZONE, "--machine-type=e2-medium",
                    "--subnet=" + SUBNET, "--private-network-ip=" + PRIVATE_IP, "--address=" + ip,
                    "--no-service-account", "--no-scopes", "--image-family=debian-12", "--image-project=debian-cloud",
                    "--boot-disk-size=30GB", "--boot-disk-type=pd-balanced", "--tags=tr-btcpay",
                    "--labels=service=lightning-btcpay", "--metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE",
                    "--shielded-secure-boot", "--shielded-vtpm", "--shielded-integrity-monitoring")
    vm = json.loads(operator.gc("compute", "instances", "describe", VM, "--zone=" + ZONE, "--format=json"))
    if vm.get("serviceAccounts") or vm.get("labels", {}).get("service") != "lightning-btcpay":
        raise RuntimeError("Existing VM does not match isolated dashboard policy")
    policies = operator.gc("compute", "resource-policies", "list", "--filter=name=tr-btcpay-daily", "--format=value(name)")
    if not policies.strip():
        operator.gc("compute", "resource-policies", "create", "snapshot-schedule", "tr-btcpay-daily",
                    "--region=" + REGION, "--daily-schedule", "--start-time=05:00", "--max-retention-days=7",
                    "--storage-location=us", "--on-source-disk-delete=keep-auto-snapshots")
    disk = json.loads(operator.gc("compute", "disks", "describe", VM, "--zone=" + ZONE, "--format=json"))
    if not any(p.endswith("/tr-btcpay-daily") for p in disk.get("resourcePolicies", [])):
        operator.gc("compute", "disks", "add-resource-policies", VM, "--zone=" + ZONE, "--resource-policies=tr-btcpay-daily")
    records = json.loads(operator.gc("dns", "record-sets", "list", "--zone=lightningrouter-ai", "--name=" + HOST, "--type=A", "--format=json"))
    if records and records[0]["rrdatas"] != [ip]:
        raise RuntimeError("Existing BTCPay DNS differs; refusing overwrite")
    if not records:
        operator.gc("dns", "record-sets", "create", HOST, "--zone=lightningrouter-ai", "--type=A", "--ttl=300", "--rrdatas=" + ip)
    return ip


def install(operator: Operator, files: dict[str, str]) -> None:
    ssh(operator, "sudo bash -s", data=base64.b64decode(files["install.sh"]).decode())
    credentials = json.loads(ssh(operator, "sudo python3 -c " + shlex.quote(NODE_SCRIPT),
                                 data=json.dumps({"permissions": RPCS}), node="tr-bitcoin-1"))
    payload = {"files": files, "credentials": credentials, "password": secrets.token_hex(32)}
    writer = r'''
import base64, json, os, sys
from pathlib import Path
p = json.load(sys.stdin)
root = Path("/opt/tr-btcpay")
os.umask(0o077)
for name in ("compose.yaml", "Caddyfile", "bootstrap.py", "install.sh"):
    (root/name).write_bytes(base64.b64decode(p["files"][name]))
env = root/".env"
if not env.exists():
    connection = "User ID=btcpay;Password=" + p["password"] + ";Host=postgres;Port=5432;Database=btcpay"
    env.write_text("POSTGRES_PASSWORD=" + p["password"] + "\nBTCPAY_DATABASE_CONNECTION=" + connection + "\n")
(root/"secrets/lnd.macaroon").write_bytes(bytes.fromhex(p["credentials"]["macaroon"]))
(root/"secrets/lnd.pem").write_text(p["credentials"]["certificate"])
(root/"expected-node.json").write_text(json.dumps({"node_pubkey": p["credentials"]["node_pubkey"]}))
'''
    ssh(operator, "sudo python3 -c " + shlex.quote(writer), data=json.dumps(payload))
    ssh(operator, "sudo docker-compose -f /opt/tr-btcpay/compose.yaml up -d postgres btcpay")


def publish(operator: Operator, output: Path) -> None:
    # Re-run is a no-op only after a complete, verified bootstrap; no public
    # listener/firewall is enabled before this succeeds.
    ssh(operator, "sudo python3 /opt/tr-btcpay/bootstrap.py --state /opt/tr-btcpay/access.json")
    state = json.loads(ssh(operator, "sudo cat /opt/tr-btcpay/access.json"))
    if not state.get("complete"):
        raise RuntimeError("Private bootstrap incomplete")
    private_json(output / "owner-access.json", {"url": "https://btcpay.lightningrouter.ai/login", "email": state["owner_email"], "password": state["owner_password"]})
    private_json(output / "greg-invitation.json", {"email": state["greg_email"], "invitation_url": state["greg_invitation"], "role": "Observer"})
    ssh(operator, "sudo systemctl start tr-btcpay.service tr-btcpay-backup.timer")
    ssh(operator, "sudo systemctl start tr-btcpay-backup.service")
    rules = set(operator.gc("compute", "firewall-rules", "list", "--format=value(name)").splitlines())
    if "tr-btcpay-https" not in rules:
        operator.gc("compute", "firewall-rules", "create", "tr-btcpay-https", "--network=tr-lightning", "--direction=INGRESS",
                    "--source-ranges=0.0.0.0/0", "--target-tags=tr-btcpay", "--allow=tcp:80,tcp:443", "--enable-logging")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("provision", "install", "publish"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".private/btcpay"))
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"stage": args.stage, "vm": VM, "existing_node": "tr-bitcoin-1", "spending": False, "apply": False}))
        return
    files = committed_bundle()
    operator = Operator(args.account)
    if args.stage == "provision":
        print(json.dumps({"public_ip": provision(operator), "public_ingress": False}))
    elif args.stage == "install":
        install(operator, files)
        print("Installed privately; existing Bitcoin/LND processes unchanged")
    else:
        publish(operator, args.output)
        print("BTCPay published; private account handoff files written (not printed)")


if __name__ == "__main__":
    main()
