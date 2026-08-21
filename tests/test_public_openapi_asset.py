from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app

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
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
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
