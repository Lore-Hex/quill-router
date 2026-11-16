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
# database. A stolen workload token is exactly the caller these records exist
# to name, so it is recorded even though the ClickHouse drains read steadily.
#
# The Compute Engine default account was recorded for about 15 minutes on
# 2026-11-15 and is exempt again: it carries serving traffic (~450 reads a
# second, ~90 GB/day of records, the same cost that ended the 2026-08-15
# allServices attempt). Moving that serving workload to its own account would
# let it be exempted by name instead.

# Ad hoc SQL text appears in these records. SPANNER_SYS already keeps the same
# text for 30 days.
#
# infra-apply runs as tr-deploy, which cannot write the project IAM policy, so
# an operator creates this resource with a targeted apply
# (terraform apply -target=google_project_iam_audit_config.spanner_data_read)
# before the change merges; infra-apply then plans no change.

locals {
  spanner_serving_readers = [
    # Serves the inference path: ~450 ExecuteStreamingSql a second, measured
    # 2026-11-15 23:56Z with it recorded (~90 GB/day of audit records). See
    # the header for why it is exempt and tr-clickhouse is not.
    "serviceAccount:${local.gcp_project_number}-compute@developer.gserviceaccount.com",
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
