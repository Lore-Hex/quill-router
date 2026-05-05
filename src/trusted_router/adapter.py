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
    """Translate Chat Completions messages to Gemini `contents`.

    Handles both string content (`{role, content: "hello"}`) and the
    multimodal array shape OpenAI's chat API uses for vision/audio:

        {role: "user", content: [
            {type: "text", text: "..."},
            {type: "image_url", image_url: {url: "data:image/jpeg;base64,..."}},
        ]}

    Image data URLs become Gemini `inline_data` parts (with mime_type +
    base64 data). Text parts become `text` parts. Without this
    conversion, the multimodal content list got `str()`-ed into a
    Python repr, which lost the image and made TR useless for OCR via
    Gemini (forty.news's primary use case).
    """
    contents: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            role = "model"
        else:
            role = "user"  # Gemini doesn't have a separate system role.
        contents.append({"role": role, "parts": _gemini_parts(msg.get("content"))})
    return contents


def _gemini_parts(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return [{"text": ""}]
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        return [{"text": str(content)}]

    parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append({"text": str(item)})
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append({"text": str(item.get("text") or "")})
        elif item_type == "image_url":
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            inline = _gemini_inline_data(url)
            if inline is not None:
                parts.append({"inline_data": inline})
            elif isinstance(url, str) and url:
                # Gemini's public API doesn't fetch arbitrary http(s)
                # image URLs the way OpenAI's vision API does. Surface
                # the URL as text so the model at least knows there was
                # one — better than silently dropping it.
                parts.append({"text": f"[image_url] {url}"})
        elif item_type == "input_audio":
            audio = item.get("input_audio") if isinstance(item.get("input_audio"), dict) else {}
            data = audio.get("data") if isinstance(audio, dict) else None
            fmt = audio.get("format") if isinstance(audio, dict) else None
            if isinstance(data, str) and isinstance(fmt, str):
                parts.append({"inline_data": {"mime_type": f"audio/{fmt}", "data": data}})
        else:
            text = item.get("text")
            if isinstance(text, str):
                parts.append({"text": text})
    if not parts:
        parts.append({"text": ""})
    return parts


def _gemini_inline_data(url: Any) -> dict[str, str] | None:
    """Convert an OpenAI-style `data:<mime>;base64,<body>` URL to
    Gemini's `inline_data` part. Returns None for non-data-URL inputs."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    head, _, body = url[5:].partition(",")
    if not body:
        return None
    mime, _, encoding = head.partition(";")
    if encoding.lower() != "base64":
        return None
    return {"mime_type": mime or "application/octet-stream", "data": body}


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
