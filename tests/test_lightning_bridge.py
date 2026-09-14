from fastapi import FastAPI
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.routes.internal.lightning import register
from trusted_router.security import new_api_key

TOKEN = "lightning-test-only-" + "a" * 32


def client_for(token: str = TOKEN) -> TestClient:
    app = FastAPI()
    register(app)
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


def test_funding_token_cannot_reuse_gateway_token() -> None:
    import pytest

    with pytest.raises(ValueError, match="must differ"):
        Settings(environment="test", lightning_funding_token=TOKEN, internal_gateway_token=TOKEN)


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
