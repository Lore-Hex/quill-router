from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from trusted_router.config import Settings
from trusted_router.regions import choose_region, region_payload
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage_models import SyntheticProbeSample


@dataclass(frozen=True)
class SyntheticTarget:
    name: str
    api_base_url: str
    region: str | None = None


def configured_targets(settings: Settings) -> list[SyntheticTarget]:
    targets = [SyntheticTarget("canonical", settings.api_base_url, choose_region(settings))]
    for region in region_payload(settings):
        name = str(region["id"])
        api_base_url = str(region["api_base_url"])
        if all(existing.api_base_url != api_base_url for existing in targets):
            targets.append(SyntheticTarget(name, api_base_url, name))
    return targets


async def run_synthetic_once(
    settings: Settings,
    *,
    monitor_region: str | None = None,
    api_key: str | None = None,
) -> list[SyntheticProbeSample]:
    region = monitor_region or settings.synthetic_monitor_region or choose_region(settings)
    key = api_key or settings.synthetic_monitor_api_key
    timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    samples: list[SyntheticProbeSample] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for target in configured_targets(settings):
            samples.append(await tls_health_probe(client, target, monitor_region=region))
            samples.append(await attestation_nonce_probe(client, target, monitor_region=region))
            if key:
                samples.append(
                    await openai_chat_pong_probe(
                        client,
                        target,
                        monitor_region=region,
                        api_key=key,
                        model=settings.synthetic_monitor_model,
                    )
                )
                samples.append(
                    await responses_pong_probe(
                        client,
                        target,
                        monitor_region=region,
                        api_key=key,
                        model=settings.synthetic_monitor_model,
                    )
                )
    return samples


async def tls_health_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
) -> SyntheticProbeSample:
    url = _root_url(target.api_base_url, "/health")
    started = time.perf_counter()
    try:
        response = await client.get(url)
        latency_ms = _elapsed_ms(started)
        ok = response.status_code == 200 and _health_ok(response)
        # The attested gateway currently protects every route except
        # /attestation. A 401 with the standard API-key error still proves TLS
        # termination and gateway request handling are alive; the nonce probe
        # below verifies the trust-specific path.
        if not ok and response.status_code == 401 and _invalid_api_key(response):
            ok = True
        return _sample(
            "tls_health",
            target,
            monitor_region,
            url,
            status="up" if ok else "down",
            latency_milliseconds=latency_ms,
            ttfb_milliseconds=latency_ms,
            http_status=response.status_code,
            error_type=None if ok else "bad_health_response",
        )
    except httpx.HTTPError as exc:
        return _sample(
            "tls_health",
            target,
            monitor_region,
            url,
            status="down",
            latency_milliseconds=_elapsed_ms(started),
            error_type=exc.__class__.__name__,
        )


async def attestation_nonce_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
) -> SyntheticProbeSample:
    nonce = secrets.token_hex(16)
    url = _root_url(target.api_base_url, f"/attestation?nonce={nonce}")
    started = time.perf_counter()
    try:
        response = await client.get(url)
        latency_ms = _elapsed_ms(started)
        evidence = _attestation_evidence(response.content, nonce)
        ok = response.status_code == 200 and evidence["nonce_ok"]
        return _sample(
            "attestation_nonce",
            target,
            monitor_region,
            url,
            status="up" if ok else "trust_degraded",
            latency_milliseconds=latency_ms,
            ttfb_milliseconds=latency_ms,
            http_status=response.status_code,
            error_type=None if ok else str(evidence["error_type"]),
            attestation_digest=_evidence_str(evidence, "attestation_digest"),
            source_commit=_evidence_str(evidence, "source_commit"),
        )
    except httpx.HTTPError as exc:
        return _sample(
            "attestation_nonce",
            target,
            monitor_region,
            url,
            status="trust_degraded",
            latency_milliseconds=_elapsed_ms(started),
            error_type=exc.__class__.__name__,
        )


async def openai_chat_pong_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    model: str,
) -> SyntheticProbeSample:
    url = _api_url(target.api_base_url, "/chat/completions")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "reply exactly PONG"}],
        "max_tokens": 4,
        "temperature": 0,
        "metadata": {"trustedrouter_synthetic": "true"},
    }
    started = time.perf_counter()
    try:
        response = await client.post(url, json=body, headers=_auth_headers(api_key))
        latency_ms = _elapsed_ms(started)
        text = _chat_text(response)
        ok = response.status_code == 200 and _pong_matches(text)
        return _sample(
            "openai_sdk_pong",
            target,
            monitor_region,
            url,
            status="up" if ok else "down",
            latency_milliseconds=latency_ms,
            ttfb_milliseconds=latency_ms,
            http_status=response.status_code,
            error_type=None if ok else "pong_mismatch",
            model=model,
            output_match=ok,
        )
    except httpx.HTTPError as exc:
        return _sample(
            "openai_sdk_pong",
            target,
            monitor_region,
            url,
            status="down",
            latency_milliseconds=_elapsed_ms(started),
            error_type=exc.__class__.__name__,
            model=model,
            output_match=False,
        )


async def responses_pong_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    model: str,
) -> SyntheticProbeSample:
    url = _api_url(target.api_base_url, "/responses")
    body = {
        "model": model,
        "input": "reply exactly PONG",
        "max_output_tokens": 4,
        "temperature": 0,
        "metadata": {"trustedrouter_synthetic": "true"},
    }
    started = time.perf_counter()
    try:
        response = await client.post(url, json=body, headers=_auth_headers(api_key))
        latency_ms = _elapsed_ms(started)
        text = _responses_text(response)
        ok = response.status_code == 200 and _pong_matches(text)
        return _sample(
            "responses_pong",
            target,
            monitor_region,
            url,
            status="up" if ok else "down",
            latency_milliseconds=latency_ms,
            ttfb_milliseconds=latency_ms,
            http_status=response.status_code,
            error_type=None if ok else "pong_mismatch",
            model=model,
            output_match=ok,
        )
    except httpx.HTTPError as exc:
        return _sample(
            "responses_pong",
            target,
            monitor_region,
            url,
            status="down",
            latency_milliseconds=_elapsed_ms(started),
            error_type=exc.__class__.__name__,
            model=model,
            output_match=False,
        )


async def gateway_billing_probe(
    client: httpx.AsyncClient,
    *,
    control_plane_base_url: str,
    monitor_region: str,
    api_key: str,
    internal_token: str,
    model: str,
) -> list[SyntheticProbeSample]:
    base = control_plane_base_url.rstrip("/")
    authorize_url = f"{base}/v1/internal/gateway/authorize"
    settle_url = f"{base}/v1/internal/gateway/settle"
    headers = {"x-trustedrouter-internal-token": internal_token}
    started = time.perf_counter()
    target = SyntheticTarget("control-plane", control_plane_base_url, None)
    try:
        authorize = await client.post(
            authorize_url,
            headers=headers,
            json={
                "api_key_lookup_hash": lookup_hash_api_key(api_key),
                "model": model,
                "estimated_input_tokens": 1,
                "max_output_tokens": 1,
                "metadata": {"trustedrouter_synthetic": "true"},
            },
        )
        if authorize.status_code != 200:
            return [
                _sample(
                    "gateway_authorize_settle",
                    target,
                    monitor_region,
                    authorize_url,
                    status="down",
                    latency_milliseconds=_elapsed_ms(started),
                    http_status=authorize.status_code,
                    error_type="authorize_failed",
                    model=model,
                )
            ]
        data = authorize.json()["data"]
        settle = await client.post(
            settle_url,
            headers=headers,
            json={
                "authorization_id": data["authorization_id"],
                "input_tokens": 1,
                "output_tokens": 1,
                "request_id": f"synthetic-{uuid.uuid4().hex}",
                "finish_reason": "stop",
                "status": "success",
                "streamed": False,
                "elapsed_seconds": 0.001,
                "app": "TrustedRouter Synthetic",
                "model": data.get("model"),
                "selected_endpoint": data.get("endpoint_id"),
                "metadata": {"trustedrouter_synthetic": "true"},
            },
        )
        settle_data = settle.json().get("data", {}) if settle.content else {}
        ok = settle.status_code == 200 and bool(settle_data.get("settled"))
        return [
            _sample(
                "gateway_authorize_settle",
                target,
                monitor_region,
                settle_url,
                status="up" if ok else "down",
                latency_milliseconds=_elapsed_ms(started),
                http_status=settle.status_code,
                error_type=None if ok else "settle_failed",
                model=model,
                selected_model=settle_data.get("model"),
                selected_provider=settle_data.get("provider"),
                generation_id=settle_data.get("generation_id"),
                cost_microdollars=int(settle_data.get("cost_microdollars") or 0),
            )
        ]
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        return [
            _sample(
                "gateway_authorize_settle",
                target,
                monitor_region,
                authorize_url,
                status="down",
                latency_milliseconds=_elapsed_ms(started),
                error_type=exc.__class__.__name__,
                model=model,
            )
        ]


async def gateway_fallback_probe(
    client: httpx.AsyncClient,
    *,
    control_plane_base_url: str,
    monitor_region: str,
    api_key: str,
    internal_token: str,
    model: str,
) -> list[SyntheticProbeSample]:
    base = control_plane_base_url.rstrip("/")
    authorize_url = f"{base}/v1/internal/gateway/authorize"
    settle_url = f"{base}/v1/internal/gateway/settle"
    headers = {"x-trustedrouter-internal-token": internal_token}
    started = time.perf_counter()
    target = SyntheticTarget("control-plane", control_plane_base_url, None)
    try:
        authorize = await client.post(
            authorize_url,
            headers=headers,
            json={
                "api_key_lookup_hash": lookup_hash_api_key(api_key),
                "model": model,
                "estimated_input_tokens": 1,
                "max_output_tokens": 1,
                "metadata": {"trustedrouter_synthetic": "true", "probe": "fallback"},
            },
        )
        if authorize.status_code != 200:
            return [
                _sample(
                    "provider_fallback",
                    target,
                    monitor_region,
                    authorize_url,
                    status="routing_degraded",
                    latency_milliseconds=_elapsed_ms(started),
                    http_status=authorize.status_code,
                    error_type="authorize_failed",
                    model=model,
                )
            ]
        data = authorize.json()["data"]
        candidates = data.get("route_candidates") or []
        if not isinstance(candidates, list) or len(candidates) < 2:
            return [
                _sample(
                    "provider_fallback",
                    target,
                    monitor_region,
                    authorize_url,
                    status="routing_degraded",
                    latency_milliseconds=_elapsed_ms(started),
                    http_status=authorize.status_code,
                    error_type="insufficient_route_candidates",
                    model=model,
                )
            ]
        fallback = candidates[1]
        settle = await client.post(
            settle_url,
            headers=headers,
            json={
                "authorization_id": data["authorization_id"],
                "input_tokens": 1,
                "output_tokens": 1,
                "request_id": f"synthetic-fallback-{uuid.uuid4().hex}",
                "finish_reason": "stop",
                "status": "success",
                "streamed": False,
                "elapsed_seconds": 0.001,
                "app": "TrustedRouter Synthetic",
                "model": fallback.get("model"),
                "selected_endpoint": fallback.get("endpoint_id"),
                "metadata": {"trustedrouter_synthetic": "true", "probe": "fallback"},
            },
        )
        settle_data = settle.json().get("data", {}) if settle.content else {}
        expected_endpoint = fallback.get("endpoint_id")
        ok = (
            settle.status_code == 200
            and bool(settle_data.get("settled"))
            and settle_data.get("endpoint_id") == expected_endpoint
        )
        return [
            _sample(
                "provider_fallback",
                target,
                monitor_region,
                settle_url,
                status="up" if ok else "routing_degraded",
                latency_milliseconds=_elapsed_ms(started),
                http_status=settle.status_code,
                error_type=None if ok else "fallback_settle_failed",
                model=model,
                selected_model=settle_data.get("model") or fallback.get("model"),
                selected_provider=settle_data.get("provider") or fallback.get("provider"),
                generation_id=settle_data.get("generation_id"),
                cost_microdollars=int(settle_data.get("cost_microdollars") or 0),
            )
        ]
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        return [
            _sample(
                "provider_fallback",
                target,
                monitor_region,
                authorize_url,
                status="routing_degraded",
                latency_milliseconds=_elapsed_ms(started),
                error_type=exc.__class__.__name__,
                model=model,
            )
        ]


def _sample(
    probe_type: str,
    target: SyntheticTarget,
    monitor_region: str,
    target_url: str,
    *,
    status: str,
    latency_milliseconds: int | None = None,
    ttfb_milliseconds: int | None = None,
    http_status: int | None = None,
    error_type: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    selected_provider: str | None = None,
    selected_model: str | None = None,
    generation_id: str | None = None,
    attestation_digest: str | None = None,
    source_commit: str | None = None,
    cost_microdollars: int = 0,
    output_match: bool | None = None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=f"syn-{uuid.uuid4().hex}",
        probe_type=probe_type,
        target=target.name,
        target_url=target_url,
        monitor_region=monitor_region,
        target_region=target.region,
        status=status,
        latency_milliseconds=latency_milliseconds,
        ttfb_milliseconds=ttfb_milliseconds,
        http_status=http_status,
        error_type=error_type,
        provider=provider,
        model=model,
        selected_provider=selected_provider,
        selected_model=selected_model,
        generation_id=generation_id,
        attestation_digest=attestation_digest,
        source_commit=source_commit,
        cost_microdollars=cost_microdollars,
        output_match=output_match,
    )


def _root_url(api_base_url: str, path: str) -> str:
    root = api_base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return urljoin(root + "/", path.lstrip("/"))


def _api_url(api_base_url: str, path: str) -> str:
    return urljoin(api_base_url.rstrip("/") + "/", path.lstrip("/"))


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {api_key}"}


def _health_ok(response: httpx.Response) -> bool:
    try:
        return response.json().get("status") == "ok"
    except ValueError:
        return False


def _invalid_api_key(response: httpx.Response) -> bool:
    try:
        error = response.json().get("error", {})
        return "invalid api key" in str(error.get("message", "")).lower()
    except ValueError:
        return False


def _pong_matches(text: str) -> bool:
    """Accept any output that contains the literal word PONG (case
    insensitive). LLMs reliably emit the word but sometimes wrap it in
    quotes, append punctuation, or prefix a token of whitespace. We only
    want to flag a hard miss (model returned something unrelated, empty
    body, or wrong language)."""
    return "pong" in text.casefold()


def _chat_text(response: httpx.Response) -> str:
    if response.status_code != 200:
        return ""
    try:
        choices = response.json().get("choices") or []
        return str(choices[0].get("message", {}).get("content") or "")
    except (ValueError, IndexError, AttributeError):
        return ""


def _responses_text(response: httpx.Response) -> str:
    if response.status_code != 200:
        return ""
    try:
        output = response.json().get("output") or []
        content = output[0].get("content") or []
        return str(content[0].get("text") or "")
    except (ValueError, IndexError, AttributeError):
        return ""


def _attestation_evidence(body: bytes, nonce_hex: str) -> dict[str, str | bool | None]:
    text = body.decode("utf-8", errors="ignore").strip()
    if text.count(".") >= 2:
        payload = _decode_jwt_payload(text)
        nonces = payload.get("eat_nonce") or payload.get("nonces") or payload.get("nonce") or []
        if isinstance(nonces, str):
            nonce_ok = nonce_hex in {nonces}
        elif isinstance(nonces, list):
            nonce_ok = nonce_hex in {str(item) for item in nonces}
        else:
            nonce_ok = False
        return {
            "nonce_ok": nonce_ok,
            "error_type": None if nonce_ok else "nonce_missing",
            "attestation_digest": _claim(payload, "image_digest", "submods.container.image_digest"),
            "source_commit": _claim(payload, "source_commit", "submods.container.source_commit"),
        }
    return {
        "nonce_ok": False,
        "error_type": "unsupported_attestation_format",
        "attestation_digest": None,
        "source_commit": None,
    }


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        value = json.loads(decoded.decode("utf-8"))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _claim(payload: dict[str, Any], *paths: str) -> str | None:
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current is not None:
            return str(current)
    return None


def _evidence_str(evidence: dict[str, str | bool | None], key: str) -> str | None:
    value = evidence.get(key)
    return value if isinstance(value, str) else None


def _elapsed_ms(started: float) -> int:
    return max(1, int(round((time.perf_counter() - started) * 1000)))
