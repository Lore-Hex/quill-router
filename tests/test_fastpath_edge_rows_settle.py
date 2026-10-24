"""Python settles the rows the Go edge will write (E2 P3,
docs/design/fast-admission-e2-decide.md §5, §8.4, decisions 3 and 11).

For every answered case of fastpath/testdata/authorize_cases_v1.json, two
worlds are built from the case's own stored rows (the key, lookup, workspace,
credit row and boot Go reads) and settings (the case's edge policy):

- ``go``: the case's ``tr_gateway_authorization`` row (payload and typed
  columns) and ``tr_reservation`` row are loaded into the Spanner fake as raw
  rows, exactly the columns §5 says Go writes, every other column NULL, as an
  edge decision would leave them. Python never authorizes.
- ``python``: the case's request is sent through the app, which leases it and
  writes its own rows (the route's leased insert; for ``unknown_landed`` the
  insert's answer is lost and the route reads the row back).

Then each world is driven through Python's real routes over the ASGI app with
the internal token: settle, refund, disposition lookup, the reaper (the
settle-outbox drain route runs it), idempotent replay (the same key and
fingerprint replay; another body under the key is 409) and the drain of a
pending leased intent. The two worlds must agree on everything Python
decides: statuses, response bodies, what the front door is asked, the
charges and states Python writes back to the rows, the outbox, the counters
and the activity records. The rows themselves are held equal too, column by
column, and Python's world against the golden (so the goldens are what this
process writes as well).

A positive control corrupts one field Go writes and shows the comparison
fails on a behaviour, not on the row text.

What this proves, and what it does not: Python handles rows shaped as the
golden says Go writes them (§5) exactly as it handles its own. It does not
prove Go writes them; that is G10's row comparison, which must hold every
column of both rows (all 25 of tr_gateway_authorization, typed columns
included, and all 18 of tr_reservation) to the golden. Behaviour alone
cannot: Python falls back to the payload for a NULL typed column, and some
facts are read by nothing these routes do. A NULL typed invocation_nonce,
idempotency_fingerprint or stage_d_boot_kid, a wrong typed model_id,
provider or estimated_microdollars, or a wrong payload endpoint_id,
requested_model_id, stage_d_reason, stage_d_prompt_tokens or
fastpath_end_of_life survives every scenario here; only row parity (this
file's test_go_rows_are_the_rows_python_writes against the golden, and G10
against Go) catches them.

The clocks are pinned to each case's instant (the route's readings, the
snapshot fixture's catalog and rank clocks, the reaper's ``now``), every
uuid4 is seeded, and the route's timing clock reads a constant.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as real_dt
import functools
import hashlib
import importlib.util
import json
import os
import sys
import types
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from google.api_core.exceptions import DeadlineExceeded

from tests.fakes.spanner import _Row, _SpannerModule, make_fake_store
from tests.test_fastpath_frontdoor import _Door, _transport
from trusted_router import gateway_timing, spend_windows, storage_gcp
from trusted_router.config import Settings
from trusted_router.gateway_boot import GATEWAY_BOOT_KIND, boot_auth_digest
from trusted_router.main import create_app
from trusted_router.receipt_keys import b64url_encode, normalize_receipt_jwk
from trusted_router.routes.internal import gateway
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.services import settle_outbox_drain
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_codec import workspace_key_id
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    KEY_LIMIT_COLUMNS,
    KEY_LIMIT_TABLE,
    key_limit_mirror_rows,
)
from trusted_router.storage_gcp_request_records import AUTHORIZATION_TYPED_COLUMNS
from trusted_router.storage_models import CreditAccount, GatewayBoot

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "authorize_cases_v1.json"
AUTHORIZE = "/internal/gateway/authorize"
FRONT_DOOR = "10.0.0.1:8080"
EPOCH = real_dt.datetime(1970, 1, 1, tzinfo=real_dt.UTC)
TTL = real_dt.timedelta(seconds=7_200)

# §5: the columns Go's two inserts name. Every other column of either table
# is NULL on a row Go writes (a column a Spanner insert does not name).
# `settled` is among them: G9 names settled=false explicitly rather than
# relying on a column default.
GO_AUTHORIZATION_COLUMNS = (
    "authorization_id", "workspace_id", "key_hash", "reservation_id", "model_id", "provider", "usage_type",
    "estimated_microdollars", "settled", "created_at", "terminal_at", "payload",
    "idempotency_fingerprint", "stage_d_boot_kid", "invocation_nonce",
)
AUTHORIZATION_COLUMNS = (
    "authorization_id", "workspace_id", "key_hash", "reservation_id", "model_id", "provider", "usage_type",
    "estimated_microdollars", "settled", "created_at", "terminal_at", "payload", *AUTHORIZATION_TYPED_COLUMNS,
)
GO_RESERVATION_COLUMNS = (
    "reservation_id", "workspace_id", "key_hash", "ws_shard", "credit_shard", "key_shard",
    "credit_reserved_micro", "key_reserved_micro", "hold_usage_type", "authorization_id",
    "idempotency_scope", "idempotency_fingerprint", "expires_at", "created_at",
)
# tr_reservation's other columns and their defaults: settled false, the rest NULL.
RESERVATION_DEFAULTS = {"settled": False, "settled_usage_type": None, "actual_micro": None, "terminal_at": None}
AUTHORIZATION_TIMESTAMPS = ("created_at", "terminal_at", "started_at", "heartbeat_at")
RESERVATION_TIMESTAMPS = ("created_at", "expires_at", "terminal_at")

REFUND_WON = {"Status": "won", "Kind": "refund", "Charge": 0}


# ------------------------------------------------------------------ files --

@functools.cache
def _vectors() -> dict[str, Any]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def _answers() -> list[dict[str, Any]]:
    return [case for case in _vectors()["cases"] if case["expect"]["outcome"] == "answer"]


def _case(name: str) -> dict[str, Any]:
    (case,) = (case for case in _answers() if case["name"] == name)
    return case


@functools.cache
def _routing_cases_vectors() -> Any:
    """P1's catalog fixture (scripts/fastpath/routing_cases_vectors.py
    ``fixture_catalog``), the one the goldens were written over. The script
    scrubs TR_* from the environment when loaded, for its own process; this
    process's environment is put back as it was."""
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "p3_routing_cases_vectors", ROOT / "scripts" / "fastpath" / "routing_cases_vectors.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return module


def _from_us(us: int | None) -> real_dt.datetime | None:
    return None if us is None else EPOCH + real_dt.timedelta(microseconds=us)


def _to_us(value: Any) -> int | None:
    if value is None:
        return None
    when = value if isinstance(value, real_dt.datetime) else real_dt.datetime.fromisoformat(
        str(value).replace("Z", "+00:00"))
    delta = when - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _row_in_us(row: dict[str, Any], timestamps: tuple[str, ...]) -> dict[str, Any]:
    return {f"{name}_us" if name in timestamps else name: _to_us(value) if name in timestamps else value
            for name, value in row.items()}


# ---------------------------------------------------------------- pinning --

@contextlib.contextmanager
def _patched(target: Any, name: str, value: Any) -> Iterator[None]:
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


class _InstantMeta(type):
    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, real_dt.datetime)


def _clock_module(at: real_dt.datetime) -> types.ModuleType:
    """A datetime module whose ``datetime.now`` answers ``at``."""

    class _Pinned(real_dt.datetime, metaclass=_InstantMeta):
        @classmethod
        def now(cls, tz: real_dt.tzinfo | None = None) -> real_dt.datetime:  # type: ignore[override]
            return at if tz is None else at.astimezone(tz)

    module = types.ModuleType("datetime")
    module.__dict__.update(real_dt.__dict__)
    module.datetime = _Pinned  # type: ignore[attr-defined]
    return module


def _seeded_uuid(seed: str) -> uuid.UUID:
    return uuid.UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4)


@contextlib.contextmanager
def _pinned(at: real_dt.datetime, seed: str) -> Iterator[None]:
    """The route's clock readings (its module's ``dt`` and every function-local
    ``import datetime``) answer ``at``, the spend-window, catalog and
    rank clocks too, and uuid4 answers a sequence derived from ``seed`` (as
    scripts/fastpath/authorize_vectors.py ``pinned``)."""
    clock = _clock_module(at)
    counter = iter(range(1 << 30))
    saved = sys.modules["datetime"], gateway.dt, uuid.uuid4
    sys.modules["datetime"], gateway.dt = clock, clock
    uuid.uuid4 = lambda: _seeded_uuid(f"{seed}#{next(counter)}")
    try:
        with _patched(spend_windows, "dt", clock), _routing_cases_vectors().fixture_catalog(at):
            yield
    finally:
        sys.modules["datetime"], gateway.dt, uuid.uuid4 = saved


@contextlib.contextmanager
def _given_ids(ids: dict[str, str]) -> Iterator[None]:
    """Python's authorize mints exactly the case's ids (as the generator's
    ``given_ids``): the authorization id, the reservation id and the key."""
    minted = uuid.UUID(ids["minted_idempotency_key"])
    reservation = uuid.UUID(ids["reservation_id"])
    with _patched(gateway, "_new_gateway_authorization_id", lambda: ids["authorization_id"]), \
            _patched(gateway, "uuid", types.SimpleNamespace(uuid4=lambda: minted)), \
            _patched(storage_gcp, "uuid", types.SimpleNamespace(uuid4=lambda: reservation)):
        yield


@contextlib.contextmanager
def _leased_insert(insert: str) -> Iterator[None]:
    """The leased insert's commit as the case had it: applied, or applied
    with its answer lost to a deadline (Python reads the row back)."""
    if insert == "inserted":
        yield
        return
    assert insert == "unknown_landed", insert
    real = storage_gcp.run_in_transaction_with_retry

    def lost(database: Any, txn: Any, **kwargs: Any) -> Any:
        if kwargs.get("transaction_tag") != "tr_authorize_leased":
            return real(database, txn, **kwargs)
        real(database, txn, **kwargs)
        raise DeadlineExceeded("the commit's answer was lost")

    with _patched(storage_gcp, "run_in_transaction_with_retry", lost):
        yield


@pytest.fixture(autouse=True)
def _timing_reads_a_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)


# ------------------------------------------------------------------ world --

def _settings(case: dict[str, Any]) -> Settings:
    """The case's edge policy (§4 body, patched) as Settings, as the
    generator's ``settings_for`` builds them; an answered case always has
    records_durable and no monitor key."""
    header = _vectors()
    policy = {**header["policy"], **case["policy_patch"]}
    assert policy["records_durable"] and policy["monitor_lookup_hash"] is None, case["name"]
    stage_d = policy["stage_d"]
    return Settings(
        environment="test",
        service_surface="internal",
        internal_gateway_token=header["internal_token"],
        regions=",".join(policy["configured_regions"]),
        primary_region=policy["primary_region"],
        multi_region_enabled=policy["multi_region_enabled"],
        auto_model_order="",
        trust_gcp_release_url="",
        trust_gcp_release_fallback_urls="",
        rate_limit_ip_per_window=1_000_000_000,
        rate_limit_internal_per_window=1_000_000_000,
        fastpath_pilot_enabled=policy["fastpath_pilot_enabled"],
        fastpath_pilot_workspaces=",".join(policy["fastpath_pilot_workspaces"]),
        fastpath_pilot_regions=",".join(policy["fastpath_pilot_regions"]),
        settle_outbox_enabled=True,
        # Production renders it true (scripts/deploy/rollout.sh,
        # TR_REAP_SNAPSHOT_BOOKING_ENABLED): the reaper as it runs there.
        reap_snapshot_booking_enabled=True,
        stage_d_eligibility_enabled=stage_d["eligibility_enabled"],
        stage_d_heartbeat_enabled=stage_d["heartbeat_enabled"],
        stage_d_require_heartbeat_declaration=stage_d["require_heartbeat_declaration"],
        stage_d_pilot_workspace_ids=",".join(stage_d["pilot_workspaces"]),
        spend_lease_accepted_gcp_image_digests=",".join(policy["accepted_image_digests"]),
        gateway_authorize_max_in_flight_per_key=policy["gateway_authorize_max_in_flight_per_key"],
        async_settle_shadow_policy=policy["async_settle_shadow_policy"],
        async_settle_shadow_workspaces=",".join(policy["async_settle_shadow_workspaces"]),
    )


_CLIENTS: dict[str, TestClient] = {}


def _client(settings: Settings) -> TestClient:
    """One app per distinct settings; no lifespan, so no background loop
    touches the store."""
    cache_key = settings.model_dump_json()
    if cache_key not in _CLIENTS:
        app = create_app(settings, configure_store_arg=False, init_observability=False)
        _CLIENTS[cache_key] = TestClient(app, raise_server_exceptions=False)
    return _CLIENTS[cache_key]


class _Answers(dict):
    """The front door's answers. A won settle answers the charge it was sent,
    so the records carry the charge Python priced (the owner books what it is
    told), and the two worlds' charges are compared, not a constant."""

    def __init__(self, door: _Door, answers: dict[str, Any]) -> None:
        super().__init__(answers)
        self.door = door

    def __getitem__(self, path: str) -> Any:
        answer = dict(super().__getitem__(path))
        if path == "/v1/settle" and answer.get("Status") == "won":
            answer["Charge"] = self.door.seen[-1][1]["Charge"]
        return answer


@dataclasses.dataclass
class World:
    case: dict[str, Any]
    store: Any
    database: Any
    door: _Door
    client: TestClient
    at: real_dt.datetime

    @property
    def authorization_id(self) -> str:
        return self.case["ids"]["authorization_id"]

    @property
    def reservation_id(self) -> str:
        return self.case["ids"]["reservation_id"]

    def answer(self, path: str, answer: dict[str, Any]) -> None:
        self.door.answers = _Answers(self.door, {**dict(self.door.answers), path: answer})


def _seed(case: dict[str, Any]) -> tuple[Any, Any]:
    """The world as Go reads it, from the case's own rows: the lookup, key
    and workspace entity bodies, the credit rows, the boot. Answered cases
    have no BYOK or broadcast ids."""
    header = _vectors()
    store, database = make_fake_store(
        request_record_write_mode="typed",
        operational_analytics_outbox_enabled=True,
        generation_records_enabled=True,
        analytics_outbox_enabled=True,
    )
    facts = case["facts"]
    workspace_id, key = facts["workspace"]["workspace_id"], facts["key"]
    assert case["byok_entity_ids"] == [] and case["broadcast_pointer_ids"] == [], case["name"]
    for kind, entity_id, body in (
        ("api_key_lookup", key["lookup_hash"], case["key"]["lookup_body"]),
        ("api_key", key["hash"], case["key"]["key_body"]),
        ("workspace", workspace_id, case["workspace"]["body"]),
    ):
        assert body is not None, (case["name"], kind)
        database.rows[(kind, entity_id)] = _Row(body=body, version=1)
    # The rest of what creating the key wrote (storage_gcp_keys.create): its
    # typed limit mirror, which settle books the key's usage on, and the
    # workspace's index entry.
    stored_key = store.api_keys.get_by_hash(key["hash"])
    with database.batch() as batch:
        batch.insert_or_update(table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS,
                               values=key_limit_mirror_rows(stored_key.hash, stored_key, _SpannerModule.COMMIT_TIMESTAMP))
    store._write_entity("api_key_by_workspace", workspace_key_id(workspace_id, stored_key.hash),
                        {"key_id": stored_key.hash})
    store._write_entity("credit", workspace_id, CreditAccount(workspace_id=workspace_id))
    for row in case["workspace"]["credit_rows"]:
        assert not row["trust_latched"], case["name"]
        database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace_id, row["shard"])] = {
            "workspace_id": workspace_id, "shard": row["shard"], "total_credits": 50_000_000,
            "total_usage": 0, "reserved": 0, "in_debt": row["in_debt"], "trust_tier": row["trust_tier"],
            "trust_latched_at": None, "billing_pause_causes": list(row["billing_pause_causes"]),
            "source_updated_at": None, "updated_at": None,
        }
    boot = case["boot"]
    store._write_entity(GATEWAY_BOOT_KIND, boot["kid"], GatewayBoot(
        kid=boot["kid"], jwk=normalize_receipt_jwk(header["boot_test_key"]["jwk"]), approved=True,
        verified=boot["verified"], image_digest=boot["image_digest"], attestation_kind="gcp",
        registered_at="2026-10-18T23:00:00Z", declares_usage_heartbeat=boot["declares_usage_heartbeat"],
    ))
    configure_store(store)
    gateway._BROADCAST_EMPTY_CACHE.clear()
    return store, database


def go_rows(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two rows exactly as §5 has Go write them, from the golden: Go's
    columns, every other column NULL (the reservation's settled false)."""
    def stored(golden: dict[str, Any]) -> dict[str, Any]:
        # The golden's instants are Unix microseconds in fields ending _us.
        return {name.removesuffix("_us"): _from_us(value) if name.endswith("_us") else value
                for name, value in golden.items()}

    golden_authorization = stored(case["expect"]["authorization_row"])
    golden_reservation = stored(case["expect"]["reservation_row"])
    assert set(golden_authorization) == set(AUTHORIZATION_COLUMNS), case["name"]
    assert set(golden_reservation) == set(GO_RESERVATION_COLUMNS), case["name"]
    authorization = {column: None for column in AUTHORIZATION_COLUMNS}
    authorization.update({column: golden_authorization[column] for column in GO_AUTHORIZATION_COLUMNS})
    reservation = {column: golden_reservation[column] for column in GO_RESERVATION_COLUMNS}
    reservation.update(RESERVATION_DEFAULTS)
    return authorization, reservation


def _load_go_rows(database: Any, authorization: dict[str, Any], reservation: dict[str, Any]) -> None:
    """Commit the two rows as one transaction would (the fake's own
    bookkeeping for two inserts: versions, the scope's unique index)."""
    database._global_version += 1
    version = database._global_version
    rid, aid = reservation["reservation_id"], authorization["authorization_id"]
    assert rid not in database.reservations and aid not in database.gateway_authorizations
    assert reservation["idempotency_scope"] not in database.reservation_idemp
    database.reservations[rid] = dict(reservation)
    database.reservation_versions[rid] = version
    database.reservation_auth_versions[aid] = version
    database.reservation_idemp[reservation["idempotency_scope"]] = rid
    database.gateway_authorizations[aid] = dict(authorization)
    database.gateway_authorization_versions[aid] = version


Corrupt = Callable[[dict[str, Any], dict[str, Any]], None]


def build_world(case: dict[str, Any], rows: str, *, corrupt: Corrupt | None = None) -> World:
    """A world whose two rows Go wrote (``go``) or Python's authorize wrote
    (``python``). Built under the caller's ``_pinned`` clock."""
    at = _from_us(case["at_us"])
    assert at is not None
    store, database = _seed(case)
    database.now = at
    door = _Door()
    world = World(case=case, store=store, database=database, door=door, client=_client(_settings(case)), at=at)
    admitted = {"Status": "admitted", "Envelope": case["admitted"]["envelope"],
                "EndOfLife": case["admitted"]["end_of_life"]}
    door.answers = _Answers(door, {"/v1/authorize": admitted, "/v1/settle": {"Status": "won", "Kind": "settle"},
                                   "/v1/refund": dict(REFUND_WON)})
    if rows == "go":
        authorization, reservation = go_rows(case)
        if corrupt is not None:
            corrupt(authorization, reservation)
        _load_go_rows(database, authorization, reservation)
        return world
    assert rows == "python" and corrupt is None
    with _given_ids(case["ids"]), _leased_insert(case["insert"]), _front_door(door):
        response = _send(world, case["request"]["headers"], case["request"]["body"])
    # Python answered the case exactly (the golden's bytes; timing reads a
    # constant clock, as the generator's did) and wrote its two rows.
    assert response.status_code == 200, (case["name"], response.text)
    assert response.content.decode() == case["expect"]["response"], case["name"]
    door.seen.clear()  # the admit is not part of what follows: Go's edge asked its own owner
    return world


@contextlib.contextmanager
def _front_door(door: _Door) -> Iterator[None]:
    client = fd.FrontDoorClient(lambda _budget: (FRONT_DOOR,), timeout_seconds=1.5, clock=lambda: 0.0,
                                transport=_transport({FRONT_DOOR: door}))
    with _patched(gateway, "_fastpath_client", client):
        yield


# --------------------------------------------------------------- requests --

def _sign(method: str, path: str, body: bytes) -> str:
    seed = bytes.fromhex(_vectors()["boot_test_key"]["seed_hex"])
    return b64url_encode(Ed25519PrivateKey.from_private_bytes(seed).sign(boot_auth_digest(method, path, body)))


def _token() -> tuple[str, str]:
    return "x-trustedrouter-internal-token", _vectors()["internal_token"]


def _send(world: World, headers: list[list[str]], body: str) -> Any:
    return world.client.post(AUTHORIZE, content=body.encode(), headers=[(name, value) for name, value in headers])


def _authorize(world: World, *, variant: bool) -> Any:
    """The case's authorize again, under the idempotency key its rows carry:
    the same bytes when a key was presented, else the minted key in the
    header (the fingerprint does not read the key). ``variant`` sends another
    body under that key: one extra field, which the fingerprint keeps."""
    case = world.case
    request = case["request"]
    payload = json.loads(case["expect"]["authorization_row"]["payload"])
    body_text = request["body"]
    body = json.loads(body_text)
    headers = [list(pair) for pair in request["headers"] if pair[0] != "x-tr-boot-auth"]
    presented = (body.get("idempotency_key") or "").strip() or next(
        (value.strip() for name, value in headers if name == "idempotency-key" and value.strip()), "")
    if not presented:
        # Starlette reads the first header: a blank one goes.
        headers = [pair for pair in headers if pair[0] != "idempotency-key"]
        headers.append(["idempotency-key", payload["idempotency_key"]])
    if variant:
        body_text = json.dumps({**body, "p3_variant_field": 1}, separators=(",", ":"), ensure_ascii=False)
    kid = case["boot"]["kid"]
    headers.append(["x-tr-boot-auth", f"kid={kid},sig={_sign('POST', AUTHORIZE, body_text.encode())}"])
    return _send(world, headers, body_text)


def _settle_body(world: World, *, success: bool) -> dict[str, Any]:
    data = json.loads(world.case["expect"]["response"])["data"]
    return {
        "authorization_id": world.authorization_id,
        "actual_input_tokens": 900,
        "actual_output_tokens": 300,
        "request_id": f"req-p3-{world.authorization_id}",
        "finish_reason": "stop" if success else "error",
        "status": "success" if success else "error",
        "streamed": False,
        "elapsed_seconds": 1.0,
        "selected_model": data["model"],
        "selected_endpoint": data["endpoint_id"],
        **({} if success else {"error_status": 502, "error_type": "provider_error"}),
    }


def _settle(world: World) -> Any:
    return world.client.post("/internal/gateway/settle", json=_settle_body(world, success=True), headers=[_token()])


def _refund(world: World) -> Any:
    return world.client.post("/internal/gateway/refund", json=_settle_body(world, success=False), headers=[_token()])


def _disposition(world: World) -> Any:
    path = f"/internal/gateway/authorizations/{world.authorization_id}/disposition"
    auth = f"kid={world.case['boot']['kid']},sig={_sign('GET', path, b'')}"
    return world.client.get(path, headers=[_token(), ("x-tr-boot-auth", auth)])


def _drain(world: World, *, at: real_dt.datetime) -> Any:
    """The settle-outbox drain route, which runs the reaper, its ``now`` at ``at``."""
    with _patched(settle_outbox_drain, "dt", _clock_module(at)):
        return world.client.post("/internal/gateway/settle-outbox/drain?limit=10", headers=[_token()])


def _make_intents_due(world: World) -> None:
    for (authorization_id, _kind), row in world.database.settle_outbox.items():
        if authorization_id == world.authorization_id and row["status"] == "pending":
            row.update(next_attempt_at="2020-01-01T00:00:00Z", lease_owner=None, leased_until=None)


# ------------------------------------------------------------ observation --

# The settle outbox's instants (created, updated, leased, next attempt,
# terminal, and the auto-refill twins) and a terminal's terminal_at are read
# from the process's wall clock (a module-level datetime, not the route's
# pinned reading), so they are never equal across two runs: compared as set
# or unset.
def _outbox_instant(name: str) -> bool:
    return name.endswith(("_at", "_until"))


def _terminal_at(name: str) -> bool:
    return name == "terminal_at"


def _wall(row: dict[str, Any], wall_clock: Callable[[str], bool]) -> dict[str, Any]:
    return {name: ("<wall clock>" if value is not None else None) if wall_clock(name) else value
            for name, value in row.items()}


def _created_at(name: str) -> bool:
    return name == "created_at"


def _analytics_event(row: dict[str, Any]) -> str:
    """A provider-benchmark event in full, its payload decoded; the event's
    created_at is the wall clock's."""
    payload = row["payload"]
    decoded = _wall(json.loads(payload), _created_at) if isinstance(payload, str) else payload
    return json.dumps({**row, "payload": decoded}, sort_keys=True, default=str)


def _response(response: Any) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "body": body}


def _payload_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "set": {name: value for name, value in after.items() if before.get(name, object()) != value},
        "dropped": sorted(set(before) - set(after)),
    }


class Observer:
    """What Python decided in one world, step by step: each request's status
    and body, what the front door was asked, and every state Python writes
    once the two rows are in place. The rows as loaded are not observed
    here (test_go_rows_are_the_rows_python_writes holds them equal): only
    what Python changes in them."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.steps: list[tuple[str, dict[str, Any]]] = []
        self._doors = 0
        authorization = world.database.gateway_authorizations[world.authorization_id]
        self._initial_authorization = dict(authorization)
        self._initial_payload = json.loads(authorization["payload"])
        self._initial_reservation = dict(world.database.reservations[world.reservation_id])

    def step(self, name: str, response: Any) -> None:
        world, database = self.world, self.world.database
        doors = world.door.seen[self._doors:]
        self._doors = len(world.door.seen)
        authorization = database.gateway_authorizations.get(world.authorization_id)
        reservation = database.reservations.get(world.reservation_id)
        workspace_id, key_hash = world.case["facts"]["key"]["workspace_id"], world.case["facts"]["key"]["hash"]
        self.steps.append((name, {
            **_response(response),
            "door": json.loads(json.dumps(doors)),
            "authorization": None if authorization is None else {
                "changed": _wall({column: value for column, value in authorization.items()
                                  if column != "payload" and value != self._initial_authorization.get(column)},
                                 _terminal_at),
                "payload": _payload_change(self._initial_payload, json.loads(authorization["payload"])),
            },
            "reservation": None if reservation is None else _wall(
                {column: value for column, value in reservation.items()
                 if value != self._initial_reservation.get(column)}, _terminal_at),
            "settle_outbox": sorted(
                json.dumps(_wall(row, _outbox_instant), sort_keys=True, default=str)
                for (aid, _kind), row in database.settle_outbox.items() if aid == world.authorization_id),
            "credit": sorted(json.dumps(row, sort_keys=True, default=str)
                             for (ws, _shard), row in database.typed.get(CREDIT_BALANCE_TABLE, {}).items()
                             if ws == workspace_id),
            "key_limit": sorted(json.dumps(row, sort_keys=True, default=str)
                                for (kh, _shard), row in database.typed.get(KEY_LIMIT_TABLE, {}).items()
                                if kh == key_hash),
            # The workspace's activity (the drain's own job heartbeat is not
            # the request's).
            "activity": [row["payload"] for row in database.operational_analytics_outbox
                         if json.loads(row["payload"]).get("workspace_id") == workspace_id],
            # Every analytics event and generation record in full (a
            # generation record's terminal_at is the wall clock's).
            "analytics": [_analytics_event(row) for row in database.analytics_outbox],
            "generations": [json.dumps(_wall(row, _terminal_at), sort_keys=True, default=str)
                            for _id, row in sorted(database.generation_records.items())],
            "authorizations": sorted(database.gateway_authorizations),
            "reservations": sorted(database.reservations),
        }))


Step = tuple[str, Callable[[World], Any]]


def _answering(path: str, answer: dict[str, Any], then: Callable[[World], Any]) -> Callable[[World], Any]:
    def act(world: World) -> Any:
        world.answer(path, answer)
        return then(world)
    return act


def _due(then: Callable[[World], Any]) -> Callable[[World], Any]:
    def act(world: World) -> Any:
        _make_intents_due(world)
        return then(world)
    return act


def _drain_at(offset_seconds: int) -> Callable[[World], Any]:
    return lambda world: _drain(world, at=world.at + TTL + real_dt.timedelta(seconds=offset_seconds))


# Each scenario's steps, run the same in both worlds.
SCENARIOS: dict[str, tuple[Step, ...]] = {
    "settle": (
        ("disposition_open", _disposition),
        ("replay_same_key_and_body", lambda world: _authorize(world, variant=False)),
        ("replay_other_body", lambda world: _authorize(world, variant=True)),
        ("settle", _settle),
        ("disposition_settled", _disposition),
        ("settle_again", _settle),
        ("replay_after_settle", lambda world: _authorize(world, variant=False)),
    ),
    "refund": (
        ("refund", _refund),
        ("disposition_refunded", _disposition),
        ("replay_other_body_after_refund", lambda world: _authorize(world, variant=True)),
    ),
    "reap": (
        ("drain_before_expiry", _drain_at(-1)),
        ("drain_after_expiry", _drain_at(1)),
        ("disposition_reaped", _disposition),
        ("settle_after_reap", _settle),
        ("replay_after_reap", lambda world: _authorize(world, variant=False)),
    ),
    "drain": (
        ("settle_undecided", _answering("/v1/settle", {"Status": "recorded"}, _settle)),
        ("disposition_pending", _disposition),
        # Past the row's expiry, still undecided: the pending intent keeps
        # the reaper off it.
        ("drain_still_undecided", _due(_drain_at(1))),
        ("drain_decided", _answering("/v1/settle", {"Status": "won", "Kind": "settle"}, _due(_drain_at(1)))),
        ("disposition_drained", _disposition),
    ),
}


def run_scenario(case: dict[str, Any], scenario: str, rows: str, *,
                 corrupt: Corrupt | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Drive one world through one scenario; everything observed."""
    at = _from_us(case["at_us"])
    assert at is not None
    with _pinned(at, f"{case['name']}#{scenario}"):
        world = build_world(case, rows, corrupt=corrupt)
        # uuid4 restarts here, so both worlds draw the same sequence.
        with _pinned(at, f"{case['name']}#{scenario}#after"), _front_door(world.door):
            observer = Observer(world)
            for name, act in SCENARIOS[scenario]:
                observer.step(name, act(world))
    return observer.steps


def compare(case: dict[str, Any], scenario: str, *, corrupt: Corrupt | None = None,
            ignore: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """Python decides the same for the rows Go wrote as for its own; the
    observations, by step. ``ignore`` (positive controls only) leaves fields
    out, to show a later one catches a difference by itself."""
    python = run_scenario(case, scenario, "python")
    go = run_scenario(case, scenario, "go", corrupt=corrupt)
    for (name, go_step), (_name, python_step) in zip(go, python, strict=True):
        for field in python_step:
            if field not in ignore and go_step[field] != python_step[field]:
                raise AssertionError(
                    f"{case['name']} {scenario}: Go-shaped rows diverge at {name}.{field}:\n"
                    f"  go:     {go_step[field]!r}\n  python: {python_step[field]!r}"
                )
    return dict(python)


@pytest.mark.parametrize("boundary", ["2026-10-24", "2026-10-26", "2026-11-01"])
def test_key_windows_ignore_wall_clock_rollover(boundary: str) -> None:
    case = _case("answer:estimate:google/gemini-3.1-pro-preview:0+1")
    midnight = real_dt.datetime.fromisoformat(boundary).replace(tzinfo=real_dt.UTC)
    before, after = midnight - real_dt.timedelta(seconds=1), midnight + real_dt.timedelta(seconds=1)
    with _patched(spend_windows, "dt", _clock_module(before)):
        python = run_scenario(case, "settle", "python")
        assert spend_windows.utcnow() == before
    with _patched(spend_windows, "dt", _clock_module(after)):
        go = run_scenario(case, "settle", "go")
        assert spend_windows.utcnow() == after
    assert go == python
    at = _from_us(case["at_us"])
    assert at is not None
    (row,) = dict(go)["settle"]["key_limit"]
    floors = spend_windows.window_floors(at)
    for column, window in (("day_start", "daily"), ("week_start", "weekly"), ("month_start", "monthly")):
        assert json.loads(row)[column] == str(floors[window])


# What each scenario must do (checked once the two worlds agree, so of both):
# a scenario that did nothing would agree trivially.

def _data(observed: dict[str, Any]) -> dict[str, Any]:
    body = observed["body"]
    assert observed["status"] == 200 and isinstance(body, dict) and "data" in body, observed
    return body["data"]


def _replayed(world_case: dict[str, Any], observed: dict[str, Any]) -> None:
    data = _data(observed)
    assert data["idempotent_replay"] is True, observed
    assert data["authorization_id"] == world_case["ids"]["authorization_id"], observed
    assert data["credit_reservation_id"] == world_case["ids"]["reservation_id"], observed
    # The retry's own admit is given back.
    assert [path for path, _sent in observed["door"]] == ["/v1/authorize", "/v1/refund"], observed


def _mismatch(observed: dict[str, Any]) -> None:
    assert observed["status"] == 409, observed
    assert observed["body"]["error"]["type"] == "conflict", observed
    assert [path for path, _sent in observed["door"]] == ["/v1/authorize", "/v1/refund"], observed


def _terminal(case: dict[str, Any], observed: dict[str, Any], *, path: str, outcome: str) -> None:
    ((asked, sent),) = observed["door"]
    assert asked == path and sent["Envelope"] == case["admitted"]["envelope"], observed
    changed = observed["authorization"]["changed"]
    assert changed["settled"] is True and changed["finalization_outcome"] == outcome, observed
    if outcome == "settled":
        assert changed["finalized_cost_microdollars"] == sent["Charge"] > 0, observed
    assert observed["reservation"]["settled"] is True, observed


def _disposed(observed: dict[str, Any], disposition: str) -> None:
    assert _data(observed)["disposition"] == disposition, observed


def _expect_settle(case: dict[str, Any], steps: dict[str, dict[str, Any]]) -> None:
    _disposed(steps["disposition_open"], "intent_durable")
    _replayed(case, steps["replay_same_key_and_body"])
    _mismatch(steps["replay_other_body"])
    assert _data(steps["settle"])["settled"] is True
    _terminal(case, steps["settle"], path="/v1/settle", outcome="settled")
    _disposed(steps["disposition_settled"], "already_finalized")
    assert _data(steps["settle_again"])["settled"] is True and steps["settle_again"]["door"] == []
    _replayed(case, steps["replay_after_settle"])


def _expect_refund(case: dict[str, Any], steps: dict[str, dict[str, Any]]) -> None:
    _data(steps["refund"])
    _terminal(case, steps["refund"], path="/v1/refund", outcome="refunded")
    _disposed(steps["disposition_refunded"], "already_finalized")
    _mismatch(steps["replay_other_body_after_refund"])


def _expect_reap(case: dict[str, Any], steps: dict[str, dict[str, Any]]) -> None:
    before, after = steps["drain_before_expiry"], steps["drain_after_expiry"]
    assert before["status"] == 200 and before["body"]["reaped"] == 0, before
    assert before["authorization"]["changed"] == {} and before["reservation"] == {}, before
    assert after["status"] == 200 and after["body"]["reaped"] == 1 and after["door"] == [], after
    assert after["authorization"]["changed"]["finalization_outcome"] == "refunded", after
    assert after["reservation"]["settled"] is True, after
    _disposed(steps["disposition_reaped"], "already_finalized")
    assert _data(steps["settle_after_reap"])["already_settled"] is True
    assert steps["settle_after_reap"]["door"] == []
    _replayed(case, steps["replay_after_reap"])


def _expect_drain(case: dict[str, Any], steps: dict[str, dict[str, Any]]) -> None:
    undecided = steps["settle_undecided"]
    assert _data(undecided)["disposition"] == "intent_durable", undecided
    assert any('"status": "pending"' in row for row in undecided["settle_outbox"]), undecided
    _disposed(steps["disposition_pending"], "intent_durable")
    still = steps["drain_still_undecided"]
    assert still["status"] == 200 and still["body"]["reaped"] == 0, still
    assert "settled" not in still["authorization"]["changed"], still
    decided = steps["drain_decided"]
    assert decided["status"] == 200, decided
    _terminal(case, decided, path="/v1/settle", outcome="settled")
    assert decided["settle_outbox"] and all('"status": "done"' in row for row in decided["settle_outbox"]), decided
    _disposed(steps["disposition_drained"], "already_finalized")


EXPECT: dict[str, Callable[[dict[str, Any], dict[str, dict[str, Any]]], None]] = {
    "settle": _expect_settle, "refund": _expect_refund, "reap": _expect_reap, "drain": _expect_drain,
}


# ------------------------------------------------------------------ tests --

def _ids(case: dict[str, Any]) -> str:
    return case["name"].removeprefix("answer:")


@pytest.mark.parametrize("case", _answers(), ids=_ids)
def test_go_rows_are_the_rows_python_writes(case: dict[str, Any]) -> None:
    """§5: the rows Go writes are, column for column, the rows Python's
    leased path writes in this process (and the golden's), and Python reads
    the same authorization from either."""
    at = _from_us(case["at_us"])
    assert at is not None
    authorization, reservation = go_rows(case)
    # Go's columns are the golden's non-NULL ones: nothing Go leaves NULL
    # holds a value in Python's row.
    golden = case["expect"]["authorization_row"]
    assert {name.removesuffix("_us") for name, value in golden.items() if value is not None} <= set(
        GO_AUTHORIZATION_COLUMNS), case["name"]
    with _pinned(at, f"{case['name']}#rows"):
        world = build_world(case, "python")
        python_authorization = dict(world.database.gateway_authorizations[world.authorization_id])
        python_reservation = dict(world.database.reservations[world.reservation_id])
        python_view = dataclasses.asdict(world.store.get_gateway_authorization(world.authorization_id))
        go_world = build_world(case, "go")
        go_view = dataclasses.asdict(go_world.store.get_gateway_authorization(go_world.authorization_id))
    assert _row_in_us(python_authorization, AUTHORIZATION_TIMESTAMPS) == golden, case["name"]
    written = {name: value for name, value in python_reservation.items() if name not in RESERVATION_DEFAULTS}
    assert _row_in_us(written, RESERVATION_TIMESTAMPS) == case["expect"]["reservation_row"], case["name"]
    assert python_authorization == authorization, case["name"]
    assert python_reservation == reservation, case["name"]
    assert go_view == python_view, case["name"]
    assert python_view["fastpath_envelope"] == case["admitted"]["envelope"], case["name"]


@pytest.mark.parametrize("scenario", tuple(SCENARIOS))
@pytest.mark.parametrize("case", _answers(), ids=_ids)
def test_python_settles_go_rows_as_its_own(case: dict[str, Any], scenario: str) -> None:
    EXPECT[scenario](case, compare(case, scenario))


def test_the_cases_cover_what_the_edge_writes() -> None:
    """Every Stage D reason an answer carries, several candidates, each tag
    source, an owner end of life with an offset, and the read-back."""
    labels = {label for case in _answers() for label in case["labels"]}
    for label in ("stage_d.not_streaming", "stage_d.stage_d_disabled", "stage_d.workspace_not_pilot",
                  "stage_d.heartbeats_disabled", "stage_d.heartbeat_undeclared", "multi_candidate",
                  "tags.key_only", "tags.request_only", "tags.merge_overwrite", "end_of_life.offset",
                  "insert.unknown_landed", "read_back.sorted_tags", "idempotency.minted",
                  "idempotency.presented_body", "idempotency.presented_header", "invocation_nonce"):
        assert label in labels, label
    assert len(_answers()) > 150


# ------------------------------------------------------- positive control --

CONTROL_CASE = "answer:tags:overlap"


def _drop_credit_reservation_id(authorization: dict[str, Any], _reservation: dict[str, Any]) -> None:
    payload = json.loads(authorization["payload"])
    del payload["credit_reservation_id"]
    authorization["payload"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _other_fingerprint(_authorization: dict[str, Any], reservation: dict[str, Any]) -> None:
    reservation["idempotency_fingerprint"] = hashlib.sha256(b"not the request's material").hexdigest()


def _payload_with(**changes: Any) -> Corrupt:
    def corrupt(authorization: dict[str, Any], _reservation: dict[str, Any]) -> None:
        payload = json.loads(authorization["payload"])
        payload.update(changes)
        authorization["payload"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return corrupt


@pytest.mark.parametrize(
    ("corrupt", "scenario", "ignore", "diverges_at"),
    [
        (_drop_credit_reservation_id, "settle", (), "replay_same_key_and_body.body"),
        (_drop_credit_reservation_id, "refund", (), "refund."),
        (_drop_credit_reservation_id, "reap", (), "drain_after_expiry."),
        (_other_fingerprint, "settle", (), "replay_same_key_and_body.status"),
        # Payload facts Python copies into the records: the activity row,
        # and on its own the generation record compared in full.
        (_payload_with(tags={"team": "other"}), "drain", (), "drain_decided.activity"),
        (_payload_with(tags={"team": "other"}), "drain", ("activity",), "drain_decided.generations"),
        (_payload_with(app_id="app_e2_other"), "drain", (), "drain_decided.activity"),
        (_payload_with(app_id="app_e2_other"), "drain", ("activity",), "drain_decided.generations"),
        (_payload_with(receipt_fee_basis_points=25), "drain", (), "settle_undecided.status"),
        (_payload_with(receipt_fee_basis_points=25), "settle", (), "replay_same_key_and_body.body"),
    ],
    ids=["drop_credit_reservation_id:replay", "drop_credit_reservation_id:refund",
         "drop_credit_reservation_id:reap", "change_fingerprint:replay",
         "tags:activity", "tags:generation", "app_id:activity", "app_id:generation",
         "receipt_fee:settle", "receipt_fee:replay"],
)
def test_a_row_go_wrote_wrong_fails_the_comparison(
    corrupt: Corrupt, scenario: str, ignore: tuple[str, ...], diverges_at: str,
) -> None:
    """Positive control: one field Go writes, wrong, and Python decides
    differently, which the comparison reports at that behaviour."""
    compare(_case(CONTROL_CASE), scenario, ignore=ignore)
    with pytest.raises(AssertionError, match=r"Go-shaped rows diverge at " + diverges_at.replace(".", r"\.")):
        compare(_case(CONTROL_CASE), scenario, corrupt=corrupt, ignore=ignore)

