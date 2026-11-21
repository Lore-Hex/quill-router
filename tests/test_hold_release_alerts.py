"""The lines a hold's release writes, held to the metrics and policies that
watch them (infra/hold_release_alerts.tf), and the early rule's evidence
lines, as the heartbeat and settle routes write them
(docs/design/fast-admission-streams.md section 13).

Also here: the drain with the rule off against the frozen drain it was
before the rule, statement for statement.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from tests.conformance import spanner_schema_source as schema
from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
from tests.settle_drain_trace import normalize_legacy_drain_trace
from tests.test_async_settle_drain_oracle import namespace
from tests.test_async_settle_handler import (
    env,  # noqa: F401 - the shared fake-store fixture
    prepare,
    row_for,
)
from tests.test_stage_d_heartbeat import (
    _boot,
    _boot_auth_header,
    _gateway_heartbeat_store,
    _literal,
    _request,
)
from tests.test_unstarted_hold_release import (
    AFTER,
    BOOT,
    CREATED,
    DUE,
    HOLD,
    PRICING,
    SINCE,
    _authorization,
    _counters,
    _money,
    _seed,
    _store,
)
from trusted_router import storage_gcp_settle_outbox as outbox_storage
from trusted_router import unstarted_release as rule
from trusted_router.config import Settings
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayHeartbeatRequest, GatewaySettleRequest
from trusted_router.services import settle_drain_observability, settle_outbox_apply
from trusted_router.services import settle_outbox_drain as drain

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "hold_release_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbook.md"
DRAIN_LOGGER = "trusted_router.services.settle_outbox_drain"
RULE_LOGGER = "trusted_router.unstarted_release"


def _payloads() -> dict[str, str]:
    """Each kind's textPayload substring, as the Terraform carries it."""
    tf = TERRAFORM.read_text(encoding="utf-8")
    found = re.findall(r'\n    (\w+) = \{\n      payload  = "([^"]+)"', tf)
    assert [kind for kind, _payload in found] == ["expired", "unstarted_stream", "contradiction"], found
    return dict(found)


class _Rendered(logging.Handler):
    """A logger's lines, as the console handler renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(_ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def rendered() -> Any:
    handler = _Rendered()
    loggers = [logging.getLogger(DRAIN_LOGGER), logging.getLogger(RULE_LOGGER)]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)
            logger.removeHandler(handler)


# -- the release counters -----------------------------------------------------


def test_each_filter_matches_its_own_rules_line_and_no_other(rendered: _Rendered) -> None:
    payloads = _payloads()
    assert drain.logger.name == DRAIN_LOGGER and rule.log.name == RULE_LOGGER
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 1)
    rule.observe_settle(
        _authorization(), success=True, mode="log", declared_since=lambda _kid: SINCE,
        now=CREATED + dt.timedelta(seconds=5),
    )
    expired, unstarted, contradiction = rendered.lines
    # The whole of a release's line: the rule's counter, and no customer.
    assert expired == payloads["expired"] + "value=1"
    assert unstarted == payloads["unstarted_stream"] + "value=1"
    assert contradiction.startswith(payloads["contradiction"] + "value=1 kind=charge_without_heartbeat ")
    for kind, line in (("expired", expired), ("unstarted_stream", unstarted), ("contradiction", contradiction)):
        assert [payload in line for payload in payloads.values()] == [kind == other for other in payloads], line
    # A longer name, another level or another logger is not it.
    assert payloads["expired"] not in f"WARNING {DRAIN_LOGGER} metric=gateway_hold_released_expired_total_x value=1"
    assert payloads["expired"] not in f"INFO {DRAIN_LOGGER} metric=gateway_hold_released_expired_total value=1"
    assert payloads["expired"] not in "WARNING trusted_router.other metric=gateway_hold_released_expired_total value=1"
    # The reaper's own summary and the early rule's are not counted as a release.
    assert not any(payload in f"INFO {DRAIN_LOGGER} gateway.reaped count=3 released_hold_micro=9" for payload in payloads.values())
    assert not any(payload in f"INFO {DRAIN_LOGGER} gateway.unstarted_release mode=log" for payload in payloads.values())


def test_the_settle_drain_workers_boundary_keeps_each_counter(rendered: _Rendered) -> None:
    """The worker applies settles from their frozen intents, so the
    contradiction line can be written there too: its counter's name is kept,
    and nothing else of any of the three lines."""
    payloads = _payloads()
    assert {
        drain._RELEASED_EXPIRED_METRIC, drain._RELEASED_UNSTARTED_METRIC, rule.CONTRADICTION_METRIC,
    } <= settle_drain_observability._COUNTED_METRICS
    remove = settle_drain_observability.install_logging()
    try:
        drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
        drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 1)
        rule.observe_settle(_authorization(), success=True, mode="log", declared_since=lambda _kid: SINCE)
    finally:
        remove()
    tail = f"logger={DRAIN_LOGGER} level=WARNING exception_class=none"
    assert rendered.lines == [
        payloads["expired"] + tail,
        payloads["unstarted_stream"] + tail,
        payloads["contradiction"] + f"logger={RULE_LOGGER} level=ERROR exception_class=none",
    ]


def test_the_metrics_and_the_policies_read_one_filter_each() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    assert tf.count("filter      = local.hold_release_filters[each.key]") == 1
    assert tf.count("filter = local.hold_release_filters[each.key]") == 1
    assert '"resource.type=\\"cloud_run_revision\\""' in tf
    assert 'metric_kind = "DELTA"' in tf and 'value_type  = "INT64"' in tf
    assert "notification_channels = local.fastpath_alert_channels" in tf
    assert re.search(r'notification_rate_limit \{\n\s+period = each\.value\.period\n\s+\}\n\s+auto_close = "1800s"', tf)
    # A release reaches a person the same day, at most once an hour; a
    # contradiction as the fast path's lines do.
    assert re.findall(r'period   = "(\d+s)"', tf) == ["3600s", "3600s", "300s"]
    assert re.findall(r'severity = "(\w+)"', tf) == ["WARNING", "WARNING", "ERROR"]
    # The schema guard reads infra/ for data definition verbs, comments included.
    assert not schema.DDL_STATEMENT.search(tf)


def test_the_runbook_names_each_policy_its_line_and_the_gate() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    titles = re.findall(r'title    = "(TR Holds: [^"]+)"', tf)
    assert len(titles) == 3
    for title in titles:
        assert f"Alert `{title}`" in runbook.replace("\n", " "), title
    payloads = _payloads()
    assert f"`{payloads['expired']}`" in runbook and f"`{payloads['unstarted_stream']}`" in runbook
    assert payloads["contradiction"].strip() in runbook
    assert '<a id="holds-released-without-a-terminal"></a>' in runbook
    assert '<a id="early-release-of-unstarted-holds"></a>' in runbook
    assert "Holds released without a terminal" in tf
    section = runbook.split('<a id="holds-released-without-a-terminal"></a>', 1)[1].split("## <a id=", 1)[0]
    # The read the runbook gives is by an index, aggregate only, at low priority.
    assert section.count("FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_terminal}") == 1
    assert "--priority=low" in section and "GROUP BY 1,2" in section
    for kind in (rule.LATE_FIRST_HEARTBEAT, rule.CHARGE_WITHOUT_HEARTBEAT):
        assert f"`kind={kind}`" in runbook


# -- the evidence lines -------------------------------------------------------


def _late(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "seq": 1, "accepted": True, "mode": "log", "after_seconds": AFTER,
        "declared_since": lambda _kid: SINCE, "now": CREATED + dt.timedelta(seconds=AFTER),
    }
    arguments.update(overrides)
    return arguments


def test_a_first_heartbeat_after_the_deadline_is_a_contradiction(rendered: _Rendered) -> None:
    rule.observe_first_heartbeat(_authorization(), **_late())
    rule.observe_first_heartbeat(_authorization(settled=True), **_late(accepted=False, mode="on"))
    assert rendered.lines == [
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat accepted=true age_seconds=900 after_seconds=900 mode=log",
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat accepted=false age_seconds=900 after_seconds=900 mode=on",
    ]


@pytest.mark.parametrize(
    ("row", "arguments"),
    [
        ({}, {"mode": "off"}),
        ({}, {"now": CREATED + dt.timedelta(seconds=AFTER // 3 - 1)}),
        ({}, {"seq": 2}),
        ({"heartbeat_seq": 1, "started_at": "2026-11-21T12:00:05Z"}, {}),
        ({"stage_d_reason": "not_streaming"}, {}),
        ({}, {"declared_since": lambda _kid: None}),
    ],
    ids=["off", "in-time", "not-the-first", "a-replay-of-the-first", "another-class", "boot-undeclared"],
)
def test_a_heartbeat_that_contradicts_nothing_writes_nothing(
    row: dict[str, Any], arguments: dict[str, Any], rendered: _Rendered
) -> None:
    rule.observe_first_heartbeat(_authorization(**row), **_late(**arguments))
    assert rendered.lines == []


def test_a_charging_settle_with_no_heartbeat_is_a_contradiction_at_any_age(rendered: _Rendered) -> None:
    def declared(_kid: str) -> str:
        return SINCE

    rule.observe_settle(_authorization(), success=True, mode="log", now=CREATED + dt.timedelta(seconds=3),
                        declared_since=declared)
    rule.observe_settle(
        _authorization(settled=True), success=True, mode="on", now=CREATED + dt.timedelta(seconds=1000),
        declared_since=declared,
    )
    assert rendered.lines == [
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=false age_seconds=3 mode=log",
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=true age_seconds=1000 mode=on",
    ]
    del rendered.lines[:]
    # A refund, a stream that heartbeated (and a replay of its settle), another
    # class, an undeclared boot, and the rule off: nothing.
    rule.observe_settle(_authorization(), success=False, mode="on", declared_since=declared)
    rule.observe_settle(_authorization(heartbeat_seq=3, started_at="2026-11-21T12:00:05Z", settled=True),
                        success=True, mode="on", declared_since=declared)
    rule.observe_settle(_authorization(stage_d_reason="not_streaming"), success=True, mode="on",
                        declared_since=declared)
    rule.observe_settle(_authorization(), success=True, mode="on", declared_since=lambda _kid: None)
    rule.observe_settle(_authorization(), success=True, mode="off", declared_since=declared)
    assert rendered.lines == []


def test_an_observer_never_raises(rendered: _Rendered) -> None:
    def broken(_kid: str) -> str:
        raise RuntimeError("storage unavailable")

    rule.observe_first_heartbeat(_authorization(), **_late(declared_since=broken))
    rule.observe_settle(_authorization(), success=True, mode="on", declared_since=broken)
    assert rendered.lines == [
        f"WARNING {RULE_LOGGER} unstarted release heartbeat observation failed error_class=RuntimeError",
        f"WARNING {RULE_LOGGER} unstarted release settle observation failed error_class=RuntimeError",
    ]


# -- the routes ---------------------------------------------------------------


def _heartbeat_route(mode: str, *, declared: bool = True, settled: bool = False) -> tuple[Any, Any]:
    private, boot = _boot(BOOT, "sha256:" + "aa" * 32)
    db = _gateway_heartbeat_store(
        boot.kid, replace(boot, declares_usage_heartbeat=declared, registered_at="2026-09-01T00:00:00Z"),
    )
    if settled:
        db.gateway_authorizations["gwa-stage-d-fixture"]["settled"] = True
        db.reservations["reservation"]["settled"] = True
    raw = _literal("heartbeat_request.json")
    body = GatewayHeartbeatRequest.model_validate_json(raw)
    assert body.seq == 1
    settings = Settings(
        environment="test", spend_lease_accepted_gcp_image_digests=boot.image_digest,
        unstarted_hold_release_mode=cast(Any, mode),
    )

    def call() -> Any:
        return gateway._heartbeat_gateway_sync(_request(_boot_auth_header(private, boot, raw)), body, settings, raw)

    return db, call


def test_the_heartbeat_route_reports_a_late_first_heartbeat_it_accepted(rendered: _Rendered) -> None:
    # The fixture row was authorized months before this clock: long past the deadline.
    db, call = _heartbeat_route("log")

    assert call()["accepted"] is True

    assert db.gateway_authorizations["gwa-stage-d-fixture"]["heartbeat_seq"] == 1
    assert len(rendered.lines) == 1
    assert re.fullmatch(
        rf"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat "
        r"accepted=true age_seconds=\d+ after_seconds=900 mode=log",
        rendered.lines[0],
    )
    # The same heartbeat again is a replay of a started stream: no second line.
    assert call()["accepted"] is True
    assert len(rendered.lines) == 1


def test_the_heartbeat_route_reports_the_first_heartbeat_a_released_row_refused(rendered: _Rendered) -> None:
    db, call = _heartbeat_route("on", settled=True)

    with pytest.raises(HTTPException) as refused:
        call()

    assert refused.value.status_code == 409
    assert db.gateway_authorizations["gwa-stage-d-fixture"]["heartbeat_seq"] in (0, None)
    assert len(rendered.lines) == 1 and " accepted=false " in rendered.lines[0] and rendered.lines[0].endswith("mode=on")


@pytest.mark.parametrize(("mode", "declared"), [("off", True), ("log", False)], ids=["off", "boot-undeclared"])
def test_the_heartbeat_route_writes_nothing_when_off_or_for_an_undeclared_boot(
    mode: str, declared: bool, rendered: _Rendered
) -> None:
    _db, call = _heartbeat_route(mode, declared=declared)
    assert call()["accepted"] is True
    assert rendered.lines == []


@pytest.mark.parametrize("mode", ["on", "off"])
def test_a_settle_for_a_released_stream_is_answered_uncharged_and_reported(
    mode: str, rendered: _Rendered
) -> None:
    """What a late settle does: the release stands, the settle is answered
    as a replay of a refund with no cost, and nothing is given back twice.
    With the rule on or in log, the line says the rule's premise failed."""
    store, db, authorization = _store()
    released = drain.release_unstarted_holds(CREATED + dt.timedelta(seconds=DUE + 1), mode="on", after_seconds=AFTER)
    assert released is not None and released.released == 1
    del rendered.lines[:]
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None and stored.settled

    answer = gateway._settle_gateway_authorization(
        GatewaySettleRequest.model_construct(authorization_id=authorization.id),
        success=True,
        settings=Settings(environment="test", unstarted_hold_release_mode=cast(Any, mode)),
        _authorization=stored,
        _boot_auth_outcome="absent",
    )

    data = answer["data"]
    assert (data["settled"], data["already_settled"], data["finalization_outcome"]) == (False, True, "refunded")
    assert data["cost_microdollars"] == 0
    assert _money(db) == (0, 0, 0, 0)
    if mode == "off":
        assert rendered.lines == []
    else:
        assert len(rendered.lines) == 1
        assert rendered.lines[0].startswith(
            f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=true "
        )


@pytest.mark.parametrize("mode", ["log", "on", "off"])
def test_a_settle_applied_from_its_frozen_intent_is_reported_too(
    env: Any, monkeypatch: pytest.MonkeyPatch, rendered: _Rendered, mode: str  # noqa: F811 - the imported fixture
) -> None:
    """Async settlement's handoff and the drain apply a settle from its
    frozen intent, not through the settle route: the same evidence line is
    written there, and the settle is applied exactly as it is with the rule
    off."""
    store, db, _runtime, _cfg = env
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    # The row as a heartbeated stream's, with no heartbeat on it.
    stored = db.gateway_authorizations[auth.id]
    payload = json.loads(stored["payload"])
    payload.update(stage_d_reason="ok", stage_d_boot_kid=BOOT, stage_d_prompt_tokens=100, stage_d_max_output_tokens=100)
    stored.update(payload=json.dumps(payload), stage_d_boot_kid=BOOT, pricing_snapshot=PRICING, heartbeat_seq=0)
    store.observe_gateway_boot(replace(_boot(BOOT, "sha256:" + "aa" * 32)[1], declares_usage_heartbeat=True))
    read = store.get_gateway_authorization(auth.id)
    assert read is not None and rule.heartbeating_stream(read) and rule.never_started(read)
    assert settle_outbox_apply._unstarted_release_mode() == "off"  # this process's own setting
    monkeypatch.setattr(settle_outbox_apply, "_unstarted_release_mode", lambda: mode)

    outcome = settle_outbox_apply.apply_frozen_settle(row)

    assert outcome == settle_outbox_apply.ApplyOutcome.SETTLED_NOW
    assert db.gateway_authorizations[auth.id]["finalization_outcome"] == "settled"
    lines = [line for line in rendered.lines if RULE_LOGGER in line]
    if mode == "off":
        assert lines == []
    else:
        assert len(lines) == 1 and re.fullmatch(
            rf"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat "
            rf"settled=false age_seconds=-?\d+ mode={mode}",
            lines[0],
        )
    # A refund's intent is never one of these.
    source = Path(settle_outbox_apply.__file__).read_text(encoding="utf-8")
    assert "    if success:\n        _observe_unstarted_charge(auth)\n" in source


def test_a_slow_first_heartbeat_short_of_the_deadline_is_written_without_the_counter(rendered: _Rendered) -> None:
    """The tail of the wait for a first heartbeat, visible before it reaches
    the deadline: from a third of the deadline on, one INFO line with the
    age, no counter and no alert."""
    payloads = _payloads()
    for age in (AFTER // 3 - 1, AFTER // 3, AFTER - 1):
        rule.observe_first_heartbeat(_authorization(), **_late(now=CREATED + dt.timedelta(seconds=age)))
    assert rendered.lines == [
        f"INFO {RULE_LOGGER} unstarted_release.slow_first_heartbeat age_seconds={AFTER // 3} after_seconds=900 mode=log",
        f"INFO {RULE_LOGGER} unstarted_release.slow_first_heartbeat age_seconds={AFTER - 1} after_seconds=900 mode=log",
    ]
    assert not any(payload in line for payload in payloads.values() for line in rendered.lines)


def test_a_leased_settle_made_durable_after_the_row_ended_as_a_refund_is_a_lost_charge(
    env: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811 - the imported fixture
) -> None:
    """The one charge the early release can lose without the route seeing
    it: a leased row is released, then its settle arrives. The route answers
    a replay and never asks the lease; the intent it made durable is applied
    by the drain, which must tell it as a lost charge (dead-lettered, with
    the alert) and not mark it done. A refund's intent, and a settle with
    nothing to charge, are still done quietly."""
    from tests.test_async_settle_handler import LEASED, _fastpath_all_mode
    from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle

    _store_, db, _runtime, cfg = env
    # A workspace in both pilots: its leased authorization carries a settle ticket.
    _fastpath_all_mode(cfg, pilots="", overlap="ws-v1", exclude="")
    body, auth, _key = prepare(env, authorize=LEASED)
    row = row_for(env, body)
    assert row.intent_kind == "settle" and row.actual_cost_micro > 0
    stored = db.gateway_authorizations[auth.id]
    assert json.loads(stored["payload"]).get("fastpath_envelope")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a terminal row's settle was sent to the lease")

    monkeypatch.setattr(gateway, "_fastpath_front_door", forbidden)
    # The row ends as a refund, as the early release (or the reaper) ends it.
    payload = json.loads(stored["payload"])
    payload.update(settled=True, finalization_outcome="refunded", finalized_cost_microdollars=0)
    stored.update(payload=json.dumps(payload), settled=True, finalization_outcome="refunded",
                  finalized_cost_microdollars=0)

    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
    assert apply_frozen_settle(replace(row, intent_kind="refund")) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert apply_frozen_settle(replace(row, actual_cost_micro=0)) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    # A row that ended charged is a replay, as before.
    payload.update(finalization_outcome="settled")
    stored.update(payload=json.dumps(payload), finalization_outcome="settled")
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE

    # The drain's handling of the outcome: dead, and the lost-charge alert.
    alerts: list[str] = []
    marks: list[dict[str, Any]] = []

    class Outbox:
        def mark(self, authorization_id: str, intent_kind: str, **kwargs: Any) -> str:
            marks.append({"authorization_id": authorization_id, "intent_kind": intent_kind, **kwargs})
            return "dead"

    monkeypatch.setattr(drain, "ops_alert", lambda message, **_kwargs: alerts.append(message))
    drain._resolve_row(cast(Any, Outbox()), row, ApplyOutcome.ALREADY_RELEASED_FREE, error_note=None)
    assert marks == [{
        "authorization_id": auth.id, "intent_kind": "settle", "done": False,
        "error": "already_released_free: settle charge was lost", "lease_owner": row.lease_owner, "force_dead": True,
    }]
    assert len(alerts) == 1 and alerts[0].startswith("ALERT settle outbox lost charge ")


def test_the_drain_route_passes_the_rule_only_when_it_is_not_off() -> None:
    source = Path(gateway.__file__).read_text(encoding="utf-8")
    route = source.split('@router.post("/internal/gateway/settle-outbox/drain")', 1)[1].split("@router.post(", 1)[0]
    gate, passed = route.split('if settings.unstarted_hold_release_mode != "off":', 1)
    assert "unstarted_release" not in gate
    assert 'drain_options["unstarted_release_mode"] = settings.unstarted_hold_release_mode' in passed
    assert 'drain_options["unstarted_release_seconds"] = settings.unstarted_hold_release_seconds' in passed
    assert "**drain_options," in passed


# -- off is the drain as it was ----------------------------------------------


@pytest.mark.parametrize("mode", ["off", "log", "on"])
def test_the_drain_with_the_rule_off_issues_the_frozen_drains_statements_and_books_the_same(
    env: Any, monkeypatch: pytest.MonkeyPatch, mode: str  # noqa: F811 - the imported fixture
) -> None:
    """A stream past the rule's deadline with no heartbeat, in front of the
    frozen drain (main before the rule) and today's. What this proves of
    off: the same response, the same durable state, the same statements
    with the same parameters and types, the same RPC counts, and the hold
    still held. It does not compare log lines: the reaper's release counter
    is written in every mode, off included, and no hold expires here. Log:
    the frozen drain's statements, then the pass's snapshot reads, and the
    same state. On: the hold given back."""
    store, db, _runtime, cfg = env
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    store.settle_outbox._async_fence = False
    clock = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock

    monkeypatch.setattr(dt, "datetime", Clock)
    monkeypatch.setattr(outbox_storage, "_iso_now", lambda: clock.isoformat().replace("+00:00", "Z"))
    monkeypatch.setattr(outbox_storage.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed-oracle-owner"))
    created = clock - dt.timedelta(seconds=DUE + 60)
    _seed(db, created_at=created.isoformat().replace("+00:00", "Z"))
    _counters(db)
    store.observe_gateway_boot(replace(_boot(BOOT, "sha256:" + "aa" * 32)[1], declares_usage_heartbeat=True))

    old_storage = namespace("storage_gcp_settle_outbox", outbox_storage)
    old_drain = namespace("settle_outbox_drain", drain)
    old_drain["SpannerSettleOutbox"] = old_storage["SpannerSettleOutbox"]
    names = ("typed", "rows", "reservations", "gateway_authorizations", "settle_outbox",
             "generation_records", "operational_analytics_outbox", "analytics_outbox", "reservation_idemp")
    saved = {name: copy.deepcopy(getattr(db, name)) for name in names}
    outputs = []
    for frozen in (True, False):
        for name, value in saved.items():
            setattr(db, name, copy.deepcopy(value))
        with monkeypatch.context() as patch:
            trace: list[Any] = []

            def record(cls: Any, method: str, trace: list[Any] = trace, patch: Any = patch) -> None:
                original = getattr(cls, method)

                def invoke(self: Any, *args: Any, **kwargs: Any) -> Any:
                    trace.append((cls.__name__, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
                    return original(self, *args, **kwargs)

                patch.setattr(cls, method, invoke)

            for method in ("execute_sql", "execute_update", "batch_update", "insert_or_update"):
                record(_FakeTransaction, method)
            record(_FakeSnapshot, "execute_sql")
            patch.setattr(drain, "_monotonic", lambda: 0.0)
            old_drain["_monotonic"] = lambda: 0.0
            rpc = ("snapshot_execute_sql_calls", "transaction_execute_sql_calls",
                   "transaction_execute_update_calls", "transaction_batch_update_calls", "commits")
            before = [getattr(db, name) for name in rpc]
            if frozen:
                result = old_drain["drain_settle_outbox"](100)
            else:
                options: dict[str, Any] = (
                    {} if mode == "off" else {"unstarted_release_mode": mode, "unstarted_release_seconds": AFTER}
                )
                result = drain.drain_settle_outbox(100, settings=Settings(environment="test"), **options)
            rpc_counts = [getattr(db, name) - value for name, value in zip(rpc, before, strict=True)]
            if not frozen:
                trace, rpc_counts = normalize_legacy_drain_trace(
                    trace, rpc_counts, expected_recovery_reads=16, parameter_types=store._param_types,
                )
            outputs.append((json.dumps(result, separators=(",", ":")),
                            {name: copy.deepcopy(getattr(db, name)) for name in names}, trace, rpc_counts))
    frozen_out, live_out = outputs
    assert frozen_out[0] == live_out[0]  # the response never changes
    held = live_out[1]["reservations"]["reservation"]["settled"] is False
    if mode == "off":
        assert frozen_out == live_out
        assert held and _money(db) == (HOLD, 0, HOLD, 0)
    elif mode == "log":
        assert frozen_out[1] == live_out[1] and held
        # The frozen drain's statements come first, unchanged; the pass adds
        # reads on read-only snapshots only (its scan, then the candidate's
        # three reads): no read-write transaction, no update, no commit.
        assert live_out[2][: len(frozen_out[2])] == frozen_out[2]
        added = live_out[2][len(frozen_out[2]):]
        assert added and {(call[0], call[1]) for call in added} == {("_FakeSnapshot", "execute_sql")}
        assert live_out[3][1:] == frozen_out[3][1:]
    else:
        assert live_out[2][: len(frozen_out[2])] == frozen_out[2]
        assert not held and _money(db) == (0, 0, 0, 0)
        assert live_out[1]["gateway_authorizations"]["gwa-unstarted"]["finalization_outcome"] == "refunded"
