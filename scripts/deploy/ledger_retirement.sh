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
#      reservation, and no spend-lease open row that is not done. The two
#      regional counts are read together: a settle-outbox intent that is still
#      pending or dead needs the ledger only while its lease is open (its
#      reservation may already be settled - normal finalization and the reaper
#      both commit settled=true before the intent is done - but then the lease
#      index row stays until reconciliation refunds and closes it; a terminal
#      replay skips regional finalization). So the open-lease index covers
#      settled-but-open work and the reservation count covers the rest, and
#      the outbox itself, which has no status index, is never scanned. The
#      spend reconciler's own "open" counts only rows DUE now; retry backoff
#      hides the rest, hence the table count;
#   3. while a reconciler schedule still exists, its exact target job (name and
#      location parsed from the schedule) reported REQUIRED all-zero passes,
#      all of them after every serving revision had been created plus a drain
#      interval, so the picture in 2 is neither a paused worker nor a stale one.
# Before any Spanner count is trusted, the fleet must have been quiescent:
# no region's traffic may have changed within the drain interval (Cloud Run
# keeps a predecessor's in-flight requests alive across a traffic move, and
# an older issuance-off revision can regain traffic seconds before the gate).
# Once the workers are gone, the durable marker written by ledger_retire_workers
# waives only the worker evidence (there is no worker left to report), and only
# while every serving revision still satisfies the retirement invariants
# (capability off, no app-profile map); the quiescence and Spanner checks
# always run, so escrow created during a temporary rollback is found even
# after traffic returns. A missing schedule without that marker is a partial
# teardown, never proof of retirement.

ledger_retirement_bucket() {
  printf '%s\n' "${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}"
}

ledger_retirement_marker_uri() {
  printf 'gs://%s/controls/ledger-retirement.json\n' "$(ledger_retirement_bucket)"
}

# 0 = a completed retirement marker for this project and database exists;
# 1 = no such marker; 2 = cannot tell.
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
ok = (
    isinstance(marker, dict)
    and marker.get("state") == "retired"
    and marker.get("project") == sys.argv[1]
    and marker.get("spanner_instance") == sys.argv[2]
    and marker.get("spanner_database") == sys.argv[3]
)
raise SystemExit(0 if ok else 1)
' "$PROJECT_ID" "$SPANNER_INSTANCE_ID" "$SPANNER_DATABASE_ID" <<<"$marker"; then
    return 0
  fi
  log "ledger retirement marker exists but does not record a completed retirement of ${PROJECT_ID}/${SPANNER_INSTANCE_ID}/${SPANNER_DATABASE_ID}; treating the ledgers as live"
  return 1
}

_ledger_spanner_count() {
  local label="$1" sql="$2" value
  value="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --priority=low \
    --timeout=60s \
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
# tr_reservation_by_expiry leads on settled (unsettled rows are the in-flight
# few, hold_usage_type is a back-join on that range), and spend_lease_open is
# the pilot's small working table. Nothing here touches tr_entities.body.
ledger_spanner_open_work() {
  local -a labels sqls
  labels=(
    "open regional lease index rows"
    "open regional lease workspace index rows"
    "unsettled RegionalCredits reservations"
    "unfinished spend-lease open rows"
  )
  sqls=(
    "SELECT COUNT(*) FROM tr_entities WHERE kind = 'regional_quota_lease_open'"
    "SELECT COUNT(*) FROM tr_entities WHERE kind = 'regional_quota_lease_workspace_open'"
    "SELECT COUNT(*) FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_expiry} WHERE settled = false AND hold_usage_type = 'RegionalCredits'"
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
  log "Spanner holds no regional lease, RegionalCredits reservation, or unfinished spend-lease row"
}

# Every serving revision, held regions included, must satisfy each marker.
# `name=false` accepts an absent marker because config.py defaults these
# switches to false; `name=__absent__` requires the setting to be unset or
# empty (an app-profile map alone opens a ledger client). Records the fleet's
# most recent traffic change in LEDGER_FLEET_NEWEST_REVISION_AT: the latest of
# each service's condition transition times (traffic moves update them) and
# the serving revision's creation time, whichever is newer.
LEDGER_FLEET_NEWEST_REVISION_AT=""
_ledger_serving_markers() {
  local purpose="$1"
  shift
  local -a regions
  IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
  local region revision_json service_json status pair name expected value created
  LEDGER_FLEET_NEWEST_REVISION_AT=""
  for region in "${regions[@]}"; do
    status=0
    revision_json="$(regional_quota_active_revision_json "$region" false)" || status=$?
    if [ "$status" -ne 0 ]; then
      log "refusing ${purpose}: cannot read the serving revision in ${region}"
      return 1
    fi
    service_json="$(gc run services describe "$SERVICE" --region="$region" --format=json 2>/dev/null)" || {
      log "refusing ${purpose}: cannot read the service in ${region}"
      return 1
    }
    created="$(python3 - "$revision_json" "$service_json" <<'PY'
import json, sys
revision = json.loads(sys.argv[1])
service = json.loads(sys.argv[2])
stamps = []
created = revision.get("metadata", {}).get("creationTimestamp")
if not isinstance(created, str) or not created:
    raise SystemExit("serving revision has no creationTimestamp")
stamps.append(created)
for condition in service.get("status", {}).get("conditions", []) or []:
    stamp = condition.get("lastTransitionTime") if isinstance(condition, dict) else None
    if isinstance(stamp, str) and stamp:
        stamps.append(stamp)
print(max(stamps))
PY
)" || {
      log "refusing ${purpose}: the serving revision in ${region} has no creation time"
      return 1
    }
    if [ -z "$LEDGER_FLEET_NEWEST_REVISION_AT" ] || [[ "$created" > "$LEDGER_FLEET_NEWEST_REVISION_AT" ]]; then
      LEDGER_FLEET_NEWEST_REVISION_AT="$created"
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

# No Spanner count is trusted until every region's last traffic change is at
# least the drain interval old: a predecessor revision's in-flight requests
# outlive a traffic move, and they can still mint or settle a hold.
ledger_fleet_quiescent() {
  local interval="${TR_LEDGER_DRAIN_INTERVAL_SECONDS:-600}"
  python3 - "$LEDGER_FLEET_NEWEST_REVISION_AT" "$interval" <<'PY' || return 1
import datetime as dt
import sys

newest, interval = sys.argv[1], int(sys.argv[2])
try:
    changed = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(f"refusing ledger retirement: fleet traffic time {newest!r} is unreadable")
if changed.tzinfo is None:
    changed = changed.replace(tzinfo=dt.UTC)
now = dt.datetime.now(dt.UTC)
ready = changed + dt.timedelta(seconds=interval)
if now < ready:
    raise SystemExit(
        f"refusing ledger retirement: fleet traffic changed at {changed.isoformat()}; "
        f"wait until {ready.isoformat()} ({interval}s drain interval)"
    )
print(f"fleet quiescent since {changed.isoformat()} (+{interval}s drain interval)")
PY
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

# The exact target job of a live schedule (project, location and name) must
# have reported REQUIRED all-zero passes, every one of them after the newest
# serving revision was created plus the drain interval. A paused or silent
# worker refuses: nobody is draining.
_ledger_reconciler_evidence() {
  local label="$1" state="$2" job_region="$3" job="$4" marker="$5"
  shift 5
  local required="${TR_LEDGER_DRAIN_REQUIRED_PASSES:-5}"
  local freshness="${TR_LEDGER_DRAIN_FRESHNESS:-15m}"
  local interval="${TR_LEDGER_DRAIN_INTERVAL_SECONDS:-600}"
  if [ "$state" != "ENABLED" ]; then
    log "refusing ledger retirement: the ${label} schedule is ${state:-in an unknown state}; it must be draining"
    return 1
  fi
  local json
  json="$(gc logging read \
    "resource.type=\"cloud_run_job\" AND resource.labels.project_id=\"${PROJECT_ID}\" AND resource.labels.location=\"${job_region}\" AND resource.labels.job_name=\"${job}\" AND textPayload:\"${marker}\"" \
    --freshness="$freshness" \
    --limit="$required" \
    --order=desc \
    --format=json)" || {
    log "refusing ledger retirement: cannot read ${label} completions"
    return 1
  }
  python3 - "$label" "$marker" "$required" "$json" "$LEDGER_FLEET_NEWEST_REVISION_AT" "$interval" "$@" <<'PY'
import datetime as dt
import json
import re
import sys

label, marker, required, raw, newest, interval = sys.argv[1:7]
required = int(required)
zero_fields = sys.argv[7:]


def parse(stamp: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value


try:
    cutoff = parse(newest) + dt.timedelta(seconds=int(interval))
except ValueError:
    raise SystemExit(f"refusing ledger retirement: serving revision time {newest!r} is unreadable")
try:
    entries = json.loads(raw or "[]")
except ValueError:
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not JSON")
if not isinstance(entries, list):
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not a list")
lines = []
for entry in entries:
    if not isinstance(entry, dict):
        continue
    payload = entry.get("textPayload")
    if not isinstance(payload, str) or marker not in payload:
        continue
    stamp = entry.get("timestamp")
    try:
        when = parse(stamp) if isinstance(stamp, str) else None
    except ValueError:
        when = None
    if when is None:
        raise SystemExit(f"refusing ledger retirement: {label} completion has no timestamp")
    lines.append((when, payload))
if len(lines) < required:
    raise SystemExit(
        f"refusing ledger retirement: {label} has {len(lines)} recent completion(s); "
        f"require {required} (is the worker running?)"
    )
early = [when for when, _ in lines if when < cutoff]
if early:
    raise SystemExit(
        f"refusing ledger retirement: {label} completion at {min(early).isoformat()} "
        f"predates the drain cutoff {cutoff.isoformat()} (newest serving revision + {interval}s)"
    )
for _, line in lines:
    values = dict(re.findall(r"\b([a-z_]+)=(\d+)\b", line))
    missing = [field for field in zero_fields if field not in values]
    if missing:
        raise SystemExit(f"refusing ledger retirement: {label} completion lacks {','.join(missing)}")
    busy = {field: values[field] for field in zero_fields if values[field] != "0"}
    if busy:
        summary = " ".join(f"{k}={v}" for k, v in busy.items())
        raise SystemExit(f"refusing ledger retirement: {label} is not drained ({summary})")
print(f"{label}: {len(lines)} consecutive empty passes after {cutoff.isoformat()}")
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
      if ledger_capability_off_everywhere; then
        # The marker waives the worker evidence, never the quiescence or
        # Spanner checks: a temporary rollback could have created escrow
        # that outlived it.
        ledger_fleet_quiescent || return 1
        ledger_spanner_open_work || return 1
        log "ledger retirement is recorded as complete and every serving revision still runs without the ledgers"
        return 0
      fi
      log "ledger retirement is recorded but a serving revision carries lease capability again; running the full gate"
      ;;
    1) ;;
    *) return 1 ;;
  esac
  ledger_issuance_off_everywhere || return 1
  ledger_fleet_quiescent || return 1
  ledger_spanner_open_work || return 1

  local target state job_region job
  target="$(_ledger_schedule_target "$(_ledger_regional_scheduler)" "$(_ledger_regional_scheduler_region)")" || return 1
  if [ -n "$target" ]; then
    IFS=$'\t' read -r state job_region job <<<"$target"
    _ledger_reconciler_evidence "regional quota reconciler" "$state" "$job_region" "$job" \
      "regional_quota.reconcile_complete" inspected backlog remaining errors || return 1
  else
    log "warning: the regional quota reconciler schedule is already gone without a retirement marker; relying on Spanner evidence alone"
  fi
  target="$(_ledger_schedule_target "$(_ledger_spend_scheduler)" "$(_ledger_spend_scheduler_region)")" || return 1
  if [ -n "$target" ]; then
    IFS=$'\t' read -r state job_region job <<<"$target"
    _ledger_reconciler_evidence "spend-lease reconciler" "$state" "$job_region" "$job" \
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

ledger_retirement_targets_uri() {
  printf 'gs://%s/controls/ledger-retirement-targets.json\n' "$(ledger_retirement_bucket)"
}

# The job names the schedules targeted, recorded durably before any schedule
# is deleted, so a retry after an interrupted teardown still knows a custom
# target that matches neither prefix nor override. Absent = nothing recorded.
_ledger_recorded_targets() {
  local listing present record
  listing="$(regional_quota_gc_read storage objects list --raw --format=json "gs://$(ledger_retirement_bucket)/controls/*")" || {
    log "refusing worker retirement: cannot list the control prefix"
    return 1
  }
  present="$(python3 -c '
import json, sys
items = json.load(sys.stdin)
urls = ["gs://" + i["bucket"] + "/" + i["name"] for i in items if isinstance(i, dict) and not i.get("timeDeleted")]
print("true" if sys.argv[1] in urls else "false")
' "$(ledger_retirement_targets_uri)" <<<"$listing")" || return 1
  [ "$present" = true ] || return 0
  record="$(regional_quota_gc_read storage cat "$(ledger_retirement_targets_uri)")" || {
    log "refusing worker retirement: cannot read the recorded worker targets"
    return 1
  }
  python3 -c '
import json, re, sys
try:
    record = json.load(sys.stdin)
except ValueError:
    raise SystemExit("not JSON")
targets = record.get("targets") if isinstance(record, dict) else None
if not isinstance(targets, list):
    raise SystemExit("no targets list")
for name in targets:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise SystemExit(f"invalid target name {name!r}")
    print(name)
' <<<"$record" || {
    log "refusing worker retirement: the recorded worker targets are unreadable"
    return 1
  }
}

_ledger_record_targets() {
  regional_quota_verify_control_lifecycle || return 1
  local record
  record="$(mktemp "${TMPDIR:-/tmp}/ledger-targets.XXXXXX")" || return 1
  if ! python3 -c '
import json, re, sys
names = sorted(set(name for name in sys.argv[1:] if name))
for name in names:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise SystemExit(f"invalid target name {name!r}")
print(json.dumps({"targets": names}))
' "$@" >"$record"; then
    rm -f "$record"
    log "refusing worker retirement: cannot serialize the worker targets"
    return 1
  fi
  gc storage cp "$record" "$(ledger_retirement_targets_uri)" --quiet || {
    rm -f "$record"
    log "refusing worker retirement: cannot record the worker targets"
    return 1
  }
  rm -f "$record"
}

# Project-wide worker inventory as "region<TAB>name" lines: every job under
# either historical prefix, the exact names the deployers accept as
# overrides, and the recorded schedule targets. Project-wide so a retry
# after an interrupted teardown - schedules already gone - still finds a
# worker in a region only a schedule used to point at. A listing that warns
# (unreachable regions come back as a warning with exit 0) is incomplete and
# proves nothing, so it refuses.
_ledger_worker_inventory() {
  local listing listing_stderr
  listing_stderr="$(mktemp "${TMPDIR:-/tmp}/ledger-jobs.XXXXXX")"
  listing="$(gc run jobs list --verbosity=warning --format='value(metadata.labels."cloud.googleapis.com/location",metadata.name)' 2>"$listing_stderr")" || {
    log "refusing worker retirement: cannot list Cloud Run jobs: $(<"$listing_stderr")"
    rm -f "$listing_stderr"
    return 1
  }
  if [ -s "$listing_stderr" ]; then
    log "refusing worker retirement: the Cloud Run job listing is incomplete: $(<"$listing_stderr")"
    rm -f "$listing_stderr"
    return 1
  fi
  rm -f "$listing_stderr"
  local exact=" ${TR_REGIONAL_QUOTA_RECONCILER_JOB:-} ${TR_SPEND_LEASE_RECONCILER_JOB:-} $* "
  # Historical default prefixes are always matched: a configured prefix must
  # not hide the workers earlier releases created under the defaults.
  printf '%s\n' "$listing" | while IFS=$'\t' read -r region job; do
    [ -n "$job" ] && [ -n "$region" ] || continue
    case "$job" in
      trusted-router-regional-quota-reconciler-*|trusted-router-spend-lease-reconciler-*|\
      "$(_ledger_regional_job_prefix)-"*|"$(_ledger_spend_job_prefix)-"*) printf '%s\t%s\n' "$region" "$job" ;;
      *)
        case "$exact" in
          *" ${job} "*) printf '%s\t%s\n' "$region" "$job" ;;
        esac
        ;;
    esac
  done
}

ledger_retire_workers() {
  local status=0 recorded_complete=false
  ledger_retirement_completed || status=$?
  case "$status" in
    0) recorded_complete=true ;;
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
  ledger_fleet_quiescent || return 1
  ledger_spanner_open_work || return 1
  if [ "$recorded_complete" = true ]; then
    # A recorded retirement is a no-op only once the current state agrees:
    # a rollback that recreated the schedules or workers gets torn down
    # again and the marker rewritten.
    local leftovers
    leftovers="$(_ledger_worker_inventory)" || return 1
    local scheduler region target
    for scheduler in "$(_ledger_regional_scheduler)|$(_ledger_regional_scheduler_region)" \
                     "$(_ledger_spend_scheduler)|$(_ledger_spend_scheduler_region)"; do
      region="${scheduler#*|}"
      scheduler="${scheduler%%|*}"
      target="$(_ledger_schedule_target "$scheduler" "$region")" || return 1
      [ -z "$target" ] || leftovers="${leftovers}${leftovers:+$'\n'}schedule:${scheduler}"
    done
    if [ -z "$leftovers" ]; then
      log "ledger retirement is already recorded as complete and nothing has come back"
      return 0
    fi
    log "ledger retirement is recorded but workers or schedules came back; retiring them again: ${leftovers//$'\n'/ }"
  fi

  # Learn the schedules' targets and record them durably before anything is
  # deleted, merged with what an interrupted earlier run recorded.
  local -a targets=()
  local recorded scheduler region target job_region job
  recorded="$(_ledger_recorded_targets)" || return 1
  while IFS= read -r job; do
    [ -n "$job" ] && targets+=("$job")
  done <<<"$recorded"
  local -a live_schedules=()
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
    targets+=("$job")
    live_schedules+=("${scheduler}|${region}|${job}|${job_region}")
  done
  if [ "${#live_schedules[@]}" -gt 0 ]; then
    _ledger_record_targets "${targets[@]+"${targets[@]}"}" || return 1
  fi
  local entry
  for entry in "${live_schedules[@]+"${live_schedules[@]}"}"; do
    IFS='|' read -r scheduler region job job_region <<<"$entry"
    gc scheduler jobs delete "$scheduler" --location="$region" --quiet || {
      log "refusing worker retirement: cannot delete schedule ${scheduler} in ${region}"
      return 1
    }
    log "deleted schedule ${scheduler} in ${region} (target ${job} in ${job_region})"
  done

  local inventory deleted=0
  inventory="$(_ledger_worker_inventory "${targets[@]+"${targets[@]}"}")" || return 1
  while IFS=$'\t' read -r region job; do
    [ -n "$job" ] || continue
    _ledger_wait_for_executions "$job" "$region" || return 1
    gc run jobs delete "$job" --region="$region" --quiet || {
      log "refusing worker retirement: cannot delete ${job} in ${region}"
      return 1
    }
    deleted=$((deleted + 1))
    log "deleted worker ${job} in ${region}"
  done <<<"$inventory"

  # Prove absence, project-wide, before recording completion.
  inventory="$(_ledger_worker_inventory "${targets[@]+"${targets[@]}"}")" || return 1
  if [ -n "$inventory" ]; then
    log "refusing to record retirement: workers remain: ${inventory//$'\t'/:}"
    return 1
  fi
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
    "project": sys.argv[1],
    "spanner_instance": sys.argv[2],
    "spanner_database": sys.argv[3],
    "completed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "release": sys.argv[4],
    "workers_deleted": int(sys.argv[5]),
}))
' "$PROJECT_ID" "$SPANNER_INSTANCE_ID" "$SPANNER_DATABASE_ID" "${RELEASE:-unknown}" "$deleted" >"$record"
  gc storage cp "$record" "$(ledger_retirement_marker_uri)" --quiet || {
    rm -f "$record"
    log "refusing to record retirement: cannot write the marker"
    return 1
  }
  rm -f "$record"
  log "ledger reconciler workers retired (${deleted} job(s) deleted); marker written to $(ledger_retirement_marker_uri)"
}
