"""Settlement must understand each provider's word for the ordinary tier.

This is a post-generation code path, which makes it unusually expensive to get
wrong: by the time settlement runs the upstream call has already been made and
paid for, so rejecting the report means TrustedRouter eats the provider cost
AND the caller gets a 502. That is exactly what happened in production — every
`anthropic/*` request failed with

    /internal/gateway/settle http 400:
    settlement service_tier must be the actual default or priority tier

because Anthropic reports `usage.service_tier="standard"` where OpenAI reports
`"default"`, and only the OpenAI spelling was accepted.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.internal.gateway import _actual_service_tier_or_error


@pytest.mark.parametrize("reported", ["standard", "STANDARD", " Standard "])
def test_anthropics_word_for_the_ordinary_tier_settles_as_default(reported: str) -> None:
    assert _actual_service_tier_or_error(reported) == "default"


@pytest.mark.parametrize("reported", ["default", "priority", "PRIORITY"])
def test_canonical_tiers_are_unchanged(reported: str) -> None:
    assert _actual_service_tier_or_error(reported) == reported.strip().lower()


def test_absent_tier_stays_absent() -> None:
    assert _actual_service_tier_or_error(None) is None


@pytest.mark.parametrize("reported", ["batch", "flex", "scale"])
def test_cheaper_tiers_are_not_silently_settled_as_default(reported: str) -> None:
    """Anthropic `batch` and OpenAI `flex`/`scale` cost LESS than default.

    Aliasing them to "default" would overcharge the customer, which is worse
    than failing, so they must keep raising rather than be swept into the
    synonym table alongside "standard".
    """
    with pytest.raises(HTTPException) as raised:
        _actual_service_tier_or_error(reported)
    assert raised.value.status_code == 400


def test_unknown_tier_still_fails_loudly() -> None:
    with pytest.raises(HTTPException):
        _actual_service_tier_or_error("turbo-supreme")


# --- end to end: the production failure, through the real settle route ---


def _client_and_key() -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "tier-vocab@example.com"},
        json={"name": "service tier vocabulary"},
    )
    assert created.status_code in (200, 201), created.text
    return client, created.json()["data"]


@pytest.mark.parametrize(
    ("model", "reported_tier"),
    [
        ("anthropic/claude-haiku-4.5", "standard"),
        ("openai/gpt-4.1-mini", "default"),
    ],
)
def test_settle_succeeds_for_each_providers_ordinary_tier(model: str, reported_tier: str) -> None:
    """The exact shape that 502'd in production: a completed Anthropic
    generation reporting its own name for the ordinary tier."""
    client, key = _client_and_key()
    authorize = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_hash": key["hash"],
            "model": model,
            "estimated_input_tokens": 100,
            "max_output_tokens": 50,
        },
    )
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]

    settle = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth["authorization_id"],
            "actual_input_tokens": 100,
            "actual_output_tokens": 20,
            "request_id": f"gw-tier-{reported_tier}",
            "elapsed_seconds": 1.0,
            "service_tier": reported_tier,
        },
    )
    assert settle.status_code == 200, settle.text
    assert settle.json()["data"]["cost_microdollars"] > 0
