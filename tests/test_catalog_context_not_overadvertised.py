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
from trusted_router.catalog_ingest import _INGEST_PATH, _ingested_models_and_endpoints


def _snapshot() -> list[dict]:
    raw = json.loads(Path(_INGEST_PATH).read_text())
    items = raw if isinstance(raw, list) else raw.get("data", raw.get("models", []))
    return [m for m in items if isinstance(m, dict)]


def test_advertised_context_matches_publisher_or_top_provider() -> None:
    models, _ = _ingested_models_and_endpoints()
    by_id = {m["id"]: m for m in _snapshot() if m.get("id")}

    over = []
    for model_id, model in models.items():
        raw = by_id.get(model_id)
        if not raw:
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
        "advertised context differs from the publisher (or fallback top_provider) window "
        f"for {len(over)} model(s): {over}"
    )


def test_glm_5_3_flash_advertises_the_publisher_window() -> None:
    """Regression: six reseller endpoints report 1310720; Z.AI's own says 1048576."""
    models, _ = _ingested_models_and_endpoints()
    model = models.get("z-ai/glm-5.3-flash")
    assert model is not None, "z-ai/glm-5.3-flash missing from the ingested catalog"
    assert model.context_length == 1_048_576, (
        f"expected the publisher's 1,048,576 window, got {model.context_length:,}"
    )


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
    models, _ = _ingested_models_and_endpoints()
    return models["z-ai/glm-5.2"].context_length


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
