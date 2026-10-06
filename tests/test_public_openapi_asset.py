from __future__ import annotations

import gzip
import importlib.util
import json
import warnings
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.route_inventory import effective_routes
from trusted_router import main
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.compat import register_gateway_compat_stub_routes
from trusted_router.routes.inference import register_inference_routes

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts" / "generate_public_openapi.py"
JSON_PATH = ROOT / "src" / "trusted_router" / "static" / "openapi-public.json"
GZIP_PATH = ROOT / "src" / "trusted_router" / "static" / "openapi-public.json.gz"


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location("generate_public_openapi", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public_app() -> FastAPI:
    return create_app(
        Settings(environment="test", service_surface="public"),
        configure_store_arg=False,
        init_observability=False,
    )


def _component_refs(value: object) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            _, _, section, name = ref.split("/", 3)
            found.add((section, name.replace("~1", "/").replace("~0", "~")))
        for nested in value.values():
            found.update(_component_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_component_refs(nested))
    return found


def test_generated_public_openapi_assets_are_deterministic_and_current(monkeypatch) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-clickhouse")
    monkeypatch.setenv("TR_SPANNER_INSTANCE_ID", "must-not-be-constructed")
    monkeypatch.setenv("TR_STRIPE_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("AXIOM_API_TOKEN", "must-not-connect")
    body, gzip_body = _generator().generated_bytes()

    assert body == JSON_PATH.read_bytes()
    # The JSON is byte-exact across runtimes; the deflate stream is not. zlib
    # builds differ between the Mac that commits the asset and the Linux CI
    # that checks it, so the compressed bytes are compared by what they
    # decompress to, plus the two header fields the generator pins (mtime=0,
    # OS=255) that make the stream reproducible apart from the codec itself.
    committed_gzip = GZIP_PATH.read_bytes()
    assert gzip.decompress(committed_gzip) == body
    assert gzip.decompress(gzip_body) == body
    for stream in (committed_gzip, gzip_body):
        assert stream[:2] == b"\x1f\x8b"
        assert stream[4:8] == b"\x00\x00\x00\x00", "gzip mtime must be pinned to 0"
        assert stream[9] == 0xFF, "gzip OS byte must be pinned to 255"


def test_public_openapi_asset_is_sanitized_and_reference_closed() -> None:
    body = JSON_PATH.read_bytes()
    schema = json.loads(body)
    paths = schema["paths"]

    assert not any(path.startswith(("/internal/", "/v1/internal/")) for path in paths)
    assert {"/v1/models", "/v1/keys", "/mcp", "/v1/chat/completions"} <= set(paths)
    lowered = body.lower()
    for forbidden in (
        b"internal_gateway_token",
        b"observer_internal_token",
        b"stripe_secret_key",
        b"stripe_webhook_secret",
        b"aws_secret_access_key",
        b"client_secret",
        b"x-trustedrouter-internal",
    ):
        assert forbidden not in lowered
    components = schema.get("components", {})
    for section, name in _component_refs(schema):
        assert name in components[section]


def _operations(router: APIRouter | FastAPI) -> set[tuple[str, str]]:
    return {
        (path.replace(route.path, route.path_format), method.lower())
        for path, route in effective_routes(router)
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in route.methods
    }


def _expected_servers(gateway: bool, prefix: str) -> list[dict[str, str]]:
    if not gateway:
        return [{"url": "https://trustedrouter.com" + prefix}]
    return [
        {"url": "https://api.trustedrouter.com" + prefix, "description": "Global"},
        {"url": "https://api-europe-west4.quillrouter.com" + prefix, "description": "EU regional"},
    ]


def test_every_public_operation_has_the_server_for_its_registered_surface() -> None:
    settings = Settings(environment="test", service_surface="combined", _env_file=None)
    gateway = APIRouter()
    register_inference_routes(gateway)
    register_gateway_compat_stub_routes(gateway)
    gateway_operations = _operations(gateway) | {("/models", "get")}
    app = create_app(settings, configure_store_arg=False, init_observability=False)
    registered = {
        (path.replace(route.path, route.path_format), method.lower(), route.endpoint)
        for path, route in effective_routes(app)
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in route.methods
    }
    # API handlers are mounted bare and under /v1, including OAuth handlers
    # added after _make_api_router. Match endpoints as well as paths so a
    # same-named website page (e.g. /benchmarks) is not mistaken for an API.
    api_operations = {
        (path, method)
        for path, method, endpoint in registered
        if (f"/v1{path}", method, endpoint) in registered
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        dynamic = app.openapi()
    asset = json.loads(JSON_PATH.read_bytes())

    for schema in (dynamic, asset):
        checked: set[tuple[str, str]] = set()
        for path, path_item in schema["paths"].items():
            if path.startswith(("/internal/", "/v1/internal/")):
                continue
            for method, operation in path_item.items():
                if method not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                    continue
                bare_path = path.removeprefix("/v1") if path.startswith("/v1/") else path
                prefix = "/v1" if (path, method) in api_operations else ""
                assert operation["servers"] == _expected_servers(
                    (bare_path, method) in gateway_operations, prefix
                ), (path, method)
                # Assert the actual URL an OpenAPI client constructs, not just
                # the host: duplicated /v1 would still send customers to 404s.
                for server in operation["servers"]:
                    assert "/v1/v1/" not in server["url"] + path
                checked.add((path, method))
        expected = {
            (path, method)
            for path, method in _operations(app)
            if not path.startswith(("/internal/", "/v1/internal/"))
        }
        assert checked == expected


def test_new_registered_operations_inherit_their_surface_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_keys = main.register_key_routes
    register_inference = main.register_inference_routes

    def account_routes(router: APIRouter) -> None:
        register_keys(router)

        @router.get("/surface-example")
        def account() -> dict[str, str]:
            return {"surface": "control"}

    def inference_routes(router: APIRouter) -> None:
        register_inference(router)

        @router.post("/surface-example")
        def inference() -> dict[str, str]:
            return {"surface": "inference"}

    monkeypatch.setattr(main, "register_key_routes", account_routes)
    monkeypatch.setattr(main, "register_inference_routes", inference_routes)
    app = create_app(
        Settings(environment="test", service_surface="combined", _env_file=None),
        configure_store_arg=False,
        init_observability=False,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        schema = app.openapi()
    assert app.openapi() is schema
    client = TestClient(app)
    for prefix in ("", "/v1"):
        path = f"{prefix}/surface-example"
        for method, gateway, surface in (
            ("get", False, "control"),
            ("post", True, "inference"),
        ):
            assert schema["paths"][path][method]["servers"] == _expected_servers(
                gateway, "" if prefix else "/v1"
            )
            response = client.request(method, path)
            assert response.status_code == 200
            assert response.json() == {"surface": surface}


def test_public_openapi_uses_static_representation_without_calling_app_openapi(
    monkeypatch,
) -> None:
    def forbidden(_self: FastAPI) -> dict[str, Any]:
        raise AssertionError("public request generated OpenAPI at runtime")

    monkeypatch.setattr(FastAPI, "openapi", forbidden)
    client = TestClient(_public_app())

    identity = client.get("/openapi.json", headers={"Accept-Encoding": "identity"})
    compressed = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    head = client.head("/openapi.json", headers={"Accept-Encoding": "identity"})
    not_modified = client.get(
        "/openapi.json",
        headers={"Accept-Encoding": "identity", "If-None-Match": identity.headers["etag"]},
    )

    assert identity.status_code == 200
    assert identity.content == JSON_PATH.read_bytes()
    assert identity.headers["cache-control"].startswith("public,")
    assert compressed.status_code == 200
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.content == identity.content
    assert compressed.headers["etag"] != identity.headers["etag"]
    assert head.status_code == 200
    assert head.content == b""
    assert not_modified.status_code == 304
    assert not_modified.content == b""


def test_public_openapi_documents_key_paging_and_bulk_delete() -> None:
    schema = json.loads(JSON_PATH.read_bytes())
    listing = schema["paths"]["/v1/keys"]["get"]
    params = {param["name"]: param["schema"] for param in listing["parameters"]}
    limit = next(value for value in params["limit"]["anyOf"] if value["type"] == "integer")
    assert limit["minimum"] == 1
    assert limit["maximum"] == 1000
    assert params["offset"]["minimum"] == 0
    assert params["include_disabled"]["default"] is True
    assert "next_offset" in listing["description"]
    assert "400" in listing["responses"]
    assert "422" not in listing["responses"]
    bulk = schema["paths"]["/v1/keys/bulk-delete"]["post"]
    assert "400" in bulk["responses"]
    assert "422" not in bulk["responses"]
    body = bulk["requestBody"]["content"]["application/json"]["schema"]
    assert body["$ref"] == "#/components/schemas/BulkDeleteKeysRequest"
    hashes = schema["components"]["schemas"]["BulkDeleteKeysRequest"]["properties"]["hashes"]
    assert hashes["minItems"] == 1
    assert hashes["maxItems"] == 1000
    assert hashes["items"] == {"type": "string"}
