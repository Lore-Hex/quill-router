from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from trusted_router.byok_crypto import decrypt_control_secret, encrypted_secret_payload
from trusted_router.config import Settings
from trusted_router.storage import STORE, BroadcastDestination, Generation

log = logging.getLogger(__name__)

POSTHOG_DEFAULT_ENDPOINT = "https://us.i.posthog.com"


def broadcast_secret_context(destination_id: str, kind: str) -> str:
    return f"broadcast:{destination_id}:{kind}"


def public_destination_shape(destination: BroadcastDestination) -> dict[str, Any]:
    return {
        "id": destination.id,
        "workspace_id": destination.workspace_id,
        "type": destination.type,
        "name": destination.name,
        "endpoint": destination.endpoint,
        "enabled": destination.enabled,
        "include_content": destination.include_content,
        "method": destination.method,
        "api_key_configured": destination.encrypted_api_key is not None,
        "header_names": list(destination.header_names),
        "headers_configured": destination.encrypted_headers is not None,
        "created_at": destination.created_at,
        "updated_at": destination.updated_at,
    }


def gateway_destination_payload(destination: BroadcastDestination) -> dict[str, Any] | None:
    if not destination.enabled or not destination.include_content:
        return None
    return {
        "id": destination.id,
        "type": destination.type,
        "endpoint": destination.endpoint,
        "method": destination.method,
        "include_content": True,
        "api_key_context": broadcast_secret_context(destination.id, "api_key"),
        "headers_context": broadcast_secret_context(destination.id, "headers"),
        "encrypted_api_key": encrypted_secret_payload(destination.encrypted_api_key),
        "encrypted_headers": encrypted_secret_payload(destination.encrypted_headers),
    }


async def test_destination(destination: BroadcastDestination, settings: Settings) -> tuple[bool, str]:
    try:
        if destination.type == "posthog":
            api_key = _decrypt_api_key(destination, settings)
            payload = {
                "api_key": api_key,
                "event": "$ai_generation",
                "distinct_id": "trustedrouter-test",
                "properties": {
                    "$ai_trace_id": "trustedrouter-test",
                    "$ai_model": "trustedrouter/test",
                    "$ai_provider": "trustedrouter",
                    "$ai_input_tokens": 0,
                    "$ai_output_tokens": 0,
                    "$ai_latency": 0,
                    "$ai_stream": False,
                    "$ai_http_status": 200,
                    "$ai_total_cost_usd": 0,
                },
            }
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    _posthog_capture_url(destination.endpoint),
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            if 200 <= response.status_code < 300:
                return True, "ok"
            return False, f"posthog returned {response.status_code}"
        if destination.type == "webhook":
            headers = _decrypt_headers(destination, settings)
            headers["X-Test-Connection"] = "true"
            headers.setdefault("content-type", "application/json")
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.request(
                    destination.method,
                    destination.endpoint,
                    json={"resourceSpans": []},
                    headers=headers,
                )
            if 200 <= response.status_code < 300 or response.status_code == 400:
                return True, "ok"
            return False, f"webhook returned {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - connection test returns message.
        return False, str(exc)
    return False, "unknown destination type"


def enqueue_metadata_broadcast(
    generation: Generation,
    *,
    settle_body: dict[str, Any],
) -> None:
    for destination in STORE.list_broadcast_destinations(generation.workspace_id):
        if not destination.enabled or destination.include_content:
            continue
        STORE.enqueue_broadcast_delivery(
            workspace_id=generation.workspace_id,
            destination_id=destination.id,
            generation_id=generation.id,
            settle_body=settle_body,
        )


def drain_broadcast_queue(*, settings: Settings, limit: int = 100) -> None:
    for job in STORE.due_broadcast_deliveries(limit=limit):
        generation = STORE.get_generation(job.generation_id)
        destination = STORE.get_broadcast_destination(job.workspace_id, job.destination_id)
        if generation is None or destination is None or not destination.enabled or destination.include_content:
            STORE.mark_broadcast_delivery(job.id, success=True)
            continue
        try:
            deliver_metadata_broadcast(
                destination,
                generation,
                settle_body=job.settle_body,
                settings=settings,
            )
        except Exception as exc:
            STORE.mark_broadcast_delivery(job.id, success=False, error=str(exc))
            log.exception("broadcast_metadata_delivery_failed destination=%s job=%s", destination.id, job.id)
            continue
        STORE.mark_broadcast_delivery(job.id, success=True)


def deliver_metadata_broadcast(
    destination: BroadcastDestination,
    generation: Generation,
    *,
    settle_body: dict[str, Any],
    settings: Settings,
) -> None:
    if destination.type == "posthog":
        payload = posthog_generation_payload(destination, generation, settle_body=settle_body, settings=settings)
        with httpx.Client(timeout=5) as client:
            response = client.post(
                _posthog_capture_url(destination.endpoint),
                json=payload,
                headers={"content-type": "application/json"},
            )
        response.raise_for_status()
        return
    if destination.type == "webhook":
        headers = _decrypt_headers(destination, settings)
        headers.setdefault("content-type", "application/json")
        payload = otlp_generation_payload(generation, settle_body=settle_body, include_content=False)
        with httpx.Client(timeout=5) as client:
            response = client.request(destination.method, destination.endpoint, json=payload, headers=headers)
        response.raise_for_status()


def posthog_generation_payload(
    destination: BroadcastDestination,
    generation: Generation,
    *,
    settle_body: dict[str, Any],
    settings: Settings,
    input_messages: list[dict[str, Any]] | None = None,
    output_choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    trace = _trace(settle_body)
    properties = _posthog_properties(
        generation,
        settle_body=settle_body,
        input_messages=input_messages,
        output_choices=output_choices,
    )
    for key, value in trace.items():
        if key not in {"trace_id", "session_id", "span_id", "span_name", "generation_name"}:
            properties[key] = value
    return {
        "api_key": _decrypt_api_key(destination, settings),
        "event": "$ai_generation",
        "distinct_id": str(settle_body.get("user") or generation.key_hash[:16]),
        "properties": properties,
        "timestamp": generation.created_at,
    }


def otlp_generation_payload(
    generation: Generation,
    *,
    settle_body: dict[str, Any],
    include_content: bool,
    input_messages: list[dict[str, Any]] | None = None,
    output_choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    trace = _trace(settle_body)
    trace_id = _otlp_hex(str(trace.get("trace_id") or generation.request_id), 32)
    span_id = _otlp_hex(generation.id, 16)
    start_ns, end_ns = _span_times_ns(generation)
    attributes = [
        _attr("gen_ai.system", "trustedrouter"),
        _attr("gen_ai.operation.name", str(settle_body.get("route_type") or "chat")),
        _attr("gen_ai.request.model", generation.model),
        _attr("gen_ai.response.model", generation.model),
        _attr("gen_ai.provider.name", generation.provider or generation.provider_name),
        _attr("gen_ai.usage.prompt_tokens", generation.tokens_prompt),
        _attr("gen_ai.usage.completion_tokens", generation.tokens_completion),
        _attr("gen_ai.usage.total_tokens", generation.tokens_prompt + generation.tokens_completion),
        _attr("gen_ai.response.finish_reasons", generation.finish_reason),
        _attr("trustedrouter.cost.microdollars", generation.total_cost_microdollars),
        _attr("trustedrouter.cost.usd", _cost_usd(generation.total_cost_microdollars)),
        _attr("trustedrouter.usage_type", generation.usage_type.value),
        _attr("trustedrouter.region", generation.region or ""),
        _attr("trustedrouter.streamed", generation.streamed),
        _attr("user.id", str(settle_body.get("user") or "")),
        _attr("session.id", str(settle_body.get("session_id") or "")),
    ]
    for key, value in trace.items():
        attributes.append(_attr(f"trace.metadata.{key}", _scalar(value)))
    if include_content:
        attributes.append(_attr("gen_ai.prompt", json.dumps(input_messages or [])))
        attributes.append(_attr("gen_ai.completion", json.dumps(output_choices or [])))
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", "trustedrouter"),
                        _attr("service.namespace", "trustedrouter"),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "trustedrouter.broadcast", "version": "1"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": str(
                                    trace.get("generation_name")
                                    or trace.get("span_name")
                                    or trace.get("trace_name")
                                    or "llm.generation"
                                ),
                                "kind": 2,
                                "startTimeUnixNano": str(start_ns),
                                "endTimeUnixNano": str(end_ns),
                                "attributes": attributes,
                                "status": {"code": 1 if generation.status == "success" else 2},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _posthog_properties(
    generation: Generation,
    *,
    settle_body: dict[str, Any],
    input_messages: list[dict[str, Any]] | None,
    output_choices: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    trace = _trace(settle_body)
    properties: dict[str, Any] = {
        "$ai_trace_id": str(trace.get("trace_id") or generation.request_id),
        "$ai_session_id": str(settle_body.get("session_id") or trace.get("session_id") or ""),
        "$ai_span_id": generation.id,
        "$ai_span_name": str(
            trace.get("generation_name") or trace.get("span_name") or trace.get("trace_name") or "llm.generation"
        ),
        "$ai_model": generation.model,
        "$ai_provider": generation.provider or generation.provider_name,
        "$ai_input_tokens": generation.tokens_prompt,
        "$ai_output_tokens": generation.tokens_completion,
        "$ai_latency": (generation.elapsed_milliseconds or 0) / 1000,
        "$ai_stream": generation.streamed,
        "$ai_http_status": 200 if generation.status == "success" else 500,
        "$ai_stop_reason": generation.finish_reason,
        "$ai_total_cost_usd": _cost_usd(generation.total_cost_microdollars),
        "trustedrouter_generation_id": generation.id,
        "trustedrouter_region": generation.region,
        "trustedrouter_usage_type": generation.usage_type.value,
    }
    if generation.first_token_milliseconds is not None:
        properties["$ai_time_to_first_token"] = generation.first_token_milliseconds / 1000
    if input_messages is not None:
        properties["$ai_input"] = input_messages
    if output_choices is not None:
        properties["$ai_output_choices"] = output_choices
    return properties


def _decrypt_api_key(destination: BroadcastDestination, settings: Settings) -> str:
    if destination.encrypted_api_key is None:
        raise ValueError("PostHog API key is not configured")
    return decrypt_control_secret(
        destination.encrypted_api_key,
        settings,
        workspace_id=destination.workspace_id,
        purpose=broadcast_secret_context(destination.id, "api_key"),
    )


def _decrypt_headers(destination: BroadcastDestination, settings: Settings) -> dict[str, str]:
    if destination.encrypted_headers is None:
        return {}
    raw = decrypt_control_secret(
        destination.encrypted_headers,
        settings,
        workspace_id=destination.workspace_id,
        purpose=broadcast_secret_context(destination.id, "headers"),
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def _posthog_capture_url(endpoint: str) -> str:
    base = (endpoint or POSTHOG_DEFAULT_ENDPOINT).rstrip("/")
    return f"{base}/i/v0/e/"


def _trace(body: dict[str, Any]) -> dict[str, Any]:
    trace = body.get("trace")
    return trace if isinstance(trace, dict) else {}


def _cost_usd(microdollars: int) -> float:
    return float(Decimal(microdollars) / Decimal(1_000_000))


def _span_times_ns(generation: Generation) -> tuple[int, int]:
    created = generation.created_at.replace("Z", "+00:00")
    try:
        created_seconds = int(time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        created_seconds = int(time.time())
    end_ns = created_seconds * 1_000_000_000
    elapsed_ns = max(generation.elapsed_milliseconds or 1, 1) * 1_000_000
    return max(0, end_ns - elapsed_ns), end_ns


def _attr(key: str, value: Any) -> dict[str, Any]:
    otlp_value: dict[str, Any]
    if isinstance(value, bool):
        otlp_value = {"boolValue": value}
    elif isinstance(value, int):
        otlp_value = {"intValue": str(value)}
    elif isinstance(value, float):
        otlp_value = {"doubleValue": value}
    else:
        otlp_value = {"stringValue": str(value)}
    return {"key": key, "value": otlp_value}


def _scalar(value: Any) -> str | int | float | bool:
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _otlp_hex(value: str, length: int) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
