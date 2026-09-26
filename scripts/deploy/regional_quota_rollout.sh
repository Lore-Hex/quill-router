# shellcheck shell=bash
# Regional-quota rollout interlock helpers.
#
# This file is sourced by rollout.sh and intentionally has no top-level cloud
# calls.  Keeping the state-resolution and fleet-preflight logic here makes the
# money-path switch executable under a recording fake rather than review-only.

_regional_quota_exact_service_not_found() {
  local message="$1"
  local line
  while IFS= read -r line; do
    # Only the describe command saying that this exact service is absent means
    # "fresh environment".  A missing revision, denied read, expired login, or
    # any other error must not be converted into feature defaults.
    if [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [${SERVICE}]"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) NOT_FOUND: Service '${SERVICE}'"* ]] ||
       [[ "$line" == "ERROR: (gcloud.run.services.describe) Service [${SERVICE}] could not be found." ]]; then
      return 0
    fi
  done <<<"$message"
  return 1
}

regional_quota_active_revision_json() {
  local region="$1"
  local allow_fresh_environment="${2:-false}"
  local service_json
  local status=0

  service_json="$(
    gc run services describe "$SERVICE" \
      --region="$region" \
      --format=json 2>&1
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    if [ "$allow_fresh_environment" = "true" ] &&
       _regional_quota_exact_service_not_found "$service_json"; then
      return 3
    fi
    log "refusing regional quota rollout: cannot read service ${SERVICE} in ${region}: ${service_json}"
    return 1
  fi

  local active_revision
  if ! active_revision="$(python3 -c '
import json
import sys

service = json.load(sys.stdin)
traffic = [
    item
    for item in service.get("status", {}).get("traffic", [])
    if int(item.get("percent") or 0) > 0
]
if len(traffic) != 1 or int(traffic[0].get("percent") or 0) != 100:
    raise SystemExit("expected exactly one 100%-traffic revision")
revision = traffic[0].get("revisionName")
if not isinstance(revision, str) or not revision:
    raise SystemExit("100%-traffic entry has no revisionName")
print(revision)
' <<<"$service_json")"; then
    log "refusing regional quota rollout: ${SERVICE} in ${region} has ambiguous active traffic"
    return 1
  fi

  # Deliberately describe the traffic revision, never latestCreatedRevisionName,
  # latestReadyRevisionName, or the service template.  After a rollback those
  # all can point at the rejected candidate while 100% traffic serves the safe
  # predecessor.
  local revision_json
  status=0
  revision_json="$(
    gc run revisions describe "$active_revision" \
      --region="$region" \
      --format=json 2>&1
  )" || status=$?
  if [ "$status" -ne 0 ]; then
    log "refusing regional quota rollout: cannot read active revision ${active_revision} in ${region}: ${revision_json}"
    return 1
  fi
  printf '%s\n' "$revision_json"
}

regional_quota_revision_env() {
  local revision_json="$1"
  local name="$2"
  local default_value="${3:-}"
  python3 -c '
import json
import sys

name = sys.argv[1]
default = sys.argv[2]
revision = json.load(sys.stdin)
matches = [
    item
    for item in revision.get("spec", {}).get("containers", [{}])[0].get("env", [])
    if item.get("name") == name
]
if len(matches) > 1:
    raise SystemExit(f"duplicate environment variable: {name}")
if not matches:
    print(default)
else:
    item = matches[0]
    if "valueFrom" in item:
        raise SystemExit(f"environment variable is not a plain value: {name}")
    value = item.get("value", "")
    if not isinstance(value, str):
        raise SystemExit(f"environment variable is not a plain value: {name}")
    print(value)
' "$name" "$default_value" <<<"$revision_json"
}

regional_quota_normalize_issuance_control() {
  local raw_control="$1"
  local live_value="$2"
  local effective_value=""

  case "$raw_control" in
    ""|preserve) effective_value="$live_value" ;;
    true|false) effective_value="$raw_control" ;;
    *)
      log "refusing rollout: regional quota issuance input must be preserve, true, or false"
      return 1
      ;;
  esac
  case "$effective_value" in
    true|false) printf '%s\n' "$effective_value" ;;
    *)
      log "refusing rollout: active TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED must be true or false"
      return 1
      ;;
  esac
}

regional_quota_migrate_legacy_pilot() {
  local live_workspaces="$1"
  # The original synthetic account has no payment history and correctly fails
  # the shared lease trust gate. Use the existing paid first-party smoke pilot,
  # not a trust exemption. Preserve custom allowlists and issuance-off state.
  case "$live_workspaces" in
    d385c399-b245-4147-a528-0a4f6f170c71)
      printf '%s\n' '45819281-0ce9-4811-a0cd-c660ab3a116d'
      ;;
    *) printf '%s\n' "$live_workspaces" ;;
  esac
}

regional_quota_preflight_issuance_fleet() {
  local raw_regions="${TR_CONTROL_PLANE_REGIONS:-}"
  if [ -z "$raw_regions" ]; then
    log "refusing regional quota issuance: TR_CONTROL_PLANE_REGIONS is empty"
    return 1
  fi

  local previous_ifs="$IFS"
  IFS=','
  # Bash 3.2-compatible indexed array; do not use readarray/mapfile here.
  local regions
  read -ra regions <<<"$raw_regions"
  IFS="$previous_ifs"

  local region
  for region in "${regions[@]}"; do
    if [ -z "$region" ]; then
      log "refusing regional quota issuance: control-plane region list has an empty entry"
      return 1
    fi

    local revision_json
    if ! revision_json="$(regional_quota_active_revision_json "$region" false)"; then
      return 1
    fi

    local capability
    if ! capability="$(
      regional_quota_revision_env \
        "$revision_json" \
        "TR_REGIONAL_QUOTA_LEASES_ENABLED" \
        "__missing__"
    )"; then
      log "refusing regional quota issuance: cannot read capability marker in ${region}"
      return 1
    fi
    if [ "$capability" != "true" ]; then
      log "refusing regional quota issuance: active ${region} revision does not declare TR_REGIONAL_QUOTA_LEASES_ENABLED=true"
      return 1
    fi

    local issuance_marker
    if ! issuance_marker="$(
      regional_quota_revision_env \
        "$revision_json" \
        "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" \
        "__missing__"
    )"; then
      log "refusing regional quota issuance: cannot read issuance marker in ${region}"
      return 1
    fi
    case "$issuance_marker" in
      true|false) ;;
      __missing__)
        log "refusing regional quota issuance: active ${region} revision lacks TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
        return 1
        ;;
      *)
        log "refusing regional quota issuance: active ${region} revision has a non-boolean TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
        return 1
        ;;
    esac
    regional_quota_require_protocol "$revision_json" || return 1
    log "regional quota issuance compatibility: ${region}=capable, marker=${issuance_marker}"
  done
}

# Accounting compatibility is independent of the git release being deployed.
# _lib.sh owns the protocol constant used by both serving revisions and workers.
regional_quota_require_protocol() {
  local revision_json="$1"
  local protocol
  protocol="$(regional_quota_revision_env "$revision_json" REGIONAL_QUOTA_ACCOUNTING_PROTOCOL)" || return 1
  if [ "$protocol" != "$REGIONAL_QUOTA_ACCOUNTING_PROTOCOL" ]; then
    log "refusing regional quota issuance: incompatible accounting protocol ${protocol:-missing}; expected ${REGIONAL_QUOTA_ACCOUNTING_PROTOCOL}"
    return 1
  fi
}

regional_quota_stop_uri() {
  printf 'gs://%s/controls/regional-quota-issuance.txt\n' \
    "${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}"
}

# Bound CLI reads even if credentials/transport hang. Preserve stderr and status;
# the function definition comes from this deploy process, never remote input.
regional_quota_gc_read() {
  export PROJECT_ID
  python3 -c '
import os, signal, subprocess, sys
command = sys.argv[1] + "\ngc \"$@\""
p = subprocess.Popen(["/bin/bash", "-c", command, "gc", *sys.argv[3:]], start_new_session=True)
try:
    raise SystemExit(p.wait(timeout=min(20, float(sys.argv[2]))))
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    raise SystemExit("refusing regional quota read: command exceeded 20 seconds")
' "$(declare -f gc)" "${REGIONAL_QUOTA_READ_TIMEOUT:-20}" "$@"
}

# Reject every Delete rule whose prefix could intersect controls/. Other
# conditions (age, suffix, storage class) cannot make that durable forever.
regional_quota_verify_control_lifecycle() {
  local bucket_json
  bucket_json="$(regional_quota_gc_read storage buckets describe "gs://${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}" --format=json)" || {
    log "refusing regional quota rollout: cannot read control bucket lifecycle"
    return 1
  }
  if ! python3 -c '
import json, sys
b = json.load(sys.stdin)
# gcloud uses lifecycle_config; accept the JSON API lifecycle spelling too.
lifecycle = b.get("lifecycle_config", b.get("lifecycle", {}))
for rule in lifecycle.get("rule", []):
    if rule["action"]["type"] != "Delete":
        continue
    prefixes = rule.get("condition", {}).get("matchesPrefix", [])
    if not prefixes or any("controls/".startswith(p) or p.startswith("controls/") for p in prefixes):
        raise SystemExit("Delete lifecycle rule could expire controls/")
' <<<"$bucket_json"; then
    log "refusing regional quota rollout: unsafe or unreadable control bucket lifecycle"
    return 1
  fi
}

regional_quota_persist_stop() {
  regional_quota_verify_control_lifecycle || return 1
  local record
  record="$(mktemp)" || return 1
  printf 'off\n' >"$record"
  local result=0
  gc storage cp "$record" "$(regional_quota_stop_uri)" --quiet || result=$?
  rm -f "$record"
  return "$result"
}

regional_quota_apply_stop_latch() {
  local requested="$1"
  regional_quota_verify_control_lifecycle || return 1
  local latch listing present
  # One reserved prefix containing one control object. No recursive/bucket scan
  # and no CLI stderr parsing: failures, including missing buckets, abort.
  listing="$(regional_quota_gc_read storage objects list --raw --format=json "gs://${TR_DEPLOY_MUTEX_BUCKET:-tr-deploy-mutex-quill-cloud-proxy}/controls/*")" || {
    log "refusing regional quota rollout: cannot list stop latch"
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
' "$(regional_quota_stop_uri)" <<<"$listing")" || {
    log "refusing regional quota rollout: cannot parse stop latch listing"
    return 1
  }
  if [ "$present" = false ]; then
    printf '%s\n' "$requested"
    return 0
  fi
  latch="$(regional_quota_gc_read storage cat "$(regional_quota_stop_uri)")" || {
    log "refusing regional quota rollout: cannot read stop latch"
    return 1
  }
  case "$latch" in
    allow) printf '%s\n' "$requested" ;;
    *)
      if [ "$latch" != "off" ]; then
        log "regional quota stop latch has invalid content; treating as off"
      fi
      log "regional quota durable stop latch forces issuance off"
      printf 'false\n'
      ;;
  esac
}

regional_quota_reconciler_prefix() {
  printf '%s\n' "${TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX:-trusted-router-regional-quota-reconciler}"
}

regional_quota_reconciler_name() {
  printf '%s\n' "${TR_REGIONAL_QUOTA_RECONCILER_JOB:-$(regional_quota_reconciler_prefix)-$1}"
}

regional_quota_preflight_reconciler() {
  local scheduler="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
  local scheduler_region="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
  local job_region="${TR_REGIONAL_QUOTA_RECONCILER_JOB_REGION:-us-east4}"
  local scheduler_json job_name job_json revision_json execution_name execution_json evidence
  if ! scheduler_json="$(gc scheduler jobs describe "$scheduler" --location="$scheduler_region" --format=json 2>&1)"; then
    log "refusing regional quota issuance: cannot read reconciler schedule: ${scheduler_json}"
    return 1
  fi
  job_name="$(python3 -c '
import json, re, sys
s = json.load(sys.stdin)
target = s.get("httpTarget", {})
prefix = f"https://{sys.argv[1]}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{sys.argv[2]}/jobs/"
uri = target.get("uri", "")
if s.get("state") != "ENABLED":
    raise SystemExit("refusing regional quota issuance: reconciler schedule is not ENABLED")
if (not uri.startswith(prefix) or not uri.endswith(":run")
        or target.get("httpMethod") != "POST"
        or target.get("oauthToken", {}).get("serviceAccountEmail") != sys.argv[3]):
    raise SystemExit("refusing regional quota issuance: unverified reconciler target")
job = uri[len(prefix):-4]
if (not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", job)
        or (job != sys.argv[4] if sys.argv[4] else
            not re.fullmatch(re.escape(sys.argv[5]) + r"-[a-z0-9-]+", job))):
    raise SystemExit("refusing regional quota issuance: unexpected reconciler job")
print(job)
' "$job_region" "$PROJECT_ID" "$RUN_SERVICE_ACCOUNT" "${TR_REGIONAL_QUOTA_RECONCILER_JOB:-}" "$(regional_quota_reconciler_prefix)" <<<"$scheduler_json")" || return 1
  if ! job_json="$(gc run jobs describe "$job_name" --region="$job_region" --format=json 2>&1)"; then
    log "refusing regional quota issuance: cannot read reconciler worker: ${job_json}"
    return 1
  fi
  revision_json="$(python3 -c '
import json, sys
job = json.load(sys.stdin)
print(json.dumps(job["spec"]["template"]["spec"]["template"]))
' <<<"$job_json")" || return 1
  regional_quota_require_protocol "$revision_json" || return 1
  execution_name="$(python3 -c '
import json, sys
j = json.load(sys.stdin)
s = j.get("status", {})
if str(s.get("observedGeneration")) != str(j["metadata"]["generation"]):
    raise SystemExit("refusing regional quota issuance: unobserved worker generation")
if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in s.get("conditions", [])):
    raise SystemExit("refusing regional quota issuance: worker not ready")
e = s.get("latestCreatedExecution", {})
if e.get("completionStatus") not in ("EXECUTION_SUCCEEDED", "EXECUTION_RUNNING", "EXECUTION_PENDING"):
    raise SystemExit("refusing regional quota issuance: latest worker execution did not succeed")
print(e["name"])
' <<<"$job_json")" || return 1
  local in_flight executions attempt candidate deadline remaining pause_seconds
  in_flight="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["latestCreatedExecution"]["completionStatus"])' <<<"$job_json")"
  if [ "$in_flight" != EXECUTION_SUCCEEDED ]; then
    # At one run/minute, ten records cover the five-minute evidence window.
    # Wait at most 90 seconds only when no recent completed run is available.
    attempt=0
    deadline=$((SECONDS + 90))
    while :; do
      remaining=$((deadline - SECONDS))
      if [ "$remaining" -le 0 ]; then
        log "refusing regional quota issuance: no recent completed worker execution after bounded wait"
        return 1
      fi
      executions="$(REGIONAL_QUOTA_READ_TIMEOUT="$remaining" regional_quota_gc_read run jobs executions list --job="$job_name" --region="$job_region" --limit=10 --sort-by=~metadata.creationTimestamp --format=json)" || return 1
      candidate="$(python3 -c '
import json, sys
from datetime import datetime, timezone
runs = json.load(sys.stdin)
completed = [e for e in runs if e.get("status", {}).get("completionTime")]
completed.sort(key=lambda e: e["status"]["completionTime"], reverse=True)
if completed:
    e = completed[0]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(e["status"]["completionTime"].replace("Z", "+00:00"))).total_seconds()
    if 0 <= age <= 300:
        print(e["metadata"]["name"])
' <<<"$executions")" || return 1
      if [ -n "$candidate" ]; then
        execution_name="$candidate"
        break
      fi
      remaining=$((deadline - SECONDS))
      if [ "$attempt" -ge 9 ] || [ "$remaining" -le 0 ]; then
        log "refusing regional quota issuance: no recent completed worker execution after bounded wait"
        return 1
      fi
      pause_seconds=10
      if [ "$remaining" -lt "$pause_seconds" ]; then pause_seconds="$remaining"; fi
      sleep "$pause_seconds"
      attempt=$((attempt + 1))
    done
  fi
  if ! execution_json="$(gc run jobs executions describe "$execution_name" --region="$job_region" --format=json 2>&1)"; then
    log "refusing regional quota issuance: cannot read reconciler execution: ${execution_json}"
    return 1
  fi
  python3 -c '
import json, sys
from datetime import datetime, timezone
j, e = map(json.loads, sys.argv[1:])
s = e.get("status", {})
completed = datetime.fromisoformat(s.get("completionTime", "").replace("Z", "+00:00"))
age = (datetime.now(timezone.utc) - completed).total_seconds()
if not 0 <= age <= 300:
    raise SystemExit("refusing regional quota issuance: worker success is not recent")
if not any(c.get("type") == "Completed" and c.get("status") == "True" for c in s.get("conditions", [])):
    raise SystemExit("refusing regional quota issuance: worker execution failed")
if e["spec"]["template"]["spec"] != j["spec"]["template"]["spec"]["template"]["spec"]:
    raise SystemExit("refusing regional quota issuance: successful execution used a different worker configuration")
' "$job_json" "$execution_json" || return 1
  # A single-flight skip also exits zero. Require the actual reconciliation
  # completion log from this execution, not merely a successful jobs:run RPC.
  evidence="$(gc logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=\"${job_name}\" AND resource.labels.location=\"${job_region}\" AND labels.\"run.googleapis.com/execution_name\"=\"${execution_name}\" AND textPayload:\"regional_quota.reconciler_complete elapsed_ms=\"" \
    --freshness=5m --limit=1 --format=json)" || return 1
  python3 -c '
import json, sys
if not json.load(sys.stdin):
    raise SystemExit("refusing regional quota issuance: no recent successful reconciliation evidence")
' <<<"$evidence"
}
