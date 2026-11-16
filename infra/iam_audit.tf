# Data-read audit records for the IAM API.
#
# Records reads of service accounts, their keys and IAM policies, so a stolen
# credential's reconnaissance (listing accounts and keys, reading policies) is
# attributable to a caller and an address. It was turned on outside Terraform
# on 2026-11-16, and the daily baseline check then reported it as drift
# ("unexpected service-level auditConfigs: iam.googleapis.com"), correctly: it
# was not in code. This file is that config, so the baseline expects it.
#
# Volume is small: about 170 records in the first five and a half hours
# (ListServiceAccountKeys, GetServiceAccount, GetIAMPolicy, GetRole, mostly
# from the baseline check and Terraform plans). No account is exempt.
#
# The config already exists, so the import block below adopts it and
# infra-apply (tr-deploy, which can read but not write the project IAM policy)
# plans an import and no change. The resource is authoritative for this one
# service only: allServices ADMIN_READ and every other service's config are
# not touched.

import {
  to = google_project_iam_audit_config.iam_data_read
  id = "quill-cloud-proxy iam.googleapis.com"
}

resource "google_project_iam_audit_config" "iam_data_read" {
  project = local.gcp_project_id
  service = "iam.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }
}
