"""Render real consent responses using only an isolated in-memory test account."""

import json

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, OAuthApp


def render() -> dict[str, str]:
    client = TestClient(create_app(Settings(environment="test", storage_backend="memory", sentry_dsn=None), init_observability=False))
    user = STORE.ensure_user("consent-preview@example.com", trial_credit_microdollars=0)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.set_user_identity_status(user.id, status="approved", verified_name="Example Developer")
    STORE.create_oauth_app(OAuthApp(
        id="pizza-ninjas", owner_user_id=user.id, name="Pizza Ninjas wiki coach",
        redirect_uris=["https://app.example.com/callback"], markup_basis_points=500,
    ))
    raw, _ = STORE.create_auth_session(user_id=user.id, provider="google", label=user.email, ttl_seconds=3600, state="active")
    client.cookies.set("tr_session", raw)
    pages = {}
    consents = {}
    for flow in ("legacy", "registered"):
        params = {"callback_url": "https://app.example.com/callback", "key_label": "Pizza Ninjas wiki coach", "limit": "5", "usage_limit_type": "monthly"}
        if flow == "registered":
            params["client_id"] = "pizza-ninjas"
        response = client.get("/auth", params=params)
        assert response.status_code == 200
        pages[f"{flow}-new"] = response.text
        consent = BeautifulSoup(response.text, "html.parser").select_one('input[name="consent"]')
        assert consent is not None
        consents[flow] = str(consent["value"])
        response = client.get("/auth", params={"consent": consents[flow], "checkout": "success"})
        assert response.status_code == 200
        pages[f"{flow}-pending"] = response.text
    STORE.credit_workspace_once(workspace.id, 20_000_000, "preview-only-credit")
    for flow, consent_id in consents.items():
        response = client.get("/auth", params={"consent": consent_id, "checkout": "success"})
        assert response.status_code == 200
        pages[f"{flow}-funded"] = response.text
    return pages


if __name__ == "__main__":
    print(json.dumps(render()))
