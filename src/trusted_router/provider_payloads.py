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
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


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
