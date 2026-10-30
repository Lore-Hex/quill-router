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
    # and decide path name their own kinds through the same line.
    assert "writeAlertOf(w, subject, what, auditor.AlertKind(what))" in service
    assert "edgeAlert := func(kind, subject, what string) { writeAlertOf(alertOutput, subject, what, kind) }" in service
    assert "decideShadow = &edge.DecideShadowConfig{Cache: cache, Decides: s, Rows: s, Windows: s, Alert: edgeAlert}" in service
    assert "decide = &edge.DecideConfig{Cache: cache, Decides: s, Rows: s, Windows: s, Alert: edgeAlert}" in service
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
    # The edge's decide shadow and decide path raise their own kinds through
    # the service's alert line (writeAlertOf): a const line each, or a line
    # of a const block, in any of the edge's files.
    edge_kinds = [
        kind
        for path in sorted((ROOT / "fastpath" / "internal" / "edge").glob("*.go"))
        if not path.name.endswith("_test.go")
        for kind in re.findall(r'^(?:const |\t)Alert\w+\s+= "(\w+)"$', path.read_text(encoding="utf-8"), re.M)
    ]
    assert sorted(edge_kinds) == [
        "edge_decide_pass_slow",
        "edge_decide_unavailable",
        "edge_decide_unavailable_sustained",
        "edge_decide_window_unread",
        "edge_would_admit_python_refused",
    ]
    kinds += edge_kinds
    # Each kind its own policy, so a kind that keeps firing cannot use up
    # another's daily notifications.
    listed = re.search(r"fastpath_alert_kinds = toset\(\[(.*?)\]\)", POLICIES.read_text(encoding="utf-8"), re.S)
    assert listed
    assert sorted(re.findall(r'"(\w+)"', listed.group(1))) == sorted([*kinds, "other"])
    runbook = _read("docs", "runbooks", "fastpath-alerts.md")
    for kind in [*kinds, "other"]:
        assert re.search(rf"^\| `{kind}` \| ", runbook, re.M), kind
    # The policies point at the runbook, and the service's comment names it.
    assert POLICIES.read_text(encoding="utf-8").count("docs/runbooks/fastpath-alerts.md") == 5
    assert "docs/runbooks/fastpath-alerts.md" in _read("fastpath", "internal", "service", "service.go")


def test_the_lag_policy_watches_every_subscription_of_the_fast_path() -> None:
    fastpath = _read("infra", "fastpath.tf")
    subscriptions = re.findall(r'^resource "google_pubsub_subscription" "(\w+)"', fastpath, re.M)
    assert sorted(subscriptions) == ["fastpath_archive", "fastpath_auditor", "fastpath_stager"]
    tf = POLICIES.read_text(encoding="utf-8")
    for subscription in subscriptions:
        assert f"google_pubsub_subscription.{subscription}" in tf, subscription
    # The metric's subscription_id label is the short name ("auditor"), not
    # the resource's full ID (projects/P/subscriptions/auditor). The auditor's
    # has its own policies; the ten-minute one watches the rest.
    assert "google_pubsub_subscription.fastpath_stager.name," in tf
    assert "google_pubsub_subscription.fastpath_auditor.name," not in tf
    assert tf.count("google_pubsub_subscription.fastpath_auditor.name") == 3
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
    assert tf.count("notification_channels = local.fastpath_alert_channels") == 4


def test_the_auditor_has_its_own_tighter_policies() -> None:
    """A member whose commits are wedged holds its leases' keys shut
    (2026-10-26): the auditor's subscription is watched at two minutes, held
    for one, and for messages waiting while none is acked."""
    fastpath = _read("infra", "fastpath.tf")
    lag = _policy("fastpath_auditor_lag")
    assert lag.count("condition_threshold {") == 1
    assert '"metric.type = \\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\\""' in lag
    assert '"resource.labels.subscription_id = \\"${google_pubsub_subscription.fastpath_auditor.name}\\""' in lag
    assert re.search(r'^\s+comparison\s+= "COMPARISON_GT"$', lag, re.M)
    (threshold,) = re.findall(r"^\s+threshold_value\s+=\s+(\d+)$", lag, re.M)
    assert re.search(r'^\s+duration\s+= "60s"$', lag, re.M)
    # Past the commit watchdog's minute (auditor.CommitStall), after which
    # a wedged member's records go to the others, and well under the
    # ten minutes the archives need.
    stall = re.search(r"^const CommitStall = (\d+) \* time.Second$", _read("fastpath", "internal", "auditor", "runtime.go"), re.M)
    assert stall and int(stall.group(1)) < int(threshold) == 120 < 600
    assert re.search(r"trigger \{\n\s+count = 1\n\s+\}", lag)

    no_acks = _policy("fastpath_auditor_no_acks")
    assert no_acks.count("conditions {") == 1 and "condition_prometheus_query_language {" in no_acks
    query = re.search(r"query\s+= <<-EOT\n(.*?)\n\s+EOT\n", no_acks, re.S)
    assert query
    lines = [line.strip() for line in query.group(1).splitlines()]
    selector = (
        '{monitored_resource="pubsub_subscription",'
        'subscription_id="${google_pubsub_subscription.fastpath_auditor.name}"}'
    )
    # Messages waiting the whole window, kept unless a nonzero ack rate in
    # the same window matches them: an absent ack series fires as a zero one.
    assert lines == [
        f"min_over_time(pubsub_googleapis_com:subscription_num_undelivered_messages{selector}[5m]) > 0",
        "unless on (subscription_id)",
        f"sum by (subscription_id) (rate(pubsub_googleapis_com:subscription_ack_message_count{selector}[5m])) > 0",
    ]
    assert 'evaluation_interval = "60s"' in no_acks
    for policy in (lag, no_acks):
        assert 'severity     = "ERROR"' in policy
        assert "notification_channels = local.fastpath_alert_channels" in policy
        assert "docs/runbooks/fastpath-alerts.md" in policy
    # The ten-minute policy no longer watches the auditor's subscription.
    assert 'name                       = "auditor"' in fastpath
    assert "fastpath_auditor" not in _policy("fastpath_subscription_lag")
    runbook = _read("docs", "runbooks", "fastpath-alerts.md")
    assert "TR Fast path: auditor lag" in runbook and "TR Fast path: auditor acknowledges nothing" in runbook
