"""The billing 5xx alert and deploy gate set aside the two authorize 503s the
app answers by design, and nothing else.

With TR_FASTPATH_LEASE_HEADROOM_503 on, authorize answers 503 (Retry-After: 1)
when a workspace's shortfall is held in fast-path leases. It answers the
storage 503 (Retry-After: 1) when a workspace's credit covers the hold but is
spread across credit shards and the rebalance cooldown is held by its other
requests. Cloud Run's request log records only status and URL, so each 503 is
told apart by the line the app writes for it,
billing.lease_headroom_unavailable or billing.credit_rebalance_busy:

* the deploy gate (scripts/deploy/assert_no_billing_5xx.sh) pairs each
  authorize 503 with a line by trace, and fails on any 503 left over;
* "TR Gateway: billing path 5xx" (gateway-alerts/billing-5xx.yaml) sets
  authorize 503s aside, and "TR Gateway: authorize 503 not lease headroom"
  (gateway-alerts/billing-authorize-503.yaml) pages when the authorize 503
  count and the count of both lines differ;
* "TR Gateway: credit rebalance busy"
  (gateway-alerts/billing-rebalance-busy.yaml) pages on more than two of
  the second line in ten minutes or more than five in an hour, which the
  policy above no longer pages on.

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
HEADROOM_COUNTER = "trustedrouter_gateway_lease_headroom_503"
BUSY_COUNTER = "trustedrouter_gateway_rebalance_busy_503"

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


def test_each_line_is_counted_by_its_own_counter_alone() -> None:
    """The policy adds the two counters: a line counted by both would be
    subtracted twice and page, a line counted by neither would leave its
    503 unmarked."""
    filters = _metric_filters()
    headroom, _ = _rendered_headroom_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    busy, _ = _rendered_busy_line({"X-Cloud-Trace-Context": f"{TRACE}/99;o=1"})
    assert matches(filters[HEADROOM_COUNTER], app_line(headroom))
    assert not matches(filters[BUSY_COUNTER], app_line(headroom))
    assert matches(filters[BUSY_COUNTER], app_line(busy))
    assert not matches(filters[HEADROOM_COUNTER], app_line(busy))
    # Neither line is a request log entry, and no request log entry is a line.
    request = request_log(503, "/internal/gateway/authorize")
    for line in (headroom, busy):
        assert not matches(filters["trustedrouter_gateway_authorize_503"], app_line(line))
    for name in (HEADROOM_COUNTER, BUSY_COUNTER):
        assert not matches(filters[name], request)


# ---------------------------------------------------------------------------
# The alert: the two policies partition what the original matched.


def _authorize_503_counter() -> str:
    return _metric_filters()["trustedrouter_gateway_authorize_503"]


def test_the_5xx_alert_and_the_authorize_503_counter_split_the_original_exactly() -> None:
    narrowed = _alert_5xx_filter()
    counter = _authorize_503_counter()
    for entry in CORPUS:
        original = matches(ORIGINAL_5XX_FILTER, entry)
        immediate = matches(narrowed, entry)
        counted = matches(counter, entry)
        # Everything the original paged on still pages, by exactly one policy.
        assert original == (immediate or counted), entry
        assert not (immediate and counted), entry
    # The corpus reaches both sides.
    assert any(matches(counter, entry) for entry in CORPUS)
    assert any(matches(narrowed, entry) for entry in CORPUS)


@pytest.mark.parametrize(
    ("status", "path"),
    [
        (503, "/internal/gateway/settle"),
        (503, "/internal/gateway/refund"),
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
    lost = [
        entry for entry in CORPUS
        if matches(ORIGINAL_5XX_FILTER, entry)
        and not (matches(mutated, entry) or matches(counter, entry))
    ]
    assert lost, mutant


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
) -> tuple[float | None, list[tuple[str, str, str]]]:
    promql = Promql({
        AUTHORIZE_503_COUNTER: authorize_503s,
        HEADROOM_COUNTER: headroom_lines,
        BUSY_COUNTER: busy_lines,
    })
    return promql.evaluate(query), promql.read


def _fires(
    authorize_503s: float | None,
    headroom_lines: float | None,
    busy_lines: float | None = None,
    *,
    query: str | None = None,
) -> bool:
    """Whether the authorize-503 policy's condition returns a series."""
    text = _promql(ALERT_503)["query"] if query is None else query
    return _evaluate(text, authorize_503s, headroom_lines, busy_lines)[0] is not None


def test_the_authorize_503_policy_compares_the_503s_with_both_kinds_of_line() -> None:
    promql = _promql(ALERT_503)
    _value, read = _evaluate(promql["query"], 1, 1, 1)
    # Each counter once, the same series and the same window.
    assert read == [
        (AUTHORIZE_503_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (HEADROOM_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
        (BUSY_COUNTER, 'monitored_resource="cloud_run_revision"', "10m"),
    ]
    # The 503s less both kinds of line, to the half that tells 0 from 1.
    assert _evaluate(promql["query"], 7, 2, 4)[0] == 1.0
    assert _evaluate(promql["query"], 6, 2, 4)[0] is None
    assert _evaluate(promql["query"], 6.4, 2, 4)[0] is None
    assert _evaluate(promql["query"], 6.6, 2, 4)[0] == pytest.approx(0.6)
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
    for token in (
        "billing.lease_headroom_unavailable", HEADROOM_COUNTER,
        "billing.credit_rebalance_busy", BUSY_COUNTER,
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
    # The store's other retryable 503 (credit headroom changed concurrently)
    # and a strict key's busy 503 write no line of these two: they page.
    assert _fires(4, 1, 2)


def test_lines_without_503s_page() -> None:
    """A line written for a request that was not answered 503 could hide a
    real 503: more lines than 503s is a defect, of either kind."""
    assert _fires(None, None, 1)
    assert _fires(None, 1, None)
    assert _fires(0, None, 1)
    assert _fires(1, 1, 1)
    assert _fires(5, 3, 3)


def _without_or_zero(query: str, term: int) -> str:
    """The query with one counter's "or vector(0)" taken out."""
    assert query.count(" or vector(0))") == 3
    pieces = query.split(" or vector(0))")
    mutant = "".join(
        piece + (")" if index == term else " or vector(0))") if index < 3 else piece
        for index, piece in enumerate(pieces)
    )
    assert mutant.count(" or vector(0))") == 2
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


@pytest.mark.parametrize("term", [0, 1, 2], ids=["authorize-503s", "lease-headroom", "rebalance-busy"])
def test_without_or_vector_zero_a_counter_with_no_series_would_silence_the_policy(term: int) -> None:
    """The mutants each "or vector(0)" kills, made in the query's own text:
    a counter with no log line in the window has no series, and a
    difference with it has none either."""
    query = _promql(ALERT_503)["query"]
    mutant = _without_or_zero(query, term)
    # Cases that must page, each with the mutated term's counter absent.
    absent: list[tuple[float | None, float | None, float | None]] = [
        (None, 1, 1),   # lines and no 503s
        (3, None, 1),   # unmarked 503s, no lease-headroom line (the usual state)
        (3, 1, None),   # unmarked 503s, no rebalance-busy line (the usual state)
    ]
    counters = absent[term]
    assert _fires(*counters)
    assert not _fires(*counters, query=mutant)


def test_the_query_takes_the_rebalance_busy_lines_off_the_503s() -> None:
    """The mutant with the wrong sign on the third counter, made in the
    query's own text: a marked 503 would page."""
    query = _promql(ALERT_503)["query"]
    lines = query.splitlines()
    minus = [index for index, line in enumerate(lines) if line.strip() == "-"]
    assert len(minus) == 2
    lines[minus[1]] = lines[minus[1]].replace("-", "+")
    mutant = "\n".join(lines)
    assert not _fires(1, None, 1)
    assert _fires(1, None, 1, query=mutant)


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
    assert with_promql == ["billing-authorize-503.yaml", "billing-rebalance-busy.yaml"]
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


# ---------------------------------------------------------------------------
# The gate, run against a stub gcloud.

STUB_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" | tr '\n' ' ' >>"$STUB_LOG"; printf '\n' >>"$STUB_LOG"
query="$3"
case "$query" in
  *"NOT (httpRequest.status=503"*) cat "$STUB_DIR/other.txt" ;;
  *"billing.lease_headroom_unavailable"*"billing.credit_rebalance_busy"*)
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
    assert "not paired with a lease-headroom or rebalance-busy line" in result.stdout
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
    assert "paired with a line of the app: 1 (lease headroom: 0, rebalance busy: 1), unpaired: 0" in result.stdout
    assert "gate passed" in result.stdout
    # One read of the lines was enough.
    assert sum("billing.credit_rebalance_busy" in call for call in calls) == 1


def test_gate_passes_with_a_503_of_each_kind(tmp_path: Path) -> None:
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(OTHER_TRACE)],
        lines=[_busy_line_entry(OTHER_TRACE), _line_entry(TRACE)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "authorize 503s: 2, paired with a line of the app: 2 (lease headroom: 1, rebalance busy: 1), unpaired: 0" in result.stdout


def test_gate_fails_on_an_unmarked_503_beside_one_of_each_kind(tmp_path: Path) -> None:
    """A storage outage during a rollout that also saw both designed 503s:
    the unmarked one is left over and fails the gate."""
    result, _calls = _run_gate(
        tmp_path,
        requests=[_authorize_503(TRACE), _authorize_503(THIRD_TRACE), _authorize_503(OTHER_TRACE)],
        lines=[_line_entry(TRACE), _busy_line_entry(OTHER_TRACE)],
        env={"TR_BILLING_5XX_LINE_RETRIES": "0"},
    )
    assert result.returncode == 1
    assert "(lease headroom: 1, rebalance busy: 1), unpaired: 1" in result.stdout
    assert "unpaired 503: 2026-11-13T13:04:15.208934Z" in result.stdout
    assert f"traces/{THIRD_TRACE}" in result.stdout
    assert f"traces/{TRACE}" not in result.stdout and f"traces/{OTHER_TRACE}" not in result.stdout
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
    assert "(lease headroom: 1, rebalance busy: 1), unpaired: 0" in result.stdout


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


def test_the_gates_line_read_is_both_lines_of_the_candidate_revision_alone(tmp_path: Path) -> None:
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

    def entry(text: str, **changes: str) -> dict[str, Any]:
        log = request_log(503, "/internal/gateway/authorize")
        log.pop("httpRequest")
        log["resource"]["labels"].update(
            {key: value for key, value in changes.items() if key != "timestamp"}
        )
        return {**log, "timestamp": changes.get("timestamp", log["timestamp"]), "textPayload": text}

    for line in (headroom, busy):
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
# gateway_reliability.sh: the counters before any policy, the rebalance-busy
# policy before the authorize-503 policy stops counting those 503s, and the
# authorize-503 policy before the narrowed 5xx filter.

STUB_RELIABILITY_GCLOUD = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
args="$*"
case "$args" in
  *"logging metrics describe"*) exit 1 ;;
  *"logging metrics create ${STUB_FAIL_METRIC:-none} "*) exit 1 ;;
  *"monitoring channels list"*) echo "projects/p/notificationChannels/1" ;;
  *"monitoring policies list"*"displayName=\"${STUB_MISSING_POLICY:-none}\""*) ;;
  *"monitoring policies list"*) echo "projects/p/alertPolicies/1" ;;
  *"monitoring policies "*"/${STUB_FAIL_POLICY:-none} "*) exit 1 ;;
esac
exit 0
"""


def _run_reliability(
    tmp_path: Path, *, fail_metric: str = "none", missing_policy: str = "none", fail_policy: str = "none",
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
        "trustedrouter_gateway_billing_slow",
    ]
    # The policy that bounds rebalance-busy 503s, then the one that stops
    # counting them as unmarked, then the 5xx filter that sets authorize
    # 503s aside for it. Each exists before the next gives its 503s up.
    assert "billing-rebalance-busy.yaml" in updates[0]
    assert "billing-authorize-503.yaml" in updates[1]
    narrowed = next(index for index, call in enumerate(updates) if "billing-5xx.yaml" in call)
    assert narrowed > 1
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
    assert "billing-authorize-503.yaml" in policies[1]
    assert sum("monitoring policies create" in call for call in policies) == 1


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
    "metric", ["trustedrouter_gateway_authorize_503", HEADROOM_COUNTER, BUSY_COUNTER]
)
def test_reliability_stops_before_any_policy_without_a_counter(tmp_path: Path, metric: str) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric=metric)
    assert status != 0
    assert not any("monitoring policies" in call for call in calls)


def test_reliability_still_tolerates_the_optional_slow_counter(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric="trustedrouter_gateway_billing_slow")
    assert status == 0
    assert any("billing-5xx.yaml" in call for call in calls if "monitoring policies update" in call)
