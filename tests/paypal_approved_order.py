"""PayPal's merchant-side GET response, distinct from the POST capture result."""
from typing import Any

import pytest

from trusted_router.config import Settings
from trusted_router.services import paypal_billing


def approved_order(order_id: str, workspace: str) -> dict[str, Any]:
    return {
        "id": order_id, "status": "APPROVED",
        "payment_source": {"paypal": {"account_status": "VERIFIED", "email_address": "buyer@example.com"}},
        "purchase_units": [{"reference_id": workspace, "custom_id": f"tr1|{workspace}|2500|2606",
                            "amount": {"currency_code": "USD", "value": "26.06"}}],
    }


def mock_approved_order(monkeypatch: pytest.MonkeyPatch, workspace: str) -> None:
    def lookup(_settings: Settings, path: str, *, order_lookup: bool) -> dict[str, Any]:
        assert order_lookup and path.startswith("/v2/checkout/orders/")
        return approved_order(path.rsplit("/", 1)[1], workspace)

    monkeypatch.setattr(paypal_billing, "_paypal_get", lookup)
