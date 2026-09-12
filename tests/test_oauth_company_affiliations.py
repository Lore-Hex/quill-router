from __future__ import annotations

import datetime as dt
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.test_oauth_increment_d import APP_ID, REDIRECT, VERIFIER, _approve, _authorize, _setup
from tests.test_oauth_key_delegation import _create_code
from trusted_router.company_affiliations import build_snapshot
from trusted_router.scopes import SCOPE_INFERENCE, SCOPE_PROFILE
from trusted_router.storage import STORE


def _seed() -> None:
    now = dt.datetime.now(dt.UTC)
    docs = build_snapshot([{
        "enabled": "TRUE", "listing_status": "active", "domain": "example.com",
        "company_name": "Example Company", "company_url": "https://example.com",
        "funding_organization": "Y Combinator",
        "directory_url": "https://www.ycombinator.com/companies/example",
        "founding_year": "2020", "founding_year_source": "https://example.com/about",
        "verified_at": now.date().isoformat(),
    }], source_sheet_url="https://docs.google.com/spreadsheets/d/test-directory/edit", now=now)
    STORE.publish_company_affiliation_documents(docs, expected_revision=None)


def test_pkce_exchange_and_userinfo_share_verified_company_metadata(client: TestClient, user_headers: dict[str, str]) -> None:
    _seed()
    verifier = "test-verifier-" + "c" * 43
    code, _ = _create_code(client, user_headers, verifier=verifier)
    alice = STORE.find_user_by_email("alice@example.com")
    STORE.mark_user_email_verified(alice.id)
    response = client.post("/v1/auth/keys", json={"code": code, "code_verifier": verifier})
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    identity = response.json()["identity"]
    assert identity["company_affiliations"][0]["funding_organization"] == "Y Combinator"
    assert identity["company_affiliations"][0]["founding_year"] == 2020
    assert identity["verification_level"] == "email"
    info = client.get("/v1/auth/userinfo", headers={"authorization": "Bearer " + response.json()["key"]})
    assert info.json()["data"] == identity
    assert "no-store" in info.headers.get("cache-control", "")


def test_unverified_identity_and_inference_only_keys_never_disclose_affiliations(client: TestClient) -> None:
    _seed()
    user = STORE.ensure_user("unverified@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    key, _ = STORE.create_api_key(workspace_id=workspace.id, name="profile", creator_user_id=user.id, management=False, scopes=[SCOPE_PROFILE])
    response = client.get("/v1/auth/userinfo", headers={"authorization": "Bearer " + key})
    assert response.status_code == 200
    assert "company_affiliations" not in response.json()["data"]
    STORE.mark_user_email_verified(user.id)
    inference, _ = STORE.create_api_key(workspace_id=workspace.id, name="inference", creator_user_id=user.id, management=False, scopes=[SCOPE_INFERENCE])
    assert client.get("/v1/auth/userinfo", headers={"authorization": "Bearer " + inference}).status_code == 403


def test_changed_email_loses_old_affiliation(client: TestClient) -> None:
    _seed()
    user = STORE.ensure_user("verified@example.com")
    STORE.mark_user_email_verified(user.id)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    key, _ = STORE.create_api_key(workspace_id=workspace.id, name="profile", creator_user_id=user.id, management=False, scopes=[SCOPE_PROFILE])
    headers = {"authorization": "Bearer " + key}
    assert client.get("/v1/auth/userinfo", headers=headers).json()["data"]["company_affiliations"]
    STORE.set_user_email(user.id, "verified@other.example")
    assert "company_affiliations" not in client.get("/v1/auth/userinfo", headers=headers).json()["data"]


@pytest.mark.parametrize("scope", ["inference", "inference profile"])
def test_registered_oauth_grants_disclose_only_with_profile_scope(client: TestClient, scope: str) -> None:
    _seed()
    _setup(client)
    user = STORE.find_user_by_email("alice@example.com")
    assert user is not None
    STORE.mark_user_email_verified(user.id)
    page = _authorize(client, scope=scope)
    approved = _approve(client, page)
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    token = client.post("/v1/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "client_id": APP_ID, "redirect_uri": REDIRECT})
    assert token.status_code == 200
    assert ("company_affiliations" in token.json()["trustedrouter"]) == ("profile" in scope)


def test_slow_directory_cannot_block_signin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import trusted_router.verification as verification

    _seed()
    user = STORE.ensure_user("slow@example.com")
    STORE.mark_user_email_verified(user.id)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    key, _ = STORE.create_api_key(workspace_id=workspace.id, name="profile", creator_user_id=user.id, management=False, scopes=[SCOPE_PROFILE])
    reads = []
    release = threading.Event()
    finished = threading.Event()

    def slow_read(key: str):
        reads.append(key)
        release.wait(5)
        finished.set()
        return None

    monkeypatch.setattr(STORE.target, "get_company_affiliation_document", slow_read)
    monkeypatch.setattr(verification, "AFFILIATION_TIMEOUT_SECONDS", 0.02)
    headers = {"authorization": "Bearer " + key}
    try:
        for _ in range(2):
            response = client.get("/v1/auth/userinfo", headers=headers)
            assert response.status_code == 200
            assert "company_affiliations" not in response.json()["data"]
        assert not finished.is_set(), "Sign-in waited for optional directory read"
        assert len(reads) == 1
    finally:
        release.set()
