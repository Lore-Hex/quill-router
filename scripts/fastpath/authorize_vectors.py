"""Golden authorize vectors for the Go edge's decide path (E2 P1,
docs/design/fast-admission-e2-decide.md §10.1 and §10.2).

Both files are computed by the real authorize route (FastAPI's app over
`_authorize_gateway_sync_impl`) over the routing-snapshot fixture catalog
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

fastpath/testdata/authorize_fingerprint_v1.json, one request body a line,
each taken by the route as far as the idempotency fingerprint (§7) or its
refusal, and labelled with what it contains; the labels decision 4 names
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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.api_core.exceptions import DeadlineExceeded  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from starlette.requests import Request  # noqa: E402

from tests.fakes.spanner import make_fake_store  # noqa: E402
from tests.test_fastpath_frontdoor import _Door, _transport  # noqa: E402
from trusted_router import gateway_timing, storage_gcp, storage_gcp_keys  # noqa: E402
from trusted_router.auth import is_api_key_expired  # noqa: E402
from trusted_router.catalog_data import Model, ModelEndpoint  # noqa: E402
from trusted_router.catalog_registry import MODEL_ENDPOINTS, MODELS  # noqa: E402
from trusted_router.config import Settings  # noqa: E402
from trusted_router.gateway_boot import GATEWAY_BOOT_KIND, boot_auth_digest  # noqa: E402
from trusted_router.main import create_app  # noqa: E402
from trusted_router.model_aliases import canonical_router_model_id  # noqa: E402
from trusted_router.pricing import PriceTier  # noqa: E402
from trusted_router.receipt_keys import b64url_encode, normalize_receipt_jwk, receipt_kid  # noqa: E402
from trusted_router.regions import configured_regions  # noqa: E402
from trusted_router.routes.internal import gateway  # noqa: E402
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
SNAPSHOT = "fastpath/testdata/routing_snapshot_v1.body.json"
PATH = "/internal/gateway/authorize"
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
BOOT_SEED = hashlib.sha256(b"trustedrouter authorize vectors v1 boot TEST key; not a secret").digest()
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
        "issued_at": issued_at, "not_after": issued_at + 120, "refresh_at": issued_at + 30,
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
    byok, broadcast = ids("byok_provider"), ids("broadcast_destination_by_workspace")
    spec = world["workspace"]
    causes = sorted(set(spec["billing_pause_causes"]).union(*(row["billing_pause_causes"] for row in credit_rows)))
    return {
        "key": {"lookup_body": body("api_key_lookup", lookup_hash(key["hash"])), "key_body": body("api_key", key["hash"])},
        "workspace": {"body": body("workspace", workspace_id), "credit_rows": credit_rows},
        "byok_entity_ids": byok,
        "broadcast_pointer_ids": broadcast,
        "facts": {
            "key": key_facts(key, workspace_id),
            "workspace": {
                "workspace_id": workspace_id, "pause_causes": causes,
                "in_debt": any(row["in_debt"] for row in credit_rows),
                "trust_tier": min(0 if row["trust_latched"] else row["trust_tier"] for row in credit_rows),
                "federated": bool(spec["federated_home"]), "deleted": spec["deleted"],
                "byok_slugs": sorted(eid.split("#", 1)[1] for eid in byok),
                "broadcast_pointer": bool(broadcast),
            },
        },
    }


def key_facts(key: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """§8.1's KeyFacts, the reasons in Python's order, by Python's predicates."""
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
    return {
        "lookup_hash": lookup_hash(key["hash"]), "eligible": not reason, "reason": reason,
        "hash": key["hash"], "workspace_id": workspace_id, "expires_at_us": _us(key["expires_at"]),
        "tags": [[k, v] for k, v in key["tags"].items()],
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

_APPS: dict[str, Any] = {}


def _client(settings: Settings) -> TestClient:
    """One app per distinct settings (an app is built over its settings)."""
    cache_key = settings.model_dump_json()
    if cache_key not in _APPS:
        _APPS[cache_key] = TestClient(create_app(settings, configure_store_arg=False, init_observability=False))
    return _APPS[cache_key]


def send(client: TestClient, request: dict[str, Any]) -> Any:
    return client.post(request["path"], content=request["body"].encode(),
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
    """The leased insert's commit: as it is, or applied (or not) with its
    answer lost to a deadline, which Python resolves by reading back."""
    if insert == "inserted":
        yield
        return

    def lost(database: Any, txn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") != "tr_authorize_leased":
            return _REAL_TRANSACTION(database, txn, **kwargs)
        if insert == "unknown_landed":
            _REAL_TRANSACTION(database, txn, **kwargs)
        raise DeadlineExceeded("the commit's answer was lost")

    with patched(storage_gcp, "run_in_transaction_with_retry", lost):
        yield


def _row_timestamps(row: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {f"{name}_us" if name in names else name: _us(value) if name in names else value
            for name, value in row.items()}


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    at = ROUTING._instant(case["_at"])
    world, policy = case["_world"], case["_policy"]
    with pinned(at, case["name"]), contextlib.ExitStack() as stack:
        for model, endpoint in (_catalog_addition(model_id) for model_id in case["_catalog"]):
            MODELS[model.id] = model
            MODEL_ENDPOINTS[endpoint.id] = endpoint
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
        response = send(client, case["request"])
        authorizations = [row for k, row in database.gateway_authorizations.items() if k not in authorizations_before]
        reservations = [row for k, row in database.reservations.items() if k not in reservations_before]
    if response.headers.get("content-encoding"):
        raise AssertionError("the response was compressed")
    body = response.content.decode()
    leased = bool(authorizations and json.loads(authorizations[0]["payload"]).get("fastpath_envelope"))
    reason = case["_reason"]
    if reason is not None:
        error = (json.loads(body).get("error") or {}) if response.status_code != 200 else {}
        return {**world_rows, "expect": {"outcome": "pass", "reason": reason, "python": {
            "status": response.status_code, "type": error.get("type"), "leased": leased}}}
    if response.status_code == 503:
        if authorizations or [path for path, _sent in door.seen] != ["/v1/authorize"]:
            raise AssertionError(f"{case['name']}: a 503 that wrote or refunded")
        return {**world_rows, "expect": {"outcome": "unavailable", "status": 503,
                                         "headers": [["retry-after", response.headers["retry-after"]]],
                                         "response": body}}
    if not (response.status_code == 200 and leased):
        raise AssertionError(f"{case['name']}: an answer case Python did not lease: {response.status_code} {body}")
    (authorization,), (reservation,) = authorizations, reservations
    if (authorization["authorization_id"], reservation["reservation_id"]) != (
            case["ids"]["authorization_id"], case["ids"]["reservation_id"]):
        raise AssertionError(f"{case['name']}: the route minted other ids")
    data = json.loads(body)["data"]
    fingerprint, material = recorder.seen[-1]
    return {**world_rows, "expect": {
        "outcome": "answer", "status": 200, "response": body,
        "generation_id": data["generation_id"],
        "estimate_microdollars": data["estimated_cost_microdollars"],
        "decide_region": data["region"],
        "fingerprint": fingerprint,
        "fingerprint_material": material,
        "idempotency_scope": reservation["idempotency_scope"],
        "authorization_row": _row_timestamps(authorization, ("created_at", "terminal_at", "started_at", "heartbeat_at")),
        "reservation_row": _row_timestamps(
            {k: v for k, v in reservation.items() if k not in {"settled", "settled_usage_type", "actual_micro", "terminal_at"}},
            ("expires_at", "created_at")),
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
             headers: tuple[tuple[str, str], ...] = (), content_type: str | None = "application/json") -> dict[str, Any]:
    if isinstance(body, dict):
        body = {"api_key_lookup_hash": lookup_hash(world["key"]["hash"]), **body}
        body = json.dumps({k: v for k, v in body.items() if v is not None}, separators=(",", ":"), ensure_ascii=False)
    return {
        "path": PATH,
        # Identity: the bytes before Python's GZip middleware, which Go
        # never applies.
        "headers": [*([["content-type", content_type]] if content_type is not None else []),
                    ["accept-encoding", "identity"], ["x-trustedrouter-internal-token", INTERNAL_TOKEN],
                    *boot_headers(boot, body.encode()), *[[k, v] for k, v in headers]],
        "body": body,
    }


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
            stored_key_tags: Any = None, workspace_row: bool = True) -> None:
        case_world = _merge(DEFAULT_WORLD, world)
        request_body = body if isinstance(body, str) else {**BASE_BODY, **(body or {})}
        boot_record = case_world["boot"]
        out.append({
            "name": name, "ids": case_ids(name), "insert": insert,
            "admitted": {"envelope": ENVELOPE, "end_of_life_us": _us(END_OF_LIFE)},
            "request": _request(case_world, request_body, boot=boot, headers=headers, content_type=content_type),
            "boot": None if boot_record is None else {**boot_record, "signature_ok": boot == "signed"},
            "_at": INSTANTS[at], "_world": case_world, "_policy": _merge(DEFAULT_POLICY, policy),
            "_front_door": door, "_reason": reason, "_catalog": catalog,
            "_stored_key_tags": stored_key_tags, "_workspace_row": workspace_row,
            "_prior": [(_request(case_world, {**BASE_BODY, **p}), answer) for p, answer in prior],
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
    # The region a request routes to, and the regions payload.
    for region in ("us-east4", "europe-west4", "us-west1", " us-east4 ", "", "unknown-region"):
        add(f"answer:region:{region.strip() or 'empty'}{':spaced' if region != region.strip() else ''}",
            body={"model": GLM, "region": region})
    add("answer:region:single_region_plane", policy={"multi_region_enabled": False})
    add("answer:route:responses", body={"route_type": "responses"})
    add("answer:route:messages", body={"route_type": "messages"})
    add("answer:route:absent", body={"route_type": None})
    # Tags: the key's stored ones, then the request's, in merge order.
    for name, key_tags, request_tags in (
        ("key_only", {"team": "core", "env": "prod"}, None),
        ("request_only", {}, {"env": "dev", "app": "cli"}),
        ("overlap", {"team": "core", "env": "prod"}, {"env": "dev", "app": "cli"}),
        ("request_empty", {"team": "core"}, {}),
        ("portable_punctuation", {}, {"a+b": "c=d", "path/x": "u@h:1.0", "space key": "v v"}),
        ("order_kept", {"z": "1", "a": "2"}, {"m": "3", "b": "4"}),
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
    add("pass:body.cache_affinity", "body.cache_affinity", body={"cache_affinity_key": "c" * 64})
    add("pass:body.priced_extra:additional_cost", "body.priced_extra",
        body={"additional_cost_reservation_microdollars": 5_000})
    add("pass:body.priced_extra:inference_receipt", "body.priced_extra", body={"inference_receipt": True})
    add("pass:body.canonical_model", "body.canonical_model", body={"model": "nyte/auto"})
    add("pass:body.canonical_model:fallback", "body.canonical_model",
        body={"model": GLM, "models": ["trustedrouter/auto-routing"]})
    add("pass:body.custom_model", "body.custom_model", body={"model": "tr-custom-model/e2-missing"})
    add("pass:body.tags", "body.tags", body={"tags": {"équipe": "café"}})
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
    for mode in ("absent", "bad_signature", "unknown_kid", "malformed", "repeated"):
        add(f"pass:boot.signature:{mode}", "boot.signature", boot=mode)
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

    # The parser's edges, each with Python's answer.
    add("pass:body.unparseable:content_type_missing", "body.unparseable", content_type=None)
    add("answer:content_type_json_charset", content_type="application/json; charset=utf-8")
    add("pass:body.unparseable:content_type_text", "body.unparseable", content_type="text/plain")
    # Python's json refuses an integer over 4300 digits and nesting past its
    # recursion limit (FastAPI answers 400 http_error); 5000 levels it takes.
    add("pass:body.unparseable:int_over_4300_digits", "body.unparseable",
        raw_body(lookup, f'"model":"{GLM}"', '"x_big":' + "9" * 4301, PLAIN_TAIL))
    add("answer:deep_nesting_5000", None,
        raw_body(lookup, f'"model":"{GLM}"', '"x_deep":' + "[" * 5000 + "1" + "]" * 5000,
                 '"estimated_input_tokens":1000', PLAIN_TAIL))
    add("pass:body.unparseable:deep_nesting_100000", "body.unparseable",
        raw_body(lookup, f'"model":"{GLM}"', '"x_deep":' + "[" * 100_000 + "1" + "]" * 100_000, PLAIN_TAIL))
    # The schema strips the keys of dict fields too (str_strip_whitespace on
    # dict[str, Any]); keys that collide after the strip keep the last.
    add("answer:dict_keys_stripped", body={"metadata": {" k ": "v"}, "trace": {" id": "t"},
                                           "provider": {" sort": "price"}})
    add("answer:dict_keys_collide", body={"metadata": {"k": "first", " k": "last"}})
    add("answer:tags_keys_stripped", body={"tags": {" env ": "dev"}})
    add("answer:tags_keys_collide", body={"tags": {"env": "a", "env ": "b"}})
    add("answer:extra_null_dropped", None,
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":1000', PLAIN_TAIL, '"x_null":null,"x_kept":"v"'))
    add("answer:app_categories_empty", body={"app_categories": []})
    # Idempotency-Key header edges: non-ASCII bytes (Starlette decodes the
    # header as latin-1) and the header repeated.
    add("pass:header.idempotency_key:non_ascii", "header.idempotency_key", headers=(("idempotency-key", "clé-1"),))
    add("pass:header.idempotency_key:repeated", "header.idempotency_key",
        headers=(("idempotency-key", "first-key"), ("idempotency-key", "second-key")))
    # Internal-only models with an ordinary key, and merged tags.
    add("pass:evaluate.refused:monitor_model", "evaluate.refused", body={"model": "trustedrouter/monitor"})
    add("pass:evaluate.refused:fortanix_test_model", "evaluate.refused", body={"model": "trustedrouter/fortanix-test"})
    add("pass:tags.merged_invalid:over_50", "tags.merged_invalid", body={"tags": {f"r{i}": "v" for i in range(25)}},
        world={"key": {"tags": {f"k{i}": "v" for i in range(30)}}})
    add("pass:tags.merged_invalid:over_4096_bytes", "tags.merged_invalid",
        body={"tags": {f"r{i}": "v" * 250 for i in range(9)}},
        world={"key": {"tags": {f"k{i}": "v" * 250 for i in range(9)}}})
    add("pass:tags.merged_invalid:corrupt_stored", "tags.merged_invalid", stored_key_tags={"bad!": "x"})
    add("pass:workspace.missing", "workspace.missing", workspace_row=False)
    return out


# ---------------------------------------------------------- fingerprints --

FINGERPRINT_KEY = {**DEFAULT_KEY, "hash": "key_e2_fingerprint", "name": "fingerprint", "tags": {"team": "e2"}}
FINGERPRINT_WORLD = _merge(DEFAULT_WORLD, {"workspace": {"id": FINGERPRINT_WS}, "key": FINGERPRINT_KEY, "boot": None})
FINGERPRINT_POLICY = _merge(DEFAULT_POLICY, {"fastpath_pilot_workspaces": [FINGERPRINT_WS]})
# §10.2 asks about 20,000 under 10 MB; at these bodies' size (an enclave
# body with a 64-hex lookup hash, about 280 bytes) the size bounds it first.
FINGERPRINT_COUNT = 14_000
ROUTE_CHECK_EVERY = 40
# Decision 4 and §3's request reasons: Go passes a body carrying any of these.
PASS_REQUIRED = frozenset({
    "float_present", "lax_coercion", "duplicate_key", "strip_ambiguous", "non_ascii_tag",
    "referer_not_strict", "canonical_model_rewrite", "native_batch_key", "partner_key",
    "idempotency_key_too_long",
    # Beyond §10.2's list: a lone surrogate escape (Go's decoder replaces
    # it) and an Idempotency-Key header outside ASCII (Starlette decodes it
    # as latin-1).
    "lone_surrogate", "idempotency_key_non_ascii",
})
STRICT_REFERER = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][\x21-\x7e]*)?$")
_DECLARED_STRINGS = ("model", "models", "region", "service_tier", "route_type", "user", "session_id", "app",
                     "http_referer", "app_categories", "requested_parameters", "input_modalities",
                     "idempotency_key", "invocation_nonce", "video_resolution", "api_key_hash",
                     "api_key_lookup_hash", "request_fingerprint", "cache_affinity_key")


class _Fingerprinted(Exception):  # noqa: N818 - a stop signal, not an error
    pass


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
        value = int(token)
        if abs(value) >= 1 << 63:
            found.add("int_beyond_2_63")
        elif abs(value) >= 1 << 53:
            found.add("int_near_2_63")
        return value

    def parse_constant(token: str) -> float:
        found.add("float_present")  # NaN, Infinity: a float, and no JSON Go parses
        return float(token)

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=parse_float, parse_int=parse_int,
                       parse_constant=parse_constant)
    return value, found


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


def _printable_ascii(text: str) -> bool:
    return all(" " <= ch <= "~" for ch in text)


def body_labels(raw: str, headers: dict[str, str]) -> list[str]:
    """§10.2's labels for one body, and the two PASS_REQUIRED adds."""
    try:
        value, labels = _decoded(raw)
    except (ValueError, RecursionError):
        return []
    if not isinstance(value, dict):
        return sorted(labels)
    fields = GatewayAuthorizeRequest.model_fields
    extras = [k for k in value if k not in fields]
    if any(value[k] is None for k in extras):
        labels.add("extra_null")
    if any(isinstance(value.get(name), dict) and any(k != k.strip() for k in value[name])
           for name in ("provider", "trace", "metadata", "tags")):
        labels.add("dict_key_whitespace")
    if extras:
        labels.add("extras")
        if any(not _printable_ascii(s) for k in extras for s in [k, *_strings(value[k])]):
            labels.add("unicode_extra")
    if any(0xD800 <= ord(ch) <= 0xDFFF for s in _strings(value) for ch in s):
        labels.add("lone_surrogate")
    declared = [s for name in _DECLARED_STRINGS for s in _strings(value.get(name))]
    if any(not _printable_ascii(s) for s in declared):
        labels.add("strip_ambiguous")
    if any(s != s.strip() for s in declared):
        labels.add("whitespace")
    for name, label in (("provider", "nested_provider"), ("trace", "nested_trace"), ("metadata", "nested_metadata")):
        if isinstance(value.get(name), dict) and value[name]:
            labels.add(label)
    if any(value.get(name) is not None for name in ("user", "session_id", "trace", "app", "http_referer", "app_categories")):
        labels.add("attribution")
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
    if "idempotency-key" in headers:
        labels.add("idempotency_header")
    if value.get("idempotency_key") is not None:
        labels.add("idempotency_body")
    presented = [k.strip() for k in (value.get("idempotency_key"), headers.get("idempotency-key")) if isinstance(k, str)]
    if any(k.startswith("tr-native-batch:") for k in presented):
        labels.add("native_batch_key")
    if any(k.startswith("partner:") for k in presented):
        labels.add("partner_key")
    if any(len(k) > 256 for k in presented):
        labels.add("idempotency_key_too_long")
    if not _printable_ascii(headers.get("idempotency-key", "")):
        labels.add("idempotency_key_non_ascii")
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


def fingerprint_expect(raw: str, headers: dict[str, str], key_hash: str) -> dict[str, Any]:
    """Run the authorize route on one body until it computes the fingerprint.

    The body is decoded and validated as the route's parser does (json.loads,
    then the schema; main.py's validation handler answers a refusal 400
    bad_request) and the route runs up to the fingerprint, which stops it
    before any row is written. A refusal on the way is recorded instead.
    """
    refused = {"outcome": "refused", "status": 400, "type": "bad_request"}
    if not raw:
        return refused
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return refused
    except (ValueError, RecursionError):
        # A decode the parser cannot finish (an integer over Python's 4300
        # digits, nesting past the recursion limit): FastAPI's 400 http_error.
        return {"outcome": "refused", "status": 400, "type": "http_error"}
    try:
        # As FastAPI validates a body model: from attributes allowed.
        body = GatewayAuthorizeRequest.model_validate(value, from_attributes=True)
    except ValidationError:
        return refused

    def stop() -> None:
        raise _Fingerprinted

    recorder = _Recorder(then=stop)
    request = Request({"type": "http", "method": "POST", "path": PATH,
                       "headers": [(k.encode(), v.encode()) for k, v in {**headers, **_TOKEN}.items()]})
    try:
        with patched(gateway, "_gateway_authorize_fingerprint", recorder):
            gateway._authorize_gateway_sync(request, body, _FINGERPRINT_SETTINGS, raw.encode())
    except _Fingerprinted:
        presented = gateway._gateway_idempotency_key(request, body)
        return {
            "outcome": "fingerprint", "fingerprint": recorder.seen[-1][0], "idempotency_key": presented,
            "idempotency_scope": scope_of(FINGERPRINT_WS, key_hash, presented) if presented else None,
        }
    except HTTPException as exc:
        error = exc.detail.get("error", {}) if isinstance(exc.detail, dict) else {}
        return {"outcome": "refused", "status": exc.status_code, "type": error.get("type")}
    raise AssertionError(f"the route answered without a fingerprint: {raw}")


_FINGERPRINT_SETTINGS = settings_for(FINGERPRINT_POLICY, FINGERPRINT_KEY)
# Each body reaches the route with the internal token, as through the edge.
_TOKEN = {"x-trustedrouter-internal-token": INTERNAL_TOKEN}

# Building blocks for the bodies. A few fields of a plain enclave body are
# drawn from these per body (a fixed seed); each body is then written one
# of several ways.
_UNICODE = ("café", "日本語", "emoji 😀", "zero\u0000width", "line sep", "tab\tquote\"", "slash/\\back",
            "\u007f del", "ñ", "Ω≈ç√", "﻿bom")
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
                     {"sort": "price", " sort ": "latency"}),
        "allow_fallbacks": (True, False, "false", 0, 1, None),
        "requested_parameters": (["tools"], [], [" tools "]),
        "input_modalities": (["text"], ["text", "image"], ["sound"]),
        "user": ("user-1", "  user-2  ", "ü", "u" * 257),
        "session_id": ("s-1", " s-2 "),
        "trace": ({"trace_id": "t"}, {"n": 1.25}, {"deep": {"a": {"b": [1, "é"]}}}, {"x": 2**63}, {" id ": "t"}),
        "metadata": ({"k": "v"}, {"temperature": 0.7}, {"u": "日本"}, {"big": -(2**63)}, {},
                     {"k": "first", " k": "last"}, {" k ": {" inner ": 1}}),
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


def fingerprint_bodies() -> list[dict[str, Any]]:
    rng = random.Random(20261019)  # noqa: S311 - fixed: the file is a function of this text
    lookup = lookup_hash(FINGERPRINT_KEY["hash"])
    options = _field_options()
    fields = list(options)
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
        header_key = rng.choice((None,) * 14 + ("hdr-key", " hdr-spaced ", "k" * 257, "tr-native-batch:h", "", "\t"))
        out.append({"body": _write(value, rng), "idempotency_key_header": header_key})
    # Bodies no draw spells: non-finite numbers, lone surrogates, a body
    # that is not an object, invalid JSON, nothing.
    for raw in (
        raw_body(lookup, f'"model":"{GLM}"', '"metadata":{"x":NaN}'),
        raw_body(lookup, f'"model":"{GLM}"', '"metadata":{"x":Infinity}'),
        raw_body(lookup, f'"model":"{GLM}"', '"user":"\\ud800"'),
        raw_body(lookup, f'"model":"{GLM}"', '"x_extra":"\\udfff\\ud83d\\ude00"'),
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":-0'),
        raw_body(lookup, f'"model":"{GLM}"', '"estimated_input_tokens":1e400'),
        raw_body(lookup, f'"model":"{GLM}"', '"max_output_tokens":1.0000000000000001'),
        raw_body(lookup, f'"model":"{GLM}"', '"x":12345678901234567890123'),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4300),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "9" * 4301),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 900 + "1" + "]" * 900),
        raw_body(lookup, f'"model":"{GLM}"', '"x":' + "[" * 100_000 + "1" + "]" * 100_000),
        raw_body(lookup, f'"model":"{GLM}"', '"x_null":null,"tags":{" a ":"1","a":"2"}'),
        '["not", "an", "object"]', '"text"', "null", '{"api_key_lookup_hash":', "",
    ):
        out.append({"body": raw, "idempotency_key_header": None})
    # Idempotency-Key bytes outside ASCII: Starlette decodes a header as latin-1.
    for header_key in ("clé", "日本"):
        out.append({"body": raw_body(lookup, f'"model":"{GLM}"'), "idempotency_key_header": header_key})
    return out


def _fingerprint_headers(entry: dict[str, Any]) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if entry["idempotency_key_header"] is not None:
        headers["idempotency-key"] = entry["idempotency_key_header"]
    return headers


def fingerprint_vectors() -> list[dict[str, Any]]:
    key_hash = FINGERPRINT_KEY["hash"]
    entries = []
    with pinned(ROUTING._instant(INSTANTS["peak"]), "fingerprints"):
        build_world(FINGERPRINT_WORLD, FINGERPRINT_POLICY)
        for index, entry in enumerate(fingerprint_bodies()):
            headers = _fingerprint_headers(entry)
            entries.append({
                "name": f"fp-{index:05d}", "labels": body_labels(entry["body"], headers),
                "workspace_id": FINGERPRINT_WS, "key_hash": key_hash,
                "headers": [[k, v] for k, v in headers.items()], "body": entry["body"],
                "expect": fingerprint_expect(entry["body"], headers, key_hash),
            })
    route_check(entries)
    return entries


def route_check(entries: list[dict[str, Any]]) -> None:
    """The full route, over HTTP, agrees on a sample of the bodies: one in
    ROUTE_CHECK_EVERY, the first to carry each label, and the hand-written ones."""
    first: dict[str, int] = {}
    for index, entry in enumerate(entries):
        for label in entry["labels"]:
            first.setdefault(label, index)
    sample = sorted(set(range(0, len(entries), ROUTE_CHECK_EVERY)) | set(first.values())
                    | set(range(FINGERPRINT_COUNT, len(entries))))
    at = ROUTING._instant(INSTANTS["peak"])
    for index in sample:
        entry, expect = entries[index], entries[index]["expect"]
        with pinned(at, f"route-check-{index}"), contextlib.ExitStack() as stack:
            build_world(FINGERPRINT_WORLD, FINGERPRINT_POLICY)
            gateway._BROADCAST_EMPTY_CACHE.clear()
            stack.enter_context(front_door(ADMIT))
            recorder = _Recorder()
            stack.enter_context(patched(gateway, "_gateway_authorize_fingerprint", recorder))
            response = _client(_FINGERPRINT_SETTINGS).post(
                PATH, content=entry["body"].encode(),
                headers=[*((k.encode(), v.encode()) for k, v in [*entry["headers"], *_TOKEN.items()]),
                         (b"accept-encoding", b"identity")])
        if expect["outcome"] == "fingerprint":
            if not recorder.seen or recorder.seen[0][0] != expect["fingerprint"]:
                raise AssertionError(f"{entry['name']}: the route hashed otherwise: {response.text}")
            continue
        said = {"outcome": "refused", "status": response.status_code,
                "type": response.json().get("error", {}).get("type")}
        if recorder.seen or said != expect:
            raise AssertionError(f"{entry['name']}: expected {expect}, the route said {said}")


# ---------------------------------------------------------------- render --

def _lines(header: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """One case a line, as routing_cases_v1.json, so a change shows as the
    cases it changes; UTF-8 text as itself (§10: a body's bytes as a string)."""
    head = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False)[:-1]
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in rows]
    return head + ',\n "cases": [\n  ' + ",\n  ".join(lines) + "\n ]}\n"


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
        "plane": PLANE, "policy": header_policy, "boot_test_key": {"kid": BOOT_KID, "jwk": BOOT_JWK},
        "internal_token": INTERNAL_TOKEN,
        # @timed_gateway_async ends every answer's data, and the 503's
        # top-level data, with timing: its shape is fixed, its values are
        # Python's own measurements (pinned here to 0 ms; spanner_rpcs is the
        # fake's count). Compare it by shape, not value.
        "volatile": ["data.timing"],
    }, rows)


def render_fingerprints() -> str:
    return _lines({"generator": GENERATOR}, fingerprint_vectors())


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
