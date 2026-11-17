# A laptop key used from somewhere else.
#
# Two service accounts have a user-managed key that lives in a file on the
# operator's laptop: tr-deploy and tr-ops-local. The keys do not expire. They
# are removed if stolen, and that needs someone to notice. A call made with
# one of them is recorded with the key's name and the caller's address when
# the call writes an audit entry: every change (Admin Activity), every read
# of configuration (ADMIN_READ, on for all services), every IAP tunnel
# opened, and data reads of Spanner and Secret Manager. A call with either
# key from an address that is not on the known list pages.
#
# What this does not see: reads of objects in Cloud Storage, which write no
# audit entry here. A stolen key used for nothing else would not page.
#
# The laptop is not always at one address. On one day of the month measured
# (2026-10-17) every call came from another network; the laptop's own session
# history shows the same commands at the same seconds. So a page that is the
# operator on another network is expected: add that address.
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
# plan in the public CI log shows the filter as hidden. The same list decides
# which user-login secret reads page in infra/secret_reader_alert.tf, so an
# address added here quiets both. Every infra-apply
# reads the newest version: add the replacement before disabling an old one,
# or every plan fails until a readable version exists.
#
# An entry that is not a CIDR, or a range wide enough to switch the alert
# off, stops the plan with a fixed message. It must stop there: the API's own
# refusal at apply quotes the bad value, and the apply log is public.
#
# CI authenticates by federation and presents no key name, so its calls never
# match. Cloud Scheduler writes a second entry for each call it receives,
# with the literal caller address "private"; the first entry carries the real
# address, so the copy is left out. The literal "gce-internal-ip" (a call
# from a VM in some project) is not left out: a laptop key on a VM pages.
#
# If a page is not the operator, remove the key from the operator's own login:
#
#   gcloud iam service-accounts keys list --iam-account=ACCOUNT --managed-by=user
#   gcloud iam service-accounts keys delete KEY_ID --iam-account=ACCOUNT

data "google_secret_manager_secret_version" "laptop_key_known_addresses" {
  project = local.gcp_project_id
  secret  = "tr-laptop-key-known-addresses"
}

# The channel the other security alerts use. It exists already and is read
# here, not managed.
data "google_monitoring_notification_channel" "security_alerts" {
  project      = local.gcp_project_id
  display_name = "TrustedRouter security alerts"
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
      "NOT protoPayload.requestMetadata.callerIp=\"private\"",
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

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]

  lifecycle {
    precondition {
      condition     = length(local.laptop_key_known_addresses) > 0
      error_message = "tr-laptop-key-known-addresses is empty: the policy would page on every call made with a laptop key."
    }
    # Fixed message, never the value. /16 is the widest range accepted.
    precondition {
      condition = alltrue([
        for cidr in local.laptop_key_known_addresses :
        can(cidrhost(cidr, 0)) && try(tonumber(split("/", cidr)[1]), 0) >= 16
      ])
      error_message = "tr-laptop-key-known-addresses holds an entry that is not a CIDR of /16 or narrower. Entries are separated by commas, each written ADDRESS/PREFIX."
    }
  }
}
