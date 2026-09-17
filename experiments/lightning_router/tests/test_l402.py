import copy
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app
from lightning_router.credentials import Credentials
from lightning_router.l402 import LOCATION, PATH, PROOF_LIFETIME, L402Funding
from lightning_router.lexe import Lexe
from lightning_router.store import Store, deposits
from pymacaroons import Macaroon, Verifier
from sqlalchemy import select

from .test_lexe import WALLET, Node


@pytest.fixture(params=["lnd", "lexe"])
def setup(funding, raw_key, request):
    node = Node()
    if request.param == "lexe":
        funding.lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393", transport=httpx.MockTransport(node.handle)), WALLET)
        funding.new_invoice_backend = "lexe"
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        yield client, funding, node, {"X-API-Key": raw_key, "Idempotency-Key": uuid.uuid4().hex}


BODY = {"usd_cents": 100, "new_account": True}


def challenge(setup, body=None):
    client, funding, _, headers = setup
    response = client.post(PATH, headers=headers, json=body or BODY)
    assert response.status_code == 402, response.text
    row = funding.store.by_request(funding.credentials.fingerprint(headers["X-API-Key"]), headers["Idempotency-Key"])
    return response, row


def proof(setup, response, row, *, pay=True):
    _, funding, node, headers = setup
    if row["backend"] == "lexe":
        preimage = node.preimages[row["provider_index"]]
        if pay:
            node.pay(row["provider_index"])
    else:
        preimage = funding.credentials.invoice_preimage(row["id"]).hex()
        if pay:
            funding.lnd.pay(row["payment_hash"])
    return {**headers, "Authorization": "L402 " + response.json()["macaroon"] + ":" + preimage}


def test_challenge_is_standard_macaroon_and_does_not_provision(setup, caplog):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    data = response.json()
    assert response.headers["www-authenticate"] == f'L402 macaroon="{data["macaroon"]}", invoice="{data["bolt11"]}"'
    token = Macaroon.deserialize(data["macaroon"])
    assert token.location == LOCATION and token.identifier == row["id"]
    verifier = Verifier()
    for caveat in token.caveats:
        verifier.satisfy_exact(caveat.caveat_id)
    assert verifier.verify(token, funding.credentials.l402_root_key())
    assert data["proof_expires_at"] == row["created_at"] + PROOF_LIFETIME
    assert "qr" not in data and not data["credited"] and not data["account_created"]
    assert not funding.credits.balances and not funding.credits.payments
    assert headers["X-API-Key"] not in response.text + caplog.text + str(token.inspect())
    for _ in range(2):
        again = client.post(PATH, headers=headers, json=BODY)
        assert again.json()["macaroon"] == data["macaroon"]
        assert again.json()["bolt11"] == data["bolt11"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("lost_ack", [False, True])
def test_verified_settlement_replays_exactly_once_across_restart(setup, lost_ack, monkeypatch):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    funding.credits.fail_after_commit = lost_ack
    first = client.post(PATH, headers=authorization, json=BODY)
    assert first.status_code == (503 if lost_ack else 200)
    funding.store = Store(funding.store.engine.url)
    monkeypatch.setattr(time, "time", lambda: row["created_at"] + 120)
    for _ in range(3):
        result = client.post(PATH, headers=authorization, json=BODY)
        assert result.status_code == 200, result.text
        assert result.json()["credit_usd"] == "1.000000"
        assert result.json()["credited"] is True
        assert result.json()["balance_usd"] == "1.000000"
    assert len(funding.credits.balances) == len(funding.credits.payments) == 1
    with funding.store.transaction() as conn:
        assert len(conn.execute(select(deposits)).all()) == 1
    assert client.get("/api/account", headers={"Authorization": "Bearer " + headers["X-API-Key"]}).json()["balance_usd"] == "1.000000"


def test_preimage_alone_cannot_claim_an_unsettled_payment(setup):
    client, funding, _, _ = setup
    response, row = challenge(setup)
    result = client.post(PATH, headers=proof(setup, response, row, pay=False), json=BODY)
    assert result.status_code == 202 and result.headers["retry-after"] == "10"
    assert "www-authenticate" not in result.headers
    assert not funding.credits.balances and not funding.credits.payments


def test_existing_key_topup_and_browser_interoperate(setup):
    client, funding, _, headers = setup
    account = funding.credits.resolve(headers["X-API-Key"], new=True)
    funding.credits.balances[account] = 2000000
    body = {"usd_cents": 100, "new_account": False}
    response, row = challenge(setup, body)
    authorization = proof(setup, response, row)
    funding.reconcile()
    assert client.post(PATH, headers=authorization, json=body).json()["balance_usd"] == "3.000000"
    result = client.post(f'/api/invoices/{row["id"]}/refresh', headers={"Authorization": "Bearer " + headers["X-API-Key"]}, json={})
    assert result.json()["credited"] and len(funding.credits.payments) == 1


@pytest.mark.parametrize("header", ["Bearer secret", "L402 broken", "L402 :::", "L402 " + "a" * 4096,
                                   "L402 abc:" + "0" * 64, "L402 abc:" + "g" * 64])
def test_bad_proof_does_not_create_invoice(setup, header, caplog):
    client, funding, node, headers = setup
    result = client.post(PATH, headers={**headers, "Authorization": header}, json=BODY)
    assert result.status_code == 401
    assert funding.lnd.creates == node.creates == 0
    assert not funding.credits.balances and not funding.credits.payments
    assert header not in caplog.text


@pytest.mark.parametrize("change", ["preimage", "signature", "owner", "amount", "request", "new", "purpose", "unknown_caveat", "missing_caveat", "location", "identifier"])
def test_proof_is_bound_to_invoice_and_destination(setup, change):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row, pay=False)
    body = dict(BODY)
    token_text, preimage = authorization["Authorization"][5:].split(":")
    token = Macaroon.deserialize(token_text)
    if change == "preimage":
        preimage = "0" * 64
    elif change == "signature":
        token.signature = "0" * 64
    elif change == "owner":
        authorization["X-API-Key"] = "sk-tr-v1-" + secrets.token_urlsafe(32)
    elif change == "amount":
        body["usd_cents"] = 200
    elif change == "request":
        authorization["Idempotency-Key"] = uuid.uuid4().hex
    elif change == "new":
        body["new_account"] = False
    elif change == "purpose":
        token.caveats[0].caveat_id = "purpose = free-inference"
    elif change == "unknown_caveat":
        token.add_first_party_caveat("anything = accepted")
    elif change == "missing_caveat":
        token.caveats.pop()
    elif change == "location":
        token.location = "https://evil.invalid"
    elif change == "identifier":
        token.identifier = uuid.uuid4().hex
    authorization["Authorization"] = "L402 " + token.serialize() + ":" + preimage
    result = client.post(PATH, headers=authorization, json=body)
    assert result.status_code == (409 if change == "amount" else 401)
    assert not funding.credits.balances and not funding.credits.payments
    assert headers["X-API-Key"] not in result.text


def test_expiry_is_separate_from_invoice_expiry(setup, monkeypatch):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    monkeypatch.setattr(time, "time", lambda: row["expires_at"] + 60)
    assert client.post(PATH, headers=authorization, json=BODY).status_code == 200
    monkeypatch.setattr(time, "time", lambda: row["created_at"] + PROOF_LIFETIME)
    assert client.post(PATH, headers=authorization, json=BODY).status_code == 401
    # A token expiry never loses paid credit or locks out normal key recovery.
    assert client.get("/api/account", headers={"Authorization": "Bearer " + headers["X-API-Key"]}).json()["balance_usd"] == "1.000000"
    assert len(funding.credits.payments) == 1


def test_revocation_blocks_proof_even_after_payment(setup):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    funding.reconcile()
    funding.credits.revoked.add(funding.credits.resolve(headers["X-API-Key"], new=False))
    assert client.post(PATH, headers=authorization, json=BODY).status_code == 401
    assert len(funding.credits.payments) == 1


def test_receiving_outage_does_not_block_durable_credit_recovery(setup, monkeypatch):
    _, funding, node, _ = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    funding.credits.fail_before_commit = True
    assert funding.reconcile()["failed"] == 1
    funding.credits.fail_before_commit = False
    node.broken = True
    monkeypatch.setattr(time, "time", lambda: row["created_at"] + 120)
    with TestClient(create_app(funding, network="regtest", start_worker=False, readiness=lambda: False)) as client:
        assert client.post(PATH, headers=authorization, json=BODY).status_code == 200


def test_canceled_invoice_is_not_replaced_or_charged(setup):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row, pay=False)
    funding.refresh(row, cancel=True)
    for h in (headers, authorization):
        result = client.post(PATH, headers=h, json=BODY)
        assert result.status_code == 410 and "www-authenticate" not in result.headers
    assert not funding.credits.balances and not funding.credits.payments


def test_concurrent_redemption_credits_once(setup):
    _, funding, _, headers = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    def redeem(_):
        return L402Funding(funding).respond(headers["X-API-Key"], headers["Idempotency-Key"], 100,
                                            new=True, authorization=authorization["Authorization"]).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(redeem, range(12))) == [200] * 12
    assert len(funding.credits.payments) == 1 and list(funding.credits.balances.values()) == [1000000]


def test_backend_settlement_is_still_verified(setup):
    client, funding, node, _ = setup
    response, row = challenge(setup)
    authorization = proof(setup, response, row)
    if row["backend"] == "lexe":
        node.rows[row["provider_index"]]["preimage"] = "0" * 64
        assert client.post(PATH, headers=authorization, json=BODY).status_code == 503
        assert not funding.credits.balances
    else:
        corrupted = copy.copy(row)
        corrupted["payment_hash"] = "0" * 64
        assert not L402Funding(funding).verify(authorization["Authorization"], corrupted, True)


def test_funding_credentials_cannot_authorize_inference(setup):
    client, funding, _, _ = setup
    response, row = challenge(setup)
    auth = proof(setup, response, row, pay=False)
    for path in ("/v1/chat/completions", "/v1/responses", "/api/credit", "/api/settle"):
        assert client.post(path, headers=auth, json={}).status_code == 404
    assert funding.credentials.l402_root_key() != funding.credentials.invoice_preimage(row["id"])
    funding.credentials = Credentials(b"different-deployment-secret" + b"x" * 32)
    assert not L402Funding(funding).verify(auth["Authorization"], row, True)


def test_rate_limit_is_shared_and_no_secrets_in_errors(setup, caplog):
    client, funding, _, headers = setup
    response, row = challenge(setup)
    auth = proof(setup, response, row, pay=False)
    while funding.store.rate_limit("l402:" + row["key_hash"], int(time.time()), 60):
        pass
    result = client.post(PATH, headers=auth, json=BODY)
    assert result.status_code == 429 and result.headers["retry-after"]
    assert headers["X-API-Key"] not in caplog.text
    assert auth["Authorization"] not in caplog.text


@pytest.mark.parametrize("body", [{"usd_cents": True}, {"usd_cents": 1.1}, {"usd_cents": 0},
                                 {"usd_cents": 100001}, {"usd_cents": 100, "settled": True}])
def test_invalid_requests_have_no_payment_effect(setup, body):
    client, funding, node, headers = setup
    assert client.post(PATH, headers=headers, json=body).status_code == 400
    assert funding.lnd.creates == node.creates == 0


def test_funding_protocol_is_opt_in_discovery(funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        assert client.post(PATH, json=BODY).status_code == 401
        assert client.post(PATH, headers={"X-API-Key": "sk-tr-v1-" + secrets.token_urlsafe(32)}, json=BODY).status_code == 400
        assert client.get(PATH).status_code == 405
        assert PATH not in client.get("/openapi.json").json()["paths"]
        assert PATH in client.get("/funding-openapi.json").json()["paths"]
        guide = client.get("/l402.md")
        assert guide.status_code == 200 and "X-API-Key" in guide.text
    assert funding.lnd.creates == 0


@pytest.mark.parametrize("authorization", ["L402 abc:" + "0" * 64, "L402 :", "L402 " + "A" * 200 + ":" + "0" * 64])
def test_malformed_macaroon_on_existing_invoice_is_not_a_server_error(setup, authorization, caplog):
    client, funding, _, headers = setup
    challenge(setup)
    result = client.post(PATH, headers={**headers, "Authorization": authorization}, json=BODY)
    assert result.status_code == 401 and not caplog.text
    assert not funding.credits.balances


def test_validation_and_origin_checks_precede_invoice_creation(setup):
    client, funding, node, headers = setup
    assert client.post(PATH, headers={**headers, "Origin": "https://evil.invalid"}, json=BODY).status_code == 403
    assert client.post(PATH, headers=headers, content="x" * 1025).status_code == 413
    assert client.post(PATH, headers=headers, json={"usd_cents": 100}).status_code == 401
    assert funding.lnd.creates == node.creates == 0 and not funding.credits.balances


def test_invoice_creation_keeps_existing_key_limit(setup):
    client, funding, _, headers = setup
    hashed = funding.credentials.fingerprint(headers["X-API-Key"])
    while funding.store.rate_limit("key:" + hashed, int(time.time())):
        pass
    assert client.post(PATH, headers=headers, json=BODY).status_code == 429
    assert not funding.credits.balances


def test_upstream_failure_logs_no_payment_secrets(setup, monkeypatch, caplog):
    client, funding, _, _ = setup
    response, row = challenge(setup)
    auth = proof(setup, response, row)
    secret = auth["Authorization"]
    def fail(_):
        raise RuntimeError(secret)
    monkeypatch.setattr(funding, "refresh", fail)
    result = client.post(PATH, headers=auth, json=BODY)
    assert result.status_code == 503
    assert "RuntimeError" in caplog.text
    assert secret not in result.text + caplog.text
    assert auth["X-API-Key"] not in result.text + caplog.text


def test_disabled_launch_never_creates_l402_challenge():
    with TestClient(create_app()) as client:
        result = client.post(PATH, json=BODY)
        assert result.status_code == 503 and "www-authenticate" not in result.headers
