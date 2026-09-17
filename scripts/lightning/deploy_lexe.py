"""Staged, receive-only Lexe activation. Existing LND funds remain untouched."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from scripts.lightning.activate_web import REGION, WEB, Operator, deployment_commands

WALLET = "88d99e0760d44858c24fbec12366facacdf4a8caf7c3ae53fbf8b36c1672bea9"
SECRET_NAME = "lightning-router-lexe-receive-client"  # noqa: S105 - Secret Manager resource name, not a credential


def commands(image: str, suffix: str, *, backend: str) -> list[tuple[str, ...]]:
    migrations = deployment_commands(image)[:2]  # validates immutable repository/digest
    if backend not in {"lnd", "lexe"} or not re.fullmatch(r"lexe[a-z0-9-]{1,30}", suffix):
        raise ValueError("Invalid staged deployment")
    return [*migrations, (
        "run", "deploy", WEB, "--region=" + REGION, "--image=" + image,
        "--revision-suffix=" + suffix, "--no-traffic", "--memory=512Mi",
        "--update-env-vars=LR_LEXE_WALLET_ID=" + WALLET + ",LR_INVOICE_BACKEND=" + backend,
        "--update-secrets=/var/secrets/lexe/receive-client=" + SECRET_NAME + ":1",
    )]


def validate_baseline(service: dict[str, Any]) -> str:
    status = service["status"]
    if not any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])):
        raise ValueError("Current funding service is not ready")
    traffic = status.get("traffic", [])
    active = [item for item in traffic if item.get("percent", 0) > 0]
    if len(active) != 1 or active[0]["percent"] != 100:
        raise ValueError("Finish the existing rollout first")
    return str(active[0]["revisionName"])


def validate_activation(revisions: list[dict[str, Any]], image: str) -> None:
    # Revision workers can continue reconciliation even at zero traffic. Retire
    # all pre-Lexe workers before creating the first Lexe invoice, rather than
    # letting an old reconciler mistake its provider identity for an LND hash.
    for revision in revisions:
        container = revision["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        if env.get("LR_PAYMENTS_ENABLED") != "true":
            continue
        if env.get("LR_LEXE_WALLET_ID") != WALLET or container["image"] != image:
            raise ValueError("Retire pre-Lexe revisions before activation; rollback uses the dual-backend image")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--backend", choices=["lnd", "lexe"], default="lnd")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    steps = commands(args.image, args.suffix, backend=args.backend)
    if not args.apply:
        print(json.dumps({"apply": False, "commands": steps, "traffic_changed": False}))
        return
    op = Operator(args.account)
    service = json.loads(op.gc("run", "services", "describe", WEB, "--region=" + REGION, "--format=json"))
    previous = validate_baseline(service)
    if args.backend == "lexe":
        revisions = json.loads(op.gc("run", "revisions", "list", "--service=" + WEB, "--region=" + REGION, "--format=json"))
        validate_activation(revisions, args.image)
    for step in steps:
        op.gc(*step)
        print("Completed " + " ".join(step[:4]), flush=True)
    print(json.dumps({"staged_revision": WEB + "-" + args.suffix, "backend": args.backend,
                      "previous_revision": previous, "traffic_changed": False,
                      "next": "Verify revision Ready and dependency preflight, then explicitly promote and smoke."}))


if __name__ == "__main__":
    main()
