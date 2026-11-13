"""The billing 5xx alert and deploy gate set aside the lease-headroom 503 and
nothing else.

With TR_FASTPATH_LEASE_HEADROOM_503 on, authorize answers 503 (Retry-After: 1)
when a workspace's shortfall is held in fast-path leases. Cloud Run's request
log records only status and URL, so the 503 is told apart by the app's
billing.lease_headroom_unavailable line:

* the deploy gate (scripts/deploy/assert_no_billing_5xx.sh) pairs each
  authorize 503 with a line by trace, and fails on any 503 left over;
* "TR Gateway: billing path 5xx" (gateway-alerts/billing-5xx.yaml) sets
  authorize 503s aside, and "TR Gateway: authorize 503 not lease headroom"
  (gateway-alerts/billing-authorize-503.yaml) pages when the authorize 503
  count and the line count differ.

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


# ---------------------------------------------------------------------------
# A Logging query evaluator for the constructs these filters use: AND, NOT,
# parentheses, and comparisons =, !=, <, <=, >, >=, =~ (regex search) and :
# (substring) on dotted field paths. A missing field never matches.


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


def matches(filter_text: str, entry: dict[str, Any]) -> bool:
    """Whether a log entry matches a filter. Newlines join terms with AND."""
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
        result = unary()
        while peek() == ("word", "AND"):
            take()
            result = unary() and result
        assert peek() != ("word", "OR"), "OR is not used by these filters"
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
        "trustedrouter_gateway_lease_headroom_503",
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


# ---------------------------------------------------------------------------
# The app's line.


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


# The PromQL condition, evaluated with PromQL's empty-vector semantics on a
# model of the two summed counters: None is a vector with no series.

_QUERY = re.compile(
    r"^abs\(\s*\(sum\(increase\(logging_googleapis_com:user_(?P<a>[a-z_0-9]+)"
    r'\{monitored_resource="cloud_run_revision"\}\[(?P<wa>\d+m)\]\)\) or vector\(0\)\)\s*-\s*'
    r"\(sum\(increase\(logging_googleapis_com:user_(?P<b>[a-z_0-9]+)"
    r'\{monitored_resource="cloud_run_revision"\}\[(?P<wb>\d+m)\]\)\) or vector\(0\)\)\s*'
    r"\) > (?P<threshold>[0-9.]+)\s*$"
)


def _query() -> tuple[dict[str, Any], re.Match[str]]:
    policy = yaml.safe_load(ALERT_503.read_text(encoding="utf-8"))
    (condition,) = policy["conditions"]
    promql = condition["conditionPrometheusQueryLanguage"]
    match = _QUERY.match(promql["query"].strip())
    assert match, promql["query"]
    return promql, match


def _fires(authorize_503s: float | None, headroom_lines: float | None, *, or_zero: bool = True) -> bool:
    def side(value: float | None) -> float | None:
        return 0.0 if value is None and or_zero else value

    a, b = side(authorize_503s), side(headroom_lines)
    if a is None or b is None:
        return False  # binary operator on an empty vector: no series, no alert
    _promql, match = _query()
    return abs(a - b) > float(match["threshold"])


def test_the_authorize_503_policy_compares_the_two_counters() -> None:
    promql, match = _query()
    assert match["a"] == "trustedrouter_gateway_authorize_503"
    assert match["b"] == "trustedrouter_gateway_lease_headroom_503"
    assert match["wa"] == match["wb"] == "10m"
    assert promql["duration"] == "180s"
    assert promql["evaluationInterval"] == "60s"
    policy = yaml.safe_load(ALERT_503.read_text(encoding="utf-8"))
    assert policy["displayName"] == "TR Gateway: authorize 503 not lease headroom"
    assert policy["enabled"] is True
    assert "TR Gateway: authorize 503 not lease headroom" in _alert_5xx_doc()


def _alert_5xx_doc() -> str:
    return str(yaml.safe_load(ALERT_5XX.read_text(encoding="utf-8"))["documentation"]["content"])


def test_a_non_lease_authorize_503_trips_the_authorize_503_policy() -> None:
    entry = request_log(503, "/internal/gateway/authorize")
    counter_a = _authorize_503_counter()
    counter_b = _metric_filters()["trustedrouter_gateway_lease_headroom_503"]
    # A real 503: the request log counts, and no line is written.
    assert matches(counter_a, entry) and not matches(counter_b, entry)
    assert _fires(1, None)
    # Among many lease-headroom 503s, one real 503 still pages.
    assert _fires(101, 100)
    # Lease-headroom 503s, each with its line, do not.
    assert not _fires(100, 100)
    assert not _fires(None, None)
    # Lines with no 503 behind them could hide real ones: they page too.
    assert _fires(None, 1)
    assert _fires(5, 7)


def test_without_or_vector_zero_no_lines_would_silence_every_503() -> None:
    """The mutant the query's "or vector(0)" kills."""
    assert _fires(3, None)
    assert not _fires(3, None, or_zero=False)


# ---------------------------------------------------------------------------
# The gate, run against a stub gcloud.

STUB_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" | tr '\n' ' ' >>"$STUB_LOG"; printf '\n' >>"$STUB_LOG"
query="$3"
case "$query" in
  *"NOT (httpRequest.status=503"*) cat "$STUB_DIR/other.txt" ;;
  *"billing.lease_headroom_unavailable"*)
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
    assert "not paired with a lease-headroom line" in result.stdout
    # It waited for the line, three reads, before failing.
    assert sum("billing.lease_headroom_unavailable" in call for call in calls) == 3


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
# gateway_reliability.sh: the counters before any policy, and the
# authorize-503 policy before the narrowed 5xx filter.

STUB_RELIABILITY_GCLOUD = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
args="$*"
case "$args" in
  *"logging metrics describe"*) exit 1 ;;
  *"logging metrics create ${STUB_FAIL_METRIC:-none} "*) exit 1 ;;
  *"monitoring channels list"*) echo "projects/p/notificationChannels/1" ;;
  *"monitoring policies list"*) echo "projects/p/alertPolicies/1" ;;
esac
exit 0
"""


def _run_reliability(tmp_path: Path, *, fail_metric: str = "none") -> tuple[int, list[str]]:
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
            "TR_REGIONS": "us-central1",
        },
    )
    return result.returncode, log.read_text(encoding="utf-8").splitlines()


def test_reliability_applies_counters_then_the_authorize_503_policy_first(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path)
    assert status == 0, calls
    creates = [call for call in calls if "logging metrics create" in call]
    updates = [call for call in calls if "monitoring policies update" in call]
    assert [call.split("logging metrics create ")[1].split()[0] for call in creates] == [
        "trustedrouter_gateway_authorize_503",
        "trustedrouter_gateway_lease_headroom_503",
        "trustedrouter_gateway_billing_slow",
    ]
    assert "billing-authorize-503.yaml" in updates[0]
    assert len(updates) == len(list((DEPLOY / "gateway-alerts").glob("*.yaml")))
    assert calls.index(creates[-1]) < calls.index(updates[0])


@pytest.mark.parametrize(
    "metric", ["trustedrouter_gateway_authorize_503", "trustedrouter_gateway_lease_headroom_503"]
)
def test_reliability_stops_before_any_policy_without_a_counter(tmp_path: Path, metric: str) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric=metric)
    assert status != 0
    assert not any("monitoring policies" in call for call in calls)


def test_reliability_still_tolerates_the_optional_slow_counter(tmp_path: Path) -> None:
    status, calls = _run_reliability(tmp_path, fail_metric="trustedrouter_gateway_billing_slow")
    assert status == 0
    assert any("billing-5xx.yaml" in call for call in calls if "monitoring policies update" in call)
