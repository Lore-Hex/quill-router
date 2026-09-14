import uuid

import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app


class Catalog:
    def current(self):
        return [{"id": "deepseek/deepseek-flash", "name": "DeepSeek Flash", "context": 128000, "output": 4096}]


@pytest.fixture
def client(funding):
    with TestClient(create_app(funding, rates=funding.rates, catalog=Catalog(), network="regtest", start_worker=False)) as result:
        yield result


def headers(key):
    return {"Authorization": "Bearer " + key, "Idempotency-Key": uuid.uuid4().hex}


def test_no_mainnet_deposits_before_inference_billing(funding):
    with pytest.raises(RuntimeError, match="USD credit delivery"):
        create_app(funding)


def test_closed_launch_accepts_no_money(raw_key):
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["payments_ready"] is False
        assert client.post("/api/invoices", headers=headers(raw_key), json={"new_account": True, "usd_cents": 1000}).status_code == 503


def test_invoice_balance_and_auth_contract(client, funding, raw_key):
    response = client.post("/api/invoices", headers=headers(raw_key), json={"new_account": True, "usd_cents": 1000})
    assert response.status_code == 200
    invoice = response.json()
    assert invoice["requested_msat"] == "10000000"
    assert invoice["qr"].startswith("data:image/png;base64,")
    assert raw_key not in response.text
    assert invoice["account_created"] is False
    assert client.get("/api/account", headers=headers(raw_key)).status_code == 401
    assert funding.credits.balances == {}
    assert client.get("/api/account").status_code == 401
    record = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
    funding.lnd.pay(record["payment_hash"])
    paid = client.post(f"/api/invoices/{invoice['id']}/refresh", json={}, headers=headers(raw_key))
    assert paid.json()["credited"] is True
    funding.rates.fail = True
    balance = client.get("/api/account", headers=headers(raw_key)).json()
    assert balance["balance_microdollars"] == "10000000"
    assert balance["balance_usd"] == "10.000000"
    assert balance["active_invoice"] is None
    assert "usd_estimate" not in balance
    assert "balance_btc" not in balance


@pytest.mark.parametrize("body", [{"usd_cents": 1.2}, {"usd_cents": True}, {"usd_cents": 0}, {"usd_cents": 100001}, {"usd_cents": 100, "settled": True}, {"usd_cents": 100, "new_account": "yes"}])
def test_client_cannot_forge_money(client, raw_key, body):
    assert client.post("/api/invoices", json=body, headers=headers(raw_key)).status_code == 400


def test_invoices_need_key_not_just_hash(client, raw_key):
    invoice = client.post("/api/invoices", headers=headers(raw_key), json={"new_account": True, "usd_cents": 1000}).json()
    assert client.post(f"/api/invoices/{invoice['id']}/refresh", json={}).status_code == 404
    assert client.post(f"/api/invoices/{invoice['id']}/cancel", json={}).status_code == 404


def test_browser_security_headers(client):
    response = client.get("/")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert 'name="username"' not in response.text.lower()
    assert "type=\"email\"" not in response.text


def test_cross_origin_and_unbounded_body_blocked(client, raw_key):
    assert client.post("/api/invoices", headers={**headers(raw_key), "Origin": "https://evil.test"}, json={"usd_cents": 1000}).status_code == 403
    assert client.post("/api/invoices", headers=headers(raw_key), content="x" * 1025).status_code == 413


def test_api_keys_do_not_expose_admin_or_inference_paths(client, raw_key):
    for path in ["/api/settle", "/api/credit", "/v1/chat/completions", "/v1/wallet/send", "/v1/gateway/authorize"]:
        assert client.post(path, headers=headers(raw_key), json={}).status_code == 404


def test_private_exception_never_reaches_server_traceback(client, funding, raw_key, monkeypatch, caplog):
    def unavailable(_):
        raise RuntimeError("private SQL parameters " + raw_key)
    monkeypatch.setattr(funding, "account", unavailable)
    # TestClient defaults to re-raising unhandled server exceptions. A generic
    # FastAPI Exception handler alone would still raise here after its response.
    response = client.get("/api/account", headers=headers(raw_key))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "10"
    assert raw_key not in response.text + caplog.text
    assert "private SQL" not in caplog.text
    assert "RuntimeError" in caplog.text
