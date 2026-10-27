from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.content import token_index
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.routes import public
from trusted_router.services import reserved_tokens as reserve
from trusted_router.storage import InMemoryStore

HEADERS = {"Origin": "https://nytokenexchange.com"}
PATH = "/token-exchange/reserve/checkout"


@pytest.fixture
def settings():
    return Settings(environment="test", reserved_tokens_checkout_enabled=True,
                    reserved_tokens_discount_bps=1234, stripe_secret_key="sk_test_reserve")  # noqa: S106


@pytest.fixture
def checkout(settings, monkeypatch):
    public._INQUIRY_HITS.clear()
    public._INQUIRY_GLOBAL_HITS.clear()
    app = FastAPI()
    public.register_public_action_routes(app, settings)
    create = Mock(return_value={"url": "https://checkout.stripe.com/c/pay/test"})
    monkeypatch.setattr(reserve.stripe.checkout.Session, "create", create)
    return TestClient(app), create


@pytest.mark.parametrize("grade,value", [("AAA", 1794), ("A", 863), ("B", 187), ("C", 27)])
def test_price_each_grade(settings, monkeypatch, grade, value):
    monkeypatch.setitem(token_index.QUOTES, grade, {"value": value})
    quote = reserve.reservation_quote(settings, grade, 3)
    expected = int((Decimal(value) * 100 * Decimal("0.8766")).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert quote["per_lot_cents"] == expected
    assert quote["total_cents"] == expected * 3
    assert quote["list_price_cents"] == value * 100
    assert quote["index_as_of"] == token_index.AS_OF_ISO


def test_round_half_up_per_lot_before_multiplication(settings, monkeypatch):
    monkeypatch.setitem(token_index.QUOTES, "C", {"value": 27})
    settings.reserved_tokens_discount_bps = 50
    quote = reserve.reservation_quote(settings, "C", 3)
    assert quote["per_lot_cents"] == 2687  # 2686.5 cents
    assert quote["total_cents"] == 8061


@pytest.mark.parametrize("field,value", [("reserved_tokens_checkout_enabled", False), ("reserved_tokens_discount_bps", None), ("stripe_secret_key", None)])
def test_unconfigured_refuses(settings, checkout, field, value):
    client, create = checkout
    setattr(settings, field, value)
    response = client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["message"]
    assert response.headers["access-control-allow-origin"] == HEADERS["Origin"]
    create.assert_not_called()


@pytest.mark.parametrize("patch", [{"grade": "ALL"}, {"grade": "model"}, {"lots": 0}, {"lots": 101}, {"lots": -1}, {"lots": 1.5}, {"lots": True}, {"lots": "1"}, {"terms_accepted": False}, {"terms_accepted": "true"}, {"price": 1}])
def test_invalid_body(checkout, patch):
    client, create = checkout
    response = client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True, **patch}, headers=HEADERS)
    assert response.status_code == 422
    create.assert_not_called()


def test_missing_terms(checkout):
    client, create = checkout
    assert client.post(PATH, json={"grade": "A", "lots": 1}, headers=HEADERS).status_code == 422
    create.assert_not_called()


def test_stripe_parameters_and_quote(checkout, settings, monkeypatch):
    monkeypatch.setitem(token_index.QUOTES, "B", {"value": 187})
    client, create = checkout
    quote = client.get("/token-exchange/reserve/quote?grade=B&lots=2", headers=HEADERS)
    assert quote.status_code == 200
    assert quote.headers["cache-control"] == "private, no-store"
    response = client.post(PATH, json={"grade": "B", "lots": 2, "terms_accepted": True}, headers=HEADERS)
    assert response.json() == {"url": "https://checkout.stripe.com/c/pay/test"}
    args = create.call_args.kwargs
    assert args["api_key"] == settings.stripe_secret_key
    assert args["mode"] == "payment"
    assert args["payment_method_types"] == ["card"]
    assert args["customer_creation"] == "always"
    assert not {"customer_email", "customer", "allow_promotion_codes", "automatic_tax"} & args.keys()
    assert args["success_url"] == "https://nytokenexchange.com/buy?reserved=success"
    assert args["cancel_url"] == "https://nytokenexchange.com/buy?step=2&grade=B&unit=blocks"
    assert args["metadata"] == {
        "kind": reserve.KIND, "grade": "B", "lots": "2", "per_lot_cents": "16392",
        "list_price_cents": "18700", "discount_bps": "1234", "index_as_of": token_index.AS_OF_ISO,
        **reserve.WINDOW,
    }
    assert args["payment_intent_data"] == {"metadata": args["metadata"]}
    assert args["line_items"] == [{"quantity": 2, "price_data": {
        "currency": "usd", "unit_amount": quote.json()["per_lot_cents"], "product_data": {
            "name": "Reserved tokens · Grade B · 1B tokens",
            "description": "Usable January 1, 2027 through December 31, 2027. Unused tokens expire December 31, 2027. Full refund on request before January 1, 2027.",
        },
    }}]


def test_custom_limit_and_upper_boundary(checkout, settings):
    client, _ = checkout
    settings.reserved_tokens_max_lots_per_checkout = 2
    assert client.post(PATH, json={"grade": "C", "lots": 2, "terms_accepted": True}, headers=HEADERS).status_code == 200
    assert client.post(PATH, json={"grade": "C", "lots": 3, "terms_accepted": True}, headers=HEADERS).status_code == 422


def test_origin_preflight_rate_limit_and_body_bounds(checkout):
    client, create = checkout
    for path in [PATH, "/token-exchange/reserve/quote"]:
        assert client.options(path, headers=HEADERS).status_code == 204
        assert client.options(path, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(PATH, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/token-exchange/reserve/quote?grade=A&lots=1", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(PATH, content='x'*2049, headers={**HEADERS, "Content-Type": "application/json"}).status_code == 413
    assert client.post(PATH, data={"grade": "A"}, headers=HEADERS).status_code == 415
    for _ in range(3):
        assert client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS).status_code == 200
    assert client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS).status_code == 429
    assert create.call_count == 3


def event():
    return {"id": "evt_reserved", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_reserved", "customer": "cus_reserved", "mode": "payment", "currency": "usd",
        "payment_status": "paid", "amount_total": 1600, "customer_details": {"email": "private@example.com"},
        "metadata": {"kind": reserve.KIND, "grade": "A", "lots": "2", "per_lot_cents": "800",
                     "list_price_cents": "1000", "discount_bps": "2000", "index_as_of": "2026-10-26",
                     "workspace_id": "ws_exists", "auto_refill": "true", "amount_microdollars": "16000000",
                     **reserve.WINDOW},
    }}}


@pytest.fixture
def reservation_mail(monkeypatch):
    mail = Mock(return_value=True)
    monkeypatch.setattr(reserve, "get_email_service", lambda _: SimpleNamespace(send=mail))
    def forbidden(*args, **kwargs):
        pytest.fail("Reservation reached a credit operation")
    for name in ["get_credit_account", "credit_workspace_typed_direct", "set_stripe_customer", "record_auto_refill_outcome", "record_adverse_trust_event"]:
        monkeypatch.setattr(InMemoryStore, name, forbidden)
    return mail


def test_webhook_isolation_dedup_log_and_email(client, reservation_mail, caplog):
    with caplog.at_level(logging.INFO, logger="trusted_router.acquisition"):
        first = client.post("/v1/internal/stripe/webhook", json=event())
        replay = event()
        replay["id"] = "evt_other"
        replay["type"] = "checkout.session.async_payment_succeeded"
        second = client.post("/v1/internal/stripe/webhook", json=replay)
        third = client.post("/v1/internal/stripe/webhook", json=event())
    assert first.status_code == 200, first.text
    assert first.json()["data"]["recorded"] is True
    assert second.json()["data"]["duplicate"] is True
    assert third.json()["data"]["duplicate"] is True
    reservation_mail.assert_called_once()
    message = reservation_mail.call_args.args[0]
    assert message.to == "enterprise@trustedrouter.com"
    assert "cs_reserved" in message.text_body
    assert "private@example.com" not in message.text_body
    record, = [r for r in caplog.records if r.message == "acquisition.reserved_tokens_recorded"]
    payload = json.loads(_ApplicationConsoleFormatter().format(record))
    assert payload["stripe_session_id"] == "cs_reserved"
    assert payload["stripe_customer_id"] == "cus_reserved"
    assert payload["per_lot_cents"] == "800"
    assert "workspace_id" not in payload and "email" not in json.dumps(payload)


@pytest.mark.parametrize("patch", [{"amount_total": 1599}, {"currency": "eur"}, {"mode": "setup"}, {"id": None}])
def test_invalid_payment_is_retryable_not_recorded(client, reservation_mail, patch):
    invalid = event()
    invalid["data"]["object"].update(patch)
    assert client.post("/v1/internal/stripe/webhook", json=invalid).status_code == 400
    reservation_mail.assert_not_called()
    assert client.post("/v1/internal/stripe/webhook", json=event()).json()["data"]["recorded"]


@pytest.mark.parametrize("kind", ["payment_intent.succeeded", "setup_intent.succeeded", "payment_intent.payment_failed", "charge.refunded", "checkout.session.async_payment_failed"])
def test_other_reserved_events_never_reach_credits(client, reservation_mail, kind):
    data = event()
    data["type"] = kind
    assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["ignored"]
    reservation_mail.assert_not_called()


def test_unpaid_then_paid_and_mail_failure_retry(client, reservation_mail):
    pending = event()
    pending["data"]["object"]["payment_status"] = "unpaid"
    assert client.post("/v1/internal/stripe/webhook", json=pending).json()["data"]["payment_pending"]
    reservation_mail.assert_not_called()
    reservation_mail.return_value = False
    assert client.post("/v1/internal/stripe/webhook", json=event()).status_code == 503
    reservation_mail.return_value = True
    assert client.post("/v1/internal/stripe/webhook", json=event()).json()["data"]["recorded"]
    assert client.post("/v1/internal/stripe/webhook", json=event()).json()["data"]["duplicate"]
    assert reservation_mail.call_count == 2


def test_concurrent_webhooks(client, reservation_mail):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/v1/internal/stripe/webhook", json=event()), range(4)))
    assert all(r.status_code in {200, 503} for r in responses)
    assert any(r.status_code == 200 for r in responses)
    reservation_mail.assert_called_once()


def test_minimum_charge_is_checked_on_total_not_per_lot(settings, monkeypatch):
    monkeypatch.setitem(token_index.QUOTES, "C", {"value": 27})
    settings.reserved_tokens_discount_bps = 9900
    assert reserve.reservation_quote(settings, "C", 2)["total_cents"] == 54


def test_notification_uses_configured_enterprise_recipient(client, settings, reservation_mail):
    settings.partner_inquiry_email = "sales@example.com"
    result = reserve.handle_reservation_event(event(), settings)
    assert result["recorded"] is True
    assert reservation_mail.call_args.args[0].to == "sales@example.com"
