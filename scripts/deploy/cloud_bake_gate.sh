#!/usr/bin/env bash
# Fleet-level bake ladder for operator-run AWS and Azure control-plane deploys.
# Source this file to call cloud_bake_gate CLOUD in the current shell, or run it
# directly as: bash scripts/deploy/cloud_bake_gate.sh CLOUD.

_cloud_bake_log() {
  printf '%s\n' "$*" >&2
}

_cloud_bake_valid_sha() {
  [[ "$1" =~ ^[0-9a-fA-F]{7,40}$ ]]
}

_cloud_bake_image_tag() {
  local image="$1"
  local tag
  # GCP deploys an immutable commit-tagged image. A digest-only reference does
  # not identify the source commit and must never be treated as one.
  case "$image" in
    *@*) return 1 ;;
    *:*) tag="${image##*:}" ;;
    *) return 1 ;;
  esac
  if ! _cloud_bake_valid_sha "$tag"; then
    return 1
  fi
  printf '%s\n' "$tag"
}

_cloud_bake_sha_has_prefix() {
  local sha
  local prefix
  sha="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  prefix="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
  case "$sha" in
    "$prefix"*) return 0 ;;
    *) return 1 ;;
  esac
}

_cloud_bake_azure_revision_release() {
  python3 -c '
import json
import sys

try:
    revisions = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(1)
if not isinstance(revisions, list):
    raise SystemExit(1)

serving = []
for revision in revisions:
    if not isinstance(revision, dict):
        continue
    properties = revision.get("properties")
    if not isinstance(properties, dict):
        continue
    try:
        traffic_weight = int(properties.get("trafficWeight") or 0)
    except (TypeError, ValueError):
        continue
    health = properties.get("healthState")
    if traffic_weight <= 0 or (
        health is not None and str(health).lower() != "healthy"
    ):
        continue
    serving.append(revision)

if not serving:
    raise SystemExit(1)
revision = max(
    serving,
    key=lambda item: (
        str(item.get("properties", {}).get("createdTime") or ""),
        str(item.get("name") or ""),
    ),
)
properties = revision.get("properties", {})
template = properties.get("template")
if not isinstance(template, dict):
    raise SystemExit(1)
containers = template.get("containers")
if not isinstance(containers, list) or not containers or not isinstance(containers[0], dict):
    raise SystemExit(1)
container = containers[0]
image = container.get("image")
if not isinstance(image, str):
    raise SystemExit(1)
release = ""
environment = container.get("env", [])
if isinstance(environment, list):
    for item in environment:
        if isinstance(item, dict) and item.get("name") == "TR_RELEASE":
            value = item.get("value")
            if isinstance(value, str):
                release = value
            break
print(image)
print(release)
'
}

_cloud_bake_serving_tag() {
  local cloud="$1"
  local value
  local arn
  local release
  local revision
  local script_dir
  local service_status
  local operation_status
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  case "$cloud" in
    gcp)
      # Evidence is the revision carrying traffic now, not the service
      # template. Rollback changes traffic without changing that template.
      if ! value="$(
        gcloud run services describe trusted-router \
          --region us-central1 \
          --project quill-cloud-proxy \
          --format=json 2>/dev/null
      )"; then
        return 1
      fi
      if ! revision="$(
        printf '%s' "$value" \
          | python3 "${script_dir}/resolve_active_revision.py" 2>/dev/null
      )"; then
        return 1
      fi
      if ! value="$(
        gcloud run revisions describe "$revision" \
          --region us-central1 \
          --project quill-cloud-proxy \
          --format='value(spec.containers[0].image)' 2>/dev/null
      )"; then
        return 1
      fi
      _cloud_bake_image_tag "$value"
      ;;
    azure)
      # Container App templates describe desired configuration. Select the
      # newest healthy revision carrying traffic now and read both artifact
      # fields from that same revision.
      if ! value="$(
        az containerapp revision list \
          --resource-group "${TR_CLOUD_BAKE_AZURE_RESOURCE_GROUP:-${RG:-tr-azure}}" \
          --name "${TR_CLOUD_BAKE_AZURE_APP:-${APP:-tr-azure-vnet}}" \
          --output json 2>/dev/null
      )"; then
        return 1
      fi
      if ! value="$(printf '%s' "$value" | _cloud_bake_azure_revision_release)"; then
        return 1
      fi
      release="${value#*$'\n'}"
      value="${value%%$'\n'*}"
      if _cloud_bake_image_tag "$value"; then
        return 0
      fi
      if [[ "$value" != *@sha256:* ]]; then
        return 1
      fi
      if ! _cloud_bake_valid_sha "$release"; then
        return 1
      fi
      printf '%s\n' "$release"
      ;;
    aws)
      # App Runner exposes desired state during an in-flight or failed
      # operation. Only a RUNNING service whose newest operation SUCCEEDED is
      # evidence of what carries traffic now.
      if ! arn="$(
        aws apprunner list-services \
          --region "${TR_CLOUD_BAKE_AWS_REGION:-${REGION:-eu-west-3}}" \
          --query "ServiceSummaryList[?ServiceName=='${TR_CLOUD_BAKE_AWS_SERVICE:-${SVC:-tr-eu}}'].ServiceArn | [0]" \
          --output text 2>/dev/null
      )"; then
        return 1
      fi
      if [ -z "$arn" ] || [ "$arn" = "None" ]; then
        return 1
      fi
      if ! service_status="$(
        aws apprunner describe-service \
          --region "${TR_CLOUD_BAKE_AWS_REGION:-${REGION:-eu-west-3}}" \
          --service-arn "$arn" \
          --query 'Service.Status' \
          --output text 2>/dev/null
      )" || [ "$service_status" != "RUNNING" ]; then
        return 1
      fi
      if ! operation_status="$(
        aws apprunner list-operations \
          --region "${TR_CLOUD_BAKE_AWS_REGION:-${REGION:-eu-west-3}}" \
          --service-arn "$arn" \
          --max-results 1 \
          --query 'OperationSummaryList[0].Status' \
          --output text 2>/dev/null
      )" || [ "$operation_status" != "SUCCEEDED" ]; then
        return 1
      fi
      if ! value="$(
        aws apprunner describe-service \
          --region "${TR_CLOUD_BAKE_AWS_REGION:-${REGION:-eu-west-3}}" \
          --service-arn "$arn" \
          --query 'Service.SourceConfiguration.ImageRepository.ImageIdentifier' \
          --output text 2>/dev/null
      )"; then
        return 1
      fi
      if _cloud_bake_image_tag "$value"; then
        return 0
      fi
      if [[ "$value" != *@sha256:* ]] || ! value="$(
        aws apprunner describe-service \
          --region "${TR_CLOUD_BAKE_AWS_REGION:-${REGION:-eu-west-3}}" \
          --service-arn "$arn" \
          --query 'Service.SourceConfiguration.ImageRepository.ImageConfiguration.RuntimeEnvironmentVariables.TR_RELEASE' \
          --output text 2>/dev/null
      )"; then
        return 1
      fi
      if ! _cloud_bake_valid_sha "$value"; then
        return 1
      fi
      printf '%s\n' "$value"
      ;;
    *) return 2 ;;
  esac
}

_cloud_bake_first_main_containing() {
  local repo_root="$1"
  local candidate="$2"
  local main_commit
  while IFS= read -r main_commit; do
    if git -C "$repo_root" merge-base --is-ancestor \
        "$candidate" "$main_commit"; then
      printf '%s\n' "$main_commit"
      return 0
    fi
  done < <(git -C "$repo_root" rev-list --first-parent --reverse origin/main)
  return 1
}

_cloud_bake_status_value() {
  python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    print("UNPARSEABLE")
    raise SystemExit(0)
if not isinstance(payload, dict):
    print("MISSING")
    raise SystemExit(0)
data = payload.get("data", payload)
if not isinstance(data, dict):
    print("MISSING")
    raise SystemExit(0)
print(str(data.get("overall_status") or "MISSING"))
'
}

_cloud_bake_fleet_healthy() {
  local attempt
  local payload
  local status
  for attempt in 1 2; do
    payload=""
    if payload="$(
      curl --fail --silent --show-error --max-time 10 \
        https://trustedrouter.com/status.json
    )"; then
      status="$(printf '%s' "$payload" | _cloud_bake_status_value)"
    else
      status="UNREACHABLE"
    fi
    if [ "$status" = "up" ]; then
      _cloud_bake_log \
        "fleet health: PASS overall_status=up attempt=${attempt}/2"
      return 0
    fi
    _cloud_bake_log \
      "fleet health attempt ${attempt}/2: not up (overall_status=${status})"
  done
  _cloud_bake_log "fleet health: FAIL (fail closed after one retry)"
  return 1
}

cloud_bake_gate() {
  local target_cloud="${1:-${TR_DEPLOY_MUTEX_CLOUD:-}}"
  local mode="${TR_CLOUD_DEPLOY_MODE:-promote}"
  local hours_raw="${TR_CLOUD_BAKE_HOURS:-24}"
  local override_reason="${TR_CLOUD_BAKE_OVERRIDE:-}"

  case "$target_cloud" in
    gcp|aws|azure) ;;
    *)
      _cloud_bake_log \
        "cloud_bake_gate.invalid_cloud cloud=${target_cloud:-missing} expected=gcp|aws|azure"
      return 2
      ;;
  esac
  case "$mode" in
    promote|canary) ;;
    *)
      _cloud_bake_log \
        "cloud_bake_gate.invalid_mode mode=${mode} expected=promote|canary"
      return 2
      ;;
  esac
  if [[ ! "$hours_raw" =~ ^[0-9]+$ ]]; then
    _cloud_bake_log \
      "cloud_bake_gate.invalid_bake_hours value=${hours_raw} expected=1..720"
    return 2
  fi
  local bake_hours=$((10#$hours_raw))
  if [ "$bake_hours" -lt 1 ] || [ "$bake_hours" -gt 720 ]; then
    _cloud_bake_log \
      "cloud_bake_gate.invalid_bake_hours value=${bake_hours} expected=1..720"
    return 2
  fi

  local script_dir
  local repo_root
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "${script_dir}/../.." && pwd)"
  local now
  now="$(date +%s)"
  local threshold_seconds=$((bake_hours * 3600))
  local failures=0

  local candidate=""
  local candidate_short="UNKNOWN"
  local candidate_subject="UNKNOWN"
  local candidate_epoch=""
  local candidate_age_seconds=-1
  local candidate_age_hours="UNKNOWN"
  if candidate="$(git -C "$repo_root" rev-parse --verify 'HEAD^{commit}' 2>/dev/null)" \
      && candidate_short="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null)" \
      && candidate_subject="$(git -C "$repo_root" log -1 --format=%s HEAD 2>/dev/null)" \
      && candidate_epoch="$(git -C "$repo_root" show -s --format=%ct HEAD 2>/dev/null)" \
      && [[ "$candidate_epoch" =~ ^[0-9]+$ ]]; then
    candidate_age_seconds=$((now - candidate_epoch))
    if [ "$candidate_age_seconds" -lt 0 ]; then
      candidate_age_seconds=0
    fi
    candidate_age_hours="$((candidate_age_seconds / 3600))"
    _cloud_bake_log "============================================================"
    _cloud_bake_log \
      "CANDIDATE ${candidate_short} | age=${candidate_age_hours}h | ${candidate_subject}"
    _cloud_bake_log "mode=${mode} target=${target_cloud} bake_requirement=${bake_hours}h"
    _cloud_bake_log "============================================================"
  else
    _cloud_bake_log "candidate: FAIL unable to resolve HEAD and commit metadata"
    failures=$((failures + 1))
  fi

  local fetch_ok=1
  if ! git -C "$repo_root" fetch --quiet origin main; then
    fetch_ok=0
    _cloud_bake_log \
      "serving commits: git fetch origin main failed; checkout may be stale or shallow"
  fi

  local clouds=(gcp azure aws)
  local serving_tags=(UNKNOWN UNKNOWN UNKNOWN)
  local serving_commits=(UNKNOWN UNKNOWN UNKNOWN)
  local serving_age_seconds=(-1 -1 -1)
  local serving_age_hours=(UNKNOWN UNKNOWN UNKNOWN)
  local index
  local tag
  local resolved
  local epoch
  for index in "${!clouds[@]}"; do
    if tag="$(_cloud_bake_serving_tag "${clouds[$index]}")"; then
      serving_tags[index]="$tag"
    else
      _cloud_bake_log \
        "${clouds[$index]} serving commit: UNKNOWN (cloud CLI/read failed or release tag is not a short sha)"
      continue
    fi
    if [ "$fetch_ok" -ne 1 ] || \
       ! resolved="$(
         git -C "$repo_root" rev-parse --verify "${tag}^{commit}" 2>/dev/null
       )"; then
      _cloud_bake_log \
        "${clouds[$index]} serving commit: UNKNOWN tag=${tag}; cannot resolve it. Fetch a full, current checkout (git fetch origin main; unshallow if needed)."
      continue
    fi
    # rev-parse prefers a ref over an abbreviated object name. A branch named
    # like a short SHA must not redirect traffic evidence to a different
    # commit, so the resolved object must retain the advertised SHA prefix.
    if ! _cloud_bake_sha_has_prefix "$resolved" "$tag"; then
      _cloud_bake_log \
        "${clouds[$index]} serving commit: UNKNOWN tag=${tag}; resolved SHA does not start with the advertised tag (possible ref shadowing)"
      continue
    fi
    if ! epoch="$(
      git -C "$repo_root" show -s --format=%ct "$resolved" 2>/dev/null
    )" || [[ ! "$epoch" =~ ^[0-9]+$ ]]; then
      _cloud_bake_log \
        "${clouds[$index]} serving commit: UNKNOWN tag=${tag}; commit time is unavailable"
      continue
    fi
    serving_commits[index]="$resolved"
    serving_age_seconds[index]=$((now - epoch))
    if [ "${serving_age_seconds[$index]}" -lt 0 ]; then
      serving_age_seconds[index]=0
    fi
    serving_age_hours[index]="$((serving_age_seconds[index] / 3600))"
  done

  _cloud_bake_log "SERVING COMMIT TABLE"
  _cloud_bake_log "cloud  serving_sha  age_hours  classification"
  local classification
  local age_display
  local lifeboat_index=-1
  for index in "${!clouds[@]}"; do
    classification="UNKNOWN"
    age_display="UNKNOWN"
    if [ "${serving_commits[$index]}" != "UNKNOWN" ]; then
      age_display="${serving_age_hours[$index]}h"
      if [ "$mode" = "canary" ] && [ "${clouds[$index]}" = "gcp" ]; then
        classification="fresh-exempt (gcp auto-deploys; ineligible as lifeboat)"
      elif [ "${clouds[$index]}" != "$target_cloud" ] \
          && [ "${serving_age_seconds[$index]}" -ge "$threshold_seconds" ]; then
        classification="LIFEBOAT"
        if [ "$lifeboat_index" -lt 0 ]; then
          lifeboat_index="$index"
        fi
      elif [ "${serving_age_seconds[$index]}" -lt "$threshold_seconds" ]; then
        classification="fresh"
      else
        classification="baked-target"
      fi
    fi
    _cloud_bake_log \
      "${clouds[$index]}  ${serving_tags[$index]}  ${age_display}  ${classification}"
  done

  if [ "$mode" = "promote" ]; then
    # Commit time is informational only: an operator can forge it with
    # GIT_COMMITTER_DATE. The authoritative proof is containment in the newest
    # first-parent origin/main commit that was itself committed at least Nh
    # ago. On this squash-linear main, that proves the candidate was merged by
    # then.
    _cloud_bake_log \
      "candidate commit age: INFO ${candidate_short} is ${candidate_age_hours}h old (not bake authority)"
    local old_main=""
    local old_main_short="UNKNOWN"
    if [ "$fetch_ok" -eq 1 ]; then
      old_main="$(
        git -C "$repo_root" rev-list origin/main --first-parent -n 1 \
          --min-age=$((now - threshold_seconds)) 2>/dev/null || true
      )"
    fi
    if [ -z "$old_main" ]; then
      _cloud_bake_log \
        "candidate merged age: FAIL origin/main has no commit at least ${bake_hours}h old (or origin/main is unavailable); fail closed"
      failures=$((failures + 1))
    elif [ -n "$candidate" ] && git -C "$repo_root" merge-base --is-ancestor \
        "$candidate" "$old_main"; then
      old_main_short="$(git -C "$repo_root" rev-parse --short "$old_main")"
      _cloud_bake_log \
        "candidate merged age: PASS candidate was present by origin/main ${old_main_short}, which is at least ${bake_hours}h old"
    else
      local merged_main=""
      local merged_epoch=""
      local merged_short="UNKNOWN"
      local remaining_seconds=""
      local remaining_hours="UNKNOWN"
      if [ -n "$candidate" ]; then
        merged_main="$(_cloud_bake_first_main_containing \
          "$repo_root" "$candidate" 2>/dev/null || true)"
      fi
      if [ -n "$merged_main" ]; then
        merged_short="$(git -C "$repo_root" rev-parse --short "$merged_main")"
        merged_epoch="$(
          git -C "$repo_root" show -s --format=%ct "$merged_main" 2>/dev/null \
            || true
        )"
      fi
      if [[ "$merged_epoch" =~ ^[0-9]+$ ]]; then
        remaining_seconds=$((merged_epoch + threshold_seconds - now))
        if [ "$remaining_seconds" -lt 0 ]; then
          remaining_seconds=0
        fi
        remaining_hours=$(((remaining_seconds + 3599) / 3600))
        _cloud_bake_log \
          "candidate merged age: FAIL first contained by origin/main ${merged_short} at epoch=${merged_epoch}; wait_remaining_hours=${remaining_hours} requires=${bake_hours}h"
      elif [ -n "$candidate" ]; then
        _cloud_bake_log \
          "candidate merged age: FAIL candidate is not contained in origin/main; requires ${bake_hours}h in first-parent main history"
      else
        _cloud_bake_log "candidate merged age: FAIL candidate is UNKNOWN"
      fi
      failures=$((failures + 1))
    fi

    local baked_cloud=""
    if [ -n "$candidate" ]; then
      for index in "${!clouds[@]}"; do
        if [ "${serving_commits[$index]}" != "UNKNOWN" ] \
            && git -C "$repo_root" merge-base --is-ancestor \
              "$candidate" "${serving_commits[$index]}"; then
          baked_cloud="${clouds[$index]}"
          break
        fi
      done
    fi
    # Lineage containment is the bake definition: an individual commit in a
    # deployed batch counts even when it never served alone.
    if [ -n "$baked_cloud" ]; then
      _cloud_bake_log \
        "production lineage: PASS candidate is contained in ${baked_cloud}'s serving code line"
    else
      _cloud_bake_log \
        "production lineage: FAIL candidate is not contained in any KNOWN serving cloud commit"
      failures=$((failures + 1))
    fi
  else
    if [ "$lifeboat_index" -ge 0 ]; then
      _cloud_bake_log \
        "lifeboat: PASS ${clouds[$lifeboat_index]} ${serving_tags[$lifeboat_index]} age=${serving_age_hours[$lifeboat_index]}h"
    else
      _cloud_bake_log \
        "lifeboat: FAIL no OTHER cloud has a KNOWN serving commit at least ${bake_hours}h old"
      failures=$((failures + 1))
    fi
  fi

  if ! _cloud_bake_fleet_healthy; then
    failures=$((failures + 1))
  fi

  if [ "$failures" -ne 0 ]; then
    if [ -n "$override_reason" ]; then
      _cloud_bake_log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      _cloud_bake_log \
        "CLOUD BAKE OVERRIDE: proceeding despite ${failures} failed check(s)"
      _cloud_bake_log "reason: ${override_reason}"
      _cloud_bake_log "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    else
      _cloud_bake_log \
        "cloud bake gate: REFUSED (${failures} failed check(s)); TR_CLOUD_BAKE_OVERRIDE requires a non-empty reason"
      return 1
    fi
  fi

  if [ "$mode" = "canary" ]; then
    local lifeboat_cloud="UNKNOWN"
    local lifeboat_sha="UNKNOWN"
    local lifeboat_age="UNKNOWN"
    if [ "$lifeboat_index" -ge 0 ]; then
      lifeboat_cloud="${clouds[$lifeboat_index]}"
      lifeboat_sha="${serving_tags[$lifeboat_index]}"
      lifeboat_age="${serving_age_hours[$lifeboat_index]}h"
    fi
    _cloud_bake_log "============================================================"
    _cloud_bake_log \
      "CANARY DEPLOY: ${target_cloud} will serve FRESH commit ${candidate_short} (${candidate_age_hours}h); lifeboat: ${lifeboat_cloud} at ${lifeboat_sha} (${lifeboat_age})"
    _cloud_bake_log "============================================================"
  else
    _cloud_bake_log \
      "PROMOTE DEPLOY: ${candidate_short} passed the ${bake_hours}h fleet bake gate"
  fi
  return 0
}

_cloud_bake_main() {
  if [ "$#" -gt 1 ]; then
    printf 'usage: %s [gcp|aws|azure]\n' "$0" >&2
    return 2
  fi
  cloud_bake_gate "${1:-${TR_DEPLOY_MUTEX_CLOUD:-}}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -u
  set -o pipefail
  _cloud_bake_main "$@"
  exit $?
fi
