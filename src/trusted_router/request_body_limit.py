"""Bounded ASGI request-body admission.

The middleware rejects unsafe framing and oversized declared bodies before a
route runs, then counts streamed bytes without prebuffering them.  Slow uploads
also have a total deadline and share process-wide upload-concurrency and memory
budgets.  A separate outer wrapper closes HTTP/1 connections whenever a route
returns without consuming a possible body; this prevents early 401/429/404
responses from leaving attacker-controlled bytes on a reusable backend socket.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trusted_router.errors import error_response
from trusted_router.types import ErrorType

_MAX_CONTENT_LENGTH_DIGITS = 20


@dataclass(frozen=True)
class _BodyFraming:
    declared_length: int | None
    transfer_encoded: bool
    error: tuple[int, str, str] | None = None

    @property
    def possible_body(self) -> bool:
        return self.transfer_encoded or bool(self.declared_length)


class _InFlightBodyBudget:
    """Small non-blocking weighted admission counter shared by one process."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._in_flight = 0
        self._lock = threading.Lock()

    def reserve(self, amount: int) -> bool:
        if amount <= 0:
            return True
        with self._lock:
            if self._in_flight + amount > self.max_bytes:
                return False
            self._in_flight += amount
            return True

    def release(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            self._in_flight -= amount
            if self._in_flight < 0:  # pragma: no cover - invariant guard
                self._in_flight = 0
                raise RuntimeError("request-body budget released more bytes than reserved")

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        max_in_flight_bytes: int | None = None,
        max_concurrent_bodies: int = 16,
        read_timeout_seconds: float = 30.0,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_in_flight_bytes = max_in_flight_bytes or max(max_bytes, 64 * 1024 * 1024)
        self.max_concurrent_bodies = max_concurrent_bodies
        self.read_timeout_seconds = read_timeout_seconds
        self._budget = _InFlightBodyBudget(self.max_in_flight_bytes)
        self._body_slots = threading.BoundedSemaphore(max_concurrent_bodies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        framing = _body_framing(scope, self.max_bytes)
        if framing.error is not None:
            await _send_rejection(scope, receive, send, *framing.error)
            return
        seen = 0
        reserved = 0
        body_slot_acquired = False
        failure: str | None = None
        response_started = False
        deadline = asyncio.get_running_loop().time() + self.read_timeout_seconds

        def release_body_slot() -> None:
            nonlocal body_slot_acquired
            if body_slot_acquired:
                self._body_slots.release()
                body_slot_acquired = False

        def acquire_body_slot() -> bool:
            nonlocal body_slot_acquired
            if body_slot_acquired:
                return True
            body_slot_acquired = self._body_slots.acquire(blocking=False)
            return body_slot_acquired

        def reserve_to(total: int) -> bool:
            nonlocal reserved
            additional = total - reserved
            if additional <= 0:
                return True
            if not self._budget.reserve(additional):
                return False
            reserved += additional
            return True

        # A declared or transfer-encoded upload consumes an upload slot before
        # route/auth work can wait for its first byte. Reserve declared memory
        # up front as well; a lying client cannot overcommit every worker by
        # advertising several simultaneous maximum-size bodies.
        if framing.possible_body:
            if not acquire_body_slot() or not reserve_to(framing.declared_length or 0):
                release_body_slot()
                if reserved:
                    self._budget.release(reserved)
                await _send_capacity_rejection(scope, receive, send)
                return

        async def limited_receive() -> Message:
            nonlocal seen, failure
            if failure is not None:
                return {"type": "http.disconnect"}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                failure = "timeout"
                return {"type": "http.request", "body": b"", "more_body": False}
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                failure = "timeout"
                return {"type": "http.request", "body": b"", "more_body": False}
            if message["type"] != "http.request":
                release_body_slot()
                return message

            body = message.get("body", b"")
            more_body = bool(message.get("more_body", False))
            if (body or more_body) and not acquire_body_slot():
                failure = "capacity"
                return {"type": "http.request", "body": b"", "more_body": False}

            seen += len(body)
            if seen > self.max_bytes:
                failure = "too_large"
                return {"type": "http.request", "body": b"", "more_body": False}
            if not reserve_to(seen):
                failure = "capacity"
                return {"type": "http.request", "body": b"", "more_body": False}
            if not more_body:
                release_body_slot()
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if failure is not None:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            try:
                await self.app(scope, limited_receive, limited_send)
            except Exception:
                if failure is None:
                    raise
        finally:
            release_body_slot()
            if reserved:
                self._budget.release(reserved)

        if failure is None:
            return
        if response_started:
            # The outer unread-body wrapper already forced Connection: close
            # before any partial response began. A second response is invalid.
            return
        if failure == "too_large":
            await _send_rejection(
                scope,
                receive,
                send,
                413,
                "Request body is too large",
                "payload_too_large",
            )
        elif failure == "timeout":
            await _send_rejection(
                scope,
                receive,
                send,
                408,
                "Request body timed out",
                ErrorType.BAD_REQUEST,
            )
        else:
            await _send_capacity_rejection(scope, receive, send)


class UnreadRequestBodyCloseMiddleware:
    """Close early responses that leave a possible HTTP/1 request body unread.

    This wrapper performs no admission or rejection itself, so the inner source
    limiter still counts malformed/oversized requests. It only observes whether
    the application consumed the body and injects ``Connection: close`` into
    an early 401/404/429/etc. response when bytes may remain on the socket.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _scope_has_possible_body(scope):
            await self.app(scope, receive, send)
            return

        body_complete = False

        async def observed_receive() -> Message:
            nonlocal body_complete
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_complete = True
            return message

        async def close_unread_send(message: Message) -> None:
            if message["type"] == "http.response.start" and not body_complete:
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"connection"
                ]
                headers.append((b"connection", b"close"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, observed_receive, close_unread_send)


async def _send_rejection(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    message: str,
    error_type: str,
) -> None:
    response = error_response(status_code, message, error_type)
    response.headers["Connection"] = "close"
    await response(scope, receive, send)


async def _send_capacity_rejection(scope: Scope, receive: Receive, send: Send) -> None:
    response = error_response(
        503,
        "Request body capacity is temporarily unavailable",
        ErrorType.SERVICE_UNAVAILABLE,
    )
    response.headers["Retry-After"] = "1"
    response.headers["Connection"] = "close"
    await response(scope, receive, send)


def _body_framing(scope: Scope, max_bytes: int) -> _BodyFraming:
    headers = scope.get("headers", ())
    lengths = [value for name, value in headers if name.lower() == b"content-length"]
    transfer_encodings = [
        value for name, value in headers if name.lower() == b"transfer-encoding"
    ]
    if len(lengths) > 1 or (lengths and transfer_encodings):
        return _BodyFraming(
            None,
            bool(transfer_encodings),
            (400, "Invalid Content-Length framing", ErrorType.BAD_REQUEST),
        )
    if not lengths:
        return _BodyFraming(None, bool(transfer_encodings))
    try:
        raw = lengths[0].decode("ascii")
    except UnicodeDecodeError:
        return _BodyFraming(
            None,
            False,
            (400, "Invalid Content-Length", ErrorType.BAD_REQUEST),
        )
    # Stay strict at the ASGI boundary: whitespace is not part of a decimal
    # field value. Both deployed HTTP parsers normalize ordinary OWS first.
    # Bound the digit count before int() so pathological direct-ASGI input can
    # never trip Python's large-integer conversion limit or amplify CPU.
    if (
        not raw
        or len(raw) > _MAX_CONTENT_LENGTH_DIGITS
        or not raw.isascii()
        or not raw.isdecimal()
    ):
        return _BodyFraming(
            None,
            False,
            (400, "Invalid Content-Length", ErrorType.BAD_REQUEST),
        )
    try:
        declared = int(raw)
    except (ValueError, OverflowError):
        return _BodyFraming(
            None,
            False,
            (400, "Invalid Content-Length", ErrorType.BAD_REQUEST),
        )
    if declared > max_bytes:
        return _BodyFraming(
            declared,
            False,
            (413, "Request body is too large", "payload_too_large"),
        )
    return _BodyFraming(declared, False)


def _scope_has_possible_body(scope: Scope) -> bool:
    headers = scope.get("headers", ())
    for name, value in headers:
        lowered = name.lower()
        if lowered == b"transfer-encoding":
            return True
        if lowered == b"content-length" and value.strip(b" \t0"):
            return True
    return False


def _declared_length_error(
    scope: Scope,
    max_bytes: int,
) -> tuple[int, str, str] | None:
    """Compatibility shim retained for focused parser tests/callers."""

    return _body_framing(scope, max_bytes).error
