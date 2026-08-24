from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import catalog as catalog_routes


def test_v1_models_builds_expensive_shapes_once(monkeypatch) -> None:
    catalog_routes._public_catalog_payload.cache_clear()
    calls = 0
    original = catalog_routes.model_to_openrouter_shape

    def counted(model):
        nonlocal calls
        calls += 1
        return original(model)

    monkeypatch.setattr(catalog_routes, "model_to_openrouter_shape", counted)
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))

    first = client.get("/v1/models")
    second = client.get("/v1/models")

    assert first.status_code == 200
    assert second.content == first.content
    assert calls == len(MODELS)


def test_v1_models_is_publicly_cacheable_and_revalidates_with_etag(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300, stale-while-revalidate=60"
    )
    etag = response.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')

    unchanged = client.get("/v1/models", headers={"if-none-match": etag})

    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag

    weak_validator = client.get(
        "/v1/models",
        headers={"if-none-match": f"W/{etag}"},
    )
    assert weak_validator.status_code == 304
    schema = client.get("/openapi.json").json()["paths"]["/v1/models"]["get"]
    assert schema["responses"]["200"]["content"]["application/json"]["schema"] == {
        "additionalProperties": {
            "items": {"additionalProperties": True, "type": "object"},
            "type": "array",
        },
        "type": "object",
        "title": "Response Models V1 Models Get",
    }


def test_v1_models_varies_strong_etag_by_content_encoding(client: TestClient) -> None:
    compressed = client.get("/v1/models")
    identity = client.get("/v1/models", headers={"accept-encoding": "identity"})

    assert compressed.content == identity.content
    assert compressed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in identity.headers
    assert compressed.headers["etag"] != identity.headers["etag"]
    assert compressed.headers["vary"] == "Accept-Encoding"
    assert identity.headers["vary"] == "Accept-Encoding"

    wrong_representation = client.get(
        "/v1/models",
        headers={
            "accept-encoding": "identity",
            "if-none-match": compressed.headers["etag"],
        },
    )
    assert wrong_representation.status_code == 200

    gzip_alias = client.get(
        "/v1/models",
        headers={"accept-encoding": "x-gzip, identity;q=0"},
    )
    assert gzip_alias.headers["content-encoding"] == "gzip"
    assert gzip_alias.headers["etag"] == compressed.headers["etag"]


def test_picker_projection_preserves_picker_contract_at_a_fraction_of_the_wire_size(
    client: TestClient,
) -> None:
    full = client.get("/v1/models")
    picker = client.get("/v1/models/picker")

    assert picker.status_code == 200
    assert picker.headers["cache-control"] == full.headers["cache-control"]
    full_rows = full.json()["data"]
    picker_rows = picker.json()["data"]
    assert [row["id"] for row in picker_rows] == [row["id"] for row in full_rows]
    assert len(picker.content) < len(full.content) // 4
    assert int(picker.headers["content-length"]) < (
        int(full.headers["content-length"]) // 4
    )
    for row in picker_rows:
        assert set(row) == {
            "id",
            "name",
            "description",
            "context_length",
            "pricing",
            "trustedrouter",
        }
        assert set(row["pricing"]) == {"prompt", "completion"}
        assert set(row["trustedrouter"]) == {
            "capabilities",
            "uptime_pct",
            "open_weights",
            "us_provider_available",
            "eu_focused_provider_available",
            "internal_only",
            "route_kind",
            "supports_chat",
        }
    unchanged = client.get(
        "/v1/models/picker",
        headers={"if-none-match": picker.headers["etag"]},
    )
    assert unchanged.status_code == 304


def test_picker_projection_preserves_normalized_capability_metadata(
    client: TestClient,
) -> None:
    full_rows = client.get("/v1/models").json()["data"]
    picker_rows = client.get("/v1/models/picker").json()["data"]

    for full, picker in zip(full_rows, picker_rows, strict=True):
        assert picker == {
            "id": full.get("id"),
            "name": full.get("name"),
            "description": full.get("description"),
            "context_length": full.get("context_length"),
            "pricing": {
                "prompt": full.get("pricing", {}).get("prompt"),
                "completion": full.get("pricing", {}).get("completion"),
            },
            "trustedrouter": {
                "capabilities": full.get("trustedrouter", {}).get(
                    "capabilities", []
                ),
                "uptime_pct": full.get("trustedrouter", {}).get("uptime_pct"),
                "open_weights": full.get("trustedrouter", {}).get(
                    "open_weights", False
                ),
                "us_provider_available": full.get("trustedrouter", {}).get(
                    "us_provider_available", False
                ),
                "eu_focused_provider_available": full.get("trustedrouter", {}).get(
                    "eu_focused_provider_available", False
                ),
                "internal_only": full.get("trustedrouter", {}).get(
                    "internal_only", False
                ),
                "route_kind": full.get("trustedrouter", {}).get(
                    "route_kind", "model"
                ),
                "supports_chat": full.get("trustedrouter", {}).get(
                    "supports_chat", True
                ),
            },
        }


def test_v1_models_honors_an_explicit_gzip_rejection(client: TestClient) -> None:
    response = client.get(
        "/v1/models",
        headers={"accept-encoding": "br, gzip ; q = 0, identity;q=1"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "identity"


def test_filtered_catalog_uses_cached_shapes_and_its_own_validator(client: TestClient) -> None:
    full = client.get("/v1/models")
    filtered = client.get("/v1/models?open_weights=true")

    assert filtered.status_code == 200
    rows = filtered.json()["data"]
    assert rows
    assert all(row["trustedrouter"]["open_weights"] for row in rows)
    assert filtered.headers["etag"] != full.headers["etag"]
    unchanged = client.get(
        "/v1/models?open_weights=true",
        headers={"if-none-match": filtered.headers["etag"]},
    )
    assert unchanged.status_code == 304


def test_irrelevant_query_parameters_keep_the_prebuilt_full_payload(client: TestClient) -> None:
    full = client.get("/v1/models")
    tracked = client.get("/v1/models?utm_source=docs")

    assert tracked.content == full.content
    assert tracked.headers["etag"] == full.headers["etag"]


def test_browser_pickers_use_projection_and_console_defers_fetch_until_open() -> None:
    static = Path(__file__).parents[1] / "src" / "trusted_router" / "static"
    shared = (static / "model_catalog.js").read_text()
    custom = (static / "custom_models.js").read_text()

    assert 'fetch(base + "/models/picker")' in shared
    assert custom.count("load();") == 1
    assert "if (nameEl && !nameEl.textContent.trim())" in custom
