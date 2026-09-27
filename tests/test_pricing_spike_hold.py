"""A price spike holds only its own provider; every other provider still refreshes.

Every refresh from 2026-09-24 18:16Z to 2026-09-27 failed the spike gate on two
providers and published nothing for any of the rest, which nearly expired eleven
unrelated provider manifests. The refresh now keeps a spiking provider at its
last published prices and publishes everything else; the workflow's gate still
fails the run on any spike the refresh could not attribute to a provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.check_price_spike import route_provider, spiking_providers
from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult


def _model(model_id: str, slug: str, prompt: str, completion: str) -> dict[str, Any]:
    pricing = {"prompt": prompt, "completion": completion}
    return {
        "id": model_id,
        "pricing": dict(pricing),
        "endpoints": [{"tr_provider_slug": slug, "model_id": model_id, "pricing": dict(pricing)}],
    }


# Refresh results are keyed by module name; routes name the public provider
# identity, e.g. module io_net publishes routes as "io-net".
@pytest.fixture(params=[("acme", "acme"), ("acme_labs", "acme-labs")], ids=["same-name", "alias"])
def provider(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    key, name = request.param
    if key != name:
        monkeypatch.setitem(refresh._PRICING_RESULT_PROVIDER_ALIASES, key, (name,))
    return key, name


@pytest.fixture
def published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: tuple[str, str]
) -> dict[str, Any]:
    key, name = provider
    snapshot = {
        "models": [
            _model("acme/model", name, "0.000001", "0.000002"),
            _model("x-ai/grok-next", "grok", "0.000003", "0.000004"),
        ]
    }
    snapshot_path = tmp_path / "data" / "openrouter_snapshot.json"
    manifest_dir = snapshot_path.parent / "provider_models"
    manifest_dir.mkdir(parents=True)
    snapshot_path.write_text(json.dumps(snapshot))
    # The gate refuses an empty manifest directory; production always has many.
    (manifest_dir / "grok.json").write_text(
        json.dumps(
            {
                "provider": "grok",
                "price_scale": "microdollars_per_million",
                "models": [
                    {
                        "id": "x-ai/grok-next",
                        "input_token_price_per_m": 3_000_000,
                        "output_token_price_per_m": 4_000_000,
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot_path)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", (key, "grok"))
    monkeypatch.setattr(refresh, "MAX_TOLERATED_FAILURES", 0)
    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: SimpleNamespace())
    monkeypatch.setattr(refresh, "_read_existing_snapshot", lambda: json.loads(json.dumps(snapshot)))
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda _requirements: None)
    monkeypatch.setattr(
        refresh,
        "build_openrouter_snapshot",
        lambda: {
            "models": [
                {
                    "id": row["id"],
                    "endpoints": [
                        {"tr_provider_slug": ep["tr_provider_slug"], "model_id": ep["model_id"]}
                        for ep in row["endpoints"]
                    ],
                }
                for row in snapshot["models"]
            ]
        },
    )
    return snapshot


def _fetched(
    monkeypatch: pytest.MonkeyPatch, key: str, acme: ModelPrice
) -> dict[str, ProviderPricingResult]:
    results = {
        key: ProviderPricingResult(slug=key, source="api", prices={"acme/model": acme}),
        "grok": ProviderPricingResult(
            slug="grok", source="api", prices={"x-ai/grok-next": ModelPrice(3_500_000, 4_000_000)}
        ),
    }
    monkeypatch.setattr(refresh, "_fetch_all_providers", lambda: (results, []))
    return results


def _endpoint_prices() -> dict[str, tuple[str, str]]:
    return {
        f"{row['id']} [{ep['tr_provider_slug']}]": (ep["pricing"]["prompt"], ep["pricing"]["completion"])
        for row in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]
        for ep in row["endpoints"]
    }


def test_a_spiking_provider_keeps_its_published_prices_while_others_refresh(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key, name = provider
    results = _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))  # prompt tripled

    assert refresh.main([]) == 0

    prices = _endpoint_prices()
    assert prices[f"acme/model [{name}]"] == ("0.000001", "0.000002")
    assert prices["x-ai/grok-next [grok]"] == ("0.0000035", "0.000004")
    assert results[key].source == "stale_snapshot"
    out = capsys.readouterr().out
    assert (
        f"{refresh.HELD_FOR_REVIEW_HEADING}\n  {key}:\n    acme/model [{name}::acme/model]\n"
        in out
    )
    assert f"::warning title=Provider prices held for review::{key} kept" in out


def test_without_a_spike_nothing_is_held(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key, name = provider
    results = _fetched(monkeypatch, key, ModelPrice(1_500_000, 2_000_000))

    assert refresh.main([]) == 0

    assert _endpoint_prices()[f"acme/model [{name}]"] == ("0.0000015", "0.000002")
    assert results[key].source == "api"
    assert refresh.HELD_FOR_REVIEW_HEADING not in capsys.readouterr().out


def test_the_spike_is_real_without_the_hold(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Positive control: with holding disabled the tripled price reaches the
    # snapshot, which is exactly what the workflow gate then refuses.
    key, name = provider
    monkeypatch.setattr(refresh, "_spiking_results", lambda *_args: {})
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))
    before = tmp_path / "before.json"
    before.write_text(json.dumps(published))

    assert refresh.main([]) == 0

    assert _endpoint_prices()[f"acme/model [{name}]"] == ("0.000003", "0.000002")
    assert set(spiking_providers(before, refresh.SNAPSHOT_PATH)) == {name}


def test_every_published_route_provider_maps_to_one_refresh_result() -> None:
    # A route whose provider name did not map would never be held, and the
    # whole refresh would freeze again on its spike.
    snapshot = json.loads(refresh.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    names = {
        endpoint["tr_provider_slug"]
        for model in snapshot["models"]
        for endpoint in model.get("endpoints") or []
        if isinstance(endpoint, dict) and isinstance(endpoint.get("tr_provider_slug"), str)
    }
    names |= {path.stem for path in refresh.PROVIDER_MANIFEST_DIR.glob("*.json")}
    unmapped = sorted(
        name for name in names if refresh._result_slug_for_provider(name) not in refresh.PROVIDER_SLUGS
    )
    assert unmapped == []
    assert route_provider("z-ai/glm-5.3 [io-net:io-net:zai-org/GLM-5.3] cached-input") == "io-net"
