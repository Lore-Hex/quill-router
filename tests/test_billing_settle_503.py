"""Settle 503s the app marks as designed and retryable do not page at once;
every other settle 503, and every refund 5xx, still does.

The snapshot settle (services/async_settle_handler.py, ``unavailable``)
answers 503 with Retry-After for designed reasons (finish_unconfirmed,
transient_store_error, ...), raised as ExpectedHTTPException; the gateway
retries the same settlement identity until the charge lands. It writes one
line for each, ``async_settle.unavailable reason=...``, for settle and refund
alike. Following the authorize precedent (tests/test_billing_5xx_lease_headroom.py):

* "TR Gateway: billing path 5xx" sets settle 503s aside, and nothing else
  new: refund 503s and every other settle 5xx page there at once;
* "TR Gateway: settle 503 not marked retryable" pages when the settle and
  refund 503s in Cloud Run's request log and the app's marked lines differ;
* "TR Gateway: settle unavailable surge" pages on more than two marked lines
  in ten minutes or more than five in an hour.

The filters and queries are EVALUATED, with the evaluators of the authorize
tests, not grepped.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_billing_5xx_lease_headroom import (
    BUSY_COUNTER,
    CORPUS,
    DEPLOY,
    HEADROOM_COUNTER,
    ORIGINAL_5XX_FILTER,
    RACED_COUNTER,
    SETTLE_503_COUNTER,
    SETTLE_UNAVAILABLE_COUNTER,
    STRICT_COUNTER,
    Promql,
    _alert_5xx_filter,
    _applies,
    _metric_filters,
    _promql,
    _rendered_busy_line,
    _rendered_headroom_line,
    _rendered_raced_line,
    _rendered_strict_line,
    _run_reliability,
    app_line,
    matches,
    request_log,
)
from trusted_router.errors import ExpectedHTTPException
from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.services import async_settle_handler

ROOT = Path(__file__).resolve().parents[1]
ALERT_5XX = DEPLOY / "gateway-alerts" / "billing-5xx.yaml"
ALERT_SETTLE_503 = DEPLOY / "gateway-alerts" / "billing-settle-503.yaml"
ALERT_SURGE = DEPLOY / "gateway-alerts" / "billing-settle-unavailable.yaml"
ALERT_BUSY = DEPLOY / "gateway-alerts" / "billing-rebalance-busy.yaml"
HANDLER = ROOT / "src" / "trusted_router" / "services" / "async_settle_handler.py"
SETTLEMENTS = ROOT / "src" / "trusted_router" / "routes" / "settlements.py"
SERIES = 'monitored_resource="cloud_run_revision"'


def _reasons() -> list[str]:
    """Every reason the app answers a marked 503 with, from its source."""
    found = sorted({
        reason
        for path in (HANDLER, SETTLEMENTS)
        for reason in re.findall(r'unavailable\("([a-z_]+)"\)', path.read_text(encoding="utf-8"))
    })
    assert {"finish_unconfirmed", "transient_store_error", "route_deadline"} <= set(found), found
    return found


def _rendered(reason: str) -> tuple[list[str], Any]:
    """Every line ``unavailable`` writes, as Cloud Run keeps them (the console
    formatter's text), and the exception it returns."""
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = async_settle_handler.logger
    handler = _Keep(logging.INFO)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        error = async_settle_handler.unavailable(reason)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    formatter = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
    return [formatter.format(record) for record in records], error


# ---------------------------------------------------------------------------
# The line and its counter.


@pytest.mark.parametrize("reason", _reasons())
def test_each_marked_503_writes_exactly_one_counted_line(reason: str) -> None:
    lines, error = _rendered(reason)
    # The designed answer: 503, Retry-After, kept out of Sentry.
    assert isinstance(error, ExpectedHTTPException)
    assert error.status_code == 503
    assert (error.headers or {}).get("Retry-After") == "1"
    counter = _metric_filters()[SETTLE_UNAVAILABLE_COUNTER]
    counted = [line for line in lines if matches(counter, app_line(line))]
    # The INFO metric= line beside it is not the mark: one 503, one count.
    assert len(lines) == 2, lines
    assert counted == [
        f"WARNING trusted_router.services.async_settle_handler async_settle.unavailable reason={reason}"
    ]


def test_lines_that_are_not_the_settle_mark_are_not_counted() -> None:
    counter = _metric_filters()[SETTLE_UNAVAILABLE_COUNTER]
    (line,) = [text for text in _rendered("finish_unconfirmed")[0] if text.startswith("WARNING ")]
    assert matches(counter, app_line(line))
    for text in (
        line.replace("WARNING ", "INFO ", 1),
        line.replace("trusted_router.services.async_settle_handler", "trusted_router.other", 1),
        line.replace("async_settle.unavailable ", "async_settle.unavailable_v2 "),
        "INFO trusted_router.services.async_settle_handler metric=async_settle_unavailable "
        "reason=finish_unconfirmed value=1",
        "WARNING trusted_router.main storage.unavailable method=POST route=/internal/gateway/settle",
    ):
        assert not matches(counter, app_line(text)), text
    assert not matches(counter, app_line(line, service="trusted-router-public"))


def test_the_settle_and_authorize_marks_are_counted_apart() -> None:
    """A line counted by the wrong pair would be subtracted from the wrong
    503s: each pair's lines must be its own."""
    filters = _metric_filters()
    trace = {"X-Cloud-Trace-Context": "97947552453ac21dc57168fd64afffe7/99;o=1"}
    authorize_lines = {
        HEADROOM_COUNTER: _rendered_headroom_line(trace)[0],
        BUSY_COUNTER: _rendered_busy_line(trace)[0],
        STRICT_COUNTER: _rendered_strict_line(trace)[0],
        RACED_COUNTER: _rendered_raced_line(trace)[0],
    }
    (settle_line,) = [text for text in _rendered("transient_store_error")[0] if text.startswith("WARNING ")]
    assert not any(matches(filters[name], app_line(settle_line)) for name in authorize_lines)
    for line in authorize_lines.values():
        assert not matches(filters[SETTLE_UNAVAILABLE_COUNTER], app_line(line)), line
    # No line is a request log entry, and no request log entry is a line.
    for path in ("/internal/gateway/settle", "/internal/gateway/refund"):
        assert not matches(filters[SETTLE_UNAVAILABLE_COUNTER], request_log(503, path))
    assert not matches(filters[SETTLE_503_COUNTER], app_line(settle_line))


@pytest.mark.parametrize(
    ("status", "path", "counted"),
    [
        (503, "/internal/gateway/settle", True),
        (503, "/internal/gateway/settle?attempt=2", True),
        (503, "/internal/gateway/refund", True),
        (500, "/internal/gateway/settle", False),
        (502, "/internal/gateway/refund", False),
        (503, "/internal/gateway/authorize", False),
        (503, "/internal/gateway/settle-outbox/drain", False),
        (503, "/internal/gateway/settle/extra", False),
        (200, "/internal/gateway/settle", False),
    ],
)
def test_the_503_counter_counts_settle_and_refund_503s_alone(status: int, path: str, counted: bool) -> None:
    assert matches(_metric_filters()[SETTLE_503_COUNTER], request_log(status, path)) is counted
    assert not matches(_metric_filters()[SETTLE_503_COUNTER], request_log(503, path, service="trusted-router-public"))


# ---------------------------------------------------------------------------
# The immediate policy.


@pytest.mark.parametrize(
    ("status", "path"),
    [
        (503, "/internal/gateway/refund"),
        (503, "/internal/gateway/refund?attempt=2"),
        (500, "/internal/gateway/refund"),
        (500, "/internal/gateway/settle"),
        (502, "/internal/gateway/settle"),
        (504, "/internal/gateway/settle"),
    ],
)
def test_every_refund_5xx_and_every_other_settle_5xx_still_pages_at_once(status: int, path: str) -> None:
    assert matches(_alert_5xx_filter(), request_log(status, path))


def test_the_settle_503_is_set_aside_only_for_the_counted_policy() -> None:
    for path in ("/internal/gateway/settle", "/internal/gateway/settle?attempt=3"):
        entry = request_log(503, path)
        assert not matches(_alert_5xx_filter(), entry)
        assert matches(_metric_filters()[SETTLE_503_COUNTER], entry)


def test_nothing_the_original_filter_paged_on_is_lost() -> None:
    narrowed = _alert_5xx_filter()
    settle = _metric_filters()[SETTLE_503_COUNTER]
    authorize = _metric_filters()["trustedrouter_gateway_authorize_503"]
    for entry in CORPUS:
        if not matches(ORIGINAL_5XX_FILTER, entry):
            assert not matches(narrowed, entry), entry
            continue
        url = entry["httpRequest"]["requestUrl"]
        if entry["httpRequest"]["status"] == 503 and re.search(r"/internal/gateway/settle([?].*)?$", url):
            assert not matches(narrowed, entry) and matches(settle, entry), entry
        elif entry["httpRequest"]["status"] == 503 and re.search(r"/internal/gateway/authorize([?].*)?$", url):
            assert not matches(narrowed, entry) and matches(authorize, entry), entry
        else:
            assert matches(narrowed, entry), entry


@pytest.mark.parametrize(
    "mutant",
    [
        # Refund 503s set aside too: a marked one would balance its line and
        # page nowhere.
        'httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/(settle|refund)([?].*)?$"',
        # Every settle 5xx set aside.
        'httpRequest.requestUrl =~ "/internal/gateway/settle([?].*)?$"',
        # Every 503.
        "httpRequest.status = 503",
    ],
)
def test_the_split_check_kills_a_wider_settle_exclusion(mutant: str) -> None:
    narrowed = _alert_5xx_filter()
    exclusion = 'NOT (httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/settle([?].*)?$")'
    assert narrowed.count(exclusion) == 1
    mutated = narrowed.replace(exclusion, f"NOT ({mutant})")
    must_page_at_once = [
        request_log(503, "/internal/gateway/refund"),
        request_log(500, "/internal/gateway/settle"),
    ]
    assert all(matches(narrowed, entry) for entry in must_page_at_once)
    assert not all(matches(mutated, entry) for entry in must_page_at_once), mutant


# ---------------------------------------------------------------------------
# The counted policy: the 503s against the marked lines.


def _settle_fires(settle_503s: float | None, lines: float | None, *, query: str | None = None) -> bool:
    text = _promql(ALERT_SETTLE_503)["query"] if query is None else query
    return Promql({SETTLE_503_COUNTER: settle_503s, SETTLE_UNAVAILABLE_COUNTER: lines}).evaluate(text) is not None


def test_the_settle_503_policy_compares_the_503s_with_the_marked_lines() -> None:
    promql = _promql(ALERT_SETTLE_503)
    evaluation = Promql({SETTLE_503_COUNTER: 1, SETTLE_UNAVAILABLE_COUNTER: 1})
    evaluation.evaluate(promql["query"])
    assert evaluation.read == [
        (SETTLE_503_COUNTER, SERIES, "10m"),
        (SETTLE_UNAVAILABLE_COUNTER, SERIES, "10m"),
    ]
    # Held as the authorize-503 policy is.
    authorize = _promql(DEPLOY / "gateway-alerts" / "billing-authorize-503.yaml")
    assert promql["duration"] == authorize["duration"] == "180s"
    assert promql["evaluationInterval"] == authorize["evaluationInterval"] == "60s"
    policy = yaml.safe_load(ALERT_SETTLE_503.read_text(encoding="utf-8"))
    # The name is how gateway_reliability.sh finds the live policy to update.
    assert policy["displayName"] == "TR Gateway: settle 503 not marked retryable"
    assert policy["enabled"] is True
    document = " ".join(str(policy["documentation"]["content"]).split())
    for token in (
        "async_settle.unavailable", SETTLE_503_COUNTER, SETTLE_UNAVAILABLE_COUNTER,
        "finish_unconfirmed", "transient_store_error", "TR Gateway: settle unavailable surge",
        "TR Gateway: billing path 5xx", "docs/spanner-reliability.md",
    ):
        assert token in document, token
    doc_5xx = " ".join(str(yaml.safe_load(ALERT_5XX.read_text(encoding="utf-8"))["documentation"]["content"]).split())
    assert "TR Gateway: settle 503 not marked retryable" in doc_5xx
    assert "TR Gateway: settle unavailable surge" in doc_5xx


def test_a_marked_settle_503_does_not_page() -> None:
    assert not _settle_fires(1, 1)
    assert not _settle_fires(None, None)
    # One settlement retried several times, each 503 with its line.
    assert not _settle_fires(4, 4)
    # A marked refund 503 balances here, and pages at once on billing-5xx.
    assert not _settle_fires(1, 1)
    assert matches(_alert_5xx_filter(), request_log(503, "/internal/gateway/refund"))


def test_an_unmarked_settle_503_pages_among_marked_ones() -> None:
    """Spanner unavailable, no instance, a crash, a legacy path's own 503:
    no line."""
    assert _settle_fires(1, None)
    assert _settle_fires(1, 0)
    assert _settle_fires(5, 4)
    assert _settle_fires(101, 100)


def test_lines_without_settle_503s_page() -> None:
    assert _settle_fires(None, 1)
    assert _settle_fires(0, 1)
    assert _settle_fires(3, 5)


@pytest.mark.parametrize("term", [0, 1], ids=["settle-503s", "marked-lines"])
def test_without_or_vector_zero_a_counter_with_no_series_would_silence_the_policy(term: int) -> None:
    query = _promql(ALERT_SETTLE_503)["query"]
    assert query.count(" or vector(0))") == 2
    pieces = query.split(" or vector(0))")
    mutant = "".join(
        piece + (")" if index == term else " or vector(0))") if index < 2 else piece
        for index, piece in enumerate(pieces)
    )
    counters: list[tuple[float | None, float | None]] = [(None, 1), (2, None)]
    assert _settle_fires(*counters[term])
    assert not _settle_fires(*counters[term], query=mutant)


# ---------------------------------------------------------------------------
# The surge policy: the marked lines, bounded.


def _surge_fires(ten_minutes: float | None, hour: float | None, *, query: str | None = None) -> bool:
    assert (hour or 0) >= (ten_minutes or 0)
    text = _promql(ALERT_SURGE)["query"] if query is None else query
    counters: dict[Any, float | None] = {
        (SETTLE_UNAVAILABLE_COUNTER, "10m"): ten_minutes,
        (SETTLE_UNAVAILABLE_COUNTER, "1h"): hour,
    }
    return Promql(counters).evaluate(text) is not None


def test_the_surge_policy_is_the_rebalance_policy_on_the_settle_counter() -> None:
    surge = _promql(ALERT_SURGE)
    busy = _promql(ALERT_BUSY)
    assert surge["query"] == busy["query"].replace(BUSY_COUNTER, SETTLE_UNAVAILABLE_COUNTER)
    assert {k: v for k, v in surge.items() if k != "query"} == {k: v for k, v in busy.items() if k != "query"}
    policy = yaml.safe_load(ALERT_SURGE.read_text(encoding="utf-8"))
    rebalance = yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))
    assert policy["displayName"] == "TR Gateway: settle unavailable surge"
    assert policy["enabled"] is True
    assert policy["combiner"] == "OR"
    assert policy["alertStrategy"] == rebalance["alertStrategy"]
    document = " ".join(str(policy["documentation"]["content"]).split())
    for token in (
        "more than two designed, retryable 503s in ten minutes", "more than five in an hour",
        "async_settle.unavailable", SETTLE_UNAVAILABLE_COUNTER, "finish_unconfirmed",
        "transient_store_error", "docs/spanner-reliability.md",
    ):
        assert token in document, token


def test_the_surge_policy_pages_on_more_than_two_in_ten_minutes_or_five_in_an_hour() -> None:
    assert not _surge_fires(None, None)
    assert not _surge_fires(1, 1)
    assert not _surge_fires(2, 2)
    assert not _surge_fires(2, 5)
    assert not _surge_fires(None, 5)
    assert _surge_fires(3, 3)
    assert _surge_fires(None, 6)
    assert _surge_fires(2, 6)


# ---------------------------------------------------------------------------
# The reconciler applies the settle policies before billing-5xx narrows.


def test_reliability_applies_the_settle_policies_before_the_5xx_filter_narrows(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path)
    assert status == 0, calls
    (authorize,) = _applies(calls, "billing-authorize-503.yaml")
    (surge,) = _applies(calls, "billing-settle-unavailable.yaml")
    (settle,) = _applies(calls, "billing-settle-503.yaml")
    (narrowed,) = _applies(calls, "billing-5xx.yaml")
    assert calls.index(authorize) < calls.index(surge) < calls.index(settle) < calls.index(narrowed)
    creates = [call for call in calls if "logging metrics create" in call]
    first_policy = min(calls.index(authorize), calls.index(surge))
    for counter in (SETTLE_503_COUNTER, SETTLE_UNAVAILABLE_COUNTER):
        (create,) = [call for call in creates if f"logging metrics create {counter} " in call]
        assert calls.index(create) < first_policy


@pytest.mark.parametrize("policy", ["billing-settle-unavailable.yaml", "billing-settle-503.yaml"])
def test_reliability_does_not_narrow_the_5xx_filter_without_the_settle_policies(
    tmp_path: Path, policy: str,
) -> None:
    # Every counter exists, so a refusal is real and stops the run at once.
    status, calls = _run_reliability(tmp_path, metrics_exist=True, fail_policy=policy)
    assert status != 0
    assert len(_applies(calls, policy)) == 1
    assert not _applies(calls, "billing-5xx.yaml")
    # When the counters were created this run, the refusal is retried within
    # the run's budget, and the filter still never narrows.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    status, calls = _run_reliability(fresh, fail_policy=policy, retries=2)
    assert status != 0
    assert len(_applies(calls, policy)) == 3
    assert not _applies(calls, "billing-5xx.yaml")


def test_reliability_waits_for_the_new_settle_counter_before_the_policies_that_read_it(tmp_path: Path) -> None:
    """The first apply after this change: the settle counters are new, and
    Cloud Monitoring refuses the policies naming them for a few seconds."""
    status, calls = _run_reliability(
        tmp_path,
        metrics_exist=True,
        new_metric=SETTLE_UNAVAILABLE_COUNTER,
        missing_policy="TR Gateway: settle unavailable surge",
        not_ready={"billing-settle-unavailable.yaml": 2},
    )
    assert status == 0, calls
    surge = _applies(calls, "billing-settle-unavailable.yaml")
    assert len(surge) == 3
    (settle,) = _applies(calls, "billing-settle-503.yaml")
    (narrowed,) = _applies(calls, "billing-5xx.yaml")
    assert calls.index(surge[-1]) < calls.index(settle) < calls.index(narrowed)
