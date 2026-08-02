from __future__ import annotations

import asyncio
import base64
import binascii
import json
import random
import secrets
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any
from urllib.parse import urljoin, urlsplit

import cbor2
import httpx

from trusted_router.config import Settings
from trusted_router.provider_reliability import model_deadlines
from trusted_router.regions import choose_region, region_payload
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    scrub_provider_error_message,
)
from trusted_router.synthetic.components import is_router_origin_error
from trusted_router.types import UsageType

DEFAULT_SYNTHETIC_BILLING_CONCURRENCY = 2
IMAGE_GENERATION_MODEL = "google/gemini-3.1-flash-image-preview"
IMAGE_GENERATION_PROVIDER = "google-ai-studio"
VIDEO_GENERATION_MODEL = "x-ai/grok-imagine-video"
VIDEO_GENERATION_PROVIDER = "grok"
VIDEO_GENERATION_DURATION_SECONDS = 1
VIDEO_GENERATION_RESOLUTION = "480p"
_IMAGE_CANARY_PROMPT = (
    "Generate and return an actual square image now, not a textual description. "
    "Show one solid red circle centered on a white background."
)
_MAX_IMAGE_DATA_URL_CHARACTERS = 32 * 1024 * 1024
_MIN_VALID_IMAGE_BYTES = 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MIN_VALID_VIDEO_BYTES = 1024
_MAX_SYNTHETIC_VIDEO_BYTES = 256 * 1024 * 1024


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
        if name == choose_region(settings):
            # The canonical hostname now publishes all healthy gateway regions,
            # so it is not a primary-region-only signal. Probe the primary
            # regional hostname separately when it exists so us-central1 can
            # fail independently of the global/canonical record.
            regional_hostname = settings.regional_api_hostname_template.format(region=name)
            regional_api_base_url = f"https://{regional_hostname}/v1"
            if regional_api_base_url != settings.api_base_url:
                targets.append(
                    SyntheticTarget(name, regional_api_base_url, name, control_plane_url)
                )
                continue
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
        targets.append(SyntheticTarget(name, api_base_url, name, control_plane_url))
    return targets


async def run_synthetic_once(
    settings: Settings,
    *,
    monitor_region: str | None = None,
    api_key: str | None = None,
    billing_semaphore: asyncio.Semaphore | None = None,
) -> list[SyntheticProbeSample]:
    region = monitor_region or settings.synthetic_monitor_region or choose_region(settings)
    key = api_key or settings.synthetic_monitor_api_key
    timeout = httpx.Timeout(settings.synthetic_monitor_timeout_seconds)
    limiter = billing_semaphore or asyncio.Semaphore(DEFAULT_SYNTHETIC_BILLING_CONCURRENCY)
    samples: list[SyntheticProbeSample] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        target_results = await asyncio.gather(
            *[
                _run_target_synthetic_probes(
                    client,
                    target,
                    monitor_region=region,
                    api_key=key,
                    model=settings.synthetic_monitor_model,
                    billing_semaphore=limiter,
                )
                for target in configured_targets(settings)
            ]
        )
    for target_samples in target_results:
        samples.extend(target_samples)
    return samples


async def _run_target_synthetic_probes(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str | None,
    model: str,
    billing_semaphore: asyncio.Semaphore,
) -> list[SyntheticProbeSample]:
    probes = [
        tls_health_probe(client, target, monitor_region=monitor_region),
        attestation_nonce_probe(client, target, monitor_region=monitor_region),
        gateway_latency_phase_probes(target, monitor_region=monitor_region),
    ]
    # Per-region control plane health via Cloud Run direct URL.
    # tls_health above probes target.api_base_url which is the
    # ENCLAVE (api-{region}.quillrouter.com) — that path can be
    # broken by an enclave-side issue (MIG at size 0, ACME cert
    # not issued, etc.) while the regional Cloud Run is fine.
    # This separate probe pins the control-plane signal per region.
    if target.control_plane_url:
        probes.append(control_plane_health_probe(client, target, monitor_region=monitor_region))
    if api_key:
        probes.extend(
            [
                _run_billing_probe(
                    billing_semaphore,
                    openai_chat_pong_probe,
                    client,
                    target,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    model=model,
                ),
                _run_billing_probe(
                    billing_semaphore,
                    responses_pong_probe,
                    client,
                    target,
                    monitor_region=monitor_region,
                    api_key=api_key,
                    model=model,
                ),
            ]
        )
    results = await asyncio.gather(*probes)
    samples: list[SyntheticProbeSample] = []
    for result in results:
        if isinstance(result, list):
            samples.extend(result)
        elif isinstance(result, SyntheticProbeSample):
            samples.append(result)
        else:
            raise TypeError(f"unexpected synthetic probe result: {type(result).__name__}")
    return samples


async def _run_billing_probe(
    semaphore: asyncio.Semaphore,
    probe: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> SyntheticProbeSample:
    async with semaphore:
        return await probe(*args, **kwargs)


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


async def gateway_latency_phase_probes(
    target: SyntheticTarget,
    *,
    monitor_region: str,
    timeout_seconds: float = 10.0,
) -> list[SyntheticProbeSample]:
    """Measure a fresh TLS request and an immediate request on that connection.

    This probe is diagnostic, not an uptime signal. It uses an HTTP/1.1-only
    connection so DNS, TCP, TLS, and application processing can be timed
    independently without relying on private httpx/httpcore trace APIs.
    """

    url = _root_url(target.api_base_url, "/health")
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or not host:
        return _failed_latency_phase_samples(
            target,
            monitor_region=monitor_region,
            url=url,
            error_type="invalid_health_url",
        )
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    writer: asyncio.StreamWriter | None = None
    raw_socket: socket.socket | None = None
    started = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        dns_started = time.perf_counter()
        addresses = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=_remaining_probe_seconds(started, timeout_seconds),
        )
        dns_ms = _elapsed_ms(dns_started)
        tcp_ms: int | None = None
        last_connect_error: OSError | TimeoutError | None = None
        for family, socktype, protocol, _canonical_name, address in addresses:
            candidate_socket = socket.socket(family, socktype, protocol)
            candidate_socket.setblocking(False)
            tcp_started = time.perf_counter()
            try:
                await asyncio.wait_for(
                    loop.sock_connect(candidate_socket, address),
                    timeout=_remaining_probe_seconds(started, timeout_seconds),
                )
            except (OSError, TimeoutError) as exc:
                candidate_socket.close()
                last_connect_error = exc
                continue
            raw_socket = candidate_socket
            tcp_ms = _elapsed_ms(tcp_started)
            break
        if raw_socket is None or tcp_ms is None:
            raise last_connect_error or OSError("no resolved address accepted TCP")

        tls_context = ssl.create_default_context()
        tls_context.set_alpn_protocols(["http/1.1"])
        tls_started = time.perf_counter()
        reader, stream_writer = await asyncio.wait_for(
            asyncio.open_connection(
                sock=raw_socket,
                ssl=tls_context,
                server_hostname=host,
            ),
            timeout=_remaining_probe_seconds(started, timeout_seconds),
        )
        writer = stream_writer
        raw_socket = None
        tls_ms = _elapsed_ms(tls_started)
        ssl_object = stream_writer.get_extra_info("ssl_object")
        negotiated_protocol = (
            ssl_object.selected_alpn_protocol() if ssl_object is not None else None
        ) or "http/1.1"

        first_started = time.perf_counter()
        first_status, first_headers, first_body, first_ttfb = await _health_http11_request(
            reader,
            stream_writer,
            host=host,
            path=path,
            timeout_seconds=_remaining_probe_seconds(started, timeout_seconds),
        )
        first_total_ms = _elapsed_ms(started)
        first_ok = first_status == 200 and first_body == b'{"status":"ok"}'
        cold = _sample(
            "gateway_cold_path",
            target,
            monitor_region,
            url,
            status="up" if first_ok else "down",
            latency_milliseconds=first_total_ms,
            ttfb_milliseconds=first_ttfb,
            dns_milliseconds=dns_ms,
            tcp_connect_milliseconds=tcp_ms,
            tls_handshake_milliseconds=tls_ms,
            gateway_processing_milliseconds=_server_timing_gateway(first_headers),
            connection_reused=False,
            protocol=negotiated_protocol,
            http_status=first_status,
            error_type=None if first_ok else "bad_health_response",
        )

        reusable = first_headers.get("connection", "").casefold() != "close"
        if not reusable or not first_ok:
            return [
                cold,
                _sample(
                    "gateway_reused_path",
                    target,
                    monitor_region,
                    url,
                    status="down",
                    latency_milliseconds=_elapsed_ms(first_started),
                    connection_reused=False,
                    protocol=negotiated_protocol,
                    error_type="connection_not_reusable",
                ),
            ]

        reused_started = time.perf_counter()
        second_status, second_headers, second_body, second_ttfb = (
            await _health_http11_request(
                reader,
                stream_writer,
                host=host,
                path=path,
                timeout_seconds=_remaining_probe_seconds(started, timeout_seconds),
            )
        )
        second_total_ms = _elapsed_ms(reused_started)
        second_ok = second_status == 200 and second_body == b'{"status":"ok"}'
        return [
            cold,
            _sample(
                "gateway_reused_path",
                target,
                monitor_region,
                url,
                status="up" if second_ok else "down",
                latency_milliseconds=second_total_ms,
                ttfb_milliseconds=second_ttfb,
                gateway_processing_milliseconds=_server_timing_gateway(second_headers),
                connection_reused=True,
                protocol=negotiated_protocol,
                http_status=second_status,
                error_type=None if second_ok else "bad_health_response",
            ),
        ]
    except (EOFError, OSError, TimeoutError, ssl.SSLError, ValueError) as exc:
        return _failed_latency_phase_samples(
            target,
            monitor_region=monitor_region,
            url=url,
            error_type=exc.__class__.__name__,
            latency_milliseconds=_elapsed_ms(started),
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass
        if raw_socket is not None:
            raw_socket.close()


async def _health_http11_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    host: str,
    path: str,
    timeout_seconds: float,
) -> tuple[int, dict[str, str], bytes, int]:
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Accept: application/json\r\n"
        "User-Agent: TrustedRouter-Synthetic/1\r\n"
        "Connection: keep-alive\r\n\r\n"
    ).encode("ascii")
    started = time.perf_counter()
    writer.write(request)
    await asyncio.wait_for(
        writer.drain(), timeout=_remaining_probe_seconds(started, timeout_seconds)
    )
    status_line = await asyncio.wait_for(
        reader.readline(), timeout=_remaining_probe_seconds(started, timeout_seconds)
    )
    ttfb_ms = _elapsed_ms(started)
    parts = status_line.decode("ascii", "replace").strip().split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("invalid HTTP status line")
    status = int(parts[1])
    headers: dict[str, str] = {}
    header_bytes = len(status_line)
    while True:
        line = await asyncio.wait_for(
            reader.readline(), timeout=_remaining_probe_seconds(started, timeout_seconds)
        )
        header_bytes += len(line)
        if header_bytes > 64 * 1024:
            raise ValueError("health response headers too large")
        if line in {b"\r\n", b"\n"}:
            break
        name, separator, value = line.decode("latin-1").partition(":")
        if not separator:
            raise ValueError("invalid HTTP header")
        headers[name.strip().casefold()] = value.strip()
    content_length = int(headers.get("content-length", "0"))
    if content_length < 0 or content_length > 4096:
        raise ValueError("invalid health response length")
    body = await asyncio.wait_for(
        reader.readexactly(content_length),
        timeout=_remaining_probe_seconds(started, timeout_seconds),
    )
    return status, headers, body, ttfb_ms


def _server_timing_gateway(headers: dict[str, str]) -> int | None:
    for metric in headers.get("server-timing", "").split(","):
        name, *parameters = metric.split(";")
        if name.strip().casefold() != "gateway":
            continue
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if separator and key.casefold() == "dur":
                try:
                    return max(0, int(round(float(value))))
                except ValueError:
                    return None
    return None


def _remaining_probe_seconds(started: float, timeout_seconds: float) -> float:
    remaining = timeout_seconds - (time.perf_counter() - started)
    if remaining <= 0:
        raise TimeoutError("gateway latency probe deadline exceeded")
    return remaining


def _failed_latency_phase_samples(
    target: SyntheticTarget,
    *,
    monitor_region: str,
    url: str,
    error_type: str,
    latency_milliseconds: int | None = None,
) -> list[SyntheticProbeSample]:
    return [
        _sample(
            probe_type,
            target,
            monitor_region,
            url,
            status="down",
            latency_milliseconds=latency_milliseconds,
            connection_reused=probe_type == "gateway_reused_path",
            error_type=error_type,
        )
        for probe_type in ("gateway_cold_path", "gateway_reused_path")
    ]


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


async def image_generation_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    model: str = IMAGE_GENERATION_MODEL,
    provider: str = IMAGE_GENERATION_PROVIDER,
) -> SyntheticProbeSample:
    """Generate and validate one image without retaining its content."""
    url = _api_url(target.api_base_url, "/chat/completions")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _IMAGE_CANARY_PROMPT}],
        "provider": {"only": [provider], "allow_fallbacks": False},
        "max_tokens": 2048,
        "metadata": {
            "trustedrouter_synthetic": "true",
            "probe": "image_generation",
        },
    }
    started = time.perf_counter()
    try:
        response = await client.post(url, json=body, headers=_auth_headers(api_key))
        latency_ms = _elapsed_ms(started)
        payload = _json_object(response)
        valid_image = response.status_code == 200 and _has_valid_generated_image(payload)
        metadata = _completion_metadata(payload)
        return _sample(
            "image_generation",
            target,
            monitor_region,
            url,
            status="up" if valid_image else "down",
            latency_milliseconds=latency_ms,
            ttfb_milliseconds=latency_ms,
            http_status=response.status_code,
            error_type=None
            if valid_image
            else (
                "invalid_image_payload"
                if response.status_code == 200
                else "image_generation_http_error"
            ),
            provider=provider,
            model=model,
            selected_provider=metadata["selected_provider"],
            selected_model=metadata["selected_model"],
            generation_id=metadata["generation_id"],
            cost_microdollars=metadata["cost_microdollars"],
            output_match=valid_image,
        )
    except httpx.HTTPError as exc:
        return _sample(
            "image_generation",
            target,
            monitor_region,
            url,
            status="down",
            latency_milliseconds=_elapsed_ms(started),
            error_type=exc.__class__.__name__,
            provider=provider,
            model=model,
            output_match=False,
        )


async def video_generation_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    idempotency_key: str,
    model: str = VIDEO_GENERATION_MODEL,
    provider: str = VIDEO_GENERATION_PROVIDER,
    duration_seconds: int = VIDEO_GENERATION_DURATION_SECONDS,
    resolution: str = VIDEO_GENERATION_RESOLUTION,
    poll_interval_seconds: float = 5.0,
    total_timeout_seconds: float = 300.0,
) -> SyntheticProbeSample:
    """Generate, validate, and discard one minimal video without retaining content."""
    url = _api_url(target.api_base_url, "/videos")
    headers = {
        **_auth_headers(api_key),
        "idempotency-key": idempotency_key,
    }
    body = {
        "model": model,
        "prompt": "A white dot moves once across a plain black background.",
        "duration": duration_seconds,
        "resolution": resolution,
        "aspect_ratio": "16:9",
        "generate_audio": False,
        "provider": {"only": [provider], "allow_fallbacks": False},
    }
    started = time.perf_counter()
    try:
        async with asyncio.timeout(total_timeout_seconds):
            response = await client.post(url, json=body, headers=headers)
            payload = _json_object(response)
            if response.status_code not in {200, 202}:
                return _video_sample(
                    target,
                    monitor_region=monitor_region,
                    target_url=url,
                    started=started,
                    status="down",
                    http_status=response.status_code,
                    error_type="video_generation_http_error",
                    provider=provider,
                    model=model,
                )
            job_id = payload.get("id")
            if not isinstance(job_id, str) or not job_id.startswith("job-"):
                return _video_sample(
                    target,
                    monitor_region=monitor_region,
                    target_url=url,
                    started=started,
                    status="down",
                    http_status=response.status_code,
                    error_type="invalid_video_job",
                    provider=provider,
                    model=model,
                )

            status_payload = payload
            polling_url = payload.get("polling_url")
            if not isinstance(polling_url, str) or not polling_url:
                polling_url = f"/v1/videos/{job_id}"
            polling_url = _root_url(target.api_base_url, polling_url)
            while status_payload.get("status") not in {"completed", "failed"}:
                if poll_interval_seconds:
                    await asyncio.sleep(poll_interval_seconds)
                status_response = await client.get(polling_url, headers=_auth_headers(api_key))
                status_payload = _json_object(status_response)
                if status_response.status_code != 200:
                    return _video_sample(
                        target,
                        monitor_region=monitor_region,
                        target_url=url,
                        started=started,
                        status="down",
                        http_status=status_response.status_code,
                        error_type="video_poll_http_error",
                        provider=provider,
                        model=model,
                    )

            generation_id, cost_microdollars = _video_usage(status_payload)
            if status_payload.get("status") != "completed":
                return _video_sample(
                    target,
                    monitor_region=monitor_region,
                    target_url=url,
                    started=started,
                    status="down",
                    http_status=200,
                    error_type="video_generation_failed",
                    provider=provider,
                    model=model,
                    generation_id=generation_id,
                    cost_microdollars=cost_microdollars,
                )

            content_url = _first_video_content_url(status_payload)
            if content_url is None:
                # A duplicate scheduler delivery reuses the daily idempotency
                # key. If the first invocation already downloaded and cleaned
                # the completed job, there is intentionally no content URL.
                valid_video = generation_id is not None
            else:
                valid_video = await _validate_video_content(
                    client,
                    _root_url(target.api_base_url, content_url),
                    api_key=api_key,
                )
            return _video_sample(
                target,
                monitor_region=monitor_region,
                target_url=url,
                started=started,
                status="up" if valid_video else "down",
                http_status=200,
                error_type=None if valid_video else "invalid_video_payload",
                provider=provider,
                model=model,
                generation_id=generation_id,
                cost_microdollars=cost_microdollars,
                output_match=valid_video,
            )
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        return _video_sample(
            target,
            monitor_region=monitor_region,
            target_url=url,
            started=started,
            status="down",
            error_type=exc.__class__.__name__,
            provider=provider,
            model=model,
        )


def _video_usage(payload: dict[str, Any]) -> tuple[str | None, int]:
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        generation_id = None
    usage = payload.get("usage")
    raw_cost = usage.get("cost_microdollars") if isinstance(usage, dict) else None
    cost = raw_cost if isinstance(raw_cost, int) and not isinstance(raw_cost, bool) else 0
    return generation_id, max(cost, 0)


def _first_video_content_url(payload: dict[str, Any]) -> str | None:
    urls = payload.get("unsigned_urls")
    if not isinstance(urls, list):
        return None
    return next((value for value in urls if isinstance(value, str) and value), None)


async def _validate_video_content(
    client: httpx.AsyncClient,
    content_url: str,
    *,
    api_key: str,
) -> bool:
    total = 0
    prefix = bytearray()
    async with client.stream("GET", content_url, headers=_auth_headers(api_key)) as response:
        if response.status_code != 200:
            await response.aread()
            return False
        content_type = response.headers.get("content-type", "").casefold()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if len(prefix) < 12:
                prefix.extend(chunk[: 12 - len(prefix)])
            if total > _MAX_SYNTHETIC_VIDEO_BYTES:
                return False
    return (
        content_type.startswith("video/")
        and total >= _MIN_VALID_VIDEO_BYTES
        and len(prefix) >= 8
        and bytes(prefix[4:8]) == b"ftyp"
    )


def _video_sample(
    target: SyntheticTarget,
    *,
    monitor_region: str,
    target_url: str,
    started: float,
    status: str,
    provider: str,
    model: str,
    http_status: int | None = None,
    error_type: str | None = None,
    generation_id: str | None = None,
    cost_microdollars: int = 0,
    output_match: bool | None = None,
) -> SyntheticProbeSample:
    return _sample(
        "video_generation",
        target,
        monitor_region,
        target_url,
        status=status,
        latency_milliseconds=_elapsed_ms(started),
        ttfb_milliseconds=None,
        http_status=http_status,
        error_type=error_type,
        provider=provider,
        model=model,
        selected_provider=provider,
        selected_model=model,
        generation_id=generation_id,
        cost_microdollars=cost_microdollars,
        output_match=output_match,
    )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_valid_generated_image(payload: dict[str, Any]) -> bool:
    return any(_valid_image_data_url(value) for value in _generated_image_data_urls(payload))


def _generated_image_data_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            urls.extend(_content_image_data_urls(message.get("content")))
            urls.extend(_content_image_data_urls(message.get("images")))
    urls.extend(_content_image_data_urls(payload.get("images")))
    return urls


def _content_image_data_urls(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content.startswith("data:image/") else []
    if isinstance(content, dict):
        image_url = content.get("image_url")
        if isinstance(image_url, str):
            return [image_url]
        if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
            return [str(image_url["url"])]
        for key in ("url", "data"):
            value = content.get(key)
            if isinstance(value, str) and value.startswith("data:image/"):
                return [value]
        return []
    if not isinstance(content, list):
        return []
    return [url for part in content for url in _content_image_data_urls(part)]


def _valid_image_data_url(value: str) -> bool:
    if len(value) > _MAX_IMAGE_DATA_URL_CHARACTERS or "," not in value:
        return False
    header, encoded = value.split(",", 1)
    header_lower = header.lower()
    if ";base64" not in header_lower:
        return False
    if header_lower not in {
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
    }:
        return False
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(image) < _MIN_VALID_IMAGE_BYTES:
        return False
    if header_lower in {"data:image/jpeg;base64", "data:image/jpg;base64"}:
        return image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9")
    return image.startswith(_PNG_SIGNATURE) and image.endswith(_PNG_END)


def _completion_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    trustedrouter = payload.get("trustedrouter")
    if not isinstance(trustedrouter, dict):
        trustedrouter = {}
    routing = trustedrouter.get("routing")
    if not isinstance(routing, dict):
        routing = {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    provider_usage = usage.get("provider_usage")
    if not isinstance(provider_usage, dict):
        provider_usage = {}
    raw_cost = usage.get("total_cost_microdollars") or usage.get("cost_microdollars")
    if raw_cost is None:
        raw_cost = provider_usage.get("total_cost_microdollars") or provider_usage.get(
            "cost_microdollars"
        )
    try:
        cost_microdollars = int(raw_cost or 0)
    except (TypeError, ValueError):
        cost_microdollars = 0
    return {
        "selected_provider": _optional_metadata_string(
            routing.get("selected_provider")
            or provider_usage.get("selected_provider")
            or payload.get("provider")
        ),
        "selected_model": _optional_metadata_string(
            routing.get("selected_model")
            or provider_usage.get("selected_model")
            or payload.get("model")
        ),
        "generation_id": _optional_metadata_string(
            provider_usage.get("generation_id")
            or trustedrouter.get("generation_id")
            or payload.get("id")
        ),
        "cost_microdollars": cost_microdollars,
    }


def _optional_metadata_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


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
    target = SyntheticTarget("control-plane", control_plane_base_url, None)
    samples: list[SyntheticProbeSample] = []
    stage = "authorize"
    started = time.perf_counter()
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
                    "gateway_authorize",
                    target,
                    monitor_region,
                    authorize_url,
                    status="down",
                    latency_milliseconds=_elapsed_ms(started),
                    http_status=authorize.status_code,
                    error_type=_probe_response_error(authorize, operation="authorize"),
                    model=model,
                )
            ]
        data = authorize.json()["data"]
        samples.append(
            _sample(
                "gateway_authorize",
                target,
                monitor_region,
                authorize_url,
                status="up",
                latency_milliseconds=_elapsed_ms(started),
                http_status=authorize.status_code,
                model=model,
                selected_model=data.get("model"),
                selected_provider=data.get("provider"),
            )
        )
        stage = "settle"
        started = time.perf_counter()
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
        settle_error = (
            None
            if ok
            else (
                _probe_response_error(settle, operation="settle")
                if settle.status_code != 200
                else "settle_failed"
            )
        )
        samples.append(
            _sample(
                "gateway_settle",
                target,
                monitor_region,
                settle_url,
                status="up" if ok else "down",
                latency_milliseconds=_elapsed_ms(started),
                http_status=settle.status_code,
                error_type=settle_error,
                model=model,
                selected_model=settle_data.get("model"),
                selected_provider=settle_data.get("provider"),
                generation_id=settle_data.get("generation_id"),
                cost_microdollars=int(settle_data.get("cost_microdollars") or 0),
            )
        )
        return samples
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        samples.append(
            _sample(
                f"gateway_{stage}",
                target,
                monitor_region,
                authorize_url if stage == "authorize" else settle_url,
                status="down",
                latency_milliseconds=_elapsed_ms(started),
                error_type=exc.__class__.__name__,
                model=model,
            )
        )
        return samples


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
                    error_type=_probe_response_error(authorize, operation="authorize"),
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
        settle_error = (
            None
            if ok
            else (
                _probe_response_error(settle, operation="fallback_settle")
                if settle.status_code != 200
                else "fallback_settle_failed"
            )
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
                error_type=settle_error,
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
    from trusted_router.provider_lifecycle import provider_model_retired

    pool: dict[str, list[str]] = {}
    for endpoint in MODEL_ENDPOINTS.values():
        if endpoint.usage_type != "Credits":
            continue
        if provider_model_retired(
            endpoint.provider,
            endpoint.model_id,
            endpoint.upstream_id,
        ):
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


@dataclass
class _StreamUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class _StreamObservation:
    ttfb_milliseconds: int | None = None
    first_token_milliseconds: int | None = None
    last_token_milliseconds: int | None = None
    elapsed_milliseconds: int = 0
    finish_reason: str | None = None
    stream_error: tuple[str, int | None, str | None] | None = None
    usage: _StreamUsage = dataclass_field(default_factory=_StreamUsage)


def _sse_line_payload(line: str) -> dict[str, Any] | None:
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
    return data if isinstance(data, dict) else None


def _sse_line_has_content(line: str) -> bool:
    """True if an SSE `data:` line carries a visible content/reasoning delta."""
    data = _sse_line_payload(line)
    if data is None:
        return False
    for choice in data.get("choices") or []:
        delta = choice.get("delta") or {}
        if (
            delta.get("content")
            or delta.get("reasoning_content")
            or delta.get("reasoning")
            or delta.get("thinking")
            or delta.get("text")
            or delta.get("output_text")
        ):
            return True
        message = choice.get("message") or {}
        if (
            message.get("content")
            or message.get("reasoning_content")
            or message.get("reasoning")
            or message.get("thinking")
            or message.get("text")
        ):
            return True
        if choice.get("text"):
            return True
    return False


def _sse_line_error(line: str) -> tuple[str, int | None, str | None] | None:
    """Return an OpenAI-style SSE error if the data line carries one."""
    data = _sse_line_payload(line)
    if data is None:
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    error_type = str(error.get("type") or "provider_error")
    message = str(error.get("message") or "") or None
    source = str(error.get("source") or "") or None
    status_raw = error.get("status") or error.get("code") or error.get("status_code")
    status: int | None
    try:
        status = int(status_raw) if status_raw is not None else None
    except (TypeError, ValueError):
        status = None
    return _rotation_error_type(error_type, status, message, source=source), status, message


def _sse_line_finish_reason(line: str) -> str | None:
    """Return the first choice finish reason from an SSE data line, if any."""
    data = _sse_line_payload(line)
    if data is None:
        return None
    for choice in data.get("choices") or []:
        reason = choice.get("finish_reason")
        if reason:
            return str(reason)
    return None


def _sse_line_usage(line: str) -> _StreamUsage | None:
    data = _sse_line_payload(line)
    if data is None:
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    return _StreamUsage(
        input_tokens=_first_int(usage, "prompt_tokens", "input_tokens"),
        output_tokens=_first_int(usage, "completion_tokens", "output_tokens"),
        reasoning_tokens=_first_int(completion_details, "reasoning_tokens"),
    )


def _first_int(values: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


async def _observe_provider_stream(
    response: httpx.Response,
    *,
    started: float,
    clock: Callable[[], float] = time.perf_counter,
) -> _StreamObservation:
    observation = _StreamObservation()
    tail = ""

    def observe_line(line: str, now_milliseconds: int) -> None:
        finish_reason = _sse_line_finish_reason(line)
        if finish_reason is not None:
            observation.finish_reason = finish_reason
        stream_error = _sse_line_error(line)
        if stream_error is not None:
            observation.stream_error = stream_error
            return
        if _sse_line_has_content(line):
            if observation.first_token_milliseconds is None:
                observation.first_token_milliseconds = now_milliseconds
            observation.last_token_milliseconds = now_milliseconds
        usage = _sse_line_usage(line)
        if usage is not None:
            observation.usage = usage

    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        now_milliseconds = _elapsed_ms_with_clock(started, clock)
        if observation.ttfb_milliseconds is None:
            observation.ttfb_milliseconds = now_milliseconds
        tail += chunk.decode("utf-8", "ignore")
        lines = tail.split("\n")
        tail = lines.pop()
        for line in lines:
            observe_line(line, now_milliseconds)
            if observation.stream_error is not None:
                break
        if observation.stream_error is not None:
            break
    if tail and observation.stream_error is None:
        observe_line(tail, _elapsed_ms_with_clock(started, clock))
    observation.elapsed_milliseconds = _elapsed_ms_with_clock(started, clock)
    return observation


def _response_error(response: httpx.Response) -> tuple[str, int | None, str | None]:
    try:
        payload = response.json()
    except ValueError:
        return f"http_{response.status_code}", response.status_code, None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) and isinstance(payload, dict):
        detail = payload.get("detail")
        error = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(error, dict):
        return f"http_{response.status_code}", response.status_code, None
    error_type = str(error.get("type") or f"http_{response.status_code}")
    message = str(error.get("message") or "") or None
    source = str(error.get("source") or "") or None
    status_raw = error.get("status") or error.get("code") or error.get("status_code")
    try:
        status = int(status_raw) if status_raw is not None else response.status_code
    except (TypeError, ValueError):
        status = response.status_code
    return _rotation_error_type(error_type, status, message, source=source), status, message


def _probe_response_error(response: httpx.Response, *, operation: str) -> str:
    error_type, _status, _message = _response_error(response)
    if is_router_origin_error(error_type):
        return error_type
    return f"{operation}_{error_type}"


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
    *,
    source: str | None = None,
) -> str:
    raw_type = error_type.casefold()
    raw_message = (message or "").casefold()
    raw_source = (source or "").casefold()
    if "workspace billing is paused" in raw_message:
        return "monitor_workspace_paused"
    if "database contention" in raw_message or "deadlock" in raw_message:
        return "router_database_contention"
    if "read-only mode" in raw_message or "planned maintenance" in raw_message:
        return "router_maintenance"
    if raw_source == "router" and any(
        marker in raw_message
        for marker in (
            "insufficient credits",
            "api key is disabled",
            "api key expired",
            "invalid api key",
            "api key not found",
        )
    ):
        return "monitor_account_unavailable"
    if raw_type in _UNSUPPORTED_ROUTE_ERROR_TYPES or any(
        marker in raw_message for marker in _UNSUPPORTED_ROUTE_MESSAGE_MARKERS
    ):
        return "unsupported_route"
    if raw_type in _PROBE_CONFIG_ERROR_TYPES or (
        status in {400, 422}
        and any(marker in raw_message for marker in _PROBE_CONFIG_MESSAGE_MARKERS)
    ):
        return "probe_config_error"
    if raw_source == "router":
        return "router_error"
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
    default_timeout_seconds: float = 20.0,
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
    served_provider = provider
    served_model = model
    deadline = model_deadlines(
        model,
        provider=provider,
        default_first_token_seconds=default_timeout_seconds,
    )
    try:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=_auth_headers(api_key),
            timeout=httpx.Timeout(deadline.first_token_seconds),
        ) as response:
            served_provider = response.headers.get("x-trustedrouter-provider") or provider
            served_model = response.headers.get("x-trustedrouter-served-model") or model
            if response.status_code != 200:
                await response.aread()
                error_type, error_status, message = _response_error(response)
                return _rotation_error_sample(
                    served_provider,
                    served_model,
                    region=monitor_region,
                    elapsed_ms=_elapsed_ms(started),
                    error_status=error_status,
                    error_type=error_type,
                    error_message=message,
                )
            observation = await _observe_provider_stream(response, started=started)
            if observation.stream_error is not None:
                error_type, status, message = observation.stream_error
                return _rotation_error_sample(
                    served_provider,
                    served_model,
                    region=monitor_region,
                    elapsed_ms=observation.elapsed_milliseconds,
                    error_status=status or 502,
                    error_type=error_type,
                    error_message=message,
                )
    except (httpx.HTTPError, ValueError) as exc:
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=_elapsed_ms(started),
            error_status=None,
            error_type=exc.__class__.__name__,
            error_message=str(exc),
        )
    if observation.first_token_milliseconds is None:
        error_type = (
            "probe_config_error" if observation.finish_reason == "length" else "empty_stream"
        )
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=observation.elapsed_milliseconds,
            error_status=None,
            error_type=error_type,
        )
    return ProviderBenchmarkSample(
        id=f"bench-{uuid.uuid4().hex}",
        model=served_model,
        provider=served_provider,
        provider_name=_provider_display_name(served_provider),
        status="success",
        usage_type=UsageType.CREDITS,
        streamed=True,
        elapsed_milliseconds=observation.elapsed_milliseconds,
        first_token_milliseconds=observation.first_token_milliseconds,
        ttfb_milliseconds=observation.ttfb_milliseconds,
        finish_reason=observation.finish_reason or "stop",
        region=monitor_region,
        source="synthetic",
    )


_THROUGHPUT_PROMPT = (
    "Continue writing the lowercase word benchmark separated by single spaces "
    "until the response token limit stops you. Do not count, explain, use "
    "punctuation, or stop early."
)


async def provider_throughput_probe(
    client: httpx.AsyncClient,
    target: SyntheticTarget,
    *,
    monitor_region: str,
    api_key: str,
    provider: str,
    model: str,
    max_tokens: int = 512,
    minimum_output_tokens: int = 128,
    total_timeout_seconds: float | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ProviderBenchmarkSample:
    """Measure effective output throughput from request start to completion.

    Unlike the tiny PONG rotation probe, this requires provider-reported final
    usage and enough output tokens for a stable sample. Measuring the complete
    request makes the result insensitive to HTTP/SSE buffering: a provider
    cannot appear artificially fast because many token events arrived in one
    network chunk. It records metadata only. The response bytes are discarded
    inside this function and are never returned to the control plane ingest
    payload.
    """
    if max_tokens <= 1:
        raise ValueError("max_tokens must be greater than one")
    if minimum_output_tokens <= 1 or minimum_output_tokens > max_tokens:
        raise ValueError("minimum_output_tokens must be between 2 and max_tokens")

    url = _api_url(target.api_base_url, "/chat/completions")
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _THROUGHPUT_PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "provider": {"only": [provider]},
        "metadata": {
            "trustedrouter_synthetic": "true",
            "trustedrouter_probe": "throughput",
        },
    }
    if not _rotation_omits_temperature(provider, model):
        body["temperature"] = 0

    started = clock()
    served_provider = provider
    served_model = model
    try:
        async with asyncio.timeout(total_timeout_seconds):
            async with client.stream(
                "POST", url, json=body, headers=_auth_headers(api_key)
            ) as response:
                served_provider = response.headers.get("x-trustedrouter-provider") or provider
                served_model = response.headers.get("x-trustedrouter-served-model") or model
                if response.status_code != 200:
                    await response.aread()
                    error_type, error_status, message = _response_error(response)
                    return _rotation_error_sample(
                        served_provider,
                        served_model,
                        region=monitor_region,
                        elapsed_ms=_elapsed_ms_with_clock(started, clock),
                        error_status=error_status,
                        error_type=error_type,
                        error_message=message,
                        source="synthetic_throughput",
                    )
                observation = await _observe_provider_stream(
                    response,
                    started=started,
                    clock=clock,
                )
    except (TimeoutError, httpx.HTTPError, ValueError) as exc:
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=_elapsed_ms_with_clock(started, clock),
            error_status=None,
            error_type=exc.__class__.__name__,
            error_message=str(exc),
            source="synthetic_throughput",
        )

    if observation.stream_error is not None:
        error_type, status, message = observation.stream_error
        return _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=observation.elapsed_milliseconds,
            error_status=status or 502,
            error_type=error_type,
            error_message=message,
            source="synthetic_throughput",
        )

    usage = observation.usage
    first_token_ms = observation.first_token_milliseconds
    if (
        usage.output_tokens < minimum_output_tokens
        or first_token_ms is None
        or observation.elapsed_milliseconds <= 0
    ):
        sample = _rotation_error_sample(
            served_provider,
            served_model,
            region=monitor_region,
            elapsed_ms=observation.elapsed_milliseconds,
            error_status=None,
            error_type="insufficient_throughput_sample",
            source="synthetic_throughput",
        )
        sample.input_tokens = usage.input_tokens
        sample.output_tokens = usage.output_tokens
        sample.first_token_milliseconds = first_token_ms
        sample.ttfb_milliseconds = observation.ttfb_milliseconds
        sample.finish_reason = observation.finish_reason or "insufficient_sample"
        return sample

    speed_tokens_per_second = usage.output_tokens * 1000 / observation.elapsed_milliseconds
    return ProviderBenchmarkSample(
        id=f"bench-{uuid.uuid4().hex}",
        model=served_model,
        provider=served_provider,
        provider_name=_provider_display_name(served_provider),
        status="success",
        usage_type=UsageType.CREDITS,
        streamed=True,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_cost_microdollars=_benchmark_route_cost_microdollars(
            served_provider,
            served_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ),
        speed_tokens_per_second=round(speed_tokens_per_second, 3),
        elapsed_milliseconds=observation.elapsed_milliseconds,
        first_token_milliseconds=first_token_ms,
        ttfb_milliseconds=observation.ttfb_milliseconds,
        finish_reason=observation.finish_reason or "stop",
        region=monitor_region,
        source="synthetic_throughput",
    )


def _benchmark_route_cost_microdollars(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
) -> int:
    from trusted_router.money import token_cost_microdollars
    from trusted_router.synthetic.throughput import credits_endpoint_prices

    prices = credits_endpoint_prices(provider, model)
    if prices is None:
        return 0
    prompt_price, completion_price = prices
    return token_cost_microdollars(
        input_tokens,
        prompt_price,
    ) + token_cost_microdollars(
        output_tokens,
        completion_price,
    )


def _rotation_max_tokens(provider: str, model: str) -> int:
    provider_l = provider.lower()
    model_l = model.lower()
    if provider_l == "openai" and (
        "/o1" in model_l or "/o3" in model_l or "/o4" in model_l or "/gpt-5" in model_l
    ):
        return 512
    if "gemini-2.5" in model_l or "gemini-3" in model_l:
        # Gemini thinks before visible content; hidden thinking consumes the
        # budget but is absent from usage, so 16 yields empty_stream. Live
        # verification on 2026-07-19 showed 2048 works; keep generous headroom.
        return 2048
    if (
        "gpt-oss" in model_l
        or "glm-4.6" in model_l
        or "glm-4.7" in model_l
        or "glm-5" in model_l
        or "nemotron" in model_l
        or "claude-fable-5" in model_l
        or "claude-sonnet-5" in model_l
        or "reasoning" in model_l
        or "thinking" in model_l
    ):
        # Reasoning models that think before emitting visible content: at 16
        # tokens they finish=length with zero streamed content and register as
        # probe_config_error. Crusoe nemotron-3 reasons by default without
        # streaming reasoning deltas; Claude Fable 5 (adaptive thinking always
        # on) and Sonnet 5 do the same, so they need the larger budget to emit
        # a visible token.
        return 512
    if "kimi-k2" in model_l or "grok" in model_l or "claude-opus" in model_l:
        return 128
    return 16


def _rotation_omits_temperature(provider: str, model: str) -> bool:
    provider_l = provider.lower()
    model_l = model.lower()
    return (
        (provider_l == "kimi" and "kimi-k2." in model_l)
        or (
            provider_l == "openai"
            and ("/o1" in model_l or "/o3" in model_l or "/o4" in model_l or "/gpt-5" in model_l)
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
    error_message: str | None = None,
    source: str = "synthetic",
) -> ProviderBenchmarkSample:
    status = "unsupported" if _rotation_error_excluded_from_uptime(error_type) else "error"
    truncated_error_message = None
    if error_message is not None:
        truncated_error_message = scrub_provider_error_message(str(error_message))[:300]
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
        error_message=truncated_error_message,
        region=region,
        source=source,
    )


def _rotation_error_excluded_from_uptime(error_type: str | None) -> bool:
    return is_router_origin_error(error_type) or error_type in {
        "unsupported_route",
        "probe_config_error",
        "provider_auth_config",
        "insufficient_throughput_sample",
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
    dns_milliseconds: int | None = None,
    tcp_connect_milliseconds: int | None = None,
    tls_handshake_milliseconds: int | None = None,
    gateway_processing_milliseconds: int | None = None,
    connection_reused: bool | None = None,
    protocol: str | None = None,
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
        dns_milliseconds=dns_milliseconds,
        tcp_connect_milliseconds=tcp_connect_milliseconds,
        tls_handshake_milliseconds=tls_handshake_milliseconds,
        gateway_processing_milliseconds=gateway_processing_milliseconds,
        connection_reused=connection_reused,
        protocol=protocol,
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
    """Extract nonce binding + identity evidence from an attestation body.

    Two live formats:
      * GCP Confidential Space — a JWT (ASCII, two dots). Nonce binding is
        in eat_nonce; identity is the container image_digest claim.
      * AWS Nitro — a binary COSE_Sign1/CBOR document. Nonce binding is the
        payload's `nonce` field; identity is PCR0 (the EIF measurement),
        reported as the attestation_digest so the status page shows the
        running enclave's measurement, comparable against the trust page.

    JWT is tried first but only for bodies that survive a STRICT ascii
    decode — a binary CBOR document can contain 0x2E ('.') bytes by chance,
    so "two dots after a lossy decode" (the previous check) would misroute
    it. A JWT can never fail ascii decoding, so the ordering is safe.

    This is the once-a-minute LIVENESS + BINDING check. Full chain
    verification to the AWS Nitro root / Google's JWKS (signatures, cert
    chains, cert-fingerprint-in-user_data) is the deploy gate's job
    (tools/verify-attestation.py in quill-cloud-proxy) — pulling X.509
    chain-walking into the control plane's probe loop would add heavy
    dependencies for a check whose job is "is the enclave up, answering
    MY nonce, and running the measurement we expect to see".
    """
    try:
        text = body.decode("ascii").strip()
    except UnicodeDecodeError:
        text = ""
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
    aws = _decode_aws_attestation_payload(body)
    if aws is not None:
        nonce = aws.get("nonce")
        nonce_ok = isinstance(nonce, bytes) and nonce.hex() == nonce_hex.lower()
        pcrs = aws.get("pcrs")
        pcr0 = pcrs.get(0) if isinstance(pcrs, dict) else None
        return {
            "nonce_ok": nonce_ok,
            "error_type": None if nonce_ok else "nonce_missing",
            "attestation_digest": pcr0.hex() if isinstance(pcr0, bytes) else None,
            "source_commit": None,
        }
    return {
        "nonce_ok": False,
        "error_type": "unsupported_attestation_format",
        "attestation_digest": None,
        "source_commit": None,
    }


def _decode_aws_attestation_payload(body: bytes) -> dict[Any, Any] | None:
    """Decode an AWS Nitro attestation document to its payload map.

    The NSM emits COSE_Sign1: a CBOR array [protected: bstr,
    unprotected: map, payload: bstr, signature: bstr], sometimes wrapped
    in CBOR tag 18. The payload bstr is itself a CBOR map (module_id,
    digest, timestamp, pcrs, nonce, user_data, ...). Returns None for
    anything that doesn't match that exact shape — the caller then
    reports unsupported_attestation_format rather than guessing.
    """
    try:
        decoded = cbor2.loads(body)
    except Exception:
        return None
    if isinstance(decoded, cbor2.CBORTag):
        decoded = decoded.value
    # cbor2 decodes arrays nested inside tags as immutable tuples (tagged
    # containers must be hashable), so accept both sequence shapes.
    if not (isinstance(decoded, (list, tuple)) and len(decoded) == 4):
        return None
    payload_bytes = decoded[2]
    if not isinstance(payload_bytes, bytes):
        return None
    try:
        payload = cbor2.loads(payload_bytes)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


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


def _elapsed_ms_with_clock(
    started: float,
    clock: Callable[[], float],
) -> int:
    return max(1, int(round((clock() - started) * 1000)))
