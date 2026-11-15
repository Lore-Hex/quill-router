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
# Deliberately NOT exempt (INC-2026-003 corrective action 6): tr-clickhouse,
# whose metadata token the intruder stole on 2026-11-09 and used against this
# database, and the Compute Engine default account, which any VM without its
# own account carries. A stolen workload token is exactly the caller these
# records exist to name, so those two accounts are recorded even though the
# ClickHouse drains read steadily.
#
# Ad hoc SQL text appears in these records. SPANNER_SYS already keeps the same
# text for 30 days.
#
# infra-apply runs as tr-deploy, which cannot write the project IAM policy, so
# an operator creates this resource with a targeted apply
# (terraform apply -target=google_project_iam_audit_config.spanner_data_read)
# before the change merges; infra-apply then plans no change.

locals {
  spanner_serving_readers = [
    "serviceAccount:tr-aws-cross-cloud@${local.gcp_project_id}.iam.gserviceaccount.com",
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
