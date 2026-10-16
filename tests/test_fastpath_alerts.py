"""W7's alerting, held to what the nodes write (infra/fastpath_alerts.tf,
the service's alert line, the node's log agent, the alert kinds and their
runbook)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "infra" / "fastpath_alerts.tf"


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def test_the_alert_policy_matches_the_lines_the_nodes_write() -> None:
    tf = POLICIES.read_text(encoding="utf-8")
    startup = _read("infra", "fastpath_node_startup.sh.tftpl")
    service = _read("fastpath", "internal", "service", "service.go")
    # The agent's journald receiver names the log the entries land in.
    assert re.search(r"^ {4}fastpath_journal:\n {6}type: systemd_journald$", startup, re.M)
    assert '"logName=\\"projects/${local.gcp_project_id}/logs/fastpath_journal\\""' in tf
    # The service labels each alert with its kind and writes it at the
    # journal's err priority, which the agent maps to ERROR.
    assert '"logging.googleapis.com/labels": map[string]string{"fastpath_alert": auditor.AlertKind(what)}' in service
    assert 'fmt.Fprintln(w, "<3>"+string(line))' in service
    assert '"labels.fastpath_alert:*"' in tf
    assert '"severity>=ERROR"' in tf
    assert 'kind = "EXTRACT(labels.fastpath_alert)"' in tf


def test_every_alert_kind_has_a_runbook_entry() -> None:
    alerts = _read("fastpath", "internal", "auditor", "alerts.go")
    kinds = re.findall(r'^\tAlert\w+:\s+"(\w+)",$', alerts, re.M)
    assert len(kinds) == len(re.findall(r"^\tAlert\w+\s+= ", alerts, re.M)) == 13
    assert 'return "other"' in alerts
    runbook = _read("docs", "runbooks", "fastpath-alerts.md")
    for kind in [*kinds, "other"]:
        assert re.search(rf"^\| `{kind}` \| ", runbook, re.M), kind
    # The policies point at the runbook, and the service's comment names it.
    assert POLICIES.read_text(encoding="utf-8").count("docs/runbooks/fastpath-alerts.md") == 3
    assert "docs/runbooks/fastpath-alerts.md" in _read("fastpath", "internal", "service", "service.go")


def test_the_lag_policy_watches_every_subscription_of_the_fast_path() -> None:
    fastpath = _read("infra", "fastpath.tf")
    subscriptions = re.findall(r'^resource "google_pubsub_subscription" "(\w+)"', fastpath, re.M)
    assert sorted(subscriptions) == ["fastpath_archive", "fastpath_auditor", "fastpath_stager"]
    tf = POLICIES.read_text(encoding="utf-8")
    for subscription in subscriptions:
        assert f"google_pubsub_subscription.{subscription}" in tf, subscription
    # An archive's oldest message waits up to its max_duration in the normal
    # course: the threshold sits past it.
    (max_duration,) = re.findall(r'max_duration\s+=\s+"(\d+)s"', fastpath)
    (threshold,) = re.findall(r"threshold_value\s+=\s+(\d+)", tf)
    assert int(threshold) > int(max_duration)
    assert "pubsub.googleapis.com/subscription/oldest_unacked_message_age" in tf


def test_the_channels_are_read_not_managed() -> None:
    """The channels are the reliability scripts'; a second manager would
    mint duplicates (gcp_security_baseline_check.sh flags them)."""
    tf = POLICIES.read_text(encoding="utf-8")
    assert 'resource "google_monitoring_notification_channel"' not in tf
    assert tf.count('data "google_monitoring_notification_channel"') == 2
    for script in ("gateway_reliability.sh", "clickhouse_reliability.sh"):
        assert 'ALERT_CHANNEL_DISPLAY_NAME:-TrustedRouter Spanner on-call}' in _read("scripts", "deploy", script)
    assert 'display_name = "TrustedRouter Spanner on-call"' in tf
    assert tf.count("notification_channels = local.fastpath_alert_channels") == 2
