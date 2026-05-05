from __future__ import annotations

from typing import Any

from trusted_router.adapter import chat_to_anthropic, chat_to_gemini
from trusted_router.catalog import Model


def anthropic_messages_payload(model: Model, request: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    base = chat_to_anthropic(messages(request), max_tokens=request.get("max_tokens") or 1024)
    payload: dict[str, Any] = {
        "model": upstream_model_id(model),
        "stream": stream,
        **base,
    }
    if request.get("temperature") is not None:
        payload["temperature"] = request["temperature"]
    return payload


def gemini_payload(request: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"contents": chat_to_gemini(messages(request))}
    generation_config: dict[str, Any] = {}
    if request.get("max_tokens") is not None:
        generation_config["maxOutputTokens"] = request["max_tokens"]
    if request.get("temperature") is not None:
        generation_config["temperature"] = request["temperature"]
    if request.get("top_p") is not None:
        generation_config["topP"] = request["top_p"]

    # Translate OpenAI's `response_format` to Gemini's
    # `responseMimeType` / `responseSchema`. forty.news's StoryDraftNode
    # and friends post `response_format: {type:"json_schema", json_schema:
    # {schema: ...}}`; without translation Gemini returns prose that
    # forty.news's `JSON.parse()` chokes on. The spelling differences
    # (camelCase, `nullable` instead of nullable types, no
    # `additionalProperties`) are smoothed over by `_gemini_response_schema`.
    response_format = request.get("response_format")
    if isinstance(response_format, dict):
        format_type = response_format.get("type")
        if format_type == "json_object":
            generation_config["responseMimeType"] = "application/json"
        elif format_type == "json_schema":
            schema_block = response_format.get("json_schema") or {}
            schema = schema_block.get("schema") if isinstance(schema_block, dict) else None
            if isinstance(schema, dict):
                generation_config["responseMimeType"] = "application/json"
                generation_config["responseSchema"] = _gemini_response_schema(schema)

    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def _gemini_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip OpenAI/JSON-Schema fields Gemini doesn't accept.

    Gemini's `responseSchema` is a strict subset of JSON Schema:
    - rejects `additionalProperties`, `$schema`, `$defs`, `definitions`
    - rejects `format` when not in its allow-list (we drop it conservatively)
    - rejects `strict`, `examples`, `default`
    - keeps `type`, `properties`, `items`, `required`, `enum`, `description`,
      `nullable`, `minimum`, `maximum`, `minLength`, `maxLength`,
      `minItems`, `maxItems`, `propertyOrdering`."""
    if not isinstance(schema, dict):
        return {}
    allowed_keys = {
        "type",
        "properties",
        "items",
        "required",
        "enum",
        "description",
        "nullable",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "propertyOrdering",
    }
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed_keys:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                prop_name: _gemini_response_schema(prop_schema)
                for prop_name, prop_schema in value.items()
                if isinstance(prop_schema, dict)
            }
        elif key == "items" and isinstance(value, dict):
            out[key] = _gemini_response_schema(value)
        else:
            out[key] = value
    return out


def messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list):
        return []
    return [msg for msg in raw_messages if isinstance(msg, dict)]


def upstream_model_id(model: Model) -> str:
    if model.upstream_id:
        return model.upstream_id
    return model.id.split("/", 1)[1] if "/" in model.id else model.id


def is_vertex_openai_model(model: Model) -> bool:
    return model.id.startswith("vertex/") or bool(model.upstream_id and model.upstream_id.startswith("google/"))


def deterministic_embedding(text: str) -> list[float]:
    seed = sum(ord(ch) for ch in text)
    return [((seed + i * 31) % 1000) / 1000.0 for i in range(16)]
