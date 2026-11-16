# The service accounts that GitHub Actions workflows run as, by tier.
#
# infra/gcp_wif.tf sorts every admitted workflow into a tier and the provider
# maps the tier to attribute.tier. This file binds each tier to one service
# account and gives the three new accounts exactly what their workflows read:
#
#   prices  tr-ci-prices            .github/workflows/refresh-prices.yml
#   audit   tr-ci-audit             typed-audit.yml
#   probe   tr-ci-probe             cloud-security-baseline.yml,
#                                   daily-embeddings-probe.yml and
#                                   mirror-repo.yml
#   dns     enclave-dns-reconciler  quill-cloud-proxy's reconcile-enclave-dns.yml
#   deploy  tr-deploy               every other admitted workflow
#
# Before the tiers, every admitted workflow ran as tr-deploy, which is close
# to a project administrator, so a mistake or a compromise in a daily probe
# had the reach of a deploy. None of the new accounts can change the project's
# IAM policy, a service account, a secret's value, a database row or a running
# service. Only tr-ci-audit reads the database.
#
# tr-deploy and enclave-dns-reconciler exist already and are not managed here;
# only their tier binding is. tr-deploy's two repository-wide bindings
# (attribute.repository/Lore-Hex/quill-router and .../quill-cloud-proxy) were
# made by hand, are not in Terraform, and are not touched: while they exist
# any admitted workflow can still impersonate tr-deploy, so this file adds
# smaller accounts without yet confining the large one. An owner removes them
# once every moved workflow has run on its own account
# (docs/runbooks/ci-identities.md, phase 5).
#
# infra-apply runs as tr-deploy, which can neither add a project custom role
# nor write the project IAM policy. An owner therefore applies the two roles
# and their two project bindings from the branch before the change merges.
# The same apply makes the three accounts, because a binding needs its member
# to exist and a targeted apply takes the account resource whole:
#
#   terraform -chdir=infra apply -target=google_project_iam_member.ci_tier
#
# The plan should show seven resources to add and nothing to change or
# destroy: google_service_account.ci_tier for "audit", "prices" and "probe",
# and google_project_iam_custom_role.ci_tier and
# google_project_iam_member.ci_tier for "prices" and "probe" (tr-ci-audit
# holds nothing on the project). infra-apply then plans no change for them
# and applies the rest, all of it resource-level: the tier bindings, the
# secret, bucket and database bindings, and the provider's mapping.

locals {
  # The account each tier's workflows impersonate. tests/test_ci_identities.py
  # holds every workflow's service_account to this map.
  ci_tier_accounts = {
    audit  = "tr-ci-audit"
    deploy = "tr-deploy"
    dns    = "enclave-dns-reconciler"
    prices = "tr-ci-prices"
    probe  = "tr-ci-probe"
  }

  # What cloud-security-baseline.yml and mirror-repo.yml read at project
  # level, one permission for each gcloud call they make
  # (scripts/deploy/gcp_security_baseline_check.sh,
  # scripts/deploy/public_surface_edge.sh check and scripts/deploy/_lib.sh).
  # Nothing here creates, changes or deletes anything, and nothing reads a
  # secret, an object or a database row.
  ci_probe_project_permissions = [
    "compute.backendServices.get",           # backend-services describe (the public backend's client-IP header)
    "compute.firewalls.get",                 # firewall-rules describe tr-allow-iap-ssh-all
    "compute.firewalls.list",                # firewall-rules list
    "essentialcontacts.contacts.list",       # essential-contacts compute
    "iam.serviceAccountKeys.list",           # iam service-accounts keys list
    "logging.logMetrics.get",                # logging metrics describe (the four CIS metrics)
    "monitoring.alertPolicies.list",         # alpha monitoring policies list
    "monitoring.notificationChannels.list",  # alpha monitoring channels list
    "resourcemanager.projects.get",          # projects describe (_lib.sh, also sourced by the mirror)
    "resourcemanager.projects.getIamPolicy", # projects get-iam-policy (the audit configuration)
    "run.revisions.get",                     # run revisions describe (the serving revision's client-IP mode)
    "run.services.get",                      # run services describe (ingress and traffic)
  ]

  # What refresh-prices.yml calls at project level: generateContent and
  # rawPredict on Vertex AI publisher models, through application default
  # credentials (scripts/pricing/providers/google_vertex.py). Listing the
  # publisher models needs no permission. Publisher models have no IAM policy
  # of their own, so this cannot be narrower than the project.
  ci_prices_project_permissions = [
    "aiplatform.endpoints.predict",
  ]

  # Every secret refresh-prices.yml reads, in the workflow's order: the
  # self-heal key, the Together key, the provider keys of its loop and the
  # optional 0G key. tests/test_ci_identities.py fails when this list and the
  # workflow differ, so a provider key is added to both in one change. The
  # secret must exist before that change merges (scripts/deploy/secrets.sh
  # creates it): a binding on a missing secret fails the apply.
  ci_prices_secret_ids = [
    "trustedrouter-tr-api-key-for-self-heal",
    "trustedrouter-together-api-key",
    "trustedrouter-parasail-api-key",
    "trustedrouter-lightning-api-key",
    "trustedrouter-gmi-api-key",
    "trustedrouter-deepinfra-api-key",
    "trustedrouter-siliconflow-api-key",
    "trustedrouter-venice-api-key",
    "trustedrouter-openai-api-key",
    "trustedrouter-meta-api-key",
    "trustedrouter-grok-api-key",
    "trustedrouter-deepseek-api-key",
    "trustedrouter-mistral-api-key",
    "trustedrouter-zai-api-key",
    "trustedrouter-cerebras-api-key",
    "trustedrouter-kimi-api-key",
    "trustedrouter-fireworks-api-key",
    "trustedrouter-gemini-api-key",
    "trustedrouter-novita-api-key",
    "trustedrouter-nebius-api-key",
    "trustedrouter-minimax-api-key",
    "trustedrouter-crusoe-api-key",
    "trustedrouter-friendli-api-key",
    "trustedrouter-baseten-api-key",
    "trustedrouter-telnyx-api-key",
    "trustedrouter-wafer-api-key",
    "trustedrouter-alibaba-api-key",
    "trustedrouter-makora-api-key",
    "trustedrouter-chutes-api-key",
    "trustedrouter-near-ai-api-key",
    "trustedrouter-digitalocean-api-key",
    "trustedrouter-cloudflare-workers-ai-api-token",
    "trustedrouter-inceptron-api-key",
    "trustedrouter-morph-api-key",
    "trustedrouter-atlas-cloud-api-key",
    "trustedrouter-neurometric-api-key",
    "trustedrouter-engy-api-key",
    "trustedrouter-confidential-ai-api-key",
    "trustedrouter-scaledown-api-key",
    "trustedrouter-pearl-api-key",
    "trustedrouter-stepfun-api-key",
    "trustedrouter-relace-api-key",
    "trustedrouter-recraft-api-key",
    "trustedrouter-bfl-api-key",
    "trustedrouter-decart-api-key",
    "trustedrouter-nvidia-nim-api-key",
    "trustedrouter-io-net-api-key",
    "trustedrouter-tencent-tokenhub-api-key",
    "trustedrouter-scaleway-api-key",
    "trustedrouter-regolo-api-key",
    "trustedrouter-lyceum-api-key",
    "trustedrouter-bedrock-api-key",
    "trustedrouter-greenference-api-key",
    "trustedrouter-hixtube-api-key",
    "trustedrouter-muna-api-key",
    "trustedrouter-negbeta-api-key",
    "trustedrouter-coralbricks-api-key",
    "trustedrouter-boundless-api-key",
    "trustedrouter-zro-api-key",
    "trustedrouter-byteplus-api-key",
    "trustedrouter-system1models-global-api-key",
    "trustedrouter-system1models-eu-api-key",
    "trustedrouter-privatemode-api-key",
    "trustedrouter-featherless-api-key",
    "trustedrouter-jina-api-key",
    "trustedrouter-sakana-api-key",
    "trustedrouter-perplexity-api-key",
    "trustedrouter-krea-api-key",
    "trustedrouter-fal-api-key",
    "trustedrouter-wandb-api-key",
    "trustedrouter-upstage-api-key",
    "trustedrouter-sail-research-api-key",
    "trustedrouter-reka-api-key",
    "trustedrouter-nextbit-api-key",
    "trustedrouter-akashml-api-key",
    "trustedrouter-mancer-api-key",
    "trustedrouter-abliterate-api-key",
    "trustedrouter-abliteration-api-key",
    "trustedrouter-nscale-api-key",
    "trustedrouter-aion-labs-api-key",
    "trustedrouter-sambanova-api-key",
    "trustedrouter-streamlake-api-key",
    "trustedrouter-arcee-api-key",
    "trustedrouter-inception-api-key",
    "trustedrouter-zero-g-api-key",
  ]

  # The secrets the workflow runs `gcloud secrets describe` on before reading
  # them, which takes secretmanager.secrets.get as well.
  ci_prices_described_secret_ids = [
    "trustedrouter-zero-g-api-key",
  ]
}

# --- The three new accounts, and what only an owner can apply --------------

resource "google_service_account" "ci_tier" {
  for_each = {
    audit  = "TrustedRouter CI: daily database audits"
    prices = "TrustedRouter CI: hourly price refresh"
    probe  = "TrustedRouter CI: scheduled checks and repository mirror"
  }

  project      = local.gcp_project_id
  account_id   = local.ci_tier_accounts[each.key]
  display_name = each.value
}

resource "google_project_iam_custom_role" "ci_tier" {
  for_each = {
    prices = {
      role_id     = "trustedRouterCiPrices"
      title       = "TrustedRouter CI price refresh"
      description = "Call Vertex AI publisher models for the hourly price refresh's discovery canaries; nothing else."
      permissions = local.ci_prices_project_permissions
    }
    probe = {
      role_id     = "trustedRouterCiProbe"
      title       = "TrustedRouter CI probe"
      description = "Read the project settings the scheduled security baseline checks; no writes, no secrets, no data."
      permissions = local.ci_probe_project_permissions
    }
  }

  project     = local.gcp_project_id
  role_id     = each.value.role_id
  title       = each.value.title
  description = each.value.description
  stage       = "GA"
  permissions = each.value.permissions
}

resource "google_project_iam_member" "ci_tier" {
  for_each = google_project_iam_custom_role.ci_tier

  project = local.gcp_project_id
  role    = each.value.id
  member  = google_service_account.ci_tier[each.key].member
}

# --- Which workflows may impersonate which account -------------------------

# Additive: one member in each account's roles/iam.workloadIdentityUser
# binding. tr-deploy holds roles/iam.serviceAccountAdmin on the project, so it
# sets this on the accounts that existed before this file as well as on the
# new ones and on itself.
resource "google_service_account_iam_member" "ci_tier" {
  for_each = local.ci_tier_accounts

  service_account_id = "projects/${local.gcp_project_id}/serviceAccounts/${each.value}@${local.gcp_project_id}.iam.gserviceaccount.com"
  role               = "roles/iam.workloadIdentityUser"
  member             = local.github_tier_principal_sets[each.key]

  depends_on = [google_service_account.ci_tier]
}

# --- audit -----------------------------------------------------------------

# typed-audit.yml: both jobs open sessions and read in one read-only snapshot,
# the billing tables (scripts/audit_typed_counters.py) and INFORMATION_SCHEMA
# (scripts/audit_spanner_schema.py). The role has no write, schema or IAM
# permission. It does read every table of this database, which is why this
# workflow has an account to itself: no other tier's account can read a row.
resource "google_spanner_database_iam_member" "ci_audit_database_reader" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.databaseReader"
  member   = google_service_account.ci_tier["audit"].member
}

# --- probe -----------------------------------------------------------------

# daily-embeddings-probe.yml reads the synthetic monitor's API key.
resource "google_secret_manager_secret_iam_member" "ci_probe_monitor_key" {
  project   = local.gcp_project_id
  secret_id = "trustedrouter-synthetic-monitor-api-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.ci_tier["probe"].member
}

# mirror-repo.yml (scripts/deploy/mirror_repo_to_gcs.sh) describes this
# bucket, sets its versioning and lifecycle on every run, and writes two
# bundles, replacing latest.bundle. The role is the bucket's administrator and
# reaches no other bucket.
resource "google_storage_bucket_iam_member" "ci_probe_git_mirror" {
  bucket = "quill-cloud-proxy-git-mirror"
  role   = "roles/storage.admin"
  member = google_service_account.ci_tier["probe"].member
}

# cloud-security-baseline.yml describes these two buckets (the ACME cache's
# encryption key and soft delete, the archive's retention rule). The role
# reads bucket settings: no objects, not even their names.
resource "google_storage_bucket_iam_member" "ci_probe_baseline_bucket" {
  for_each = toset([
    "quill-acme-cache",
    "quill-cloud-proxy-tr-clickhouse-archive",
  ])

  bucket = each.key
  role   = "roles/storage.bucketViewer"
  member = google_service_account.ci_tier["probe"].member
}

# --- prices ----------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "ci_prices_secret" {
  for_each = toset(local.ci_prices_secret_ids)

  project   = local.gcp_project_id
  secret_id = each.key
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.ci_tier["prices"].member
}

resource "google_secret_manager_secret_iam_member" "ci_prices_described_secret" {
  for_each = toset(local.ci_prices_described_secret_ids)

  project   = local.gcp_project_id
  secret_id = each.key
  role      = "roles/secretmanager.viewer"
  member    = google_service_account.ci_tier["prices"].member
}
