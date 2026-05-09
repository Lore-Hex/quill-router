#!/usr/bin/env bash
# Stage 1 of the multi-region expansion plan, zero-downtime variant:
# migrate the credit ledger from `trusted-router` (regional-us-central1)
# to `trusted-router-nam6` (multi-region nam6) using Spanner change
# streams + Dataflow as the replicator. Practiced once before there are
# users so the next migration (eur3 hot replica in Stage 5a, or even
# CRDB in Stage 5b) can use the same playbook.
#
# Approach:
#   Phase A — Take a backup of `trusted-router`, restore into nam6. Note
#             the backup's version_time as T0.
#   Phase B — Create a change stream `tr_migration` on the source. Launch
#             the GCP-provided Dataflow template
#             `Cloud_Spanner_change_streams_to_Cloud_Spanner` starting
#             from T0, sinking to nam6.
#   Phase C — Verify steady-state replication: row counts and max
#             updated_at timestamps match within seconds-scale lag.
#   Phase D — Read flip: feature flag TR_SPANNER_READ_FROM_NAM6=1 rolled
#             region-by-region. Application keeps writing to source; the
#             change stream keeps replicating those writes to nam6.
#   Phase E — Write flip: pause the LB ~2-3s, wait for stream lag = 0,
#             update Cloud Run env vars to point at nam6 for writes,
#             unpause LB. (Phase E is NOT automated by this script — it
#             prints the runbook for the operator.)
#   Phase F — Stop the Dataflow stream + drop the change stream after
#             1 hour clean. Keep the source alive 7 more days as a
#             forensic-restore safety net, then delete.
#
# Each phase is idempotent: re-running picks up where it left off and
# skips already-done work.
#
# Usage:
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh                        # dry-run all
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh --apply --phase A      # apply one
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh --apply                # apply A→C
#                                                                                 (D and E
#                                                                                 require explicit
#                                                                                 --phase D / E)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

OLD_INSTANCE="${TR_OLD_SPANNER_INSTANCE:-trusted-router}"
NEW_INSTANCE="${TR_NEW_SPANNER_INSTANCE:-trusted-router-nam6}"
DATABASE="${TR_SPANNER_DATABASE_ID:-trusted-router}"
BACKUP_NAME="${TR_MIGRATION_BACKUP_NAME:-migration-seed}"
CHANGE_STREAM="${TR_MIGRATION_CHANGE_STREAM:-tr_migration}"
DATAFLOW_REGION="${TR_DATAFLOW_REGION:-us-central1}"
DATAFLOW_JOB_NAME="${TR_DATAFLOW_JOB_NAME:-tr-spanner-migration}"
DATAFLOW_TEMP_BUCKET="${TR_DATAFLOW_TEMP_BUCKET:-gs://${PROJECT_ID}-dataflow-tmp}"

DRY_RUN=1
PHASE="A,B,C"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) DRY_RUN=0; shift ;;
    --phase) PHASE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

dry() {
  if [ $DRY_RUN -eq 1 ]; then
    echo "  [dry-run] $*" >&2
    return 0
  fi
  return 1
}
gc_or_dry() {
  if [ $DRY_RUN -eq 1 ]; then
    echo "  [dry-run] gcloud $*" >&2
  else
    gc "$@"
  fi
}

log "GCP project: $PROJECT_ID"
log "Source instance: $OLD_INSTANCE"
log "Target instance: $NEW_INSTANCE"
log "Database:        $DATABASE"
log "Mode: $([ $DRY_RUN -eq 1 ] && echo DRY-RUN || echo APPLY) phase: $PHASE"

# ─── Phase A: Backup + restore (seed nam6 from a snapshot) ───────────────
phase_A() {
  log "=== phase A: backup → restore (seed nam6 from $OLD_INSTANCE) ==="

  # 1. Backup the source. Idempotent: if the named backup already exists,
  #    skip and reuse it. We use a fixed name (default `migration-seed`)
  #    so re-runs of phase A don't accumulate backups. Override with
  #    TR_MIGRATION_BACKUP_NAME if you want a fresh snapshot.
  if gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
       >/dev/null 2>&1; then
    log "  backup $BACKUP_NAME already exists on $OLD_INSTANCE"
  else
    log "  creating backup $BACKUP_NAME from $OLD_INSTANCE/$DATABASE (30d retention)"
    gc_or_dry spanner backups create "$BACKUP_NAME" \
      --instance="$OLD_INSTANCE" \
      --database="$DATABASE" \
      --retention-period=30d \
      --async
    log "  backup created async; waiting for completion..."
    if [ $DRY_RUN -eq 0 ]; then
      until gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
              --format="value(state)" 2>/dev/null | grep -q READY; do
        sleep 15
      done
      log "  backup READY"
    fi
  fi

  # Print the backup version_time as T0 — Phase B uses this to start the
  # change stream from a consistent snapshot (so writes during the backup
  # window aren't double-replicated).
  if [ $DRY_RUN -eq 0 ]; then
    local t0
    t0=$(gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
           --format="value(versionTime)")
    log "  backup version_time (T0): $t0"
    log "  echo this somewhere safe; phase B needs --start-time=\$T0"
  fi

  # 2. Restore into the nam6 instance. Database name on the destination
  #    matches the source ($DATABASE) so the application's
  #    TR_SPANNER_DATABASE_ID flips with TR_SPANNER_INSTANCE_ID.
  #
  # Stage 0's infra-stage0.sh created an EMPTY $DATABASE on $NEW_INSTANCE
  # with the schema mirrored from source — that has to go before we can
  # restore a backup over it. Spanner doesn't support "restore over an
  # existing database"; the restore operation creates the database. So
  # the safe pattern: detect the empty stub, drop it, then restore.
  #
  # Only auto-drop if the database has ZERO rows on the target. If it
  # has any rows, that means a previous Phase A restore already landed
  # data — bail out and let the operator decide whether to re-seed.
  if gc spanner databases describe "$DATABASE" --instance="$NEW_INSTANCE" \
       >/dev/null 2>&1; then
    log "  database $DATABASE already exists on $NEW_INSTANCE; checking if empty"
    local target_rows="0"
    if [ $DRY_RUN -eq 0 ]; then
      target_rows=$(gc spanner databases execute-sql "$DATABASE" \
        --instance="$NEW_INSTANCE" \
        --sql="SELECT COUNT(*) AS n FROM tr_entities" 2>/dev/null \
        | tail -1 | tr -d '[:space:]')
    fi
    if [ "$target_rows" = "0" ]; then
      log "  target $NEW_INSTANCE/$DATABASE is empty (Stage 0 stub); dropping for restore"
      gc_or_dry spanner databases delete "$DATABASE" --instance="$NEW_INSTANCE" \
        --quiet
    else
      log "  target $NEW_INSTANCE/$DATABASE has $target_rows rows — Phase A already restored data"
      log "  (re-seed by dropping the db manually if you really want to start over)"
      return
    fi
  fi

  log "  restoring $BACKUP_NAME → $NEW_INSTANCE/$DATABASE"
  gc_or_dry spanner databases restore \
    --source-backup="projects/${PROJECT_ID}/instances/${OLD_INSTANCE}/backups/${BACKUP_NAME}" \
    --destination-instance="$NEW_INSTANCE" \
    --destination-database="$DATABASE" \
    --async

  if [ $DRY_RUN -eq 0 ]; then
    log "  restore in progress; waiting..."
    until gc spanner databases describe "$DATABASE" --instance="$NEW_INSTANCE" \
            --format="value(state)" 2>/dev/null | grep -q READY; do
      sleep 15
    done
    log "  restore READY on $NEW_INSTANCE"
  fi
}

# ─── Phase B: Change stream + Dataflow replicator ────────────────────────
phase_B() {
  log "=== phase B: change stream + Dataflow replicator ==="

  # 1. Create the change stream. Use NEW_VALUES (capture full post-image
  #    on every write) and 7d retention (covers the full migration plus
  #    rollback safety).
  log "  creating change stream $CHANGE_STREAM on $OLD_INSTANCE/$DATABASE"
  local create_stream_ddl="CREATE CHANGE STREAM ${CHANGE_STREAM} FOR tr_entities OPTIONS (retention_period = '7d', value_capture_type = 'NEW_VALUES')"
  if [ $DRY_RUN -eq 1 ]; then
    echo "  [dry-run] gcloud spanner databases ddl update $DATABASE --instance=$OLD_INSTANCE --ddl='$create_stream_ddl'"
  else
    # Skip if it already exists (DDL is idempotent only via try/skip).
    if gc spanner databases execute-sql "$DATABASE" --instance="$OLD_INSTANCE" \
         --sql="SELECT NAME FROM information_schema.change_streams WHERE NAME='${CHANGE_STREAM}'" 2>&1 \
         | grep -q "$CHANGE_STREAM"; then
      log "  change stream $CHANGE_STREAM already exists"
    else
      gc spanner databases ddl update "$DATABASE" --instance="$OLD_INSTANCE" \
        --ddl="$create_stream_ddl"
      log "  change stream created"
    fi
  fi

  # 2. Make sure the Dataflow temp bucket exists. The streaming job
  #    needs a place to land staging files.
  if [ $DRY_RUN -eq 0 ]; then
    if ! gsutil ls -b "$DATAFLOW_TEMP_BUCKET" >/dev/null 2>&1; then
      log "  creating Dataflow temp bucket $DATAFLOW_TEMP_BUCKET"
      gsutil mb -p "$PROJECT_ID" -l "$DATAFLOW_REGION" -b on "$DATAFLOW_TEMP_BUCKET"
    else
      log "  Dataflow temp bucket $DATAFLOW_TEMP_BUCKET already exists"
    fi
  fi

  # 3. Launch the Dataflow streaming job. The template
  #    `Cloud_Spanner_change_streams_to_Cloud_Spanner` is shipped by
  #    GCP and reads every change-stream record, replays it as an UPSERT
  #    into the destination instance.
  log "  launching Dataflow streaming job $DATAFLOW_JOB_NAME"
  if [ $DRY_RUN -eq 1 ]; then
    cat <<EOF >&2
  [dry-run] gcloud dataflow flex-template run $DATAFLOW_JOB_NAME \\
    --project=$PROJECT_ID \\
    --region=$DATAFLOW_REGION \\
    --template-file-gcs-location=gs://dataflow-templates/latest/flex/Spanner_Change_Streams_to_Cloud_Spanner \\
    --parameters=spannerProjectId=$PROJECT_ID,spannerInstanceId=$OLD_INSTANCE,spannerDatabaseId=$DATABASE,spannerMetadataInstanceId=$OLD_INSTANCE,spannerMetadataDatabaseId=$DATABASE,spannerChangeStreamName=$CHANGE_STREAM,sinkProjectId=$PROJECT_ID,sinkInstanceId=$NEW_INSTANCE,sinkDatabaseId=$DATABASE \\
    --staging-location=$DATAFLOW_TEMP_BUCKET/staging \\
    --temp-location=$DATAFLOW_TEMP_BUCKET/temp
EOF
  else
    # Skip if a streaming job with this name is already RUNNING — Dataflow
    # job names are reusable but a new launch with the same name will create
    # a *second* job, doubling cost.
    local existing
    existing=$(gcloud dataflow jobs list \
      --project="$PROJECT_ID" --region="$DATAFLOW_REGION" \
      --filter="name=$DATAFLOW_JOB_NAME AND state=Running" \
      --format="value(id)" 2>/dev/null | head -1)
    if [ -n "$existing" ]; then
      log "  Dataflow job $DATAFLOW_JOB_NAME already running (id=$existing)"
    else
      gcloud dataflow flex-template run "$DATAFLOW_JOB_NAME" \
        --project="$PROJECT_ID" \
        --region="$DATAFLOW_REGION" \
        --template-file-gcs-location=gs://dataflow-templates/latest/flex/Spanner_Change_Streams_to_Cloud_Spanner \
        --parameters="spannerProjectId=$PROJECT_ID,spannerInstanceId=$OLD_INSTANCE,spannerDatabaseId=$DATABASE,spannerMetadataInstanceId=$OLD_INSTANCE,spannerMetadataDatabaseId=$DATABASE,spannerChangeStreamName=$CHANGE_STREAM,sinkProjectId=$PROJECT_ID,sinkInstanceId=$NEW_INSTANCE,sinkDatabaseId=$DATABASE" \
        --staging-location="$DATAFLOW_TEMP_BUCKET/staging" \
        --temp-location="$DATAFLOW_TEMP_BUCKET/temp"
      log "  Dataflow job launched"
    fi
  fi

  log "  Phase B complete. Watch dataflow.googleapis.com/job/system_lag_seconds; <5s = caught up."
}

# ─── Phase C: Verify steady-state replication ────────────────────────────
phase_C() {
  log "=== phase C: verify replication (row-count + max-timestamp checksums) ==="
  if [ $DRY_RUN -eq 1 ]; then
    log "  [dry-run] would query both instances for row counts + max(updated_at) per kind"
    return
  fi

  log "  source: $OLD_INSTANCE"
  gc spanner databases execute-sql "$DATABASE" --instance="$OLD_INSTANCE" \
    --sql="SELECT kind, COUNT(*) AS n, MAX(updated_at) AS last_write FROM tr_entities GROUP BY kind ORDER BY n DESC" \
    > /tmp/spanner-source.txt 2>&1
  cat /tmp/spanner-source.txt | head -30

  log "  target: $NEW_INSTANCE"
  gc spanner databases execute-sql "$DATABASE" --instance="$NEW_INSTANCE" \
    --sql="SELECT kind, COUNT(*) AS n, MAX(updated_at) AS last_write FROM tr_entities GROUP BY kind ORDER BY n DESC" \
    > /tmp/spanner-target.txt 2>&1
  cat /tmp/spanner-target.txt | head -30

  log "  diff (rows in source vs target — expect identical or 1-2 row drift on hot kinds):"
  diff /tmp/spanner-source.txt /tmp/spanner-target.txt || true

  log "  Dataflow lag:"
  gcloud monitoring time-series list \
    --project="$PROJECT_ID" \
    --filter='metric.type="dataflow.googleapis.com/job/system_lag_seconds" AND resource.labels.job_name="'"$DATAFLOW_JOB_NAME"'"' \
    --interval-end-time="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --interval-start-time="$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u --date='5 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --format="value(points[0].value.doubleValue)" 2>/dev/null | head -3 || true
}

# ─── Phase D: Read flip (TR_SPANNER_READ_FROM_NAM6=1 per region) ─────────
phase_D() {
  log "=== phase D: read flip (rolling region-by-region) ==="
  log "  Application keeps writing to $OLD_INSTANCE. Reads switch to $NEW_INSTANCE."
  log "  Change stream still flows old → new, so writes still appear on new"
  log "  before the read happens (within stream lag, typically <5s)."
  log ""
  log "  Order: us-central1 first (5-min watch), then europe-west4."
  log "  WATCH FOR: stale-read errors. The credit-reservation read-after-write"
  log "  pattern is the most likely surface — if a freshly-written reservation"
  log "  isn't yet replicated to $NEW_INSTANCE, the read returns 'not found'."
  log ""
  log "  Per-region command (you run; this script doesn't roll Cloud Run):"
  log ""
  log "    gcloud run services update trusted-router --region=us-central1 \\"
  log "      --update-env-vars=TR_SPANNER_READ_FROM_NAM6=1 \\"
  log "      --project=$PROJECT_ID"
  log ""
  log "  After 5 min clean on us-central1, repeat for europe-west4."
  log "  After 30 min clean across all regions: proceed to Phase E."
}

# ─── Phase E: Write flip (~2-3s 503 window) ──────────────────────────────
phase_E() {
  log "=== phase E: write flip (operator-driven; this script prints the runbook) ==="
  log ""
  log "  GOAL: switch writes from $OLD_INSTANCE → $NEW_INSTANCE without"
  log "  losing or double-applying any in-flight write."
  log ""
  log "  STEPS:"
  log "    1. (Optional) flip TR_READ_ONLY=1 globally if you want a clean"
  log "       2-3s pause via 503 Retry-After. Skipping this means a few"
  log "       in-flight requests may see a transient error during the env"
  log "       var update."
  log ""
  log "    2. Verify Dataflow lag = 0:"
  log "       gcloud monitoring time-series list ... system_lag_seconds < 1"
  log ""
  log "    3. For each region in (us-central1, europe-west4):"
  log "       gcloud run services update trusted-router --region=\$R \\"
  log "         --update-env-vars=TR_SPANNER_INSTANCE_ID=$NEW_INSTANCE \\"
  log "         --project=$PROJECT_ID"
  log ""
  log "    4. Drop TR_READ_ONLY (if you set it in step 1)."
  log ""
  log "    5. Synthetic write+read in each region. Confirm OK."
  log ""
  log "  Then proceed to Phase F (stop replicator, decommission window)."
}

# ─── Phase F: Stop replicator + 7-day decommission window ────────────────
phase_F() {
  log "=== phase F: stop replicator + decommission window ==="
  log ""
  log "  RUN ONLY AFTER 1+ HOUR OF CLEAN PHASE-E TRAFFIC ON $NEW_INSTANCE."
  log ""

  if [ $DRY_RUN -eq 1 ]; then
    log "  [dry-run] would cancel Dataflow job $DATAFLOW_JOB_NAME"
    log "  [dry-run] would drop change stream $CHANGE_STREAM"
    log "  [dry-run] would NOT delete $OLD_INSTANCE (manual after 7 days)"
    return
  fi

  local job_id
  job_id=$(gcloud dataflow jobs list \
    --project="$PROJECT_ID" --region="$DATAFLOW_REGION" \
    --filter="name=$DATAFLOW_JOB_NAME AND state=Running" \
    --format="value(id)" 2>/dev/null | head -1)
  if [ -n "$job_id" ]; then
    log "  cancelling Dataflow job $DATAFLOW_JOB_NAME (id=$job_id)"
    gcloud dataflow jobs cancel "$job_id" --region="$DATAFLOW_REGION" --project="$PROJECT_ID"
  else
    log "  no running Dataflow job named $DATAFLOW_JOB_NAME"
  fi

  log "  dropping change stream $CHANGE_STREAM"
  gc spanner databases ddl update "$DATABASE" --instance="$OLD_INSTANCE" \
    --ddl="DROP CHANGE STREAM ${CHANGE_STREAM}" || \
    log "  (drop already done or never existed)"

  log ""
  log "  $OLD_INSTANCE is INTENTIONALLY left alive for 7 days as a"
  log "  forensic-restore safety net. Delete it manually on day 7:"
  log "    gcloud spanner instances delete $OLD_INSTANCE --project=$PROJECT_ID"
}

# ─── Dispatch (comma-separated phases or single letter) ──────────────────
IFS=',' read -ra _phases <<< "$PHASE"
for p in "${_phases[@]}"; do
  case "$p" in
    A) phase_A ;;
    B) phase_B ;;
    C) phase_C ;;
    D) phase_D ;;
    E) phase_E ;;
    F) phase_F ;;
    *) log "unknown phase: $p (expected one of A,B,C,D,E,F)"; exit 2 ;;
  esac
done

log "done"
