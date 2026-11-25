"""The billing 5xx alert and deploy gate set aside the four authorize 503s
the app answers by design, and nothing else.

With TR_FASTPATH_LEASE_HEADROOM_503 on, authorize answers 503 (Retry-After: 1)
when a workspace's shortfall is held in fast-path leases. It answers the
storage 503 (Retry-After: 1) when a workspace's credit covers the hold but is
spread across credit shards and the rebalance cooldown is held by its other
requests. It answers the strict-busy 503 (Retry-After: 1) when a key with a
strict budget cannot get its per-process admission slot. It answers the
storage 503 when a workspace's own concurrent requests took its spread
credit from every bounded repair ("credit headroom changed concurrently").
Cloud Run's request log records only status and URL, so each 503 is told
apart by the line the app writes for it, billing.lease_headroom_unavailable,
billing.credit_rebalance_busy, billing.strict_budget_busy or
billing.credit_headroom_raced:

* the deploy gate (scripts/deploy/assert_no_billing_5xx.sh) pairs each
  authorize 503 with a line by trace, and fails on any 503 left over;
* "TR Gateway: billing path 5xx" (gateway-alerts/billing-5xx.yaml) sets
  authorize 503s aside, and "TR Gateway: authorize 503 not lease headroom"
  (gateway-alerts/billing-authorize-503.yaml) pages when the authorize 503
  count and the count of all four lines differ;
* "TR Gateway: credit rebalance busy"
  (gateway-alerts/billing-rebalance-busy.yaml), "TR Gateway: strict budget
  busy" (gateway-alerts/billing-strict-budget-busy.yaml) and "TR Gateway:
  credit headroom raced" (gateway-alerts/billing-headroom-raced.yaml) each
  page on more than two of their line in ten minutes or more than five in
  an hour, which the policy above no longer pages on.

The filters are EVALUATED here against log entries, not grepped: a small
evaluator for the Logging query constructs the filters use, and the gate is
run against a stub gcloud.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from starlette.requests import Request

from trusted_router.main import _ApplicationConsoleFormatter
from trusted_router.routes.internal import gateway

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy"
GATE = DEPLOY / "assert_no_billing_5xx.sh"
BASH = shutil.which("bash") or "/bin/bash"
RELIABILITY = DEPLOY / "gateway_reliability.sh"
ALERT_5XX = DEPLOY / "gateway-alerts" / "billing-5xx.yaml"
ALERT_503 = DEPLOY / "gateway-alerts" / "billing-authorize-503.yaml"
ALERT_BUSY = DEPLOY / "gateway-alerts" / "billing-rebalance-busy.yaml"
ALERT_STRICT = DEPLOY / "gateway-alerts" / "billing-strict-budget-busy.yaml"
ALERT_RACED = DEPLOY / "gateway-alerts" / "billing-headroom-raced.yaml"
HEADROOM_COUNTER = "trustedrouter_gateway_lease_headroom_503"
BUSY_COUNTER = "trustedrouter_gateway_rebalance_busy_503"
STRICT_COUNTER = "trustedrouter_gateway_strict_budget_busy_503"
RACED_COUNTER = "trustedrouter_gateway_headroom_raced_503"
# The settle pair (tests/test_billing_settle_503.py evaluates it): every
# settle and refund 503, and the snapshot settle's async_settle.unavailable
# line.
SETTLE_503_COUNTER = "trustedrouter_gateway_settle_503"
SETTLE_UNAVAILABLE_COUNTER = "trustedrouter_gateway_settle_unavailable_503"
# The two WARNINGs every storage 503 of authorize leaves, here for a
# headroom-raced one, as Cloud Run keeps them. They name the type, not the
# mark, and must never count as it.
RACED_ROUTE_WARNING = (
    "WARNING trusted_router.routes.internal.gateway billing.authorize_storage_unavailable "
    "request_id=req-1 workspace_id=ws-1 error_class=CreditHeadroomRaced"
)
RACED_HANDLER_WARNING = (
    "WARNING trusted_router.main storage.unavailable method=POST route=/internal/gateway/authorize "
    "request_id=req-1 error_class=CreditHeadroomRaced workspace_id=ws-1"
)
# The WARNING the app's handler writes for every strict-busy 503 (main.py,
# _log_storage_503), as Cloud Run keeps it: the same event name as the mark,
# another level and logger, and no trace. It must never count as the mark.
STRICT_HANDLER_WARNING = (
    "WARNING trusted_router.main billing.strict_budget_busy method=POST "
    "route=/internal/gateway/authorize request_id=req-1 error_class=StrictBudgetBusy "
    "workspace_id=ws-1"
)
STRICT_ROUTE_WARNING = (
    "WARNING trusted_router.routes.internal.gateway billing.authorize_strict_budget_busy "
    "request_id=req-1 workspace_id=ws-1 error_class=StrictBudgetBusy"
)

# The billing 5xx alert's filter before lease-headroom 503s existed
# (2026-07-28 to 2026-11-13; the live policy matched it on 2026-11-13).
# Everything it matched must still page, by one policy or the other.
ORIGINAL_5XX_FILTER = (
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = "trusted-router" '
    "AND httpRequest.status >= 500 AND httpRequest.status < 600 AND httpRequest.status != 501 "
    'AND httpRequest.requestUrl =~ "/internal/gateway/(authorize|settle|refund)([?].*)?$"'
)
TRACE = "97947552453ac21dc57168fd64afffe7"
OTHER_TRACE = "2c7d30be713c0ae0afce4767e4c6405a"
THIRD_TRACE = "5b8aa5a2d2c872e8321cf37308d69df2"


# ---------------------------------------------------------------------------
# A Logging query evaluator for the constructs these filters use: AND, NOT,
# parentheses, and comparisons =, !=, <, <=, >, >=, =~ (regex search) and :
# (substring) on dotted field paths. A missing field never matches. OR is
# evaluated only where a caller asks for it (the gate's read of the app's
# two lines); the alert and counter filters do not use it.


_TOKEN = re.compile(
    r'\s*(?:(?P<lp>\()|(?P<rp>\))|(?P<str>"(?:[^"\\]|\\.)*")|(?P<op>=~|!=|>=|<=|=|<|>|:)'
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_.]*|-?\d+))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match and match.end() > position, f"unparsed filter text at {text[position:]!r}"
        kind = match.lastgroup
        assert kind is not None
        out.append((kind, match.group(kind)))
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    return out


def _field(entry: dict[str, Any], path: str) -> Any:
    value: Any = entry
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _compare(actual: Any, op: str, literal: str) -> bool:
    if actual is None:
        return False
    if literal.startswith('"'):
        expected: Any = json.loads(literal)
    else:
        expected = int(literal)
    if op == ":":
        return str(expected) in str(actual)
    if op == "=~":
        return re.search(str(expected), str(actual)) is not None
    if isinstance(expected, int):
        actual = int(actual)
    else:
        actual = str(actual)
    return {
        "=": actual == expected,
        "!=": actual != expected,
        "<": actual < expected,
        "<=": actual <= expected,
        ">": actual > expected,
        ">=": actual >= expected,
    }[op]


def matches(filter_text: str, entry: dict[str, Any], *, allow_or: bool = False) -> bool:
    """Whether a log entry matches a filter. Newlines join terms with AND.
    With allow_or, OR binds tighter than AND, as in the Logging query
    language."""
    tokens = _tokens(filter_text.replace("\n", " AND "))
    position = 0

    def peek() -> tuple[str, str] | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def conjunction() -> bool:
        result = disjunction()
        while peek() == ("word", "AND"):
            take()
            result = disjunction() and result
        return result

    def disjunction() -> bool:
        result = unary()
        while peek() == ("word", "OR"):
            assert allow_or, "OR is not used by these filters"
            take()
            result = unary() or result
        return result

    def unary() -> bool:
        if peek() == ("word", "NOT"):
            take()
            return not unary()
        if peek() == ("lp", "("):
            take()
            result = conjunction()
            assert take() == ("rp", ")")
            return result
        kind, name = take()
        assert kind == "word", (kind, name)
        op_kind, op = take()
        assert op_kind == "op", (op_kind, op)
        _kind, literal = take()
        return _compare(_field(entry, name), op, literal)

    result = conjunction()
    assert position == len(tokens), tokens[position:]
    return result


def request_log(
    status: int,
    path: str,
    *,
    service: str = "trusted-router",
    trace: str | None = TRACE,
    revision: str = "trusted-router-02000-new",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": service,
                "location": "us-central1",
                "revision_name": revision,
            },
        },
        "timestamp": "2026-11-13T13:04:15.208934Z",
        "httpRequest": {
            "status": status,
            "requestUrl": f"https://trusted-router-vsjf6qu4la-uc.a.run.app{path}",
        },
    }
    if trace is not None:
        entry["trace"] = f"projects/quill-cloud-proxy/traces/{trace}"
    return entry


def app_line(text: str, *, service: str = "trusted-router") -> dict[str, Any]:
    return {
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": service}},
        "timestamp": "2026-11-13T13:04:15.233651Z",
        "textPayload": text,
    }


def _alert_5xx_filter() -> str:
    policy = yaml.safe_load(ALERT_5XX.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    return str(condition["conditionMatchedLog"]["filter"])


def _metric_filters() -> dict[str, str]:
    """The log-metric filters gateway_reliability.sh reconciles, by name."""
    script = RELIABILITY.read_text(encoding="utf-8")
    names = dict(re.findall(r'^([A-Z_0-9]+_METRIC_NAME)="([a-z_0-9]+)"$', script, re.M))
    found = re.findall(
        r'ensure_log_metric "\$([A-Z_0-9]+)" \\\n\s+"[^"\n]*" \\\n\s+\'([^\'\n]*)\'', script
    )
    filters = {names[variable]: text for variable, text in found}
    assert set(filters) == {
        "trustedrouter_gateway_billing_slow",
        "trustedrouter_gateway_authorize_503",
        HEADROOM_COUNTER,
        BUSY_COUNTER,
        STRICT_COUNTER,
        RACED_COUNTER,
        SETTLE_503_COUNTER,
        SETTLE_UNAVAILABLE_COUNTER,
    }, filters
    return filters


STATUSES = (500, 501, 502, 503, 504, 599)
PATHS = (
    "/internal/gateway/authorize",
    "/internal/gateway/authorize?attempt=2",
    "/v1/internal/gateway/authorize",
    "/internal/gateway/settle",
    "/internal/gateway/refund",
    "/internal/gateway/heartbeat",
    "/internal/gateway/authorize/extra",
    "/v1/chat/completions",
)
CORPUS = [
    request_log(status, path, service=service)
    for status in (*STATUSES, 200, 402, 429)
    for path in PATHS
    for service in ("trusted-router", "trusted-router-public")
]


def _rendered_headroom_line(trace_header: dict[str, str] | None) -> tuple[str, str]:
    """The line _lease_headroom_error writes for a request, as Cloud Run keeps
    it (the console formatter's text), and the trace it was given."""
    headers = [(name.lower().encode(), value.encode()) for name, value in (trace_header or {}).items()]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers})
    trace_id = gateway._cloud_trace_id(request)
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        error = gateway._lease_headroom_error(
            SimpleNamespace(id="ws-1"), reservation=82_960, estimate=82_960,
            key_id="key-hash", request_id="req-1", trace_id=trace_id,
        )
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    assert getattr(error, "status_code", None) == 503
    (record,) = records
    formatter = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
    return formatter.format(record), trace_id


def _rendered_busy_line(trace_header: dict[str, str] | None) -> tuple[str, str]:
    """The line _log_credit_rebalance_busy writes for a request, as Cloud Run
    keeps it, and the trace it was given."""
    headers = [(name.lower().encode(), value.encode()) for name, value in (trace_header or {}).items()]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers})
    trace_id = gateway._cloud_trace_id(request)
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        gateway._log_credit_rebalance_busy("ws-1", request_id="req-1", trace_id=trace_id)
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    formatter = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
    return formatter.format(record), trace_id


def _rendered_strict_line(trace_header: dict[str, str] | None) -> tuple[str, str]:
    """The line _log_strict_budget_busy writes for a request, as Cloud Run
    keeps it, and the trace it was given."""
    headers = [(name.lower().encode(), value.encode()) for name, value in (trace_header or {}).items()]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers})
    trace_id = gateway._cloud_trace_id(request)
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        gateway._log_strict_budget_busy("ws-1", request_id="req-1", trace_id=trace_id)
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    formatter = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
    return formatter.format(record), trace_id


def _rendered_raced_line(trace_header: dict[str, str] | None) -> tuple[str, str]:
    """The line _log_credit_headroom_raced writes for a request, as Cloud
    Run keeps it, and the trace it was given."""
    headers = [(name.lower().encode(), value.encode()) for name, value in (trace_header or {}).items()]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers})
    trace_id = gateway._cloud_trace_id(request)
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        gateway._log_credit_headroom_raced("ws-1", request_id="req-1", trace_id=trace_id)
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    formatter = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
    return formatter.format(record), trace_id


# ---------------------------------------------------------------------------
# The app's lines.


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Cloud-Trace-Context": f"{TRACE.upper()}/1234567890;o=1"}, TRACE),
        ({"X-Cloud-Trace-Context": TRACE}, TRACE),
        ({"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"}, TRACE),
        (
            {"X-Cloud-Trace-Context": f"{OTHER_TRACE}/1;o=1", "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"},
            OTHER_TRACE,
        ),
        ({"X-Cloud-Trace-Context": "not-a-trace/1;o=1"}, "-"),
        ({"X-Cloud-Trace-Context": f"{TRACE}0/1"}, "-"),
        ({"X-Cloud-Trace-Context": "0" * 32 + "/1"}, "-"),
        ({}, "-"),
    ],
)
def test_cloud_trace_id_reads_cloud_runs_header(headers: dict[str, str], expected: str) -> None:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": raw})
    assert gateway._cloud_trace_id(request) == expected


def test_the_headroom_line_is_what_the_counter_and_the_gate_read() -> None:
    from scripts.deploy import billing_5xx_lease_headroom as pairing

    line, trace_id = _rendered_headroom_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    assert trace_id == TRACE
    assert line.startswith(pairing.LINE_PREFIX)
    assert line.endswith(f" trace={TRACE}")
    counter = _metric_filters()["trustedrouter_gateway_lease_headroom_503"]
    assert matches(counter, app_line(line))
    # The gate pairs it with the 503's request log entry.
    request = request_log(503, "/internal/gateway/authorize")
    assert pairing.line_trace(app_line(line)) == pairing.request_trace(request) == TRACE
    assert pairing.unpaired([request], [app_line(line)]) == []


def test_lines_that_are_not_lease_headroom_are_not_counted() -> None:
    counter = _metric_filters()["trustedrouter_gateway_lease_headroom_503"]
    line, _trace = _rendered_headroom_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    for text in (
        line.replace("billing.lease_headroom_unavailable", "billing.insufficient_credits"),
        line.replace("INFO ", "WARNING ", 1),
        line.replace("trusted_router.routes.internal.gateway", "trusted_router.other", 1),
        line.replace("billing.lease_headroom_unavailable ", "billing.lease_headroom_unavailable_v2 "),
        "INFO trusted_router.routes.internal.gateway gateway.idempotency_key_source source=gateway",
    ):
        assert not matches(counter, app_line(text)), text
    # Another service's lines are not this service's 503s.
    assert not matches(counter, app_line(line, service="trusted-router-public"))


def test_the_busy_line_is_what_its_counter_and_the_gate_read() -> None:
    from scripts.deploy import billing_5xx_lease_headroom as pairing

    line, trace_id = _rendered_busy_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    assert trace_id == TRACE
    assert line == (
        "INFO trusted_router.routes.internal.gateway billing.credit_rebalance_busy "
        f"workspace_id=ws-1 request_id=req-1 trace={TRACE}"
    )
    assert line.startswith(pairing.REBALANCE_BUSY_LINE_PREFIX)
    assert matches(_metric_filters()[BUSY_COUNTER], app_line(line))
    # The gate pairs it with the 503's request log entry.
    request = request_log(503, "/internal/gateway/authorize")
    assert pairing.line_kind(app_line(line)) == "rebalance busy"
    assert pairing.line_trace(app_line(line)) == pairing.request_trace(request) == TRACE
    assert pairing.unpaired([request], [app_line(line)]) == []


def test_lines_that_are_not_rebalance_busy_are_not_counted() -> None:
    counter = _metric_filters()[BUSY_COUNTER]
    line, _trace = _rendered_busy_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    for text in (
        # The lines every storage 503 of authorize leaves, this one included.
        "WARNING trusted_router.routes.internal.gateway billing.authorize_storage_unavailable "
        "request_id=req-1 workspace_id=ws-1 error_class=CreditRebalanceBusy",
        "WARNING trusted_router.main storage.unavailable method=POST route=/internal/gateway/authorize "
        "request_id=req-1 error_class=CreditRebalanceBusy workspace_id=ws-1",
        "WARNING trusted_router.storage_gcp credit rebalance cooldown exhausted workspace=ws-1 attempts=3",
        line.replace("INFO ", "WARNING ", 1),
        line.replace("trusted_router.routes.internal.gateway", "trusted_router.other", 1),
        line.replace("billing.credit_rebalance_busy ", "billing.credit_rebalance_busy_v2 "),
        "INFO trusted_router.routes.internal.gateway gateway.idempotency_key_source source=gateway",
    ):
        assert not matches(counter, app_line(text)), text
    assert not matches(counter, app_line(line, service="trusted-router-public"))


def test_the_strict_line_is_what_its_counter_and_the_gate_read() -> None:
    from scripts.deploy import billing_5xx_lease_headroom as pairing

    line, trace_id = _rendered_strict_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    assert trace_id == TRACE
    assert line == (
        "INFO trusted_router.routes.internal.gateway billing.strict_budget_busy "
        f"workspace_id=ws-1 request_id=req-1 trace={TRACE}"
    )
    assert line.startswith(pairing.STRICT_BUDGET_BUSY_LINE_PREFIX)
    assert matches(_metric_filters()[STRICT_COUNTER], app_line(line))
    # The gate pairs it with the 503's request log entry.
    request = request_log(503, "/internal/gateway/authorize")
    assert pairing.line_kind(app_line(line)) == "strict budget busy"
    assert pairing.line_trace(app_line(line)) == pairing.request_trace(request) == TRACE
    assert pairing.unpaired([request], [app_line(line)]) == []


def test_lines_that_are_not_the_strict_mark_are_not_counted() -> None:
    """The handler's WARNING for the same 503 shares the mark's event name.
    Counted too, every strict-busy 503 would be subtracted twice and page as
    more lines than 503s."""
    counter = _metric_filters()[STRICT_COUNTER]
    line, _trace = _rendered_strict_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    for text in (
        # The two warnings every strict-busy 503 of authorize also leaves.
        STRICT_HANDLER_WARNING,
        STRICT_ROUTE_WARNING,
        # The handler's warning with a trace on it is still not the mark.
        f"{STRICT_HANDLER_WARNING} trace={TRACE}",
        line.replace("INFO ", "WARNING ", 1),
        line.replace("trusted_router.routes.internal.gateway", "trusted_router.main", 1),
        line.replace("billing.strict_budget_busy ", "billing.strict_budget_busy_v2 "),
        line.replace("billing.strict_budget_busy ", "billing.authorize_strict_budget_busy "),
        "INFO trusted_router.routes.internal.gateway gateway.idempotency_key_source source=gateway",
    ):
        assert not matches(counter, app_line(text)), text
    assert not matches(counter, app_line(line, service="trusted-router-public"))


def test_the_raced_line_is_what_its_counter_and_the_gate_read() -> None:
    from scripts.deploy import billing_5xx_lease_headroom as pairing

    line, trace_id = _rendered_raced_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    assert trace_id == TRACE
    assert line == (
        "INFO trusted_router.routes.internal.gateway billing.credit_headroom_raced "
        f"workspace_id=ws-1 request_id=req-1 trace={TRACE}"
    )
    assert line.startswith(pairing.HEADROOM_RACED_LINE_PREFIX)
    assert matches(_metric_filters()[RACED_COUNTER], app_line(line))
    # The gate pairs it with the 503's request log entry.
    request = request_log(503, "/internal/gateway/authorize")
    assert pairing.line_kind(app_line(line)) == "headroom raced"
    assert pairing.line_trace(app_line(line)) == pairing.request_trace(request) == TRACE
    assert pairing.unpaired([request], [app_line(line)]) == []


def test_lines_that_are_not_the_raced_mark_are_not_counted() -> None:
    """The warnings every storage 503 of authorize leaves name the type, and
    the store's own lines name the race; none of them is the mark. Counted
    too, a headroom-raced 503 would be subtracted twice and page."""
    counter = _metric_filters()[RACED_COUNTER]
    line, _trace = _rendered_raced_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    for text in (
        RACED_ROUTE_WARNING,
        RACED_HANDLER_WARNING,
        f"{RACED_ROUTE_WARNING} trace={TRACE}",
        "INFO trusted_router.storage_gcp credit rebalance workspace=ws-1 outcome=moved "
        "moved_micro=1 mode=consolidate estimate=1 attempt=3",
        "WARNING trusted_router.storage_gcp credit shard-count forced reload failed after "
        "incomplete precheck workspace=ws-1",
        line.replace("INFO ", "WARNING ", 1),
        line.replace("trusted_router.routes.internal.gateway", "trusted_router.main", 1),
        line.replace("billing.credit_headroom_raced ", "billing.credit_headroom_raced_v2 "),
        line.replace("billing.credit_headroom_raced ", "billing.credit_rebalance_busy "),
        "INFO trusted_router.routes.internal.gateway gateway.idempotency_key_source source=gateway",
    ):
        assert not matches(counter, app_line(text)), text
    assert not matches(counter, app_line(line, service="trusted-router-public"))


def test_each_line_is_counted_by_its_own_counter_alone() -> None:
    """The policy adds the four counters: a line counted by two would be
    subtracted twice and page, a line counted by none would leave its 503
    unmarked."""
    filters = _metric_filters()
    trace = {"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"}
    lines = {
        HEADROOM_COUNTER: _rendered_headroom_line(trace)[0],
        BUSY_COUNTER: _rendered_busy_line(trace)[0],
        STRICT_COUNTER: _rendered_strict_line(trace)[0],
        RACED_COUNTER: _rendered_raced_line(trace)[0],
    }
    for owner, line in lines.items():
        counted = {name for name in lines if matches(filters[name], app_line(line))}
        assert counted == {owner}, (line, counted)
    # The other lines of a strict-busy or a headroom-raced 503 are counted by
    # none of them.
    for text in (STRICT_HANDLER_WARNING, STRICT_ROUTE_WARNING, RACED_ROUTE_WARNING, RACED_HANDLER_WARNING):
        assert not any(matches(filters[name], app_line(text)) for name in lines), text
    # No line is a request log entry, and no request log entry is a line.
    request = request_log(503, "/internal/gateway/authorize")
    for line in lines.values():
        assert not matches(filters["trustedrouter_gateway_authorize_503"], app_line(line))
    for name in lines:
        assert not matches(filters[name], request)


# ---------------------------------------------------------------------------
# The alert: the two policies partition what the original matched.


def _authorize_503_counter() -> str:
    return _metric_filters()["trustedrouter_gateway_authorize_503"]


def _settle_503_counter() -> str:
    return _metric_filters()[SETTLE_503_COUNTER]


def test_the_5xx_alert_and_the_authorize_503_counter_split_the_original_exactly() -> None:
    narrowed = _alert_5xx_filter()
    counter = _authorize_503_counter()
    settle = _settle_503_counter()
    for entry in CORPUS:
        original = matches(ORIGINAL_5XX_FILTER, entry)
        immediate = matches(narrowed, entry)
        counted = matches(counter, entry)
        settle_counted = matches(settle, entry)
        # Everything the original paged on still pages, by the immediate
        # policy or a counted one.
        assert original == (immediate or counted or settle_counted), entry
        # An authorize 503 is set aside for its counted policy alone.
        assert not (immediate and counted), entry
        assert not (counted and settle_counted), entry
        # A settle 503 likewise; a refund 503 is counted by the settle pair
        # (its line is the same) and still pages at once.
        if settle_counted:
            assert immediate == ("/refund" in entry["httpRequest"]["requestUrl"]), entry
    # The corpus reaches every side.
    assert any(matches(counter, entry) for entry in CORPUS)
    assert any(matches(settle, entry) and not matches(narrowed, entry) for entry in CORPUS)
    assert any(matches(narrowed, entry) for entry in CORPUS)


@pytest.mark.parametrize(
    ("status", "path"),
    [
        (502, "/internal/gateway/settle"),
        (504, "/internal/gateway/settle"),
        (503, "/internal/gateway/refund"),
        (500, "/internal/gateway/refund"),
        (500, "/internal/gateway/authorize"),
        (502, "/internal/gateway/authorize"),
        (504, "/internal/gateway/authorize"),
        (500, "/internal/gateway/settle"),
    ],
)
def test_every_other_billing_5xx_still_pages_at_once(status: int, path: str) -> None:
    assert matches(_alert_5xx_filter(), request_log(status, path))


def test_the_authorize_503_is_set_aside_only_for_the_counted_policy() -> None:
    entry = request_log(503, "/internal/gateway/authorize")
    assert not matches(_alert_5xx_filter(), entry)
    assert matches(_authorize_503_counter(), entry)


@pytest.mark.parametrize(
    "mutant",
    [
        # Every 503 set aside: settle and refund 503s would page nowhere.
        'httpRequest.status = 503',
        # Every authorize 5xx set aside.
        'httpRequest.requestUrl =~ "/internal/gateway/authorize([?].*)?$"',
        # A looser path: settle 503s caught by a wider pattern.
        'httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/"',
    ],
)
def test_the_partition_check_kills_a_wider_exclusion(mutant: str) -> None:
    narrowed = _alert_5xx_filter()
    exclusion = 'NOT (httpRequest.status = 503 AND httpRequest.requestUrl =~ "/internal/gateway/authorize([?].*)?$")'
    assert narrowed.endswith(f" AND {exclusion}")
    mutated = narrowed.replace(exclusion, f"NOT ({mutant})")
    counter = _authorize_503_counter()
    settle = _settle_503_counter()

    def counted_elsewhere(entry: dict[str, Any]) -> bool:
        # The settle pair takes over settle 503s only: it counts refund 503s
        # too, but balances a marked one against its line, so a refund 503
        # must still page at once.
        return matches(counter, entry) or (
            matches(settle, entry) and "/refund" not in entry["httpRequest"]["requestUrl"]
        )

    # The narrowed filter as it is loses nothing; each mutant loses entries
    # that no counted policy takes over.
    for candidate, expect_lost in ((narrowed, False), (mutated, True)):
        lost = [
            entry for entry in CORPUS
            if matches(ORIGINAL_5XX_FILTER, entry)
            and not (matches(candidate, entry) or counted_elsewhere(entry))
        ]
        assert bool(lost) == expect_lost, (mutant, lost[:1])


# The PromQL conditions, EVALUATED from the policies' own query text with
# PromQL's instant-vector semantics, for the constructs these queries use:
# sum(increase(METRIC{...}[WINDOW])), vector(N), abs(), "or", "-", "+", ">"
# and parentheses, with PromQL's precedence ("or" below ">" below "-"). Every
# vector here has at most one series, with no labels, as sum() without "by"
# and vector() give: None is a vector with no series, which a counter that
# has no log line in the window is. What Cloud Monitoring's own evaluator
# gave for the same text is pinned below
# (test_the_evaluator_gives_what_cloud_monitoring_gave).

_PROMQL_TOKEN = re.compile(
    r"\s*(?:(?P<duration>\d+[smhd])(?![\w.])|(?P<number>\d+(?:\.\d+)?)(?![\w.])"
    r'|(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)|(?P<string>"[^"]*")|(?P<op>[()\[\]{}=>+-]))'
)


@dataclass
class Promql:
    """One evaluation of a query over the counters' increases. A counter is
    keyed by its name, for every window, or by (name, window)."""

    counters: dict[Any, float | None]
    read: list[tuple[str, str, str]] = field(default_factory=list)

    def evaluate(self, query: str) -> float | None:
        """The query's one sample, or None when it returns no series (an
        alert condition fires on a returned series, never on none)."""
        self._tokens = [
            (match.lastgroup or "", match.group(match.lastgroup or ""))
            for match in _PROMQL_TOKEN.finditer(query)
        ]
        assert "".join(text for _kind, text in self._tokens) == re.sub(r"\s+", "", query), query
        self._at = 0
        value = self._or()
        assert self._at == len(self._tokens), self._tokens[self._at:]
        assert not isinstance(value, _Scalar), "a scalar is not an alert condition"
        return value

    def _peek(self) -> str | None:
        return self._tokens[self._at][1] if self._at < len(self._tokens) else None

    def _take(self, expected: str | None = None) -> str:
        _kind, text = self._tokens[self._at]
        assert expected is None or text == expected, (expected, text)
        self._at += 1
        return text

    def _or(self) -> Any:
        left = self._comparison()
        while self._peek() == "or":
            self._take()
            right = self._comparison()
            assert not isinstance(left, _Scalar) and not isinstance(right, _Scalar)
            # Both sides carry no labels: the right adds its sample only
            # when the left has none.
            left = right if left is None else left
        return left

    def _comparison(self) -> Any:
        left = self._difference()
        while self._peek() == ">":
            self._take()
            right = self._difference()
            # A filter, as without "bool": the sample stays when it holds.
            assert isinstance(right, _Scalar) and not isinstance(left, _Scalar)
            left = left if left is not None and left > right.value else None
        return left

    def _difference(self) -> Any:
        left = self._primary()
        while self._peek() in ("-", "+"):
            sign = -1 if self._take() == "-" else 1
            right = self._primary()
            assert not isinstance(left, _Scalar) and not isinstance(right, _Scalar)
            # One-to-one matching: a side with no series leaves no result.
            left = None if left is None or right is None else left + sign * right
        return left

    def _primary(self) -> Any:
        word = self._take()
        if word == "(":
            value = self._or()
            self._take(")")
            return value
        if word == "abs":
            self._take("(")
            value = self._or()
            self._take(")")
            return None if value is None else abs(value)
        if word == "sum":
            self._take("(")
            value = self._or()
            self._take(")")
            return value
        if word == "vector":
            self._take("(")
            value = float(self._take())
            self._take(")")
            return value
        if word == "increase":
            self._take("(")
            metric = self._take()
            self._take("{")
            label = self._take()
            self._take("=")
            matcher = f"{label}={self._take()}"
            self._take("}")
            self._take("[")
            window = self._take()
            self._take("]")
            self._take(")")
            prefix = "logging_googleapis_com:user_"
            assert metric.startswith(prefix), metric
            name = metric.removeprefix(prefix)
            self.read.append((name, matcher, window))
            if (name, window) in self.counters:
                return self.counters[(name, window)]
            return self.counters[name]
        return _Scalar(float(word))


@dataclass(frozen=True)
class _Scalar:
    value: float


AUTHORIZE_503_COUNTER = "trustedrouter_gateway_authorize_503"


def _promql(path: Path) -> dict[str, Any]:
    policy = yaml.safe_load(path.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    return dict(condition["conditionPrometheusQueryLanguage"])


def _evaluate(
    query: str,
    authorize_503s: float | None,
    headroom_lines: float | None,
    busy_lines: float | None,
    strict_lines: float | None = None,
    raced_lines: float | None = None,
) -> tuple[float | None, list[tuple[str, str, str]]]:
    promql = Promql({
        AUTHORIZE_503_COUNTER: authorize_503s,
        HEADROOM_COUNTER: headroom_lines,
        BUSY_COUNTER: busy_lines,
        STRICT_COUNTER: strict_lines,
        RACED_COUNTER: raced_lines,
    })
    return promql.evaluate(query), promql.read


def _fires(
    authorize_503s: float | None,
    headroom_lines: float | None,
    busy_lines: float | None = None,
    strict_lines: float | None = None,
    raced_lines: float | None = None,
    *,
    query: str | None = None,
) -> bool:
    """Whether the authorize-503 policy's condition returns a series."""
    text = _promql(ALERT_503)["query"] if query is None else query
    return _evaluate(text, authorize_503s, headroom_lines, busy_lines, strict_lines, raced_lines)[0] is not None


def test_the_authorize_503_policy_compares_the_503s_with_every_kind_of_line() -> None:
    promql = _promql(ALERT_503)
    _value, read = _evaluate(promql["query"], 1, 1, 1, 1, 1)
    # Each counter once, the same series and the same window.
    assert read == [
        (AUTHORIZE_503_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (HEADROOM_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (BUSY_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (STRICT_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (RACED_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
    ]
    # The 503s less every kind of line, to the half that tells 0 from 1.
    assert _evaluate(promql["query"], 7, 2, 4)[0] == 1.0
    assert _evaluate(promql["query"], 6, 2, 4)[0] is None
    assert _evaluate(promql["query"], 6.4, 2, 4)[0] is None
    assert _evaluate(promql["query"], 6.6, 2, 4)[0] == pytest.approx(0.6)
    assert _evaluate(promql["query"], 10, 2, 4, 3)[0] == 1.0
    assert _evaluate(promql["query"], 9, 2, 4, 3)[0] is None
    assert _evaluate(promql["query"], 12, 2, 4, 3, 2)[0] == 1.0
    assert _evaluate(promql["query"], 11, 2, 4, 3, 2)[0] is None
    assert promql["duration"] == "180s"
    assert promql["evaluationInterval"] == "60s"
    policy = yaml.safe_load(ALERT_503.read_text(encoding="utf-8"))
    # The name is how gateway_reliability.sh finds the live policy to
    # update: under another name it would be left as it is, still paging.
    assert policy["displayName"] == "TR Gateway: authorize 503 not lease headroom"
    assert policy["enabled"] is True
    assert "TR Gateway: authorize 503 not lease headroom" in _alert_5xx_doc()
    for document in (str(policy["documentation"]["content"]), _alert_5xx_doc()):
        assert "TR Gateway: credit rebalance busy" in " ".join(document.split())
        assert "TR Gateway: strict budget busy" in " ".join(document.split())
        assert "TR Gateway: credit headroom raced" in " ".join(document.split())
    for token in (
        "billing.lease_headroom_unavailable", HEADROOM_COUNTER,
        "billing.credit_rebalance_busy", BUSY_COUNTER,
        "billing.strict_budget_busy", STRICT_COUNTER,
        "billing.credit_headroom_raced", RACED_COUNTER,
        "scripts/deploy/assert_no_billing_5xx.sh", "docs/spanner-reliability.md",
    ):
        assert token in policy["documentation"]["content"], token


def _alert_5xx_doc() -> str:
    return str(yaml.safe_load(ALERT_5XX.read_text(encoding="utf-8"))["documentation"]["content"])


def test_a_non_lease_authorize_503_trips_the_authorize_503_policy() -> None:
    entry = request_log(503, "/internal/gateway/authorize")
    counter_a = _authorize_503_counter()
    counter_b = _metric_filters()[HEADROOM_COUNTER]
    counter_c = _metric_filters()[BUSY_COUNTER]
    # A real 503: the request log counts, and no line is written.
    assert matches(counter_a, entry) and not matches(counter_b, entry)
    assert not matches(counter_c, entry)
    assert _fires(1, None)
    # Among many lease-headroom 503s, one real 503 still pages.
    assert _fires(101, 100)
    # Lease-headroom 503s, each with its line, do not.
    assert not _fires(100, 100)
    assert not _fires(None, None)
    # Lines with no 503 behind them could hide real ones: they page too.
    assert _fires(None, 1)
    assert _fires(5, 7)


def test_a_rebalance_busy_503_with_its_line_does_not_page() -> None:
    # The 2026-11-16 page: one authorize 503, and now its line.
    assert not _fires(1, None, 1)
    assert not _fires(1, 0, 1)
    # The same 503 before the app wrote the line: it paged, and still would.
    assert _fires(1, None, None)
    # One request's three attempts, all answered busy.
    assert not _fires(3, None, 3)
    # Both kinds in one window, each 503 with its line.
    assert not _fires(5, 2, 3)
    assert not _fires(100, 60, 40)


def test_an_unmarked_503_pages_among_marked_ones() -> None:
    """A storage outage, no instance, a crash: no line of either kind."""
    assert _fires(1, None, None)
    assert _fires(2, None, 1)
    assert _fires(2, 1, None)
    assert _fires(6, 2, 3)
    assert _fires(101, 60, 40)
    # "credit headroom changed concurrently" from a recovery that stopped
    # early (its forced shard-count reload failed) writes no line of these
    # four: it pages.
    assert _fires(4, 1, 2)
    assert _fires(5, 1, 2, 1)
    assert _fires(2, None, None, 1)
    assert _fires(2, None, None, None, 1)
    assert _fires(6, 1, 2, 1, 1)


def test_a_strict_budget_busy_503_with_its_line_does_not_page() -> None:
    # The 2026-11-17 page: one authorize 503, and now its line.
    assert not _fires(1, None, None, 1)
    assert not _fires(1, 0, 0, 1)
    # The same 503 before the app wrote the line: it paged, and still would.
    assert _fires(1, None, None, None)
    # One request's three attempts, all answered busy.
    assert not _fires(3, None, None, 3)
    # Every kind in one window, each 503 with its line.
    assert not _fires(6, 2, 3, 1)
    assert not _fires(100, 50, 30, 20)
    # A strict line with no 503 behind it pages, as any other line.
    assert _fires(None, None, None, 1)
    assert _fires(1, 1, None, 1)


def test_lines_without_503s_page() -> None:
    """A line written for a request that was not answered 503 could hide a
    real 503: more lines than 503s is a defect, of either kind."""
    assert _fires(None, None, 1)
    assert _fires(None, 1, None)
    assert _fires(0, None, 1)
    assert _fires(1, 1, 1)
    assert _fires(5, 3, 3)


AUTHORIZE_503_TERMS = 5  # the 503s, and the four kinds of line


def _without_or_zero(query: str, term: int) -> str:
    """The query with one counter's "or vector(0)" taken out."""
    assert query.count(" or vector(0))") == AUTHORIZE_503_TERMS
    pieces = query.split(" or vector(0))")
    mutant = "".join(
        piece + (")" if index == term else " or vector(0))") if index < AUTHORIZE_503_TERMS else piece
        for index, piece in enumerate(pieces)
    )
    assert mutant.count(" or vector(0))") == AUTHORIZE_503_TERMS - 1
    return mutant


def test_the_evaluator_gives_what_cloud_monitoring_gave() -> None:
    """The evaluator above is a model. These are the results Cloud
    Monitoring's PromQL API gave for the same text on 2026-11-16, as
    read-only instant queries over the live counters: the incident's one
    authorize 503 in the window, no lease-headroom line ever written, and
    the rebalance-busy counter not yet in existence, so the second and
    third counters had no series. The model must give the same."""
    query = _promql(ALERT_503)["query"]
    bare = query.rsplit(" > ", 1)[0]
    # The incident's window: the unmarked 503 is returned, with value 1.
    assert _evaluate(query, 1, None, None)[0] == 1.0
    # A quiet window: the condition returns no series, and the expression
    # without its comparison returns 0, a value and not nothing.
    assert _evaluate(query, None, None, None)[0] is None
    assert _evaluate(bare, None, None, None)[0] == 0.0
    # The third counter without "or vector(0)": no series in the incident's
    # window, which would have silenced that 503.
    assert _evaluate(_without_or_zero(query, 2), 1, None, None)[0] is None
    # The third counter given the 503s' own series, as a marked 503 gives
    # it: the difference is 0 and the condition returns no series.
    assert _evaluate(bare, 1, None, 1)[0] == 0.0
    assert _evaluate(query, 1, None, 1)[0] is None
    # The rate policy's condition, as written, on the counter that did not
    # exist, and on a quiet day: no error and no series.
    assert _busy_fires(None, None) == set()
    # Their shapes on the 503 counter, which had the incident's one event:
    # increase[10m] and increase[1h] both gave exactly 1 six minutes after
    # it, increase[1h] still gave 1 at 42 and at 59 minutes while
    # increase[10m] gave no series, and neither gave any at 67 minutes.
    # With one event neither comparison (> 2.5, > 5.5) returned a series.
    assert _increases(1, 1) == {"10m": 1.0, "1h": 1.0}
    assert _increases(None, 1) == {"10m": None, "1h": 1.0}
    assert _increases(None, None) == {"10m": None, "1h": None}
    assert _busy_fires(1, 1) == set()
    assert _busy_fires(None, 1) == set()
    # The condition's own shape, "(count[10m] > a) or (count[1h] > b)", on
    # that counter with bounds lowered to 0.5 where noted: the ten-minute
    # side met gave one series, its count; the hour side met alone, the
    # ten-minute side having no series, gave one series, the hour's count;
    # both met gave one series still; with "and" for "or" and the hour side
    # met alone, none.
    query = _busy_condition()["query"]
    lowered_fast = query.replace("> 2.5", "> 0.5")
    lowered_slow = query.replace("> 5.5", "> 0.5")
    lowered_both = lowered_fast.replace("> 5.5", "> 0.5")
    assert Promql(_busy_counters(1, 1)).evaluate(lowered_fast) == 1.0
    assert Promql(_busy_counters(None, 1)).evaluate(lowered_slow) == 1.0
    assert Promql(_busy_counters(1, 1)).evaluate(lowered_both) == 1.0
    assert Promql(_busy_counters(None, None)).evaluate(lowered_both) is None
    assert _promql_and(re.sub(r"\n(\s*)or(\s*)\n", r"\n\1and\2\n", lowered_slow), None, 1) is None


@pytest.mark.parametrize(
    "term",
    [0, 1, 2, 3, 4],
    ids=["authorize-503s", "lease-headroom", "rebalance-busy", "strict-budget-busy", "headroom-raced"],
)
def test_without_or_vector_zero_a_counter_with_no_series_would_silence_the_policy(term: int) -> None:
    """The mutants each "or vector(0)" kills, made in the query's own text:
    a counter with no log line in the window has no series, and a
    difference with it has none either."""
    query = _promql(ALERT_503)["query"]
    mutant = _without_or_zero(query, term)
    # Cases that must page, each with the mutated term's counter absent.
    absent: list[tuple[float | None, float | None, float | None, float | None, float | None]] = [
        (None, 1, 1, 1, 1),   # lines and no 503s
        (4, None, 1, 1, 1),   # unmarked 503s, no lease-headroom line (the usual state)
        (4, 1, None, 1, 1),   # unmarked 503s, no rebalance-busy line (the usual state)
        (4, 1, 1, None, 1),   # unmarked 503s, no strict-budget-busy line (the usual state)
        (4, 1, 1, 1, None),   # unmarked 503s, no headroom-raced line (the usual state)
    ]
    counters = absent[term]
    assert _fires(*counters)
    assert not _fires(*counters, query=mutant)


def _with_plus_for_minus(query: str, which: int) -> str:
    """The query with the sign before one kind of line flipped."""
    lines = query.splitlines()
    minus = [index for index, line in enumerate(lines) if line.strip() == "-"]
    assert len(minus) == AUTHORIZE_503_TERMS - 1
    lines[minus[which]] = lines[minus[which]].replace("-", "+")
    return "\n".join(lines)


def test_the_query_takes_the_rebalance_busy_lines_off_the_503s() -> None:
    """The mutant with the wrong sign on the third counter, made in the
    query's own text: a marked 503 would page."""
    mutant = _with_plus_for_minus(_promql(ALERT_503)["query"], 1)
    assert not _fires(1, None, 1)
    assert _fires(1, None, 1, query=mutant)


def test_the_query_takes_the_strict_budget_busy_lines_off_the_503s() -> None:
    """The mutant with the wrong sign on the fourth counter: a marked
    strict-budget-busy 503 would page, as on 2026-11-17."""
    mutant = _with_plus_for_minus(_promql(ALERT_503)["query"], 2)
    assert not _fires(1, None, None, 1)
    assert _fires(1, None, None, 1, query=mutant)


def test_the_query_takes_the_headroom_raced_lines_off_the_503s() -> None:
    """The mutant with the wrong sign on the fifth counter: a marked
    headroom-raced 503 would page, as on 2026-11-18."""
    mutant = _with_plus_for_minus(_promql(ALERT_503)["query"], 3)
    assert not _fires(1, None, None, None, 1)
    assert _fires(1, None, None, None, 1, query=mutant)


# ---------------------------------------------------------------------------
# The rate policy: a storm of rebalance-busy 503s is not hidden.


FAST_BURN = "more than two in ten minutes"
SLOW_BURN = "more than five in an hour"
BUSY_CONDITION = "More than two rebalance-busy 503s in ten minutes, or more than five in an hour"
BUSY_SERIES = 'monitored_resource="cloud_run_revision"'


def _busy_condition() -> dict[str, Any]:
    """The rate policy's one condition."""
    policy = yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    assert condition["displayName"] == BUSY_CONDITION
    return dict(condition["conditionPrometheusQueryLanguage"])


def _busy_bounds(query: str | None = None) -> dict[str, str]:
    """The two sides of the condition's "or", each "(count > bound)", by the
    bound's name: the side over ten minutes and the side over an hour."""
    text = (_busy_condition()["query"] if query is None else query).strip()
    sides = re.split(r"\n\s*or\s*\n", text)
    assert len(sides) == 2, text
    bounds: dict[str, str] = {}
    for side in sides:
        assert side.startswith("(") and side.endswith(")"), side
        evaluation = Promql(_busy_counters(9, 9))
        evaluation.evaluate(side)
        # Each side reads the rebalance-busy counter, once, summed over
        # every revision and region.
        ((counter, series, window),) = evaluation.read
        assert (counter, series) == (BUSY_COUNTER, BUSY_SERIES), side
        bounds[{"10m": FAST_BURN, "1h": SLOW_BURN}[window]] = side
    assert list(bounds) == [FAST_BURN, SLOW_BURN], text
    return bounds


def _busy_counters(ten_minutes: float | None, hour: float | None) -> dict[Any, float | None]:
    # The last hour holds the last ten minutes.
    assert (hour or 0) >= (ten_minutes or 0), (ten_minutes, hour)
    return {(BUSY_COUNTER, "10m"): ten_minutes, (BUSY_COUNTER, "1h"): hour}


def _increases(ten_minutes: float | None, hour: float | None) -> dict[str, float | None]:
    """What each side's expression gives without its comparison."""
    out: dict[str, float | None] = {}
    for side in _busy_bounds().values():
        evaluation = Promql(_busy_counters(ten_minutes, hour))
        value = evaluation.evaluate(side[1:-1].rsplit(" > ", 1)[0])
        ((_name, _series, window),) = evaluation.read
        out[window] = value
    return out


def _busy_fires(ten_minutes: float | None, hour: float | None, *, query: str | None = None) -> set[str]:
    """The bounds that are crossed, given the lines of the last ten minutes
    and of the last hour. The condition as a whole returns a series, and so
    opens the incident, exactly when either is."""
    counters = _busy_counters(ten_minutes, hour)
    text = _busy_condition()["query"] if query is None else query
    crossed = {
        name for name, side in _busy_bounds(text).items()
        if Promql(counters).evaluate(side) is not None
    }
    assert (Promql(counters).evaluate(text) is not None) == bool(crossed), (ten_minutes, hour)
    return crossed


def test_a_promql_condition_is_the_only_condition_of_its_policy() -> None:
    """Cloud Monitoring's rule ("An alerting policy that uses PromQL must
    have only one condition"): a second condition beside one would be
    refused when the script applies the policy. Two bounds on one counter
    are therefore the two sides of one "or", not two conditions as in
    spanner-alerts/contention.yaml, whose conditions are threshold ones."""
    with_promql = []
    for path in sorted(DEPLOY.glob("*-alerts/*.yaml")):
        policy = yaml.safe_load(path.read_text(encoding="utf-8"))
        kinds = [key for condition in policy["conditions"] for key in condition if key.startswith("condition")]
        if "conditionPrometheusQueryLanguage" in kinds:
            with_promql.append(path.name)
            assert kinds == ["conditionPrometheusQueryLanguage"], path.name
    assert with_promql == [
        "billing-authorize-503.yaml", "billing-headroom-raced.yaml", "billing-rebalance-busy.yaml",
        "billing-settle-503.yaml", "billing-settle-unavailable.yaml", "billing-strict-budget-busy.yaml",
    ]
    contention = yaml.safe_load((DEPLOY / "spanner-alerts" / "contention.yaml").read_text(encoding="utf-8"))
    assert len(contention["conditions"]) > 1
    assert all("conditionThreshold" in condition for condition in contention["conditions"])


def test_the_rebalance_busy_policy_has_a_fast_and_a_slow_bound() -> None:
    promql = _busy_condition()
    bounds = _busy_bounds()
    assert list(bounds) == [FAST_BURN, SLOW_BURN]
    # The whole condition reads the counter twice, over ten minutes and
    # over an hour, and nothing else.
    evaluation = Promql(_busy_counters(9, 9))
    assert evaluation.evaluate(promql["query"]) == 9.0
    assert evaluation.read == [(BUSY_COUNTER, BUSY_SERIES, "10m"), (BUSY_COUNTER, BUSY_SERIES, "1h")]
    # One series at most, whichever bound is crossed: the first side's
    # count, else the second's. One series is one incident.
    assert Promql(_busy_counters(3, 3)).evaluate(promql["query"]) == 3.0
    assert Promql(_busy_counters(None, 6)).evaluate(promql["query"]) == 6.0
    assert Promql(_busy_counters(4, 40)).evaluate(promql["query"]) == 4.0
    # It fires on the first evaluation that sees either count.
    assert promql["duration"] == "0s"
    assert promql["evaluationInterval"] == "60s"
    policy = yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))
    assert policy["displayName"] == "TR Gateway: credit rebalance busy"
    assert policy["enabled"] is True
    assert policy["combiner"] == "OR"
    # As the directory's other metric policies: one incident, no
    # renotification. A PromQL policy does not follow autoClose: Cloud
    # Monitoring closes it about four and a half minutes after its condition
    # was last met. The channel is the script's.
    pressure = yaml.safe_load((DEPLOY / "gateway-alerts" / "billing-pressure.yaml").read_text(encoding="utf-8"))
    assert policy["alertStrategy"] == pressure["alertStrategy"] == {
        "autoClose": "1800s", "notificationPrompts": ["OPENED", "CLOSED"],
    }
    assert "notificationChannels" not in policy
    document = " ".join(str(policy["documentation"]["content"]).split())
    for token in (
        "more than two rebalance-busy 503s in ten minutes", "more than five in an hour",
        "billing.credit_rebalance_busy", BUSY_COUNTER, "REBALANCE_COOLDOWN_SECONDS",
        "credit rebalance cooldown exhausted", "tr_credit_balance", "shard",
        "docs/incidents/2026-09-25-credit-rebalance-convoy.md",
        "TR Gateway: authorize 503 not lease headroom", "docs/spanner-reliability.md",
    ):
        assert token in document, token


def test_either_bound_alone_opens_the_incident() -> None:
    """The mutant with "and" for "or", made in the condition's own text:
    a steady trickle that never reaches the fast bound would not page, nor
    would a failed request in an otherwise quiet hour."""
    query = _busy_condition()["query"]
    assert len(re.findall(r"\n\s*or\s*\n", query)) == 1
    mutant = re.sub(r"\n(\s*)or(\s*)\n", r"\n\1and\2\n", query)
    for ten_minutes, hour in ((3, 3), (None, 6), (2, 12)):
        assert Promql(_busy_counters(ten_minutes, hour)).evaluate(query) is not None
        assert _promql_and(mutant, ten_minutes, hour) is None
    # Both crossed: both forms return a series.
    assert _promql_and(mutant, 6, 6) is not None


def _promql_and(query: str, ten_minutes: float | None, hour: float | None) -> float | None:
    """A query whose two sides are joined by "and": the first side's sample
    when both sides have one (neither carries labels)."""
    left, right = re.split(r"\n\s*and\s*\n", query.strip())
    counters = _busy_counters(ten_minutes, hour)
    first, second = Promql(counters).evaluate(left), Promql(counters).evaluate(right)
    return first if first is not None and second is not None else None


def test_the_fast_bound_is_more_than_two_in_ten_minutes() -> None:
    for lines in (None, 0, 1, 2):
        assert FAST_BURN not in _busy_fires(lines, lines), lines
    for lines in (3, 4, 24, 1000):
        assert FAST_BURN in _busy_fires(lines, lines), lines
    # Lines older than ten minutes are the slow bound's, not this one's.
    assert FAST_BURN not in _busy_fires(None, 5)
    assert FAST_BURN not in _busy_fires(2, 1000)


def test_the_slow_bound_is_more_than_five_in_an_hour() -> None:
    for lines in (None, 0, 1, 2, 3, 4, 5):
        assert SLOW_BURN not in _busy_fires(None, lines), lines
    for lines in (6, 7, 12, 1000):
        assert SLOW_BURN in _busy_fires(None, lines), lines
    # It counts the hour, whatever the last ten minutes hold.
    assert SLOW_BURN in _busy_fires(2, 6)
    assert SLOW_BURN not in _busy_fires(2, 5)


def test_a_request_that_fails_all_three_attempts_as_rebalance_busy_pages() -> None:
    """One customer request, three authorize attempts seconds apart, each
    answered rebalance busy: three 503s and three lines. The authorize-503
    policy sees no difference, so the rate policy must page. Before the
    lines existed these were three unmarked 503s, which paged."""
    assert not _fires(3, None, 3)
    assert _busy_fires(3, 3) == {FAST_BURN}
    # The same request before this change: no line, a page.
    assert _fires(3, None, None)
    # Two attempts answered busy and the third served is a request made to
    # wait, not a failed one: it does not page.
    assert not _fires(2, None, 2)
    assert _busy_fires(2, 2) == set()
    # Two busy attempts and a third answered by an unmarked 503 is a failed
    # request too: the authorize-503 policy pages on the unmarked one.
    assert _fires(3, None, 2)


def test_a_trickle_under_the_fast_bound_cannot_go_on() -> None:
    """Two lines every ten minutes never reach the fast bound. That is
    twelve an hour against a baseline of about one a week; the slow bound
    pages within the hour."""
    in_the_hour = 0
    paged_at = None
    for ten_minute_block in range(1, 7):
        in_the_hour += 2
        fired = _busy_fires(2, in_the_hour)
        assert FAST_BURN not in fired
        if fired and paged_at is None:
            paged_at = ten_minute_block
    assert paged_at == 3  # the sixth line, half an hour in
    assert _busy_fires(2, 12) == {SLOW_BURN}
    # One line every ten minutes pages at the sixth as well.
    assert _busy_fires(1, 5) == set()
    assert _busy_fires(1, 6) == {SLOW_BURN}


def test_the_history_would_not_have_paged() -> None:
    """Three events in the thirty days to 2026-11-16 (10-31, 11-04, 11-16),
    one line each, days apart."""
    assert _busy_fires(1, 1) == set()
    # The line is out of the ten-minute window and still in the hour.
    assert _busy_fires(None, 1) == set()
    # No lines: neither side has a series, and neither errs or fires.
    assert _busy_fires(None, None) == set()
    assert _busy_fires(0, 0) == set()


def test_the_runbook_states_the_policys_two_bounds() -> None:
    """The name of the condition, the policy's documentation and the
    runbook give the same two numbers."""
    runbook = " ".join((ROOT / "docs" / "spanner-reliability.md").read_text(encoding="utf-8").split())
    document = " ".join(
        str(yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))["documentation"]["content"]).split()
    )
    assert BUSY_CONDITION == "More than two rebalance-busy 503s in ten minutes, or more than five in an hour"
    for text in (runbook, document, BUSY_CONDITION[0].lower() + BUSY_CONDITION[1:]):
        assert "more than two rebalance-busy 503s in ten minutes" in text
        assert "more than five in an hour" in text
        assert "more than three rebalance-busy" not in text
    assert "### Credit rebalance busy" in (ROOT / "docs" / "spanner-reliability.md").read_text(encoding="utf-8")


def test_marked_503s_page_as_a_burst_and_an_unmarked_one_always() -> None:
    """What the authorize-503 policy stops paging on, the rate policy
    counts: marked 503s page once they are a burst, and an unmarked 503
    beside any number of them still pages."""
    for busy in range(0, 12):
        marked_only = _fires(busy or None, None, busy or None)
        assert not marked_only
        assert bool(_busy_fires(busy or None, busy or None)) == (busy > 2)
        assert _fires(busy + 1, None, busy or None)
    for strict in range(0, 12):
        assert not _fires(strict or None, None, None, strict or None)
        assert _strict_fires(strict or None, strict or None) == (strict > 2)
        assert _fires(strict + 1, None, None, strict or None)
    for raced in range(0, 12):
        assert not _fires(raced or None, None, None, None, raced or None)
        assert _raced_fires(raced or None, raced or None) == (raced > 2)
        assert _fires(raced + 1, None, None, None, raced or None)


# ---------------------------------------------------------------------------
# The strict-budget-busy rate policy: the rebalance-busy policy's shape, on
# its own counter. A burst of these is a customer's strict key throttled.


STRICT_CONDITION = "More than two strict-budget-busy 503s in ten minutes, or more than five in an hour"


def _strict_condition() -> dict[str, Any]:
    policy = yaml.safe_load(ALERT_STRICT.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    assert condition["displayName"] == STRICT_CONDITION
    return dict(condition["conditionPrometheusQueryLanguage"])


def _strict_fires(ten_minutes: float | None, hour: float | None, *, query: str | None = None) -> bool:
    """Whether the strict-budget-busy policy's condition returns a series,
    given the lines of the last ten minutes and of the last hour."""
    assert (hour or 0) >= (ten_minutes or 0), (ten_minutes, hour)
    counters = {(STRICT_COUNTER, "10m"): ten_minutes, (STRICT_COUNTER, "1h"): hour}
    text = _strict_condition()["query"] if query is None else query
    return Promql(counters).evaluate(text) is not None


def test_the_strict_policy_is_the_rebalance_policy_on_its_own_counter() -> None:
    """The bounds tested above for the rebalance-busy policy hold here: the
    query is that query with the counter swapped, and nothing else."""
    strict = _strict_condition()
    busy = _busy_condition()
    assert strict["query"] == busy["query"].replace(BUSY_COUNTER, STRICT_COUNTER)
    assert BUSY_COUNTER not in strict["query"]
    assert {k: v for k, v in strict.items() if k != "query"} == {
        k: v for k, v in busy.items() if k != "query"
    }
    evaluation = Promql({(STRICT_COUNTER, "10m"): 9, (STRICT_COUNTER, "1h"): 9})
    assert evaluation.evaluate(strict["query"]) == 9.0
    assert evaluation.read == [(STRICT_COUNTER, BUSY_SERIES, "10m"), (STRICT_COUNTER, BUSY_SERIES, "1h")]
    # It reads its own counter alone: rebalance-busy lines do not page it.
    assert Promql({
        (STRICT_COUNTER, "10m"): None, (STRICT_COUNTER, "1h"): None,
        (BUSY_COUNTER, "10m"): 9, (BUSY_COUNTER, "1h"): 9,
    }).evaluate(strict["query"]) is None


def test_the_strict_policy_pages_on_more_than_two_in_ten_minutes_or_five_in_an_hour() -> None:
    for lines in (None, 0, 1, 2):
        assert not _strict_fires(lines, lines), lines
    for lines in (3, 4, 24, 1000):
        assert _strict_fires(lines, lines), lines
    for lines in (None, 0, 1, 2, 3, 4, 5):
        assert not _strict_fires(None, lines), lines
    for lines in (6, 7, 12, 1000):
        assert _strict_fires(None, lines), lines
    assert _strict_fires(2, 6)
    assert not _strict_fires(2, 5)
    # The 2026-11-17 event alone, one line: no page from either policy.
    assert not _strict_fires(1, 1)
    assert not _fires(1, None, None, 1)
    # One request failing all three attempts on this 503 pages here, and
    # only here.
    assert not _fires(3, None, None, 3)
    assert _strict_fires(3, 3)
    # Either bound alone opens the incident ("and" for "or" would not).
    mutant = re.sub(r"\n(\s*)or(\s*)\n", r"\n\1and\2\n", _strict_condition()["query"])
    assert mutant != _strict_condition()["query"]
    left, right = re.split(r"\n\s*and\s*\n", mutant.strip())
    for ten_minutes, hour in ((3, 3), (None, 6), (2, 12)):
        assert _strict_fires(ten_minutes, hour)
        assert not (_strict_fires(ten_minutes, hour, query=left) and _strict_fires(ten_minutes, hour, query=right))


def test_the_strict_policy_names_its_line_its_counter_and_its_runbook() -> None:
    policy = yaml.safe_load(ALERT_STRICT.read_text(encoding="utf-8"))
    assert policy["displayName"] == "TR Gateway: strict budget busy"
    assert policy["enabled"] is True
    assert policy["combiner"] == "OR"
    busy = yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))
    assert policy["alertStrategy"] == busy["alertStrategy"]
    assert "notificationChannels" not in policy
    document = " ".join(str(policy["documentation"]["content"]).split())
    for token in (
        "more than two strict-budget-busy 503s in ten minutes", "more than five in an hour",
        "billing.strict_budget_busy", "trusted_router.routes.internal.gateway", STRICT_COUNTER,
        "billing.authorize_strict_budget_busy", "STRICT_ADMISSION_WAIT_SECONDS",
        "trusted_router/strict_budget.py", "TR Gateway: authorize 503 not lease headroom",
        "docs/spanner-reliability.md",
    ):
        assert token in document, token
    runbook_text = (ROOT / "docs" / "spanner-reliability.md").read_text(encoding="utf-8")
    assert "### Strict budget busy" in runbook_text
    runbook = " ".join(runbook_text.split())
    for text in (runbook, document, STRICT_CONDITION[0].lower() + STRICT_CONDITION[1:]):
        assert "more than two strict-budget-busy 503s in ten minutes" in text
        assert "more than five in an hour" in text
    # The wait the documents name is the code's.
    from trusted_router.strict_budget import STRICT_ADMISSION_WAIT_SECONDS

    assert STRICT_ADMISSION_WAIT_SECONDS == 1.0
    assert "(one second)" in document


# ---------------------------------------------------------------------------
# The headroom-raced rate policy: the rebalance-busy policy's shape, on its
# own counter. A burst of these is a workspace racing itself for spread
# credit, past what three repairs absorb.


RACED_CONDITION = "More than two headroom-raced 503s in ten minutes, or more than five in an hour"


def _raced_condition() -> dict[str, Any]:
    policy = yaml.safe_load(ALERT_RACED.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    assert condition["displayName"] == RACED_CONDITION
    return dict(condition["conditionPrometheusQueryLanguage"])


def _raced_fires(ten_minutes: float | None, hour: float | None, *, query: str | None = None) -> bool:
    """Whether the headroom-raced policy's condition returns a series, given
    the lines of the last ten minutes and of the last hour."""
    assert (hour or 0) >= (ten_minutes or 0), (ten_minutes, hour)
    counters = {(RACED_COUNTER, "10m"): ten_minutes, (RACED_COUNTER, "1h"): hour}
    text = _raced_condition()["query"] if query is None else query
    return Promql(counters).evaluate(text) is not None


def test_the_raced_policy_is_the_rebalance_policy_on_its_own_counter() -> None:
    """The bounds tested above for the rebalance-busy policy hold here: the
    query is that query with the counter swapped, and nothing else."""
    raced = _raced_condition()
    busy = _busy_condition()
    assert raced["query"] == busy["query"].replace(BUSY_COUNTER, RACED_COUNTER)
    assert BUSY_COUNTER not in raced["query"]
    assert {k: v for k, v in raced.items() if k != "query"} == {
        k: v for k, v in busy.items() if k != "query"
    }
    evaluation = Promql({(RACED_COUNTER, "10m"): 9, (RACED_COUNTER, "1h"): 9})
    assert evaluation.evaluate(raced["query"]) == 9.0
    assert evaluation.read == [(RACED_COUNTER, BUSY_SERIES, "10m"), (RACED_COUNTER, BUSY_SERIES, "1h")]
    # It reads its own counter alone: the other marks do not page it.
    assert Promql({
        (RACED_COUNTER, "10m"): None, (RACED_COUNTER, "1h"): None,
        (BUSY_COUNTER, "10m"): 9, (BUSY_COUNTER, "1h"): 9,
        (STRICT_COUNTER, "10m"): 9, (STRICT_COUNTER, "1h"): 9,
    }).evaluate(raced["query"]) is None


def test_the_raced_policy_pages_on_more_than_two_in_ten_minutes_or_five_in_an_hour() -> None:
    for lines in (None, 0, 1, 2):
        assert not _raced_fires(lines, lines), lines
    for lines in (3, 4, 24, 1000):
        assert _raced_fires(lines, lines), lines
    for lines in (None, 0, 1, 2, 3, 4, 5):
        assert not _raced_fires(None, lines), lines
    for lines in (6, 7, 12, 1000):
        assert _raced_fires(None, lines), lines
    assert _raced_fires(2, 6)
    assert not _raced_fires(2, 5)
    # The 2026-11-18 event alone, one line: no page from either policy.
    assert not _raced_fires(1, 1)
    assert not _fires(1, None, None, None, 1)
    # The same 503 before the app wrote the line: it paged, and still would.
    assert _fires(1, None, None, None, None)
    # One request failing all three attempts on this 503 pages here, and
    # only here.
    assert not _fires(3, None, None, None, 3)
    assert _raced_fires(3, 3)
    # Either bound alone opens the incident ("and" for "or" would not).
    mutant = re.sub(r"\n(\s*)or(\s*)\n", r"\n\1and\2\n", _raced_condition()["query"])
    assert mutant != _raced_condition()["query"]
    left, right = re.split(r"\n\s*and\s*\n", mutant.strip())
    for ten_minutes, hour in ((3, 3), (None, 6), (2, 12)):
        assert _raced_fires(ten_minutes, hour)
        assert not (_raced_fires(ten_minutes, hour, query=left) and _raced_fires(ten_minutes, hour, query=right))


def test_the_raced_policy_names_its_line_its_counter_and_its_runbook() -> None:
    policy = yaml.safe_load(ALERT_RACED.read_text(encoding="utf-8"))
    assert policy["displayName"] == "TR Gateway: credit headroom raced"
    assert policy["enabled"] is True
    assert policy["combiner"] == "OR"
    busy = yaml.safe_load(ALERT_BUSY.read_text(encoding="utf-8"))
    assert policy["alertStrategy"] == busy["alertStrategy"]
    assert "notificationChannels" not in policy
    document = " ".join(str(policy["documentation"]["content"]).split())
    for token in (
        "more than two headroom-raced 503s in ten minutes", "more than five in an hour",
        "billing.credit_headroom_raced", RACED_COUNTER, "credit headroom changed concurrently",
        "storage_gcp.py", "tr_credit_balance", "TR Gateway: authorize 503 not lease headroom",
        "docs/spanner-reliability.md",
    ):
        assert token in document, token
    runbook_text = (ROOT / "docs" / "spanner-reliability.md").read_text(encoding="utf-8")
    assert "### Credit headroom raced" in runbook_text
    runbook = " ".join(runbook_text.split())
    for text in (runbook, document, RACED_CONDITION[0].lower() + RACED_CONDITION[1:]):
        assert "more than two headroom-raced 503s in ten minutes" in text
        assert "more than five in an hour" in text


def test_every_gateway_alert_display_name_fits_gcps_limit() -> None:
    """Cloud Monitoring refuses a policy or condition display name over 100
    characters when the script applies it (tests/test_terraform_display_name_length.py
    checks infra/ only)."""
    for path in sorted(DEPLOY.glob("*-alerts/*.yaml")):
        policy = yaml.safe_load(path.read_text(encoding="utf-8"))
        names = [policy["displayName"], *(c.get("displayName", "") for c in policy["conditions"])]
        assert all(len(name) <= 100 for name in names), (path.name, names)


# ---------------------------------------------------------------------------
# The gate, run against a stub gcloud.

STUB_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" | tr '\n' ' ' >>"$STUB_LOG"; printf '\n' >>"$STUB_LOG"
query="$3"
case "$query" in
  *"NOT (httpRequest.status=503"*) cat "$STUB_DIR/other.txt" ;;
  *"billing.lease_headroom_unavailable"*"billing.credit_rebalance_busy"*"billing.strict_budget_busy"*"billing.credit_headroom_raced"*)
    printf '%s' "$query" >"$STUB_DIR/line_query.txt"
    n="$(cat "$STUB_DIR/line_calls" 2>/dev/null || echo 0)"
    echo $((n + 1)) >"$STUB_DIR/line_calls"
    if [ -f "$STUB_DIR/lines.$n.json" ]; then cat "$STUB_DIR/lines.$n.json"; else cat "$STUB_DIR/lines.json"; fi ;;
  *"httpRequest.status=503"*) cat "$STUB_DIR/requests.json" ;;
  *) echo "unexpected query: $query" >&2; exit 3 ;;
esac
"""


def _line_entry(trace: str, event: str = "billing.lease_headroom_unavailable") -> dict[str, Any]:
    return {
        "timestamp": "2026-11-13T13:04:15.233651Z",
        "textPayload": (
            f"INFO trusted_router.routes.internal.gateway {event} workspace_id=ws-1 key_id=k "
            f"request_id=r reserved_microdollars=1 estimated_microdollars=1 trace={trace}"
        ),
    }


def _busy_line_entry(trace: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-11-13T13:04:15.233651Z",
        "textPayload": (
            "INFO trusted_router.routes.internal.gateway billing.credit_rebalance_busy "
            f"workspace_id=ws-1 request_id=r trace={trace}"
        ),
    }


def _strict_line_entry(trace: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-11-13T13:04:15.233651Z",
        "textPayload": (
            "INFO trusted_router.routes.internal.gateway billing.strict_budget_busy "
            f"workspace_id=ws-1 request_id=r trace={trace}"
        ),
    }


def _raced_line_entry(trace: str) -> dict[str, Any]:
    return {
        "timestamp": "2026-11-13T13:04:15.233651Z",
        "textPayload": (
            "INFO trusted_router.routes.internal.gateway billing.credit_headroom_raced "
            f"workspace_id=ws-1 request_id=r trace={trace}"
        ),
    }


def _run_gate(
    tmp_path: Path,
    *,
    other: str = "",
    requests: list[dict[str, Any]] | None = None,
    lines: list[dict[str, Any]] | None = None,
    later_lines: list[dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gcloud").write_text(STUB_GCLOUD, encoding="utf-8")
    (stub / "gcloud").chmod(0o755)
    (tmp_path / "other.txt").write_text(other, encoding="utf-8")
    (tmp_path / "requests.json").write_text(json.dumps(requests or []), encoding="utf-8")
    (tmp_path / "lines.0.json").write_text(json.dumps(lines or []), encoding="utf-8")
    (tmp_path / "lines.json").write_text(
        json.dumps(later_lines if later_lines is not None else (lines or [])), encoding="utf-8"
    )
    log = tmp_path / "gcloud.log"
    result = subprocess.run(  # noqa: S603 - the repo's own script, fixed arguments
        [BASH, str(GATE), "us-central1", "2026-11-13T13:00:00Z", "trusted-router-02000-new"],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{stub}:{os.environ['PATH']}",
            "STUB_DIR": str(tmp_path),
            "STUB_LOG": str(log),
            "TR_BILLING_5XX_LOG_GRACE_SECONDS": "0",
            "TR_BILLING_5XX_LINE_RETRY_SECONDS": "0",
            **(env or {}),
        },
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def _authorize_503(trace: str | None = TRACE) -> dict[str, Any]:
    entry = request_log(503, "/internal/gateway/authorize", trace=trace)
    return {key: entry[key] for key in ("timestamp", "trace", "httpRequest") if key in entry}


def test_gate_passes_with_no_5xx(tmp_path: Path) -> None:
    result, calls = _run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gate passed" in result.stdout
    # Only the immediate query and the authorize-503 read; no line read.
    assert len(calls) == 2


def test_gate_fails_at_once_on_any_other_gateway_5xx(tmp_path: Path) -> None:
    result, calls = _run_gate(
        tmp_path,
        other="2026-11-13T13:01:00Z\thttps://x/internal/gateway/settle\t503\t0.1s\n",
        requests=[_authorize_503()],
        lines=[_line_entry(TRACE)],
    )
    assert result.returncode == 1
    assert "billing-path 5xx detected" in result.stdout
    assert len(calls) == 1
    # The immediate query sets aside exactly the authorize 503 shape.
    (first,) = calls
    assert "httpRequest.status>=500" in first
    assert 'httpRequest.requestUrl:"/internal/gateway/"' in first
    assert 'NOT (httpRequest.status=503 AND httpRequest.requestUrl=~"/internal/gateway/authorize([?].*)?$")' in first
    assert 'resource.labels.revision_name="trusted-router-02000-new"' in first


def test_gate_passes_when_every_authorize_503_is_lease_headroom(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(OTHER_TRACE)],
        lines=[_line_entry(OTHER_TRACE), _line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unpaired: 0" in result.stdout


def test_gate_fails_on_a_non_lease_authorize_503(tmp_path: Path) -> None:
    """The mutant: an authorize 503 the app did not mark as lease headroom."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(OTHER_TRACE)],
        lines=[_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "2"},
    )
    assert result.returncode == 1
    assert f"traces/{OTHER_TRACE}" in result.stdout
    assert "not paired with a lease-headroom, rebalance-busy, strict-budget-busy or headroom-raced line" in result.stdout
    # It waited for the line, three reads, before failing.
    assert sum("billing.lease_headroom_unavailable" in call for call in calls) == 3


def test_gate_passes_when_an_authorize_503_is_rebalance_busy(tmp_path: Path) -> None:
    """The 2026-11-16 page, had it come during a rollout: one authorize 503,
    answered while the workspace's rebalance cooldown was held."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[_busy_line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "paired with a line of the app: 1 (lease headroom: 0, rebalance busy: 1, strict budget busy: 0, "
        "headroom raced: 0), unpaired: 0"
    ) in result.stdout
    assert "gate passed" in result.stdout
    # One read of the lines was enough.
    assert sum("billing.credit_rebalance_busy" in call for call in calls) == 1


def test_gate_passes_when_an_authorize_503_is_strict_budget_busy(tmp_path: Path) -> None:
    """The 2026-11-17 page, had it come during a rollout: one authorize 503,
    answered after a strict key's admission wait."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[_strict_line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "paired with a line of the app: 1 (lease headroom: 0, rebalance busy: 0, strict budget busy: 1, "
        "headroom raced: 0), unpaired: 0"
    ) in result.stdout
    assert "gate passed" in result.stdout
    assert sum("billing.strict_budget_busy" in call for call in calls) == 1


def test_gate_fails_on_an_unpaired_strict_budget_busy_503(tmp_path: Path) -> None:
    """The same 503 before the app wrote the line: the handler's warning and
    the route's are all it left, and neither excuses it."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[
            {"timestamp": "2026-11-13T13:04:15.233651Z", "textPayload": STRICT_HANDLER_WARNING},
            {"timestamp": "2026-11-13T13:04:15.233651Z", "textPayload": f"{STRICT_HANDLER_WARNING} trace={TRACE}"},
            {"timestamp": "2026-11-13T13:04:15.233651Z", "textPayload": f"{STRICT_ROUTE_WARNING} trace={TRACE}"},
        ],
        env={"TR_BILLING_5XX_LINE_RETRIES": "1"},
    )
    assert result.returncode == 1
    assert "strict budget busy: 0, headroom raced: 0), unpaired: 1" in result.stdout
    assert f"traces/{TRACE}" in result.stdout
    assert "not paired with a lease-headroom, rebalance-busy, strict-budget-busy or headroom-raced line" in result.stdout
    # It waited for the line before failing.
    assert sum("billing.strict_budget_busy" in call for call in calls) == 2


def test_gate_does_not_let_one_strict_line_excuse_two_503s(tmp_path: Path) -> None:
    """An enclave's retry under one trace: two 503s, one strict line."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(TRACE)],
        lines=[_strict_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "unpaired: 1" in result.stdout


def test_gate_passes_when_an_authorize_503_is_headroom_raced(tmp_path: Path) -> None:
    """The 2026-11-18 page, had it come during a rollout: one authorize 503,
    "credit headroom changed concurrently" after three repairs lost."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[_raced_line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "paired with a line of the app: 1 (lease headroom: 0, rebalance busy: 0, strict budget busy: 0, "
        "headroom raced: 1), unpaired: 0"
    ) in result.stdout
    assert "gate passed" in result.stdout
    assert sum("billing.credit_headroom_raced" in call for call in calls) == 1


def test_gate_fails_on_an_unmarked_headroom_503(tmp_path: Path) -> None:
    """The same 503 before the app wrote the line, or from a recovery that
    stopped early: the storage warnings are all it left, and neither
    excuses it, though they name the type."""
    result, calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[
            {"timestamp": "2026-11-13T13:04:15.233651Z", "textPayload": f"{RACED_ROUTE_WARNING} trace={TRACE}"},
            {"timestamp": "2026-11-13T13:04:15.233651Z", "textPayload": f"{RACED_HANDLER_WARNING} trace={TRACE}"},
        ],
        env={"TR_BILLING_5XX_LINE_RETRIES": "1"},
    )
    assert result.returncode == 1
    assert "headroom raced: 0), unpaired: 1" in result.stdout
    assert f"traces/{TRACE}" in result.stdout
    assert sum("billing.credit_headroom_raced" in call for call in calls) == 2


def test_gate_does_not_let_one_raced_line_excuse_two_503s(tmp_path: Path) -> None:
    """An enclave's retry under one trace: two 503s, one raced line."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(TRACE)],
        lines=[_raced_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "unpaired: 1" in result.stdout


def test_gate_passes_with_a_503_of_each_kind(tmp_path: Path) -> None:
    fourth_trace = "8e3f0c1d2b4a59687766554433221100"
    result, _calls = _run_gate(
        tmp_path,
        requests=[
            _authorize_503(TRACE), _authorize_503(OTHER_TRACE), _authorize_503(THIRD_TRACE),
            _authorize_503(fourth_trace),
        ],
        lines=[
            _busy_line_entry(OTHER_TRACE), _line_entry(TRACE), _strict_line_entry(THIRD_TRACE),
            _raced_line_entry(fourth_trace),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "authorize 503s: 4, paired with a line of the app: 4 "
        "(lease headroom: 1, rebalance busy: 1, strict budget busy: 1, headroom raced: 1), unpaired: 0"
    ) in result.stdout


def test_gate_fails_on_an_unmarked_503_beside_one_of_each_kind(tmp_path: Path) -> None:
    """A storage outage during a rollout that also saw the designed 503s:
    the unmarked one is left over and fails the gate."""
    fourth_trace = "8e3f0c1d2b4a59687766554433221100"
    result, _calls = _run_gate(
        tmp_path,
        requests=[
            _authorize_503(TRACE), _authorize_503(THIRD_TRACE), _authorize_503(OTHER_TRACE),
            _authorize_503(fourth_trace),
        ],
        lines=[_line_entry(TRACE), _busy_line_entry(OTHER_TRACE), _strict_line_entry(fourth_trace)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert (
        "(lease headroom: 1, rebalance busy: 1, strict budget busy: 1, headroom raced: 0), unpaired: 1"
    ) in result.stdout
    assert "unpaired 503: 2026-11-13T13:04:15.208934Z" in result.stdout
    assert f"traces/{THIRD_TRACE}" in result.stdout
    for paired in (TRACE, OTHER_TRACE, fourth_trace):
        assert f"traces/{paired}" not in result.stdout
    assert "billing-path 5xx detected" in result.stdout


def test_gate_does_not_let_one_busy_line_excuse_two_503s(tmp_path: Path) -> None:
    """An enclave's retry under one trace: two 503s, the second a real one."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(TRACE)],
        lines=[_busy_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "unpaired: 1" in result.stdout


def test_gate_pairs_a_retried_request_with_a_line_of_each_kind(tmp_path: Path) -> None:
    """One trace, two attempts, each answered by a different designed 503."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(TRACE)],
        lines=[_busy_line_entry(TRACE), _line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "(lease headroom: 1, rebalance busy: 1, strict budget busy: 0, headroom raced: 0), unpaired: 0"
    ) in result.stdout


def test_gate_is_not_excused_by_the_warnings_of_a_storage_503(tmp_path: Path) -> None:
    """The lines every storage 503 of authorize leaves are not marks, even
    when they name the rebalance-busy type."""
    warnings = [
        {
            "timestamp": "2026-11-13T13:04:15.233651Z",
            "textPayload": (
                "WARNING trusted_router.routes.internal.gateway billing.authorize_storage_unavailable "
                f"request_id=r workspace_id=ws-1 error_class=CreditRebalanceBusy trace={TRACE}"
            ),
        },
        {
            "timestamp": "2026-11-13T13:04:15.233651Z",
            "textPayload": (
                "WARNING trusted_router.storage_gcp credit rebalance cooldown exhausted "
                f"workspace=ws-1 attempts=3 trace={TRACE}"
            ),
        },
    ]
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=warnings,
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "unpaired: 1" in result.stdout


def test_gate_passes_on_lines_without_503s_and_does_not_read_them(tmp_path: Path) -> None:
    """Lines with no 503 behind them are the alert's to page on, not the
    gate's: with no authorize 503 on the revision there is nothing to pair."""
    result, calls = _run_gate(
        tmp_path,
        lines=[_busy_line_entry(TRACE), _line_entry(OTHER_TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert not (tmp_path / "line_query.txt").exists()


def test_the_gates_line_read_is_every_line_of_the_candidate_revision_alone(tmp_path: Path) -> None:
    """The read the gate sends, evaluated against log entries."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[_busy_line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    query = (tmp_path / "line_query.txt").read_text(encoding="utf-8")
    headroom, _ = _rendered_headroom_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    busy, _ = _rendered_busy_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    strict, _ = _rendered_strict_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    raced, _ = _rendered_raced_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})

    def entry(text: str, **changes: str) -> dict[str, Any]:
        log = request_log(503, "/internal/gateway/authorize")
        log.pop("httpRequest")
        log["resource"]["labels"].update(
            {key: value for key, value in changes.items() if key != "timestamp"}
        )
        return {**log, "timestamp": changes.get("timestamp", log["timestamp"]), "textPayload": text}

    for line in (headroom, busy, strict, raced):
        assert matches(query, entry(line), allow_or=True), line
        # Another revision's, another region's, another service's or an
        # earlier line is not this rollout's.
        assert not matches(query, entry(line, revision_name="trusted-router-01999-old"), allow_or=True)
        assert not matches(query, entry(line, location="us-east4"), allow_or=True)
        assert not matches(query, entry(line, service_name="trusted-router-public"), allow_or=True)
        assert not matches(query, entry(line, timestamp="2026-11-13T12:59:59Z"), allow_or=True)
    for text in (
        busy.replace("INFO ", "WARNING ", 1),
        busy.replace("billing.credit_rebalance_busy ", "billing.credit_rebalance_busy_v2 "),
        "WARNING trusted_router.main storage.unavailable method=POST route=/internal/gateway/authorize "
        "request_id=r error_class=CreditRebalanceBusy workspace_id=ws-1",
        "INFO trusted_router.routes.internal.gateway billing.insufficient_credits workspace_id=ws-1",
        strict.replace("INFO ", "WARNING ", 1),
        STRICT_HANDLER_WARNING,
        STRICT_ROUTE_WARNING,
        raced.replace("INFO ", "WARNING ", 1),
        RACED_ROUTE_WARNING,
        RACED_HANDLER_WARNING,
    ):
        assert not matches(query, entry(text), allow_or=True), text


def test_gate_waits_for_a_line_ingested_late(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[],
        later_lines=[_line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_does_not_let_one_line_excuse_two_503s(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(TRACE)],
        lines=[_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "unpaired: 1" in result.stdout


def test_gate_counts_a_503_with_no_trace(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(trace=None)],
        lines=[_line_entry(TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1


def test_gate_does_not_pair_another_lines_trace(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE)],
        lines=[_line_entry(TRACE, event="billing.insufficient_credits")],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1


def test_gate_fails_when_the_503s_reach_the_read_limit(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(OTHER_TRACE)],
        lines=[_line_entry(TRACE), _line_entry(OTHER_TRACE)],
        env={"TR_BILLING_5XX_LEASE_503_LIMIT": "2"},
    )
    assert result.returncode == 1
    assert "reach the read limit" in result.stdout


def test_gate_hotfix_skip_is_unchanged(tmp_path: Path) -> None:
    result, calls = _run_gate(tmp_path, other="x\n", env={"TR_BILLING_5XX_GATE_SKIP": "true"})
    assert result.returncode == 0
    assert calls == []


# ---------------------------------------------------------------------------
# gateway_reliability.sh: the counters before any policy, the rate policies
# before the authorize-503 policy stops counting their 503s, and the
# authorize-503 policy before the narrowed 5xx filter.

# A stub gcloud. By default no counter exists (each is created); with
# STUB_METRICS_EXIST every counter exists but STUB_NEW_METRIC. STUB_NOT_READY
# lists "policy-file:N" pairs: each policy is refused its first N applies, as
# Cloud Monitoring refuses a PromQL policy that names a counter it does not
# know yet; with STUB_CREATE_LANDS a refused create lands all the same.
STUB_RELIABILITY_GCLOUD = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
args="$*"
case "$args" in
  *"logging metrics describe ${STUB_NEW_METRIC:-none}") exit 1 ;;
  *"logging metrics describe"*) [ "${STUB_METRICS_EXIST:-false}" = true ] && exit 0; exit 1 ;;
  *"logging metrics create ${STUB_FAIL_METRIC:-none} "*) exit 1 ;;
  *"monitoring channels list"*) echo "projects/p/notificationChannels/1" ;;
  *"monitoring policies list"*"displayName=\"${STUB_MISSING_POLICY:-none}\""*)
    if [ -f "$STUB_LOG.landed" ]; then echo "projects/p/alertPolicies/9"; fi ;;
  *"monitoring policies list"*) echo "projects/p/alertPolicies/1" ;;
  *"monitoring policies "*"/${STUB_FAIL_POLICY:-none} "*) exit 1 ;;
  *"monitoring policies "*"--policy-from-file="*)
    for spec in ${STUB_NOT_READY:-}; do
      file="${spec%%:*}"
      case "$args" in
        *"/${file} "*)
          n="$(cat "$STUB_LOG.${file}" 2>/dev/null || echo 0)"
          echo $((n + 1)) >"$STUB_LOG.${file}"
          if [ "$n" -lt "${spec##*:}" ]; then
            if [ "${STUB_CREATE_LANDS:-false}" = true ]; then : >"$STUB_LOG.landed"; fi
            echo "ERROR: (gcloud.monitoring.policies) INVALID_ARGUMENT: metric invalid" >&2
            exit 1
          fi ;;
      esac
    done ;;
esac
exit 0
"""


def _run_reliability(
    tmp_path: Path, *, fail_metric: str = "none", missing_policy: str = "none", fail_policy: str = "none",
    metrics_exist: bool = False, new_metric: str = "none", not_ready: dict[str, int] | None = None,
    create_lands: bool = False, retries: int | None = None,
) -> tuple[int, list[str]]:
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gcloud").write_text(STUB_RELIABILITY_GCLOUD, encoding="utf-8")
    (stub / "gcloud").chmod(0o755)
    log = tmp_path / "gcloud.log"
    result = subprocess.run(  # noqa: S603 - the repo's own script, fixed arguments
        [BASH, str(RELIABILITY), "--apply"],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{stub}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_FAIL_METRIC": fail_metric,
            "STUB_MISSING_POLICY": missing_policy,
            "STUB_FAIL_POLICY": fail_policy,
            "STUB_METRICS_EXIST": "true" if metrics_exist else "false",
            "STUB_NEW_METRIC": new_metric,
            "STUB_NOT_READY": " ".join(f"{name}:{times}" for name, times in (not_ready or {}).items()),
            "STUB_CREATE_LANDS": "true" if create_lands else "false",
            "TR_GATEWAY_NEW_COUNTER_RETRY_SECONDS": "0",
            **({"TR_GATEWAY_NEW_COUNTER_RETRIES": str(retries)} if retries is not None else {}),
            "TR_REGIONS": "us-central1",
        },
    )
    return result.returncode, log.read_text(encoding="utf-8").splitlines()


def test_reliability_applies_counters_then_the_policies_that_take_over_first(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path)
    assert status == 0, calls
    creates = [call for call in calls if "logging metrics create" in call]
    updates = [call for call in calls if "monitoring policies update" in call]
    assert [call.split("logging metrics create ")[1].split()[0] for call in creates] == [
        "trustedrouter_gateway_authorize_503",
        HEADROOM_COUNTER,
        BUSY_COUNTER,
        STRICT_COUNTER,
        RACED_COUNTER,
        SETTLE_503_COUNTER,
        SETTLE_UNAVAILABLE_COUNTER,
        "trustedrouter_gateway_billing_slow",
    ]
    # The policies that bound rebalance-busy, strict-budget-busy and
    # headroom-raced 503s, then the one that stops counting them as
    # unmarked, then the 5xx filter that sets authorize 503s aside for it.
    # Each exists before the next gives its 503s up.
    assert "billing-rebalance-busy.yaml" in updates[0]
    assert "billing-strict-budget-busy.yaml" in updates[1]
    assert "billing-headroom-raced.yaml" in updates[2]
    assert "billing-authorize-503.yaml" in updates[3]
    narrowed = next(index for index, call in enumerate(updates) if "billing-5xx.yaml" in call)
    assert narrowed > 3
    files = [call.split("--policy-from-file=")[1].split()[0].rsplit("/", 1)[1] for call in updates]
    assert sorted(files) == sorted(path.name for path in (DEPLOY / "gateway-alerts").glob("*.yaml"))
    assert len(updates) == len(list((DEPLOY / "gateway-alerts").glob("*.yaml")))
    assert calls.index(creates[-1]) < calls.index(updates[0])
    # Every policy notifies the one on-call channel the script resolved.
    assert all("--set-notification-channels=projects/p/notificationChannels/1" in call for call in updates)


def test_reliability_creates_the_rebalance_busy_policy_before_the_other_changes(tmp_path: Path) -> None:
    """The first apply: the policy does not exist yet."""
    status, calls = _run_reliability(tmp_path, missing_policy="TR Gateway: credit rebalance busy")
    assert status == 0, calls
    policies = [call for call in calls if "monitoring policies create" in call or "monitoring policies update" in call]
    assert "monitoring policies create" in policies[0]
    assert "billing-rebalance-busy.yaml" in policies[0]
    assert "--notification-channels=projects/p/notificationChannels/1" in policies[0]
    assert "monitoring policies update" in policies[1]
    assert "billing-strict-budget-busy.yaml" in policies[1]
    assert "monitoring policies update" in policies[2]
    assert "billing-headroom-raced.yaml" in policies[2]
    assert "monitoring policies update" in policies[3]
    assert "billing-authorize-503.yaml" in policies[3]
    assert sum("monitoring policies create" in call for call in policies) == 1


def test_reliability_creates_the_strict_budget_busy_policy_before_the_authorize_503_policy(
    tmp_path: Path,
) -> None:
    """The first apply after this change: the strict policy does not exist
    yet, and is created before the authorize-503 policy stops paging on
    strict-budget-busy 503s."""
    status, calls = _run_reliability(tmp_path, missing_policy="TR Gateway: strict budget busy")
    assert status == 0, calls
    policies = [call for call in calls if "monitoring policies create" in call or "monitoring policies update" in call]
    assert "monitoring policies update" in policies[0]
    assert "billing-rebalance-busy.yaml" in policies[0]
    assert "monitoring policies create" in policies[1]
    assert "billing-strict-budget-busy.yaml" in policies[1]
    assert "--notification-channels=projects/p/notificationChannels/1" in policies[1]
    assert "billing-headroom-raced.yaml" in policies[2]
    assert "billing-authorize-503.yaml" in policies[3]
    assert sum("monitoring policies create" in call for call in policies) == 1


def test_reliability_creates_the_headroom_raced_policy_before_the_authorize_503_policy(
    tmp_path: Path,
) -> None:
    """The first apply after this change: the raced policy does not exist
    yet, and is created before the authorize-503 policy stops paging on
    headroom-raced 503s."""
    status, calls = _run_reliability(tmp_path, missing_policy="TR Gateway: credit headroom raced")
    assert status == 0, calls
    policies = [call for call in calls if "monitoring policies create" in call or "monitoring policies update" in call]
    assert "billing-rebalance-busy.yaml" in policies[0]
    assert "billing-strict-budget-busy.yaml" in policies[1]
    assert "monitoring policies create" in policies[2]
    assert "billing-headroom-raced.yaml" in policies[2]
    assert "--notification-channels=projects/p/notificationChannels/1" in policies[2]
    assert "monitoring policies update" in policies[3]
    assert "billing-authorize-503.yaml" in policies[3]
    assert sum("monitoring policies create" in call for call in policies) == 1


def test_reliability_does_not_narrow_the_authorize_503_policy_without_the_headroom_raced_policy(
    tmp_path: Path,
) -> None:
    """Were the authorize-503 policy changed and the raced rate policy not
    there, no policy would page on a storm of headroom-raced 503s."""
    status, calls = _run_reliability(
        tmp_path,
        missing_policy="TR Gateway: credit headroom raced",
        fail_policy="billing-headroom-raced.yaml",
    )
    assert status != 0
    assert any("monitoring policies create" in call and "billing-headroom-raced.yaml" in call for call in calls)
    assert not any("billing-authorize-503.yaml" in call for call in calls)
    assert not any("billing-5xx.yaml" in call for call in calls)


def _applies(calls: list[str], policy: str) -> list[str]:
    """The creates and updates of one policy file, in order."""
    return [
        call for call in calls
        if ("monitoring policies create" in call or "monitoring policies update" in call) and f"/{policy} " in call
    ]


def test_reliability_waits_for_a_new_counter_before_the_policies_that_read_it(tmp_path: Path) -> None:
    """The first apply after this change, as the live state is: every
    counter exists but the new one, and Cloud Monitoring refuses a PromQL
    policy naming that counter ("metric invalid") for its first seconds.
    The raced policy is applied again until it is accepted, and the
    authorize-503 policy, which reads the counter too, only after."""
    status, calls = _run_reliability(
        tmp_path,
        metrics_exist=True,
        new_metric=RACED_COUNTER,
        missing_policy="TR Gateway: credit headroom raced",
        not_ready={"billing-headroom-raced.yaml": 2},
    )
    assert status == 0, calls
    creates = [call for call in calls if "logging metrics create" in call]
    assert [call.split("logging metrics create ")[1].split()[0] for call in creates] == [RACED_COUNTER]
    raced = _applies(calls, "billing-headroom-raced.yaml")
    assert len(raced) == 3
    assert all("monitoring policies create" in call for call in raced)
    # Each attempt looks the policy up again first.
    for call in raced:
        assert "monitoring policies list" in calls[calls.index(call) - 1]
    # The policies that read no new counter are applied once each, and the
    # authorize-503 policy waits for the raced one to be accepted.
    assert len(_applies(calls, "billing-rebalance-busy.yaml")) == 1
    assert len(_applies(calls, "billing-strict-budget-busy.yaml")) == 1
    (authorize,) = _applies(calls, "billing-authorize-503.yaml")
    assert calls.index(raced[-1]) < calls.index(authorize)
    (narrowed,) = _applies(calls, "billing-5xx.yaml")
    assert calls.index(authorize) < calls.index(narrowed)


def test_reliability_stops_when_a_new_counter_is_never_accepted(tmp_path: Path) -> None:
    """Bounded: after the last attempt the script stops, before the
    authorize-503 policy stops paging on headroom-raced 503s."""
    status, calls = _run_reliability(
        tmp_path,
        metrics_exist=True,
        new_metric=RACED_COUNTER,
        missing_policy="TR Gateway: credit headroom raced",
        not_ready={"billing-headroom-raced.yaml": 99},
        retries=2,
    )
    assert status != 0
    assert len(_applies(calls, "billing-headroom-raced.yaml")) == 3
    assert not _applies(calls, "billing-authorize-503.yaml")
    assert not _applies(calls, "billing-5xx.yaml")


def test_reliability_shares_one_retry_budget_across_the_run(tmp_path: Path) -> None:
    """The wait is for counters created together, so its budget is the
    run's, not each policy's: retries spent on the raced policy are not
    there again for the authorize-503 policy, and the run stops when the
    budget is gone, before the 5xx filter narrows."""
    status, calls = _run_reliability(
        tmp_path,
        metrics_exist=True,
        new_metric=RACED_COUNTER,
        not_ready={"billing-headroom-raced.yaml": 2, "billing-authorize-503.yaml": 2},
        retries=3,
    )
    assert status != 0
    assert len(_applies(calls, "billing-headroom-raced.yaml")) == 3  # two retries
    assert len(_applies(calls, "billing-authorize-503.yaml")) == 2  # the last one
    assert not _applies(calls, "billing-5xx.yaml")
    # Two refusals and one: the same budget covers both, and the run goes on.
    control = tmp_path / "control"
    control.mkdir()
    status, calls = _run_reliability(
        control,
        metrics_exist=True,
        new_metric=RACED_COUNTER,
        not_ready={"billing-headroom-raced.yaml": 2, "billing-authorize-503.yaml": 1},
        retries=3,
    )
    assert status == 0, calls
    assert len(_applies(calls, "billing-authorize-503.yaml")) == 2


def test_the_new_counter_wait_fits_the_workflows_timeout() -> None:
    """The script's default retry budget, at its default spacing, is at most
    a quarter of the time the workflow gives the job, leaving the rest for
    its ordinary run and gcloud's own latency."""
    script = RELIABILITY.read_text(encoding="utf-8")
    (retries,) = re.findall(r'^NEW_COUNTER_RETRIES="\$\{TR_GATEWAY_NEW_COUNTER_RETRIES:-(\d+)\}"$', script, re.M)
    (spacing,) = re.findall(
        r'^NEW_COUNTER_RETRY_SECONDS="\$\{TR_GATEWAY_NEW_COUNTER_RETRY_SECONDS:-(\d+)\}"$', script, re.M
    )
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "gateway-reliability.yml").read_text(encoding="utf-8"))
    (job,) = workflow["jobs"].values()
    assert "gateway_reliability.sh --apply" in json.dumps(job["steps"])
    assert int(retries) * int(spacing) * 4 <= int(job["timeout-minutes"]) * 60


def test_reliability_retry_updates_a_policy_an_earlier_attempt_created(tmp_path: Path) -> None:
    """A create refused to the client may still have landed: the next
    attempt finds it by name and updates it, never creating a second."""
    status, calls = _run_reliability(
        tmp_path,
        metrics_exist=True,
        new_metric=RACED_COUNTER,
        missing_policy="TR Gateway: credit headroom raced",
        not_ready={"billing-headroom-raced.yaml": 1},
        create_lands=True,
    )
    assert status == 0, calls
    first, second = _applies(calls, "billing-headroom-raced.yaml")
    assert "monitoring policies create" in first
    assert "monitoring policies update projects/p/alertPolicies/9 " in second


@pytest.mark.parametrize(
    ("new_metric", "failing"),
    [
        # No counter is new: a refusal is real, and stops the script at once.
        ("none", "billing-headroom-raced.yaml"),
        # A counter is new, but this policy does not read it.
        (RACED_COUNTER, "billing-rebalance-busy.yaml"),
        (RACED_COUNTER, "billing-strict-budget-busy.yaml"),
    ],
    ids=["no-new-counter", "rebalance-busy", "strict-budget-busy"],
)
def test_reliability_applies_once_a_policy_that_reads_no_new_counter(
    tmp_path: Path, new_metric: str, failing: str,
) -> None:
    status, calls = _run_reliability(
        tmp_path, metrics_exist=True, new_metric=new_metric, fail_policy=failing,
    )
    assert status != 0
    assert len(_applies(calls, failing)) == 1
    assert not _applies(calls, "billing-authorize-503.yaml")


def test_reliability_does_not_narrow_the_authorize_503_policy_without_the_strict_budget_busy_policy(
    tmp_path: Path,
) -> None:
    """Were the authorize-503 policy changed and the strict rate policy not
    there, no policy would page on a storm of strict-budget-busy 503s."""
    status, calls = _run_reliability(
        tmp_path,
        missing_policy="TR Gateway: strict budget busy",
        fail_policy="billing-strict-budget-busy.yaml",
    )
    assert status != 0
    assert any("monitoring policies create" in call and "billing-strict-budget-busy.yaml" in call for call in calls)
    assert not any("billing-authorize-503.yaml" in call for call in calls)
    assert not any("billing-5xx.yaml" in call for call in calls)


def test_reliability_does_not_narrow_the_authorize_503_policy_without_the_rebalance_busy_policy(
    tmp_path: Path,
) -> None:
    """Were the authorize-503 policy changed and the rate policy not there,
    no policy would page on a storm of rebalance-busy 503s."""
    status, calls = _run_reliability(
        tmp_path,
        missing_policy="TR Gateway: credit rebalance busy",
        fail_policy="billing-rebalance-busy.yaml",
    )
    assert status != 0
    assert any("monitoring policies create" in call and "billing-rebalance-busy.yaml" in call for call in calls)
    assert not any("billing-authorize-503.yaml" in call for call in calls)
    assert not any("monitoring policies update" in call for call in calls)


@pytest.mark.parametrize(
    "metric",
    [
        "trustedrouter_gateway_authorize_503", HEADROOM_COUNTER, BUSY_COUNTER, STRICT_COUNTER, RACED_COUNTER,
        SETTLE_503_COUNTER, SETTLE_UNAVAILABLE_COUNTER,
    ],
)
def test_reliability_stops_before_any_policy_without_a_counter(tmp_path: Path, metric: str) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric=metric)
    assert status != 0
    assert not any("monitoring policies" in call for call in calls)


def test_reliability_still_tolerates_the_optional_slow_counter(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric="trustedrouter_gateway_billing_slow")
    assert status == 0
    assert any("billing-5xx.yaml" in call for call in calls if "monitoring policies update" in call)
