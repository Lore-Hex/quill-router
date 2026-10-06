"""A price spike holds only its own provider; every other provider still refreshes.

Every refresh from 2026-09-24 18:16Z to 2026-09-27 failed the spike gate on two
providers and published nothing for any of the rest, which nearly expired eleven
unrelated provider manifests. The refresh now keeps a spiking provider at its
last published prices and publishes everything else; the workflow's gate still
fails the run on any spike the refresh could not attribute to a provider.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.check_price_spike import route_provider, spiking_providers
from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import tencent


@pytest.mark.parametrize("feed_change", ["unchanged", "synthetic", "tag", "missing-model"])
def test_committed_tencent_spike_keeps_published_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    feed_change: str,
) -> None:
    # Reproduce against committed artifacts, never rewrite the real data.
    snapshot = tmp_path / refresh.SNAPSHOT_PATH.name
    manifests = tmp_path / refresh.PROVIDER_MANIFEST_DIR.name
    parsers = tmp_path / refresh.PARSERS_DIR.name
    shutil.copyfile(refresh.SNAPSHOT_PATH, snapshot)
    shutil.copytree(refresh.PROVIDER_MANIFEST_DIR, manifests)
    shutil.copytree(refresh.PARSERS_DIR, parsers)
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(tencent, "MANIFEST_PATH", manifests / "tencent.json")
    # Discovery only remembers today's native IDs. A held manifest can recover
    # a price whose native ID is absent from that fresh discovery map.
    monkeypatch.setattr(tencent, "UPSTREAM_ID_MAP", {})
    published = json.loads(snapshot.read_text())
    model_id = "deepseek/deepseek-v4-flash-0731"
    old = next(row for row in published["models"] if row["id"] == model_id)
    endpoint = next(ep for ep in old["endpoints"] if ep["tr_provider_slug"] == "tencent")
    assert endpoint["pricing"] == {
        "prompt": "0.00000022", "completion": "0.00000066", "input_cache_read": "0.000000007",
    }
    # Both committed sources are off-peak. A synthetic 2x fresh result trips
    # the real spike detector; the stale manifest alone has identical prices.
    recovered: dict[str, ProviderPricingResult] = {}
    assert refresh._apply_stale_fallbacks(recovered, [("tencent", "spike")], published) == []
    assert refresh._price_to_pricing_block(recovered["tencent"].prices[model_id]) == endpoint["pricing"]
    healthy = _model("acme/healthy", "healthy", "0.000003", "0.000004")
    feed = json.loads(json.dumps(published))
    feed["models"].append(healthy)
    fresh_model = next(row for row in feed["models"] if row["id"] == model_id)
    if feed_change == "synthetic":
        # Without an OR endpoint or discovered native ID, _merge_snapshot
        # synthesizes model_id=deepseek/deepseek-v4-flash-0731
        # instead of the published deepseek-v4-flash-0731. The old hold then
        # drops that newly keyed route and fails exactness on the missing one.
        fresh_model["endpoints"] = [
            ep for ep in fresh_model["endpoints"] if ep["tr_provider_slug"] != "tencent"
        ]
    elif feed_change == "tag":
        # The old merger takes tag from today's OR feed, so the hold loses
        # [tencent:tencent:deepseek-v4-flash-0731] despite identical prices.
        for ep in fresh_model["endpoints"]:
            if ep["tr_provider_slug"] == "tencent":
                ep["tag"] = "tencent/new-region"
    elif feed_change == "missing-model":
        feed["models"].remove(fresh_model)

    baseline = refresh._copy_published_prices()
    disabled = refresh._published_disabled_held_routes(baseline, published, {"tencent": []})
    manifest_before = tencent.MANIFEST_PATH.read_bytes()

    def write_tencent(result: ProviderPricingResult) -> list[str]:
        raw = json.loads(manifest_before)
        row = next(row for row in raw["models"] if row["id"] == model_id)
        row["input_token_price_per_m"] = result.prices[model_id].prompt_micro_per_m
        row["output_token_price_per_m"] = result.prices[model_id].completion_micro_per_m
        tencent.MANIFEST_PATH.write_text(json.dumps(raw))
        return ["tencent refreshed"]

    monkeypatch.setattr(tencent, "write_provider_manifest", write_tencent)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", ("tencent", "healthy"))
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: tencent if slug == "tencent" else SimpleNamespace())
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda *_args: None)
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    monkeypatch.setattr(refresh, "_fetch_all_providers", lambda: ({
        "tencent": ProviderPricingResult(
            slug="tencent", source="api", prices={model_id: ModelPrice(440_000, 1_320_000, prompt_cached_micro_per_m=14_000)},
        ),
        "healthy": ProviderPricingResult(
            slug="healthy", source="api", prices={
                "acme/healthy": ModelPrice(3_500_000, 4_000_000),
                model_id: ModelPrice(300_000, 700_000),
            },
        ),
    }, []))

    if feed_change == "synthetic":
        rebuilt = refresh._merge_snapshot(feed, refresh._index_provider_prices(recovered), set())
        rebuilt_model = next(row for row in rebuilt["models"] if row["id"] == model_id)
        rebuilt_endpoint = next(ep for ep in rebuilt_model["endpoints"] if ep["tr_provider_slug"] == "tencent")
        assert rebuilt_endpoint["model_id"] == model_id
        assert endpoint["model_id"] == "deepseek-v4-flash-0731"
        assert rebuilt_endpoint["tag"] == endpoint["tag"] == "tencent"
        assert rebuilt_endpoint["pricing"] == endpoint["pricing"]

    assert refresh.main([]) == 0

    after = json.loads(snapshot.read_text())
    models = {row["id"]: row for row in after["models"]}
    expected = {
        route: prices for route, prices in refresh._held_endpoint_pricing(published, {"tencent": []}).items()
        if route not in disabled
    }
    assert refresh._held_endpoint_pricing(after, {"tencent": []}) == expected
    assert next(ep for ep in models[model_id]["endpoints"] if ep["tr_provider_slug"] == "tencent") == endpoint
    assert models["acme/healthy"]["pricing"]["prompt"] == "0.0000035"
    assert tencent.MANIFEST_PATH.read_bytes() == manifest_before
    assert "  tencent:\n" in capsys.readouterr().out


@pytest.mark.parametrize("at, multiplier", [("2026-10-02T02:00:00Z", 2), ("2026-10-04T07:56:00Z", 1)])
def test_tencent_refresh_compares_off_peak_prices_in_every_runtime_period(
    monkeypatch: pytest.MonkeyPatch, at: str, multiplier: int,
) -> None:
    from trusted_router import provider_lifecycle

    model_id = "deepseek/deepseek-v4-flash-0731"
    fixtures = Path(__file__).parent / "fixtures" / "pricing"
    html = (fixtures / "tencent_pricing_2026-09-29.html").read_text()
    monkeypatch.setattr(tencent, "fetch_html", lambda _url: html)
    monkeypatch.setattr(tencent, "_MODEL_NAMES", {model_id: "deepseek-v4-flash 0731 ga"})
    effective_at = provider_lifecycle._effective_time(at)
    monkeypatch.setattr(provider_lifecycle, "_effective_time", lambda _at: effective_at)

    runtime_price = provider_lifecycle.provider_price_microdollars("tencent", model_id)
    assert runtime_price is not None
    assert runtime_price.prompt_microdollars_per_million_tokens == 220_000 * multiplier
    # Refresh does not call the runtime override. Both the parser and the
    # published sources above use this stable baseline, even during peak hours.
    assert tencent._load_prices()[model_id] == ModelPrice(
        220_000, 660_000, prompt_cached_micro_per_m=7_000,
    )


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
    # refresh.main snapshots PARSERS_DIR and restores it on a rejected run.
    # Point it at a temp directory so parallel tests never rewrite the
    # repository's parsers.
    parsers_dir = tmp_path / "parsers"
    parsers_dir.mkdir()
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers_dir)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", (key, "grok"))
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
                        {key: ep[key] for key in ("tr_provider_slug", "model_id", "tag") if key in ep}
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
    assert key not in results
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


@pytest.mark.parametrize("restore", [True, False], ids=["restored", "restore-skipped"])
def test_a_held_manifest_only_provider_is_restored_exactly_and_is_not_a_failure(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    restore: bool,
) -> None:
    # beta has no snapshot route, so the stale fallback recovers nothing for
    # it; its routes live only in the manifest, which is restored as published.
    key, _name = provider
    manifest = refresh.PROVIDER_MANIFEST_DIR / "beta.json"
    published_manifest = json.dumps(
        {
            "provider": "beta",
            "price_scale": "microdollars_per_million",
            "models": [
                {
                    "id": "beta/model",
                    "input_token_price_per_m": 1_000_000,
                    "output_token_price_per_m": 2_000_000,
                }
            ],
        }
    )
    manifest.write_text(published_manifest)
    parsers = tmp_path / "parsers"  # created by the published fixture
    parser = parsers / "beta.py"
    parser.write_text("# published parser\n")
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "parser_path", lambda slug: parsers / f"{slug}.py")

    def spiking_hook(_result: ProviderPricingResult) -> list[str]:
        spiked = json.loads(published_manifest)
        spiked["models"][0]["input_token_price_per_m"] = 3_000_000
        manifest.write_text(json.dumps(spiked))
        parser.write_text("# self-healed parser that produced the spike\n")
        return ["beta: refreshed"]

    modules = {"beta": SimpleNamespace(MANIFEST_PATH=manifest, write_provider_manifest=spiking_hook)}
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: modules.get(slug, SimpleNamespace()))
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", (key, "grok", "beta"))
    results = _fetched(monkeypatch, key, ModelPrice(1_000_000, 2_000_000))
    results["beta"] = ProviderPricingResult(
        slug="beta", source="api", prices={"beta/model": ModelPrice(3_000_000, 2_000_000)}
    )

    if not restore:
        # The exactness guard must refuse a held manifest left as refreshed.
        monkeypatch.setattr(refresh, "_restore_published_files", lambda *_args: None)
        assert refresh.main([]) == 1
        return

    assert refresh.main([]) == 0

    assert manifest.read_text() == published_manifest
    assert parser.read_text() == "# published parser\n"
    assert _endpoint_prices()["x-ai/grok-next [grok]"] == ("0.0000035", "0.000004")
    assert "beta" not in results


def test_a_hold_publishes_when_the_published_route_carries_openrouter_only_keys(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Old endpoint metadata survives verbatim, including ignored OR keys.
    key, name = provider
    published["models"][0]["endpoints"][0]["pricing"].update(discount=0, web_search="0.01")
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))  # prompt tripled

    assert refresh.main([]) == 0

    assert _endpoint_prices()[f"acme/model [{name}]"] == ("0.000001", "0.000002")
    assert _endpoint_prices()["x-ai/grok-next [grok]"] == ("0.0000035", "0.000004")


def test_a_hold_preserves_a_price_rejected_by_stale_recovery(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The stale fallback refuses a one-sided zero price, so re-pricing cannot
    # reproduce this route. Copying the published endpoint must still work.
    key, name = provider
    published["models"][0] = _model("acme/model", name, "0.000001", "0")
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 1_000_000))

    assert refresh.main([]) == 0

    models = {row["id"]: row for row in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]}
    assert models["acme/model"] == published["models"][0]
    assert models["x-ai/grok-next"]["pricing"]["prompt"] == "0.0000035"
    assert refresh.HELD_FOR_REVIEW_HEADING in capsys.readouterr().out


def test_a_hold_can_prune_a_route_already_quarantined_in_the_published_manifest(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sept 29: io.net M2.7 was still in the snapshot but its published
    # manifest already said provider-canary-failed. Stale recovery correctly
    # omitted it, then the hold guard incorrectly blocked all providers.
    key, name = provider
    manifest = refresh.PROVIDER_MANIFEST_DIR / f"{name}.json"
    original = json.dumps({"provider": name, "models": [{
        "id": "acme/model", "routable": False,
        "routable_reason": "provider-canary-failed",
        "input_token_price_per_m": 1_000_000,
        "output_token_price_per_m": 2_000_000,
    }]})
    manifest.write_text(original)
    module = SimpleNamespace(MANIFEST_PATH=manifest, MANIFEST_STALE_FALLBACK=True)
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: module if slug == key else SimpleNamespace())
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))

    assert refresh.main([]) == 0

    assert manifest.read_text() == original
    assert _endpoint_prices() == {"x-ai/grok-next [grok]": ("0.0000035", "0.000004")}


@pytest.mark.parametrize("evidence", ["disabled", "active", "missing", "malformed", "wrong-provider", "new-hold", "retired"])
def test_hold_route_removal_requires_published_disable_or_effective_retirement(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    key, name = provider
    manifest = refresh.PROVIDER_MANIFEST_DIR / f"{name}.json"
    raw = {
        "provider": "another-provider" if evidence == "wrong-provider" else name,
        "models": [{"id": "acme/model", "routable": evidence in {"active", "retired"}}],
    }
    if evidence not in {"missing", "new-hold"}:
        manifest.write_text("invalid" if evidence == "malformed" else json.dumps(raw))
    module = SimpleNamespace(MANIFEST_PATH=manifest)
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: module if slug == key else SimpleNamespace())
    baseline = refresh._copy_published_prices()
    if evidence == "new-hold":
        manifest.write_text(json.dumps(raw))
    monkeypatch.setattr(
        refresh, "provider_model_retired",
        lambda slug, model_id, upstream_id: evidence == "retired" and slug == name and model_id == "acme/model",
    )
    published["models"] = published["models"][1:]
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))

    changed = refresh._held_routes_changed(baseline, {key: []})

    route = f"acme/model [{name}::acme/model]"
    assert (route not in changed) == (evidence in {"disabled", "retired"})
    if evidence == "new-hold":
        assert f"{name}.json (manifest)" in changed


def test_disabled_route_prices_still_cannot_change_during_a_hold(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, name = provider
    manifest = refresh.PROVIDER_MANIFEST_DIR / f"{name}.json"
    manifest.write_text(json.dumps({"provider": name, "models": [{"id": "acme/model", "routable": False}]}))
    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: SimpleNamespace(MANIFEST_PATH=manifest))
    baseline = refresh._copy_published_prices()
    published["models"][0]["endpoints"][0]["pricing"]["prompt"] = "0.000003"
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))

    assert refresh._held_routes_changed(baseline, {key: []}) == [f"acme/model [{name}::acme/model]"]


@pytest.mark.parametrize("same_tag", [False, True], ids=["distinct-tags", "same-tag"])
def test_a_hold_keeps_distinct_published_cache_prices_verbatim(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    same_tag: bool,
) -> None:
    # Two published endpoints of one provider differ only in cached input;
    # re-pricing keeps one price per provider and model, so one would change.
    key, name = provider
    model = _model("acme/model", name, "0.000001", "0.000002")
    model["endpoints"] = [
        {
            "tr_provider_slug": name,
            "model_id": "acme/model",
            "tag": tag,
            "pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": cached},
        }
        for tag, cached in (
            (f"{name}/a", "0.0000001"),
            (f"{name}/a" if same_tag else f"{name}/b", "0.00000011"),
        )
    ]
    published["models"][0] = model
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))

    assert refresh.main([]) == 0

    models = {row["id"]: row for row in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]}
    assert models["acme/model"]["endpoints"] == model["endpoints"]
    assert len(models["acme/model"]["endpoints"]) == 2
    assert models["x-ai/grok-next"]["pricing"]["prompt"] == "0.0000035"
    assert refresh.HELD_FOR_REVIEW_HEADING in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["cache", "tiers", "duplicate", "missing", "new"])
def test_inexact_spike_hold_restores_only_affected_model_rows(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    key, name = provider
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))
    keep_routes = refresh._keep_failed_snapshot_routes

    def inexact(merged: dict, old: dict, held: dict, **kwargs: Any) -> dict:
        held_models = keep_routes(merged, old, held, **kwargs)
        if key not in held:
            return held_models
        model = next(row for row in merged["models"] if row["id"] == "acme/model")
        # Damage even a shared endpoint object: repair must read the immutable
        # published baseline, not an in-memory row affected by the same bug.
        if damage == "cache":
            model["endpoints"][0]["pricing"]["input_cache_read"] = "0.0000009"
        elif damage == "tiers":
            model["endpoints"][0]["pricing"]["completion_tiers"] = [
                {"max_prompt_tokens": None, "completion": "0.000009"},
            ]
        elif damage == "duplicate":
            model["endpoints"].append(dict(model["endpoints"][0]))
        elif damage == "missing":
            merged["models"].remove(model)
        else:
            merged["models"].append(_model("acme/new", name, "0.000001", "0.000002"))
        return held_models

    monkeypatch.setattr(refresh, "_keep_failed_snapshot_routes", inexact)

    assert refresh.main([]) == 0

    after = json.loads(refresh.SNAPSHOT_PATH.read_text())
    models = {row["id"]: row for row in after["models"]}
    assert after["model_count"] == 2
    assert models["acme/model"] == published["models"][0]
    assert "acme/new" not in models
    assert models["x-ai/grok-next"]["pricing"]["prompt"] == "0.0000035"
    model_id = "acme/new" if damage == "new" else "acme/model"
    action = "kept unpublished (no committed row)" if damage == "new" else "kept committed row"
    assert f"{model_id}: held provider routes could not be kept exact; {action}" in capsys.readouterr().out


def test_a_hold_can_preserve_the_only_published_model(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, name = provider
    published["models"][:] = [_model("acme/model", name, "0.000001", "0")]
    refresh.SNAPSHOT_PATH.write_text(json.dumps(published))
    results = _fetched(monkeypatch, key, ModelPrice(3_000_000, 1_000_000))
    del results["grok"]

    assert refresh.main([]) == 0
    after = json.loads(refresh.SNAPSHOT_PATH.read_text())
    assert after["models"] == published["models"]
    assert after["model_count"] == 1


def test_a_held_provider_publishes_only_its_published_routes(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenRouter's feed lists a new regional endpoint for the held provider's
    # model (production, 2026-09-27: z-ai/glm-5.3 [wafer:wafer/us:GLM-5.3]).
    key, name = provider
    feed = refresh.build_openrouter_snapshot()
    feed["models"][0]["endpoints"].append(
        {"tr_provider_slug": name, "model_id": "acme/model", "tag": f"{name}/us"}
    )
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))

    assert refresh.main([]) == 0

    acme = next(model for model in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"] if model["id"] == "acme/model")
    assert [(ep.get("tag"), ep["pricing"]["prompt"]) for ep in acme["endpoints"]] == [(None, "0.000001")]
    assert _endpoint_prices()["x-ai/grok-next [grok]"] == ("0.0000035", "0.000004")


def test_a_held_provider_sets_no_headline_for_a_model_it_had_not_published(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manifest fallback prices every model in the held provider's manifest,
    # including one OpenRouter now lists for it that it never published.
    key, name = provider
    feed = refresh.build_openrouter_snapshot()
    feed["models"][1]["endpoints"].append({"tr_provider_slug": name, "model_id": "x-ai/grok-next"})
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))

    def manifest_fallback(
        results: dict[str, ProviderPricingResult],
        failures: list[tuple[str, str]],
        _snapshot: Any,
    ) -> list[tuple[str, str]]:
        for slug, _reason in failures:
            results[slug] = ProviderPricingResult(
                slug=slug,
                source="stale_manifest",
                prices={
                    "acme/model": ModelPrice(1_000_000, 2_000_000),
                    "x-ai/grok-next": ModelPrice(1_000_000, 1_000_000),
                },
            )
        return []

    monkeypatch.setattr(refresh, "_apply_stale_fallbacks", manifest_fallback)

    assert refresh.main([]) == 0

    grok = next(
        model
        for model in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]
        if model["id"] == "x-ai/grok-next"
    )
    assert [ep["tr_provider_slug"] for ep in grok["endpoints"]] == ["grok"]
    assert grok["pricing"]["prompt"] == "0.0000035"


def test_a_hold_that_leaves_no_routes_keeps_only_that_models_published_row(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # grok prices its model at $0, so the merge keeps OpenRouter's endpoints for
    # it; the only one belongs to the held provider, which never published it.
    key, name = provider
    feed = refresh.build_openrouter_snapshot()
    grok = feed["models"][1]
    grok["pricing"] = {"prompt": "0.000003", "completion": "0.000004"}
    grok["endpoints"] = [
        {
            "tr_provider_slug": name,
            "model_id": "x-ai/grok-next",
            "tag": f"{name}/us",
            "pricing": {"prompt": "0.000003", "completion": "0.000004"},
        }
    ]
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    results = _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))
    results["grok"] = ProviderPricingResult(
        slug="grok", source="api", prices={"x-ai/grok-next": ModelPrice(0, 0)}
    )

    assert refresh.main([]) == 0

    models = {row["id"]: row for row in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]}
    assert models["x-ai/grok-next"] == published["models"][1]
    assert "x-ai/grok-next: no surviving endpoint can price the headline; kept committed row" in capsys.readouterr().out


@pytest.mark.parametrize(
    "grok_price",
    [ModelPrice(0, 0), ModelPrice(3_500_000, 4_000_000)],
    ids=["openrouter-priced", "grok-priced"],
)
def test_a_hold_reprices_the_headline_after_removing_an_unpublished_route(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    grok_price: ModelPrice,
) -> None:
    # OpenRouter's headline for the model may be the held provider's new route.
    # When grok prices $0, the all-zero fallback publishes that headline.
    key, name = provider
    feed = refresh.build_openrouter_snapshot()
    grok = feed["models"][1]
    grok["pricing"] = {"prompt": "0.000001", "completion": "0.000001"}
    grok["endpoints"] = [
        {
            "tr_provider_slug": name,
            "model_id": "x-ai/grok-next",
            "tag": f"{name}/us",
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
        },
        {
            "tr_provider_slug": "grok",
            "model_id": "x-ai/grok-next",
            "pricing": {"prompt": "0.000003", "completion": "0.000004"},
        },
    ]
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    results = _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))
    results["grok"] = ProviderPricingResult(
        slug="grok", source="api", prices={"x-ai/grok-next": grok_price}
    )

    assert refresh.main([]) == 0

    published_grok = next(
        model
        for model in json.loads(refresh.SNAPSHOT_PATH.read_text())["models"]
        if model["id"] == "x-ai/grok-next"
    )
    assert published_grok["pricing"]["prompt"] == ("0.000003" if grok_price == ModelPrice(0, 0) else "0.0000035")
    assert [ep["tr_provider_slug"] for ep in published_grok["endpoints"]] == ["grok"]


def test_unusable_comparison_input_skips_holding_without_crashing(
    published: dict[str, Any],
    provider: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key, _name = provider
    _fetched(monkeypatch, key, ModelPrice(3_000_000, 2_000_000))

    def unusable(*_args: Any, **_kwargs: Any) -> Any:
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(refresh, "spiking_providers", unusable)

    assert refresh.main([]) == 0

    assert "pricing.spike_hold_skipped" in caplog.text
    # Nothing was held: the workflow's spike gate decides on the tripled price.
    assert refresh.HELD_FOR_REVIEW_HEADING not in caplog.text


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
    known = {*refresh.PROVIDER_SLUGS, *refresh.RETIRED_PROVIDER_SLUGS}
    unmapped = sorted(
        name for name in names if refresh._result_slug_for_provider(name) not in known
    )
    assert unmapped == []
    assert route_provider("z-ai/glm-5.3 [io-net:io-net:zai-org/GLM-5.3] cached-input") == "io-net"
