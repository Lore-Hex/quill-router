# The project's Cloud Logging ingestion guards.
#
# Two policies, both to the security channel:
#
# - "ingestion volume spike (audit-log cost guard)" is the absolute ceiling on
#   billed ingestion summed over one-day alignment windows. It was made by hand
#   in August 2026 when Data Access audit logs were turned on, and it is
#   adopted here as it stands (the import block below) with more headroom: a
#   fixed daily line is a poor detector of sudden jumps while traffic grows, so
#   its job is now only the cost ceiling.
#
# - "ingestion step change" catches sudden jumps early and at any traffic
#   level: Data Access DATA_READ coming back on, a sampling exclusion going
#   missing, or a service starting to log every request. It compares the last
#   six hours with the same six hours a day earlier, so steady day-to-day
#   growth does not trip it, and it needs a floor of real volume so a quiet
#   night cannot.
#
# scripts/deploy/gcp_security_baseline_check.sh requires the step-change
# policy to be on with a channel, and reads the ceiling's threshold against its
# own expected value (INGEST_THRESHOLD_BYTES); tests/test_log_ingestion_cost_guard.py
# keeps the two in step.

import {
  to = google_monitoring_alert_policy.log_ingestion_ceiling
  id = "projects/quill-cloud-proxy/alertPolicies/3305751919896009722"
}

locals {
  # 16 GiB a day of billed ingestion.
  log_ingestion_ceiling_bytes = 17179869184
}

resource "google_monitoring_alert_policy" "log_ingestion_ceiling" {
  project      = local.gcp_project_id
  display_name = "TR Logging: ingestion volume spike (audit-log cost guard)"
  combiner     = "OR"

  conditions {
    display_name = "log ingestion > 16 GiB/day"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/billing/bytes_ingested\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = local.log_ingestion_ceiling_bytes
      duration        = "0s"
      aggregations {
        alignment_period     = "86400s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "604800s"
  }

  documentation {
    content   = <<-EOT
      Billed Cloud Logging ingestion for the project passed 16 GiB in a day.

      First check whether Data Access audit logging changed: compare the project's auditConfigs with infra/ and scripts/deploy/gcp_security_baseline_check.sh (DATA_READ is meant to be off). If it did not, find the growing log: Logs Explorer, Log fields panel or `gcloud logging read` grouped by logName and resource type over the last day, and compare with the sampling exclusions in infra/logging_sampling.tf. Organic traffic growth reaching this ceiling means the noisiest routine success lines need sampling; failure lines are always kept.

      Managed in infra/log_ingestion_cost_guard.tf in Lore-Hex/quill-router. The sudden-jump guard is "TR Logging: ingestion step change".
    EOT
    mime_type = "text/markdown"
  }

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]
}

# A query condition is kept in a policy of its own, apart from threshold
# conditions.
resource "google_monitoring_alert_policy" "log_ingestion_step_change" {
  project      = local.gcp_project_id
  display_name = "TR Logging: ingestion step change (day over day)"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Last 6 h of ingestion is over 2.5x the same 6 h a day earlier, and over 2 GiB"
    condition_prometheus_query_language {
      query               = <<-EOT
        sum(increase(logging_googleapis_com:billing_bytes_ingested{monitored_resource="global"}[6h]))
          > 2.5 * sum(increase(logging_googleapis_com:billing_bytes_ingested{monitored_resource="global"}[6h] offset 1d))
        and
        sum(increase(logging_googleapis_com:billing_bytes_ingested{monitored_resource="global"}[6h])) > 2147483648
      EOT
      duration            = "1800s"
      evaluation_interval = "300s"
    }
  }

  alert_strategy {
    auto_close = "86400s"
  }

  documentation {
    content   = <<-EOT
      Cloud Logging ingestion over the last six hours is more than 2.5 times the same six hours a day earlier, and more than 2 GiB.

      Most likely causes, in order: Data Access audit logging changed (DATA_READ is meant to be off; compare the project's auditConfigs with scripts/deploy/gcp_security_baseline_check.sh), a sampling exclusion in infra/logging_sampling.tf was removed or stopped matching, or a service started logging every request or a stack trace per request. Find the log that grew with Logs Explorer's Log fields panel, or `gcloud logging read` grouped by logName and resource type, for the last six hours against the day before. A planned large traffic step can also trip it; if so, note it and let it close.

      Managed in infra/log_ingestion_cost_guard.tf in Lore-Hex/quill-router. The daily ceiling is "TR Logging: ingestion volume spike (audit-log cost guard)".
    EOT
    mime_type = "text/markdown"
  }

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]
}
