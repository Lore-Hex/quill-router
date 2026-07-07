from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console.activity import _USAGE_CACHE
from trusted_router.storage import STORE, Generation, InMemoryStore
from trusted_router.storage_activity import summarize_activity
from trusted_router.storage_gcp_activity_index import usage_series, write_generation


def _generation(
    generation_id: str,
    *,
    workspace_id: str = "ws_1",
    key_hash: str = "key_a",
    model: str = "model-a",
    usage_type: str = "Credits",
    created_at: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int,
    cost_micro: int,
) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req-{generation_id}",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model=model,
        provider_name="Provider",
        app="usage-series-test",
        tokens_prompt=prompt_tokens,
        tokens_completion=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_cost_microdollars=cost_micro,
        usage_type=usage_type,
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at=created_at,
    )


def _seed_generations() -> tuple[Any, list[Generation]]:
    _store, _db, table = make_fake_store()
    generations = [
        _generation(
            "gen-1",
            created_at="2026-05-01T10:05:00Z",
            prompt_tokens=10,
            completion_tokens=5,
            reasoning_tokens=1,
            cost_micro=100,
        ),
        _generation(
            "gen-2",
            key_hash="key_b",
            model="model-b",
            usage_type="BYOK",
            created_at="2026-05-01T10:45:00Z",
            prompt_tokens=20,
            completion_tokens=10,
            reasoning_tokens=2,
            cost_micro=200,
        ),
        _generation(
            "gen-3",
            created_at="2026-05-01T11:00:00Z",
            prompt_tokens=30,
            completion_tokens=15,
            reasoning_tokens=3,
            cost_micro=300,
        ),
        _generation(
            "gen-4",
            model="model-b",
            created_at="2026-05-02T09:15:00Z",
            prompt_tokens=40,
            completion_tokens=20,
            reasoning_tokens=4,
            cost_micro=400,
        ),
        _generation(
            "gen-other-workspace",
            workspace_id="ws_other",
            created_at="2026-05-01T10:10:00Z",
            prompt_tokens=99,
            completion_tokens=99,
            reasoning_tokens=99,
            cost_micro=99,
        ),
    ]
    for generation in generations:
        write_generation(table, "m", generation)
    return table, generations[:4]


def test_usage_series_hourly_and_daily_buckets() -> None:
    table, _generations = _seed_generations()

    hourly = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="hour",
    )
    daily = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
    )

    assert hourly["buckets"] == [
        {
            "bucket": "2026-05-01T10",
            "requests": 2,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 100,
            "byok_micro": 200,
        },
        {
            "bucket": "2026-05-01T11",
            "requests": 1,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 300,
            "byok_micro": 0,
        },
        {
            "bucket": "2026-05-02T09",
            "requests": 1,
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "reasoning_tokens": 4,
            "cost_micro": 400,
            "byok_micro": 0,
        },
    ]
    assert daily["buckets"] == [
        {
            "bucket": "2026-05-01",
            "requests": 3,
            "prompt_tokens": 60,
            "completion_tokens": 30,
            "reasoning_tokens": 6,
            "cost_micro": 400,
            "byok_micro": 200,
        },
        {
            "bucket": "2026-05-02",
            "requests": 1,
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "reasoning_tokens": 4,
            "cost_micro": 400,
            "byok_micro": 0,
        },
    ]
    assert table.reads[0] == (
        b"ws#ws_1#2026-05-01",
        b"ws#ws_1#2026-05-02~",
        200_000,
    )


def test_usage_series_by_model_breakdown() -> None:
    table, _generations = _seed_generations()

    series = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
        by_model=True,
    )

    assert series["by_model"] == {
        "model-a": [
            {
                "bucket": "2026-05-01",
                "requests": 2,
                "prompt_tokens": 40,
                "completion_tokens": 20,
                "reasoning_tokens": 4,
                "cost_micro": 400,
                "byok_micro": 0,
            }
        ],
        "model-b": [
            {
                "bucket": "2026-05-01",
                "requests": 1,
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "reasoning_tokens": 2,
                "cost_micro": 0,
                "byok_micro": 200,
            },
            {
                "bucket": "2026-05-02",
                "requests": 1,
                "prompt_tokens": 40,
                "completion_tokens": 20,
                "reasoning_tokens": 4,
                "cost_micro": 400,
                "byok_micro": 0,
            },
        ],
    }


def test_usage_series_day_totals_match_summarize_activity() -> None:
    table, generations = _seed_generations()

    series = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
    )

    expected: dict[str, dict[str, int]] = {}
    for row in summarize_activity(generations):
        bucket = expected.setdefault(
            str(row["date"]),
            {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_micro": 0,
                "byok_micro": 0,
            },
        )
        bucket["requests"] += int(row["requests"])
        bucket["prompt_tokens"] += int(row["prompt_tokens"])
        bucket["completion_tokens"] += int(row["completion_tokens"])
        bucket["reasoning_tokens"] += int(row["reasoning_tokens"])
        bucket["cost_micro"] += int(row["usage_microdollars"])
        bucket["byok_micro"] += int(row["byok_usage_inference_microdollars"])
    actual = {
        str(bucket["bucket"]): {
            key: int(bucket[key])
            for key in (
                "requests",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cost_micro",
                "byok_micro",
            )
        }
        for bucket in series["buckets"]
    }

    assert actual == expected


def test_usage_series_api_key_filter_and_truncation() -> None:
    table, _generations = _seed_generations()

    filtered = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="hour",
        api_key_hash="key_b",
    )
    truncated = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
        max_rows=2,
    )

    assert filtered["buckets"] == [
        {
            "bucket": "2026-05-01T10",
            "requests": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "reasoning_tokens": 2,
            "cost_micro": 0,
            "byok_micro": 200,
        }
    ]
    assert truncated["truncated"] is True
    assert truncated["buckets"] == [
        {
            "bucket": "2026-05-01",
            "requests": 2,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 100,
            "byok_micro": 200,
        }
    ]
    assert table.reads[-1] == (
        b"ws#ws_1#2026-05-01",
        b"ws#ws_1#2026-05-02~",
        2,
    )


def test_console_usage_series_endpoint_returns_json_and_uses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _USAGE_CACHE.clear()
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("usage-route@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="usage-route@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    calls: list[tuple[str, int, str, str | None, bool]] = []

    def spy_usage_series(
        self: InMemoryStore,
        workspace_id: str,
        *,
        days: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        _ = self
        calls.append((workspace_id, days, granularity, api_key_hash, by_model))
        return {
            "granularity": granularity,
            "start_day": "2026-07-06",
            "end_day": "2026-07-07",
            "truncated": False,
            "buckets": [],
            "by_model": {"model-a": []} if by_model else {},
        }

    monkeypatch.setattr(InMemoryStore, "usage_series", spy_usage_series)

    first = client.get("/console/activity/usage.json?days=2&by_model=true&api_key_hash=key_a")
    second = client.get("/console/activity/usage.json?days=2&by_model=true&api_key_hash=key_a")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["granularity"] == "hour"
    assert first.json() == second.json()
    assert calls == [(workspace.id, 2, "hour", "key_a", True)]


def test_console_usage_series_endpoint_rejects_bad_granularity() -> None:
    _USAGE_CACHE.clear()
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("usage-bad-granularity@example.com")
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="usage-bad-granularity@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)

    response = client.get("/console/activity/usage.json?granularity=minute")

    assert response.status_code == 400
