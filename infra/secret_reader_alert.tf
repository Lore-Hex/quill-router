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
#   1. Any read by an account that is not in secret_readers below.
#   2. A read of one of the two signing keys by anyone but the one account
#      that signs with it. IAM already gives each key a single reader, but an
#      account with access to every secret in the project (the Compute Engine
#      default account, the control plane, tr-aws-cross-cloud, tr-ops-local,
#      the deploy identity and the owner) can still read it, and every one of
#      those but the owner is in secret_readers. A stolen token of any of them
#      reading a signing key pages here.
#
# Measured on 2026-11-16, the first day of records (00:24Z to 19:29Z): 64,052
# reads by nine principals. Eight are below with their counts. The ninth is
# the owner's own user login, which is not in this repository and so is not
# in the list: until the operator decides otherwise, its reads page. The
# accounts not seen that day are given secrets by this repository or by
# quill-cloud-proxy, and read when a service or a node starts or when a
# scheduled workflow runs.
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
# in the same change that gives it a secret, or its first read pages.
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

  # An entry with no principalEmail (a federated identity used directly,
  # with no service account) matches the NOT clause, so it pages.
  unexpected_secret_reader_filter = join(" AND ", [
    local.secret_read_filter,
    "NOT protoPayload.authenticationInfo.principalEmail=(${join(" OR ", formatlist("\"%s\"", sort(values(local.secret_readers))))})",
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
  display_name = "TR Security: a secret was read by an account that does not normally read secrets"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "A secret's value was read by an account that is not on the readers list"
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
    content   = "An account that is not on the readers list read a secret's value (Secret Manager AccessSecretVersion). The incident names the account (`protoPayload.authenticationInfo.principalEmail`, or `principalSubject` for a federated identity) and the secret (`protoPayload.resourceName`). If the account is a new workload that was given a secret on purpose, add it to `secret_readers` and run infra-apply. If not, treat its credential as stolen: list everything it read with `gcloud logging read 'protoPayload.serviceName=\"secretmanager.googleapis.com\" AND protoPayload.authenticationInfo.principalEmail=\"ACCOUNT\"' --project=quill-cloud-proxy --freshness=7d`, rotate each secret it read, and remove the credential. infra/secret_reader_alert.tf in Lore-Hex/quill-router."
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
    # The Logging query language takes at most 20,000 characters.
    precondition {
      condition     = length(local.unexpected_secret_reader_filter) <= 20000
      error_message = "The unexpected-secret-reader filter is longer than Cloud Logging accepts."
    }
  }
}

resource "google_monitoring_alert_policy" "signing_key_read_by_other" {
  project      = local.gcp_project_id
  display_name = "TR Security: a signing key was read by an account other than its signer"
  combiner     = "OR"
  severity     = "CRITICAL"

  conditions {
    display_name = "The fast path envelope key or the routing snapshot signing key was read by another account"
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
    content   = "A signing key's value was read by an account other than the one that signs with it: fastpath-envelope-key by anyone but tr-fastpath-node, or trustedrouter-routing-snapshot-signing-key by anyone but trusted-router-control-run. The operator's step that reads the routing snapshot key while adding a version (docs/runbooks/routing-snapshot-key.md) pages here too; any other read means the key must be treated as known to someone else. Rotate it (docs/runbooks/fastpath-key-rotation.md, docs/runbooks/routing-snapshot-key.md), then find what else the reading account did in the audit log. infra/secret_reader_alert.tf in Lore-Hex/quill-router."
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
