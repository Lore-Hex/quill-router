"""GET /v1/models leaves per-route detail out of the default list.

On 2026-10-10 the list was 4,249,828 bytes for 656 models and OpenClaw, which
caps a decoded provider catalog at 4 MiB, showed TrustedRouter as unavailable.
trustedrouter.endpoints was 2.87 MB of it. The list now omits that array and
the model-level provider_policy prose. ?include=endpoints returns the complete
rows, and GET /v1/models/{author}/{slug}/endpoints serves one model's routes
without a key.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes import catalog as catalog_routes

# OpenClaw rejects a provider catalog whose decoded body exceeds 4 MiB.
OPENCLAW_DECODED_CATALOG_CAP = 4 * 1024 * 1024
# Half that cap keeps room for catalog growth (617 models on 2026-08-24, 657
# on 2026-10-12). The per-row figure fails if per-route arrays come back,
# whatever the model count; rows averaged about 2.1 KB when this was written.
DEFAULT_LIST_BUDGET_BYTES = OPENCLAW_DECODED_CATALOG_CAP // 2
DEFAULT_LIST_BUDGET_BYTES_PER_ROW = 2_560
OMITTED = {"endpoints", "provider_policy"}
IDENTITY = {"accept-encoding": "identity"}


def _rebuilt_pre_projection_body() -> bytes:
    """The list body as built before the projection, from the same inputs."""
    rows: list[dict[str, Any]] = []
    last_changes = catalog_routes.last_route_changes(
        catalog_routes.timestamp(catalog_routes._utc_now())
    )
    for model in catalog_routes.MODELS.values():
        shape = catalog_routes.model_to_openrouter_shape(model)
        trustedrouter = shape.get("trustedrouter")
        if isinstance(trustedrouter, dict) and trustedrouter.get("internal_only"):
            continue
        if isinstance(trustedrouter, dict):
            trustedrouter["last_route_change_at"] = last_changes.get(model.id)
        rows.append(shape)
    return json.dumps(
        {"data": rows}, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _without_omitted(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "trustedrouter": {
            key: value for key, value in row["trustedrouter"].items() if key not in OMITTED
        },
    }


def test_default_list_omits_only_per_route_detail_and_fits_the_size_budget(
    client: TestClient,
) -> None:
    default = client.get("/v1/models", headers=IDENTITY)
    full = client.get("/v1/models?include=endpoints", headers=IDENTITY)

    assert default.status_code == 200
    assert "content-encoding" not in default.headers
    rows = default.json()["data"]
    full_rows = full.json()["data"]
    assert [row["id"] for row in rows] == [row["id"] for row in full_rows]
    assert any(row["trustedrouter"].get("endpoints") for row in full_rows)
    for row, full_row in zip(rows, full_rows, strict=True):
        assert not OMITTED & set(row["trustedrouter"]), row["id"]
        # Every OpenRouter-standard field and every other trustedrouter
        # field is unchanged, value for value and in order.
        assert list(row) == list(full_row)
        assert row == _without_omitted(full_row), row["id"]

    size = len(default.content)
    assert int(default.headers["content-length"]) == size
    assert size < DEFAULT_LIST_BUDGET_BYTES, size
    assert size <= DEFAULT_LIST_BUDGET_BYTES_PER_ROW * len(rows), (size, len(rows))
    assert len(full.content) > OPENCLAW_DECODED_CATALOG_CAP // 2


def test_include_endpoints_is_the_pre_projection_list_byte_for_byte(
    client: TestClient,
) -> None:
    catalog_routes._public_catalog_payload.cache_clear()
    expected = _rebuilt_pre_projection_body()

    identity = client.get("/v1/models?include=endpoints", headers=IDENTITY)
    compressed = client.get(
        "/v1/models?include=endpoints", headers={"accept-encoding": "gzip"}
    )

    assert identity.status_code == 200
    assert identity.content == expected
    assert identity.headers["etag"] == f'"{hashlib.sha256(expected).hexdigest()}"'
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.content == expected
    assert compressed.headers["etag"] != identity.headers["etag"]
    assert compressed.headers["vary"] == "Accept-Encoding"
    assert int(compressed.headers["content-length"]) < len(expected) // 10


@pytest.mark.parametrize(
    "query",
    [
        "include=endpoints",
        "include=ENDPOINTS",
        "include=unknown,endpoints",
        "include=unknown&include=endpoints",
    ],
)
def test_include_endpoints_spellings_select_the_complete_rows(
    client: TestClient,
    query: str,
) -> None:
    full = client.get("/v1/models?include=endpoints", headers=IDENTITY)
    response = client.get(f"/v1/models?{query}", headers=IDENTITY)

    assert response.content == full.content
    assert response.headers["etag"] == full.headers["etag"]


@pytest.mark.parametrize("query", ["include=", "include=unknown", "include=endpoint"])
def test_other_include_values_keep_the_default_list(client: TestClient, query: str) -> None:
    default = client.get("/v1/models", headers=IDENTITY)
    response = client.get(f"/v1/models?{query}", headers=IDENTITY)

    assert response.content == default.content
    assert response.headers["etag"] == default.headers["etag"]


def test_full_and_default_lists_revalidate_with_their_own_validators(
    client: TestClient,
) -> None:
    default = client.get("/v1/models", headers=IDENTITY)
    full = client.get("/v1/models?include=endpoints", headers=IDENTITY)

    assert default.headers["etag"] != full.headers["etag"]
    assert (
        client.get(
            "/v1/models?include=endpoints",
            headers={**IDENTITY, "if-none-match": full.headers["etag"]},
        ).status_code
        == 304
    )
    assert (
        client.get(
            "/v1/models?include=endpoints",
            headers={**IDENTITY, "if-none-match": default.headers["etag"]},
        ).status_code
        == 200
    )


def test_filtered_lists_apply_the_same_projection(client: TestClient) -> None:
    full_rows = client.get("/v1/models?include=endpoints", headers=IDENTITY).json()["data"]
    filtered = client.get("/v1/models?open_weights=true")
    filtered_full = client.get(
        "/v1/models?open_weights=true&include=endpoints", headers=IDENTITY
    )

    assert filtered.headers["content-encoding"] == "gzip"
    expected_full = [row for row in full_rows if row["trustedrouter"]["open_weights"]]
    assert expected_full
    assert filtered_full.json()["data"] == expected_full
    assert filtered.json()["data"] == [_without_omitted(row) for row in expected_full]
    assert filtered.headers["etag"] != filtered_full.headers["etag"]


def test_default_list_gzip_round_trips_to_the_identity_bytes(client: TestClient) -> None:
    identity = client.get("/v1/models", headers=IDENTITY)
    with client.stream("GET", "/v1/models", headers={"accept-encoding": "gzip"}) as response:
        raw = b"".join(response.iter_raw())
        headers = response.headers

    assert headers["content-encoding"] == "gzip"
    assert headers["vary"] == "Accept-Encoding"
    assert int(headers["content-length"]) == len(raw)
    assert gzip.decompress(raw) == identity.content
    assert len(raw) < len(identity.content) // 10


def test_openapi_documents_the_include_parameter(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/v1/models"]["get"]

    assert operation["parameters"] == [
        {
            "name": "include",
            "in": "query",
            "required": False,
            "description": (
                "endpoints: also return trustedrouter.endpoints and "
                "trustedrouter.provider_policy for every model."
            ),
            "schema": {"type": "string", "enum": ["endpoints"]},
        }
    ]
    assert "?include=endpoints" in operation["description"]


@pytest.fixture
def unthrottled_client(test_settings: Settings) -> TestClient:
    # One request per public model; the per-IP limit would otherwise answer 429.
    settings = test_settings.model_copy(update={"rate_limit_enabled": False})
    return TestClient(create_app(settings, init_observability=False))


def test_public_endpoints_route_serves_every_listed_route_without_a_key(
    unthrottled_client: TestClient,
) -> None:
    full_rows = unthrottled_client.get("/v1/models?include=endpoints").json()["data"]
    checked = 0
    for row in full_rows:
        embedded = row["trustedrouter"]["endpoints"]
        response = unthrottled_client.get(f"/v1/models/{row['id']}/endpoints")
        assert response.status_code == 200, (row["id"], response.text)
        served = {endpoint["endpoint_id"]: endpoint for endpoint in response.json()["data"]}
        assert set(served) == {endpoint["id"] for endpoint in embedded}, row["id"]
        for endpoint in embedded:
            route = served[endpoint["id"]]
            # Each embedded fact is served at the top level or under
            # trustedrouter, with the same value.
            for key, value in endpoint.items():
                if key == "id":
                    continue
                if key in route and key != "trustedrouter":
                    assert route[key] == value, (row["id"], endpoint["id"], key)
                else:
                    assert route["trustedrouter"][key] == value, (row["id"], endpoint["id"], key)
            checked += 1
    assert checked > 1_000


def test_public_endpoints_route_does_not_depend_on_the_caller(
    client: TestClient,
    inference_headers: dict[str, str],
) -> None:
    model_id = "openai/gpt-4o-mini"
    anonymous = client.get(f"/v1/models/{model_id}/endpoints", headers=IDENTITY)
    keyed = client.get(
        f"/v1/models/{model_id}/endpoints", headers={**IDENTITY, **inference_headers}
    )
    invalid = client.get(
        f"/v1/models/{model_id}/endpoints",
        headers={**IDENTITY, "authorization": "Bearer sk-tr-v1-not-a-real-key"},
    )

    assert anonymous.status_code == 200
    assert anonymous.json()["data"]
    assert keyed.status_code == 200
    assert invalid.status_code == 200
    assert keyed.content == anonymous.content
    assert invalid.content == anonymous.content


def test_public_endpoints_route_is_gzip_encoded_on_request(client: TestClient) -> None:
    identity = client.get("/v1/models/openai/gpt-4o-mini/endpoints", headers=IDENTITY)
    compressed = client.get(
        "/v1/models/openai/gpt-4o-mini/endpoints", headers={"accept-encoding": "gzip"}
    )

    assert "content-encoding" not in identity.headers
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.content == identity.content
