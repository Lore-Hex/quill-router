# A settle that could not book a key's usage because the key has no counter
# row at any shard (docs/storage-portability/uncapped-skip-validation.md):
# the settle completes, the workspace is charged as usual, and the key's own
# usage count misses the amount. The storage layer writes one line for it
# after the commit, counted by a log-based metric and paged on by a policy,
# as infra/fastpath_control_plane_alerts.tf pages on the control plane's
# fast path lines.
#
# The application logger writes to stderr as "LEVEL logger message"
# (trusted_router.main, _ApplicationConsoleFormatter), which Cloud Run keeps
# as the entry's textPayload. The filter matches the level, the logger and
# the metric's name with the space after it, so no other line, and no longer
# metric name, matches. The API services and the settle drain worker are all
# Cloud Run revisions.
#
# The app writes a key's counter row in the same transaction as the key, so
# each line means a key record that reached storage some other way. What it
# means and what to do: docs/runbook.md, "Key usage unbooked".

locals {
  key_usage_unbooked_filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "textPayload:\"WARNING trusted_router.storage_gcp_authorize metric=key_usage_unbooked_total \"",
  ])
}

resource "google_logging_metric" "key_usage_unbooked" {
  project     = local.gcp_project_id
  name        = "key_usage_unbooked"
  description = "A settle completed without booking a key's usage: the key has no counter row at any shard (docs/runbook.md, Key usage unbooked)."
  filter      = local.key_usage_unbooked_filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "key_usage_unbooked" {
  project      = local.gcp_project_id
  display_name = "TR Settle: key usage unbooked"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "A settle wrote a key_usage_unbooked_total line"
    condition_matched_log {
      filter = local.key_usage_unbooked_filter
    }
  }

  # As the fast path's policies: one notification per five minutes, the
  # incident closing on its own half an hour after the last line. A
  # log-matched policy must carry the rate limit.
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    content   = "A settle completed without booking a key's usage: the key has no `tr_key_limit` row at any shard. The line names the key hash, the shard and the amount. The workspace was charged correctly; the key record did not come from the app. What it means and what to do: docs/runbook.md (Key usage unbooked) in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
