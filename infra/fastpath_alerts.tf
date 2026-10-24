# W7 (docs/design/fast-admission-production-rollout.md): the fast path's
# alerting. A node's service writes each alert as an ERROR entry of the log
# fastpath_journal, labelled fastpath_alert with the alert's kind
# (writeAlert in fastpath/internal/service; the node's Ops Agent carries the
# journal there, infra/fastpath_node_startup.sh.tftpl). A policy for each
# kind notifies on its entries; one more on the lag of the fast path's
# subscriptions: the auditor's (an auditor making no progress), the stager's
# and each topic's archive. What each means and what to do:
# docs/runbooks/fastpath-alerts.md.
#
# The channels exist already (the reliability scripts made them) and are
# read here, not managed. The applying identity's role adds and changes
# policies but cannot delete one (trustedRouterAlertReconciler,
# scripts/deploy/gateway_reliability_iam.sh): a change that replaces a
# policy, or a kind taken off the list, needs an operator.

data "google_monitoring_notification_channel" "fastpath_oncall" {
  project      = local.gcp_project_id
  display_name = "TrustedRouter Spanner on-call"
}

data "google_monitoring_notification_channel" "fastpath_ops_chat" {
  project      = local.gcp_project_id
  display_name = "TrustedRouter ops chat gcp"
}

locals {
  fastpath_alert_channels = [
    data.google_monitoring_notification_channel.fastpath_oncall.name,
    data.google_monitoring_notification_channel.fastpath_ops_chat.name,
  ]
  fastpath_lag_subscriptions = concat(
    [
      google_pubsub_subscription.fastpath_auditor.name,
      google_pubsub_subscription.fastpath_stager.name,
    ],
    [for subscription in google_pubsub_subscription.fastpath_archive : subscription.name],
  )
  # Every kind of fastpath/internal/auditor/alerts.go, the edge's (the Alert
  # constants of fastpath/internal/edge: shadow_full.go's decide shadow and
  # decide_path.go's decide path), and "other", which the service gives an
  # alert of no kind it knows. A policy each: Cloud
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
    "edge_would_admit_python_refused",
    "edge_decide_unavailable",
    "edge_decide_unavailable_sustained",
    "edge_decide_pass_slow",
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
    content   = "A fast path node wrote a `${each.key}` alert; the entry's `subject` names the lease or authorization. What it means and what to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
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
    content   = "A fast path subscription is behind: the incident's resource names it (auditor, stager, settle-log-archive or records-archive). What to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
