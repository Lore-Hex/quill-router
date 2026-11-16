"""A control-plane catch-up must not advertise an older gateway's new routes."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.pinned_manifests import CONFIDENTIAL_MEDIA_ROUTES, USE_PINNED_MANIFESTS, pinned_manifests
from trusted_router import catalog_ingest
from trusted_router.catalog_deployment import unavailable_gateway_routes
from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HELD_AT_FIELD,
    UNSERVED_ROUTE_HOLD_REASON,
)


@pytest.mark.parametrize(("primary", "serving", "environment", "allowed"), [
    ("us-central1", "", "production", True),
    ("us-central1", "europe-west4", "production", True),
    ("us-central1", "southamerica-east1", "production", True),
    ("eu-west-3", "", "production", False),
    ("australiaeast", "", "production", False),
    ("us-central1", "aws-eu-west-3", "production", False),
    ("us-central1", "azure-australiaeast", "production", False),
    ("us-central1", "unknown", "production", False),
    ("", "", "production", False),
    ("", "", "local", True),
    ("", "", "test", True),
])
def test_media_cloud_hold_fails_closed(
    primary: str, serving: str, environment: str, allowed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_PRIMARY_REGION", primary)
    monkeypatch.setenv("TR_SERVING_REGION", serving)
    monkeypatch.setenv("TR_ENVIRONMENT", environment)
    assert len(unavailable_gateway_routes()) == (0 if allowed else 6)


def _held_upstream(directory: Path) -> Path:
    """Today's manifests with every pinned media route held the way the hourly
    refresh holds a listed route its host refuses (provider-route-unserved)."""
    held = [
        (provider, {
            **row,
            "routable": False,
            "routable_reason": UNSERVED_ROUTE_HOLD_REASON,
            UNSERVED_ROUTE_HELD_AT_FIELD: "2026-11-16T12:02:23Z",
        })
        for provider, row in CONFIDENTIAL_MEDIA_ROUTES
    ]
    return Path(pinned_manifests(directory, held)["TR_TEST_PROVIDER_MODELS_DIR"])


@pytest.mark.parametrize("committed", ["as-listed", "held-upstream"])
@pytest.mark.parametrize("region", [
    "us-central1", "europe-west4", "aws-eu-west-3", "azure-australiaeast", "unknown",
])
def test_media_discovery_and_authorization_follow_gateway_release(
    region: str, committed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gateway release rule runs on pinned routes, whatever today's
    # manifests say: the refresh that rebuilds them runs this suite before it
    # publishes, and a host refusing one of these models must not block it.
    if committed == "held-upstream":
        monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", _held_upstream(tmp_path / "committed"))
    environ = pinned_manifests(tmp_path, CONFIDENTIAL_MEDIA_ROUTES)
    result = subprocess.run(  # noqa: S603 - fixed local regression script
        [sys.executable, "-c", USE_PINNED_MANIFESTS + textwrap.dedent('''
            import asyncio
            import os
            import httpx
            from tests.test_decide_models import _authorize, _assert_no_financial_side_effects
            from trusted_router.catalog import MODELS, MODEL_ENDPOINTS, endpoints_for_model
            from trusted_router.config import Settings
            from trusted_router.main import create_app

            allowed = os.environ["TR_SERVING_REGION"] in {"us-central1", "europe-west4"}
            routes = {
                ("deepseek/deepseek-ocr-2", "privatemode"),
                ("nomic-ai/nomic-embed-text", "tinfoil"),
                ("qwen/qwen3-embedding-4b", "privatemode"),
                ("openai/whisper-large-v3", "privatemode"),
                ("mistralai/voxtral-mini-3b-2507", "privatemode"),
                ("mistralai/voxtral-small-24b", "tinfoil"),
            }
            for model, provider in routes:
                assert any(e.model_id == model and e.provider == provider
                           for e in MODEL_ENDPOINTS.values()) is allowed, (model, provider)
            # A hold is provider-specific: Novita OCR and existing Tinfoil audio stay up.
            assert any(e.provider == "novita" for e in endpoints_for_model("deepseek/deepseek-ocr-2"))
            assert any(e.provider == "tinfoil" for e in endpoints_for_model("openai/whisper-large-v3-turbo"))

            async def verify():
                app = create_app(Settings(environment="test"), init_observability=False)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                ) as client:
                    response = await client.get("/v1/models")
                assert response.status_code == 200, response.text
                ids = {row["id"] for row in response.json()["data"]}
                for model, provider in routes:
                    # A model another host serves stays listed (Novita's OCR).
                    elsewhere = any(e.provider != provider for e in endpoints_for_model(model))
                    assert (model in ids) is (allowed or elsewhere), model
                assert "deepseek/deepseek-ocr-2" in ids
                response = await _authorize({
                    "model": "openai/whisper-large-v3", "route_type": "audio.transcriptions",
                    "estimated_input_tokens": 0, "max_output_tokens": 1,
                    "transcription_duration_ms": 1250,
                    "idempotency_key": "cloud-admission-audio", "request_fingerprint": "c" * 64,
                    "provider": {"only": ["privatemode"], "min_privacy": "confidential"},
                })
                assert response.status_code == (200 if allowed else 400), response.text
                if not allowed:
                    _assert_no_financial_side_effects()
            asyncio.run(verify())
        ''')],
        env={**environ, "TR_PRIMARY_REGION": "us-central1", "TR_SERVING_REGION": region,
             "TR_ENVIRONMENT": "test", "TR_STORAGE_BACKEND": "memory"},
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
