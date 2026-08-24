#!/usr/bin/env bash
# Provision keyless cross-cloud access to GCP for the AWS and Azure test
# networks, via Workload Identity Federation.
#
# SCOPE — read docs/storage-portability/multi-cloud-separation.md first.
#
# Each cloud is a standalone deployment with its own database, so NO DATA PATH
# crosses clouds. This script is not part of one. It exists so that a workload
# on AWS or Azure can reach GCP for *operational* reasons — pulling the shared
# container image, bootstrap, ops tooling — without a long-lived key.
#
# The alternative is a service-account key JSON mirrored into each cloud's
# secret store. That is the single worst artifact in a multi-cloud setup — it
# never expires, it is copyable, and every new cloud multiplies it. So even for
# a narrow ops path it is worth not having.
#
# Do not extend this to carry application data between clouds. That is the
# design that was superseded.
#
# The answer here is federation. Each cloud already gives its own workloads a
# verifiable identity (an AWS instance role, an Azure managed identity). GCP
# STS accepts that identity directly and hands back a short-lived token. The
# configuration that makes it work is not a secret: it names a pool, a
# provider, and a service account to impersonate, and points the SDK at the
# LOCAL cloud's metadata for proof. So it travels as a plain env var — see
# scripts/entrypoint.sh, which refuses anything carrying key material.
#
# Net effect: zero long-lived Google credentials on any other cloud, and one
# identical mechanism per cloud rather than one bespoke story each.
#
# Usage:
#   bash scripts/deploy/wif_setup.sh                    # dry-run, prints plan
#   bash scripts/deploy/wif_setup.sh --apply            # create AWS provider
#   bash scripts/deploy/wif_setup.sh --apply --azure-tenant <TENANT_ID>
#   bash scripts/deploy/wif_setup.sh --emit-config aws  # print the JSON config
#   bash scripts/deploy/wif_setup.sh --emit-config azure
#
# Idempotent: every create is check-then-create.
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
POOL="${POOL:-multicloud}"
SA_NAME="${SA_NAME:-tr-multicloud}"
SPANNER_INSTANCE="${SPANNER_INSTANCE:-trusted-router-nam6}"
SPANNER_DATABASE="${SPANNER_DATABASE:-trusted-router}"
BIGTABLE_INSTANCE="${BIGTABLE_INSTANCE:-trusted-router-logs}"
AWS_ACCOUNT="${AWS_ACCOUNT:-330422590279}"
# The AWS role the test-network instance runs as. WIF is scoped to exactly
# this role, so an unrelated principal in the same account cannot impersonate
# the service account.
AWS_ROLE_NAME="${AWS_ROLE_NAME:-tr-test-network}"
AZURE_TENANT="${AZURE_TENANT:-}"
# Azure hands out tokens per audience. Default to the well-known exchange
# audience; override if a dedicated App Registration is used instead.
AZURE_AUDIENCE="${AZURE_AUDIENCE:-api://AzureADTokenExchange}"
AZURE_PRINCIPAL="${AZURE_PRINCIPAL:-}"   # managed identity object id (the `sub` claim)

APPLY=0
EMIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --azure-tenant) AZURE_TENANT="$2"; shift 2 ;;
    --azure-principal) AZURE_PRINCIPAL="$2"; shift 2 ;;
    --emit-config) EMIT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { echo "[wif] $*" >&2; }
run() {
  if [ "$APPLY" = "1" ]; then "$@"; else echo "  [dry-run] $*" >&2; fi
}

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
POOL_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}"

# ─── config emission ───────────────────────────────────────────────────────
# Printed, never written to a secret store. These files contain no key
# material; that is the entire point.
emit_config() {
  local cloud="$1"
  local impersonation="https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${SA_EMAIL}:generateAccessToken"
  case "$cloud" in
    aws)
      cat <<JSON
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/${POOL_RESOURCE}/providers/aws-workloads",
  "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
  "service_account_impersonation_url": "${impersonation}",
  "token_url": "https://sts.googleapis.com/v1/token",
  "credential_source": {
    "environment_id": "aws1",
    "region_url": "http://169.254.169.254/latest/meta-data/placement/availability-zone",
    "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials",
    "regional_cred_verification_url": "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15",
    "imdsv2_session_token_url": "http://169.254.169.254/latest/api/token"
  }
}
JSON
      ;;
    azure)
      # Azure's instance metadata endpoint mints an OIDC token for the VM's
      # managed identity. Same shape as AWS: proof comes from the local cloud.
      cat <<JSON
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/${POOL_RESOURCE}/providers/azure-workloads",
  "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
  "service_account_impersonation_url": "${impersonation}",
  "token_url": "https://sts.googleapis.com/v1/token",
  "credential_source": {
    "url": "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=${AZURE_AUDIENCE}",
    "headers": {"Metadata": "True"},
    "format": {"type": "json", "subject_token_field_name": "access_token"}
  }
}
JSON
      ;;
    *) echo "unknown cloud: $cloud (want aws|azure)" >&2; exit 2 ;;
  esac
}

if [ -n "$EMIT" ]; then
  emit_config "$EMIT"
  exit 0
fi

say "project=$PROJECT (#$PROJECT_NUMBER) pool=$POOL sa=$SA_EMAIL apply=$APPLY"

# ─── service account ───────────────────────────────────────────────────────
# Deliberately NOT the Cloud Run runtime SA. A test network on another cloud
# should not be able to do everything production can; it gets the two roles it
# needs to read and write the system of record, and nothing else. Notably no
# secretmanager.secretAccessor: nothing on a test network reads secrets yet,
# and the role can be added when something does.
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  say "service account exists"
else
  say "creating service account $SA_EMAIL"
  run gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name "TrustedRouter multi-cloud (federated)" \
    --description "Impersonated by AWS/Azure test networks via Workload Identity Federation. No keys."
fi

# Scoped to the specific database and instance rather than granted
# project-wide. A project-level roles/spanner.databaseUser reaches EVERY
# database in the project, including any future one; a test network on
# another cloud has no business with that.
say "granting spanner.databaseUser on ${SPANNER_INSTANCE}/${SPANNER_DATABASE}"
run gcloud spanner databases add-iam-policy-binding "$SPANNER_DATABASE" \
  --instance="$SPANNER_INSTANCE" --project "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/spanner.databaseUser --quiet

say "granting bigtable.user on ${BIGTABLE_INSTANCE}"
run gcloud bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE" --project "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/bigtable.user --quiet

# ─── pool ──────────────────────────────────────────────────────────────────
if gcloud iam workload-identity-pools describe "$POOL" --location=global --project "$PROJECT" >/dev/null 2>&1; then
  say "pool exists"
else
  say "creating pool $POOL"
  run gcloud iam workload-identity-pools create "$POOL" --location=global --project "$PROJECT" \
    --display-name "Multi-cloud test networks" \
    --description "AWS and Azure workloads federating into GCP. No key material."
fi

# ─── AWS provider ──────────────────────────────────────────────────────────
# GCP verifies a signed sts:GetCallerIdentity request, so the trust is in the
# AWS account, and the attribute mapping narrows it to one role.
if gcloud iam workload-identity-pools providers describe aws-workloads \
     --workload-identity-pool="$POOL" --location=global --project "$PROJECT" >/dev/null 2>&1; then
  say "aws provider exists"
else
  say "creating aws provider (account $AWS_ACCOUNT)"
  run gcloud iam workload-identity-pools providers create-aws aws-workloads \
    --workload-identity-pool="$POOL" --location=global --project "$PROJECT" \
    --account-id="$AWS_ACCOUNT" \
    --attribute-mapping="google.subject=assertion.arn,attribute.aws_role=assertion.arn.extract('assumed-role/{role}/')"
fi

say "binding AWS role $AWS_ROLE_NAME to $SA_EMAIL"
run gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/${POOL_RESOURCE}/attribute.aws_role/${AWS_ROLE_NAME}" \
  --quiet

# ─── Azure provider ────────────────────────────────────────────────────────
if [ -z "$AZURE_TENANT" ]; then
  say "skipping Azure provider (pass --azure-tenant <TENANT_ID> to create it)"
else
  if gcloud iam workload-identity-pools providers describe azure-workloads \
       --workload-identity-pool="$POOL" --location=global --project "$PROJECT" >/dev/null 2>&1; then
    say "azure provider exists"
  else
    say "creating azure provider (tenant $AZURE_TENANT)"
    run gcloud iam workload-identity-pools providers create-oidc azure-workloads \
      --workload-identity-pool="$POOL" --location=global --project "$PROJECT" \
      --issuer-uri="https://sts.windows.net/${AZURE_TENANT}/" \
      --allowed-audiences="$AZURE_AUDIENCE" \
      --attribute-mapping="google.subject=assertion.sub"
  fi

  if [ -z "$AZURE_PRINCIPAL" ]; then
    say "NOTE: no --azure-principal given, so nothing is bound yet."
    say "      Pass the managed identity's object id (the token's 'sub' claim)."
  else
    say "binding Azure principal $AZURE_PRINCIPAL to $SA_EMAIL"
    run gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT" \
      --role roles/iam.workloadIdentityUser \
      --member "principal://iam.googleapis.com/${POOL_RESOURCE}/subject/${AZURE_PRINCIPAL}" \
      --quiet
  fi
fi

say "done."
say ""
say "Credential config for a deployment (no secrets — pass as a plain env var):"
say "  bash $0 --emit-config aws"
say "  bash $0 --emit-config azure"
