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
