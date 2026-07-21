from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import friendli, gemini, wafer
from trusted_router import catalog_ingest


def _manifest_row(model_id: str, upstream_id: str, **metadata: object) -> dict[str, object]:
    return {
        "id": model_id,
        "upstream_id": upstream_id,
        "display_name": model_id,
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_token_price_per_m": 1,
        "output_token_price_per_m": 2,
        **metadata,
    }


def _write_manifest(path: Path, provider: str, rows: list[dict[str, object]]) -> str:
    text = json.dumps(
        {
            "_about": f"{provider} test manifest",
            "provider": provider,
            "generated_at": "2026-01-01T00:00:00Z",
            "model_count": len(rows),
            "models": rows,
        },
        indent=2,
    ) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def test_friendli_tombstones_second_miss_then_restores_annotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "friendli.json"
    _write_manifest(
        manifest_path,
        "friendli",
        [
            _manifest_row("z-ai/glm-5.2", "zai-org/GLM-5.2", _note="keep me"),
            _manifest_row(
                "meta-llama/llama-3.3-70b-instruct",
                "meta-llama-3.3-70b-instruct",
            ),
            _manifest_row(
                "qwen/qwen3-235b-a22b-2507",
                "Qwen/Qwen3-235B-A22B-Instruct-2507",
                _about="curated annotation: keep byte-identical",
                note={"owner": "catalog", "reason": "manual"},
            ),
            _manifest_row("metadata/future", "future", routable=False, _note="staged"),
        ],
    )
    payload = {
        "data": [
            {
                "id": "zai-org/GLM-5.2",
                "pricing": {"input": "0.0000014", "output": "0.0000044"},
            },
            {
                "id": "meta-llama-3.3-70b-instruct",
                "pricing": {"input": "0.0000006", "output": "0.0000006"},
            },
        ]
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(friendli, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(friendli.httpx, "Client", FakeClient)

    result = friendli.fetch()
    friendli.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    missing = by_id["qwen/qwen3-235b-a22b-2507"]
    assert missing["missing_since"]
    assert missing.get("routable") is not False
    assert by_id["z-ai/glm-5.2"]["_note"] == "keep me"
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 1_400_000
    assert by_id["metadata/future"]["routable"] is False
    assert by_id["metadata/future"]["_note"] == "staged"

    result = friendli.fetch()
    friendli.write_provider_manifest(result)
    tombstoned_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    tombstoned = {row["id"]: row for row in tombstoned_raw["models"]}[
        "qwen/qwen3-235b-a22b-2507"
    ]
    assert tombstoned["routable"] is False
    assert tombstoned["routable_reason"] == "delisted-upstream"
    assert tombstoned["missing_since"] == missing["missing_since"]

    payload["data"].append(
        {
            "id": "Qwen/Qwen3-235B-A22B-Instruct-2507",
            "pricing": {"input": "0.0000002", "output": "0.0000008"},
        }
    )
    result = friendli.fetch()
    friendli.write_provider_manifest(result)
    restored_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored = {row["id"]: row for row in restored_raw["models"]}[
        "qwen/qwen3-235b-a22b-2507"
    ]
    assert restored["routable"] is True
    assert "routable_reason" not in restored
    assert "missing_since" not in restored
    assert restored["_about"] == tombstoned["_about"]
    assert restored["note"] == tombstoned["note"]


def test_gemini_tombstones_second_miss_and_empty_discovery_keeps_old_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "gemini.json"
    old_text = _write_manifest(
        manifest_path,
        "gemini",
        [
            _manifest_row("google/gemini-2.5-pro", "gemini-2.5-pro", _note="keep"),
            _manifest_row("google/gemini-2.5-flash", "gemini-2.5-flash"),
            _manifest_row("google/gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
            _manifest_row("google/gemini-staged", "gemini-staged", routable=False),
        ],
    )
    pricing = ProviderPricingResult(
        slug="gemini",
        prices={
            "google/gemini-2.5-pro": ModelPrice(1_250_000, 10_000_000),
            "google/gemini-2.5-flash": ModelPrice(300_000, 2_500_000),
        },
        source="api",
        fetched_url=gemini.URL,
    )
    live_payload = {
        "models": [
            {
                "name": "models/gemini-2.5-pro",
                "displayName": "Gemini 2.5 Pro",
                "inputTokenLimit": 1_048_576,
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
        ]
    }
    monkeypatch.setattr(gemini, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(gemini, "fetch_provider", lambda **_kwargs: pricing)
    monkeypatch.setattr(gemini, "fetch_json", lambda *_args, **_kwargs: live_payload)

    result = gemini.fetch()
    gemini.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert by_id["google/gemini-2.5-flash-lite"]["missing_since"]
    assert by_id["google/gemini-2.5-flash-lite"].get("routable") is not False
    assert by_id["google/gemini-2.5-pro"]["_note"] == "keep"
    assert by_id["google/gemini-staged"]["routable"] is False

    result = gemini.fetch()
    gemini.write_provider_manifest(result)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    tombstoned = {row["id"]: row for row in raw["models"]}[
        "google/gemini-2.5-flash-lite"
    ]
    assert tombstoned["routable"] is False
    assert tombstoned["routable_reason"] == "delisted-upstream"

    # A failed/empty discovery pass cannot rewrite even top-level timestamps.
    manifest_path.write_text(old_text, encoding="utf-8")
    gemini._DISCOVERED_MANIFEST_ROWS = {}  # noqa: SLF001
    gemini.write_provider_manifest(pricing)
    assert manifest_path.read_text(encoding="utf-8") == old_text


def test_wafer_feed_presence_survives_pricing_schema_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "wafer.json"
    native_ids = {
        "z-ai/glm-5.1": "GLM-5.1",
        "z-ai/glm-5.2": "GLM-5.2",
        "z-ai/glm-5.2-fast": "glm5.2-fast",
        "moonshotai/kimi-k2.6": "Kimi-K2.6",
        "minimax/minimax-m3": "MiniMax-M3",
    }
    _write_manifest(
        manifest_path,
        "wafer",
        [
            _manifest_row(
                model_id,
                native_id,
                input_token_price_per_m=index + 100,
                output_token_price_per_m=index + 200,
            )
            for index, (model_id, native_id) in enumerate(native_ids.items())
        ],
    )
    payload_rows: list[dict[str, object]] = []
    for index, native_id in enumerate(native_ids.values()):
        pricing_key = "renamed_pricing" if index < 2 else "pricing"
        payload_rows.append(
            {
                "id": native_id,
                "wafer": {
                    pricing_key: {
                        "input_cents_per_million": 10 + index,
                        "output_cents_per_million": 20 + index,
                    }
                },
            }
        )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": payload_rows}

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(wafer, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(wafer.httpx, "Client", FakeClient)

    result = wafer.fetch()
    wafer.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert set(by_id) == set(native_ids)
    assert set(result.prices) == set(native_ids) - {"z-ai/glm-5.1", "z-ai/glm-5.2"}
    assert by_id["z-ai/glm-5.1"]["input_token_price_per_m"] == 100
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 101
    assert all("missing_since" not in row for row in by_id.values())
    assert all(row.get("routable") is not False for row in by_id.values())


def test_gemini_paginates_complete_feed_and_rejects_stuck_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "gemini.json"
    _write_manifest(
        manifest_path,
        "gemini",
        [
            _manifest_row("google/gemini-2.5-pro", "gemini-2.5-pro"),
            _manifest_row("google/gemini-2.5-flash", "gemini-2.5-flash"),
            _manifest_row("google/gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
        ],
    )
    pricing = ProviderPricingResult(
        slug="gemini",
        prices={
            "google/gemini-2.5-pro": ModelPrice(1, 2),
            "google/gemini-2.5-flash": ModelPrice(3, 4),
        },
        source="deterministic",
    )
    requested_urls: list[str] = []

    def paginated_json(url: str, **_kwargs: object) -> dict[str, object]:
        requested_urls.append(url)
        if "pageToken=" not in url:
            return {
                "models": [{"name": "models/gemini-2.5-pro"}],
                "nextPageToken": "page two",
            }
        return {"models": [{"name": "models/gemini-2.5-flash"}]}

    monkeypatch.setattr(gemini, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(gemini, "fetch_provider", lambda **_kwargs: pricing)
    monkeypatch.setattr(gemini, "fetch_json", paginated_json)

    result = gemini.fetch()
    gemini.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert set(result.prices) == {
        "google/gemini-2.5-pro",
        "google/gemini-2.5-flash",
    }
    assert by_id["google/gemini-2.5-flash"].get("routable") is not False
    assert "missing_since" not in by_id["google/gemini-2.5-flash"]
    assert "pageSize=1000" in requested_urls[0]
    assert "pageToken=page+two" in requested_urls[1]

    monkeypatch.setattr(
        gemini,
        "fetch_json",
        lambda *_args, **_kwargs: {
            "models": [{"name": "models/gemini-2.5-pro"}],
            "nextPageToken": "stuck",
        },
    )
    with pytest.raises(RuntimeError, match="pagination token did not advance"):
        gemini._live_model_rows()  # noqa: SLF001


def test_awaiting_price_auto_promotes_but_curated_false_row_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "friendli.json"
    curated = _manifest_row(
        "metadata/curated",
        "curated",
        routable=False,
        _note="hands off",
    )
    _write_manifest(
        manifest_path,
        "friendli",
        [
            _manifest_row("z-ai/glm-5.2", "zai-org/GLM-5.2"),
            curated,
        ],
    )
    discovered = {
        "z-ai/glm-5.2": {
            "id": "z-ai/glm-5.2",
            "upstream_id": "zai-org/GLM-5.2",
        },
        "meta-llama/llama-3.1-8b-instruct": {
            "id": "meta-llama/llama-3.1-8b-instruct",
            "upstream_id": "meta-llama-3.1-8b-instruct",
        },
        "metadata/curated": {
            "id": "metadata/curated",
            "upstream_id": "changed-by-feed",
        },
    }
    first = ProviderPricingResult(
        slug="friendli",
        prices={"z-ai/glm-5.2": ModelPrice(10, 20)},
        source="api",
    )
    monkeypatch.setattr(friendli, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(friendli, "_DISCOVERED_MANIFEST_ROWS", discovered)

    friendli.write_provider_manifest(first)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    awaiting = by_id["meta-llama/llama-3.1-8b-instruct"]
    assert awaiting["routable"] is False
    assert awaiting["routable_reason"] == "awaiting-price"
    assert by_id["metadata/curated"] == curated

    second = ProviderPricingResult(
        slug="friendli",
        prices={
            "z-ai/glm-5.2": ModelPrice(10, 20),
            "meta-llama/llama-3.1-8b-instruct": ModelPrice(30, 40),
        },
        source="api",
    )
    friendli.write_provider_manifest(second)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    promoted = by_id["meta-llama/llama-3.1-8b-instruct"]
    assert promoted["routable"] is True
    assert "routable_reason" not in promoted
    assert promoted["input_token_price_per_m"] == 30
    assert by_id["metadata/curated"] == curated


def test_routable_false_supplemental_row_produces_no_catalog_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(
        tmp_path / "friendli.json",
        "friendli",
        [
            _manifest_row(
                "z-ai/metadata-only",
                "metadata-only",
                routable=False,
            )
        ],
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()

    assert "z-ai/metadata-only" not in models
    assert not [endpoint for endpoint in endpoints.values() if endpoint.provider == "friendli"]
