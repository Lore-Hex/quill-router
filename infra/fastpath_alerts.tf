# W7 (docs/design/fast-admission-production-rollout.md): the fast path's
# alerting. A node's service writes each alert as an ERROR entry of the log
# fastpath_journal, labelled fastpath_alert with the alert's kind
# (writeAlert in fastpath/internal/service; the node's Ops Agent carries the
# journal there, infra/fastpath_node_startup.sh.tftpl). A policy for each
# kind notifies on its entries; one on the lag of the stager's subscription
# and each topic's archive; and two on the auditor's, which a member whose
# commits are wedged stops (2026-10-26): its oldest unacked message, at a
# tighter threshold than the others', and messages waiting while none is
# acked; and one on the fleet audit's summary line gone quiet. What each
# means and what to do: docs/runbooks/fastpath-alerts.md.
#
# The channel exists already (the reliability scripts made it) and is
# read here, not managed. The applying identity's role adds and changes
# policies but cannot delete one (trustedRouterAlertReconciler,
# scripts/deploy/gateway_reliability_iam.sh): a change that replaces a
# policy, or a kind taken off the list, needs an operator.

data "google_monitoring_notification_channel" "fastpath_oncall" {
  project      = local.gcp_project_id
  display_name = "TrustedRouter Spanner on-call"
}

locals {
  fastpath_alert_channels = [
    data.google_monitoring_notification_channel.fastpath_oncall.name,
  ]
  # The subscriptions held to the ten-minute lag. The auditor's has its own
  # policies below: a member wedged on it holds its leases' keys shut.
  fastpath_lag_subscriptions = concat(
    [
      google_pubsub_subscription.fastpath_stager.name,
    ],
    [for subscription in google_pubsub_subscription.fastpath_archive : subscription.name],
  )
  # Every kind of fastpath/internal/auditor/alerts.go, the fleet audit's
  # (the Kind constants of fastpath/internal/auditor/fleet.go), the edge's
  # (the Alert constants of fastpath/internal/edge: shadow_full.go's decide
  # shadow and decide_path.go's decide path), and "other", which the service
  # gives an alert of no kind it knows. A policy each: Cloud
  # Monitoring sends a policy at most 20 notifications a day, so a kind that
  # keeps firing cannot use up a money fault's.
  fastpath_alert_kinds = toset([
    "gap",
    "audit_fault",
    "fault_usage",
    "unreadable_record",
    "unappliable_record",
    "record_of_no_lease",
    "unknown_record_kind",
    "full_record_of_no_authorization",
    "full_record_of_no_lease",
    "unreadable_pending_work",
    "no_boot_binding",
    "drain_overdue",
    "pending_work_overdue",
    "fleet_audit_fault",
    "fleet_audit_stale",
    "edge_would_admit_python_refused",
    "edge_decide_unavailable",
    "edge_decide_unavailable_sustained",
    "edge_decide_pass_slow",
    "edge_decide_window_unread",
    "edge_window_race",
    "other",
  ])
}

resource "google_monitoring_alert_policy" "fastpath_alert" {
  for_each = local.fastpath_alert_kinds

  project      = local.gcp_project_id
  display_name = "TR Fast path: ${each.key}"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "A fast path node wrote a ${each.key} alert"
    condition_matched_log {
      filter = join(" AND ", [
        "logName=\"projects/${local.gcp_project_id}/logs/fastpath_journal\"",
        "labels.fastpath_alert=\"${each.key}\"",
        "severity>=ERROR",
      ])
    }
  }

  # A fault can write an alert per record it meets (2,222 lines in one P1
  # run): one notification per five minutes, the incident closing on its
  # own half an hour after the last. A log-matched policy must carry the
  # rate limit; the API refuses one without it.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    content   = "A fast path node wrote a `${each.key}` alert; the entry's `subject` names the lease, the authorization or, for the fleet audit's, the workspace. What it means and what to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}

resource "google_monitoring_alert_policy" "fastpath_subscription_lag" {
  project      = local.gcp_project_id
  display_name = "TR Fast path: subscription lag"
  combiner     = "OR"
  severity     = "ERROR"

  # An archive writes a file every max_duration (300 s), so its oldest
  # message is up to five minutes old in the normal course; ten minutes,
  # held for five, is past that for every subscription.
  conditions {
    display_name = "A fast path subscription's oldest unacked message is over ten minutes old"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\"",
        "resource.labels.subscription_id = one_of(${join(", ", [for name in local.fastpath_lag_subscriptions : "\"${name}\""])})",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 600
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "A fast path subscription is behind: the incident's resource names it (stager, settle-log-archive or records-archive). What to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}

resource "google_monitoring_alert_policy" "fastpath_auditor_lag" {
  project      = local.gcp_project_id
  display_name = "TR Fast path: auditor lag"
  combiner     = "OR"
  severity     = "ERROR"

  # A healthy auditor acknowledges a record within a second or two of its
  # delivery (a round of commits every second), and its oldest message is a
  # few seconds old. A member with a call stuck for a minute stops itself
  # (auditor.CommitStall), and its records come back to the
  # others within seconds; two minutes, held for one, is past both, and
  # fires on a wedged member the watchdog did not stop, on every member
  # stuck at once, or on a backlog of many batches worth a look.
  conditions {
    display_name = "The auditor subscription's oldest unacked message is over two minutes old"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_subscription\"",
        "metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\"",
        "resource.labels.subscription_id = \"${google_pubsub_subscription.fastpath_auditor.name}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 120
      duration        = "60s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The fast path auditor is behind: its subscription's oldest unacked message is over two minutes old. What to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}

resource "google_monitoring_alert_policy" "fastpath_auditor_no_acks" {
  project      = local.gcp_project_id
  display_name = "TR Fast path: auditor acknowledges nothing"
  combiner     = "OR"
  severity     = "ERROR"

  # Messages waiting on the auditor subscription the whole of five minutes
  # while not one was acknowledged: the subscription has stopped, not slowed.
  # The ack count can be absent rather than zero when nothing is acked, so
  # the query keeps the waiting series unless a nonzero ack rate matches
  # it, which a threshold condition cannot say. It is a policy of its own,
  # as a query condition is kept apart from threshold ones.
  conditions {
    display_name = "The auditor subscription has messages waiting and no acks for five minutes"
    condition_prometheus_query_language {
      query               = <<-EOT
        min_over_time(pubsub_googleapis_com:subscription_num_undelivered_messages{monitored_resource="pubsub_subscription",subscription_id="${google_pubsub_subscription.fastpath_auditor.name}"}[5m]) > 0
        unless on (subscription_id)
        sum by (subscription_id) (rate(pubsub_googleapis_com:subscription_ack_message_count{monitored_resource="pubsub_subscription",subscription_id="${google_pubsub_subscription.fastpath_auditor.name}"}[5m])) > 0
      EOT
      evaluation_interval = "60s"
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "The fast path auditor has acknowledged nothing for five minutes while messages wait on its subscription. What to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}

# The fleet audit's line (FleetSummaryLine in
# fastpath/internal/auditor/fleet.go), which whichever node owns the ring key
# fleet-audit writes as each pass begins, every 30 minutes as it goes and as
# it ends, a pass an hour, counted from the nodes' journal: the lines are
# never more than an hour and a minute apart, though a pass may run its
# whole hour. No line from any node for 90 minutes is fleet_audit_stale: no
# owner runs it, or its owner cannot read the workspaces due. A node cannot
# see its own silence, so this is Cloud Monitoring's to see, where a leased
# workspace left unaudited is the owner's own fleet_audit_stale alert above.
# The count is summed over the nodes, so the key moving from one node to
# another is not an absence. The policy is on with the nodes' attribute
# (local.fastpath_fleet_audit in infra/fastpath.tf), so it pages nobody
# while the audit is dark.
locals {
  fastpath_fleet_audit_summary_filter = join(" AND ", [
    "logName=\"projects/${local.gcp_project_id}/logs/fastpath_journal\"",
    "jsonPayload.MESSAGE:\"fastpath fleet audit: due=\"",
  ])
}

resource "google_logging_metric" "fastpath_fleet_audit_summary" {
  project     = local.gcp_project_id
  name        = "fastpath_fleet_audit_summary"
  description = "A fast path node wrote one of the fleet audit's lines, a pass's start, progress or summary (docs/design/fast-admission-widening.md section 7)."
  filter      = local.fastpath_fleet_audit_summary_filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "fastpath_fleet_audit_absent" {
  project      = local.gcp_project_id
  display_name = "TR Fast path: fleet_audit_stale, no summary"
  combiner     = "OR"
  severity     = "ERROR"
  enabled      = local.fastpath_fleet_audit

  conditions {
    display_name = "No fast path node wrote a fleet audit line for 90 minutes"
    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"gce_instance\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.fastpath_fleet_audit_summary.name}\"",
      ])
      duration = "5400s"

      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "No fast path node has written any of the fleet audit's lines, a pass's start, progress or summary (`fastpath fleet audit: due=...`), for 90 minutes: `fleet_audit_stale`. What to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
