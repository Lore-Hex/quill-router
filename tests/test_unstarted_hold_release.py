"""The early release of a hold whose stream never started
(docs/design/fast-admission-streams.md section 13).

Three levels: the rule as a pure function (its class, its deadline and each
reason it keeps a hold), the reaper's transaction with the rule applied on the
Spanner fake (the release itself, log mode, and each race the design names),
and the drain's pass with its lines, its lease refund and its switch.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from trusted_router import storage_gcp_authorize as authorize_mod
from trusted_router import unstarted_release as rule
from trusted_router.config import Settings
from trusted_router.gateway_boot import GatewayBoot
from trusted_router.services import fastpath_frontdoor
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_authorize import (
    SettleOutcome,
    UnstartedRelease,
    reap_expired_reservations_result,
    release_unstarted_holds_result,
    typed_finalize_atomic,
)
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import insert_reservation
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_request_records import insert_gateway_authorization
from trusted_router.storage_gcp_stage_d import HeartbeatResult, heartbeat_gateway_atomic
from trusted_router.storage_models import GatewayAuthorization, boot_with_heartbeat_history
from trusted_router.types import UsageType
from trusted_router.user_model_rules import GATEWAY_RESERVATION_TTL_SECONDS

ROOT = Path(__file__).resolve().parents[1]
PRICING = (ROOT / "tests" / "fixtures" / "stage_d" / "pricing_document.json").read_text().strip()
CREATED = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AFTER = rule.DEFAULT_AFTER_SECONDS
#: The age at which the rule first releases: the deadline and the clock skew allowance.
DUE = AFTER + rule.CLOCK_SKEW_ALLOWANCE_SECONDS
#: A boot that has declared usage heartbeats since two hours before the rows here.
SINCE = "2026-11-21T10:00:00Z"
TTL = timedelta(seconds=GATEWAY_RESERVATION_TTL_SECONDS)
BOOT = "boot-kid"
HOLD = 300


def _authorization(**overrides: Any) -> GatewayAuthorization:
    """A heartbeated stream as authorize leaves it: Stage D ok, its pricing
    snapshot and boot binding, and nothing a heartbeat writes."""
    fields: dict[str, Any] = {
        "id": "gwa-unstarted",
        "workspace_id": "workspace",
        "key_hash": "key",
        "model_id": "model",
        "provider": "anthropic",
        "usage_type": UsageType.CREDITS,
        "estimated_microdollars": HOLD,
        "credit_reservation_id": "reservation",
        "created_at": CREATED.isoformat().replace("+00:00", "Z"),
        "pricing_snapshot": PRICING,
        "heartbeat_seq": 0,
        "stage_d_reason": "ok",
        "stage_d_prompt_tokens": 100,
        "stage_d_max_output_tokens": 100,
        "stage_d_boot_kid": BOOT,
    }
    fields.update(overrides)
    return GatewayAuthorization(**fields)


def _since(_kid: str) -> str | None:
    return SINCE


def _why(authorization: Any, *, age: int = DUE, since: Any = _since, reservation: str = "reservation") -> str | None:
    return rule.ineligibility(
        authorization,
        reservation_id=reservation,
        now=CREATED + timedelta(seconds=age),
        after_seconds=AFTER,
        declared_since=since,
    )


# -- the rule ----------------------------------------------------------------


def test_the_rule_releases_at_the_deadline_plus_the_skew_allowance_and_not_a_second_before() -> None:
    row = _authorization()
    assert DUE == AFTER + 60
    assert _why(row, age=AFTER) == rule.YOUNG
    assert _why(row, age=DUE - 1) == rule.YOUNG
    assert _why(row, age=DUE) is None
    assert _why(row, age=DUE + 1) is None
    # A creation time a second ahead of this clock is a younger row.
    assert _why(replace(row, created_at="2026-11-21T12:00:01Z"), age=DUE) == rule.YOUNG
    # One that cannot be read is no age at all: the hold is kept.
    assert _why(replace(row, created_at="not a time"), age=10 * DUE) == rule.YOUNG
    assert _why(replace(row, created_at=cast(Any, None)), age=10 * DUE) == rule.YOUNG


@pytest.mark.parametrize(
    "heartbeat",
    [
        {"started_at": "2026-11-21T12:00:05Z"},
        {"heartbeat_seq": 1},
        {"heartbeat_at": "2026-11-21T12:00:05Z"},
        {"heartbeat_hash": "a" * 64},
        {"delivered_usage": "{}"},
        {"selected_endpoint_id": "anthropic/test"},
        {"heartbeat_seq": None, "started_at": "2026-11-21T12:00:05Z"},
    ],
    ids=["started", "heartbeated", "heartbeat-time", "heartbeat-hash", "delivered-usage", "selected-endpoint",
         "no-sequence-but-started"],
)
def test_anything_a_heartbeat_writes_keeps_the_hold(heartbeat: dict[str, Any]) -> None:
    assert _why(_authorization(**heartbeat), age=10 * DUE) == rule.STARTED


@pytest.mark.parametrize(
    "reason",
    ["not_streaming", "route", "boot_not_accepted", "stage_d_disabled", "mixed_usage_type", "service_tier",
     "pricing_kind", "heartbeat_undeclared", "abliterate_estimated_usage", "", "OK", None, True, 1],
)
def test_every_other_class_keeps_its_hold_at_any_age(reason: Any) -> None:
    """Only a stream the gateway heartbeats is released early: a request with
    no heartbeat at all (not a stream, another route, BYOK, a priority tier,
    a batch, a video job, a boot that was not accepted) may be running for
    as long as its provider takes, and nothing on its row says otherwise."""
    row = _authorization(stage_d_reason=reason)
    assert _why(row, age=GATEWAY_RESERVATION_TTL_SECONDS - 1) == rule.OTHER_CLASS
    assert not rule.heartbeating_stream(row)


class _Row:
    """A row with whatever attributes it is given, and no others."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _row_without(name: str) -> _Row:
    fields = dict(vars(_authorization()))
    del fields[name]
    return _Row(**fields)


@pytest.mark.parametrize(
    "broken",
    [
        {"pricing_snapshot": None},
        {"pricing_snapshot": "not-json"},
        {"pricing_snapshot": ""},
        {"pricing_snapshot": "[]"},
        {"pricing_snapshot": "{}"},
        {"pricing_snapshot": json.dumps({"v": 1, "kind": "endpoint", "candidates": []})},
        {"pricing_snapshot": json.dumps({"v": 2, "kind": "endpoint", "candidates": [{}]})},
        {"pricing_snapshot": json.dumps({"v": 1, "kind": "video", "candidates": [{}]})},
        {"pricing_snapshot": {"v": 1}},
        {"stage_d_boot_kid": None},
        {"stage_d_boot_kid": ""},
        {"stage_d_boot_kid": 7},
        {"native_batch_eligible": True},
        {"native_batch_eligible": None},
        {"video_pricing_snapshot": "{}"},
        {"settlement": "deferred_home"},
        {"settlement": "spend_lease"},
        {"settlement": None},
        {"usage_type": UsageType.BYOK},
        {"stage_d_prompt_tokens": None},
        {"stage_d_max_output_tokens": None},
        {"stage_d_max_output_tokens": -1},
        {"stage_d_prompt_tokens": "100"},
        {"estimated_microdollars": None},
        {"heartbeat_seq": None},
        {"heartbeat_seq": "0"},
        {"heartbeat_seq": False},
        {"heartbeat_seq": -1},
    ],
    ids=lambda broken: ",".join(f"{name}={value!r}"[:44] for name, value in broken.items()),
)
def test_a_stage_d_row_that_is_not_as_authorize_writes_it_keeps_its_hold(broken: dict[str, Any]) -> None:
    """Every field the rule reads must be there, of the type authorize
    writes and with a value the rule knows. A snapshot that does not parse,
    a settlement that is not local, a sequence that is null where authorize
    writes zero: each keeps the hold."""
    row = replace(_authorization(), **broken)
    assert _why(row, age=GATEWAY_RESERVATION_TTL_SECONDS - 1) == rule.MALFORMED


@pytest.mark.parametrize(
    "missing",
    ["settlement", "usage_type", "native_batch_eligible", "video_pricing_snapshot", "stage_d_boot_kid",
     "pricing_snapshot", "stage_d_prompt_tokens", "stage_d_max_output_tokens", "estimated_microdollars",
     "heartbeat_seq"],
)
def test_a_field_the_rule_reads_that_is_missing_keeps_the_hold(missing: str) -> None:
    assert _why(_Row(**vars(_authorization()))) is None
    assert _why(_row_without(missing)) == rule.MALFORMED


def test_a_terminal_a_missing_or_a_misbound_row_is_left_alone() -> None:
    assert _why(None) == rule.NOT_TYPED
    assert _why(_authorization(settled=True)) == rule.TERMINAL
    assert _why(replace(_authorization(), settled=cast(Any, None))) == rule.TERMINAL
    assert _why(_row_without("settled")) == rule.TERMINAL
    assert _why(_row_without("stage_d_reason")) == rule.OTHER_CLASS
    assert _why(_authorization(), reservation="another") == rule.BINDING
    assert _why(_row_without("credit_reservation_id")) == rule.BINDING


@pytest.mark.parametrize(
    ("since", "released"),
    [
        ("2026-11-21T10:00:00Z", True),
        ("2026-11-21T11:59:00Z", True),  # exactly the allowance before the row
        ("2026-11-21T11:59:01Z", False),  # inside the allowance
        ("2026-11-21T12:00:00Z", False),  # the row's own instant
        ("2026-11-21T12:10:00Z", False),  # declared only after the row was made
        (None, False),
        ("", False),
        ("not a time", False),
        (True, False),
    ],
)
def test_the_boot_must_have_been_declaring_heartbeats_when_the_row_was_made(since: Any, released: bool) -> None:
    """Membership rests on what was true when the authorization was made:
    the boot's record must say it has declared usage heartbeats without a
    break since before the row's creation, by the skew allowance. That the
    boot declares them now says nothing about then."""
    assert (_why(_authorization(), since=lambda _kid: since) is None) is released
    if not released:
        assert _why(_authorization(), since=lambda _kid: since) == rule.BOOT_UNDECLARED


def test_the_boot_is_read_last_and_only_for_a_row_that_passed_every_other_test() -> None:
    asked: list[str] = []

    def since(kid: str) -> str | None:
        asked.append(kid)
        return None

    assert _why(_authorization(), since=since) == rule.BOOT_UNDECLARED
    assert asked == [BOOT]
    for row, age in (
        (_authorization(settled=True), DUE),
        (_authorization(stage_d_reason="not_streaming"), DUE),
        (_authorization(pricing_snapshot="not-json"), DUE),
        (_authorization(heartbeat_seq=1), DUE),
        (_authorization(), DUE - 1),
    ):
        assert _why(row, age=age, since=since) not in (None, rule.BOOT_UNDECLARED)
    assert asked == [BOOT]


def _registration(*, declares: bool, at: str, since: str | None = None) -> GatewayBoot:
    return GatewayBoot(
        kid=BOOT, jwk={"kty": "OKP", "crv": "Ed25519", "x": "x"}, approved=True, verified=True,
        image_digest="sha256:" + "aa" * 32, attestation_kind="gcp-cs-jwt", registered_at=at,
        declares_usage_heartbeat=declares, usage_heartbeat_declared_since=since,
    )


def test_a_boots_declaration_start_is_kept_only_across_an_unbroken_run() -> None:
    first = boot_with_heartbeat_history(None, _registration(declares=True, at="T1"), registered_at="T1")
    assert first.usage_heartbeat_declared_since == "T1"
    again = boot_with_heartbeat_history(first, _registration(declares=True, at="T1"), registered_at="T2")
    assert again.usage_heartbeat_declared_since == "T1"
    withdrawn = boot_with_heartbeat_history(again, _registration(declares=False, at="T1"), registered_at="T3")
    assert withdrawn.usage_heartbeat_declared_since is None
    restored = boot_with_heartbeat_history(withdrawn, _registration(declares=True, at="T1"), registered_at="T4")
    assert restored.usage_heartbeat_declared_since == "T4"
    # A record from before the field existed says nothing of its past: the
    # run starts at the first registration a store stamps.
    legacy = _registration(declares=True, at="T0")
    assert boot_with_heartbeat_history(legacy, legacy, registered_at="T5").usage_heartbeat_declared_since == "T5"
    # A time left on a record that does not declare is never carried.
    stale = _registration(declares=False, at="T0", since="T0")
    assert boot_with_heartbeat_history(stale, _registration(declares=True, at="T0"), registered_at="T6") \
        .usage_heartbeat_declared_since == "T6"
    undeclared = boot_with_heartbeat_history(None, _registration(declares=False, at="T7"), registered_at="T7")
    assert undeclared.usage_heartbeat_declared_since is None


def test_the_setting_defaults_to_off_and_its_bounds_are_the_rules() -> None:
    settings = Settings(environment="test")
    assert settings.unstarted_hold_release_mode == rule.MODE_OFF == "off"
    assert settings.unstarted_hold_release_seconds == rule.DEFAULT_AFTER_SECONDS == 900
    assert rule.RESERVATION_TTL_SECONDS == GATEWAY_RESERVATION_TTL_SECONDS
    assert rule.MIN_AFTER_SECONDS <= rule.DEFAULT_AFTER_SECONDS <= rule.MAX_AFTER_SECONDS < rule.RESERVATION_TTL_SECONDS
    for seconds in (rule.MIN_AFTER_SECONDS, rule.MAX_AFTER_SECONDS):
        assert Settings(environment="test", unstarted_hold_release_seconds=seconds)
    for seconds in (rule.MIN_AFTER_SECONDS - 1, rule.MAX_AFTER_SECONDS + 1, 0, -1):
        with pytest.raises(ValueError, match="TR_UNSTARTED_HOLD_RELEASE_SECONDS"):
            Settings(environment="test", unstarted_hold_release_seconds=seconds)
    with pytest.raises(ValueError):
        Settings(environment="test", unstarted_hold_release_mode=cast(Any, "yes"))
    assert get_args(Settings.model_fields["unstarted_hold_release_mode"].annotation) == rule.MODES


def test_production_runs_the_rule_in_log_mode_only() -> None:
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text(encoding="utf-8")
    assert rollout.count('"TR_UNSTARTED_HOLD_RELEASE_MODE=log"') == 1
    assert "TR_UNSTARTED_HOLD_RELEASE_MODE=on" not in rollout
    assert rollout.count('"TR_UNSTARTED_HOLD_RELEASE_SECONDS=900"') == 1


# -- the transaction, on the Spanner fake --------------------------------------


def _seed(
    db: FakeSpannerDatabase | None = None,
    *,
    suffix: str = "",
    hold: int = HOLD,
    expires_at: datetime | None = None,
    **overrides: Any,
) -> tuple[FakeSpannerDatabase, GatewayAuthorization]:
    """One authorization and its reservation, as authorize writes them: the
    reservation expires two hours after the creation."""
    reservation_id = "reservation" + suffix
    authorization = _authorization(
        id="gwa-unstarted" + suffix, credit_reservation_id=reservation_id, **overrides
    )
    db = db or FakeSpannerDatabase(now=CREATED)
    created = datetime.fromisoformat(str(authorization.created_at).replace("Z", "+00:00"))

    def seed(transaction: Any) -> None:
        insert_reservation(
            transaction,
            _ParamTypes,
            reservation_id=reservation_id,
            workspace_id="workspace",
            key_hash="key",
            ws_shard=0,
            credit_shard=0,
            key_shard=0,
            credit_reserved_micro=hold,
            key_reserved_micro=hold,
            hold_usage_type="Credits",
            authorization_id=authorization.id,
            idempotency_scope=None,
            idempotency_fingerprint=None,
            expires_at=expires_at or created + TTL,
        )
        insert_gateway_authorization(transaction, _ParamTypes, authorization, created_at=created)

    db.run_in_transaction(seed)
    return db, authorization


def _counters(db: FakeSpannerDatabase, *, reserved: int = HOLD) -> None:
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("workspace", 0)] = {
        "workspace_id": "workspace", "shard": 0, "total_credits": 1_000, "total_usage": 0,
        "reserved": reserved, "source_updated_at": None, "updated_at": None,
    }
    db.typed.setdefault(KEY_LIMIT_TABLE, {})[("key", 0)] = {
        "key_hash": "key", "shard": 0, "limit_microdollars": 1_000, "include_byok": True,
        "usage": 0, "byok_usage": 0, "reserved": reserved, "day_usage": 0, "day_start": None,
        "week_usage": 0, "week_start": None, "month_usage": 0, "month_start": None,
    }


def _money(db: FakeSpannerDatabase) -> tuple[int, int, int, int]:
    credit = db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]
    key = db.typed[KEY_LIMIT_TABLE][("key", 0)]
    return credit["reserved"], credit["total_usage"], key["reserved"], key["usage"]


def _state(db: FakeSpannerDatabase) -> dict[str, Any]:
    names = ("typed", "reservations", "gateway_authorizations", "settle_outbox", "generation_records")
    return {name: copy.deepcopy(getattr(db, name)) for name in names}


def _pass(
    db: FakeSpannerDatabase,
    *,
    age: int = DUE,
    log_only: bool = False,
    since: Any = _since,
    after_seconds: int = AFTER,
    limit: int = 100,
) -> authorize_mod.UnstartedReleaseResult:
    return release_unstarted_holds_result(
        db,
        _ParamTypes,
        now=CREATED + timedelta(seconds=age),
        limit=limit,
        rule=UnstartedRelease(after_seconds=after_seconds, log_only=log_only, declared_since=since),
    )


def _heartbeat(db: FakeSpannerDatabase, *, at: datetime, authorization_id: str = "gwa-unstarted") -> HeartbeatResult:
    return heartbeat_gateway_atomic(
        db,
        _ParamTypes,
        authorization_id=authorization_id,
        seq=1,
        started_at=CREATED,
        selected_endpoint_id="anthropic/test",
        usage={
            "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "price_tier_input_tokens": 0, "reasoning_tokens": 0,
        },
        heartbeat_hash="a" * 64,
        stream=True,
        grace_seconds=300,
        now=at,
    )


def test_the_hold_of_a_stream_that_never_started_is_given_back_at_the_deadline() -> None:
    db, authorization = _seed()
    _counters(db)

    result = _pass(db, age=DUE + 1)

    assert (result.scanned, result.released, result.released_hold_micro) == (1, 1, HOLD)
    assert (result.would_release, result.errors, result.guarded, result.guard_lost) == (0, 0, 0, 0)
    assert result.skipped == () and result.leased == () and result.unguarded is False
    # The reaper's refund: the exact holds given back, nothing booked.
    assert _money(db) == (0, 0, 0, 0)
    reservation = db.reservations["reservation"]
    assert reservation["settled"] is True and reservation["actual_micro"] == 0
    stored = db.gateway_authorizations[authorization.id]
    assert stored["settled"] is True and stored["finalization_outcome"] == "refunded"
    assert stored["finalized_cost_microdollars"] == 0
    # The terminal time is the pass's own time, not the horizon the scan used.
    assert stored["terminal_at"] == CREATED + timedelta(seconds=DUE + 1)
    assert db.generation_records == {}


def test_a_second_before_the_deadline_the_row_is_not_even_a_candidate() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    result = _pass(db, age=DUE - 1)

    assert (result.scanned, result.released) == (0, 0)
    assert _state(db) == before


def test_the_deadline_is_counted_from_the_authorizations_own_creation() -> None:
    """The scan finds candidates by the reservation's expiry; the decision
    is the authorization's age. A reservation that expires sooner than its
    two hours brings a young row into view, and the rule still keeps it."""
    db, _authorization_row = _seed(expires_at=CREATED + timedelta(seconds=120))
    _counters(db)
    before = _state(db)

    result = _pass(db, age=60)

    assert (result.scanned, result.released) == (1, 0)
    assert result.skipped == ((rule.YOUNG, 1),)
    assert _state(db) == before


@pytest.mark.parametrize(
    ("overrides", "scanned", "reason"),
    [
        ({"stage_d_reason": "not_streaming", "pricing_snapshot": None, "heartbeat_seq": None}, 0, None),
        ({"stage_d_reason": "route"}, 0, None),
        ({"stage_d_reason": "abliterate_estimated_usage"}, 0, None),
        ({"stage_d_boot_kid": None}, 0, None),
        ({"native_batch_eligible": True}, 1, rule.MALFORMED),
        ({"pricing_snapshot": "not-json"}, 1, rule.MALFORMED),
        ({"settlement": "deferred_home"}, 1, rule.MALFORMED),
    ],
    ids=["not-a-stream", "another-route", "estimated-usage", "no-boot-binding", "batch", "malformed-snapshot",
         "another-settlement"],
)
def test_a_request_the_rule_does_not_cover_keeps_its_hold_until_the_reaper(
    overrides: dict[str, Any], scanned: int, reason: str | None
) -> None:
    """Another class is not a candidate at all (the scan returns only the
    class), and a Stage D row that is not as authorize writes one is read
    and left."""
    db, authorization = _seed(**overrides)
    _counters(db)
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=GATEWAY_RESERVATION_TTL_SECONDS - 120, log_only=log_only)
        assert (result.scanned, result.released, result.would_release) == (scanned, 0, 0)
        assert result.skipped == (((reason, 1),) if reason else ())
        assert _state(db) == before
    # The reaper still takes it at its expiry, as before.
    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1))
    assert reaped.count == 1 and db.gateway_authorizations[authorization.id]["settled"] is True


def test_a_boot_that_did_not_declare_heartbeats_keeps_its_streams_holds() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    result = _pass(db, age=DUE + 1, since=lambda _kid: None)

    assert result.released == 0 and result.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _state(db) == before


def test_a_boots_declaration_is_read_once_a_pass_and_outside_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _first = _seed()
    _seed(db, suffix="-2")
    _counters(db, reserved=2 * HOLD)
    in_transaction: list[bool] = []
    asked: list[str] = []
    real = db.run_in_transaction

    def tracking(function: Any, *args: Any, **kwargs: Any) -> Any:
        in_transaction.append(True)
        try:
            return real(function, *args, **kwargs)
        finally:
            in_transaction.pop()

    monkeypatch.setattr(db, "run_in_transaction", tracking)

    def since(kid: str) -> str | None:
        assert not in_transaction, "the boot was read inside a transaction"
        asked.append(kid)
        return SINCE

    result = _pass(db, age=DUE + 1, since=since)

    assert result.released == 2 and asked == [BOOT]
    assert _money(db) == (0, 0, 0, 0)


def test_log_mode_reports_the_row_and_opens_no_read_write_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log mode reads on read-only snapshots: no read-write transaction, so
    no lock a concurrent settle or heartbeat could wait on, and nothing
    written."""
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)
    updates = db.transaction_execute_update_calls
    commits = db.commits
    transaction_reads = db.transaction_execute_sql_calls
    snapshots = len(db.snapshot_calls)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("log mode opened a read-write transaction")

    with monkeypatch.context() as patch:
        patch.setattr(db, "run_in_transaction", forbidden)
        result = _pass(db, age=DUE + 1, log_only=True)
        assert result.would_release == 1
        # The same row is found again the next pass.
        assert _pass(db, age=DUE + 301, log_only=True).would_release == 1
    assert (db.commits, db.transaction_execute_sql_calls) == (commits, transaction_reads)
    # Each pass reads its one candidate on one multi-use, read-only snapshot.
    assert sum(bool(call.get("multi_use")) for call in db.snapshot_calls[snapshots:]) == 2

    assert (result.scanned, result.would_release, result.would_release_hold_micro) == (1, 1, HOLD)
    assert (result.released, result.released_hold_micro, result.leased) == (0, 0, ())
    assert _state(db) == before
    assert db.transaction_execute_update_calls == updates
    # And then it is the reaper's.
    assert reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1)).count == 1


def test_a_first_heartbeat_before_the_release_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The race, heartbeat first: it lands after the pass's scan and before
    the release's transaction, whose own read then finds a started row."""
    db, authorization = _seed()
    _counters(db)
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic

    def heartbeat_then_finalize(*args: Any, **kwargs: Any) -> Any:
        assert _heartbeat(db, at=CREATED + timedelta(seconds=DUE)).accepted
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", heartbeat_then_finalize)

    result = _pass(db, age=DUE + 1)

    assert result.released == 0 and result.skipped == ((rule.STARTED, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    stored = db.gateway_authorizations[authorization.id]
    assert stored["settled"] is False and stored["heartbeat_seq"] == 1 and stored["started_at"] is not None
    assert db.reservations["reservation"]["settled"] is False


def test_a_first_heartbeat_after_the_release_is_refused() -> None:
    """The race, release first: the gateway's first heartbeat meets a
    terminal row, so it answers its client 503 with nothing delivered and
    sends no settle. Nothing of the heartbeat is written."""
    db, authorization = _seed()
    _counters(db)
    assert _pass(db, age=DUE + 1).released == 1
    after_release = _state(db)

    refused = _heartbeat(db, at=CREATED + timedelta(seconds=DUE + 2))

    assert refused.accepted is False and refused.reason == "already_terminal"
    assert _state(db) == after_release
    assert db.gateway_authorizations[authorization.id]["heartbeat_seq"] in (0, None)


def test_a_settle_after_the_release_books_nothing_and_releases_nothing_twice() -> None:
    db, authorization = _seed()
    _counters(db)
    assert _pass(db, age=DUE + 1).released == 1
    after_release = _state(db)

    settle_row = replace(authorization)
    settle_row.record_finalization(
        success=True, actual_microdollars=120, selected_usage_type=UsageType.CREDITS, generation=None,
    )
    late = typed_finalize_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        authorization_id=authorization.id,
        success=True,
        actual_micro=120,
        settled_usage_type="Credits",
        now=CREATED + timedelta(seconds=DUE + 5),
        outbox_available=True,
        authorization=settle_row,
        auth_body_settled=json_body(settle_row),
    )

    assert late["outcome"] != SettleOutcome.SETTLED
    assert _state(db) == after_release
    assert _money(db) == (0, 0, 0, 0)


@pytest.mark.parametrize("success", [True, False], ids=["settle", "refund"])
def test_a_terminal_before_the_release_wins_and_the_release_does_nothing(success: bool) -> None:
    db, authorization = _seed()
    _counters(db)
    terminal_row = replace(authorization)
    terminal_row.record_finalization(
        success=success, actual_microdollars=120 if success else 0,
        selected_usage_type=UsageType.CREDITS, generation=None,
    )
    first = typed_finalize_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        authorization_id=authorization.id,
        success=success,
        actual_micro=120 if success else 0,
        settled_usage_type="Credits",
        now=CREATED + timedelta(seconds=DUE),
        outbox_available=True,
        authorization=terminal_row,
        auth_body_settled=json_body(terminal_row),
    )
    assert first["outcome"] == SettleOutcome.SETTLED
    after_terminal = _state(db)

    # The scan no longer returns a settled reservation; a pass that had
    # scanned it a moment before reads it again under its transaction.
    assert _pass(db, age=DUE + 1).scanned == 0
    stale = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=CREATED + timedelta(seconds=DUE + 1),
        guard_outbox=True,
        snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
        expiry_horizon=CREATED + timedelta(seconds=DUE + 1) + TTL,
        unstarted=UnstartedRelease(after_seconds=AFTER, log_only=False, declared_since=_since),
    )

    assert stale.outcome == SettleOutcome.NOT_ELIGIBLE
    assert _state(db) == after_terminal
    assert _money(db) == (0, 120 if success else 0, 0, 120 if success else 0)


@pytest.mark.parametrize("status", ["pending", "dead"])
def test_a_pending_settle_intent_freezes_the_hold(status: str) -> None:
    """A settle the control plane has made durable and not yet applied: the
    outbox guard, in the scan and again under the transaction."""
    db, authorization = _seed()
    _counters(db)
    db.settle_outbox[(authorization.id, "settle")] = {
        "authorization_id": authorization.id, "intent_kind": "settle", "status": status,
    }
    before = _state(db)

    assert _pass(db, age=DUE + 1).scanned == 0
    guarded = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=CREATED + timedelta(seconds=DUE + 1),
        guard_outbox=True,
        snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
        expiry_horizon=CREATED + timedelta(seconds=DUE + 1) + TTL,
        unstarted=UnstartedRelease(after_seconds=AFTER, log_only=False, declared_since=_since),
    )

    assert guarded.outcome == SettleOutcome.OUTBOX_GUARDED
    assert _state(db) == before


def test_without_the_settle_outbox_the_pass_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)
    monkeypatch.setattr(authorize_mod, "_outbox_table_available", lambda _database, _types: False)
    reads = db.snapshot_execute_sql_calls

    result = _pass(db, age=DUE + 1)

    assert result.unguarded is True and (result.scanned, result.released) == (0, 0)
    assert _state(db) == before and db.snapshot_execute_sql_calls == reads


def test_a_row_the_reaper_already_took_is_not_taken_again() -> None:
    db, authorization = _seed()
    _counters(db)
    assert reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1)).count == 1
    after_reap = _state(db)

    result = _pass(db, age=GATEWAY_RESERVATION_TTL_SECONDS + 2)

    assert (result.scanned, result.released) == (0, 0)
    assert _state(db) == after_reap
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"


def test_one_rows_failure_is_its_own_and_the_pass_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _first = _seed()
    _seed(db, suffix="-2")
    _counters(db, reserved=2 * HOLD)
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic

    def fail_the_first(*args: Any, **kwargs: Any) -> Any:
        if kwargs["reservation_id"] == "reservation":
            raise RuntimeError("storage unavailable")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", fail_the_first)

    result = _pass(db, age=DUE + 1)

    assert (result.scanned, result.released, result.errors) == (2, 1, 1)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False


def test_a_leased_streams_row_is_released_and_named_for_the_leases_refund() -> None:
    """A stream admitted under a fast path lease: the reservation holds
    nothing, so the row's release moves no counter, and the pass names the
    authorization for the refund that gives the lease's hold back."""
    db, authorization = _seed(hold=0, fastpath_envelope="sealed-envelope")
    _counters(db, reserved=0)

    logged = _pass(db, age=DUE + 1, log_only=True)
    assert (logged.would_release, logged.would_release_leased, logged.leased) == (1, 1, ())

    result = _pass(db, age=DUE + 1)

    assert (result.released, result.released_hold_micro, result.leased) == (1, 0, (authorization.id,))
    assert _money(db) == (0, 0, 0, 0)
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"


def test_the_reaper_itself_is_unchanged_by_the_rules_parameters() -> None:
    """The reaper passes neither parameter: a hold short of its expiry is
    not its to take, whatever its class or age."""
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL - timedelta(seconds=1))

    assert reaped.count == 0 and reaped.not_eligible == 0
    assert _state(db) == before


# -- the store and the drain ---------------------------------------------------


def _boot(*, declared: bool) -> GatewayBoot:
    return GatewayBoot(
        kid=BOOT, jwk={"kty": "OKP", "crv": "Ed25519", "x": "x"}, approved=True, verified=True,
        image_digest="sha256:" + "aa" * 32, attestation_kind="gcp-cs-jwt",
        registered_at="2026-11-21T00:00:00Z", declares_usage_heartbeat=declared,
    )


def _store(*, declared: bool = True, **overrides: Any) -> tuple[Any, FakeSpannerDatabase, GatewayAuthorization]:
    store, db = make_fake_store(request_record_write_mode="typed")
    db.now = CREATED
    configure_store(store)
    _db, authorization = _seed(db, **overrides)
    _counters(db, reserved=int(overrides.get("hold", HOLD)))
    store.observe_gateway_boot(_boot(declared=declared))
    return store, db, authorization


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@pytest.fixture
def drain_lines() -> Any:
    handler = _Lines()
    drain.logger.addHandler(handler)
    previous = drain.logger.level
    drain.logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        drain.logger.setLevel(previous)
        drain.logger.removeHandler(handler)


@pytest.mark.parametrize("declared", [True, False])
def test_the_store_reads_the_declaration_from_the_boots_own_record(declared: bool) -> None:
    store, db, _authorization_row = _store(declared=declared)

    result = store.release_unstarted_holds_result(
        now=CREATED + timedelta(seconds=DUE + 1), after_seconds=AFTER, log_only=False,
    )

    assert result.released == int(declared)
    assert result.skipped == (() if declared else ((rule.BOOT_UNDECLARED, 1),))
    assert _money(db) == ((0, 0, 0, 0) if declared else (HOLD, 0, HOLD, 0))


def test_a_boot_record_that_is_gone_or_unreadable_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, authorization = _store()
    db.gateway_authorizations[authorization.id]["stage_d_boot_kid"] = "boot-gone"
    payload = json.loads(db.gateway_authorizations[authorization.id]["payload"])
    payload["stage_d_boot_kid"] = "boot-gone"
    db.gateway_authorizations[authorization.id]["payload"] = json.dumps(payload)

    gone = store.release_unstarted_holds_result(
        now=CREATED + timedelta(seconds=DUE + 1), after_seconds=AFTER, log_only=False,
    )
    assert gone.released == 0 and gone.skipped == ((rule.BOOT_UNDECLARED, 1),)

    def unreadable(_self: Any, _kid: str) -> Any:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(type(store), "get_gateway_boot", unreadable)
    unread = store.release_unstarted_holds_result(
        now=CREATED + timedelta(seconds=DUE + 1), after_seconds=AFTER, log_only=False,
    )
    assert unread.released == 0 and unread.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)


def test_the_pass_in_on_mode_releases_counts_and_reports(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER)

    assert result is not None and result.released == 1
    assert _money(db) == (0, 0, 0, 0)
    messages = drain_lines.messages()
    assert messages == [
        "gateway.unstarted_release mode=on rule=unstarted_stream after_seconds=900 scanned=1 "
        "released=1 released_hold_micro=300 would_release=0 would_release_hold_micro=0 "
        "would_release_leased=0 guarded=0 guard_lost=0 errors=0 unguarded=false truncated=false skipped=none "
        "lease_refunds=0 lease_refund_failures=0",
        "metric=gateway_hold_released_unstarted_stream_total value=1",
    ]
    counted = drain_lines.records[1]
    assert counted.levelno == logging.WARNING
    assert counted.metric == drain._RELEASED_UNSTARTED_METRIC  # type: ignore[attr-defined]


def test_the_pass_in_log_mode_releases_nothing_and_counts_no_release(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()
    before = _state(db)

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="log", after_seconds=AFTER)

    assert result is not None and (result.released, result.would_release) == (0, 1)
    assert _state(db) == before
    assert drain_lines.messages() == [
        "gateway.unstarted_release mode=log rule=unstarted_stream after_seconds=900 scanned=1 "
        "released=0 released_hold_micro=0 would_release=1 would_release_hold_micro=300 "
        "would_release_leased=0 guarded=0 guard_lost=0 errors=0 unguarded=false truncated=false skipped=none "
        "lease_refunds=0 lease_refund_failures=0",
    ]


@pytest.mark.parametrize("mode", ["off", "", "ON", "release"])
def test_off_and_any_unknown_mode_never_reach_the_store(
    mode: str, drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, db, _authorization_row = _store()
    before = _state(db)

    def forbidden(_self: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the early rule ran while off")

    monkeypatch.setattr(type(store), "release_unstarted_holds_result", forbidden)
    reads = db.snapshot_execute_sql_calls

    assert drain.release_unstarted_holds(CREATED + TTL, mode=mode, after_seconds=AFTER) is None
    assert _state(db) == before and db.snapshot_execute_sql_calls == reads
    assert drain_lines.messages() == []


def test_a_failing_pass_is_logged_and_never_raised(drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, _authorization_row = _store()
    before = _state(db)

    def broken(_self: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(type(store), "release_unstarted_holds_result", broken)

    assert drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER) is None
    assert _state(db) == before
    assert drain_lines.messages() == ["gateway.unstarted_release failed mode=on error_class=RuntimeError"]


def test_a_store_without_the_capability_is_left_alone(drain_lines: _Lines) -> None:
    """The memory store and the Postgres planes: no pass, as before."""
    from trusted_router.storage import STORE, InMemoryStore

    configure_store(InMemoryStore())
    assert not hasattr(STORE, "release_unstarted_holds_result")
    assert drain.release_unstarted_holds(CREATED + TTL, mode="on", after_seconds=AFTER) is None
    assert drain_lines.messages() == []


class _FrontDoor:
    def __init__(self, status: str = fastpath_frontdoor.WON, error: Exception | None = None) -> None:
        self.status, self.error = status, error
        self.refunds: list[dict[str, Any]] = []

    def refund(self, *, envelope: str, money: dict[str, Any]) -> fastpath_frontdoor.Terminal:
        self.refunds.append({"envelope": envelope, "money": money})
        if self.error is not None:
            raise self.error
        return fastpath_frontdoor.Terminal(
            status=self.status, kind="refund", charge=0, outcome="", cost=0, cost_known=False,
        )


def _front_door(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    from trusted_router.routes.internal import gateway

    monkeypatch.setattr(gateway, "_fastpath_front_door", lambda _settings: client)


def test_a_released_leased_row_sends_the_lease_the_refund_the_gateway_never_did(
    drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_, db, authorization = _store(hold=0, fastpath_envelope="sealed-envelope")
    client = _FrontDoor()
    _front_door(monkeypatch, client)

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER)

    assert result is not None and result.leased == (authorization.id,)
    # After the row is terminal, with the refund's own money: no charge.
    assert db.gateway_authorizations[authorization.id]["settled"] is True
    assert client.refunds == [{"envelope": "sealed-envelope", "money": {"charge": 0, "estimate": HOLD}}]
    assert "lease_refunds=1 lease_refund_failures=0" in drain_lines.messages()[0]


def test_log_mode_sends_a_lease_nothing(drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_, db, _authorization_row = _store(hold=0, fastpath_envelope="sealed-envelope")
    client = _FrontDoor()
    _front_door(monkeypatch, client)
    before = _state(db)

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="log", after_seconds=AFTER)

    assert result is not None and (result.would_release, result.would_release_leased) == (1, 1)
    assert client.refunds == [] and _state(db) == before


@pytest.mark.parametrize(
    "client",
    [
        _FrontDoor(status=fastpath_frontdoor.FAILED),
        _FrontDoor(status=fastpath_frontdoor.INVALID),
        _FrontDoor(error=fastpath_frontdoor.FrontDoorUnavailable("/v1/refund")),
        _FrontDoor(error=RuntimeError("socket closed")),
        None,
    ],
    ids=["failed", "invalid", "unavailable", "raised", "no-front-door"],
)
def test_a_lease_refund_that_does_not_land_is_counted_and_the_row_stays_released(
    client: Any, drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease's hold then ends uncharged at its end of life, as every
    such hold did before the rule: the row is terminal, so no heartbeat and
    no settle of it reaches the lease again."""
    _store_, db, authorization = _store(hold=0, fastpath_envelope="sealed-envelope")
    _front_door(monkeypatch, client)

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER)

    assert result is not None and result.released == 1
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"
    summary = next(line for line in drain_lines.messages() if line.startswith("gateway.unstarted_release mode=on"))
    assert "lease_refunds=0 lease_refund_failures=1" in summary
    refused = _heartbeat(db, at=CREATED + timedelta(seconds=DUE + 2))
    assert refused.accepted is False and refused.reason == "already_terminal"


def test_a_pass_that_stops_at_its_page_budget_says_so(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store(stage_d_boot_kid="boot-never-registered")
    pages = authorize_mod._UNSTARTED_SCAN_PAGES
    for index in range(2 * pages):
        _seed(db, suffix=f"-{index}", stage_d_boot_kid="boot-never-registered")

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER, limit=2)

    assert result is not None and (result.scanned, result.released, result.truncated) == (2 * pages, 0, True)
    assert drain_lines.messages()[-1] == f"gateway.unstarted_release scan truncated scanned={2 * pages} limit=2"
    assert f"truncated=true skipped=boot_undeclared:{2 * pages}" in drain_lines.messages()[0]


def test_each_hold_the_reaper_releases_is_counted_under_its_own_rule(drain_lines: _Lines) -> None:
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 3)
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 0)
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, -1)

    assert drain_lines.messages() == ["metric=gateway_hold_released_expired_total value=1"] * 3
    assert {record.levelno for record in drain_lines.records} == {logging.WARNING}
    assert {record.metric for record in drain_lines.records} == {"gateway_hold_released_expired_total"}  # type: ignore[attr-defined]


# -- membership is frozen at authorize time, and the scan makes progress --------


def test_a_boot_that_declares_heartbeats_only_later_does_not_bring_an_older_row_into_the_class() -> None:
    """The interleaving the rule must refuse: a boot registers without the
    declaration; a stream is authorized Stage D ok (enforcement is off) and
    runs with no heartbeat; the same key registers again, declaring. The
    declaration began after the row was made, so the row is not of the
    class, however long the pass waits and however the boot reads now."""
    store, db = make_fake_store(request_record_write_mode="typed")
    db.now = CREATED
    configure_store(store)
    store.observe_gateway_boot(_registration(declares=False, at="2026-11-21T09:00:00Z"))
    _seed(db)
    _counters(db)
    before = _state(db)
    store.observe_gateway_boot(_registration(declares=True, at="2026-11-21T12:05:00Z"))
    boot = store.get_gateway_boot(BOOT)
    assert boot is not None and boot.declares_usage_heartbeat
    assert boot.usage_heartbeat_declared_since == "2026-11-21T12:05:00Z"

    for age in (DUE + 1, 3600, GATEWAY_RESERVATION_TTL_SECONDS - 120):
        for log_only in (True, False):
            result = store.release_unstarted_holds_result(
                now=CREATED + timedelta(seconds=age), after_seconds=AFTER, log_only=log_only,
            )
            assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
            assert result.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _state(db) == before

    # A row made after the declaration began (and the allowance) is of the class.
    later = "2026-11-21T12:06:00Z"
    _seed(db, suffix="-later", created_at=later)
    released = store.release_unstarted_holds_result(
        now=datetime(2026, 11, 21, 12, 6, tzinfo=UTC) + timedelta(seconds=DUE + 1), after_seconds=AFTER, log_only=False,
    )
    assert released.released == 1
    assert db.reservations["reservation-later"]["settled"] is True
    assert db.reservations["reservation"]["settled"] is False


def test_a_boot_that_withdraws_the_declaration_takes_its_rows_out_of_the_class() -> None:
    store, db, _authorization_row = _store()
    store.observe_gateway_boot(_registration(declares=False, at="2026-11-21T12:20:00Z"))
    assert store.get_gateway_boot(BOOT).usage_heartbeat_declared_since is None

    result = store.release_unstarted_holds_result(
        now=CREATED + timedelta(seconds=DUE + 1), after_seconds=AFTER, log_only=False,
    )

    assert result.released == 0 and result.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)


def test_requests_of_other_classes_past_the_deadline_never_hide_a_releasable_row() -> None:
    """A scan that stops at its limit must still find the row it can act
    on: three pages' worth of unsettled requests of other classes, all past
    the deadline and all expiring before it, and the releasable row is
    released in the first pass."""
    db, _excluded = _seed(suffix="-x0", stage_d_reason="not_streaming", pricing_snapshot=None, heartbeat_seq=None,
                          created_at="2026-11-21T11:00:00Z")
    for index in range(1, 30):
        _seed(db, suffix=f"-x{index}", stage_d_reason="not_streaming", pricing_snapshot=None, heartbeat_seq=None,
              created_at="2026-11-21T11:00:00Z")
    _seed(db)
    _counters(db)

    for log_only, expected in ((True, (1, 0, 1)), (False, (1, 1, 0)), (False, (0, 0, 0))):
        result = _pass(db, age=DUE + 1, log_only=log_only, limit=10)
        assert (result.scanned, result.released, result.would_release) == expected
        assert result.truncated is False
    assert db.reservations["reservation"]["settled"] is True
    assert sum(row["settled"] for row in db.reservations.values()) == 1


def test_rows_of_the_class_the_transaction_leaves_do_not_hide_the_ones_behind_them() -> None:
    """Pages follow a cursor in expiry order: rows of the class that every
    pass reads and leaves (their boot has no declaration to show) fill the
    first pages, and the releasable row behind them is still reached."""
    limit = 4
    db, _first = _seed(suffix="-u0", stage_d_boot_kid="boot-undeclared", created_at="2026-11-21T11:00:00Z")
    for index in range(1, 2 * limit + 1):
        _seed(db, suffix=f"-u{index}", stage_d_boot_kid="boot-undeclared", created_at="2026-11-21T11:00:00Z")
    _seed(db)
    _counters(db)

    def since(kid: str) -> str | None:
        return SINCE if kid == BOOT else None

    result = _pass(db, age=DUE + 1, since=since, limit=limit)

    assert (result.scanned, result.released, result.truncated) == (2 * limit + 2, 1, False)
    assert result.skipped == ((rule.BOOT_UNDECLARED, 2 * limit + 1),)
    assert db.reservations["reservation"]["settled"] is True
    # Passes repeat without ever taking a row they may not.
    again = _pass(db, age=DUE + 301, since=since, limit=limit)
    assert (again.scanned, again.released) == (2 * limit + 1, 0)


def test_the_scan_names_its_index_and_bounds_its_read() -> None:
    sql = authorize_mod._UNSTARTED_SCAN_SQL
    assert "FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_expiry} AS r " in sql
    assert "WHERE r.settled=false AND r.expires_at < @now " in sql
    assert "ON a.authorization_id = r.authorization_id" in sql  # one primary-key read a candidate
    assert sql.endswith("ORDER BY r.expires_at, r.reservation_id LIMIT @limit")
    assert authorize_mod._UNSTARTED_SCAN_PAGES == 5


@pytest.mark.parametrize("log_only", [True, False], ids=["log", "on"])
def test_a_settle_intent_made_durable_after_the_scan_still_freezes_the_hold(
    log_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan leaves out a row with a pending intent, but it is advisory:
    an intent that lands after it is found by the row's own read, in log
    mode's snapshot as in the release's transaction."""
    db, authorization = _seed()
    _counters(db)
    name = "_observe_unstarted_candidate" if log_only else "_finalize_reaped_reservation_atomic"
    real = getattr(authorize_mod, name)

    def intent_then_read(*args: Any, **kwargs: Any) -> Any:
        db.settle_outbox[(authorization.id, "settle")] = {
            "authorization_id": authorization.id, "intent_kind": "settle", "status": "pending",
        }
        return real(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, name, intent_then_read)

    result = _pass(db, age=DUE + 1, log_only=log_only)

    assert (result.scanned, result.guarded, result.released, result.would_release) == (1, 1, 0, 0)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False
