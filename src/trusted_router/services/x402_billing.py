from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal
from typing import Any, cast

import stripe

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import (
    MICRODOLLARS_PER_CENT,
    dollars_to_cents,
    dollars_to_microdollars,
    microdollars_to_decimal,
)
from trusted_router.schemas import X402FundingRequest
from trusted_router.storage import STORE
from trusted_router.types import ErrorType

X402_HEADER = "payment-required"
X402_PAYMENT_METHOD = "x402"
X402_EVENT_PREFIX = "x402:"

log = logging.getLogger(__name__)


def create_x402_funding_challenge(
    *,
    body: X402FundingRequest,
    workspace_id: str,
    settings: Settings,
) -> dict[str, Any]:
    _require_x402_enabled(settings)
    amount_microdollars = dollars_to_microdollars(body.amount)
    payment_intent = _create_payment_intent(
        amount=body.amount,
        amount_microdollars=amount_microdollars,
        workspace_id=workspace_id,
        settings=settings,
    )
    payment_intent_id = str(payment_intent.get("id") or "")
    pay_to = _extract_pay_to_address(payment_intent, settings.x402_network)
    payload = _payment_required_payload(
        amount_microdollars=amount_microdollars,
        payment_intent_id=payment_intent_id,
        workspace_id=workspace_id,
        pay_to=pay_to,
        settings=settings,
    )
    encoded = _base64_json(payload)
    return {
        "payment_intent_id": payment_intent_id,
        "payment_required": payload,
        "payment_required_header": encoded,
        "pay_to": pay_to,
        "network": settings.x402_network_id,
        "asset": "USDC",
        "amount_decimal": microdollars_to_decimal(amount_microdollars),
        "amount_microdollars": amount_microdollars,
    }


def settle_x402_payment(
    *,
    payment_intent_id: str,
    workspace_id: str,
    settings: Settings,
) -> dict[str, Any]:
    _require_x402_enabled(settings)
    payment_intent = retrieve_x402_payment_intent(payment_intent_id, settings)
    return credit_x402_payment_intent(
        payment_intent,
        expected_workspace_id=workspace_id,
        settings=settings,
        hide_cross_workspace=True,
    )


def retrieve_x402_payment_intent(payment_intent_id: str, settings: Settings) -> dict[str, Any]:
    if not settings.stripe_secret_key and _mock_payments_enabled(settings):
        if not payment_intent_id.startswith("pi_x402_mock_"):
            raise api_error(400, "Mock x402 settle requires a mock payment_intent_id", ErrorType.BAD_REQUEST)
        amount_microdollars = dollars_to_microdollars("10")
        return {
            "id": payment_intent_id,
            "status": "succeeded",
            "currency": "usd",
            "amount": amount_microdollars // MICRODOLLARS_PER_CENT,
            "amount_received": amount_microdollars // MICRODOLLARS_PER_CENT,
            "metadata": {
                "workspace_id": workspace_id_from_mock_payment_intent(payment_intent_id),
                "amount_microdollars": str(amount_microdollars),
                "payment_method": X402_PAYMENT_METHOD,
                "purpose": "trustedrouter_credits",
                "asset": "USDC",
                "network": settings.x402_network,
            },
            "next_action": {
                "crypto_display_details": {
                    "deposit_addresses": {
                        settings.x402_network: {
                            "address": "0x0000000000000000000000000000000000000402",
                            "supported_tokens": [{"token_currency": "usdc"}],
                        }
                    }
                }
            },
        }
    if not settings.stripe_secret_key:
        raise api_error(503, "Stripe x402 payment is not configured", ErrorType.SERVICE_UNAVAILABLE)
    stripe.api_key = settings.stripe_secret_key
    try:
        return _stripe_object_to_dict(
            cast(Any, stripe.PaymentIntent).retrieve(
                payment_intent_id,
                stripe_version=settings.x402_stripe_api_version,
            )
        )
    except Exception as exc:
        raise api_error(503, "Could not verify x402 payment", ErrorType.SERVICE_UNAVAILABLE) from exc


def credit_x402_payment_intent(
    payment_intent: dict[str, Any],
    *,
    expected_workspace_id: str | None,
    settings: Settings,
    hide_cross_workspace: bool = False,
) -> dict[str, Any]:
    metadata = _dict_value(payment_intent.get("metadata"))
    if metadata.get("payment_method") != X402_PAYMENT_METHOD:
        raise api_error(400, "PaymentIntent is not a TrustedRouter x402 payment", ErrorType.BAD_REQUEST)
    workspace_id = str(metadata.get("workspace_id") or "")
    if (
        not workspace_id
        or STORE.get_credit_account(workspace_id) is None
        or (expected_workspace_id is not None and workspace_id != expected_workspace_id)
    ):
        if hide_cross_workspace:
            raise api_error(404, "PaymentIntent not found", ErrorType.NOT_FOUND)
        raise api_error(404, "Workspace not found", ErrorType.NOT_FOUND)
    requested_microdollars = _metadata_requested_microdollars(metadata)
    _validate_payment_intent_currency(payment_intent)
    _validate_payment_intent_crypto_details(payment_intent, settings)
    status = str(payment_intent.get("status") or "")
    settled_microdollars = _settled_amount_microdollars(payment_intent)
    if status != "succeeded":
        return _x402_settle_payload(
            status=status or "pending",
            payment_intent_id=str(payment_intent.get("id") or ""),
            workspace_id=workspace_id,
            amount_microdollars=0,
            credited=False,
            requested_microdollars=requested_microdollars,
            settled_microdollars=settled_microdollars,
        )
    amount_microdollars = min(settled_microdollars, requested_microdollars)
    if amount_microdollars <= 0:
        raise api_error(400, "PaymentIntent has no settled amount", ErrorType.BAD_REQUEST)
    payment_intent_id = str(payment_intent.get("id") or "")
    if not payment_intent_id:
        raise api_error(400, "PaymentIntent is missing an id", ErrorType.BAD_REQUEST)
    credited = STORE.credit_workspace_once(
        workspace_id,
        amount_microdollars,
        x402_event_id(payment_intent_id),
    )
    return _x402_settle_payload(
        status=status,
        payment_intent_id=payment_intent_id,
        workspace_id=workspace_id,
        amount_microdollars=amount_microdollars,
        credited=credited,
        requested_microdollars=requested_microdollars,
        settled_microdollars=settled_microdollars,
    )


def x402_payment_required_response_body(challenge: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {
            "code": 402,
            "message": "Stablecoin payment required to add TrustedRouter credits",
            "type": ErrorType.INSUFFICIENT_CREDITS.value,
        },
        "data": {
            "payment_protocol": "x402",
            "provider": "stripe",
            **challenge,
        },
    }


def x402_event_id(payment_intent_id: str) -> str:
    return f"{X402_EVENT_PREFIX}{payment_intent_id}"


def workspace_id_from_mock_payment_intent(payment_intent_id: str) -> str:
    # Local tests monkeypatch retrieve for real workspace binding. This fallback
    # exists only for manual mock demos; it deliberately cannot target a real
    # workspace without a monkeypatch.
    return payment_intent_id.removeprefix("pi_x402_mock_").rsplit("_", 1)[0]


def _create_payment_intent(
    *,
    amount: Decimal | str | int,
    amount_microdollars: int,
    workspace_id: str,
    settings: Settings,
) -> dict[str, Any]:
    amount_cents = dollars_to_cents(amount)
    if not settings.stripe_secret_key and _mock_payments_enabled(settings):
        payment_intent_id = f"pi_x402_mock_{workspace_id}_000000"
        return {
            "id": payment_intent_id,
            "status": "requires_action",
            "currency": "usd",
            "amount": amount_cents,
            "metadata": {
                "workspace_id": workspace_id,
                "amount_microdollars": str(amount_microdollars),
                "payment_method": X402_PAYMENT_METHOD,
                "purpose": "trustedrouter_credits",
                "asset": "USDC",
                "network": settings.x402_network,
            },
            "next_action": {
                "crypto_display_details": {
                    "deposit_addresses": {
                        settings.x402_network: {
                            "address": "0x0000000000000000000000000000000000000402",
                            "supported_tokens": [{"token_currency": "usdc"}],
                        }
                    }
                }
            },
        }
    if not settings.stripe_secret_key:
        raise api_error(503, "Stripe x402 payment is not configured", ErrorType.SERVICE_UNAVAILABLE)
    stripe.api_key = settings.stripe_secret_key
    params = {
        "amount": amount_cents,
        "currency": "usd",
        "payment_method_types": ["crypto"],
        "payment_method_data": {"type": "crypto"},
        "payment_method_options": {
            "crypto": {
                "mode": "deposit",
                "deposit_options": {"networks": [settings.x402_network]},
            }
        },
        "confirm": True,
        "metadata": {
            "workspace_id": workspace_id,
            "amount_microdollars": str(amount_microdollars),
            "payment_method": X402_PAYMENT_METHOD,
            "purpose": "trustedrouter_credits",
            "asset": "USDC",
            "network": settings.x402_network,
        },
    }
    try:
        return _stripe_object_to_dict(
            cast(Any, stripe.PaymentIntent).create(
                **params,
                stripe_version=settings.x402_stripe_api_version,
            )
        )
    except Exception as exc:
        raise api_error(503, "Could not create Stripe x402 payment", ErrorType.SERVICE_UNAVAILABLE) from exc


def _payment_required_payload(
    *,
    amount_microdollars: int,
    payment_intent_id: str,
    workspace_id: str,
    pay_to: str,
    settings: Settings,
) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "error": "payment_required",
        "accepts": [
            {
                "scheme": "exact",
                "price": f"${microdollars_to_decimal(amount_microdollars)}",
                "network": settings.x402_network_id,
                "payTo": pay_to,
                "asset": "USDC",
                "maxAmountRequired": str(amount_microdollars),
                "resource": settings.api_base_url.rstrip("/") + "/billing/x402/settle",
                "description": "TrustedRouter prepaid credits",
                "mimeType": "application/json",
            }
        ],
        "metadata": {
            "provider": "stripe",
            "payment_intent_id": payment_intent_id,
            "workspace_id": workspace_id,
            "amount_microdollars": str(amount_microdollars),
        },
    }


def _extract_pay_to_address(payment_intent: dict[str, Any], network: str) -> str:
    details = _crypto_display_details(payment_intent)
    network_details = _dict_value(_dict_value(details.get("deposit_addresses")).get(network))
    address = network_details.get("address")
    if not isinstance(address, str) or not address.strip():
        raise api_error(
            503,
            "Stripe x402 payment intent did not include a deposit address",
            ErrorType.SERVICE_UNAVAILABLE,
        )
    return address.strip()


def _validate_payment_intent_currency(payment_intent: dict[str, Any]) -> None:
    if str(payment_intent.get("currency") or "").lower() != "usd":
        raise api_error(400, "x402 payment currency must be usd", ErrorType.BAD_REQUEST)


def _validate_payment_intent_crypto_details(
    payment_intent: dict[str, Any],
    settings: Settings,
) -> None:
    metadata = _dict_value(payment_intent.get("metadata"))
    if str(metadata.get("asset") or "").upper() != "USDC":
        raise api_error(400, "x402 payment asset must be USDC", ErrorType.BAD_REQUEST)
    if str(metadata.get("network") or "") != settings.x402_network:
        raise api_error(400, "x402 payment network is not accepted", ErrorType.BAD_REQUEST)
    details = _crypto_display_details(payment_intent)
    if not details:
        # Stripe may omit next_action after success; metadata carries the
        # asset/network contract we stamped at create time.
        return
    addresses = _dict_value(details.get("deposit_addresses"))
    network_details = _dict_value(addresses.get(settings.x402_network))
    if addresses and not network_details:
        raise api_error(400, "x402 payment network is not accepted", ErrorType.BAD_REQUEST)
    tokens = network_details.get("supported_tokens")
    if isinstance(tokens, list) and tokens:
        currencies = {
            str(_dict_value(token).get("token_currency") or "").lower()
            for token in tokens
            if isinstance(token, dict)
        }
        if "usdc" not in currencies:
            raise api_error(400, "x402 payment asset must be USDC", ErrorType.BAD_REQUEST)


def _crypto_display_details(payment_intent: dict[str, Any]) -> dict[str, Any]:
    return _dict_value(_dict_value(payment_intent.get("next_action")).get("crypto_display_details"))


def _metadata_requested_microdollars(metadata: dict[str, Any]) -> int:
    raw = metadata.get("amount_microdollars")
    if not isinstance(raw, str) or not raw.isdigit():
        raise api_error(400, "PaymentIntent is missing TrustedRouter amount metadata", ErrorType.BAD_REQUEST)
    return int(raw)


def _settled_amount_microdollars(payment_intent: dict[str, Any]) -> int:
    raw_amount = payment_intent.get("amount_received")
    if raw_amount is None:
        raw_amount = payment_intent.get("amount")
    amount_cents = int(raw_amount or 0)
    return amount_cents * MICRODOLLARS_PER_CENT


def _x402_settle_payload(
    *,
    status: str,
    payment_intent_id: str,
    workspace_id: str,
    amount_microdollars: int,
    credited: bool,
    requested_microdollars: int,
    settled_microdollars: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "payment_intent_id": payment_intent_id,
        "workspace_id": workspace_id,
        "credited": credited,
        "amount_decimal": microdollars_to_decimal(amount_microdollars),
        "amount_microdollars": amount_microdollars,
        "requested_amount_microdollars": requested_microdollars,
        "settled_amount_microdollars": settled_microdollars,
    }


def validate_x402_funding_amount(amount: Decimal | str | int, settings: Settings) -> None:
    amount_microdollars = dollars_to_microdollars(amount)
    if amount_microdollars % MICRODOLLARS_PER_CENT != 0:
        raise api_error(400, "x402 funding amount must be exactly representable in cents", ErrorType.BAD_REQUEST)
    max_microdollars = dollars_to_microdollars(settings.x402_max_fund_dollars)
    if amount_microdollars > max_microdollars:
        raise api_error(400, "x402 funding amount exceeds the workspace cap", ErrorType.BAD_REQUEST)


def _require_x402_enabled(settings: Settings) -> None:
    if not settings.x402_enabled:
        raise api_error(404, "route not found", ErrorType.NOT_FOUND)


def _base64_json(payload: dict[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _stripe_object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "_to_dict_recursive", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, dict):
            return cast(dict[str, Any], converted)
    return cast(dict[str, Any], dict(value))


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mock_payments_enabled(settings: Settings) -> bool:
    return settings.x402_allow_mock_payments and settings.environment.lower() in {"local", "test"}
