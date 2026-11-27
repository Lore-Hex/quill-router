# A secret's value read by an account that does not normally read secrets.
#
# Every read of a secret's value is recorded with the caller's name since
# DATA_READ records were turned on for Secret Manager
# (infra/secret_manager_audit.tf). A stolen credential's first use is
# reading secrets (docs/incidents/2026-11-11-spanner-stolen-token-table-scans.md),
# so a read by an account that has no reason to read one pages.
#
# Two policies:
#
#   1. Any read by an account that is not in secret_readers below, except a
#      read by a user login from one of the operator's known addresses.
#   2. A read of one of the two signing keys by anyone but the one account
#      that signs with it. IAM already gives each key a single reader, but an
#      account with access to every secret in the project (the Compute Engine
#      default account, the control plane, tr-aws-cross-cloud, tr-ops-local,
#      the deploy identity and the owner) can still read it, and every one of
#      those but the owner is in secret_readers. A stolen token of any of them
#      reading a signing key pages here.
#
# Measured on 2026-11-16, the first day of records (00:24Z to 19:29Z): 64,046
# reads by nine principals, not counting the operator's test reads. Eight are
# below with their counts. The accounts not seen that day are given secrets
# by this repository or by quill-cloud-proxy, and read when a service or a
# node starts or when a scheduled workflow runs.
#
# The ninth is the owner's own user login: the operator and the operator's
# agents read secrets with it many times a day (the ClickHouse operator
# password, for one). Its address is not written here. Instead a read by any
# user login, as opposed to a service account, from an address on the laptop
# alert's known list (local.laptop_key_known_addresses, from the secret
# tr-laptop-key-known-addresses; infra/laptop_key_use_alert.tf) does not
# page, and from any other address it does. The project's IAM policy lets no
# other user login read a secret, so in practice this is the owner's. The
# list is held to the laptop alert's rule (not empty, every entry a CIDR of
# /16 or narrower) by the same two checks, and the filter is hidden in plans
# for the same reason.
#
# Accounts that can read secrets but are not in the list, so their first
# read pages: quill-cloud-run (nine secrets; neither repository explains
# it), and lightning-router-web and lightning-router-migrate, whose service
# was retired on 2026-10-22 (docs/lightning-retirement.md) and whose access
# remains. No Google service agent read a secret that day, and
# enclave-dns-reconciler, tr-ci-audit, tr-multicloud and
# tr-google-data-manager can read no secret.
#
# There is no clause for Cloud Scheduler's copies (the literal caller address
# "private", infra/laptop_key_use_alert.tf). Scheduler does not read secrets,
# and every Cloud Run read carries "private" as its address, so leaving those
# entries out would hide a read by an unknown account from inside a service.
#
# Adding an account: put it in secret_readers with where its reads come from,
# in the same change that gives it a secret, or its first read pages. An
# account this public repository must not name goes in the confidential list
# instead (infra/confidential_secret_readers.tf), before its first read.
#
# The two policies are separate because a policy with a log-match condition
# takes no other condition.

locals {
  service_account_domain = "${local.gcp_project_id}.iam.gserviceaccount.com"

  # Every account that reads a secret's value in normal operation. Reads on
  # 2026-11-16 in brackets.
  secret_readers = {
    # The gateway's Cloud Run revisions (scripts/deploy/_lib.sh,
    # RUN_SERVICE_ACCOUNT). [34,380]
    gateway_runtime = "${local.gcp_project_number}-compute@developer.gserviceaccount.com"
    # The control plane's Cloud Run revisions (scripts/deploy/_lib.sh,
    # CONTROL_RUN_SERVICE_ACCOUNT). [23,374]
    control_runtime = "trusted-router-control-run@${local.service_account_domain}"
    # The enclave fleet's VMs (quill-cloud-proxy tools/deploy-gcp-mig.sh,
    # WORKLOAD_SA). [4,174]
    enclave_workload = "quill-workload@${local.service_account_domain}"
    # The growth export job (docs/marketing/axiom-growth-refresh.md). [916]
    growth_sync = "tr-growth-sync@${local.service_account_domain}"
    # The public surface (scripts/deploy/public_surface.sh). [660]
    public_runtime = "tr-public@${local.service_account_domain}"
    # Deploys, infra-apply and every workflow of the deploy tier
    # (infra/ci_identities.tf), and the laptop key. [477]
    deploy = "${local.ci_tier_accounts["deploy"]}@${local.service_account_domain}"
    # The operator's laptop key (infra/laptop_key_use_alert.tf). [23]
    ops_local = "tr-ops-local@${local.service_account_domain}"
    # The fast path's nodes: the envelope key and the gateway token
    # (infra/fastpath.tf). [16]
    fastpath_node = google_service_account.fastpath_node.email
    # The hourly price refresh's provider keys (infra/ci_identities.tf). [0]
    ci_prices = google_service_account.ci_tier["prices"].email
    # The daily embeddings probe's monitor key (infra/ci_identities.tf). [0]
    ci_probe = google_service_account.ci_tier["probe"].email
    # The internal surface (scripts/deploy/internal_surface.sh). [0]
    internal_runtime = "tr-internal@${local.service_account_domain}"
    # The ClickHouse nodes read their password at boot
    # (scripts/deploy/clickhouse_startup.sh, infra/clickhouse_node_role.tf). [0]
    clickhouse_node = trimprefix(local.clickhouse_node_member, "serviceAccount:")
    # The AWS enclave's account (quill-cloud-proxy tools/deploy-aws-nitro.sh). [0]
    aws_enclave = "tr-aws-cross-cloud@${local.service_account_domain}"
  }

  # Each signing key, and the one account that signs with it.
  signing_key_readers = {
    (google_secret_manager_secret.fastpath_envelope_key.secret_id)        = local.secret_readers.fastpath_node
    (google_secret_manager_secret.routing_snapshot_signing_key.secret_id) = local.secret_readers.control_runtime
  }

  # A read of a secret's value, through any version of the API.
  secret_read_filter = join(" AND ", [
    "log_id(\"cloudaudit.googleapis.com/data_access\")",
    "protoPayload.serviceName=\"secretmanager.googleapis.com\"",
    "protoPayload.methodName:\"SecretManagerService.AccessSecretVersion\"",
  ])

  # A user login (an address that is not a service account's) calling from
  # one of the operator's known addresses. A caller address that is not an
  # address ("private", "gce-internal-ip") or is missing is in no range.
  operator_login_at_known_address = join(" AND ", [
    "protoPayload.authenticationInfo.principalEmail:*",
    "NOT protoPayload.authenticationInfo.principalEmail:\"gserviceaccount.com\"",
    "(${join(" OR ", [
      for cidr in local.laptop_key_known_addresses :
      "ip_in_net(protoPayload.requestMetadata.callerIp, \"${cidr}\")"
    ])})",
  ])

  # The listed accounts plus the confidential ones that this public repository
  # does not name (infra/confidential_secret_readers.tf).
  all_secret_readers = sort(concat(values(local.secret_readers), local.confidential_secret_readers))

  # An entry with no principalEmail (a federated identity used directly,
  # with no service account) matches both NOT clauses, so it pages.
  unexpected_secret_reader_filter = join(" AND ", [
    local.secret_read_filter,
    "NOT protoPayload.authenticationInfo.principalEmail=(${join(" OR ", formatlist("\"%s\"", local.all_secret_readers))})",
    "NOT (${local.operator_login_at_known_address})",
  ])

  signing_key_read_by_other_filter = join(" AND ", [
    local.secret_read_filter,
    "(${join(" OR ", [
      for secret, reader in local.signing_key_readers :
      "(protoPayload.resourceName:\"/secrets/${secret}/\" AND NOT protoPayload.authenticationInfo.principalEmail=\"${reader}\")"
    ])})",
  ])
}

resource "google_monitoring_alert_policy" "unexpected_secret_reader" {
  project      = local.gcp_project_id
  display_name = "TR Security: unexpected caller attempted to read a secret"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "A secret read was attempted by an unlisted account or a user from an unknown address"
    condition_matched_log {
      filter = local.unexpected_secret_reader_filter
    }
  }

  # One notification an hour while it continues; the incident closes a day
  # after the last matching read. A log-matched policy must carry the rate
  # limit.
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "An unexpected caller attempted AccessSecretVersion. Check the matching audit record: protoPayload.status.code = 7 means permission denied; this request did not return the value. Inspect all calls by the principal (principalEmail or principalSubject), their resourceName, callerIp, status and authorizationInfo. A denied attempt warrants investigation, not a claim of disclosure. If there are successful unexpected reads, contain the credential and rotate the affected secrets. Verify any new workload or operator address independently before changing any allowlist. See infra/secret_reader_alert.tf."
    mime_type = "text/markdown"
  }

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]

  lifecycle {
    # Fixed messages, never a value.
    precondition {
      condition     = length(local.secret_readers) > 0
      error_message = "secret_readers is empty: the policy would page on every secret read."
    }
    precondition {
      condition = alltrue([
        for email in values(local.secret_readers) :
        can(regex("^[a-z0-9][a-z0-9.-]*@[a-z0-9][a-z0-9.-]*\\.gserviceaccount\\.com$", email))
      ])
      error_message = "secret_readers holds an entry that is not one whole service-account address. Wildcards, users, groups and domains are refused: each entry names one account."
    }
    precondition {
      condition     = length(distinct(values(local.secret_readers))) == length(local.secret_readers)
      error_message = "secret_readers names the same account twice."
    }
    # The confidential list (infra/confidential_secret_readers.tf) may name only whole
    # service accounts of this project: no other project's, no Google-managed
    # account, no user, group, domain or wildcard. Fixed messages: the list
    # is private and the plan log is public.
    # The whole entry is matched, start to end: the list is interpolated into
    # the Logging filter, so a quote, space, second "@" or operator inside an
    # entry could otherwise add alternatives to the exemption.
    precondition {
      condition = alltrue([
        for email in local.confidential_secret_readers :
        can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", email))
        && endswith(email, "@${local.service_account_domain}")
      ])
      error_message = "tr-confidential-secret-readers holds an entry that is not one whole service account of this project. Entries are separated by commas."
    }
    precondition {
      condition     = length(distinct(local.all_secret_readers)) == length(local.all_secret_readers)
      error_message = "tr-confidential-secret-readers names an account twice, or one that secret_readers already lists."
    }
    # The Logging query language takes at most 20,000 characters.
    precondition {
      condition     = length(local.unexpected_secret_reader_filter) <= 20000
      error_message = "The unexpected-secret-reader filter is longer than Cloud Logging accepts."
    }
    # The laptop alert's two checks on the known addresses, word for word
    # (infra/laptop_key_use_alert.tf). Empty, the filter would not parse; too
    # wide, a user login would not page from anywhere.
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

resource "google_monitoring_alert_policy" "signing_key_read_by_other" {
  project      = local.gcp_project_id
  display_name = "TR Security: an account other than the signer attempted to read a signing key"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "An unexpected account attempted to read the envelope or routing snapshot signing key"
    condition_matched_log {
      filter = local.signing_key_read_by_other_filter
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }

  documentation {
    content   = "An account other than the configured signer attempted AccessSecretVersion on an envelope or routing snapshot signing key. Check the audit record: protoPayload.status.code = 7 means permission denied; this request did not return the key. Investigate all calls by the principal. For successful reads, verify whether this was the documented operator key-version procedure (docs/runbooks/routing-snapshot-key.md). Treat an unexplained successful read as disclosure: follow docs/runbooks/fastpath-key-rotation.md or docs/runbooks/routing-snapshot-key.md and investigate the credential."
    mime_type = "text/markdown"
  }

  notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]

  lifecycle {
    precondition {
      condition = alltrue([
        for secret, reader in local.signing_key_readers :
        can(regex("^[a-zA-Z0-9_-]+$", secret)) && can(regex("^[a-z0-9][a-z0-9.-]*@[a-z0-9][a-z0-9.-]*\\.gserviceaccount\\.com$", reader))
      ])
      error_message = "signing_key_readers holds an entry that is not a secret id mapped to one whole service-account address."
    }
  }
}
