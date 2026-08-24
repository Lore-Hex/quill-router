#!/usr/bin/env bash
# Phase 1: enable GCP APIs and provision Spanner + Bigtable.
# Idempotent — skip-if-exists for every step. Safe to re-run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

log "enabling required GCP APIs"
gc services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudkms.googleapis.com \
  datamanager.googleapis.com \
  spanner.googleapis.com \
  bigtableadmin.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com

log "ensuring Spanner instance/database"
if ! gc spanner instances describe "$SPANNER_INSTANCE_ID" >/dev/null 2>&1; then
  gc spanner instances create "$SPANNER_INSTANCE_ID" \
    --config="$SPANNER_CONFIG" \
    --edition="$SPANNER_EDITION" \
    --description="TrustedRouter ledger" \
    --processing-units="$SPANNER_PROCESSING_UNITS"
fi
if ! gc spanner databases describe "$SPANNER_DATABASE_ID" --instance="$SPANNER_INSTANCE_ID" >/dev/null 2>&1; then
  gc spanner databases create "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --database-dialect=GOOGLE_STANDARD_SQL \
    --ddl='CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind, id)'
fi
if [ "$(gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(versionRetentionPeriod)')" != "7d" ]; then
  gc spanner databases ddl update "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --ddl="ALTER DATABASE \`${SPANNER_DATABASE_ID}\` SET OPTIONS (version_retention_period = '7d')"
fi
if [ "$(gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(enableDropProtection)')" != "True" ]; then
  gc spanner databases update "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --enable-drop-protection \
    --quiet
fi

log "ensuring Bigtable instance/table"
if ! gc bigtable instances describe "$BIGTABLE_INSTANCE_ID" >/dev/null 2>&1; then
  gc bigtable instances create "$BIGTABLE_INSTANCE_ID" \
    --display-name="TrustedRouter logs" \
    --instance-type="$BIGTABLE_INSTANCE_TYPE" \
    --cluster="$BIGTABLE_CLUSTER_ID" \
    --cluster-zone="${ZONE:-${REGION}-a}" \
    --cluster-num-nodes=1
fi
if ! gc bigtable instances tables describe "$BIGTABLE_GENERATION_TABLE" --instance="$BIGTABLE_INSTANCE_ID" >/dev/null 2>&1; then
  gc bigtable instances tables create "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" \
    --column-families=m
fi

# Retention migrations need only table-schema reads and column-family updates.
# Keep that capability table-scoped and separate from row-data access.
BIGTABLE_SCHEMA_ROLE_ID="${TR_BIGTABLE_SCHEMA_ROLE_ID:-trustedRouterBigtableSchemaManager}"
BIGTABLE_SCHEMA_ROLE="projects/${PROJECT_ID}/roles/${BIGTABLE_SCHEMA_ROLE_ID}"
DEPLOY_SERVICE_ACCOUNT="${TR_DEPLOY_SERVICE_ACCOUNT:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
OPS_SERVICE_ACCOUNT="${TR_OPS_SERVICE_ACCOUNT:-tr-ops-local@${PROJECT_ID}.iam.gserviceaccount.com}"
if ! gc iam roles describe "$BIGTABLE_SCHEMA_ROLE_ID" >/dev/null 2>&1; then
  gc iam roles create "$BIGTABLE_SCHEMA_ROLE_ID" \
    --title="TrustedRouter Bigtable Schema Manager" \
    --description="May read table schema and update column-family GC policies; no row data access." \
    --permissions=bigtable.tables.get,bigtable.tables.update \
    --stage=GA \
    --quiet
fi
for service_account in "$DEPLOY_SERVICE_ACCOUNT" "$OPS_SERVICE_ACCOUNT"; do
  gc bigtable tables add-iam-policy-binding "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" \
    --member="serviceAccount:${service_account}" \
    --role="$BIGTABLE_SCHEMA_ROLE" \
    --quiet >/dev/null
done

log "ensuring production deployment mutex bucket"
DEPLOY_MUTEX_BUCKET="${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}"
DEPLOY_MUTEX_LOCATION="${TR_DEPLOY_MUTEX_LOCATION:-us-central1}"
DEPLOY_MUTEX_LIFECYCLE_FILE="$(mktemp "${TMPDIR:-/tmp}/tr-deploy-mutex-lifecycle-XXXXXX.json")"
printf '%s\n' \
  '{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}' \
  >"$DEPLOY_MUTEX_LIFECYCLE_FILE"
if ! gc storage buckets describe "gs://${DEPLOY_MUTEX_BUCKET}" >/dev/null 2>&1; then
  gc storage buckets create "gs://${DEPLOY_MUTEX_BUCKET}" \
    --location="$DEPLOY_MUTEX_LOCATION" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --lifecycle-file="$DEPLOY_MUTEX_LIFECYCLE_FILE" \
    --quiet
fi
# Reassert the safety controls on existing buckets as well as newly created
# ones so a later manual setting change is repaired by the idempotent script.
gc storage buckets update "gs://${DEPLOY_MUTEX_BUCKET}" \
  --uniform-bucket-level-access \
  --public-access-prevention \
  --lifecycle-file="$DEPLOY_MUTEX_LIFECYCLE_FILE" \
  --quiet
rm -f "$DEPLOY_MUTEX_LIFECYCLE_FILE"
gc storage buckets add-iam-policy-binding "gs://${DEPLOY_MUTEX_BUCKET}" \
  --member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin" \
  --quiet >/dev/null

log "ensuring BYOK envelope KMS key"
if ! gc kms keyrings describe "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keyrings create "$KMS_KEYRING_ID" --location "$REGION"
fi
if ! gc kms keys describe "$BYOK_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keys create "$BYOK_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" \
    --location "$REGION" \
    --purpose=encryption
fi
gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyEncrypter" \
  --quiet >/dev/null

# Google Ads click identifiers use a separate envelope key. The conversion
# worker can unwrap this key but never receives permission to unwrap BYOK keys.
if ! gc kms keys describe "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keys create "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" \
    --location "$REGION" \
    --purpose=encryption
fi
gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyEncrypter" \
  --quiet >/dev/null
if ! gc iam service-accounts describe \
  "$CONTROL_RUN_SERVICE_ACCOUNT" >/dev/null 2>&1; then
  echo "ERROR: control-plane service account ${CONTROL_RUN_SERVICE_ACCOUNT} is missing" >&2
  exit 1
fi
gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${CONTROL_RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyEncrypter" \
  --quiet >/dev/null

# Runtime-SA project-level role grants. These call projects.setIamPolicy
# and need roles/resourcemanager.projectIamAdmin on the caller, so they
# must run as a project Owner — not as tr-deploy@ (which secrets.sh and
# rollout.sh use). Idempotent: gcloud no-ops if the binding already exists.
log "ensuring runtime IAM for ${RUN_SERVICE_ACCOUNT}"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/secretmanager.secretAccessor"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/spanner.databaseUser"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/bigtable.user"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/aiplatform.user"

# Metadata-only Google Ads conversion worker. It can read the durable Spanner
# outbox and unwrap only the dedicated Google-click envelope key. It has no
# Bigtable, Secret Manager, provider-key, or BYOK-key decrypt permission.
GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID="${TR_GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID:-tr-google-data-manager}"
GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT="${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gc iam service-accounts describe \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
  gc iam service-accounts create "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID" \
    --display-name="TrustedRouter Google Data Manager" \
    --description="Uploads encrypted-click signup, activation, and purchase conversions to Google Ads" \
    --quiet
fi
ensure_project_role \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/spanner.databaseUser"
ensure_project_role \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/serviceusage.serviceUsageConsumer"
gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyDecrypter" \
  --quiet >/dev/null
gc iam service-accounts add-iam-policy-binding \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
  --member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null
gc iam service-accounts add-iam-policy-binding \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet >/dev/null
