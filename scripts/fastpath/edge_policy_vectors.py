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
accept it and what the member says, and the async admission members'
vectors (docs/design/fast-admission-e2-g15-async.md sections 4.1 and 4.5)
to fastpath/testdata/edge_policy_admission_v1.json: bodies the publisher
writes from each combination of admission, the async pilot set, the
fast-path overlap list and the fast-path set, each with Python's own
admission answer per workspace, which Go's AsyncSettle.Admits must give, and
the members' other spellings, refused by both readers, with all-mode rows
(docs/design/fast-admission-widening.md section 9, PR 5): the same
combinations with the explicit fast-path set empty and every async pilot
workspace outside the overlap list on the exclude list, as the settings
require, each with and without a reserved list (section 9.1: with the async
pilot set empty, every workspace is admitted but the reserved list's), and
the all-mode golden files (sections 6.3 and 11, PR 3) to
fastpath/testdata/edge_policy_all_v1.{body,manifest}.json: the publisher's
body from the golden settings in all mode (fastpath_pilot_all and
fastpath_pilot_exclude, the explicit list emptied), signed alike, and the
optional fastpath_stream_ceiling member's vectors
(docs/design/fast-admission-streams.md section 12.4, PR 4) to
fastpath/testdata/edge_policy_stream_ceiling_v1.json: the publisher's body
with the setting off and on, and the member's other spellings, each with
whether both readers accept it and what the member says. Workspace
ids, digests and the monitor key are synthetic.
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
from trusted_router.edge_policy import (  # noqa: E402
    EdgePolicyV1,
    canonical_edge_policy_bytes,
)
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
OUTPUT_ADMISSION = ROOT / "fastpath" / "testdata" / "edge_policy_admission_v1.json"
OUTPUT_ALL_BODY = ROOT / "fastpath" / "testdata" / "edge_policy_all_v1.body.json"
OUTPUT_ALL_MANIFEST = ROOT / "fastpath" / "testdata" / "edge_policy_all_v1.manifest.json"
OUTPUT_STREAM_CEILING = ROOT / "fastpath" / "testdata" / "edge_policy_stream_ceiling_v1.json"
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


def fixture_settings(**overrides: object) -> Settings:
    # No .env file and the storage backend pinned, so the developer's
    # environment can neither fail construction nor reach a value read here.
    # overrides replace fixture values, parsed as the deployed settings are.
    values: dict[str, object] = dict(
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
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


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


# fastpath_stream_ceiling's spellings the publisher never writes, each refused
# by both readers: present only as true, as fastpath_edge_streams.
STREAM_CEILING_REFUSED = STREAMS_REFUSED


def stream_ceiling_cases() -> list[dict[str, object]]:
    """The member's vectors (docs/design/fast-admission-streams.md section
    12.4, PR 4): the publisher's body with the setting off (no member) and
    on, then the on body with the member spelled every other way."""
    cases: list[dict[str, object]] = []
    bodies: dict[bool, str] = {}
    for on in (False, True):
        settings = fixture_settings().model_copy(update={"fastpath_stream_ceiling": on})
        bodies[on] = canonical_edge_policy_bytes(policy(settings)).decode("ascii")
        cases.append({"name": f"publisher:{'on' if on else 'off'}", "setting": on, "body": bodies[on],
                      "accept": True, "stream_ceiling": on})
    member_text = '"fastpath_stream_ceiling":true'
    if bodies[True].count(member_text) != 1 or "fastpath_stream_ceiling" in bodies[False]:
        raise AssertionError("the member is not in the on body once, or is in the off body")
    if bodies[False] != render_body().decode("ascii"):
        raise AssertionError("the off body is not the golden body")
    for name, spelling in STREAM_CEILING_REFUSED:
        cases.append({"name": f"refused:{name}", "setting": None,
                      "body": bodies[True].replace(member_text, f'"fastpath_stream_ceiling":{spelling}'),
                      "accept": False, "stream_ceiling": False})
    return cases


def render_stream_ceiling() -> str:
    return json.dumps({"generator": GENERATOR, "cases": stream_ceiling_cases()}, indent=1, sort_keys=True) + "\n"


# Async admission per workspace (docs/design/fast-admission-e2-g15-async.md
# sections 4.1 and 4.5): the settings Python's admission reads, the fast-path
# set among them. Two fast-path workspaces, one async-only workspace, one in
# neither.
ADMISSION_FASTPATH = "ws-synthetic-pilot-1,ws-synthetic-pilot-2"
ADMISSION_PROBES = ("ws-synthetic-async-a", "ws-synthetic-neither", "ws-synthetic-pilot-1", "ws-synthetic-pilot-2")
ADMISSION_PILOTS = ("", "ws-synthetic-async-a", "ws-synthetic-async-a,ws-synthetic-pilot-1",
                    "ws-synthetic-pilot-1,ws-synthetic-pilot-2")
ADMISSION_OVERLAPS = ("", "ws-synthetic-pilot-1")
# All mode's reserved lists (docs/design/fast-admission-widening.md section
# 9.1): with a named async pilot set, none or one workspace outside it; with
# the pilot set empty, also a list of two, given unsorted.
ADMISSION_RESERVED = ("", "ws-synthetic-neither")
ADMISSION_RESERVED_NO_PILOTS = (*ADMISSION_RESERVED, "ws-synthetic-pilot-2,ws-synthetic-neither")
# The deployed shape (section 9.1, "The deployed pins"): the pilot set empty,
# a reserved list, and a workspace on the fast path's exclude list, which
# async admission then covers like any other.
ADMISSION_EXCLUDE_NO_PILOTS = "ws-synthetic-async-a"
# The members' spellings the publisher never writes, each refused by both
# readers, spliced into the async_settle object of the all-workspaces body
# (admission on, no pilot set, no fast-path set), and one into the
# admission-off body.
ADMISSION_REFUSED = (
    ("excluded_null", '"admission_excluded_workspaces":null'),
    ("excluded_empty", '"admission_excluded_workspaces":[]'),
    ("excluded_unsorted", '"admission_excluded_workspaces":["ws-synthetic-pilot-2","ws-synthetic-pilot-1"]'),
    ("excluded_duplicate", '"admission_excluded_workspaces":["ws-synthetic-pilot-1","ws-synthetic-pilot-1"]'),
    ("excluded_outside_admission_workspaces",
     '"admission_excluded_workspaces":["ws-synthetic-pilot-1"],"admission_workspaces":["ws-synthetic-async-a"]'),
)
ADMISSION_OFF_REFUSED = ("excluded_admission_off", '"admission_excluded_workspaces":["ws-synthetic-pilot-1"]')


def admission_settings(admission: bool, pilots: str, overlap: str, fastpath: str,
                       exclude: str | None = None, reserved: str = "") -> Settings:
    """The admission settings; with ``exclude`` set, all mode (the explicit
    fast-path set then empty, and the typed Spanner store it requires), and
    ``reserved`` its reserved list."""
    pilot_all = {} if exclude is None else dict(
        fastpath_pilot_all=True, fastpath_pilot_exclude=exclude, storage_backend="spanner-clickhouse",
        async_settle_fastpath_reserved_workspaces=reserved)
    return fixture_settings(
        async_settle_enabled=admission, async_settle_protection=admission,
        async_settle_pilot_workspaces=pilots, async_settle_fastpath_overlap_workspaces=overlap,
        fastpath_pilot_workspaces=fastpath, **pilot_all,
    )


def all_mode_exclude(pilots: str, overlap: str) -> str:
    """The exclude list all mode's settings require for these lists: every
    async pilot workspace the overlap list does not name."""
    return ",".join(sorted({ws for ws in pilots.split(",") if ws} - {ws for ws in overlap.split(",") if ws}))


def admission_all_mode_combinations() -> list[tuple[bool, str, str, str, str]]:
    """(admission, pilots, overlap, exclude, reserved) for the all-mode rows:
    every combination with the exclude list the settings require, the empty
    async pilot set with admission on among them (section 9.1: every
    workspace admitted but the reserved list); then the empty pilot set
    beside a reserved list and an exclude list the settings do not require,
    which is the deployed shape."""
    required = [(admission, pilots, overlap, all_mode_exclude(pilots, overlap), reserved)
                for admission in (False, True) for pilots in ADMISSION_PILOTS for overlap in ADMISSION_OVERLAPS
                for reserved in (ADMISSION_RESERVED if pilots else ADMISSION_RESERVED_NO_PILOTS)]
    deployed = [(admission, "", overlap, ADMISSION_EXCLUDE_NO_PILOTS, ADMISSION_RESERVED[1])
                for admission in (False, True) for overlap in ADMISSION_OVERLAPS]
    return required + deployed


def _spliced(settings: Settings, members: str) -> str:
    body = canonical_edge_policy_bytes(policy(settings)).decode("ascii")
    anchor = f'"async_settle":{{"admission_enabled":{str(settings.async_settle_admission_enabled).lower()},'
    if body.count(anchor) != 1 or "admission_workspaces" in body or "admission_excluded" in body:
        raise AssertionError("the splice's base body has a member already")
    return body.replace(anchor, anchor + members + ",")


def admission_cases() -> list[dict[str, object]]:
    """The admission members' vectors: the publisher's body for every
    combination of admission, the async pilot set, the overlap list and the
    fast-path set, each with Python's own admission answer per probe
    workspace (async_settle_admission_enabled and async_settle_pilot_allows,
    which snapshot_projection and the settlement handler read); then the
    members spelled every way the publisher never writes, refused."""
    cases: list[dict[str, object]] = []
    for admission in (False, True):
        for pilots in ADMISSION_PILOTS:
            for overlap in ADMISSION_OVERLAPS:
                for fastpath in ("", ADMISSION_FASTPATH):
                    settings = admission_settings(admission, pilots, overlap, fastpath)
                    admits = {ws: settings.async_settle_admission_enabled and settings.async_settle_pilot_allows(ws)
                              for ws in ADMISSION_PROBES}
                    cases.append({
                        "name": f"publisher:admission={str(admission).lower()}:pilots={pilots or '-'}"
                                f":overlap={overlap or '-'}:fastpath={'set' if fastpath else '-'}",
                        "settings": {"admission": admission, "pilots": pilots, "overlap": overlap,
                                     "fastpath": fastpath},
                        "body": canonical_edge_policy_bytes(policy(settings)).decode("ascii"),
                        "accept": True, "admits": admits,
                    })
    # All mode: ws-synthetic-neither stands for a workspace the enabler turns
    # on (covered, on no list). A named async pilot set leaves it out; an
    # empty one admits it unless the reserved list names it, and admits a
    # workspace on the exclude list too.
    for admission, pilots, overlap, exclude, reserved in admission_all_mode_combinations():
        settings = admission_settings(admission, pilots, overlap, "", exclude, reserved)
        admits = {ws: settings.async_settle_admission_enabled and settings.async_settle_pilot_allows(ws)
                  for ws in ADMISSION_PROBES}
        cases.append({
            "name": f"publisher:all:admission={str(admission).lower()}:pilots={pilots or '-'}"
                    f":overlap={overlap or '-'}:exclude={exclude or '-'}:reserved={reserved or '-'}",
            "settings": {"admission": admission, "pilots": pilots, "overlap": overlap, "fastpath": "",
                         "exclude": exclude, "reserved": reserved},
            "body": canonical_edge_policy_bytes(policy(settings)).decode("ascii"),
            "accept": True, "admits": admits,
        })
    on = admission_settings(True, "", "", "")
    for name, members in ADMISSION_REFUSED:
        cases.append({"name": f"refused:{name}", "settings": None, "body": _spliced(on, members),
                      "accept": False, "admits": None})
    name, members = ADMISSION_OFF_REFUSED
    cases.append({"name": f"refused:{name}", "settings": None,
                  "body": _spliced(admission_settings(False, "", "", ""), members), "accept": False, "admits": None})
    return cases


def render_admission() -> str:
    return json.dumps({"generator": GENERATOR, "probes": list(ADMISSION_PROBES), "cases": admission_cases()},
                      indent=1, sort_keys=True) + "\n"


# All mode's exclude list: two synthetic workspaces, so the Go reader is
# exercised on a list of more than one.
ALL_EXCLUDE = ("ws-synthetic-excluded-a", "ws-synthetic-excluded-b")


def all_mode_settings() -> Settings:
    """The golden settings in all mode: fastpath_pilot_all on, the exclude
    list (given unsorted, as an operator may write it), the explicit list
    emptied and the typed Spanner store, as the settings require."""
    return fixture_settings(fastpath_pilot_workspaces="", fastpath_pilot_all=True,
                            fastpath_pilot_exclude=",".join(reversed(ALL_EXCLUDE)),
                            storage_backend="spanner-clickhouse")


def all_policy() -> EdgePolicyV1:
    """The publisher's policy from the all-mode settings; every other field
    the golden body's."""
    return policy(all_mode_settings())


def render_all_body() -> bytes:
    return canonical_edge_policy_bytes(all_policy())


def render_all_manifest() -> str:
    return _manifest(render_all_body())


def render_manifest() -> str:
    return _manifest(render_body())


def _manifest(body: bytes) -> str:
    manifest = {
        "generator": GENERATOR,
        "test_key": True,
        "kid": TEST_KEY.kid,
        "purpose": TEST_KEY.purpose,
        "iss": TEST_KEY.iss,
        "aud": TEST_KEY.aud,
        "public_key_b64url": TEST_KEY.public_key_b64url,
        "jws": golden_signer().sign(body),
    }
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUTPUT_BODY.write_bytes(render_body())
    OUTPUT_MANIFEST.write_text(render_manifest())
    OUTPUT_STREAMS.write_text(render_streams())
    OUTPUT_ADMISSION.write_text(render_admission())
    OUTPUT_ALL_BODY.write_bytes(render_all_body())
    OUTPUT_ALL_MANIFEST.write_text(render_all_manifest())
    OUTPUT_STREAM_CEILING.write_text(render_stream_ceiling())
    print(f"wrote {OUTPUT_BODY.relative_to(ROOT)}, {OUTPUT_MANIFEST.relative_to(ROOT)}, "
          f"{OUTPUT_STREAMS.relative_to(ROOT)}, {OUTPUT_ADMISSION.relative_to(ROOT)}, "
          f"{OUTPUT_ALL_BODY.relative_to(ROOT)}, {OUTPUT_ALL_MANIFEST.relative_to(ROOT)} and "
          f"{OUTPUT_STREAM_CEILING.relative_to(ROOT)}")
