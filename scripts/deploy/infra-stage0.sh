#!/usr/bin/env bash
# Stage 0 of the multi-region expansion plan: provision the new GCP
# infrastructure ALONGSIDE the existing prod infra without touching
# what's currently serving traffic.
#
# What this script provisions, idempotently, in quill-cloud-proxy:
#
#   nam6         Spanner instance `trusted-router-nam6` (config=nam6,
#                100 PUs). Replicas in us-central1 + us-east1 + us-east4.
#                Schema mirrors `trusted-router` (current regional-us-central1).
#                ~$219/mo when up. Existing trusted-router stays untouched
#                until Stage 1's cutover.
#
#   bt-hdd       Bigtable instance `trusted-router-logs-v2` with 3 HDD
#                clusters (us-central1-a + europe-west4-a + us-east4-a).
#                ~$372/mo total when up (3 × $124 HDD nodes). Existing
#                trusted-router-logs (SSD, 2 clusters) keeps serving
#                until Stage 1.
#
#   tr-multi     App profile on trusted-router-logs-v2 with
#                multi-cluster-routing-use-any (the 99.999% pattern).
#                Default profile stays single-cluster as a safety net.
#
#   us-east4     Cloud Run service in us-east4 redeployed with current
#                image. Already exists from a prior deploy but isn't in
#                the LB backend service yet — that join happens in
#                Stage 3. min-instances=1 (warm) so failover-to-east is
#                fast when it's wired up.
#
# Each phase is idempotent: re-running picks up where it left off and
# skips already-provisioned resources. Total run time on a fresh apply
# is ~30 minutes (Spanner + BT instance provisioning is the slow step).
#
# Usage:
#   bash scripts/deploy/infra-stage0.sh                              # dry-run all
#   bash scripts/deploy/infra-stage0.sh --apply                      # apply all
#   bash scripts/deploy/infra-stage0.sh --apply --phase nam6         # apply one
#
# Run order (dependencies):
#   nam6  →  (independent)
#   bt-hdd  →  tr-multi  (app profile needs the instance)
#   us-east4  →  (independent)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NEW_SPANNER_INSTANCE="${TR_NEW_SPANNER_INSTANCE:-trusted-router-nam6}"
NEW_SPANNER_DATABASE="${TR_NEW_SPANNER_DATABASE:-trusted-router}"
NEW_BT_INSTANCE="${TR_NEW_BT_INSTANCE:-trusted-router-logs-v2}"
NEW_BT_APP_PROFILE="${TR_NEW_BT_APP_PROFILE:-tr-multi}"
NEW_BT_GENERATION_TABLE="${TR_NEW_BT_GENERATION_TABLE:-trustedrouter-generations}"
US_EAST4_REGION="us-east4"

DRY_RUN=1
PHASE="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) DRY_RUN=0; shift ;;
    --phase) PHASE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

dry_log() {
  if [ $DRY_RUN -eq 1 ]; then
    echo "  [dry-run] $*" >&2
  fi
}
gc_or_dry() {
  if [ $DRY_RUN -eq 1 ]; then
    echo "  [dry-run] gcloud $*" >&2
  else
    gc "$@"
  fi
}

log "GCP project: $PROJECT_ID"
log "Mode: $([ $DRY_RUN -eq 1 ] && echo DRY-RUN || echo APPLY) phase: $PHASE"

# ─── Phase: Spanner nam6 ───────────────────────────────────────────────────
phase_nam6() {
  log "=== phase: nam6 ==="
  if gc spanner instances describe "$NEW_SPANNER_INSTANCE" >/dev/null 2>&1; then
    log "  Spanner instance $NEW_SPANNER_INSTANCE already exists"
  else
    log "  creating Spanner instance $NEW_SPANNER_INSTANCE (config=nam6, 100 PUs)"
    # `--description` actually sets display_name in the GCP API and is
    # capped at 30 characters. Keep it short.
    gc_or_dry spanner instances create "$NEW_SPANNER_INSTANCE" \
      --config=nam6 \
      --description="TrustedRouter (nam6)" \
      --processing-units=100
  fi

  if gc spanner databases describe "$NEW_SPANNER_DATABASE" \
       --instance="$NEW_SPANNER_INSTANCE" >/dev/null 2>&1; then
    log "  Spanner database $NEW_SPANNER_DATABASE already exists on $NEW_SPANNER_INSTANCE"
  else
    log "  creating database $NEW_SPANNER_DATABASE on $NEW_SPANNER_INSTANCE (mirrored DDL)"
    gc_or_dry spanner databases create "$NEW_SPANNER_DATABASE" \
      --instance="$NEW_SPANNER_INSTANCE" \
      --database-dialect=GOOGLE_STANDARD_SQL \
      --ddl='CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind, id)'
  fi

  log "  nam6 ready. Stage 1 cutover will populate this instance from a"
  log "  regional-us-central1 backup + flip TR_SPANNER_INSTANCE_ID."
}

# ─── Phase: Bigtable HDD instance with 3 clusters ─────────────────────────
phase_bt_hdd() {
  log "=== phase: bt-hdd ==="
  # GCP Bigtable does NOT allow mixed SSD+HDD clusters in the same
  # instance. We provision a brand-new instance with 3 HDD clusters
  # and migrate during Stage 1's coordinated cutover. The existing
  # trusted-router-logs (SSD, 2 clusters) stays serving prod until
  # then.
  if gc bigtable instances describe "$NEW_BT_INSTANCE" >/dev/null 2>&1; then
    log "  Bigtable instance $NEW_BT_INSTANCE already exists"
  else
    log "  creating Bigtable instance $NEW_BT_INSTANCE with first cluster (us-central1-a, HDD)"
    # Keep display-name short to match Spanner's 30-char limit
    # convention (Bigtable's is more lenient but consistency helps).
    gc_or_dry bigtable instances create "$NEW_BT_INSTANCE" \
      --display-name="TR logs (HDD multi)" \
      --instance-type=PRODUCTION \
      --cluster-config="id=${NEW_BT_INSTANCE}-c1,zone=us-central1-a,nodes=1" \
      --cluster-storage-type=HDD
  fi

  # Add the other 2 clusters separately. Adding a cluster to an
  # existing instance is the standard way; replication enables
  # automatically between all clusters in an instance.
  for cluster_zone in "${NEW_BT_INSTANCE}-c2:europe-west4-a" "${NEW_BT_INSTANCE}-c3:us-east4-a"; do
    cluster_id="${cluster_zone%%:*}"
    zone="${cluster_zone##*:}"
    if gc bigtable clusters describe "$cluster_id" --instance="$NEW_BT_INSTANCE" \
         >/dev/null 2>&1; then
      log "  cluster $cluster_id already exists"
    else
      log "  creating cluster $cluster_id in $zone (HDD)"
      gc_or_dry bigtable clusters create "$cluster_id" \
        --instance="$NEW_BT_INSTANCE" \
        --zone="$zone" \
        --num-nodes=1
      # Note: --storage-type isn't accepted on cluster-create when
      # adding to an existing instance; the new cluster inherits the
      # instance's storage type (HDD set when c1 was created).
    fi
  done

  # Mirror the generation table schema from the SSD instance.
  if gc bigtable instances tables describe "$NEW_BT_GENERATION_TABLE" \
       --instance="$NEW_BT_INSTANCE" >/dev/null 2>&1; then
    log "  table $NEW_BT_GENERATION_TABLE already exists on $NEW_BT_INSTANCE"
  else
    log "  creating table $NEW_BT_GENERATION_TABLE (column family m)"
    gc_or_dry bigtable instances tables create "$NEW_BT_GENERATION_TABLE" \
      --instance="$NEW_BT_INSTANCE" \
      --column-families=m
  fi
}

# ─── Phase: tr-multi app profile ──────────────────────────────────────────
phase_tr_multi() {
  log "=== phase: tr-multi ==="
  # Multi-cluster-routing-use-any is the 99.999% topology per GCP
  # docs. Reads/writes go to the closest healthy cluster of the 3.
  # The default profile keeps single-cluster routing so a rollback
  # is instant (just unset TR_BIGTABLE_APP_PROFILE_ID).
  if gc bigtable app-profiles describe "$NEW_BT_APP_PROFILE" \
       --instance="$NEW_BT_INSTANCE" >/dev/null 2>&1; then
    log "  app profile $NEW_BT_APP_PROFILE already exists on $NEW_BT_INSTANCE"
  else
    log "  creating app profile $NEW_BT_APP_PROFILE on $NEW_BT_INSTANCE"
    gc_or_dry bigtable app-profiles create "$NEW_BT_APP_PROFILE" \
      --instance="$NEW_BT_INSTANCE" \
      --description="3-cluster multi-region routing (us-central1+europe-west4+us-east4)" \
      --route-any
  fi
}

# ─── Phase: us-east4 Cloud Run ────────────────────────────────────────────
phase_us_east4() {
  log "=== phase: us-east4 ==="
  # The Cloud Run service in us-east4 already exists from a prior
  # rollout; this phase just ensures it's running the current image
  # with min-instances=1 and is NOT yet attached to the LB backend
  # (that's Stage 3 work).
  if gc run services describe trusted-router --region="$US_EAST4_REGION" \
       >/dev/null 2>&1; then
    log "  Cloud Run service already exists in $US_EAST4_REGION"
    log "  (re-deploys happen via the standard rollout.sh flow; this script"
    log "   doesn't redeploy on its own — set TR_DEPLOY_TARGET_REGIONS=us-east4"
    log "   when running rollout.sh to push current code.)"
  else
    log "  ERROR: Cloud Run service trusted-router not found in $US_EAST4_REGION"
    log "  Run rollout.sh with TR_DEPLOY_TARGET_REGIONS=$US_EAST4_REGION first."
    return 1
  fi

  log "  current revision:"
  gc run services describe trusted-router --region="$US_EAST4_REGION" \
    --format="value(status.latestCreatedRevisionName,status.url)" 2>&1 | head -3 || true

  log "  us-east4 Cloud Run is provisioned. Stage 3 will:"
  log "    1. Redeploy with the latest image (rollout.sh handles this)"
  log "    2. Attach its NEG to trusted-router-control-backend"
  log "    3. Ramp traffic 10/50/100% with watchdog gates"
}

# ─── Dispatch ──────────────────────────────────────────────────────────────
case "$PHASE" in
  nam6) phase_nam6 ;;
  bt-hdd) phase_bt_hdd ;;
  tr-multi) phase_tr_multi ;;
  us-east4) phase_us_east4 ;;
  all)
    phase_nam6
    phase_bt_hdd
    phase_tr_multi
    phase_us_east4
    ;;
  *) log "unknown phase: $PHASE"; exit 2 ;;
esac

log "done"
