"""The published snapshot does not depend on when the publishing process imported its catalog.

docs/design/routing-snapshot-v1.md, "Publishing". The registry is built at
import and drops what is retired then, so a process imported before a
retirement R holds endpoints and models one imported after R does not. The
publisher compiles as of the window's start in a worker process; these tests
run a "warm" publisher (imported before R) and a "cold" one (imported after R)
in processes of their own, both after R, and hold their bytes to a process
imported exactly at the window's start (the definition of the as-of catalog).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trusted_router import provider_lifecycle
from trusted_router.catalog_ingest import provider_manifest_deadlines
from trusted_router.routing_snapshot import (
    EPOCH_SECONDS,
    publication_window_as_of,
)

ROOT = Path(__file__).resolve().parents[1]
HOUR = EPOCH_SECONDS
# Real retirements: DeepInfra's GPT-OSS-120B Turbo routes, Together's and
# OpenAI's October cutovers. Every process below pins its own clocks, so these
# hold whatever the wall clock or the session's lifecycle override says.
RETIREMENTS = {
    "deepinfra-2026-10-20": int(datetime(2026, 10, 20, tzinfo=UTC).timestamp()),
    "together-2026-10-22": int(provider_lifecycle.TOGETHER_OCTOBER_22_RETIREMENT_AT.timestamp()),
    "openai-2026-10-23": int(provider_lifecycle.OPENAI_OCTOBER_23_RETIREMENT_AT.timestamp()),
}

_PIN = """
import sys
from datetime import UTC, datetime
from trusted_router import catalog_data, provider_lifecycle
def pin(seconds):
    instant = datetime.fromtimestamp(seconds, UTC)
    provider_lifecycle._utc_now = lambda: instant
    catalog_data._utc_now = lambda: instant
"""

# A publishing process imported at argv[1], asked at argv[2]: its live
# registry's size, its own in-process compile of the window (which refuses
# when its import is stale), and what its publisher publishes.
_PUBLISHER = _PIN + """
import base64, hashlib, json
pin(int(sys.argv[1]))
from trusted_router import catalog_registry
from trusted_router import routing_snapshot as rs
from trusted_router.config import Settings
import tempfile
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.routing_snapshot_signing import PURPOSE, RoutingSnapshotSigner
from trusted_router.services.routing_snapshot_publisher import AsOfSource, SnapshotIdentity
now = int(sys.argv[2])
pin(now)
settings = Settings(environment="test", sentry_dsn=None, auto_model_order="")
identity = SnapshotIdentity(snapshot_settings=rs.snapshot_settings_from(settings),
                            plane=rs.Plane(cloud="gcp", region="us-central1"),
                            source_revision="as-of-test")
private = Ed25519PrivateKey.generate()
key_file = Path(tempfile.mkdtemp()) / "signing.pem"
key_file.write_bytes(private.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
signer = RoutingSnapshotSigner(private, TrustedKey(
    "as-of-test", PURPOSE, b64encode(private.public_key().public_bytes_raw()),
    "https://trustedrouter.com", "tr-fastpath-edge"))
plan = AsOfSource(identity, auto_model_order="", key_file=str(key_file)).plan(now)
try:
    rs.compile_routing_snapshot(at=plan.start, horizon=identity.horizon, settings=identity.snapshot_settings,
                                source_revision="as-of-test", plane=identity.plane,
                                inputs=rs.CatalogInputs.live(settings))
    in_process = "compiled"
except rs.SnapshotRefused:
    in_process = "refused"
signed = plan.compile(signer)
snapshot = rs.parse_routing_snapshot(signed.body)
print(json.dumps({
    "start": plan.start, "next_start": plan.next_start,
    "live_endpoints": len(catalog_registry.MODEL_ENDPOINTS), "live_models": len(catalog_registry.MODELS),
    "in_process": in_process,
    "digest": hashlib.sha256(signed.body).hexdigest(),
    "endpoints": len(snapshot.endpoints), "models": len(snapshot.models),
}))
"""

# The definition: a process whose clocks read the window's start (argv[1])
# when it imports the catalog and when it compiles.
_TRUTH = _PIN + """
import hashlib, json
pin(int(sys.argv[1]))
from trusted_router import routing_snapshot as rs
from trusted_router.config import Settings
settings = Settings(environment="test", sentry_dsn=None, auto_model_order="")
snapshot = rs.compile_routing_snapshot(
    at=int(sys.argv[1]), horizon=6 * 3600, settings=rs.snapshot_settings_from(settings),
    source_revision="as-of-test", plane=rs.Plane(cloud="gcp", region="us-central1"),
    inputs=rs.CatalogInputs.live(settings))
print(json.dumps({"digest": hashlib.sha256(rs.canonical_snapshot_bytes(snapshot)).hexdigest()}))
"""


def _run(script: str, *args: int) -> dict[str, Any]:
    env = {**os.environ, "TR_ENVIRONMENT": "test", "PYTHONPATH": str(ROOT / "src")}
    # Only the pins decide the clocks: not the session's lifecycle override.
    env.pop(provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV, None)
    result = subprocess.run(  # noqa: S603 - fixed Python script, no shell or caller input
        [sys.executable, "-c", script, *map(str, args)], env=env, cwd=ROOT,
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("retirement", list(RETIREMENTS.values()), ids=list(RETIREMENTS))
def test_warm_and_cold_publishers_publish_the_as_of_snapshot(retirement: int) -> None:
    now = retirement + HOUR // 2
    warm = _run(_PUBLISHER, retirement - HOUR // 2, now)
    cold = _run(_PUBLISHER, retirement + HOUR // 3, now)
    # The scenario: the warm registry still holds what retired at R, and its
    # own in-process compile refuses; the cold one compiles in process.
    assert warm["live_endpoints"] > cold["live_endpoints"]
    assert (warm["in_process"], cold["in_process"]) == ("refused", "compiled")
    # Both publish the same window, as of its start, byte for byte.
    assert warm["start"] == cold["start"] == retirement
    truth = _run(_TRUTH, warm["start"])
    assert warm["digest"] == cold["digest"] == truth["digest"]
    assert (warm["endpoints"], warm["models"]) == (cold["endpoints"], cold["models"])


def test_the_as_of_compile_judges_catalog_freshness_at_the_window_start() -> None:
    # A window that starts at an expiring manifest's deadline (the latest, so
    # its routes are in the catalog): the privacy pools behind the
    # requested-model expansions must be judged as of the window's start, not
    # on the worker's wall clock.
    deadline = max(int(d.timestamp()) for d in provider_manifest_deadlines().values())
    now = deadline + 600
    published = _run(_PUBLISHER, now - 60, now)
    assert published["start"] == deadline < now < published["next_start"]
    assert published["digest"] == _run(_TRUTH, published["start"])["digest"]


def test_the_window_events_come_from_the_image_not_the_registry() -> None:
    # Every expiring manifest's deadline and every retirement starts a window,
    # whether or not this process's registry still holds the routes.
    deadline = min(
        seconds for seconds in (int(d.timestamp()) for d in provider_manifest_deadlines().values())
        if seconds > 0 and seconds % HOUR
    )
    hour = deadline - deadline % HOUR
    assert publication_window_as_of(deadline - 1) == (hour, deadline)
    assert publication_window_as_of(deadline) == (deadline, hour + HOUR)
    retirement = RETIREMENTS["deepinfra-2026-10-20"]
    assert publication_window_as_of(retirement + 60) == (retirement, retirement + HOUR)


def test_the_worker_refuses_a_catalog_imported_before_its_pins() -> None:
    from trusted_router import routing_snapshot_worker

    # This process imported the catalog at session start: the worker must not
    # compile it as if it were as of another instant.
    with pytest.raises(RuntimeError, match="before its clocks were pinned"):
        routing_snapshot_worker.compile_as_of({"at": RETIREMENTS["deepinfra-2026-10-20"]})


def test_the_worker_signs_only_with_the_pinned_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from trusted_router import routing_snapshot_worker
    from trusted_router.detached_jws import b64encode

    private = Ed25519PrivateKey.generate()
    path = tmp_path / "signing.pem"
    path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key = {"file": str(path), "kid": "as-of-test",
           "public_key": b64encode(private.public_key().public_bytes_raw()),
           "iss": "https://trustedrouter.com", "aud": "tr-fastpath-edge"}
    # Positive control: the pinned key loads.
    assert routing_snapshot_worker._signer(key).trusted.kid == "as-of-test"
    other = b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw())
    with pytest.raises(ValueError, match="pinned public key"):
        routing_snapshot_worker._signer({**key, "public_key": other})
