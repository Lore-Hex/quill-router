from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import ApiKey, CreditAccount, GatewayAuthorization, Generation
from trusted_router.types import UsageType


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
