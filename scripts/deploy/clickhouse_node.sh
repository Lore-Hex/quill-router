#!/usr/bin/env bash
# Provision the analytics ClickHouse node.
#
# Stage 0 of docs/storage-portability/analytics-ingestion.md: a single
# internal-only node. Deliberately small — today's volume is ~280 rows/hour and
# the row rate at scale is unknown within 450x, so sizing now would be guessing.
# Stage 3 revisits this with measured numbers.
#
# NETWORKING: no public IP. Under the Bigtable-replay ingestion design only the
# ingester talks to ClickHouse, and the ingester runs on this same host, so
# nothing needs to reach it from outside the VPC. That is a security property
# worth keeping: do not add an external IP to "make testing easier" — use
# `gcloud compute ssh --tunnel-through-iap` instead.
#
# Idempotent: re-running skips resources that already exist.
set -euo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ZONE="${ZONE:-us-central1-a}"          # colocated with Bigtable/Spanner to keep the ingest scan local
NAME="${NAME:-tr-clickhouse-1}"
MACHINE="${MACHINE:-e2-standard-4}"    # 4 vCPU / 16 GB
DISK_GB="${DISK_GB:-200}"
DISK_TYPE="${DISK_TYPE:-pd-ssd}"
SECRET="${SECRET:-trustedrouter-clickhouse-password}"

log() { printf '\n=== %s\n' "$*"; }

log "project=$PROJECT zone=$ZONE name=$NAME machine=$MACHINE disk=${DISK_GB}GB"

# ---------------------------------------------------------------- password
if gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  log "secret $SECRET already exists — reusing"
else
  log "creating secret $SECRET"
  # Generated here and never echoed; the VM reads it from Secret Manager at boot.
  python3 -c "import secrets;print(secrets.token_urlsafe(32),end='')" \
    | gcloud secrets create "$SECRET" --project "$PROJECT" --data-file=- --replication-policy=automatic
fi

# ---------------------------------------------------------------- firewall
# Internal-only: the VPC's own ranges may reach the native + HTTP ports. No
# 0.0.0.0/0 rule anywhere in this script, by design.
if gcloud compute firewall-rules describe tr-clickhouse-internal --project "$PROJECT" >/dev/null 2>&1; then
  log "firewall rule exists"
else
  log "creating internal-only firewall rule"
  gcloud compute firewall-rules create tr-clickhouse-internal \
    --project "$PROJECT" --network default \
    --allow tcp:8123,tcp:9000 \
    --source-ranges 10.128.0.0/9 \
    --target-tags tr-clickhouse \
    --description "ClickHouse HTTP+native, VPC-internal only"
fi

# ---------------------------------------------------------------- startup
# The startup script lives in its own file rather than a heredoc inside $( ):
# bash parses quotes inside command substitution, so a single apostrophe in a
# comment there silently breaks the whole script (it did).
STARTUP_FILE="$(dirname "$0")/clickhouse_startup.sh"

if gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  log "instance $NAME already exists — skipping create"
else
  log "creating instance (no external IP)"
  PASSWORD=$(gcloud secrets versions access latest --secret "$SECRET" --project "$PROJECT")
  gcloud compute instances create "$NAME" \
    --project "$PROJECT" --zone "$ZONE" \
    --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size "${DISK_GB}GB" --boot-disk-type "$DISK_TYPE" \
    --tags tr-clickhouse \
    --no-address \
    --scopes https://www.googleapis.com/auth/cloud-platform \
    --metadata-from-file startup-script="$STARTUP_FILE" \
    --metadata ch-password="$PASSWORD"
fi

log "done. tail provisioning with:"
echo "  gcloud compute ssh $NAME --zone $ZONE --project $PROJECT --tunnel-through-iap --command 'sudo tail -f /var/log/tr-clickhouse-startup.log'"
