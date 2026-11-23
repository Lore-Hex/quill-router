"""Cloud Logging ingestion guards (infra/log_ingestion_cost_guard.tf).

The daily ceiling was a hand-made policy until it was adopted here, and the
security baseline check reads its threshold against an expected value of its
own, so the two must never drift apart. The step-change policy is what catches
a sudden jump (Data Access DATA_READ coming back, a sampling exclusion going
missing); its query is pinned so a rewrite cannot quietly stop comparing like
with like.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = (ROOT / "infra" / "log_ingestion_cost_guard.tf").read_text()
BASELINE = (ROOT / "scripts" / "deploy" / "gcp_security_baseline_check.sh").read_text()
GIB = 1024**3


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _resource(name: str) -> str:
    code = _code(GUARD)
    start = code.index(f'resource "google_monitoring_alert_policy" "{name}" {{')
    depth = 0
    for index in range(start, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[start : index + 1]
    raise AssertionError(f"unterminated resource {name}")


def _ceiling_bytes() -> int:
    match = re.search(r"log_ingestion_ceiling_bytes\s*=\s*(\d+)", _code(GUARD))
    assert match, "ceiling local missing"
    return int(match.group(1))


def _step_query() -> str:
    match = re.search(r"query\s*=\s*<<-EOT\n(.*?)\n\s*EOT", _resource("log_ingestion_step_change"), re.S)
    assert match, "step-change query missing"
    return " ".join(match.group(1).split())


def test_the_hand_made_ceiling_policy_is_adopted_not_recreated() -> None:
    code = _code(GUARD)
    assert re.search(
        r'import\s*\{\s*to\s*=\s*google_monitoring_alert_policy\.log_ingestion_ceiling\s*'
        r'id\s*=\s*"projects/quill-cloud-proxy/alertPolicies/3305751919896009722"\s*\}',
        code,
    )
    # The baseline check finds the policy by this name; keeping it makes the
    # adoption an in-place change.
    assert 'display_name = "TR Logging: ingestion volume spike (audit-log cost guard)"' in _resource(
        "log_ingestion_ceiling"
    )


def test_ceiling_and_the_baseline_check_expect_the_same_threshold() -> None:
    match = re.search(r'INGEST_THRESHOLD_BYTES="\$\{INGEST_THRESHOLD_BYTES:-(\d+)\}"', BASELINE)
    assert match, "baseline expected threshold missing"
    assert int(match.group(1)) == _ceiling_bytes()
    ceiling = _resource("log_ingestion_ceiling")
    assert "threshold_value = local.log_ingestion_ceiling_bytes" in ceiling
    # The condition's name states the value it checks.
    assert f'display_name = "log ingestion > {_ceiling_bytes() // GIB} GiB/day"' in ceiling


def test_ceiling_sums_one_day_of_billed_bytes_for_the_whole_project() -> None:
    ceiling = _resource("log_ingestion_ceiling")
    assert 'metric.type=\\"logging.googleapis.com/billing/bytes_ingested\\" AND resource.type=\\"global\\"' in ceiling
    assert 'alignment_period     = "86400s"' in ceiling
    assert 'per_series_aligner   = "ALIGN_SUM"' in ceiling
    assert 'cross_series_reducer = "REDUCE_SUM"' in ceiling
    assert 'comparison      = "COMPARISON_GT"' in ceiling


def test_step_change_compares_the_same_window_a_day_apart_with_a_floor() -> None:
    query = _step_query()
    metric = 'logging_googleapis_com:billing_bytes_ingested{monitored_resource="global"}'
    now = f"sum(increase({metric}[6h]))"
    day_before = f"sum(increase({metric}[6h] offset 1d))"
    ratio = re.search(re.escape(now) + r" > ([0-9.]+) \* " + re.escape(day_before), query)
    assert ratio, query
    floor = re.search(r" and " + re.escape(now) + r" > (\d+)$", query)
    assert floor, query
    factor, floor_bytes = float(ratio.group(1)), int(floor.group(1))
    # Steady growth must not trip it; a jump that several times the project's
    # logs (DATA_READ alone was) must.
    assert 2.0 <= factor <= 3.0
    assert floor_bytes >= GIB
    # The condition's name states both numbers.
    name = re.search(r'display_name = "(Last 6 h[^"]*)"', _resource("log_ingestion_step_change"))
    assert name, "condition name missing"
    assert f"{factor:g}x" in name.group(1) and f"{floor_bytes // GIB} GiB" in name.group(1)


def test_step_change_holds_before_paging_and_closes_itself() -> None:
    step = _resource("log_ingestion_step_change")
    assert 'duration            = "1800s"' in step
    assert 'evaluation_interval = "300s"' in step
    assert 'auto_close = "86400s"' in step


def test_both_guards_page_the_security_channel_and_say_where_they_live() -> None:
    for name in ("log_ingestion_ceiling", "log_ingestion_step_change"):
        policy = _resource(name)
        assert "notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]" in policy
        assert "infra/log_ingestion_cost_guard.tf" in policy


def test_baseline_check_requires_the_step_change_policy() -> None:
    step_name = re.search(r'display_name = "(TR Logging: ingestion step change[^"]*)"', GUARD)
    assert step_name
    assert f'"{step_name.group(1)}"; do' in BASELINE
