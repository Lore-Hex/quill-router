"""The DeepSeek V4 Pro release leaves fail closed, one leaf at a time.

`_install_deepseek_v4_pro_release_routes` runs at import over a catalog the
hourly price refresh rewrites without a human in the loop. A leaf whose
required routes are gone is not offered at all; it is never offered on a
different route set, and nothing raises, so the control plane still starts.
The cases below run on a fixture catalog, not on today's providers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from trusted_router import catalog_ingest, catalog_registry
from trusted_router.catalog_data import (
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

BASE = "deepseek/deepseek-v4-pro"
_TEMPLATE_MODEL = next(iter(catalog_registry.MODELS.values()))
_TEMPLATE_ENDPOINT = next(iter(catalog_registry.MODEL_ENDPOINTS.values()))


def _endpoint(model_id: str, provider: str, *, usage_type: str = "Credits") -> object:
    suffix = "prepaid" if usage_type == "Credits" else "byok"
    return replace(
        _TEMPLATE_ENDPOINT,
        id=f"{model_id}@{provider}/{suffix}",
        model_id=model_id,
        provider=provider,
        usage_type=usage_type,
        prompt_price_microdollars_per_million_tokens=1_000_000,
        completion_price_microdollars_per_million_tokens=3_000_000,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    base: bool = True,
    deepseek: bool = True,
    baseten: bool = True,
    fireworks: bool = True,
    fireworks_retired: bool = False,
    historical: tuple[str, ...] = ("parasail", "venice"),
    rolling_window: int | None = None,
    windows: dict[str, int] | None = None,
    filtered: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    models: dict = {}
    if base:
        models[BASE] = replace(_TEMPLATE_MODEL, id=BASE, name="DeepSeek V4 Pro")
        if rolling_window is not None:
            models[BASE] = replace(models[BASE], context_length=rolling_window)
    # A provider manifest listed the release id itself. The installer owns
    # that id: the row must neither survive on its own nor join the leaf.
    models[DEEPSEEK_V4_PRO_0813_MODEL_ID] = replace(
        _TEMPLATE_MODEL, id=DEEPSEEK_V4_PRO_0813_MODEL_ID, byok_available=True
    )
    ingested = {
        endpoint.id: endpoint
        for endpoint in (
            *(_endpoint(BASE, provider) for provider in historical),
            _endpoint(BASE, "parasail", usage_type="BYOK"),
        )
    }
    endpoints = dict(ingested)
    manifest_rows = [_endpoint(DEEPSEEK_V4_PRO_0813_MODEL_ID, "novita", usage_type="BYOK")]
    if deepseek:
        manifest_rows.append(_endpoint(BASE, "deepseek"))
    if baseten:
        manifest_rows.append(_endpoint(DEEPSEEK_V4_PRO_0813_MODEL_ID, "baseten"))
    if fireworks:
        manifest_rows.append(_endpoint(DEEPSEEK_V4_PRO_0813_MODEL_ID, "fireworks"))
    endpoints.update({endpoint.id: endpoint for endpoint in manifest_rows})
    labeled = ("parasail", "venice", "siliconflow", "deepseek")
    snapshot = tmp_path / "openrouter_snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": BASE,
                        "endpoints": [
                            {
                                "name": f"{slug} | deepseek-v4-pro-20260423",
                                "tr_provider_slug": slug,
                                **({"context_length": windows[slug]} if slug in (windows or {}) else {}),
                            }
                            for slug in labeled
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_registry, "MODELS", models)
    monkeypatch.setattr(catalog_registry, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(catalog_registry, "_INGESTED_ENDPOINTS", ingested)
    monkeypatch.setattr(catalog_registry, "_INGEST_PATH", snapshot)
    monkeypatch.setattr(
        catalog_registry,
        "provider_model_retired",
        lambda provider, model_id, *, at: fireworks_retired and provider == "fireworks",
    )
    host_windows = catalog_registry._install_deepseek_v4_pro_release_routes()
    # The provider filters run between the two, as at import.
    for provider in filtered:
        del endpoints[f"{DEEPSEEK_V4_PRO_0423_MODEL_ID}@{provider}/prepaid"]
    catalog_registry._settle_deepseek_v4_pro_0423_leaf(host_windows)
    return models, endpoints


def _routes(endpoints: dict, model_id: str) -> set[tuple[str, str]]:
    return {
        (endpoint.provider, endpoint.usage_type)
        for endpoint in endpoints.values()
        if endpoint.model_id == model_id
    }



@pytest.mark.parametrize(
    ("rolling", "hosts", "filtered", "leaf"),
    [
        # Its hosts shrink while the rolling model keeps 1M.
        (1_048_576, {"parasail": 262_144, "venice": 524_288}, (), 524_288),
        # The rolling model shrinks while its hosts keep 1M.
        (262_144, {"parasail": 1_048_576, "venice": 1_000_000}, (), 1_048_576),
        # A host whose route the provider filters drop does not count.
        (1_048_576, {"parasail": 262_144, "venice": 1_048_576}, ("venice",), 262_144),
        # No host lists a window: the rolling model's.
        (777_777, {}, (), 777_777),
    ],
)
def test_the_0423_leaf_advertises_the_largest_window_its_own_routes_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rolling: int,
    hosts: dict[str, int],
    filtered: tuple[str, ...],
    leaf: int,
) -> None:
    # SiliconFlow is labeled but has no Credits route here, and DeepSeek is
    # the rolling route; at 2M either would show if it counted.
    windows = {**hosts, "siliconflow": 2_000_000, "deepseek": 2_000_000} if hosts else {}
    models, _ = _install(
        monkeypatch, tmp_path, rolling_window=rolling, windows=windows, filtered=filtered,
    )

    assert models[DEEPSEEK_V4_PRO_0423_MODEL_ID].context_length == leaf
    assert models[DEEPSEEK_V4_PRO_0813_MODEL_ID].context_length == rolling


def test_a_0423_leaf_whose_routes_the_provider_filters_all_drop_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, endpoints = _install(
        monkeypatch, tmp_path, windows={"parasail": 1_048_576, "venice": 1_048_576},
        filtered=("parasail", "venice"),
    )

    assert DEEPSEEK_V4_PRO_0423_MODEL_ID not in models
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0423_MODEL_ID) == set()
    # The 0813 leaf stands on its own routes.
    assert DEEPSEEK_V4_PRO_0813_MODEL_ID in models

def test_a_complete_catalog_installs_both_leaves_on_exactly_their_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, endpoints = _install(monkeypatch, tmp_path)

    # 0423: the snapshot's 20260423-labeled hosts that have a Credits route,
    # never the rolling first-party route or a BYOK route.
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0423_MODEL_ID) == {
        ("parasail", "Credits"),
        ("venice", "Credits"),
    }
    # 0813: DeepSeek, Baseten and Fireworks, and not the manifest's BYOK row.
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0813_MODEL_ID) == {
        ("deepseek", "Credits"),
        ("baseten", "Credits"),
        ("fireworks", "Credits"),
    }
    for leaf in (DEEPSEEK_V4_PRO_0423_MODEL_ID, DEEPSEEK_V4_PRO_0813_MODEL_ID):
        assert models[leaf].byok_available is False
        assert models[leaf].prepaid_available is True


@pytest.mark.parametrize(
    "missing",
    [{"deepseek": False}, {"baseten": False}, {"fireworks": False}],
    ids=["deepseek", "baseten", "fireworks-not-retired"],
)
def test_0813_is_not_offered_when_a_required_route_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: dict[str, bool]
) -> None:
    models, endpoints = _install(monkeypatch, tmp_path, **missing)

    # Not offered at all, rather than offered on the routes that remain, and
    # the manifest's own row for the id is gone with it.
    assert DEEPSEEK_V4_PRO_0813_MODEL_ID not in models
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0813_MODEL_ID) == set()
    # The other leaf does not depend on these routes.
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0423_MODEL_ID) == {
        ("parasail", "Credits"),
        ("venice", "Credits"),
    }


def test_0813_drops_only_a_route_the_lifecycle_has_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, endpoints = _install(monkeypatch, tmp_path, fireworks=False, fireworks_retired=True)

    assert DEEPSEEK_V4_PRO_0813_MODEL_ID in models
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0813_MODEL_ID) == {
        ("deepseek", "Credits"),
        ("baseten", "Credits"),
    }


def test_0423_is_not_offered_without_a_historical_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, endpoints = _install(monkeypatch, tmp_path, historical=())

    assert DEEPSEEK_V4_PRO_0423_MODEL_ID not in models
    assert _routes(endpoints, DEEPSEEK_V4_PRO_0423_MODEL_ID) == set()
    assert DEEPSEEK_V4_PRO_0813_MODEL_ID in models


def test_neither_leaf_is_offered_without_the_base_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    models, endpoints = _install(monkeypatch, tmp_path, base=False)

    for leaf in (DEEPSEEK_V4_PRO_0423_MODEL_ID, DEEPSEEK_V4_PRO_0813_MODEL_ID):
        assert leaf not in models
        assert _routes(endpoints, leaf) == set()


def test_the_control_plane_starts_when_a_release_route_is_delisted(tmp_path: Path) -> None:
    # Today's catalog with Baseten's 0813 route delisted the way a refresh
    # publishes it: the manifest row tombstoned and the snapshot's endpoint
    # gone. The registry used to raise at import here.
    manifests = tmp_path / "provider_models"
    shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, manifests)
    baseten = manifests / "baseten.json"
    raw = json.loads(baseten.read_text(encoding="utf-8"))
    for row in raw["models"]:
        if row.get("id") == DEEPSEEK_V4_PRO_0813_MODEL_ID:
            row.update(routable=False, routable_reason="delisted-upstream")
    baseten.write_text(json.dumps(raw), encoding="utf-8")
    snapshot = json.loads(catalog_ingest._INGEST_PATH.read_text(encoding="utf-8"))
    for model in snapshot["models"]:
        if model.get("id") == DEEPSEEK_V4_PRO_0813_MODEL_ID:
            model["endpoints"] = [
                endpoint
                for endpoint in model.get("endpoints", [])
                if endpoint.get("tr_provider_slug") != "baseten"
            ]
    snapshot_path = tmp_path / "openrouter_snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    environ = dict(os.environ)
    environ["TR_TEST_PROVIDER_MODELS_DIR"] = str(manifests)
    environ["TR_TEST_SNAPSHOT_PATH"] = str(snapshot_path)
    result = subprocess.run(  # noqa: S603 - fixed Python regression script
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import os
                from pathlib import Path
                from trusted_router import catalog_ingest
                catalog_ingest._PROVIDER_MODELS_DIR = Path(os.environ["TR_TEST_PROVIDER_MODELS_DIR"])
                catalog_ingest._INGEST_PATH = Path(os.environ["TR_TEST_SNAPSHOT_PATH"])
                from trusted_router import catalog_registry
                leaf = catalog_registry.DEEPSEEK_V4_PRO_0813_MODEL_ID
                assert leaf not in catalog_registry.MODELS
                assert not any(
                    endpoint.model_id == leaf
                    for endpoint in catalog_registry.MODEL_ENDPOINTS.values()
                )
                print("started")
                """
            ),
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("started")
