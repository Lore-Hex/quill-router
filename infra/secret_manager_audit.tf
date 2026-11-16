# Data-read audit records for Secret Manager.
#
# Reading a secret's value (AccessSecretVersion) is a DATA_READ operation,
# and the project recorded ADMIN_READ only, so a read of any of the project's
# secrets left no record of who made it. The November intrusion worked with a
# stolen credential (docs/incidents/2026-11-11-spanner-stolen-token-table-scans.md):
# the first thing such a credential is used for is reading secrets, and these
# records name the caller, the secret and the calling address.
#
# No account is exempt, the serving fleet included. Unlike the database's
# reads, secret reads are few: about 65,000 a day across every caller
# (measured 2026-11-13 to 2026-11-15 from the API's request counts), which is
# on the order of 0.1 GB of records a day. A stolen workload token is exactly
# the caller these records exist to name, so exempting the workloads would
# remove most of their value.
#
# infra-apply runs as tr-deploy, which cannot write the project IAM policy, so
# an operator creates this resource with a targeted apply
# (terraform apply -target=google_project_iam_audit_config.secret_manager_data_read)
# before the change merges; infra-apply then plans no change. The resource is
# authoritative for this one service only: the allServices ADMIN_READ config
# and any other service's config are not touched.

resource "google_project_iam_audit_config" "secret_manager_data_read" {
  project = local.gcp_project_id
  service = "secretmanager.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }
}
