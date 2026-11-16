# A laptop key used from somewhere else.
#
# Two service accounts have a user-managed key that lives in a file on the
# operator's laptop: tr-deploy and tr-ops-local. The keys do not expire. They
# are removed if stolen, and that needs someone to notice. Every call made
# with one of them is recorded with the key's name and the caller's address
# (Admin Activity always, and ADMIN_READ for reads), and over a month of
# sampled days both keys were used from one address only. A call with either
# key from any other address pages.
#
# The known addresses are not in this repository, which is public. They are
# the comma-separated CIDR list in the secret tr-laptop-key-known-addresses,
# which the operator makes with gcloud before this merges, and replaces when
# the laptop's address changes by adding a version:
#
#   printf '%s' 'ADDRESS/32' | gcloud secrets versions add \
#     tr-laptop-key-known-addresses --project=quill-cloud-proxy --data-file=-
#
# followed by an infra-apply run. The value is sensitive to Terraform, so a
# plan in the public CI log shows the filter as hidden.
#
# CI authenticates by federation and presents no key name, so its calls never
# match. A page here that is the operator travelling is expected: add the new
# address. Otherwise remove the key from the operator's own login:
#
#   gcloud iam service-accounts keys list --iam-account=ACCOUNT --managed-by=user
#   gcloud iam service-accounts keys delete KEY_ID --iam-account=ACCOUNT

data "google_secret_manager_secret_version" "laptop_key_known_addresses" {
  project = local.gcp_project_id
  secret  = "tr-laptop-key-known-addresses"
}

locals {
  laptop_key_accounts = [
    "tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com",
    "tr-ops-local@${local.gcp_project_id}.iam.gserviceaccount.com",
  ]
  laptop_key_known_addresses = [
    for cidr in split(",", data.google_secret_manager_secret_version.laptop_key_known_addresses.secret_data) :
    trimspace(cidr) if trimspace(cidr) != ""
  ]
  laptop_key_used_elsewhere_filter = join(" AND ", concat(
    [
      "protoPayload.authenticationInfo.serviceAccountKeyName:\"/keys/\"",
      "protoPayload.authenticationInfo.principalEmail=(${join(" OR ", formatlist("\"%s\"", local.laptop_key_accounts))})",
      "protoPayload.requestMetadata.callerIp:*",
    ],
    [for cidr in local.laptop_key_known_addresses : "NOT ip_in_net(protoPayload.requestMetadata.callerIp, \"${cidr}\")"],
  ))
}

resource "google_monitoring_alert_policy" "laptop_key_used_elsewhere" {
  project      = local.gcp_project_id
  display_name = "TR Security: a laptop service-account key was used from an unknown address"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "A call made with a laptop key came from an address that is not on the known list"
    condition_matched_log {
      filter = local.laptop_key_used_elsewhere_filter
    }
  }

  # One notification an hour while it continues; the incident closes a day
  # after the last matching call. A log-matched policy must carry the rate
  # limit.
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "A call authenticated with a user-managed key of tr-deploy or tr-ops-local came from an address that is not on the known list (secret tr-laptop-key-known-addresses). If that is the operator on a new network, add the address to the secret and run infra-apply. If not, treat the key as stolen: list the account's keys with `gcloud iam service-accounts keys list --iam-account=ACCOUNT --managed-by=user` and remove the key with `gcloud iam service-accounts keys delete KEY_ID --iam-account=ACCOUNT`, from the operator's own login, then read what the key did in the audit log (`protoPayload.authenticationInfo.serviceAccountKeyName`). infra/laptop_key_use_alert.tf in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels

  lifecycle {
    precondition {
      condition     = length(local.laptop_key_known_addresses) > 0
      error_message = "tr-laptop-key-known-addresses is empty: the policy would page on every call made with a laptop key."
    }
  }
}
