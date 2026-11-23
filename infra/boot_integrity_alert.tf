# A VM boot that did not pass its Shielded VM integrity policy.
#
# Every instance in the project has integrity monitoring on (the enclave
# groups, the fast path and ClickHouse nodes). Compute Engine measures each
# boot against the policy learned at the instance's first boot and writes an
# earlyBootReportEvent and a lateBootReportEvent to the
# compute.googleapis.com/shielded_vm_integrity log, each with
# policyEvaluationPassed. Nothing read them before this policy: no alert or
# log metric named the log. Over the month before it was added every report
# passed, and the filter below matched none of them while matching every
# passing report once inverted. infra/logging_sampling.tf samples the log's
# per-quote TDX events and keeps every boot report, so this policy sees all
# of them.
#
# A report that failed, or carries no result, pages. A known change to an
# instance's boot (a new kernel or image on a fast path or ClickHouse node)
# fails the late boot report until the instance relearns its policy; the
# command is in the documentation below.
#
# An enclave instance is replaced, never changed in place, so its new
# instance learns its own policy at first boot and a failure there is never
# expected. Anything unexplained is a changed boot chain on a production
# host.
resource "google_monitoring_alert_policy" "boot_integrity_failed" {
  project      = local.gcp_project_id
  display_name = "TR Security: a VM boot did not pass its Shielded VM integrity policy"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "An early or late boot report failed or carries no result"
    condition_matched_log {
      filter = <<-EOT
        logName="projects/quill-cloud-proxy/logs/compute.googleapis.com%2Fshielded_vm_integrity"
        AND (
          (jsonPayload.earlyBootReportEvent:* AND NOT jsonPayload.earlyBootReportEvent.policyEvaluationPassed=true)
          OR (jsonPayload.lateBootReportEvent:* AND NOT jsonPayload.lateBootReportEvent.policyEvaluationPassed=true)
        )
      EOT
      label_extractors = {
        instance_id = "EXTRACT(resource.labels.instance_id)"
        zone        = "EXTRACT(resource.labels.zone)"
      }
    }
  }

  # One notification an hour while it continues; the incident closes a day
  # after the last matching report. A log-matched policy must carry the rate
  # limit.
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "A VM's early or late boot report in the shielded_vm_integrity log did not pass its integrity policy (policyEvaluationPassed false or absent). Read the entry (labels instance_id and zone; its actualMeasurements against policyMeasurements show which PCR changed) and find the instance with `gcloud compute instances list --filter=id=INSTANCE_ID`. If the change is a known kernel or image update on a fast path or ClickHouse node, learn the policy again with `gcloud compute instances update INSTANCE --zone=ZONE --shielded-learn-integrity-policy`. An enclave instance learns its own policy at first boot, so a failure there is never expected. Otherwise treat the host as compromised: take it out of service and keep its disk for forensics. infra/boot_integrity_alert.tf in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]
}
