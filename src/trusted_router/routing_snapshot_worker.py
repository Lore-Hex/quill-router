"""Compiles one routing snapshot as of its window's start, in a process of its own.

``python -m trusted_router.routing_snapshot_worker`` reads one request (JSON)
on stdin and writes one answer (JSON) on stdout: ``{"body": <base64 of the
canonical bytes>, "manifest": <JWS>}`` or ``{"refused": <reason>}``, or
``{"error": <exception class>}`` with exit status 1. The
publisher runs it for each window (services/routing_snapshot_publisher.py) and
names the mounted signing key's file, kid and public key; the key itself never
crosses the pipe, and nothing here writes it anywhere.

The catalog registry is built once, when it is imported, and drops what is
retired at that instant; the catalog freshness clock decides the privacy pools
behind the requested-model expansions. A serving process imported before a
retirement therefore holds endpoints, models, pool members and expansions that
a process imported after it does not. This process pins both clocks to the
window's start ``at`` before anything imports the catalog, so the snapshot is
what a process imported at ``at`` compiles: the same bytes whichever instance
asks, cold or warm, and the serving process's catalog is never touched.

Before the pins, this module imports only the two clock modules; everything
else is imported after, and the registry's import instant is checked.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime
from typing import Any


def compile_as_of(request: dict[str, Any]) -> dict[str, str]:
    at = request["at"]
    if type(at) is not int or at < 0:
        raise ValueError("at must be integer seconds")
    instant = datetime.fromtimestamp(at, UTC)

    from trusted_router import catalog_data, provider_lifecycle

    if "trusted_router.catalog_registry" in sys.modules:
        raise RuntimeError("the catalog was imported before its clocks were pinned")
    # Both clocks, for this process only: the registry's import instant
    # (lifecycle) and catalog freshness (the expansions' privacy pools).
    provider_lifecycle._utc_now = lambda: instant
    catalog_data._utc_now = lambda: instant

    from trusted_router import routing_snapshot as rs
    from trusted_router.catalog_registry import CATALOG_RESOLVED_AT
    from trusted_router.config import Settings

    if CATALOG_RESOLVED_AT != instant:
        raise RuntimeError("the catalog was not built as of the window's start")
    # Routing reads one setting while expanding requested models.
    settings = Settings.model_construct(auto_model_order=request["auto_model_order"])
    try:
        inputs = rs.CatalogInputs.live(settings)
        snapshot = rs.compile_routing_snapshot(
            at=at, horizon=request["horizon"],
            settings=rs.SnapshotSettings.model_validate(request["settings"]),
            source_revision=request["source_revision"],
            plane=rs.Plane.model_validate(request["plane"]), inputs=inputs,
        )
    except rs.SnapshotRefused as exc:
        return {"refused": str(exc)}
    body = rs.canonical_snapshot_bytes(snapshot)
    return {
        "body": base64.b64encode(body).decode("ascii"),
        "manifest": _signer(request["key"]).sign(body),
    }


def _signer(key: dict[str, str]) -> Any:
    """The publisher's signing key, from the same mounted file; refused unless pinned.

    The serving process checked the file, kid and public key against the other
    signing purposes when it started, and checks this manifest under its own
    trusted key; this only refuses a file that is not the pinned key.
    """
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from trusted_router.detached_jws import TrustedKey, b64encode
    from trusted_router.routing_snapshot_signing import PURPOSE, RoutingSnapshotSigner

    private = serialization.load_pem_private_key(Path(key["file"]).read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("routing snapshot key must be Ed25519")
    public = b64encode(private.public_key().public_bytes_raw())
    if public != key["public_key"]:
        raise ValueError("mounted routing snapshot key is not the pinned public key")
    return RoutingSnapshotSigner(
        private, TrustedKey(key["kid"], PURPOSE, public, key["iss"], key["aud"]),
    )


def main() -> int:
    """Answer on stdout; on failure ``{"error": <exception class>}`` and exit 1.

    Only the class: an exception's message could carry a path, a value or key
    material, and nothing here prints it, to stdout or stderr.
    """
    try:
        answer, status = compile_as_of(json.loads(sys.stdin.buffer.read())), 0
    except Exception as exc:  # the publisher logs the class and backs off
        answer, status = {"error": type(exc).__name__}, 1
    sys.stdout.write(json.dumps(answer, separators=(",", ":")))
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
