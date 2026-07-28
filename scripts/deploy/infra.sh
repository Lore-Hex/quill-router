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
  spanner.googleapis.com \
  bigtableadmin.googleapis.com \
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

# Runtime-SA project-level role grants. These call projects.setIamPolicy
# and need roles/resourcemanager.projectIamAdmin on the caller, so they
# must run as a project Owner — not as tr-deploy@ (which secrets.sh and
# rollout.sh use). Idempotent: gcloud no-ops if the binding already exists.
log "ensuring runtime IAM for ${RUN_SERVICE_ACCOUNT}"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/secretmanager.secretAccessor"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/spanner.databaseUser"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/bigtable.user"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/aiplatform.user"
