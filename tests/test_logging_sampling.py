"""Log sampling exclusions (infra/logging_sampling.tf).

The launcher's journald export carries enclave lines in jsonPayload.message
(lowercase). Field names are case-sensitive in Cloud Logging filters; a
filter that named only jsonPayload.MESSAGE matched nothing for weeks and kept
every success line. These tests pin that each enclave clause names both
spellings, that failures are never sampled, and the heartbeat sample rate.

Every exclusion is also evaluated against log entries shaped like the real
ones: the success lines it is meant to sample must match it (so the checker
is not vacuous), and no failure line, nor any line a log metric, alert or
deploy gate reads, may match any exclusion. The evaluator below implements
the part of the Logging query language these filters use; an exclusion that
needs more of it fails to parse here rather than passing unchecked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra/logging_sampling.tf").read_text()
ALERT_TF = (ROOT / "infra/boot_integrity_alert.tf").read_text()
PROJECT_LOGS = "projects/quill-cloud-proxy/logs/"


def _heredoc(text: str, start: int) -> str:
    body_start = text.index("<<-EOT", start) + len("<<-EOT")
    return text[body_start: text.index("EOT", body_start)]


def _filter(name: str) -> str:
    return _heredoc(TF, TF.index(f'resource "google_logging_project_exclusion" "{name}"'))


def _clauses(filter_text: str, field: str) -> list[str]:
    return re.findall(rf'jsonPayload\.{field}:"(enclave\.[a-z_]+) "', filter_text)


EXCLUSIONS = re.findall(r'resource "google_logging_project_exclusion" "([a-z_]+)"', TF)


# --- A small evaluator for Cloud Logging filters -----------------------------
#
# Cloud Logging's precedence is NOT, then OR, then AND, so `a AND b OR c`
# means `a AND (b OR c)`, and terms side by side are joined by an implicit
# AND. Missing fields
# (https://cloud.google.com/logging/docs/view/logging-query-language#missing_fields):
# `field:*` is true only when the entry explicitly has the field, defaulted
# or not. A comparison on a missing field is false (so NOT of it is true),
# except on a LogEntry field with a default value, which compares as that
# default. severity always compares as DEFAULT when missing. For the other
# defaulted scalars the filters compare, every entry is evaluated both ways
# (missing compares false, and missing compares as the default), and an
# entry that must be kept may match in neither.

SEVERITY = {
    "DEFAULT": 0, "DEBUG": 100, "INFO": 200, "NOTICE": 300, "WARNING": 400,
    "ERROR": 500, "CRITICAL": 600, "ALERT": 700, "EMERGENCY": 800,
}
ALWAYS_DEFAULTED = {"severity": "DEFAULT"}
# The defaulted LogEntry scalars the filters compare, besides severity.
SCALAR_DEFAULTS = {"httpRequest.status": 0, "httpRequest.latency": "0s"}
_TOKEN = re.compile(r'\s*(?:(\()|(\))|(,)|(=~|!~|!=|<=|>=|=|<|>|:)|("(?:[^"\\]|\\.)*")|([A-Za-z0-9_.*%/+-]+))')


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        assert m and m.end() > pos, f"cannot parse filter at: {text[pos:pos + 40]!r}"
        pos = m.end()
        kinds = ("lp", "rp", "comma", "op", "str", "word")
        kind = next(k for k, g in zip(kinds, m.groups(), strict=True) if g is not None)
        value = m.group(kinds.index(kind) + 1)
        if kind == "str":
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out.append((kind, value))
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _tokens(text)
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Any:
        node = self.and_expr()
        assert self.peek() is None, f"unparsed tail {self.toks[self.i:]}"
        return node

    def and_expr(self) -> Any:
        # Lowest precedence: explicit AND, or two terms side by side.
        nodes = [self.or_expr()]
        while self.peek() is not None and self.peek() != ("rp", ")"):
            if self.peek() == ("word", "AND"):
                self.take()
            nodes.append(self.or_expr())
        return ("and", nodes) if len(nodes) > 1 else nodes[0]

    def or_expr(self) -> Any:
        nodes = [self.unary()]
        while self.peek() == ("word", "OR"):
            self.take()
            nodes.append(self.unary())
        return ("or", nodes) if len(nodes) > 1 else nodes[0]

    def unary(self) -> Any:
        if self.peek() in (("word", "NOT"), ("word", "-")):
            self.take()
            return ("not", self.unary())
        if self.peek() == ("lp", "("):
            self.take()
            node = self.and_expr()
            assert self.take() == ("rp", ")")
            return node
        kind, word = self.take()
        assert kind == "word" and word not in ("AND", "OR"), f"expected a field, got {word!r}"
        if word == "sample":
            # sample(insertId, fraction): the worst case, as if the entry were
            # in the sampled-out share. A failure must not reach this clause.
            while self.take()[0] != "rp":
                pass
            return ("true",)
        kind, op = self.take()
        assert kind == "op", f"expected an operator after {word}, got {op!r}"
        if self.peek() == ("lp", "("):
            self.take()
            values = [self.take()[1]]
            while self.peek() == ("word", "OR"):
                self.take()
                values.append(self.take()[1])
            assert self.take() == ("rp", ")")
            return ("cmp", word, op, values)
        return ("cmp", word, op, [self.take()[1]])


_MISSING = object()


def _explicit(entry: dict[str, Any], path: str) -> Any:
    """The field's value if the entry explicitly has it, else _MISSING."""
    value: Any = entry
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value or value[part] is None:
            return _MISSING
        value = value[part]
    return value


def _compared(entry: dict[str, Any], path: str, scalar_defaults: bool) -> Any:
    """The value a comparison sees: a missing defaulted field reads as its default."""
    value = _explicit(entry, path)
    if value is _MISSING and path in ALWAYS_DEFAULTED:
        return ALWAYS_DEFAULTED[path]
    if value is _MISSING and scalar_defaults and path in SCALAR_DEFAULTS:
        return SCALAR_DEFAULTS[path]
    return value


def _re2(pattern: str) -> str:
    # RE2's `$` matches only at the end of the text; Python's also matches
    # before a final newline, unless written \Z.
    return re.sub(r"(?<!\\)\$$", r"\\Z", pattern)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.fullmatch(r"(-?[0-9.]+)s?", value)
        return float(m.group(1)) if m else None
    return None


def _compare(field: str, actual: Any, op: str, expected: str) -> bool:
    a: Any
    b: Any
    if actual is _MISSING:
        return False
    if field == "severity":
        a, b = SEVERITY[actual], SEVERITY[expected]
    elif op == ":":
        return expected.lower() in str(actual).lower()
    elif op in ("=~", "!~"):
        found = re.search(_re2(expected), str(actual)) is not None
        return found if op == "=~" else not found
    elif isinstance(actual, bool):
        a, b = actual, expected == "true"
        assert op in ("=", "!="), "booleans compare by equality only"
    elif _number(actual) is not None and _number(expected) is not None:
        a, b = _number(actual), _number(expected)
    else:
        a, b = str(actual), expected
    return {
        "=": a == b, "!=": a != b,
        "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
    }[op]


def _eval(node: Any, entry: dict[str, Any], scalar_defaults: bool) -> bool:
    kind = node[0]
    if kind == "true":
        return True
    if kind == "and":
        return all(_eval(n, entry, scalar_defaults) for n in node[1])
    if kind == "or":
        return any(_eval(n, entry, scalar_defaults) for n in node[1])
    if kind == "not":
        return not _eval(node[1], entry, scalar_defaults)
    _, field, op, values = node
    if op == ":" and values == ["*"]:
        # Presence: only a field the entry explicitly has, defaulted or not.
        return _explicit(entry, field) is not _MISSING
    actual = _compared(entry, field, scalar_defaults)
    return any(_compare(field, actual, op, v) for v in values)


def matches(filter_text: str, entry: dict[str, Any], scalar_defaults: bool = False) -> bool:
    return _eval(_Parser(filter_text).parse(), entry, scalar_defaults)


def matches_either_way(filter_text: str, entry: dict[str, Any]) -> bool:
    return matches(filter_text, entry) or matches(filter_text, entry, scalar_defaults=True)


# --- Log entries shaped like the real ones ----------------------------------

def lb(status: int | None, latency: str, details: str = "response_sent_by_backend",
       backend: str = "tr-fastpath-edge-backend", **payload: Any) -> dict[str, Any]:
    http: dict[str, Any] = {"latency": latency, "requestMethod": "POST"}
    if status is not None:
        http["status"] = status
    return {
        "logName": PROJECT_LOGS + "requests",
        "resource": {"type": "http_load_balancer", "labels": {"backend_service_name": backend}},
        "httpRequest": http,
        "jsonPayload": {"statusDetails": details, **payload},
        "severity": "INFO" if status is not None and status < 400 else "WARNING",
    }


STATIC_SITE = "sample_static_site_request_noise"
EXCHANGE = "token-exchange-static"
PUBLIC = "trusted-router-public-backend"
# Cloud Armor's record on the public site's requests (cloud_armor.tf, public_edge),
# as the balancer logs it: ACCEPT on every request the policy passes to the
# backend, DENY from the host rule or from the per-source throttle.
ARMOR_ACCEPT = {
    "name": "trusted-router-public-edge", "priority": 1200, "configuredAction": "THROTTLE", "outcome": "ACCEPT",
    "rateLimitAction": {"key": "192.0.2.10", "outcome": "RATE_LIMIT_THRESHOLD_CONFORM"},
}
ARMOR_THROTTLED = {**ARMOR_ACCEPT, "outcome": "DENY",
                   "rateLimitAction": {"key": "192.0.2.10", "outcome": "RATE_LIMIT_THRESHOLD_EXCEED"}}
ARMOR_HOST_DENY = {"name": "trusted-router-public-edge", "priority": 900, "configuredAction": "DENY", "outcome": "DENY"}
ARMOR_PREVIEW = {"name": "trusted-router-public-edge", "priority": 1000, "configuredAction": "DENY", "outcome": "DENY"}


def site(status: int | None, latency: str, details: str = "response_sent_by_backend",
         backend: str = EXCHANGE, **payload: Any) -> dict[str, Any]:
    """A request to the public site or an exchange site, as the balancer logs it.

    A GET with a Cloud CDN lookup; INFO below 400 and when there is no status,
    WARNING for every status of 400 or more, 5xx included.
    """
    entry = lb(status, latency, details, backend, cacheId="IAD", **payload)
    entry["httpRequest"].update(requestMethod="GET", cacheLookup=True)
    if details.startswith("response_from_cache"):
        entry["httpRequest"]["cacheHit"] = True
    entry["severity"] = "WARNING" if status is not None and status >= 400 else "INFO"
    return entry


def integrity(event: str, severity: str = "NOTICE", **body: Any) -> dict[str, Any]:
    return {
        "logName": PROJECT_LOGS + "compute.googleapis.com%2Fshielded_vm_integrity",
        "resource": {"type": "gce_instance", "labels": {"instance_id": "1", "zone": "us-central1-b"}},
        "jsonPayload": {"bootCounter": "1", event: body},
        "severity": severity,
    }


def run_line(text: str, log: str = "stderr", service: str = "trusted-router") -> dict[str, Any]:
    return {
        "logName": PROJECT_LOGS + f"run.googleapis.com%2F{log}",
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": service}},
        "textPayload": text,
    }


def access(status: str, path: str = "/internal/gateway/heartbeat", method: str = "POST",
           service: str = "trusted-router") -> dict[str, Any]:
    return run_line(f'INFO:     192.0.2.10:40312 - "{method} {path} HTTP/1.1" {status}', "stdout", service)


def run_request(status: int, latency: str, url: str) -> dict[str, Any]:
    return {
        "logName": PROJECT_LOGS + "run.googleapis.com%2Frequests",
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "trusted-router"}},
        "httpRequest": {"status": status, "latency": latency, "requestUrl": url},
    }


def launcher(message: str) -> dict[str, Any]:
    return {
        "logName": PROJECT_LOGS + "confidential-space-launcher",
        "resource": {"type": "gce_instance", "labels": {}},
        "jsonPayload": {"message": message},
    }


GW = "INFO trusted_router.routes.internal.gateway "
AID = "gwa-" + "0" * 32
SETTLE = (f"{GW}settle timing authorization_id={AID} success=True origin=typed total_ms=41.2 auth_ms=3.1 "
          "enqueue_ms=0.0 finalize_ms=30.4 mark_ms=1.2 commit_path=one_commit one_commit_ms=29.9 boot_auth=absent")
FINALIZE = f"INFO trusted_router.storage_gcp typed finalize timing authorization_id={AID} spanner_ms=28.1 index_ms=0.0 attempts=1"
# The speculation timing line is logged in a finally block with its fallback
# fields still at their initial values when the transaction raises
# (storage_gcp_authorize.py), so a FailedPrecondition or a DeadlineExceeded
# logs exactly the routine form; none of them may be sampled.
SPECULATION = ("INFO trusted_router.storage_gcp_authorize typed finalize speculation timing eligible_attempts=1 "
               "attempts=1 fallback_reason=none rollback_ms=0.0 remaining_ms=19000.0 fallback_outcome=not_attempted")
SPECULATION_FAILED_PRECONDITION = SPECULATION.replace("remaining_ms=19000.0", "remaining_ms=18950.4")
SPECULATION_DEADLINE_EXCEEDED = SPECULATION.replace("remaining_ms=19000.0", "remaining_ms=0.0")
KEY_SOURCE = f"{GW}gateway.idempotency_key_source source=gateway declared=gateway image_digest=sha256:{'a' * 64}"

# What each new exclusion is meant to sample, so the evaluator is shown to
# match real lines (a parser that matched nothing would pass every test below).
SAMPLED = {
    "sample_cloud_run_request_successes": [run_request(200, "0.2s", "https://x/internal/gateway/settle")],
    "sample_enclave_success_lifecycle": [
        launcher('enclave.request_end method="POST" route="/v1/chat/completions" status=200 outcome="ok" ms=812'),
        launcher("enclave.request_start method=POST"),
    ],
    "sample_enclave_stage_d_heartbeats": [launcher("enclave.stage_d_heartbeat_sent seq=4")],
    "sample_edge_lb_request_successes": [
        lb(200, "0.183s"),
        lb(200, "0.999s", enforcedSecurityPolicy={"outcome": "ACCEPT"}),
    ],
    "sample_tdx_quote_events": [integrity("tdxLaunchAttestationReportEvent", tdQuoteBody={"reportData": "00"})],
    "sample_control_plane_access_successes": [
        access("200 OK"),
        access("202 Accepted", "/internal/gateway/settle"),
        access("304 Not Modified", "/internal/fastpath/edge-policy", "GET"),
        access("200 OK", "/pricing", "GET", service="trusted-router-public"),
    ],
    "sample_control_plane_routine_lines": [
        run_line(SETTLE),
        run_line(SETTLE.replace("total_ms=41.2", "total_ms=12.3")),
        run_line(SETTLE.replace("total_ms=41.2", "total_ms=999.9")),
        run_line(SETTLE.replace("boot_auth=absent", "boot_auth=verified")),
        {**run_line(SETTLE), "severity": "INFO"},
        run_line(FINALIZE),
        run_line(FINALIZE.replace("spanner_ms=28.1 index_ms=0.0", "spanner_ms=999.9 index_ms=999.9")),
        run_line(f"{GW}gateway.authorize_affinity affinity=cold region=us-east4"),
        run_line(f"{GW}gateway.authorize_affinity affinity=warm_same region=us-central1"),
        run_line(KEY_SOURCE),
        run_line(f"{GW}fastpath pilot passed over reason=key_cap workspace_id=ws_x"),
    ],
    STATIC_SITE: [
        site(200, "0.002s", "response_from_cache"),
        site(200, "0.130s"),
        site(200, "0.120s", "response_from_cache_validated"),
        site(304, "0.004s", "response_from_cache"),
        # A directory brute force's guess, logged at WARNING like every 4xx.
        site(404, "0.140s"),
        # The public site: a cache miss carries Cloud Armor's ACCEPT, a cache hit no Armor record.
        site(200, "0.042s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(200, "0.005s", "response_from_cache", PUBLIC),
        site(304, "0.003s", "response_from_cache", PUBLIC),
        site(308, "0.010s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(404, "0.020s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(404, "0.999s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
    ],
}

# Failure forms, and the forms each new exclusion must keep whole.
KEPT = {
    "edge balancer failures and slow calls": [
        lb(503, "0.020s"),
        lb(504, "8.004314s", "backend_timeout"),
        lb(402, "0.150s"),
        lb(401, "0.090s"),
        lb(429, "0.001s", "denied_by_security_policy"),
        lb(200, "1.2s"),
        lb(200, "4.2s"),
        lb(200, "0.300s", "client_disconnected_after_partial_response"),
        lb(None, "0.050s", "client_disconnected_before_any_response"),
        lb(200, "0.100s", previewSecurityPolicy={"outcome": "DENY"}),
        # Other backends sample at the backend (control_terminal_backend.tf).
        lb(200, "0.100s", backend="trusted-router-control-terminal-c1-backend"),
    ],
    "integrity boot reports and other events": [
        integrity("lateBootReportEvent", policyEvaluationPassed=False),
        integrity("earlyBootReportEvent", policyEvaluationPassed=False),
        integrity("lateBootReportEvent"),
        integrity("lateBootReportEvent", policyEvaluationPassed=True),
        integrity("earlyBootReportEvent", policyEvaluationPassed=True),
        integrity("startupEvent"),
        integrity("shutdownEvent"),
        integrity("tdxLaunchAttestationReportEvent", severity="WARNING"),
        integrity("tdxLaunchAttestationReportEvent", severity="ERROR"),
    ],
    "control plane access failures and other stdout lines": [
        access("500 Internal Server Error", "/internal/gateway/authorize"),
        access("503 Service Unavailable", "/internal/gateway/authorize"),
        access("401 Unauthorized"),
        access("404 Not Found", "/x", "GET", service="trusted-router-public"),
        access("429 Too Many Requests", "/v1/chat/completions"),
        access("200 OK", service="lorehex-site"),
        run_line("LIVENESS_TICK 12.5", "stdout"),
        {**run_line("", "stdout"), "textPayload": None, "jsonPayload": {"event": "acquisition.api_key_created"}},
    ],
    "control plane failure forms": [
        run_line(SETTLE.replace("success=True", "success=False")),
        run_line(SETTLE.replace("commit_path=one_commit", "commit_path=deferred")),
        run_line(SETTLE.replace("boot_auth=absent", "boot_auth=ignored_no_boot_kid")),
        run_line(SETTLE.replace("INFO ", "WARNING ", 1)),
        run_line(SETTLE, service="trusted-router-settle-drain"),
        run_line(FINALIZE.replace("attempts=1", "attempts=2")),
        run_line(FINALIZE.replace("attempts=1", "attempts=12")),
        run_line(SPECULATION),
        run_line(SPECULATION_FAILED_PRECONDITION),
        run_line(SPECULATION_DEADLINE_EXCEEDED),
        run_line(SPECULATION.replace("fallback_reason=none", "fallback_reason=aborted")),
        run_line(SPECULATION.replace("fallback_outcome=not_attempted", "fallback_outcome=committed")),
        run_line(KEY_SOURCE.replace("source=gateway", "source=legacy")),
        run_line(KEY_SOURCE.replace("source=gateway declared=gateway", "source=legacy declared=absent")),
        run_line("WARNING trusted_router.routes.internal.gateway gateway.idempotency_key_source_mismatch "
                 "reason=boot_unverified image_digest=- request_id=r"),
        run_line(f"{GW}fastpath pilot passed over reason=stream.hold workspace_id=ws_x"),
        run_line(f"{GW}fastpath pilot passed over reason=stream.stage_d:stage_d_disabled workspace_id=ws_x"),
    ],
    # A settle or finalize of 1 s or more is kept whole, whatever its other fields.
    "slow timing lines": [
        run_line(SETTLE.replace("total_ms=41.2", "total_ms=20001.2")),
        run_line(SETTLE.replace("total_ms=41.2", "total_ms=1000.0")),
        run_line(SETTLE.replace("total_ms=41.2", "total_ms=1234.5")),
        run_line(FINALIZE.replace("spanner_ms=28.1", "spanner_ms=20001.1")),
        run_line(FINALIZE.replace("spanner_ms=28.1", "spanner_ms=1000.0")),
        run_line(FINALIZE.replace("index_ms=0.0", "index_ms=1500.0")),
        run_line(FINALIZE.replace("spanner_ms=28.1", "spanner_ms=-0.3")),
    ],
    "lines a metric, alert, gate, script or runbook reads": [
        run_line(f"INFO trusted_router.fastpath fastpath pilot terminal won authorization_id={AID} "
                 "kind=settle won=settle charge=1200 outcome="),
        run_line(f"{GW}fastpath edge boot verdict compared result=agree edge=verified python=verified"),
        run_line(f"{GW}fastpath edge boot verdict compared result=differ edge=verified python=invalid"),
        run_line("INFO trusted_router.async_settle_timing metric=async_settle_projection_admission eligible=yes "
                 "admission_ms=1.0 lock_wait_ms=0.0 health=none health_ms=0.0 workspace_ms=0.4"),
        run_line("WARNING trusted_router.storage_gcp_authorize metric=key_usage_unbooked_total value=1"),
        run_line("WARNING trusted_router.services.settle_outbox_drain metric=gateway_hold_released_expired_total value=1"),
        run_line("ERROR trusted_router.fastpath fastpath.heartbeat_divergence authorization_id=x"),
        run_line("ERROR trusted_router.fastpath fastpath.leased_reap_overdue count=1"),
        run_line("INFO trusted_router.routes.client_events client_observed.invisible_outage tenant=t"),
        run_line(f"{GW}billing.lease_headroom_unavailable workspace_id=ws_x"),
        run_request(503, "0.4s", "https://x/internal/gateway/authorize"),
        run_request(500, "0.4s", "https://x/internal/gateway/settle"),
        run_request(200, "12.0s", "https://x/internal/gateway/settle"),
        launcher('enclave.request_end method="POST" route="/v1/chat/completions" status=502 outcome="fail" ms=812'),
        launcher("enclave.invoke_attempt provider=x outcome=fail "),
        launcher("enclave.stage_d_cap_reached streams=64"),
        launcher("settle_outbox.sweep dead=1 quarantined=0"),
    ],
    "public site and exchange site failures, slow calls and Armor matches": [
        site(403, "0.002s", "denied_by_security_policy", PUBLIC, enforcedSecurityPolicy=ARMOR_HOST_DENY),
        site(403, "0.030s"),
        site(429, "0.100s", "denied_by_security_policy", PUBLIC, enforcedSecurityPolicy=ARMOR_THROTTLED),
        site(429, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(400, "0.020s"),
        site(405, "0.010s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(500, "0.200s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(503, "0.050s"),
        # The balancer's own failures.
        site(503, "0.010s", "failed_to_connect_to_backend", PUBLIC),
        site(502, "0.005s", "failed_to_pick_backend", PUBLIC),
        site(None, "0s", "internal_error"),
        # Cloud CDN's revalidation sub-entry: no status, which compares as 0.
        site(None, "0s", "response_from_cache_validated"),
        site(None, "0.050s", "client_disconnected_before_any_response", PUBLIC),
        site(200, "0.300s", "client_disconnected_after_partial_response"),
        site(404, "0.100s", "client_disconnected_after_partial_response"),
        site(200, "1.250s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(200, "1.5s", "response_from_cache"),
        site(404, "1.0s"),
        site(200, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT, previewSecurityPolicy=ARMOR_PREVIEW),
        site(404, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT, previewSecurityPolicy=ARMOR_PREVIEW),
        site(404, "0.040s", previewSecurityPolicy=ARMOR_PREVIEW),
        # Armor's denials also carry a 4xx and denied_by_security_policy; an
        # outcome other than ACCEPT on a served form is kept on its own too.
        site(200, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_HOST_DENY),
        site(404, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_THROTTLED),
        {**site(200, "0.050s"), "severity": "ERROR"},
        {**site(404, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT), "severity": "ERROR"},
        {**site(404, "0.050s"), "severity": "CRITICAL"},
        {**site(200, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT), "severity": "WARNING"},
        # Another backend's 404 is not part of a static site's scan.
        site(404, "0.020s", backend="trusted-router-control-backend"),
        site(404, "0.020s", backend="tr-fastpath-edge-backend"),
    ],
}

KEPT_ENTRIES = [pytest.param(e, id=f"{group}-{i}") for group, entries in KEPT.items() for i, e in enumerate(entries)]


def test_every_exclusion_has_lines_it_samples() -> None:
    assert sorted(SAMPLED) == sorted(EXCLUSIONS)
    for name, entries in SAMPLED.items():
        for entry in entries:
            assert matches(_filter(name), entry), (name, entry)


@pytest.mark.parametrize("entry", KEPT_ENTRIES)
def test_no_exclusion_matches_a_failure_or_a_line_something_reads(entry: dict[str, Any]) -> None:
    for name in EXCLUSIONS:
        assert not matches_either_way(_filter(name), entry), name


NEW_EXCLUSIONS = (
    "sample_edge_lb_request_successes",
    "sample_tdx_quote_events",
    "sample_control_plane_access_successes",
    "sample_control_plane_routine_lines",
)
HIGH_SEVERITY = [
    pytest.param({**entry, "severity": severity}, id=f"{name}-{i}-{severity}")
    for name in NEW_EXCLUSIONS
    for i, entry in enumerate(SAMPLED[name])
    for severity in ("WARNING", "ERROR", "CRITICAL")
] + [
    # The balancer logs every 4xx at WARNING, so the static-site exclusion
    # samples a 404 at WARNING by design; every other entry it samples is held
    # to the same rule as above, and a 404 at ERROR or above is kept.
    pytest.param({**entry, "severity": severity}, id=f"{STATIC_SITE}-{i}-{severity}")
    for i, entry in enumerate(SAMPLED[STATIC_SITE])
    for severity in ("WARNING", "ERROR", "CRITICAL")
    if severity != "WARNING" or entry["httpRequest"]["status"] != 404
]


@pytest.mark.parametrize("entry", HIGH_SEVERITY)
def test_no_exclusion_matches_an_otherwise_sampled_entry_at_warning_or_above(entry: dict[str, Any]) -> None:
    for name in EXCLUSIONS:
        assert not matches_either_way(_filter(name), entry), name


def test_the_new_exclusions_still_match_default_info_and_notice() -> None:
    for name in ("sample_edge_lb_request_successes", "sample_control_plane_access_successes",
                 "sample_control_plane_routine_lines", STATIC_SITE):
        entry = SAMPLED[name][0]
        for severity in (None, "DEFAULT", "INFO", "NOTICE"):
            assert matches(_filter(name), {**entry, "severity": severity}), (name, severity)


def test_the_duration_bounds_keep_one_second_and_more() -> None:
    f = _filter("sample_control_plane_routine_lines")
    settle = re.search(r'textPayload=~"( total_ms=[^"]+)"', f)
    finalize = re.search(r'textPayload=~"( spanner_ms=[^"]+)"', f)
    assert settle and finalize
    # No backslashes, only character classes and counts, which RE2 and Python
    # read alike; the finalize pattern's trailing `$` goes through _re2, since
    # Python's `$` alone would also match before a final newline.
    assert "\\" not in settle.group(1) + finalize.group(1)
    settle_re, finalize_re = _re2(settle.group(1)), _re2(finalize.group(1))
    assert re.search(settle_re, " total_ms=12.3 ")
    assert re.search(settle_re, " total_ms=999.9 ")
    assert not re.search(settle_re, " total_ms=20001.2 ")
    assert not re.search(settle_re, " total_ms=1000.0 ")
    assert re.search(finalize_re, " spanner_ms=12.3 index_ms=0.0 attempts=1")
    assert not re.search(finalize_re, " spanner_ms=12.3 index_ms=0.0 attempts=1\n")
    assert not re.search(finalize_re, " spanner_ms=20001.1 index_ms=0.0 attempts=1")
    assert not re.search(finalize_re, " spanner_ms=12.3 index_ms=1500.0 attempts=1")
    assert not re.search(finalize_re, " spanner_ms=12.3 index_ms=0.0 attempts=12")


def test_the_check_fails_a_filter_that_would_sample_failures() -> None:
    # Each guard removed in turn lets a kept entry match: the tests above
    # would fail on the loosened filter.
    edge = _filter("sample_edge_lb_request_successes")
    loose = re.sub(r"\n\s*AND httpRequest\.(status|latency)<[^\n]+", "", edge)
    assert loose != edge
    # At INFO, so only the status and latency guards stand between it and the sample.
    assert matches(loose, {**lb(504, "8.004314s"), "severity": "INFO"})
    assert not matches(edge, {**lb(504, "8.004314s"), "severity": "INFO"})
    loose = edge.replace("AND severity<WARNING", "")
    assert matches(loose, {**lb(200, "0.183s"), "severity": "ERROR"})
    routine = _filter("sample_control_plane_routine_lines")
    loose = routine.replace('AND textPayload:" success=True "', "")
    assert matches(loose, run_line(SETTLE.replace("success=True", "success=False")))
    loose = re.sub(r'\n\s*AND textPayload=~" total_ms=[^\n]+', "", routine)
    assert loose != routine
    assert matches(loose, run_line(SETTLE.replace("total_ms=41.2", "total_ms=20001.2")))
    loose = routine.replace("AND severity<WARNING", "")
    assert matches(loose, {**run_line(SETTLE), "severity": "WARNING"})
    loose = _filter("sample_control_plane_access_successes").replace("[23][0-9][0-9]", "[0-9]+")
    assert matches(loose, access("503 Service Unavailable"))


def _loosened(filter_text: str, pattern: str, replacement: str = "") -> str:
    loose, count = re.subn(pattern, replacement, filter_text)
    assert count == 1, (pattern, count)
    return loose


# Each guard of the static-site exclusion, and entries that guard alone
# keeps: with the guard taken out, each would be sampled.
STATIC_SITE_GUARDS = [
    # LB logs put every status of 400 or more at WARNING, which the 404
    # branch admits, so its status is what keeps the other 4xx and the 5xx.
    pytest.param(r"httpRequest\.status=404 AND ", [
        site(500, "0.200s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(503, "0.050s"),
        site(403, "0.030s"),
        site(429, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
    ], id="status-404"),
    # Cloud Logging compares a missing status as 0, so without the lower
    # bound status<400 would sample Cloud CDN's revalidation sub-entry, which
    # has none and a statusDetails the exclusion otherwise admits.
    pytest.param(r"httpRequest\.status>=200 AND ", [
        site(None, "0s", "response_from_cache_validated"),
        {**site(None, "0.020s", "response_from_cache_validated", PUBLIC), "severity": "DEFAULT"},
    ], id="status-lower-bound"),
    # At INFO, so only the upper bound stands between them and the sample.
    pytest.param(r" AND httpRequest\.status<400", [
        {**site(503, "0.050s"), "severity": "INFO"},
        {**site(403, "0.030s"), "severity": "INFO"},
    ], id="status-upper-bound"),
    pytest.param(r"\n\s*AND httpRequest\.latency<[^\n]+", [
        site(200, "1.250s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT),
        site(200, "1.5s", "response_from_cache"),
        site(404, "1.0s"),
    ], id="latency"),
    pytest.param(r"\n\s*AND resource\.labels\.backend_service_name=[^\n]+", [
        site(404, "0.020s", backend="trusted-router-control-backend"),
        site(404, "0.020s", backend="tr-fastpath-edge-backend"),
    ], id="backend"),
    pytest.param(r"\n\s*AND jsonPayload\.statusDetails=[^\n]+", [
        site(404, "0.100s", "client_disconnected_after_partial_response"),
        site(200, "0.300s", "client_disconnected_after_partial_response"),
    ], id="status-details"),
    pytest.param(r"\n\s*AND NOT jsonPayload\.previewSecurityPolicy:\*", [
        site(404, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT, previewSecurityPolicy=ARMOR_PREVIEW),
        site(404, "0.040s", previewSecurityPolicy=ARMOR_PREVIEW),
    ], id="armor-preview"),
    pytest.param(r"\n\s*AND \(NOT jsonPayload\.enforcedSecurityPolicy:\*[^\n]+", [
        site(200, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_HOST_DENY),
        site(404, "0.040s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_THROTTLED),
    ], id="armor-enforced"),
    pytest.param(r" AND severity<ERROR", [
        {**site(404, "0.050s"), "severity": "ERROR"},
        {**site(404, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT), "severity": "CRITICAL"},
    ], id="severity-404"),
    pytest.param(r" AND severity<WARNING", [
        {**site(200, "0.050s"), "severity": "ERROR"},
        {**site(200, "0.050s", backend=PUBLIC, enforcedSecurityPolicy=ARMOR_ACCEPT), "severity": "WARNING"},
    ], id="severity-2xx-3xx"),
]


@pytest.mark.parametrize("pattern,entries", STATIC_SITE_GUARDS)
def test_each_static_site_guard_keeps_what_it_guards(pattern: str, entries: list[dict[str, Any]]) -> None:
    f = _filter(STATIC_SITE)
    loose = _loosened(f, pattern)
    for entry in entries:
        assert not matches_either_way(f, entry), entry
        assert matches_either_way(loose, entry), entry


def test_static_site_armor_guard_names_the_outcome_not_the_field() -> None:
    # The public site's policy logs enforcedSecurityPolicy, outcome ACCEPT, on
    # every request it passes to the backend; a guard on the field's presence
    # would sample no cache miss and none of a scan's 404s.
    f = _filter(STATIC_SITE)
    presence = _loosened(f, r"\(NOT jsonPayload\.enforcedSecurityPolicy:\* OR [^)\n]+\)",
                         "NOT jsonPayload.enforcedSecurityPolicy:*")
    for entry in SAMPLED[STATIC_SITE]:
        assert matches(f, entry), entry
    accepted = [e for e in SAMPLED[STATIC_SITE] if "enforcedSecurityPolicy" in e["jsonPayload"]]
    assert accepted
    for entry in accepted:
        assert not matches(presence, entry), entry


# --- The evaluator follows Cloud Logging's rules -------------------------------

def test_evaluator_precedence_is_not_then_or_then_and() -> None:
    entry = {"a": "1", "c": "1"}  # a true, b false, c true
    # a AND (b OR c), not (a AND b) OR c
    assert matches('a="1" AND b="1" OR c="1"', entry)
    assert not matches('a="0" AND b="1" OR c="1"', entry)
    # NOT binds tighter than OR: (NOT a) OR c
    assert matches('NOT a="1" OR c="1"', entry)
    assert not matches('NOT a="1" OR b="1"', entry)


def test_evaluator_joins_adjacent_terms_with_and() -> None:
    entry = {"a": "1", "b": "2"}
    assert matches('a="1" b="2"', entry)
    assert not matches('a="1" b="3"', entry)
    assert matches('a="1" b="3" OR b="2"', entry)


def test_evaluator_absent_fields_and_defaults() -> None:
    entry = {"httpRequest": {"latency": "0.1s"}}
    assert not matches("httpRequest.status<400", entry)
    assert matches("NOT httpRequest.status<400", entry)
    assert not matches("jsonPayload.x:*", entry)
    assert matches("NOT jsonPayload.x:*", entry)
    # Read as its default, a missing status compares as 0.
    assert matches("httpRequest.status<400", entry, scalar_defaults=True)
    assert not matches("NOT httpRequest.status<400", entry, scalar_defaults=True)


def test_evaluator_presence_needs_the_field_even_when_it_has_a_default() -> None:
    for scalar_defaults in (False, True):
        # severity compares as DEFAULT when missing, but is not present.
        assert not matches("severity:*", {}, scalar_defaults)
        assert matches("NOT severity:*", {}, scalar_defaults)
        assert matches("severity=DEFAULT", {}, scalar_defaults)
        assert matches("severity<WARNING", {}, scalar_defaults)
        assert not matches("severity>=WARNING", {}, scalar_defaults)
        assert matches("severity:*", {"severity": "DEFAULT"}, scalar_defaults)
        assert not matches("httpRequest.status:*", {"httpRequest": {}}, scalar_defaults)
        assert not matches("httpRequest.latency:*", {"httpRequest": {"status": 200}}, scalar_defaults)


def test_evaluator_missing_latency_compares_as_zero_when_defaulted() -> None:
    entry = {"httpRequest": {"status": 200}}
    assert not matches('httpRequest.latency<"1s"', entry)
    assert matches('NOT httpRequest.latency<"1s"', entry)
    assert matches('httpRequest.latency<"1s"', entry, scalar_defaults=True)
    assert not matches('httpRequest.latency>="1s"', entry, scalar_defaults=True)
    assert matches('httpRequest.latency<"1s"', {}, scalar_defaults=True)
    # A must-keep edge entry without latency stays out of the sample either
    # way: it is not a response the edge sent.
    no_latency = lb(200, "0s", "client_disconnected_before_any_response")
    del no_latency["httpRequest"]["latency"]
    assert not matches_either_way(_filter("sample_edge_lb_request_successes"), no_latency)


def _gateway_reliability_metrics() -> dict[str, str]:
    script = (ROOT / "scripts/deploy/gateway_reliability.sh").read_text()
    found = dict(re.findall(r'ensure_log_metric "\$(\w+)" \\\n\s*"(?:[^"\\]|\\.)*" \\\n\s*\'([^\']+)\'', script))
    assert len(found) >= 6, found
    return found


# An entry each gateway log metric counts (scripts/deploy/gateway_reliability.sh).
GATEWAY_METRIC_ENTRIES = {
    "SLOW_METRIC_NAME": run_request(200, "12.5s", "https://x/internal/gateway/settle"),
    "AUTHORIZE_503_METRIC_NAME": run_request(503, "0.3s", "https://x/internal/gateway/authorize"),
    "LEASE_HEADROOM_METRIC_NAME": run_line(f"{GW}billing.lease_headroom_unavailable workspace_id=ws_x"),
    "REBALANCE_BUSY_METRIC_NAME": run_line(f"{GW}billing.credit_rebalance_busy workspace_id=ws_x"),
    "STRICT_BUDGET_BUSY_METRIC_NAME": run_line(f"{GW}billing.strict_budget_busy key=k"),
    "HEADROOM_RACED_METRIC_NAME": run_line(f"{GW}billing.credit_headroom_raced workspace_id=ws_x"),
    "SETTLE_503_METRIC_NAME": run_request(503, "0.3s", "https://x/internal/gateway/settle"),
    "SETTLE_UNAVAILABLE_METRIC_NAME": run_line(
        "WARNING trusted_router.services.async_settle_handler async_settle.unavailable reason=finish_unconfirmed"
    ),
}


def test_no_exclusion_matches_a_line_a_gateway_log_metric_counts() -> None:
    metrics = _gateway_reliability_metrics()
    assert sorted(metrics) == sorted(GATEWAY_METRIC_ENTRIES)
    for name, metric_filter in metrics.items():
        entry = GATEWAY_METRIC_ENTRIES[name]
        assert matches(metric_filter, entry), f"the fixture for {name} no longer matches its metric"
        for exclusion in EXCLUSIONS:
            assert not matches(_filter(exclusion), entry), (name, exclusion)


def test_no_exclusion_matches_a_line_a_gateway_alert_reads() -> None:
    alerts = {}
    for path in sorted((ROOT / "scripts/deploy/gateway-alerts").glob("*.yaml")):
        for f in re.findall(r"^\s*filter: '([^']+)'", path.read_text(), re.M):
            if "textPayload" in f or "httpRequest" in f:
                alerts[path.name] = f
    entries = {
        "billing-5xx.yaml": run_request(500, "0.4s", "https://x/internal/gateway/settle"),
        "client-invisible-outage.yaml": run_line("INFO trusted_router.routes.client_events client_observed.invisible_outage t"),
        "client-telemetry-stale.yaml": run_line("WARNING trusted_router.client_telemetry client_observed.pipeline_stale"),
    }
    assert sorted(alerts) == sorted(entries)
    for name, alert_filter in alerts.items():
        assert matches(alert_filter, entries[name]), name
        for exclusion in EXCLUSIONS:
            assert not matches(_filter(exclusion), entries[name]), (name, exclusion)


# --- Pins on each exclusion --------------------------------------------------

def test_enclave_success_sampling_names_both_field_spellings() -> None:
    f = _filter("sample_enclave_success_lifecycle")
    lower, upper = _clauses(f, "message"), _clauses(f, "MESSAGE")
    assert lower, "the lowercase field the launcher actually exports must be matched"
    assert sorted(lower) == sorted(upper)
    assert set(lower) == {
        "enclave.request_accept",
        "enclave.request_start",
        "enclave.request_end",
        "enclave.invoke_attempt",
        "enclave.invoke_complete",
    }
    # Only successful ends and invokes are sampled; failures always stay.
    assert f.count('status=200 outcome=\\"ok\\"') == 2
    assert f.count('" outcome=ok "') == 4
    assert "sample(insertId, 0.9)" in f


def test_stage_d_heartbeats_keep_one_percent_and_only_success_lines() -> None:
    f = _filter("sample_enclave_stage_d_heartbeats")
    lines = {"enclave.stage_d_heartbeat_sent", "enclave.stage_d_heartbeat_accepted"}
    assert set(_clauses(f, "message")) == lines
    assert set(_clauses(f, "MESSAGE")) == lines
    assert "sample(insertId, 0.99)" in f
    assert "stage_d_cap" not in f and "refus" not in f and "fail" not in f


def test_every_exclusion_samples_rather_than_drops() -> None:
    assert EXCLUSIONS, "no exclusions found"
    for name in EXCLUSIONS:
        assert re.search(r"sample\(insertId, 0\.9{1,2}\)", _filter(name)), name


def test_edge_balancer_sampling_is_scoped_to_the_edge_and_keeps_failures() -> None:
    f = _filter("sample_edge_lb_request_successes")
    assert 'resource.labels.backend_service_name="tr-fastpath-edge-backend"' in f
    assert "AND severity<WARNING" in f
    assert "httpRequest.status<400" in f
    # 1 s, the edge's budget in docs/runbooks/fastpath-alerts.md (edge_decide_pass_slow).
    assert 'httpRequest.latency<"1s"' in f
    assert 'jsonPayload.statusDetails="response_sent_by_backend"' in f
    assert "NOT jsonPayload.previewSecurityPolicy:*" in f
    assert "sample(insertId, 0.9)" in f
    # The backend itself still logs every request: sampling there would
    # sample the failures too.
    backend = (ROOT / "infra/fastpath_edge_backend.tf").read_text()
    block = backend[backend.index('resource "google_compute_backend_service" "fastpath_edge"'):]
    assert "sample_rate" not in block


def test_static_site_sampling_is_scoped_to_the_two_site_backends() -> None:
    f = _filter(STATIC_SITE)
    assert 'logName="projects/quill-cloud-proxy/logs/requests"' in f
    assert 'resource.type="http_load_balancer"' in f
    backends = re.search(r"resource\.labels\.backend_service_name=\(([^)]*)\)", f)
    assert backends and set(re.findall(r'"([^"]+)"', backends.group(1))) == {EXCHANGE, PUBLIC}
    # Answered by the backend or by Cloud CDN; client disconnects, byte range
    # forms and the balancer's own failures stay whole.
    details = re.search(r"jsonPayload\.statusDetails=\(([^)]*)\)", f)
    assert details and set(re.findall(r'"([^"]+)"', details.group(1))) == {
        "response_sent_by_backend", "response_from_cache", "response_from_cache_validated",
    }
    # 1 s, the edge exclusion's bound.
    assert 'httpRequest.latency<"1s"' in f
    assert "sample(insertId, 0.9)" in f


def test_tdx_quote_sampling_keeps_every_boot_report() -> None:
    f = _filter("sample_tdx_quote_events")
    assert "jsonPayload.tdxLaunchAttestationReportEvent:*" in f
    assert "NOT jsonPayload.earlyBootReportEvent:*" in f
    assert "NOT jsonPayload.lateBootReportEvent:*" in f
    assert "severity<=NOTICE" in f
    assert "policyEvaluationPassed" not in f
    assert "sample(insertId, 0.9)" in f


def test_access_line_sampling_keeps_only_2xx_and_3xx() -> None:
    f = _filter("sample_control_plane_access_successes")
    assert "run.googleapis.com%2Fstdout" in f
    assert "AND severity<WARNING" in f
    assert re.search(r'textPayload=~"\^INFO: .* \[23\]\[0-9\]\[0-9\] "', f)
    assert "sample(insertId, 0.9)" in f


def test_routine_line_sampling_names_only_info_lines_and_their_success_forms() -> None:
    f = _filter("sample_control_plane_routine_lines")
    assert 'resource.labels.service_name="trusted-router"' in f
    assert "AND severity<WARNING" in f
    prefixes = re.findall(r'textPayload:"((?:INFO|WARNING|ERROR) [^"]+)"', f)
    assert prefixes and all(p.startswith("INFO trusted_router.") for p in prefixes)
    for guard in ('" success=True "', '" total_ms=[0-9]{1,3}[.][0-9] "', '" commit_path=one_commit "',
                  '" spanner_ms=[0-9]{1,3}[.][0-9] index_ms=[0-9]{1,3}[.][0-9] attempts=1$"',
                  "source=gateway declared=gateway ", "reason=key_cap "):
        assert guard in f, guard
    # Its routine form is also the form it logs when the transaction raises.
    assert "speculation" not in f
    for read_elsewhere in ("billing.", "metric=", "fastpath.", "client_observed", "terminal won", "verdict compared"):
        assert read_elsewhere not in f, read_elsewhere
    assert "sample(insertId, 0.9)" in f


# --- The boot integrity alert (infra/boot_integrity_alert.tf) -----------------

def _alert_filter() -> str:
    return _heredoc(ALERT_TF, ALERT_TF.index("condition_matched_log"))


@pytest.mark.parametrize("entry,pages", [
    (integrity("lateBootReportEvent", policyEvaluationPassed=False), True),
    (integrity("earlyBootReportEvent", policyEvaluationPassed=False), True),
    (integrity("lateBootReportEvent"), True),
    (integrity("lateBootReportEvent", policyEvaluationPassed=True), False),
    (integrity("earlyBootReportEvent", policyEvaluationPassed=True), False),
    (integrity("tdxLaunchAttestationReportEvent"), False),
    (integrity("startupEvent"), False),
])
def test_boot_integrity_alert_pages_on_a_report_that_did_not_pass(entry: dict[str, Any], pages: bool) -> None:
    assert matches(_alert_filter(), entry) is pages


def test_boot_integrity_alert_reaches_the_security_channel_at_a_bounded_rate() -> None:
    assert "data.google_monitoring_notification_channel.security_alerts.name" in ALERT_TF
    assert 'period = "3600s"' in ALERT_TF
    assert "compute.googleapis.com%2Fshielded_vm_integrity" in _alert_filter()
