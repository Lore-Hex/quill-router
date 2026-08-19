"""Streaming ASGI request-body admission limit.

The middleware rejects an oversized declared body before route execution and
counts actual ASGI body chunks when no trustworthy length is available. It
never prebuffers or drains a request: handlers that intentionally ignore an
undeclared body remain cheap, while handlers that call ``body()``/``json()``
cannot buffer more than the configured wire-byte ceiling.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trusted_router.errors import error_response
from trusted_router.types import ErrorType


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        framing_error = _declared_length_error(scope, self.max_bytes)
        if framing_error is not None:
            status_code, message, error_type = framing_error
            response = error_response(status_code, message, error_type)
            # The body has not been consumed. Never leave a reusable HTTP/1
            # connection carrying rejected bytes for the next request; this is
            # mandatory for ambiguous framing and conservative for an oversized
            # declared body.
            response.headers["Connection"] = "close"
            await response(scope, receive, send)
            return

        seen = 0
        too_large = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal seen, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            seen += len(message.get("body", b""))
            if seen <= self.max_bytes:
                return message
            # Do not hand the overflowing chunk to Request.body/json. Returning
            # an empty terminal chunk lets ordinary route error handling unwind;
            # limited_send suppresses that response and emits the canonical 413.
            too_large = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if too_large:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not too_large:
                raise
        if not too_large:
            return
        if response_started:
            # This can only occur for an application that starts a response and
            # then reads more request bytes. Do not attempt a second response.
            return
        response = error_response(413, "Request body is too large", "payload_too_large")
        # The overflowing body was deliberately not drained.
        response.headers["Connection"] = "close"
        await response(scope, receive, send)


def _declared_length_error(
    scope: Scope,
    max_bytes: int,
) -> tuple[int, str, str] | None:
    headers = scope.get("headers", ())
    lengths = [value for name, value in headers if name.lower() == b"content-length"]
    transfer_encodings = [
        value for name, value in headers if name.lower() == b"transfer-encoding"
    ]
    if len(lengths) > 1 or (lengths and transfer_encodings):
        return 400, "Invalid Content-Length framing", ErrorType.BAD_REQUEST
    if not lengths:
        return None
    try:
        raw = lengths[0].decode("ascii")
    except UnicodeDecodeError:
        return 400, "Invalid Content-Length", ErrorType.BAD_REQUEST
    if not raw or not raw.isdecimal() or not raw.isascii():
        return 400, "Invalid Content-Length", ErrorType.BAD_REQUEST
    declared = int(raw)
    if declared > max_bytes:
        return 413, "Request body is too large", "payload_too_large"
    return None
