"""Advertised context must be a window some route will actually honour.

Upstream endpoint metadata is not trustworthy: resellers publish context
windows larger than the publisher's own endpoint serves. Taking the max
across endpoints therefore advertised a window no route accepts -- callers
sized a request to it and got a provider-side rejection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trusted_router import catalog_ingest
from trusted_router.catalog_data import maker_provider_slug
from trusted_router.catalog_ingest import _INGEST_PATH, _ingested_models_and_endpoints


def _snapshot() -> list[dict]:
    raw = json.loads(Path(_INGEST_PATH).read_text())
    items = raw if isinstance(raw, list) else raw.get("data", raw.get("models", []))
    return [m for m in items if isinstance(m, dict)]


def _api_reported_windows() -> dict[tuple[str, str], int]:
    """Each committed manifest's windows that its provider's own API reported."""
    windows: dict[tuple[str, str], int] = {}
    for path in catalog_ingest._PROVIDER_MODELS_DIR.glob("*.json"):
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            continue
        for row in raw.get("models") or []:
            if not isinstance(row, dict) or row.get("context_length_source") != "api":
                continue
            window = row.get("context_length")
            if row.get("routable") is not False and type(window) is int and window > 0:
                windows[(raw["provider"], row["id"])] = window
    return windows


def test_advertised_context_matches_publisher_or_top_provider() -> None:
    models, endpoints = _ingested_models_and_endpoints()
    by_id = {m["id"]: m for m in _snapshot() if m.get("id")}
    api_windows = _api_reported_windows()

    over = []
    for model_id, model in models.items():
        raw = by_id.get(model_id)
        if not raw:
            continue
        maker = maker_provider_slug(model_id)
        maker_routes = [
            endpoint
            for endpoint in endpoints.values()
            if endpoint.model_id == model_id and endpoint.provider == maker
        ]
        if maker_routes and (maker, model_id) in api_windows:
            if model.context_length != api_windows[(maker, model_id)]:
                over.append((model_id, model.context_length, api_windows[(maker, model_id)]))
            continue
        publisher_windows = [
            ep["context_length"]
            for ep in raw.get("endpoints", [])
            if ep.get("tr_provider_slug") == model.provider
            and type(ep.get("context_length")) is int
            and ep["context_length"] > 0
        ]
        canonical = max(publisher_windows, default=0)
        if not canonical:
            top = raw.get("top_provider")
            window = top.get("context_length") if isinstance(top, dict) else None
            canonical = window if type(window) is int and window > 0 else 0
        if canonical and model.context_length != canonical:
            over.append((model_id, model.context_length, canonical))

    assert not over, (
        "advertised context differs from the maker's API-reported window, the publisher's, "
        "or the fallback top_provider window "
        f"for {len(over)} model(s): {over}"
    )


@pytest.mark.provider_health
def test_glm_5_3_flash_advertises_the_publisher_window() -> None:
    """Regression: six reseller endpoints report 1310720; Z.AI's own says 1048576.

    Live provider state: it needs Z.AI to list the model today. The rule holds
    on a fixture in test_publisher_window_wins_over_ranked_reseller."""
    models, _ = _ingested_models_and_endpoints()
    model = models.get("z-ai/glm-5.3-flash")
    assert model is not None, "z-ai/glm-5.3-flash missing from the ingested catalog"
    assert model.context_length == 1_048_576, (
        f"expected the publisher's 1,048,576 window, got {model.context_length:,}"
    )


@pytest.mark.provider_health
def test_minimax_m3_advertises_minimax_api_window() -> None:
    """Regression: OpenRouter lists MiniMax's own endpoint at 524288, and
    DeepInfra, the first host, at 524288 too, while MiniMax's /v1/models
    reports 1000000.

    Live provider state: it needs MiniMax's API to report the window and the
    snapshot to list MiniMax's route. The rule holds on fixtures in
    test_maker_api_window_outranks_the_listing_of_its_route."""
    models, _ = _ingested_models_and_endpoints()
    model = models.get("minimax/minimax-m3")
    assert model is not None, "minimax/minimax-m3 missing from the ingested catalog"
    assert model.context_length == 1_000_000


def test_every_ingested_model_keeps_a_usable_context() -> None:
    """Narrowing must never zero a model out."""
    models, _ = _ingested_models_and_endpoints()
    assert models, "no models ingested"
    zeroed = [mid for mid, m in models.items() if not m.context_length]
    assert not zeroed, f"models left with no context window: {zeroed[:5]}"


def _ingest_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    publisher_windows: list[object],
    top_provider: object,
    model_window: object = 1_310_720,
    reseller_window: object = 1_310_720,
) -> int:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "z-ai/glm-5.2",
                        "context_length": model_window,
                        "top_provider": top_provider,
                        "endpoints": [
                            {"tr_provider_slug": "cloudflare-workers-ai", "context_length": reseller_window},
                            *(
                                {"tr_provider_slug": "zai", **window}
                                if isinstance(window, dict)
                                else {"tr_provider_slug": "zai", "context_length": window}
                                for window in publisher_windows
                            ),
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    monkeypatch.setattr(catalog_ingest, "_api_reported_context_windows", lambda: {})
    models, _ = _ingested_models_and_endpoints()
    return models["z-ai/glm-5.2"].context_length


M3 = "minimax/minimax-m3"


def _ingest_m3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoints: list[tuple[str, int]],
    api_windows: dict[tuple[str, str], int],
) -> tuple[str, int]:
    """(default route, window) built from one MiniMax M3 snapshot entry."""
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": M3,
                        "context_length": 1_048_576,
                        "top_provider": {"context_length": 1_048_576},
                        "endpoints": [
                            {"tr_provider_slug": slug, "context_length": window}
                            for slug, window in endpoints
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    monkeypatch.setattr(catalog_ingest, "_api_reported_context_windows", lambda: api_windows)
    models, _ = _ingested_models_and_endpoints()
    return models[M3].provider, models[M3].context_length


def test_maker_api_window_outranks_the_listing_of_its_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = [("deepinfra", 524_288), ("minimax", 524_288), ("novita", 1_000_000)]
    # Without MiniMax's API report, the first host's listed window (#966).
    assert _ingest_m3(tmp_path, monkeypatch, endpoints=hosts, api_windows={}) == (
        "deepinfra",
        524_288,
    )
    # MiniMax's own API reports 1000000. The default route stays DeepInfra:
    # only the advertised window changes, never routing.
    assert _ingest_m3(
        tmp_path, monkeypatch, endpoints=hosts, api_windows={("minimax", M3): 1_000_000}
    ) == ("deepinfra", 1_000_000)
    # The maker's API also narrows a listing that over-reports.
    assert _ingest_m3(
        tmp_path,
        monkeypatch,
        endpoints=[("deepinfra", 1_048_576), ("minimax", 1_048_576)],
        api_windows={("minimax", M3): 1_000_000},
    ) == ("deepinfra", 1_000_000)


def test_maker_api_window_needs_a_live_maker_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_windows = {("minimax", M3): 1_000_000}
    # The snapshot lists no MiniMax route.
    assert _ingest_m3(
        tmp_path, monkeypatch, endpoints=[("deepinfra", 200_000)], api_windows=api_windows
    ) == ("deepinfra", 200_000)
    # MiniMax's listed route is deprecated, so a reseller serves every request.
    monkeypatch.setitem(
        catalog_ingest._PROVIDER_DEPRECATED_UPSTREAM_MODELS, "minimax", frozenset({M3})
    )
    assert _ingest_m3(
        tmp_path,
        monkeypatch,
        endpoints=[("minimax", 200_000), ("deepinfra", 200_000)],
        api_windows=api_windows,
    ) == ("minimax", 200_000)


def test_only_the_makers_api_window_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _ingest_m3(
        tmp_path,
        monkeypatch,
        endpoints=[("deepinfra", 524_288), ("minimax", 524_288), ("novita", 524_288)],
        api_windows={("deepinfra", M3): 2_097_152, ("novita", M3): 2_097_152},
    ) == ("deepinfra", 524_288)


def test_api_windows_come_from_marked_routable_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unusable = [None, 0, -1, "1000000", 1_000_000.0, True, {}]
    (tmp_path / "minimax.json").write_text(
        json.dumps(
            {
                "provider": "minimax",
                "models": [
                    {"id": M3, "context_length": 1_000_000, "context_length_source": "api"},
                    {
                        "id": "minimax/listed",
                        "context_length": 204_800,
                        "context_length_source": "api",
                        "routable": True,
                    },
                    # A hand-entered or documentation-derived window.
                    {"id": "minimax/unmarked", "context_length": 1_000_000},
                    {"id": "minimax/other-source", "context_length": 1_000_000, "context_length_source": "docs"},
                    {
                        "id": "minimax/held",
                        "context_length": 1_000_000,
                        "context_length_source": "api",
                        "routable": False,
                    },
                    *(
                        {
                            "id": f"minimax/unusable-{index}",
                            "context_length": window,
                            "context_length_source": "api",
                        }
                        for index, window in enumerate(unusable)
                    ),
                    {"context_length": 1_000_000, "context_length_source": "api"},
                    "minimax/not-a-row",
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "no-provider.json").write_text(
        json.dumps({"models": [{"id": "a/b", "context_length": 1, "context_length_source": "api"}]}),
        encoding="utf-8",
    )
    (tmp_path / "no-rows.json").write_text(json.dumps({"provider": "x", "models": {}}), encoding="utf-8")
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    (tmp_path / "truncated.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    assert catalog_ingest._api_reported_context_windows() == {
        ("minimax", M3): 1_000_000,
        ("minimax", "minimax/listed"): 204_800,
    }


@pytest.mark.parametrize("top_window", [202_752, 1_024_000, 1_310_720])
def test_publisher_window_wins_over_ranked_reseller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top_window: int
) -> None:
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=[1_048_576],
            top_provider={"context_length": top_window},
        )
        == 1_048_576
    )


@pytest.mark.parametrize(
    "windows",
    [
        [262_144, 1_048_576, 512_000],
        [1_048_576, 512_000, 262_144],
        [None, 0, -1, "2097152", 2_097_152.0, True, {}, 1_048_576],
    ],
)
def test_largest_usable_publisher_window_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: list[object]
) -> None:
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=windows,
            top_provider={"context_length": 202_752},
        )
        == 1_048_576
    )


@pytest.mark.parametrize(
    "publisher_windows",
    [
        [],
        [{}],
        [None],
        [0],
        [-1],
        ["1048576"],
        [1_048_576.0],
        [True],
    ],
)
def test_top_provider_wins_without_usable_publisher_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publisher_windows: list[object]
) -> None:
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=publisher_windows,
            top_provider={"context_length": 202_752},
        )
        == 202_752
    )


@pytest.mark.parametrize(
    "top_provider",
    [
        None,
        {},
        "invalid",
        {"context_length": None},
        {"context_length": 0},
        {"context_length": -1},
        {"context_length": "2097152"},
        {"context_length": 2_097_152.0},
        {"context_length": True},
    ],
)
@pytest.mark.parametrize(
    "model_window,reseller_window",
    [
        (1_048_576, 262_144),
        (262_144, 1_048_576),
    ],
)
def test_max_fallback_without_usable_publisher_or_top_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    top_provider: object,
    model_window: int,
    reseller_window: int,
) -> None:
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=[],
            top_provider=top_provider,
            model_window=model_window,
            reseller_window=reseller_window,
        )
        == 1_048_576
    )


@pytest.mark.parametrize("unusable", [None, 0, -1, "2097152", 2_097_152.0, True, {}])
def test_max_fallback_ignores_unusable_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unusable: object
) -> None:
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=[],
            top_provider=None,
            model_window=unusable,
            reseller_window=262_144,
        )
        == 262_144
    )
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=[],
            top_provider=None,
            model_window=262_144,
            reseller_window=unusable,
        )
        == 262_144
    )
    assert (
        _ingest_context(
            tmp_path,
            monkeypatch,
            publisher_windows=[unusable],
            top_provider=None,
            model_window=unusable,
            reseller_window=unusable,
        )
        == 0
    )
