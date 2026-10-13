"""Deterministic, first-party inference for the isolated shadow cohort.

Denominator v1: one buffered router generation ID per line (unchanged), then
exactly one final line ``# shadow-probe-pass-v1 `` followed by a JSON object:
``requests_sent``, ``buffered_with_id``, ``streams_sent``, ``streams_without_id``,
``failed`` are integer counts; ``by_shape`` maps each of the eight fixed names
to {"sent": integer, "with_id": integer}; ``stream_generation_ids`` is a list
of optional stream IDs, never buffered-ID lines. Every request, including a
failed request, contributes to sent counts. CLI creation remains exclusive 0600.

Exit 1 means at least one request failed validation (or the pass/file failed);
exit 2 means invalid configuration. Missing stream identity alone is healthy.
Stream requests are reconciled by count in the isolated, probe-only workspace;
buffered requests still require identity. Exit 0 does not certify settlement:
run probe_cohort_check with this pass file after settlement, using a ten-minute
window containing exactly this pass and no other traffic.
"""
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
    shape_name: str


DENOMINATOR_PREFIX = "# shadow-probe-pass-v1 "


def denominator(results: list[ProbeResult]) -> str:
    by_shape = {shape.name: {"sent": 0, "with_id": 0} for shape in SHAPES}
    streams = {shape.name for shape in SHAPES if shape.stream}
    buffered_ids: list[str] = []
    stream_ids: list[str] = []
    for result in results:
        counts = by_shape[result.shape_name]
        counts["sent"] += 1
        if result.generation_id is not None:
            counts["with_id"] += 1
            (stream_ids if result.shape_name in streams else buffered_ids).append(result.generation_id)
    streams_sent = sum(by_shape[name]["sent"] for name in streams)
    totals = dict(requests_sent=len(results), buffered_with_id=len(buffered_ids),
                  streams_sent=streams_sent, streams_without_id=streams_sent - len(stream_ids),
                  failed=sum(result.status != "up" for result in results),
                  by_shape=by_shape, stream_generation_ids=stream_ids)
    return "".join(identity + "\n" for identity in buffered_ids) + DENOMINATOR_PREFIX + json.dumps(totals, sort_keys=True) + "\n"


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
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
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
                        if (not isinstance(payload, dict) or "error" in payload
                                or payload.get("type") == "error"
                                or (isinstance(payload.get("response"), dict)
                                    and payload["response"].get("error"))):
                            raise ValueError("stream payload")
                        generation_id = _generation_id(payload) or generation_id
                        terminal = terminal or (_terminal(payload, shape) and (
                            shape.route != "/responses" or payload.get("type") == "response.completed"
                        ))
                    if not terminal or (shape.route == "/chat/completions" and not done):
                        raise ValueError("missing terminal")
                else:
                    await response.aread()
                    payload = response.json()
                    _verify_payload(payload, api_key)
                    generation_id = _generation_id(payload)
                    if not _terminal(payload, shape):
                        raise ValueError("missing terminal")
                if not shape.stream and generation_id is None:
                    raise ValueError("missing router usage identity")
    except Exception as exc:  # Sample only the class; never provider text or request headers.
        error = type(exc).__name__
        generation_id = None
    elapsed = int((time.perf_counter() - started) * 1000)
    if report is not None:
        status_class = f"{status // 100}xx" if status is not None else "none"
        report(f"{target.name} {shape.name} {status_class} {elapsed}ms"
               + (f" generation_id={generation_id}" if generation_id else ""))
    return ProbeResult("up" if error is None else "down", generation_id, error, shape.name)


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
