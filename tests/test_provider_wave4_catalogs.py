from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.pricing import refresh
from scripts.pricing.providers import baidu, darkbloom, perceptron, riverflow, vultr
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)
from trusted_router.provider_manifest_policy import EXPIRING_PROVIDER_MANIFEST_SLUGS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "src/trusted_router/data/provider_models"
ACTIVE = {"vultr", "darkbloom", "baidu", "riverflow"}


def _manifest(slug: str) -> dict[str, object]:
    return json.loads((MANIFEST_DIR / f"{slug}.json").read_text(encoding="utf-8"))


def test_wave4_routes_are_priced_canaried_and_privacy_conservative() -> None:
    endpoint_providers = {endpoint.provider for endpoint in MODEL_ENDPOINTS.values()}
    assert ACTIVE <= set(refresh.PROVIDER_SLUGS)
    assert ACTIVE <= GATEWAY_PREPAID_PROVIDER_SLUGS
    assert ACTIVE <= EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert ACTIVE <= endpoint_providers

    for slug in ACTIVE:
        provider = PROVIDERS[slug]
        assert provider.supports_prepaid is True
        assert provider.supports_byok is False
        assert provider.stores_content is True
        assert provider.provider_zero_data_retention is not True
        assert provider.provider_confidential_compute is not True
        assert provider.provider_e2ee is not True

        rows = _manifest(slug)["models"]
        assert rows
        assert any(row.get("routable") is not False for row in rows)
        for row in rows:
            if row.get("routable") is False:
                assert row.get("routable_reason") == "provider-canary-failed"
                continue
            if row.get("model_type") == "image":
                assert row["fixed_output_price_microdollars"]
            else:
                assert row["input_token_price_per_m"] > 0
                assert row["output_token_price_per_m"] > 0


def test_provider_native_ids_are_preserved_for_gateway_calls() -> None:
    assert vultr.UPSTREAM_ID_MAP["z-ai/glm-5.2"] == "zai-org/GLM-5.2-FP8"
    assert darkbloom.UPSTREAM_ID_MAP["google/gemma-4-26b"] == "gemma-4-26b"
    assert baidu.UPSTREAM_ID_MAP["deepseek/deepseek-v4-flash-0731"] == ("deepseek-v4-flash-0731")


def test_riverflow_fixed_price_is_exact_microdollars() -> None:
    assert riverflow._microdollars("0.02") == 20_000
    assert riverflow._microdollars("0.0200001") is None
    row = _manifest("riverflow")["models"][0]
    assert row["id"] == riverflow.MODEL_ID
    assert row["fixed_output_price_microdollars"] == {"1k": 20_000}


def test_riverflow_failed_paid_canary_retries_at_most_daily(tmp_path: Path) -> None:
    now = datetime(2026, 8, 28, 20, tzinfo=UTC)
    manifest = tmp_path / "riverflow.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": riverflow.MODEL_ID,
                        "routable": False,
                        "routable_reason": "provider-canary-failed",
                        "canary_checked_at": (now - timedelta(hours=23)).isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert not riverflow._models_requiring_paid_canary(manifest, {riverflow.MODEL_ID}, now=now)
    assert riverflow._models_requiring_paid_canary(
        manifest, {riverflow.MODEL_ID}, now=now + timedelta(hours=1)
    ) == frozenset({riverflow.MODEL_ID})


def test_perceptron_inc_stays_dark_without_exact_prices() -> None:
    assert perceptron.BASE_URL == "https://api.perceptron.inc"
    assert perceptron.URL == "https://api.perceptron.inc/v1/models"
    assert (
        perceptron._model_ids(
            {"data": [{"id": model_id} for model_id in perceptron.EXPECTED_MODEL_IDS]}
        )
        == perceptron.EXPECTED_MODEL_IDS
    )
    assert "perceptron" not in refresh.PROVIDER_SLUGS
    assert "perceptron" not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert not any(endpoint.provider == "perceptron" for endpoint in MODEL_ENDPOINTS.values())
    assert PROVIDERS["perceptron"].supports_chat is False
    assert PROVIDERS["perceptron"].supports_prepaid is False


def test_wave4_refresh_secrets_are_scoped_and_fal_stays_dark() -> None:
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    for env_name, secret_name in {
        "VULTR_API_KEY": "trustedrouter-vultr-api-key",
        "DARKBLOOM_API_KEY": "trustedrouter-darkbloom-api-key",
        "BAIDU_API_KEY": "trustedrouter-baidu-api-key",
        "RIVERFLOW_API_KEY": "trustedrouter-riverflow-api-key",
    }.items():
        assert f"{env_name}:{secret_name}" in workflow
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets

    assert 'ensure_secret_from_env_file "FAL_API_KEY" "trustedrouter-fal-api-key"' in secrets
    assert "FAL_API_KEY:trustedrouter-fal-api-key" not in workflow
    assert 'grant_tr_deploy_secret_access "trustedrouter-fal-api-key"' not in secrets


def test_tencent_root_credential_is_never_wired_into_prompt_runtime() -> None:
    provider = PROVIDERS["tencent-cloud"]
    assert provider.supports_prepaid is False
    assert provider.supports_byok is False
    assert provider.provider_headquarters_country == "CN"
    assert "TENCENT_SECRET_KEY" not in (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
