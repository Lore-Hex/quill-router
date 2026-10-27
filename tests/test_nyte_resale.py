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


@pytest.fixture(autouse=True)
def healthy_stripe_payment(monkeypatch):
    # Enabled purchase creation now verifies live Stripe state. Keep unit tests
    # offline; adverse-state cases override this explicit healthy response.
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **kw: {
        "status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 0},
    })


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
def test_webhook_position_created_when_sales_disabled_and_legacy_terms(client, test_settings) -> None:
    from tests.test_reserved_tokens import event
    data = event()
    test_settings.resale_enabled = True
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
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
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
    call(store, "reconcile_payment", payment_intent="pi_holder", payment_state={"amount_refunded": 300})
    assert call(store, "position", position="holder")["status"] == "refund_requested"
    resale.acknowledge_full_refund("holder", "re_full", settings)
    resale.acknowledge_full_refund("holder", "re_full", settings)
    assert call(store, "position", position="holder")["status"] == "refunded"
    assert not call(store, "pending", queue="full_refunds")["rows"]


@pytest.mark.parametrize("action", ["email", "redeem", "positions", "backfill", "list", "withdraw", "request_refund"])
def test_resale_disabled_routes_are_inert_without_email_or_ledger(action, settings, monkeypatch):
    from copy import deepcopy
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "holder")
    listing(store, "holder", lots=1)
    before = deepcopy(store.resale_entities)
    ledger = Mock(side_effect=AssertionError("Disabled route touched ledger"))
    mail = Mock(side_effect=AssertionError("Disabled route sent email"))
    monkeypatch.setattr(InMemoryStore, "reserved_resale", ledger)
    monkeypatch.setattr(resale, "send_holder_link", mail)
    settings.resale_enabled = False
    app = FastAPI()
    routes.register(app, settings)
    client = TestClient(app)
    headers = {"Origin": "https://nytokenexchange.com"}
    response = client.post(routes.PATH, json={"action": action, "email": "known@example.com"}, headers=headers)
    assert response.status_code == 200 and response.json() == {"message": resale.GENERIC_RESPONSE}
    assert client.options(routes.PATH, headers=headers).json() == response.json()
    assert routes.handle(routes.HolderRequest(action=action), settings) == response.json()
    assert store.resale_entities == before
    ledger.assert_not_called()
    mail.assert_not_called()


@pytest.mark.usefixtures("reservation_mail")
def test_disabled_paid_webhook_uses_original_notification_path_even_with_broken_ledger(client, test_settings, monkeypatch):
    from tests.test_reserved_tokens import event
    from trusted_router.storage import STORE
    test_settings.resale_enabled = False
    ledger = Mock(side_effect=AssertionError("Disabled webhook touched resale ledger"))
    monkeypatch.setattr(InMemoryStore, "reserved_resale", ledger)
    first = client.post("/v1/internal/stripe/webhook", json=event())
    assert first.status_code == 200 and first.json()["data"]["recorded"] is True
    state, timestamp = STORE.claim_reserved_notification("cs_reserved", "probe", now=9999999999)
    assert state == "sent"
    retry = client.post("/v1/internal/stripe/webhook", json=event())
    assert retry.status_code == 200 and retry.json()["data"]["duplicate"] is True
    assert STORE.claim_reserved_notification("cs_reserved", "probe", now=9999999999) == ("sent", timestamp)
    assert not STORE.record_webhook_event_once("reserved_tokens_2027", "cs_reserved")
    ledger.assert_not_called()


@pytest.mark.parametrize("fee", [-1, 0, 499, 501, 10000])
def test_resale_fee_change_requires_new_terms_version(fee, monkeypatch):
    from pydantic import ValidationError
    monkeypatch.setenv("TR_RESALE_FEE_BPS", str(fee))
    with pytest.raises(ValidationError, match="fee change needs a new terms version"):
        Settings(environment="test")
    with pytest.raises(ValidationError, match="fee change needs a new terms version"):
        Settings(environment="test", resale_fee_bps=fee)
    assert Settings(environment="test", resale_fee_bps=500).resale_fee_bps == 500


def test_postgres_transaction_implementation_on_sql_fake(monkeypatch):
    from tests.conformance import test_nyte_resale_ledger as contract
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    conn = sqlite_postgres_conn()
    try:
        store = postgres_store_on(conn)
        contract.test_nyte_fifo_partial_fill_and_session_event_idempotency(store, "postgresunique")
        contract.test_nyte_relist_goes_to_tail_and_refund_lease_fencing(store, "postgreslease")
        contract.test_nyte_external_payment_change_blocks_existing_position_and_listings(store, "pgrefund0000", {"amount_refunded": 1})
        contract.test_nyte_external_payment_change_blocks_existing_position_and_listings(store, "pgdispute000", {"disputed": True})
        contract.test_nyte_own_refund_before_worker_commit_is_exempt_but_external_excess_blocks(store, "pgownrefund0")
        contract.test_nyte_timestamped_import_still_allocates_after_webhook_claim(store, "pgimportrace")
        contract.test_nyte_completed_legacy_notification_import_is_historical(store, "pglegacy0000")
        contract.test_nyte_adverse_before_position_is_durable_and_blocks_purchase(store, "pgearlyref00", {"amount_refunded": 100}, monkeypatch)
        contract.test_nyte_adverse_before_position_is_durable_and_blocks_purchase(store, "pgearlydisp0", {"disputed": True}, monkeypatch)
        contract.test_nyte_old_purchase_checks_current_stripe_before_held(store, "pgoldstripe0", monkeypatch, "webhook_replay")
        contract.test_nyte_old_purchase_checks_current_stripe_before_held(store, "pgoldimport0", monkeypatch, "historical_import")
        contract.test_nyte_successful_refund_later_fails_without_reissuance(store, "pgfailedref0", monkeypatch, "failed")
        contract.test_nyte_successful_refund_later_fails_without_reissuance(store, "pgcancelref0", monkeypatch, "canceled")
        for failure_first in (True, False):
            for failure in ("failed", "canceled"):
                contract.test_nyte_refund_failure_and_success_commit_are_order_independent(
                    store, f"pgrace{failure_first}{failure}", monkeypatch, failure_first, failure)
        for kind in ("resale_fill", "full_refund"):
            contract.test_nyte_unbound_refund_failure_survives_acknowledgement(store, "pgunbound" + kind, kind)
            for failure_first in (True, False):
                for by_metadata in (True, False):
                    for failure in ("failed", "canceled"):
                        contract.test_nyte_shared_refund_obligations_are_monotonic(
                            store, contract.reference(f"pgshared{kind}{failure_first}{by_metadata}{failure}")[:20], monkeypatch,
                            kind, failure_first, by_metadata, failure)

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
def test_reserved_refunds_without_metadata_never_reach_credit_handlers(client, kind, monkeypatch, test_settings):
    test_settings.resale_enabled = True
    from tests.test_reserved_tokens import event
    data = event()
    data["data"]["object"]["payment_intent"] = "pi_original"
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 200
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **k: {"latest_charge": {"paid": True, "amount_refunded": 100}})
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    response = client.post("/v1/internal/stripe/webhook", json={
        "id": "evt_refund", "type": kind, "data": {"object": {
            "id": "re_card", "payment_intent": "pi_original", "amount": 100, "status": "succeeded", "paid": True, "amount_refunded": 100,
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


@pytest.mark.parametrize("kind,metadata", [("charge.refunded", {}), ("charge.refunded", {"kind": "reserved_tokens_2027"}),
                                         ("charge.dispute.created", {}), ("charge.dispute.closed", {})])
def test_external_payment_webhook_blocks_and_withdraws_before_allocation(client, test_settings, monkeypatch, kind, metadata):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    purchase(STORE, "seller")
    listing(STORE, "seller")
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    response = client.post("/v1/internal/stripe/webhook", json={"id": "evt_external", "type": kind, "data": {"object": {
        "id": "ch_seller", "payment_intent": "pi_seller", "paid": True, "amount_refunded": 1, "metadata": metadata,
    }}})
    assert response.status_code == 200
    p = call(STORE, "positions", holder="seller")["positions"][0]
    assert p["status"] == "needs_review" and p["listed_lots"] == 0
    assert p["listing_rows"][0]["status"] == "withdrawn"
    with pytest.raises(ValueError, match="review"):
        listing(STORE, "seller", lots=1, request="new")
    assert purchase(STORE, "buyer", allocate=True)["new_lots"] == 3


def test_own_refund_webhook_and_reimport_keep_unsold_lots_sellable(client, test_settings, monkeypatch):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    purchase(STORE, "seller")
    listing(STORE, "seller")
    p = purchase(STORE, "buyer", lots=1, allocate=True)
    fid = p["fills"][0]
    refund = {"id": "re_own", "payment_intent": "pi_seller", "amount": 95,
              "status": "succeeded", "metadata": {"nyte_fill_id": fid}}
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([refund])))
    response = client.post("/v1/internal/stripe/webhook", json={"id": "evt_own", "type": "charge.refunded", "data": {"object": {
        "id": "ch_seller", "payment_intent": "pi_seller", "paid": True, "amount_refunded": 95,
        "metadata": {"kind": "reserved_tokens_2027"},
    }}})
    assert response.status_code == 200
    seller = call(STORE, "position", position="seller")
    assert seller["status"] == "held" and seller["listed_lots"] == 2
    state = resale.payment_state("pi_seller", {"paid": True, "amount_refunded": 95}, test_settings)
    result = call(STORE, "purchase", position=seller, event_id="backfill:seller", backfill=True, payment_state=state)
    assert result["status"] == "held" and result["listed_lots"] == 2
    assert purchase(STORE, "next", lots=2, allocate=True)["new_lots"] == 0


def test_reimport_existing_refunded_position_is_blocked(settings, monkeypatch):
    from tests.test_reserved_tokens import event
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    obj = event()["data"]["object"]
    obj["payment_intent"] = "pi_external"
    # Historical paid purchase was already imported and listed.
    store.claim_reserved_notification(obj["id"], "legacy", now=100)
    store.finish_reserved_notification(obj["id"], "legacy", sent=True)
    p = resale.record_purchase(obj, "backfill:" + obj["id"], None, settings, backfill=True)
    call(store, "list", holder=p["holder"], position=obj["id"], lots=1, request_id="list")
    monkeypatch.setattr(resale.stripe.Customer, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([{"id": "cus_old"}])))
    monkeypatch.setattr(resale.stripe.checkout.Session, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([obj])))
    monkeypatch.setattr(resale.stripe.PaymentIntent, "retrieve", lambda *a, **kw: {"status": "succeeded", "latest_charge": {"paid": True, "amount_refunded": 1600}})
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    resale.backfill_email("private@example.com", p["holder"], settings)
    saved = call(store, "positions", holder=p["holder"])["positions"][0]
    assert saved["status"] == "needs_review" and saved["listing_rows"][0]["status"] == "withdrawn"


@pytest.mark.usefixtures("reservation_mail")
@pytest.mark.parametrize("kind", ["charge.refunded", "charge.dispute.created", "refund.created"])
def test_adverse_webhook_before_checkout_is_not_lost(client, test_settings, monkeypatch, kind):
    from tests.test_reserved_tokens import event
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    response = client.post("/v1/internal/stripe/webhook", json={"id": "evt_early", "type": kind, "data": {"object": {
        "id": "ch_early", "payment_intent": "pi_early", "paid": True, "amount_refunded": 1600, "amount": 1600, "status": "succeeded",
        "metadata": {"kind": "reserved_tokens_2027"},
    }}})
    assert response.status_code == 200
    assert call(STORE, "payment", payment_intent="pi_early") == {}
    data = event()
    data["data"]["object"]["payment_intent"] = "pi_early"
    assert client.post("/v1/internal/stripe/webhook", json=data).status_code == 200
    p = call(STORE, "position", position="cs_reserved")
    assert p["status"] == "needs_review" and p["fills"] == []
    with pytest.raises(ValueError, match="review"):
        call(STORE, "list", position="cs_reserved", holder=p["holder"], lots=1, request_id="after-adverse")


@pytest.mark.parametrize("kind,status", [("refund.failed", "failed"), ("refund.updated", "failed"), ("refund.updated", "canceled")])
@pytest.mark.parametrize("legacy", [False, True])
def test_late_refund_failure_reopens_for_ops_and_never_reissues(client, test_settings, monkeypatch, kind, status, legacy):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    purchase(STORE, "seller")
    listing(STORE, "seller")
    fid = purchase(STORE, "buyer", lots=1, allocate=True)["fills"][0]
    call(STORE, "fill_claim", fill=fid, owner="worker")
    call(STORE, "fill_finish", fill=fid, owner="worker", status="refunded", stripe_refund_id="re_later")
    if legacy:
        STORE.target.resale_entities.pop(("nyte_refund", "re_later"))
    mail = Mock(return_value=True)
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=mail))
    create = Mock(side_effect=AssertionError("Duplicate issuance"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    data = {"id": "evt_failure", "type": kind, "data": {"object": {
        "id": "re_later", "payment_intent": "pi_seller", "amount": 95, "status": status,
        "metadata": {"nyte_fill_id": fid} if legacy else {}, "description": "private@example.com",
    }}}
    for _ in range(2):
        response = client.post("/v1/internal/stripe/webhook", json=data)
        assert response.status_code == 200
    rows = call(STORE, "pending", queue="refunds_needs_ops")["rows"]
    assert len(rows) == 1 and rows[0]["status"] == "needs_ops"
    assert rows[0]["stripe_refund_id"] == "re_later"
    assert call(STORE, "pending")["rows"] == []
    resale.process_fill(fid, test_settings)
    create.assert_not_called()
    assert mail.called
    message = mail.call_args.args[0]
    assert fid in message.text_body and "re_later" in message.text_body
    assert "private@example.com" not in message.text_body


def test_unknown_failed_refund_cannot_reopen_fill(client, test_settings, monkeypatch):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    purchase(STORE, "seller")
    listing(STORE, "seller")
    fid = purchase(STORE, "buyer", lots=1, allocate=True)["fills"][0]
    call(STORE, "fill_claim", fill=fid, owner="worker")
    call(STORE, "fill_finish", fill=fid, owner="worker", status="refunded", stripe_refund_id="re_known")
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)
    result = client.post("/v1/internal/stripe/webhook", json={"id": "evt_unknown", "type": "refund.failed", "data": {"object": {
        "id": "re_unknown", "payment_intent": "pi_seller", "amount": 95, "status": "failed", "metadata": {"fill_id": "unknown_fill"},
    }}})
    assert result.status_code == 200
    assert call(STORE, "pending", queue="refunds_needs_ops")["rows"] == []
    assert not call(STORE, "fill_claim", fill=fid, owner="again")
    alert.assert_not_called()


def test_worker_observing_failed_refund_uses_ops_queue(settings, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "seller")
    listing(store, "seller")
    fid = purchase(store, "buyer", lots=1, allocate=True)["fills"][0]
    call(store, "fill_claim", fill=fid, owner="first")
    call(store, "fill_finish", fill=fid, owner="first", status="retry", stripe_refund_id="re_pending")
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", lambda *a, **kw: {
        "id": "re_pending", "payment_intent": "pi_seller", "amount": 95, "status": "failed",
    })
    monkeypatch.setattr(resale, "alert_ops", Mock())
    create = Mock()
    monkeypatch.setattr(resale.stripe.Refund, "create", create)
    resale.process_fill(fid, settings)
    resale.process_fill(fid, settings)
    create.assert_not_called()
    assert call(store, "pending")["rows"] == []
    assert call(store, "pending", queue="refunds_needs_ops")["rows"][0]["status"] == "needs_ops"


def test_refund_failure_webhook_before_create_returns_is_terminal(client, test_settings, monkeypatch):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    test_settings.stripe_secret_key = "sk_test_fixture"  # noqa: S105
    purchase(STORE, "seller")
    listing(STORE, "seller")
    fid = purchase(STORE, "buyer", lots=1, allocate=True)["fills"][0]
    monkeypatch.setattr(resale, "alert_ops", Mock())
    monkeypatch.setattr(resale.stripe.Refund, "list", lambda **kw: SimpleNamespace(auto_paging_iter=lambda: iter([])))
    def create(**kwargs):
        assert kwargs["metadata"] == {"kind": "resale_fill", "fill_id": fid, "position_id": "seller", "nyte_fill_id": fid}
        assert kwargs["idempotency_key"] == "nyte-resale-" + fid
        assert "description" not in kwargs
        refund = {"id": "re_raced", "payment_intent": kwargs["payment_intent"], "amount": kwargs["amount"],
                  "metadata": kwargs["metadata"], "status": "failed"}
        result = client.post("/v1/internal/stripe/webhook", json={"id": "evt_raced", "type": "refund.failed", "data": {"object": refund}})
        assert result.status_code == 200
        return {**refund, "status": "succeeded"}  # Earlier API response arrives last.
    create_mock = Mock(side_effect=create)
    monkeypatch.setattr(resale.stripe.Refund, "create", create_mock)
    resale.process_fill(fid, test_settings)
    resale.process_fill(fid, test_settings)
    create_mock.assert_called_once()
    fill = call(STORE, "pending", queue="refunds_needs_ops")["rows"][0]
    assert fill["status"] == "needs_ops" and fill["stripe_refund_id"] == "re_raced"
    assert fill["history"][-1]["action"] == "stale_write"
    assert call(STORE, "pending")["rows"] == []
    assert resale.alert_ops.called


def test_refund_worker_and_full_ack_hide_stripe_debug_payloads(settings, monkeypatch, caplog):
    import logging

    from stripe import _util

    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    monkeypatch.setattr(resale.stripe, "log", None)
    monkeypatch.setattr(_util, "STRIPE_LOG", None)
    purchase(store, "seller")
    listing(store, "seller")
    fid = purchase(store, "buyer", lots=1, allocate=True)["fills"][0]
    email = "private-refund@example.com"
    def log_payload():
        for logger in ("stripe", "stripe.http_client", "urllib3.connectionpool", "httpx"):
            logging.getLogger(logger).debug("API response description=%s metadata.email=%s", email, email)
    def refund_list(**kwargs):
        log_payload()
        return SimpleNamespace(auto_paging_iter=lambda: iter([]))
    def create(**kwargs):
        log_payload()
        assert email not in str(kwargs)
        assert set(kwargs["metadata"]) == {"kind", "fill_id", "nyte_fill_id", "position_id"}
        assert "description" not in kwargs
        return {"id": "re_private", "payment_intent": "pi_seller", "amount": 95, "status": "pending", "description": email}
    def retrieve(rid, **kwargs):
        log_payload()
        return {"id": rid, "payment_intent": "pi_full" if rid == "re_full" else "pi_seller",
                "amount": 300 if rid == "re_full" else 95, "status": "succeeded", "metadata": {"email": email}}
    listing_mock, create_mock, retrieve_mock = Mock(side_effect=refund_list), Mock(side_effect=create), Mock(side_effect=retrieve)
    monkeypatch.setattr(resale.stripe.Refund, "list", listing_mock)
    monkeypatch.setattr(resale.stripe.Refund, "create", create_mock)
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", retrieve_mock)
    monkeypatch.setattr(resale, "alert_ops", Mock())
    purchase(store, "full")
    call(store, "request_refund", position="full", holder="full", request_id="full", now=101)
    with caplog.at_level(logging.DEBUG):
        resale.process_fill(fid, settings)
        resale.process_fill(fid, settings)
        resale.acknowledge_full_refund("full", "re_full", settings)
        logging.getLogger("stripe").debug("outside-private-call")
    listing_mock.assert_called_once()
    create_mock.assert_called_once()
    assert retrieve_mock.call_count == 2
    assert email not in caplog.text and "outside-private-call" in caplog.text
    assert store.resale_entities["nyte_fill", fid]["status"] == "refunded"
    assert call(store, "position", position="full")["status"] == "refunded"


@pytest.mark.parametrize("setting", ["log", "STRIPE_LOG"])
@pytest.mark.parametrize("level", ["debug", "info"])
def test_refund_calls_refuse_console_diagnostics(settings, monkeypatch, setting, level, capsys):
    from stripe import _util
    monkeypatch.setattr(resale.stripe, "log", None)
    monkeypatch.setattr(_util, "STRIPE_LOG", None)
    monkeypatch.setattr(resale.stripe if setting == "log" else _util, setting, level)
    command = Mock(side_effect=AssertionError("Guard must run before ledger changes"))
    monkeypatch.setattr(resale, "command", command)
    for action in (lambda: resale.process_fill("fill", settings), lambda: resale.acknowledge_full_refund("session", "refund", settings)):
        with pytest.raises(ValueError, match="console diagnostics"):
            action()
    command.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("event_type", ["refund.created", "refund.updated", "refund.failed", "charge.refund.updated"])
@pytest.mark.parametrize("failure_first", [True, False])
def test_full_refund_acknowledgement_and_failure_share_monotonic_rule(client, test_settings, monkeypatch, event_type, failure_first):
    from trusted_router.storage import STORE
    test_settings.resale_enabled = True
    purchase(STORE, "holder")
    call(STORE, "request_refund", position="holder", holder="holder", now=101, request_id="full")
    refund = {"id": "re_full_lifecycle", "amount": 300, "payment_intent": "pi_holder", "status": "succeeded"}
    mail = Mock(return_value=True)
    monkeypatch.setattr(resale, "get_email_service", lambda _: SimpleNamespace(send=mail))
    create = Mock(side_effect=AssertionError("Full refunds must not be reissued"))
    monkeypatch.setattr(resale.stripe.Refund, "create", create)

    def failure_event():
        failed = {**refund, "status": "failed", "description": "private-full@example.com"}
        result = client.post("/v1/internal/stripe/webhook", json={"id": "evt_full_failed", "type": event_type, "data": {"object": failed}})
        assert result.status_code == 200

    def retrieve(*args, **kwargs):
        if failure_first:
            failure_event()  # Before the ops tool has ever bound the refund ID.
        return refund
    monkeypatch.setattr(resale.stripe.Refund, "retrieve", retrieve)
    first = resale.acknowledge_full_refund("holder", refund["id"], test_settings)
    assert first["status"] == ("needs_ops" if failure_first else "refunded")
    if not failure_first:
        failure_event()
    repeated = resale.acknowledge_full_refund("holder", refund["id"], test_settings)
    assert repeated["status"] == "needs_ops"
    assert repeated["history"][-1]["action"] == "stale_write"
    assert call(STORE, "position", position="holder")["status"] == "needs_ops"
    assert call(STORE, "pending", queue="full_refunds")["rows"] == []
    queued = call(STORE, "pending", queue="full_refunds_needs_ops")["rows"]
    assert len(queued) == 1 and queued[0]["stripe_refund_id"] == refund["id"]
    create.assert_not_called()
    assert mail.called
    body = mail.call_args.args[0].text_body
    assert "--needs-ops --full-refunds" in body and refund["id"] in body
    assert "private-full@example.com" not in body


def test_legacy_completed_full_refund_is_reopened_by_exact_payment_mapping(settings, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(resale, "STORE", store)
    purchase(store, "oldfull")
    call(store, "request_refund", position="oldfull", holder="oldfull", request_id="full", now=101)
    refund = {"id": "re_legacy_full", "amount": 300, "payment_intent": "pi_oldfull", "status": "succeeded"}
    call(store, "refund_acknowledge", position="oldfull", owner="ops", refund=refund)
    # Model the pre-round-4 schema: no index, no status on the saved request.
    store.resale_entities.pop(("nyte_refund", refund["id"]))
    store.resale_entities["nyte_refund_request", "oldfull"] = {"position": "oldfull", "requested_at": 101}
    alert = Mock()
    monkeypatch.setattr(resale, "alert_ops", alert)
    assert resale.reconcile_payment_event({"type": "refund.failed", "data": {"object": {**refund, "status": "failed"}}}, settings)
    assert call(store, "pending", queue="full_refunds_needs_ops")["rows"][0]["status"] == "needs_ops"
    alert.assert_called_once()


@pytest.mark.parametrize("kind", ["resale_fill", "full_refund"])
def test_disabled_resale_typed_refunds_do_not_touch_ledger_or_credits(client, test_settings, monkeypatch, kind):
    test_settings.resale_enabled = False
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled NYTE refund reached ledger or credits")
    monkeypatch.setattr(InMemoryStore, "reserved_resale", forbidden)
    monkeypatch.setattr(InMemoryStore, "credit_workspace_typed_direct", forbidden)
    response = client.post("/v1/internal/stripe/webhook", json={"id": "evt_disabled_refund", "type": "refund.created", "data": {"object": {
        "id": "re_disabled", "payment_intent": "pi_disabled", "amount": 100, "status": "succeeded", "metadata": {"kind": kind},
    }}})
    assert response.status_code == 200 and response.json()["data"]["credited"] is False
