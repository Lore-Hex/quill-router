from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.request_body_limit import (
    RequestBodyLimitMiddleware,
    UnreadRequestBodyCloseMiddleware,
    _InFlightBodyBudget,
)


def _scope(
    headers: Iterable[tuple[bytes, bytes]],
    *,
    method: str = "POST",
    path: str = "/v1/mcp",
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
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
    method: str = "POST",
    wrap_unread_close: bool = False,
    max_in_flight_bytes: int | None = None,
    max_concurrent_bodies: int = 16,
    read_timeout_seconds: float = 30.0,
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

    middleware: Any = RequestBodyLimitMiddleware(
        app,
        max_bytes=max_bytes,
        max_in_flight_bytes=max_in_flight_bytes,
        max_concurrent_bodies=max_concurrent_bodies,
        read_timeout_seconds=read_timeout_seconds,
    )
    if wrap_unread_close:
        middleware = UnreadRequestBodyCloseMiddleware(middleware)
    await middleware(_scope(headers, method=method), receive, send)
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
        [(b"content-length", b"9" * 5_000)],
        [(b"content-length", b"1 ")],
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


def test_outer_wrapper_closes_a_response_when_possible_body_was_not_consumed() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(
            headers=[(b"transfer-encoding", b"chunked")],
            chunks=[b"ignored"],
            read_body=False,
            wrap_unread_close=True,
        )
    )

    assert _status(messages) == 200
    assert _response_header(messages, b"connection") == b"close"
    assert delivered == []
    assert receive_calls == 0


def test_total_body_read_deadline_rejects_a_drip_before_route_response() -> None:
    async def scenario() -> list[Message]:
        sent: list[Message] = []

        async def receive() -> Message:
            await asyncio.sleep(0.05)
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        async def app(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
            await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"late"})

        middleware = UnreadRequestBodyCloseMiddleware(
            RequestBodyLimitMiddleware(
                app,
                max_bytes=8,
                max_in_flight_bytes=8,
                max_concurrent_bodies=1,
                read_timeout_seconds=0.01,
            )
        )
        await middleware(
            _scope([(b"content-length", b"1")]),
            receive,
            send,
        )
        return sent

    messages = asyncio.run(scenario())
    assert _status(messages) == 408
    assert _response_header(messages, b"connection") == b"close"
    assert _json_body(messages)["error"]["message"] == "Request body timed out"


def test_body_upload_concurrency_refuses_without_reading_then_recovers() -> None:
    async def scenario() -> tuple[list[int], int]:
        release_first = asyncio.Event()
        first_receive_started = asyncio.Event()
        statuses: list[int] = []
        route_calls = 0

        async def app(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
            nonlocal route_calls
            route_calls += 1
            await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=16,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def call(*, blocked: bool) -> None:
            messages: list[Message] = []

            async def receive() -> Message:
                if blocked:
                    first_receive_started.set()
                    await release_first.wait()
                return {"type": "http.request", "body": b"1234", "more_body": False}

            async def send(message: Message) -> None:
                messages.append(message)

            await middleware(
                _scope([(b"content-length", b"4")]),
                receive,
                send,
            )
            statuses.append(_status(messages))

        first = asyncio.create_task(call(blocked=True))
        await first_receive_started.wait()
        await call(blocked=False)
        release_first.set()
        await first
        await call(blocked=False)
        return statuses, route_calls

    statuses, route_calls = asyncio.run(scenario())
    assert statuses == [503, 200, 200]
    assert route_calls == 2


def test_weighted_body_budget_refuses_overcommit_and_releases_exactly() -> None:
    budget = _InFlightBodyBudget(8)
    start = threading.Barrier(6)
    reservations_done = threading.Barrier(6)
    release = threading.Event()
    admitted: list[bool] = []

    def contender() -> None:
        start.wait()
        accepted = budget.reserve(2)
        admitted.append(accepted)
        reservations_done.wait()
        if accepted:
            release.wait(timeout=1)
            budget.release(2)

    threads = [threading.Thread(target=contender) for _ in range(5)]
    for thread in threads:
        thread.start()
    start.wait()
    reservations_done.wait()

    assert admitted.count(True) == 4
    assert admitted.count(False) == 1
    assert budget.in_flight == 8
    release.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert budget.in_flight == 0
    assert budget.reserve(8)
    budget.release(8)


def test_request_body_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="TR_MAX_REQUEST_BODY_BYTES must be positive"):
        Settings(environment="test", max_request_body_bytes=0)


def test_request_body_capacity_settings_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES"):
        Settings(
            environment="test",
            max_request_body_bytes=9,
            max_in_flight_request_body_bytes=8,
        )
    with pytest.raises(ValueError, match="TR_MAX_CONCURRENT_REQUEST_BODIES"):
        Settings(environment="test", max_concurrent_request_bodies=0)
    with pytest.raises(ValueError, match="TR_REQUEST_BODY_READ_TIMEOUT_SECONDS"):
        Settings(environment="test", request_body_read_timeout_seconds=0)


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


def test_malformed_framing_is_still_counted_by_source_admission() -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", rate_limit_ip_per_window=1),
            init_observability=False,
        )
    )
    headers = [("content-length", "1"), ("content-length", "1")]

    first = client.post("/mcp", headers=headers, content=b"x")
    second = client.post("/mcp", headers=headers, content=b"x")

    assert first.status_code == 400
    assert first.headers["connection"] == "close"
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"
    assert second.headers["connection"] == "close"
