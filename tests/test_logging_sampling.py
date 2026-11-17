"""Log sampling exclusions (infra/logging_sampling.tf).

The launcher's journald export carries enclave lines in jsonPayload.message
(lowercase). Field names are case-sensitive in Cloud Logging filters; a
filter that named only jsonPayload.MESSAGE matched nothing for weeks and kept
every success line. These tests pin that each enclave clause names both
spellings, that failures are never sampled, and the heartbeat sample rate.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra/logging_sampling.tf").read_text()


def _filter(name: str) -> str:
    start = TF.index(f'resource "google_logging_project_exclusion" "{name}"')
    body_start = TF.index("<<-EOT", start) + len("<<-EOT")
    return TF[body_start: TF.index("EOT", body_start)]


def _clauses(filter_text: str, field: str) -> list[str]:
    return re.findall(rf'jsonPayload\.{field}:"(enclave\.[a-z_]+) "', filter_text)


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
    names = re.findall(r'resource "google_logging_project_exclusion" "([a-z_]+)"', TF)
    assert names, "no exclusions found"
    for name in names:
        assert re.search(r"sample\(insertId, 0\.9{1,2}\)", _filter(name)), name
