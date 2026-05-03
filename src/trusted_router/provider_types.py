from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    provider_name: str
    request_id: str
    usage_estimated: bool = True
    elapsed_seconds: float = 0.001
    first_token_seconds: float | None = None


@dataclass
class ProviderStreamState:
    provider_name: str
    request_id: str
    input_tokens: int
    output_tokens: int = 0
    finish_reason: str = "stop"
    usage_estimated: bool = True
    elapsed_seconds: float = 0.001
    first_token_seconds: float | None = None
    started_at: float = 0.0
    text_parts: list[str] | None = None

    def __post_init__(self) -> None:
        if self.text_parts is None:
            self.text_parts = []
        if self.started_at == 0.0:
            self.started_at = time.monotonic()

    def record_text(self, text: str) -> None:
        if text:
            if self.first_token_seconds is None:
                self.first_token_seconds = max(time.monotonic() - self.started_at, 0.001)
            assert self.text_parts is not None
            self.text_parts.append(text)

    def to_result(self) -> ProviderResult:
        assert self.text_parts is not None
        text = "".join(self.text_parts)
        output_tokens = self.output_tokens or estimate_tokens_from_text(text)
        return ProviderResult(
            text=text,
            input_tokens=self.input_tokens,
            output_tokens=output_tokens,
            finish_reason=self.finish_reason,
            provider_name=self.provider_name,
            request_id=self.request_id,
            usage_estimated=self.usage_estimated,
            elapsed_seconds=max(time.monotonic() - self.started_at, 0.001),
            first_token_seconds=self.first_token_seconds,
        )


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status_code: int, message: str) -> None:
        super().__init__(f"{provider} http {status_code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.message = message


def estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += len(str(content))
    return max(1, chars // 4)


def estimate_tokens_from_text(text: str) -> int:
    return max(1, len(text) // 4)


def mock_text(model_id: str) -> str:
    return f"TrustedRouter response from {model_id}."
