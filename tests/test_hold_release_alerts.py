"""The line the drain writes for each hold the reaper released at its expiry,
held to the metric and policy that watch it (infra/hold_release_alerts.tf):
the filter's textPayload substring is read from the Terraform and matched
against the line the drain writes, rendered by the formatter Cloud Run's
textPayload carries, on an API service and behind the settle drain worker's
output boundary. Also what is counted: holds given back with nothing booked,
one line each, and nothing a customer could be told from."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, cast

import pytest

from tests.conformance import spanner_schema_source as schema
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.services import settle_drain_observability
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.storage_gcp_authorize import ReapPassResult

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "hold_release_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbook.md"
DRAIN_LOGGER = "trusted_router.services.settle_outbox_drain"


def _payloads() -> dict[str, str]:
    """Each kind's textPayload substring, as the Terraform carries it."""
    tf = TERRAFORM.read_text(encoding="utf-8")
    found = re.findall(r'\n    (\w+) = \{\n      payload  = "([^"]+)"', tf)
    assert [kind for kind, _payload in found][:1] == ["expired"], found
    return dict(found)


class _Rendered(logging.Handler):
    """The drain logger's lines, as the console handler renders them."""

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
    logger = logging.getLogger(DRAIN_LOGGER)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.setLevel(level)
        logger.removeHandler(handler)


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


def test_the_drain_counts_what_the_reaper_released_after_its_pass(
    rendered: _Rendered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain as it runs: after the reaper's pass it writes one line for
    each hold the pass released, and none for a snapshot booking."""
    from trusted_router.storage import STORE

    class Outbox:
        def claim(self, **_kwargs: Any) -> list[Any]:
            return []

        def purge_done(self) -> int:
            return 0

        def due_shard(self, **_kwargs: Any) -> list[Any]:
            return []

        _async_fence = False
        _database = None

    monkeypatch.setattr(drain, "spanner_settle_outbox", lambda: Outbox())
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

    class Outbox:
        def claim(self, **_kwargs: Any) -> list[Any]:
            return []

        def purge_done(self) -> int:
            return 0

        def due_shard(self, **_kwargs: Any) -> list[Any]:
            return []

        _async_fence = False
        _database = None

    class Raising(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.seen = 0

        def emit(self, record: logging.LogRecord) -> None:
            if "metric=gateway_hold_released_expired_total" in record.getMessage():
                self.seen += 1
                raise OSError("the log pipe is gone")

    monkeypatch.setattr(drain, "spanner_settle_outbox", lambda: Outbox())
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
    # A release reaches a person the same day, at most once an hour.
    assert re.findall(r'period   = "(\d+s)"', tf)[0] == "3600s"
    assert re.findall(r'severity = "(\w+)"', tf)[0] == "WARNING"
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
