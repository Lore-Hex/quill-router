"""A broken provider writer must not publish mixed or partial catalog state."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["raise", "invalid_json", "empty", "missing"])
def test_manifest_failure_rolls_back_before_other_providers_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    existing: bool,
    failure: str,
) -> None:
    broken = tmp_path / "broken.json"
    healthy = tmp_path / "grok.json"
    original = '{ "models": [{"id": "old"}] }\n'
    if existing:
        broken.write_text(original)

    def broken_hook(_result: ProviderPricingResult) -> list[str]:
        broken.write_text('{"models": []}' if failure == "empty" else '{"partial":')
        if failure == "raise":
            raise RuntimeError("secret-key must not appear in logs")
        if failure == "missing":
            broken.unlink()
        return ["this update must not be reported as successful"]

    def healthy_hook(_result: ProviderPricingResult) -> list[str]:
        healthy.write_text(json.dumps({"models": [{"id": "x-ai/grok-next"}]}))
        return ["grok updated"]

    modules = {
        "broken": SimpleNamespace(MANIFEST_PATH=broken, write_provider_manifest=broken_hook),
        "grok": SimpleNamespace(MANIFEST_PATH=healthy, write_provider_manifest=healthy_hook),
    }
    monkeypatch.setattr(refresh, "_import_provider", modules.__getitem__)
    results = {
        slug: ProviderPricingResult(slug=slug, source="api", prices={"model": ModelPrice(1, 2)})
        for slug in modules
    }

    notes, failures = refresh._write_provider_manifests(results)

    assert notes == ["grok updated"]
    assert len(failures) == 1
    assert failures[0][0] == "broken"
    assert failures[0][1].startswith("stage=manifest ")
    assert "secret-key" not in str(failures) + caplog.text
    assert healthy.exists()
    if existing:
        assert broken.read_text() == original
    else:
        assert not broken.exists()


@pytest.mark.parametrize("recoverable", [False, True])
def test_refresh_merges_only_recovered_prices_and_keeps_publication_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recoverable: bool,
) -> None:
    manifest = tmp_path / "xiaomi.json"
    original = '{"models": [{"id": "xiaomi/old-model"}]}\n'
    manifest.write_text(original)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("original snapshot")
    committed = {
        "models": [{
            "id": "xiaomi/old-model",
            "endpoints": [{
                "tr_provider_slug": "xiaomi",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            }],
        }] if recoverable else [],
    }

    def broken_hook(_result: ProviderPricingResult) -> None:
        manifest.write_text("partial")
        raise RuntimeError("writer failed")

    modules = {
        "xiaomi": SimpleNamespace(MANIFEST_PATH=manifest, write_provider_manifest=broken_hook),
        "grok": SimpleNamespace(),
    }
    results = {
        "grok": ProviderPricingResult(
            slug="grok", source="api", prices={"x-ai/grok-next": ModelPrice(3_000_000, 4_000_000)},
        ),
        "xiaomi": ProviderPricingResult(
            slug="xiaomi", source="api", prices={"xiaomi/old-model": ModelPrice(50, 60)},
        ),
    }
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot_path)
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", tuple(modules))
    monkeypatch.setattr(refresh, "MAX_TOLERATED_FAILURES", 0)
    monkeypatch.setattr(refresh, "_import_provider", modules.__getitem__)
    monkeypatch.setattr(refresh, "_read_existing_snapshot", lambda: committed)
    monkeypatch.setattr(refresh, "_fetch_all_providers", lambda: (results, []))
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda _requirements: None)
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: {
        "models": [{"id": model_id, "endpoints": []} for model_id in (
            "x-ai/grok-next", "xiaomi/old-model",
        )],
    })

    status = refresh.main([])

    assert manifest.read_text() == original
    if not recoverable:
        assert status == 1
        assert snapshot_path.read_text() == "original snapshot"
        assert "xiaomi" not in results
        return
    assert status == 0
    assert results["xiaomi"].source == "stale_snapshot"
    models = {row["id"]: row for row in json.loads(snapshot_path.read_text())["models"]}
    assert models["xiaomi/old-model"]["pricing"]["prompt"] == "0.000001"
    assert models["xiaomi/old-model"]["pricing"]["completion"] == "0.000002"
    assert models["x-ai/grok-next"]["pricing"]["prompt"] == "0.000003"


def test_rollback_failure_aborts_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"models": [{"id": "old"}]}')

    def hook(_result: ProviderPricingResult) -> None:
        manifest.unlink()
        manifest.mkdir()
        raise RuntimeError("write failed")

    module = SimpleNamespace(MANIFEST_PATH=manifest, write_provider_manifest=hook)
    monkeypatch.setattr(refresh, "_import_provider", lambda _slug: module)
    with pytest.raises(IsADirectoryError):
        refresh._write_provider_manifests({
            "broken": ProviderPricingResult(slug="broken", source="api", prices={}),
        })
