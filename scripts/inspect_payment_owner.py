"""Read payment ownership without scanning the production ledger.

Stripe is queried by PaymentIntent ID. Spanner is read by complete primary
keys only. This tool never credits, refunds, retries, or repairs a payment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

READ_TIMEOUT_SECONDS = 5.0
ALLOWED_ENTITY_KINDS = frozenset({"workspace", "user"})


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:@.+-]{1,256}", value):
        raise ValueError(f"Invalid {label}")
    return value


def stripe_payment_metadata(client: httpx.Client, payment_intent: str) -> dict[str, Any]:
    if not re.fullmatch(r"pi_[A-Za-z0-9]{1,240}", payment_intent):
        raise ValueError("Expected one Stripe PaymentIntent ID")
    response = client.get(f"https://api.stripe.com/v1/payment_intents/{payment_intent}")
    if response.status_code != 200:
        # Do not dump provider response bodies, request headers or card details.
        raise RuntimeError(f"Stripe lookup returned HTTP {response.status_code}")
    payment = response.json()
    if not isinstance(payment, dict) or payment.get("id") != payment_intent:
        raise ValueError("Stripe returned an unexpected payment")
    metadata = payment.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Stripe returned malformed payment metadata")
    return {
        "payment_intent": payment_intent,
        "payment_status": payment.get("status"),
        "workspace_id": metadata.get("workspace_id"),
        "initiating_user_id": metadata.get("initiating_user_id"),
    }


def read_entity(client: Any, session_name: str, kind: str, entity_id: str) -> dict[str, Any] | None:
    if kind not in ALLOWED_ENTITY_KINDS:
        raise ValueError("Entity kind is not allowed for payment ownership lookup")
    entity_id = _identifier(entity_id, label="entity ID")
    rows = client.read(
        request={
            "session": session_name,
            "table": "tr_entities",
            "columns": ["body"],
            "key_set": {"keys": [[kind, entity_id]]},
            "limit": 1,
            "transaction": {"single_use": {"read_only": {"strong": True}}},
            "request_options": {"priority": "PRIORITY_LOW", "request_tag": "tr_ops_payment_owner"},
        },
        retry=None,
        timeout=READ_TIMEOUT_SECONDS,
    ).rows
    if not rows:
        return None
    body = json.loads(rows[0][0])
    if not isinstance(body, dict):
        raise ValueError("Invalid entity metadata")
    return body


def resolve_owner(
    payment: dict[str, Any],
    reader: Callable[[str, str], dict[str, Any] | None],
    *,
    include_email: bool = False,
) -> dict[str, Any]:
    result = {
        "payment_intent": payment["payment_intent"],
        "payment_status": payment.get("payment_status"),
    }
    workspace_id = payment.get("workspace_id")
    if not workspace_id:
        return {**result, "attribution": "missing_workspace_metadata"}
    workspace_id = _identifier(workspace_id, label="workspace ID")
    workspace = reader("workspace", workspace_id)
    result["workspace_id"] = workspace_id
    if workspace is None:
        return {**result, "attribution": "workspace_not_found"}
    if workspace.get("federated_home"):
        return {**result, "attribution": "workspace_has_remote_home"}
    initiating_user_id = payment.get("initiating_user_id")
    owner_user_id = workspace.get("owner_user_id")
    user_id = initiating_user_id or owner_user_id
    if not user_id:
        return {**result, "attribution": "missing_user_metadata"}
    user_id = _identifier(user_id, label="user ID")
    user = reader("user", user_id)
    result["user_id"] = user_id
    if user is None:
        return {**result, "attribution": "user_not_found"}
    result["attribution"] = "initiating_user" if initiating_user_id else "current_workspace_owner"
    # The current owner is not proof of who owned the workspace when it paid.
    if include_email:
        result["email"] = user.get("email")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--payment-intent", required=True)
    parser.add_argument("--include-email", action="store_true")
    args = parser.parse_args(argv)
    credential_file = os.environ.get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE")
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if not credential_file or not stripe_key:
        parser.error("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE and STRIPE_SECRET_KEY are required")

    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        Path(credential_file), scopes=["https://www.googleapis.com/auth/spanner.data"]
    )
    print(
        json.dumps(
            {
                "event": "payment_owner_lookup_started",
                "operator_email": credentials.service_account_email,
            }
        ),
        file=sys.stderr,
    )
    try:
        with httpx.Client(
            headers={"Authorization": f"Bearer {stripe_key}"},
            timeout=READ_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        ) as client:
            payment = stripe_payment_metadata(client, args.payment_intent)
        # Missing Stripe metadata is terminal. Never try a body search to fill it.
        if payment.get("workspace_id"):
            # Use the bounded RPC client: no session pool, background metrics
            # exporter, or implicit session-creation retries for an ops lookup.
            with SpannerClient(credentials=credentials) as spanner_client:
                session = spanner_client.create_session(
                    request={
                        "database": f"projects/{args.project}/instances/{args.instance}/databases/{args.database}"
                    },
                    retry=None,
                    timeout=READ_TIMEOUT_SECONDS,
                )
                try:
                    result = resolve_owner(
                        payment,
                        lambda kind, entity_id: read_entity(
                            spanner_client, session.name, kind, entity_id
                        ),
                        include_email=args.include_email,
                    )
                finally:
                    spanner_client.delete_session(
                        request={"name": session.name},
                        retry=None,
                        timeout=READ_TIMEOUT_SECONDS,
                    )
        else:
            result = {**payment, "attribution": "missing_workspace_metadata"}
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(
            json.dumps({"event": "payment_owner_lookup_failed", "error_type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"event": "payment_owner_lookup_completed"}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
