from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from trusted_router.request_tags import InvalidTags, merge_tags, validate_tags
from trusted_router.storage import STORE


def _create_tagged_key(
    client: TestClient,
    user_headers: dict[str, str],
    *,
    tags: dict[str, str] | None = None,
) -> dict:
    response = client.post(
        "/v1/keys",
        headers=user_headers,
        json={"name": "tagged", "tags": tags or {}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _authorize(
    client: TestClient,
    key_hash: str,
    *,
    tags: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> dict:
    body: dict[str, object] = {
        "api_key_hash": key_hash,
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 12,
        "max_output_tokens": 4,
    }
    if tags is not None:
        body["tags"] = tags
    headers = {"idempotency-key": idempotency_key} if idempotency_key else None
    response = client.post(
        "/v1/internal/gateway/authorize",
        headers=headers,
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _settle(
    client: TestClient,
    authorization_id: str,
    *,
    request_id: str,
    tags: dict[str, str] | None = None,
) -> str:
    body: dict[str, object] = {
        "authorization_id": authorization_id,
        "actual_input_tokens": 12,
        "actual_output_tokens": 2,
        "request_id": request_id,
        "elapsed_seconds": 0.25,
        "user": "user-123",
        "session_id": "matter-456",
        "app": "Contract Review",
        "http_referer": "https://legal.example/review",
        "app_categories": ["legal", "productivity"],
    }
    if tags is not None:
        body["tags"] = tags
    response = client.post("/v1/internal/gateway/settle", json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["generation_id"])


@pytest.mark.parametrize(
    "tags, message",
    [
        ({f"key-{index}": "value" for index in range(51)}, "at most 50"),
        ({"aws:owner": "legal"}, "reserved prefix"),
        ({"TrustedRouter:owner": "legal"}, "reserved prefix"),
        ({"bad#key": "legal"}, "unsupported characters"),
        ({"": "legal"}, "1 to 128"),
        ({"team": "x" * 257}, "at most 256"),
        ({"team": "x" * 4096}, "at most 256"),
    ],
)
def test_tag_validation_rejects_invalid_maps(tags: dict[str, str], message: str) -> None:
    with pytest.raises(InvalidTags, match=message):
        validate_tags(tags)


def test_tag_validation_rejects_non_string_values() -> None:
    with pytest.raises(InvalidTags, match="value must be a string"):
        validate_tags({"priority": 7})


def test_effective_tag_map_has_bounded_total_utf8_size() -> None:
    oversized = {f"tag-{index:02d}": "x" * 90 for index in range(50)}
    with pytest.raises(InvalidTags, match="4096 UTF-8 bytes"):
        validate_tags(oversized)


def test_merge_tags_overlays_request_values_without_mutating_defaults() -> None:
    defaults = {"environment": "production", "team": "platform"}
    assert merge_tags(defaults, {"team": "legal", "project": "atlas"}) == {
        "environment": "production",
        "team": "legal",
        "project": "atlas",
    }
    assert defaults == {"environment": "production", "team": "platform"}


def test_api_key_default_tags_round_trip_and_patch(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    created = _create_tagged_key(
        client,
        user_headers,
        tags={"environment": "production", "team": "legal"},
    )
    assert created["data"]["tags"] == {
        "environment": "production",
        "team": "legal",
    }

    key_hash = created["data"]["hash"]
    patched = client.patch(
        f"/v1/keys/{key_hash}",
        headers=user_headers,
        json={"tags": {"environment": "staging"}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["tags"] == {"environment": "staging"}


def test_authorize_freezes_merged_tags_and_settle_cannot_change_them(
    client: TestClient,
    user_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = _create_tagged_key(
        client,
        user_headers,
        tags={"environment": "production", "team": "platform"},
    )
    auth = _authorize(
        client,
        created["data"]["hash"],
        tags={"team": "legal", "project": "atlas"},
    )
    assert auth["tags"] == {
        "environment": "production",
        "team": "legal",
        "project": "atlas",
    }

    with caplog.at_level(logging.WARNING):
        generation_id = _settle(
            client,
            auth["authorization_id"],
            request_id="tagged-request",
            tags={"team": "tampered"},
        )
    generation = STORE.get_generation(generation_id)
    assert generation is not None
    assert generation.tags == auth["tags"]
    assert generation.user == "user-123"
    assert generation.session_id == "matter-456"
    assert generation.http_referer == "https://legal.example/review"
    assert generation.app_categories == ["legal", "productivity"]
    assert "settlement tags ignored" in caplog.text
    assert "tampered" not in caplog.text


def test_idempotent_retry_replays_frozen_tags_after_key_defaults_change(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    created = _create_tagged_key(
        client,
        user_headers,
        tags={"environment": "production"},
    )
    key_hash = created["data"]["hash"]
    first = _authorize(client, key_hash, idempotency_key="tag-default-drift")

    patched = client.patch(
        f"/v1/keys/{key_hash}",
        headers=user_headers,
        json={"tags": {"environment": "staging"}},
    )
    assert patched.status_code == 200
    replay = _authorize(client, key_hash, idempotency_key="tag-default-drift")
    assert replay["authorization_id"] == first["authorization_id"]
    assert replay["idempotent_replay"] is True
    assert replay["tags"] == {"environment": "production"}


def test_idempotency_key_rejects_different_request_tags(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    created = _create_tagged_key(client, user_headers)
    key_hash = created["data"]["hash"]
    _authorize(
        client,
        key_hash,
        tags={"environment": "production"},
        idempotency_key="tag-conflict",
    )
    response = client.post(
        "/v1/internal/gateway/authorize",
        headers={"idempotency-key": "tag-conflict"},
        json={
            "api_key_hash": key_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 12,
            "max_output_tokens": 4,
            "tags": {"environment": "staging"},
        },
    )
    assert response.status_code == 409


def test_activity_can_filter_and_group_by_tag(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    created = _create_tagged_key(client, user_headers)
    key_hash = created["data"]["hash"]
    for index, team in enumerate(("legal", "platform", "legal")):
        auth = _authorize(client, key_hash, tags={"team": team, "request": str(index)})
        _settle(client, auth["authorization_id"], request_id=f"tag-activity-{index}")

    filtered = client.get(
        "/v1/activity?group_by=none&tag_key=team&tag_value=legal",
        headers=user_headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert len(filtered.json()["data"]) == 2
    assert all(row["tags"]["team"] == "legal" for row in filtered.json()["data"])

    grouped = client.get(
        "/v1/activity?group_by=tag:team",
        headers=user_headers,
    )
    assert grouped.status_code == 200, grouped.text
    counts = {row["tag_value"]: row["requests"] for row in grouped.json()["data"]}
    assert counts == {"legal": 2, "platform": 1}


def test_activity_rejects_value_without_tag_key(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    response = client.get("/v1/activity?tag_value=legal", headers=user_headers)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_tags"
