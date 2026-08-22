from __future__ import annotations

from pathlib import Path

from scripts.pricing import refresh
from scripts.pricing.providers import relace, stepfun
from trusted_router.catalog_data import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    PROVIDER_JURISDICTION_CN,
    PROVIDER_JURISDICTION_UNVERIFIED,
    PROVIDERS,
)
from trusted_router.catalog_ingest import _supplemental_provider_models_and_endpoints

RELACE_QUICKSTART = """
| Model | Model ID | Context | Input | Output | Cached Input |
| --- | --- | --- | --- | --- | --- |
| DeepSeek V4 Flash 0731 | `deepseek-ai/DeepSeek-V4-Flash-0731` | 1M | \\$0.14 / M | \\$0.28 / M | \\$0.028 / M |
| Kimi K3 | `moonshotai/kimi-k3` | 1M | $3.00 / M | $15.00 / M | $0.30 / M |
| Future Model | `future/model` | 256K | $1.00 / M | $2.00 / M | $0.10 / M |
"""

STEPFUN_PRICING = """
<table>
  <tr><th>Model</th><th>Billing Unit</th><th>Input (Cache Miss)</th><th>Input (Cache Hit)</th><th>Output Price</th></tr>
  <tr><td>step-3.7-flash</td><td>1M tokens</td><td>$0.20</td><td>$0.04</td><td>$1.15</td></tr>
  <tr><td>step-3.5-flash-2603</td><td>1M tokens</td><td>$0.10</td><td>$0.02</td><td>$0.30</td></tr>
  <tr><td>step-3.5-flash</td><td>1M tokens</td><td>$0.10</td><td>$0.02</td><td>$0.30</td></tr>
  <tr><td>stepaudio-2.5-chat</td><td>1M tokens</td><td>$1.50</td><td>$0.30</td><td>$3.50</td></tr>
</table>
"""


def test_stepfun_catalog_intersects_live_availability_with_known_prices() -> None:
    prices = stepfun._parse_pricing(STEPFUN_PRICING)
    rows = stepfun._parse_catalog(
        {
            "data": [
                {"id": "step-3.5-flash"},
                {"id": "step-3.7-flash"},
                {"id": "step-tts-mini"},
            ]
        },
        prices,
    )
    assert set(rows) == {
        "stepfun/step-3.5-flash",
        "stepfun/step-3.7-flash",
    }
    assert rows["stepfun/step-3.7-flash"]["upstream_id"] == "step-3.7-flash"
    assert rows["stepfun/step-3.7-flash"]["context_length"] == 262_144
    assert prices["stepfun/step-3.7-flash"].prompt_micro_per_m == 200_000
    assert prices["stepfun/step-3.7-flash"].tiers[0].prompt_cached_micro_per_m == 40_000
    assert prices["stepfun/step-3.7-flash"].completion_micro_per_m == 1_150_000


def test_relace_pricing_parser_is_allowlisted_and_exact() -> None:
    prices, rows = relace._parse_quickstart(RELACE_QUICKSTART)
    assert set(rows) == {
        "deepseek/deepseek-v4-flash-0731",
        "moonshotai/kimi-k3",
    }
    deepseek = prices["deepseek/deepseek-v4-flash-0731"]
    assert deepseek.prompt_micro_per_m == 140_000
    assert deepseek.completion_micro_per_m == 280_000
    assert deepseek.tiers[0].prompt_cached_micro_per_m == 28_000
    assert rows["moonshotai/kimi-k3"]["context_length"] == 1_000_000


def test_stepfun_and_relace_are_fully_wired_provider_direct_routes() -> None:
    assert {"stepfun", "relace"} <= set(refresh.PROVIDER_SLUGS)
    assert {"stepfun", "relace"} <= GATEWAY_PREPAID_PROVIDER_SLUGS
    assert PROVIDERS["stepfun"].provider_headquarters_country == PROVIDER_JURISDICTION_CN
    assert "relace" in PROVIDER_JURISDICTION_UNVERIFIED


def test_stepfun_and_relace_committed_manifests_are_ingested() -> None:
    models, endpoints = _supplemental_provider_models_and_endpoints()
    for model_id, provider in (
        ("stepfun/step-3.7-flash", "stepfun"),
        ("deepseek/deepseek-v4-flash-0731", "relace"),
        ("moonshotai/kimi-k3", "relace"),
    ):
        assert model_id in models
        assert f"{model_id}@{provider}/prepaid" in endpoints


def test_stepfun_and_relace_refresh_credentials_are_narrowly_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text()
    secrets = (root / "scripts/deploy/secrets.sh").read_text()
    expected = {
        "STEPFUN_API_KEY": "trustedrouter-stepfun-api-key",
        "RELACE_API_KEY": "trustedrouter-relace-api-key",
    }
    for env_name, secret_name in expected.items():
        assert f"{env_name}:{secret_name}" in workflow
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets
