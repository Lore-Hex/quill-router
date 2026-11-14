# Data-read audit records for the serving instance's API.
#
# On November 11 whole-table operator searches opened three alerts and the
# caller could not be named: the project recorded ADMIN_READ only
# (docs/incidents/2026-11-11-spanner-operator-table-scans.md). The September 6
# note recorded the same gap.
#
# The serving fleet is exempt. Its reads are request-tagged and already
# attributable in SPANNER_SYS, and at thousands of reads a second its records
# would cost more than everything else in Logging. What remains is operator,
# CI and vendor traffic, plus any principal added later: the default for a new
# reader is "recorded".
#
# Ad hoc SQL text appears in these records. SPANNER_SYS already keeps the same
# text for 30 days.
#
# infra-apply runs as tr-deploy, which cannot write the project IAM policy.
# An owner writes this audit config once (the command is in the PR that added
# this file); it is imported here after that.
import {
  to = google_project_iam_audit_config.spanner_data_read
  id = "quill-cloud-proxy spanner.googleapis.com"
}

locals {
  spanner_serving_readers = [
    "serviceAccount:${local.gcp_project_number}-compute@developer.gserviceaccount.com",
    "serviceAccount:tr-aws-cross-cloud@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-clickhouse@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-fastpath-node@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-google-data-manager@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-internal@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-multicloud@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:tr-public@${local.gcp_project_id}.iam.gserviceaccount.com",
    "serviceAccount:trusted-router-control-run@${local.gcp_project_id}.iam.gserviceaccount.com",
  ]
}

resource "google_project_iam_audit_config" "spanner_data_read" {
  project = local.gcp_project_id
  service = "spanner.googleapis.com"

  audit_log_config {
    log_type         = "DATA_READ"
    exempted_members = local.spanner_serving_readers
  }
}
