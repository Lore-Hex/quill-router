"""Multimodal content adapters: forty.news (and any OCR caller) sends
OpenAI-style `content: [{type:"image_url",image_url:{url:"data:..."}}, ...]`
arrays. These tests pin the conversion to upstream provider shapes."""

from __future__ import annotations

from trusted_router.adapter import chat_to_gemini


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
