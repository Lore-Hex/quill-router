"""Connect only invoice RPCs to the isolated Cloud Run funding subnet."""

from __future__ import annotations

import argparse
import json
import shlex

from scripts.lightning.activate_web import Operator

NODE_SCRIPT = r'''
import json, os, ssl, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, "/opt/tr-lightning")
import lnd_node
channels = lnd_node.rpc("/v1/channels")["channels"]
if any(c.get("pending_htlcs") for c in channels):
    raise SystemExit("Wait for pending HTLCs before changing the listener")
cert = Path("/srv/lnd/tls.cert")
details = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"], text=True)
if "IP Address:10.92.0.2" not in details:
    raise SystemExit("Private address absent from pinned LND certificate")
config = Path("/etc/lnd/lnd.conf")
original = config.read_text()
if "restlisten=10.92.0.2:8080" not in original:
    if "restlisten=127.0.0.1:8080\n" not in original:
        raise SystemExit("Unexpected REST listener configuration")
    backup = Path("/etc/lnd/lnd.conf.before-funding")
    if not backup.exists():
        backup.touch(mode=0o600)
        backup.write_text(original)
    config.write_text(original.replace("restlisten=127.0.0.1:8080\n", "restlisten=127.0.0.1:8080\nrestlisten=10.92.0.2:8080\n"))
    subprocess.run(["systemctl", "restart", "tr-lnd.service"], check=True)
    for attempt in range(30):
        try:
            lnd_node.rpc("/v1/getinfo")
            break
        except Exception:
            time.sleep(2)
    else:
        config.write_text(original)
        subprocess.run(["systemctl", "restart", "tr-lnd.service"], check=True)
        raise SystemExit("Private listener change rolled back")
macaroon = Path("/etc/lnd/funding.macaroon")
if not macaroon.exists():
    subprocess.run([
        "/opt/lnd-v0.21.3-beta/lncli", "--lnddir=/srv/lnd", "bakemacaroon",
        "--root_key_id=23", "--save_to=" + str(macaroon),
        "uri:/lnrpc.Lightning/GetInfo", "uri:/lnrpc.Lightning/ListChannels",
        "uri:/lnrpc.Lightning/AddInvoice", "uri:/lnrpc.Lightning/LookupInvoice",
        "uri:/invoicesrpc.Invoices/CancelInvoice",
    ], check=True, stdout=subprocess.DEVNULL)
os.chmod(macaroon, 0o600)
context = ssl.create_default_context(cafile=str(cert))
def check(path):
    request = urllib.request.Request("https://10.92.0.2:8080" + path,
        headers={"Grpc-Metadata-macaroon": macaroon.read_bytes().hex()})
    try:
        with urllib.request.urlopen(request, context=context, timeout=8) as result:
            return result.status
    except urllib.error.HTTPError as error:
        # LND wraps macaroon denial as gRPC UNKNOWN (2), which grpc-gateway
        # maps to HTTP 500. Accept only that exact authenticated denial, not
        # arbitrary server errors, as the negative permission check.
        body = json.loads(error.read())
        if error.code == 500 and body.get("code") == 2 and body.get("message") == "permission denied":
            return 403
        return error.code
if check("/v1/getinfo") != 200 or check("/v1/channels") != 200:
    raise SystemExit("Invoice credential health permissions failed")
if check("/v1/balance/blockchain") not in (401, 403):
    raise SystemExit("Credential unexpectedly permits non-invoice wallet RPC")
print(json.dumps({"macaroon": macaroon.read_bytes().hex(), "certificate": cert.read_text()}))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    operator = Operator(args.account)
    result = json.loads(operator.gc(
        "compute", "ssh", "tr-bitcoin-1", "--zone=us-central1-a", "--tunnel-through-iap",
        "--ssh-key-expire-after=30m", "--command=sudo python3 -c " + shlex.quote(NODE_SCRIPT),
    ))
    # Captured credentials never appear in the terminal, argv, or a local file.
    operator.secret("lightning-router-lnd-invoice-macaroon", result["macaroon"])
    operator.secret("lightning-router-lnd-tls-cert", result["certificate"])
    rules = operator.gc("compute", "firewall-rules", "list", "--format=value(name)").splitlines()
    if "tr-lightning-funding-invoices" not in rules:
        operator.gc("compute", "firewall-rules", "create", "tr-lightning-funding-invoices",
                    "--network=tr-lightning", "--direction=INGRESS", "--priority=900",
                    "--source-ranges=10.92.1.0/26", "--target-tags=tr-lightning", "--allow=tcp:8080", "--enable-logging")
    print("Invoice-only credential verified; wallet RPC denied; private subnet access configured")


if __name__ == "__main__":
    main()
