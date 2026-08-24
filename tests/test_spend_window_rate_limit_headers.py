from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from trusted_router.spend_windows import decide_key_window_limits
from trusted_router.storage import STORE

_RATE_LIMIT_HEADERS = (
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Reset",
    "Retry-After",
)


def _limited_inference_key() -> tuple[str, str]:
    user = STORE.ensure_user("rate-limit-headers@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credit_workspace_once(workspace.id, 5_000_000, "rate-limit-header-test")
    raw_key, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="limited agent",
        creator_user_id=user.id,
        limit_daily_microdollars=1_000_000,
    )
    STORE.api_keys.add_usage(key.hash, 200_000, is_byok=False)
    return raw_key, key.hash


def _chat(client: TestClient, raw_key: str, *, stream: bool = False):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "meta-llama/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "Say pong."}],
            "max_tokens": 1_000,
            "stream": stream,
        },
    )


def test_limited_public_route_reports_consistent_headers_and_429(
    client: TestClient,
) -> None:
    raw_key, key_hash = _limited_inference_key()

    allowed = _chat(client, raw_key)
    assert allowed.status_code == 200, allowed.text
    assert int(allowed.headers["RateLimit-Limit"]) == 1_000_000
    assert int(allowed.headers["RateLimit-Remaining"]) == 800_000
    assert (
        int(allowed.headers["RateLimit-Limit"])
        - int(allowed.headers["RateLimit-Remaining"])
        == 200_000
    )
    assert 1 <= int(allowed.headers["RateLimit-Reset"]) <= 86_400
    assert "Retry-After" not in allowed.headers

    current = STORE.api_keys.window_usage_snapshot(key_hash)["daily"]
    STORE.api_keys.add_usage(key_hash, 1_000_000 - current, is_byok=False)
    rejected = _chat(client, raw_key, stream=True)

    assert rejected.status_code == 429, rejected.text
    assert rejected.json()["error"]["type"] == "key_window_limit_exceeded"
    assert int(rejected.headers["RateLimit-Limit"]) == 1_000_000
    assert int(rejected.headers["RateLimit-Remaining"]) == 0
    assert int(rejected.headers["Retry-After"]) >= 1
    assert rejected.headers["Retry-After"] == rejected.headers["RateLimit-Reset"]


def test_route_without_spend_window_emits_no_rate_limit_headers(
    client: TestClient,
) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert all(header not in response.headers for header in _RATE_LIMIT_HEADERS)


def test_docs_publish_headers_and_agent_backoff_example(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert 'id="rate-limit-headers"' in docs.text
    assert "RateLimit-Limit" in docs.text
    assert "Retry-After" in docs.text
    assert "while jobs:" in docs.text

    agent_docs = client.get("/docs/llms-full.txt")
    assert agent_docs.status_code == 200
    assert "## Spend-Window Rate Limits" in agent_docs.text
    assert "RateLimit-Remaining" in agent_docs.text


def test_governing_window_is_the_one_with_least_headroom() -> None:
    decision = decide_key_window_limits(
        {"daily": 1_000, "monthly": 10_000},
        {"daily": 100, "monthly": 9_900},
        50,
        now=dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC),
    )

    assert decision is not None
    assert decision.allowed is True
    assert decision.window == "monthly"
    assert decision.limit == 10_000
    assert decision.remaining == 100
