"""Authenticated same-origin streaming proxy for the chat playground.

Background
==========
TR's control plane (trustedrouter.com) intentionally does NOT serve
the inference routes in production — `_control_plane_inference_enabled`
in main.py restricts that to local/test, so prompts can only execute
through the attested enclave at api.trustedrouter.com.

That's the right policy for SDK / production traffic, but it breaks
the browser chat playground at trustedrouter.com/chat: cross-origin
fetch from trustedrouter.com → api.trustedrouter.com is hard-blocked by
CORS (the attested gateway returns 401 to OPTIONS preflight with no
ACAO headers).

This module adds a minimal same-origin streaming pipe at the one browser-used
endpoint, ``POST /chat-proxy/v1/chat/completions``. A valid inference key is
resolved locally before any body read or outbound allocation; the handler then
forwards the request bytes-for-bytes to api.trustedrouter.com and streams the
response bytes back.
The proxy:

  * NEVER deserializes / inspects / logs the request or response body.
    This browser-only proxy terminates TLS in the control plane; direct SDK
    traffic should use the attested API endpoint, not this route.
  * Passes through the caller's ``Authorization`` header verbatim, so
    the browser-issued ``sk-tr-…`` key authenticates against the
    attested gateway exactly as before.
  * Surfaces the upstream's ``x-trustedrouter-provider`` and
    ``x-trustedrouter-served-model`` headers back to the browser so
    the "via {provider}" meta line in the playground works.
  * Limits exposure to one exact method and path so this cannot become a
    general-purpose authenticated hop.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

import anyio
import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

from trusted_router.auth import InferencePrincipal, Principal, SettingsDep
from trusted_router.config import Settings

log = logging.getLogger(__name__)
_STREAM_ERROR = (
    b'\n\ndata: {"error":{"message":"Upstream connection interrupted",'
    b'"type":"bad_gateway","code":502,"source":"router"}}\n\n'
)

# Headers we strip from the incoming browser request before forwarding
# (httpx will re-derive Host/Content-Length itself; hop-by-hop headers
# don't survive a proxy).
_REQUEST_HEADERS_TO_STRIP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "cookie",  # API keys go in Authorization, never cookies
    }
)

# Headers we strip from the upstream response before returning it.
# Preserve content-encoding: aiter_raw forwards encoded bytes unchanged.
_RESPONSE_HEADERS_TO_STRIP = frozenset(
    {
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def register_chat_proxy_routes(router: APIRouter | FastAPI) -> None:
    @router.post("/chat-proxy/v1/chat/completions")
    async def chat_proxy(
        request: Request,
        principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> StreamingResponse:
        # The auth dependency runs before this function, so invalid traffic
        # cannot read a body, allocate an outbound client, or hold a 300-second
        # upstream stream. Browser code currently uses this one exact path.
        return await _forward(request, "chat/completions", settings, principal)


async def _forward(
    request: Request, path: str, settings: Settings, principal: Principal
) -> StreamingResponse:
    upstream_base = _upstream_base_url(settings)
    upstream_url = f"{upstream_base}/v1/{path}"
    query = request.url.query
    if query:
        upstream_url = f"{upstream_url}?{query}"

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _REQUEST_HEADERS_TO_STRIP
    }
    request_id = request.state.request_id
    forward_headers["x-trustedrouter-request-id"] = request_id
    forward_headers["accept-encoding"] = "identity"
    # Read the entire request body into memory before forwarding.
    # The chat playground requests are small (a few KB of messages
    # JSON) so this is fine; streaming uploads aren't a use case here.
    body = await request.body()

    # Long timeout because chat completions can take a while; the
    # browser-side stream reader will time out independently if needed.
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    upstream_response: httpx.Response | None = None
    started = time.monotonic()
    response_bytes = 0
    metadata = {
        "request_id": request_id,
        "workspace_id": principal.workspace.id,
        "credential_id": principal.api_key.hash if principal.api_key else None,
    }
    log.info("chat_proxy.request_start %s", json.dumps(metadata), extra=metadata)

    def finish(outcome: str, status: int, error: str | None = None) -> None:
        fields = {
            **metadata, "outcome": outcome, "status": status, "error_type": error,
            "response_bytes": response_bytes,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        log.log(logging.ERROR if error else logging.INFO,
                "chat_proxy.request_end %s", json.dumps(fields), extra=fields)

    async def close() -> None:
        # Client cancellation must release both upstream resources too.
        with anyio.CancelScope(shield=True):
            try:
                if upstream_response is not None:
                    await upstream_response.aclose()
            finally:
                await client.aclose()

    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=forward_headers,
            content=body,
        )
        upstream_response = await client.send(upstream_request, stream=True)
        chunks = upstream_response.aiter_raw()
        first = await anext(chunks, b"")
    except httpx.HTTPError as exc:
        await close()
        finish("upstream_error", 502, type(exc).__name__)
        # Surface as a 502 — the chat client classifies this as
        # "Upstream provider hiccup" in friendlyStreamError().
        return StreamingResponse(
            content=iter(
                [
                    b'{"error":{"message":"upstream unreachable","type":"bad_gateway",'
                    b'"code":502,"source":"router"}}'
                ]
            ),
            status_code=502,
            media_type="application/json",
        )
    except BaseException:
        await close()
        finish("cancelled", 499)
        raise

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _RESPONSE_HEADERS_TO_STRIP
    }

    async def body_iter() -> AsyncIterator[bytes]:
        nonlocal response_bytes
        outcome, error = "cancelled", None
        try:
            if first:
                response_bytes += len(first)
                yield first
            async for chunk in chunks:
                response_bytes += len(chunk)
                yield chunk
            outcome = "complete"
        except httpx.HTTPError as exc:
            outcome, error = "upstream_error", type(exc).__name__
            if (
                upstream_response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "text/event-stream"
                and upstream_response.headers.get("content-encoding", "identity") == "identity"
            ):
                # Never replay a partially served request. Delimit even a partial
                # SSE frame and send an explicit failure, not a success [DONE].
                yield _STREAM_ERROR
            else:
                # Appending JSON/SSE to an opaque non-SSE or compressed body
                # would corrupt it and hide the transport failure.
                raise httpx.RemoteProtocolError("Upstream connection interrupted") from None
        finally:
            await close()
            finish(outcome, upstream_response.status_code, error)

    return StreamingResponse(
        body_iter(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


def _upstream_base_url(settings: Settings) -> str:
    # settings.api_base_url is "https://api.trustedrouter.com/v1" in
    # production. Strip the trailing /v1 so we can rebuild it from the
    # {path} parameter in the route — also future-proofs against
    # non-/v1 paths (e.g. /openai/v1/responses).
    base = settings.api_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


__all__ = ["register_chat_proxy_routes"]
