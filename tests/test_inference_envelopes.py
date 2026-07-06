from __future__ import annotations

from types import SimpleNamespace

from trusted_router.routes.inference import _chat_completion_envelope


def test_chat_completion_envelope_includes_known_usage_details() -> None:
    result = SimpleNamespace(
        request_id="req_details",
        text="hello",
        input_tokens=10,
        output_tokens=8,
        finish_reason="stop",
        cached_input_tokens=4,
        reasoning_tokens=3,
    )

    envelope = _chat_completion_envelope(
        result=result,
        model_id="openai/gpt-5.4-nano",
        generation_id="gen_details",
    )

    assert envelope["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 8,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }


def test_chat_completion_envelope_omits_empty_usage_details() -> None:
    result = SimpleNamespace(
        request_id="req_no_details",
        text="hello",
        input_tokens=10,
        output_tokens=8,
        finish_reason="stop",
        cached_input_tokens=0,
        reasoning_tokens=0,
    )

    envelope = _chat_completion_envelope(
        result=result,
        model_id="openai/gpt-5.4-nano",
        generation_id="gen_no_details",
    )

    assert envelope["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 8,
        "total_tokens": 18,
    }


def test_chat_completion_envelope_uses_null_content_for_tool_call_only_reply() -> None:
    tool_calls = [
        {
            "id": "call_lookup",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]
    result = SimpleNamespace(
        request_id="req_tool",
        text="",
        input_tokens=10,
        output_tokens=2,
        finish_reason="tool_calls",
        cached_input_tokens=0,
        reasoning_tokens=0,
        tool_calls=tool_calls,
    )

    envelope = _chat_completion_envelope(
        result=result,
        model_id="openai/gpt-5.4-nano",
        generation_id="gen_tool",
    )

    message = envelope["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"] == tool_calls
