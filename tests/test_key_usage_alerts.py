"""The key-usage-unbooked line, held to the metric and policy that watch it
(infra/key_usage_alerts.tf): the filter's textPayload substring is read from
the Terraform and matched against the lines the storage layer writes, rendered
by the formatter Cloud Run's textPayload carries."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from trusted_router import storage_gcp_authorize
from trusted_router.main import _ApplicationConsoleFormatter

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "infra" / "key_usage_alerts.tf"
RUNBOOK = ROOT / "docs" / "runbook.md"
LOGGER = "trusted_router.storage_gcp_authorize"


def _payload_match() -> str:
    """The quoted textPayload substring the filter carries."""
    found = re.findall(r'"textPayload:\\"(.*?)\\""', TERRAFORM.read_text(encoding="utf-8"))
    assert len(found) == 1, found
    return found[0]


class _Lines(logging.Handler):
    """The storage logger's lines, as the console handler renders them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(_ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _rendered(warnings: list[dict[str, object]]) -> list[str]:
    assert storage_gcp_authorize.log.name == LOGGER
    handler = _Lines()
    storage_gcp_authorize.log.addHandler(handler)
    try:
        storage_gcp_authorize._log_missing_key_releases({"missing_key_releases": warnings})
    finally:
        storage_gcp_authorize.log.removeHandler(handler)
    return handler.lines


_UNBOOKED = {"key_hash": "kh-alert", "hold_micro": 0, "shard": 3, "actual_micro": 8,
             "book_to_byok": False, "usage_unbooked": True}
_RECOVERED = {"key_hash": "kh-recovered", "hold_micro": 0, "missing_shard": 3, "actual_micro": 8}
_SKIPPED = {"key_hash": "kh-skipped", "hold_micro": 5}


def test_the_filter_matches_the_unbooked_line_and_no_other() -> None:
    match = _payload_match()
    lines = _rendered([dict(_RECOVERED), dict(_UNBOOKED), dict(_SKIPPED)])
    assert len(lines) == 3, lines
    hits = [line for line in lines if match in line]
    assert len(hits) == 1, (match, lines)
    assert hits[0].startswith(match + "value=1 key_hash=kh-alert shard=3 actual_micro=8 "), hits
    # A longer metric name, another level or another logger is not it.
    assert match not in f"WARNING {LOGGER} metric=key_usage_unbooked_total_x value=1"
    assert match not in f"ERROR {LOGGER} metric=key_usage_unbooked_total value=1"
    assert match not in "WARNING trusted_router.other metric=key_usage_unbooked_total value=1"
    assert match not in f"WARNING {LOGGER} metric=key_usage_shard_fallback_total value=1"


def test_the_metric_and_the_policy_read_one_filter_and_page_as_the_fast_path_does() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    assert tf.count("filter      = local.key_usage_unbooked_filter") == 1
    assert tf.count("filter = local.key_usage_unbooked_filter") == 1
    assert '"resource.type=\\"cloud_run_revision\\""' in tf
    assert re.search(r'notification_rate_limit \{\n\s+period = "300s"\n\s+\}\n\s+auto_close = "1800s"', tf)
    assert "notification_channels = local.fastpath_alert_channels" in tf
    assert 'metric_kind = "DELTA"' in tf and 'value_type  = "INT64"' in tf


def test_the_runbook_names_the_policy_the_filter_and_the_scoped_rearm() -> None:
    tf = TERRAFORM.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    name = re.search(r'display_name = "(TR Settle: [^"]+)"', tf)
    assert name and f"**Alert `{name.group(1)}`**" in runbook
    assert "Key usage unbooked" in tf and '<a id="key-usage-unbooked"></a>Key usage unbooked' in runbook
    assert f'textPayload:"{_payload_match()}"' in runbook
    # The re-arm is scoped to one row and to the error the storage layer raises.
    raised = "key usage booking failed reservation_id="
    assert raised in Path(storage_gcp_authorize.__file__).read_text(encoding="utf-8")
    section = runbook.split('<a id="key-usage-unbooked"></a>', 1)[1].split("Monitoring signals:", 1)[0]
    assert section.count("UPDATE tr_settle_outbox") == 1
    assert "WHERE authorization_id='<authorization_id>' AND intent_kind='settle'" in section
    assert f"STARTS_WITH(last_error, 'RuntimeError: {raised}')" in section
