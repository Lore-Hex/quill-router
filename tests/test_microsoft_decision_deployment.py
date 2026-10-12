"""Secondary control-plane promotion must not outrun its gateway protocol."""

import os
import subprocess
import sys
import textwrap

import pytest

from trusted_router.catalog_deployment import unavailable_gateway_model_ids

MODEL = "microsoft/microsoft-decision-1"


@pytest.mark.parametrize(("primary", "serving", "environment", "allowed"), [
    ("us-central1", "", "production", True),
    ("us-central1", "southamerica-east1", "production", True),
    ("us-central1", "europe-west4", "production", True),
    ("eu-west-3", "", "production", False),
    ("australiaeast", "", "production", False),
    ("us-central1", "aws-eu-west-3", "production", False),
    ("us-central1", "azure-australiaeast", "production", False),
    ("us-central1", "unknown", "production", False),
    ("", "", "production", False),
    ("", "", "local", True),
    ("", "", "test", True),
])
def test_only_upgraded_gateway_cloud_is_admitted(
    primary: str, serving: str, environment: str, allowed: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_PRIMARY_REGION", primary)
    monkeypatch.setenv("TR_SERVING_REGION", serving)
    monkeypatch.setenv("TR_ENVIRONMENT", environment)
    assert unavailable_gateway_model_ids() == (frozenset() if allowed else frozenset({MODEL}))


@pytest.mark.parametrize("region", ["us-central1", "eu-west-3", "australiaeast"])
def test_fresh_cloud_catalog_discovery_and_authorization(region: str) -> None:
    # A fresh interpreter exercises import-time admission without mutating the
    # global catalog or STORE used by other tests in this worker.
    result = subprocess.run(  # noqa: S603 - fixed local regression script
        [sys.executable, "-c", textwrap.dedent('''
            import asyncio
            import os
            import httpx
            from tests.test_decide_models import _authorize
            from tests.lifecycle_clock import CATALOG_CLOCK
            from trusted_router.catalog import MODELS, MODEL_ENDPOINTS
            from trusted_router.config import Settings
            from trusted_router.main import create_app
            from trusted_router.provider_lifecycle import provider_model_retired

            model = "microsoft/microsoft-decision-1"
            allowed = os.environ["TR_PRIMARY_REGION"] == "us-central1"
            assert (model in MODELS) is allowed
            available = allowed and not provider_model_retired("azure", model, at=CATALOG_CLOCK)
            assert any(e.model_id == model for e in MODEL_ENDPOINTS.values()) is available
            assert "typesafe-ai/jev" in MODELS

            async def verify():
                response = await _authorize({
                    "model": model, "route_type": "decide",
                    "estimated_input_tokens": 400, "max_output_tokens": 3,
                })
                assert response.status_code == (200 if available else 400), response.text
                app = create_app(Settings(environment="test"), init_observability=False)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                ) as client:
                    response = await client.get("/v1/models")
                assert response.status_code == 200, response.text
                ids = {row["id"] for row in response.json()["data"]}
                # Retirement disables routes but preserves the seed's public
                # metadata. A gateway hold removes the entire model instead.
                assert (model in ids) is allowed
            asyncio.run(verify())
        ''')],
        env={**os.environ, "TR_PRIMARY_REGION": region, "TR_SERVING_REGION": "", "TR_ENVIRONMENT": "test"},
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
