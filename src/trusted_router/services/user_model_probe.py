from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from trusted_router.byok_crypto import decrypt_control_secret
from trusted_router.config import Settings
from trusted_router.services.safe_egress import aassert_public_url
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import UserProvidedModel
from trusted_router.user_model_rules import sign_request_body

_PROBE_TIMEOUT_SECONDS = 15.0  # per socket operation
_PROBE_TOTAL_SECONDS = 20.0  # hard deadline for the whole probe, redirects included
_PROBE_MAX_BYTES = 64 * 1024  # a canary answer is a few hundred bytes
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


async def probe_user_model(
    model: UserProvidedModel,
    settings: Settings,
) -> ProbeResult:
    checked_at = datetime.now(UTC)
    try:
        signing_secret = _decrypt_signing_secret(model, settings)
        endpoint_api_key = _decrypt_endpoint_key(model, settings)
        buffered = await _probe_once(
            model,
            settings,
            signing_secret=signing_secret,
            endpoint_api_key=endpoint_api_key,
            stream=False,
        )
        if not _valid_chat_completion(buffered):
            return _recorded_result(
                model,
                ok=False,
                detail="Endpoint response was not an OpenAI chat completion",
                checked_at=checked_at,
            )
        if model.supports_streaming:
            streamed = await _probe_once(
                model,
                settings,
                signing_secret=signing_secret,
                endpoint_api_key=endpoint_api_key,
                stream=True,
            )
            if not _valid_stream(streamed):
                return _recorded_result(
                    model,
                    ok=False,
                    detail="Endpoint response was not a valid chat completion stream",
                    checked_at=checked_at,
                )
    except httpx.TimeoutException:
        return _recorded_result(
            model,
            ok=False,
            detail="Endpoint probe timed out",
            checked_at=checked_at,
        )
    except httpx.HTTPError:
        return _recorded_result(
            model,
            ok=False,
            detail="Endpoint probe failed",
            checked_at=checked_at,
        )
    except Exception as exc:  # fail closed without echoing secret-bearing details
        return _recorded_result(
            model,
            ok=False,
            detail=f"Endpoint probe failed ({type(exc).__name__})",
            checked_at=checked_at,
        )
    return _recorded_result(
        model,
        ok=True,
        detail="Probe succeeded",
        checked_at=checked_at,
    )


async def _probe_once(
    model: UserProvidedModel,
    settings: Settings,
    *,
    signing_secret: str,
    endpoint_api_key: str | None,
    stream: bool,
) -> bytes:
    body = json.dumps(
        {
            "model": model.upstream_model_id,
            "messages": [
                {"role": "user", "content": "Reply with the single word: pong"}
            ],
            "max_tokens": 16,
            "stream": stream,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "TR-Signature": sign_request_body(signing_secret, body, datetime.now(UTC)),
    }
    if endpoint_api_key is not None:
        headers["authorization"] = f"Bearer {endpoint_api_key}"
    current = f"{model.endpoint_url.rstrip('/')}/chat/completions"
    origin = _origin_of(current)
    timeout = httpx.Timeout(_PROBE_TIMEOUT_SECONDS)
    allow_http = settings.environment in {"local", "test"}
    # httpx's read timeout resets on every socket read, so it never bounds a
    # trickling endpoint; asyncio.timeout is the actual deadline. The byte cap
    # keeps a chatty owner from filling control-plane memory: the canary
    # answer is a few hundred bytes.
    async with (
        asyncio.timeout(_PROBE_TOTAL_SECONDS),
        httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client,
    ):
        for _ in range(_MAX_REDIRECTS + 1):
            await aassert_public_url(current, allow_http=allow_http)
            hop_headers = dict(headers)
            if _origin_of(current) != origin:
                # Credentials are for the registered endpoint only. A redirect
                # to another origin must not carry the owner's upstream key or
                # a valid TR signature to whoever sits there.
                hop_headers.pop("authorization", None)
                hop_headers.pop("TR-Signature", None)
            async with client.stream(
                "POST", current, content=body, headers=hop_headers
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPError("redirect without location")
                    current = str(httpx.URL(current).join(location))
                    continue
                if not 200 <= response.status_code < 300:
                    raise httpx.HTTPStatusError(
                        "probe returned a non-success status",
                        request=response.request,
                        response=response,
                    )
                return await _read_capped(response, _PROBE_MAX_BYTES)
    raise httpx.TooManyRedirects("probe exceeded redirect limit")


def _origin_of(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    return (parsed.scheme, parsed.host, parsed.port)


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise httpx.HTTPError("probe response exceeded size cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _valid_chat_completion(raw: bytes) -> bool:
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    choices = body.get("choices") if isinstance(body, dict) else None
    return bool(
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
        and isinstance(choices[0].get("message"), dict)
    )


def _valid_stream(raw: bytes) -> bool:
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        payload = line.removeprefix(b"data:").strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            chunk: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(chunk, dict) and chunk.get("object") == "chat.completion.chunk":
            return True
    return False


def _decrypt_signing_secret(model: UserProvidedModel, settings: Settings) -> str:
    if model.encrypted_signing_secret is None:
        raise ValueError("missing signing secret")
    return decrypt_control_secret(
        model.encrypted_signing_secret,
        settings,
        workspace_id=model.owner_workspace_id,
        purpose=USER_MODEL_SIGNING_PURPOSE,
    )


def _decrypt_endpoint_key(
    model: UserProvidedModel,
    settings: Settings,
) -> str | None:
    if model.encrypted_endpoint_api_key is None:
        return None
    return decrypt_control_secret(
        model.encrypted_endpoint_api_key,
        settings,
        workspace_id=model.owner_workspace_id,
        purpose=USER_MODEL_ENDPOINT_KEY_PURPOSE,
    )


def _recorded_result(
    model: UserProvidedModel,
    *,
    ok: bool,
    detail: str,
    checked_at: datetime,
) -> ProbeResult:
    STORE.record_user_model_probe(
        model.id,
        status="ok" if ok else "failed",
        checked_at=checked_at.isoformat().replace("+00:00", "Z"),
    )
    return ProbeResult(ok=ok, detail=detail)
