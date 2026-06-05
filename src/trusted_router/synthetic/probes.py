from __future__ import annotations

import base64
import json
import random
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
from trusted_router.storage_models import ProviderBenchmarkSample, SyntheticProbeSample
from trusted_router.types import UsageType


@dataclass(frozen=True)
class SyntheticTarget:
    name: str
    api_base_url: str
    region: str | None = None
    # Cloud Run direct URL for this region's control plane. When set,
    # the synthetic monitor probes /health here too — separately from
    # api_base_url's enclave probe — so we get a distinct per-region
    # signal even when api_base_url's regional hostname CNAMEs to the
    # global LB (cold regions, or warm regions whose ACME cert hasn't
    # been issued yet because the MIG is at targetSize=0).
    #
    # None for the canonical target since that probe already hits the
    # global enclave LB by definition.
    control_plane_url: str | None = None


def configured_targets(settings: Settings) -> list[SyntheticTarget]:
    targets = [SyntheticTarget("canonical", settings.api_base_url, choose_region(settings))]
    for region in region_payload(settings):
        name = str(region["id"])
        api_base_url = str(region["api_base_url"])
        control_plane_url = region.get("control_plane_url") or None
        # If the api_base_url is already represented (e.g. the primary
        # region whose api_base_url == settings.api_base_url), skip
        # adding a duplicate enclave target — but DO still attach the
        # control_plane_url to the canonical target so we don't lose
        # the per-region health probe.
        existing = next((t for t in targets if t.api_base_url == api_base_url), None)
        if existing is not None:
            if control_plane_url and existing.control_plane_url is None:
                # Replace canonical target with one carrying the
                # primary's Cloud Run direct URL.
                targets[targets.index(existing)] = SyntheticTarget(
                    existing.name,
                    existing.api_base_url,
                    existing.region,
                    control_plane_url,
                )
            continue
        targets.append(
            SyntheticTarget(name, api_base_url, name, control_plane_url)
        )
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
            # Per-region control plane health via Cloud Run direct URL.
            # tls_health above probes target.api_base_url which is the
            # ENCLAVE (api-{region}.quillrouter.com) — that path can be
            # broken by an enclave-side issue (MIG at size 0, ACME cert
            # not issued, etc.) while the regional Cloud Run is fine.
            # This separate probe pins the control-plane signal per
            # region so dashboards can tell "the Cloud Run instance is
            # up but the regional enclave isn't" from "the whole
            # region is dead".
            if target.control_plane_url:
                samples.append(
                    await control_plane_health_probe(
                        client, target, monitor_region=region
                    )
                )
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


async def control_plane_health_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
) -> SyntheticProbeSample:
    """Probe `/health` on the per-region Cloud Run direct URL.

    Distinct from tls_health_probe (which hits the enclave-fronted
    api_base_url) — this one bypasses the enclave LB entirely and
    pins the request to the specific Cloud Run service running in
    `target.region`. It's the only way to tell:

      * "the Cloud Run instance in us-east4 is fine but its enclave
        cert hasn't issued yet" (control_plane up, tls_health down),
      * "the regional Cloud Run is OOM-killing" (control_plane down,
        tls_health up because LB routes around to a different region).

    The endpoint is /health (not /healthz) — that's what FastAPI
    registered in main.py: `@router.get("/health")`. /healthz returns
    401 because it falls through to the auth-required catch-all.
    """
    if not target.control_plane_url:
        return _sample(
            "control_plane_health",
            target,
            monitor_region,
            "(no control_plane_url configured)",
            status="down",
            latency_milliseconds=0,
            error_type="missing_control_plane_url",
        )
    url = _root_url(target.control_plane_url, "/health")
    started = time.perf_counter()
    try:
        response = await client.get(url)
        latency_ms = _elapsed_ms(started)
        ok = response.status_code == 200 and _health_ok(response)
        return _sample(
            "control_plane_health",
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
            "control_plane_health",
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
        # Reverted from "Respond with only the word PONG." back to
        # "reply exactly PONG" — the original phrasing worked at
        # 99.97% uptime for ~24h on the same monitor pool, then the
        # rephrase coincided with a surge to 100% pong_mismatch at
        # 06:00Z 2026-06-02. DeepSeek V4 Flash (current pool leader)
        # appears to interpret the new phrasing differently — maybe
        # refusing, maybe wrapping in markdown the extractor doesn't
        # reach. Reverting to the known-good prompt while we
        # investigate the underlying response shape.
        "messages": [{"role": "user", "content": "reply exactly PONG"}],
        # max_tokens stays at 128 so reasoning models (kimi-k2.6,
        # glm-4.6) in the rollover tail still finish their thinking
        # phase if they're ever reached.
        "max_tokens": 128,
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
        # Same prompt as chat-completions — see that probe's revert
        # comment. Original phrasing worked, rephrase coincided with
        # 100% failure surge on 2026-06-02.
        "input": "reply exactly PONG",
        # See chat-completions probe — same reason: reasoning models in
        # the monitor pool need headroom past their thinking phase.
        "max_output_tokens": 128,
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


# ---------------------------------------------------------------------------
# Provider/model rotation probe — a synthetic "user" that exercises every
# provider+model reachable via a prepaid endpoint, measuring TTFB (first byte)
# and TTFT (first content token) from real streaming responses. Feeds the SAME
# ProviderBenchmarkSample store as organic production traffic (tagged
# source="synthetic"), so the public leaderboard and the measured-routing
# snapshot get coverage for models with little/no organic traffic yet — and we
# get a daily API-drift signal. Deliberately NOT a SyntheticProbeSample: it
# never touches the /status router-health SLO or its burn-rate alerts.
# ---------------------------------------------------------------------------


def rotation_candidates() -> dict[str, list[str]]:
    """Map each provider to the model IDs it serves via a prepaid (Credits)
    endpoint. Iterates ENDPOINTS rather than Model.prepaid_available (a catalog
    dedup marker) so supplemental provider-native models are covered too."""
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS

    pool: dict[str, list[str]] = {}
    for endpoint in MODEL_ENDPOINTS.values():
        if endpoint.usage_type != "Credits":
            continue
        model = MODELS.get(endpoint.model_id)
        provider = PROVIDERS.get(endpoint.provider)
        if model is None or provider is None:
            continue
        if not model.supports_chat or not provider.supports_chat:
            continue
        models = pool.setdefault(endpoint.provider, [])
        if endpoint.model_id not in models:
            models.append(endpoint.model_id)
    return pool


def choose_rotation_target(
    pool: dict[str, list[str]], rng: random.Random
) -> tuple[str, str] | None:
    """Two-stage random pick: uniform over providers, then uniform over that
    provider's models — equal airtime per provider regardless of catalog size."""
    providers = sorted(provider for provider, models in pool.items() if models)
    if not providers:
        return None
    provider = rng.choice(providers)
    return provider, rng.choice(sorted(set(pool[provider])))


def _provider_display_name(provider: str) -> str:
    from trusted_router.catalog import PROVIDERS

    entry = PROVIDERS.get(provider)
    return entry.name if entry is not None else provider


def _sse_line_has_content(line: str) -> bool:
    """True if an SSE `data:` line carries a visible content/reasoning delta."""
    line = line.strip()
    if not line.startswith("data:"):
        return False
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return False
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    for choice in data.get("choices") or []:
        delta = choice.get("delta") or {}
        if (
            delta.get("content")
            or delta.get("reasoning_content")
            or delta.get("reasoning")
            or delta.get("text")
            or delta.get("output_text")
        ):
            return True
        message = choice.get("message") or {}
        if (
            message.get("content")
            or message.get("reasoning_content")
            or message.get("reasoning")
            or message.get("text")
        ):
            return True
        if choice.get("text"):
            return True
    return False


def _sse_line_error(line: str) -> tuple[str, int | None, str | None] | None:
    """Return an OpenAI-style SSE error if the data line carries one."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    error_type = str(error.get("type") or "provider_error")
    message = str(error.get("message") or "") or None
    status_raw = error.get("status") or error.get("code") or error.get("status_code")
    status: int | None
    try:
        status = int(status_raw) if status_raw is not None else None
    except (TypeError, ValueError):
        status = None
    return _rotation_error_type(error_type, status, message), status, message


def _response_error(response: httpx.Response) -> tuple[str, int | None, str | None]:
    try:
        payload = response.json()
    except ValueError:
        return f"http_{response.status_code}", response.status_code, None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"http_{response.status_code}", response.status_code, None
    error_type = str(error.get("type") or f"http_{response.status_code}")
    message = str(error.get("message") or "") or None
    status_raw = error.get("status") or error.get("code") or error.get("status_code")
    try:
        status = int(status_raw) if status_raw is not None else response.status_code
    except (TypeError, ValueError):
        status = response.status_code
    return _rotation_error_type(error_type, status, message), status, message


_UNSUPPORTED_ROUTE_ERROR_TYPES = frozenset(
    {
        "model_not_found",
        "model_not_available",
        "not_found",
        "not_supported",
        "unsupported",
        "unsupported_model",
        "unsupported_provider",
        "unsupported_route",
    }
)

_PROBE_CONFIG_ERROR_TYPES = frozenset(
    {
        "bad_request",
        "invalid_request",
        "invalid_request_error",
        "invalid_request_error_type",
    }
)

_UNSUPPORTED_ROUTE_MESSAGE_MARKERS = (
    "model not found",
    "model_not_found",
    "unknown model",
    "invalid model",
    "no such model",
    "model does not exist",
    "does not exist",
    "not available",
    "unavailable",
    "not enabled",
    "not authorized",
    "not permitted",
    "does not support",
    "not supported",
    "unsupported",
    "no endpoint",
    "no route",
)

_PROBE_CONFIG_MESSAGE_MARKERS = (
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
)


def _rotation_error_type(
    error_type: str,
    status: int | None,
    message: str | None,
) -> str:
    raw_type = error_type.casefold()
    raw_message = (message or "").casefold()
    if raw_type in _UNSUPPORTED_ROUTE_ERROR_TYPES or any(
        marker in raw_message for marker in _UNSUPPORTED_ROUTE_MESSAGE_MARKERS
    ):
        return "unsupported_route"
    if raw_type in _PROBE_CONFIG_ERROR_TYPES or (
        status in {400, 422}
        and any(marker in raw_message for marker in _PROBE_CONFIG_MESSAGE_MARKERS)
    ):
        return "probe_config_error"
    if status in {401, 403}:
        return "provider_auth_config"
    return error_type


async def provider_rotation_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    provider: str,
    model: str,
) -> ProviderBenchmarkSample:
    """Stream a tiny request to one provider+model and measure TTFB (first
    byte) and TTFT (first content token). Pins `provider.only` so the sample is
    attributed to the intended upstream; records the actually-served
    provider/model from the provenance headers when present. Output caps stay
    small, with a higher cap only for reasoning-heavy models that otherwise
    consume the whole budget before emitting visible content. We never assert
    the content — we measure token *flow*, not text."""
    url = _api_url(target.api_base_url, "/chat/completions")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "reply exactly PONG"}],
        "max_tokens": _rotation_max_tokens(provider, model),
        "stream": True,
        "provider": {"only": [provider]},
        "metadata": {"trustedrouter_synthetic": "true"},
    }
    if not _rotation_omits_temperature(provider, model):
        body["temperature"] = 0
    started = time.perf_counter()
    ttfb_ms: int | None = None
    ttft_ms: int | None = None
    served_provider = provider
    served_model = model
    try:
        async with client.stream(
            "POST", url, json=body, headers=_auth_headers(api_key)
        ) as response:
            served_provider = response.headers.get("x-trustedrouter-provider") or provider
            served_model = response.headers.get("x-trustedrouter-served-model") or model
            if response.status_code != 200:
                await response.aread()
                error_type, error_status, _message = _response_error(response)
                return _rotation_error_sample(
                    served_provider,
                    served_model,
                    region=monitor_region,
                    elapsed_ms=_elapsed_ms(started),
                    error_status=error_status,
                    error_type=error_type,
                )
            tail = ""
            stream_error: tuple[str, int | None, str | None] | None = None
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                now = _elapsed_ms(started)
                if ttfb_ms is None:
                    ttfb_ms = now
                tail += chunk.decode("utf-8", "ignore")
                lines = tail.split("\n")
                tail = lines.pop()
                for line in lines:
                    stream_error = _sse_line_error(line)
                    if stream_error is not None:
                        break
                    if ttft_ms is None and _sse_line_has_content(line):
                        ttft_ms = now
                if stream_error is not None:
                    break
            elapsed_ms = _elapsed_ms(started)
            if stream_error is not None:
                error_type, status, _message = stream_error
                return _rotation_error_sample(
                    served_provider,
                    served_model,
                    region=monitor_region,
                    elapsed_ms=elapsed_ms,
                    error_status=status or 502,
                    error_type=error_type,
                )
    except (httpx.HTTPError, ValueError) as exc:
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=_elapsed_ms(started),
            error_status=None,
            error_type=exc.__class__.__name__,
        )
    if ttft_ms is None:
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=elapsed_ms,
            error_status=None,
            error_type="empty_stream",
        )
    return ProviderBenchmarkSample(
        id=f"bench-{uuid.uuid4().hex}",
        model=served_model,
        provider=served_provider,
        provider_name=_provider_display_name(served_provider),
        status="success",
        usage_type=UsageType.CREDITS,
        streamed=True,
        elapsed_milliseconds=elapsed_ms,
        first_token_milliseconds=ttft_ms,
        ttfb_milliseconds=ttfb_ms,
        finish_reason="stop",
        region=monitor_region,
        source="synthetic",
    )


def _rotation_max_tokens(provider: str, model: str) -> int:
    provider_l = provider.lower()
    model_l = model.lower()
    if provider_l == "openai" and (
        "/o1" in model_l
        or "/o3" in model_l
        or "/o4" in model_l
        or "/gpt-5" in model_l
    ):
        return 128
    if (
        "kimi-k2" in model_l
        or "grok" in model_l
        or "claude-opus" in model_l
        or "gpt-oss" in model_l
        or "glm-4.6" in model_l
        or "glm-4.7" in model_l
        or "glm-5" in model_l
        or "reasoning" in model_l
        or "thinking" in model_l
    ):
        return 128
    return 16


def _rotation_omits_temperature(provider: str, model: str) -> bool:
    provider_l = provider.lower()
    model_l = model.lower()
    return (
        (provider_l == "kimi" and "kimi-k2." in model_l)
        or (
            provider_l == "openai"
            and (
                "/o1" in model_l
                or "/o3" in model_l
                or "/o4" in model_l
                or "/gpt-5" in model_l
            )
        )
        or (
            provider_l == "anthropic"
            and ("claude-opus-4.7" in model_l or "claude-opus-4.8" in model_l)
        )
    )


def _rotation_error_sample(
    provider: str,
    model: str,
    *,
    region: str,
    elapsed_ms: int,
    error_status: int | None,
    error_type: str,
) -> ProviderBenchmarkSample:
    status = "unsupported" if _rotation_error_excluded_from_uptime(error_type) else "error"
    return ProviderBenchmarkSample(
        id=f"bench-{uuid.uuid4().hex}",
        model=model,
        provider=provider,
        provider_name=_provider_display_name(provider),
        status=status,
        usage_type=UsageType.CREDITS,
        streamed=True,
        elapsed_milliseconds=elapsed_ms,
        first_token_milliseconds=None,
        ttfb_milliseconds=None,
        finish_reason=status,
        error_type=error_type,
        error_status=error_status,
        region=region,
        source="synthetic",
    )


def _rotation_error_excluded_from_uptime(error_type: str | None) -> bool:
    return error_type in {
        "unsupported_route",
        "probe_config_error",
        "provider_auth_config",
    }


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
    """Extract assistant-visible text from a /chat/completions reply.

    Handles three shapes the catalog actually returns:
      * Plain string content (OpenAI canonical)
      * List-of-parts content (Anthropic, multimodal adapters):
        [{"type":"text", "text":"…"}, …]
      * Reasoning-content split (kimi-k2.6, glm-4.6, deepseek-v4):
        message.content is empty while message.reasoning_content (or
        message.reasoning) carries the actual answer.

    Concatenates anything we find so the pong matcher sees the full
    answer regardless of which path the upstream took. Before this
    was reasoning-aware, the probe flagged `pong_mismatch` on every
    reasoning model whose visible content arrived empty.
    """
    if response.status_code != 200:
        return ""
    try:
        choices = response.json().get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        parts: list[str] = []
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if isinstance(text, str):
                        parts.append(text)
        # Reasoning shapes: some providers expose the thinking trace,
        # some emit the answer only inside it when max_tokens caps
        # the visible content. Treat both as fair game for the
        # output_match check.
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        text = item.get("text") or ""
                        if isinstance(text, str):
                            parts.append(text)
        return " ".join(p for p in parts if p)
    except (ValueError, AttributeError):
        return ""


def _responses_text(response: httpx.Response) -> str:
    """Extract text from a /responses reply, walking the full output[].

    OpenAI's Responses API emits an ordered output[] array; for
    reasoning models the first item is a `reasoning` block and the
    visible answer is further down in a `message`-type item. The
    previous extractor read output[0].content[0].text exclusively,
    so reasoning models showed up as empty → pong_mismatch.
    """
    if response.status_code != 200:
        return ""
    try:
        output = response.json().get("output") or []
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for piece in content:
                    if isinstance(piece, dict):
                        text = piece.get("text") or ""
                        if isinstance(text, str):
                            parts.append(text)
            elif isinstance(content, str):
                parts.append(content)
            # Reasoning summary blocks
            summary = item.get("summary")
            if isinstance(summary, list):
                for piece in summary:
                    if isinstance(piece, dict):
                        text = piece.get("text") or ""
                        if isinstance(text, str):
                            parts.append(text)
        return " ".join(p for p in parts if p)
    except (ValueError, AttributeError):
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
