"""Deterministic, first-party inference for the isolated shadow cohort."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from trusted_router.config import Settings, validate_shadow_probe_key
from trusted_router.enclave_regions import ENCLAVE_REGIONS
from trusted_router.synthetic.probes import SyntheticTarget, configured_targets
from trusted_router.synthetic.shadow_probe_contract import SHAPES, Shape


@dataclass(frozen=True)
class ProbeResult:
    status: str
    generation_id: str | None
    error_type: str | None


def targets(settings: Settings) -> list[SyntheticTarget]:
    # Reuse the monitor's hostname resolution, forcing the complete GCP
    # inventory even when an older job's TR_REGIONS omitted gateway-only west.
    cfg = settings.model_copy(update={
        "regions": ",".join(ENCLAVE_REGIONS), "synthetic_regional_probes_enabled": True,
        "synthetic_gateway_region_targets": "",
    })
    resolved = {target.name: target for target in configured_targets(cfg)}
    result = [resolved[region] for region in ENCLAVE_REGIONS]
    for target in result:
        expected = f"https://api-{target.name}.quillrouter.com/v1"
        if target.api_base_url != expected or target.connect_host:
            raise ValueError("shadow probe requires the regional enclave HTTPS hostname")
    return result


def _terminal(payload: dict[str, Any], shape: Shape) -> bool:
    if shape.route == "/responses":
        response = payload.get("response", payload)
        return isinstance(response, dict) and response.get("status") == "completed"
    return any(isinstance(choice, dict) and choice.get("finish_reason") is not None
               for choice in payload.get("choices", []))


def _generation_id(payload: dict[str, Any]) -> str | None:
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    if not isinstance(usage, dict) or not all(
        type(value) is int and value >= 0 for value in (
            usage.get("input_tokens", usage.get("prompt_tokens")),
            usage.get("output_tokens", usage.get("completion_tokens")),
        )
    ):
        return None
    provider_usage = usage.get("provider_usage") if isinstance(usage, dict) else None
    # storage_models.generation_id_for_authorization: gen- + uuid5.hex.
    # Only usage.provider_usage.generation_id is router-owned wire metadata.
    identity = provider_usage.get("generation_id") if isinstance(provider_usage, dict) else None
    return identity if isinstance(identity, str) and re.fullmatch(r"gen-[0-9a-f]{32}", identity) else None


def _verify_payload(payload: Any, key: str) -> None:
    # Check recursively, including keys, before retaining ANY response metadata.
    if isinstance(payload, str) and key in payload:
        raise ValueError("credential echo")
    if isinstance(payload, dict):
        for name, value in payload.items():
            _verify_payload(name, key)
            _verify_payload(value, key)
    elif isinstance(payload, list):
        for value in payload:
            _verify_payload(value, key)


async def probe(
    client: httpx.AsyncClient, target: SyntheticTarget, shape: Shape, *,
    api_key: str, monitor_region: str,
    report: Callable[[str], None] | None = None,
) -> ProbeResult:
    url = target.api_base_url + shape.route
    started = time.perf_counter()
    status: int | None = None
    error: str | None = None
    generation_id: str | None = None
    try:
        async with asyncio.timeout(30):
            async with client.stream("POST", url, json=shape.body(), headers={
                "authorization": f"Bearer {api_key}",
                "idempotency-key": f"async-settle-probe-{uuid.uuid4()}",
            }) as response:
                status = response.status_code
                response.raise_for_status()
                if shape.stream:
                    terminal = done = False
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ValueError("stream content type")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                            continue
                        payload = json.loads(data)
                        _verify_payload(payload, api_key)
                        if not isinstance(payload, dict) or payload.get("error"):
                            raise ValueError("stream payload")
                        generation_id = _generation_id(payload) or generation_id
                        terminal = terminal or _terminal(payload, shape)
                    if not terminal or (shape.route == "/chat/completions" and not done):
                        raise ValueError("missing terminal")
                else:
                    await response.aread()
                    payload = response.json()
                    _verify_payload(payload, api_key)
                    generation_id = _generation_id(payload)
                    if not _terminal(payload, shape):
                        raise ValueError("missing terminal")
                if generation_id is None:
                    raise ValueError("missing router usage identity")
    except Exception as exc:  # Sample only the class; never provider text or request headers.
        error = type(exc).__name__
        generation_id = None
    elapsed = int((time.perf_counter() - started) * 1000)
    if report is not None:
        status_class = f"{status // 100}xx" if status is not None else "none"
        report(f"{target.name} {shape.name} {status_class} {elapsed}ms"
               + (f" generation_id={generation_id}" if generation_id else ""))
    return ProbeResult("up" if error is None else "down", generation_id, error)


async def run_pass(
    settings: Settings, *, monitor_region: str, client: httpx.AsyncClient,
    report: Callable[[str], None] | None = None,
) -> list[ProbeResult]:
    key = settings.async_settle_shadow_probe_api_key
    validate_shadow_probe_key(settings, key)
    if not key:
        raise ValueError("a distinct shadow probe key is required")
    samples = []
    target_list = targets(settings)
    # At most four concurrent calls: one per target. No retry, rotation or
    # provider fallback can silently change the comparison denominator.
    for _ in range(settings.async_settle_shadow_probe_repetitions):
        for shape in SHAPES:
            samples.extend(await asyncio.gather(*(
                probe(client, target, shape, api_key=key, monitor_region=monitor_region, report=report)
                for target in target_list
            )))
    return samples
