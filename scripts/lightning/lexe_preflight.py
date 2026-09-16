"""Read-only checks for a local, receive-only Lexe sidecar. Never moves funds."""

from __future__ import annotations

import argparse
import json
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

SCOPES = {"read_info", "read_payments", "receive"}
# Pinned to the reviewed Lexe scope expansion. Unknown additions require review,
# even when their parent scope keeps the same name.
PERMISSIONS = {
    "node_info", "list_channels", "get_human_bitcoin_address",
    "get_payments_by_indexes", "get_new_payments", "get_updated_payments",
    "get_payment_by_id", "list_broadcasted_txs", "get_next_unused_address",
    "create_invoice", "create_offer", "resync", "cancel_payment",
}
REQUIRED_PERMISSIONS = {"node_info", "list_channels", "get_payment_by_id", "create_invoice", "cancel_payment"}


class PreflightError(ValueError):
    """Static error codes only; never include upstream response bodies."""


def sidecar_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.port is None):
        raise PreflightError("loopback_sidecar_required")
    return value.rstrip("/")


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PreflightError("invalid_permission_response")
    return set(value)


def check_credentials(data: dict[str, Any], now_ms: int) -> None:
    if (data.get("kind") != "client_credentials"
            or _strings(data.get("scopes")) != SCOPES
            or _strings(data.get("permissions", []))):
        raise PreflightError("receive_only_credentials_required")
    effective = _strings(data.get("effective_permissions"))
    if not REQUIRED_PERMISSIONS <= effective or not effective <= PERMISSIONS:
        raise PreflightError("unexpected_effective_permissions")
    if "expires_at" not in data:
        raise PreflightError("invalid_credential_expiry")
    expiry = data["expires_at"]
    if expiry is not None and (type(expiry) is not int or expiry <= now_ms + 3_600_000):
        raise PreflightError("credential_expired_or_expiring")


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    # No redirects, retries, mutation endpoints, credentials, or payment lists.
    with client.stream("GET", path, follow_redirects=False) as response:
        if response.status_code != 200:
            raise PreflightError("sidecar_read_failed")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 1_048_576:
                raise PreflightError("sidecar_response_too_large")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise PreflightError("invalid_sidecar_response")
    return data


def inspect_sidecar(client: httpx.Client, expected_user_pk: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_user_pk):
        raise PreflightError("expected_wallet_public_key_required")
    if _get(client, "/v2/health").get("status") != "ok":
        raise PreflightError("sidecar_not_ready")
    check_credentials(_get(client, "/v2/node/client_info"), int(time.time() * 1000))
    info = _get(client, "/v2/node/node_info")
    if info.get("user_pk") != expected_user_pk:
        raise PreflightError("wrong_wallet")
    version, measurement = info.get("version"), info.get("measurement")
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", version)
            or not isinstance(measurement, str) or not re.fullmatch(r"[0-9a-f]{64}", measurement)):
        raise PreflightError("invalid_node_identity")
    channels = _get(client, "/v2/node/list_channels").get("channels")
    if not isinstance(channels, list):
        raise PreflightError("invalid_channels")
    usable, inbound_msat = 0, 0
    for channel in channels:
        if not isinstance(channel, dict) or type(channel.get("is_usable")) is not bool:
            raise PreflightError("invalid_channels")
        amount = channel.get("inbound_capacity")
        if not isinstance(amount, str) or not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,3})?", amount):
            raise PreflightError("invalid_inbound_capacity")
        if channel["is_usable"]:
            usable += 1
            inbound_msat += int(Decimal(amount) * 1000)
    # Zero existing liquidity is valid with JIT, but is not proof of receipt.
    # Measurement is reported, not verified here; attestation belongs to SDK.
    return {"status": "configuration_checked", "node_version": version,
            "reported_measurement": measurement, "usable_channels": usable,
            "existing_inbound_msat": str(inbound_msat), "payment_verified": False,
            "production_cutover_allowed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar-url", default="http://127.0.0.1:5393")
    parser.add_argument("--expected-user-pk", required=True)
    args = parser.parse_args()
    try:
        url = sidecar_url(args.sidecar_url)
        with httpx.Client(base_url=url, timeout=15, trust_env=False, follow_redirects=False) as client:
            result = inspect_sidecar(client, args.expected_user_pk)
    except PreflightError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    except (httpx.HTTPError, ValueError):
        print(json.dumps({"status": "blocked", "reason": "sidecar_unavailable_or_invalid"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
