"""The lines a hold's release writes, held to the metrics and policies that
watch them (infra/hold_release_alerts.tf): each filter's textPayload
substring is read from the Terraform and matched against the line as the
control plane writes it, rendered by the formatter Cloud Run's textPayload
carries, on an API service and behind the settle drain worker's output
boundary. Also what is counted: holds given back with nothing booked, one
line each, and nothing a customer could be told from.

Then the early rule's evidence lines, as the heartbeat and settle paths
write them (docs/design/fast-admission-streams.md section 13), and the drain
with the rule off against the frozen drain it was before the rule, statement
for statement.
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
from tests.test_fastpath_pilot_route import _body as _route_body
from tests.test_fastpath_pilot_route import _request as _route_request
from tests.test_fastpath_pilot_route import _seed as _route_seed
from tests.test_fastpath_pilot_route import _settle_body as _route_settle_body
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
    HOLD,
    PRICING,
    SINCE,
    _authorization,
    _counters,
    _money,
    _register,
    _registration,
    _seed,
    _store,
)
from trusted_router import storage_gcp_authorize
from trusted_router import storage_gcp_settle_outbox as outbox_storage
from trusted_router import unstarted_release as rule
from trusted_router.config import Settings
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayHeartbeatRequest, GatewaySettleRequest
from trusted_router.services import settle_drain_observability, settle_outbox_apply
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.storage_gcp_authorize import ReapPassResult

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "hold_release_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbook.md"
DRAIN_LOGGER = "trusted_router.services.settle_outbox_drain"
RULE_LOGGER = "trusted_router.unstarted_release"
KINDS = ["expired", "unstarted_stream", "contradiction", "observation_failed"]


def _payloads() -> dict[str, str]:
    """Each kind's textPayload substring, as the Terraform carries it."""
    tf = TERRAFORM.read_text(encoding="utf-8")
    found = re.findall(r'\n    (\w+) = \{\n      payload  = "([^"]+)"', tf)
    assert [kind for kind, _payload in found] == KINDS, found
    return dict(found)


class _Rendered(logging.Handler):
    """A logger's lines, as the console handler renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.records: list[logging.LogRecord] = []
        self.setFormatter(_ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
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


def _since(_kid: str) -> dt.datetime:
    return SINCE


# -- the release counters -----------------------------------------------------


def test_the_filter_matches_the_expiry_line_and_no_other(rendered: _Rendered) -> None:
    payload = _payloads()["expired"]
    assert drain.logger.name == DRAIN_LOGGER
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
    # The whole line: the counter, and nothing of a customer.
    assert rendered.lines == [payload + "value=1"]
    assert rendered.records[0].levelno == logging.WARNING
    assert rendered.records[0].metric == drain._RELEASED_EXPIRED_METRIC  # type: ignore[attr-defined]
    # A longer name, another level or another logger is not it, nor is the
    # reaper's own summary line.
    assert payload not in f"WARNING {DRAIN_LOGGER} metric=gateway_hold_released_expired_total_x value=1"
    assert payload not in f"INFO {DRAIN_LOGGER} metric=gateway_hold_released_expired_total value=1"
    assert payload not in "WARNING trusted_router.other metric=gateway_hold_released_expired_total value=1"
    assert payload not in f"INFO {DRAIN_LOGGER} gateway.reaped count=3 released_hold_micro=9"


def test_each_filter_matches_its_own_line_and_no_other(rendered: _Rendered) -> None:
    payloads = _payloads()
    assert drain.logger.name == DRAIN_LOGGER and rule.log.name == RULE_LOGGER
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
    drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 1)
    rule.observe_settle(_authorization(), charging=True, mode="log", declared_since=_since,
                        now=CREATED + dt.timedelta(seconds=5))
    rule.observation_failed("settle", RuntimeError("storage unavailable for a workspace"))
    rule.observe_pass(mode="log", errors=2, truncated=True, unguarded=False)
    expired, unstarted, contradiction, observer, the_pass = rendered.lines
    # The whole of a release's line: the rule's counter, and no customer.
    assert expired == payloads["expired"] + "value=1"
    assert unstarted == payloads["unstarted_stream"] + "value=1"
    assert contradiction.startswith(payloads["contradiction"] + "value=1 kind=charge_without_heartbeat ")
    # An observation that failed: where, and the error's class, never its text.
    assert observer == payloads["observation_failed"] + "value=1 where=settle error_class=RuntimeError"
    assert the_pass == (
        payloads["observation_failed"] + "value=1 where=pass mode=log errors=2 truncated=true unguarded=false failed=false"
    )
    lines = (("expired", expired), ("unstarted_stream", unstarted), ("contradiction", contradiction),
             ("observation_failed", observer), ("observation_failed", the_pass))
    for kind, line in lines:
        assert [payload in line for payload in payloads.values()] == [kind == other for other in payloads], line
    # The reaper's own summary and the early rule's are not counted as anything.
    assert not any(payload in f"INFO {DRAIN_LOGGER} gateway.reaped count=3 released_hold_micro=9" for payload in payloads.values())
    assert not any(payload in f"INFO {DRAIN_LOGGER} gateway.unstarted_release mode=log" for payload in payloads.values())


def test_one_line_a_hold_and_none_for_nothing(rendered: _Rendered) -> None:
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 3)
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 0)
    drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, -1)
    assert rendered.lines == [_payloads()["expired"] + "value=1"] * 3


@pytest.mark.parametrize(
    ("result", "released"),
    [
        (ReapPassResult(), 0),
        (ReapPassResult(count=4, refunded=4), 4),
        # A stream ended at its last heartbeat's snapshot is charged it: not a release.
        (ReapPassResult(count=5, refunded=3, snapshot_bookings=2), 3),
        (ReapPassResult(count=2, snapshot_bookings=2), 0),
        # The count-only reaper (a store without the rich result) only refunds.
        (ReapPassResult(count=7), 7),
        (ReapPassResult(count=1, snapshot_bookings=3), 0),
    ],
)
def test_what_is_counted_is_holds_given_back_with_nothing_booked(result: ReapPassResult, released: int) -> None:
    assert drain._released_at_expiry(result) == released


def test_the_settle_drain_workers_boundary_keeps_the_counter(rendered: _Rendered) -> None:
    assert drain._RELEASED_EXPIRED_METRIC in settle_drain_observability._COUNTED_METRICS
    remove = settle_drain_observability.install_logging()
    try:
        drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
    finally:
        remove()
    tail = f"logger={DRAIN_LOGGER} level=WARNING exception_class=none"
    assert rendered.lines == [_payloads()["expired"] + tail]


def test_the_settle_drain_workers_boundary_keeps_each_counter(rendered: _Rendered) -> None:
    """The worker applies settles from their frozen intents, so the evidence
    lines can be written there too: each counter's name is kept, and nothing
    else of any of the lines."""
    payloads = _payloads()
    assert {
        drain._RELEASED_EXPIRED_METRIC, drain._RELEASED_UNSTARTED_METRIC, rule.CONTRADICTION_METRIC,
        rule.OBSERVATION_FAILED_METRIC,
    } <= settle_drain_observability._COUNTED_METRICS
    remove = settle_drain_observability.install_logging()
    try:
        drain._count_released_holds(drain._RELEASED_EXPIRED_METRIC, 1)
        drain._count_released_holds(drain._RELEASED_UNSTARTED_METRIC, 1)
        rule.observe_settle(_authorization(), charging=True, mode="log", declared_since=_since)
        rule.observation_failed("settle", RuntimeError("storage unavailable for a workspace"))
    finally:
        remove()
    tail = f"logger={DRAIN_LOGGER} level=WARNING exception_class=none"
    assert rendered.lines == [
        payloads["expired"] + tail,
        payloads["unstarted_stream"] + tail,
        payloads["contradiction"] + f"logger={RULE_LOGGER} level=ERROR exception_class=none",
        payloads["observation_failed"] + f"logger={RULE_LOGGER} level=WARNING exception_class=none",
    ]


class _EmptyOutbox:
    """A settle outbox with nothing due."""

    _async_fence = False
    _database = None

    def claim(self, **_kwargs: Any) -> list[Any]:
        return []

    def purge_done(self) -> int:
        return 0

    def due_shard(self, **_kwargs: Any) -> list[Any]:
        return []


def test_the_drain_counts_what_the_reaper_released_after_its_pass(
    rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain as it runs: after the reaper's pass it writes one line for
    each hold the pass released, and none for a snapshot booking."""
    from trusted_router.storage import STORE

    monkeypatch.setattr(drain, "spanner_settle_outbox", lambda: _EmptyOutbox())
    monkeypatch.setattr(drain, "_reconcile_leased", lambda _result: {})
    monkeypatch.setattr(
        type(cast(Any, STORE).target), "reap_expired_reservations_result",
        lambda _self, **_kwargs: ReapPassResult(count=3, refunded=2, snapshot_bookings=1, released_hold_micro=9),
        raising=False,
    )

    answer = drain.drain_settle_outbox(10)

    assert answer["reaped"] == 3
    counted = [line for line in rendered.lines if "metric=gateway_hold_released_expired_total " in line]
    assert counted == [_payloads()["expired"] + "value=1"] * 2
    assert any(" gateway.reaped count=3 " in line and " snapshot_bookings=1 " in line for line in rendered.lines)


def test_a_logging_failure_does_not_fail_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reaper has committed by the time the lines are written: a handler
    that raises leaves the pass's answer as it was."""
    from trusted_router.storage import STORE

    class Raising(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.seen = 0

        def emit(self, record: logging.LogRecord) -> None:
            if "metric=gateway_hold_released_expired_total" in record.getMessage():
                self.seen += 1
                raise OSError("the log pipe is gone")

    monkeypatch.setattr(drain, "spanner_settle_outbox", lambda: _EmptyOutbox())
    monkeypatch.setattr(drain, "_reconcile_leased", lambda _result: {})
    monkeypatch.setattr(
        type(cast(Any, STORE).target), "reap_expired_reservations_result",
        lambda _self, **_kwargs: ReapPassResult(count=1, refunded=1), raising=False,
    )
    monkeypatch.setattr(logging, "raiseExceptions", True)
    handler = Raising()
    monkeypatch.setattr(logging.Handler, "handleError", lambda _self, _record: (_ for _ in ()).throw(OSError("handler failed")))
    target = logging.getLogger(DRAIN_LOGGER)
    target.addHandler(handler)
    try:
        answer = drain.drain_settle_outbox(10)
    finally:
        target.removeHandler(handler)

    assert handler.seen == 1
    assert answer["reaped"] == 1


def test_the_metric_and_the_policy_read_one_filter_and_notify_hourly() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    assert tf.count("filter      = local.hold_release_filters[each.key]") == 1
    assert tf.count("filter = local.hold_release_filters[each.key]") == 1
    assert '"resource.type=\\"cloud_run_revision\\""' in tf
    assert 'metric_kind = "DELTA"' in tf and 'value_type  = "INT64"' in tf
    assert "notification_channels = local.fastpath_alert_channels" in tf
    assert re.search(r'notification_rate_limit \{\n\s+period = each\.value\.period\n\s+\}\n\s+auto_close = "1800s"', tf)
    # A release reaches a person the same day, at most once an hour, as does
    # evidence that did not complete; a contradiction as the fast path's lines do.
    assert re.findall(r'period   = "(\d+s)"', tf) == ["3600s", "3600s", "300s", "3600s"]
    assert re.findall(r'severity = "(\w+)"', tf) == ["WARNING", "WARNING", "ERROR", "WARNING"]
    # The policy says the background is expected, and why it still notifies.
    assert "Expect tens a week" in tf
    # The schema guard reads infra/ for data definition verbs, comments included.
    assert not schema.DDL_STATEMENT.search(tf)


def test_the_runbook_names_the_policy_its_line_and_what_is_counted() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    title = re.findall(r'title    = "(TR Holds: [^"]+)"', tf)[0]
    assert f"Alert `{title}`" in runbook.replace("\n", " ")
    assert f"`{_payloads()['expired']}`" in runbook
    assert '<a id="holds-released-without-a-terminal"></a>' in runbook
    assert "Holds released without a terminal" in tf
    section = runbook.split('<a id="holds-released-without-a-terminal"></a>', 1)[1].split("## <a id=", 1)[0]
    assert "snapshot_bookings" in section
    # The read the runbook gives is by an index, aggregate only, at low priority.
    assert section.count("FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_terminal}") == 1
    assert "--priority=low" in section and "GROUP BY 1,2" in section


def test_the_runbook_names_each_policy_its_line_and_the_gate() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", runbook)
    titles = re.findall(r'title    = "(TR Holds: [^"]+)"', tf)
    assert len(titles) == len(KINDS)
    for title in titles:
        assert f"Alert `{title}`" in flat, title
    payloads = _payloads()
    assert f"`{payloads['expired']}`" in runbook and f"`{payloads['unstarted_stream']}`" in runbook
    assert payloads["contradiction"].strip() in runbook and payloads["observation_failed"].strip() in runbook
    assert '<a id="early-release-of-unstarted-holds"></a>' in runbook
    section = runbook.split('<a id="early-release-of-unstarted-holds"></a>', 1)[1].split("\n## <a id=", 1)[0]
    for kind in (rule.LATE_FIRST_HEARTBEAT, rule.CHARGE_WITHOUT_HEARTBEAT):
        assert f"`kind={kind}`" in section
    # The gate reads the health of the evidence, not only its silence.
    gate = re.sub(r"\s+", " ", section.split("**The gate for `on`.**", 1)[1].split("**Turning it on.**", 1)[0])
    for needle in (
        rule.CONTRADICTION_METRIC, rule.OBSERVATION_FAILED_METRIC, "where=pass", "where=heartbeat", "where=settle",
        "errors=0", "truncated=false", "unguarded=false", "gateway.unstarted_release mode=log",
        "gateway_hold_release_observation_failed", "gateway_hold_release_contradiction",
    ):
        assert needle in gate, needle


# -- the evidence lines -------------------------------------------------------


def _late(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "seq": 1, "accepted": True, "mode": "log", "after_seconds": AFTER,
        "declared_since": _since, "now": CREATED + dt.timedelta(seconds=AFTER),
    }
    arguments.update(overrides)
    return arguments


def test_a_first_heartbeat_after_the_deadline_is_a_contradiction(rendered: _Rendered) -> None:
    rule.observe_first_heartbeat(_authorization(), **_late())
    rule.observe_first_heartbeat(_authorization(settled=True), **_late(accepted=False, mode="on"))
    # The observer reads two application clocks, so it calls the heartbeat
    # late from the clock allowance before the deadline: one too many, never
    # one too few.
    early = CREATED + dt.timedelta(seconds=AFTER - rule.CLOCK_SKEW_ALLOWANCE_SECONDS)
    rule.observe_first_heartbeat(_authorization(), **_late(now=early))
    assert rendered.lines == [
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat accepted=true age_seconds=900 after_seconds=900 mode=log",
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat accepted=false age_seconds=900 after_seconds=900 mode=on",
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=late_first_heartbeat accepted=true age_seconds=840 after_seconds=900 mode=log",
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


def test_a_slow_first_heartbeat_short_of_the_deadline_is_written_without_the_counter(rendered: _Rendered) -> None:
    """The tail of the wait for a first heartbeat, visible before it reaches
    the deadline: from a third of the deadline on, one INFO line with the
    age, no counter and no alert."""
    payloads = _payloads()
    last = AFTER - rule.CLOCK_SKEW_ALLOWANCE_SECONDS - 1
    for age in (AFTER // 3 - 1, AFTER // 3, last):
        rule.observe_first_heartbeat(_authorization(), **_late(now=CREATED + dt.timedelta(seconds=age)))
    assert rendered.lines == [
        f"INFO {RULE_LOGGER} unstarted_release.slow_first_heartbeat age_seconds={AFTER // 3} after_seconds=900 mode=log",
        f"INFO {RULE_LOGGER} unstarted_release.slow_first_heartbeat age_seconds={last} after_seconds=900 mode=log",
    ]
    assert not any(payload in line for payload in payloads.values() for line in rendered.lines)


def test_a_settle_with_a_charge_and_no_heartbeat_is_a_contradiction_at_any_age(rendered: _Rendered) -> None:
    rule.observe_settle(_authorization(), charging=True, mode="log", now=CREATED + dt.timedelta(seconds=3),
                        declared_since=_since)
    rule.observe_settle(
        _authorization(settled=True), charging=True, mode="on", now=CREATED + dt.timedelta(seconds=1000),
        declared_since=_since,
    )
    assert rendered.lines == [
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=false age_seconds=3 mode=log",
        f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=true age_seconds=1000 mode=on",
    ]
    del rendered.lines[:]
    # A settle of nothing (it loses nothing), a stream that heartbeated (and
    # a replay of its settle), another class, an undeclared boot, and the
    # rule off: nothing.
    rule.observe_settle(_authorization(), charging=False, mode="on", declared_since=_since)
    rule.observe_settle(_authorization(settled=True), charging=False, mode="log", declared_since=_since)
    rule.observe_settle(_authorization(heartbeat_seq=3, started_at="2026-11-21T12:00:05Z", settled=True),
                        charging=True, mode="on", declared_since=_since)
    rule.observe_settle(_authorization(stage_d_reason="not_streaming"), charging=True, mode="on",
                        declared_since=_since)
    rule.observe_settle(_authorization(), charging=True, mode="on", declared_since=lambda _kid: None)
    rule.observe_settle(_authorization(), charging=True, mode="off", declared_since=_since)
    assert rendered.lines == []


def test_an_ordinary_settle_or_first_heartbeat_stops_before_the_snapshot_is_parsed(
    rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observers sit on every stream's first heartbeat and settle in
    log mode: a stream that heartbeated, and a first heartbeat seconds
    after its authorize, are decided on fields alone."""
    def forbidden(_snapshot: Any) -> Any:
        raise AssertionError("the pricing snapshot was parsed for an ordinary request")

    monkeypatch.setattr(rule, "pricing_snapshot_valid", forbidden)
    rule.observe_settle(
        _authorization(heartbeat_seq=4, started_at="2026-11-21T12:00:02Z"), charging=True, mode="log",
        declared_since=_since,
    )
    rule.observe_first_heartbeat(_authorization(), **_late(now=CREATED + dt.timedelta(seconds=4)))
    assert rendered.lines == []


def test_an_observer_that_cannot_finish_is_counted_and_never_raises(rendered: _Rendered) -> None:
    """A window with no contradiction line is evidence only if the observers
    worked: one that could not finish writes the other counter, with the
    error's class and none of its text."""
    def broken(_kid: str) -> Any:
        raise RuntimeError("storage unavailable for workspace and key")

    rule.observe_first_heartbeat(_authorization(), **_late(declared_since=broken))
    rule.observe_settle(_authorization(), charging=True, mode="on", declared_since=broken)
    prefix = f"WARNING {RULE_LOGGER} metric=unstarted_release_observation_failed_total value=1 "
    assert rendered.lines == [
        prefix + "where=heartbeat error_class=RuntimeError",
        prefix + "where=settle error_class=RuntimeError",
    ]
    assert all(record.exc_info is None for record in rendered.records)
    assert {record.metric for record in rendered.records} == {rule.OBSERVATION_FAILED_METRIC}  # type: ignore[attr-defined]


def test_a_pass_that_did_not_examine_everything_is_counted_with_the_observers(rendered: _Rendered) -> None:
    rule.observe_pass(mode="log", errors=0, truncated=False, unguarded=False)
    assert rendered.lines == []
    rule.observe_pass(mode="log", errors=1, truncated=False, unguarded=False)
    rule.observe_pass(mode="log", errors=0, truncated=True, unguarded=False)
    rule.observe_pass(mode="on", errors=0, truncated=False, unguarded=True)
    rule.observe_pass(mode="log", errors=0, truncated=False, unguarded=False, failed=True)
    prefix = f"WARNING {RULE_LOGGER} metric=unstarted_release_observation_failed_total value=1 where=pass "
    assert rendered.lines == [
        prefix + "mode=log errors=1 truncated=false unguarded=false failed=false",
        prefix + "mode=log errors=0 truncated=true unguarded=false failed=false",
        prefix + "mode=on errors=0 truncated=false unguarded=true failed=false",
        prefix + "mode=log errors=0 truncated=false unguarded=false failed=true",
    ]


def test_the_drains_pass_reports_its_own_health(rendered: _Rendered, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass as the drain runs it, in log mode: one that read every row
    writes its summary and nothing more; a candidate it could not read, a
    scan that filled its limit, and a pass that failed outright each write
    the observation counter the gate reads."""
    store, db, _authorization_row = _store()
    counted = f"WARNING {RULE_LOGGER} metric=unstarted_release_observation_failed_total value=1 where=pass mode=log "

    def run(**options: Any) -> list[str]:
        del rendered.lines[:]
        db.now = CREATED + dt.timedelta(seconds=AFTER + 1)
        drain.release_unstarted_holds(db.now, mode="log", after_seconds=AFTER, **options)
        return [line for line in rendered.lines if RULE_LOGGER in line]

    assert run() == []
    assert any(" would_release=1 " in line and " errors=0 " in line for line in rendered.lines)

    assert run(limit=1) == [counted + "errors=0 truncated=true unguarded=false failed=false"]

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("storage unavailable for reservation and workspace")

    with monkeypatch.context() as patch:
        patch.setattr(storage_gcp_authorize, "_observe_unstarted_candidate", unreadable)
        assert run() == [counted + "errors=1 truncated=false unguarded=false failed=false"]
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "release_unstarted_holds_result", unreadable)
        assert run() == [counted + "errors=0 truncated=false unguarded=false failed=true"]
    with monkeypatch.context() as patch:
        patch.setattr(storage_gcp_authorize, "_outbox_table_available", lambda _database, _types: False)
        assert run() == [counted + "errors=0 truncated=false unguarded=true failed=false"]


# -- the routes ---------------------------------------------------------------


def _heartbeat_route(mode: str, *, declared: bool = True, settled: bool = False) -> tuple[Any, Any]:
    private, boot = _boot(BOOT, "sha256:" + "aa" * 32)
    db = _gateway_heartbeat_store(boot.kid, replace(boot, declares_usage_heartbeat=declared))
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


def test_the_heartbeat_route_counts_an_observer_that_cannot_read_the_boot(
    rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trusted_router.storage import STORE

    _db, call = _heartbeat_route("log")

    def away(_self: Any, _kid: str) -> Any:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(type(cast(Any, STORE).target), "gateway_boot_declaring_since", away)

    assert call()["accepted"] is True
    assert rendered.lines == [
        f"WARNING {RULE_LOGGER} metric=unstarted_release_observation_failed_total value=1 where=heartbeat error_class=RuntimeError"
    ]


@pytest.mark.parametrize("mode", ["on", "off"])
def test_a_settle_for_a_released_stream_is_answered_uncharged_and_reported(
    mode: str, rendered: _Rendered
) -> None:
    """What a late settle does: the release stands, the settle is answered
    as a replay of a refund with no cost, and nothing is given back twice.
    With the rule on or in log, a settle that reports usage writes the line
    that says the rule's premise failed; one that reports none loses nothing
    and writes nothing."""
    store, db, authorization = _store()
    db.now = CREATED + dt.timedelta(seconds=AFTER + 1)
    released = drain.release_unstarted_holds(db.now, mode="on", after_seconds=AFTER)
    assert released is not None and released.released == 1
    stored = store.get_gateway_authorization(authorization.id)
    assert stored is not None and stored.settled

    def settle(input_tokens: int, output_tokens: int) -> list[str]:
        del rendered.lines[:]
        answer = gateway._settle_gateway_authorization(
            GatewaySettleRequest.model_construct(
                authorization_id=authorization.id, actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
            ),
            success=True,
            settings=Settings(environment="test", unstarted_hold_release_mode=cast(Any, mode)),
            _authorization=stored,
            _boot_auth_outcome="absent",
        )
        data = answer["data"]
        assert (data["settled"], data["already_settled"], data["finalization_outcome"]) == (False, True, "refunded")
        assert data["cost_microdollars"] == 0
        assert _money(db) == (0, 0, 0, 0)
        return list(rendered.lines)

    reported = settle(90, 40)
    nothing = settle(0, 0)
    assert nothing == []
    if mode == "off":
        assert reported == []
    else:
        assert len(reported) == 1
        assert reported[0].startswith(
            f"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat settled=true "
        )


@pytest.mark.parametrize(("tokens", "charged"), [((90, 40), True), ((0, 0), False)], ids=["charge", "no-charge"])
def test_the_settle_route_reports_an_open_rows_settle_only_when_it_charges(
    tokens: tuple[int, int], charged: bool, rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle route, on an open row of the heartbeated class with no
    heartbeat on it: the settle is applied as it is with the rule off, and
    the line is written only for a charge above zero. A settle that prices
    to nothing is not a contradiction: nothing is lost if its hold had been
    released."""
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    store, database, key = _route_seed()
    settings = Settings(environment="test", service_surface="internal", settle_outbox_enabled=True,
                        unstarted_hold_release_mode="log")
    authorized = gateway._authorize_gateway_sync(_route_request(), _route_body(key.hash, stream=True), settings)["data"]
    store.observe_gateway_boot(_registration(declares=True))
    stored = database.gateway_authorizations[authorized["authorization_id"]]
    payload = json.loads(stored["payload"])
    payload.update(stage_d_reason="ok", stage_d_boot_kid=BOOT, stage_d_prompt_tokens=100, stage_d_max_output_tokens=100)
    stored.update(payload=json.dumps(payload), stage_d_boot_kid=BOOT, pricing_snapshot=PRICING, heartbeat_seq=0)
    body = _route_settle_body(authorized).model_copy(
        update={"actual_input_tokens": tokens[0], "actual_output_tokens": tokens[1], "streamed": True},
    )

    data = gateway._settle_gateway_authorization(body, success=True, settings=settings)["data"]

    assert data["settled"] is True and data["finalization_outcome"] == "settled"
    assert (data["cost_microdollars"] > 0) is charged
    lines = [line for line in rendered.lines if RULE_LOGGER in line]
    if charged:
        assert len(lines) == 1 and re.fullmatch(
            rf"ERROR {RULE_LOGGER} metric=unstarted_release_contradiction_total value=1 kind=charge_without_heartbeat "
            r"settled=false age_seconds=-?\d+ mode=log",
            lines[0],
        )
    else:
        assert lines == []


def _frozen_settle_of_an_unstarted_stream(env: Any) -> tuple[Any, Any]:  # noqa: F811 - the imported fixture's name
    """A frozen settle intent for a row doctored into a heartbeated stream's
    with no heartbeat on it, on a boot that declares usage heartbeats."""
    store, db, _runtime, _cfg = env
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    stored = db.gateway_authorizations[auth.id]
    payload = json.loads(stored["payload"])
    payload.update(stage_d_reason="ok", stage_d_boot_kid=BOOT, stage_d_prompt_tokens=100, stage_d_max_output_tokens=100)
    stored.update(payload=json.dumps(payload), stage_d_boot_kid=BOOT, pricing_snapshot=PRICING, heartbeat_seq=0)
    store.observe_gateway_boot(replace(_boot(BOOT, "sha256:" + "aa" * 32)[1], declares_usage_heartbeat=True))
    read = store.get_gateway_authorization(auth.id)
    assert read is not None
    view = rule.decoded_view(read)
    assert rule.class_reason(view) is None and rule.start_reason(view) is None
    return row, auth


@pytest.mark.parametrize("mode", ["log", "on", "off"])
def test_a_settle_applied_from_its_frozen_intent_is_reported_too(
    env: Any, monkeypatch: pytest.MonkeyPatch, rendered: _Rendered, mode: str  # noqa: F811 - the imported fixture
) -> None:
    """Async settlement's handoff and the drain apply a settle from its
    frozen intent, not through the settle route: the same evidence line is
    written there, and the settle is applied exactly as it is with the rule
    off."""
    _store_, db, _runtime, _cfg = env
    row, auth = _frozen_settle_of_an_unstarted_stream(env)
    assert row.actual_cost_micro > 0
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


@pytest.mark.parametrize("intent", ["zero-charge", "refund"])
def test_a_frozen_intent_that_charges_nothing_is_not_reported(
    env: Any, monkeypatch: pytest.MonkeyPatch, rendered: _Rendered, intent: str  # noqa: F811 - the imported fixture
) -> None:
    """Only a charge above zero is a contradiction: a settle frozen at no
    charge, and a refund, lose nothing if the hold had been released."""
    row, _auth = _frozen_settle_of_an_unstarted_stream(env)
    monkeypatch.setattr(settle_outbox_apply, "_unstarted_release_mode", lambda: "log")
    observed: list[Any] = []
    monkeypatch.setattr(settle_outbox_apply, "_observe_unstarted_charge", observed.append)
    frozen = replace(row, actual_cost_micro=0) if intent == "zero-charge" else replace(row, intent_kind="refund")

    settle_outbox_apply.apply_frozen_settle(frozen)

    assert observed == []
    assert [line for line in rendered.lines if RULE_LOGGER in line] == []
    # And the charging settle of the same row is one.
    settle_outbox_apply.apply_frozen_settle(row)
    assert len(observed) == 1


def test_the_apply_paths_switch_that_cannot_be_read_is_counted_and_not_remembered(
    rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen-intent path reads the rule's switch once a process. A
    settings error reads as off for that settle, is written as an
    observation that failed, and is tried again on the next settle."""
    calls: list[int] = []

    def broken() -> str:
        calls.append(1)
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settle_outbox_apply, "_configured_unstarted_release_mode", broken)

    assert settle_outbox_apply._unstarted_release_mode() == "off"
    assert settle_outbox_apply._unstarted_release_mode() == "off"

    assert len(calls) == 2
    assert rendered.lines == [
        f"WARNING {RULE_LOGGER} metric=unstarted_release_observation_failed_total value=1 where=settle error_class=RuntimeError"
    ] * 2


def test_a_leased_settle_for_a_row_the_control_plane_ended_is_a_lost_charge(
    env: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811 - the imported fixture
) -> None:
    """The one charge the early release can lose without the route seeing
    it: a leased row is released, then its settle arrives. The route answers
    a replay and never asks the lease; the intent it made durable is applied
    by the drain, which must tell it as a lost charge (dead-lettered, with
    the alert) and not mark it done.

    Only a row the control plane itself ended is told so. The gateway's own
    refund of a leased row is always made durable as a refund intent before
    it is applied, so a refunded row with that intent is the ordinary
    sequence (a refund, then an old settle retried) and stays done, quietly,
    as it was before. A refund's intent and a settle with nothing to charge
    are done quietly too."""
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
    # The row ends as a refund, as the early release (or the reaper) ends it:
    # no refund intent was ever recorded for it.
    payload = json.loads(stored["payload"])
    payload.update(settled=True, finalization_outcome="refunded", finalized_cost_microdollars=0)
    stored.update(payload=json.dumps(payload), settled=True, finalization_outcome="refunded",
                  finalized_cost_microdollars=0)
    assert not [key for key in db.settle_outbox if key[0] == auth.id]

    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
    assert apply_frozen_settle(replace(row, intent_kind="refund")) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    assert apply_frozen_settle(replace(row, actual_cost_micro=0)) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE

    # The same row, ended by the gateway's own refund: its refund intent is
    # in the outbox, whatever its status, and the settle is done quietly.
    for status in ("done", "pending"):
        db.settle_outbox[(auth.id, "refund")] = {
            "authorization_id": auth.id, "intent_kind": "refund", "status": status, "settle_origin": "typed",
            "actual_cost_micro": 0,
        }
        assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    # A settle intent of its own says nothing about who ended the row.
    del db.settle_outbox[(auth.id, "refund")]
    db.settle_outbox[(auth.id, "settle")] = {
        "authorization_id": auth.id, "intent_kind": "settle", "status": "pending", "settle_origin": "typed",
        "actual_cost_micro": row.actual_cost_micro,
    }
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
    del db.settle_outbox[(auth.id, "settle")]

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
    # The database's clock is the drain's here: a boot that has declared for
    # an hour, a row committed a minute past the deadline ago.
    created = clock - dt.timedelta(seconds=AFTER + 60)
    _register(store, db, declares=True, at=created - dt.timedelta(hours=1))
    _seed(db, created=created)
    db.now = clock
    _counters(db)

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
        # reads on read-only snapshots only (its scan, the candidate's three
        # reads and the boot's): no read-write transaction, no update, no
        # commit.
        assert live_out[2][: len(frozen_out[2])] == frozen_out[2]
        added = live_out[2][len(frozen_out[2]):]
        assert {(call[0], call[1]) for call in added} == {("_FakeSnapshot", "execute_sql")}
        # The pass's whole work for one candidate: the outbox table's probe
        # (a named database remembers it for the life of the process; this
        # double has no name), one scan, the candidate's three point reads,
        # and one read of its boot.
        statements = [call[2][0] for call in added]
        assert len(statements) == 6
        assert statements[0] == statements[3] == storage_gcp_authorize.GUARD_COUNT_SQL
        assert added[0][3]["params"] == {"aid": ""} and added[3][3]["params"] == {"aid": "gwa-unstarted"}
        assert statements[1] == storage_gcp_authorize._UNSTARTED_SCAN_SQL
        assert statements[2].startswith("SELECT reservation_id, workspace_id, ")
        assert statements[4].startswith("SELECT authorization_id, workspace_id, ")
        assert statements[5] == "SELECT body, updated_at FROM tr_entities WHERE kind=@kind AND id=@id"
        assert live_out[3][1:] == frozen_out[3][1:]
    else:
        assert live_out[2][: len(frozen_out[2])] == frozen_out[2]
        assert not held and _money(db) == (0, 0, 0, 0)
        assert live_out[1]["gateway_authorizations"]["gwa-unstarted"]["finalization_outcome"] == "refunded"
