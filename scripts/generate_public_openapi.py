#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "src" / "trusted_router" / "static" / "openapi-public.json"
GZIP_PATH = ROOT / "src" / "trusted_router" / "static" / "openapi-public.json.gz"
_FORBIDDEN_PUBLIC_SCHEMA_FRAGMENTS = (
    b"internal_gateway_token",
    b"observer_internal_token",
    b"stripe_secret_key",
    b"stripe_webhook_secret",
    b"aws_secret_access_key",
    b"client_secret",
    b"x-trustedrouter-internal",
)
_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})


def _component_refs(value: object) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            parts = ref.split("/", 3)
            if len(parts) == 4:
                yield parts[2], parts[3].replace("~1", "/").replace("~0", "~")
        for nested in value.values():
            yield from _component_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _component_refs(nested)


def _security_scheme_names(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        security = value.get("security")
        if isinstance(security, list):
            for requirement in security:
                if isinstance(requirement, dict):
                    yield from (name for name in requirement if isinstance(name, str))
        for nested in value.values():
            yield from _security_scheme_names(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _security_scheme_names(nested)


def _prune_components(schema: dict[str, Any]) -> None:
    raw_components = schema.pop("components", {})
    if not isinstance(raw_components, dict):
        return
    pending = list(_component_refs(schema))
    pending.extend(("securitySchemes", name) for name in _security_scheme_names(schema))
    reachable: set[tuple[str, str]] = set()
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        section, name = key
        section_values = raw_components.get(section)
        if not isinstance(section_values, dict) or name not in section_values:
            raise ValueError(f"dangling OpenAPI component reference: {section}/{name}")
        reachable.add(key)
        pending.extend(_component_refs(section_values[name]))
        pending.extend(
            ("securitySchemes", nested)
            for nested in _security_scheme_names(section_values[name])
        )
    pruned: dict[str, dict[str, Any]] = {}
    for section, name in sorted(reachable):
        section_values = raw_components[section]
        assert isinstance(section_values, dict)
        pruned.setdefault(section, {})[name] = section_values[name]
    if pruned:
        schema["components"] = pruned


def _canonicalize_operation_ids(paths: dict[str, Any]) -> None:
    for path, path_shape in paths.items():
        if not isinstance(path_shape, dict):
            continue
        path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_") or "root"
        for method, operation in path_shape.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation["operationId"] = f"{method.lower()}_{path_slug}"


def build_public_schema() -> dict[str, Any]:
    # Importing trusted_router.main normally constructs its ASGI app. Isolate
    # that one import from ambient shell/.env credentials and force the memory
    # test backend so schema generation cannot initialize a cloud Store or
    # telemetry client. The explicit factory call below also disables both.
    saved_environment = dict(os.environ)
    saved_cwd = Path.cwd()
    try:
        for name in tuple(os.environ):
            if name.startswith(("TR_", "AXIOM_")):
                os.environ.pop(name, None)
        os.environ.update(
            TR_ENVIRONMENT="test",
            TR_SERVICE_SURFACE="combined",
            TR_STORAGE_BACKEND="memory",
        )
        os.chdir(ROOT / "scripts")
        from trusted_router.config import Settings
        from trusted_router.main import create_app

        schema_settings = Settings(
            environment="test",
            service_surface="combined",
            storage_backend="memory",
            _env_file=None,
        )
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_environment)

    app = create_app(
        schema_settings,
        configure_store_arg=False,
        init_observability=False,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        schema = app.openapi()
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("combined OpenAPI schema has no paths object")
    schema["paths"] = {
        path: shape
        for path, shape in paths.items()
        if not path.startswith(("/internal/", "/v1/internal/"))
    }
    _canonicalize_operation_ids(schema["paths"])
    _prune_components(schema)
    # Prove the final graph is closed after pruning.
    components = schema.get("components", {})
    for section, name in _component_refs(schema):
        section_values = components.get(section) if isinstance(components, dict) else None
        if not isinstance(section_values, dict) or name not in section_values:
            raise ValueError(f"unresolved final OpenAPI component: {section}/{name}")
    return schema


def generated_bytes() -> tuple[bytes, bytes]:
    body = json.dumps(
        build_public_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    lowered = body.lower()
    leaked = [
        fragment.decode("ascii")
        for fragment in _FORBIDDEN_PUBLIC_SCHEMA_FRAGMENTS
        if fragment in lowered
    ]
    if leaked:
        raise ValueError("private credential names leaked into public OpenAPI: " + ", ".join(leaked))
    compressed = bytearray(gzip.compress(body, compresslevel=6, mtime=0))
    # mtime=0 alone is not byte-stable: the gzip header's OS byte (offset 9)
    # differs across Python runtimes (3.12 wrote 19, 3.14 writes 255), which
    # would fail the byte-exact drift test under a different interpreter than
    # the one that committed the asset. Pin it to 255 ("unknown").
    compressed[9] = 0xFF
    return body, bytes(compressed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    body, gzip_body = generated_bytes()
    if args.check:
        if JSON_PATH.read_bytes() != body or GZIP_PATH.read_bytes() != gzip_body:
            raise SystemExit("public OpenAPI assets are stale; run this generator")
        return 0
    JSON_PATH.write_bytes(body)
    GZIP_PATH.write_bytes(gzip_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
