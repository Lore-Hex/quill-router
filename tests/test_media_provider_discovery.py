from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.manifest import guard_fixed_output_prices
from scripts.pricing.providers import bfl, decart, fal, krea, recraft
from trusted_router.catalog_data import GATEWAY_PREPAID_PROVIDER_SLUGS
from trusted_router.catalog_ingest import _supplemental_provider_models_and_endpoints
from trusted_router.image_generation import (
    FIXED_IMAGE_PRICES_MICRODOLLARS,
    IMAGE_MODEL_ID_SET,
)

MANIFEST_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "trusted_router" / "data" / "provider_models"
)


def _manifest_prices(provider: str) -> dict[str, dict[str, int]]:
    payload = json.loads((MANIFEST_DIR / f"{provider}.json").read_text())
    return {
        row["id"]: row["fixed_output_price_microdollars"]
        for row in payload["models"]
        if "fixed_output_price_microdollars" in row
    }


def _manifest_video_prices(provider: str) -> dict[str, int]:
    payload = json.loads((MANIFEST_DIR / f"{provider}.json").read_text())
    return {
        row["id"]: row["fixed_output_price_per_second_microdollars"]
        for row in payload["models"]
        if "fixed_output_price_per_second_microdollars" in row
    }


def test_recraft_pricing_parser_matches_exact_raster_rows() -> None:
    html = """
    <table>
      <tr><td>Raster image generation:<br>Recraft V4.1 Pro<br>Recraft V4.1 Utility Pro</td><td>$0.21</td></tr>
      <tr><td>Raster image generation:<br>Recraft V4.1<br>Recraft V4.1 Utility</td><td>$0.035</td></tr>
      <tr><td>Recraft V4 Vector image generation</td><td>$0.08</td></tr>
    </table>
    """
    assert recraft._parse_pricing(html) == {
        "recraft/recraftv4_1_pro": 210_000,
        "recraft/recraftv4_1_utility_pro": 210_000,
        "recraft/recraftv4_1": 35_000,
        "recraft/recraftv4_1_utility": 35_000,
    }


def test_bfl_pricing_parser_ignores_unrecognized_models() -> None:
    html = """
    <table>
      <tr><td>FLUX.2 [klein] 4B</td><td>$0.014</td></tr>
      <tr><td>FLUX.2 [max]</td><td>$0.07</td></tr>
      <tr><td>Experimental</td><td>$0.001</td></tr>
    </table>
    """
    assert bfl._parse_pricing(html) == {
        "black-forest-labs/flux-2-klein-4b": 14_000,
        "black-forest-labs/flux-2-max": 70_000,
    }


def test_decart_pricing_parser_maps_native_resolutions() -> None:
    html = """
    <table>
      <tr><th>Model</th><th>ID</th><th>720p</th><th>Best for</th></tr>
      <tr><td>Lucy 2.5</td><td>lucy-2.5</td><td>$0.02/sec</td><td>Realtime</td></tr>
    </table>
    <table>
      <tr><th>Model</th><th>ID</th><th>480p</th><th>720p</th><th>Best for</th></tr>
      <tr><td>Lucy 2.5</td><td>lucy-2.5</td><td>-</td><td>$0.04/sec</td><td>Video</td></tr>
      <tr><td>Lucy VTON 3.5</td><td>lucy-vton-3.5</td><td>-</td><td>$0.04/sec</td><td>Video</td></tr>
      <tr><td>Lucy Restyle 2</td><td>lucy-restyle-2</td><td>-</td><td>$0.01/sec</td><td>Video</td></tr>
      <tr><td>Lucy Image 2</td><td>lucy-image-2</td><td>$0.01</td><td>$0.02</td><td>Image</td></tr>
    </table>
    """
    assert decart._parse_pricing(html) == {
        "decart/lucy-image-2": {"480p": 10_000, "720p": 20_000},
        "decart/lucy-2.5": 40_000,
        "decart/lucy-vton-3.5": 40_000,
        "decart/lucy-restyle-2": 10_000,
    }


def test_krea_pricing_parser_uses_exact_text_to_image_price() -> None:
    openapi = {
        "paths": {
            "/generate/image/krea/krea-2/medium": {
                "post": {
                    "x-krea-pricing": {
                        "type": "fixed",
                        "currency": "USD",
                        "price_points": [
                            {
                                "amount": "0.03",
                                "dimensions": {"k2BillingTier": "text-to-image"},
                            },
                            {
                                "amount": "0.05",
                                "dimensions": {"k2BillingTier": "image-to-image"},
                            },
                        ],
                    }
                }
            }
        }
    }
    assert krea._fixed_text_to_image_price(openapi) == 30_000


def test_fal_h3_max_parser_uses_post_promotion_prices() -> None:
    page = """
    Video costs $0.0125 per second at 480p and $0.02 per second at 768p.
    The discount ends September 7, after which 480p is $0.05/second and
    768p is $0.08/second.
    """
    assert fal._h3_max_standard_rates(page) == {"480p": 50_000, "768p": 80_000}


def test_fal_h3_max_parser_uses_standard_display_after_promotion() -> None:
    page = """
    <p>Video costs <strong>$0.05</strong> per second at <strong>480p</strong>,
    <strong>$0.08</strong> per second at <strong>768p</strong>.</p>
    """
    assert fal._h3_max_standard_rates(page) == {"480p": 50_000, "768p": 80_000}


def test_fal_h3_max_parser_never_treats_promotion_as_standard_price() -> None:
    page = """
    Video costs $0.0125 per second at 480p and $0.02 per second at 768p.
    These are promotional launch rates for a limited time.
    """
    try:
        fal._h3_max_standard_rates(page)
    except RuntimeError as exc:
        assert "missing or ambiguous" in str(exc)
    else:
        raise AssertionError("temporary fal promotion must not become the standard rate")


def test_fal_h3_max_parser_rejects_ambiguous_standard_prices() -> None:
    page = """
    after which 480p is $0.05/second and 768p is $0.08/second
    after which 480p is $0.06/second and 768p is $0.09/second
    """
    try:
        fal._h3_max_standard_rates(page)
    except RuntimeError as exc:
        assert "missing or ambiguous" in str(exc)
    else:
        raise AssertionError("ambiguous fal prices must fail closed")


def test_media_manifests_match_runtime_fixed_price_contract() -> None:
    discovered: dict[str, dict[str, int]] = {}
    for provider in ("recraft", "bfl", "decart", "nscale", "krea", "fal"):
        discovered.update(_manifest_prices(provider))
    assert discovered == FIXED_IMAGE_PRICES_MICRODOLLARS
    assert _manifest_video_prices("decart") == {
        "decart/lucy-2.5": 40_000,
        "decart/lucy-vton-3.5": 40_000,
        "decart/lucy-restyle-2": 10_000,
    }
    assert _manifest_video_prices("fal") == {"minimax/h3-max": 80_000}


def test_media_providers_are_refreshable_prepaid_gateway_routes() -> None:
    expected = {"recraft", "bfl", "decart", "nscale", "krea", "fal"}
    assert expected <= set(refresh.PROVIDER_SLUGS)
    assert expected <= GATEWAY_PREPAID_PROVIDER_SLUGS

    models, endpoints = _supplemental_provider_models_and_endpoints()
    for model_id, provider in (
        ("recraft/recraftv4_1", "recraft"),
        ("black-forest-labs/flux-2-klein-4b", "bfl"),
        ("decart/lucy-image-2", "decart"),
        (fal.MODEL_ID, fal.SLUG),
    ):
        assert model_id in models
        assert f"{model_id}@{provider}/prepaid" in endpoints

    # Video routes are installed from the audited enclave registry, not the
    # generic chat/image manifest ingester.
    assert "minimax/h3-max" not in models
    assert "minimax/h3-max@fal/prepaid" not in endpoints

    nscale_model = "black-forest-labs/flux.1-schnell"
    assert nscale_model in IMAGE_MODEL_ID_SET
    nscale_manifest = json.loads((MANIFEST_DIR / "nscale.json").read_text())
    nscale_image = next(row for row in nscale_manifest["models"] if row["id"] == nscale_model)
    if nscale_image.get("routable") is False:
        assert nscale_model not in models
        assert f"{nscale_model}@nscale/prepaid" not in endpoints
    else:
        assert nscale_model in models
        assert f"{nscale_model}@nscale/prepaid" in endpoints

    krea_model = "krea/krea-2-medium"
    assert krea_model in IMAGE_MODEL_ID_SET
    krea_manifest = json.loads((MANIFEST_DIR / "krea.json").read_text())
    krea_image = next(row for row in krea_manifest["models"] if row["id"] == krea_model)
    if krea_image.get("routable") is False:
        assert krea_model not in models
        assert f"{krea_model}@krea/prepaid" not in endpoints
    else:
        assert krea_model in models
        assert f"{krea_model}@krea/prepaid" in endpoints


def test_fixed_media_price_change_fails_before_manifest_write(tmp_path: Path) -> None:
    manifest = tmp_path / "media.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "media/model",
                        "fixed_output_price_microdollars": {"1k": 10_000},
                    }
                ]
            }
        )
    )
    discovered = {"media/model": {"fixed_output_price_microdollars": {"1k": 20_000}}}
    try:
        guard_fixed_output_prices(manifest, discovered)
    except RuntimeError as exc:
        assert "review and deploy billing contract first" in str(exc)
    else:
        raise AssertionError("fixed price drift must stop the provider refresh")
    assert json.loads(manifest.read_text())["models"][0]["fixed_output_price_microdollars"] == {
        "1k": 10_000
    }


def test_media_and_discovery_results_do_not_enter_token_price_index() -> None:
    result = ProviderPricingResult(
        slug="media",
        prices={"media/model": ModelPrice(0, 0)},
        source="api",
        include_in_price_index=False,
    )
    assert refresh._index_provider_prices({"media": result}) == {}


def test_mixed_provider_indexes_only_explicit_chat_models() -> None:
    result = ProviderPricingResult(
        slug="mixed",
        prices={
            "vendor/chat": ModelPrice(10, 20),
            "vendor/embedding": ModelPrice(5, 0),
            "vendor/image": ModelPrice(0, 0),
        },
        source="api",
        price_index_model_ids=frozenset({"vendor/chat"}),
    )
    assert refresh._index_provider_prices({"mixed": result}) == {
        "vendor/chat": {"mixed": ModelPrice(10, 20)}
    }


def test_failed_new_provider_recovers_from_committed_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "provider.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "vendor/model",
                        "routable": True,
                        "input_token_price_per_m": 120_000,
                        "cached_input_token_price_per_m": 30_000,
                        "output_token_price_per_m": 480_000,
                    }
                ]
            }
        )
    )
    module = SimpleNamespace(
        MANIFEST_STALE_FALLBACK=True,
        MANIFEST_PATH=manifest,
        INCLUDE_IN_PRICE_INDEX=True,
    )
    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: module)
    results: dict[str, ProviderPricingResult] = {}
    failures = refresh._apply_stale_fallbacks(
        results,
        [("provider", "temporary outage")],
        {"models": []},
    )
    assert failures == []
    recovered = results["provider"]
    assert recovered.source == "stale_manifest"
    assert recovered.prices["vendor/model"] == ModelPrice(
        120_000,
        480_000,
        prompt_cached_micro_per_m=30_000,
    )


def test_media_and_nvidia_refresh_credentials_are_narrowly_wired() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/refresh-prices.yml").read_text()
    secrets = (root / "scripts/deploy/secrets.sh").read_text()
    expected = {
        "RECRAFT_API_KEY": "trustedrouter-recraft-api-key",
        "BFL_API_KEY": "trustedrouter-bfl-api-key",
        "DECART_API_KEY": "trustedrouter-decart-api-key",
        "KREA_API_KEY": "trustedrouter-krea-api-key",
        "FAL_API_KEY": "trustedrouter-fal-api-key",
        "NVIDIA_NIM_API_KEY": "trustedrouter-nvidia-nim-api-key",
    }
    for env_name, secret_name in expected.items():
        assert f"{env_name}:{secret_name}" in workflow
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets
