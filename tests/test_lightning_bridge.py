from fastapi import FastAPI
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.routes.internal.lightning import register
from trusted_router.routes.lightning_support import register_lightning_support_routes
from trusted_router.security import new_api_key

TOKEN = "lightning-test-only-" + "a" * 32


def client_for(token: str = TOKEN) -> TestClient:
    app = FastAPI()
    register(app)
    register_lightning_support_routes(app)
    app.state.settings = Settings(environment="test", lightning_funding_token=token)
    return TestClient(app)


def test_funding_bridge_commits_real_test_ledger_and_redacts_key() -> None:
    client = client_for()
    headers = {"Authorization": "Bearer " + TOKEN}
    raw = new_api_key()
    response = client.post("/internal/lightning/resolve", headers=headers, json={"api_key": raw, "new": True})
    assert response.status_code == 200
    assert raw not in response.text
    account_id = response.json()["account_id"]
    payment = {"account_id": account_id, "payment_hash": "a" * 64, "amount_microdollars": 1_000_001}
    for _ in range(2):
        assert client.post("/internal/lightning/credit", headers=headers, json=payment).json() == {"committed": True}
    assert client.post("/internal/lightning/balance", headers=headers, json={"account_id": account_id}).json() == {"available_microdollars": 1_000_001}
    summary = client.post("/internal/lightning/account", headers=headers, json={"api_key": raw})
    assert summary.json() == {"account_id": account_id, "available_microdollars": 1_000_001, "support_eligible": True}
    assert raw not in summary.text
    payment["amount_microdollars"] = 2
    assert client.post("/internal/lightning/credit", headers=headers, json=payment).status_code == 409


def test_bridge_disabled_even_in_local_mode_without_dedicated_secret() -> None:
    response = client_for("").post("/internal/lightning/resolve", json={"api_key": new_api_key(), "new": True})
    assert response.status_code == 403


def test_wrong_service_token_cannot_create_accounts_or_credit() -> None:
    client = client_for()
    response = client.post("/internal/lightning/resolve", headers={"Authorization": "Bearer gateway-token"}, json={"api_key": new_api_key(), "new": True})
    assert response.status_code == 401


def test_credit_amount_is_strict_integer() -> None:
    response = client_for().post("/internal/lightning/credit", headers={"Authorization": "Bearer " + TOKEN}, json={"account_id": "workspace", "payment_hash": "a" * 64, "amount_microdollars": 1.1})
    assert response.status_code == 422


def test_max_invoice_rounding_and_overpayment_are_credited_exactly_once() -> None:
    import uuid

    client = client_for()
    headers = {"Authorization": "Bearer " + TOKEN}
    account = client.post("/internal/lightning/resolve", headers=headers,
                          json={"api_key": new_api_key(), "new": True}).json()["account_id"]
    # $1000 at effective $90000/BTC rounded to a whole sat, then paid twice.
    amount = 2_000_001_600
    payment = {"account_id": account, "payment_hash": uuid.uuid4().hex * 2,
               "amount_microdollars": amount}
    for _ in range(2):
        assert client.post("/internal/lightning/credit", headers=headers, json=payment).status_code == 200
    assert client.post("/internal/lightning/balance", headers=headers, json={"account_id": account}).json()["available_microdollars"] == amount


def test_funding_receipt_limit_matches_isolated_service(monkeypatch) -> None:
    from pathlib import Path

    from trusted_router.storage_lightning import MAX_CREDIT_RECEIPT

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "experiments/lightning_router"))
    from lightning_router.money import MAX_CREDIT_RECEIPT as FUNDING_LIMIT
    assert FUNDING_LIMIT == MAX_CREDIT_RECEIPT


def test_funding_token_cannot_reuse_gateway_token() -> None:
    import pytest

    with pytest.raises(ValueError, match="must differ"):
        Settings(environment="test", lightning_funding_token=TOKEN, internal_gateway_token=TOKEN)


def test_readiness_requires_dedicated_token_and_does_not_create_account(monkeypatch) -> None:
    from trusted_router.storage import InMemoryStore

    reads: list[str] = []

    def get_workspace(_self, workspace_id: str):
        reads.append(workspace_id)
        return None

    monkeypatch.setattr(InMemoryStore, "get_workspace", get_workspace)
    client = client_for()
    assert client.post("/internal/lightning/health", headers={"Authorization": "Bearer gateway-token"}).status_code == 401
    assert reads == []
    assert client.post("/internal/lightning/health", headers={"Authorization": "Bearer " + TOKEN}).json() == {"ready": True}
    assert reads == ["ws_lightning_readiness"]
    assert client_for("").post("/internal/lightning/health").status_code == 403


def test_postgres_rejects_noncanonical_lightning_event_before_sql() -> None:
    from datetime import UTC, datetime
    from unittest.mock import Mock

    import pytest

    from trusted_router.storage_models import CreditProvenance
    from trusted_router.storage_postgres import PostgresStore

    conn = Mock()
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="canonical event ID"):
        PostgresStore._insert_credit_trust_event_tx(
            conn, workspace_id="workspace", event_id="arbitrary-id",
            amount_microdollars=1,
            provenance=CreditProvenance("invoice", "lightning", "a" * 64, now),
            recorded_at=now, payment_amount_microdollars=1, currency="USD",
        )
    conn.execute.assert_not_called()


def test_existing_lightning_identity_avoids_another_claim_write() -> None:
    from unittest.mock import Mock

    from trusted_router.storage_lightning import key_record, postgres_key
    from trusted_router.storage_models import Workspace
    from trusted_router.storage_postgres import PostgresStore

    raw = new_api_key()
    key = key_record(raw, Workspace(id="workspace", name="Lightning", owner_user_id="owner"))
    store = Mock(spec=PostgresStore)
    conn = Mock()
    store._run_transaction.side_effect = lambda operation: operation(conn)
    store._read_entity_tx.side_effect = [{"key_id": key.hash}, key]
    assert postgres_key(store, raw) == key
    store._insert_entity_once_tx.assert_not_called()
    store._write_entity_tx.assert_not_called()
    conn.execute.assert_not_called()


def test_provisioning_lock_timeout_never_starts_transaction(monkeypatch) -> None:
    from unittest.mock import Mock

    import pytest

    from trusted_router import storage_lightning
    from trusted_router.storage_errors import StoreConflict
    from trusted_router.storage_postgres import PostgresStore

    lock = Mock()
    lock.acquire.return_value = False
    monkeypatch.setattr(storage_lightning, "_PROVISION_LOCKS", (lock,))
    store = Mock(spec=PostgresStore)
    with pytest.raises(StoreConflict, match="busy"):
        storage_lightning.postgres_key(store, new_api_key())
    lock.acquire.assert_called_once_with(timeout=10)
    lock.release.assert_not_called()
    store._run_transaction.assert_not_called()


def test_failed_provisioning_releases_capacity(monkeypatch) -> None:
    from unittest.mock import Mock

    import pytest

    from trusted_router import storage_lightning
    from trusted_router.storage_postgres import PostgresStore

    lock = Mock()
    lock.acquire.return_value = True
    monkeypatch.setattr(storage_lightning, "_PROVISION_LOCKS", (lock,))
    store = Mock(spec=PostgresStore)
    store._run_transaction.side_effect = ValueError("invalid_lightning_key")
    with pytest.raises(ValueError, match="invalid_lightning_key"):
        storage_lightning.postgres_key(store, new_api_key())
    lock.release.assert_called_once_with()


def test_feedback_uses_verified_identity_and_existing_mail_pipeline(monkeypatch) -> None:
    from unittest.mock import Mock

    from trusted_router.routes import lightning_support as lightning
    from trusted_router.services.lightning import LightningAccount

    mailer = Mock()
    mailer.send.return_value = True
    monkeypatch.setattr(lightning, "get_email_service", lambda _: mailer)
    monkeypatch.setattr(lightning, "_inquiry_rate_ok", lambda _: True)
    monkeypatch.setattr(lightning.LightningCredits, "account", lambda self, raw: LightningAccount("verified-workspace", "verified-user", 0, True))
    raw = new_api_key()
    client = client_for()
    response = client.post("/lightning/feedback", headers={"Authorization": "Bearer " + raw},
                           json={"email": "customer@example.com", "message": "Help with my invoice"})
    assert response.json() == {"sent": True}
    message = mailer.send.call_args.args[0]
    assert message.to == client.app.state.settings.support_email
    assert message.reply_to == "customer@example.com"
    assert message.mail_class == "support_inquiry"
    assert "verified-user" in message.text_body and "verified-workspace" in message.text_body
    assert raw not in str(message) + response.text


def test_feedback_fail_closed_for_unfunded_invalid_and_mail_failure(monkeypatch, caplog) -> None:
    from unittest.mock import Mock

    from trusted_router.routes import lightning_support as lightning
    from trusted_router.services.lightning import LightningAccount

    mailer = Mock()
    monkeypatch.setattr(lightning, "get_email_service", lambda _: mailer)
    monkeypatch.setattr(lightning, "_inquiry_rate_ok", lambda _: True)
    account = LightningAccount("workspace", "user", 0, False)
    monkeypatch.setattr(lightning.LightningCredits, "account", lambda self, raw: account)
    raw = new_api_key()
    client = client_for()
    body = {"email": "customer@example.com", "message": "private feedback"}
    headers = {"Authorization": "Bearer " + raw}
    assert client.post("/lightning/feedback", headers=headers, json=body).status_code == 403
    mailer.send.assert_not_called()
    account = LightningAccount("workspace", "user", 0, True)
    assert client.post("/lightning/feedback", headers=headers, json={**body, "user_id": "spoofed"}).status_code == 422
    for changes in ({"email": "x@y.com\r\nBcc: a@b.com"}, {"message": " "}, {"message": raw}):
        assert client.post("/lightning/feedback", headers=headers, json={**body, **changes}).status_code == 400
    mailer.send.assert_not_called()
    for failure in (False, RuntimeError(raw)):
        mailer.send.return_value = False
        mailer.send.side_effect = failure if isinstance(failure, Exception) else None
        assert client.post("/lightning/feedback", headers=headers, json=body).status_code == 503
        assert raw not in caplog.text and body["message"] not in caplog.text
    assert client.post("/internal/lightning/account", headers={"Authorization": "Bearer wrong-token"}, json={"api_key": raw}).status_code == 401
    assert client.post("/lightning/feedback", json=body).status_code == 401
    monkeypatch.setattr(lightning, "_inquiry_rate_ok", lambda _: False)
    assert client.post("/lightning/feedback", headers=headers, json=body).status_code == 429


def test_feedback_does_not_accept_funding_authority_as_a_customer_key() -> None:
    body = {"email": "customer@example.com", "message": "hello"}
    assert client_for().post("/lightning/feedback", headers={"Authorization": "Bearer " + TOKEN}, json=body).status_code == 401


def test_account_funding_uses_historical_credits_and_rejects_revocation(monkeypatch) -> None:
    from unittest.mock import Mock

    import pytest

    from trusted_router.services import lightning
    from trusted_router.storage_models import ApiKey, ApiKeyAuthContext, Workspace

    workspace = Workspace(id="workspace", name="Lightning", owner_user_id="owner")
    key = Mock(spec=ApiKey, disabled=False, federated_home=None, expires_at=None, creator_user_id="creator")
    store = Mock()
    store.api_key_auth_context.return_value = ApiKeyAuthContext(key, workspace)
    monkeypatch.setattr(lightning, "live_credit_summary", lambda *a, **kw: {"total_credits": 100, "total_usage": 100, "reserved": 0, "available": 0})
    account = lightning.LightningCredits(store).account("raw")
    assert account.available_microdollars == 0 and account.support_eligible
    assert account.user_id == "creator"
    key.creator_user_id = None
    assert lightning.LightningCredits(store).account("raw").user_id == "owner"
    key.disabled = True
    with pytest.raises(ValueError):
        lightning.LightningCredits(store).account("raw")
    key.disabled = False
    workspace.deleted = True
    with pytest.raises(ValueError):
        lightning.LightningCredits(store).account("raw")
