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
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType
from trusted_router.user_model_rules import GATEWAY_RESERVATION_TTL_SECONDS

ROOT = Path(__file__).resolve().parents[1]
PRICING = (ROOT / "tests" / "fixtures" / "stage_d" / "pricing_document.json").read_text().strip()
CREATED = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AFTER = rule.DEFAULT_AFTER_SECONDS
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


def _declared(_kid: str) -> bool:
    return True


def _why(authorization: Any, *, age: int = AFTER, declared: Any = _declared, reservation: str = "reservation") -> str | None:
    return rule.ineligibility(
        authorization,
        reservation_id=reservation,
        now=CREATED + timedelta(seconds=age),
        after_seconds=AFTER,
        heartbeat_declared=declared,
    )


# -- the rule ----------------------------------------------------------------


def test_the_rule_releases_at_the_deadline_and_not_a_second_before() -> None:
    row = _authorization()
    assert _why(row, age=AFTER - 1) == rule.YOUNG
    assert _why(row, age=AFTER) is None
    assert _why(row, age=AFTER + 1) is None
    # A creation time a second ahead of this clock (skew) is a younger row.
    assert _why(replace(row, created_at="2026-11-21T12:00:01Z"), age=AFTER) == rule.YOUNG
    # One that cannot be read is no age at all: the hold is kept.
    assert _why(replace(row, created_at="not a time"), age=10 * AFTER) == rule.YOUNG


@pytest.mark.parametrize(
    "heartbeat",
    [
        {"started_at": "2026-11-21T12:00:05Z"},
        {"heartbeat_seq": 1},
        {"heartbeat_at": "2026-11-21T12:00:05Z"},
        {"delivered_usage": "{}"},
    ],
    ids=["started", "heartbeated", "heartbeat-time", "delivered-usage"],
)
def test_anything_a_heartbeat_writes_keeps_the_hold(heartbeat: dict[str, Any]) -> None:
    assert _why(_authorization(**heartbeat), age=10 * AFTER) == rule.STARTED


@pytest.mark.parametrize(
    "other",
    [
        {"stage_d_reason": "not_streaming"},
        {"stage_d_reason": "route"},
        {"stage_d_reason": "boot_not_accepted", "stage_d_boot_kid": None, "pricing_snapshot": None},
        {"stage_d_reason": "stage_d_disabled"},
        {"stage_d_reason": "mixed_usage_type"},
        {"stage_d_reason": "service_tier"},
        {"stage_d_reason": "abliterate_estimated_usage"},
        {"stage_d_reason": None},
        {"pricing_snapshot": None},
        {"stage_d_boot_kid": None},
        {"stage_d_boot_kid": ""},
        {"native_batch_eligible": True},
        {"video_pricing_snapshot": "{}"},
        {"settlement": "deferred_home"},
    ],
    ids=lambda other: ",".join(f"{name}={value}"[:40] for name, value in other.items()),
)
def test_every_other_class_keeps_its_hold_at_any_age(other: dict[str, Any]) -> None:
    """Only a stream the gateway heartbeats is released early: a request with
    no heartbeat at all (not a stream, another route, BYOK, a priority tier,
    a batch, a video job, a boot that was not accepted) may be running for
    as long as its provider takes, and nothing on its row says otherwise."""
    assert _why(_authorization(**other), age=GATEWAY_RESERVATION_TTL_SECONDS - 1) == rule.OTHER_CLASS


def test_a_terminal_a_missing_or_a_misbound_row_is_left_alone() -> None:
    assert _why(None) == rule.NOT_TYPED
    assert _why(_authorization(settled=True)) == rule.TERMINAL
    assert _why(_authorization(), reservation="another") == rule.BINDING


def test_a_boot_that_did_not_declare_heartbeats_keeps_the_hold_and_is_read_last() -> None:
    asked: list[str] = []

    def undeclared(kid: str) -> bool:
        asked.append(kid)
        return False

    assert _why(_authorization(), declared=undeclared) == rule.BOOT_UNDECLARED
    assert asked == [BOOT]
    # Any other reason answers before the boot is read.
    for row, age in (
        (_authorization(settled=True), AFTER),
        (_authorization(stage_d_reason="not_streaming"), AFTER),
        (_authorization(heartbeat_seq=1), AFTER),
        (_authorization(), AFTER - 1),
    ):
        assert _why(row, age=age, declared=undeclared) not in (None, rule.BOOT_UNDECLARED)
    assert asked == [BOOT]


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
    age: int = AFTER,
    log_only: bool = False,
    declared: Any = _declared,
    after_seconds: int = AFTER,
    limit: int = 100,
) -> authorize_mod.UnstartedReleaseResult:
    return release_unstarted_holds_result(
        db,
        _ParamTypes,
        now=CREATED + timedelta(seconds=age),
        limit=limit,
        rule=UnstartedRelease(after_seconds=after_seconds, log_only=log_only, heartbeat_declared=declared),
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

    result = _pass(db, age=AFTER + 1)

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
    assert stored["terminal_at"] == CREATED + timedelta(seconds=AFTER + 1)
    assert db.generation_records == {}


def test_a_second_before_the_deadline_the_row_is_not_even_a_candidate() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    result = _pass(db, age=AFTER - 1)

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
    ("overrides", "reason"),
    [
        ({"stage_d_reason": "not_streaming", "pricing_snapshot": None}, rule.OTHER_CLASS),
        ({"stage_d_reason": "route"}, rule.OTHER_CLASS),
        ({"stage_d_reason": "abliterate_estimated_usage"}, rule.OTHER_CLASS),
        ({"stage_d_boot_kid": None}, rule.OTHER_CLASS),
        ({"native_batch_eligible": True}, rule.OTHER_CLASS),
    ],
    ids=["not-a-stream", "another-route", "estimated-usage", "no-boot-binding", "batch"],
)
def test_a_request_of_another_class_keeps_its_hold_until_the_reaper(
    overrides: dict[str, Any], reason: str
) -> None:
    db, authorization = _seed(**overrides)
    _counters(db)
    before = _state(db)

    result = _pass(db, age=GATEWAY_RESERVATION_TTL_SECONDS - 60)

    assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
    assert result.skipped == ((reason, 1),)
    assert _state(db) == before
    # The reaper still takes it at its expiry, as before.
    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1))
    assert reaped.count == 1 and db.gateway_authorizations[authorization.id]["settled"] is True


def test_a_boot_that_did_not_declare_heartbeats_keeps_its_streams_holds() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    result = _pass(db, age=AFTER + 1, declared=lambda _kid: False)

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

    def declared(kid: str) -> bool:
        assert not in_transaction, "the boot was read inside a transaction"
        asked.append(kid)
        return True

    result = _pass(db, age=AFTER + 1, declared=declared)

    assert result.released == 2 and asked == [BOOT]
    assert _money(db) == (0, 0, 0, 0)


def test_log_mode_reports_the_row_and_writes_nothing() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)
    updates = db.transaction_execute_update_calls

    result = _pass(db, age=AFTER + 1, log_only=True)

    assert (result.scanned, result.would_release, result.would_release_hold_micro) == (1, 1, HOLD)
    assert (result.released, result.released_hold_micro, result.leased) == (0, 0, ())
    assert _state(db) == before
    assert db.transaction_execute_update_calls == updates
    # The same row is found again the next pass, and then by the reaper.
    assert _pass(db, age=AFTER + 301, log_only=True).would_release == 1
    assert reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1)).count == 1


def test_a_first_heartbeat_before_the_release_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The race, heartbeat first: it lands after the pass's scan and before
    the release's transaction, whose own read then finds a started row."""
    db, authorization = _seed()
    _counters(db)
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic

    def heartbeat_then_finalize(*args: Any, **kwargs: Any) -> Any:
        assert _heartbeat(db, at=CREATED + timedelta(seconds=AFTER)).accepted
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", heartbeat_then_finalize)

    result = _pass(db, age=AFTER + 1)

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
    assert _pass(db, age=AFTER + 1).released == 1
    after_release = _state(db)

    refused = _heartbeat(db, at=CREATED + timedelta(seconds=AFTER + 2))

    assert refused.accepted is False and refused.reason == "already_terminal"
    assert _state(db) == after_release
    assert db.gateway_authorizations[authorization.id]["heartbeat_seq"] in (0, None)


def test_a_settle_after_the_release_books_nothing_and_releases_nothing_twice() -> None:
    db, authorization = _seed()
    _counters(db)
    assert _pass(db, age=AFTER + 1).released == 1
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
        now=CREATED + timedelta(seconds=AFTER + 5),
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
        now=CREATED + timedelta(seconds=AFTER),
        outbox_available=True,
        authorization=terminal_row,
        auth_body_settled=json_body(terminal_row),
    )
    assert first["outcome"] == SettleOutcome.SETTLED
    after_terminal = _state(db)

    # The scan no longer returns a settled reservation; a pass that had
    # scanned it a moment before reads it again under its transaction.
    assert _pass(db, age=AFTER + 1).scanned == 0
    stale = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=CREATED + timedelta(seconds=AFTER + 1),
        guard_outbox=True,
        snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
        expiry_horizon=CREATED + timedelta(seconds=AFTER + 1) + TTL,
        unstarted=UnstartedRelease(after_seconds=AFTER, log_only=False, heartbeat_declared=_declared),
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

    assert _pass(db, age=AFTER + 1).scanned == 0
    guarded = authorize_mod._finalize_reaped_reservation_atomic(
        db,
        _ParamTypes,
        reservation_id="reservation",
        reap_now=CREATED + timedelta(seconds=AFTER + 1),
        guard_outbox=True,
        snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
        expiry_horizon=CREATED + timedelta(seconds=AFTER + 1) + TTL,
        unstarted=UnstartedRelease(after_seconds=AFTER, log_only=False, heartbeat_declared=_declared),
    )

    assert guarded.outcome == SettleOutcome.OUTBOX_GUARDED
    assert _state(db) == before


def test_without_the_settle_outbox_the_pass_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)
    monkeypatch.setattr(authorize_mod, "_outbox_table_available", lambda _database, _types: False)
    reads = db.snapshot_execute_sql_calls

    result = _pass(db, age=AFTER + 1)

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

    result = _pass(db, age=AFTER + 1)

    assert (result.scanned, result.released, result.errors) == (2, 1, 1)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False


def test_a_leased_streams_row_is_released_and_named_for_the_leases_refund() -> None:
    """A stream admitted under a fast path lease: the reservation holds
    nothing, so the row's release moves no counter, and the pass names the
    authorization for the refund that gives the lease's hold back."""
    db, authorization = _seed(hold=0, fastpath_envelope="sealed-envelope")
    _counters(db, reserved=0)

    logged = _pass(db, age=AFTER + 1, log_only=True)
    assert (logged.would_release, logged.would_release_leased, logged.leased) == (1, 1, ())

    result = _pass(db, age=AFTER + 1)

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
        now=CREATED + timedelta(seconds=AFTER + 1), after_seconds=AFTER, log_only=False,
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
        now=CREATED + timedelta(seconds=AFTER + 1), after_seconds=AFTER, log_only=False,
    )
    assert gone.released == 0 and gone.skipped == ((rule.BOOT_UNDECLARED, 1),)

    def unreadable(_self: Any, _kid: str) -> Any:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(type(store), "get_gateway_boot", unreadable)
    unread = store.release_unstarted_holds_result(
        now=CREATED + timedelta(seconds=AFTER + 1), after_seconds=AFTER, log_only=False,
    )
    assert unread.released == 0 and unread.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)


def test_the_pass_in_on_mode_releases_counts_and_reports(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="on", after_seconds=AFTER)

    assert result is not None and result.released == 1
    assert _money(db) == (0, 0, 0, 0)
    messages = drain_lines.messages()
    assert messages == [
        "gateway.unstarted_release mode=on rule=unstarted_stream after_seconds=900 scanned=1 "
        "released=1 released_hold_micro=300 would_release=0 would_release_hold_micro=0 "
        "would_release_leased=0 guarded=0 guard_lost=0 errors=0 unguarded=false skipped=none "
        "lease_refunds=0 lease_refund_failures=0",
        "metric=gateway_hold_released_unstarted_stream_total value=1",
    ]
    counted = drain_lines.records[1]
    assert counted.levelno == logging.WARNING
    assert counted.metric == drain._RELEASED_UNSTARTED_METRIC  # type: ignore[attr-defined]


def test_the_pass_in_log_mode_releases_nothing_and_counts_no_release(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()
    before = _state(db)

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="log", after_seconds=AFTER)

    assert result is not None and (result.released, result.would_release) == (0, 1)
    assert _state(db) == before
    assert drain_lines.messages() == [
        "gateway.unstarted_release mode=log rule=unstarted_stream after_seconds=900 scanned=1 "
        "released=0 released_hold_micro=0 would_release=1 would_release_hold_micro=300 "
        "would_release_leased=0 guarded=0 guard_lost=0 errors=0 unguarded=false skipped=none "
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

    assert drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="on", after_seconds=AFTER) is None
    assert _state(db) == before
    assert drain_lines.messages() == ["gateway.unstarted_release failed mode=on"]


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

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="on", after_seconds=AFTER)

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

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="log", after_seconds=AFTER)

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

    result = drain.release_unstarted_holds(CREATED + timedelta(seconds=AFTER + 1), mode="on", after_seconds=AFTER)

    assert result is not None and result.released == 1
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"
    summary = next(line for line in drain_lines.messages() if line.startswith("gateway.unstarted_release mode=on"))
    assert "lease_refunds=0 lease_refund_failures=1" in summary
    refused = _heartbeat(db, at=CREATED + timedelta(seconds=AFTER + 2))
    assert refused.accepted is False and refused.reason == "already_terminal"


def test_a_full_scan_is_told(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store(stage_d_reason="not_streaming", pricing_snapshot=None)
    _seed(db, suffix="-2", stage_d_reason="not_streaming", pricing_snapshot=None)

    result = drain.release_unstarted_holds(
        CREATED + timedelta(seconds=AFTER + 1), mode="on", after_seconds=AFTER, limit=2,
    )

    assert result is not None and (result.scanned, result.released) == (2, 0)
    assert drain_lines.messages()[-1] == "gateway.unstarted_release scan full scanned=2 limit=2"
    assert "skipped=other_class:2" in drain_lines.messages()[0]


def test_each_hold_the_reaper_releases_is_counted_under_its_own_rule(drain_lines: _Lines) -> None:
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 3)
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 0)
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, -1)

    assert drain_lines.messages() == ["metric=gateway_hold_released_expired_total value=1"] * 3
    assert {record.levelno for record in drain_lines.records} == {logging.WARNING}
    assert {record.metric for record in drain_lines.records} == {"gateway_hold_released_expired_total"}  # type: ignore[attr-defined]
