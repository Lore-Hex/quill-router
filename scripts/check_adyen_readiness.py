#!/usr/bin/env python3
"""Run non-charging Adyen test-account readiness checks.

The script never creates a payment or session. It validates the API credential,
merchant state, browser origin, and payment-method discovery without printing
credentials.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


def main() -> int:
    api_key = os.environ.get("ADYEN_API_KEY") or os.environ.get("TR_ADYEN_API_KEY")
    merchant = os.environ.get("ADYEN_MERCHANT_ACCOUNT") or os.environ.get(
        "TR_ADYEN_MERCHANT_ACCOUNT"
    )
    hmac_key = os.environ.get("ADYEN_HMAC_KEY") or os.environ.get("TR_ADYEN_HMAC_KEY")
    reference_key = os.environ.get("ADYEN_REFERENCE_KEY") or os.environ.get(
        "TR_ADYEN_REFERENCE_KEY"
    )
    expected_origin = os.environ.get("ADYEN_ALLOWED_ORIGIN", "https://trustedrouter.com")
    expected_webhook = os.environ.get(
        "ADYEN_WEBHOOK_URL",
        "https://trustedrouter.com/v1/internal/adyen/webhook",
    )
    if not api_key or not merchant:
        print(
            "Set ADYEN_API_KEY and ADYEN_MERCHANT_ACCOUNT before running this check.",
            file=sys.stderr,
        )
        return 2

    checks: dict[str, dict[str, Any]] = {}
    checks["local_secrets"] = {
        "ok": _valid_hmac_key(hmac_key) and bool(reference_key and len(reference_key) >= 32),
        "hmac_configured": _valid_hmac_key(hmac_key),
        "reference_key_configured": bool(reference_key and len(reference_key) >= 32),
    }
    headers = {"X-API-Key": api_key}
    with httpx.Client(timeout=15.0, headers=headers) as client:
        credential = _request(client, "GET", "https://management-test.adyen.com/v3/me")
        checks["credential"] = {
            "ok": credential.status_code == 200,
            "status": credential.status_code,
        }

        origins = _request(
            client,
            "GET",
            "https://management-test.adyen.com/v3/me/allowedOrigins",
        )
        origin_values = _origin_values(_json_object(origins))
        checks["allowed_origin"] = {
            "ok": origins.status_code == 200 and expected_origin in origin_values,
            "status": origins.status_code,
            "expected": expected_origin,
        }

        merchant_response = _request(
            client,
            "GET",
            f"https://management-test.adyen.com/v3/merchants/{merchant}",
        )
        merchant_body = _json_object(merchant_response)
        merchant_status = str(merchant_body.get("status") or "unknown")
        checks["merchant"] = {
            "ok": merchant_response.status_code == 200 and merchant_status.lower() == "active",
            "status": merchant_response.status_code,
            "merchant_status": merchant_status,
        }

        webhooks = _request(
            client,
            "GET",
            f"https://management-test.adyen.com/v3/merchants/{merchant}/webhooks",
        )
        webhook_values = _webhook_values(_json_object(webhooks))
        checks["standard_webhook"] = {
            "ok": webhooks.status_code == 200
            and any(
                item.get("url") == expected_webhook
                and item.get("type") == "standard"
                and item.get("active") is True
                for item in webhook_values
            ),
            "status": webhooks.status_code,
            "expected": expected_webhook,
            "matching_count": sum(
                1 for item in webhook_values if item.get("url") == expected_webhook
            ),
        }

        payment_methods = _request(
            client,
            "POST",
            "https://checkout-test.adyen.com/v72/paymentMethods",
            json={
                "amount": {"currency": "USD", "value": 100},
                "channel": "Web",
                "countryCode": "US",
                "merchantAccount": merchant,
            },
        )
        payment_body = _json_object(payment_methods)
        methods = payment_body.get("paymentMethods")
        checks["payment_methods"] = {
            "ok": payment_methods.status_code == 200
            and isinstance(methods, list)
            and bool(methods),
            "status": payment_methods.status_code,
            "count": len(methods) if isinstance(methods, list) else 0,
            "error_code": payment_body.get("errorCode"),
            "message": payment_body.get("message"),
        }

    ready = all(bool(check["ok"]) for check in checks.values())
    print(json.dumps({"ready": ready, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ready else 1


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        print(f"Adyen readiness request failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _origin_values(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("data")
    if not isinstance(raw, list):
        return set()
    origins: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("domain"), str):
            origins.add(entry["domain"])
    return origins


def _webhook_values(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _valid_hmac_key(value: str | None) -> bool:
    if not value or len(value) < 32 or len(value) % 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
