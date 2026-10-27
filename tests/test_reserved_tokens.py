from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.content import token_index
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.routes import public
from trusted_router.services import reserved_tokens as reserve
from trusted_router.storage import InMemoryStore

HEADERS = {"Origin": "https://nytokenexchange.com"}
PATH = "/token-exchange/reserve/checkout"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reserve, "_utc_now", lambda: datetime(2026, 10, 27, 12, tzinfo=UTC))
    monkeypatch.setattr(token_index, "AS_OF_ISO", "2026-10-26")


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", reserved_tokens_checkout_enabled=True,
                    reserved_tokens_discount_bps=1234, stripe_secret_key="sk_test_reserve")  # noqa: S106


@pytest.fixture
def checkout(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Mock]:
    public._INQUIRY_HITS.clear()
    public._INQUIRY_GLOBAL_HITS.clear()
    app = FastAPI()
    public.register_public_action_routes(app, settings)
    create = Mock(return_value={"url": "https://checkout.stripe.com/c/pay/test"})
    monkeypatch.setattr(reserve.stripe.checkout.Session, "create", create)
    return TestClient(app), create


@pytest.mark.parametrize("grade,value", [("AAA", 1794), ("A", 863), ("B", 187), ("C", 27)])
def test_price_each_grade(settings: Settings, monkeypatch: pytest.MonkeyPatch, grade: str, value: Any) -> None:
    monkeypatch.setitem(token_index.QUOTES, grade, {"value": value})
    quote = reserve.reservation_quote(settings, grade, 3)
    expected = int((Decimal(value) * 100 * Decimal("0.8766")).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert quote["per_lot_cents"] == expected
    assert quote["total_cents"] == expected * 3
    assert quote["list_price_cents"] == value * 100
    assert quote["index_as_of"] == token_index.AS_OF_ISO


def test_round_half_up_per_lot_before_multiplication(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(token_index.QUOTES, "C", {"value": 27})
    settings.reserved_tokens_discount_bps = 50
    quote = reserve.reservation_quote(settings, "C", 3)
    assert quote["per_lot_cents"] == 2687  # 2686.5 cents
    assert quote["total_cents"] == 8061


@pytest.mark.parametrize("field,value", [("reserved_tokens_checkout_enabled", False), ("reserved_tokens_discount_bps", None), ("stripe_secret_key", None)])
def test_unconfigured_refuses(settings: Settings, checkout: tuple[TestClient, Mock], field: str, value: Any) -> None:
    client, create = checkout
    setattr(settings, field, value)
    response = client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["message"]
    assert response.headers["access-control-allow-origin"] == HEADERS["Origin"]
    create.assert_not_called()


@pytest.mark.parametrize("patch", [{"grade": "ALL"}, {"grade": "model"}, {"lots": 0}, {"lots": 101}, {"lots": -1}, {"lots": 1.5}, {"lots": True}, {"lots": "1"}, {"terms_accepted": False}, {"terms_accepted": "true"}, {"price": 1}])
def test_invalid_body(checkout: tuple[TestClient, Mock], patch: dict[str, Any]) -> None:
    client, create = checkout
    response = client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True, **patch}, headers=HEADERS)
    assert response.status_code == 422
    create.assert_not_called()


def test_missing_terms(checkout: tuple[TestClient, Mock]) -> None:
    client, create = checkout
    assert client.post(PATH, json={"grade": "A", "lots": 1}, headers=HEADERS).status_code == 422
    create.assert_not_called()


def test_stripe_parameters_and_quote(checkout: tuple[TestClient, Mock], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert args["metadata"]["refund_window_days"] == "30"
    assert "refundable_until" not in args["metadata"]
    assert args["metadata"]["terms_version"] == "2026-10-27-v3"
    assert args["metadata"]["transfer_policy"] == "nyte_resale_only"
    assert args["metadata"]["resale_fee_bps"] == "500"
    assert args["metadata"]["resale_priority"] == "ahead_of_new_inventory"
    assert args["metadata"]["resale_payout_method"] == "original_card_refund"
    assert args["metadata"]["resale_payout_cap"] == "original_lot_purchase_price"
    assert args["payment_intent_data"] == {"metadata": args["metadata"]}
    assert args["line_items"] == [{"quantity": 2, "price_data": {
        "currency": "usd", "unit_amount": quote.json()["per_lot_cents"], "product_data": {
            "name": "Reserved tokens · Grade B · 1B tokens",
            "description": "Usable January 1, 2027 through December 31, 2027. Unused tokens expire December 31, 2027. Full refund on request within 30 days of purchase. After 30 days, exit only through NYTE resale. Transfer only through NYTE resale: list lots; NYTE resells ahead of new inventory. Each sale refunds the resale price minus a 5% fee to the original card, capped at what you paid for those lots.",
        },
    }}]


def test_custom_limit_and_upper_boundary(checkout: tuple[TestClient, Mock], settings: Settings) -> None:
    client, _ = checkout
    settings.reserved_tokens_max_lots_per_checkout = 2
    assert client.post(PATH, json={"grade": "C", "lots": 2, "terms_accepted": True}, headers=HEADERS).status_code == 200
    assert client.post(PATH, json={"grade": "C", "lots": 3, "terms_accepted": True}, headers=HEADERS).status_code == 422


def test_origin_preflight_rate_limit_and_body_bounds(checkout: tuple[TestClient, Mock]) -> None:
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


def event() -> dict[str, Any]:
    return {"id": "evt_reserved", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_reserved", "created": 1793102400, "customer": "cus_reserved", "mode": "payment", "currency": "usd",
        "payment_status": "paid", "amount_total": 1600, "customer_details": {"email": "private@example.com"},
        "metadata": {"kind": reserve.KIND, "grade": "A", "lots": "2", "per_lot_cents": "800",
                     "list_price_cents": "1000", "discount_bps": "2000", "index_as_of": "2026-10-26",
                     "workspace_id": "ws_exists", "auto_refill": "true", "amount_microdollars": "16000000",
                     **reserve.WINDOW},
    }}}


@pytest.fixture
def reservation_mail(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mail = Mock(return_value=True)
    monkeypatch.setattr(reserve, "get_email_service", lambda _: SimpleNamespace(send=mail))
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Reservation reached a credit operation")
    for name in ["get_credit_account", "credit_workspace_typed_direct", "set_stripe_customer", "record_auto_refill_outcome", "record_adverse_trust_event"]:
        monkeypatch.setattr(InMemoryStore, name, forbidden)
    return mail


def test_webhook_isolation_dedup_log_and_email(client: TestClient, reservation_mail: Mock, caplog: pytest.LogCaptureFixture) -> None:
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
    assert payload["position_id"] == "nyte_pos_7367bde47b80a6074439f18379a00da7cfa3a94a3815b130640a90eda935ca22"
    assert payload["position_id"] in message.subject
    details = json.loads(message.text_body.split("\n", 1)[1])
    for key in ("position_id", "purchase_completed_at", "refund_deadline_at", *reserve.WINDOW):
        assert details[key] == payload[key]
    assert payload["purchase_completed_at"] == "2026-10-27T12:00:00+00:00"
    assert payload["refund_deadline_at"] == "2026-11-26T12:00:00+00:00"
    assert payload["refund_window_days"] == "30"
    assert "refundable_until" not in payload
    assert payload["stripe_session_id"] == "cs_reserved"
    assert payload["stripe_customer_id"] == "cus_reserved"
    assert payload["per_lot_cents"] == "800"
    assert "workspace_id" not in payload and "email" not in json.dumps(payload)


@pytest.mark.parametrize("patch", [{"amount_total": 1599}, {"currency": "eur"}, {"mode": "setup"}, {"id": None}])
def test_invalid_payment_is_retryable_not_recorded(client: TestClient, reservation_mail: Mock, patch: dict[str, Any]) -> None:
    invalid = event()
    invalid["data"]["object"].update(patch)
    assert client.post("/v1/internal/stripe/webhook", json=invalid).status_code == 400
    reservation_mail.assert_not_called()
    assert client.post("/v1/internal/stripe/webhook", json=event()).json()["data"]["recorded"]


@pytest.mark.parametrize("kind", ["payment_intent.succeeded", "setup_intent.succeeded", "payment_intent.payment_failed", "charge.refunded", "checkout.session.async_payment_failed"])
def test_other_reserved_events_never_reach_credits(client: TestClient, reservation_mail: Mock, kind: str) -> None:
    data = event()
    data["type"] = kind
    assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["ignored"]
    reservation_mail.assert_not_called()


def test_unpaid_then_paid_and_mail_failure_retry(client: TestClient, reservation_mail: Mock) -> None:
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
    first, retry = [call.args[0] for call in reservation_mail.call_args_list]
    assert first.subject == retry.subject
    assert first.text_body == retry.text_body


def test_concurrent_webhooks(client: TestClient, reservation_mail: Mock) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/v1/internal/stripe/webhook", json=event()), range(4)))
    assert all(r.status_code in {200, 503} for r in responses)
    assert any(r.status_code == 200 for r in responses)
    reservation_mail.assert_called_once()


def test_minimum_charge_is_checked_on_total_not_per_lot(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(token_index.QUOTES, "C", {"value": 27})
    settings.reserved_tokens_discount_bps = 9900
    assert reserve.reservation_quote(settings, "C", 2)["total_cents"] == 54


def test_notification_uses_configured_enterprise_recipient(client: TestClient, settings: Settings, reservation_mail: Mock) -> None:
    settings.partner_inquiry_email = "sales@example.com"
    result = reserve.handle_reservation_event(event(), settings)
    assert result["recorded"] is True
    assert reservation_mail.call_args.args[0].to == "sales@example.com"


@pytest.mark.parametrize("event_type", ["checkout.session.completed", "checkout.session.async_payment_succeeded"])
def test_refund_window_starts_at_first_paid_webhook(client: TestClient, reservation_mail: Mock, monkeypatch: pytest.MonkeyPatch, event_type: str) -> None:
    created_at = datetime(2026, 12, 31, 12, tzinfo=UTC)
    purchased_at = created_at + timedelta(hours=23)
    clock = created_at
    monkeypatch.setattr(reserve, "_utc_now", lambda: clock)
    data = event()
    data["created"] = 1  # Neither Stripe event nor session creation determines purchase time.
    data["data"]["object"]["created"] = int(created_at.timestamp())
    data["data"]["object"]["payment_status"] = "unpaid"
    assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["payment_pending"]
    reservation_mail.assert_not_called()

    clock = purchased_at
    data["type"] = event_type
    data["data"]["object"]["payment_status"] = "paid"
    reservation_mail.return_value = False
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 503
    first = json.loads(reservation_mail.call_args.args[0].text_body.split("\n", 1)[1])
    assert first["purchase_completed_at"] == "2027-01-01T11:00:00+00:00"
    assert first["refund_deadline_at"] == "2027-01-31T11:00:00+00:00"

    clock = purchased_at + timedelta(days=2)
    data["id"] = "evt_retry"
    data["type"] = "checkout.session.async_payment_succeeded"
    reservation_mail.return_value = True
    assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["recorded"]
    retry = json.loads(reservation_mail.call_args.args[0].text_body.split("\n", 1)[1])
    assert retry == first
    clock += timedelta(days=1)
    assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["duplicate"]
    assert reservation_mail.call_count == 2


def test_distinct_purchases_get_distinct_positions(client: TestClient, reservation_mail: Mock) -> None:
    for session_id in ("cs_reserved", "cs_another_purchase"):
        data = event()
        data["data"]["object"]["id"] = session_id
        assert client.post("/v1/internal/stripe/webhook", json=data).json()["data"]["recorded"]
    positions = [json.loads(call.args[0].text_body.split("\n", 1)[1])["position_id"]
                 for call in reservation_mail.call_args_list]
    assert len(set(positions)) == 2
    assert all(position.startswith("nyte_pos_") and len(position) == 73 for position in positions)


@pytest.mark.parametrize("patch", [
    {"refund_window_days": "31"}, {"terms_version": "2026-10-27"},
    {"transfer_policy": "unrestricted"}, {"resale_fee_bps": "0"},
    {"resale_priority": "after_new_inventory"}, {"resale_payout_method": "cash"},
    {"resale_payout_cap": "unlimited"},
])
def test_invalid_locked_terms_do_not_record_position(client: TestClient, reservation_mail: Mock, patch: dict[str, Any]) -> None:
    data = event()
    data["data"]["object"]["metadata"].update(patch)
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 400
    reservation_mail.assert_not_called()


def test_approved_rollout_discount(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    rollout = (Path(__file__).resolve().parents[1] / "scripts/deploy/rollout.sh").read_text()
    assert '"TR_RESERVED_TOKENS_CHECKOUT_ENABLED=false"' in rollout
    assert '"TR_RESERVED_TOKENS_DISCOUNT_BPS=1000"' in rollout
    settings.reserved_tokens_discount_bps = 1000
    monkeypatch.setitem(token_index.QUOTES, "B", {"value": 187})
    quote = reserve.reservation_quote(settings, "B", 2)
    assert quote["per_lot_cents"] == 16830
    assert quote["total_cents"] == 33660


@pytest.mark.parametrize("as_of", ["2026-09-11", "2026-10-28", "not-a-date", "2026-02-30"])
def test_invalid_price_date_blocks_quote_and_stripe(checkout: tuple[TestClient, Mock], monkeypatch: pytest.MonkeyPatch, as_of: str) -> None:
    monkeypatch.setattr(token_index, "AS_OF_ISO", as_of)
    client, create = checkout
    responses = [
        client.get("/token-exchange/reserve/quote?grade=A&lots=1", headers=HEADERS),
        client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "service_unavailable"
        assert response.json()["error"]["message"] == "Reserved token prices are being updated; book a call"
    create.assert_not_called()


@pytest.mark.parametrize("as_of", ["2026-09-12", "2026-10-26", "2026-10-27"])
def test_fresh_price_date_returns_quote(checkout: tuple[TestClient, Mock], monkeypatch: pytest.MonkeyPatch, as_of: str) -> None:
    monkeypatch.setattr(token_index, "AS_OF_ISO", as_of)
    client, _ = checkout
    response = client.get("/token-exchange/reserve/quote?grade=A&lots=1", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["index_as_of"] == as_of
    assert response.json()["per_lot_cents"] > 0


def test_configured_max_price_age(checkout: tuple[TestClient, Mock], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.reserved_tokens_max_price_age_days = 1
    monkeypatch.setattr(token_index, "AS_OF_ISO", "2026-10-25")
    client, create = checkout
    assert client.post(PATH, json={"grade": "A", "lots": 1, "terms_accepted": True}, headers=HEADERS).status_code == 503
    create.assert_not_called()


def test_max_price_age_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TR_RESERVED_TOKENS_MAX_PRICE_AGE_DAYS", raising=False)
    assert Settings(environment="test").reserved_tokens_max_price_age_days == 45
    monkeypatch.setenv("TR_RESERVED_TOKENS_MAX_PRICE_AGE_DAYS", "7")
    assert Settings(environment="test").reserved_tokens_max_price_age_days == 7
    for invalid in ("0", "-1"):
        monkeypatch.setenv("TR_RESERVED_TOKENS_MAX_PRICE_AGE_DAYS", invalid)
        with pytest.raises(ValidationError, match="reserved_tokens_max_price_age_days"):
            Settings(environment="test")
