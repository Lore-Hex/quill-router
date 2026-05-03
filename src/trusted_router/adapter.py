"""Message-format adapters between API surfaces.

The control plane accepts three input shapes:
- OpenAI Chat Completions (`/chat/completions`) — the canonical internal shape.
- Anthropic Messages (`/messages`) — `system` + `messages` arrays, content blocks.
- OpenAI Responses (`/responses`) — `instructions` + `input` (string or list).

Provider clients also need outbound conversions back to provider-specific
shapes: Anthropic separates `system` from `messages`, Gemini uses `contents`
with `model`/`user` roles. Centralizing the conversions here keeps the
mappings in one place and makes it easy to add a new shape.
"""

from __future__ import annotations

from typing import Any

from trusted_router.errors import api_error


def responses_to_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI Responses request to a Chat Completions body."""
    model = body.get("model")
    if not model:
        raise api_error(400, "model is required", "bad_request")
    input_value = body.get("input")
    if input_value is None:
        raise api_error(400, "input is required", "bad_request")
    messages: list[dict[str, Any]] = []
    if instructions := body.get("instructions"):
        messages.append({"role": "system", "content": str(instructions)})
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            messages.append(_responses_input_item_to_chat_message(item))
    else:
        raise api_error(400, "input must be a string or list", "bad_request")
    return {
        "model": str(model),
        "messages": messages,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_tokens": body.get("max_output_tokens") or body.get("max_tokens"),
        "stream": False,
    }


def messages_to_chat_body(body: dict[str, Any], *, model_id: str) -> dict[str, Any]:
    """Translate an Anthropic Messages request to a Chat Completions body.

    Anthropic-style `system` is moved into a leading `system` chat message.
    """
    chat_body: dict[str, Any] = {
        "model": model_id,
        "messages": list(body.get("messages") or []),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
    }
    if system := body.get("system"):
        chat_body["messages"] = [{"role": "system", "content": system}, *chat_body["messages"]]
    return chat_body


def chat_to_anthropic(messages: list[dict[str, Any]], *, max_tokens: int) -> dict[str, Any]:
    """Translate Chat Completions messages to an Anthropic Messages payload.

    System messages are concatenated into the top-level `system` field; only
    user/assistant turns appear in `messages`.
    """
    system_parts: list[str] = []
    out_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.append(str(msg.get("content", "")))
        elif role in {"user", "assistant"}:
            out_messages.append({"role": role, "content": msg.get("content", "")})
    payload: dict[str, Any] = {
        "messages": out_messages,
        "max_tokens": int(max_tokens or 1024),
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    return payload


def chat_to_gemini(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Chat Completions messages to Gemini `contents`."""
    contents: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            role = "model"
        else:
            role = "user"  # Gemini doesn't have a separate system role.
        contents.append({"role": role, "parts": [{"text": str(msg.get("content", ""))}]})
    return contents


def _responses_input_item_to_chat_message(item: Any) -> dict[str, Any]:
    if isinstance(item, dict) and item.get("type") == "message":
        role = str(item.get("role") or "user")
        content = item.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        return {"role": role, "content": str(content)}
    if isinstance(item, dict) and "role" in item:
        return {
            "role": str(item.get("role")),
            "content": str(item.get("content", "")),
        }
    return {"role": "user", "content": str(item)}
