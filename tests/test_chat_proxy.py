"""Tests for /chat-proxy/v1/* — the same-origin streaming pipe the
chat playground uses to reach api.trustedrouter.com without tripping
browser CORS.

These tests use httpx.MockTransport to stand in for api.trustedrouter.com
and assert that the proxy is a transparent bytes-for-bytes pipe:
request body forwarded verbatim, response body streamed back verbatim,
upstream status + headers (minus hop-by-hop) preserved.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app


@pytest.fixture
def settings() -> Settings:
    # api_base_url stays the canonical attested gateway; the proxy
    # strips /v1 and rebuilds the URL from the route parameter.
    return Settings(
        environment="test",
        api_base_url="https://api.trustedrouter.com/v1",
    )


def _install_upstream(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> None:
    """Replace httpx.AsyncClient.send with a transport that calls the
    given handler. Used to stub api.trustedrouter.com from the proxy.

    The handler's returned Response is wrapped so its content is
    delivered through a ByteStream — required for the proxy's
    `aiter_raw()` iteration to work without StreamConsumed errors."""
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


class _AsyncStream(httpx.AsyncByteStream):
    """Minimal AsyncByteStream wrapper so MockTransport responses can
    be consumed via aiter_raw() (which the proxy uses)."""

    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aiter__(self):  # type: ignore[override]
        yield self._content

    async def aclose(self) -> None:
        pass


def _streaming_response(
    *,
    status_code: int,
    content: bytes,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a Response whose body is an AsyncByteStream so the proxy's
    stream=True + aiter_raw path works under MockTransport."""
    h = dict(headers or {})
    h.setdefault("content-type", "application/json")
    return httpx.Response(status_code, stream=_AsyncStream(content), headers=h)


def test_chat_proxy_forwards_body_and_returns_response(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = bytes(request.content)
        captured["authorization"] = request.headers.get("authorization")
        import json as _json
        return _streaming_response(
            status_code=200,
            content=_json.dumps(
                {"choices": [{"message": {"content": "pong"}}]}
            ).encode(),
            headers={
                "x-trustedrouter-provider": "anthropic",
                "x-trustedrouter-served-model": "anthropic/claude-sonnet-4.6",
            },
        )

    _install_upstream(monkeypatch, handler)

    client = TestClient(create_app(settings))
    body = {"model": "anthropic/claude-sonnet-4.6", "messages": []}
    response = client.post(
        "/chat-proxy/v1/chat/completions",
        json=body,
        headers={"Authorization": "Bearer sk-tr-test-fakekey"},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "pong"
    assert response.headers["x-trustedrouter-provider"] == "anthropic"
    assert response.headers["x-trustedrouter-served-model"] == "anthropic/claude-sonnet-4.6"
    # Forwarded URL hits the attested gateway
    assert captured["url"] == "https://api.trustedrouter.com/v1/chat/completions"
    assert captured["method"] == "POST"
    # Authorization preserved verbatim
    assert captured["authorization"] == "Bearer sk-tr-test-fakekey"
    # Body forwarded byte-for-byte
    import json

    assert json.loads(captured["body"]) == body


def test_chat_proxy_forwards_streaming_sse(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming responses (text/event-stream) must reach the browser
    as bytes-for-bytes from the upstream — the chat client parses the
    chunks directly."""
    sse_body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(
            status_code=200,
            content=sse_body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    _install_upstream(monkeypatch, handler)

    client = TestClient(create_app(settings))
    with client.stream(
        "POST",
        "/chat-proxy/v1/chat/completions",
        json={"stream": True, "model": "x", "messages": []},
        headers={"Authorization": "Bearer sk-tr-test"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        got = b"".join(response.iter_bytes())
    assert got.decode() == sse_body


def test_chat_proxy_passes_through_non_200_status(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream 401/429/5xx must reach the browser so the chat client
    can classify the error and show Retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        return _streaming_response(
            status_code=429,
            content=_json.dumps(
                {"error": {"message": "rate limited", "type": "rate_limit"}}
            ).encode(),
            headers={"retry-after": "5", "content-type": "application/json"},
        )

    _install_upstream(monkeypatch, handler)

    client = TestClient(create_app(settings))
    response = client.post(
        "/chat-proxy/v1/chat/completions",
        json={"model": "x", "messages": []},
        headers={"Authorization": "Bearer sk-tr-test"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["type"] == "rate_limit"


def test_chat_proxy_returns_502_on_upstream_network_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_upstream(monkeypatch, handler)

    client = TestClient(create_app(settings))
    response = client.post(
        "/chat-proxy/v1/chat/completions",
        json={"model": "x", "messages": []},
        headers={"Authorization": "Bearer sk-tr-test"},
    )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "bad_gateway"
    assert response.json()["error"]["source"] == "router"


def test_chat_proxy_strips_hop_by_hop_request_headers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hop-by-hop request headers (Host, Cookie, Connection, …) must
    NOT reach the upstream. Authorization must."""
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for k, v in request.headers.items():
            seen_headers[k.lower()] = v
        return _streaming_response(status_code=200, content=b"{}")

    _install_upstream(monkeypatch, handler)

    client = TestClient(create_app(settings))
    client.post(
        "/chat-proxy/v1/chat/completions",
        json={"model": "x", "messages": []},
        headers={
            "Authorization": "Bearer sk-tr-test",
            "Cookie": "tr_session=secret-session-cookie",
        },
    )
    assert "authorization" in seen_headers
    assert "cookie" not in seen_headers
