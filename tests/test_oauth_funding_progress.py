from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup, Tag
from fastapi.testclient import TestClient

from trusted_router.storage import STORE, OAuthApp


@pytest.fixture(params=["legacy", "registered"])
def consent_page(client: TestClient, request: pytest.FixtureRequest) -> tuple[BeautifulSoup, str]:
    user = STORE.ensure_user("funding-progress@example.com", trial_credit_microdollars=0)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw)
    callback = "https://app.example.com/callback"
    if request.param == "registered":
        STORE.set_user_identity_status(user.id, status="approved", verified_name="Example Developer")
        STORE.create_oauth_app(OAuthApp(
            id="funding-progress", owner_user_id=user.id, name="Pizza Ninjas wiki coach",
            redirect_uris=[callback], markup_basis_points=500,
        ))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 43).digest()).decode().rstrip("=")
        response = client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": "funding-progress", "redirect_uri": callback,
            "scope": "inference profile", "state": "preserved-state",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
    else:
        response = client.get("/auth", params={
            "callback_url": callback, "key_label": "Pizza Ninjas wiki coach",
            "limit": "5", "usage_limit_type": "monthly",
        })
    assert response.status_code == 200, response.text
    return BeautifulSoup(response.text, "html.parser"), workspace.id


def _element(page: BeautifulSoup, selector: str) -> Tag:
    element = page.select_one(selector)
    assert isinstance(element, Tag), selector
    return element


def _resume(client: TestClient, page: BeautifulSoup, checkout: str) -> BeautifulSoup:
    consent = str(_element(page, 'input[name="consent"]')["value"])
    response = client.get("/auth", params={"consent": consent, "checkout": checkout})
    assert response.status_code == 200, response.text
    resumed = BeautifulSoup(response.text, "html.parser")
    for name in ("consent", "csrf_token"):
        assert _element(resumed, f'input[name="{name}"]')["value"] == _element(page, f'input[name="{name}"]')["value"]
    return resumed


def test_unfunded_consent_opens_credit_step(consent_page: tuple[BeautifulSoup, str]) -> None:
    page, _ = consent_page
    assert _element(page, "details.funding-step").has_attr("open")
    assert "Add credits" in _element(page, "summary").get_text()
    assert "Credits ready" not in page.get_text()
    assert _element(page, 'input[name="fund_amount"]:checked')["value"] == "20"
    assert _element(page, 'form[action="/auth/approve"] button').has_attr("type")


@pytest.mark.parametrize("checkout", ["", "success", "cancel", "mock"])
def test_funded_consent_collapses_credit_step(
    client: TestClient, consent_page: tuple[BeautifulSoup, str], checkout: str,
) -> None:
    page, workspace_id = consent_page
    STORE.credit_workspace_once(workspace_id, 20_000_000, "funding-progress-credit")
    page = _resume(client, page, checkout)
    assert not _element(page, "details.funding-step").has_attr("open")
    summary = _element(page, "summary").get_text(" ", strip=True)
    assert "Step 1 complete" in summary
    assert "Credits ready" in summary
    assert "$20.00 available" in summary
    assert "Add more" in summary
    assert "Stripe is confirming" not in page.get_text()
    assert "Waiting for payment confirmation" not in page.get_text()
    assert "Final step" in page.get_text()
    assert "emphasized" in _element(page, '[aria-labelledby="approve-heading"]').get("class", [])
    assert "secondary" not in _element(page, 'form[action="/auth/approve"] button').get("class", [])
    assert _element(page, 'input[name="fund_amount"]:checked')["value"] == "20"
    # Funding does not change the user's app budget, disclosures, or consent token.
    if page.select_one('input[name="monthly_budget"]'):
        assert _element(page, 'input[name="monthly_budget"]:checked')["value"] == "20"
        assert "This app adds 5%" in page.get_text()
    else:
        assert _element(page, 'input[name="limit"]')["value"] == "5"
        assert _element(page, 'select[name="usage_limit_type"] option:checked')["value"] == "monthly"
    assert "You can revoke the key" in page.get_text()


def test_success_query_does_not_complete_pending_payment(
    client: TestClient, consent_page: tuple[BeautifulSoup, str],
) -> None:
    page, _ = consent_page
    pending = _resume(client, page, "success")
    assert _element(pending, "details.funding-step").has_attr("open")
    assert "Credits ready" not in pending.get_text()
    assert "Waiting for payment confirmation" in pending.get_text()
    assert not pending.select('form[action="/auth/fund"]')
    refresh = _element(pending, 'a[data-check-credits]')
    url = urlsplit(str(refresh["href"]))
    assert url.path == "/auth"
    assert set(parse_qs(url.query)) == {"consent", "checkout"}
    assert parse_qs(url.query)["consent"] == [str(_element(page, 'input[name="consent"]')["value"])]
    assert "secondary" in _element(pending, 'form[action="/auth/approve"] button').get("class", [])


@pytest.mark.parametrize("checkout", ["cancel", "mock"])
def test_unpaid_checkout_return_keeps_funding_open(
    client: TestClient, consent_page: tuple[BeautifulSoup, str], checkout: str,
) -> None:
    page, _ = consent_page
    page = _resume(client, page, checkout)
    assert _element(page, "details.funding-step").has_attr("open")
    assert "Credits ready" not in page.get_text()
    assert page.select('form[action="/auth/fund"]')


def test_check_credits_reads_new_balance_without_consuming_consent(
    client: TestClient, consent_page: tuple[BeautifulSoup, str],
) -> None:
    page, workspace_id = consent_page
    pending = _resume(client, page, "success")
    STORE.credit_workspace_once(workspace_id, 20_000_000, "funding-progress-credit")
    response = client.get(str(_element(pending, 'a[data-check-credits]')["href"]))
    assert response.status_code == 200
    ready = BeautifulSoup(response.text, "html.parser")
    assert not _element(ready, "details.funding-step").has_attr("open")
    consent_id = str(_element(ready, 'input[name="consent"]')["value"])
    assert not STORE.get_consent_request(consent_id).consumed_at
    assert _element(ready, 'input[name="csrf_token"]')["value"] == _element(page, 'input[name="csrf_token"]')["value"]
