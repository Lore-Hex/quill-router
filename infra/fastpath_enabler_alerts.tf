# The fast path's enabler (docs/design/fast-admission-widening.md sections
# 4(a), 5.3 and 8.1 item 7): two lines its Cloud Run job writes, each counted
# by a log-based metric and paged on by a policy, as
# infra/fastpath_control_plane_alerts.tf pages on the control plane's.
#
# The job logs to stderr as "LEVEL logger message"
# (trusted_router.fastpath_enabler, main), which Cloud Run keeps as the
# entry's textPayload. Each filter matches the job, the level, the logger and
# the line's event name with the space after it, so no other line, and no
# longer event name, matches.
#
#   - fastpath_enabler_mass_disable: a run would have turned off more
#     enabled workspaces for a change of facts (tier, balance, a federated
#     home, a deletion, a probe, a bypass) than its cap, the larger of 20 and
#     a tenth of the enabled set, so it turned none of them off.
#   - fastpath_enabler_trust_stale: more than half of a run's eligible
#     workspaces had a trust watermark older than the reconciler's maximum
#     age, which points at the reconciler; their new enables are frozen.
#
# With the job's mode off it writes neither line, so these are dormant until
# an activation step moves the mode. What each means and what to do:
# docs/runbooks/fastpath-alerts.md.

locals {
  fastpath_enabler_job = "trusted-router-fastpath-enabler"
  fastpath_enabler_lines = {
    mass_disable = "fastpath_enabler_mass_disable"
    trust_stale  = "fastpath_enabler_trust_stale"
  }
  fastpath_enabler_filters = {
    for kind, event in local.fastpath_enabler_lines : kind => join(" AND ", [
      "resource.type=\"cloud_run_job\"",
      "resource.labels.job_name=\"${local.fastpath_enabler_job}\"",
      "textPayload:\"ERROR trusted_router.fastpath_enabler ${event} \"",
    ])
  }
}

resource "google_logging_metric" "fastpath_enabler" {
  for_each = local.fastpath_enabler_lines

  project     = local.gcp_project_id
  name        = "fastpath_enabler_${each.key}"
  description = "The fast path enabler wrote a ${each.value} line (docs/design/fast-admission-widening.md)."
  filter      = local.fastpath_enabler_filters[each.key]

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "fastpath_enabler" {
  for_each = local.fastpath_enabler_lines

  project      = local.gcp_project_id
  display_name = "TR Fast path enabler: ${each.key}"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "The fast path enabler wrote a ${each.value} line"
    condition_matched_log {
      filter = local.fastpath_enabler_filters[each.key]
    }
  }

  # As the nodes' and the control plane's: one notification per five
  # minutes, the incident closing on its own half an hour after the last
  # line. A log-matched policy must carry the rate limit.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    content   = "The fast path enabler wrote a `${each.value}` line; its run's report (`fastpath_enabler.report`) has the counts. What it means and what to do: docs/runbooks/fastpath-alerts.md in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
