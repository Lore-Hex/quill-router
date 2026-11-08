from __future__ import annotations

from tests.test_wallet_only_billing import _wallet_client
from trusted_router.storage import STORE


def test_wallet_only_api_cannot_register_byok_key() -> None:
    with _wallet_client() as (client, user, workspace, _session):
        raw_key, _ = STORE.create_api_key(
            workspace_id=workspace.id,
            name="wallet-management",
            creator_user_id=user.id,
            management=True,
        )
        response = client.put(
            "/v1/byok/providers/openai",
            headers={"authorization": f"Bearer {raw_key}"},
            json={"api_key": "sk-test-0123456789abcdef"},
        )
        stored = STORE.list_byok_providers(workspace.id)

    assert response.status_code == 403
    assert response.json()["error"]["message"].startswith("Wallet-only accounts cannot register")
    assert stored == []


def test_wallet_only_console_cannot_register_byok_key() -> None:
    with _wallet_client() as (client, _user, workspace, _session):
        response = client.post(
            "/console/byok",
            data={"provider": "openai", "api_key": "sk-test-0123456789abcdef"},
            follow_redirects=False,
        )
        stored = STORE.list_byok_providers(workspace.id)

    assert response.status_code == 303
    assert response.headers["location"] == "/console/byok?error=wallet_only"
    assert stored == []


def test_wallet_user_with_verified_email_can_register_byok_key() -> None:
    with _wallet_client(email="wallet@example.org", email_verified=True) as (
        client,
        _user,
        workspace,
        _session,
    ):
        response = client.post(
            "/console/byok",
            data={"provider": "openai", "api_key": "sk-test-0123456789abcdef"},
            follow_redirects=False,
        )
        stored = STORE.list_byok_providers(workspace.id)

    assert response.status_code == 303
    assert response.headers["location"] == "/console/byok"
    assert [config.provider for config in stored] == ["openai"]
