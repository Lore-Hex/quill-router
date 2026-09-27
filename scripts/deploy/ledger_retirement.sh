# shellcheck shell=bash
# Ledger retirement interlock (2026-09-27).
#
# The regional quota and spend-lease escrow ledgers leave the runtime only once
# nothing can still need them, and their once-a-minute workers are removed only
# after that. This file is sourced by rollout.sh (so every rollout entry point
# is covered: the release workflow, deploy-gcp.sh, break-glass, the analytics
# cutover) and by the two release-step wrappers. It has no top-level cloud
# calls. Requires _lib.sh and regional_quota_rollout.sh to be sourced first.
#
# What "drained" means here, and what proves it:
#   1. every serving revision, held regions included, carries
#      TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false,
#      TR_SPEND_LEASE_ISSUANCE_ENABLED=false, TR_SPEND_LEASE_BINDING_ENABLED=false
#      and TR_SPEND_LEASE_ADMISSION_ACCEPT=false (every one of them defaults to
#      false in config.py, so a revision without the marker cannot mint
#      either; only an explicit other value refuses);
#   2. Spanner, the source of truth, holds no work that would need a ledger:
#      no open regional lease index rows, no unsettled RegionalCredits
#      reservation (settlement, refund and reaping of one all replay through
#      the ledger), no pending or dead settle-outbox intent for such a
#      reservation, and no spend-lease open row that is not done (the
#      reconciler's own "open" counts only rows DUE now; retry backoff hides
#      the rest);
#   3. while a reconciler schedule still exists, its exact target job reported
#      five recent all-zero passes, so the picture in 2 is not a paused worker.
# Once the workers are gone the durable marker written by ledger_retire_workers
# is the only thing that stands the gate down. A missing schedule without that
# marker is a partial teardown, never proof of retirement.

ledger_retirement_bucket() {
  printf '%s\n' "${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}"
}

ledger_retirement_marker_uri() {
  printf 'gs://%s/controls/ledger-retirement.json\n' "$(ledger_retirement_bucket)"
}

# 0 = a completed retirement marker exists; 1 = no marker; 2 = cannot tell.
ledger_retirement_completed() {
  regional_quota_verify_control_lifecycle || return 2
  local listing present marker
  listing="$(regional_quota_gc_read storage objects list --raw --format=json "gs://$(ledger_retirement_bucket)/controls/*")" || {
    log "refusing ledger retirement check: cannot list the control prefix"
    return 2
  }
  present="$(python3 -c '
import json, sys
items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("expected control object JSON array")
urls = []
for item in items:
    if not isinstance(item, dict) or not isinstance(item.get("bucket"), str) or not isinstance(item.get("name"), str):
        raise SystemExit("invalid control object listing")
    if not item.get("timeDeleted"):
        urls.append("gs://" + item["bucket"] + "/" + item["name"])
print("true" if sys.argv[1] in urls else "false")
' "$(ledger_retirement_marker_uri)" <<<"$listing")" || {
    log "refusing ledger retirement check: cannot parse the control listing"
    return 2
  }
  [ "$present" = true ] || return 1
  marker="$(regional_quota_gc_read storage cat "$(ledger_retirement_marker_uri)")" || {
    log "refusing ledger retirement check: cannot read the retirement marker"
    return 2
  }
  if python3 -c '
import json, sys
try:
    marker = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(marker, dict) and marker.get("state") == "retired" else 1)
' <<<"$marker"; then
    return 0
  fi
  log "ledger retirement marker exists but does not record a completed retirement; treating the ledgers as live"
  return 1
}

_ledger_spanner_count() {
  local label="$1" sql="$2" value
  value="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --sql="$sql" \
    --format='value(rows[0])')" || {
    log "refusing ledger retirement: cannot count ${label}"
    return 1
  }
  case "$value" in
    ''|*[!0-9]*)
      log "refusing ledger retirement: ${label} count is not a number: '${value}'"
      return 1
      ;;
  esac
  printf '%s\n' "$value"
}

# Every query is bounded by a key or an index: tr_entities is keyed by kind,
# tr_reservation_by_expiry leads on settled, spend_lease_open is the pilot's
# small working table, and the outbox status scan is the same one the release
# gates already run. Nothing here touches tr_entities.body.
ledger_spanner_open_work() {
  local -a labels sqls
  labels=(
    "open regional lease index rows"
    "open regional lease workspace index rows"
    "unsettled RegionalCredits reservations"
    "pending or dead settle intents for RegionalCredits reservations"
    "unfinished spend-lease open rows"
  )
  sqls=(
    "SELECT COUNT(*) FROM tr_entities WHERE kind = 'regional_quota_lease_open'"
    "SELECT COUNT(*) FROM tr_entities WHERE kind = 'regional_quota_lease_workspace_open'"
    "SELECT COUNT(*) FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_expiry} WHERE settled = false AND hold_usage_type = 'RegionalCredits'"
    "SELECT COUNT(*) FROM tr_settle_outbox AS o JOIN tr_reservation AS r ON r.reservation_id = o.reservation_id WHERE o.status IN ('pending', 'dead') AND r.hold_usage_type = 'RegionalCredits'"
    "SELECT COUNT(*) FROM spend_lease_open WHERE phase != 'done' OR dead = true"
  )
  local index count
  for index in "${!labels[@]}"; do
    count="$(_ledger_spanner_count "${labels[$index]}" "${sqls[$index]}")" || return 1
    if [ "$count" != "0" ]; then
      log "refusing ledger retirement: Spanner still holds ${count} ${labels[$index]}"
      return 1
    fi
  done
  log "Spanner holds no regional lease, RegionalCredits reservation, regional settle intent, or unfinished spend-lease row"
}

# Every serving revision, held regions included, must satisfy each marker.
# `name=false` accepts an absent marker because config.py defaults these
# switches to false; `name=__absent__` requires the setting to be unset or
# empty (an app-profile map alone opens a ledger client).
_ledger_serving_markers() {
  local purpose="$1"
  shift
  local -a regions
  IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
  local region revision_json status pair name expected value
  for region in "${regions[@]}"; do
    status=0
    revision_json="$(regional_quota_active_revision_json "$region" false)" || status=$?
    if [ "$status" -ne 0 ]; then
      log "refusing ${purpose}: cannot read the serving revision in ${region}"
      return 1
    fi
    for pair in "$@"; do
      name="${pair%%=*}"
      expected="${pair#*=}"
      value="$(regional_quota_revision_env "$revision_json" "$name" __missing__)" || return 1
      if [ "$expected" = "__absent__" ]; then
        if [ "$value" != "__missing__" ] && [ -n "$value" ]; then
          log "refusing ${purpose}: ${region} still serves ${name}=${value}"
          return 1
        fi
      elif [ "$value" = "__missing__" ] && [ "$expected" = "false" ]; then
        log "${region}: ${name} is not set on the serving revision; it defaults to false"
      elif [ "$value" != "$expected" ]; then
        log "refusing ${purpose}: ${region} still serves ${name}=${value}"
        return 1
      fi
    done
  done
}

ledger_issuance_off_everywhere() {
  _ledger_serving_markers "ledger retirement" \
    TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_BINDING_ENABLED=false \
    TR_SPEND_LEASE_ADMISSION_ACCEPT=false
}

ledger_capability_off_everywhere() {
  _ledger_serving_markers "worker retirement" \
    TR_REGIONAL_QUOTA_LEASES_ENABLED=false \
    TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false \
    TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=__absent__ \
    TR_SPEND_LEASE_BIGTABLE_APP_PROFILES=__absent__
}

# Prints "state<TAB>job_region<TAB>job_name" for a schedule, or nothing when it
# does not exist. Any other failure aborts.
_ledger_schedule_target() {
  local scheduler="$1" region="$2" describe_stderr scheduler_json status=0
  describe_stderr="$(mktemp "${TMPDIR:-/tmp}/ledger-schedule.XXXXXX")"
  scheduler_json="$(gc scheduler jobs describe "$scheduler" --location="$region" --format=json 2>"$describe_stderr")" || status=$?
  if [ "$status" -ne 0 ]; then
    if grep -qE '(^|[[:space:]])NOT_FOUND([[:space:]:]|$)' "$describe_stderr"; then
      rm -f "$describe_stderr"
      return 0
    fi
    log "refusing ledger retirement: cannot read schedule ${scheduler} in ${region}: $(<"$describe_stderr")"
    rm -f "$describe_stderr"
    return 1
  fi
  rm -f "$describe_stderr"
  python3 -c '
import json, re, sys
scheduler = json.load(sys.stdin)
uri = scheduler.get("httpTarget", {}).get("uri", "")
match = re.fullmatch(r"https://([a-z0-9-]+)-run\.googleapis\.com/apis/run\.googleapis\.com/v1/namespaces/([^/]+)/jobs/([a-z][a-z0-9-]{0,62}):run", uri)
if not match or match.group(2) != sys.argv[1]:
    raise SystemExit("refusing ledger retirement: schedule %s does not target a Cloud Run job of this project" % sys.argv[2])
print("%s\t%s\t%s" % (scheduler.get("state", ""), match.group(1), match.group(3)))
' "$PROJECT_ID" "$scheduler" <<<"$scheduler_json" || return 1
}

# The exact target job of a live schedule must have reported REQUIRED recent
# all-zero passes. A paused or silent worker refuses: nobody is draining.
_ledger_reconciler_evidence() {
  local label="$1" state="$2" job="$3" marker="$4"
  shift 4
  local required="${TR_LEDGER_DRAIN_REQUIRED_PASSES:-5}"
  local freshness="${TR_LEDGER_DRAIN_FRESHNESS:-15m}"
  if [ "$state" != "ENABLED" ]; then
    log "refusing ledger retirement: the ${label} schedule is ${state:-in an unknown state}; it must be draining"
    return 1
  fi
  local json
  json="$(gc logging read \
    "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${job}\" AND textPayload:\"${marker}\"" \
    --freshness="$freshness" \
    --limit="$required" \
    --order=desc \
    --format=json)" || {
    log "refusing ledger retirement: cannot read ${label} completions"
    return 1
  }
  python3 - "$label" "$marker" "$required" "$json" "$@" <<'PY'
import json
import re
import sys

label, marker, required, raw = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
zero_fields = sys.argv[5:]
try:
    entries = json.loads(raw or "[]")
except ValueError:
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not JSON")
if not isinstance(entries, list):
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not a list")
lines = []
for entry in entries:
    payload = entry.get("textPayload") if isinstance(entry, dict) else None
    if isinstance(payload, str) and marker in payload:
        lines.append(payload)
if len(lines) < required:
    raise SystemExit(
        f"refusing ledger retirement: {label} has {len(lines)} recent completion(s); "
        f"require {required} (is the worker running?)"
    )
for line in lines:
    values = dict(re.findall(r"\b([a-z_]+)=(\d+)\b", line))
    missing = [field for field in zero_fields if field not in values]
    if missing:
        raise SystemExit(f"refusing ledger retirement: {label} completion lacks {','.join(missing)}")
    busy = {field: values[field] for field in zero_fields if values[field] != "0"}
    if busy:
        summary = " ".join(f"{k}={v}" for k, v in busy.items())
        raise SystemExit(f"refusing ledger retirement: {label} is not drained ({summary})")
print(f"{label} ({sys.argv[2].split('.')[0]}): {len(lines)} consecutive empty passes")
PY
}

_ledger_regional_scheduler() { printf '%s\n' "${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"; }
_ledger_regional_scheduler_region() { printf '%s\n' "${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"; }
_ledger_spend_scheduler() { printf '%s\n' "${TR_SPEND_LEASE_RECONCILER_SCHEDULER:-trusted-router-spend-lease-reconcile}"; }
_ledger_spend_scheduler_region() { printf '%s\n' "${TR_SPEND_LEASE_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"; }
_ledger_regional_job_prefix() { printf '%s\n' "${TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX:-trusted-router-regional-quota-reconciler}"; }
_ledger_spend_job_prefix() { printf '%s\n' "${TR_SPEND_LEASE_RECONCILER_JOB_PREFIX:-trusted-router-spend-lease-reconciler}"; }

# The release gate. Read-only. Exit 0 = the ledgers may be absent from the
# revisions this run creates.
ledger_retirement_gate() {
  local status=0
  ledger_retirement_completed || status=$?
  case "$status" in
    0)
      log "ledger retirement is recorded as complete; nothing to gate"
      return 0
      ;;
    1) ;;
    *) return 1 ;;
  esac
  ledger_issuance_off_everywhere || return 1
  ledger_spanner_open_work || return 1

  local target state job
  target="$(_ledger_schedule_target "$(_ledger_regional_scheduler)" "$(_ledger_regional_scheduler_region)")" || return 1
  if [ -n "$target" ]; then
    IFS=$'\t' read -r state _ job <<<"$target"
    _ledger_reconciler_evidence "regional quota reconciler" "$state" "$job" \
      "regional_quota.reconcile_complete" inspected backlog remaining errors || return 1
  else
    log "warning: the regional quota reconciler schedule is already gone without a retirement marker; relying on Spanner evidence alone"
  fi
  target="$(_ledger_schedule_target "$(_ledger_spend_scheduler)" "$(_ledger_spend_scheduler_region)")" || return 1
  if [ -n "$target" ]; then
    IFS=$'\t' read -r state _ job <<<"$target"
    _ledger_reconciler_evidence "spend-lease reconciler" "$state" "$job" \
      "spend_lease.reconcile_complete" candidates open dead errors || return 1
  else
    log "warning: the spend-lease reconciler schedule is already gone without a retirement marker; relying on Spanner evidence alone"
  fi
  log "ledger escrow is drained; the ledgers may leave the runtime"
}

# Cloud Run deletes a job's running executions with the job, so wait for the
# ones the schedule already started before deleting the definition.
_ledger_wait_for_executions() {
  local job="$1" region="$2"
  local attempts="${TR_LEDGER_RETIRE_EXECUTION_WAIT_ATTEMPTS:-12}"
  local pause="${TR_LEDGER_RETIRE_RETRY_SLEEP_SECONDS:-10}"
  local attempt=0 listing running
  while :; do
    listing="$(gc run jobs executions list --job="$job" --region="$region" \
      --format='value(metadata.name,status.completionTime)')" || {
      log "refusing worker retirement: cannot list executions of ${job} in ${region}"
      return 1
    }
    running="$(printf '%s\n' "$listing" | awk -F'\t' 'NF >= 1 && $1 != "" && $2 == "" { print $1 }')"
    [ -n "$running" ] || return 0
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$attempts" ]; then
      log "refusing worker retirement: ${job} in ${region} still has running executions after ${attempts} checks: ${running//$'\n'/ }"
      return 1
    fi
    sleep "$pause"
  done
}

_ledger_matching_jobs() {
  local region="$1" listing
  listing="$(gc run jobs list --region="$region" --format='value(metadata.name)')" || {
    log "refusing worker retirement: cannot list Cloud Run jobs in ${region}"
    return 1
  }
  printf '%s\n' "$listing" | while IFS= read -r job; do
    [ -n "$job" ] || continue
    case "$job" in
      "$(_ledger_regional_job_prefix)-"*|"$(_ledger_spend_job_prefix)-"*) printf '%s\n' "$job" ;;
    esac
  done
}

ledger_retire_workers() {
  local status=0
  ledger_retirement_completed || status=$?
  case "$status" in
    0)
      log "ledger retirement is already recorded as complete"
      return 0
      ;;
    1) ;;
    *) return 1 ;;
  esac
  # A held region still serves a capability-on revision with its profile
  # maps: the workers are harmless while nothing new can be issued, so leave
  # them for the next release rather than retiring under a live ledger.
  if ! ledger_capability_off_everywhere; then
    echo "::warning::ledger workers kept: a serving revision still carries lease capability; retirement retries on the next release"
    log "deferring worker retirement; the ledgers stay provisioned until every region serves a capability-off revision"
    return 0
  fi
  ledger_spanner_open_work || return 1

  local -a job_regions
  IFS=',' read -r -a job_regions <<<"$TR_CONTROL_PLANE_REGIONS"
  job_regions+=(
    "${TR_REGIONAL_QUOTA_RECONCILER_JOB_REGION:-us-east4}"
    "${TR_SPEND_LEASE_RECONCILER_JOB_REGION:-us-east4}"
  )
  local scheduler region target job_region job
  for scheduler in "$(_ledger_regional_scheduler)|$(_ledger_regional_scheduler_region)" \
                   "$(_ledger_spend_scheduler)|$(_ledger_spend_scheduler_region)"; do
    region="${scheduler#*|}"
    scheduler="${scheduler%%|*}"
    target="$(_ledger_schedule_target "$scheduler" "$region")" || return 1
    if [ -z "$target" ]; then
      log "schedule ${scheduler} is already gone"
      continue
    fi
    IFS=$'\t' read -r _ job_region job <<<"$target"
    job_regions+=("$job_region")
    gc scheduler jobs delete "$scheduler" --location="$region" --quiet || {
      log "refusing worker retirement: cannot delete schedule ${scheduler} in ${region}"
      return 1
    }
    log "deleted schedule ${scheduler} in ${region} (target ${job} in ${job_region})"
  done

  local seen=" " deleted=0 jobs
  for region in "${job_regions[@]}"; do
    case "$seen" in *" ${region} "*) continue ;; esac
    seen="${seen}${region} "
    jobs="$(_ledger_matching_jobs "$region")" || return 1
    while IFS= read -r job; do
      [ -n "$job" ] || continue
      _ledger_wait_for_executions "$job" "$region" || return 1
      gc run jobs delete "$job" --region="$region" --quiet || {
        log "refusing worker retirement: cannot delete ${job} in ${region}"
        return 1
      }
      deleted=$((deleted + 1))
      log "deleted worker ${job} in ${region}"
    done <<<"$jobs"
  done

  # Prove absence before recording completion.
  for region in $seen; do
    jobs="$(_ledger_matching_jobs "$region")" || return 1
    if [ -n "$jobs" ]; then
      log "refusing to record retirement: workers remain in ${region}: ${jobs//$'\n'/ }"
      return 1
    fi
  done
  for scheduler in "$(_ledger_regional_scheduler)|$(_ledger_regional_scheduler_region)" \
                   "$(_ledger_spend_scheduler)|$(_ledger_spend_scheduler_region)"; do
    region="${scheduler#*|}"
    scheduler="${scheduler%%|*}"
    target="$(_ledger_schedule_target "$scheduler" "$region")" || return 1
    if [ -n "$target" ]; then
      log "refusing to record retirement: schedule ${scheduler} still exists in ${region}"
      return 1
    fi
  done

  regional_quota_verify_control_lifecycle || return 1
  local record
  record="$(mktemp "${TMPDIR:-/tmp}/ledger-retirement.XXXXXX")" || return 1
  python3 -c '
import datetime as dt, json, sys
print(json.dumps({
    "state": "retired",
    "completed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "release": sys.argv[1],
    "workers_deleted": int(sys.argv[2]),
}))
' "${RELEASE:-unknown}" "$deleted" >"$record"
  gc storage cp "$record" "$(ledger_retirement_marker_uri)" --quiet || {
    rm -f "$record"
    log "refusing to record retirement: cannot write the marker"
    return 1
  }
  rm -f "$record"
  log "ledger reconciler workers retired (${deleted} job(s) deleted); marker written to $(ledger_retirement_marker_uri)"
}
