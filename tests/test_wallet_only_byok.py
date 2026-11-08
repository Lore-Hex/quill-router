from __future__ import annotations

from tests.test_wallet_only_billing import _wallet_client
from trusted_router.storage import STORE, Member


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


def test_wallet_only_derived_management_key_cannot_register_byok_key() -> None:
    with _wallet_client() as (client, user, workspace, _session):
        raw_key, _ = STORE.create_api_key(
            workspace_id=workspace.id,
            name="wallet-management",
            creator_user_id=user.id,
            management=True,
        )
        minted = client.post(
            "/v1/keys",
            headers={"authorization": f"Bearer {raw_key}"},
            json={"name": "derived", "management": True},
        )
        assert minted.status_code in (200, 201), minted.text
        derived_key = minted.json()["key"]
        response = client.put(
            "/v1/byok/providers/openai",
            headers={"authorization": f"Bearer {derived_key}"},
            json={"api_key": "sk-test-0123456789abcdef"},
        )
        stored = STORE.list_byok_providers(workspace.id)

    assert response.status_code == 403
    assert stored == []


def test_wallet_only_console_cannot_register_byok_key() -> None:
    with _wallet_client() as (client, _user, workspace, _session):
        response = client.post(
            "/console/byok",
            data={"provider": "openai", "api_key": "sk-test-0123456789abcdef"},
            follow_redirects=False,
        )
        stored = STORE.list_byok_providers(workspace.id)
        page = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"] == "/console/byok?error=wallet_only"
    assert stored == []
    assert "Add and verify an email to use BYOK." in page.text


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
        client_page = client.get("/console/byok")

    assert response.status_code == 303
    assert response.headers["location"] == "/console/byok"
    assert "Add and verify an email to use BYOK." not in client_page.text
    assert [config.provider for config in stored] == ["openai"]


def _derived_key_byok_status(client, workspace_id: str, creator_user_id: str) -> tuple[int, int]:
    parent, _ = STORE.create_api_key(
        workspace_id=workspace_id,
        name="parent",
        creator_user_id=creator_user_id,
        management=True,
    )
    minted = client.post(
        "/v1/keys",
        headers={"authorization": f"Bearer {parent}"},
        json={"name": "child", "management": True},
    )
    assert minted.status_code in (200, 201), minted.text
    statuses = []
    for raw in (parent, minted.json()["key"]):
        STORE.delete_byok_provider(workspace_id, "openai")
        statuses.append(
            client.put(
                "/v1/byok/providers/openai",
                headers={"authorization": f"Bearer {raw}"},
                json={"api_key": "sk-test-0123456789abcdef"},
            ).status_code
        )
    return statuses[0], statuses[1]


def test_verified_admin_child_key_in_wallet_owned_workspace_can_register_byok() -> None:
    with _wallet_client() as (client, _owner, workspace, _session):
        admin = STORE.add_members(workspace.id, ["admin@example.org"], role="admin")[0]
        STORE.mark_user_email_verified(admin.user_id)
        parent_status, child_status = _derived_key_byok_status(client, workspace.id, admin.user_id)

    assert (parent_status, child_status) == (201, 201)


def test_wallet_admin_child_key_in_verified_workspace_cannot_register_byok() -> None:
    with _wallet_client() as (client, wallet_user, _wallet_ws, _session):
        owner = STORE.ensure_user("owner@example.org")
        STORE.mark_user_email_verified(owner.id)
        workspace = STORE.list_workspaces_for_user(owner.id)[0]
        STORE.members[(workspace.id, wallet_user.id)] = Member(
            workspace_id=workspace.id, user_id=wallet_user.id, role="admin"
        )
        parent_status, child_status = _derived_key_byok_status(client, workspace.id, wallet_user.id)

    assert (parent_status, child_status) == (403, 403)
