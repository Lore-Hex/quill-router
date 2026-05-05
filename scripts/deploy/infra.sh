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
    --config="regional-${REGION}" \
    --description="TrustedRouter ledger" \
    --processing-units=100
fi
if ! gc spanner databases describe "$SPANNER_DATABASE_ID" --instance="$SPANNER_INSTANCE_ID" >/dev/null 2>&1; then
  gc spanner databases create "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --database-dialect=GOOGLE_STANDARD_SQL \
    --ddl='CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind, id)'
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
