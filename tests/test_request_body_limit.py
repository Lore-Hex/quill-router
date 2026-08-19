from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.request_body_limit import RequestBodyLimitMiddleware


def _scope(headers: Iterable[tuple[bytes, bytes]]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/mcp",
        "raw_path": b"/v1/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("trustedrouter.com", 443),
    }


async def _run(
    *,
    headers: Iterable[tuple[bytes, bytes]] = (),
    chunks: Iterable[bytes] = (),
    max_bytes: int = 8,
    read_body: bool = True,
) -> tuple[list[Message], list[bytes], int]:
    pending = list(chunks)
    receive_calls = 0
    delivered: list[bytes] = []
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        body = pending.pop(0) if pending else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(pending),
        }

    async def send(message: Message) -> None:
        sent.append(message)

    async def app(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
        if read_body:
            while True:
                message = await app_receive()
                if message["type"] != "http.request":
                    break
                delivered.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
        await app_send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await app_send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBodyLimitMiddleware(app, max_bytes=max_bytes)
    await middleware(_scope(headers), receive, send)
    return sent, delivered, receive_calls


def _status(messages: list[Message]) -> int:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return int(start["status"])


def _json_body(messages: list[Message]) -> dict[str, Any]:
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return dict(json.loads(raw))


def _response_header(messages: list[Message], name: bytes) -> bytes | None:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return next(
        (value for key, value in start["headers"] if key.lower() == name.lower()),
        None,
    )


def test_declared_oversize_is_rejected_without_reading_or_calling_route() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(headers=[(b"content-length", b"9")], chunks=[b"ignored"])
    )

    assert _status(messages) == 413
    assert _json_body(messages)["error"]["type"] == "payload_too_large"
    assert _response_header(messages, b"connection") == b"close"
    assert delivered == []
    assert receive_calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"bogus")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
    ],
)
def test_invalid_or_ambiguous_content_length_is_rejected(
    headers: list[tuple[bytes, bytes]],
) -> None:
    messages, delivered, receive_calls = asyncio.run(_run(headers=headers, chunks=[b"x"]))

    assert _status(messages) == 400
    assert _json_body(messages)["error"]["type"] == "bad_request"
    assert _response_header(messages, b"connection") == b"close"
    assert delivered == []
    assert receive_calls == 0


def test_chunked_body_is_counted_without_prebuffering_and_overflow_chunk_is_hidden() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(
            headers=[(b"transfer-encoding", b"chunked")],
            chunks=[b"1234", b"5678", b"9"],
        )
    )

    assert _status(messages) == 413
    assert _json_body(messages)["error"]["type"] == "payload_too_large"
    assert _response_header(messages, b"connection") == b"close"
    assert delivered == [b"1234", b"5678", b""]
    assert receive_calls == 3


def test_exact_limit_is_delivered_incrementally() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(
            headers=[(b"transfer-encoding", b"chunked")],
            chunks=[b"1234", b"5678"],
        )
    )

    assert _status(messages) == 200
    assert delivered == [b"1234", b"5678"]
    assert receive_calls == 2


def test_route_that_does_not_read_body_and_streaming_response_are_untouched() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(
            headers=[(b"transfer-encoding", b"chunked")],
            chunks=[b"larger-than-the-limit"],
            read_body=False,
        )
    )

    assert _status(messages) == 200
    assert delivered == []
    assert receive_calls == 0
    assert b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ) == b"ok"


def test_request_body_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="TR_MAX_REQUEST_BODY_BYTES must be positive"):
        Settings(environment="test", max_request_body_bytes=0)


def test_registered_middleware_rejects_declared_body_before_mcp_parsing() -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", max_request_body_bytes=8),
            init_observability=False,
        )
    )

    response = client.post("/mcp", content=b"123456789")

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "payload_too_large"
    assert response.headers["connection"] == "close"


def test_registered_middleware_rejects_chunked_body_before_mcp_buffers_it() -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", max_request_body_bytes=8),
            init_observability=False,
        )
    )

    response = client.post("/mcp", content=iter([b"1234", b"5678", b"9"]))

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "payload_too_large"
    assert response.headers["connection"] == "close"
