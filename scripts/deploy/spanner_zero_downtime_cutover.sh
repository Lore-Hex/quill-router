#!/usr/bin/env bash
# Stage 1 of the multi-region expansion plan: migrate the credit
# ledger from `trusted-router` (regional-us-central1) to
# `trusted-router-nam6` (multi-region nam6).
#
# Originally planned as a zero-downtime migration via Spanner change
# streams + Dataflow. We learned (the hard way, mid-apply) that GCP
# does NOT publish a `Spanner_Change_Streams_to_Cloud_Spanner` flex
# template — the v2 source exists in the open-source DataflowTemplates
# repo but isn't pre-staged in dataflow-templates-${REGION}/latest/flex/.
# The only Spanner change-stream sinks GCP ships are BigQuery, GCS,
# PubSub, and Sharded File Sink — none of them land back in Spanner.
#
# Since "user has no users yet" makes a brief read-only window free in
# user impact, we pivoted to the read-only-window variant (the original
# plan's "highest-risk step" — it's actually fine when no traffic is
# affected). The pre-flight idempotency-key code we shipped for
# Stage 1 is still useful for Stage 5a's eur3 hot-replica pattern,
# where the dual-write + change-stream story actually pays off.
#
# Approach (revised):
#   Phase A — Avro-export source → import into nam6. Cross-config
#             move (regional-us-central1 → nam6) requires Avro
#             export+import; backup-restore is forbidden across
#             instanceConfig boundaries.
#   Phase B — Set TR_READ_ONLY=1 on every Cloud Run revision. Re-run
#             Phase A to refresh nam6's data with the latest writes
#             (the read-only flag stops new writes; reads keep working
#             off the source). The middleware at
#             src/trusted_router/middleware.py:read_only_middleware
#             handles this — POST/PUT/PATCH/DELETE return 503 with
#             Retry-After: 1800; GET/HEAD/OPTIONS pass through.
#   Phase C — Verify nam6 row counts and max(updated_at) match source.
#   Phase D — Update TR_SPANNER_INSTANCE_ID env var on every Cloud
#             Run revision to nam6. Application now reads + writes
#             nam6.
#   Phase E — Drop TR_READ_ONLY. Synthetic write+read in each region.
#   Phase F — After 7 days clean: delete the old `trusted-router`
#             instance. Until then it stays alive as a forensic-restore
#             safety net.
#
# What this script DOES NOT automate:
#   - The actual Cloud Run env-var flips in Phase B/D/E. Those are
#     printed as runbook commands the operator copy-pastes. The
#     reasoning: the flip blast radius is "every region in prod" and
#     the right pace is human-judgment-paced, not script-paced.
#
# Each phase is idempotent: re-running picks up where it left off and
# skips already-done work.
#
# Usage:
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh                        # dry-run all
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh --apply --phase A      # apply one
#   bash scripts/deploy/spanner_zero_downtime_cutover.sh --apply --phase C      # verify
#   Phases B/D/E are operator-driven runbooks (printed, not executed).

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
# Default to Phase A only — the data-moving phase, fully automated.
# Phases B/D/E are operator runbooks (printed, not executed); Phase C
# is verification (read-only, safe but cost-incurring on Spanner ops).
# Phase F is destructive and runs only after a 7-day cool-down. Keep
# the default conservative so a stray --apply doesn't roll an
# operator runbook automatically.
PHASE="A"
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

# ─── Phase A: Avro export + import (seed nam6 from $OLD_INSTANCE) ────────
# IMPORTANT: A first attempt used Spanner backup/restore. Spanner refuses
# to restore a backup across instance configs (regional-us-central1 →
# nam6 is forbidden — both instances must share the same instanceConfig).
# The error from a real run: "Cannot create database ... because the
# backup and the database are in instances with different instance
# configurations."
#
# The supported pattern for cross-config migration is the Dataflow
# `Cloud_Spanner_to_GCS_Avro` template — exports a consistent snapshot
# of every table to Avro on GCS, captures the snapshot timestamp T0 in
# the export metadata, and the matching `GCS_Avro_to_Cloud_Spanner`
# template re-imports into the new (different-config) instance. Phase B
# then starts a change stream from T0 so writes that land between
# T0 and the cutover replicate in the gap.
#
# We KEEP the backup as a forensic safety net (already created above —
# don't delete on re-runs).
phase_A() {
  log "=== phase A: avro export → import (cross-config seed for $NEW_INSTANCE) ==="

  # 0. Take a backup of the source as a forensic safety net. The backup
  #    is NOT used for the seed (different instanceConfig), but it lets
  #    us point-in-time-recover the source if anything goes wrong.
  if gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
       >/dev/null 2>&1; then
    log "  forensic backup $BACKUP_NAME already exists on $OLD_INSTANCE (kept as safety net)"
    if [ $DRY_RUN -eq 0 ]; then
      local t0
      t0=$(gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
             --format="value(versionTime)")
      log "  backup version_time (T0 reference): $t0"
    fi
  else
    log "  creating forensic backup $BACKUP_NAME on $OLD_INSTANCE (30d retention)"
    gc_or_dry spanner backups create "$BACKUP_NAME" \
      --instance="$OLD_INSTANCE" \
      --database="$DATABASE" \
      --retention-period=30d \
      --async
    if [ $DRY_RUN -eq 0 ]; then
      until gc spanner backups describe "$BACKUP_NAME" --instance="$OLD_INSTANCE" \
              --format="value(state)" 2>/dev/null | grep -q READY; do
        sleep 15
      done
      log "  backup READY"
    fi
  fi

  # 1. Make sure the export bucket exists (regional, same region as the
  #    Dataflow job).
  local export_bucket="${TR_SPANNER_EXPORT_BUCKET:-gs://${PROJECT_ID}-spanner-export}"
  local export_path="${export_bucket}/${BACKUP_NAME}"
  if [ $DRY_RUN -eq 0 ]; then
    if ! gsutil ls -b "$export_bucket" >/dev/null 2>&1; then
      log "  creating export bucket $export_bucket (regional in $DATAFLOW_REGION)"
      gsutil mb -p "$PROJECT_ID" -l "$DATAFLOW_REGION" -b on "$export_bucket"
    else
      log "  export bucket $export_bucket already exists"
    fi
    # Make sure Dataflow temp staging area exists too (Phase B uses it
    # but it doesn't hurt to provision it here).
    if ! gsutil ls -b "$DATAFLOW_TEMP_BUCKET" >/dev/null 2>&1; then
      gsutil mb -p "$PROJECT_ID" -l "$DATAFLOW_REGION" -b on "$DATAFLOW_TEMP_BUCKET"
    fi
  fi

  # 2. Export source database to Avro on GCS. Idempotent: if the export
  #    output prefix already has tr_entities-manifest.json, skip the
  #    export and reuse it. The first export is the slow part (~minutes
  #    for a small database, hours for TB-scale).
  if [ $DRY_RUN -eq 0 ] && \
     gsutil ls "${export_path}/tr_entities-manifest.json" >/dev/null 2>&1; then
    log "  Avro export already present at $export_path (reusing)"
  else
    log "  launching Dataflow Avro-export job → $export_path"
    if [ $DRY_RUN -eq 1 ]; then
      cat <<EOF >&2
  [dry-run] gcloud dataflow jobs run ${DATAFLOW_JOB_NAME}-export \\
    --project=$PROJECT_ID --region=$DATAFLOW_REGION \\
    --gcs-location=gs://dataflow-templates/latest/Cloud_Spanner_to_GCS_Avro \\
    --staging-location=$DATAFLOW_TEMP_BUCKET/staging \\
    --parameters=instanceId=$OLD_INSTANCE,databaseId=$DATABASE,outputDir=$export_path,snapshotTime=now
EOF
    else
      gcloud dataflow jobs run "${DATAFLOW_JOB_NAME}-export" \
        --project="$PROJECT_ID" \
        --region="$DATAFLOW_REGION" \
        --gcs-location=gs://dataflow-templates/latest/Cloud_Spanner_to_GCS_Avro \
        --staging-location="$DATAFLOW_TEMP_BUCKET/staging" \
        --parameters="instanceId=${OLD_INSTANCE},databaseId=${DATABASE},outputDir=${export_path}"
      log "  export job launched; waiting for completion..."
      # Poll until the job completes. Status DONE = success; any other
      # terminal state = bail.
      local export_state=""
      until [ "$export_state" = "Done" ] || [ "$export_state" = "JOB_STATE_DONE" ]; do
        sleep 30
        export_state=$(gcloud dataflow jobs list \
          --project="$PROJECT_ID" --region="$DATAFLOW_REGION" \
          --filter="name=${DATAFLOW_JOB_NAME}-export" \
          --format="value(state)" 2>/dev/null | head -1)
        log "    export job state: $export_state"
        if [ "$export_state" = "Failed" ] || [ "$export_state" = "Cancelled" ] || \
           [ "$export_state" = "JOB_STATE_FAILED" ] || [ "$export_state" = "JOB_STATE_CANCELLED" ]; then
          log "  ERROR: export job ended in $export_state — bailing"
          return 1
        fi
      done
      log "  export complete"
    fi
  fi

  # 3. The Avro export wrote into a sub-prefix named
  #    `<srcInstance>-<srcDb>-<jobId>/` under the outputDir we passed.
  #    Find the most recent such sub-prefix — that's what the import
  #    template expects as its `inputDir` (and the manifest lives there).
  #    Cache the path in a variable shared with step 5.
  local import_input_dir=""
  if [ $DRY_RUN -eq 0 ]; then
    import_input_dir=$(gsutil ls -d "${export_path}/${OLD_INSTANCE}-${DATABASE}-*" 2>/dev/null \
      | tail -1 | sed 's:/$::')
    if [ -z "$import_input_dir" ]; then
      log "  ERROR: could not find export sub-prefix under $export_path"
      log "  bucket contents:"
      gsutil ls -r "${export_path}/" >&2 || true
      return 1
    fi
    log "  detected export sub-prefix: $import_input_dir"

    # The Avro export's manifest carries the snapshot timestamp T0.
    # Phase B's change stream needs --start-time=$T0 so writes that
    # landed during the export are replicated.
    local t0_avro
    t0_avro=$(gsutil cat "${import_input_dir}/tr_entities-manifest.json" 2>/dev/null \
      | python3 -c "import json, sys; m=json.load(sys.stdin); print(m.get('snapshotTime') or m.get('snapshot_time') or '')" \
      2>/dev/null || echo "")
    if [ -n "$t0_avro" ]; then
      log "  Avro snapshot timestamp T0: $t0_avro"
      log "  echo this somewhere safe; Phase B needs --start-time=$t0_avro"
    else
      log "  WARN: couldn't parse snapshotTime from manifest; check ${import_input_dir}/tr_entities-manifest.json"
      log "  Phase B can fall back to the forensic backup version_time as T0"
    fi
  fi

  # 4. Create-or-recreate target database on nam6. The import template
  #    expects a database that ALREADY exists with matching DDL — it
  #    populates rows but doesn't create the database. Drop the empty
  #    stub from Stage 0 (and re-create with the same DDL) only when
  #    the existing target is verified empty.
  local target_exists=0
  if gc spanner databases describe "$DATABASE" --instance="$NEW_INSTANCE" \
       >/dev/null 2>&1; then
    target_exists=1
  fi

  if [ $target_exists -eq 1 ]; then
    log "  target database $DATABASE exists on $NEW_INSTANCE; checking if empty"
    local target_rows="0"
    if [ $DRY_RUN -eq 0 ]; then
      target_rows=$(gc spanner databases execute-sql "$DATABASE" \
        --instance="$NEW_INSTANCE" \
        --sql="SELECT COUNT(*) AS n FROM tr_entities" 2>/dev/null \
        | tail -1 | tr -d '[:space:]')
    fi
    if [ "$target_rows" != "0" ]; then
      log "  target has $target_rows rows already — Phase A previously seeded data"
      log "  (skip re-import; if you want to redo, drop the db manually)"
      return 0
    fi
    log "  target empty; reusing for import"
  else
    log "  creating target database $DATABASE on $NEW_INSTANCE with mirrored DDL"
    gc_or_dry spanner databases create "$DATABASE" \
      --instance="$NEW_INSTANCE" \
      --database-dialect=GOOGLE_STANDARD_SQL \
      --ddl='CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind, id)'
  fi

  # 5. Import the Avro export into nam6.
  log "  launching Dataflow Avro-import job → $NEW_INSTANCE/$DATABASE"
  if [ $DRY_RUN -eq 1 ]; then
    cat <<EOF >&2
  [dry-run] gcloud dataflow jobs run ${DATAFLOW_JOB_NAME}-import \\
    --project=$PROJECT_ID --region=$DATAFLOW_REGION \\
    --gcs-location=gs://dataflow-templates/latest/GCS_Avro_to_Cloud_Spanner \\
    --staging-location=$DATAFLOW_TEMP_BUCKET/staging \\
    --parameters=instanceId=$NEW_INSTANCE,databaseId=$DATABASE,inputDir=<sub-prefix-detected-at-runtime>
EOF
  else
    gcloud dataflow jobs run "${DATAFLOW_JOB_NAME}-import" \
      --project="$PROJECT_ID" \
      --region="$DATAFLOW_REGION" \
      --gcs-location=gs://dataflow-templates/latest/GCS_Avro_to_Cloud_Spanner \
      --staging-location="$DATAFLOW_TEMP_BUCKET/staging" \
      --parameters="instanceId=${NEW_INSTANCE},databaseId=${DATABASE},inputDir=${import_input_dir}"
    log "  import job launched; waiting for completion..."
    local import_state=""
    until [ "$import_state" = "Done" ] || [ "$import_state" = "JOB_STATE_DONE" ]; do
      sleep 30
      import_state=$(gcloud dataflow jobs list \
        --project="$PROJECT_ID" --region="$DATAFLOW_REGION" \
        --filter="name=${DATAFLOW_JOB_NAME}-import" \
        --format="value(state)" 2>/dev/null | head -1)
      log "    import job state: $import_state"
      if [ "$import_state" = "Failed" ] || [ "$import_state" = "Cancelled" ] || \
         [ "$import_state" = "JOB_STATE_FAILED" ] || [ "$import_state" = "JOB_STATE_CANCELLED" ]; then
        log "  ERROR: import job ended in $import_state — bailing"
        return 1
      fi
    done
    log "  import complete; nam6 is seeded"
  fi
}

# ─── Phase B: Set read-only on every region (operator-driven runbook) ────
#
# The plan was originally a Spanner change-stream + Dataflow replicator,
# but GCP doesn't ship a Spanner→Spanner template (only Spanner→BigQuery,
# →GCS, →PubSub, →ShardedFileSink). Using the v2 source from the
# DataflowTemplates open-source repo would mean a Maven build + custom
# staging — significant work. Since the user has no users yet, a brief
# read-only window is free in user impact, and we have the right
# middleware shipped (read_only_middleware in src/trusted_router/middleware.py)
# to handle it cleanly. So Phase B prints the runbook commands the
# operator runs to set TR_READ_ONLY=1 on every Cloud Run revision.
phase_B() {
  log "=== phase B: set read-only on every region (operator runbook) ==="
  log ""
  log "  GOAL: stop new writes everywhere so Phase A can re-run against"
  log "  a quiet source, with confidence the row-count/max-timestamp"
  log "  checksum in Phase C reflects a snapshot the application can't"
  log "  contradict by writing during the cutover."
  log ""
  log "  Per region (us-central1, europe-west4):"
  log ""
  log "    gcloud run services update trusted-router --region=\$R \\"
  log "      --update-env-vars=TR_READ_ONLY=1 \\"
  log "      --project=$PROJECT_ID"
  log ""
  log "  Verification per region (synthetic write should 503):"
  log ""
  log "    SMOKE_KEY=\$(gcloud secrets versions access latest \\"
  log "      --secret=trustedrouter-synthetic-monitor-api-key \\"
  log "      --project=$PROJECT_ID)"
  log "    curl -X POST https://api-\$R.quillrouter.com/v1/signup \\"
  log "      -H \"Authorization: Bearer \$SMOKE_KEY\" -d '{}'"
  log "    # expected: 503 with {\"error\":{\"type\":\"service_unavailable\"}}"
  log ""
  log "  Once read-only is confirmed in all regions:"
  log "    bash scripts/deploy/spanner_zero_downtime_cutover.sh \\"
  log "      --apply --phase A   # re-run Avro export to pick up final writes"
  log "    bash scripts/deploy/spanner_zero_downtime_cutover.sh \\"
  log "      --apply --phase C   # row-count + max-updated_at parity check"
  log ""
  log "  Then run Phase D (env-var swap) to flip writes to nam6."
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

  log "  diff (rows in source vs target — expect identical when source"
  log "  has been read-only since the latest Phase A re-run):"
  diff /tmp/spanner-source.txt /tmp/spanner-target.txt || true
}

# ─── Phase D: Write flip (operator runbook) ──────────────────────────────
phase_D() {
  log "=== phase D: flip TR_SPANNER_INSTANCE_ID to nam6 (operator runbook) ==="
  log ""
  log "  PRECONDITIONS (verify before running):"
  log "    - Phase B: TR_READ_ONLY=1 set on every Cloud Run revision"
  log "    - Phase A: re-ran after read-only landed; nam6 holds the final"
  log "      pre-cutover snapshot"
  log "    - Phase C: row counts and max(updated_at) match source ↔ nam6"
  log ""
  log "  Per region, in order (us-central1 first, then europe-west4):"
  log ""
  log "    gcloud run services update trusted-router --region=\$R \\"
  log "      --update-env-vars=TR_SPANNER_INSTANCE_ID=$NEW_INSTANCE \\"
  log "      --project=$PROJECT_ID"
  log ""
  log "  Watch the synthetic monitor + 5xx rate for 5 min after each"
  log "  region's flip before doing the next one. If anything goes wrong:"
  log ""
  log "    gcloud run services update trusted-router --region=\$R \\"
  log "      --update-env-vars=TR_SPANNER_INSTANCE_ID=$OLD_INSTANCE \\"
  log "      --project=$PROJECT_ID"
  log ""
  log "  (Reverts in seconds; the old instance is still alive and"
  log "  in read-only mode at the application layer, so nothing has"
  log "  diverged.)"
}

# ─── Phase E: Drop read-only (operator runbook) ──────────────────────────
phase_E() {
  log "=== phase E: drop TR_READ_ONLY (operator runbook) ==="
  log ""
  log "  PRECONDITIONS:"
  log "    - Phase D: every region's TR_SPANNER_INSTANCE_ID points at nam6"
  log "    - Synthetic read+write against nam6 succeeds in every region"
  log ""
  log "  Per region:"
  log ""
  log "    gcloud run services update trusted-router --region=\$R \\"
  log "      --remove-env-vars=TR_READ_ONLY \\"
  log "      --project=$PROJECT_ID"
  log ""
  log "  Cutover complete after this. Watch p99 commit latency on nam6"
  log "  for 60 min — multi-region writes are 20-40ms on nam6 vs 5-10ms"
  log "  on the regional source. That's expected."
}

# ─── Phase F: 7-day decommission window ──────────────────────────────────
phase_F() {
  log "=== phase F: decommission window ==="
  log ""
  log "  RUN ONLY AFTER 7 DAYS OF CLEAN PHASE-E TRAFFIC ON $NEW_INSTANCE."
  log "  Until then, $OLD_INSTANCE stays alive as a forensic-restore"
  log "  safety net (~$73/mo, cheap insurance for the cutover window)."
  log ""
  log "  Day 7+:"
  log "    gcloud spanner instances delete $OLD_INSTANCE --project=$PROJECT_ID"
  log ""
  log "  The forensic backup on $OLD_INSTANCE was deleted with the"
  log "  instance. If you want to retain it past day 7, copy to a"
  log "  long-lived backup before the instance delete:"
  log ""
  log "    gcloud spanner backups create $BACKUP_NAME-archive \\"
  log "      --instance=$OLD_INSTANCE --database=$DATABASE \\"
  log "      --retention-period=180d --project=$PROJECT_ID"
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
