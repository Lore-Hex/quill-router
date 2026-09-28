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
#      hides the rest, hence the table count. A spend lease closed on both
#      sides (local_closed_at set, which requires global_closed_at) has
#      released its escrow and keeps phase='open' only for retention; that
#      cleanup needs no ledger and never runs for the pilot's last lease
#      while the fence names it (nothing succeeds it once issuance is off),
#      so such rows are not counted. Dead rows always are;
#   3. while a reconciler schedule still exists, its exact target job (name and
#      location parsed from the schedule) reported REQUIRED healthy passes -
#      no errors, nothing dead, nothing left half-created - all of them after
#      the current fleet state was first seen plus the drain interval, so the
#      picture in 2 is neither a paused worker nor a stale one. Open work is
#      Spanner's call, not the worker's: the spend reconciler visits a
#      retention-only lease on every pass (open=1 deferred=1).
# Every control object (observation, gate pass, targets, marker) is written
# with a generation precondition read before its content, so an upload that
# lands after another writer's - a lease that ran out while gcloud retried -
# fails instead of overwriting the newer record.
# Before any Spanner count is trusted, the fleet must have been quiescent:
# every revision that can still take requests in a region - the traffic
# split's members and every tagged revision, tags stay addressable at 0% -
# must carry the markers, and the region must have served only such
# revisions for the drain interval. That interval is proven by a durable
# observation (controls/ledger-drain-observation.json): each region's service
# generation and reachable revision set, with the time this fleet state was
# first seen; any change - a deploy, a rollback, a tag - resets that time,
# because Cloud Run offers no traffic history and keeps a predecessor's
# in-flight requests alive across a move. A service is read only once its
# status is reconciled (observedGeneration == generation, no Ready=Unknown):
# status.traffic describes the last reconciled spec, not a pending one. The
# interval only has to outlast the authorize phase of such a request (the
# gateway's 25 s budget), so it defaults to 120 s.
# The release workflow warms the four regions in parallel processes that
# share one deployment mutex operation. Each of them runs this gate, and each
# would otherwise see the others' new revisions as fleet changes. So a pass
# is recorded (controls/ledger-gate-pass.json, written only by a gate that
# runs under a mutex operation, so no observation rewrite by an unlocked gate
# can erase it) under the mutex operation it ran under, and a later gate
# under the same operation stands on it - provided the live
# production lock is still held by that operation and unexpired, since the
# premise is that while the lock is held, the fleet changes only through
# this release's own revisions, which render every marker off. A standalone
# rollout takes its own lock and its own operation, so it always runs the
# full gate; a teardown under an inherited operation requires the same live
# lock before every destructive step and the marker - its waits can outlast
# a lease - and fails otherwise (a re-run acquires afresh).
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

# Reads a control object into the file at $2 - empty when it does not exist
# (the prefix listing decides that, never a failed read) - and fails when
# that cannot be told. Every control object here is JSON, so present means
# non-empty. Leaves the object's generation in LEDGER_CONTROL_GENERATION
# (0 = absent) for a fenced write; it is read BEFORE the content, so a write
# landing between the two fails the fence rather than being overwritten.
# Call it directly, never inside a command substitution: the generation
# comes back through the shell variable.
LEDGER_CONTROL_GENERATION=0
_ledger_read_control() {
  local uri="$1" out="$2" listing present generation
  LEDGER_CONTROL_GENERATION=0
  : >"$out"
  listing="$(regional_quota_gc_read storage objects list --raw --format=json "gs://$(ledger_retirement_bucket)/controls/*")" || {
    log "refusing ledger retirement: cannot list the control prefix"
    return 1
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
' "$uri" <<<"$listing")" || {
    log "refusing ledger retirement: cannot parse the control listing"
    return 1
  }
  [ "$present" = true ] || return 0
  generation="$(regional_quota_gc_read storage objects describe "$uri" --format='value(generation)')" || {
    log "refusing ledger retirement: cannot read the generation of ${uri}"
    return 1
  }
  case "$generation" in
    ''|*[!0-9]*)
      log "refusing ledger retirement: the generation of ${uri} is unreadable: '${generation}'"
      return 1
      ;;
  esac
  LEDGER_CONTROL_GENERATION="$generation"
  regional_quota_gc_read storage cat "$uri" >"$out" || {
    log "refusing ledger retirement: cannot read ${uri}"
    return 1
  }
}

# Uploads a control object only while it is still at the generation it was
# read at (0 = it must not exist yet). The upload's own output never reaches
# a caller that captures this function's result.
_ledger_write_control() {
  local file="$1" uri="$2" generation="$3"
  regional_quota_verify_control_lifecycle || return 1
  gc storage cp "$file" "$uri" --quiet --if-generation-match="$generation" >/dev/null
}

# 0 = a completed retirement marker for this project and database exists;
# 1 = no such marker; 2 = cannot tell.
ledger_retirement_completed() {
  regional_quota_verify_control_lifecycle || return 2
  local marker_file
  marker_file="$(mktemp "${TMPDIR:-/tmp}/ledger-marker.XXXXXX")" || return 2
  if ! _ledger_read_control "$(ledger_retirement_marker_uri)" "$marker_file"; then
    rm -f "$marker_file"
    return 2
  fi
  if [ ! -s "$marker_file" ]; then
    rm -f "$marker_file"
    return 1
  fi
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
' "$PROJECT_ID" "$SPANNER_INSTANCE_ID" "$SPANNER_DATABASE_ID" <"$marker_file"; then
    rm -f "$marker_file"
    return 0
  fi
  rm -f "$marker_file"
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

# tr_entities is keyed by kind and tr_reservation_by_expiry leads on settled
# (unsettled rows are the in-flight few, hold_usage_type is a back-join on
# that range). spend_lease_open has no index that covers this predicate: a
# dead row keeps phase='open' with next_attempt_at NULL, which the
# null-filtered due index cannot see, so the table is read the way its own
# reconciler reads it (phase/dead scans) - it is the single pilot
# workspace's working table, and done rows are purged by retention. Nothing
# here touches tr_entities.body.
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
    "SELECT COUNT(*) FROM spend_lease_open WHERE dead = true OR (phase != 'done' AND local_closed_at IS NULL)"
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

# Every revision that can still take requests in a region - the traffic
# split's members and every tagged revision - must satisfy each marker.
# `name=false` accepts an absent marker because config.py defaults these
# switches to false; `name=__absent__` requires the setting to be unset or
# empty (an app-profile map alone opens a ledger client). Records each
# region's fleet state (service generation + reachable revisions) in
# LEDGER_FLEET_STATE, one "region<TAB>generation<TAB>rev,rev" line per region.
# Exit 2 = a reachable revision serves a disallowed value (a known state);
# exit 1 = the fleet could not be read or is not reconciled (unknown).
LEDGER_FLEET_STATE=""
_ledger_serving_markers() {
  local purpose="$1"
  shift
  local -a regions
  IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
  local region service_json reachable generation names revision revision_json pair name expected value
  LEDGER_FLEET_STATE=""
  for region in "${regions[@]}"; do
    service_json="$(gc run services describe "$SERVICE" --region="$region" --format=json 2>/dev/null)" || {
      log "refusing ${purpose}: cannot read the service in ${region}"
      return 1
    }
    # Two lines: the service generation, then the reachable revision names.
    # (A tab-separated pair would lose an empty first field to `read`.)
    # status.traffic describes the last RECONCILED spec, so the status must
    # have caught up with the generation and no reconciliation may be in
    # progress; a reachable entry without a resolved revision is unreadable.
    parse_stderr="$(mktemp "${TMPDIR:-/tmp}/ledger-service.XXXXXX")"
    reachable="$(python3 -c '
import json, sys
service = json.load(sys.stdin)
metadata = service.get("metadata") or {}
status = service.get("status") or {}
generation = metadata.get("generation")
if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
    raise SystemExit("the service has no generation")
if status.get("observedGeneration") != generation:
    raise SystemExit("generation %s is not reconciled yet (observed %r)" % (generation, status.get("observedGeneration")))
for condition in status.get("conditions") or []:
    if isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") not in ("True", "False"):
        raise SystemExit("the service is still reconciling (Ready=%s)" % condition.get("status"))
names = set()
for item in status.get("traffic") or []:
    if not isinstance(item, dict):
        raise SystemExit("a traffic entry is not an object")
    percent = item.get("percent") or 0
    if not isinstance(percent, int) or isinstance(percent, bool) or percent < 0:
        raise SystemExit("a traffic entry has an unreadable percent")
    if percent == 0 and not item.get("tag"):
        continue
    name = item.get("revisionName")
    if not isinstance(name, str) or not name:
        raise SystemExit("a reachable traffic entry has no revisionName")
    names.add(name)
if not names:
    raise SystemExit("no reachable revision")
print(generation)
print(",".join(sorted(names)))
' <<<"$service_json" 2>"$parse_stderr")" || {
      log "refusing ${purpose}: ${region}: $(<"$parse_stderr")"
      rm -f "$parse_stderr"
      return 1
    }
    rm -f "$parse_stderr"
    { IFS= read -r generation; IFS= read -r names; } <<<"$reachable"
    if [ -z "$generation" ] || [ -z "$names" ]; then
      log "refusing ${purpose}: cannot read the fleet state of ${region}"
      return 1
    fi
    LEDGER_FLEET_STATE="${LEDGER_FLEET_STATE}${LEDGER_FLEET_STATE:+$'\n'}${region}	${generation}	${names}"
    for revision in ${names//,/ }; do
      revision_json="$(gc run revisions describe "$revision" --region="$region" --format=json 2>/dev/null)" || {
        log "refusing ${purpose}: cannot read revision ${revision} in ${region}"
        return 1
      }
      for pair in "$@"; do
        name="${pair%%=*}"
        expected="${pair#*=}"
        value="$(regional_quota_revision_env "$revision_json" "$name" __missing__)" || return 1
        if [ "$expected" = "__absent__" ]; then
          if [ "$value" != "__missing__" ] && [ -n "$value" ]; then
            log "refusing ${purpose}: ${region} still serves ${name}=${value} on ${revision}"
            return 2
          fi
        elif [ "$value" = "__missing__" ] && [ "$expected" = "false" ]; then
          log "${region}: ${name} is not set on ${revision}; it defaults to false"
        elif [ "$value" != "$expected" ]; then
          log "refusing ${purpose}: ${region} still serves ${name}=${value} on ${revision}"
          return 2
        fi
      done
    done
  done
}

ledger_drain_observation_uri() {
  printf 'gs://%s/controls/ledger-drain-observation.json\n' "$(ledger_retirement_bucket)"
}

ledger_gate_pass_uri() {
  printf 'gs://%s/controls/ledger-gate-pass.json\n' "$(ledger_retirement_bucket)"
}

# Merge the fleet state just verified into the durable observation and print
# the time the current state of the region that changed most recently was
# first seen. A region keeps its off_since only while its service generation
# and reachable revision set are unchanged; otherwise it starts over now.
# Two gates may write this concurrently (an unlocked one never holds the
# mutex); the write is fenced on the generation read, and a lost fence
# re-reads and merges again.
_ledger_observe_fleet() {
  local attempt merged
  for attempt in 1 2 3; do
    if merged="$(_ledger_observe_fleet_once)"; then
      printf '%s\n' "$merged"
      return 0
    fi
    [ "$attempt" -lt 3 ] || break
    log "the drain observation changed underneath this gate; reading it again (attempt ${attempt})"
  done
  log "refusing ledger retirement: cannot record the drain observation"
  return 1
}

_ledger_observe_fleet_once() {
  local record merged generation record_file
  record_file="$(mktemp "${TMPDIR:-/tmp}/ledger-observation.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_drain_observation_uri)" "$record_file"; then
    rm -f "$record_file"
    return 1
  fi
  generation="$LEDGER_CONTROL_GENERATION"
  record="$(cat "$record_file")"
  rm -f "$record_file"
  local merged_file
  merged_file="$(mktemp "${TMPDIR:-/tmp}/ledger-observation.XXXXXX")" || return 1
  merged="$(python3 - "$record" "$LEDGER_FLEET_STATE" "$merged_file" <<'PY'
import datetime as dt
import json
import sys

raw, state, out = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    old = json.loads(raw or "{}")
except ValueError:
    old = {}
regions = old.get("regions") if isinstance(old, dict) else None
if not isinstance(regions, dict):
    regions = {}
now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
new = {}
for line in state.splitlines():
    region, generation, names = line.split("\t")
    previous = regions.get(region) if isinstance(regions.get(region), dict) else {}
    unchanged = (
        str(previous.get("generation", "")) == generation
        and previous.get("revisions") == names
        and isinstance(previous.get("off_since"), str)
    )
    new[region] = {
        "generation": generation,
        "revisions": names,
        "off_since": previous["off_since"] if unchanged else now,
    }
with open(out, "w") as handle:
    json.dump({"regions": new, "updated_at": now}, handle)
print(max(entry["off_since"] for entry in new.values()))
PY
)" || {
    rm -f "$merged_file"
    log "refusing ledger retirement: cannot merge the drain observation"
    return 1
  }
  _ledger_write_control "$merged_file" "$(ledger_drain_observation_uri)" "$generation" || {
    rm -f "$merged_file"
    return 1
  }
  rm -f "$merged_file"
  printf '%s\n' "$merged"
}

# No Spanner count is trusted until every region has served only verified
# revisions for the drain interval, as the durable observation records it.
# Sets LEDGER_FLEET_QUIESCENT_IN to the seconds still to wait when refusing.
LEDGER_FLEET_QUIESCENT_IN=0
LEDGER_FLEET_OFF_SINCE=""
ledger_fleet_quiescent() {
  local interval="${TR_LEDGER_DRAIN_INTERVAL_SECONDS:-120}" off_since result
  LEDGER_FLEET_QUIESCENT_IN=0
  LEDGER_FLEET_OFF_SINCE=""
  off_since="$(_ledger_observe_fleet)" || return 1
  LEDGER_FLEET_OFF_SINCE="$off_since"
  result="$(python3 - "$off_since" "$interval" <<'PY'
import datetime as dt
import sys

since, interval = sys.argv[1], int(sys.argv[2])
try:
    changed = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(f"refusing ledger retirement: drain observation time {since!r} is unreadable")
if changed.tzinfo is None:
    changed = changed.replace(tzinfo=dt.UTC)
now = dt.datetime.now(dt.UTC)
ready = changed + dt.timedelta(seconds=interval)
remaining = max(0, int((ready - now).total_seconds()) + 1) if now < ready else 0
print(remaining)
if remaining:
    print(f"fleet state first seen at {changed.isoformat()}; wait until {ready.isoformat()} ({interval}s drain interval)", file=sys.stderr)
else:
    print(f"fleet quiescent since {changed.isoformat()} (+{interval}s drain interval)", file=sys.stderr)
PY
)" || return 1
  LEDGER_FLEET_QUIESCENT_IN="$result"
  if [ "$result" != "0" ]; then
    log "refusing ledger retirement: the fleet is not yet quiescent (${result}s to go)"
    return 1
  fi
}

ledger_issuance_off_everywhere() {
  _ledger_serving_markers "ledger retirement" \
    TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_BINDING_ENABLED=false \
    TR_SPEND_LEASE_ADMISSION_ACCEPT=false
}

# Everything the gate requires, plus no capability and no profile map (spend
# shadow issuance needs no profile map, so its switches are checked here too).
ledger_capability_off_everywhere() {
  _ledger_serving_markers "worker retirement" \
    TR_REGIONAL_QUOTA_LEASES_ENABLED=false \
    TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_ISSUANCE_ENABLED=false \
    TR_SPEND_LEASE_BINDING_ENABLED=false \
    TR_SPEND_LEASE_ADMISSION_ACCEPT=false \
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
# have reported REQUIRED all-zero passes, every one of them after the fleet
# state was first observed plus the drain interval. A paused or silent
# worker refuses: nobody is draining.
_ledger_reconciler_evidence() {
  local label="$1" state="$2" job_region="$3" job="$4" marker="$5"
  shift 5
  local required="${TR_LEDGER_DRAIN_REQUIRED_PASSES:-5}"
  local freshness="${TR_LEDGER_DRAIN_FRESHNESS:-15m}"
  local interval="${TR_LEDGER_DRAIN_INTERVAL_SECONDS:-120}"
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
  python3 - "$label" "$marker" "$required" "$json" "$LEDGER_FLEET_OFF_SINCE" "$interval" "$@" <<'PY'
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
    raise SystemExit(f"refusing ledger retirement: fleet observation time {newest!r} is unreadable")
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
        f"predates the drain cutoff {cutoff.isoformat()} (fleet state first seen + {interval}s)"
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

# 0 = the live production lock is held by this mutex operation and has not
# expired. The lock record is what deploy_mutex.sh writes; an inherited
# operation string alone proves nothing once the lock expired or was taken
# over.
_ledger_mutex_held_by() {
  local operation="$1" record
  record="$(regional_quota_gc_read storage cat "gs://$(ledger_retirement_bucket)/locks/trusted-router-production.json" 2>/dev/null)" || return 1
  python3 -c '
import datetime as dt, json, sys
try:
    record = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
if not isinstance(record, dict) or record.get("operation_id") != sys.argv[1]:
    raise SystemExit(1)
raw = record.get("expires_at")
try:
    expires = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else None
except ValueError:
    expires = None
if expires is None or expires.tzinfo is None:
    raise SystemExit(1)
raise SystemExit(0 if expires > dt.datetime.now(dt.UTC) else 1)
' "$operation" <<<"$record"
}

# 0 = the gate-pass record names this mutex operation. Only a gate that runs
# under an operation writes that record, so nothing an unlocked gate writes
# (the observation) can erase it.
_ledger_gate_passed_under() {
  local operation="$1" record_file status=0
  record_file="$(mktemp "${TMPDIR:-/tmp}/ledger-gate.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_gate_pass_uri)" "$record_file" || [ ! -s "$record_file" ]; then
    rm -f "$record_file"
    return 1
  fi
  python3 -c '
import json, sys
try:
    record = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(record, dict) and record.get("operation") == sys.argv[1] else 1)
' "$operation" <"$record_file" || status=$?
  rm -f "$record_file"
  return "$status"
}

# Publishes the pass for OPERATION, or records nothing (exit 0) when that
# operation no longer holds the production lock. Ownership is checked AFTER
# the generation read: a lease that runs out between the two would hand
# the fence to the new holder, whose record a generation read afterwards
# would overwrite.
_ledger_record_gate_pass() {
  local operation="$1" record generation
  record="$(mktemp "${TMPDIR:-/tmp}/ledger-gate.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_gate_pass_uri)" "$record"; then
    rm -f "$record"
    return 1
  fi
  generation="$LEDGER_CONTROL_GENERATION"
  if ! _ledger_mutex_held_by "$operation"; then
    rm -f "$record"
    log "not recording the gate pass: deployment operation ${operation} does not hold the production lock"
    return 0
  fi
  python3 -c '
import datetime as dt, json, sys
print(json.dumps({"operation": sys.argv[1], "passed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}))
' "$operation" >"$record"
  _ledger_write_control "$record" "$(ledger_gate_pass_uri)" "$generation" || {
    rm -f "$record"
    log "refusing ledger retirement: cannot record the gate pass"
    return 1
  }
  rm -f "$record"
}

# The release gate. Changes nothing but the drain observation. Exit 0 = the
# ledgers may be absent from the revisions this run creates. Under a
# deployment mutex operation the pass is recorded, and the parallel siblings
# of the release workflow stand on it instead of reading a fleet the others
# are already changing.
ledger_retirement_gate() {
  local operation="${TR_DEPLOY_MUTEX_OPERATION:-}"
  if [ -n "$operation" ] && _ledger_gate_passed_under "$operation"; then
    if _ledger_mutex_held_by "$operation"; then
      log "ledger retirement gate already passed under deployment operation ${operation}; while that lock is held the fleet changes only through this release's own revisions"
      return 0
    fi
    log "a gate pass is recorded under deployment operation ${operation} but the production lock is no longer held by it (expired or replaced); running the full gate"
  fi
  _ledger_retirement_gate_checks || return 1
  # Only the operation that holds the live lock may publish a pass: a
  # stale one whose checks happen to succeed must not overwrite the current
  # release's record.
  if [ -n "$operation" ]; then
    _ledger_record_gate_pass "$operation" || return 1
  fi
}

_ledger_retirement_gate_checks() {
  local status=0
  ledger_retirement_completed || status=$?
  case "$status" in
    0)
      status=0
      ledger_capability_off_everywhere || status=$?
      case "$status" in
        0)
          # The marker waives the worker evidence, never the quiescence or
          # Spanner checks: a temporary rollback could have created escrow
          # that outlived it.
          ledger_fleet_quiescent || return 1
          ledger_spanner_open_work || return 1
          log "ledger retirement is recorded as complete and every serving revision still runs without the ledgers"
          return 0
          ;;
        2) log "ledger retirement is recorded but a serving revision carries lease capability again; running the full gate" ;;
        *) return 1 ;;
      esac
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
      "spend_lease.reconcile_complete" candidates dead errors || return 1
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

# A worker's identity is "location/name": a job name alone would match an
# unrelated job of the same name in another region.
_LEDGER_TARGET_PATTERN='[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9-]{0,62}'

# The targets the schedules pointed at, recorded durably before any schedule
# is deleted, so a retry after an interrupted teardown still knows a custom
# target that matches neither prefix nor override. Absent = nothing recorded.
# Validated "location/name" lines from a targets record file; nothing when
# the file is empty (no record); fails on anything it cannot trust.
_ledger_targets_in() {
  [ -s "$1" ] || return 0
  python3 -c '
import json, re, sys
try:
    record = json.load(sys.stdin)
except ValueError:
    raise SystemExit("not JSON")
targets = record.get("targets") if isinstance(record, dict) else None
if not isinstance(targets, list):
    raise SystemExit("no targets list")
for target in targets:
    if not isinstance(target, str) or not re.fullmatch(sys.argv[1], target):
        raise SystemExit("invalid target %r" % (target,))
    print(target)
' "$_LEDGER_TARGET_PATTERN" <"$1" || {
    log "refusing worker retirement: the recorded worker targets are unreadable"
    return 1
  }
}

_ledger_recorded_targets() {
  local file status=0
  file="$(mktemp "${TMPDIR:-/tmp}/ledger-targets.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_retirement_targets_uri)" "$file"; then
    rm -f "$file"
    return 1
  fi
  _ledger_targets_in "$file" || status=$?
  rm -f "$file"
  return "$status"
}

# The record only ever grows: what an earlier run recorded is kept, so a
# fenced write that lands after another run's cannot lose a target.
_ledger_record_targets() {
  local record recorded generation existing
  existing="$(mktemp "${TMPDIR:-/tmp}/ledger-targets.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_retirement_targets_uri)" "$existing"; then
    rm -f "$existing"
    return 1
  fi
  generation="$LEDGER_CONTROL_GENERATION"
  recorded="$(_ledger_targets_in "$existing")" || {
    rm -f "$existing"
    return 1
  }
  rm -f "$existing"
  record="$(mktemp "${TMPDIR:-/tmp}/ledger-targets.XXXXXX")" || return 1
  if ! python3 -c '
import json, re, sys
targets = sorted(set(target for target in sys.argv[2:] + sys.stdin.read().split() if target))
for target in targets:
    if not re.fullmatch(sys.argv[1], target):
        raise SystemExit("invalid target %r" % (target,))
print(json.dumps({"targets": targets}))
' "$_LEDGER_TARGET_PATTERN" "$@" <<<"$recorded" >"$record"; then
    rm -f "$record"
    log "refusing worker retirement: cannot serialize the worker targets"
    return 1
  fi
  _ledger_write_control "$record" "$(ledger_retirement_targets_uri)" "$generation" || {
    rm -f "$record"
    log "refusing worker retirement: cannot record the worker targets"
    return 1
  }
  rm -f "$record"
}

# Project-wide worker inventory as "region<TAB>name" lines: every job under
# either historical prefix (a configured prefix must not hide the workers
# earlier releases created under the defaults), the exact "location/name"
# identities the deployers accept as overrides, and the recorded schedule
# targets. Project-wide so a retry after an interrupted teardown - schedules
# already gone - still finds a worker in a region only a schedule used to
# point at. A listing that warns (unreachable regions come back as a warning
# with exit 0) is incomplete and proves nothing, and so is a job whose name
# or location cannot be read; both refuse.
_ledger_worker_inventory() {
  local listing listing_stderr
  listing_stderr="$(mktemp "${TMPDIR:-/tmp}/ledger-jobs.XXXXXX")"
  listing="$(gc run jobs list --verbosity=warning --format=json 2>"$listing_stderr")" || {
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
  python3 -c '
import json, re, sys
pattern, regional_prefix, spend_prefix = sys.argv[1], sys.argv[2], sys.argv[3]
exact = set(target for target in sys.argv[4:] if re.fullmatch(pattern, target))
prefixes = tuple(prefix + "-" for prefix in (
    "trusted-router-regional-quota-reconciler", "trusted-router-spend-lease-reconciler", regional_prefix, spend_prefix))
jobs = json.load(sys.stdin)
if not isinstance(jobs, list):
    raise SystemExit("expected a job list")
for job in jobs:
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if not isinstance(metadata, dict):
        raise SystemExit("a listed job has no metadata")
    name = metadata.get("name")
    location = (metadata.get("labels") or {}).get("cloud.googleapis.com/location")
    if not isinstance(name, str) or not isinstance(location, str) or not re.fullmatch(pattern, location + "/" + name):
        raise SystemExit("a listed job has no usable name or location: %r in %r" % (name, location))
    if name.startswith(prefixes) or location + "/" + name in exact:
        print("%s\t%s" % (location, name))
' "$_LEDGER_TARGET_PATTERN" "$(_ledger_regional_job_prefix)" "$(_ledger_spend_job_prefix)" \
    "${TR_REGIONAL_QUOTA_RECONCILER_JOB_REGION:-us-east4}/${TR_REGIONAL_QUOTA_RECONCILER_JOB:-}" \
    "${TR_SPEND_LEASE_RECONCILER_JOB_REGION:-us-east4}/${TR_SPEND_LEASE_RECONCILER_JOB:-}" \
    "$@" <<<"$listing" || {
    log "refusing worker retirement: the Cloud Run job listing is unreadable"
    return 1
  }
}

# Under a deployment mutex operation (inherited from the workflow, or the
# one this process acquired), the production lock must still be held by it.
# Checked on entry and again before every destructive step and the marker:
# the quiescence and execution waits can outlast a lease.
_ledger_require_lock() {
  [ -n "${TR_DEPLOY_MUTEX_OPERATION:-}" ] || return 0
  _ledger_mutex_held_by "$TR_DEPLOY_MUTEX_OPERATION" && return 0
  log "refusing worker retirement: the deployment operation ${TR_DEPLOY_MUTEX_OPERATION} does not hold the production lock (expired or replaced); a re-run acquires its own"
  return 1
}

ledger_retire_workers() {
  local status=0 recorded_complete=false
  _ledger_require_lock || return 1
  ledger_retirement_completed || status=$?
  case "$status" in
    0) recorded_complete=true ;;
    1) ;;
    *) return 1 ;;
  esac
  # A held region still serves a capability-on revision with its profile
  # maps: the workers are harmless while nothing new can be issued, so leave
  # them for the next release rather than retiring under a live ledger. A
  # fleet that cannot be read is not that: the step fails.
  status=0
  ledger_capability_off_everywhere || status=$?
  case "$status" in
    0) ;;
    2)
      echo "::warning::ledger workers kept: a serving revision still carries lease capability; retirement retries on the next release"
      log "deferring worker retirement; the ledgers stay provisioned until every region serves a capability-off revision"
      return 0
      ;;
    *)
      log "refusing worker retirement: the fleet could not be verified"
      return 1
      ;;
  esac
  # The last secondary may have moved traffic moments ago: wait for the fleet
  # state found here to age out, under the mutex, re-reading the fleet each
  # time, rather than failing the release step. A fleet that changes again
  # during that wait would move the goal; that fails instead.
  local pause="${TR_LEDGER_RETIRE_RETRY_SLEEP_SECONDS:-10}" first_off_since="" nap
  until ledger_fleet_quiescent; do
    [ "$LEDGER_FLEET_QUIESCENT_IN" != "0" ] || return 1
    if [ -z "$first_off_since" ]; then
      first_off_since="$LEDGER_FLEET_OFF_SINCE"
    elif [ "$LEDGER_FLEET_OFF_SINCE" != "$first_off_since" ]; then
      log "refusing worker retirement: the fleet changed again while waiting for it to become quiescent"
      return 1
    fi
    nap="$LEDGER_FLEET_QUIESCENT_IN"
    [ "$nap" -le "$pause" ] || nap="$pause"
    log "waiting ${LEDGER_FLEET_QUIESCENT_IN}s for the fleet to become quiescent"
    sleep "$nap"
    ledger_capability_off_everywhere || return 1
  done
  _ledger_require_lock || return 1
  ledger_spanner_open_work || return 1
  # The names an interrupted earlier run recorded participate in every
  # inventory, the recorded-retirement re-check included.
  local -a targets=()
  local recorded job
  recorded="$(_ledger_recorded_targets)" || return 1
  while IFS= read -r job; do
    [ -n "$job" ] && targets+=("$job")
  done <<<"$recorded"
  if [ "$recorded_complete" = true ]; then
    # A recorded retirement is a no-op only once the current state agrees:
    # a rollback that recreated the schedules or workers gets torn down
    # again and the marker rewritten.
    local leftovers
    leftovers="$(_ledger_worker_inventory "${targets[@]+"${targets[@]}"}")" || return 1
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
  local scheduler region target job_region
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
    targets+=("${job_region}/${job}")
    live_schedules+=("${scheduler}|${region}|${job}|${job_region}")
  done
  if [ "${#live_schedules[@]}" -gt 0 ]; then
    _ledger_record_targets "${targets[@]+"${targets[@]}"}" || return 1
    # The record is a union with whatever another run added since the
    # first read; every inventory below works from that union.
    targets=()
    recorded="$(_ledger_recorded_targets)" || return 1
    while IFS= read -r job; do
      [ -n "$job" ] && targets+=("$job")
    done <<<"$recorded"
  fi
  local entry
  for entry in "${live_schedules[@]+"${live_schedules[@]}"}"; do
    IFS='|' read -r scheduler region job job_region <<<"$entry"
    _ledger_require_lock || return 1
    gc scheduler jobs delete "$scheduler" --location="$region" --quiet || {
      log "refusing worker retirement: cannot delete schedule ${scheduler} in ${region}"
      return 1
    }
    log "deleted schedule ${scheduler} in ${region} (target ${job} in ${job_region})"
  done

  local inventory deleted=0
  inventory="$(_ledger_worker_inventory "${targets[@]+"${targets[@]}"}")" || return 1
  # Every worker's running executions finish before any definition goes:
  # an execution a schedule started moments before its deletion can still
  # change Spanner (a spend reconciler marks a lease dead after enough
  # ledger timeouts), and the count below must see its last word.
  while IFS=$'\t' read -r region job; do
    [ -n "${region}${job}" ] || continue
    if [ -z "$region" ] || [ -z "$job" ]; then
      log "refusing worker retirement: an inventory line is incomplete: '${region}' '${job}'"
      return 1
    fi
    _ledger_wait_for_executions "$job" "$region" || return 1
  done <<<"$inventory"
  ledger_spanner_open_work || return 1
  while IFS=$'\t' read -r region job; do
    [ -n "${region}${job}" ] || continue
    _ledger_require_lock || return 1
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

  # Generation first, then ownership, then the fenced write: a lease that
  # runs out between the two hands the fence to the new holder.
  local record generation
  record="$(mktemp "${TMPDIR:-/tmp}/ledger-retirement.XXXXXX")" || return 1
  if ! _ledger_read_control "$(ledger_retirement_marker_uri)" "$record"; then
    rm -f "$record"
    return 1
  fi
  generation="$LEDGER_CONTROL_GENERATION"
  _ledger_require_lock || {
    rm -f "$record"
    return 1
  }
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
  _ledger_write_control "$record" "$(ledger_retirement_marker_uri)" "$generation" || {
    rm -f "$record"
    log "refusing to record retirement: cannot write the marker"
    return 1
  }
  rm -f "$record"
  log "ledger reconciler workers retired (${deleted} job(s) deleted); marker written to $(ledger_retirement_marker_uri)"
}
