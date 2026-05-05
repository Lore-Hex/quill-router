"""Multimodal content adapters: forty.news (and any OCR caller) sends
OpenAI-style `content: [{type:"image_url",image_url:{url:"data:..."}}, ...]`
arrays. These tests pin the conversion to upstream provider shapes —
both at the adapter unit level and at the live-adapter integration
level mirroring forty.news's actual `ImageToTextNode` request."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from trusted_router.adapter import chat_to_gemini
from trusted_router.catalog import MODELS
from trusted_router.providers import ProviderClient
from trusted_router.secrets import LocalKeyFile


def test_chat_to_gemini_passes_string_content_through() -> None:
    out = chat_to_gemini([{"role": "user", "content": "hello"}])
    assert out == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_chat_to_gemini_maps_assistant_role_to_model() -> None:
    out = chat_to_gemini([
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ])
    assert [c["role"] for c in out] == ["user", "model"]


def test_chat_to_gemini_handles_image_url_data_url() -> None:
    """forty.news OCR call — `image_url.url` is a data: base64 URL.
    Must become a Gemini `inline_data` part (mime_type + raw base64)."""
    out = chat_to_gemini([
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Zm9v"}},
                {"type": "text", "text": "Extract all text from this newspaper image."},
            ],
        }
    ])
    assert out == [
        {
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": "Zm9v"}},
                {"text": "Extract all text from this newspaper image."},
            ],
        }
    ]


def test_chat_to_gemini_handles_png_data_url() -> None:
    out = chat_to_gemini([
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,UE5HRGF0YQ=="}},
            ],
        }
    ])
    assert out[0]["parts"][0] == {
        "inline_data": {"mime_type": "image/png", "data": "UE5HRGF0YQ=="}
    }


def test_chat_to_gemini_falls_back_to_text_for_non_data_image_url() -> None:
    """Gemini's public API doesn't fetch http(s) image URLs the way
    OpenAI's vision API does. Surface the URL as text so the model at
    least knows there was one rather than silently dropping it."""
    out = chat_to_gemini([
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}},
            ],
        }
    ])
    assert out[0]["parts"] == [{"text": "[image_url] https://example.com/photo.jpg"}]


def test_chat_to_gemini_drops_unknown_part_types_gracefully() -> None:
    """If an unknown content-array part comes through, keep any text we
    can find and don't crash."""
    out = chat_to_gemini([
        {
            "role": "user",
            "content": [
                {"type": "tool_use", "text": "fallback"},
                {"type": "text", "text": "hello"},
            ],
        }
    ])
    assert {"text": "fallback"} in out[0]["parts"]
    assert {"text": "hello"} in out[0]["parts"]


def test_chat_to_gemini_empty_content_array_yields_empty_text_part() -> None:
    out = chat_to_gemini([{"role": "user", "content": []}])
    assert out == [{"role": "user", "parts": [{"text": ""}]}]


def test_chat_to_gemini_input_audio_becomes_inline_audio_data() -> None:
    out = chat_to_gemini([
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}},
            ],
        }
    ])
    assert out[0]["parts"][0] == {"inline_data": {"mime_type": "audio/mp3", "data": "QUJD"}}


def test_gemini_response_inline_data_becomes_data_url_in_content() -> None:
    """forty.news's StoryImageNode posts to a Gemini image-gen model
    (e.g. gemini-3.1-flash-image-preview) and parses the response with:

      content?.match(/data:image\\/[^;]+;base64,[A-Za-z0-9+/=]+/)
      data.choices?.[0]?.message?.images?.[0]?.image_url?.url
      data.choices?.[0]?.message?.parts?.[].inline_data?.data

    Gemini returns the image via `candidates[0].content.parts[].inline_data`.
    Pre-fix, TR's adapter only concatenated `text` parts, so the image
    bytes were dropped and forty.news got an empty content. Now image
    parts emerge as `data:<mime>;base64,<body>` URLs in `content` —
    forty.news's first regex match catches them."""
    from trusted_router.provider_adapters import _gemini_parts_to_openai_content

    parts = [
        {"text": "Here is the requested newspaper photograph:"},
        {"inline_data": {"mime_type": "image/png", "data": "UE5HRGF0YQ=="}},
    ]
    out = _gemini_parts_to_openai_content(parts)
    assert "Here is the requested newspaper photograph:" in out
    assert "data:image/png;base64,UE5HRGF0YQ==" in out


def test_gemini_response_inline_data_handles_camelcase_inlineData_alias() -> None:
    """Gemini's REST API has shipped both spellings (`inline_data` snake
    case and `inlineData` camelCase) at different times; accept both."""
    from trusted_router.provider_adapters import _gemini_parts_to_openai_content

    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": "Zm9v"}}]
    out = _gemini_parts_to_openai_content(parts)
    assert out == "data:image/jpeg;base64,Zm9v"


def test_gemini_response_text_only_unchanged() -> None:
    """Pure-text responses (the OCR / chat case) keep their content
    intact — joining multiple text parts with newlines, no extra
    artifacts from the image-handling branch."""
    from trusted_router.provider_adapters import _gemini_parts_to_openai_content

    parts = [{"text": "line one"}, {"text": "line two"}]
    assert _gemini_parts_to_openai_content(parts) == "line one\nline two"


@pytest.mark.asyncio
async def test_forty_news_ocr_request_reaches_gemini_with_inline_image_data(
    tmp_path, monkeypatch,
) -> None:
    """End-to-end: builds the exact request shape forty.news's
    `ImageToTextNode.exec()` posts to OpenRouter, fires it through the
    live Gemini adapter, and asserts the upstream call carries Gemini
    `inline_data` (mime_type + base64 data) — not a stringified Python
    repr of the OpenAI content list. Without the multimodal fix this
    test fails: Gemini receives garbage instead of the image."""

    key_file = tmp_path / "keys.private"
    key_file.write_text("GEMINI_API_KEY=gemini-value\n", encoding="utf-8")

    captured: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            params: dict[str, str],
            json: dict[str, Any],
            **_: Any,
        ):
            captured.append({"url": url, "params": params, "json": json})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": "Section A Page 1\nNEWS HEADLINE..."}]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 2048,
                        "candidatesTokenCount": 512,
                    },
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    # Tiny but valid base64 PNG — the actual bytes don't matter for the
    # test; we just need to verify they survive the adapter intact.
    fake_jpeg_b64 = "/9j/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AKpgD//Z"

    # Mirrors scripts/caskada/nodes/ImageToTextNode.js exactly:
    #   model: 'google/gemini-2.5-flash'
    #   messages: [{role: 'user', content: [
    #     {type: 'image_url', image_url: {url: 'data:image/jpeg;base64,...'}},
    #     {type: 'text', text: '...'},
    #   ]}]
    forty_news_request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{fake_jpeg_b64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract all text from this newspaper image. "
                            "IMPORTANT: Pay special attention to the section "
                            "and page number..."
                        ),
                    },
                ],
            }
        ],
        "max_tokens": 4096,
    }

    result = await client.chat(MODELS["google/gemini-2.5-flash"], forty_news_request)

    # Gemini answered the OCR request and TR returned the OCR text.
    assert "NEWS HEADLINE" in result.text

    # The upstream call carried real inline_data, not a stringified
    # OpenAI content list.
    assert len(captured) == 1
    upstream_body = captured[0]["json"]
    contents = upstream_body["contents"]
    assert len(contents) == 1
    parts = contents[0]["parts"]
    assert len(parts) == 2
    image_part, text_part = parts
    assert image_part == {
        "inline_data": {"mime_type": "image/jpeg", "data": fake_jpeg_b64}
    }
    assert "newspaper image" in text_part["text"]


def test_gemini_payload_translates_openai_json_schema_response_format() -> None:
    """forty.news's StoryDraftNode posts:

        response_format: {type:"json_schema", json_schema:{schema:{...}}}

    Gemini doesn't speak that — it wants `responseMimeType=application/json`
    + `responseSchema={...}` on `generationConfig`. Without translation,
    Gemini ignores the hint and returns prose; forty.news's
    `JSON.parse()` then crashes. Pin the translation here."""
    from trusted_router.provider_payloads import gemini_payload

    out = gemini_payload({
        "messages": [{"role": "user", "content": "make a story"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "story_draft",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string", "description": "Short factual headline"},
                        "article": {"type": "string", "description": "1-200 word synopsis"},
                    },
                    "required": ["headline", "article"],
                    "additionalProperties": False,
                },
            },
        },
        "max_tokens": 500,
    })
    config = out["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    assert schema["type"] == "object"
    # Required keys preserved.
    assert sorted(schema["required"]) == ["article", "headline"]
    # `additionalProperties` and `strict` must be stripped — Gemini
    # rejects them as unknown fields.
    assert "additionalProperties" not in schema
    assert "strict" not in schema
    # Property descriptions preserved verbatim.
    assert schema["properties"]["headline"]["description"] == "Short factual headline"


def test_gemini_payload_handles_json_object_response_format() -> None:
    """`response_format: {type:"json_object"}` (the lighter sibling of
    json_schema) just sets the mime type — no schema attached."""
    from trusted_router.provider_payloads import gemini_payload

    out = gemini_payload({
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_object"},
    })
    assert out["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" not in out["generationConfig"]


def test_gemini_payload_strips_nested_unsupported_fields() -> None:
    """Unsupported fields buried inside `properties.<x>` and `items`
    must also be removed — Gemini validates the schema recursively."""
    from trusted_router.provider_payloads import gemini_payload

    out = gemini_payload({
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "deep",
                "schema": {
                    "type": "object",
                    "properties": {
                        "tags": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "format": "uuid",   # not in Gemini's allow-list
                                "examples": ["foo"],
                            },
                            "additionalProperties": False,
                        },
                    },
                },
            },
        },
    })
    items = out["generationConfig"]["responseSchema"]["properties"]["tags"]["items"]
    assert items == {"type": "string"}
    assert "additionalProperties" not in out["generationConfig"]["responseSchema"]["properties"]["tags"]


@pytest.mark.asyncio
async def test_forty_news_image_payload_is_not_stringified_or_dropped(
    tmp_path, monkeypatch,
) -> None:
    """Regression guard: before the multimodal fix, the content list was
    `str()`-cast and the upstream Gemini body would contain something
    like `{"text": "[{'type': 'image_url', 'image_url': ...}]"}` —
    losing the image bytes entirely. Pin that this never happens."""

    key_file = tmp_path / "keys.private"
    key_file.write_text("GEMINI_API_KEY=gemini-value\n", encoding="utf-8")

    captured: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(
            self, url: str, *, params: dict[str, str], json: dict[str, Any], **_: Any
        ):
            captured.append(json)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                },
            )

    monkeypatch.setattr("trusted_router.provider_adapters.httpx.AsyncClient", FakeAsyncClient)
    client = ProviderClient(LocalKeyFile(key_file), live=True)

    await client.chat(
        MODELS["google/gemini-2.5-flash"],
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,UE5H"}},
                        {"type": "text", "text": "describe"},
                    ],
                }
            ],
        },
    )

    body = captured[0]
    flat = str(body)
    # Smoking-gun strings that would only appear if the OpenAI content
    # list got `str()`-cast wholesale.
    assert "'type': 'image_url'" not in flat
    assert "'image_url':" not in flat
    # The base64 image bytes survived to the upstream payload.
    assert "UE5H" in flat
