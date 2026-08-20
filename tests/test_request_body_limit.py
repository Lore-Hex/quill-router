from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.request_body_limit import (
    RequestBodyLimitMiddleware,
    UnreadRequestBodyCloseMiddleware,
    _InFlightBodyBudget,
)
from trusted_router.storage import STORE


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
        [(b"transfer-encoding", b"")],
        [(b"transfer-encoding", b"gzip")],
        [(b"transfer-encoding", b"chunked"), (b"transfer-encoding", b"chunked")],
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


def test_outer_wrapper_closes_unread_unknown_length_post_response() -> None:
    messages, delivered, receive_calls = asyncio.run(
        _run(
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


def test_completed_request_body_does_not_truncate_asgi_23_streaming_response() -> None:
    async def scenario() -> list[Message]:
        sent: list[Message] = []
        receive_calls = 0
        disconnect = asyncio.Event()

        async def receive() -> Message:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": b"x", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            request = await app_receive()
            assert request.get("body") == b"x"

            async def chunks() -> Any:
                for index in range(10):
                    await asyncio.sleep(0.01)
                    yield str(index).encode("ascii")

            await StreamingResponse(chunks())(scope, app_receive, app_send)

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=0.035,
        )
        scope = _scope([(b"content-length", b"1")])
        scope["asgi"] = {"version": "3.0", "spec_version": "2.3"}
        await middleware(scope, receive, send)
        return sent

    messages = asyncio.run(scenario())
    assert _status(messages) == 200
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert body == b"0123456789"


def test_no_body_http2_streaming_get_does_not_compete_with_slow_post() -> None:
    async def scenario() -> tuple[list[Message], list[Message], int]:
        release_post = asyncio.Event()
        post_receive_started = asyncio.Event()
        disconnect = asyncio.Event()
        post_messages: list[Message] = []
        get_messages: list[Message] = []
        get_receive_calls = 0

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            if scope["method"] == "POST":
                await app_receive()
                await app_send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await app_send({"type": "http.response.body", "body": b"post"})
                return

            async def chunks() -> Any:
                yield b"streaming-get"

            await StreamingResponse(chunks())(scope, app_receive, app_send)

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def post_receive() -> Message:
            post_receive_started.set()
            await release_post.wait()
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send_post(message: Message) -> None:
            post_messages.append(message)

        async def get_receive() -> Message:
            nonlocal get_receive_calls
            get_receive_calls += 1
            if get_receive_calls == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send_get(message: Message) -> None:
            get_messages.append(message)

        post = asyncio.create_task(
            middleware(
                _scope([(b"content-length", b"1")]),
                post_receive,
                send_post,
            )
        )
        await post_receive_started.wait()
        get_scope = _scope([], method="GET")
        get_scope["http_version"] = "2"
        get_scope["asgi"] = {"version": "3.0", "spec_version": "2.3"}
        await middleware(get_scope, get_receive, send_get)
        release_post.set()
        await post
        return post_messages, get_messages, get_receive_calls

    post_messages, get_messages, get_receive_calls = asyncio.run(scenario())
    assert _status(post_messages) == 200
    assert _status(get_messages) == 200
    assert get_receive_calls >= 1
    body = b"".join(
        message.get("body", b"")
        for message in get_messages
        if message["type"] == "http.response.body"
    )
    assert body == b"streaming-get"


def test_unknown_length_streaming_get_outlives_request_body_deadline() -> None:
    async def scenario() -> list[Message]:
        sent: list[Message] = []
        receive_calls = 0
        disconnect = asyncio.Event()

        async def receive() -> Message:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            async def chunks() -> Any:
                for index in range(6):
                    await asyncio.sleep(0.01)
                    yield str(index).encode("ascii")

            await StreamingResponse(chunks())(scope, app_receive, app_send)

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=0.02,
        )
        scope = _scope([], method="GET")
        scope["http_version"] = "2"
        scope["asgi"] = {"version": "3.0", "spec_version": "2.3"}
        await middleware(scope, receive, send)
        return sent

    messages = asyncio.run(scenario())
    assert _status(messages) == 200
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert body == b"012345"


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


def test_unknown_length_http2_posts_are_admitted_before_route_work() -> None:
    async def scenario() -> tuple[list[int], int, int, int]:
        release_uploads = asyncio.Event()
        first_route_entered = asyncio.Event()
        statuses: list[int] = []
        route_calls = 0
        receive_calls = 0

        async def app(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
            nonlocal route_calls
            route_calls += 1
            first_route_entered.set()
            await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def call() -> None:
            nonlocal receive_calls
            messages: list[Message] = []

            async def receive() -> Message:
                nonlocal receive_calls
                receive_calls += 1
                await release_uploads.wait()
                return {"type": "http.request", "body": b"x", "more_body": False}

            async def send(message: Message) -> None:
                messages.append(message)

            scope = _scope([])
            scope["http_version"] = "2"
            await middleware(scope, receive, send)
            statuses.append(_status(messages))

        first = asyncio.create_task(call())
        await first_route_entered.wait()
        contenders = [asyncio.create_task(call()) for _ in range(19)]
        # Every contender either receives an admission response or exposes the
        # regression by entering the route before the first body byte arrives.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        route_calls_before_release = route_calls
        receive_calls_before_release = receive_calls
        completed_contenders = sum(task.done() for task in contenders)

        release_uploads.set()
        await asyncio.gather(first, *contenders)
        return (
            statuses,
            route_calls_before_release,
            receive_calls_before_release,
            completed_contenders,
        )

    statuses, route_calls, receive_calls, completed_contenders = asyncio.run(scenario())
    assert route_calls == 1
    assert receive_calls == 1
    assert completed_contenders == 19
    assert statuses.count(200) == 1
    assert statuses.count(503) == 19


def test_explicit_zero_length_post_does_not_consume_upload_slot() -> None:
    async def scenario() -> tuple[int, int]:
        release_unknown = asyncio.Event()
        unknown_entered = asyncio.Event()
        zero_messages: list[Message] = []
        unknown_messages: list[Message] = []
        route_calls = 0

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            nonlocal route_calls
            route_calls += 1
            if not scope["headers"]:
                unknown_entered.set()
                await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def unknown_receive() -> Message:
            await release_unknown.wait()
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send_unknown(message: Message) -> None:
            unknown_messages.append(message)

        async def unused_receive() -> Message:
            raise AssertionError("Content-Length: 0 route read a body")

        async def send_zero(message: Message) -> None:
            zero_messages.append(message)

        unknown_scope = _scope([])
        unknown_scope["http_version"] = "2"
        unknown = asyncio.create_task(
            middleware(unknown_scope, unknown_receive, send_unknown)
        )
        await unknown_entered.wait()
        await middleware(
            _scope([(b"content-length", b"0")]),
            unused_receive,
            send_zero,
        )
        release_unknown.set()
        await unknown
        assert _status(unknown_messages) == 200
        return _status(zero_messages), route_calls

    zero_status, route_calls = asyncio.run(scenario())
    assert zero_status == 200
    assert route_calls == 2


def test_empty_options_does_not_compete_with_slow_post_upload() -> None:
    async def scenario() -> tuple[int, int]:
        release_post = asyncio.Event()
        post_receive_started = asyncio.Event()
        post_messages: list[Message] = []
        options_messages: list[Message] = []

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            if scope["method"] == "POST":
                await app_receive()
                status = 200
            else:
                status = 204
            await app_send(
                {"type": "http.response.start", "status": status, "headers": []}
            )
            await app_send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def post_receive() -> Message:
            post_receive_started.set()
            await release_post.wait()
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send_post(message: Message) -> None:
            post_messages.append(message)

        async def unused_options_receive() -> Message:
            raise AssertionError("empty OPTIONS route read a request body")

        async def send_options(message: Message) -> None:
            options_messages.append(message)

        post = asyncio.create_task(
            middleware(
                _scope([(b"content-length", b"1")]),
                post_receive,
                send_post,
            )
        )
        await post_receive_started.wait()
        options_scope = _scope([], method="OPTIONS")
        options_scope["http_version"] = "2"
        await middleware(options_scope, unused_options_receive, send_options)
        release_post.set()
        await post
        return _status(post_messages), _status(options_messages)

    post_status, options_status = asyncio.run(scenario())
    assert post_status == 200
    assert options_status == 204


@pytest.mark.parametrize(
    "get_headers",
    [
        [(b"content-length", b"1")],
        [(b"transfer-encoding", b"chunked")],
    ],
)
def test_known_framed_get_body_still_reserves_before_receive(
    get_headers: list[tuple[bytes, bytes]],
) -> None:
    async def scenario() -> tuple[int, int, int]:
        release_post = asyncio.Event()
        post_receive_started = asyncio.Event()
        post_messages: list[Message] = []
        get_messages: list[Message] = []
        get_source_reads = 0

        async def app(_scope: Scope, app_receive: Receive, app_send: Send) -> None:
            await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(
            app,
            max_bytes=8,
            max_in_flight_bytes=8,
            max_concurrent_bodies=1,
            read_timeout_seconds=1,
        )

        async def post_receive() -> Message:
            post_receive_started.set()
            await release_post.wait()
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send_post(message: Message) -> None:
            post_messages.append(message)

        async def get_receive() -> Message:
            nonlocal get_source_reads
            get_source_reads += 1
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send_get(message: Message) -> None:
            get_messages.append(message)

        post = asyncio.create_task(
            middleware(
                _scope([(b"content-length", b"1")]),
                post_receive,
                send_post,
            )
        )
        await post_receive_started.wait()
        await middleware(
            _scope(get_headers, method="GET"),
            get_receive,
            send_get,
        )
        release_post.set()
        await post
        return _status(post_messages), _status(get_messages), get_source_reads

    post_status, get_status, get_source_reads = asyncio.run(scenario())
    assert post_status == 200
    assert get_status == 503
    assert get_source_reads == 0


def test_unread_safe_method_body_does_not_reserve_upload_capacity() -> None:
    async def scenario() -> tuple[int, int]:
        get_started = asyncio.Event()
        release_get = asyncio.Event()
        get_messages: list[Message] = []
        post_messages: list[Message] = []

        async def app(scope: Scope, app_receive: Receive, app_send: Send) -> None:
            if scope["method"] == "GET":
                get_started.set()
                await release_get.wait()
            else:
                await app_receive()
            await app_send({"type": "http.response.start", "status": 200, "headers": []})
            await app_send({"type": "http.response.body", "body": b"ok"})

        middleware = UnreadRequestBodyCloseMiddleware(
            RequestBodyLimitMiddleware(
                app,
                max_bytes=8,
                max_in_flight_bytes=8,
                max_concurrent_bodies=1,
                read_timeout_seconds=1,
            )
        )

        async def unread_get_receive() -> Message:
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def post_receive() -> Message:
            return {"type": "http.request", "body": b"1234", "more_body": False}

        async def send_get(message: Message) -> None:
            get_messages.append(message)

        async def send_post(message: Message) -> None:
            post_messages.append(message)

        get_task = asyncio.create_task(
            middleware(
                _scope([(b"content-length", b"4")], method="GET"),
                unread_get_receive,
                send_get,
            )
        )
        await get_started.wait()
        await middleware(
            _scope([(b"content-length", b"4")]),
            post_receive,
            send_post,
        )
        release_get.set()
        await get_task
        assert _response_header(get_messages, b"connection") == b"close"
        return _status(get_messages), _status(post_messages)

    get_status, post_status = asyncio.run(scenario())
    assert (get_status, post_status) == (200, 200)


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


def test_anonymous_mcp_rejects_before_consuming_chunked_body() -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", max_request_body_bytes=8),
            init_observability=False,
        )
    )
    consumed: list[bytes] = []

    def chunks() -> Iterable[bytes]:
        for chunk in (b"1234", b"5678", b"9"):
            consumed.append(chunk)
            yield chunk

    response = client.post("/mcp", content=chunks())

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"
    assert response.headers["connection"] == "close"
    assert consumed == []


def test_authenticated_mcp_streamed_body_still_enforces_body_limit() -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", max_request_body_bytes=8),
            init_observability=False,
        )
    )
    user = STORE.ensure_user("mcp-body-limit@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_key, _api_key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="MCP body limit",
        creator_user_id=user.id,
    )

    response = client.post(
        "/mcp",
        headers={"authorization": f"Bearer {raw_key}"},
        content=iter([b"1234", b"5678", b"9"]),
    )

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
