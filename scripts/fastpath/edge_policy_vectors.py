"""Golden files for the Go edge-policy reader (E2 P2, docs/design/routing-snapshot-v1.md).

Builds an edge policy v1 body issued at 2026-10-19T00:00Z from synthetic
settings (every value the body reads is passed explicitly, so the process's
environment cannot change the files) through the publisher's own
``settings_facts`` and ``build_policy``, and signs it with the routing
snapshot's TEST key (scripts/fastpath/routing_snapshot_vectors.py, derived from
a public seed) under the edge-policy ``typ``. It writes the exact canonical
body (no trailing newline) and the signed manifest to fastpath/testdata/,
and the optional fastpath_edge_streams member's vectors
(docs/design/fast-admission-streams.md section 5.4) to
fastpath/testdata/edge_policy_streams_v1.json: bodies the publisher writes
from each combination of its three switches, and the member's other
spellings, each with whether Python's EdgePolicyV1 and Go's ParseEdgePolicy
accept it and what the member says. Workspace ids, digests and the monitor
key are synthetic.
tests/test_edge_policy_publish.py fails when the files are not what this
script writes now.

    uv run python scripts/fastpath/edge_policy_vectors.py
"""

from __future__ import annotations

import calendar
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from trusted_router.config import Settings  # noqa: E402
from trusted_router.edge_policy import EdgePolicyV1, canonical_edge_policy_bytes  # noqa: E402
from trusted_router.routing_snapshot_signing import (  # noqa: E402
    EdgePolicySigner,
    RoutingSnapshotSigner,
)
from trusted_router.services.edge_policy_publisher import (  # noqa: E402
    build_policy,
    edge_streams_on,
    settings_facts,
)
from trusted_router.services.routing_snapshot_publisher import plane_of  # noqa: E402

OUTPUT_BODY = ROOT / "fastpath" / "testdata" / "edge_policy_v1.body.json"
OUTPUT_MANIFEST = ROOT / "fastpath" / "testdata" / "edge_policy_v1.manifest.json"
OUTPUT_STREAMS = ROOT / "fastpath" / "testdata" / "edge_policy_streams_v1.json"
GENERATOR = "scripts/fastpath/edge_policy_vectors.py"

ISSUED_AT = calendar.timegm((2026, 10, 19, 0, 0, 0))  # 2026-10-19T00:00Z


def _routing_vectors() -> ModuleType:
    path = ROOT / "scripts" / "fastpath" / "routing_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("routing_snapshot_vectors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTING = _routing_vectors()
TEST_KEY = ROUTING.TEST_KEY
TEST_KEY_SEED = ROUTING.TEST_KEY_SEED


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


# The verified Stage D policy's set (the resolver's), and one break-glass
# digest that is also in it plus one that is not: the body holds the union.
RESOLVER_DIGESTS = frozenset({_digest("synthetic enclave image 1"), _digest("synthetic enclave image 2")})
BREAK_GLASS_DIGESTS = (_digest("synthetic enclave image 2"), _digest("synthetic enclave image 3"))
MONITOR_KEY = "sk-tr-v1-synthetic-monitor-key-for-golden-files"  # noqa: S105 - synthetic
# Fixed here, not read from the catalog, so a catalog change does not move
# the files; the publisher reads internal_model_access.internal_model_ids().
INTERNAL_MODELS = frozenset({"trustedrouter/monitor", "trustedrouter/fortanix-test"})


def fixture_settings() -> Settings:
    # No .env file and the storage backend pinned, so the developer's
    # environment can neither fail construction nor reach a value read here.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test", sentry_dsn=None, storage_backend="memory",
        primary_region="us-central1", serving_region="us-central1",
        regions="us-central1,europe-west4,us-east4",
        multi_region_enabled=True,
        spend_lease_accepted_gcp_image_digests=",".join(BREAK_GLASS_DIGESTS),
        stage_d_eligibility_enabled=True,
        stage_d_heartbeat_enabled=True,
        stage_d_require_heartbeat_declaration=False,
        stage_d_pilot_workspace_ids="ws-synthetic-stage-d-b,ws-synthetic-stage-d-a",
        settle_outbox_enabled=True,
        fastpath_pilot_enabled=True,
        fastpath_pilot_workspaces="ws-synthetic-pilot-2,ws-synthetic-pilot-1",
        fastpath_pilot_regions="us-central1",
        gateway_authorize_max_in_flight_per_key=4,
        synthetic_monitor_api_key=MONITOR_KEY,
        async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws-synthetic-async-shadow",
        # async_settle: admission and the edge's async switch off, as deployed.
        async_settle_enabled=False,
        async_settle_protection=False,
        fastpath_edge_decide_async=False,
        # Off, as deployed: the body carries no fastpath_pilot_key_windows,
        # the member Go nodes accept only from e2-key-windows increment 2.
        fastpath_pilot_key_windows="off",
    )


def policy(settings: Settings | None = None) -> EdgePolicyV1:
    settings = settings or fixture_settings()
    # async_settle: admission and the edge's async switch off (pinned in
    # fixture_settings, as rollout.sh deploys them), a ticket authority and
    # typed records, so the body carries both values.
    facts = settings_facts(
        settings, accepted=RESOLVER_DIGESTS | settings.stage_d_accepted_gcp_digests, durable=True,
        internal=INTERNAL_MODELS, ticket_authority=True, typed_records=True,
    )
    return build_policy(plane_of(settings), ISSUED_AT, facts)


def golden_signer() -> EdgePolicySigner:
    return EdgePolicySigner(RoutingSnapshotSigner(
        Ed25519PrivateKey.from_private_bytes(TEST_KEY_SEED), TEST_KEY, allow_test_key=True,
    ))


def render_body() -> bytes:
    return canonical_edge_policy_bytes(policy())


# The three switches fastpath_edge_streams needs, by name.
STREAMS_SWITCHES = ("fastpath_edge_streams", "fastpath_pilot_streams", "reap_snapshot_booking_enabled")
# The member's spellings the publisher never writes, each refused by both
# readers: present only as true.
STREAMS_REFUSED = (("false", "false"), ("null", "null"), ("one", "1"), ("string", '"true"'))


def streams_cases() -> list[dict[str, object]]:
    """The member's vectors: the publisher's body for every combination of
    the three switches (the member present only with all three on), then
    the all-on body with the member spelled every other way."""
    cases: list[dict[str, object]] = []
    on_body = ""
    for mask in range(1 << len(STREAMS_SWITCHES)):
        switches = {name: bool(mask >> i & 1) for i, name in enumerate(STREAMS_SWITCHES)}
        settings = fixture_settings().model_copy(update=switches)
        body = canonical_edge_policy_bytes(policy(settings)).decode("ascii")
        member = edge_streams_on(settings)
        if member:
            on_body = body
        name = "+".join(n for n, v in switches.items() if v) or "all_off"
        cases.append({"name": f"publisher:{name}", "switches": switches, "body": body, "accept": True,
                      "edge_streams": member})
    member_text = '"fastpath_edge_streams":true'
    if on_body.count(member_text) != 1:
        raise AssertionError("the all-on body does not carry the member once")
    for name, spelling in STREAMS_REFUSED:
        cases.append({"name": f"refused:{name}", "switches": None,
                      "body": on_body.replace(member_text, f'"fastpath_edge_streams":{spelling}'),
                      "accept": False, "edge_streams": False})
    return cases


def render_streams() -> str:
    return json.dumps({"generator": GENERATOR, "cases": streams_cases()}, indent=1, sort_keys=True) + "\n"


def render_manifest() -> str:
    manifest = {
        "generator": GENERATOR,
        "test_key": True,
        "kid": TEST_KEY.kid,
        "purpose": TEST_KEY.purpose,
        "iss": TEST_KEY.iss,
        "aud": TEST_KEY.aud,
        "public_key_b64url": TEST_KEY.public_key_b64url,
        "jws": golden_signer().sign(render_body()),
    }
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUTPUT_BODY.write_bytes(render_body())
    OUTPUT_MANIFEST.write_text(render_manifest())
    OUTPUT_STREAMS.write_text(render_streams())
    print(f"wrote {OUTPUT_BODY.relative_to(ROOT)}, {OUTPUT_MANIFEST.relative_to(ROOT)} and "
          f"{OUTPUT_STREAMS.relative_to(ROOT)}")
