# No new service-account keys in this project.
#
# A user-managed key is a long-lived credential in a file. Minting one for an
# account an intruder can already act as is the standard way to keep access
# after the first way in is closed, and nothing in a normal deploy needs to
# mint one: the workloads and CI use attached identities and federation. The
# constraint stops key creation for every account in the project. Keys that
# already exist keep working.
#
# Only an Organization Policy Administrator can set or lift it, which the
# deploy identity is not, so a stolen deploy credential cannot turn it off.
# The operator turned it on by hand, and the import below adopts it:
#
#   gcloud resource-manager org-policies enable-enforce \
#     iam.disableServiceAccountKeyCreation --project=quill-cloud-proxy
#
# To mint a key on purpose (rotating a laptop key, or the cross-cloud key of
# tools/deploy-aws-nitro.sh in quill-cloud-proxy), the operator lifts it with
# the same command and disable-enforce, mints the key, and turns it back on.
# An infra-apply that runs while it is lifted plans to turn it back on, cannot,
# and fails; run it again afterwards.

import {
  to = google_project_organization_policy.no_new_service_account_keys
  id = "${local.gcp_project_id}:constraints/iam.disableServiceAccountKeyCreation"
}

resource "google_project_organization_policy" "no_new_service_account_keys" {
  project    = local.gcp_project_id
  constraint = "constraints/iam.disableServiceAccountKeyCreation"

  boolean_policy {
    enforced = true
  }
}
