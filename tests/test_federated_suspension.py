"""Federated peers answer a home abuse suspension like the home plane does.

The home plane refuses an abuse-suspended workspace with a final 403 and no
Retry-After (assert_workspace_billing_active). A peer only sees the home
workspace through its resolve-key record, so the record says which pause is a
suspension; otherwise the peer rebuilds the bare paused bit as the transient
"migration" cause and answers 503 + Retry-After, which clients retry.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from trusted_router.storage import STORE


def _federated_record(client: TestClient, key_hash: str) -> dict[str, Any]:
    key = STORE.get_key_by_hash(key_hash)
    assert key is not None
    client.app.state.settings.federation_peer_token = "peer-secret"  # noqa: S105
    served = client.post(
        "/v1/internal/federation/resolve-key",
        headers={
            "x-trustedrouter-federation-token": "peer-secret",
            "x-trustedrouter-federation-features": "scopes",
        },
        json={"api_key_lookup_hash": key.lookup_hash},
    )
    assert served.status_code == 200, served.text
    return dict(served.json()["data"])


def _key(client: TestClient, email: str) -> dict[str, Any]:
    created = client.post("/v1/keys", headers={"x-trustedrouter-user": email}, json={"name": "k"})
    assert created.status_code == 201, created.text
    return dict(created.json()["data"])


def test_home_plane_tells_peers_which_pause_is_a_suspension(client: TestClient) -> None:
    suspended = _key(client, "alice@example.com")
    assert STORE.record_workspace_abuse_and_demote(
        suspended["workspace_id"],
        abuse_ref="carding-test",
        operator_identity="ops@example.com",
        reason="stolen cards",
    )
    record = _federated_record(client, suspended["hash"])
    assert record["workspace_billing_paused"] is True
    assert record["workspace_suspended"] is True

    key = _key(client, "bob@example.com")
    assert "workspace_suspended" not in _federated_record(client, key["hash"])
    STORE.update_workspace(key["workspace_id"], billing_paused=True)
    migrating = _federated_record(client, key["hash"])
    assert migrating["workspace_billing_paused"] is True
    assert "workspace_suspended" not in migrating
