"""Deploy the isolated, payment-disabled Lightning website from committed code.

Own HTTPS load balancer, no TR URL-map edits, no wallet/cloud data permissions.
This intentionally cannot activate mainnet payments or deploy a prompt proxy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

PROJECT = "quill-cloud-proxy"
REGION = "us-central1"
NAME = "lightning-router-web"
DOMAIN = "lightningrouter.ai"
GIT = shutil.which("git") or "/usr/bin/git"
GCLOUD = shutil.which("gcloud") or "/usr/local/bin/gcloud"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    if args.account == "tr-ops-local@quill-cloud-proxy.iam.gserviceaccount.com":
        parser.error("Use a separately authenticated deployment identity")
    root = Path(__file__).resolve().parents[2]
    dirty = subprocess.check_output([GIT, "status", "--porcelain"], cwd=root, text=True)  # noqa: S603 - fixed operator tool
    if dirty:
        raise SystemExit("Refusing deployment from a dirty worktree")
    revision = subprocess.check_output([GIT, "rev-parse", "HEAD"], cwd=root, text=True).strip()  # noqa: S603 - fixed operator tool

    def gc(*command: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        import os
        env = dict(os.environ)
        env.pop("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", None)
        return subprocess.run([  # noqa: S603 - structured operator-selected arguments, no shell
            GCLOUD, *command, "--project=" + PROJECT, "--account=" + args.account, "--quiet",
        ], text=True, capture_output=capture, env=env, check=not capture)

    def ensure(describe: list[str], create: list[str]) -> dict[str, Any]:
        result = gc(*describe, "--format=json", capture=True)
        if result.returncode:
            if not any(marker in result.stderr for marker in ("was not found", "NOT_FOUND", "does not exist")):
                raise RuntimeError(result.stderr)
            gc(*create)
            result = gc(*describe, "--format=json", capture=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    identity = NAME + "@" + PROJECT + ".iam.gserviceaccount.com"
    ensure(["iam", "service-accounts", "describe", identity],
           ["iam", "service-accounts", "create", NAME, "--display-name=Lightning website (no data access)"])
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/trusted-router/{NAME}:{revision}"
    with tempfile.TemporaryDirectory(prefix="lr-release-") as temporary:
        archive = Path(temporary) / "source.tar"
        subprocess.run([GIT, "archive", "--format=tar", "--output=" + str(archive), revision,  # noqa: S603 - local committed Git object only
                        "experiments/lightning_router"], cwd=root, check=True)
        with tarfile.open(archive) as source:
            source.extractall(temporary, filter="data")
        directory = Path(temporary) / "experiments/lightning_router"
        gc("builds", "submit", str(directory), "--config=" + str(directory / "cloudbuild.yaml"),
           "--substitutions=_IMAGE=" + image)
    gc("run", "deploy", NAME, "--region=" + REGION, "--image=" + image,
       "--service-account=" + identity, "--memory=512Mi", "--cpu=1", "--concurrency=40",
       "--min-instances=0", "--max-instances=2", "--timeout=30s", "--port=8080",
       "--ingress=internal-and-cloud-load-balancing", "--allow-unauthenticated",
       "--set-env-vars=LR_PAYMENTS_ENABLED=false", "--labels=app=lightning-router,release=" + revision[:12])
    address = ensure(["compute", "addresses", "describe", NAME, "--global"],
                     ["compute", "addresses", "create", NAME, "--global", "--ip-version=IPV4"])
    ensure(["compute", "network-endpoint-groups", "describe", NAME, "--region=" + REGION],
           ["compute", "network-endpoint-groups", "create", NAME, "--region=" + REGION,
            "--network-endpoint-type=serverless", "--cloud-run-service=" + NAME])
    backend = ensure(["compute", "backend-services", "describe", NAME, "--global"],
                     ["compute", "backend-services", "create", NAME, "--global",
                      "--load-balancing-scheme=EXTERNAL_MANAGED"])
    if not backend.get("backends"):
        gc("compute", "backend-services", "add-backend", NAME, "--global",
           "--network-endpoint-group=" + NAME, "--network-endpoint-group-region=" + REGION)
    ensure(["compute", "url-maps", "describe", NAME, "--global"],
           ["compute", "url-maps", "create", NAME, "--default-service=" + NAME, "--global"])
    ensure(["compute", "ssl-certificates", "describe", NAME, "--global"],
           ["compute", "ssl-certificates", "create", NAME, "--domains=" + DOMAIN, "--global"])
    ensure(["compute", "ssl-policies", "describe", NAME, "--global"],
           ["compute", "ssl-policies", "create", NAME, "--min-tls-version=1.2", "--profile=MODERN", "--global"])
    ensure(["compute", "target-https-proxies", "describe", NAME, "--global"],
           ["compute", "target-https-proxies", "create", NAME, "--url-map=" + NAME,
            "--ssl-certificates=" + NAME, "--ssl-policy=" + NAME, "--global"])
    ensure(["compute", "forwarding-rules", "describe", NAME, "--global"],
           ["compute", "forwarding-rules", "create", NAME, "--global",
            "--load-balancing-scheme=EXTERNAL_MANAGED", "--address=" + NAME,
            "--target-https-proxy=" + NAME, "--ports=443"])
    record = gc("dns", "record-sets", "list", "--zone=lightningrouter-ai", "--name=" + DOMAIN + ".", "--type=A", "--format=json", capture=True)
    if record.returncode:
        raise RuntimeError(record.stderr)
    rows = json.loads(record.stdout)
    if rows and rows[0]["rrdatas"] != [address["address"]]:
        raise SystemExit("Unexpected existing A record; refusing to overwrite")
    if not rows:
        gc("dns", "record-sets", "create", DOMAIN + ".", "--zone=lightningrouter-ai",
           "--type=A", "--ttl=300", "--rrdatas=" + address["address"])
    print(json.dumps({"domain": DOMAIN, "ip": address["address"], "revision": revision,
                      "payments_enabled": False, "certificate": "Await managed certificate ACTIVE before claiming live"}))


if __name__ == "__main__":
    main()
