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
forwards the request bytes-for-bytes to the same managed domain's attested API
host and streams the response bytes back.
The proxy:

  * NEVER deserializes / inspects / logs the request or response body.
    It pipes raw bytes only — same privacy posture as the attested
    gateway forwarding to upstream providers.
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

from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

from trusted_router.auth import InferencePrincipal, SettingsDep
from trusted_router.config import Settings
from trusted_router.domains import (
    configured_control_domains,
    request_api_base_url,
    request_hostname,
)
from trusted_router.errors import api_error
from trusted_router.types import ErrorType

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
# Same hop-by-hop list plus content-length (re-set by Starlette) and
# content-encoding (we want raw decoded bytes through to the browser).
_RESPONSE_HEADERS_TO_STRIP = frozenset(
    {
        "content-length",
        "content-encoding",
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
        _principal: InferencePrincipal,
        settings: SettingsDep,
    ) -> StreamingResponse:
        # The auth dependency runs before this function, so invalid traffic
        # cannot read a body, allocate an outbound client, or hold a 300-second
        # upstream stream. Browser code currently uses this one exact path.
        return await _forward(request, "chat/completions", settings)


async def _forward(
    request: Request, path: str, settings: Settings
) -> StreamingResponse:
    upstream_base = _upstream_base_url(request, settings)
    upstream_url = f"{upstream_base}/v1/{path}"
    query = request.url.query
    if query:
        upstream_url = f"{upstream_url}?{query}"

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _REQUEST_HEADERS_TO_STRIP
    }
    # Read the entire request body into memory before forwarding.
    # The chat playground requests are small (a few KB of messages
    # JSON) so this is fine; streaming uploads aren't a use case here.
    body = await request.body()

    # Long timeout because chat completions can take a while; the
    # browser-side stream reader will time out independently if needed.
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=forward_headers,
            content=body,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
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

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _RESPONSE_HEADERS_TO_STRIP
    }

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


def _upstream_base_url(request: Request, settings: Settings) -> str:
    # Keep each operational alias on its own attested API hostname. The domain
    # helper derives only from the configured first-party allowlist. Reject an
    # unknown or malformed Host before allocating an outbound client: the edge
    # should never send one here, and silently falling back would let a valid
    # browser key turn a misdirected request into canonical API traffic.
    if request_hostname(request) not in configured_control_domains(settings):
        raise api_error(
            421,
            "Chat proxy request Host is not a configured domain",
            ErrorType.BAD_REQUEST,
        )
    base = request_api_base_url(request, settings).rstrip("/")
    # Strip the trailing /v1 so we can rebuild it from the {path} parameter in
    # the route — also future-proofs against non-/v1 paths (for example,
    # /openai/v1/responses).
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


__all__ = ["register_chat_proxy_routes"]
