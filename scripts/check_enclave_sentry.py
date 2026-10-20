"""Fail-closed Sentry source/dependency guard; not a whole-image SBOM audit."""

from __future__ import annotations

import os
from pathlib import Path


def check_enclave(enclave: Path) -> None:
    for required in ("go.mod", "cmd/enclave/main.go", "sidecar/go.mod", "Dockerfile.enclave.gcp.multi"):
        if not (enclave / required).is_file():
            raise AssertionError(
                f"Missing enclave source: {enclave / required}. Check out quill-cloud-proxy "
                "beside quill-router or set TR_ENCLAVE_SOURCE_ROOT to its enclave-go directory."
            )
    for path in enclave.rglob("*"):
        if path.is_symlink():
            raise AssertionError(f"Security scan refuses symlink: {path}")
        if path.is_file() and (
            path.suffix == ".go"
            or path.name.startswith("Dockerfile")
            or path.name in {"go.mod", "go.sum", "modules.txt"}
        ):
            content = path.read_text(encoding="utf-8").lower()
            # Match package/configuration identifiers, not prose comments or
            # unrelated identifiers such as publicModelEndpointsEntryMaxBytes.
            for forbidden in ("github.com/getsentry/", "sentry.io", "sentry_dsn", "58539b11263132bcb70ea30f0b92e0f4"):
                if forbidden in content:
                    raise AssertionError(f"Forbidden Sentry dependency/configuration in {path}")


def configured_enclave() -> Path:
    configured = os.environ.get("TR_ENCLAVE_SOURCE_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "quill-cloud-proxy" / "enclave-go"


if __name__ == "__main__":
    check_enclave(configured_enclave())
