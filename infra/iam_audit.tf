# Data-read audit records for the IAM API.
#
# The operator's tripwire setup (2026-11-16) turned this on outside Terraform
# so that reads of decoy service accounts (GetServiceAccount,
# ListServiceAccountKeys, GetIAMPolicy on them) leave a record that its alert
# policies can match. The daily baseline check then reported it as drift
# ("unexpected service-level auditConfigs: iam.googleapis.com"), correctly:
# it was not in code. This file is that config, so the baseline expects it.
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
