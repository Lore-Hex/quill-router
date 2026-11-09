from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import CreditMovement

HEADERS = {"x-trustedrouter-user": "user-model-owner@example.com"}


def _body(
    *,
    slug: str = "community-machine",
    kind: str = "machine",
    display_identity: str = "handle",
    display_name: str = "helpful-operator",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Community model",
        "slug": slug,
        "kind": kind,
        "description": "A model operated by a community member.",
        "display_identity": display_identity,
        "display_name": display_name,
        "endpoint_url": "https://owner.example/v1",
        "upstream_model_id": "owner-model",
        "supports_streaming": True,
        "heartbeat_interval_seconds": 30,
        "max_concurrency": 2,
        "prompt_price_microdollars_per_million_tokens": 100,
        "completion_price_microdollars_per_million_tokens": 200,
    }
    if kind == "human":
        body["prompt_price_microdollars_per_million_tokens"] = 100_000_000_000
        body["completion_price_microdollars_per_million_tokens"] = 100_000_000_000
    return body


def _create(
    client: TestClient,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] = HEADERS,
) -> dict[str, Any]:
    from dataclasses import asdict

    _create_key(client, headers=headers)
    owner = STORE.find_user_by_email(headers["x-trustedrouter-user"])
    assert owner is not None
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    return asdict(
        STORE.create_user_model(
            owner_user_id=owner.id,
            owner_workspace_id=workspace.id,
            **(body or _body()),
        )
    )


def _create_key(
    client: TestClient,
    *,
    headers: dict[str, str] = HEADERS,
) -> dict[str, Any]:
    response = client.post("/v1/keys", headers=headers, json={"name": "gateway"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.parametrize(
    "before",
    [
        "99999999999",
        "not-a-date",
        "2026-13-01T00:00:00Z",
        "2026-02-30",
        "0001-01-01T00:00:00+05:00",
        "9999-12-31T23:59:59-05:00",
        "",
    ],
)
def test_owner_earnings_rejects_invalid_before(client: TestClient, before: str) -> None:
    response = client.get("/v1/user-models/earnings", headers=HEADERS, params={"before": before})
    assert response.status_code == 400, response.text
    assert response.json() == {
        "error": {
            "code": 400,
            "message": "before must be an ISO 8601 timestamp",
            "type": "bad_request",
            "source": "router",
        }
    }


@pytest.mark.parametrize(
    ("before", "canonical"),
    [
        ("2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z"),
        ("2026-10-30T00:00:00", "2026-10-30T00:00:00Z"),
        ("2026-10-30T02:00:00+02:00", "2026-10-30T00:00:00Z"),
        ("20261030", "2026-10-30T00:00:00Z"),
        ("2026-10-30", "2026-10-30T00:00:00Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z"),
    ],
)
def test_owner_earnings_before_normalizes_and_filters_strictly(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    before: str,
    canonical: str,
) -> None:
    from trusted_router.storage import _parse_iso_timestamp as parse_memory
    from trusted_router.storage_gcp import _iso_timestamp
    from trusted_router.storage_gcp import _parse_iso_timestamp as parse_gcp
    from trusted_router.storage_postgres import _parse_timestamp as parse_postgres

    user = STORE.ensure_user(HEADERS["x-trustedrouter-user"])
    account_id = f"user:{user.id}"
    cutoff = datetime.fromisoformat(canonical.replace("Z", "+00:00"))
    # At the representable extremes only one side exists. Always seed equality
    # as well, so changing the store's strict comparison to <= fails this test.
    timestamps = {"equal": cutoff}
    if cutoff > datetime.min.replace(tzinfo=UTC):
        timestamps["earlier"] = cutoff - timedelta(microseconds=1)
    if cutoff < datetime.max.replace(tzinfo=UTC):
        timestamps["later"] = cutoff + timedelta(microseconds=1)
    for movement_id, timestamp in timestamps.items():
        STORE.in_memory_target.credit_movements[(account_id, movement_id)] = CreditMovement(
            account_id=account_id,
            movement_id=movement_id,
            kind="custom_model_payout",
            amount_microdollars=1,
            created_at=timestamp.isoformat().replace("+00:00", "Z"),
        )

    received: list[str | None] = []
    original = InMemoryStore.list_credit_movements

    def recording_movements(
        store: InMemoryStore,
        account_id: str,
        *,
        kinds: list[str] | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> list[CreditMovement]:
        received.append(before)
        return original(store, account_id, kinds=kinds, limit=limit, before=before)

    monkeypatch.setattr(InMemoryStore, "list_credit_movements", recording_movements)
    response = client.get("/v1/user-models/earnings", headers=HEADERS, params={"before": before})
    assert response.status_code == 200, response.text
    assert received == [canonical]
    assert [row["movement_id"] for row in response.json()["data"]["recent"]] == (
        ["earlier"] if "earlier" in timestamps else []
    )
    for parse in (parse_memory, parse_gcp, parse_postgres):
        assert parse(canonical) == cutoff
        assert _iso_timestamp(parse(canonical)) == canonical


def test_owner_earnings_api_lists_summary_models_movements_and_transfers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(client, body=_body(slug="earnings-api"))
    owner_user_id = created["owner_user_id"]
    workspace = STORE.list_workspaces_for_user(owner_user_id)[0]
    assert STORE.credit_user_earnings(
        owner_user_id,
        2_500_000,
        "custom_model_payout:earnings-api-auth",
        custom_model_id=created["id"],
        payer_workspace_id=workspace.id,
    )

    response = client.get("/v1/user-models/earnings", headers=HEADERS)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["summary"] == {
        "total_earned": 2_500_000,
        "total_earned_display": "$2.50",
        "total_transferred": 0,
        "total_transferred_display": "$0.00",
        "available": 2_500_000,
        "available_display": "$2.50",
    }
    assert data["by_model_30d"] == [{"model_id": created["id"], "earned_microdollars": 2_500_000}]
    assert data["recent"][0]["kind"] == "custom_model_payout"
    assert data["recent"][0]["amount_display"] == "$2.50"

    summary_staleness: list[bool] = []
    original_summary = InMemoryStore.earnings_summary

    def recording_summary(
        store: InMemoryStore,
        user_id: str,
        *,
        allow_stale: bool = False,
    ) -> dict[str, int]:
        summary_staleness.append(allow_stale)
        return original_summary(store, user_id, allow_stale=allow_stale)

    # Patch the backend class, never the module-global STORE proxy.
    monkeypatch.setattr(InMemoryStore, "earnings_summary", recording_summary)

    body = {
        "workspace_id": workspace.id,
        "amount_microdollars": 1_000_000,
        "idempotency_key": "earnings-api-transfer",
    }
    accepted = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json=body,
    )
    assert accepted.status_code == 200, accepted.text
    assert summary_staleness == [False]
    assert accepted.json()["data"] == {
        "deduplicated": False,
        "summary": {
            "total_earned": 2_500_000,
            "total_earned_display": "$2.50",
            "total_transferred": 1_000_000,
            "total_transferred_display": "$1.00",
            "available": 1_500_000,
            "available_display": "$1.50",
        },
    }
    duplicate = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json=body,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["deduplicated"] is True
    assert summary_staleness == [False, False]

    insufficient = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": workspace.id, "amount_microdollars": 2_000_000},
    )
    assert insufficient.status_code == 402
    assert insufficient.json()["error"]["type"] == "insufficient_credits"
    assert insufficient.json()["error"]["available"] == 1_500_000
    assert summary_staleness == [False, False, False]


def test_owner_earnings_transfer_hides_membership_and_refuses_invalid_targets(
    client: TestClient,
) -> None:
    _create(client, body=_body(slug="earnings-targets"))
    user = STORE.find_user_by_email(HEADERS["x-trustedrouter-user"])
    assert user is not None
    workspace = STORE.list_workspaces_for_user(user.id)[0]

    for amount in (0, -1):
        invalid = client.post(
            "/v1/user-models/earnings/transfer",
            headers=HEADERS,
            json={"workspace_id": workspace.id, "amount_microdollars": amount},
        )
        assert invalid.status_code == 400

    stranger = STORE.ensure_user("earnings-stranger@example.com")
    stranger_workspace = STORE.list_workspaces_for_user(stranger.id)[0]
    hidden = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": stranger_workspace.id, "amount_microdollars": 1},
    )
    assert hidden.status_code == 404

    workspace.federated_home = "https://home.example"
    federated = client.post(
        "/v1/user-models/earnings/transfer",
        headers=HEADERS,
        json={"workspace_id": workspace.id, "amount_microdollars": 1},
    )
    assert federated.status_code == 400


def test_earnings_console_renders_and_transfers_with_idempotent_flash(
    client: TestClient,
) -> None:
    user = STORE.ensure_user("earnings-console@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="earnings console",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    model = STORE.create_user_model(
        owner_user_id=user.id,
        owner_workspace_id=workspace.id,
        name="Earning model",
        kind="machine",
        display_name="earner",
        endpoint_url="https://owner.example/v1",
        slug="earnings-console",
    )
    assert STORE.credit_user_earnings(
        user.id,
        3_000_000,
        "custom_model_payout:earnings-console-auth",
        custom_model_id=model.id,
        payer_workspace_id=workspace.id,
    )

    page = client.get("/console/earnings")
    assert page.status_code == 200
    assert "You receive 70% of registered app markup" in page.text
    assert "Previously earned model revenue remains available" not in page.text
    assert "cash out at least $100 in USD." in page.text
    assert "Routable" not in page.text
    assert "Earning model" not in page.text
    assert "Earnings by model" not in page.text
    assert 'href="/console/earnings"' not in page.text
    assert "Recent movements" in page.text
    assert model.id in page.text
    for path in ("/console/api-keys", "/console/credits", "/console/settings"):
        console_page = client.get(path)
        assert console_page.status_code == 200
        assert 'href="/console/earnings"' not in console_page.text

    form = {
        "workspace_id": workspace.id,
        "amount": "1.25",
        "idempotency_key": "console-transfer",
    }
    accepted = client.post(
        "/console/earnings/transfer",
        data=form,
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/console/earnings?saved=transferred"
    duplicate = client.post(
        "/console/earnings/transfer",
        data=form,
        follow_redirects=False,
    )
    assert duplicate.headers["location"] == "/console/earnings?saved=duplicate"
    assert STORE.earnings_summary(user.id)["available"] == 1_750_000
