# Service accounts that read secrets in normal operation but are not named in
# this repository, which is public. A job run for an outside party under a
# confidential agreement is one: listing its account here would disclose the
# arrangement.
#
# Their addresses are the comma-separated list in the secret
# tr-confidential-secret-readers. The operator creates it with gcloud before the
# change that first reads it merges, and changes the list by adding a version:
#
#   printf '%s' 'ACCOUNT@PROJECT.iam.gserviceaccount.com' | gcloud secrets versions add \
#     tr-confidential-secret-readers --project=quill-cloud-proxy --data-file=-
#
# followed by an infra-apply run, which every change under infra/ starts.
# Every infra-apply reads the newest version: add the replacement before
# disabling an old one, or every plan fails until a readable version exists.
#
# infra/secret_reader_alert.tf treats these accounts like its own
# secret_readers: a read by one of them does not page. The value is sensitive
# to Terraform, so a plan in the public CI log shows the filter as hidden, the
# same as the operator's known addresses (infra/laptop_key_use_alert.tf).
# Only whole service accounts of this project are accepted; anything else
# stops the plan with a fixed message (the checks are on the alert policy), so
# no entry is ever quoted in a public log.
#
# The secret's container is managed here and adopted by the import in
# infra/imports.tf; its versions are added by hand, so the list is never in
# this file. Terraform does keep it, marked sensitive, in the plan and in the
# private state bucket (through the data source and the alert filter), as it
# already does for the operator's known addresses. No IAM binding: the deploy
# identity, which runs infra-apply, already reads every secret in the project.
resource "google_secret_manager_secret" "confidential_secret_readers" {
  project   = local.gcp_project_id
  secret_id = "tr-confidential-secret-readers"

  # As created (automatic). Changing replication would replace the secret and
  # destroy its versions.
  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

data "google_secret_manager_secret_version" "confidential_secret_readers" {
  project = local.gcp_project_id
  secret  = "tr-confidential-secret-readers"
}

locals {
  confidential_secret_readers = [
    for email in split(",", data.google_secret_manager_secret_version.confidential_secret_readers.secret_data) :
    trimspace(email) if trimspace(email) != ""
  ]
}
