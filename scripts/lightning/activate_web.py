"""Activate the isolated Lightning funding service from committed source.

bootstrap provisions only this service's secret and database capabilities.
deploy requires an immutable image and runs schema migration separately.
No Bitcoin spending RPCs are present in this tool.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request
from typing import Any

PROJECT = "quill-cloud-proxy"
REGION = "us-central1"
INSTANCE = "lightning-router-funding"
CONNECTION = f"{PROJECT}:{REGION}:{INSTANCE}"
WEB = "lightning-router-web"
MIGRATOR = "lightning-router-migrate"
GCLOUD = shutil.which("gcloud") or "/usr/local/bin/gcloud"


class Operator:
    def __init__(self, account: str) -> None:
        if account == f"tr-ops-local@{PROJECT}.iam.gserviceaccount.com":
            raise ValueError("Use a separate deployment identity")
        self.account = account
        self.env = dict(os.environ)
        self.env.pop("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", None)

    def gc(self, *args: str, data: str | None = None) -> str:
        result = subprocess.run(  # noqa: S603 - fixed executable and structured operator arguments
            [GCLOUD, *args, "--project=" + PROJECT, "--account=" + self.account, "--quiet"],
            input=data, text=True, capture_output=True, env=self.env,
        )
        if result.returncode:
            # Never print secret input or a command's full diagnostic body.
            raise RuntimeError(f"gcloud {' '.join(args[:3])} failed ({result.returncode})")
        return result.stdout

    def secret(self, name: str, value: str | None = None, *, rotate: bool = False) -> str:
        if rotate and name not in {"lightning-router-lnd-invoice-macaroon", "lightning-router-lnd-tls-cert"}:
            raise ValueError("Only node transport credentials support explicit rotation")
        names = self.gc("secrets", "list", "--format=value(name)").splitlines()
        if name not in names:
            self.gc("secrets", "create", name, "--replication-policy=automatic")
            self.gc("secrets", "versions", "add", name, "--data-file=-", data=value or secrets.token_hex(32))
        payload = json.loads(self.gc("secrets", "versions", "access", "latest", "--secret=" + name, "--format=json"))
        current = base64.urlsafe_b64decode(payload["payload"]["data"]).decode()
        if value is not None and not hmac.compare_digest(current, value):
            if not rotate:
                raise ValueError("Secret differs; explicit node credential rotation required")
            self.gc("secrets", "versions", "add", name, "--data-file=-", data=value)
            return value
        return current

    def api(self, path: str, body: dict[str, Any], *, method: str = "POST") -> None:
        if not path.startswith(f"https://sqladmin.googleapis.com/sql/v1beta4/projects/{PROJECT}/"):
            raise ValueError("Unexpected provisioning API")
        token = self.gc("auth", "print-access-token").strip()
        request = urllib.request.Request(path, data=json.dumps(body).encode(), method=method,  # noqa: S310 - fixed HTTPS API allowlist above
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS API allowlist above
                response.read()
        except Exception as exc:
            raise RuntimeError("Cloud SQL credential provisioning failed") from exc

    def grant_secret(self, name: str, identity: str) -> None:
        self.gc("secrets", "add-iam-policy-binding", name,
                "--member=serviceAccount:" + identity, "--role=roles/secretmanager.secretAccessor")


def identity(name: str) -> str:
    return f"{name}@{PROJECT}.iam.gserviceaccount.com"


def bootstrap(operator: Operator) -> None:
    instance = json.loads(operator.gc("sql", "instances", "describe", INSTANCE, "--format=json"))
    if instance.get("state") != "RUNNABLE" or instance["settings"]["ipConfiguration"].get("authorizedNetworks"):
        raise RuntimeError("Database must be ready with no public client networks")
    if not instance["settings"]["backupConfiguration"].get("pointInTimeRecoveryEnabled"):
        raise RuntimeError("Database recovery must be enabled")
    accounts = operator.gc("iam", "service-accounts", "list", "--format=value(email)").splitlines()
    for name in (WEB, MIGRATOR):
        if identity(name) not in accounts:
            operator.gc("iam", "service-accounts", "create", name)
        operator.gc("projects", "add-iam-policy-binding", PROJECT,
                    "--member=serviceAccount:" + identity(name), "--role=roles/cloudsql.client",
                    f"--condition=title=lightning-funding-only,expression=resource.name == 'projects/{PROJECT}/instances/{INSTANCE}'")
    database_names = operator.gc("sql", "databases", "list", "--instance=" + INSTANCE, "--format=value(name)").splitlines()
    if "funding" not in database_names:
        operator.gc("sql", "databases", "create", "funding", "--instance=" + INSTANCE)
    admin_password = operator.secret("lightning-router-db-admin-password")
    app_password = operator.secret("lightning-router-db-app-password")
    operator.api(f"https://sqladmin.googleapis.com/sql/v1beta4/projects/{PROJECT}/instances/{INSTANCE}/users?name=postgres",
                 {"name": "postgres", "password": admin_password}, method="PUT")
    for name, user, password in (("admin", "postgres", admin_password), ("app", "lr_app", app_password)):
        operator.secret(f"lightning-router-db-{name}-url", f"postgresql+psycopg://{user}:{password}@/funding?host=/cloudsql/{CONNECTION}&connect_timeout=5")
    operator.secret("lightning-router-checkout-secret")
    operator.secret("trustedrouter-lightning-funding-token")
    for secret in ("lightning-router-db-app-url", "lightning-router-checkout-secret", "trustedrouter-lightning-funding-token",
                   "lightning-router-lnd-invoice-macaroon", "lightning-router-lnd-tls-cert"):
        operator.grant_secret(secret, identity(WEB))
    for secret in ("lightning-router-db-admin-url", "lightning-router-db-app-password"):
        operator.grant_secret(secret, identity(MIGRATOR))
    operator.grant_secret("trustedrouter-lightning-funding-token", identity("trusted-router-control-run"))
    print("Dedicated funding secrets and database permissions configured")


def deployment_commands(image: str) -> list[tuple[str, ...]]:
    if not re.fullmatch(f"{REGION}-docker.pkg.dev/{PROJECT}/trusted-router/{WEB}@sha256:[0-9a-f]{{64}}", image):
        raise ValueError("Deploy an immutable Lightning image digest")
    return [
        ("run", "jobs", "deploy", MIGRATOR, "--region=" + REGION, "--image=" + image,
         "--service-account=" + identity(MIGRATOR), "--set-cloudsql-instances=" + CONNECTION,
         "--set-secrets=LR_DATABASE_URL=lightning-router-db-admin-url:latest,LR_DATABASE_APP_PASSWORD=lightning-router-db-app-password:latest",
         "--command=python", "--args=-m,lightning_router.runtime", "--tasks=1", "--max-retries=0", "--task-timeout=120s"),
        ("run", "jobs", "execute", MIGRATOR, "--region=" + REGION, "--wait"),
        ("run", "deploy", WEB, "--region=" + REGION, "--image=" + image,
         "--service-account=" + identity(WEB), "--add-cloudsql-instances=" + CONNECTION,
         "--memory=512Mi", "--cpu=1", "--no-cpu-throttling", "--min-instances=1", "--max-instances=2",
         "--concurrency=20", "--timeout=60s", "--port=8080", "--ingress=internal-and-cloud-load-balancing",
         "--network=tr-lightning", "--subnet=tr-lightning-web-us-central1", "--vpc-egress=private-ranges-only",
         "--network-tags=lightning-funding-web", "--allow-unauthenticated",
         "--set-env-vars=LR_PAYMENTS_ENABLED=true,LR_CREDITS_ENDPOINT=https://trustedrouter.com,LR_LND_MACAROON_FILE=/var/secrets/lnd/macaroon,LR_LND_CERT_FILE=/var/secrets/lnd-cert/tls.pem",
         "--set-secrets=LR_DATABASE_URL=lightning-router-db-app-url:latest,LR_CHECKOUT_SECRET=lightning-router-checkout-secret:latest,LR_CREDITS_TOKEN=trustedrouter-lightning-funding-token:latest,/var/secrets/lnd/macaroon=lightning-router-lnd-invoice-macaroon:latest,/var/secrets/lnd-cert/tls.pem=lightning-router-lnd-tls-cert:latest"),
    ]


def edge_policy(operator: Operator) -> None:
    name = "lightning-router-funding"
    policies = operator.gc("compute", "security-policies", "list", "--format=value(name)").splitlines()
    if name not in policies:
        operator.gc("compute", "security-policies", "create", name, "--description=Lightning funding ingress limits")
    current = json.loads(operator.gc("compute", "security-policies", "describe", name, "--global", "--format=json"))
    # gcloud versions differ between an object and a singleton result list.
    if isinstance(current, list) and len(current) == 1:
        current = current[0]
    if not isinstance(current, dict) or current.get("name") != name or not isinstance(current.get("rules"), list):
        raise ValueError("Expected exactly one global funding policy")
    priorities = {rule["priority"] for rule in current["rules"]}
    for priority, expression, count, seconds in (
        (900, "request.method == 'POST' && (request.path == '/api/invoices' || "
              "(request.path == '/api/l402/funding' && "
              "(!has(request.headers['authorization']) || request.headers['authorization'] == '')))", 20, 900),
        (950, "request.path == '/api/account' || request.path == '/api/usage' || request.path == '/api/feedback' || request.path == '/api/l402/funding'", 20, 60),
        (1000, "true", 180, 60),
    ):
        action = "update" if priority in priorities else "create"
        operator.gc("compute", "security-policies", "rules", action, str(priority), "--security-policy=" + name,
                    "--expression=" + expression, "--action=throttle", "--conform-action=allow",
                    "--exceed-action=deny-429", "--enforce-on-key=IP",
                    "--rate-limit-threshold-count=" + str(count), "--rate-limit-threshold-interval-sec=" + str(seconds))
    operator.gc("compute", "backend-services", "update", WEB, "--global", "--security-policy=" + name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("bootstrap", "deploy"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--image", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    commands = deployment_commands(args.image) if args.stage == "deploy" else []
    if not args.apply:
        print(json.dumps({"stage": args.stage, "commands": commands, "apply": False}))
        return
    operator = Operator(args.account)
    if args.stage == "bootstrap":
        bootstrap(operator)
    else:
        edge_policy(operator)
        for command in commands:
            operator.gc(*command)
            print("Completed " + " ".join(command[:4]))


if __name__ == "__main__":
    main()
