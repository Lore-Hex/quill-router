import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app
from lightning_router.lookup import LookupGate

PRODUCTION_DELAY = LookupGate.delay

@pytest.fixture
def client(funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        yield client


def fund(funding, key):
    invoice = funding.create(key, uuid.uuid4().hex, 100, new=True)
    row = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(key))
    funding.lnd.pay(row["payment_hash"])
    funding.reconcile()


def test_feedback_requires_funding_and_survives_spent_balance(client, funding, raw_key):
    headers = {"Authorization": "Bearer " + raw_key}
    body = {"email": "customer@example.com", "message": "Please help with my invoice."}
    assert client.post("/api/feedback", headers=headers, json=body).status_code == 401
    account = funding.credits.resolve(raw_key, new=True)
    assert client.get("/api/account", headers=headers).json()["support_eligible"] is False
    assert client.post("/api/feedback", headers=headers, json=body).status_code == 403
    fund(funding, raw_key)
    funding.credits.balances[account] = 0
    response = client.get("/api/account", headers=headers)
    assert response.json()["support_eligible"] is True
    assert account not in response.text
    assert client.post("/api/feedback", headers=headers, json=body).json() == {"sent": True}
    assert funding.credits.feedback_messages == [{"account_id": account, **body}]
    funding.credits.revoked.add(account)
    assert client.post("/api/feedback", headers=headers, json=body).status_code == 401
    assert len(funding.credits.feedback_messages) == 1


@pytest.mark.parametrize("body", [
    {"email": "bad", "message": "hello"}, {"email": "a@b.com\r\nBcc: x@y.com", "message": "hello"},
    {"email": "a@b.com", "message": " "}, {"message": "hello"},
    {"email": "a@b.com", "message": "sk-tr-v1-secret"},
    {"email": "a@b.com", "message": "hello", "user_id": "spoofed"},
    {"email": "a@b.com", "message": "hello", "account_id": "spoofed"},
])
def test_feedback_rejects_invalid_content_and_identity(client, funding, raw_key, body):
    fund(funding, raw_key)
    assert client.post("/api/feedback", headers={"Authorization": "Bearer " + raw_key}, json=body).status_code == 400
    assert not funding.credits.feedback_messages


def test_feedback_rate_limit_delivery_failure_and_redaction(client, funding, raw_key, monkeypatch, caplog):
    fund(funding, raw_key)
    body = {"email": "customer@example.com", "message": "private feedback"}
    headers = {"Authorization": "Bearer " + raw_key}

    def failed(*args):
        raise RuntimeError("private " + raw_key)
    monkeypatch.setattr(funding.credits, "feedback", failed)
    for _ in range(3):
        response = client.post("/api/feedback", json=body, headers=headers)
        assert response.status_code == 503
        assert "private" not in response.text + caplog.text
        assert raw_key not in response.text + caplog.text
    assert client.post("/api/feedback", json=body, headers=headers).status_code == 429
    assert not funding.credits.feedback_messages


def test_feedback_same_origin_body_limit(client, raw_key):
    headers = {"Authorization": "Bearer " + raw_key}
    assert client.post("/api/feedback", json={}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/feedback", content="x" * 16385, headers=headers).status_code == 413


def test_lookup_rate_limit_is_shared_across_endpoints(client, funding, raw_key, monkeypatch):
    identities = []

    def limited(identity, now, maximum=10):
        identities.append(identity)
        return False
    monkeypatch.setattr(funding.store, "rate_limit", limited)
    for path in ["/api/account", "/api/usage"]:
        response = client.get(path, headers={"Authorization": "Bearer " + raw_key})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "900"
    assert len(set(identities)) == 1


def test_jitter_range_and_bounded_admission(monkeypatch):
    from lightning_router import lookup
    gate = LookupGate(capacity=1)
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(lookup.asyncio, "sleep", sleep)
    for value in [0, 1000, 2000]:
        monkeypatch.setattr(lookup.secrets, "randbelow", lambda n, value=value: value)
        asyncio.run(PRODUCTION_DELAY(gate))
    assert waits == [3, 4, 5]
    assert gate.slots.acquire(blocking=False)
    assert not gate.slots.acquire(blocking=False)
    gate.slots.release()
    assert gate.slots.acquire(blocking=False)


def test_delay_precedes_valid_invalid_and_missing_credentials(client, funding, raw_key, monkeypatch):
    events = []
    async def delay(self):
        events.append("delay")
    monkeypatch.setattr(LookupGate, "delay", delay)
    fund(funding, raw_key)
    for key in [raw_key, "wrong", None]:
        client.get("/api/account", headers={"Authorization": "Bearer " + key} if key else {})
    assert events == ["delay"] * 3


def test_saturated_lookup_gate_does_not_queue_or_block_health(funding, monkeypatch):
    from lightning_router import app
    gate = LookupGate(capacity=1)
    assert gate.slots.acquire(blocking=False)
    monkeypatch.setattr(app, "LookupGate", lambda: gate)
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        assert client.get("/api/account").status_code == 429
        assert client.get("/health").status_code == 200
        gate.slots.release()
        assert client.get("/api/account").status_code == 401
        assert gate.slots.acquire(blocking=False)
        gate.slots.release()
