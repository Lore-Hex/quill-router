"""Golden authorize vectors for the Go edge's decide path (E2 P1,
docs/design/fast-admission-e2-decide.md §10.1 and §10.2).

Both files are what the ASGI app answers (FastAPI's body read and
content-type rule, the validation handler, the timing decorator, the
exception handlers, `_authorize_gateway_sync_impl`), over httpx's ASGI
transport with the internal token, over the routing-snapshot fixture catalog
(scripts/fastpath/routing_snapshot_vectors.py), the Spanner fake
(tests/fakes/spanner.make_fake_store, typed request records) and the
pilot's front-door fake (tests/test_fastpath_frontdoor._Door):

fastpath/testdata/authorize_cases_v1.json, one authorize a line, each in its
own world: the stored key, workspace, credit rows, boot, BYOK and broadcast
ids, the edge policy (the header's, patched), the ids and envelope Go would
use, and the request's exact headers and body. A case expects Go's
`answer` (Python's exact 200 bytes and the two rows it wrote), Python's
`unavailable` 503, or a `pass` with the §3 reason the case trips and what
Python then answered.

fastpath/testdata/authorize_fingerprint_v1.json, one request a line (its
headers and body), each sent through the app in a fresh world on the
cohort path (a pilot workspace, a signed boot, the owner admitting): the
fingerprint and scope from the reservation row it wrote (§7), or Python's
refusal, and labelled with what it carries; §10.2's pass-required labels
are the ones on which Go passes.

Everything is pinned: the clock (each case's instant, inside the snapshot's
window), every uuid4, key ids and salts, the boot key (a TEST Ed25519 key
from a fixed seed below; the signatures are real), the envelope, and the
route's timing clock (data.timing's milliseconds are 0; spanner_rpcs is the
fake's count). Every workspace, key, digest and envelope is synthetic. The
script installs the fixture catalog and patches clocks in its own process;
run it as a script, never import it.

    uv run python scripts/fastpath/authorize_vectors.py            # write both files
    uv run python scripts/fastpath/authorize_vectors.py --print cases|fingerprints
    uv run python scripts/fastpath/authorize_vectors.py --out DIR  # both, under DIR
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as real_dt
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
import types
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
# Settings must not take routing inputs from the shell that runs this.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]
os.environ["TR_ENVIRONMENT"] = "test"
os.environ["TR_STORAGE_BACKEND"] = "memory"

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from google.api_core.exceptions import (  # noqa: E402
    Aborted,
    DeadlineExceeded,
    FailedPrecondition,
    InvalidArgument,
)
from pydantic import ValidationError  # noqa: E402

from scripts.fastpath import affinity_rule  # noqa: E402
from tests.fakes.spanner import _SpannerModule, make_fake_store  # noqa: E402
from tests.test_fastpath_frontdoor import _Door, _transport  # noqa: E402
from trusted_router import gateway_timing, storage_gcp, storage_gcp_keys  # noqa: E402
from trusted_router.auth import is_api_key_expired  # noqa: E402
from trusted_router.authorize_candidates import HOLD_OUTPUT_TOKENS  # noqa: E402
from trusted_router.catalog_data import MONITOR_MODEL_ID, Model, ModelEndpoint  # noqa: E402
from trusted_router.catalog_registry import MODEL_ENDPOINTS, MODELS  # noqa: E402
from trusted_router.config import Settings  # noqa: E402
from trusted_router.gateway_boot import BOOT_AUTH_DOMAIN, GATEWAY_BOOT_KIND, boot_auth_digest  # noqa: E402
from trusted_router.main import create_app  # noqa: E402
from trusted_router.model_aliases import canonical_router_model_id  # noqa: E402
from trusted_router.pricing import PriceTier  # noqa: E402
from trusted_router.receipt_keys import b64url_encode, normalize_receipt_jwk, receipt_kid  # noqa: E402
from trusted_router.regions import configured_regions  # noqa: E402
from trusted_router.routes.internal import gateway  # noqa: E402
from trusted_router.routing_state import RoutingState  # noqa: E402
from trusted_router.schemas import GatewayAuthorizeRequest  # noqa: E402
from trusted_router.scopes import SCOPE_INFERENCE  # noqa: E402
from trusted_router.services import fastpath_frontdoor as fd  # noqa: E402
from trusted_router.spend_windows import key_window_limits  # noqa: E402
from trusted_router.stage_d import ESTIMATED_USAGE_SNAPSHOT_MODELS  # noqa: E402
from trusted_router.storage import configure_store  # noqa: E402
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE  # noqa: E402
from trusted_router.storage_models import CreditAccount, GatewayBoot, Workspace  # noqa: E402

CASES_OUTPUT = "fastpath/testdata/authorize_cases_v1.json"
FINGERPRINT_OUTPUT = "fastpath/testdata/authorize_fingerprint_v1.json"
GENERATOR = "scripts/fastpath/authorize_vectors.py"
SNAPSHOT = "fastpath/testdata/routing_snapshot_v3.body.json"
PATH = "/internal/gateway/authorize"
# A reservation row's created_at in the goldens: the commit's timestamp (§5).
RESERVATION_CREATED_AT = "commit_timestamp"
PLANE = {"cloud": "gcp", "region": "us-central1"}


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "fastpath" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTING = _load("routing_cases_vectors")
REGIONS, PRIMARY_REGION, INSTANTS = ROUTING.REGIONS, ROUTING.PRIMARY_REGION, ROUTING.INSTANTS
FLASH, TENCENT_FLASH, GLM, GEMINI = ROUTING.FLASH, ROUTING.TENCENT_FLASH, ROUTING.GLM, ROUTING.GEMINI
FUGU, BEDROCK, INKLING = ROUTING.FUGU, ROUTING.BEDROCK, ROUTING.INKLING

# Synthetic identities: never a real workspace, key, image or envelope.
COHORT_WS, OTHER_WS, FINGERPRINT_WS = "ws_e2_cohort", "ws_e2_other", "ws_e2_fingerprint"
DIGEST = "sha256:" + hashlib.sha256(b"e2 p1 synthetic accepted image").hexdigest()
OTHER_DIGEST = "sha256:" + hashlib.sha256(b"e2 p1 synthetic other image").hexdigest()
# A TEST boot key, public by construction: the seed is this repository's text.
BOOT_SEED_PREIMAGE = b"trustedrouter authorize vectors v1 boot TEST key; not a secret"
BOOT_SEED = hashlib.sha256(BOOT_SEED_PREIMAGE).digest()
BOOT_KEY = Ed25519PrivateKey.from_private_bytes(BOOT_SEED)
BOOT_JWK = normalize_receipt_jwk(
    {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(BOOT_KEY.public_key().public_bytes_raw())},
)
BOOT_KID = receipt_kid(BOOT_JWK)
ENVELOPE, END_OF_LIFE = "sealed-e2-p1-test-envelope", "2026-10-19T05:00:00Z"
ADMIT = {"Status": "admitted", "Envelope": ENVELOPE, "EndOfLife": END_OF_LIFE}
REFUND_WON = {"Status": "won", "Kind": "refund", "Charge": 0}
SALT = hashlib.sha256(b"e2 p1 synthetic salt").hexdigest()
FRONT_DOOR = "10.0.0.1:8080"
INTERNAL_TOKEN = "e2-p1-synthetic-internal-token"  # noqa: S105 - a TEST value; the edge forwards the real one


def _us(stamp: str | real_dt.datetime | None) -> int | None:
    if stamp is None:
        return None
    when = stamp if isinstance(stamp, real_dt.datetime) else real_dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    delta = when - real_dt.datetime(1970, 1, 1, tzinfo=real_dt.UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _seeded_uuid(seed: str) -> uuid.UUID:
    return uuid.UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4)


def case_ids(name: str) -> dict[str, str]:
    """The ids a case's request is given: Go's inputs, Python's own mint."""
    return {
        "authorization_id": f"gwa-{_seeded_uuid(f'{name}#authorization').hex}",
        "reservation_id": str(_seeded_uuid(f"{name}#reservation")),
        "minted_idempotency_key": str(_seeded_uuid(f"{name}#idempotency_key")),
    }


# ---------------------------------------------------------------- pinning --

class _InstantMeta(type):
    # isinstance(any datetime, the pinned class) holds, as for the real one.
    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, real_dt.datetime)


@contextlib.contextmanager
def patched(target: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


@contextlib.contextmanager
def pinned(at: real_dt.datetime, seed: str) -> Iterator[None]:
    """Pin the route's clock readings to ``at`` and every uuid4 to ``seed``.

    The route reads the clock through its module's ``dt`` and through a
    function-local ``import datetime``; both see a datetime module whose
    ``datetime.now`` answers ``at``. uuid4 answers a sequence derived from
    the seed, so a case's ids do not depend on the cases before it.
    """

    class _Pinned(real_dt.datetime, metaclass=_InstantMeta):
        @classmethod
        def now(cls, tz: real_dt.tzinfo | None = None) -> real_dt.datetime:  # type: ignore[override]
            return at if tz is None else at.astimezone(tz)

    pinned_module = types.ModuleType("datetime")
    pinned_module.__dict__.update(real_dt.__dict__)
    pinned_module.datetime = _Pinned  # type: ignore[attr-defined]
    counter = iter(range(1 << 30))
    saved = sys.modules["datetime"], gateway.dt, uuid.uuid4
    sys.modules["datetime"], gateway.dt = pinned_module, pinned_module  # type: ignore[assignment]
    uuid.uuid4 = lambda: _seeded_uuid(f"{seed}#{next(counter)}")  # type: ignore[assignment]
    try:
        with ROUTING.fixture_catalog(at):
            yield
    finally:
        sys.modules["datetime"], gateway.dt, uuid.uuid4 = saved  # type: ignore[assignment]


@contextlib.contextmanager
def given_ids(ids: dict[str, str]) -> Iterator[None]:
    """The route mints exactly these: the authorization id, the reservation
    id (the leased insert's), and the idempotency key when none is presented."""
    minted = uuid.UUID(ids["minted_idempotency_key"])
    reservation = uuid.UUID(ids["reservation_id"])
    with patched(gateway, "_new_gateway_authorization_id", lambda: ids["authorization_id"]), \
            patched(gateway, "uuid", types.SimpleNamespace(uuid4=lambda: minted)), \
            patched(storage_gcp, "uuid", types.SimpleNamespace(uuid4=lambda: reservation)):
        yield


class _Recorder:
    """The fingerprints the route computed, and the exact JSON each hashed."""

    def __init__(self, then: Callable[[], None] | None = None) -> None:
        self.seen: list[tuple[str, str]] = []
        self.then = then

    def __call__(self, **kwargs: Any) -> str:
        tap = _JsonTap()
        with patched(gateway, "json", tap):
            fingerprint = _REAL_FINGERPRINT(**kwargs)
        (material,) = tap.encoded
        if hashlib.sha256(material.encode()).hexdigest() != fingerprint:
            raise AssertionError("the tapped material is not what the route hashed")
        self.seen.append((fingerprint, material))
        if self.then is not None:
            self.then()
        return fingerprint


class _JsonTap:
    def __init__(self) -> None:
        self.encoded: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(json, name)

    def dumps(self, *args: Any, **kwargs: Any) -> str:
        encoded = json.dumps(*args, **kwargs)
        self.encoded.append(encoded)
        return encoded


_REAL_FINGERPRINT = gateway._gateway_authorize_fingerprint
_REAL_TRANSACTION = storage_gcp.run_in_transaction_with_retry
gateway_timing.perf_counter = lambda: 100.0  # data.timing's milliseconds are 0


def scope_of(workspace_id: str, key_hash: str, idempotency_key: str) -> str:
    return f"{workspace_id}#{key_hash}#{hashlib.sha256(idempotency_key.encode()).hexdigest()}"


# ------------------------------------------------------------------ world --

# The harness's policy: the edge policy's inputs (published as §4's body)
# and the two settings records_durable folds together.
DEFAULT_POLICY: dict[str, Any] = {
    "fastpath_pilot_enabled": True,
    "fastpath_pilot_workspaces": [COHORT_WS],
    "fastpath_pilot_regions": ["us-central1"],
    "settle_outbox_enabled": True,
    "activity_outbox_enabled": True,
    "stage_d": {
        "eligibility_enabled": True, "heartbeat_enabled": True,
        "require_heartbeat_declaration": False, "pilot_workspaces": [COHORT_WS],
    },
    "accepted_image_digests": [DIGEST],
    "gateway_authorize_max_in_flight_per_key": 4,
    "monitor_key": False,
    "async_settle_shadow_policy": "",
    "async_settle_shadow_workspaces": [],
    "configured_regions": list(REGIONS),
    "primary_region": PRIMARY_REGION,
    "multi_region_enabled": True,
}
DEFAULT_KEY: dict[str, Any] = {
    "hash": "key_e2_cohort", "name": "cohort", "disabled": False, "expires_at": None,
    "scopes": [], "app_id": "", "federated_home": "", "limit_microdollars": None,
    "limit_daily_microdollars": None, "limit_weekly_microdollars": None,
    "limit_monthly_microdollars": None, "budget_strict": False, "budget_alert_only": False,
    "include_byok_in_limit": True, "tags": {},
}
DEFAULT_WORLD: dict[str, Any] = {
    "workspace": {"id": COHORT_WS, "billing_pause_causes": [], "federated_home": "", "deleted": False},
    "credit_micro": 50_000_000,
    "key": DEFAULT_KEY,
    "boot": {"kid": BOOT_KID, "verified": True, "image_digest": DIGEST, "declares_usage_heartbeat": True},
    "byok_providers": [],
    "broadcast_destinations": 0,
}


def _raw_key(key_hash: str) -> str:
    # The key's secret, derived from its synthetic id; never written out.
    return f"e2-p1-synthetic-raw-key:{key_hash}"


def lookup_hash(key_hash: str) -> str:
    return hashlib.sha256(_raw_key(key_hash).encode()).hexdigest()


def settings_for(policy: dict[str, Any], key: dict[str, Any]) -> Settings:
    stage_d = policy["stage_d"]
    return Settings(
        environment="test",
        service_surface="internal",
        internal_gateway_token=INTERNAL_TOKEN,
        regions=",".join(policy["configured_regions"]),
        primary_region=policy["primary_region"],
        multi_region_enabled=policy["multi_region_enabled"],
        auto_model_order="",
        # No signed digest policy to fetch: the accepted digests are the
        # policy's own (the break-glass list). The ingress rate-limit
        # backstop, which counts every request this process sends, is off.
        trust_gcp_release_url="",
        trust_gcp_release_fallback_urls="",
        rate_limit_ip_per_window=1_000_000_000,
        rate_limit_internal_per_window=1_000_000_000,
        fastpath_pilot_enabled=policy["fastpath_pilot_enabled"],
        fastpath_pilot_workspaces=",".join(policy["fastpath_pilot_workspaces"]),
        fastpath_pilot_regions=",".join(policy["fastpath_pilot_regions"]),
        settle_outbox_enabled=policy["settle_outbox_enabled"],
        stage_d_eligibility_enabled=stage_d["eligibility_enabled"],
        stage_d_heartbeat_enabled=stage_d["heartbeat_enabled"],
        stage_d_require_heartbeat_declaration=stage_d["require_heartbeat_declaration"],
        stage_d_pilot_workspace_ids=",".join(stage_d["pilot_workspaces"]),
        spend_lease_accepted_gcp_image_digests=",".join(policy["accepted_image_digests"]),
        gateway_authorize_max_in_flight_per_key=policy["gateway_authorize_max_in_flight_per_key"],
        synthetic_monitor_api_key=_raw_key(key["hash"]) if policy["monitor_key"] else None,
        async_settle_shadow_policy=policy["async_settle_shadow_policy"],
        async_settle_shadow_workspaces=",".join(policy["async_settle_shadow_workspaces"]),
    )


def edge_policy_body(policy: dict[str, Any], key: dict[str, Any], at: real_dt.datetime) -> dict[str, Any]:
    """§4's body for this policy, issued ten seconds before the instant."""
    settings = settings_for(policy, key)
    issued_at = int(at.timestamp()) - 10
    return {
        "v": 1, "kind": "tr-edge-policy", "plane": dict(PLANE),
        "issued_at": issued_at, "not_after": issued_at + 60, "refresh_at": issued_at + 15,
        "accepted_image_digests": sorted(settings.stage_d_accepted_gcp_digests),
        "stage_d": {
            "eligibility_enabled": settings.stage_d_eligibility_enabled,
            "heartbeat_enabled": settings.stage_d_heartbeat_enabled,
            "require_heartbeat_declaration": settings.stage_d_require_heartbeat_declaration,
            "pilot_workspaces": sorted(settings.stage_d_pilot_workspaces),
        },
        "records_durable": policy["settle_outbox_enabled"] and policy["activity_outbox_enabled"],
        "fastpath_pilot_enabled": settings.fastpath_pilot_enabled,
        "fastpath_pilot_workspaces": sorted(settings.fastpath_pilot_workspace_ids),
        "fastpath_pilot_regions": sorted(settings.fastpath_pilot_region_ids),
        "gateway_authorize_max_in_flight_per_key": settings.gateway_authorize_max_in_flight_per_key,
        "monitor_lookup_hash": lookup_hash(key["hash"]) if policy["monitor_key"] else None,
        "async_settle_shadow_policy": settings.async_settle_shadow_policy,
        "async_settle_shadow_workspaces": sorted(settings.async_settle_shadow_workspace_ids),
        "configured_regions": list(configured_regions(settings)),
        "primary_region": settings.primary_region,
        "multi_region_enabled": settings.multi_region_enabled,
        "internal_models": internal_models(),
    }


def build_world(world: dict[str, Any], policy: dict[str, Any]) -> tuple[Any, Any]:
    store, database = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=policy["activity_outbox_enabled"],
        generation_records_enabled=True,
        analytics_outbox_enabled=True,
    )
    spec = world["workspace"]
    workspace = Workspace(
        id=spec["id"], name="E2 vectors", owner_user_id="user_e2", created_at="2026-10-01T00:00:00Z",
        deleted=spec["deleted"], billing_pause_causes=list(spec["billing_pause_causes"]),
        federated_home=spec["federated_home"],
    )
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": world["credit_micro"],
        "total_usage": 0, "reserved": 0, "in_debt": False, "trust_tier": 3, "trust_latched_at": None,
        "billing_pause_causes": [], "source_updated_at": None, "updated_at": None,
        **world.get("credit_row", {}),
    }
    key = world["key"]
    with patched(storage_gcp_keys, "new_key_id", lambda: key["hash"]), \
            patched(storage_gcp_keys, "new_hash_salt", lambda: SALT):
        _raw, created = store.api_keys.create(
            workspace_id=workspace.id, name=key["name"], creator_user_id="user_e2",
            raw_key=_raw_key(key["hash"]), expires_at=key["expires_at"], scopes=key["scopes"] or None,
            app_id=key["app_id"], limit_microdollars=key["limit_microdollars"],
            limit_daily_microdollars=key["limit_daily_microdollars"],
            limit_weekly_microdollars=key["limit_weekly_microdollars"],
            limit_monthly_microdollars=key["limit_monthly_microdollars"],
            budget_strict=key["budget_strict"], budget_alert_only=key["budget_alert_only"],
            include_byok_in_limit=key["include_byok_in_limit"], tags=dict(key["tags"]),
        )
    # The stored key as the case gives it, created at a fixed instant.
    store._write_entity("api_key", created.hash, dataclasses.replace(
        created, disabled=key["disabled"], federated_home=key["federated_home"], created_at="2026-10-01T00:00:00Z",
    ))
    boot = world["boot"]
    if boot is not None:
        store._write_entity(GATEWAY_BOOT_KIND, boot["kid"], GatewayBoot(
            kid=boot["kid"], jwk=BOOT_JWK, approved=True, verified=boot["verified"],
            image_digest=boot["image_digest"], attestation_kind="gcp",
            registered_at="2026-10-18T23:00:00Z", declares_usage_heartbeat=boot["declares_usage_heartbeat"],
        ))
    for provider in world["byok_providers"]:
        store.upsert_byok_provider(
            workspace_id=workspace.id, provider=provider, secret_ref=f"ref-e2-{provider}", key_hint=None,
        )
    for index in range(world["broadcast_destinations"]):
        store.create_broadcast_destination(
            workspace_id=workspace.id, type="webhook", name=f"e2-{index}",
            endpoint=f"https://broadcast-{index}.example.invalid/hook",
        )
    configure_store(store)
    return store, database


def stored(database: Any, world: dict[str, Any]) -> dict[str, Any]:
    """The world as Go reads it: entity bodies, credit rows, ids, facts."""
    key, workspace_id = world["key"], world["workspace"]["id"]

    def body(kind: str, entity_id: str) -> str | None:
        row = database.rows.get((kind, entity_id))
        return row.body if row is not None else None

    def ids(kind: str) -> list[str]:
        return sorted(eid for (row_kind, eid) in database.rows if row_kind == kind and eid.startswith(f"{workspace_id}#"))

    credit_rows = [
        {"shard": row["shard"], "in_debt": bool(row.get("in_debt")), "trust_tier": row.get("trust_tier") or 0,
         "trust_latched": row.get("trust_latched_at") is not None,
         "billing_pause_causes": list(row.get("billing_pause_causes") or [])}
        for (candidate, _shard), row in sorted(database.typed[CREDIT_BALANCE_TABLE].items())
        if candidate == workspace_id
    ]
    # BYOK entities are kind "byok" (storage_gcp.py's auth-context query and
    # get_byok_providers), the kind Go's key-context read ranges over.
    byok, broadcast = ids("byok"), ids("broadcast_destination_by_workspace")
    spec = world["workspace"]
    causes = sorted(set(spec["billing_pause_causes"]).union(*(row["billing_pause_causes"] for row in credit_rows)))
    key_body, workspace_body = body("api_key", key["hash"]), body("workspace", workspace_id)
    return {
        "key": {"lookup_body": body("api_key_lookup", lookup_hash(key["hash"])), "key_body": key_body},
        "workspace": {"body": workspace_body, "credit_rows": credit_rows},
        "byok_entity_ids": byok,
        "broadcast_pointer_ids": broadcast,
        "facts": {
            "key": key_facts(key, workspace_id, key_body),
            "workspace": {
                "workspace_id": workspace_id, "missing": workspace_body is None, "pause_causes": causes,
                "in_debt": any(row["in_debt"] for row in credit_rows),
                "trust_tier": min(0 if row["trust_latched"] else row["trust_tier"] for row in credit_rows),
                "federated": bool(spec["federated_home"]), "deleted": spec["deleted"],
                "byok_slugs": sorted(eid.split("#", 1)[1] for eid in byok),
                "broadcast_pointer": bool(broadcast),
            },
        },
    }


def key_facts(key: dict[str, Any], workspace_id: str, key_body: str | None) -> dict[str, Any]:
    """§8.1's KeyFacts, the reasons in Python's order, by Python's predicates.

    Tags are the stored body's, in its order (json_body sorts the keys, and
    Python merges what it decodes from there): an object's entries, none
    for anything else (merge_tags counts it absent), and usable only when
    every entry is a string pair of printable ASCII (§6)."""
    reasons = (
        ("disabled", key["disabled"]),
        ("expired", is_api_key_expired(key["expires_at"])),
        ("scope", bool(key["scopes"]) and SCOPE_INFERENCE not in key["scopes"]),
        ("federated", bool(key["federated_home"])),
        ("app", bool(key["app_id"])),
        ("key_cap", key["limit_microdollars"] is not None),
        ("key_window", bool(key_window_limits(types.SimpleNamespace(**key)))),
        ("budget_strict", key["budget_strict"]),
    )
    reason = next((name for name, holds in reasons if holds), "")
    stored_tags = json.loads(key_body).get("tags", {}) if key_body is not None else {}
    tags = stored_tags if isinstance(stored_tags, dict) else {}
    return {
        "lookup_hash": lookup_hash(key["hash"]), "eligible": not reason, "reason": reason,
        "hash": key["hash"], "workspace_id": workspace_id, "expires_at_us": _us(key["expires_at"]),
        "tags": [[k, v] for k, v in tags.items()],
        "tags_usable": all(isinstance(v, str) and _printable_ascii(k) and _printable_ascii(v) for k, v in tags.items()),
    }


def sign(body: bytes) -> str:
    return b64url_encode(BOOT_KEY.sign(boot_auth_digest("POST", PATH, body)))


def boot_headers(mode: str, body: bytes) -> list[list[str]]:
    if mode == "absent":
        return []
    if mode == "signed":
        return [["x-tr-boot-auth", f"kid={BOOT_KID},sig={sign(body)}"]]
    if mode == "bad_signature":
        return [["x-tr-boot-auth", f"kid={BOOT_KID},sig={sign(body + b' ')}"]]
    if mode == "unknown_kid":
        return [["x-tr-boot-auth", f"kid=boot-e2-unknown,sig={sign(body)}"]]
    if mode == "malformed":
        return [["x-tr-boot-auth", f"kid={BOOT_KID}"]]
    if mode == "repeated":
        header = ["x-tr-boot-auth", f"kid={BOOT_KID},sig={sign(body)}"]
        return [header, list(header)]
    raise ValueError(mode)


# ------------------------------------------------------------------- run --

class _App:
    """The app over httpx's ASGI transport, driven by one event loop: no
    lifespan (so no background loop touches the store) and no thread hop
    per request beyond the route's own. An unhandled error is the app's
    500, as a server answers it."""

    _loop = asyncio.new_event_loop()

    def __init__(self, settings: Settings) -> None:
        app = create_app(settings, configure_store_arg=False, init_observability=False)
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver",
        )

    def post(self, path: str, *, content: bytes, headers: list[tuple[bytes, bytes]]) -> httpx.Response:
        return self._loop.run_until_complete(self._client.post(path, content=content, headers=headers))


_APPS: dict[str, _App] = {}


def _client(settings: Settings) -> _App:
    """One app per distinct settings (an app is built over its settings)."""
    cache_key = settings.model_dump_json()
    if cache_key not in _APPS:
        _APPS[cache_key] = _App(settings)
    return _APPS[cache_key]


def request_body(request: dict[str, Any]) -> str:
    """The body as sent: a body_expand replaces its marker, once, with
    count repeats of repeat (so a 4 MiB body is a line, not 4 MiB)."""
    expand = request.get("body_expand")
    if expand is None:
        return request["body"]
    return request["body"].replace(expand["marker"], expand["repeat"] * expand["count"], 1)


def send(client: _App, request: dict[str, Any]) -> Any:
    return client.post(request["path"], content=request_body(request).encode(),
                       headers=[(name.encode(), value.encode()) for name, value in request["headers"]])


@contextlib.contextmanager
def front_door(answers: dict[str, Any] | None) -> Iterator[_Door]:
    """The pilot's front door: one fake answering ``answers``, or none
    reachable (``None``)."""
    door = _Door({"/v1/authorize": dict(answers or {}), "/v1/refund": dict(REFUND_WON)})
    transport = _transport({FRONT_DOOR: door}, down=frozenset() if answers is not None else {FRONT_DOOR})
    with patched(gateway, "_fastpath_client", fd.FrontDoorClient(
        lambda _budget: (FRONT_DOOR,), timeout_seconds=1.5, clock=lambda: 0.0, transport=transport,
    )):
        yield door


@contextlib.contextmanager
def leased_insert(insert: str) -> Iterator[None]:
    """The leased insert's commit: as it is; applied, or not, with its answer
    lost to a deadline, which Python resolves by reading back; or refused
    with nothing applied. Go classifies a refusal by its code and how many
    times its callback ran (§8.4, store.ClassifyInsert): invalid_argument
    (InvalidArgument, the callback run once) proves nothing applied, Go's
    `Failed`; failed_precondition (FailedPrecondition, run once: possibly a
    re-sent commit's answer) and aborted (Aborted once the client's re-runs
    are spent, run more than once) prove nothing, Go's `Unknown`, which reads
    back, finds no row and answers decision 1's 503."""
    if insert == "inserted":
        yield
        return

    def lost(database: Any, txn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") != "tr_authorize_leased":
            return _REAL_TRANSACTION(database, txn, **kwargs)
        if insert == "invalid_argument":
            raise InvalidArgument("the commit was refused; nothing applied")
        if insert == "failed_precondition":
            raise FailedPrecondition("the commit was refused; nothing applied")
        if insert == "aborted":
            raise Aborted("the commit lost its retries; nothing applied")
        if insert == "unknown_landed":
            _REAL_TRANSACTION(database, txn, **kwargs)
        raise DeadlineExceeded("the commit's answer was lost")

    with patched(storage_gcp, "run_in_transaction_with_retry", lost):
        yield


def _error(body: str) -> dict[str, Any]:
    """An error body's error object; {} for one that is not JSON (a bare 500)."""
    try:
        return json.loads(body).get("error") or {}
    except ValueError:
        return {}


def _eol(answer: dict[str, Any]) -> str:
    """An owner end of life as Python reads it: at most microseconds."""
    text = answer.get("EndOfLife", END_OF_LIFE)
    return re.sub(r"(\.\d{6})\d+", r"\1", text)


def _row_timestamps(row: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {f"{name}_us" if name in names else name: _us(value) if name in names else value
            for name, value in row.items()}


def _reservation_row(row: dict[str, Any]) -> dict[str, Any]:
    """The reservation's columns as the insert wrote them. created_at is the
    commit's timestamp (PENDING_COMMIT_TIMESTAMP(), §5), which has no value
    before the commit: the golden says so rather than name an instant."""
    if row["created_at"] != _SpannerModule.COMMIT_TIMESTAMP:
        raise AssertionError(f"the reservation's created_at is {row['created_at']!r}, not the commit's timestamp")
    written = {k: v for k, v in row.items() if k not in {"settled", "settled_usage_type", "actual_micro", "terminal_at"}}
    return {**_row_timestamps(written, ("expires_at",)), "created_at": RESERVATION_CREATED_AT}


def _session_key(world: dict[str, Any], body: dict[str, Any], region: str) -> tuple[str, str, str, str]:
    """The route's affinity session (gateway.py, ``rerank``): the key's hash,
    body.model, the region it chose and the cache affinity key."""
    return (world["key"]["hash"], body["model"], region, body["cache_affinity_key"])


def _seeded_routing_state(case: dict[str, Any], sessions: tuple[tuple[str, float], ...],
                          region: str | None) -> RoutingState:
    """A fresh process-local routing state (the route's is per process and
    seeded only by settle), remembering each (endpoint, age in seconds) under
    the case's session. ``rank`` reads the monotonic clock, so an age past
    600 s is an expired session."""
    state = RoutingState()
    if sessions:
        body = json.loads(case["request"]["body"])
        assert region is not None
        for endpoint_id, age in sessions:
            state.remember(_session_key(case["_world"], body, region), endpoint_id, now=time.monotonic() - age)
    return state


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    if case["_affinity"] is None:
        return {k: v for k, v in _run_case(case, ()).items() if k != "_models"}
    # A cache affinity key: Python's order with no session (the cold order,
    # which Go composes) and then with the case's sessions remembered.
    cold = _run_case(case, ())
    if cold["expect"]["outcome"] != "answer":
        raise AssertionError(f"{case['name']}: an affinity case Python did not answer: {cold['expect']}")
    data = json.loads(cold["expect"]["response"])["data"]
    warm = _run_case(case, case["_affinity"], region=data["region"])
    if warm["expect"]["outcome"] != "answer":
        raise AssertionError(f"{case['name']}: a seeded affinity case Python did not answer: {warm['expect']}")
    cold_ids = [c["endpoint_id"] for c in data["route_candidates"]]
    warm_data = json.loads(warm["expect"]["response"])["data"]
    warm_ids = [c["endpoint_id"] for c in warm_data["route_candidates"]]
    # The class, or a refusal: Python's warm candidates must be rank's
    # re-rank of the cold order (provider.order and the sort partition
    # respected, the session's age too), with nothing but the first
    # candidate's members (and, with fallbacks off, the estimate) changed.
    affinity_class = affinity_rule.affinity_class(
        case["name"], json.loads(case["request"]["body"]), cold["expect"]["response"], warm["expect"]["response"],
        case["_affinity"], cold["_models"].__getitem__)
    for field in ("fingerprint", "fingerprint_material", "idempotency_scope", "decide_region"):
        if cold["expect"][field] != warm["expect"][field]:
            raise AssertionError(f"{case['name']}: the session changed {field}")
    expect = {**cold["expect"],
              "affinity": {"class": affinity_class, "sessions": [list(s) for s in case["_affinity"]],
                           **{field: cold["expect"][field] for field in
                              ("fingerprint", "fingerprint_material", "idempotency_scope", "decide_region")},
                           "response": warm["expect"]["response"], "cold_response": cold["expect"]["response"],
                           "timing": warm["expect"]["timing"], "cold_timing": cold["expect"]["timing"],
                           "candidates": warm_ids, "cold_candidates": cold_ids,
                           "estimate_microdollars": warm_data["estimated_cost_microdollars"],
                           "cold_estimate_microdollars": data["estimated_cost_microdollars"],
                           # The lease's hold (§5, "The two figures") in each run.
                           "reservation_microdollars": warm["expect"]["reservation_microdollars"],
                           "cold_reservation_microdollars": cold["expect"]["reservation_microdollars"]}}
    return {**{k: v for k, v in cold.items() if k not in ("expect", "_models")}, "expect": expect}


def _run_case(case: dict[str, Any], sessions: tuple[tuple[str, float], ...], *,
              region: str | None = None) -> dict[str, Any]:
    at = ROUTING._instant(case["_at"])
    world, policy = case["_world"], case["_policy"]
    with pinned(at, case["name"]), contextlib.ExitStack() as stack:
        for model, endpoint in (_catalog_addition(model_id) for model_id in case["_catalog"]):
            MODELS[model.id] = model
            MODEL_ENDPOINTS[endpoint.id] = endpoint
        # The route's process-local routing state, fresh for every case.
        stack.enter_context(patched(gateway, "ROUTING_STATE", _seeded_routing_state(case, sessions, region)))
        store, database = build_world(world, policy)
        if case["_stored_key_tags"] is not None:
            # Tags as a corrupt row holds them: writes validate, this does not.
            key = store.api_keys.get_by_hash(world["key"]["hash"])
            store._write_entity("api_key", key.hash, dataclasses.replace(key, tags=case["_stored_key_tags"]))
        if not case["_workspace_row"]:
            database.rows.pop(("workspace", world["workspace"]["id"]))
        gateway._BROADCAST_EMPTY_CACHE.clear()
        client = _client(settings_for(policy, world["key"]))
        for index, (prior, answer) in enumerate(case["_prior"]):
            with front_door(answer), given_ids(case_ids(f"{case['name']}#prior{index}")):
                response = send(client, prior)
            if response.status_code != 200:
                raise AssertionError(f"{case['name']}: a prior request failed: {response.text}")
        world_rows = stored(database, world)
        door = stack.enter_context(front_door(case["_front_door"]))
        recorder = _Recorder()
        stack.enter_context(patched(gateway, "_gateway_authorize_fingerprint", recorder))
        stack.enter_context(given_ids(case["ids"]))
        stack.enter_context(leased_insert(case["insert"]))
        authorizations_before, reservations_before = set(database.gateway_authorizations), set(database.reservations)
        # The fixture catalog's endpoint models (installed by pinned), for
        # the affinity rule, which run_case applies outside it.
        models = {endpoint_id: endpoint.model_id for endpoint_id, endpoint in MODEL_ENDPOINTS.items()}
        response = send(client, case["request"])
        authorizations = [row for k, row in database.gateway_authorizations.items() if k not in authorizations_before]
        reservations = [row for k, row in database.reservations.items() if k not in reservations_before]
    if response.headers.get("content-encoding"):
        raise AssertionError("the response was compressed")
    body = response.content.decode()
    leased = bool(authorizations and json.loads(authorizations[0]["payload"]).get("fastpath_envelope"))
    reason = case["_reason"]
    if reason is not None:
        error = _error(body) if response.status_code != 200 else {}
        # The credit reservation Python's typed store wrote for a request it
        # answered without a lease (a stream, say): at most HOLD_OUTPUT_TOKENS
        # of the output budget, or the whole budget for a stream (§5, "The
        # two figures").
        reserved = (
            {"reservation_microdollars": reservations[0]["credit_reserved_micro"]}
            if len(reservations) == 1 and not leased else {}
        )
        return {**world_rows, "_models": models, "expect": {"outcome": "pass", "reason": reason, "python": {
            "status": response.status_code, "type": error.get("type"), "leased": leased, **reserved}}}
    if case["insert"] == "failed_precondition" and response.status_code != 503:
        # Python counts this code permanent and fails the request; Go cannot
        # tell it from the refusal of a re-sent commit whose first applied
        # (§8.4), so it answers as for an outcome not known and not landed:
        # Python's own 503 for the same request, recorded with what Python
        # answered here.
        error = _error(body) if response.status_code != 200 else {}
        unknown = _run_case({**case, "insert": "unknown_not_landed"}, sessions, region=region)
        unknown["expect"]["python"] = {"status": response.status_code, "type": error.get("type"), "leased": leased}
        return unknown
    if response.status_code == 503:
        if authorizations or [path for path, _sent in door.seen] != ["/v1/authorize"]:
            raise AssertionError(f"{case['name']}: a 503 that wrote or refunded")
        return {**world_rows, "_models": models, "expect": {"outcome": "unavailable", "status": 503,
                                         "headers": [["retry-after", response.headers["retry-after"]]],
                                         "response": body, "timing": json.loads(body)["data"]["timing"]}}
    if not (response.status_code == 200 and leased):
        raise AssertionError(f"{case['name']}: an answer case Python did not lease: {response.status_code} {body}")
    (authorization,), (reservation,) = authorizations, reservations
    if (authorization["authorization_id"], reservation["reservation_id"]) != (
            case["ids"]["authorization_id"], case["ids"]["reservation_id"]):
        raise AssertionError(f"{case['name']}: the route minted other ids")
    data = json.loads(body)["data"]
    fingerprint, material = recorder.seen[-1]
    return {**world_rows, "_models": models, "expect": {
        "outcome": "answer", "status": 200, "response": body,
        "generation_id": data["generation_id"],
        "estimate_microdollars": data["estimated_cost_microdollars"],
        # The credit reservation the lease held (the front door's Estimate):
        # at most HOLD_OUTPUT_TOKENS of the output budget, where the estimate
        # above prices the whole budget (authorize_candidates.hold_output_tokens).
        "reservation_microdollars": door.seen[0][1]["Estimate"],
        "decide_region": data["region"],
        "fingerprint": fingerprint,
        "fingerprint_material": material,
        "idempotency_scope": reservation["idempotency_scope"],
        "timing": data["timing"],
        "authorization_row": _row_timestamps(authorization, ("created_at", "terminal_at", "started_at", "heartbeat_at")),
        "reservation_row": _reservation_row(reservation),
    }}


def _catalog_addition(model_id: str) -> tuple[Model, ModelEndpoint]:
    """A route outside the snapshot fixture, for the cases that name one."""
    if model_id not in ESTIMATED_USAGE_SNAPSHOT_MODELS:
        raise ValueError(model_id)
    model = Model(id=model_id, name=model_id, provider="abliterate", context_length=131_072)
    endpoint = ModelEndpoint(
        id=f"{model_id}@abliterate/prepaid", model_id=model_id, provider="abliterate", usage_type="Credits",
        upstream_id=None, supported_parameters=("max_tokens",), input_modalities=("text",),
        prompt_price_microdollars_per_million_tokens=300_000,
        completion_price_microdollars_per_million_tokens=1_200_000,
        published_prompt_price_microdollars_per_million_tokens=300_000,
        published_completion_price_microdollars_per_million_tokens=1_200_000,
        price_tiers=(PriceTier(None, 300_000, 1_200_000, 30_000),),
    )
    return model, endpoint


def case_labels(case: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """§10.1's labels."""
    if expect["outcome"] == "pass":
        return [f"pass.{expect['reason']}"]
    labels = {f"insert.{case['insert']}"}
    if "affinity" in expect:
        labels.add(expect["affinity"]["class"].replace("_", ".", 1))
    if expect["outcome"] == "unavailable":
        return sorted(labels)
    data = json.loads(expect["response"])["data"]
    body = json.loads(case["request"]["body"])
    labels.add(f"stage_d.{data['stage_d']['reason']}")
    if any(c.get("wafer_zdr_required") for c in data["route_candidates"]):
        labels.add("wafer_zdr")
    if len(data["route_candidates"]) > 1:
        labels.add("multi_candidate")
    key_tags, request_tags = case["_world"]["key"]["tags"], body.get("tags") or {}
    if key_tags and not request_tags:
        labels.add("tags.key_only")
    if request_tags and not key_tags:
        labels.add("tags.request_only")
    if set(key_tags) & set(request_tags):
        labels.add("tags.merge_overwrite")
    estimate = re.search(r'"estimated_cost":([^,]+),', expect["response"])
    if estimate is not None and "e" in estimate.group(1):
        labels.add("float.exponent")
    if data["estimated_cost_microdollars"] % 1_000_000 == 0:
        labels.add("float.integral")
    headers = dict((name, value) for name, value in case["request"]["headers"])
    if (body.get("idempotency_key") or "").strip():
        labels.add("idempotency.presented_body")
    elif headers.get("idempotency-key", "").strip():
        labels.add("idempotency.presented_header")
    else:
        labels.add("idempotency.minted")
    if body.get("invocation_nonce"):
        labels.add("invocation_nonce")
    labels.add("region.requested" if (body.get("region") or "").strip() in REGIONS else "region.default")
    if case["insert"] == "unknown_landed" and data["tags"]:
        labels.add("read_back.sorted_tags")
    if not case["admitted"]["end_of_life"].endswith("Z"):
        labels.add("end_of_life.offset")
    return sorted(labels)


# ----------------------------------------------------------------- cases --

def _merge(base: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for name, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(out.get(name), dict):
            out[name] = _merge(out[name], value)
        else:
            out[name] = value
    return out


def _request(world: dict[str, Any], body: Any, *, boot: str = "signed",
             headers: tuple[tuple[str, str], ...] = (), content_type: str | None = "application/json",
             body_expand: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(body, dict):
        body = {"api_key_lookup_hash": lookup_hash(world["key"]["hash"]), **body}
        body = json.dumps({k: v for k, v in body.items() if v is not None}, separators=(",", ":"), ensure_ascii=False)
    request = {"path": PATH, "body": body, **({"body_expand": body_expand} if body_expand else {})}
    # Identity: the bytes before Python's GZip middleware, which Go never applies.
    request["headers"] = [*([["content-type", content_type]] if content_type is not None else []),
                          ["accept-encoding", "identity"], ["x-trustedrouter-internal-token", INTERNAL_TOKEN],
                          *boot_headers(boot, request_body(request).encode()), *[[k, v] for k, v in headers]]
    return request


PLAIN_TAIL = '"max_output_tokens":512,"stream":false,"route_type":"chat.completions"'


def raw_body(lookup: str, *fields: str) -> str:
    """A body written by hand, for what a dict cannot spell (a repeated key,
    1000.0, NaN, a lone surrogate escape)."""
    return '{"api_key_lookup_hash":"' + lookup + '",' + ",".join(fields) + "}"


BASE_BODY: dict[str, Any] = {
    "model": GLM, "estimated_input_tokens": 1_000, "max_output_tokens": 512,
    "stream": False, "route_type": "chat.completions",
}


def cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, reason: str | None = None, body: dict[str, Any] | str | None = None, *,
            at: str = "peak", world: dict[str, Any] | None = None, policy: dict[str, Any] | None = None,
            boot: str = "signed", headers: tuple[tuple[str, str], ...] = (),
            door: dict[str, Any] | None = ADMIT, insert: str = "inserted",
            prior: tuple[tuple[dict[str, Any], dict[str, Any]], ...] = (),
            catalog: tuple[str, ...] = (), content_type: str | None = "application/json",
            stored_key_tags: Any = None, workspace_row: bool = True,
            body_expand: dict[str, Any] | None = None,
            affinity: tuple[tuple[str, float], ...] | None = None) -> None:
        # affinity: a body with cache_affinity_key, run cold and then with
        # these (endpoint, age in seconds) sessions remembered; () is cold.
        if (affinity is not None) != ("cache_affinity_key" in (body if isinstance(body, dict) else {})):
            raise AssertionError(f"{name}: an affinity case is one with cache_affinity_key, and vice versa")
        case_world = _merge(DEFAULT_WORLD, world)
        admitted = door if door is not None and "Envelope" in door else ADMIT
        request_body = body if isinstance(body, str) else {**BASE_BODY, **(body or {})}
        boot_record = case_world["boot"]
        out.append({
            "name": name, "ids": case_ids(name), "insert": insert,
            # end_of_life: the owner's text as it answered (Go's JSON for a
            # time.Time), which Python parses and writes back (§5).
            "admitted": {"envelope": admitted["Envelope"], "end_of_life_us": _us(_eol(admitted)),
                         "end_of_life": admitted["EndOfLife"]},
            "request": _request(case_world, request_body, boot=boot, headers=headers, content_type=content_type,
                                body_expand=body_expand),
            "boot": None if boot_record is None else {**boot_record, "signature_ok": boot == "signed"},
            "_at": INSTANTS[at], "_world": case_world, "_policy": _merge(DEFAULT_POLICY, policy),
            "_front_door": door, "_reason": reason, "_catalog": catalog,
            "_stored_key_tags": stored_key_tags, "_workspace_row": workspace_row,
            "_prior": [(_request(case_world, {**BASE_BODY, **p}), answer) for p, answer in prior],
            "_affinity": affinity,
        })

    # Answers: every fixture model at every instant, as Python leases it.
    for model, region in ((FLASH, None), (TENCENT_FLASH, None), (GLM, None), (GEMINI, None),
                          (FUGU, None), (BEDROCK, "us-east4"), (INKLING, None)):
        for at in INSTANTS:
            add(f"answer:model:{model}@{at}", body={"model": model, "region": region}, at=at)
    # Every Stage D reason an answered request carries.
    for reason, policy, world in (
        ("not_streaming", None, None),
        ("stage_d_disabled", {"stage_d": {"eligibility_enabled": False}}, None),
        ("workspace_not_pilot", {"stage_d": {"pilot_workspaces": [OTHER_WS]}}, None),
        ("heartbeats_disabled", {"stage_d": {"heartbeat_enabled": False}}, None),
        ("heartbeat_undeclared", {"stage_d": {"require_heartbeat_declaration": True}},
         {"boot": {"declares_usage_heartbeat": False}}),
        ("declared_and_required", {"stage_d": {"require_heartbeat_declaration": True}}, None),
        ("every_workspace_pilot", {"stage_d": {"pilot_workspaces": []}}, None),
    ):
        for model in (GLM, GEMINI, FLASH):
            add(f"answer:stage_d:{reason}:{model}", body={"model": model}, policy=policy, world=world)
    # Estimates on both sides of the float spellings' thresholds.
    small = ((0, 1), (1, 1), (0, 2), (3, 7), (10, 10), (100, 1), (99, 99), (1_000, 1), (12_345, 678),
             (60_000, 4_096))
    large = ((199_999, 1), (200_000, 1), (200_001, 1), (500_000, 4_096), (1_000_000, 1), (2_000_000_000, 1),
             (474, 79), (4_740, 790))
    for model, sizes in ((FLASH, small), (GLM, small), (TENCENT_FLASH, small[:6]), (GEMINI, small + large)):
        for tokens_in, tokens_out in sizes:
            add(f"answer:estimate:{model}:{tokens_in}+{tokens_out}",
                body={"model": model, "estimated_input_tokens": tokens_in, "max_output_tokens": tokens_out})
    # max_output_tokens prices but does not count toward the window. DeepSeek
    # charges 633,000 (off peak) or 1,266,000 (peak) per million completion
    # tokens: a billion of them is a whole number of dollars.
    for at in INSTANTS:
        for tokens_out in (1_000_000_000, 1_562_500, 15_625_000):
            add(f"answer:estimate:{FLASH}:0+{tokens_out}@{at}", at=at,
                body={"model": FLASH, "estimated_input_tokens": 0, "max_output_tokens": tokens_out})
    # The reservation (reservation_microdollars, the lease's hold) prices at
    # most HOLD_OUTPUT_TOKENS of the budget while the estimate prices it all:
    # both sides of the bound, and the production shape that motivated it, a
    # max_tokens of 131,072 (which counts toward the window, so only Gemini's
    # unknown window admits it).
    for tokens_out in (HOLD_OUTPUT_TOKENS - 1, HOLD_OUTPUT_TOKENS, HOLD_OUTPUT_TOKENS + 1, 131_072):
        add(f"answer:estimate:hold_cap:{FLASH}:1000+{tokens_out}",
            body={"model": FLASH, "estimated_input_tokens": 1_000, "max_output_tokens": tokens_out})
    add(f"answer:estimate:hold_cap:{GEMINI}:8000+max_tokens:131072",
        body={"model": GEMINI, "estimated_input_tokens": 8_000, "max_output_tokens": None, "max_tokens": 131_072})
    # A stream's reservation is its estimate for now (finish_candidates,
    # stream): Go passes a stream to Python (body.stream), whose typed store
    # writes the whole budget's price into the reservation row, recorded as
    # the pass case's python.reservation_microdollars.
    for tokens_out in (HOLD_OUTPUT_TOKENS, 131_072):
        add(f"pass:body.stream:hold_cap:{FLASH}:1000+{tokens_out}", "body.stream",
            body={"model": FLASH, "stream": True, "estimated_input_tokens": 1_000, "max_output_tokens": tokens_out})
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        add(f"answer:output_field:{field}", body={"max_output_tokens": None, field: 2_048})
    add("answer:output_default_512", body={"max_output_tokens": None})
    add("answer:input_default_1", body={"estimated_input_tokens": None})
    add("answer:max_tokens_wins", body={"max_tokens": 64, "max_completion_tokens": 128, "max_output_tokens": 256})
    # Candidates: several, across models, filtered and ordered by the body.
    add("answer:fallback_models", body={"model": FLASH, "models": [GLM, GEMINI]})
    add("answer:fallback_models_no_fallbacks",
        body={"model": GLM, "models": [FLASH], "provider": {"allow_fallbacks": False}})
    for option, provider in (
        ("order", {"order": ["fireworks", "together"]}), ("only", {"only": ["wafer", "together"]}),
        ("ignore", {"ignore": ["wafer"]}), ("sort_price", {"sort": "price"}),
        ("sort_latency", {"sort": "latency"}), ("sort_throughput", {"sort": "throughput"}),
        ("zdr", {"zdr": True}), ("deny_collection", {"data_collection": "deny"}),
        ("credits_only", {"usage": "credits"}), ("max_price", {"max_price": {"prompt": 1, "completion": 4}}),
    ):
        add(f"answer:provider:{option}", body={"provider": provider})
    add("answer:top_level_no_fallbacks", body={"allow_fallbacks": False})
    add("answer:variant_floor", body={"model": f"{GLM}:floor"})
    add("answer:variant_nitro", body={"model": f"{FLASH}:nitro"})
    add("answer:image_input", body={"model": TENCENT_FLASH, "input_modalities": ["text", "image"]})
    add("answer:required_parameters",
        body={"model": GEMINI, "requested_parameters": ["tools"], "provider": {"require_parameters": True}})
    # G14: the snapshot carries the parameter tables and the privacy
    # spellings, so these are answered: a capability-required parameter drops
    # the GLM 5.3 route that does not declare it; require_parameters holds
    # every route to the requested names through their aliases; min_privacy
    # keeps the hosts of its tier.
    add("answer:requested_parameters:tools", body={"requested_parameters": ["tools"]})
    add("answer:require_parameters:structured_alias",
        body={"requested_parameters": ["response_format"], "provider": {"require_parameters": True}})
    for spelling in ("zdr", "confidential", "trusted", " E2E "):
        add(f"answer:provider:min_privacy:{spelling.strip().lower()}", body={"provider": {"min_privacy": spelling}})
    add("answer:provider:min_privacy_and_order", body={"provider": {"min_privacy": "zdr", "order": ["chutes", "together"]}})
    # The region a request routes to, and the regions payload.
    for region in ("us-east4", "europe-west4", "us-west1", " us-east4 ", "", "unknown-region"):
        add(f"answer:region:{region.strip() or 'empty'}{':spaced' if region != region.strip() else ''}",
            body={"model": GLM, "region": region})
    add("answer:region:single_region_plane", policy={"multi_region_enabled": False})
    add("answer:route:responses", body={"route_type": "responses"})
    add("answer:route:messages", body={"route_type": "messages"})
    add("answer:route:absent", body={"route_type": None})
    # Tags: the key's stored ones, then the request's, in merge order. The
    # stored ones are in sorted order whatever order they were set in
    # (json_body sorts the key's body); the request's keep the body's order.
    for name, key_tags, request_tags in (
        ("key_only", {"team": "core", "env": "prod"}, None),
        ("request_only", {}, {"env": "dev", "app": "cli"}),
        ("overlap", {"team": "core", "env": "prod"}, {"env": "dev", "app": "cli"}),
        ("request_empty", {"team": "core"}, {}),
        ("portable_punctuation", {}, {"a+b": "c=d", "path/x": "u@h:1.0", "space key": "v v"}),
        ("stored_sorted_then_request_order", {"z": "1", "a": "2"}, {"m": "3", "b": "4"}),
    ):
        add(f"answer:tags:{name}", body={"tags": request_tags}, world={"key": {"tags": key_tags}})
    # Idempotency: the body's key first, then the header's, stripped.
    add("answer:idempotency:header", headers=(("idempotency-key", "client-key-1"),))
    add("answer:idempotency:body", body={"idempotency_key": "client-key-2"})
    add("answer:idempotency:header_spaced", headers=(("idempotency-key", "  client-key-3 "),))
    add("answer:idempotency:body_spaced", body={"idempotency_key": " client-key-4  "})
    add("answer:idempotency:body_and_header", body={"idempotency_key": "body-key"},
        headers=(("idempotency-key", "header-key"),))
    add("answer:idempotency:empty_header", headers=(("idempotency-key", "   "),))
    add("answer:idempotency:enclave_shape", body={"idempotency_key": "tr-gateway-" + "ab" * 16})
    add("answer:idempotency:256_chars", body={"idempotency_key": "k" * 256})
    add("answer:idempotency:fresh_key_after_another", body={"idempotency_key": "second"},
        prior=(({"idempotency_key": "first"}, ADMIT),))
    add("answer:invocation_nonce", body={"invocation_nonce": "nonce-e2-1"})
    add("answer:attribution", body={"user": "user-1", "session_id": "s-1", "trace": {"trace_id": "t-1", "n": 3},
                                    "app": "My App", "http_referer": "https://example.com/x",
                                    "app_categories": ["cli-agent"]})
    add("answer:referer_strict_without_app", body={"http_referer": "https://Example.COM:8443/path?q=1"})
    add("answer:metadata", body={"metadata": {"k": "v", "n": 2}})
    add("answer:extras_kept", body={"temperature_hint": "low", "nested_extra": {"a": [1, 2]}})
    add("answer:credits_short_owner_admits", world={"credit_micro": 0})
    add("answer:cache_affinity_explicit_without_key", body={"cache_affinity_explicit": True})
    add("answer:key_not_yet_expired", world={"key": {"expires_at": "2099-01-01T00:00:00Z"}})
    add("answer:key_inference_scope", world={"key": {"scopes": ["inference"]}})
    # The insert's outcome unknown: read back, landed (answer) or not (503).
    add("answer:insert_unknown_landed", insert="unknown_landed")
    add("unavailable:insert_unknown_not_landed", insert="unknown_not_landed")

    # Passes, each tripping one §3 reason, with Python's outcome.
    lookup = lookup_hash(DEFAULT_KEY["hash"])
    add("pass:body.unparseable", "body.unparseable", '["not an object"]')
    add("pass:body.duplicate_key", "body.duplicate_key",
        raw_body(lookup, f'"model":"{FLASH}"', f'"model":"{GLM}"', '"estimated_input_tokens":1000', PLAIN_TAIL))
    add("pass:body.float", "body.float", body={"metadata": {"temperature": 0.7}})
    add("pass:body.float:integral_spelling", "body.float",
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":1000.0', PLAIN_TAIL))
    add("pass:body.lax_type", "body.lax_type",
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":"1000"', PLAIN_TAIL))
    add("pass:body.lax_type:bool", "body.lax_type", body={"stream": 0})
    add("pass:body.strip_ambiguous", "body.strip_ambiguous", body={"user": "ünï"})
    add("pass:body.strip_ambiguous:tab", "body.strip_ambiguous", body={"session_id": "\ts-1"})
    add("pass:body.refused:schema", "body.refused", body={"estimated_input_tokens": -1})
    add("pass:body.refused:tags", "body.refused", body={"tags": {"bad!": "x"}})
    add("pass:body.refused:reserved_tag", "body.refused", body={"tags": {"trustedrouter:x": "y"}})
    add("pass:body.refused:referer", "body.refused", body={"http_referer": "ftp://example.com"})
    add("pass:body.api_key_hash", "body.api_key_hash",
        body={"api_key_hash": DEFAULT_KEY["hash"], "api_key_lookup_hash": None})
    add("pass:body.api_key_hash:both", "body.api_key_hash", body={"api_key_hash": DEFAULT_KEY["hash"]})
    add("pass:body.stream", "body.stream", body={"stream": True})
    add("pass:body.stream:stage_d_ok", "body.stream", body={"stream": True, "model": GEMINI})
    for tier in ("auto", "priority", "flex", "default"):
        add(f"pass:body.service_tier:{tier}", "body.service_tier", body={"model": GEMINI, "service_tier": tier})
    # A cache affinity key (G13, §1 item 5): Python's order comes from a
    # per-process session table that only settle seeds, so Go decides the
    # cold order, Python's answer with no session (the case's answer), and
    # the shadow compares Python's bytes with the sessions remembered
    # against it (agree_full when they are the same, a differ named
    # differ.affinity_warm when they are a re-rank).
    key = {"cache_affinity_key": "c" * 64}
    glm_wafer, glm_together, glm_fireworks = (f"{GLM}@{p}/prepaid" for p in ("wafer", "together", "fireworks"))
    add("answer:cache_affinity", body=key, affinity=())
    add("answer:cache_affinity:cold:fallback_models",
        body={**key, "model": FLASH, "models": [GLM, GEMINI]}, affinity=())
    add("answer:cache_affinity:cold:explicit",
        body={**key, "cache_affinity_explicit": True}, affinity=())
    add("answer:cache_affinity:cold:expired", body=key,
        affinity=((glm_fireworks, 601.0),))
    add("answer:cache_affinity:cold:other_key", body=key,
        affinity=((f"{FLASH}@deepseek/prepaid", 1.0),))
    add("answer:cache_affinity:cold:order",
        body={**key, "provider": {"order": ["fireworks", "together"]}}, affinity=((glm_together, 1.0),))
    add("answer:cache_affinity:warm", body=key, affinity=((glm_fireworks, 1.0),))
    add("answer:cache_affinity:warm:second", body=key, affinity=((glm_together, 599.0),))
    add("answer:cache_affinity:cold:already_first", body=key, affinity=((glm_wafer, 1.0),))
    add("answer:cache_affinity:warm:fallback_models",
        body={**key, "model": FLASH, "models": [GLM, GEMINI]}, affinity=((glm_fireworks, 1.0),))
    add("answer:cache_affinity:warm:no_fallbacks",
        body={**key, "provider": {"allow_fallbacks": False}}, affinity=((glm_fireworks, 1.0),))
    add("answer:cache_affinity:warm:top_level_no_fallbacks",
        body={**key, "allow_fallbacks": False}, affinity=((glm_together, 1.0),))
    add("answer:cache_affinity:warm:sort_price",
        body={**key, "provider": {"sort": "price"}}, affinity=((glm_together, 1.0),))
    # An empty provider.order names no provider: Python re-ranks as with none.
    add("answer:cache_affinity:warm:empty_order",
        body={**key, "provider": {"order": []}}, affinity=((glm_fireworks, 1.0),))
    add("answer:cache_affinity:warm:no_fallbacks:empty_order",
        body={**key, "provider": {"allow_fallbacks": False, "order": []}}, affinity=((glm_fireworks, 1.0),))
    add("answer:cache_affinity:warm:top_level_no_fallbacks:empty_order",
        body={**key, "allow_fallbacks": False, "provider": {"order": []}}, affinity=((glm_together, 1.0),))
    # The sort partitioned by model (the default) keeps the remembered
    # endpoint within its model's run; partition none moves it to the front
    # of the whole list.
    add("answer:cache_affinity:warm:partition_none:fallback_models",
        body={**key, "model": FLASH, "models": [GLM, GEMINI], "provider": {"sort": {"sort": "price", "partition": "none"}}},
        affinity=((glm_together, 1.0),))
    add("answer:cache_affinity:warm:partition_model:fallback_models",
        body={**key, "model": FLASH, "models": [GLM, GEMINI], "provider": {"sort": {"sort": "price", "partition": "model"}}},
        affinity=((glm_together, 1.0),))
    add("pass:body.priced_extra:additional_cost", "body.priced_extra",
        body={"additional_cost_reservation_microdollars": 5_000})
    add("pass:body.priced_extra:inference_receipt", "body.priced_extra", body={"inference_receipt": True})
    add("pass:body.canonical_model", "body.canonical_model", body={"model": "nyte/auto"})
    add("pass:body.canonical_model:fallback", "body.canonical_model",
        body={"model": GLM, "models": ["trustedrouter/auto-routing"]})
    add("pass:body.custom_model", "body.custom_model", body={"model": "tr-custom-model/e2-missing"})
    add("pass:body.tags", "body.tags", body={"tags": {"k": "café"}})
    add("pass:body.referer", "body.referer", body={"http_referer": "https://user@example.com/x"})
    add("pass:body.referer:space", "body.referer", body={"http_referer": "https://example.com/a b"})
    add("pass:body.evaluator_input", "body.evaluator_input", body={"route_type": "x.unknown"})
    add("pass:header.idempotency_key:too_long", "header.idempotency_key", headers=(("idempotency-key", "k" * 257),))
    add("pass:header.idempotency_key:native_batch", "header.idempotency_key",
        body={"idempotency_key": "tr-native-batch:e2-1"})
    add("pass:header.idempotency_key:partner", "header.idempotency_key",
        body={"idempotency_key": "partner:parasail-liberty-2.0:e2-1"})
    add("pass:header.settlement_async", "header.settlement_async", headers=(("x-tr-settlement-mode", "async-v1"),))
    add("pass:header.video_providers", "header.video_providers",
        headers=(("x-quill-video-allowed-providers", "wafer"),))
    # The boot: E1's signature check and decision 8.
    for mode in ("absent", "bad_signature", "malformed", "repeated"):
        add(f"pass:boot.signature:{mode}", "boot.signature", boot=mode)
    # A kid with no boot row is boot.missing (§3), whether the row is gone or
    # the header names a kid never registered: the edge sees the same miss.
    add("pass:boot.missing:unknown_kid", "boot.missing", boot="unknown_kid")
    add("pass:boot.unverified", "boot.unverified", world={"boot": {"verified": False}})
    add("pass:boot.missing", "boot.missing", world={"boot": None})
    add("pass:policy.digest_not_accepted", "policy.digest_not_accepted",
        policy={"accepted_image_digests": [OTHER_DIGEST]})
    add("pass:policy.digest_not_accepted:none", "policy.digest_not_accepted", policy={"accepted_image_digests": []})
    # Key and workspace facts.
    add("pass:key.missing", "key.missing", body={"api_key_lookup_hash": "f" * 64})
    add("pass:key.disabled", "key.disabled", world={"key": {"disabled": True}})
    add("pass:key.expired", "key.expired", world={"key": {"expires_at": "2026-01-01T00:00:00Z"}})
    add("pass:key.scope", "key.scope", world={"key": {"scopes": ["profile"]}})
    add("pass:key.app", "key.app", world={"key": {"app_id": "app_e2_missing"}})
    add("pass:key.key_cap", "key.key_cap", world={"key": {"limit_microdollars": 10_000_000}})
    add("pass:key.key_window", "key.key_window", world={"key": {"limit_daily_microdollars": 10_000_000}})
    add("pass:key.key_window:alert_only", "key.key_window",
        world={"key": {"limit_daily_microdollars": 1_000, "budget_alert_only": True}})
    add("pass:key.budget_strict", "key.budget_strict", world={"key": {"budget_strict": True}})
    add("pass:workspace.paused", "workspace.paused", world={"workspace": {"billing_pause_causes": ["abuse"]}})
    add("pass:workspace.deleted", "workspace.deleted", world={"workspace": {"deleted": True}})
    add("pass:workspace.federated", "workspace.federated",
        world={"workspace": {"federated_home": "https://home.example.invalid"}})
    add("pass:workspace.broadcast", "workspace.broadcast", world={"broadcast_destinations": 1})
    # Policy.
    add("pass:policy.pilot_off", "policy.pilot_off", policy={"fastpath_pilot_enabled": False})
    add("pass:policy.not_pilot_workspace", "policy.not_pilot_workspace",
        policy={"fastpath_pilot_workspaces": [OTHER_WS]})
    add("pass:policy.pilot_region", "policy.pilot_region", policy={"fastpath_pilot_regions": ["europe-west4"]})
    add("pass:policy.records_not_durable:settle_outbox", "policy.records_not_durable",
        policy={"settle_outbox_enabled": False})
    add("pass:policy.records_not_durable:activity_outbox", "policy.records_not_durable",
        policy={"activity_outbox_enabled": False})
    add("pass:policy.async_settle_shadow", "policy.async_settle_shadow",
        policy={"async_settle_shadow_workspaces": [COHORT_WS]})
    add("pass:policy.monitor_key", "policy.monitor_key", policy={"monitor_key": True})
    # Evaluation.
    add("pass:evaluate.refused:unknown_model", "evaluate.refused", body={"model": "nope/unknown-model"})
    add("pass:evaluate.refused:region_denied", "evaluate.refused", body={"model": FUGU, "region": "europe-west4"})
    add("pass:evaluate.refused:no_origin", "evaluate.refused", body={"model": BEDROCK})
    add("pass:evaluate.refused:context", "evaluate.refused", body={"model": FLASH, "estimated_input_tokens": 70_000})
    add("pass:evaluate.refused:unknown_provider", "evaluate.refused", body={"provider": {"only": ["nope-provider"]}})
    add("pass:evaluate.refused:min_privacy_spelling", "evaluate.refused", body={"provider": {"min_privacy": "secret"}})
    add("pass:evaluate.refused:min_privacy_empties", "evaluate.refused",
        body={"model": FLASH, "provider": {"min_privacy": "confidential"}})
    add("pass:evaluate.refused:unsupported_parameter", "evaluate.refused",
        body={"requested_parameters": ["tools"], "provider": {"only": ["together"]}})
    add("pass:evaluate.refused:require_parameters_not_boolean", "evaluate.refused",
        body={"provider": {"require_parameters": "true"}})
    add("pass:candidates.byok", "candidates.byok", body={"model": GEMINI}, world={"byok_providers": ["google-ai-studio"]})
    add("pass:candidates.byok:only", "candidates.byok", body={"model": GEMINI, "provider": {"usage": "byok"}},
        world={"byok_providers": ["google-ai-studio"]})
    for model_id in sorted(ESTIMATED_USAGE_SNAPSHOT_MODELS):
        add(f"pass:candidates.estimated_usage:{model_id}", "candidates.estimated_usage",
            body={"model": model_id}, catalog=(model_id,))
    add("pass:candidates.private_proxy", "candidates.private_proxy", body={"model": "trustedrouter/dev-1.0"})
    # Admission and insert.
    for status in ("busy", "off", "invalid", "failed"):
        add(f"pass:owner.{status}", f"owner.{status}", door={"Status": status})
    add("pass:owner.unreachable", "owner.unreachable", door=None)
    leased_first = (({"idempotency_key": "replayed"}, ADMIT),)
    todays_first = (({"idempotency_key": "replayed"}, {"Status": "busy"}),)
    add("pass:insert.scope_taken:same_body", "insert.scope_taken", body={"idempotency_key": "replayed"},
        prior=leased_first)
    add("pass:insert.scope_taken:header", "insert.scope_taken", headers=(("idempotency-key", "replayed"),),
        prior=leased_first)
    add("pass:insert.scope_taken:body_over_header", "insert.scope_taken", body={"idempotency_key": "replayed"},
        headers=(("idempotency-key", "other"),), prior=leased_first)
    add("pass:insert.scope_taken:different_body", "insert.scope_taken",
        body={"idempotency_key": "replayed", "max_output_tokens": 7}, prior=leased_first)
    add("pass:insert.scope_taken:todays_path", "insert.scope_taken", body={"idempotency_key": "replayed"},
        prior=todays_first)
    add("pass:insert.scope_taken:todays_path_different_body", "insert.scope_taken",
        body={"idempotency_key": "replayed", "max_output_tokens": 7}, prior=todays_first)

    # §3's content-type rule: exactly one application/json, optionally
    # charset=utf-8.
    add("answer:content_type_json_charset", content_type="application/json; charset=utf-8")
    add("pass:body.content_type:missing", "body.content_type", content_type=None)
    add("pass:body.content_type:text", "body.content_type", content_type="text/plain")
    add("pass:body.content_type:latin1", "body.content_type", content_type="application/json; charset=latin-1")
    add("pass:body.content_type:repeated", "body.content_type", headers=(("content-type", "application/json"),))
    # body.unparseable, each cause: not an object (above), a UTF-8 BOM,
    # nesting past 128, an integer over 4,000 digits, a body over 4 MiB.
    # Python's json refuses an integer over 4,300 digits and nesting past
    # its recursion limit (400 http_error); 5,000 levels and 4,001 digits it
    # takes.
    plain = (f'"model":"{GLM}"', '"estimated_input_tokens":1000', PLAIN_TAIL)
    add("pass:body.unparseable:bom", "body.unparseable", "\ufeff" + raw_body(lookup, *plain))
    add("pass:body.unparseable:depth_129", "body.unparseable",
        raw_body(lookup, *plain, '"x_deep":' + "[" * 128 + "1" + "]" * 128))
    add("pass:body.unparseable:depth_5000", "body.unparseable",
        raw_body(lookup, *plain, '"x_deep":' + "[" * 5000 + "1" + "]" * 5000))
    add("pass:body.unparseable:depth_100000", "body.unparseable",
        raw_body(lookup, *plain, '"x_deep":' + "[" * 100_000 + "1" + "]" * 100_000))
    add("answer:depth_128", None, raw_body(lookup, *plain, '"x_deep":' + "[" * 127 + "1" + "]" * 127))
    add("pass:body.unparseable:int_4001_digits", "body.unparseable", raw_body(lookup, *plain, '"x_big":' + "9" * 4001))
    add("pass:body.unparseable:int_4301_digits", "body.unparseable", raw_body(lookup, *plain, '"x_big":' + "9" * 4301))
    add("answer:int_4000_digits", None, raw_body(lookup, *plain, '"x_big":' + "9" * 4000))
    add("pass:body.unparseable:over_4_mib", "body.unparseable",
        raw_body(lookup, *plain, '"x_pad":"@@PAD@@"'),
        body_expand={"marker": "@@PAD@@", "repeat": "p", "count": 4 * 1024 * 1024})
    # An estimate past int64: the fake store keeps it (a real Spanner INT64
    # column would refuse the commit), so Python's answer here is the fake's.
    add("pass:evaluate.estimate_overflow", "evaluate.estimate_overflow",
        body={"model": GEMINI, "max_output_tokens": 2**63 - 1})
    # The schema strips the top-level keys of dict fields (str_strip_whitespace
    # on dict[str, Any]); keys that collide after the strip keep the last
    # value. Go passes on any such key, or one outside printable ASCII.
    add("pass:body.dict_key_strip:metadata", "body.dict_key_strip", body={"metadata": {" k ": "v"}})
    add("pass:body.dict_key_strip:trace", "body.dict_key_strip", body={"trace": {" id": "t"}})
    add("pass:body.dict_key_strip:provider", "body.dict_key_strip", body={"provider": {" sort": "price"}})
    add("pass:body.dict_key_strip:collide", "body.dict_key_strip", body={"metadata": {"k": "first", " k": "last"}})
    add("pass:body.dict_key_strip:non_ascii_key", "body.dict_key_strip", body={"metadata": {"clé": "v"}})
    add("pass:body.dict_key_strip:tags", "body.dict_key_strip", body={"tags": {" env ": "dev"}})
    add("pass:body.dict_key_strip:tags_collide", "body.dict_key_strip", body={"tags": {"env": "a", "env ": "b"}})
    add("answer:extra_null_dropped", None, raw_body(lookup, *plain, '"x_null":null,"x_kept":"v"'))
    add("answer:nested_null_kept", body={"metadata": {"k": None}, "x_list": [None]})
    add("answer:app_categories_empty", body={"app_categories": []})
    # Idempotency-Key header edges: a byte outside ASCII (Starlette decodes
    # the header as latin-1) and the header sent twice (Starlette reads the first).
    add("pass:header.idempotency_key:non_ascii", "header.idempotency_key", headers=(("idempotency-key", "clé-1"),))
    add("pass:header.idempotency_key:repeated", "header.idempotency_key",
        headers=(("idempotency-key", "first-key"), ("idempotency-key", "second-key")))
    # Internal-only models with an ordinary key (decision 19).
    add("pass:body.internal_model:monitor", "body.internal_model", body={"model": "trustedrouter/monitor"})
    add("pass:body.internal_model:fortanix_test", "body.internal_model", body={"model": "trustedrouter/fortanix-test"})
    add("pass:body.internal_model:fallback", "body.internal_model", body={"models": ["trustedrouter/monitor"]})
    # Merged tags Python refuses: count, bytes, corrupt stored tags.
    add("pass:tags.merged_invalid:over_50", "tags.merged_invalid", body={"tags": {f"r{i}": "v" for i in range(25)}},
        world={"key": {"tags": {f"k{i}": "v" for i in range(30)}}})
    add("pass:tags.merged_invalid:over_4096_bytes", "tags.merged_invalid",
        body={"tags": {f"r{i}": "v" * 250 for i in range(9)}},
        world={"key": {"tags": {f"k{i}": "v" * 250 for i in range(9)}}})
    add("pass:tags.merged_invalid:corrupt_stored", "tags.merged_invalid", stored_key_tags={"bad!": "x"})
    # Facts Python reads strongly and the leased path does not refuse on.
    add("pass:workspace.missing", "workspace.missing", workspace_row=False)
    add("pass:workspace.in_debt", "workspace.in_debt", world={"credit_row": {"in_debt": True}})
    add("pass:workspace.tier:low", "workspace.tier", world={"credit_row": {"trust_tier": 0}})
    add("pass:workspace.tier:latched", "workspace.tier",
        world={"credit_row": {"trust_latched_at": "2026-10-18T00:00:00Z"}})
    # A federated key, served from its local shadow: this plane has no home
    # configured, so Python makes no home-plane call (_federated_key_still_valid).
    add("pass:key.federated", "key.federated", world={"key": {"federated_home": "https://home.example.invalid"}})
    # The leased insert refused with nothing applied: proven (§8.4's Failed),
    # and not provably (Go's Unknown, the 503).
    add("pass:insert.failed:invalid_argument", "insert.failed", insert="invalid_argument")
    add("unavailable:insert_failed_precondition", insert="failed_precondition")
    add("unavailable:insert_aborted", insert="aborted")
    # A read-back answers from the stored authorization: tags sorted (§6).
    add("answer:insert_unknown_landed_tags", insert="unknown_landed", body={"tags": {"zeta": "1", "alpha": "2"}},
        world={"key": {"tags": {"mid": "k", "beta": "k"}}})
    # The owner's end of life: another offset, and a fraction past microseconds.
    add("answer:end_of_life_offset", door={**ADMIT, "EndOfLife": "2026-10-19T07:00:00+02:00"})
    add("answer:end_of_life_nanoseconds", door={**ADMIT, "EndOfLife": "2026-10-19T05:00:00.123456789Z"})
    add("answer:end_of_life_offset_nanoseconds", door={**ADMIT, "EndOfLife": "2026-10-18T23:30:00.987654321-05:30"})
    return out


# ---------------------------------------------------------- fingerprints --

FINGERPRINT_KEY = {**DEFAULT_KEY, "hash": "key_e2_fingerprint", "name": "fingerprint", "tags": {"team": "e2"}}
FINGERPRINT_WORLD = _merge(DEFAULT_WORLD, {"workspace": {"id": FINGERPRINT_WS}, "key": FINGERPRINT_KEY})
FINGERPRINT_POLICY = _merge(DEFAULT_POLICY, {"fastpath_pilot_workspaces": [FINGERPRINT_WS]})
# §10.2 asks about 20,000 under 10 MB; at these bodies' size (an enclave
# body with a 64-hex lookup hash, about 280 bytes) the size bounds it first.
FINGERPRINT_COUNT = 14_000
# §10.2's labels on which Go passes (decision 4 and §3), and one more: a
# lone surrogate escape, which Go's decoder cannot keep (§10.4 refuses it).
PASS_REQUIRED = frozenset({
    "float_present", "lax_coercion", "duplicate_key", "strip_ambiguous", "dict_key_strip",
    "non_ascii_tag", "referer_not_strict", "canonical_model_rewrite", "native_batch_key", "partner_key",
    "idempotency_key_too_long", "idempotency_header_repeated", "idempotency_header_non_ascii",
    "content_type", "deep_nesting", "int_over_4000_digits", "bom",
    "lone_surrogate",
    # Beyond §10.2: Python wrote an estimate past int64, which the fake store
    # keeps and a real Spanner INT64 column refuses (evaluate.estimate_overflow).
    "estimate_overflow",
})
INT64_MAX = 2**63 - 1
STRICT_REFERER = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][\x21-\x7e]*)?$")
JSON_CONTENT_TYPE = re.compile(r"^application/json(;\s*charset=utf-8)?$", re.IGNORECASE)
# Declared str and list[str] fields (str_strip_whitespace); input_modalities
# is a Literal list, not stripped.
_DECLARED_STRINGS = ("model", "models", "region", "service_tier", "route_type", "user", "session_id", "app",
                     "http_referer", "app_categories", "requested_parameters", "idempotency_key",
                     "invocation_nonce", "video_resolution", "api_key_hash", "api_key_lookup_hash",
                     "request_fingerprint", "cache_affinity_key")
_SENT_WITH_EVERY_BODY = [("x-trustedrouter-internal-token", INTERNAL_TOKEN), ("accept-encoding", "identity")]


def _decoded(raw: str) -> tuple[Any, set[str]]:
    """json.loads as the route's body parser runs it, and what the text holds."""
    found: set[str] = set()

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({k for k, _v in items}) != len(items):
            found.add("duplicate_key")
        return dict(items)

    def parse_float(token: str) -> float:
        found.add("float_present")
        return float(token)

    def parse_int(token: str) -> int:
        if len(token.lstrip("-")) > 4_000:
            found.add("int_over_4000_digits")
        value = int(token)
        if abs(value) >= 1 << 63:
            found.add("int_beyond_2_63")
        elif abs(value) >= 1 << 53:
            found.add("int_near_2_63")
        return value

    def parse_constant(token: str) -> float:
        found.add("float_present")  # NaN, Infinity: a float, and no JSON Go parses
        return float(token)

    if raw.startswith("\ufeff"):
        found.add("bom")
        raw = raw[1:]
    value = json.loads(raw, object_pairs_hook=pairs, parse_float=parse_float, parse_int=parse_int,
                       parse_constant=parse_constant)
    return value, found


def _depth(raw: str) -> int:
    """The deepest nesting in the text, strings skipped."""
    depth = deepest = 0
    in_string = escaped = False
    for ch in raw:
        if in_string:
            escaped, in_string = (False, True) if escaped else (ch == "\\", ch != '"')
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _has_nested_null(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        for child in children:
            if child is None:
                return True
            stack.append(child)
    return False


def _printable_ascii(text: str) -> bool:
    return all(" " <= ch <= "~" for ch in text)


def body_labels(raw: str, headers: list[list[str]]) -> list[str]:
    """§10.2's labels for one request's headers and body."""
    labels: set[str] = set()
    content_types = [v for k, v in headers if k == "content-type"]
    if len(content_types) != 1 or not JSON_CONTENT_TYPE.match(content_types[0].strip()):
        labels.add("content_type")
    header_keys = [v for k, v in headers if k == "idempotency-key"]
    if header_keys:
        labels.add("idempotency_header")
    if len(header_keys) > 1:
        labels.add("idempotency_header_repeated")
    if any(not _printable_ascii(v) for v in header_keys):
        labels.add("idempotency_header_non_ascii")
    if _depth(raw) > 128:
        labels.add("deep_nesting")
    try:
        value, found = _decoded(raw)
    except (ValueError, RecursionError):
        # Undecodable as Python decodes it: what the text shows is all there is.
        if raw.startswith("\ufeff"):
            labels.add("bom")
        if re.search(r"\d{4001}", raw):
            labels.add("int_over_4000_digits")
        return sorted(labels)
    labels |= found
    if not isinstance(value, dict):
        return sorted(labels)
    fields = GatewayAuthorizeRequest.model_fields
    extras = [k for k in value if k not in fields]
    if extras:
        labels.add("extras")
        if any(not _printable_ascii(s) for k in extras for s in [k, *_strings(value[k])]):
            labels.add("unicode_extra")
    if any(value[k] is None for k in extras):
        labels.add("null_extra")
    if any(isinstance(v, dict | list) and _has_nested_null(v) for v in value.values()):
        labels.add("null_nested")
    if any(0xD800 <= ord(ch) <= 0xDFFF for s in _strings(value) for ch in s):
        labels.add("lone_surrogate")
    declared = [s for name in _DECLARED_STRINGS for s in _strings(value.get(name))]
    if any(not _printable_ascii(s) for s in declared):
        labels.add("strip_ambiguous")
    if any(s != s.strip() for s in declared):
        labels.add("whitespace")
    if any(isinstance(value.get(name), dict) and any(k != k.strip() or not _printable_ascii(k) for k in value[name])
           for name in ("provider", "trace", "metadata", "tags")):
        labels.add("dict_key_strip")
    for name, label in (("provider", "nested_provider"), ("trace", "nested_trace"), ("metadata", "nested_metadata")):
        if isinstance(value.get(name), dict) and value[name]:
            labels.add(label)
    if any(value.get(name) is not None for name in ("user", "session_id", "trace", "app", "http_referer", "app_categories")):
        labels.add("attribution")
    if value.get("app_categories") == []:
        labels.add("app_categories_empty")
    if value.get("input_modalities") is not None:
        labels.add("input_modalities")
    if value.get("tags") is not None:
        labels.add("tags" if value["tags"] != {} else "tags_empty")
        if any(not _printable_ascii(s) for s in _strings(value["tags"])):
            labels.add("non_ascii_tag")
    referer = value.get("http_referer")
    if isinstance(referer, str) and value.get("app") is None and not STRICT_REFERER.match(referer.strip()):
        labels.add("referer_not_strict")
    if isinstance(value.get("models"), list):
        labels.add("models_list")
    if "allow_fallbacks" in value or (isinstance(value.get("provider"), dict) and "allow_fallbacks" in value["provider"]):
        labels.add("allow_fallbacks_spelling")
    if value.get("invocation_nonce") is not None:
        labels.add("invocation_nonce")
    if "estimated_input_tokens" not in value:
        labels.add("defaults")
    models = [value.get("model"), *(value["models"] if isinstance(value.get("models"), list) else [])]
    if any(isinstance(m, str) and canonical_router_model_id(m.strip()) != m.strip() for m in models):
        labels.add("canonical_model_rewrite")
    if value.get("idempotency_key") is not None:
        labels.add("idempotency_body")
    presented = [k.strip() for k in (value.get("idempotency_key"), *header_keys[:1]) if isinstance(k, str)]
    if any(k.startswith("tr-native-batch:") for k in presented):
        labels.add("native_batch_key")
    if any(k.startswith("partner:") for k in presented):
        labels.add("partner_key")
    if any(len(k) > 256 for k in presented):
        labels.add("idempotency_key_too_long")
    try:
        GatewayAuthorizeRequest.model_validate(value, strict=True)
    except ValidationError:
        try:
            GatewayAuthorizeRequest.model_validate(value)
        except ValidationError:
            pass
        else:
            labels.add("lax_coercion")
    return sorted(labels)


_FINGERPRINT_SETTINGS = settings_for(FINGERPRINT_POLICY, FINGERPRINT_KEY)

# Building blocks for the bodies. A few fields of a plain enclave body are
# drawn from these per body (a fixed seed); each body is then written one
# of several ways and sent with one of several header sets.
_UNICODE = ("café", "日本語", "emoji 😀", "zero\u0000width", "line\u2028sep", "tab\tquote\"", "slash/\\back",
            "\u007f del", "ñ", "Ω≈ç√", "\ufeffbom")
_INTS = (0, 1, 7, 512, 1_000, 65_536, 2**31 - 1, 2**31, 2**53 - 1, 2**53, 2**53 + 1, 2**62,
         2**63 - 1, 2**63, 2**64, -1, -(2**63))
_FLOATS = (0.0, 1.0, 0.5, 0.1, 1e-05, 1.5e-7, 2.5, 100.0, 1e16, 1.7976931348623157e308, -0.0, 123456.789)


def _random_value(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.randrange(9 if depth < 3 else 6)
    if kind == 0:
        return rng.choice(_INTS)
    if kind == 1:
        return rng.choice(_FLOATS)
    if kind == 2:
        return rng.choice(_UNICODE)
    if kind == 3:
        return rng.choice(("plain", "  padded  ", "", "x" * rng.randrange(1, 40)))
    if kind == 4:
        return rng.choice((True, False, None))
    if kind == 5:
        return rng.choice(_INTS + ("a", "b"))
    if kind in (6, 7):
        return {f"k{index}{rng.choice(('', 'é', '_x'))}": _random_value(rng, depth + 1)
                for index in range(rng.randrange(0, 4))}
    return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))]


def _field_options() -> dict[str, tuple[Any, ...]]:
    """Per field, values to choose among (``...`` = leave it out)."""
    absent = ...
    return {
        "model": (FLASH, GEMINI, f"  {GLM} ", f"{GLM}:floor", "nyte/auto", "trustedrouter/auto",
                  f"{FLASH}:nitro", f"{GLM}-2025-04-14", "nope/unknown-model", "", 7, "glm\t5"),
        "models": (absent, None, [], [FLASH], [FLASH, GEMINI], [f" {FLASH} "], ["trustedrouter/auto-routing"],
                   "not-a-list", [1]),
        "estimated_input_tokens": (absent, 0, 1, 100_000, 2**31, 2**53 + 1, 2**63 - 1, 2**63,
                                   "1000", 1000.0, 1.5, -1, True, None),
        "max_output_tokens": (absent, 1, 4_096, 2**63 - 1, "512", 512.0, 0, None),
        "max_tokens": (64, "64", None),
        "max_completion_tokens": (128, 128.0),
        "stream": (False, True, None, "false", 0, 1, "yes"),
        "route_type": ("chat.completions", " responses ", "messages", None),
        "region": ("us-east4", " us-east4 ", "", "unknown-region", None),
        "service_tier": ("auto", " flex ", "default", ""),
        "provider": (None, {}, {"order": ["wafer", "together"]}, {"sort": "price"},
                     {"sort": {"by": "latency", "partition": "none"}}, {"allow_fallbacks": False},
                     {"allow_fallbacks": "false"}, {"allow_fallbacks": 0},
                     {"max_price": {"prompt": 0.5, "completion": 2}},
                     {"only": ["wafer"], "note": "é"}, {"big": 2**63}, {"zdr": True}, {" sort": "price"},
                     {"sort": "price", " sort ": "latency"}, {"sort": None}),
        "allow_fallbacks": (True, False, "false", 0, 1, None),
        "requested_parameters": (["tools"], [], [" tools "]),
        "input_modalities": (["text"], ["text", "image"], ["sound"], [" text"]),
        "user": ("user-1", "  user-2  ", "ü", "u" * 257),
        "session_id": ("s-1", " s-2 "),
        "trace": ({"trace_id": "t"}, {"n": 1.25}, {"deep": {"a": {"b": [1, "é"]}}}, {"x": 2**63}, {" id ": "t"},
                  {"span": None}),
        "metadata": ({"k": "v"}, {"temperature": 0.7}, {"u": "日本"}, {"big": -(2**63)}, {},
                     {"k": "first", " k": "last"}, {" k ": {" inner ": 1}}, {"clé": "v"}, {"n": [None]}),
        "tags": (None, {}, {"team": "a"}, {"env": "prod", "team": "b"}, {"équipe": "x"},
                 {"k": "café"}, {"bad!": "x"}, {"k": 1}, {"aws:x": "y"}, {"sp ace": "v v"},
                 {f"t{i}": "v" for i in range(50)}, {" env ": "x"}, {"env": "a", "env ": "b"}),
        "app": ("My App", "  padded app  ", "Ünicode App"),
        "http_referer": ("https://example.com/x", "https://Example.COM:8443/p?q=1", "http://exämple.com/",
                         "https://user@example.com/", "ftp://example.com", "https://"),
        "app_categories": (["cli-agent"], ["Bad Category"], []),
        "idempotency_key": ("client-key", "  spaced  ", "k" * 256, "k" * 257, "tr-native-batch:x",
                            "partner:parasail-liberty-2.0:x", "", "é-key"),
        "invocation_nonce": ("nonce-1",),
        "inference_receipt": (False, True),
        "additional_cost_reservation_microdollars": (0, 5_000),
        "cache_affinity_key": ("a" * 64, "not-hex"),
        "cache_affinity_explicit": (True, False),
    }


def _write(value: dict[str, Any], rng: random.Random) -> str:
    """The body's text in one of several spellings: the same JSON value
    each way except where a spelling changes it (a duplicated key)."""
    style = rng.randrange(8)
    items = list(value.items())
    if style == 1:
        rng.shuffle(items)
        return json.dumps(dict(items), separators=(",", ":"))
    if style == 2:
        return json.dumps(value, separators=(", ", ":"), ensure_ascii=rng.random() < 0.5).replace(", ", ",\n", 2)
    if style == 3:
        return json.dumps(value, separators=(", ", ": "))
    if style == 4 and items:
        # A key written twice; the decoder keeps the last.
        name, last = rng.choice(items)
        first = rng.choice((last, "shadowed", 0, None))
        head = json.dumps({name: first}, separators=(",", ":"))[:-1]
        return head + "," + json.dumps(value, separators=(",", ":"), ensure_ascii=False)[1:]
    if style == 5:
        # Integral numbers in another spelling: 1000.0, 1e3, 1E+0.
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        for name in ("estimated_input_tokens", "max_output_tokens"):
            number = value.get(name)
            if type(number) is int and 0 < number < 10**15:
                spelled = rng.choice((f"{number}.0", f"{number}e0", f"{number / 1000:g}e3", f"{number}E+0"))
                text = text.replace(f'"{name}":{number}', f'"{name}":{spelled}', 1)
        return text
    if style == 6:
        # Escapes JSON allows anywhere: \/ and ..
        text = json.dumps(value, separators=(",", ":"))
        return text.replace("chat.completions", "chat\\u002ecompletions").replace("/", "\\/")
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


_CONTENT_TYPES = (("application/json",) * 24 + (None, "text/plain", "application/json; charset=utf-8",
                                                 "application/json; charset=latin-1", "APPLICATION/JSON"))
_IDEMPOTENCY_HEADERS: tuple[tuple[str, ...], ...] = (
    ((),) * 22 + (("hdr-key",), (" hdr-spaced ",), ("k" * 257,), ("tr-native-batch:h",), ("",), ("\t",),
                  ("first", "second"), ("clé",), ("日本",))
)


def _headers(rng: random.Random) -> list[list[str]]:
    content_type = rng.choice(_CONTENT_TYPES)
    headers = [] if content_type is None else [["content-type", content_type]]
    if rng.random() < 0.01:
        headers.append(["content-type", "application/json"])  # sent twice
    headers += [["idempotency-key", value] for value in rng.choice(_IDEMPOTENCY_HEADERS)]
    return headers


def _nested(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = [value]
    return value


def fingerprint_bodies() -> list[dict[str, Any]]:
    rng = random.Random(20261019)  # noqa: S311 - fixed: the file is a function of this text
    lookup = lookup_hash(FINGERPRINT_KEY["hash"])
    options = _field_options()
    fields = list(options)
    json_only = [["content-type", "application/json"]]
    out: list[dict[str, Any]] = []
    while len(out) < FINGERPRINT_COUNT:
        value: dict[str, Any] = {"api_key_lookup_hash": lookup, "model": GLM, "estimated_input_tokens": 1_000,
                                 "max_output_tokens": 512}
        for name in rng.sample(fields, rng.choice((0, 1, 1, 2, 2, 3, 3, 4, 5, 7))):
            choice = rng.choice(options[name])
            if choice is ...:
                value.pop(name, None)
            else:
                value[name] = choice
        for _ in range(rng.choice((0, 0, 0, 0, 1, 1, 2))):
            value[f"x_{rng.choice(('extra', 'opt', 'é', 'nested'))}_{rng.randrange(100)}"] = _random_value(rng)
        if rng.random() < 0.03:
            value = {"api_key_hash": FINGERPRINT_KEY["hash"],
                     **{k: v for k, v in value.items() if k != "api_key_lookup_hash"}}
        draw = rng.random()
        if draw < 0.002:
            # Nesting around §3's 128 levels, and integers around 4,000 digits.
            value["x_deep"] = _nested(rng.randrange(120, 200))
        elif draw < 0.004:
            value["x_digits"] = int("9" * rng.randrange(3_990, 4_100))
        body = _write(value, rng)
        if rng.random() < 0.005:
            body = "\ufeff" + body
        out.append({"body": body, "headers": _headers(rng)})
    # Bodies no draw spells: non-finite numbers, lone surrogates, integers
    # around Python's digit limit, nesting around 128 levels and past the
    # recursion limit, a body that is not an object, invalid JSON, nothing.
    for raw in (
        raw_body(lookup, f'"model":"{GLM}"', '"metadata":{"x":NaN}'),
        raw_body(lookup, f'"model":"{GLM}"', '"metadata":{"x":Infinity}'),
        raw_body(lookup, f'"model":"{GLM}"', '"user":"\\ud800"'),
        raw_body(lookup, f'"model":"{GLM}"', '"x_extra":"\\udfff\\ud83d\\ude00"'),
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":-0'),
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":1e400'),
        raw_body(lookup, f'"model":"{GLM}"', '"max_output_tokens":1.0000000000000001'),
        raw_body(lookup, f'"model":"{GLM}"', '"x":12345678901234567890123'),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4000),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4001),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4300),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4301),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 127 + "1" + "]" * 127),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 128 + "1" + "]" * 128),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 900 + "1" + "]" * 900),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 100_000 + "1" + "]" * 100_000),
        raw_body(lookup, f'"model":"{GLM}"', '"x_null":null,"tags":{" a ":"1","a":"2"}'),
        "\ufeff" + raw_body(lookup, f'"model":"{GLM}"'),
        '["not", "an", "object"]', '"text"', "null", '{"api_key_lookup_hash":', "",
    ):
        out.append({"body": raw, "headers": json_only})
    return out


def fingerprint_vectors() -> list[dict[str, Any]]:
    """Every body through the app, each in a fresh world: the fingerprint
    and scope from the reservation row it wrote, or the refusal."""
    key_hash = FINGERPRINT_KEY["hash"]
    entries: list[dict[str, Any]] = []
    with pinned(ROUTING._instant(INSTANTS["peak"]), "fingerprints"), front_door(ADMIT):
        client = _client(_FINGERPRINT_SETTINGS)
        for index, entry in enumerate(fingerprint_bodies()):
            name = f"fp-{index:05d}"
            ids = case_ids(name)
            _store, database = build_world(FINGERPRINT_WORLD, FINGERPRINT_POLICY)
            gateway._BROADCAST_EMPTY_CACHE.clear()
            with given_ids(ids):
                # The cohort path (§10.2: sent as §10.1's are): every body
                # carries the TEST boot's signature over its exact bytes.
                response = client.post(PATH, content=entry["body"].encode(), headers=[
                    (k.encode(), v.encode())
                    for k, v in [*entry["headers"], *_SENT_WITH_EVERY_BODY, *boot_headers("signed", entry["body"].encode())]])
            if response.status_code == 200:
                (reservation,) = database.reservations.values()
                (authorization,) = database.gateway_authorizations.values()
                key = json.loads(authorization["payload"])["idempotency_key"]
                presented = None if key == ids["minted_idempotency_key"] else key
                expect = {"outcome": "fingerprint", "fingerprint": reservation["idempotency_fingerprint"],
                          "idempotency_key": presented,
                          "idempotency_scope": reservation["idempotency_scope"] if presented else None}
                if presented and reservation["idempotency_scope"] != scope_of(FINGERPRINT_WS, key_hash, presented):
                    raise AssertionError(f"{name}: the scope is not the presented key's")
                overflow = authorization["estimated_microdollars"] > INT64_MAX
            else:
                overflow = False
                expect = {"outcome": "refused", "status": response.status_code,
                          "type": _error(response.text).get("type")}
            labels = body_labels(entry["body"], entry["headers"])
            entries.append({
                "name": name, "labels": sorted([*labels, "estimate_overflow"]) if overflow else labels,
                "workspace_id": FINGERPRINT_WS, "key_hash": key_hash,
                "headers": entry["headers"], "body": entry["body"], "expect": expect,
            })
    return entries


# ---------------------------------------------------------------- render --

def _lines(header: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """One case a line, as routing_cases_v1.json, so a change shows as the
    cases it changes; UTF-8 text as itself (§10: a body's bytes as a string)."""
    head = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False)[:-1]
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in rows]
    return head + ',\n "cases": [\n  ' + ",\n  ".join(lines) + "\n ]}\n"


# The TEST boot key and the signature every signed request carries, so Go
# can make the same bytes (src/trusted_router/gateway_boot.py:53-58).
BOOT_TEST_KEY = {
    "kid": BOOT_KID, "jwk": BOOT_JWK,
    "seed_hex": BOOT_SEED.hex(), "seed": "sha256 of seed_preimage (UTF-8): the Ed25519 private key's 32-byte seed",
    "seed_preimage": BOOT_SEED_PREIMAGE.decode(),
}
BOOT_AUTH_RULE = {
    "source": "src/trusted_router/gateway_boot.py:53-58 (boot_auth_digest)",
    "digest": "sha256(domain || method || path || sha256(body)): domain the ASCII bytes of domain, method "
              "\"POST\", path the request path, both UTF-8, sha256(body) the 32 raw bytes of the body's "
              "digest; concatenated with no separators",
    "domain": BOOT_AUTH_DOMAIN.decode(), "method": "POST", "path": PATH,
    "signature": "Ed25519 (RFC 8032, deterministic) over the 32-byte digest, as base64url without padding",
    "header": "x-tr-boot-auth: kid=<kid>,sig=<signature>",
}


def internal_models() -> list[str]:
    """Decision 19: the monitor model and every internal_only catalog model."""
    return sorted({MONITOR_MODEL_ID, *(m.id for m in MODELS.values() if m.internal_only)})


def render_cases() -> str:
    header_policy = edge_policy_body(DEFAULT_POLICY, DEFAULT_KEY, ROUTING._instant(INSTANTS["peak"]))
    rows = []
    for case in sorted(cases(), key=lambda c: c["name"]):
        at = ROUTING._instant(case["_at"])
        policy = edge_policy_body(case["_policy"], case["_world"]["key"], at)
        result = run_case(case)
        row = {k: v for k, v in case.items() if not k.startswith("_")}
        row.update(result)
        row["at_us"] = _us(at)
        row["policy_patch"] = {k: v for k, v in policy.items() if v != header_policy[k]}
        row["labels"] = case_labels(case, result["expect"])
        rows.append(row)
    names = [row["name"] for row in rows]
    if len(set(names)) != len(names):
        raise AssertionError("case names must be unique")
    snapshot = (ROOT / SNAPSHOT).read_bytes()
    return _lines({
        "generator": GENERATOR, "snapshot": SNAPSHOT, "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
        "plane": PLANE, "policy": header_policy, "boot_test_key": BOOT_TEST_KEY, "boot_auth": BOOT_AUTH_RULE,
        "internal_token": INTERNAL_TOKEN,
        "body_expand": ("a request with body_expand is sent with its body's marker, which occurs exactly "
                        "once, replaced by repeat written count times; the boot signature and the "
                        "request are over those expanded bytes"),
        "internal_models": internal_models(),
        "estimated_usage_models": sorted(ESTIMATED_USAGE_SNAPSHOT_MODELS),
        # @timed_gateway_async ends every answer's data, and the 503's
        # top-level data, with timing: its shape is fixed, its values are
        # Python's own measurements (pinned here to 0 ms; spanner_rpcs is the
        # fake's count). Compare it by shape, not value.
        "volatile": ["data.timing"],
    }, rows)


def render_fingerprints() -> str:
    return _lines({
        "generator": GENERATOR,
        # Sent in this order: the case's headers, then these three.
        "sent_with_every_body": [*(list(pair) for pair in _SENT_WITH_EVERY_BODY),
                                 ["x-tr-boot-auth", "kid=<boot_test_key.kid>,sig=<boot_auth.signature>"]],
        "boot_test_key": BOOT_TEST_KEY,
        "boot_auth": BOOT_AUTH_RULE,
        "policy": edge_policy_body(FINGERPRINT_POLICY, FINGERPRINT_KEY, ROUTING._instant(INSTANTS["peak"])),
    }, fingerprint_vectors())


RENDERS = {"cases": (CASES_OUTPUT, render_cases), "fingerprints": (FINGERPRINT_OUTPUT, render_fingerprints)}

if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--print"]:
        sys.stdout.write(RENDERS[args[1]][1]())
    else:
        out_dir = Path(args[args.index("--out") + 1]) if "--out" in args else ROOT
        for relative, render in RENDERS.values():
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render(), encoding="utf-8")
            print(f"wrote {target}")
