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


def _policy(name: str) -> str:
    """One policy's resource block, through the next top-level block."""
    tf = POLICIES.read_text(encoding="utf-8")
    start = tf.index(f'resource "google_monitoring_alert_policy" "{name}" {{')
    end = tf.find("\n}\n", start)
    assert end > start
    return tf[start:end]


def test_the_alert_policy_matches_the_lines_the_nodes_write() -> None:
    import yaml

    tf = _policy("fastpath_alert")
    startup = _read("infra", "fastpath_node_startup.sh.tftpl")
    service = _read("fastpath", "internal", "service", "service.go")
    # The agent's active pipeline reads the journal through the receiver
    # that names the log, and parses each line's MESSAGE as JSON, which
    # makes the line's labels the entry's.
    match = re.search(r"<<AGENT\n(.*?)\nAGENT\n", startup, re.S)
    assert match
    logging = yaml.safe_load(match.group(1))["logging"]
    assert logging["receivers"] == {"fastpath_journal": {"type": "systemd_journald"}}
    pipeline = logging["service"]["pipelines"]["fastpath"]
    assert pipeline["receivers"] == ["fastpath_journal"] and "fastpath_json" in pipeline["processors"]
    assert logging["processors"]["fastpath_json"] == {"type": "parse_json", "field": "MESSAGE"}
    assert '"logName=\\"projects/${local.gcp_project_id}/logs/fastpath_journal\\""' in tf
    # The service labels each alert with its kind and writes it at the
    # journal's err priority, which the agent maps to ERROR.
    # The auditor's alerts by their message's kind; the edge's decide shadow
    # names its own kind (edge.AlertWouldAdmit) through the same line.
    assert "writeAlertOf(w, subject, what, auditor.AlertKind(what))" in service
    assert '"logging.googleapis.com/labels": map[string]string{"fastpath_alert": kind}' in service
    assert 'fmt.Fprintln(w, "<3>"+string(line))' in service
    # A policy per kind, each matching all three parts at once: the log, its
    # kind's label and the severity, so an ordinary entry of the same log, or
    # another kind's, does not match.
    assert "for_each = local.fastpath_alert_kinds" in tf
    assert tf.count("filter = join(") == 1 and 'filter = join(" AND ", [' in tf
    assert " OR " not in tf
    assert '"labels.fastpath_alert=\\"${each.key}\\""' in tf
    assert '"severity>=ERROR"' in tf
    # A log-matched policy must carry a notification rate limit, which
    # terraform validate does not check and the API does.
    assert re.search(r'notification_rate_limit \{\n\s+period = "300s"\n\s+\}\n\s+auto_close = "1800s"', tf)


def test_every_alert_kind_has_a_policy_and_a_runbook_entry() -> None:
    alerts = _read("fastpath", "internal", "auditor", "alerts.go")
    kinds = re.findall(r'^\tAlert\w+:\s+"(\w+)",$', alerts, re.M)
    assert len(kinds) == len(re.findall(r"^\tAlert\w+\s+= ", alerts, re.M)) == 13
    assert 'return "other"' in alerts
    # Each kind its own policy, so a kind that keeps firing cannot use up
    # another's daily notifications.
    listed = re.search(r"fastpath_alert_kinds = toset\(\[(.*?)\]\)", POLICIES.read_text(encoding="utf-8"), re.S)
    assert listed
    assert sorted(re.findall(r'"(\w+)"', listed.group(1))) == sorted([*kinds, "other"])
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
    # The metric's subscription_id label is the short name ("auditor"), not
    # the resource's full ID (projects/P/subscriptions/auditor).
    assert "google_pubsub_subscription.fastpath_auditor.name," in tf
    assert "google_pubsub_subscription.fastpath_stager.name," in tf
    assert "[for subscription in google_pubsub_subscription.fastpath_archive : subscription.name]" in tf
    assert not re.search(r"(fastpath_auditor|fastpath_stager|subscription)\.id\b", tf)
    # The condition itself: the fast path's subscriptions only, their oldest
    # message's age past the threshold, held for the duration.
    lag = _policy("fastpath_subscription_lag")
    assert lag.count("condition_threshold {") == 1
    assert '"resource.type = \\"pubsub_subscription\\""' in lag
    assert '"metric.type = \\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\\""' in lag
    assert (
        '"resource.labels.subscription_id = one_of(${join(", ", '
        '[for name in local.fastpath_lag_subscriptions : "\\"${name}\\""])})"'
    ) in lag
    assert re.search(r'^\s+comparison\s+= "COMPARISON_GT"$', lag, re.M)
    assert re.search(r'^\s+duration\s+= "300s"$', lag, re.M)
    # An archive's oldest message waits up to its max_duration in the normal
    # course: the threshold sits past it.
    (max_duration,) = re.findall(r'max_duration\s+=\s+"(\d+)s"', fastpath)
    (threshold,) = re.findall(r"^\s+threshold_value\s+=\s+(\d+)$", lag, re.M)
    assert int(threshold) == 600 > int(max_duration)
    # One series past the threshold is enough: the auditor's alone.
    assert re.search(r"trigger \{\n\s+count = 1\n\s+\}", lag)


def test_the_channels_are_read_not_managed() -> None:
    """The channels are the reliability scripts'; a second manager would
    mint duplicates (gcp_security_baseline_check.sh flags them)."""
    tf = POLICIES.read_text(encoding="utf-8")
    assert 'resource "google_monitoring_notification_channel"' not in tf
    assert tf.count('data "google_monitoring_notification_channel"') == 2
    for script in ("gateway_reliability.sh", "clickhouse_reliability.sh"):
        assert 'ALERT_CHANNEL_DISPLAY_NAME:-TrustedRouter Spanner on-call}' in _read("scripts", "deploy", script)
    assert 'display_name = "TrustedRouter Spanner on-call"' in tf
    assert 'display_name = "TrustedRouter ops chat gcp"' in tf
    # Both channels in the list every policy notifies.
    assert (
        "fastpath_alert_channels = [\n"
        "    data.google_monitoring_notification_channel.fastpath_oncall.name,\n"
        "    data.google_monitoring_notification_channel.fastpath_ops_chat.name,\n"
        "  ]"
    ) in tf
    assert tf.count("notification_channels = local.fastpath_alert_channels") == 2
