"""The early release of a hold whose stream never started
(docs/design/fast-admission-streams.md section 13).

Three levels: the rule on a stored row (its class, its deadline and each
reason it keeps a hold), the pass on the Spanner fake through the real
reader (the release itself, log mode, the scan's bound, and each race the
design names), and the store and the drain (the boot's declaration start by
commit time, the pass's lines, its lease refund and its switch).

The fake's clock is the database's: a reservation's commit timestamp, a boot
row's commit timestamp and the scan's own time are all read from it.
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

from tests.fakes.spanner import (
    FakeAborted,
    FakeSpannerDatabase,
    _FakeSnapshot,
    _ParamTypes,
    make_fake_store,
)
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
from trusted_router.storage_gcp_request_records import (
    insert_gateway_authorization,
    read_gateway_authorization,
)
from trusted_router.storage_gcp_stage_d import HeartbeatResult, heartbeat_gateway_atomic
from trusted_router.storage_models import (
    BOOT_DECLARED_AT_ITS_WRITE,
    GatewayAuthorization,
    boot_with_heartbeat_history,
)
from trusted_router.types import UsageType
from trusted_router.user_model_rules import GATEWAY_RESERVATION_TTL_SECONDS

ROOT = Path(__file__).resolve().parents[1]
PRICING = (ROOT / "tests" / "fixtures" / "stage_d" / "pricing_document.json").read_text().strip()
CREATED = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AFTER = rule.DEFAULT_AFTER_SECONDS
TTL = timedelta(seconds=GATEWAY_RESERVATION_TTL_SECONDS)
BOOT = "boot-kid"
HOLD = 300
#: A boot whose declaring run began two hours before the rows here.
SINCE = CREATED - timedelta(hours=2)


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


def _since(_kid: str) -> datetime | None:
    return SINCE


def _seed(
    db: FakeSpannerDatabase | None = None,
    *,
    suffix: str = "",
    hold: int = HOLD,
    created: datetime = CREATED,
    expires_at: datetime | None = None,
    **overrides: Any,
) -> tuple[FakeSpannerDatabase, GatewayAuthorization]:
    """One authorization and its reservation, committed at ``created`` by the
    database's clock, as authorize writes them: the reservation expires two
    hours after."""
    reservation_id = "reservation" + suffix
    authorization = _authorization(
        id="gwa-unstarted" + suffix, credit_reservation_id=reservation_id,
        created_at=created.isoformat().replace("+00:00", "Z"), **overrides,
    )
    db = db or FakeSpannerDatabase(now=created)
    clock, db.now = db.now, created

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
    db.now = clock
    return db, authorization


def _stored_row(db: FakeSpannerDatabase, authorization_id: str = "gwa-unstarted") -> dict[str, Any]:
    """The row as the real reader hands it to the rule."""
    stored: dict[str, Any] = {}
    with db.snapshot() as snapshot:
        assert read_gateway_authorization(snapshot, _ParamTypes, authorization_id, stored=stored) is not None
    return stored


def _edit_payload(db: FakeSpannerDatabase, edit: Any, authorization_id: str = "gwa-unstarted") -> None:
    """Change the stored payload itself, under the reader."""
    record = db.gateway_authorizations[authorization_id]
    payload = json.loads(record["payload"])
    edit(payload)
    record["payload"] = json.dumps(payload)


def _why(
    row: Any,
    *,
    age: int = AFTER,
    since: Any = _since,
    reservation: str = "reservation",
    created_at: Any = CREATED,
) -> str | None:
    return rule.ineligibility(
        row,
        reservation_id=reservation,
        created_at=created_at,
        read_at=CREATED + timedelta(seconds=age),
        after_seconds=AFTER,
        declared_since=since,
    )


@pytest.fixture
def stored() -> dict[str, Any]:
    db, _authorization_row = _seed()
    return _stored_row(db)


# -- the rule, on the row as stored ------------------------------------------


def test_the_stored_row_of_a_heartbeated_stream_is_what_the_rule_expects(stored: dict[str, Any]) -> None:
    assert _why(stored) is None
    # What authorize writes, as the reader hands it over: nothing defaulted.
    assert stored["settled"] is False and stored["settlement"] == "local" and stored["usage_type"] == "Credits"
    assert stored["native_batch_eligible"] is False and stored["heartbeat_seq"] == 0
    # The serializer's own omissions, and only those, read as None.
    assert not set(rule.OMITTED_WHEN_NONE) & stored.keys()


def test_the_rule_releases_at_the_deadline_by_the_databases_clock(stored: dict[str, Any]) -> None:
    assert _why(stored, age=AFTER - 1) == rule.YOUNG
    assert _why(stored, age=AFTER) is None
    assert _why(stored, age=AFTER + 1) is None
    # No commit timestamp, or none for the scan, is no age: the hold is kept.
    assert _why(stored, created_at=None) == rule.YOUNG
    assert _why(stored, created_at="not a time") == rule.YOUNG
    assert rule.ineligibility(stored, reservation_id="reservation", created_at=CREATED, read_at=None,
                              after_seconds=AFTER, declared_since=_since) == rule.YOUNG
    # The row's own application-clock creation time decides nothing.
    assert _why({**stored, "created_at": "2099-01-01T00:00:00Z"}) is None


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
def test_anything_a_heartbeat_writes_keeps_the_hold(heartbeat: dict[str, Any], stored: dict[str, Any]) -> None:
    assert _why({**stored, **heartbeat}, age=10 * AFTER) == rule.STARTED


@pytest.mark.parametrize(
    "reason",
    ["not_streaming", "route", "boot_not_accepted", "stage_d_disabled", "mixed_usage_type", "service_tier",
     "pricing_kind", "heartbeat_undeclared", "abliterate_estimated_usage", "", "OK", None, True, 1],
)
def test_every_other_class_keeps_its_hold_at_any_age(reason: Any, stored: dict[str, Any]) -> None:
    """Only a stream the gateway heartbeats is released early: a request with
    no heartbeat at all may be running for as long as its provider takes,
    and nothing on its row says otherwise."""
    assert _why({**stored, "stage_d_reason": reason}, age=GATEWAY_RESERVATION_TTL_SECONDS - 1) == rule.OTHER_CLASS
    missing = {name: value for name, value in stored.items() if name != "stage_d_reason"}
    assert _why(missing) == rule.OTHER_CLASS


@pytest.mark.parametrize(
    "broken",
    [
        {"stage_d_boot_kid": None}, {"stage_d_boot_kid": ""}, {"stage_d_boot_kid": 7},
        {"native_batch_eligible": True}, {"native_batch_eligible": None}, {"native_batch_eligible": 0},
        {"video_pricing_snapshot": "{}"},
        {"settlement": "deferred_home"}, {"settlement": "spend_lease"}, {"settlement": None},
        {"usage_type": "BYOK"}, {"usage_type": "credits"}, {"usage_type": None}, {"usage_type": 17},
        {"stage_d_prompt_tokens": None}, {"stage_d_max_output_tokens": -1}, {"stage_d_prompt_tokens": "100"},
        {"stage_d_max_output_tokens": True}, {"estimated_microdollars": None},
        {"heartbeat_seq": None}, {"heartbeat_seq": "0"}, {"heartbeat_seq": False}, {"heartbeat_seq": -1},
    ],
    ids=lambda broken: ",".join(f"{name}={value!r}" for name, value in broken.items()),
)
def test_a_member_of_another_type_or_value_keeps_the_hold(broken: dict[str, Any], stored: dict[str, Any]) -> None:
    assert _why({**stored, **broken}, age=GATEWAY_RESERVATION_TTL_SECONDS - 1) == rule.MALFORMED


@pytest.mark.parametrize(
    "missing",
    ["settlement", "usage_type", "native_batch_eligible", "stage_d_boot_kid", "pricing_snapshot",
     "stage_d_prompt_tokens", "stage_d_max_output_tokens", "estimated_microdollars", "heartbeat_seq"],
)
def test_a_member_the_rule_reads_that_is_missing_keeps_the_hold(missing: str, stored: dict[str, Any]) -> None:
    """The decoder would fill each of these with a default (a local
    settlement, credits, not a batch); the rule reads the stored row, where
    it is simply not there."""
    row = {name: value for name, value in stored.items() if name != missing}
    assert _why(row) == rule.MALFORMED


def _candidate(**overrides: Any) -> dict[str, Any]:
    rates = {"input_micro_per_million": 1, "output_micro_per_million": 2,
             "cached_input_micro_per_million": 0, "cache_creation_micro_per_million": 0}
    candidate: dict[str, Any] = {"endpoint_id": "anthropic/test", "price_history_version": 1, "rates": rates,
                                 "tiers": [{"max_prompt_tokens": None, "rates": dict(rates)}],
                                 "request_fee_micro": 0, "rounding": "half_up_per_million"}
    candidate.update(overrides)
    return candidate


def _document(**overrides: Any) -> str:
    document: dict[str, Any] = {"v": 1, "kind": "endpoint", "candidates": [_candidate()]}
    document.update(overrides)
    return json.dumps(document)


def test_the_fixture_snapshot_and_a_minimal_one_are_valid() -> None:
    assert rule.pricing_snapshot_valid(PRICING)
    assert rule.pricing_snapshot_valid(_document())
    assert rule.pricing_snapshot_valid(_document(candidates=[_candidate(tiers=[])]))


@pytest.mark.parametrize(
    "snapshot",
    [
        None, 7, {"v": 1}, "", "not-json", "[]", "{}", "null",
        _document(v=True), _document(v="1"), _document(v=2), _document(v=1.0), _document(kind="video"),
        _document(candidates=[]), _document(candidates=None), _document(candidates={}),
        _document(candidates=[None]), _document(candidates=[{}]), _document(candidates=[_candidate(), None]),
        _document(candidates=[_candidate(endpoint_id="")]), _document(candidates=[_candidate(endpoint_id=3)]),
        _document(candidates=[_candidate(price_history_version="1")]),
        _document(candidates=[_candidate(rates={})]), _document(candidates=[_candidate(rates=None)]),
        _document(candidates=[_candidate(rates={"input_micro_per_million": 1})]),
        _document(candidates=[_candidate(tiers=None)]), _document(candidates=[_candidate(tiers=[{}])]),
        _document(candidates=[_candidate(tiers=[{"max_prompt_tokens": "8", "rates": {}}])]),
        _document(candidates=[_candidate(request_fee_micro=-1)]), _document(candidates=[_candidate(rounding="")]),
    ],
    ids=lambda snapshot: repr(snapshot)[:60],
)
def test_a_snapshot_that_is_not_a_pricing_document_keeps_the_hold(snapshot: Any, stored: dict[str, Any]) -> None:
    assert not rule.pricing_snapshot_valid(snapshot)
    assert _why({**stored, "pricing_snapshot": snapshot}) == rule.MALFORMED


def test_a_terminal_a_missing_or_a_misbound_row_is_left_alone(stored: dict[str, Any]) -> None:
    assert _why(None) == rule.NOT_TYPED and _why({}) == rule.NOT_TYPED
    for settled in (True, None, 1, "false"):
        assert _why({**stored, "settled": settled}) == rule.TERMINAL
    assert _why({name: value for name, value in stored.items() if name != "settled"}) == rule.TERMINAL
    assert _why(stored, reservation="another") == rule.BINDING
    assert _why({name: value for name, value in stored.items() if name != "credit_reservation_id"}) == rule.BINDING


@pytest.mark.parametrize(
    ("started", "released"),
    [
        (CREATED - timedelta(hours=2), True),
        (CREATED - timedelta(microseconds=1), True),
        (CREATED, False),  # the same commit instant is not before
        (CREATED + timedelta(seconds=5), False),  # declared only after the row was made
        ((CREATED - timedelta(seconds=1)).isoformat(), True),
        (None, False), ("", False), ("not a time", False), (True, False),
    ],
)
def test_the_boots_declaring_run_must_have_begun_before_the_row_was_made(
    started: Any, released: bool, stored: dict[str, Any]
) -> None:
    """Membership rests on what was true when the row was made: the boot's
    unbroken declaring run began, by its commit time, before the
    reservation's commit. Both are the database's clock, so there is no
    allowance between them."""
    answer = _why(stored, since=lambda _kid: started)
    assert answer == (None if released else rule.BOOT_UNDECLARED)


def test_the_boot_is_read_last_and_only_for_a_row_that_passed_every_other_test(stored: dict[str, Any]) -> None:
    asked: list[str] = []

    def since(kid: str) -> None:
        asked.append(kid)

    assert _why(stored, since=since) == rule.BOOT_UNDECLARED and asked == [BOOT]
    for row, age in (({**stored, "settled": True}, AFTER), ({**stored, "stage_d_reason": "route"}, AFTER),
                     ({**stored, "pricing_snapshot": "not-json"}, AFTER), ({**stored, "heartbeat_seq": 1}, AFTER),
                     (stored, AFTER - 1)):
        assert _why(row, age=age, since=since) not in (None, rule.BOOT_UNDECLARED)
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


# -- the pass, on the Spanner fake, through the real reader --------------------


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
    since: Any = _since,
    limit: int = 100,
) -> authorize_mod.UnstartedReleaseResult:
    """A pass ``age`` seconds after CREATED, by the database's clock and the
    drain's alike."""
    db.now = CREATED + timedelta(seconds=age)
    return release_unstarted_holds_result(
        db,
        _ParamTypes,
        now=db.now,
        limit=limit,
        rule=UnstartedRelease(after_seconds=AFTER, log_only=log_only, declared_since=since),
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


def _transactions(db: FakeSpannerDatabase, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts the read-write transactions opened on the database."""
    opened: list[int] = []
    real = db.run_in_transaction

    def counting(function: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(1)
        return real(function, *args, **kwargs)

    monkeypatch.setattr(db, "run_in_transaction", counting)
    return opened


def test_the_hold_of_a_stream_that_never_started_is_given_back_at_the_deadline() -> None:
    db, authorization = _seed()
    _counters(db)

    result = _pass(db, age=AFTER + 1)

    assert (result.scanned, result.released, result.released_hold_micro) == (1, 1, HOLD)
    assert (result.would_release, result.errors, result.guarded, result.guard_lost) == (0, 0, 0, 0)
    assert result.skipped == () and result.leased == () and not result.unguarded and not result.truncated
    # The reaper's refund: the exact holds given back, nothing booked.
    assert _money(db) == (0, 0, 0, 0)
    reservation = db.reservations["reservation"]
    assert reservation["settled"] is True and reservation["actual_micro"] == 0
    row = db.gateway_authorizations[authorization.id]
    assert row["settled"] is True and row["finalization_outcome"] == "refunded"
    assert row["finalized_cost_microdollars"] == 0
    # The terminal time is the pass's own time, not a bound of the scan's window.
    assert row["terminal_at"] == CREATED + timedelta(seconds=AFTER + 1)
    assert db.generation_records == {}


def test_a_second_before_the_deadline_the_row_is_read_and_left() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=AFTER - 1, log_only=log_only)
        assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
        assert result.skipped == ((rule.YOUNG, 1),)
    assert _state(db) == before


def test_the_age_is_the_databases_not_the_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drain whose own clock runs ahead brings the row into the scan's
    window early; the row's age is its commit timestamp against the
    database's time at the scan, and by that it is too young."""
    db, _authorization_row = _seed()
    _counters(db)
    db.now = CREATED + timedelta(seconds=AFTER - 200)

    result = release_unstarted_holds_result(
        db, _ParamTypes, now=CREATED + timedelta(seconds=AFTER + 90), limit=10,
        rule=UnstartedRelease(after_seconds=AFTER, log_only=False, declared_since=_since),
    )

    assert (result.scanned, result.released) == (1, 0) and result.skipped == ((rule.YOUNG, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    # And a drain running behind still finds the row, a little later.
    db.now = CREATED + timedelta(seconds=AFTER + 200)
    late = release_unstarted_holds_result(
        db, _ParamTypes, now=CREATED + timedelta(seconds=AFTER - 90), limit=10,
        rule=UnstartedRelease(after_seconds=AFTER, log_only=False, declared_since=_since),
    )
    assert late.released == 1


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda payload: payload.pop("settlement"), rule.MALFORMED),
        (lambda payload: payload.pop("native_batch_eligible"), rule.MALFORMED),
        (lambda payload: payload.update(usage_type="Tokens"), rule.MALFORMED),
        (lambda payload: payload.update(usage_type=None), rule.MALFORMED),
        (lambda payload: payload.update(usage_type=17), rule.MALFORMED),
        (lambda payload: payload.update(settlement="deferred_home"), rule.MALFORMED),
        (lambda payload: payload.update(native_batch_eligible=True), rule.MALFORMED),
        (lambda payload: payload.pop("stage_d_prompt_tokens"), rule.MALFORMED),
        (lambda payload: payload.pop("stage_d_max_output_tokens"), rule.MALFORMED),
        (lambda payload: payload.update(video_pricing_snapshot="{}"), rule.MALFORMED),
        (lambda payload: payload.pop("stage_d_reason"), rule.OTHER_CLASS),
        (lambda payload: payload.update(stage_d_reason="not_streaming"), rule.OTHER_CLASS),
    ],
    ids=["no-settlement", "no-batch-flag", "usage-type-unknown", "usage-type-null", "usage-type-17",
         "other-settlement", "batch", "no-prompt-bound", "no-output-bound", "video", "no-reason", "other-reason"],
)
def test_a_stored_row_the_decoder_would_have_repaired_keeps_its_hold(edit: Any, reason: str) -> None:
    """Through the real reader and the real pass: the decoder fills a
    missing settlement with local, a missing batch flag with False and any
    usage type with credits, and each of those released a hold. The rule
    reads the row as stored."""
    db, authorization = _seed()
    _counters(db)
    _edit_payload(db, edit)
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=AFTER + 1, log_only=log_only)
        assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
        assert result.skipped == ((reason, 1),)
    assert _state(db) == before
    # The reaper still takes it at its expiry, as before.
    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1))
    assert reaped.count == 1 and db.gateway_authorizations[authorization.id]["settled"] is True


_EMPTY_CANDIDATE = '{"v":1,"kind":"endpoint","candidates":[{}]}'


@pytest.mark.parametrize(
    "edit",
    [
        lambda payload: payload.pop("settlement"),
        lambda payload: payload.pop("native_batch_eligible"),
        lambda payload: payload.update(usage_type=17),
        lambda payload: payload.pop("stage_d_max_output_tokens"),
        lambda payload: payload.update(pricing_snapshot=_EMPTY_CANDIDATE),
    ],
    ids=["no-settlement", "no-batch-flag", "usage-type-17", "no-output-bound", "empty-candidate"],
)
def test_the_release_transaction_judges_the_stored_row_on_its_own_read(
    edit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock-free read is advisory, here too: a row that read found
    releasable and that is not as authorize writes one by the time the
    transaction reads it is left by the transaction, which judges the stored
    row itself and not the decoder's repair of it."""
    db, authorization = _seed()
    _counters(db)
    real = authorize_mod._finalize_reaped_reservation_atomic

    def change_then_finalize(*args: Any, **kwargs: Any) -> Any:
        _edit_payload(db, edit)
        record = db.gateway_authorizations[authorization.id]
        if json.loads(record["payload"]).get("pricing_snapshot") == _EMPTY_CANDIDATE:
            record["pricing_snapshot"] = _EMPTY_CANDIDATE  # the typed column, which is read over the payload
        return real(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", change_then_finalize)

    result = _pass(db, age=AFTER + 1)

    assert (result.scanned, result.released, result.errors) == (1, 0, 0)
    assert result.skipped == ((rule.MALFORMED, 1),)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False
    assert db.gateway_authorizations[authorization.id]["settled"] is False


def test_a_stored_row_bound_to_no_reservation_is_left_to_the_reapers_own_guard() -> None:
    db, _authorization_row = _seed()
    _counters(db)
    _edit_payload(db, lambda payload: payload.pop("credit_reservation_id"))
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=AFTER + 1, log_only=log_only)
        assert (result.scanned, result.released, result.would_release, result.errors) == (1, 0, 0, 0)
        assert result.skipped == ((rule.BINDING, 1),)
    assert _state(db) == before
    # As before the rule: the reaper will not release a row whose binding is lost.
    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1))
    assert (reaped.count, reaped.guard_lost) == (0, 1) and _state(db) == before


def test_a_stored_row_the_decoder_refuses_is_the_passes_error_and_keeps_its_hold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A payload without a member the decoder requires does not decode at
    all. The pass counts an error, which the gate for turning the rule on
    reads, and nothing is released."""
    db, _authorization_row = _seed()
    _counters(db)
    _edit_payload(db, lambda payload: payload.pop("usage_type"))
    before = _state(db)

    for log_only in (True, False):
        with caplog.at_level(logging.WARNING, logger=authorize_mod.log.name):
            result = _pass(db, age=AFTER + 1, log_only=log_only)
        assert (result.scanned, result.released, result.would_release, result.errors) == (1, 0, 0, 1)
        assert result.skipped == ()
    assert _state(db) == before
    assert {record.getMessage() for record in caplog.records} == {
        "gateway early release failed error_class=TypeError; continuing the pass"
    }


@pytest.mark.parametrize(
    "snapshot",
    ['{"v":true,"kind":"endpoint","candidates":[null]}', '{"v":1,"kind":"endpoint","candidates":[{}]}',
     "not-json", '{"v":1,"kind":"endpoint","candidates":[]}'],
)
def test_a_stored_snapshot_that_is_not_a_pricing_document_keeps_the_hold(snapshot: str) -> None:
    db, authorization = _seed()
    _counters(db)
    db.gateway_authorizations[authorization.id]["pricing_snapshot"] = snapshot
    _edit_payload(db, lambda payload: payload.update(pricing_snapshot=snapshot))
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=AFTER + 1, log_only=log_only)
        assert (result.released, result.would_release) == (0, 0) and result.skipped == ((rule.MALFORMED, 1),)
    assert _state(db) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage_d_reason": "not_streaming", "pricing_snapshot": None, "heartbeat_seq": None},
        {"stage_d_reason": "route"},
        {"stage_d_reason": "abliterate_estimated_usage"},
    ],
    ids=["not-a-stream", "another-route", "estimated-usage"],
)
def test_a_request_of_another_class_keeps_its_hold_until_the_reaper(overrides: dict[str, Any]) -> None:
    db, authorization = _seed(**overrides)
    _counters(db)
    before = _state(db)

    for log_only in (True, False):
        result = _pass(db, age=AFTER + 1, log_only=log_only)
        assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
        assert result.skipped == ((rule.OTHER_CLASS, 1),)
    assert _state(db) == before
    reaped = reap_expired_reservations_result(db, _ParamTypes, now=CREATED + TTL + timedelta(seconds=1))
    assert reaped.count == 1 and db.gateway_authorizations[authorization.id]["settled"] is True


def test_log_mode_and_a_row_the_rule_leaves_open_no_read_write_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every candidate is read on a read-only snapshot first. Log mode stops
    there, and a releasing pass opens its transaction only for a row that
    read found releasable: no lock for anything else."""
    db, _releasable = _seed()
    _seed(db, suffix="-other", stage_d_reason="route")
    _counters(db)
    before = _state(db)
    opened = _transactions(db, monkeypatch)
    snapshots = len(db.snapshot_calls)

    logged = _pass(db, age=AFTER + 1, log_only=True)

    assert (logged.scanned, logged.would_release, logged.would_release_hold_micro) == (2, 1, HOLD)
    assert (logged.released, logged.leased) == (0, ()) and logged.skipped == ((rule.OTHER_CLASS, 1),)
    assert opened == [] and _state(db) == before
    # One multi-use, read-only snapshot for each candidate.
    assert sum(bool(call.get("multi_use")) for call in db.snapshot_calls[snapshots:]) == 2

    released = _pass(db, age=AFTER + 1)

    assert released.released == 1 and opened == [1]
    assert db.reservations["reservation-other"]["settled"] is False


def test_a_boots_record_is_read_once_a_pass_and_outside_the_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def since(kid: str) -> datetime:
        assert not in_transaction, "the boot was read inside a transaction"
        asked.append(kid)
        return SINCE

    result = _pass(db, age=AFTER + 1, since=since)

    assert result.released == 2 and asked == [BOOT]
    assert _money(db) == (0, 0, 0, 0)


def test_a_first_heartbeat_before_the_release_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The race, heartbeat first: it lands after the lock-free read found
    the row releasable and before the release's transaction, whose own
    strong read then finds a started row."""
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
    row = db.gateway_authorizations[authorization.id]
    assert row["settled"] is False and row["heartbeat_seq"] == 1 and row["started_at"] is not None
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
def test_a_terminal_before_the_release_wins_and_the_release_does_nothing(
    success: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, authorization = _seed()
    _counters(db)
    terminal_row = replace(authorization)
    terminal_row.record_finalization(
        success=success, actual_microdollars=120 if success else 0,
        selected_usage_type=UsageType.CREDITS, generation=None,
    )

    def terminal() -> None:
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

    # The terminal lands after the lock-free read and before the transaction.
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic

    def terminal_then_finalize(*args: Any, **kwargs: Any) -> Any:
        terminal()
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", terminal_then_finalize)

    result = _pass(db, age=AFTER + 1)

    assert result.released == 0 and result.skipped == (("reservation", 1),)
    assert _money(db) == (0, 120 if success else 0, 0, 120 if success else 0)
    # The scan no longer returns a settled reservation.
    monkeypatch.undo()
    assert _pass(db, age=AFTER + 2).scanned == 0


@pytest.mark.parametrize("status", ["pending", "dead"])
@pytest.mark.parametrize("log_only", [True, False], ids=["log", "on"])
def test_a_pending_settle_intent_freezes_the_hold(status: str, log_only: bool) -> None:
    """A settle the control plane has made durable and not yet applied: the
    outbox guard, on the lock-free read."""
    db, authorization = _seed()
    _counters(db)
    db.settle_outbox[(authorization.id, "settle")] = {
        "authorization_id": authorization.id, "intent_kind": "settle", "status": status,
    }
    before = _state(db)

    result = _pass(db, age=AFTER + 1, log_only=log_only)

    assert (result.scanned, result.guarded, result.released, result.would_release) == (1, 1, 0, 0)
    assert _state(db) == before


def test_a_settle_intent_made_durable_after_the_lock_free_read_still_freezes_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock-free read is advisory: an intent that lands after it is
    found by the release transaction's own read."""
    db, authorization = _seed()
    _counters(db)
    real = authorize_mod._finalize_reaped_reservation_atomic

    def intent_then_finalize(*args: Any, **kwargs: Any) -> Any:
        db.settle_outbox[(authorization.id, "settle")] = {
            "authorization_id": authorization.id, "intent_kind": "settle", "status": "pending",
        }
        return real(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", intent_then_finalize)

    result = _pass(db, age=AFTER + 1)

    assert (result.guarded, result.released) == (1, 0)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False


def test_without_the_settle_outbox_the_pass_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _authorization_row = _seed()
    _counters(db)
    before = _state(db)
    monkeypatch.setattr(authorize_mod, "_outbox_table_available", lambda _database, _types: False)
    reads = db.snapshot_execute_sql_calls

    result = _pass(db, age=AFTER + 1)

    assert result.unguarded is True and (result.scanned, result.released) == (0, 0)
    assert _state(db) == before and db.snapshot_execute_sql_calls == reads


def test_one_rows_failure_is_its_own_and_the_pass_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db, _first = _seed()
    _seed(db, suffix="-2", created=CREATED + timedelta(seconds=1))
    _counters(db, reserved=2 * HOLD)
    real_finalize = authorize_mod._finalize_reaped_reservation_atomic

    def fail_the_first(*args: Any, **kwargs: Any) -> Any:
        if kwargs["reservation_id"] == "reservation":
            raise RuntimeError("storage unavailable for reservation and workspace")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "_finalize_reaped_reservation_atomic", fail_the_first)

    with caplog.at_level(logging.WARNING, logger=authorize_mod.log.name):
        result = _pass(db, age=AFTER + 2)

    assert (result.scanned, result.released, result.errors) == (2, 1, 1)
    assert _money(db) == (HOLD, 0, HOLD, 0)
    assert db.reservations["reservation"]["settled"] is False
    # The line names the error's class: no identifier, no text of the error.
    lines = [record.getMessage() for record in caplog.records if "early release failed" in record.getMessage()]
    assert lines == ["gateway early release failed error_class=RuntimeError; continuing the pass"]
    assert all(record.exc_info is None for record in caplog.records)


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


# -- the scan: bounded input, and progress whatever was left --------------------


def _scan_params(db: FakeSpannerDatabase, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = _FakeSnapshot.execute_sql

    def record(self: Any, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql == authorize_mod._UNSTARTED_SCAN_SQL:
            seen.append(dict(kwargs["params"]))
        return real(self, sql, *args, **kwargs)

    monkeypatch.setattr(_FakeSnapshot, "execute_sql", record)
    return seen


def test_the_scan_names_its_index_bounds_its_input_and_selects_no_class() -> None:
    sql = authorize_mod._UNSTARTED_SCAN_SQL
    assert "FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_expiry} " in sql
    assert "WHERE settled=false AND expires_at >= @from_expiry AND expires_at < @now " in sql
    # The limit is on the index range itself: nothing is joined before it.
    assert sql.endswith("ORDER BY expires_at LIMIT @limit") and "JOIN" not in sql and "EXISTS" not in sql
    # The rule is Python's alone: the statement knows nothing of a class.
    assert "stage_d" not in sql and "payload" not in sql and "tr_gateway_authorization" not in sql
    assert "created_at, CURRENT_TIMESTAMP() AS read_at" in sql


def test_the_scans_window_is_the_rows_that_crossed_the_deadline_in_the_last_three_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _authorization_row = _seed()
    _counters(db)
    seen = _scan_params(db, monkeypatch)
    slack, window = authorize_mod._UNSTARTED_SCAN_SLACK_SECONDS, authorize_mod._UNSTARTED_SCAN_WINDOW_SECONDS
    assert (slack, window) == (300, 900)

    _pass(db, age=AFTER + 1, log_only=True, limit=7)

    now = CREATED + timedelta(seconds=AFTER + 1)
    assert seen == [{
        "now": now + TTL - timedelta(seconds=AFTER) + timedelta(seconds=slack),
        "from_expiry": now + TTL - timedelta(seconds=AFTER) - timedelta(seconds=window + slack),
        "limit": 7,
    }]
    # In the window from the slack before the deadline until the window and
    # slack after it; then the row is the reaper's alone.
    assert _pass(db, age=AFTER - slack - 1, log_only=True).scanned == 0
    assert _pass(db, age=AFTER - slack + 1, log_only=True).skipped == ((rule.YOUNG, 1),)
    assert _pass(db, age=AFTER + window + slack - 1, log_only=True).would_release == 1
    assert _pass(db, age=AFTER + window + slack + 1, log_only=True).scanned == 0


def test_rows_the_rule_left_in_earlier_passes_never_hide_a_row_that_has_just_crossed() -> None:
    """Progress does not depend on what was refused. Any number of rows the
    rule leaves (here, of a boot with no declaration to show), from more
    than a window ago, are out of the scan; a new releasable row is read on
    its first pass, with a budget far smaller than their number."""
    db, _blocker = _seed(suffix="-b0", stage_d_boot_kid="boot-undeclared", created=CREATED - timedelta(hours=1))
    for index in range(1, 40):
        _seed(db, suffix=f"-b{index}", stage_d_boot_kid="boot-undeclared",
              created=CREATED - timedelta(hours=1) + timedelta(seconds=index))
    _seed(db)
    _counters(db)

    def since(kid: str) -> datetime | None:
        return SINCE if kid == BOOT else None

    for log_only, expected in ((True, (1, 0, 1)), (False, (1, 1, 0)), (False, (0, 0, 0))):
        result = _pass(db, age=AFTER + 1, since=since, log_only=log_only, limit=2)
        assert (result.scanned, result.released, result.would_release) == expected
        assert result.truncated is False
    assert db.reservations["reservation"]["settled"] is True


def test_more_rows_crossing_in_the_window_than_a_pass_examines_is_told_and_the_rest_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is on the rows examined, of any class. When more cross the
    deadline than a pass's budget, the pass says ``truncated``; the rows it
    did not reach are read by a later pass once the earlier ones have left
    the window, and a row that many others crossed just before, in its own
    five minutes, is the reaper's."""
    db, _first = _seed(suffix="-b0", stage_d_reason="route", created=CREATED - timedelta(seconds=600))
    for index in range(1, 6):
        _seed(db, suffix=f"-b{index}", stage_d_reason="route",
              created=CREATED - timedelta(seconds=600) + timedelta(seconds=index))
    _seed(db)
    _counters(db)
    opened = _transactions(db, monkeypatch)
    window, slack = authorize_mod._UNSTARTED_SCAN_WINDOW_SECONDS, authorize_mod._UNSTARTED_SCAN_SLACK_SECONDS

    first = _pass(db, age=AFTER + 1, limit=3)
    # Three rows examined, the oldest in the window; none of them releasable.
    assert (first.scanned, first.released, first.truncated) == (3, 0, True)
    assert first.skipped == ((rule.OTHER_CLASS, 3),) and opened == []

    # Once the six earlier rows have left the window, the row is reached.
    later = _pass(db, age=window + slack + 1 + AFTER - 600 + 6, limit=3)
    assert (later.scanned, later.released, later.truncated) == (1, 1, False)
    assert opened == [1]


# -- the boot's declaration start, by commit time -------------------------------


def _registration(*, declares: bool, registered_at: str = "2020-01-01T00:00:00Z", since: str | None = None) -> GatewayBoot:
    return GatewayBoot(
        kid=BOOT, jwk={"kty": "OKP", "crv": "Ed25519", "x": "x"}, approved=True, verified=True,
        image_digest="sha256:" + "aa" * 32, attestation_kind="gcp-cs-jwt", registered_at=registered_at,
        declares_usage_heartbeat=declares, usage_heartbeat_declared_since=since,
    )


def test_a_declaring_runs_start_is_its_first_writes_commit_time_and_no_callers_clock() -> None:
    first = boot_with_heartbeat_history(None, _registration(declares=True), existing_written_at=None)
    assert first.usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    # The next declaring registration materialises it: the first row's commit time.
    second = boot_with_heartbeat_history(first, _registration(declares=True), existing_written_at="T1-commit")
    assert second.usage_heartbeat_declared_since == "T1-commit"
    third = boot_with_heartbeat_history(second, _registration(declares=True), existing_written_at="T2-commit")
    assert third.usage_heartbeat_declared_since == "T1-commit"
    withdrawn = boot_with_heartbeat_history(third, _registration(declares=False), existing_written_at="T3-commit")
    assert withdrawn.usage_heartbeat_declared_since is None
    restored = boot_with_heartbeat_history(withdrawn, _registration(declares=True), existing_written_at="T4-commit")
    assert restored.usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    # A store that cannot say when it wrote the previous row starts the run again.
    assert boot_with_heartbeat_history(first, _registration(declares=True), existing_written_at=None) \
        .usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    # Whatever a caller sends, its own time or a start, is never taken.
    forged = _registration(declares=True, registered_at="1999-01-01T00:00:00Z", since="1999-01-01T00:00:00Z")
    assert boot_with_heartbeat_history(None, forged, existing_written_at=None) \
        .usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    stale = _registration(declares=False, since="1999-01-01T00:00:00Z")
    assert boot_with_heartbeat_history(stale, _registration(declares=True), existing_written_at="T5-commit") \
        .usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    assert boot_with_heartbeat_history(None, _registration(declares=False, since="x"), existing_written_at=None) \
        .usage_heartbeat_declared_since is None


def _boot_store() -> tuple[Any, FakeSpannerDatabase]:
    store, db = make_fake_store(request_record_write_mode="typed")
    configure_store(store)
    return store, db


def _register(store: Any, db: FakeSpannerDatabase, *, declares: bool, at: datetime, stamped: str = "2020-01-01T00:00:00Z") -> None:
    """A registration that commits at ``at`` by the database's clock, with
    whatever application-clock time its caller stamped on it."""
    db.now = at
    store.observe_gateway_boot(_registration(declares=declares, registered_at=stamped))


def test_the_store_resolves_the_start_from_the_boot_rows_commit_time() -> None:
    store, db = _boot_store()
    t1, t2, t3 = (CREATED - timedelta(hours=3), CREATED - timedelta(hours=2), CREATED - timedelta(hours=1))
    assert store.gateway_boot_declaring_since(BOOT) is None

    _register(store, db, declares=True, at=t1)
    assert store.get_gateway_boot(BOOT).usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    assert store.gateway_boot_declaring_since(BOOT) == t1

    # Declaring again keeps the start: materialised from the row it replaces.
    _register(store, db, declares=True, at=t2)
    assert store.get_gateway_boot(BOOT).usage_heartbeat_declared_since == t1.isoformat().replace("+00:00", "Z")
    assert store.gateway_boot_declaring_since(BOOT) == t1
    _register(store, db, declares=True, at=t3)
    assert store.gateway_boot_declaring_since(BOOT) == t1

    _register(store, db, declares=False, at=CREATED - timedelta(minutes=30))
    assert store.gateway_boot_declaring_since(BOOT) is None
    _register(store, db, declares=True, at=CREATED - timedelta(minutes=10))
    assert store.gateway_boot_declaring_since(BOOT) == CREATED - timedelta(minutes=10)


def test_a_delayed_declaring_registration_cannot_start_before_its_own_commit() -> None:
    """The interleaving a caller's clock allowed: a declaring registration is
    prepared, stamped a minute in the past, and stalls; a withdrawing one
    commits; a stream is authorized, Stage D ok, while the boot declares
    nothing, and runs with no heartbeat; the delayed registration then
    commits. Its run begins at its own commit, after the row: the row is not
    of the class, and its hold is kept."""
    store, db = _boot_store()
    _register(store, db, declares=True, at=CREATED - timedelta(hours=1))
    _register(store, db, declares=False, at=CREATED - timedelta(seconds=3))
    _seed(db, created=CREATED)
    _counters(db)
    before = _state(db)
    _register(store, db, declares=True, at=CREATED + timedelta(seconds=5), stamped="2026-11-21T11:59:00Z")
    assert store.gateway_boot_declaring_since(BOOT) == CREATED + timedelta(seconds=5)

    for age in (AFTER + 1, AFTER + 600):
        for log_only in (True, False):
            db.now = CREATED + timedelta(seconds=age)
            result = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=log_only)
            assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
            assert result.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _state(db) == before

    # A row made after that commit is of the class.
    _seed(db, suffix="-later", created=CREATED + timedelta(seconds=6))
    db.now = CREATED + timedelta(seconds=AFTER + 7)
    released = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=False)
    assert released.released == 1 and db.reservations["reservation-later"]["settled"] is True
    assert db.reservations["reservation"]["settled"] is False


def test_a_retried_registration_takes_the_commit_time_of_the_attempt_that_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declaring registration whose first attempt aborts and whose retry
    commits later: the run begins at the retry's commit. A stream authorized
    between the two attempts, while the stored boot declared nothing, is not
    of the class. Nothing an aborted attempt read or wrote is carried: the
    start is the stored row's own commit timestamp."""
    store, db = _boot_store()
    _register(store, db, declares=False, at=CREATED - timedelta(seconds=30))
    attempts: list[datetime] = []
    real_write = type(store)._write_entity_tx

    def abort_the_first_attempt(self: Any, transaction: Any, kind: str, entity_id: str, value: Any) -> Any:
        attempts.append(db.current_timestamp())
        result = real_write(self, transaction, kind, entity_id, value)
        if len(attempts) == 1:
            # The attempt is thrown away after its write was staged; a stream
            # is authorized, and only then does the retry run and commit.
            _seed(db, created=CREATED)
            db.now = CREATED + timedelta(seconds=5)
            raise FakeAborted()
        return result

    monkeypatch.setattr(type(store), "_write_entity_tx", abort_the_first_attempt)
    db.now = CREATED - timedelta(seconds=5)
    store.observe_gateway_boot(_registration(declares=True))
    monkeypatch.undo()

    assert attempts == [CREATED - timedelta(seconds=5), CREATED + timedelta(seconds=5)]
    assert store.get_gateway_boot(BOOT).usage_heartbeat_declared_since == BOOT_DECLARED_AT_ITS_WRITE
    assert store.gateway_boot_declaring_since(BOOT) == CREATED + timedelta(seconds=5)
    _counters(db)
    before = _state(db)
    db.now = CREATED + timedelta(seconds=AFTER + 1)
    for log_only in (True, False):
        result = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=log_only)
        assert (result.scanned, result.released, result.would_release) == (1, 0, 0)
        assert result.skipped == ((rule.BOOT_UNDECLARED, 1),)
    assert _state(db) == before


def test_a_retried_registration_that_carries_a_start_reads_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The materialising registration, retried: each attempt reads the row
    it replaces, and its commit timestamp, in its own transaction. Between
    the attempts the declaration is withdrawn and restored; the retry
    carries the restored run's start, not the one its first attempt read."""
    store, db = _boot_store()
    first, restored = CREATED - timedelta(hours=3), CREATED - timedelta(minutes=10)
    _register(store, db, declares=True, at=first)
    real_write = type(store)._write_entity_tx
    staged: list[str | None] = []

    def abort_the_first_attempt(self: Any, transaction: Any, kind: str, entity_id: str, value: Any) -> Any:
        staged.append(value.usage_heartbeat_declared_since)
        result = real_write(self, transaction, kind, entity_id, value)
        if len(staged) == 1:
            monkeypatch.undo()
            _register(store, db, declares=False, at=CREATED - timedelta(minutes=20))
            _register(store, db, declares=True, at=restored)
            monkeypatch.setattr(type(store), "_write_entity_tx", abort_the_first_attempt)
            db.now = CREATED - timedelta(minutes=5)
            raise FakeAborted()
        return result

    monkeypatch.setattr(type(store), "_write_entity_tx", abort_the_first_attempt)
    db.now = CREATED - timedelta(hours=1)
    store.observe_gateway_boot(_registration(declares=True))
    monkeypatch.undo()

    text = lambda moment: moment.isoformat().replace("+00:00", "Z")  # noqa: E731
    assert staged == [text(first), text(restored)]
    assert store.gateway_boot_declaring_since(BOOT) == restored


@pytest.mark.parametrize("declares", [True, False])
def test_the_store_releases_only_for_a_boot_whose_run_began_before_the_row(declares: bool) -> None:
    store, db = _boot_store()
    _register(store, db, declares=declares, at=CREATED - timedelta(hours=1))
    _seed(db)
    _counters(db)
    db.now = CREATED + timedelta(seconds=AFTER + 1)

    result = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=False)

    assert result.released == int(declares)
    assert result.skipped == (() if declares else ((rule.BOOT_UNDECLARED, 1),))
    assert _money(db) == ((0, 0, 0, 0) if declares else (HOLD, 0, HOLD, 0))


def test_a_boot_record_that_is_gone_unreadable_or_unstamped_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db = _boot_store()
    _seed(db)
    _counters(db)
    db.now = CREATED + timedelta(seconds=AFTER + 1)

    gone = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=False)
    assert gone.released == 0 and gone.skipped == ((rule.BOOT_UNDECLARED, 1),)

    # A record from before the field existed: it declares, and says no more.
    store._write_entity("spend_lease_boot", BOOT, _registration(declares=True))
    assert store.get_gateway_boot(BOOT).declares_usage_heartbeat is True
    unstamped = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=False)
    assert unstamped.released == 0 and unstamped.skipped == ((rule.BOOT_UNDECLARED, 1),)

    def unreadable(_self: Any, _kid: str) -> Any:
        raise RuntimeError("storage unavailable")

    # A read that fails keeps the hold too, as the row's error: the pass
    # counts it, so it is not taken for a pass that found nothing to release.
    monkeypatch.setattr(type(store), "gateway_boot_declaring_since", unreadable)
    for log_only in (True, False):
        unread = store.release_unstarted_holds_result(now=db.now, after_seconds=AFTER, log_only=log_only)
        assert (unread.released, unread.would_release, unread.errors, unread.skipped) == (0, 0, 1, ())
    assert _money(db) == (HOLD, 0, HOLD, 0)


# -- the drain -------------------------------------------------------------------


def _store(**overrides: Any) -> tuple[Any, FakeSpannerDatabase, GatewayAuthorization]:
    """A store with a boot that has declared since an hour before the row."""
    store, db = _boot_store()
    _register(store, db, declares=True, at=CREATED - timedelta(hours=1))
    _db, authorization = _seed(db, **overrides)
    _counters(db, reserved=int(overrides.get("hold", HOLD)))
    return store, db, authorization


def _drain_pass(db: FakeSpannerDatabase, *, mode: str, age: int = AFTER + 1, limit: int = 200) -> Any:
    db.now = CREATED + timedelta(seconds=age)
    return drain.release_unstarted_holds(db.now, mode=mode, after_seconds=AFTER, limit=limit)


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


def _summary(mode: str, **fields: Any) -> str:
    values: dict[str, Any] = {
        "scanned": 1, "released": 0, "released_hold_micro": 0, "would_release": 0,
        "would_release_hold_micro": 0, "would_release_leased": 0, "guarded": 0, "guard_lost": 0, "errors": 0,
        "unguarded": "false", "truncated": "false", "skipped": "none", "lease_refunds": 0,
        "lease_refund_failures": 0,
    }
    values.update(fields)
    return f"gateway.unstarted_release mode={mode} rule=unstarted_stream after_seconds=900 " + " ".join(
        f"{name}={value}" for name, value in values.items()
    )


def test_the_pass_in_on_mode_releases_counts_and_reports(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()

    result = _drain_pass(db, mode="on")

    assert result is not None and result.released == 1
    assert _money(db) == (0, 0, 0, 0)
    assert drain_lines.messages() == [
        _summary("on", released=1, released_hold_micro=300),
        "metric=gateway_hold_released_unstarted_stream_total value=1",
    ]
    counted = drain_lines.records[1]
    assert counted.levelno == logging.WARNING
    assert counted.metric == drain._RELEASED_UNSTARTED_METRIC  # type: ignore[attr-defined]


def test_the_pass_in_log_mode_releases_nothing_and_counts_no_release(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store()
    before = _state(db)

    result = _drain_pass(db, mode="log")

    assert result is not None and (result.released, result.would_release) == (0, 1)
    assert _state(db) == before
    assert drain_lines.messages() == [_summary("log", would_release=1, would_release_hold_micro=300)]


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

    assert _drain_pass(db, mode=mode, age=GATEWAY_RESERVATION_TTL_SECONDS) is None
    assert _state(db) == before and db.snapshot_execute_sql_calls == reads
    assert drain_lines.messages() == []


def test_a_failing_pass_is_told_by_its_class_and_never_raised(
    drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, db, _authorization_row = _store()
    before = _state(db)

    def broken(_self: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage unavailable for reservation and workspace")

    monkeypatch.setattr(type(store), "release_unstarted_holds_result", broken)

    assert _drain_pass(db, mode="on") is None
    assert _state(db) == before
    assert drain_lines.messages() == ["gateway.unstarted_release failed mode=on error_class=RuntimeError"]
    assert all(record.exc_info is None for record in drain_lines.records)


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

    result = _drain_pass(db, mode="on")

    assert result is not None and result.leased == (authorization.id,)
    # After the row is terminal, with the refund's own money: no charge.
    assert db.gateway_authorizations[authorization.id]["settled"] is True
    assert client.refunds == [{"envelope": "sealed-envelope", "money": {"charge": 0, "estimate": HOLD}}]
    assert drain_lines.messages()[0] == _summary("on", released=1, lease_refunds=1)


def test_log_mode_sends_a_lease_nothing(drain_lines: _Lines, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_, db, _authorization_row = _store(hold=0, fastpath_envelope="sealed-envelope")
    client = _FrontDoor()
    _front_door(monkeypatch, client)
    before = _state(db)

    result = _drain_pass(db, mode="log")

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

    result = _drain_pass(db, mode="on")

    assert result is not None and result.released == 1
    assert db.gateway_authorizations[authorization.id]["finalization_outcome"] == "refunded"
    summary = next(line for line in drain_lines.messages() if line.startswith("gateway.unstarted_release mode=on"))
    assert summary == _summary("on", released=1, lease_refund_failures=1)
    assert all(record.exc_info is None for record in drain_lines.records)
    refused = _heartbeat(db, at=CREATED + timedelta(seconds=AFTER + 2))
    assert refused.accepted is False and refused.reason == "already_terminal"


def test_a_pass_whose_scan_filled_its_limit_says_so(drain_lines: _Lines) -> None:
    _store_, db, _authorization_row = _store(stage_d_reason="route")
    _seed(db, suffix="-2", stage_d_reason="route", created=CREATED + timedelta(seconds=1))

    result = _drain_pass(db, mode="on", age=AFTER + 2, limit=2)

    assert result is not None and (result.scanned, result.released, result.truncated) == (2, 0, True)
    assert drain_lines.messages() == [
        _summary("on", scanned=2, truncated="true", skipped="other_class:2"),
        "gateway.unstarted_release scan truncated scanned=2 limit=2",
    ]


def test_each_hold_the_early_rule_releases_is_counted_under_its_own_rule(drain_lines: _Lines) -> None:
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 2)
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 0)

    assert drain_lines.messages() == ["metric=gateway_hold_released_unstarted_stream_total value=1"] * 2
    assert {record.levelno for record in drain_lines.records} == {logging.WARNING}
