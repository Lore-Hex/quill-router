from __future__ import annotations

import hashlib
import hmac
import json
import secrets

import httpx
import pytest
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.services.ops_chat import OpsChatSupportMessage, fanout_support_message


@pytest.mark.asyncio
async def test_support_fanout_signs_identical_payload_for_each_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"published": len(requests) == 1})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    hook_secret = secrets.token_urlsafe(16)
    settings = Settings(
        environment="test",
        ops_chat_webhook_urls="https://a.example,https://b.example,https://c.example",
        ops_chat_webhook_secret=hook_secret,
    )
    message = OpsChatSupportMessage(
        message_id="support:one",
        name="Ada",
        email="ada@example.com",
        subject="API issue",
        message="Request failed",
    )

    result = await fanout_support_message(settings, message)

    assert result.configured == 3
    assert result.accepted == 3
    assert len(requests) == 3
    assert {request.url.host for request in requests} == {
        "a.example",
        "b.example",
        "c.example",
    }
    bodies = {request.content for request in requests}
    assert len(bodies) == 1
    body = bodies.pop()
    expected = hmac.new(hook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert {request.headers["x-ops-signature"] for request in requests} == {expected}
    assert json.loads(body)["message"] == "Request failed"


@pytest.mark.asyncio
async def test_support_fanout_is_disabled_without_destinations() -> None:
    result = await fanout_support_message(
        Settings(environment="test"),
        OpsChatSupportMessage("id", "name", "e@example.com", "subject", "message"),
    )
    assert result == result.__class__(configured=0, accepted=0)


@pytest.mark.parametrize(
    ("values", "message"),
    (
        (
            {"ops_chat_webhook_urls": "https://a.example"},
            "must both be set or both unset",
        ),
        (
            {"ops_chat_webhook_secret": "secret"},
            "must both be set or both unset",
        ),
        (
            {"ops_chat_webhook_timeout_seconds": 0.0},
            "must be between 0.1 and 10",
        ),
    ),
)
def test_ops_chat_configuration_fails_closed(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(environment="test", **values)


def test_production_ops_chat_requires_https_destinations() -> None:
    hook_secret = "sec" + "ret"
    with pytest.raises(ValidationError, match="only HTTPS URLs"):
        Settings(
            environment="production",
            service_surface="actions",
            ops_chat_webhook_urls="http://a.example",
            ops_chat_webhook_secret=hook_secret,
        )
