"""POST /chat-proxy/v1/* — same-origin streaming proxy for the chat playground.

Background
==========
TR's control plane (trustedrouter.com) intentionally does NOT serve
the inference routes in production — `_control_plane_inference_enabled`
in main.py restricts that to local/test, so prompts can only execute
through the attested enclave at api.quillrouter.com.

That's the right policy for SDK / production traffic, but it breaks
the browser chat playground at trustedrouter.com/chat: cross-origin
fetch from trustedrouter.com → api.quillrouter.com is hard-blocked by
CORS (the attested gateway returns 401 to OPTIONS preflight with no
ACAO headers).

This module adds a minimal same-origin streaming pipe at
``/chat-proxy/v1/{path:path}`` that forwards the request body bytes-
for-bytes to api.quillrouter.com and streams the response bytes back.
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
  * Limits exposure to a single path prefix
    (``/chat-proxy/v1/``) so this is unambiguously the chat playground's
    proxy, not a general-purpose hop.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings

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
    @router.api_route(
        "/chat-proxy/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def chat_proxy(
        request: Request,
        path: str,
        settings: SettingsDep,
    ) -> StreamingResponse:
        return await _forward(request, path, settings)


async def _forward(
    request: Request, path: str, settings: Settings
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
            content=iter([b'{"error":{"message":"upstream unreachable","type":"bad_gateway","code":502}}']),
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


def _upstream_base_url(settings: Settings) -> str:
    # settings.api_base_url is "https://api.quillrouter.com/v1" in
    # production. Strip the trailing /v1 so we can rebuild it from the
    # {path} parameter in the route — also future-proofs against
    # non-/v1 paths (e.g. /openai/v1/responses).
    base = settings.api_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


__all__ = ["register_chat_proxy_routes"]
