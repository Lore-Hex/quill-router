# Streams on the fast-path lease (docs/design/fast-admission-streams.md,
# sections 4.4, 6.4 and 8.3): two lines the control plane writes, each
# counted by a log-based metric and paged on by a policy, as
# infra/fastpath_alerts.tf pages on the nodes' journal.
#
# The control plane's application logger writes to stderr as
# "LEVEL logger message" (trusted_router.main, _ApplicationConsoleFormatter),
# which Cloud Run keeps as the entry's textPayload. Each filter matches the
# level, the logger and the line's event name with the space after it, so
# no other line, and no longer event name, matches.
#
#   - heartbeat_divergence: an owner refused a leased heartbeat the
#     control plane's transaction accepted, or the envelope's seal failed
#     (services/fastpath_heartbeat.py). Gate P needs none.
#   - settled_without_intent: the lease booked a settle for a leased stream
#     whose row the reaper found with no settle intent to write its records
#     (services/fastpath_settle.py, reconcile_reaped).
#
# What each means and what to do: docs/runbooks/fastpath-alerts.md.

locals {
  fastpath_control_plane_lines = {
    heartbeat_divergence   = "fastpath.heartbeat_divergence"
    settled_without_intent = "fastpath.settled_without_intent"
  }
  fastpath_control_plane_filters = {
    for kind, event in local.fastpath_control_plane_lines : kind => join(" AND ", [
      "resource.type=\"cloud_run_revision\"",
      "textPayload:\"ERROR trusted_router.fastpath ${event} \"",
    ])
  }
}

resource "google_logging_metric" "fastpath_control_plane" {
  for_each = local.fastpath_control_plane_lines

  project     = local.gcp_project_id
  name        = "fastpath_control_plane_${each.key}"
  description = "The control plane wrote a ${each.value} line (docs/design/fast-admission-streams.md)."
  filter      = local.fastpath_control_plane_filters[each.key]

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "fastpath_control_plane" {
  for_each = local.fastpath_control_plane_lines

  project      = local.gcp_project_id
  display_name = "TR Fast path control plane: ${each.key}"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "The control plane wrote a ${each.value} line"
    condition_matched_log {
      filter = local.fastpath_control_plane_filters[each.key]
    }
  }

  # As the nodes' policies: one notification per five minutes, the incident
  # closing on its own half an hour after the last line. A log-matched
  # policy must carry the rate limit.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    content   = "The control plane wrote a `${each.value}` line; the line names the authorization. What it means and what to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
