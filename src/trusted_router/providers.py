from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from trusted_router.catalog import PROVIDERS, Model
from trusted_router.provider_adapters import (
    anthropic_chat,
    anthropic_messages_stream,
    gemini_chat,
    gemini_chat_stream,
    openai_compatible_chat,
    openai_compatible_chat_stream,
)
from trusted_router.provider_payloads import (
    deterministic_embedding,
    is_vertex_openai_model,
    messages,
)
from trusted_router.provider_streaming import (
    anthropic_sse,
    chunk_text,
    stream_openai_chunks,
)
from trusted_router.provider_types import (
    ProviderError,
    ProviderResult,
    ProviderStreamState,
    estimate_tokens_from_messages,
    estimate_tokens_from_text,
    mock_text,
)
from trusted_router.secrets import LocalKeyFile

OPENAI_COMPATIBLE_PROVIDERS: dict[str, tuple[tuple[str, ...], str]] = {
    "openai": (("OPENAI_API_KEY",), "https://api.openai.com/v1"),
    "cerebras": (("CEREBRAS_API_KEY",), "https://api.cerebras.ai/v1"),
    "deepseek": (("DEEPSEEK_API_KEY",), "https://api.deepseek.com"),
    "mistral": (("MISTRAL_API_KEY",), "https://api.mistral.ai/v1"),
    "kimi": (("KIMI_API_KEY", "MOONSHOT_API_KEY"), "https://api.moonshot.ai/v1"),
}

__all__ = [
    "OPENAI_COMPATIBLE_PROVIDERS",
    "ProviderClient",
    "ProviderError",
    "ProviderResult",
    "ProviderStreamState",
    "estimate_tokens_from_messages",
    "estimate_tokens_from_text",
    "stream_openai_chunks",
]


class ProviderClient:
    def __init__(self, key_file: LocalKeyFile, live: bool = False) -> None:
        self.key_file = key_file
        self.live = live

    async def chat(self, model: Model, request: dict[str, Any]) -> ProviderResult:
        provider = PROVIDERS[model.provider]
        if self.live:
            openai_compatible = OPENAI_COMPATIBLE_PROVIDERS.get(model.provider)
            if openai_compatible is not None:
                env_keys, base_url = openai_compatible
                return await self._openai_compatible_chat(
                    model,
                    request,
                    env_keys=env_keys,
                    base_url=self._provider_base_url(model.provider, base_url),
                )
            if model.provider == "anthropic":
                return await self._anthropic_chat(model, request)
            if model.provider == "gemini":
                return await self._gemini_chat(model, request)
            if model.provider == "vertex" and is_vertex_openai_model(model):
                return await self._vertex_openai_compatible_chat(model, request)

        started = time.monotonic()
        text = mock_text(model.id)
        return ProviderResult(
            text=text,
            input_tokens=estimate_tokens_from_messages(messages(request)),
            output_tokens=estimate_tokens_from_text(text),
            finish_reason="stop",
            provider_name=provider.name,
            request_id=f"req-{uuid.uuid4()}",
            usage_estimated=True,
            elapsed_seconds=max(time.monotonic() - started, 0.001),
        )

    def new_stream_state(self, model: Model, request: dict[str, Any]) -> ProviderStreamState:
        return ProviderStreamState(
            provider_name=PROVIDERS[model.provider].name,
            request_id=f"req-{uuid.uuid4()}",
            input_tokens=estimate_tokens_from_messages(messages(request)),
        )

    def stream_chat(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        if self.live:
            openai_compatible = OPENAI_COMPATIBLE_PROVIDERS.get(model.provider)
            if openai_compatible is not None:
                env_keys, base_url = openai_compatible
                return self._openai_compatible_chat_stream(
                    model,
                    request,
                    state,
                    env_keys=env_keys,
                    base_url=self._provider_base_url(model.provider, base_url),
                )
            if model.provider == "anthropic":
                return self._anthropic_messages_stream(model, request, state, output_format="openai")
            if model.provider == "gemini":
                return self._gemini_chat_stream(model, request, state)
            if model.provider == "vertex" and is_vertex_openai_model(model):
                return self._vertex_openai_compatible_chat_stream(model, request, state)
        return self._synthetic_chat_stream(model, request, state)

    def stream_messages(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        if self.live and model.provider == "anthropic":
            return self._anthropic_messages_stream(model, request, state, output_format="anthropic")
        return self._synthetic_anthropic_messages_stream(model, request, state)

    async def embeddings(self, model: Model, request: dict[str, Any]) -> dict[str, Any]:
        input_value = request.get("input", "")
        inputs = input_value if isinstance(input_value, list) else [input_value]
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": deterministic_embedding(str(item)),
                    "index": index,
                }
                for index, item in enumerate(inputs)
            ],
            "model": model.id,
            "usage": {
                "prompt_tokens": sum(max(1, len(str(item)) // 4) for item in inputs),
                "total_tokens": sum(max(1, len(str(item)) // 4) for item in inputs),
            },
        }

    async def _openai_compatible_chat(
        self,
        model: Model,
        request: dict[str, Any],
        *,
        env_keys: tuple[str, ...] | None = None,
        auth_token: str | None = None,
        base_url: str,
    ) -> ProviderResult:
        api_key = auth_token or self._secret_any(env_keys)
        if not api_key:
            raise RuntimeError(f"{_credential_label(env_keys, model.provider)} is not configured")
        return await openai_compatible_chat(model, request, api_key=api_key, base_url=base_url)

    async def _anthropic_chat(self, model: Model, request: dict[str, Any]) -> ProviderResult:
        api_key = self._secret("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        return await anthropic_chat(model, request, api_key=api_key)

    def _openai_compatible_chat_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
        *,
        env_keys: tuple[str, ...],
        auth_token: str | None = None,
        base_url: str,
    ) -> AsyncIterator[bytes]:
        api_key = auth_token or self._secret_any(env_keys)
        if not api_key:
            raise RuntimeError(f"{_credential_label(env_keys, model.provider)} is not configured")
        return openai_compatible_chat_stream(model, request, state, api_key=api_key, base_url=base_url)

    def _anthropic_messages_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
        *,
        output_format: str,
    ) -> AsyncIterator[bytes]:
        api_key = self._secret("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        return anthropic_messages_stream(model, request, state, api_key=api_key, output_format=output_format)

    async def _synthetic_chat_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        result = await self.chat(model, {**request, "stream": False})
        state.request_id = result.request_id
        state.provider_name = result.provider_name
        state.input_tokens = result.input_tokens
        state.output_tokens = result.output_tokens
        state.finish_reason = result.finish_reason
        state.usage_estimated = result.usage_estimated
        state.elapsed_seconds = result.elapsed_seconds
        state.text_parts = [result.text]
        async for chunk in stream_openai_chunks(
            request_id=result.request_id,
            model_id=model.id,
            text=result.text,
            finish_reason=result.finish_reason,
        ):
            yield chunk

    async def _synthetic_anthropic_messages_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        result = await self.chat(model, {**request, "stream": False})
        state.request_id = result.request_id
        state.provider_name = result.provider_name
        state.input_tokens = result.input_tokens
        state.output_tokens = result.output_tokens
        state.finish_reason = result.finish_reason
        state.usage_estimated = result.usage_estimated
        state.elapsed_seconds = result.elapsed_seconds
        state.text_parts = [result.text]
        yield anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": result.request_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model.id,
                    "stop_reason": None,
                    "usage": {"input_tokens": result.input_tokens, "output_tokens": 0},
                },
            },
        )
        yield anthropic_sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )
        for token in chunk_text(result.text):
            yield anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": token},
                },
            )
        yield anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": result.finish_reason, "stop_sequence": None},
                "usage": {"output_tokens": result.output_tokens},
            },
        )
        yield anthropic_sse("message_stop", {"type": "message_stop"})

    async def _gemini_chat(self, model: Model, request: dict[str, Any]) -> ProviderResult:
        api_key = self._secret("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        return await gemini_chat(model, request, api_key=api_key)

    def _gemini_chat_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        api_key = self._secret("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        return gemini_chat_stream(model, request, state, api_key=api_key)

    async def _vertex_openai_compatible_chat(
        self, model: Model, request: dict[str, Any]
    ) -> ProviderResult:
        token, base_url = self._vertex_auth_and_base_url()
        return await self._openai_compatible_chat(
            model,
            request,
            auth_token=token,
            base_url=base_url,
        )

    def _vertex_openai_compatible_chat_stream(
        self,
        model: Model,
        request: dict[str, Any],
        state: ProviderStreamState,
    ) -> AsyncIterator[bytes]:
        token, base_url = self._vertex_auth_and_base_url()
        return self._openai_compatible_chat_stream(
            model,
            request,
            state,
            env_keys=("VERTEX_ACCESS_TOKEN",),
            auth_token=token,
            base_url=base_url,
        )

    def _secret(self, name: str | None) -> str | None:
        if name is None:
            return None
        return self.key_file.get(name) or os.environ.get(name)

    def _secret_any(self, names: tuple[str, ...] | None) -> str | None:
        for name in names or ():
            value = self._secret(name)
            if value:
                return value
        return None

    def _provider_base_url(self, provider: str, default: str) -> str:
        if provider == "kimi":
            return self._secret("KIMI_BASE_URL") or self._secret("MOONSHOT_BASE_URL") or default
        return self._secret(f"{provider.upper()}_BASE_URL") or default

    def _vertex_auth_and_base_url(self) -> tuple[str, str]:
        token = self._secret("VERTEX_ACCESS_TOKEN") or self._secret("GOOGLE_OAUTH_ACCESS_TOKEN")
        project_id = (
            self._secret("VERTEX_PROJECT_ID")
            or self._secret("GOOGLE_CLOUD_PROJECT")
            or self._secret("GCP_PROJECT_ID")
            or self._secret("TR_GCP_PROJECT_ID")
        )
        location = (
            self._secret("VERTEX_LOCATION")
            or self._secret("GOOGLE_CLOUD_REGION")
            or self._secret("TR_PRIMARY_REGION")
            or "us-central1"
        )

        if token is None or project_id is None:
            try:
                import google.auth
                from google.auth.transport.requests import Request
            except ImportError as exc:
                raise RuntimeError("google-auth is required for Vertex ADC") from exc
            credentials, default_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if token is None:
                credentials.refresh(Request())
                token = credentials.token
            if project_id is None:
                project_id = default_project

        if not token:
            raise RuntimeError("VERTEX_ACCESS_TOKEN or Google ADC is required for Vertex")
        if not project_id:
            raise RuntimeError("VERTEX_PROJECT_ID or Google ADC project is required for Vertex")

        base_url = self._secret("VERTEX_OPENAI_BASE_URL")
        if base_url:
            return token, base_url.rstrip("/")

        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        return (
            token,
            f"https://{host}/v1beta1/projects/{project_id}/locations/{location}/endpoints/openapi",
        )


def _credential_label(env_keys: tuple[str, ...] | None, provider: str) -> str:
    if env_keys:
        return " or ".join(env_keys)
    return provider + " credentials"
