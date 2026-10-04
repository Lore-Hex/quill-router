"""Provider failures hold only their own committed state; majority failures roll back."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import crusoe


@pytest.fixture
def refresh_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    slugs = ("bad_a", "bad_b", "bad_c", "good_a", "good_b", "good_c", "good_d")
    manifests = tmp_path / "provider_models"
    parsers = tmp_path / "parsers"
    manifests.mkdir()
    parsers.mkdir()
    snapshot = tmp_path / "snapshot.json"
    old_endpoint = {
        "tr_provider_slug": "bad_a",
        "model_id": "native/shared",
        "tag": "region-a",
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000002",
            "input_cache_read": "0.0000001",
        },
    }
    published = {
        "models": [
            {
                "id": "acme/shared",
                "pricing": old_endpoint["pricing"],
                "endpoints": [
                    old_endpoint,
                    {
                        **old_endpoint,
                        "tag": "region-b",
                        "pricing": {
                            **old_endpoint["pricing"],
                            "input_cache_read": "0.0000002",
                        },
                    },
                ],
            },
            {
                "id": "acme/removed-from-feed",
                "pricing": old_endpoint["pricing"],
                "endpoints": [
                    {**old_endpoint, "model_id": "native/old"},
                ],
            },
        ]
    }
    snapshot.write_text(json.dumps(published))
    state: dict[str, Any] = {
        "fetch_failures": {"bad_a", "bad_b"},
        "manifest_failures": {"bad_c"},
        "written": [],
        "results": {},
        "published": published,
    }

    def model_id(slug: str) -> str:
        return "acme/shared" if slug == "good_a" else f"acme/{slug}"

    def manifest(slug: str, price: int) -> dict[str, Any]:
        return {
            "provider": slug,
            "generated_at": "old" if price == 3_000_000 else "fresh",
            "models": [
                {
                    "id": model_id(slug),
                    "input_token_price_per_m": price,
                    "output_token_price_per_m": 4_000_000,
                }
            ],
        }

    modules = {}
    for slug in slugs:
        path = manifests / f"{slug}.json"
        if slug not in {"bad_b", "good_d"}:
            raw = manifest(slug, 3_000_000)
            # No valid fallback for these providers; the committed manifest
            # and snapshot still must be kept exactly, without inventing rates.
            if slug.startswith("bad_"):
                raw["models"][0].pop("input_token_price_per_m")
            path.write_bytes((json.dumps(raw, indent=1) + "\r\n").encode())
        (parsers / f"{slug}.py").write_text("# committed parser\n")

        def write(result: ProviderPricingResult, slug: str = slug, path: Path = path) -> list[str]:
            state["written"].append(slug)
            if slug in state["manifest_failures"]:
                path.write_text('{"partial":')
                raise ValueError("bad manifest")
            path.write_text(json.dumps(manifest(slug, 3_500_000)))
            return [f"{slug}: refreshed"]

        modules[slug] = SimpleNamespace(
            MANIFEST_PATH=path,
            MANIFEST_STALE_FALLBACK=True,
            write_provider_manifest=write,
        )

    def fetch() -> tuple[dict[str, ProviderPricingResult], list[tuple[str, str]]]:
        for slug in refresh.PROVIDER_SLUGS:
            (parsers / f"{slug}.py").write_text("# rewritten parser\n")
        results = {
            slug: ProviderPricingResult(
                slug=slug, source="api", prices={model_id(slug): ModelPrice(3_500_000, 4_000_000)}
            )
            for slug in refresh.PROVIDER_SLUGS
            if slug not in state["fetch_failures"]
        }
        state["results"] = results
        return results, [(slug, "fetch failed") for slug in sorted(state["fetch_failures"])]

    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "parser_path", lambda slug: parsers / f"{slug}.py")
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", slugs)
    monkeypatch.setattr(refresh, "_import_provider", modules.__getitem__)
    monkeypatch.setattr(refresh, "_fetch_all_providers", fetch)
    monkeypatch.setattr(
        refresh,
        "build_openrouter_snapshot",
        lambda: {
            "models": [
                {
                    "id": model_id(slug),
                    "endpoints": [{"tr_provider_slug": slug, "model_id": model_id(slug)}],
                }
                for slug in slugs
            ]
        },
    )
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda *_args: None)
    state["before"] = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    state["root"] = tmp_path
    return state


def test_three_unrecovered_failures_keep_exact_state_and_publish_every_healthy_manifest(
    refresh_run: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert refresh.main([]) == 0

    root = refresh_run["root"]
    for slug in ("bad_a", "bad_b", "bad_c"):
        manifest = Path("provider_models") / f"{slug}.json"
        if manifest in refresh_run["before"]:
            assert (root / manifest).read_bytes() == refresh_run["before"][manifest]
        else:
            assert not (root / manifest).exists()
        assert (root / "parsers" / f"{slug}.py").read_bytes() == refresh_run["before"][
            Path("parsers") / f"{slug}.py"
        ]
        assert slug not in refresh_run["results"]
    for slug in ("good_a", "good_b", "good_c", "good_d"):
        raw = json.loads((root / "provider_models" / f"{slug}.json").read_text())
        assert raw["generated_at"] == "fresh"
        assert raw["models"][0]["input_token_price_per_m"] == 3_500_000
    assert set(refresh_run["written"]) == {"bad_c", "good_a", "good_b", "good_c", "good_d"}
    published = json.loads(refresh.SNAPSHOT_PATH.read_text())
    models = {model["id"]: model for model in published["models"]}
    assert published["model_count"] == 5
    assert set(models) == {
        "acme/shared",
        "acme/removed-from-feed",
        "acme/good_b",
        "acme/good_c",
        "acme/good_d",
    }
    shared = models["acme/shared"]
    assert len(shared["endpoints"]) == 3
    assert shared["endpoints"][0]["pricing"]["prompt"] == "0.0000035"
    assert shared["endpoints"][1:] == refresh_run["published"]["models"][0]["endpoints"]
    assert shared["pricing"]["prompt"] == "0.000001"
    assert models["acme/removed-from-feed"] == refresh_run["published"]["models"][1]
    out = capsys.readouterr().out
    assert refresh.HELD_FOR_REVIEW_HEADING in out
    for slug in ("bad_a", "bad_b", "bad_c"):
        assert f"  {slug}:\n    refresh failed:" in out
    assert "stage=manifest ValueError" in out
    assert "3 failed (kept committed state; absent providers remain absent)" in out


@pytest.mark.parametrize("recoverable", [False, True])
def test_majority_failure_restores_snapshot_all_manifests_and_parsers(
    refresh_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recoverable: bool,
) -> None:
    refresh_run["fetch_failures"].add("good_b")
    if recoverable:

        def recover(results: dict, failures: list, _snapshot: dict) -> list:
            for slug, _error in failures:
                results[slug] = ProviderPricingResult(
                    slug=slug, source="stale_manifest", prices={"acme/old": ModelPrice(1, 2)}
                )
            return []

        monkeypatch.setattr(refresh, "_apply_stale_fallbacks", recover)

    assert refresh.main([]) == 1

    root = refresh_run["root"]
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == refresh_run["before"]
    assert (
        "Systemic refresh failure: 4/7 providers failed; nothing published."
        in capsys.readouterr().out
    )
    # Healthy writers really ran, including a newly created manifest; rollback
    # must undo their writes as well as failed providers' parser rewrites.
    assert {"good_a", "good_c", "good_d"} <= set(refresh_run["written"])


def test_exactly_half_failing_is_not_a_systemic_failure(
    refresh_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", refresh.PROVIDER_SLUGS[:-1])
    assert refresh.main([]) == 0
    assert set(refresh_run["written"]) == {"bad_c", "good_a", "good_b", "good_c"}
    assert (
        json.loads((refresh.PROVIDER_MANIFEST_DIR / "good_a.json").read_text())["generated_at"]
        == "fresh"
    )


def test_failed_provider_preservation_is_still_checked_for_exactness(
    refresh_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(refresh, "_keep_failed_snapshot_routes", lambda *_args: None)
    assert refresh.main([]) == 1
    assert "Held providers could not be kept exactly as published" in capsys.readouterr().out
    root = refresh_run["root"]
    assert {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == refresh_run["before"]


def test_removing_new_failed_route_cannot_leave_its_openrouter_headline() -> None:
    good_price = {"prompt": "0.000003", "completion": "0.000004"}
    merged = {
        "models": [
            {
                "id": "acme/shared",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "pricing_source": "openrouter_fallback",
                "endpoints": [
                    {
                        "tr_provider_slug": "bad_a",
                        "model_id": "new",
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    },
                    {"tr_provider_slug": "good_a", "model_id": "shared", "pricing": good_price},
                ],
            }
        ]
    }
    assert refresh._keep_failed_snapshot_routes(merged, {"models": []}, {"bad_a": []}) == {}

    assert merged["model_count"] == 1
    assert merged["models"][0]["pricing"] == good_price
    assert len(merged["models"][0]["endpoints"]) == 1
    assert merged["models"][0]["endpoints"][0]["tr_provider_slug"] == "good_a"


@pytest.mark.parametrize("committed", [False, True])
def test_unpriceable_headline_holds_only_its_model_and_reports_it(
    refresh_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    committed: bool,
) -> None:
    model_id = "acme/unpriceable"
    price = {"prompt": "0.000001", "completion": "0.000002"}
    old = {
        "id": model_id,
        "name": "Committed name",
        "context_length": 4096,
        "pricing": price,
        "pricing_source": "provider_direct",
        "endpoints": [
            {"tr_provider_slug": "bad_a", "model_id": "old", "pricing": {}},
            {"tr_provider_slug": "good_a", "model_id": "old", "pricing": price},
        ],
    }
    if committed:
        refresh_run["published"]["models"].append(old)
        refresh.SNAPSHOT_PATH.write_text(json.dumps(refresh_run["published"]))
    feed = refresh.build_openrouter_snapshot()
    feed["models"].append({
        "id": model_id,
        "name": "Fresh name",
        "context_length": 8192,
        "pricing": price,
        "endpoints": [
            {"tr_provider_slug": "bad_a", "model_id": "new", "pricing": price},
            {"tr_provider_slug": "good_a", "model_id": "new", "pricing": {}},
        ],
    })
    original_fetch = refresh._fetch_all_providers

    def fetch() -> tuple[dict[str, ProviderPricingResult], list[tuple[str, str]]]:
        results, failures = original_fetch()
        # Exercise the real merger's all-zero OpenRouter fallback. Once the
        # failed provider's new route is removed, no endpoint can price it.
        results["good_a"].prices[model_id] = ModelPrice(0, 0)
        return results, failures

    monkeypatch.setattr(refresh, "_fetch_all_providers", fetch)
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)

    assert refresh.main([]) == 0

    snapshot = json.loads(refresh.SNAPSHOT_PATH.read_text())
    models = {model["id"]: model for model in snapshot["models"]}
    assert snapshot["model_count"] == 5 + int(committed)
    if committed:
        assert models[model_id] == old
    else:
        assert model_id not in models
    assert models["acme/good_b"]["pricing"]["prompt"] == "0.0000035"
    assert models["acme/shared"]["endpoints"][0]["pricing"]["prompt"] == "0.0000035"
    assert json.loads((refresh.PROVIDER_MANIFEST_DIR / "good_a.json").read_text())["generated_at"] == "fresh"
    out = capsys.readouterr().out
    action = "kept committed row" if committed else "kept unpublished (no committed row)"
    assert f"Snapshot models held:\n  {model_id}: no surviving endpoint can price the headline; {action}" in out
    assert "3 failed (kept committed state; absent providers remain absent)" in out


def test_crusoe_mass_prune_rejection_is_isolated_and_keeps_committed_data(
    refresh_run: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = crusoe.MANIFEST_PATH.read_bytes()
    path = refresh.PROVIDER_MANIFEST_DIR / "crusoe.json"
    path.write_bytes(before)
    committed = next(row for row in json.loads(before)["models"] if row["id"] == "moonshotai/kimi-k2.6")
    price = refresh._price_to_pricing_block(ModelPrice(
        committed["input_token_price_per_m"], committed["output_token_price_per_m"],
    ))
    old = {
        "id": committed["id"],
        "pricing": price,
        "endpoints": [{
            "tr_provider_slug": "crusoe", "model_id": committed["upstream_id"], "pricing": price,
        }],
    }
    refresh_run["published"]["models"].append(old)
    refresh.SNAPSHOT_PATH.write_text(json.dumps(refresh_run["published"]))
    refresh_run["fetch_failures"].clear()
    refresh_run["manifest_failures"].clear()
    original_import = refresh._import_provider
    original_fetch = refresh._fetch_all_providers

    def fetch() -> tuple[dict[str, ProviderPricingResult], list[tuple[str, str]]]:
        results, failures = original_fetch()
        results["crusoe"] = ProviderPricingResult(slug="crusoe", source="api_auth_failed", prices={})
        return results, failures

    monkeypatch.setattr(crusoe, "MANIFEST_PATH", path)
    monkeypatch.setattr(crusoe, "_LIVE_CANARY_OK", False)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", (*refresh.PROVIDER_SLUGS, "crusoe"))
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: crusoe if slug == "crusoe" else original_import(slug))
    monkeypatch.setattr(refresh, "_fetch_all_providers", fetch)

    assert refresh.main([]) == 0

    assert path.read_bytes() == before
    assert refresh_run["results"]["crusoe"].source == "stale_snapshot"
    snapshot = json.loads(refresh.SNAPSHOT_PATH.read_text())
    models = {model["id"]: model for model in snapshot["models"]}
    assert snapshot["model_count"] == 8
    assert models[committed["id"]] == old
    assert models["acme/good_b"]["pricing"]["prompt"] == "0.0000035"
    assert len(refresh_run["written"]) == 7
    for slug in refresh_run["written"]:
        assert json.loads((refresh.PROVIDER_MANIFEST_DIR / f"{slug}.json").read_text())["generated_at"] == "fresh"
    captured = capsys.readouterr()
    out = captured.out
    assert "crusoe: manifest rebuild blocked by mass-prune guard" in captured.err
    assert refresh.HELD_FOR_REVIEW_HEADING in out
    assert "  crusoe:\n    refresh failed: stage=manifest ValueError category=manifest_invalid" in out
    assert "1 failed (kept committed state; absent providers remain absent)" in out


def test_failed_route_does_not_inherit_a_departed_healthy_providers_headline() -> None:
    cheap = {"prompt": "0.000001", "completion": "0.000002"}
    costly = {"prompt": "0.000003", "completion": "0.000004"}
    failed_endpoint = {"tr_provider_slug": "bad_a", "model_id": "shared", "pricing": costly}
    published = {
        "models": [
            {
                "id": "acme/shared",
                "pricing": cheap,
                "endpoints": [
                    {"tr_provider_slug": "good_a", "model_id": "shared", "pricing": cheap},
                    failed_endpoint,
                ],
            }
        ]
    }
    merged: dict[str, Any] = {"models": []}

    refresh._keep_failed_snapshot_routes(merged, published, {"bad_a": []})

    assert merged["model_count"] == 1
    assert merged["models"][0]["endpoints"] == [failed_endpoint]
    assert merged["models"][0]["pricing"] == costly


def test_restore_never_writes_the_repository_parsers_or_manifests(tmp_path, monkeypatch):
    """A restore aimed at temp directories must leave the real files untouched."""
    from scripts.pricing import base
    from scripts.pricing.providers import crusoe

    real_parser = base.parser_path("crusoe")
    real_manifest = Path(crusoe.MANIFEST_PATH)
    before = {
        path: path.read_bytes() for path in (real_parser, real_manifest) if path.exists()
    }
    manifests = tmp_path / "live" / refresh.PROVIDER_MANIFEST_DIR.name
    parsers = tmp_path / "live" / refresh.PARSERS_DIR.name
    manifests.mkdir(parents=True)
    parsers.mkdir(parents=True)
    baseline = tmp_path / "baseline"
    (baseline / manifests.name).mkdir(parents=True)
    (baseline / parsers.name).mkdir(parents=True)
    # An empty published parser and manifest: restoring them must only touch temp.
    (baseline / parsers.name / real_parser.name).write_text("")
    (baseline / manifests.name / real_manifest.name).write_text("")
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)

    refresh._restore_published_files(baseline, "crusoe")

    assert (parsers / real_parser.name).read_text() == ""
    assert (manifests / real_manifest.name).read_text() == ""
    assert {path: path.read_bytes() for path in before} == before
