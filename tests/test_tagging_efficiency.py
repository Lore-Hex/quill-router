from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict
from typing import Any

import httpx
import pytest

import trusted_router.storage_activity as storage_activity
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_generations import SpannerGenerations
from trusted_router.storage_models import ApiKey, CreditAccount, GatewayAuthorization, Generation
from trusted_router.types import UsageType


@pytest.fixture(autouse=True)
def clear_activity_tag_cache() -> None:
    storage_activity.ACTIVITY_TAG_CACHE.clear()


def _generation(
    index: int = 0,
    *,
    workspace_id: str = "ws-eff",
    key_hash: str = "key-eff",
    tags: dict[str, str] | None = None,
) -> Generation:
    return Generation(
        id=f"gen-{index}",
        request_id=f"req-{index}",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="openai/gpt-5.4-nano",
        provider_name="OpenAI",
        app="test",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=100,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at=f"2026-07-11T12:{index:02d}:00Z",
        tags=tags or {},
    )


def test_json_body_elides_only_round_trip_safe_dataclass_defaults() -> None:
    untagged = _generation()
    tagged = _generation(1, tags={"team": "legal", "request": "r1"})
    empty_auth = GatewayAuthorization(
        id="auth-empty",
        workspace_id="ws-eff",
        key_hash="key-eff",
        model_id="openai/gpt-5.4-nano",
        provider="openai",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=123,
        created_at="2026-07-11T12:00:00Z",
    )
    tagged_auth = GatewayAuthorization(
        id="auth-tagged",
        workspace_id="ws-eff",
        key_hash="key-eff",
        model_id="openai/gpt-5.4-nano",
        provider="openai",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=123,
        created_at="2026-07-11T12:00:00Z",
        tags={"team": "legal"},
    )
    api_key = ApiKey(
        hash="key-eff",
        salt="salt",
        secret_hash="digest",  # noqa: S106 - placeholder test digest.
        lookup_hash="lookup",
        name="test key",
        label="sk-tr...eff",
        workspace_id="ws-eff",
        creator_user_id=None,
        created_at="2026-07-11T12:00:00Z",
    )
    credit = CreditAccount(workspace_id="ws-eff")

    for obj in (untagged, tagged, empty_auth, tagged_auth, api_key, credit):
        body = json_body(obj)
        assert type(obj)(**json.loads(body)) == obj

    old_body = json.dumps(asdict(untagged), separators=(",", ":"), sort_keys=True)
    new_payload = json.loads(json_body(untagged))
    assert len(json_body(untagged)) < len(old_body)
    for key in ("user", "session_id", "http_referer", "app_categories", "tags"):
        assert key not in new_payload

    assert "created_at" in new_payload
    assert json.loads(json_body(tagged))["tags"] == {"team": "legal", "request": "r1"}
    assert "tags" not in json.loads(json_body(empty_auth))
    assert json.loads(json_body(tagged_auth))["tags"] == {"team": "legal"}


class _FakeCell:
    def __init__(self, value: Generation) -> None:
        self.value = json_body(value).encode("utf-8")


class _FakeReadRow:
    def __init__(self, value: Generation) -> None:
        self.cells = {"m": {b"body": [_FakeCell(value)]}}


class _LazyBigtable:
    def __init__(self, rows: list[Generation]) -> None:
        self.rows = [_FakeReadRow(row) for row in rows]
        self.read_count = 0
        self.yielded = 0
        self.reads: list[tuple[bytes, bytes, int]] = []

    def read_rows(self, *, start_key: bytes, end_key: bytes, limit: int) -> Any:
        self.read_count += 1
        self.reads.append((start_key, end_key, limit))

        def _rows() -> Any:
            for row in self.rows[:limit]:
                self.yielded += 1
                yield row

        return _rows()


def _spanner_generations(rows: list[Generation]) -> tuple[SpannerGenerations, _LazyBigtable]:
    table = _LazyBigtable(rows)
    store = object.__new__(SpannerGenerations)
    store._bt_table = table
    store._family = "m"
    return store, table


def test_tag_filtered_activity_events_exit_after_limit_plus_one_matches() -> None:
    rows = [_generation(index, tags={"team": "legal"}) for index in range(60)]
    store, table = _spanner_generations(rows)

    result = store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")

    assert len(result.data) == 5
    assert result.truncated is True
    assert result.scanned < 60
    assert table.yielded < 60


def test_tag_filtered_activity_events_sparse_matches_scan_available_rows() -> None:
    rows = [
        _generation(index, tags={"team": "legal"} if index in {0, 20, 40} else {})
        for index in range(60)
    ]
    store, _table = _spanner_generations(rows)

    result = store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")

    assert len(result.data) == 3
    assert result.truncated is False
    assert result.scanned == 60


def test_tag_filtered_activity_cache_is_ttl_lru_and_tag_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    cache = storage_activity.ActivityTagCache(clock=lambda: now[0])
    monkeypatch.setattr(storage_activity, "ACTIVITY_TAG_CACHE", cache)
    rows = [
        _generation(index, tags={"team": "legal" if index % 2 == 0 else "platform"})
        for index in range(20)
    ]
    store, table = _spanner_generations(rows)

    first = store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")
    second = store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")
    # Cache hits hand back an equal COPY, never the same object: callers mutate
    # returned event dicts in place, and a shared cached list would leak one
    # caller's mutations into the next.
    assert second is not first
    assert second == first
    assert table.read_count == 1
    second.data[0]["cost_display"] = "mutated"
    third = store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")
    assert "cost_display" not in third.data[0]
    assert table.read_count == 1

    store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="platform")
    assert table.read_count == 2

    before_untagged = table.read_count
    store.activity_events_result("ws-eff", limit=5)
    store.activity_events_result("ws-eff", limit=5)
    assert table.read_count == before_untagged + 2

    before_grouped = table.read_count
    grouped = store.activity_result("ws-eff", tag_key="team", tag_value="legal", group_by_tag="team")
    grouped_again = store.activity_result(
        "ws-eff",
        tag_key="team",
        tag_value="legal",
        group_by_tag="team",
    )
    assert grouped_again is not grouped
    assert grouped_again == grouped
    assert table.read_count == before_grouped + 1

    now[0] += storage_activity.ACTIVITY_TAG_CACHE_TTL_SECONDS + 0.1
    store.activity_events_result("ws-eff", limit=5, tag_key="team", tag_value="legal")
    assert table.read_count == before_grouped + 2


@pytest.mark.parametrize(
    ("query", "method_name"),
    [
        ("?group_by=none", "activity_events_result"),
        ("", "activity_result"),
    ],
)
def test_activity_handler_runs_storage_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    method_name: str,
) -> None:
    app = create_app(
        Settings(environment="test", internal_gateway_token=None),
        init_observability=False,
    )
    seen: dict[str, int] = {}
    original = getattr(InMemoryStore, method_name)

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen["tid"] = threading.get_ident()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InMemoryStore, method_name, spy)

    async def scenario() -> int:
        loop_tid = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            response = await ac.get(
                f"/v1/activity{query}",
                headers={"x-trustedrouter-user": "activity-loop@example.com"},
            )
            assert response.status_code == 200, response.text
        return loop_tid

    loop_tid = asyncio.run(scenario())
    assert seen["tid"] != loop_tid


def test_date_scoped_tag_filter_returns_newest_matches_not_oldest() -> None:
    """Regression: the date-scoped ws# prefix streams OLDEST-first, so the
    early-exit must not apply there — it would return the day's oldest matches
    instead of the newest (and cache them). The date path scans the full
    bounded window and sorts newest-first downstream, exactly the pre-change
    behavior; only the newest-first ws_recent (date=None) path early-exits."""
    rows = [_generation(index, tags={"team": "legal"}) for index in range(60)]
    store, table = _spanner_generations(rows)

    result = store.activity_events_result(
        "ws-eff", date="2026-07-11", limit=5, tag_key="team", tag_value="legal"
    )

    assert [event["id"] for event in result.data] == [
        "gen-59", "gen-58", "gen-57", "gen-56", "gen-55",
    ]
    assert result.scanned == 60  # full window scanned: no early exit on the date path
