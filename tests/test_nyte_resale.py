from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.nyte_resale_helpers import call, listing, purchase
from trusted_router.config import Settings
from trusted_router.routes import resale as routes
from trusted_router.services import resale
from trusted_router.storage import InMemoryStore
from trusted_router.storage_resale import refund_cents


@pytest.mark.parametrize("price,paid,fee,refund", [(100, 100, 5, 95), (110, 90, 5, 90), (10, 100, 0, 10), (30, 100, 1, 29), (1, 1, 0, 1), (9999, 10000, 500, 9499)])
def test_resale_rounding(price: int, paid: int, fee: int, refund: int) -> None:
    assert refund_cents(price, paid, 500) == (fee, refund)


def test_resale_rounding_reference_and_per_lot_cap() -> None:
    from decimal import ROUND_HALF_UP, Decimal
    for price in range(1, 10000):
        fee, refund = refund_cents(price, 800, 500)
        net = int((Decimal(price) * Decimal("0.95")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        assert refund == min(net, 800)
        assert fee == price - net


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", stripe_secret_key="sk_test_holder", resale_enabled=True)  # noqa: S106


def test_refund_retry_crash_reconciliation_and_no_credits(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "seller", lots=2)
    listing(store, "seller", lots=2)
    p = purchase(store, "buyer", lots=2, allocate=True)
    fill = p["fills"][0]
    calls = []
    refunds = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("network failure private@example.com")
        row = {"id": "re_once", "amount": 190, "payment_intent": "pi_seller", "status": "succeeded", "metadata": {"nyte_fill_id": fill}}
        refunds.append(row)
        return row
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter(refunds)))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    mail = Mock(return_value=True)
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=mail))
    def forbidden(*args, **kwargs):
        pytest.fail("Resale reached balance credits")
    monkeypatch.setattr(InMemoryStore, "credit_workspace_typed_direct", forbidden)
    resale.process_fill(fill, settings)
    assert store.resale_entities["nyte_fill", fill]["status"] == "retry"
    assert "private@example.com" not in str(mail.call_args)
    assert mail.called
    # Simulate failure to persist a successful refund, followed by recovery.
    original = resale.command
    def lost_commit(op, **kwargs):
        if op == "fill_finish" and kwargs["status"] == "refunded":
            raise RuntimeError("commit lost")
        return original(op, **kwargs)
    monkeypatch.setattr(resale, "command", lost_commit)
    with pytest.raises(RuntimeError, match="commit lost"):
        resale.process_fill(fill, settings)
    monkeypatch.setattr(resale, "command", original)
    store.resale_entities["nyte_fill", fill]["lease_until"] = 0
    resale.process_fill(fill, settings)
    resale.process_fill(fill, settings)
    assert len(calls) == 2
    assert calls[0]["idempotency_key"] == calls[1]["idempotency_key"]
    assert calls[1]["amount"] == 190 and calls[1]["payment_intent"] == "pi_seller"
    assert store.resale_entities["nyte_fill", fill]["stripe_refund_id"] == "re_once"
    assert store.resale_entities["nyte_fill", fill]["status"] == "refunded"
    assert not call(store, "pending")["rows"]


def test_old_ambiguous_refund_never_uses_expired_idempotency_key(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "seller")
    listing(store, "seller")
    p = purchase(store, "buyer", allocate=True)
    fill = p["fills"][0]
    store.resale_entities["nyte_fill", fill]["first_attempt_at"] = 1
    create = Mock()
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    monkeypatch.setattr(resale, "alert_ops", Mock())
    resale.process_fill(fill, settings)
    create.assert_not_called()
    assert store.resale_entities["nyte_fill", fill]["status"] == "retry"


def test_signed_link_expiry_single_use_and_uniform_http(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.routes import public
    public._INQUIRY_HITS.clear()
    public._INQUIRY_GLOBAL_HITS.clear()
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    clock = [100]
    monkeypatch.setattr(resale.time, "time", lambda: clock[0])
    mail = Mock(return_value=True)
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=mail))
    app = FastAPI()
    routes.register(app, settings)
    client = TestClient(app)
    headers = {"Origin": "https://nytokenexchange.com"}
    purchase(store, resale.holder_key("known@example.com"))
    for email in ("known@example.com", "unknown@example.com", "not an email"):
        response = client.post(routes.PATH, json={"action": "email", "email": email}, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"message": resale.GENERIC_RESPONSE}
        assert response.headers["cache-control"] == "private, no-store"
    assert mail.call_count == 2
    token = mail.call_args.args[0].text_body.split("#")[1].splitlines()[0]
    assert "unknown@example.com" not in token
    for bad in (token + "x", token.replace("1000", "9999")):
        with pytest.raises(ValueError):
            resale.redeem_link(bad, settings)
    session = resale.redeem_link(token, settings)["session"]
    with pytest.raises(ValueError, match="already used"):
        resale.redeem_link(token, settings)
    data = routes.handle(routes.HolderRequest(action="positions", session=session), settings)
    assert data["positions"] == []
    resale.send_holder_link("known@example.com", settings)
    expired = mail.call_args.args[0].text_body.split("#")[1].splitlines()[0]
    clock[0] = 1000
    with pytest.raises(ValueError, match="expired"):
        resale.redeem_link(expired, settings)
    with pytest.raises(ValueError, match="expired"):
        routes.handle(routes.HolderRequest(action="positions", session=session), settings)
    assert client.post(routes.PATH, json={"action": "email"}, headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.usefixtures("reservation_mail")
def test_webhook_position_created_when_sales_disabled_and_legacy_terms(client, settings) -> None:
    from tests.test_reserved_tokens import event
    data = event()
    data["data"]["object"]["metadata"]["terms_version"] = "2026-10-27-v3"
    data["data"]["object"]["payment_intent"] = "pi_original"
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 200
    p = resale.command("positions", holder=resale.holder_key("private@example.com"))["positions"][0]
    assert p["payment_intent"] == "pi_original" and p["new_lots"] == 2
    assert p["purchase_completed_at"] != data["data"]["object"]["created"]
    assert p["terms_version"] == "2026-10-27-v3"


# Reuse the credit-poisoning guards and notification stub from checkout tests.
from tests.test_reserved_tokens import reservation_mail  # noqa: E402, F401


def test_backfill_uses_first_paid_timestamp_and_never_allocates_history(settings, monkeypatch):
    from tests.test_reserved_tokens import event
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    obj = event()["data"]["object"]
    obj["payment_intent"] = "pi_import"
    store.claim_reserved_notification(obj["id"], "original", now=200)
    store.finish_reserved_notification(obj["id"], "original", sent=True)
    purchase(store, "seller")
    listing(store, "seller")
    monkeypatch.setattr(resale.stripe.Customer, "list", lambda **kwargs: SimpleNamespace(auto_paging_iter=lambda: iter([{"id": "cus_one"}])))
    monkeypatch.setattr(resale.stripe.checkout.Session, "list", lambda **kwargs: SimpleNamespace(auto_paging_iter=lambda: iter([obj])))
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *args, **kwargs: {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0}})
    holder = resale.holder_key("private@example.com")
    resale.backfill_email("private@example.com", holder, settings)
    resale.backfill_email("private@example.com", holder, settings)
    p = resale.command("positions", holder=holder)["positions"]
    assert len(p) == 1 and p[0]["purchase_completed_at"] == 200
    assert p[0]["new_lots"] == 2 and not p[0]["fills"]
    with pytest.raises(ValueError, match="does not match"):
        resale.backfill_email("other@example.com", holder, settings)
    # Already-refunded Stripe sessions still have payment_status=paid; they
    # must never be imported as sellable inventory.
    obj["id"] = "cs_refunded"
    obj["payment_intent"] = "pi_refunded_import"
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *args, **kwargs: {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 1600}})
    resale.backfill_email("private@example.com", holder, settings)
    imported = resale.command("positions", holder=holder)["positions"][0]
    assert imported["status"] == "needs_review" and imported["purchase_completed_at"] is None


def test_full_refund_is_ops_request_and_reconciles_original_payment_only(settings, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "holder")
    call(store, "request_refund", position="holder", holder="holder", now=101, request_id="refund")
    refund = {"id": "re_full", "amount": 300, "status": "succeeded", "payment_intent": "pi_other"}
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *args, **kwargs: refund)
    with pytest.raises(ValueError, match="does not match"):
        resale.acknowledge_full_refund("holder", "re_full", settings)
    refund["payment_intent"] = "pi_holder"
    resale.acknowledge_full_refund("holder", "re_full", settings)
    resale.acknowledge_full_refund("holder", "re_full", settings)
    assert call(store, "position", position="holder")["status"] == "refunded"
    assert not call(store, "pending", queue="full_refunds")["rows"]


def test_resale_flag_disables_list_but_not_withdrawal(settings, monkeypatch):
    from trusted_router.storage_resale import reference
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "holder")
    lid = listing(store, "holder", lots=1)
    resale.command("token_create", key="link", holder="holder")
    resale.command("token_consume", key="link", session=reference("session"))
    settings.resale_enabled = False
    with pytest.raises(ValueError, match="not enabled"):
        routes.handle(routes.HolderRequest(action="list", position="holder", lots=1, session="session", request_id="list"), settings)
    result = routes.handle(routes.HolderRequest(action="withdraw", position="holder", listing=lid, session="session", request_id="withdraw"), settings)
    assert result["status"] == "withdrawn"


def test_postgres_transaction_implementation_on_sql_fake():
    from tests.conformance import test_nyte_resale_ledger as contract
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    conn = sqlite_postgres_conn()
    try:
        store = postgres_store_on(conn)
        contract.test_nyte_fifo_partial_fill_and_session_event_idempotency(store, "postgresunique")
        contract.test_nyte_relist_goes_to_tail_and_refund_lease_fencing(store, "postgreslease")
    finally:
        conn._raw.close()


def test_paid_webhook_allocates_and_refund_failure_never_fails_buyer(settings, monkeypatch):
    from tests.test_reserved_tokens import event
    from trusted_router.services import reserved_tokens
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    purchase(store, "seller", lots=3, price=900)
    lid = listing(store, "seller", lots=3)
    mail = Mock(return_value=True)
    monkeypatch.setattr(reserved_tokens, "get_email_service", lambda _: SimpleNamespace(send=mail))
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=mail))
    monkeypatch.setattr(resale.stripe.Refund, "list", Mock(side_effect=RuntimeError("temporarily offline")))
    data = event()
    data["data"]["object"]["payment_intent"] = "pi_buyer"
    result = reserved_tokens.handle_reservation_event(data, settings)
    assert result["recorded"] is True and result["credited"] is False
    p = call(store, "position", position="cs_reserved")
    assert p["new_lots"] == 0 and len(p["fills"]) == 1
    fill = store.resale_entities["nyte_fill", p["fills"][0]]
    assert fill["listing"] == lid and fill["refund_amount_cents"] == 1520
    assert fill["status"] == "retry"
    data["id"] = "evt_another_delivery"
    assert reserved_tokens.handle_reservation_event(data, settings)["duplicate"]
    assert call(store, "position", position="seller")["sold_lots"] == 2


@pytest.mark.usefixtures("reservation_mail")
@pytest.mark.parametrize("kind", ["refund.created", "refund.updated", "refund.failed", "charge.refunded", "charge.refund.updated"])
def test_reserved_refunds_without_metadata_never_reach_credit_handlers(client, kind):
    from tests.test_reserved_tokens import event
    data = event()
    data["data"]["object"]["payment_intent"] = "pi_original"
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 200
    response = client.post("/v1/internal/stripe/webhook", json={
        "id": "evt_refund", "type": kind, "data": {"object": {
            "id": "re_card", "payment_intent": "pi_original", "amount": 100, "status": "succeeded",
            "metadata": {}, "refunds": {"data": [{"id": "re_card", "amount": 100}]},
        }},
    })
    assert response.status_code == 200
    assert response.json()["data"] == {"reserved_tokens": True, "credited": False, "ignored": True}


def test_legacy_sent_notification_without_timestamp_does_not_reopen_refund_window(settings, monkeypatch):
    from tests.test_reserved_tokens import event
    from trusted_router.services import reserved_tokens
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(reserved_tokens, "STORE", store)
    store.reserved_notifications["cs_reserved"] = {"sent": True}
    result = reserved_tokens.handle_reservation_event(event(), settings)
    assert result["duplicate"]
    assert call(store, "position", position="cs_reserved")["purchase_completed_at"] is None


def test_stripe_contact_lookup_never_logs_email(settings, monkeypatch, caplog):
    import logging
    email = "secret-holder@example.com"
    def customer_list(**kwargs):
        logging.getLogger("stripe").info("Request /v1/customers?email=%s", kwargs["email"])
        logging.getLogger("stripe").debug("API response body email=%s", kwargs["email"])
        logging.getLogger("urllib3.connectionpool").debug("GET email=%s", kwargs["email"])
        return SimpleNamespace(auto_paging_iter=lambda: iter([]))
    mock = Mock(side_effect=customer_list)
    monkeypatch.setattr(resale.stripe.Customer, "list", mock)
    with caplog.at_level(logging.DEBUG):
        resale.backfill_email(email, resale.holder_key(email), settings)
    assert email not in caplog.text
    mock.assert_called_once()
    monkeypatch.setattr(resale.stripe, "log", "info")
    with pytest.raises(ValueError, match="console diagnostics"):
        resale.backfill_email(email, resale.holder_key(email), settings)
    mock.assert_called_once()


def test_holder_email_sdk_diagnostics_cannot_log_address_or_link(settings, monkeypatch, caplog):
    import logging
    def send(message):
        logging.getLogger("botocore.endpoint").info("SES body %s %s", message.to, message.text_body)
        return True
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=send))
    with caplog.at_level(logging.DEBUG):
        resale.send_holder_link("secret-holder@example.com", settings)
    assert "secret-holder@example.com" not in caplog.text
    assert "manage-reservation#" not in caplog.text


def test_contact_lookup_suppresses_sentry_outbound_query_spans(settings, monkeypatch):
    import sentry_sdk
    from sentry_sdk.integrations.stdlib import StdlibIntegration
    with sentry_sdk.new_scope() as parent:
        outside = sentry_sdk.Client(dsn="", default_integrations=False)
        parent.set_client(outside)
        def customer_list(**kwargs):
            client = sentry_sdk.get_client()
            assert client is not outside
            assert client.options["dsn"] == ""
            assert client.get_integration(StdlibIntegration) is None
            assert sentry_sdk.get_current_scope().span is None
            return SimpleNamespace(auto_paging_iter=lambda: iter([]))
        monkeypatch.setattr(resale.stripe.Customer, "list", customer_list)
        resale.backfill_email("private@example.com", resale.holder_key("private@example.com"), settings)
        assert sentry_sdk.get_client() is outside


def test_memory_ledger_never_aliases_caller_input_or_result():
    store = InMemoryStore()
    position = {"session_id": "cs_copy", "holder": "owner", "grade": "A", "lots": 2,
                "per_lot_cents": 100, "payment_intent": "pi_copy", "purchase_completed_at": 100}
    result = call(store, "purchase", position=position, event_id="evt_copy")
    position["lots"] = 100
    result["lots"] = 200
    saved = call(store, "position", position="cs_copy")
    assert saved["lots"] == 2 and saved["purchase_completed_at"] == 100
