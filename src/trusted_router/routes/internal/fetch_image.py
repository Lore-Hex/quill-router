"""/internal/gateway/fetch-image — server-side image-URL fetcher.

When a chat request references an image by URL and the gateway cannot fetch
it directly, it proxies the fetch through this endpoint. The control plane
does:

  1. URL parse + scheme allowlist (http / https only)
  2. DNS resolve via socket.getaddrinfo
  3. Reject if any resolved IP is private / loopback / link-local /
     multicast / 0.0.0.0 / 169.254.x.x — same IP-class rules as the
     GCP-direct path's allowedImageIP (enclave-go's
     internal/llm/multimodal_direct.go)
  4. httpx GET with size cap (10 MiB), timeout (15 s), and a manual
     redirect chain (max 3) so each hop re-runs the SSRF check
  5. Return media_type + base64-encoded bytes; the enclave normalizes
     and embeds them in the upstream provider request

Trust property: this URL is metadata about user intent, not prompt content —
the same kind of metadata authorize/settle already see. The image bytes flow
back to the attested gateway, which normalizes them before calling providers.

Auth: same internal-gateway-token guard as the rest of /internal/*.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.schemas import GatewayFetchImageRequest
from trusted_router.services.safe_egress import (
    resolve_public_or_reject,
    validate_url_scheme,
)
from trusted_router.types import ErrorType

# Mirrored from enclave-go/internal/llm/multimodal.go const block. Keep
# in lockstep — both ends MUST agree on the size cap so a request that
# the enclave will accept doesn't get rejected here (or vice versa).
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 3
_FETCH_TIMEOUT_SECONDS = 15.0
_ACCEPT_HEADER = "image/png,image/jpeg"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


async def _fetch_with_redirect_chain(client: httpx.AsyncClient, url: str) -> tuple[str, bytes]:
    """Walk the redirect chain manually so each hop's hostname runs
    through _resolve_or_reject. httpx's built-in follow_redirects
    re-resolves DNS too but doesn't let us inject SSRF checks per hop."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        _, host = validate_url_scheme(current)
        resolve_public_or_reject(host)
        async with client.stream("GET", current, headers={"accept": _ACCEPT_HEADER}) as resp:
            if resp.status_code in _REDIRECT_STATUSES:
                next_url = resp.headers.get("location")
                if not next_url:
                    raise api_error(
                        400,
                        "image fetch: redirect without location",
                        ErrorType.BAD_REQUEST,
                    )
                current = str(httpx.URL(current).join(next_url))
                continue
            if resp.status_code != 200:
                raise api_error(
                    400,
                    f"image fetch: http {resp.status_code}",
                    ErrorType.BAD_REQUEST,
                )
            media_type = (resp.headers.get("content-type", "") or "").split(";")[0].strip().lower()
            buf = bytearray()
            async for chunk in resp.aiter_raw():
                buf.extend(chunk)
                if len(buf) > _MAX_IMAGE_BYTES:
                    raise api_error(
                        400, "image fetch: image too large", ErrorType.BAD_REQUEST
                    )
            return media_type, bytes(buf)
    raise api_error(
        400, "image fetch: too many redirects", ErrorType.BAD_REQUEST
    )


def register(router: APIRouter) -> None:
    @router.post("/internal/gateway/fetch-image")
    async def gateway_fetch_image(
        request: Request,
        body: GatewayFetchImageRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        url = body.url.strip()
        if not url:
            raise api_error(
                400, "image fetch: url is required", ErrorType.BAD_REQUEST
            )
        # Pre-flight scheme/host check before opening any client. The
        # actual per-hop SSRF check happens inside the redirect loop.
        validate_url_scheme(url)
        timeout = httpx.Timeout(_FETCH_TIMEOUT_SECONDS, connect=_FETCH_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=timeout
        ) as client:
            try:
                media_type, data = await _fetch_with_redirect_chain(client, url)
            except httpx.TimeoutException as exc:
                raise api_error(
                    400, "image fetch: timeout", ErrorType.BAD_REQUEST
                ) from exc
            except httpx.HTTPError as exc:
                raise api_error(
                    400, "image fetch: fetch failed", ErrorType.BAD_REQUEST
                ) from exc
        if not media_type:
            # Sniff a minimal media type — match the Go enclave's
            # contentTypeMedia + http.DetectContentType behavior on
            # magic bytes for png/jpeg. Anything else falls through to
            # normalizeImageBytes' "unsupported image media type" error
            # at the enclave; better to label it now so the caller
            # gets a clear diagnostic.
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif data[:3] == b"\xff\xd8\xff":
                media_type = "image/jpeg"
            else:
                media_type = "application/octet-stream"
        return {
            "data": {
                "media_type": media_type,
                "data_base64": base64.standard_b64encode(data).decode("ascii"),
            }
        }


__all__ = ["register"]
