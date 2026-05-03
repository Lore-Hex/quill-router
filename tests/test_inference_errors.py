"""Coverage for services.inference_errors — the small mapping module
that translates ProviderError status codes into HTTP responses, decides
which HTTP errors are eligible for fallback rollover, and centralizes
the default secret-ref pointer for each provider.

Each helper is a pure function, so we test them directly rather than
through the FastAPI app."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from trusted_router.providers import ProviderError
from trusted_router.services.inference_errors import (
    all_candidates_failed,
    default_provider_secret_ref,
    http_error_message,
    is_rollover_http_error,
    provider_error_type,
    provider_http_error,
)
from trusted_router.types import ErrorType

# ── provider_http_error ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "upstream_status, expected_status, expected_type",
    [
        (401, 401, ErrorType.PROVIDER_AUTH_ERROR),
        (403, 401, ErrorType.PROVIDER_AUTH_ERROR),
        (404, 400, ErrorType.MODEL_NOT_SUPPORTED),
        (429, 429, ErrorType.PROVIDER_RATE_LIMITED),
        (500, 502, ErrorType.PROVIDER_ERROR),
        (502, 502, ErrorType.PROVIDER_ERROR),
        (503, 502, ErrorType.PROVIDER_ERROR),
        (504, 502, ErrorType.PROVIDER_ERROR),
        (529, 502, ErrorType.PROVIDER_ERROR),
        # Anything outside the matched buckets falls through to the default
        # 502 PROVIDER_ERROR.
        (418, 502, ErrorType.PROVIDER_ERROR),
        (200, 502, ErrorType.PROVIDER_ERROR),
    ],
)
def test_provider_http_error_status_mapping(
    upstream_status: int, expected_status: int, expected_type: str
) -> None:
    exc = ProviderError("openai", upstream_status, "boom")
    http = provider_http_error(exc)
    assert isinstance(http, HTTPException)
    assert http.status_code == expected_status
    assert http.detail["error"]["type"] == expected_type
    assert http.detail["error"]["message"] == "openai: boom"


# ── provider_error_type (used to label benchmark samples) ──────────────


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, ErrorType.PROVIDER_AUTH_ERROR),
        (403, ErrorType.PROVIDER_AUTH_ERROR),
        (429, ErrorType.PROVIDER_RATE_LIMITED),
        (500, ErrorType.PROVIDER_ERROR),
        (502, ErrorType.PROVIDER_ERROR),
        (599, ErrorType.PROVIDER_ERROR),
    ],
)
def test_provider_error_type_classification(status: int, expected: str) -> None:
    assert provider_error_type(status) == expected


# ── default_provider_secret_ref ────────────────────────────────────────


@pytest.mark.parametrize(
    "provider, expected_ref",
    [
        ("anthropic", "env://ANTHROPIC_API_KEY"),
        ("openai", "env://OPENAI_API_KEY"),
        ("gemini", "env://GEMINI_API_KEY"),
        ("cerebras", "env://CEREBRAS_API_KEY"),
        ("deepseek", "env://DEEPSEEK_API_KEY"),
        ("mistral", "env://MISTRAL_API_KEY"),
        ("kimi", "env://KIMI_API_KEY"),
        ("vertex", "env://VERTEX_ACCESS_TOKEN"),
        # Unknown providers fall back to UPPER + _API_KEY.
        ("nebula", "env://NEBULA_API_KEY"),
    ],
)
def test_default_provider_secret_ref_known_and_fallback(
    provider: str, expected_ref: str
) -> None:
    assert default_provider_secret_ref(provider) == expected_ref


def test_vertex_uses_access_token_not_api_key() -> None:
    """Vertex is special: production GCP uses short-lived access tokens
    from metadata/ADC, not a long-lived API key. If this regresses to
    `VERTEX_API_KEY`, every Vertex call breaks in the enclave."""
    assert default_provider_secret_ref("vertex") == "env://VERTEX_ACCESS_TOKEN"


# ── is_rollover_http_error ─────────────────────────────────────────────


def _detail(error_type: str | None = None, message: str = "x") -> dict:
    error: dict = {"message": message}
    if error_type is not None:
        error["type"] = error_type
    return {"error": error}


@pytest.mark.parametrize(
    "status, error_type, expected",
    [
        # Provider failures across the rollover set are eligible.
        (502, ErrorType.PROVIDER_ERROR, True),
        (503, ErrorType.PROVIDER_ERROR, True),
        (504, ErrorType.PROVIDER_ERROR, True),
        (429, ErrorType.PROVIDER_RATE_LIMITED, True),
        (500, ErrorType.PROVIDER_ERROR, True),
        # Provider auth errors are eligible: a key rotation can land
        # mid-rollover.
        (502, ErrorType.PROVIDER_AUTH_ERROR, True),
        # Status outside the rollover set fails closed.
        (400, ErrorType.PROVIDER_ERROR, False),
        (401, ErrorType.PROVIDER_AUTH_ERROR, False),
        (403, ErrorType.PROVIDER_AUTH_ERROR, False),
    ],
)
def test_is_rollover_http_error_with_typed_detail(
    status: int, error_type: str, expected: bool
) -> None:
    exc = HTTPException(status_code=status, detail=_detail(error_type))
    assert is_rollover_http_error(exc) is expected


def test_is_rollover_http_error_falls_back_to_status_only_when_detail_is_string() -> None:
    """detail can be a plain string (FastAPI's default for HTTPException
    raised without a body). The function still has to make a yes/no call;
    it falls back to the broad rollover-status set."""
    assert is_rollover_http_error(HTTPException(status_code=502, detail="upstream")) is True
    assert is_rollover_http_error(HTTPException(status_code=429, detail="busy")) is True
    assert is_rollover_http_error(HTTPException(status_code=500, detail="boom")) is False
    assert is_rollover_http_error(HTTPException(status_code=400, detail="bad")) is False


def test_is_rollover_http_error_treats_bad_request_type_as_non_rollover() -> None:
    exc = HTTPException(status_code=502, detail=_detail(ErrorType.BAD_REQUEST))
    assert is_rollover_http_error(exc) is False


# ── http_error_message ─────────────────────────────────────────────────


def test_http_error_message_extracts_from_typed_detail() -> None:
    exc = HTTPException(status_code=502, detail=_detail(ErrorType.PROVIDER_ERROR, "openai down"))
    assert http_error_message(exc) == "openai down"


def test_http_error_message_falls_back_to_type_then_status() -> None:
    # No message → returns the type.
    no_message = HTTPException(
        status_code=502, detail={"error": {"type": ErrorType.PROVIDER_ERROR}}
    )
    assert http_error_message(no_message) == ErrorType.PROVIDER_ERROR

    # No type either → returns the stringified status.
    bare = HTTPException(status_code=502, detail={"error": {}})
    assert http_error_message(bare) == "502"


def test_http_error_message_handles_string_detail() -> None:
    assert http_error_message(HTTPException(status_code=502, detail="raw")) == "raw"


# ── all_candidates_failed ──────────────────────────────────────────────


def test_all_candidates_failed_with_messages_keeps_last_three() -> None:
    errors = ["a fail", "b fail", "c fail", "d fail", "e fail"]
    exc = all_candidates_failed(errors)

    assert isinstance(exc, HTTPException)
    assert exc.status_code == 502
    assert exc.detail["error"]["type"] == ErrorType.PROVIDER_ERROR
    suffix = exc.detail["error"]["message"]
    assert "c fail; d fail; e fail" in suffix
    assert "a fail" not in suffix


def test_all_candidates_failed_without_messages_uses_default_suffix() -> None:
    exc = all_candidates_failed([])
    assert exc.status_code == 502
    assert "no candidates were available" in exc.detail["error"]["message"]
