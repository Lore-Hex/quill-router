#!/usr/bin/env bash
# Stage the six GCP web surfaces without changing production traffic.
#
# This command reconciles the six LB backends/NEGs/Armor policies, deploys an
# exact fail-closed revision for every surface and region without changing a
# preexisting service's traffic,
# verifies Cloud Run and application startup postconditions, and writes a
# non-secret recovery manifest plus prior/candidate URL-map snapshots.  It does
# not import the candidate URL map or promote a revision.  Promotion and
# rollback are owned by rollout_rollback.sh.

set -euo pipefail

usage() {
  echo "Usage: bash scripts/deploy/rollout.sh --manifest PATH" >&2
  exit 2
}

[ "$#" -eq 2 ] || usage
[ "$1" = "--manifest" ] || usage
MANIFEST="$2"
[ -n "$MANIFEST" ] || usage

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"
# shellcheck source=scripts/deploy/_edge_security.sh
source "${SCRIPT_DIR}/_edge_security.sh"

STATE_TOOL="${SCRIPT_DIR}/rollout_state.py"
URL_MAP_TOOL="${SCRIPT_DIR}/service_surface_url_map.py"
IAM_VERIFY="${SCRIPT_DIR}/rollout_iam_verify.sh"
STAGE_OPERATION_ID="${TR_ROLLOUT_OPERATION_ID:-}"
if ! [[ "$STAGE_OPERATION_ID" =~ ^[A-Za-z0-9._:-]{8,160}$ ]]; then
  echo "ERROR: staging requires a canonical TR_ROLLOUT_OPERATION_ID" >&2
  exit 2
fi
MANIFEST_DIR="$(mkdir -p "$(dirname "$MANIFEST")" && cd "$(dirname "$MANIFEST")" && pwd)"
MANIFEST="${MANIFEST_DIR}/$(basename "$MANIFEST")"
STAGE_OPERATION_LOCK="${TR_ROLLOUT_LOCAL_LOCK_PATH:-${TMPDIR:-/tmp}/trusted-router-${PROJECT_ID}.stage.lock}"
if [ "${TR_ROLLOUT_LOCAL_LOCK_HELD:-}" != "$STAGE_OPERATION_LOCK" ]; then
  export TR_ROLLOUT_LOCAL_LOCK_HELD="$STAGE_OPERATION_LOCK"
  exec python3 "${SCRIPT_DIR}/rollout_local_lock.py" "$STAGE_OPERATION_LOCK" -- \
    /bin/bash "$0" --manifest "$MANIFEST"
fi
STAGE_JOURNAL="${MANIFEST}.stage.state"
LEGACY_HARDENING_ARTIFACT_NAME="$(basename "$MANIFEST").legacy-hardening.json"
FRONTEND_ATTESTATION_NAME="$(basename "$MANIFEST").frontend-attestation.json"
LEGACY_HARDENING_ARTIFACT="${MANIFEST_DIR}/${LEGACY_HARDENING_ARTIFACT_NAME}"
FRONTEND_ATTESTATION="${MANIFEST_DIR}/${FRONTEND_ATTESTATION_NAME}"
PRIOR_URL_MAP_NAME="url-map.prior.json"
CANDIDATE_URL_MAP_NAME="url-map.candidate.json"
PRIOR_URL_MAP="${MANIFEST_DIR}/${PRIOR_URL_MAP_NAME}"
CANDIDATE_URL_MAP="${MANIFEST_DIR}/${CANDIDATE_URL_MAP_NAME}"

RESUMING_INITIAL_STAGE=false
if [ -e "$STAGE_JOURNAL" ] || [ -L "$STAGE_JOURNAL" ]; then
  RESUMING_INITIAL_STAGE=true
fi
if [ -e "$MANIFEST" ] && [ "$RESUMING_INITIAL_STAGE" != true ]; then
  echo "ERROR: refusing to overwrite existing rollout manifest ${MANIFEST}" >&2
  exit 1
fi
for recovery_artifact in \
  "$PRIOR_URL_MAP" "$CANDIDATE_URL_MAP" \
  "$LEGACY_HARDENING_ARTIFACT" "$FRONTEND_ATTESTATION"; do
  if [ -e "$recovery_artifact" ]; then
    if [ "$RESUMING_INITIAL_STAGE" != true ]; then
      echo "ERROR: refusing to overwrite existing rollout recovery artifact ${recovery_artifact}" >&2
      exit 1
    fi
  fi
done
[ ! -e "${MANIFEST_DIR}/promotion-state.json" ] || {
  echo "ERROR: staging cannot resume after a promotion state exists" >&2
  exit 1
}
for command_name in gcloud jq python3; do
  command -v "$command_name" >/dev/null || {
    echo "ERROR: required command is missing: ${command_name}" >&2
    exit 1
  }
done
[ -x "$IAM_VERIFY" ] || {
  echo "ERROR: six-runtime IAM verifier is missing or not executable: ${IAM_VERIFY}" >&2
  exit 1
}

DOMAINS="trustedrouter.com,allyrouter.com,uptimerouter.com"
# Every host below must already have an explicit hostRule. The transformer
# preserves its matcher/backend byte-for-byte and refuses a wildcard/default
# dependency, so the six web services can never steal an API, AWS, or Azure
# hostname during the atomic URL-map import.
REQUIRED_PRESERVED_HOSTS="api.trustedrouter.com,api.allyrouter.com,api.uptimerouter.com,api.quillrouter.com,api-aws.trustedrouter.com,api-azure.trustedrouter.com,api-azure-nz.trustedrouter.com,api-azure-sea.trustedrouter.com,api-eu-west-1.trustedrouter.com,aws.trustedrouter.com,azure.trustedrouter.com"

HTTPS_PROXY="${TR_HTTPS_PROXY:-trusted-router-control-https-proxy}"
[[ "$HTTPS_PROXY" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
  echo "ERROR: TR_HTTPS_PROXY is not a canonical resource name" >&2
  exit 1
}
URL_MAP_NAME="$(gc compute target-https-proxies describe "$HTTPS_PROXY" \
  --global --format='value(urlMap.basename())')"
[ -n "$URL_MAP_NAME" ] || {
  echo "ERROR: HTTPS proxy ${HTTPS_PROXY} has no URL map" >&2
  exit 1
}
[[ "$URL_MAP_NAME" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
  echo "ERROR: HTTPS proxy returned an invalid URL-map name" >&2
  exit 1
}

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tr-six-surface-stage-XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
ENTRIES_FILE="${WORK_DIR}/services.jsonl"
: >"$ENTRIES_FILE"
LEGACY_FALLBACK_FILE="${WORK_DIR}/legacy-fallback.jsonl"
: >"$LEGACY_FALLBACK_FILE"
SECRET_VERSIONS_FILE="${WORK_DIR}/secret-versions.tsv"
: >"$SECRET_VERSIONS_FILE"
chmod 600 "$SECRET_VERSIONS_FILE"

RELEASE="${TR_RELEASE:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
read_stage_journal_suffix() {
  python3 - "$STAGE_JOURNAL" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("initial-stage journal must be a regular mode-0600 file")
value = json.loads(path.read_text(encoding="utf-8"))
suffix = value.get("revision_suffix")
if not isinstance(suffix, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,34}[a-z0-9]", suffix):
    raise SystemExit("initial-stage journal revision suffix is invalid")
print(suffix)
PY
}
if [ -n "${TR_ROLLOUT_REVISION_SUFFIX:-}" ]; then
  REVISION_SUFFIX="$TR_ROLLOUT_REVISION_SUFFIX"
elif [ "$RESUMING_INITIAL_STAGE" = true ]; then
  REVISION_SUFFIX="$(read_stage_journal_suffix)"
else
  REVISION_SUFFIX="r$(date -u +%Y%m%d%H%M%S)-${GITHUB_RUN_ATTEMPT:-0}"
fi
if ! [[ "$REVISION_SUFFIX" =~ ^[a-z][a-z0-9-]{0,34}[a-z0-9]$ ]]; then
  echo "ERROR: TR_ROLLOUT_REVISION_SUFFIX must be a canonical revision suffix" >&2
  exit 1
fi
if [ "${#REVISION_SUFFIX}" -gt 36 ]; then
  echo "ERROR: rollout revision suffix is too long" >&2
  exit 1
fi

IFS=',' read -r -a REGIONS <<<"$TR_CONTROL_PLANE_REGIONS"
[ "${#REGIONS[@]}" -gt 0 ] || { echo "ERROR: no control-plane regions configured" >&2; exit 1; }
SEEN_REGIONS="|"
for region in "${REGIONS[@]}"; do
  [[ "$region" =~ ^[a-z]+-[a-z0-9]+[0-9]$ ]] || {
    echo "ERROR: invalid control-plane region: ${region:-<empty>}" >&2
    exit 1
  }
  case "$SEEN_REGIONS" in *"|${region}|"*)
    echo "ERROR: duplicate control-plane region: ${region}" >&2; exit 1 ;;
  esac
  SEEN_REGIONS="${SEEN_REGIONS}${region}|"
done
REGION_CSV="$(IFS=,; echo "${REGIONS[*]}")"
if [ -n "${TR_DEPLOY_TARGET_REGIONS:-}" ] && [ "$TR_DEPLOY_TARGET_REGIONS" != "$REGION_CSV" ]; then
  echo "ERROR: six-surface staging requires the complete configured regional inventory" >&2
  exit 1
fi
IFS=',' read -r -a GATEWAY_REGIONS <<<"$TR_REGIONS"
[ "${#GATEWAY_REGIONS[@]}" -gt 0 ] || { echo "ERROR: no gateway regions configured" >&2; exit 1; }
SEEN_GATEWAY_REGIONS="|"
for gateway_region in "${GATEWAY_REGIONS[@]}"; do
  [[ "$gateway_region" =~ ^[a-z]+-[a-z0-9]+[0-9]$ ]] || {
    echo "ERROR: invalid gateway region: ${gateway_region:-<empty>}" >&2
    exit 1
  }
  case "$SEEN_GATEWAY_REGIONS" in *"|${gateway_region}|"*)
    echo "ERROR: duplicate gateway region: ${gateway_region}" >&2; exit 1 ;;
  esac
  SEEN_GATEWAY_REGIONS="${SEEN_GATEWAY_REGIONS}${gateway_region}|"
done
GATEWAY_REGION_CSV="$(IFS=,; echo "${GATEWAY_REGIONS[*]}")"
[ -n "${TR_PRIMARY_REGION:-}" ] || {
  echo "ERROR: TR_PRIMARY_REGION must be explicitly configured" >&2
  exit 1
}
[[ "$TR_PRIMARY_REGION" =~ ^[a-z]+-[a-z0-9]+[0-9]$ ]] || {
  echo "ERROR: TR_PRIMARY_REGION is not a canonical region" >&2
  exit 1
}
[ "$TR_PRIMARY_REGION" = "${REGIONS[0]}" ] || {
  echo "ERROR: TR_PRIMARY_REGION must be the first control-plane region" >&2
  exit 1
}
case ",$GATEWAY_REGION_CSV," in
  *",${TR_PRIMARY_REGION},"*) ;;
  *) echo "ERROR: TR_PRIMARY_REGION must also belong to the gateway inventory" >&2; exit 1 ;;
esac
CLOUD_RUN_NETWORK="${TR_CLOUD_RUN_NETWORK:-default}"
CLOUD_RUN_SUBNET="${TR_CLOUD_RUN_SUBNET:-default}"
[ "$CLOUD_RUN_NETWORK" = default ] && [ "$CLOUD_RUN_SUBNET" = default ] || {
  echo "ERROR: six-surface rollout pins the reviewed default VPC network/subnet" >&2
  exit 1
}
INTERNAL_ALLOWED_REGIONS=("${REGIONS[@]}")
for internal_region in \
  ${TR_SYNTHETIC_MONITOR_REGIONS//,/ } \
  "$TR_SYNTHETIC_THROUGHPUT_REGION" \
  "$TR_SYNTHETIC_IMAGE_REGION" \
  "$TR_SYNTHETIC_VIDEO_REGION"; do
  [[ "$internal_region" =~ ^[a-z]+-[a-z0-9]+[0-9]$ ]] || {
    echo "ERROR: invalid internal/synthetic region: ${internal_region:-<empty>}" >&2
    exit 1
  }
  case " ${INTERNAL_ALLOWED_REGIONS[*]} " in
    *" ${internal_region} "*) ;;
    *) INTERNAL_ALLOWED_REGIONS+=("$internal_region") ;;
  esac
done
INTERNAL_ALLOWED_REGION_CSV="$(IFS=,; echo "${INTERNAL_ALLOWED_REGIONS[*]}")"
REGIONAL_QUILL_HOSTS="$(printf 'api-%s.quillrouter.com,' "${REGIONS[@]}" "${GATEWAY_REGIONS[@]}")"
REGIONAL_QUILL_HOSTS="${REGIONAL_QUILL_HOSTS%,}"
PRESERVED_HOSTS="$(python3 - "$REQUIRED_PRESERVED_HOSTS" "$REGIONAL_QUILL_HOSTS" \
  "${TR_ROLLOUT_PRESERVED_HOSTS:-}" <<'PY'
import re
import sys

hosts = []
for raw in sys.argv[1:]:
    hosts.extend(
        item.strip().lower().rstrip(".")
        for item in raw.split(",")
        if item.strip()
    )
hosts = list(dict.fromkeys(hosts))
if any(
    not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
        host,
    )
    for host in hosts
):
    raise SystemExit("invalid preserved hostname")
print(",".join(hosts))
PY
)" || exit 1

SURFACES=(public actions console chat webhooks internal)
surface_service() {
  case "$1" in
    public) echo "$PUBLIC_SERVICE" ;;
    actions) echo "$ACTIONS_SERVICE" ;;
    console) echo "$CONSOLE_SERVICE" ;;
    chat) echo "$CHAT_SERVICE" ;;
    webhooks) echo "$WEBHOOKS_SERVICE" ;;
    internal) echo "$INTERNAL_SERVICE" ;;
    *) return 2 ;;
  esac
}
surface_account() {
  case "$1" in
    public) echo "$PUBLIC_RUN_SERVICE_ACCOUNT" ;;
    actions) echo "$ACTIONS_RUN_SERVICE_ACCOUNT" ;;
    console) echo "$CONSOLE_RUN_SERVICE_ACCOUNT" ;;
    chat) echo "$CHAT_RUN_SERVICE_ACCOUNT" ;;
    webhooks) echo "$WEBHOOKS_RUN_SERVICE_ACCOUNT" ;;
    internal) echo "$INTERNAL_RUN_SERVICE_ACCOUNT" ;;
    *) return 2 ;;
  esac
}
surface_backend() {
  case "$1" in
    public) echo "${TR_PUBLIC_BACKEND:-trusted-router-public-backend}" ;;
    actions) echo "${TR_ACTIONS_BACKEND:-trusted-router-actions-backend}" ;;
    console) echo "${TR_CONSOLE_BACKEND:-trusted-router-console-backend}" ;;
    chat) echo "${TR_CHAT_BACKEND:-trusted-router-chat-backend}" ;;
    webhooks) echo "${TR_WEBHOOKS_BACKEND:-trusted-router-webhooks-backend}" ;;
    internal) echo "${TR_INTERNAL_BACKEND:-trusted-router-billing-backend}" ;;
    *) return 2 ;;
  esac
}
surface_neg() {
  case "$1" in
    public) echo "${TR_PUBLIC_NEG:-trusted-router-public-neg}" ;;
    actions) echo "${TR_ACTIONS_NEG:-trusted-router-actions-neg}" ;;
    console) echo "${TR_CONSOLE_NEG:-trusted-router-console-neg}" ;;
    chat) echo "${TR_CHAT_NEG:-trusted-router-chat-neg}" ;;
    webhooks) echo "${TR_WEBHOOKS_NEG:-trusted-router-webhooks-neg}" ;;
    internal) echo "${TR_INTERNAL_NEG:-trusted-router-billing-neg}" ;;
    *) return 2 ;;
  esac
}
surface_policy() {
  case "$1" in
    public) echo "${TR_PUBLIC_EDGE_POLICY:-trusted-router-public-edge}" ;;
    actions) echo "${TR_ACTIONS_EDGE_POLICY:-trusted-router-actions-edge}" ;;
    console) echo "${TR_CONSOLE_EDGE_POLICY:-trusted-router-console-edge}" ;;
    chat) echo "${TR_CHAT_EDGE_POLICY:-trusted-router-chat-edge}" ;;
    webhooks) echo "${TR_WEBHOOKS_EDGE_POLICY:-trusted-router-webhooks-edge}" ;;
    internal) echo "${TR_INTERNAL_EDGE_POLICY:-trusted-router-billing-edge}" ;;
    *) return 2 ;;
  esac
}
surface_region_lines() {
  if [ "$1" = internal ]; then
    printf '%s\n' "${INTERNAL_ALLOWED_REGIONS[@]}"
  else
    printf '%s\n' "${REGIONS[@]}"
  fi
}

validate_resource_name() {
  local kind="$1" value="$2"
  [[ "$value" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
    echo "ERROR: invalid ${kind}: ${value}" >&2
    return 1
  }
}
[[ "$RELEASE" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || {
  echo "ERROR: TR_RELEASE contains an unsafe delimiter or character" >&2
  exit 1
}
[[ "$IMAGE" =~ ^[^,|[:space:]]+$ ]] || {
  echo "ERROR: IMAGE contains an unsafe delimiter or whitespace" >&2
  exit 1
}
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || {
  echo "ERROR: PROJECT_ID is not a canonical GCP project identifier" >&2
  exit 1
}
[ "$TR_CLOUD_RUN_INGRESS" = internal-and-cloud-load-balancing ] || {
  echo "ERROR: six-surface rollout requires LB-only Cloud Run ingress" >&2
  exit 1
}
for surface in "${SURFACES[@]}"; do
  validate_resource_name "Cloud Run service" "$(surface_service "$surface")"
  validate_resource_name "backend" "$(surface_backend "$surface")"
  validate_resource_name "NEG" "$(surface_neg "$surface")"
  validate_resource_name "Cloud Armor policy" "$(surface_policy "$surface")"
done
for surface in "${SURFACES[@]}"; do
  validate_resource_name "candidate revision" "$(surface_service "$surface")-${REVISION_SUFFIX}"
done
for surface in "${SURFACES[@]}"; do
  case "$surface" in
    public) expected_backend=trusted-router-public-backend; expected_neg=trusted-router-public-neg; expected_policy=trusted-router-public-edge ;;
    actions) expected_backend=trusted-router-actions-backend; expected_neg=trusted-router-actions-neg; expected_policy=trusted-router-actions-edge ;;
    console) expected_backend=trusted-router-console-backend; expected_neg=trusted-router-console-neg; expected_policy=trusted-router-console-edge ;;
    chat) expected_backend=trusted-router-chat-backend; expected_neg=trusted-router-chat-neg; expected_policy=trusted-router-chat-edge ;;
    webhooks) expected_backend=trusted-router-webhooks-backend; expected_neg=trusted-router-webhooks-neg; expected_policy=trusted-router-webhooks-edge ;;
    internal) expected_backend=trusted-router-billing-backend; expected_neg=trusted-router-billing-neg; expected_policy=trusted-router-billing-edge ;;
  esac
  [ "$(surface_backend "$surface")" = "$expected_backend" ] &&
  [ "$(surface_neg "$surface")" = "$expected_neg" ] &&
  [ "$(surface_policy "$surface")" = "$expected_policy" ] || {
    echo "ERROR: ${surface} must use its canonical backend, NEG, and Cloud Armor policy" >&2
    exit 1
  }
done
[ "$PUBLIC_SERVICE" = trusted-router-public ] &&
[ "$ACTIONS_SERVICE" = trusted-router-actions ] &&
[ "$CONSOLE_SERVICE" = trusted-router-console ] &&
[ "$CHAT_SERVICE" = trusted-router-chat ] &&
[ "$WEBHOOKS_SERVICE" = trusted-router-webhooks ] &&
[ "$INTERNAL_SERVICE" = trusted-router-billing ] || {
  echo "ERROR: production six-surface service names must use the canonical inventory" >&2
  exit 1
}
[ "${LEGACY_CONSOLE_SERVICE:-}" = trusted-router ] || {
  echo "ERROR: initial migration must preserve trusted-router as the legacy monolith" >&2
  exit 1
}
for surface in "${SURFACES[@]}"; do
  [ "$(surface_account "$surface")" = "tr-${surface}@${PROJECT_ID}.iam.gserviceaccount.com" ] || {
    echo "ERROR: ${surface} must use its canonical dedicated runtime identity" >&2
    exit 1
  }
done

validate_runtime_service_accounts
for runtime_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  runtime_description="$(gc iam service-accounts describe "$runtime_account" --format=json)"
  if ! jq -e --arg email "$runtime_account" '
      .email == $email and ((.disabled // false) == false)
    ' <<<"$runtime_description" >/dev/null; then
    echo "ERROR: runtime service account is missing, disabled, or renamed: ${runtime_account}" >&2
    exit 1
  fi
  runtime_policy="$(gc iam service-accounts get-iam-policy "$runtime_account" --format=json)"
  if ! jq -e --arg member "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" '
      ([.bindings[]? | select(any(.members[]?; . == $member))
        | {role, condition: (.condition // null)}] | unique)
      == [{role:"roles/iam.serviceAccountUser",condition:null}]
    ' <<<"$runtime_policy" >/dev/null; then
    echo "ERROR: ${DEPLOY_SERVICE_ACCOUNT} must have exactly unconditional actAs on ${runtime_account}" >&2
    exit 1
  fi
done

RAW_URL_MAP="${WORK_DIR}/url-map.raw.json"
gc compute url-maps describe "$URL_MAP_NAME" --global --format=json >"$RAW_URL_MAP"
CAPTURED_PRIOR_URL_MAP="${WORK_DIR}/url-map.prior.current.json"
python3 "$STATE_TOOL" sanitize-url-map "$RAW_URL_MAP" "$CAPTURED_PRIOR_URL_MAP"
if [ "$RESUMING_INITIAL_STAGE" = true ]; then
  python3 - "$PRIOR_URL_MAP" <<'PY'
import os
import stat
import sys
metadata = os.lstat(sys.argv[1])
if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("resumed prior URL-map snapshot must be a regular mode-0600 file")
PY
  [ "$(python3 "$STATE_TOOL" hash-url-map "$CAPTURED_PRIOR_URL_MAP")" = \
    "$(python3 "$STATE_TOOL" hash-url-map "$PRIOR_URL_MAP")" ] || {
    echo "ERROR: live URL map changed since the initial-stage journal was created" >&2
    exit 1
  }
else
  python3 - "$CAPTURED_PRIOR_URL_MAP" "$PRIOR_URL_MAP" <<'PY'
import os
import stat
import sys
from pathlib import Path
source = Path(sys.argv[1])
destination = Path(sys.argv[2])
descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as output:
    output.write(source.read_bytes())
    output.flush()
    os.fsync(output.fileno())
directory = os.open(destination.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
if stat.S_IMODE(os.stat(destination).st_mode) != 0o600:
    raise SystemExit("prior URL-map snapshot mode differs")
PY
fi
if jq -e 'any(.pathMatchers[]?; .name == "trusted-router-service-surfaces")' \
    "$PRIOR_URL_MAP" >/dev/null; then
  ROLLOUT_MODE="existing_split"
else
  ROLLOUT_MODE="initial_split"
fi

persist_attested_artifact() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" "$RESUMING_INITIAL_STAGE" <<'PY'
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
resuming = sys.argv[3] == "true"
source_metadata = os.lstat(source)
if (
    not stat.S_ISREG(source_metadata.st_mode)
    or stat.S_IMODE(source_metadata.st_mode) != 0o600
):
    raise SystemExit("rollout prerequisite artifact must be a regular mode-0600 file")
payload = source.read_bytes()
if destination.exists() or destination.is_symlink():
    destination_metadata = os.lstat(destination)
    if (
        not resuming
        or not stat.S_ISREG(destination_metadata.st_mode)
        or stat.S_IMODE(destination_metadata.st_mode) != 0o600
        or destination.read_bytes() != payload
    ):
        raise SystemExit("persisted rollout prerequisite artifact differs")
else:
    descriptor, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
        os.unlink(temporary)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
print(hashlib.sha256(payload).hexdigest())
PY
}

[ -n "${TR_ROLLOUT_FRONTEND_ATTESTATION:-}" ] || {
  echo "ERROR: rollout requires TR_ROLLOUT_FRONTEND_ATTESTATION" >&2
  exit 1
}
FRONTEND_ATTESTATION_SHA256="$(persist_attested_artifact \
  "$TR_ROLLOUT_FRONTEND_ATTESTATION" "$FRONTEND_ATTESTATION")" || exit 1
python3 "${SCRIPT_DIR}/rollout_frontend_attest.py" \
  verify-artifact "$FRONTEND_ATTESTATION" || exit 1

LEGACY_HARDENING_ARTIFACT_SHA256=""
if [ "$ROLLOUT_MODE" = initial_split ]; then
  [ -n "${TR_LEGACY_HARDENING_ARTIFACT:-}" ] || {
    echo "ERROR: initial split requires TR_LEGACY_HARDENING_ARTIFACT" >&2
    exit 1
  }
  LEGACY_HARDENING_ARTIFACT_SHA256="$(persist_attested_artifact \
    "$TR_LEGACY_HARDENING_ARTIFACT" "$LEGACY_HARDENING_ARTIFACT")" || exit 1
  bash "${SCRIPT_DIR}/rollout_legacy_harden.sh" \
    --verify-artifact "$LEGACY_HARDENING_ARTIFACT" || exit 1
fi

# Validate the preserved-host and three-domain transformation before the first
# provider mutation. Placeholder self-links are deliberately canonical but do
# not need to exist; the real candidate is rendered and provider-validated
# after inactive backends are reconciled and before any active service deploy.
EARLY_CANDIDATE_URL_MAP="${WORK_DIR}/url-map.candidate.preflight.json"
python3 "$URL_MAP_TOOL" \
  --input "$PRIOR_URL_MAP" \
  --output "$EARLY_CANDIDATE_URL_MAP" \
  --public-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend public)" \
  --actions-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend actions)" \
  --console-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend console)" \
  --chat-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend chat)" \
  --webhooks-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend webhooks)" \
  --internal-backend "projects/${PROJECT_ID}/global/backendServices/$(surface_backend internal)" \
  --domains "$DOMAINS" \
  --preserved-hosts "$PRESERVED_HOSTS"

prior_json_path() {
  echo "${WORK_DIR}/prior-$1-$2.json"
}
legacy_console_json_path() {
  echo "${WORK_DIR}/legacy-console-$1.json"
}
service_exists() {
  local surface="$1" region="$2" service output describe_output
  service="$(surface_service "$surface")"
  output="$(prior_json_path "$surface" "$region")"
  if describe_output="$(gc run services describe "$service" --region="$region" --format=json 2>&1)"; then
    printf '%s\n' "$describe_output" >"$output"
    return 0
  fi
  case "$describe_output" in
    *NOT_FOUND*|*"not found"*) return 1 ;;
    *) echo "ERROR: cannot determine whether ${service}/${region} exists" >&2; return 2 ;;
  esac
}

# Capture the complete pre-mutation inventory. Partial service families are a
# failed precondition, not something a rollout silently fills in.
for surface in "${SURFACES[@]}"; do
  family_count=0
  while IFS= read -r region; do
    service_rc=0
    service_exists "$surface" "$region" || service_rc=$?
    if [ "$service_rc" = 0 ]; then
      family_count=$((family_count + 1))
    elif [ "$service_rc" != 1 ]; then
      exit 1
    fi
  done < <(surface_region_lines "$surface")
  expected_family_count="${#REGIONS[@]}"
  [ "$surface" = internal ] && expected_family_count="${#INTERNAL_ALLOWED_REGIONS[@]}"
  if [ "$ROLLOUT_MODE" = "existing_split" ] && [ "$family_count" -ne "$expected_family_count" ]; then
    echo "ERROR: existing split has incomplete ${surface} regional inventory" >&2
    exit 1
  fi
  if [ "$ROLLOUT_MODE" = "initial_split" ]; then
    if [ "$surface" = internal ] && [ "$family_count" -ne "$expected_family_count" ]; then
      echo "ERROR: initial split requires bootstrap internal in every control/synthetic region" >&2
      exit 1
    fi
    if [ "$surface" != internal ] && [ "$family_count" -ne 0 ] && \
        [ "$RESUMING_INITIAL_STAGE" != true ]; then
      echo "ERROR: initial split requires the new ${surface} companion to be absent in every region" >&2
      exit 1
    fi
  fi
done

# The legacy monolith is deliberately outside the six new service names.  It
# remains the prior URL map's rollback target and is never deployed or assigned
# traffic by this transaction.  Initial capability/storage discovery reads it
# explicitly; subsequent split releases read the serving console companion.
if [ "$ROLLOUT_MODE" = initial_split ]; then
  LEGACY_BACKEND_PATH="${WORK_DIR}/legacy-control-backend.json"
  gc compute backend-services describe trusted-router-control-backend \
    --global --format=json >"$LEGACY_BACKEND_PATH" || {
    echo "ERROR: initial split cannot capture the legacy control backend" >&2
    exit 1
  }
  LEGACY_BACKEND_HASH="$(python3 "$STATE_TOOL" hash-resource "$LEGACY_BACKEND_PATH")" || exit 1
  python3 - "$LEGACY_BACKEND_PATH" "$PROJECT_ID" "$REGION_CSV" <<'PY' || exit 1
import json
import sys
from pathlib import Path

backend = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
project = sys.argv[2]
regions = sys.argv[3].split(",")
expected = {
    (
        f"https://www.googleapis.com/compute/v1/projects/{project}/regions/"
        f"{region}/networkEndpointGroups/trusted-router-control-neg"
    )
    for region in regions
}
actual = {item.get("group") for item in backend.get("backends") or []}
if actual != expected:
    raise SystemExit("legacy control backend has inexact regional NEG membership")
PY
  for region in "${REGIONS[@]}"; do
    legacy_neg_path="${WORK_DIR}/legacy-control-neg-${region}.json"
    gc compute network-endpoint-groups describe trusted-router-control-neg \
      --region="$region" --format=json >"$legacy_neg_path" || exit 1
    python3 - "$legacy_neg_path" "$LEGACY_CONSOLE_SERVICE" <<'PY' || exit 1
import json
import sys
from pathlib import Path

neg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cloud_run = (neg.get("cloudRun") or {})
if neg.get("networkEndpointType") not in {None, "SERVERLESS"}:
    raise SystemExit("legacy control NEG is not serverless")
if cloud_run != {"service": sys.argv[2]}:
    raise SystemExit("legacy control NEG does not target only the legacy monolith")
PY
  done
  python3 - "$PRIOR_URL_MAP" <<'PY' || exit 1
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

def strings(item):
    if isinstance(item, dict):
        for nested in item.values():
            yield from strings(nested)
    elif isinstance(item, list):
        for nested in item:
            yield from strings(nested)
    elif isinstance(item, str):
        yield item

references = {
    item.rstrip("/").rsplit("/", 1)[-1]
    for item in strings(value)
    if "backendServices" in item or item.endswith("-backend")
}
if "trusted-router-control-backend" not in references:
    raise SystemExit("prior URL map does not bind the legacy control backend")
managed = {
    "trusted-router-public-backend",
    "trusted-router-actions-backend",
    "trusted-router-console-backend",
    "trusted-router-chat-backend",
    "trusted-router-webhooks-backend",
    "trusted-router-billing-backend",
}
unexpected = references & managed
if unexpected:
    raise SystemExit(
        "initial prior URL map already references split backend(s): "
        + ",".join(sorted(unexpected))
    )
PY
  for region in "${REGIONS[@]}"; do
    legacy_path="$(legacy_console_json_path "$region")"
    if ! gc run services describe "$LEGACY_CONSOLE_SERVICE" \
        --region="$region" --format=json >"$legacy_path"; then
      echo "ERROR: initial split requires legacy monolith ${LEGACY_CONSOLE_SERVICE}/${region}" >&2
      exit 1
    fi
    python3 "$STATE_TOOL" validate-prior-traffic "$legacy_path" || {
      echo "ERROR: legacy fallback ${LEGACY_CONSOLE_SERVICE}/${region} traffic is not exactly restorable" >&2
      exit 1
    }
    legacy_traffic="$(python3 "$STATE_TOOL" traffic-state "$legacy_path")" || exit 1
    legacy_revision="$(jq -er '
      if (.status.traffic | length) == 1 and
         (.status.traffic[0].percent == 100) and
         (.status.traffic[0].tag // null) == null and
         (.status.traffic[0].revisionName != null)
      then .status.traffic[0].revisionName
      else error("legacy fallback is not one named untagged 100% target") end
    ' "$legacy_path")" || exit 1
    legacy_generation="$(jq -er '
      if (.metadata.generation | type) == "number" and
         (.metadata.generation == .status.observedGeneration)
      then .metadata.generation
      else error("legacy fallback generation is not observed") end
    ' "$legacy_path")" || exit 1
    legacy_hash="$(python3 "$STATE_TOOL" hash-service "$legacy_path")" || exit 1
    legacy_revision_path="${WORK_DIR}/legacy-revision-${region}.json"
    legacy_iam_path="${WORK_DIR}/legacy-service-iam-${region}.json"
    legacy_secret_refs="${WORK_DIR}/legacy-secret-refs-${region}.tsv"
    gc run revisions describe "$legacy_revision" --region="$region" --format=json \
      >"$legacy_revision_path" || exit 1
    gc run services get-iam-policy "$LEGACY_CONSOLE_SERVICE" \
      --region="$region" --format=json >"$legacy_iam_path" || exit 1
    python3 - "$legacy_path" "$legacy_revision_path" "$legacy_iam_path" \
      "$legacy_revision" "$RUN_SERVICE_ACCOUNT" "$legacy_secret_refs" <<'PY' || exit 1
import json
import os
import re
import sys
from pathlib import Path

service = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
revision = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
policy = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
expected_revision = sys.argv[4]
expected_account = sys.argv[5]
refs_path = Path(sys.argv[6])

status = service.get("status") or {}
annotations = (service.get("metadata") or {}).get("annotations") or {}
ready = any(
    item.get("type") == "Ready" and item.get("status") == "True"
    for item in status.get("conditions") or []
)
if not ready or status.get("latestReadyRevisionName") != expected_revision:
    raise SystemExit("legacy fallback service is not Ready on its serving revision")
if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
    raise SystemExit("legacy fallback desired ingress is not LB-only")
if annotations.get("run.googleapis.com/ingress-status") != "internal-and-cloud-load-balancing":
    raise SystemExit("legacy fallback effective ingress is not LB-only")

revision_status = revision.get("status") or {}
revision_ready = any(
    item.get("type") == "Ready" and item.get("status") == "True"
    for item in revision_status.get("conditions") or []
)
if (revision.get("metadata") or {}).get("name") != expected_revision or not revision_ready:
    raise SystemExit("legacy fallback serving revision is not Ready")
revision_spec = revision.get("spec") or {}
containers = revision_spec.get("containers") or []
if len(containers) != 1 or revision_spec.get("serviceAccountName") != expected_account:
    raise SystemExit("legacy fallback revision identity/container shape is inexact")

all_users = []
for binding in policy.get("bindings") or []:
    members = binding.get("members") or []
    if "allUsers" in members:
        all_users.append(
            (binding.get("role"), binding.get("condition"), members.count("allUsers"))
        )
if all_users != [("roles/run.invoker", None, 1)]:
    raise SystemExit("legacy fallback service IAM lacks exact unconditional allUsers invoker")

resource_re = re.compile(
    r"(?:projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/)?"
    r"[a-z][a-z0-9-]{0,253}[a-z0-9]$"
)
refs = []
for item in containers[0].get("env") or []:
    secret_ref = ((item.get("valueFrom") or {}).get("secretKeyRef") or {})
    if not secret_ref:
        continue
    resource = secret_ref.get("name")
    version = str(secret_ref.get("key") or "")
    if not isinstance(resource, str) or not resource_re.fullmatch(resource):
        raise SystemExit("legacy fallback has a malformed mounted secret resource")
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise SystemExit("legacy fallback mounted secret versions must be numeric")
    refs.append((resource, version))
if len(refs) != len(set(refs)):
    raise SystemExit("legacy fallback has duplicate mounted secret references")
descriptor = os.open(refs_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    for resource, version in sorted(refs):
        output.write(f"{resource}\t{version}\n")
PY
    while IFS=$'\t' read -r legacy_secret legacy_version; do
      [ -n "$legacy_secret" ] || continue
      legacy_version_path="${WORK_DIR}/legacy-secret-version-${region}-${legacy_version}.json"
      legacy_policy_path="${WORK_DIR}/legacy-secret-policy-${region}-${legacy_version}.json"
      gc secrets versions describe "$legacy_version" --secret="$legacy_secret" \
        --format=json >"$legacy_version_path" || exit 1
      gc secrets get-iam-policy "$legacy_secret" --format=json \
        >"$legacy_policy_path" || exit 1
      python3 - "$legacy_version_path" "$legacy_policy_path" \
        "serviceAccount:${RUN_SERVICE_ACCOUNT}" <<'PY' || exit 1
import json
import sys
from pathlib import Path

version = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
policy = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
member = sys.argv[3]
if version.get("state") != "ENABLED":
    raise SystemExit("legacy fallback mounted secret version is not ENABLED")
direct = sorted(
    (binding.get("role"), binding.get("condition"))
    for binding in policy.get("bindings") or []
    if member in (binding.get("members") or [])
)
if direct != [("roles/secretmanager.secretAccessor", None)]:
    raise SystemExit("legacy fallback runtime lacks exact mounted-secret access")
PY
    done <"$legacy_secret_refs"
    legacy_revision_hash="$(python3 "$STATE_TOOL" hash-revision "$legacy_revision_path")" || exit 1
    legacy_invoker_iam_hash="$(python3 "$STATE_TOOL" hash-iam-policy "$legacy_iam_path")" || exit 1
    jq -cn \
      --arg service "$LEGACY_CONSOLE_SERVICE" \
      --arg backend trusted-router-control-backend \
      --arg region "$region" \
      --argjson generation "$legacy_generation" \
      --arg serving_revision "$legacy_revision" \
      --arg serving_revision_sha256 "$legacy_revision_hash" \
      --argjson traffic "$legacy_traffic" \
      --arg postcondition_sha256 "$legacy_hash" \
      --arg backend_postcondition_sha256 "$LEGACY_BACKEND_HASH" \
      --arg invoker_iam_sha256 "$legacy_invoker_iam_hash" \
      '{service:$service,backend:$backend,region:$region,generation:$generation,
        serving_revision:$serving_revision,
        serving_revision_sha256:$serving_revision_sha256,traffic:$traffic,
        postcondition_sha256:$postcondition_sha256,
        backend_postcondition_sha256:$backend_postcondition_sha256,
        invoker_iam_sha256:$invoker_iam_sha256}' >>"$LEGACY_FALLBACK_FILE"
  done
fi

# A removed control-plane region must not leave a canonical split service (and
# especially a default run.app origin) outside this transaction. Cloud Run's
# project-wide inventory is therefore exact for the six canonical names.
ALL_RUN_SERVICES="${WORK_DIR}/all-run-services.json"
gc run services list --platform=managed --format=json >"$ALL_RUN_SERVICES"
python3 - "$ALL_RUN_SERVICES" "$REGION_CSV" "$INTERNAL_ALLOWED_REGION_CSV" \
  "$PUBLIC_SERVICE" "$ACTIONS_SERVICE" "$CONSOLE_SERVICE" "$CHAT_SERVICE" \
  "$WEBHOOKS_SERVICE" "$INTERNAL_SERVICE" <<'PY'
import json
import sys
from pathlib import Path

items = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
regions = set(sys.argv[2].split(","))
internal_regions = set(sys.argv[3].split(","))
canonical = set(sys.argv[4:])
if not isinstance(items, list):
    raise SystemExit("Cloud Run service inventory is not a list")
for item in items:
    metadata = item.get("metadata") or {}
    name = metadata.get("name") or item.get("name")
    if name not in canonical:
        continue
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    region = (
        item.get("location")
        or labels.get("cloud.googleapis.com/location")
        or labels.get("run.googleapis.com/location")
        or annotations.get("run.googleapis.com/location")
    )
    allowed = internal_regions if name == "trusted-router-billing" else regions
    if region not in allowed:
        raise SystemExit(
            f"canonical Cloud Run service {name} exists outside configured regions: {region!r}"
        )
PY

# `gcloud run deploy --no-traffic` deliberately converts floating LATEST
# traffic to its currently resolved revision before it creates a candidate.
# That conversion cannot be reversed safely: setting LATEST during rollback
# would select the candidate, and Cloud Run does not permit deleting the latest
# revision. Require operators to make that semantic change explicit by pinning
# every floating traffic/tag target before this all-surface transaction.
for surface in "${SURFACES[@]}"; do
  while IFS= read -r region; do
    prior_path="$(prior_json_path "$surface" "$region")"
    [ -f "$prior_path" ] || continue
    if ! python3 "$STATE_TOOL" validate-prior-traffic "$prior_path"; then
      echo "ERROR: ${surface}/${region} prior traffic is not exactly restorable" >&2
      exit 1
    fi
    captured_traffic="$(python3 "$STATE_TOOL" traffic-state "$prior_path")" || exit 1
    if jq -e 'any(.[]; .latest_revision)' <<<"$captured_traffic" >/dev/null; then
      echo "ERROR: ${surface}/${region} has floating LATEST traffic or tags; pin it to named revisions before staging" >&2
      exit 1
    fi
  done < <(surface_region_lines "$surface")
done

if [ "$ROLLOUT_MODE" = initial_split ]; then
  PRIMARY_PRIOR="$(legacy_console_json_path "${REGIONS[0]}")"
else
  PRIMARY_PRIOR="$(prior_json_path console "${REGIONS[0]}")"
fi
PRIMARY_CAPABILITY_STATE=""
SERVING_CONSOLE_JSON="${WORK_DIR}/serving-console-${REGIONS[0]}.json"
for region in "${REGIONS[@]}"; do
  if [ "$ROLLOUT_MODE" = initial_split ]; then
    region_prior="$(legacy_console_json_path "$region")"
  else
    region_prior="$(prior_json_path console "$region")"
  fi
  serving_revision="$(jq -er '
    [.status.traffic[]? | select((.percent // 0) > 0)] as $live
    | if ($live | length) == 1 and ($live[0].percent == 100) and ($live[0].revisionName != null)
      then $live[0].revisionName else error("console traffic is not one unambiguous 100% revision") end
  ' "$region_prior")"
  serving_json="${WORK_DIR}/serving-console-${region}.json"
  gc run revisions describe "$serving_revision" --region="$region" --format=json >"$serving_json"
  capability_state="$(python3 - "$serving_json" <<'PY'
import json
import sys
from pathlib import Path

revision = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
items = ((revision.get("spec") or {}).get("containers") or [{}])[0].get("env") or []
values = {item.get("name"): str(item.get("value", "")) for item in items if "valueFrom" not in item}
secrets = {
    item.get("name"): (((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") or "").split("/")[-1]
    for item in items
    if "valueFrom" in item
}
def boolean_or_missing(name: str) -> str:
    value = values.get(name, "__missing__")
    if value not in {"true", "false", "__missing__"}:
        raise SystemExit(f"invalid serving capability {name}")
    return value
def oauth(name: str, fields: tuple[str, str]) -> str:
    explicit = boolean_or_missing(name)
    if explicit != "__missing__":
        return explicit
    present = [bool(secrets.get(field)) for field in fields]
    if any(present) and not all(present):
        raise SystemExit(f"partial serving OAuth capability {name}")
    return "true" if all(present) else "false"
state = {
    "new_signups": values.get("TR_NEW_SIGNUPS_ENABLED", "true"),
    "google_oauth": oauth("TR_GOOGLE_OAUTH_LOGIN_AVAILABLE", ("TR_GOOGLE_CLIENT_ID", "TR_GOOGLE_CLIENT_SECRET")),
    "github_oauth": oauth("TR_GITHUB_OAUTH_LOGIN_AVAILABLE", ("TR_GITHUB_CLIENT_ID", "TR_GITHUB_CLIENT_SECRET")),
    "paypal": boolean_or_missing("TR_PAYPAL_CHECKOUT_ENABLED"),
    "paypal_console_credentials": all(
        bool(secrets.get(name))
        for name in ("TR_PAYPAL_CLIENT_ID", "TR_PAYPAL_CLIENT_SECRET")
    ),
    "adyen": boolean_or_missing("TR_ADYEN_ENABLED"),
    "adyen_console_credentials": all(
        bool(secrets.get(name))
        for name in ("TR_ADYEN_API_KEY", "TR_ADYEN_CLIENT_KEY", "TR_ADYEN_REFERENCE_KEY")
    ),
    "veriff": boolean_or_missing("TR_VERIFF_ENABLED"),
    "veriff_console_credentials": bool(secrets.get("TR_VERIFF_API_KEY")),
    "storage": values.get("TR_STORAGE_BACKEND", "spanner-bigtable"),
    "request_records": values.get("TR_REQUEST_RECORD_WRITE_MODE", "__missing__"),
    "analytics_reads": values.get("TR_ANALYTICS_READ_MODE", "bigtable"),
    "generation_records": values.get("TR_GENERATION_RECORDS_ENABLED", "false"),
    "bigtable_mirror": values.get("TR_BIGTABLE_MIRROR_WRITES_ENABLED", "true"),
    "provider_clickhouse_url": values.get("TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL", ""),
    "telnyx_from": values.get("TR_TELNYX_FROM_NUMBER", ""),
    "telnyx_account": values.get("TR_TELNYX_TEXML_ACCOUNT_ID", ""),
    "telnyx_application": values.get("TR_TELNYX_TEXML_APPLICATION_ID", ""),
    "twilio_from": values.get("TR_TWILIO_FROM_NUMBER", ""),
}
print(json.dumps(state, sort_keys=True, separators=(",", ":")))
PY
)"
  if [ -z "$PRIMARY_CAPABILITY_STATE" ]; then
    PRIMARY_CAPABILITY_STATE="$capability_state"
  elif [ "$capability_state" != "$PRIMARY_CAPABILITY_STATE" ]; then
    echo "ERROR: serving console capability/storage state differs across regions" >&2
    exit 1
  fi
done

serving_env_value() {
  local name="$1" default_value="${2:-}"
  jq -r --arg name "$name" --arg default "$default_value" '
    [.spec.containers[0].env[]? | select(.name == $name) | .value][0] // $default
  ' "$SERVING_CONSOLE_JSON"
}

# Regional quota capability and traffic issuance are separate switches. Resolve
# preserved values from the one revision receiving 100% of primary traffic,
# never the latest candidate or service template: both still point at a rejected
# revision after rollback. During the initial split that authority is the legacy
# service; after the split it is the internal/billing service.
ROLLOUT_SERVICE="$SERVICE"
REGIONAL_QUOTA_SERVICE="$SERVICE"
if [ "$ROLLOUT_MODE" = existing_split ]; then
  REGIONAL_QUOTA_SERVICE="$INTERNAL_SERVICE"
fi
SERVICE="$REGIONAL_QUOTA_SERVICE"
REGIONAL_QUOTA_PRIMARY_FRESH=false
REGIONAL_QUOTA_PRIMARY_REVISION_JSON=""
if REGIONAL_QUOTA_PRIMARY_REVISION_JSON="$(
  regional_quota_active_revision_json "$TR_PRIMARY_REGION" true
)"; then
  :
else
  regional_quota_primary_status=$?
  if [ "$regional_quota_primary_status" -eq 3 ]; then
    REGIONAL_QUOTA_PRIMARY_FRESH=true
  else
    exit "$regional_quota_primary_status"
  fi
fi

read_primary_regional_quota_env() {
  local name="$1"
  local default_value="${2:-}"
  if [ "$REGIONAL_QUOTA_PRIMARY_FRESH" = true ]; then
    printf '%s\n' "$default_value"
    return 0
  fi
  regional_quota_revision_env \
    "$REGIONAL_QUOTA_PRIMARY_REVISION_JSON" \
    "$name" \
    "$default_value"
}

LIVE_REGIONAL_QUOTA_LEASES_ENABLED="$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASES_ENABLED" "false"
)"
REGIONAL_QUOTA_LEASES_ENABLED="${TR_REGIONAL_QUOTA_LEASES_ENABLED:-${LIVE_REGIONAL_QUOTA_LEASES_ENABLED:-false}}"
case "$REGIONAL_QUOTA_LEASES_ENABLED" in
  true|false) ;;
  *)
    log "refusing rollout: TR_REGIONAL_QUOTA_LEASES_ENABLED must be true or false"
    exit 1
    ;;
esac

# workflow_dispatch passes one of preserve/false/true through unchanged. The
# deploy shell, not GitHub's expression coercion, turns that raw operator intent
# into the boolean written on the Cloud Run revision. A missing marker on the
# first compatibility deploy defaults OFF.
LIVE_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED="$(
  read_primary_regional_quota_env \
    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" \
    "false"
)"
REGIONAL_QUOTA_LEASE_ISSUANCE_CONTROL="${TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED:-}"
REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED="$(
  regional_quota_normalize_issuance_control \
    "$REGIONAL_QUOTA_LEASE_ISSUANCE_CONTROL" \
    "$LIVE_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
)"
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = true ] &&
   [ "$REGIONAL_QUOTA_LEASES_ENABLED" != true ]; then
  log "refusing rollout: regional quota issuance requires lease capability"
  exit 1
fi

REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS="${TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS"
)}"
REGIONAL_QUOTA_BIGTABLE_TABLE="${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_BIGTABLE_TABLE" "trustedrouter-regional-quota"
)}"
REGIONAL_QUOTA_BIGTABLE_APP_PROFILES="${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"
)}"
REGIONAL_QUOTA_LEASE_TTL_SECONDS="${TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS" "60"
)}"
REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS="${TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS" "10000000"
)}"
REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS="${TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS" "1000"
)}"
REGIONAL_QUOTA_LEASE_SHARD_COUNT="${TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT" "16"
)}"
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = true ] && {
  [ -z "$REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS" ] ||
  [ -z "$REGIONAL_QUOTA_BIGTABLE_APP_PROFILES" ];
}; then
  log "refusing rollout: regional quota issuance requires pilot workspaces and fixed Bigtable app profiles"
  exit 1
fi

# This executes before gcloud run deploy can create any issuance-enabled
# revision. Every currently active internal/billing fleet member must already
# be settlement-capable and explicitly carry the independent issuance marker.
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = true ]; then
  regional_quota_preflight_issuance_fleet
fi
SERVICE="$ROLLOUT_SERVICE"
if [ "$ROLLOUT_MODE" = initial_split ] && {
  [ "$REGIONAL_QUOTA_LEASES_ENABLED" != false ] ||
  [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" != false ];
}; then
  echo "ERROR: initial six-surface split requires regional quota capability and issuance disabled until the bootstrapped internal cohort is replaced" >&2
  exit 1
fi

serving_secret_name() {
  local name="$1"
  jq -r --arg name "$name" '
    [.spec.containers[0].env[]? | select(.name == $name)
      | .valueFrom.secretKeyRef.name][0] // ""
    | split("/")[-1]
  ' "$SERVING_CONSOLE_JSON"
}
require_bool() {
  local name="$1" value="$2"
  case "$value" in true|false) echo "$value" ;; *) echo "ERROR: ${name} must be true or false" >&2; return 1 ;; esac
}
resolve_credential_capability() {
  local input_name="$1" field="$2" missing_default="$3"
  shift 3
  local explicit="${!input_name:-}" live any=0 all=1 credential
  if [ -n "$explicit" ]; then require_bool "$input_name" "$explicit"; return; fi
  live="$(serving_env_value "$field")"
  if [ -n "$live" ]; then require_bool "$field on serving console" "$live"; return; fi
  for credential in "$@"; do
    if [ -n "$(serving_secret_name "$credential")" ]; then any=1; else all=0; fi
  done
  if [ "$any" = 1 ] && [ "$all" = 0 ]; then
    echo "ERROR: serving console has a partial ${field} credential set" >&2
    return 1
  fi
  if [ "$all" = 1 ] && [ "$#" -gt 0 ]; then echo true; else echo "$missing_default"; fi
}
resolve_explicit_or_serving_flag() {
  local input_name="$1" field="$2"
  local explicit="${!input_name:-}" live
  if [ -n "$explicit" ]; then require_bool "$input_name" "$explicit"; return; fi
  live="$(serving_env_value "$field")"
  if [ -z "$live" ]; then
    echo "ERROR: ${input_name} must be explicit because the unambiguous serving console revision has no capability flag" >&2
    return 1
  fi
  require_bool "$field on serving console" "$live"
}

GOOGLE_OAUTH_AVAILABLE="$(resolve_credential_capability \
  TR_GOOGLE_OAUTH_LOGIN_AVAILABLE TR_GOOGLE_OAUTH_LOGIN_AVAILABLE false \
  TR_GOOGLE_CLIENT_ID TR_GOOGLE_CLIENT_SECRET)"
GITHUB_OAUTH_AVAILABLE="$(resolve_credential_capability \
  TR_GITHUB_OAUTH_LOGIN_AVAILABLE TR_GITHUB_OAUTH_LOGIN_AVAILABLE false \
  TR_GITHUB_CLIENT_ID TR_GITHUB_CLIENT_SECRET)"
PAYPAL_CHECKOUT_ENABLED="$(resolve_explicit_or_serving_flag \
  TR_PAYPAL_CHECKOUT_ENABLED TR_PAYPAL_CHECKOUT_ENABLED)"
ADYEN_ENABLED="$(resolve_explicit_or_serving_flag \
  TR_ADYEN_ENABLED TR_ADYEN_ENABLED)"
VERIFF_ENABLED="$(resolve_explicit_or_serving_flag \
  TR_VERIFF_ENABLED TR_VERIFF_ENABLED)"
NEW_SIGNUPS_ENABLED="$(require_bool TR_NEW_SIGNUPS_ENABLED \
  "${TR_NEW_SIGNUPS_ENABLED:-$(serving_env_value TR_NEW_SIGNUPS_ENABLED true)}")"

REQUEST_RECORD_WRITE_MODE="${TR_REQUEST_RECORD_WRITE_MODE:-$(serving_env_value TR_REQUEST_RECORD_WRITE_MODE)}"
case "$REQUEST_RECORD_WRITE_MODE" in legacy|typed) ;; *)
  echo "ERROR: cannot determine TR_REQUEST_RECORD_WRITE_MODE from the serving console" >&2; exit 1 ;;
esac
STORAGE_BACKEND="${TR_STORAGE_BACKEND:-$(serving_env_value TR_STORAGE_BACKEND spanner-bigtable)}"
case "$STORAGE_BACKEND" in spanner-bigtable|spanner-clickhouse) ;; *)
  echo "ERROR: invalid TR_STORAGE_BACKEND" >&2; exit 1 ;;
esac
ANALYTICS_READ_MODE="${TR_ANALYTICS_READ_MODE:-$(serving_env_value TR_ANALYTICS_READ_MODE bigtable)}"
case "$ANALYTICS_READ_MODE" in bigtable|dual|clickhouse|clickhouse-only) ;; *)
  echo "ERROR: invalid TR_ANALYTICS_READ_MODE" >&2; exit 1 ;;
esac
GENERATION_RECORDS_ENABLED="$(require_bool TR_GENERATION_RECORDS_ENABLED \
  "${TR_GENERATION_RECORDS_ENABLED:-$(serving_env_value TR_GENERATION_RECORDS_ENABLED true)}")"
BIGTABLE_MIRROR_WRITES_ENABLED="$(require_bool TR_BIGTABLE_MIRROR_WRITES_ENABLED \
  "${TR_BIGTABLE_MIRROR_WRITES_ENABLED:-$(serving_env_value TR_BIGTABLE_MIRROR_WRITES_ENABLED true)}")"
if [ "$STORAGE_BACKEND" = spanner-clickhouse ] && {
  [ "$ANALYTICS_READ_MODE" != clickhouse-only ] ||
  [ "$GENERATION_RECORDS_ENABLED" != true ] ||
  [ "$BIGTABLE_MIRROR_WRITES_ENABLED" != false ] ||
  [ "$REQUEST_RECORD_WRITE_MODE" != typed ];
}; then
  echo "ERROR: spanner-clickhouse requires clickhouse-only, typed records, generation records, and no Bigtable mirror" >&2
  exit 1
fi

verify_policy_roles() {
  local label="$1" member="$2" expected_role="$3"
  shift 3
  local policy actual expected
  policy="$("$@")" || { echo "ERROR: cannot read ${label} IAM" >&2; return 1; }
  actual="$(jq -c --arg member "$member" '[
    .bindings[]? | select(any(.members[]?; . == $member))
    | {role, condition: (.condition // null)}
  ] | unique' <<<"$policy")" || return 1
  if [ -n "$expected_role" ]; then
    expected="$(jq -cn --arg role "$expected_role" '[{role:$role,condition:null}]')"
  else
    expected='[]'
  fi
  [ "$actual" = "$expected" ] || {
    echo "ERROR: ${member} has ${label} IAM ${actual}, expected ${expected}" >&2
    return 1
  }
}

PROJECT_POLICY_JSON="$(gc projects get-iam-policy "$PROJECT_ID" --format=json)"
SPANNER_POLICY_JSON="$(gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" --format=json)"
BIGTABLE_POLICY_JSON="$(gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID" --format=json)"
BYOK_POLICY_JSON="$(gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION" --format=json)"
GOOGLE_ADS_POLICY_JSON="$(gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION" --format=json)"

for surface in "${SURFACES[@]}"; do
  account="$(surface_account "$surface")"
  member="serviceAccount:${account}"
  case "$surface" in actions) project_role="" ;; *) project_role="roles/serviceusage.serviceUsageConsumer" ;; esac
  verify_policy_roles "project" "$member" "$project_role" printf '%s' "$PROJECT_POLICY_JSON"
  case "$surface" in
    public|chat) spanner_role="roles/spanner.databaseReader" ;;
    console|webhooks|internal) spanner_role="roles/spanner.databaseUser" ;;
    actions) spanner_role="" ;;
  esac
  verify_policy_roles "Spanner database" "$member" "$spanner_role" printf '%s' "$SPANNER_POLICY_JSON"
  case "$surface" in
    public|console) bigtable_role="roles/bigtable.reader" ;;
    internal) bigtable_role="roles/bigtable.user" ;;
    *) bigtable_role="" ;;
  esac
  verify_policy_roles "Bigtable instance" "$member" "$bigtable_role" printf '%s' "$BIGTABLE_POLICY_JSON"
  case "$surface" in
    console) byok_role="roles/cloudkms.cryptoKeyEncrypterDecrypter" ;;
    internal) byok_role="roles/cloudkms.cryptoKeyDecrypter" ;;
    *) byok_role="" ;;
  esac
  verify_policy_roles "BYOK KMS key" "$member" "$byok_role" printf '%s' "$BYOK_POLICY_JSON"
  case "$surface" in console) ads_role="roles/cloudkms.cryptoKeyEncrypter" ;; *) ads_role="" ;; esac
  verify_policy_roles "Google Ads KMS key" "$member" "$ads_role" printf '%s' "$GOOGLE_ADS_POLICY_JSON"
done

# Direct org/folder grants are inherited project power and must not silently
# defeat the resource-level matrix above. This audit intentionally fails if
# the deploy identity cannot read an ancestor policy.
ANCESTORS_JSON="$(gc projects get-ancestors "$PROJECT_ID" --format=json)"
ANCESTOR_ROWS="$(jq -r '.[] | [.type, (.id|tostring)] | @tsv' <<<"$ANCESTORS_JSON")" || exit 1
while IFS=$'\t' read -r ancestor_type ancestor_id; do
  [ -n "$ancestor_type" ] || continue
  case "$ancestor_type" in
    folder) ancestor_policy="$(gc resource-manager folders get-iam-policy "$ancestor_id" --format=json)" ;;
    organization) ancestor_policy="$(gc organizations get-iam-policy "$ancestor_id" --format=json)" ;;
    project) continue ;;
    *) echo "ERROR: unknown project ancestor type ${ancestor_type}" >&2; exit 1 ;;
  esac
  for runtime_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
    verify_policy_roles "${ancestor_type}/${ancestor_id}" \
      "serviceAccount:${runtime_account}" "" printf '%s' "$ancestor_policy"
  done
done <<<"$ANCESTOR_ROWS"

OPERATIONAL_CLICKHOUSE_URL="${TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL:-$(serving_env_value TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL)}"
PROVIDER_CLICKHOUSE_URL="${TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL:-$(serving_env_value TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL)}"
if [ "$ANALYTICS_READ_MODE" != bigtable ] && [ -z "$OPERATIONAL_CLICKHOUSE_URL" ]; then
  echo "ERROR: non-Bigtable analytics mode requires the serving operational ClickHouse URL" >&2
  exit 1
fi
if [ -z "$PROVIDER_CLICKHOUSE_URL" ]; then
  clickhouse_address_output=""
  if clickhouse_address_output="$(gc compute addresses describe tr-clickhouse-ilb \
      --region=us-central1 --format='value(address)' 2>&1)"; then
    PROVIDER_CLICKHOUSE_URL="http://${clickhouse_address_output}:8123"
  else
    echo "ERROR: provider analytics needs an explicit private URL or the tr-clickhouse-ilb address" >&2
    exit 1
  fi
fi
python3 - "$PROVIDER_CLICKHOUSE_URL" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
parsed = urlsplit(value)
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.password
    or parsed.query
    or parsed.fragment
):
    raise SystemExit("provider ClickHouse URL is malformed")
try:
    private_host = ipaddress.ip_address(parsed.hostname).is_private
except ValueError:
    private_host = parsed.hostname.endswith((".internal", ".svc"))
if not private_host:
    raise SystemExit("provider ClickHouse URL must resolve through a private VPC address")
PY

secret_state() {
  local secret="$1" describe_output version_json version_state version_name version
  if ! describe_output="$(gc secrets describe "$secret" 2>&1)"; then
    case "$describe_output" in
      *NOT_FOUND*|*"not found"*) echo absent; return 0 ;;
      *) echo "ERROR: cannot determine Secret Manager state for ${secret}" >&2; return 1 ;;
    esac
  fi
  if ! version_json="$(gc secrets versions describe latest --secret="$secret" --format=json 2>&1)"; then
    echo "ERROR: cannot determine latest-version state for ${secret}" >&2
    return 1
  fi
  version_state="$(jq -er '.state' <<<"$version_json")" || {
    echo "ERROR: latest version metadata for ${secret} has no state" >&2
    return 1
  }
  if [ "$version_state" = ENABLED ]; then
    version_name="$(jq -er '.name' <<<"$version_json")" || return 1
    version="${version_name##*/}"
    [[ "$version" =~ ^[1-9][0-9]*$ ]] || {
      echo "ERROR: latest enabled version for ${secret} is not a numeric immutable version" >&2
      return 1
    }
    if ! awk -F '\t' -v secret="$secret" '$1 == secret {found=1} END {exit !found}' \
        "$SECRET_VERSIONS_FILE"; then
      printf '%s\t%s\n' "$secret" "$version" >>"$SECRET_VERSIONS_FILE"
    elif [ "$(awk -F '\t' -v secret="$secret" '$1 == secret {print $2}' "$SECRET_VERSIONS_FILE")" != "$version" ]; then
      echo "ERROR: latest version for ${secret} changed during rollout preflight" >&2
      return 1
    fi
    echo present
  else
    echo "ERROR: latest version for ${secret} is not ENABLED" >&2
    return 1
  fi
}
pinned_secret_version() {
  local secret="$1" version state
  version="$(awk -F '\t' -v secret="$secret" '$1 == secret {print $2}' "$SECRET_VERSIONS_FILE")"
  if [ -z "$version" ]; then
    state="$(secret_state "$secret")" || return 1
    [ "$state" = present ] || {
      echo "ERROR: cannot pin absent secret ${secret}" >&2
      return 1
    }
    version="$(awk -F '\t' -v secret="$secret" '$1 == secret {print $2}' "$SECRET_VERSIONS_FILE")"
  fi
  [[ "$version" =~ ^[1-9][0-9]*$ ]] || return 1
  echo "$version"
}
pin_surface_secret_versions() {
  local binding env_name resource version
  local pinned=()
  for binding in "${SURFACE_SECRETS[@]}"; do
    env_name="${binding%%=*}"
    resource="${binding#*=}"
    version="$(pinned_secret_version "$resource")" || return 1
    pinned+=("${env_name}=${resource}:${version}")
  done
  SURFACE_SECRETS=("${pinned[@]}")
}
runtime_account_for_member() {
  surface_account "$1"
}
allowed_surfaces_for_secret() {
  case "$1" in
    trustedrouter-attribution-cookie-secret) echo "public console" ;;
    trustedrouter-sentry-dsn) echo "console chat webhooks internal" ;;
    trustedrouter-stripe-secret-key) echo "console" ;;
    trustedrouter-internal-stripe-payment-intents-key) echo "internal" ;;
    trustedrouter-stripe-webhook-secret) echo "webhooks" ;;
    trustedrouter-internal-gateway-token|trustedrouter-observer-internal-token|trustedrouter-synthetic-monitor-api-key|trustedrouter-federation-peer-token|trustedrouter-federation-home-token|trustedrouter-federation-credit-inbound-token|trustedrouter-federation-credit-peer-token|trustedrouter-federation-settlement-inbound-tokens|trustedrouter-federation-settlement-home-token) echo "internal" ;;
    trustedrouter-aws-access-key-id|trustedrouter-aws-secret-access-key) echo "actions console" ;;
    trustedrouter-internal-ses-access-key-id|trustedrouter-internal-ses-secret-access-key) echo "internal" ;;
    trustedrouter-ops-chat-webhook-secret) echo "actions" ;;
    trustedrouter-google-client-id|trustedrouter-google-client-secret|trustedrouter-google-alias-credentials-json|trustedrouter-github-client-id|trustedrouter-github-client-secret|trustedrouter-github-alias-credentials-json) echo "console" ;;
    trustedrouter-paypal-client-id|trustedrouter-paypal-client-secret) echo "console webhooks" ;;
    trustedrouter-paypal-webhook-id) echo "webhooks" ;;
    trustedrouter-adyen-test-api-key|trustedrouter-adyen-test-client-key) echo "console" ;;
    trustedrouter-adyen-test-hmac-key) echo "webhooks" ;;
    trustedrouter-adyen-test-reference-key) echo "console webhooks" ;;
    trustedrouter-veriff-api-key) echo "console" ;;
    trustedrouter-veriff-shared-secret-key) echo "webhooks" ;;
    trustedrouter-telnyx-api-key|trustedrouter-twilio-account-sid|trustedrouter-twilio-api-key-sid|trustedrouter-twilio-api-key-secret|trustedrouter-twilio-auth-token) echo "console" ;;
    trustedrouter-clickhouse-provider-read-password) echo "console" ;;
    trustedrouter-clickhouse-control-read-password) echo "public console internal" ;;
    trustedrouter-clickhouse-password) echo "console internal" ;;
    trustedrouter-axiom-api-token) echo "" ;;
    trustedrouter-anthropic-api-key|trustedrouter-openai-api-key|trustedrouter-openai-video-api-key|trustedrouter-gemini-api-key|trustedrouter-cerebras-api-key|trustedrouter-deepseek-api-key|trustedrouter-mistral-api-key|trustedrouter-kimi-api-key|trustedrouter-zai-api-key|trustedrouter-together-api-key|trustedrouter-fireworks-api-key|trustedrouter-deepinfra-api-key|trustedrouter-cohere-api-key|trustedrouter-voyage-api-key|trustedrouter-xiaomi-api-key|trustedrouter-grok-api-key|trustedrouter-novita-api-key|trustedrouter-phala-api-key|trustedrouter-phala-confidential-api-key|trustedrouter-siliconflow-api-key|trustedrouter-tinfoil-api-key|trustedrouter-venice-api-key|trustedrouter-nebius-api-key|trustedrouter-minimax-api-key|trustedrouter-friendli-api-key|trustedrouter-baseten-api-key|trustedrouter-thinking-machines-api-key|trustedrouter-wafer-api-key|trustedrouter-crusoe-api-key|trustedrouter-makora-api-key|trustedrouter-alibaba-api-key|trustedrouter-ltx-api-key|trustedrouter-runway-api-key|trustedrouter-kling-api-key|trustedrouter-chutes-api-key|trustedrouter-digitalocean-api-key|trustedrouter-cloudflare-workers-ai-api-token|trustedrouter-inceptron-api-key|trustedrouter-morph-api-key|trustedrouter-atlas-cloud-api-key|trustedrouter-streamlake-api-key|trustedrouter-neurometric-api-key|trustedrouter-engy-api-key|trustedrouter-pearl-api-key|trustedrouter-zero-g-api-key|trustedrouter-athena-worker-prompt-v1|trustedrouter-gcp-service-account-key-json) echo "" ;;
    *) echo "ERROR: secret ${1} has no declared six-surface owner set" >&2; return 1 ;;
  esac
}
preflight_secret_iam() {
  local secret="$1" requirement="${2:-required}" allowed policy surface account member should_have state direct_bindings
  state="$(secret_state "$secret")" || return 1
  if [ "$state" = absent ]; then
    [ "$requirement" = optional ] && return 0
    echo "ERROR: required enabled secret is missing: ${secret}" >&2
    return 1
  fi
  allowed="$(allowed_surfaces_for_secret "$secret")"
  policy="$(gc secrets get-iam-policy "$secret" --format=json)"
  for surface in "${SURFACES[@]}"; do
    account="$(runtime_account_for_member "$surface")"
    member="serviceAccount:${account}"
    should_have=0
    case " ${allowed} " in *" ${surface} "*) should_have=1 ;; esac
    direct_bindings="$(jq -c --arg member "$member" '[
      .bindings[]?
      | select(any(.members[]?; . == $member))
      | {role, condition: (.condition // null)}
    ] | unique' <<<"$policy")" || return 1
    if [ "$should_have" = 1 ]; then
      [ "$direct_bindings" = '[{"role":"roles/secretmanager.secretAccessor","condition":null}]' ] || {
        echo "ERROR: ${secret} must grant ${account} exactly one unconditional secretAccessor role" >&2
        return 1
      }
    elif [ "$direct_bindings" != '[]' ]; then
      echo "ERROR: ${secret} grants an unauthorized direct role to ${account}" >&2
      return 1
    fi
  done
}

OPTIONAL_CONSOLE_SECRET_GROUPS=()
TELNYX_STATE="$(secret_state trustedrouter-telnyx-api-key)" || exit 1
if [ "$TELNYX_STATE" = present ]; then
  OPTIONAL_CONSOLE_SECRET_GROUPS+=("TR_TELNYX_API_KEY=trustedrouter-telnyx-api-key")
fi
TWILIO_COUNT=0
for twilio_secret in trustedrouter-twilio-account-sid trustedrouter-twilio-api-key-sid trustedrouter-twilio-api-key-secret trustedrouter-twilio-auth-token; do
  TWILIO_STATE="$(secret_state "$twilio_secret")" || exit 1
  [ "$TWILIO_STATE" = present ] && TWILIO_COUNT=$((TWILIO_COUNT + 1))
done
if [ "$TWILIO_COUNT" -ne 0 ] && [ "$TWILIO_COUNT" -ne 4 ]; then
  echo "ERROR: Twilio secret group is partially provisioned" >&2
  exit 1
fi
if [ "$TWILIO_COUNT" -eq 4 ]; then
  OPTIONAL_CONSOLE_SECRET_GROUPS+=(
    "TR_TWILIO_ACCOUNT_SID=trustedrouter-twilio-account-sid"
    "TR_TWILIO_API_KEY_SID=trustedrouter-twilio-api-key-sid"
    "TR_TWILIO_API_KEY_SECRET=trustedrouter-twilio-api-key-secret"
    "TR_TWILIO_AUTH_TOKEN=trustedrouter-twilio-auth-token"
  )
fi

provider_secret_group_state() {
  local provider="$1" enabled="$2"
  shift 2
  local resource state present=0 total="$#"
  for resource in "$@"; do
    state="$(secret_state "$resource")" || return 1
    [ "$state" = present ] && present=$((present + 1))
  done
  if [ "$present" -eq "$total" ]; then
    echo present
    return 0
  fi
  if [ "$present" -eq 0 ] && [ "$enabled" = false ]; then
    echo absent
    return 0
  fi
  echo "ERROR: ${provider} credentials must be complete when enabled and may only be fully absent when disabled" >&2
  return 1
}

PAYPAL_SECRET_GROUP_STATE="$(provider_secret_group_state PayPal "$PAYPAL_CHECKOUT_ENABLED" \
  trustedrouter-paypal-client-id trustedrouter-paypal-client-secret \
  trustedrouter-paypal-webhook-id)" || exit 1
ADYEN_SECRET_GROUP_STATE="$(provider_secret_group_state Adyen "$ADYEN_ENABLED" \
  trustedrouter-adyen-test-api-key trustedrouter-adyen-test-client-key \
  trustedrouter-adyen-test-hmac-key trustedrouter-adyen-test-reference-key)" || exit 1
VERIFF_SECRET_GROUP_STATE="$(provider_secret_group_state Veriff "$VERIFF_ENABLED" \
  trustedrouter-veriff-api-key trustedrouter-veriff-shared-secret-key)" || exit 1

TELNYX_FROM_NUMBER="${TR_TELNYX_FROM_NUMBER:-$(serving_env_value TR_TELNYX_FROM_NUMBER +17869471547)}"
TELNYX_TEXML_ACCOUNT_ID="${TR_TELNYX_TEXML_ACCOUNT_ID:-$(serving_env_value TR_TELNYX_TEXML_ACCOUNT_ID 1eea716a-02e0-4d4f-96fa-36d1f556edca)}"
TELNYX_TEXML_APPLICATION_ID="${TR_TELNYX_TEXML_APPLICATION_ID:-$(serving_env_value TR_TELNYX_TEXML_APPLICATION_ID 3026758434193146987)}"
TWILIO_FROM_NUMBER="${TR_TWILIO_FROM_NUMBER:-$(serving_env_value TR_TWILIO_FROM_NUMBER +15055313623)}"
if [ "$TELNYX_STATE" = present ] && {
  [ -z "$TELNYX_FROM_NUMBER" ] || [ -z "$TELNYX_TEXML_ACCOUNT_ID" ] || [ -z "$TELNYX_TEXML_APPLICATION_ID" ];
}; then
  echo "ERROR: Telnyx credential exists but nonsecret telephony identifiers are incomplete" >&2
  exit 1
fi
if [ "$TWILIO_COUNT" -eq 4 ] && [ -z "$TWILIO_FROM_NUMBER" ]; then
  echo "ERROR: Twilio credentials exist but TR_TWILIO_FROM_NUMBER is empty" >&2
  exit 1
fi

surface_secret_bindings() {
  local surface="$1"
  SURFACE_SECRETS=()
  case "$surface" in
    public)
      SURFACE_SECRETS+=("TR_ATTRIBUTION_COOKIE_SECRET=trustedrouter-attribution-cookie-secret")
      ;;
    actions)
      SURFACE_SECRETS+=(
        "TR_AWS_ACCESS_KEY_ID=trustedrouter-aws-access-key-id"
        "TR_AWS_SECRET_ACCESS_KEY=trustedrouter-aws-secret-access-key"
        "TR_OPS_CHAT_WEBHOOK_SECRET=trustedrouter-ops-chat-webhook-secret"
      )
      ;;
    console)
      SURFACE_SECRETS+=(
        "TR_SENTRY_DSN=trustedrouter-sentry-dsn"
        "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key"
        "TR_ATTRIBUTION_COOKIE_SECRET=trustedrouter-attribution-cookie-secret"
        "TR_AWS_ACCESS_KEY_ID=trustedrouter-aws-access-key-id"
        "TR_AWS_SECRET_ACCESS_KEY=trustedrouter-aws-secret-access-key"
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD=trustedrouter-clickhouse-provider-read-password"
      )
      if [ "${#OPTIONAL_CONSOLE_SECRET_GROUPS[@]}" -gt 0 ]; then
        SURFACE_SECRETS+=("${OPTIONAL_CONSOLE_SECRET_GROUPS[@]}")
      fi
      if [ "$GOOGLE_OAUTH_AVAILABLE" = true ]; then
        SURFACE_SECRETS+=(
          "TR_GOOGLE_CLIENT_ID=trustedrouter-google-client-id"
          "TR_GOOGLE_CLIENT_SECRET=trustedrouter-google-client-secret"
          "TR_GOOGLE_ALIAS_CREDENTIALS_JSON=trustedrouter-google-alias-credentials-json"
        )
      fi
      if [ "$GITHUB_OAUTH_AVAILABLE" = true ]; then
        SURFACE_SECRETS+=(
          "TR_GITHUB_CLIENT_ID=trustedrouter-github-client-id"
          "TR_GITHUB_CLIENT_SECRET=trustedrouter-github-client-secret"
          "TR_GITHUB_ALIAS_CREDENTIALS_JSON=trustedrouter-github-alias-credentials-json"
        )
      fi
      # A false provider gate prevents new sessions. A complete preexisting
      # group remains mounted for in-flight work and late signed callbacks;
      # a never-configured disabled group stays absent.
      if [ "$PAYPAL_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=(
          "TR_PAYPAL_CLIENT_ID=trustedrouter-paypal-client-id"
          "TR_PAYPAL_CLIENT_SECRET=trustedrouter-paypal-client-secret"
        )
      fi
      if [ "$ADYEN_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=(
          "TR_ADYEN_API_KEY=trustedrouter-adyen-test-api-key"
          "TR_ADYEN_CLIENT_KEY=trustedrouter-adyen-test-client-key"
          "TR_ADYEN_REFERENCE_KEY=trustedrouter-adyen-test-reference-key"
        )
      fi
      if [ "$VERIFF_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=("TR_VERIFF_API_KEY=trustedrouter-veriff-api-key")
      fi
      ;;
    chat)
      SURFACE_SECRETS+=("TR_SENTRY_DSN=trustedrouter-sentry-dsn")
      ;;
    webhooks)
      SURFACE_SECRETS+=(
        "TR_SENTRY_DSN=trustedrouter-sentry-dsn"
        "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret"
      )
      if [ "$PAYPAL_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=(
          "TR_PAYPAL_CLIENT_ID=trustedrouter-paypal-client-id"
          "TR_PAYPAL_CLIENT_SECRET=trustedrouter-paypal-client-secret"
          "TR_PAYPAL_WEBHOOK_ID=trustedrouter-paypal-webhook-id"
        )
      fi
      if [ "$ADYEN_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=(
          "TR_ADYEN_HMAC_KEY=trustedrouter-adyen-test-hmac-key"
          "TR_ADYEN_REFERENCE_KEY=trustedrouter-adyen-test-reference-key"
        )
      fi
      if [ "$VERIFF_SECRET_GROUP_STATE" = present ]; then
        SURFACE_SECRETS+=("TR_VERIFF_SHARED_SECRET_KEY=trustedrouter-veriff-shared-secret-key")
      fi
      ;;
    internal)
      SURFACE_SECRETS+=(
        "TR_SENTRY_DSN=trustedrouter-sentry-dsn"
        "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token"
        "TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token"
        "TR_SYNTHETIC_MONITOR_API_KEY=trustedrouter-synthetic-monitor-api-key"
        "TR_STRIPE_SECRET_KEY=trustedrouter-internal-stripe-payment-intents-key"
        "TR_AWS_ACCESS_KEY_ID=trustedrouter-internal-ses-access-key-id"
        "TR_AWS_SECRET_ACCESS_KEY=trustedrouter-internal-ses-secret-access-key"
      )
      for optional_pair in \
        "TR_FEDERATION_PEER_TOKEN=trustedrouter-federation-peer-token" \
        "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS=trustedrouter-federation-settlement-inbound-tokens"; do
        optional_resource="${optional_pair#*=}"
        OPTIONAL_STATE="$(secret_state "$optional_resource")" || return 1
        [ "$OPTIONAL_STATE" = present ] && SURFACE_SECRETS+=("$optional_pair")
      done
      ;;
  esac
  if [ "$ANALYTICS_READ_MODE" != bigtable ]; then
    case "$surface" in public|console|internal)
      SURFACE_SECRETS+=("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD=trustedrouter-clickhouse-control-read-password") ;;
    esac
  fi
  pin_surface_secret_versions || return 1
}

# Read-only Secret Manager + IAM checks finish before any backend or revision
# mutation. Complete --set-secrets later strips every stale binding.
SECRETS_TO_PREFLIGHT=()
for surface in "${SURFACES[@]}"; do
  surface_secret_bindings "$surface"
  for binding in "${SURFACE_SECRETS[@]}"; do
    resource="${binding#*=}"; resource="${resource%:*}"
    case " ${SECRETS_TO_PREFLIGHT[*]-} " in *" ${resource} "*) ;; *) SECRETS_TO_PREFLIGHT+=("$resource") ;; esac
  done
done
for resource in "${SECRETS_TO_PREFLIGHT[@]}"; do preflight_secret_iam "$resource" required; done

# Omission from a container environment is not an isolation boundary if the
# runtime identity can still fetch the resource. Existing but unmounted
# provider/logging/legacy resources therefore receive the same exact-owner IAM
# audit; all upstream inference keys deliberately have an empty six-service
# owner set because chat forwards caller authorization to the attested gateway.
KNOWN_OPTIONAL_RUNTIME_SECRETS=(
  trustedrouter-google-client-id trustedrouter-google-client-secret
  trustedrouter-google-alias-credentials-json trustedrouter-github-client-id
  trustedrouter-github-client-secret trustedrouter-github-alias-credentials-json
  trustedrouter-paypal-client-id trustedrouter-paypal-client-secret
  trustedrouter-paypal-webhook-id trustedrouter-adyen-test-api-key
  trustedrouter-adyen-test-client-key trustedrouter-adyen-test-hmac-key
  trustedrouter-adyen-test-reference-key trustedrouter-veriff-api-key
  trustedrouter-veriff-shared-secret-key trustedrouter-telnyx-api-key
  trustedrouter-twilio-account-sid trustedrouter-twilio-api-key-sid
  trustedrouter-twilio-api-key-secret trustedrouter-twilio-auth-token
  trustedrouter-clickhouse-password trustedrouter-axiom-api-token
  trustedrouter-federation-home-token trustedrouter-federation-credit-inbound-token
  trustedrouter-federation-credit-peer-token trustedrouter-federation-settlement-home-token
  trustedrouter-anthropic-api-key trustedrouter-openai-api-key
  trustedrouter-openai-video-api-key trustedrouter-gemini-api-key
  trustedrouter-cerebras-api-key trustedrouter-deepseek-api-key
  trustedrouter-mistral-api-key trustedrouter-kimi-api-key trustedrouter-zai-api-key
  trustedrouter-together-api-key trustedrouter-fireworks-api-key
  trustedrouter-deepinfra-api-key trustedrouter-cohere-api-key
  trustedrouter-voyage-api-key trustedrouter-xiaomi-api-key trustedrouter-grok-api-key
  trustedrouter-novita-api-key trustedrouter-phala-api-key
  trustedrouter-phala-confidential-api-key trustedrouter-siliconflow-api-key
  trustedrouter-tinfoil-api-key trustedrouter-venice-api-key trustedrouter-nebius-api-key
  trustedrouter-minimax-api-key trustedrouter-friendli-api-key trustedrouter-baseten-api-key
  trustedrouter-thinking-machines-api-key trustedrouter-wafer-api-key
  trustedrouter-crusoe-api-key trustedrouter-makora-api-key trustedrouter-alibaba-api-key
  trustedrouter-ltx-api-key trustedrouter-runway-api-key trustedrouter-kling-api-key
  trustedrouter-chutes-api-key trustedrouter-digitalocean-api-key
  trustedrouter-cloudflare-workers-ai-api-token trustedrouter-inceptron-api-key
  trustedrouter-morph-api-key trustedrouter-atlas-cloud-api-key
  trustedrouter-streamlake-api-key trustedrouter-neurometric-api-key
  trustedrouter-engy-api-key trustedrouter-pearl-api-key trustedrouter-zero-g-api-key
  trustedrouter-athena-worker-prompt-v1 trustedrouter-gcp-service-account-key-json
)
for resource in "${KNOWN_OPTIONAL_RUNTIME_SECRETS[@]}"; do
  case " ${SECRETS_TO_PREFLIGHT[*]-} " in *" ${resource} "*) ;; *)
    preflight_secret_iam "$resource" optional ;;
  esac
done

# Re-run the shared complete isolation audit immediately before staging.  This
# covers parent scopes, cross-runtime impersonation, and unknown project
# secrets that the surface-specific environment builder cannot enumerate.
bash "$IAM_VERIFY" --project "$PROJECT_ID"

if ! IMAGE_METADATA="$(gc artifacts docker images describe "$IMAGE" --format=json 2>&1)"; then
  echo "ERROR: image does not exist: ${IMAGE}" >&2
  exit 1
fi
RESOLVED_IMAGE="$(jq -er '
  .image_summary.fully_qualified_digest //
  .imageSummary.fullyQualifiedDigest //
  .fully_qualified_digest //
  .fullyQualifiedDigest
' <<<"$IMAGE_METADATA")" || {
  echo "ERROR: Artifact Registry did not return a fully qualified image digest" >&2
  exit 1
}
[[ "$RESOLVED_IMAGE" =~ ^[^,\|[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "ERROR: Artifact Registry returned an invalid immutable image digest" >&2
  exit 1
}
if [[ "$IMAGE" == *@* ]] && [ "$IMAGE" != "$RESOLVED_IMAGE" ]; then
  echo "ERROR: requested image digest differs from the Artifact Registry resolution" >&2
  exit 1
fi
IMAGE="$RESOLVED_IMAGE"

preflight_private_internal_origin() {
  local network="${TR_SYNTHETIC_NETWORK:-default}"
  local subnet="${TR_SYNTHETIC_SUBNET:-default}"
  local zone="${TR_PRIVATE_RUN_APP_DNS_ZONE:-trusted-router-private-run-app}"
  local region subnet_json zone_json apex_json wildcard_json
  validate_resource_name "synthetic VPC network" "$network"
  validate_resource_name "synthetic VPC subnet" "$subnet"
  validate_resource_name "private run.app DNS zone" "$zone"
  while IFS= read -r region; do
    subnet_json="$(gc compute networks subnets describe "$subnet" \
      --region="$region" --format=json)" || return 1
    jq -e --arg network "$network" '
      .privateIpGoogleAccess == true and
      ((.network // "") | rtrimstr("/") | endswith("/networks/" + $network))
    ' <<<"$subnet_json" >/dev/null || {
      echo "ERROR: ${subnet}/${region} lacks Private Google Access on ${network}" >&2
      return 1
    }
  done
  zone_json="$(gc dns managed-zones describe "$zone" --format=json)" || return 1
  jq -e --arg network "$network" '
    .dnsName == "run.app." and .visibility == "private" and
    any(.privateVisibilityConfig.networks[]?;
      (.networkUrl // "") | rtrimstr("/") | endswith("/networks/" + $network))
  ' <<<"$zone_json" >/dev/null || {
    echo "ERROR: private run.app DNS zone does not match the synthetic VPC" >&2
    return 1
  }
  apex_json="$(gc dns record-sets describe run.app. --zone="$zone" --type=A --format=json)" || return 1
  jq -e '
    (.rrdatas | sort) == [
      "199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"
    ]
  ' <<<"$apex_json" >/dev/null || {
    echo "ERROR: private run.app A record does not use the restricted Google VIP" >&2
    return 1
  }
  wildcard_json="$(gc dns record-sets describe '*.run.app.' \
    --zone="$zone" --type=CNAME --format=json)" || return 1
  jq -e '.rrdatas == ["run.app."]' <<<"$wildcard_json" >/dev/null || {
    echo "ERROR: private wildcard run.app record is absent or drifted" >&2
    return 1
  }
}
preflight_private_internal_origin

# The first split adopts an already promoted private internal service and
# verifies that every canonical synthetic Job/Scheduler pair uses it. The
# legacy monolith remains untouched and keeps its run.app URL throughout this
# transaction; existing splits have already crossed this one-way boundary and
# do not depend on a historical bootstrap artifact.
BOOTSTRAP_ARTIFACT_SHA256=""
if [ "$ROLLOUT_MODE" = initial_split ]; then
  [ -n "${TR_INTERNAL_BOOTSTRAP_ARTIFACT:-}" ] || {
    echo "ERROR: initial split requires TR_INTERNAL_BOOTSTRAP_ARTIFACT" >&2
    exit 1
  }
  bash "${SCRIPT_DIR}/rollout_bootstrap_internal.sh" \
    --verify-artifact "$TR_INTERNAL_BOOTSTRAP_ARTIFACT" \
    --expected-image "$IMAGE"
  BOOTSTRAP_ARTIFACT_SHA256="$(python3 - "$TR_INTERNAL_BOOTSTRAP_ARTIFACT" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)" || exit 1
fi

bootstrap_internal_revision() {
  local region="$1"
  [ "$ROLLOUT_MODE" = initial_split ] || return 2
  jq -er --arg region "$region" '
    [.services[] | select(.region == $region) | .revision] as $matches
    | if ($matches | length) == 1 then $matches[0]
      else error("bootstrap artifact has inexact regional service inventory") end
  ' "$TR_INTERNAL_BOOTSTRAP_ARTIFACT"
}

surface_service_max_preflight() {
  case "$1" in
    public) echo 10 ;; actions) echo 2 ;; console|chat) echo 20 ;;
    webhooks) echo 10 ;; internal) echo 50 ;; *) return 2 ;;
  esac
}

verify_existing_service_metadata_is_safe() {
  local surface="$1" region="$2" prior="$3" service maximum allow_legacy_url iam
  service="$(surface_service "$surface")"
  maximum="$(surface_service_max_preflight "$surface")"
  allow_legacy_url=false
  python3 - "$prior" "$surface" "$TR_CLOUD_RUN_INGRESS" "$maximum" \
    "$allow_legacy_url" <<'PY' || return 1
import json
import sys
from pathlib import Path

path, surface, ingress, maximum, allow_legacy_url = sys.argv[1:]
service = json.loads(Path(path).read_text(encoding="utf-8"))
metadata = service.get("metadata") or {}
annotations = metadata.get("annotations") or {}
spec = service.get("spec") or {}
status = service.get("status") or {}
if metadata.get("generation") is None or status.get("observedGeneration") is None:
    raise SystemExit(f"{surface}: existing service generation metadata is absent")
if str(metadata.get("generation")) != str(status.get("observedGeneration")):
    raise SystemExit(f"{surface}: existing service has an unobserved metadata change")
if annotations.get("run.googleapis.com/ingress") != ingress:
    raise SystemExit(f"{surface}: existing ingress would be changed by staging")
if annotations.get("run.googleapis.com/ingress-status") != ingress:
    raise SystemExit(f"{surface}: existing effective ingress differs from desired ingress")
service_max = (spec.get("scaling") or {}).get("maxInstanceCount")
if service_max is None:
    service_max = annotations.get("run.googleapis.com/maxScale")
if str(service_max) != maximum:
    raise SystemExit(f"{surface}: existing service-level max would be changed by staging")
disabled = str(annotations.get("run.googleapis.com/default-url-disabled") or "").lower() == "true"
url = status.get("url") or ""
if surface == "internal":
    if disabled or not url:
        raise SystemExit("internal: private run.app origin must remain enabled")
elif allow_legacy_url == "true":
    if disabled != (not url):
        raise SystemExit("console: default URL annotation/status are inconsistent")
else:
    if not disabled or url:
        raise SystemExit(f"{surface}: default URL would be changed by staging")
PY
  iam="$(gc run services get-iam-policy "$service" --region="$region" --format=json)" || return 1
  jq -e '
    [.bindings[]? | select(any(.members[]?; . == "allUsers"))
      | {role, condition: (.condition // null),
         allUsersCount: ([.members[]? | select(. == "allUsers")] | length)}]
    == [{role:"roles/run.invoker",condition:null,allUsersCount:1}]
  ' <<<"$iam" >/dev/null || {
    echo "ERROR: ${service}/${region} service IAM would be changed by staging" >&2
    return 1
  }
}

stage_configuration_sha256() {
  python3 - "$0" "$SECRET_VERSIONS_FILE" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
    "$REGION_CSV" "$GATEWAY_REGION_CSV" "$INTERNAL_ALLOWED_REGION_CSV" \
    "$STORAGE_BACKEND" "$REQUEST_RECORD_WRITE_MODE" "$ANALYTICS_READ_MODE" \
    "$GENERATION_RECORDS_ENABLED" "$BIGTABLE_MIRROR_WRITES_ENABLED" \
    "$NEW_SIGNUPS_ENABLED" "$GOOGLE_OAUTH_AVAILABLE" "$GITHUB_OAUTH_AVAILABLE" \
    "$PAYPAL_CHECKOUT_ENABLED" "$ADYEN_ENABLED" "$VERIFF_ENABLED" \
    "$TELNYX_FROM_NUMBER" "$TELNYX_TEXML_ACCOUNT_ID" \
    "$TELNYX_TEXML_APPLICATION_ID" "$TWILIO_FROM_NUMBER" <<'PY'
import hashlib
import sys
from pathlib import Path
pins = sorted(
    line for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line
)
payload = Path(sys.argv[1]).read_bytes() + b"\0" + "\n".join(pins).encode()
payload += b"\0" + b"\0".join(item.encode() for item in sys.argv[3:])
print(hashlib.sha256(payload).hexdigest())
PY
}

restore_stage_journal_secret_pins() {
  local restored="${WORK_DIR}/journal-secret-versions.tsv"
  python3 - "$STAGE_JOURNAL" "$restored" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
descriptor = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    for item in value.get("pinned_secrets") or []:
        output.write(f"{item['resource']}\t{item['version']}\n")
PY
  mv "$restored" "$SECRET_VERSIONS_FILE"
  chmod 600 "$SECRET_VERSIONS_FILE"
  while IFS=$'\t' read -r resource version; do
    [ -n "$resource" ] || continue
    version_json="$(gc secrets versions describe "$version" \
      --secret="$resource" --format=json)" || return 1
    [ "$(jq -er 'select(.state == "ENABLED") | .name | split("/")[-1]' \
      <<<"$version_json")" = "$version" ] || {
      echo "ERROR: journaled secret version is no longer ENABLED: ${resource}:${version}" >&2
      return 1
    }
  done <"$SECRET_VERSIONS_FILE"
}

create_initial_stage_journal() {
  local config_sha="$1" plan_file="${WORK_DIR}/stage-services.tsv"
  local surface service region candidate prior_exists adopted
  : >"$plan_file"
  chmod 600 "$plan_file"
  for surface in "${SURFACES[@]}"; do
    service="$(surface_service "$surface")"
    while IFS= read -r region; do
      prior_exists=false
      [ -f "$(prior_json_path "$surface" "$region")" ] && prior_exists=true
      adopted=false
      if [ "$surface" = internal ]; then
        candidate="$(bootstrap_internal_revision "$region")" || return 1
        adopted=true
      else
        candidate="${service}-${REVISION_SUFFIX}"
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$surface" "$service" "$region" "$candidate" "$prior_exists" "$adopted" \
        >>"$plan_file"
    done < <(surface_region_lines "$surface")
  done
  python3 - "$STAGE_JOURNAL" "$MANIFEST" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
    "$REVISION_SUFFIX" "$REGION_CSV" "$TR_PRIMARY_REGION" "$GATEWAY_REGION_CSV" \
    "$INTERNAL_ALLOWED_REGION_CSV" "$HTTPS_PROXY" "$URL_MAP_NAME" \
    "$PRIOR_URL_MAP_NAME" "$(python3 "$STATE_TOOL" hash-url-map "$PRIOR_URL_MAP")" \
    "$BOOTSTRAP_ARTIFACT_SHA256" "$LEGACY_HARDENING_ARTIFACT_SHA256" \
    "$FRONTEND_ATTESTATION_SHA256" "$config_sha" "$SECRET_VERSIONS_FILE" \
    "$plan_file" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$STAGE_OPERATION_ID" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
pins = []
for line in Path(sys.argv[19]).read_text(encoding="utf-8").splitlines():
    if line:
        resource, version = line.split("\t")
        pins.append({"resource": resource, "version": version})
services = []
for line in Path(sys.argv[20]).read_text(encoding="utf-8").splitlines():
    surface, name, region, candidate, prior_exists, adopted = line.split("\t")
    services.append({
        "surface": surface,
        "name": name,
        "region": region,
        "candidate_revision": candidate,
        "prior_exists": prior_exists == "true",
        "adopted_bootstrap": adopted == "true",
        "state": "pending",
        "postcondition_sha256": None,
    })
value = {
    "schema_version": 1,
    "kind": "trusted-router-six-surface-initial-stage-state",
    "manifest_path": str(Path(sys.argv[2]).resolve()),
    "project_id": sys.argv[3],
    "image": sys.argv[4],
    "release": sys.argv[5],
    "revision_suffix": sys.argv[6],
    "rollout_mode": "initial_split",
    "regions": sys.argv[7].split(","),
    "primary_region": sys.argv[8],
    "gateway_regions": sys.argv[9].split(","),
    "internal_regions": sys.argv[10].split(","),
    "https_proxy": sys.argv[11],
    "url_map_name": sys.argv[12],
    "prior_snapshot": sys.argv[13],
    "prior_sha256": sys.argv[14],
    "bootstrap_artifact_sha256": sys.argv[15],
    "legacy_hardening_artifact_sha256": sys.argv[16],
    "frontend_attestation_sha256": sys.argv[17],
    "configuration_sha256": sys.argv[18],
    "pinned_secrets": sorted(pins, key=lambda item: item["resource"]),
    "created_at": sys.argv[21],
    "operation_id": sys.argv[22],
    "phase": "edge",
    "edge_states": [
        {"surface": surface, "state": "pending", "postcondition_sha256": None}
        for surface in ("public", "actions", "console", "chat", "webhooks", "internal")
    ],
    "service_states": services,
    "candidate_snapshot_sha256": None,
    "manifest_sha256": None,
}
payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.link(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
directory = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
    raise SystemExit("initial-stage journal mode differs")
PY
}

validate_initial_stage_journal() {
  local config_sha="$1"
  python3 - "$STAGE_JOURNAL" "$MANIFEST" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
    "$REVISION_SUFFIX" "$REGION_CSV" "$TR_PRIMARY_REGION" "$GATEWAY_REGION_CSV" \
    "$INTERNAL_ALLOWED_REGION_CSV" "$HTTPS_PROXY" "$URL_MAP_NAME" \
    "$PRIOR_URL_MAP_NAME" "$(python3 "$STATE_TOOL" hash-url-map "$PRIOR_URL_MAP")" \
    "$BOOTSTRAP_ARTIFACT_SHA256" "$LEGACY_HARDENING_ARTIFACT_SHA256" \
    "$FRONTEND_ATTESTATION_SHA256" "$config_sha" "$STAGE_OPERATION_ID" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("initial-stage journal must be a regular mode-0600 file")
value = json.loads(path.read_text(encoding="utf-8"))
required = {
    "schema_version", "kind", "manifest_path", "project_id", "image", "release",
    "revision_suffix", "rollout_mode", "regions", "primary_region",
    "gateway_regions", "internal_regions", "https_proxy", "url_map_name",
    "prior_snapshot", "prior_sha256", "bootstrap_artifact_sha256",
    "legacy_hardening_artifact_sha256", "frontend_attestation_sha256",
    "configuration_sha256", "pinned_secrets", "created_at", "operation_id", "phase",
    "edge_states", "service_states", "candidate_snapshot_sha256", "manifest_sha256",
}
if not isinstance(value, dict) or set(value) != required:
    raise SystemExit("initial-stage journal fields differ")
if value["schema_version"] != 1 or value["kind"] != "trusted-router-six-surface-initial-stage-state":
    raise SystemExit("initial-stage journal schema differs")
expected = {
    "manifest_path": str(Path(sys.argv[2]).resolve()),
    "project_id": sys.argv[3], "image": sys.argv[4], "release": sys.argv[5],
    "revision_suffix": sys.argv[6], "rollout_mode": "initial_split",
    "regions": sys.argv[7].split(","), "primary_region": sys.argv[8],
    "gateway_regions": sys.argv[9].split(","),
    "internal_regions": sys.argv[10].split(","), "https_proxy": sys.argv[11],
    "url_map_name": sys.argv[12], "prior_snapshot": sys.argv[13],
    "prior_sha256": sys.argv[14], "bootstrap_artifact_sha256": sys.argv[15],
    "legacy_hardening_artifact_sha256": sys.argv[16],
    "frontend_attestation_sha256": sys.argv[17],
    "configuration_sha256": sys.argv[18],
    "operation_id": sys.argv[19],
}
for field, wanted in expected.items():
    if value[field] != wanted:
        raise SystemExit(f"initial-stage journal binding differs: {field}")
if value["phase"] not in {"edge", "services", "manifest_intent", "complete"}:
    raise SystemExit("initial-stage journal phase differs")
if not isinstance(value["pinned_secrets"], list) or any(
    not isinstance(item, dict)
    or set(item) != {"resource", "version"}
    or not re.fullmatch(r"[1-9][0-9]*", str(item["version"]))
    for item in value["pinned_secrets"]
):
    raise SystemExit("initial-stage journal secret pins differ")
surfaces = ["public", "actions", "console", "chat", "webhooks", "internal"]
edges = value["edge_states"]
if [item.get("surface") for item in edges] != surfaces or any(
    set(item) != {"surface", "state", "postcondition_sha256"}
    or item["state"] not in {"pending", "reconcile_intent", "reconciled"}
    for item in edges
):
    raise SystemExit("initial-stage edge state inventory differs")
expected_services = []
for surface in surfaces:
    regions = value["internal_regions"] if surface == "internal" else value["regions"]
    expected_services.extend((surface, region) for region in regions)
services = value["service_states"]
if [(item.get("surface"), item.get("region")) for item in services] != expected_services:
    raise SystemExit("initial-stage service state inventory differs")
for item in services:
    if set(item) != {"surface", "name", "region", "candidate_revision", "prior_exists", "adopted_bootstrap", "state", "postcondition_sha256"}:
        raise SystemExit("initial-stage service state fields differ")
    if item["state"] not in {"pending", "deploy_intent", "staged"}:
        raise SystemExit("initial-stage service transition differs")
PY
}

stage_journal_read() {
  local kind="$1" first="$2" second="${3:-}"
  python3 - "$STAGE_JOURNAL" "$kind" "$first" "$second" <<'PY'
import json
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
items = value["edge_states"] if sys.argv[2] == "edge" else value["service_states"]
matches = [item for item in items if item["surface"] == sys.argv[3] and (sys.argv[2] == "edge" or item["region"] == sys.argv[4])]
if len(matches) != 1:
    raise SystemExit("initial-stage journal lookup differs")
print(json.dumps(matches[0], sort_keys=True, separators=(",", ":")))
PY
}

stage_journal_transition() {
  local kind="$1" first="$2" second="$3" old_state="$4" new_state="$5" digest="${6:-}"
  validate_initial_stage_journal "$STAGE_CONFIGURATION_SHA256"
  python3 - "$STAGE_JOURNAL" "$kind" "$first" "$second" "$old_state" "$new_state" "$digest" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
items = value["edge_states"] if sys.argv[2] == "edge" else value["service_states"]
matches = [item for item in items if item["surface"] == sys.argv[3] and (sys.argv[2] == "edge" or item["region"] == sys.argv[4])]
if len(matches) != 1 or matches[0]["state"] != sys.argv[5]:
    raise SystemExit("initial-stage journal transition precondition differs")
matches[0]["state"] = sys.argv[6]
if sys.argv[7]:
    matches[0]["postcondition_sha256"] = sys.argv[7]
payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

stage_journal_set_phase() {
  local old_phase="$1" new_phase="$2" candidate_sha="${3:-}" manifest_sha="${4:-}"
  python3 - "$STAGE_JOURNAL" "$old_phase" "$new_phase" "$candidate_sha" "$manifest_sha" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if value["phase"] != sys.argv[2]:
    raise SystemExit("initial-stage phase transition precondition differs")
value["phase"] = sys.argv[3]
if sys.argv[4]:
    value["candidate_snapshot_sha256"] = sys.argv[4]
if sys.argv[5]:
    value["manifest_sha256"] = sys.argv[5]
payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

if [ "$ROLLOUT_MODE" = initial_split ]; then
  if [ "$RESUMING_INITIAL_STAGE" = true ]; then
    restore_stage_journal_secret_pins
  fi
  STAGE_CONFIGURATION_SHA256="$(stage_configuration_sha256)" || exit 1
  if [ "$RESUMING_INITIAL_STAGE" = true ]; then
    validate_initial_stage_journal "$STAGE_CONFIGURATION_SHA256"
  else
    create_initial_stage_journal "$STAGE_CONFIGURATION_SHA256"
    RESUMING_INITIAL_STAGE=true
    validate_initial_stage_journal "$STAGE_CONFIGURATION_SHA256"
  fi
else
  [ "$RESUMING_INITIAL_STAGE" != true ] || {
    echo "ERROR: initial-stage journal cannot be resumed after the split URL map is active" >&2
    exit 1
  }
  STAGE_CONFIGURATION_SHA256=""
fi

# A revision suffix is transaction ownership, not merely a label. A fresh
# artifact directory may never adopt an existing immutable revision created by
# another run after an ambiguous deploy response.
for surface in "${SURFACES[@]}"; do
  service="$(surface_service "$surface")"
  while IFS= read -r region; do
    if [ "$ROLLOUT_MODE" = initial_split ] && [ "$surface" = internal ]; then
      candidate="$(bootstrap_internal_revision "$region")" || exit 1
      prior_path="$(prior_json_path "$surface" "$region")"
      verify_existing_service_metadata_is_safe "$surface" "$region" "$prior_path" || exit 1
      continue
    fi
    candidate="${service}-${REVISION_SUFFIX}"
    revision_rc=0
    revision_description="$(gc run revisions describe "$candidate" \
      --region="$region" --format=json 2>&1)" || revision_rc=$?
    if [ "$revision_rc" = 0 ]; then
      if [ "$ROLLOUT_MODE" = initial_split ]; then
        recorded_service="$(stage_journal_read service "$surface" "$region")" || exit 1
        recorded_candidate="$(jq -er .candidate_revision <<<"$recorded_service")" || exit 1
        recorded_state="$(jq -er .state <<<"$recorded_service")" || exit 1
        [ "$recorded_candidate" = "$candidate" ] || {
          echo "ERROR: existing candidate differs from the journal: ${candidate}/${region}" >&2
          exit 1
        }
        case "$recorded_state" in
          deploy_intent|staged) ;;
          *)
            echo "ERROR: candidate exists without a recorded deploy intent: ${candidate}/${region}" >&2
            exit 1
            ;;
        esac
      else
        echo "ERROR: candidate revision already exists outside this rollout: ${candidate}/${region}" >&2
        exit 1
      fi
    fi
    if [ "$revision_rc" != 0 ]; then
      case "$revision_description" in
        *NOT_FOUND*|*"not found"*) ;;
        *) echo "ERROR: cannot prove candidate revision absence: ${candidate}/${region}" >&2; exit 1 ;;
      esac
      if [ "$ROLLOUT_MODE" = initial_split ]; then
        recorded_state="$(stage_journal_read service "$surface" "$region" | jq -er .state)" || exit 1
        [ "$recorded_state" != staged ] || {
          echo "ERROR: journaled staged candidate is absent: ${candidate}/${region}" >&2
          exit 1
        }
      fi
    fi
    prior_path="$(prior_json_path "$surface" "$region")"
    if [ "$ROLLOUT_MODE" = initial_split ]; then
      recorded_prior_exists="$(stage_journal_read service "$surface" "$region" | jq -r .prior_exists)" || exit 1
      [ "$recorded_prior_exists" = true ] || continue
    else
      [ -f "$prior_path" ] || continue
    fi
    verify_existing_service_metadata_is_safe "$surface" "$region" "$prior_path" || exit 1
  done < <(surface_region_lines "$surface")
done

surface_backend_timeout() {
  case "$1" in
    public) echo 60 ;; actions) echo 30 ;; console|chat|internal) echo 300 ;;
    webhooks) echo 60 ;; *) return 2 ;;
  esac
}

EXPECTED_EDGE_ALLOWED_HOST_REGEX='^(trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|trust[.]uptimerouter[.]com)(:[0-9]+)?$'
[ "${TR_EDGE_ALLOWED_HOST_REGEX:-$EXPECTED_EDGE_ALLOWED_HOST_REGEX}" = "$EXPECTED_EDGE_ALLOWED_HOST_REGEX" ] &&
[ "${TR_CLOUD_ARMOR_RATE_INTERVAL_SECONDS:-60}" = 60 ] &&
[ "${TR_CLOUD_ARMOR_BROWSER_RATE_COUNT:-120}" = 120 ] &&
[ "${TR_CLOUD_ARMOR_WRITE_RATE_COUNT:-300}" = 300 ] &&
[ "${TR_CLOUD_ARMOR_GLOBAL_RATE_COUNT:-2400}" = 2400 ] &&
[ "${TR_CLOUD_ARMOR_PREVIEW:-1}" = 1 ] || {
  echo "ERROR: six-surface rollout requires the reviewed exact Cloud Armor rule constants" >&2
  exit 1
}

backend_reference_maps() {
  local backend="$1" self_link="$2"
  python3 - "$backend" "$self_link" "$URL_MAP_INVENTORY" <<'PY'
import json
import sys
from pathlib import Path

backend, self_link, inventory_path = sys.argv[1:]

def values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from values(item)
    elif isinstance(value, list):
        for item in value:
            yield from values(item)
    elif isinstance(value, str):
        yield value

for line in Path(inventory_path).read_text(encoding="utf-8").splitlines():
    item = json.loads(line)
    references = any(
        value == backend
        or value == self_link
        or value.rstrip("/").endswith(f"/backendServices/{backend}")
        for value in values(item)
    )
    if references:
        print(item.get("name", ""))
PY
}

ensure_inactive_backend() {
  local surface="$1" backend timeout describe_output describe_rc=0
  backend="$(surface_backend "$surface")"
  timeout="$(surface_backend_timeout "$surface")"
  describe_output="$(gc compute backend-services describe "$backend" --global 2>&1)" || describe_rc=$?
  if [ "$describe_rc" != 0 ]; then
    if [[ "$describe_output" != *NOT_FOUND* && "$describe_output" != *"not found"* ]]; then
      echo "ERROR: cannot determine whether inactive backend ${backend} exists" >&2
      return 1
    fi
    gc compute backend-services create "$backend" --global \
      --load-balancing-scheme=EXTERNAL_MANAGED --protocol=HTTP --port-name=http \
      --timeout="${timeout}s" --quiet >/dev/null
  else
    gc compute backend-services update "$backend" --global \
      --timeout="${timeout}s" --quiet >/dev/null
  fi
  if [ "$surface" = public ]; then
    gc compute backend-services update "$backend" --global \
      --enable-cdn \
      --cache-mode=USE_ORIGIN_HEADERS \
      --cache-key-include-host \
      --cache-key-include-protocol \
      --cache-key-include-query-string \
      --cache-key-query-string-blacklist= \
      --compression-mode=AUTOMATIC \
      --serve-while-stale=600 \
      --no-negative-caching \
      --quiet >/dev/null
  else
    gc compute backend-services update "$backend" --global --no-enable-cdn --quiet >/dev/null
  fi
}

verify_exact_neg_target() {
  local service="$1" neg="$2" region="$3" neg_json="$4"
  python3 - "$service" "$neg" "$region" "$neg_json" <<'PY'
import json
import sys
from pathlib import Path

service, neg, region, path = sys.argv[1:]
target = (json.loads(Path(path).read_text(encoding="utf-8")).get("cloudRun") or {})
if target != {"service": service}:
    raise SystemExit(
        f"{neg}/{region} must target exactly Cloud Run service {service} without tag/urlMask"
    )
PY
}

ensure_neg() {
  local surface="$1" region="$2" service backend neg neg_json attached conflicting group_counts
  local describe_output describe_rc=0
  service="$(surface_service "$surface")"
  backend="$(surface_backend "$surface")"
  neg="$(surface_neg "$surface")"
  neg_json="${WORK_DIR}/neg-${surface}-${region}.json"
  describe_output="$(gc compute network-endpoint-groups describe "$neg" --region="$region" --format=json 2>&1)" || describe_rc=$?
  if [ "$describe_rc" = 0 ]; then
    printf '%s\n' "$describe_output" >"$neg_json"
    verify_exact_neg_target "$service" "$neg" "$region" "$neg_json" || return 1
  else
    if [[ "$describe_output" != *NOT_FOUND* && "$describe_output" != *"not found"* ]]; then
      echo "ERROR: cannot determine whether ${neg}/${region} exists" >&2
      return 1
    fi
    gc compute network-endpoint-groups create "$neg" --region="$region" \
      --network-endpoint-type=serverless --cloud-run-service="$service" --quiet >/dev/null
  fi
  group_counts="$(gc compute backend-services describe "$backend" --global --format=json \
    | jq -r --arg project "$PROJECT_ID" --arg region "$region" --arg neg "$neg" '
      ("/projects/" + $project + "/regions/" + $region + "/networkEndpointGroups/" + $neg) as $expected
      | ("/regions/" + $region + "/networkEndpointGroups/" + $neg) as $suffix
      | [
          [.backends[]?.group | select(. == ($expected | ltrimstr("/")) or endswith($expected))] | length,
          [.backends[]?.group | select(endswith($suffix) and
            (. != ($expected | ltrimstr("/")) and (endswith($expected) | not)))] | length
        ] | @tsv
    ')" || return 1
  read -r attached conflicting <<<"$group_counts" || return 1
  [ "$conflicting" = 0 ] || {
    echo "ERROR: ${backend} references same-named ${neg}/${region} outside project ${PROJECT_ID}" >&2
    return 1
  }
  if [ "$attached" = 0 ]; then
    gc compute backend-services add-backend "$backend" --global \
      --network-endpoint-group="$neg" --network-endpoint-group-region="$region" --quiet >/dev/null
  elif [ "$attached" != 1 ]; then
    echo "ERROR: ${backend} has duplicate ${neg}/${region} attachments" >&2
    return 1
  fi
}

remove_extra_inactive_backend_groups() {
  local surface="$1" backend neg backend_json groups group project region group_name keep
  backend="$(surface_backend "$surface")"
  neg="$(surface_neg "$surface")"
  backend_json="$(gc compute backend-services describe "$backend" --global --format=json)"
  groups="$(jq -r '.backends[]?.group' <<<"$backend_json")" || return 1
  while IFS= read -r group; do
    project="$(sed -n 's#.*projects/\([^/]*\)/regions/.*#\1#p' <<<"$group")"
    region="$(sed -n 's#.*regions/\([^/]*\)/networkEndpointGroups/.*#\1#p' <<<"$group")"
    group_name="${group##*/}"
    [ -n "$project" ] && [ -n "$region" ] || {
      echo "ERROR: inactive managed backend has a noncanonical group reference: ${group}" >&2
      return 1
    }
    [ "$project" = "$PROJECT_ID" ] || {
      echo "ERROR: inactive managed backend references a NEG outside project ${PROJECT_ID}: ${group}" >&2
      return 1
    }
    keep=0
    for expected_region in "${REGIONS[@]}"; do
      [ "$region" = "$expected_region" ] && [ "$group_name" = "$neg" ] && keep=1
    done
    if [ "$keep" = 0 ]; then
      gc compute backend-services remove-backend "$backend" --global \
        --network-endpoint-group="$group_name" --network-endpoint-group-region="$region" --quiet >/dev/null
    fi
  done <<<"$groups"
}

verify_backend_contract() {
  local surface="$1" backend neg service policy timeout backend_json policy_json
  local backend_payload policy_payload regions_csv
  backend="$(surface_backend "$surface")"
  neg="$(surface_neg "$surface")"
  service="$(surface_service "$surface")"
  policy="$(surface_policy "$surface")"
  timeout="$(surface_backend_timeout "$surface")"
  backend_json="${WORK_DIR}/backend-${surface}.json"
  gc compute backend-services describe "$backend" --global --format=json >"$backend_json"
  backend_payload="$(<"$backend_json")"
  regions_csv="$(IFS=,; echo "${REGIONS[*]}")"
  verify_edge_backend_contract_json "$surface" "$backend_payload" \
    "$PROJECT_ID" "$regions_csv" "$service" "$neg" "$policy" "$timeout" || return 1
  policy_json="${WORK_DIR}/policy-${surface}.json"
  gc compute security-policies describe "$policy" --global --format=json >"$policy_json"
  policy_payload="$(<"$policy_json")"
  verify_cloud_armor_policy_contract_json "$policy_payload" || return 1
  for region in "${REGIONS[@]}"; do
    neg_json="${WORK_DIR}/verify-neg-${surface}-${region}.json"
    gc compute network-endpoint-groups describe "$neg" --region="$region" --format=json >"$neg_json"
    verify_exact_neg_target "$service" "$neg" "$region" "$neg_json" || return 1
  done
}

# TR_DEPLOY_RECONCILE_LB gates only the MUTATING edge work below. The
# read-only inventory and backend verification always run: edge_header source
# trust requires every deploy to verify the LB contract, and the workflow's
# reconcile-exactly-once design (primary region reconciles, secondaries pass
# TR_DEPLOY_RECONCILE_LB=0 so concurrent rollouts never race on the shared
# global LB) only ever skips mutation, never verification. Skipping is legal
# only when nothing needed reconciling; that is asserted after classification.

# Inventory every global URL map, not only the selected HTTPS proxy map. A
# canonical backend referenced by another proxy is live even when absent from
# the selected map and therefore must never be reconciled as an inactive
# rollout companion.
URL_MAP_INVENTORY="${WORK_DIR}/all-url-maps.jsonl"
: >"$URL_MAP_INVENTORY"
URL_MAP_NAMES="$(gc compute url-maps list --format='value(name)')" || exit 1
[ -n "$URL_MAP_NAMES" ] || {
  echo "ERROR: no global URL maps are visible for backend reachability preflight" >&2
  exit 1
}
while IFS= read -r inventory_map; do
  [ -n "$inventory_map" ] || continue
  validate_resource_name "global URL map" "$inventory_map"
  gc compute url-maps describe "$inventory_map" --global --format=json \
    | jq -c . >>"$URL_MAP_INVENTORY"
done <<<"$URL_MAP_NAMES"
jq -se --arg name "$URL_MAP_NAME" 'any(.[]; .name == $name)' \
  "$URL_MAP_INVENTORY" >/dev/null || {
  echo "ERROR: selected URL map is absent from the global inventory" >&2
  exit 1
}

# Verify every currently reachable backend before any LB mutation. Active
# backends are never reconciled in-place by staging because that would require
# a much larger rollback manifest for cache, policy, headers, and membership.
# Inactive companion backends may be reconciled because the captured URL map
# proves they receive no production request.
INACTIVE_SURFACES=()
for surface in "${SURFACES[@]}"; do
  backend="$(surface_backend "$surface")"
  backend_describe_rc=0
  backend_json="$(gc compute backend-services describe "$backend" --global --format=json 2>&1)" || backend_describe_rc=$?
  if [ "$backend_describe_rc" = 0 ]; then
    self_link="$(jq -er '.selfLink' <<<"$backend_json")"
    reference_maps="$(backend_reference_maps "$backend" "$self_link")" || exit 1
    if grep -Fxq "$URL_MAP_NAME" <<<"$reference_maps"; then
      if [ "$ROLLOUT_MODE" = initial_split ]; then
        echo "ERROR: initial URL map unexpectedly references the ${surface} backend" >&2
        exit 1
      fi
      verify_backend_contract "$surface"
      continue
    fi
    if [ -n "$reference_maps" ]; then
      echo "ERROR: ${backend} is reachable from another global URL map: ${reference_maps//$'\n'/,}" >&2
      exit 1
    fi
  elif [[ "$backend_json" != *NOT_FOUND* && "$backend_json" != *"not found"* ]]; then
    echo "ERROR: cannot determine whether backend ${backend} exists" >&2
    exit 1
  fi
  [ "$ROLLOUT_MODE" = existing_split ] && {
    echo "ERROR: existing split does not route the ${surface} backend" >&2
    exit 1
  }
  INACTIVE_SURFACES+=("$surface")
done

edge_contract_digest() {
  local surface="$1" backend policy region path hashes=""
  backend="$(surface_backend "$surface")"
  policy="$(surface_policy "$surface")"
  path="${WORK_DIR}/journal-backend-${surface}.json"
  gc compute backend-services describe "$backend" --global --format=json >"$path" || return 1
  hashes="$(python3 "$STATE_TOOL" hash-resource "$path")"
  path="${WORK_DIR}/journal-policy-${surface}.json"
  gc compute security-policies describe "$policy" --global --format=json >"$path" || return 1
  hashes="${hashes}$(python3 "$STATE_TOOL" hash-resource "$path")"
  for region in "${REGIONS[@]}"; do
    path="${WORK_DIR}/journal-neg-${surface}-${region}.json"
    gc compute network-endpoint-groups describe "$(surface_neg "$surface")" \
      --region="$region" --format=json >"$path" || return 1
    hashes="${hashes}$(python3 "$STATE_TOOL" hash-resource "$path")"
  done
  python3 - "$hashes" <<'PY'
import hashlib
import sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
}

if [ "${TR_DEPLOY_RECONCILE_LB:-1}" = "1" ]; then
for surface in "${INACTIVE_SURFACES[@]}"; do
  policy="$(surface_policy "$surface")"
  backend="$(surface_backend "$surface")"
  # A supposedly inactive policy may still protect another live backend.
  policy_describe_rc=0
  policy_describe_output="$(gc compute security-policies describe "$policy" --global 2>&1)" || policy_describe_rc=$?
  if [ "$policy_describe_rc" = 0 ]; then
    policy_consumers="$(gc compute backend-services list --global --format=json \
      | jq -r --arg policy "$policy" '[.[] | select((.securityPolicy // "" | split("/")[-1]) == $policy) | .name] | unique | join(",")')"
    [ -z "$policy_consumers" ] || [ "$policy_consumers" = "$backend" ] || {
      echo "ERROR: inactive policy ${policy} is attached to other backend(s): ${policy_consumers}" >&2
      exit 1
    }
  elif [[ "$policy_describe_output" != *NOT_FOUND* && "$policy_describe_output" != *"not found"* ]]; then
    echo "ERROR: cannot determine whether inactive policy ${policy} exists" >&2
    exit 1
  fi
  edge_state=pending
  if [ "$ROLLOUT_MODE" = initial_split ]; then
    edge_record="$(stage_journal_read edge "$surface")" || exit 1
    edge_state="$(jq -er .state <<<"$edge_record")" || exit 1
    if [ "$edge_state" = pending ]; then
      stage_journal_transition edge "$surface" "" pending reconcile_intent
      edge_state=reconcile_intent
    fi
  fi
  if [ "$edge_state" = reconciled ]; then
    verify_backend_contract "$surface"
    edge_digest="$(edge_contract_digest "$surface")" || exit 1
    [ "$edge_digest" = "$(jq -er .postcondition_sha256 <<<"$edge_record")" ] || {
      echo "ERROR: reconciled edge state drifted for ${surface}" >&2
      exit 1
    }
    continue
  fi
  ensure_inactive_backend "$surface"
  for region in "${REGIONS[@]}"; do ensure_neg "$surface" "$region"; done
  remove_extra_inactive_backend_groups "$surface"
  reconcile_edge_backend_mappings "${backend}=${policy}"
  verify_backend_contract "$surface"
  if [ "$ROLLOUT_MODE" = initial_split ]; then
    edge_digest="$(edge_contract_digest "$surface")" || exit 1
    stage_journal_transition edge "$surface" "" reconcile_intent reconciled "$edge_digest"
  fi
done
else
  log "skipping shared load-balancer reconciliation"
  # Mutation was skipped, so it must not have been needed: every backend had
  # to be classified active (routed by the selected map) above. An inactive
  # backend here means the primary region's reconcile-once pass has not run.
  [ "${#INACTIVE_SURFACES[@]}" = 0 ] || {
    echo "ERROR: unreconciled inactive backends require TR_DEPLOY_RECONCILE_LB=1: ${INACTIVE_SURFACES[*]}" >&2
    exit 1
  }
fi
for surface in "${SURFACES[@]}"; do verify_backend_contract "$surface"; done
if [ "$ROLLOUT_MODE" = initial_split ]; then
  stage_phase="$(jq -er .phase "$STAGE_JOURNAL")" || exit 1
  if [ "$stage_phase" = edge ]; then
    stage_journal_set_phase edge services
  elif [ "$stage_phase" != services ] && [ "$stage_phase" != manifest_intent ] && \
      [ "$stage_phase" != complete ]; then
    echo "ERROR: initial-stage journal phase differs after edge reconciliation" >&2
    exit 1
  fi
fi

backend_self_link() {
  gc compute backend-services describe "$(surface_backend "$1")" \
    --global --format='value(selfLink)'
}

# All inactive edge resources now exist and are exact, so build and ask GCP to
# validate the real atomic map before any deploy can alter the active console
# service. The helper later imports these exact private snapshot bytes.
RENDERED_CANDIDATE_URL_MAP="$CANDIDATE_URL_MAP"
if [ -e "$CANDIDATE_URL_MAP" ]; then
  [ "$ROLLOUT_MODE" = initial_split ] && [ "$RESUMING_INITIAL_STAGE" = true ] || {
    echo "ERROR: refusing to overwrite candidate URL-map snapshot" >&2
    exit 1
  }
  RENDERED_CANDIDATE_URL_MAP="${WORK_DIR}/url-map.candidate.current.json"
fi
python3 "$URL_MAP_TOOL" \
  --input "$PRIOR_URL_MAP" \
  --output "$RENDERED_CANDIDATE_URL_MAP" \
  --public-backend "$(backend_self_link public)" \
  --actions-backend "$(backend_self_link actions)" \
  --console-backend "$(backend_self_link console)" \
  --chat-backend "$(backend_self_link chat)" \
  --webhooks-backend "$(backend_self_link webhooks)" \
  --internal-backend "$(backend_self_link internal)" \
  --domains "$DOMAINS" \
  --preserved-hosts "$PRESERVED_HOSTS"
if [ "$RENDERED_CANDIDATE_URL_MAP" != "$CANDIDATE_URL_MAP" ]; then
  [ "$(python3 "$STATE_TOOL" hash-url-map "$RENDERED_CANDIDATE_URL_MAP")" = \
    "$(python3 "$STATE_TOOL" hash-url-map "$CANDIDATE_URL_MAP")" ] || {
    echo "ERROR: resumed candidate URL-map snapshot differs from the journaled plan" >&2
    exit 1
  }
else
  chmod 600 "$CANDIDATE_URL_MAP"
fi
chmod 600 "$PRIOR_URL_MAP" "$CANDIDATE_URL_MAP"
gc compute url-maps validate --global --source="$CANDIDATE_URL_MAP" >/dev/null
if [ "$ROLLOUT_MODE" = initial_split ]; then
  candidate_snapshot_sha="$(python3 "$STATE_TOOL" hash-url-map "$CANDIDATE_URL_MAP")"
  recorded_candidate_sha="$(jq -r '.candidate_snapshot_sha256 // ""' "$STAGE_JOURNAL")"
  if [ -n "$recorded_candidate_sha" ] && [ "$recorded_candidate_sha" != "$candidate_snapshot_sha" ]; then
    echo "ERROR: candidate URL-map snapshot hash differs from the initial-stage journal" >&2
    exit 1
  fi
  if [ -z "$recorded_candidate_sha" ]; then
    stage_phase="$(jq -er .phase "$STAGE_JOURNAL")" || exit 1
    stage_journal_set_phase "$stage_phase" "$stage_phase" "$candidate_snapshot_sha"
  fi
fi

cloud_run_min_instances_for_region() {
  local target="$1"
  local entry
  local region
  local count
  local entries=()
  IFS=',' read -ra entries <<<"$TR_CLOUD_RUN_MIN_INSTANCES_BY_REGION"
  for entry in "${entries[@]}"; do
    region="${entry%%=*}"
    count="${entry#*=}"
    if [ "$region" = "$target" ] && [[ "$count" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$count"
      return 0
    fi
  done
  case ",${TR_WARM_REGIONS}," in
    *",${target},"*) printf '1\n' ;;
    *) printf '0\n' ;;
  esac
}

surface_runtime_contract() {
  local surface="$1"
  local region="$2"
  case "$surface" in
    public)
      CONCURRENCY=4; MIN_INSTANCES=0; MAX_INSTANCES=10; TIMEOUT_SECONDS=60
      MEMORY=1Gi; SPANNER_POOL_SIZE=1; MAX_REQUEST_BODY_BYTES=1048576
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=4194304; MAX_CONCURRENT_REQUEST_BODIES=2
      REQUEST_BODY_READ_TIMEOUT_SECONDS=10; DEFAULT_URL_DISABLED=true ;;
    actions)
      CONCURRENCY=4; MIN_INSTANCES=0; MAX_INSTANCES=2; TIMEOUT_SECONDS=30
      MEMORY=512Mi; SPANNER_POOL_SIZE=0; MAX_REQUEST_BODY_BYTES=262144
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=1048576; MAX_CONCURRENT_REQUEST_BODIES=2
      REQUEST_BODY_READ_TIMEOUT_SECONDS=10; DEFAULT_URL_DISABLED=true ;;
    console)
      CONCURRENCY=4; MIN_INSTANCES=1; MAX_INSTANCES=20; TIMEOUT_SECONDS=300
      MEMORY=2Gi; SPANNER_POOL_SIZE=2; MAX_REQUEST_BODY_BYTES=4194304
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=16777216; MAX_CONCURRENT_REQUEST_BODIES=2
      REQUEST_BODY_READ_TIMEOUT_SECONDS=30; DEFAULT_URL_DISABLED=true ;;
    chat)
      CONCURRENCY=2; MIN_INSTANCES=1; MAX_INSTANCES=20; TIMEOUT_SECONDS=300
      MEMORY=2Gi; SPANNER_POOL_SIZE=2; MAX_REQUEST_BODY_BYTES=33554432
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=67108864; MAX_CONCURRENT_REQUEST_BODIES=2
      REQUEST_BODY_READ_TIMEOUT_SECONDS=30; DEFAULT_URL_DISABLED=true ;;
    webhooks)
      CONCURRENCY=4; MIN_INSTANCES=1; MAX_INSTANCES=10; TIMEOUT_SECONDS=60
      MEMORY=1Gi; SPANNER_POOL_SIZE=2; MAX_REQUEST_BODY_BYTES=1048576
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=4194304; MAX_CONCURRENT_REQUEST_BODIES=2
      REQUEST_BODY_READ_TIMEOUT_SECONDS=10; DEFAULT_URL_DISABLED=true ;;
    internal)
      CONCURRENCY="$TR_CLOUD_RUN_CONCURRENCY"; MAX_INSTANCES=50; TIMEOUT_SECONDS=300
      MIN_INSTANCES="${TR_CLOUD_RUN_MIN_INSTANCES:-$(cloud_run_min_instances_for_region "$region")}"
      MEMORY=2Gi; SPANNER_POOL_SIZE="$TR_SPANNER_POOL_SIZE"; MAX_REQUEST_BODY_BYTES=33554432
      MAX_IN_FLIGHT_REQUEST_BODY_BYTES=67108864; MAX_CONCURRENT_REQUEST_BODIES=4
      REQUEST_BODY_READ_TIMEOUT_SECONDS=30; DEFAULT_URL_DISABLED=false ;;
    *) return 2 ;;
  esac
}

surface_env_vars() {
  local surface="$1"
  local region="$2"
  surface_runtime_contract "$surface" "$region"
  SURFACE_ENV=(
    "TR_ENVIRONMENT=production"
    "TR_RELEASE=${RELEASE}"
    "TR_SERVICE_SURFACE=${surface}"
    "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
    "TR_TRUSTED_DOMAIN=trustedrouter.com"
    "TR_TRUSTED_DOMAIN_ALIASES=allyrouter.com,uptimerouter.com"
    "TR_GCP_PROJECT_ID=${PROJECT_ID}"
    "TR_REGIONS=${TR_REGIONS}"
    "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
    "TR_ENABLE_LIVE_PROVIDERS=false"
    "TR_RATE_LIMIT_CLIENT_IP_MODE=edge_header"
    "TR_MAX_REQUEST_BODY_BYTES=${MAX_REQUEST_BODY_BYTES}"
    "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES=${MAX_IN_FLIGHT_REQUEST_BODY_BYTES}"
    "TR_MAX_CONCURRENT_REQUEST_BODIES=${MAX_CONCURRENT_REQUEST_BODIES}"
    "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS=${REQUEST_BODY_READ_TIMEOUT_SECONDS}"
    "TR_REMEDIATOR_IN_PROCESS_ENABLED=false"
    "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=0"
    "TR_ACTIVATION_REMINDER_INTERVAL_SECONDS=0"
  )
  if [ "$surface" = actions ]; then
    SURFACE_ENV+=(
      "TR_STORAGE_BACKEND=memory"
      "TR_AWS_REGION=us-east-1"
      "TR_SES_FROM_EMAIL=noreply@trustedrouter.com"
      "TR_SES_FROM_NAME=TrustedRouter"
      "TR_OPS_CHAT_WEBHOOK_URLS=https://a.uptimerouter.com,https://b.trustedrouter.com,https://c.allyrouter.com"
      "TR_PARTNER_INQUIRY_EMAIL=joseph@jperla.com"
    )
    return
  fi
  SURFACE_ENV+=(
    "TR_STORAGE_BACKEND=${STORAGE_BACKEND}"
    "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
    "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
    "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
    "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
    "TR_SPANNER_POOL_SIZE=${SPANNER_POOL_SIZE}"
    "TR_BIGTABLE_MIRROR_WRITES_ENABLED=${BIGTABLE_MIRROR_WRITES_ENABLED}"
    "TR_GENERATION_RECORDS_ENABLED=${GENERATION_RECORDS_ENABLED}"
    "TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}"
    "TR_ANALYTICS_DUAL_READ_STARTED_AT=${TR_ANALYTICS_DUAL_READ_STARTED_AT:-$(serving_env_value TR_ANALYTICS_DUAL_READ_STARTED_AT)}"
    "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT=${TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT:-$(serving_env_value TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT)}"
    "TR_REQUEST_RECORD_WRITE_MODE=${REQUEST_RECORD_WRITE_MODE}"
    "TR_SETTLE_OUTBOX_ENABLED=true"
    "TR_ANALYTICS_OUTBOX_ENABLED=true"
    "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
  )
  case "$surface" in public|console|internal)
    if [ "$ANALYTICS_READ_MODE" != bigtable ]; then
      SURFACE_ENV+=(
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=${OPERATIONAL_CLICKHOUSE_URL}"
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=tr_control_read"
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=tr"
      )
    fi ;;
  esac
  case "$surface" in
    public)
      SURFACE_ENV+=(
        "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=${GOOGLE_OAUTH_AVAILABLE}"
        "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=${GITHUB_OAUTH_AVAILABLE}"
        "TR_TRUST_GCP_RELEASE_URL=https://trust.trustedrouter.com/trust/gcp-release.json"
        "TR_TRUST_GCP_RELEASE_FALLBACK_URLS=https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/main/trust-page/gcp-release.json"
        "TR_TRUST_AWS_RELEASE_URL=https://trust.trustedrouter.com/trust/aws-release.json"
        "TR_TRUST_AZURE_RELEASE_URL=https://trust.trustedrouter.com/trust/azure-release.json"
      ) ;;
    console)
      SURFACE_ENV+=(
        "TR_SIGNUP_TRIAL_CREDIT_MICRODOLLARS=300000"
        "TR_NEW_SIGNUPS_ENABLED=${NEW_SIGNUPS_ENABLED}"
        "TR_USER_MODELS_DISPATCH_ENABLED=true"
        # Emergency telemetry disablement is an explicit rollout edit: with
        # this flag false the route returns 202 plus x-tr-telemetry: off before
        # it reads a request body.
        "TR_CLIENT_EVENTS_ENABLED=true"
        "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
        "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=${GOOGLE_OAUTH_AVAILABLE}"
        "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=${GITHUB_OAUTH_AVAILABLE}"
        "TR_GOOGLE_OAUTH_REDIRECT_URL=https://trustedrouter.com/google_oauth_callback"
        "TR_GITHUB_OAUTH_REDIRECT_URL=https://trustedrouter.com/github_oauth_callback"
        "TR_PAYPAL_CHECKOUT_ENABLED=${PAYPAL_CHECKOUT_ENABLED}"
        "TR_ADYEN_ENABLED=${ADYEN_ENABLED}"
        "TR_ADYEN_ENVIRONMENT=test"
        "TR_ADYEN_MERCHANT_ACCOUNT=TrustedRouterUS"
        "TR_ADYEN_CHECKOUT_API_VERSION=72"
        "TR_ADYEN_WEB_VERSION=6.41.0"
        "TR_ADYEN_CARD_FEE_BASIS_POINTS=0"
        "TR_ADYEN_CARD_FEE_FIXED_CENTS=0"
        "TR_CHECKOUT_CARD_FEE_MINIMUM_CENTS=80"
        "TR_VERIFF_ENABLED=${VERIFF_ENABLED}"
        "TR_CUSTOM_MODELS_REQUIRE_VERIFICATION=${VERIFF_ENABLED}"
        "TR_VERIFF_BASE_URL=https://stationapi.veriff.com"
        "TR_NOTIFY_ENABLED=true"
        "TR_NOTIFY_SMS_AVAILABLE=false"
        "TR_TELNYX_FROM_NUMBER=${TELNYX_FROM_NUMBER}"
        "TR_TELNYX_TEXML_ACCOUNT_ID=${TELNYX_TEXML_ACCOUNT_ID}"
        "TR_TELNYX_TEXML_APPLICATION_ID=${TELNYX_TEXML_APPLICATION_ID}"
        "TR_TWILIO_FROM_NUMBER=${TWILIO_FROM_NUMBER}"
        "TR_AWS_REGION=us-east-1"
        "TR_SES_FROM_EMAIL=noreply@trustedrouter.com"
        "TR_SES_FROM_NAME=TrustedRouter"
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL=${PROVIDER_CLICKHOUSE_URL}"
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_USER=tr_provider_read"
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_DATABASE=tr"
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_TABLE=provider_benchmark_samples"
      ) ;;
    webhooks)
      SURFACE_ENV+=(
        "TR_PAYPAL_CHECKOUT_ENABLED=${PAYPAL_CHECKOUT_ENABLED}"
        "TR_ADYEN_ENABLED=${ADYEN_ENABLED}"
        "TR_ADYEN_ENVIRONMENT=test"
        "TR_ADYEN_MERCHANT_ACCOUNT=TrustedRouterUS"
        "TR_VERIFF_ENABLED=${VERIFF_ENABLED}"
      ) ;;
    internal)
      # The immutable bootstrap cohort predates the quota compatibility marker;
      # an initial split may adopt it only while both switches are off. Every
      # subsequent internal candidate carries the complete main-side contract.
      if [ "$ROLLOUT_MODE" = initial_split ]; then
        SURFACE_ENV+=(
          "TR_REGIONAL_QUOTA_LEASES_ENABLED=${REGIONAL_QUOTA_LEASES_ENABLED}"
        )
      else
        SURFACE_ENV+=(
          "TR_REGIONAL_QUOTA_LEASES_ENABLED=${REGIONAL_QUOTA_LEASES_ENABLED}"
          "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=${REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED}"
          "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS=${REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS}"
          "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS=${REGIONAL_QUOTA_LEASE_TTL_SECONDS}"
          "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS=${REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS}"
          "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS=${REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS}"
          "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT=${REGIONAL_QUOTA_LEASE_SHARD_COUNT}"
          "TR_REGIONAL_QUOTA_BIGTABLE_TABLE=${REGIONAL_QUOTA_BIGTABLE_TABLE}"
          "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"
        )
      fi
      SURFACE_ENV+=(
        "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
        "TR_AWS_REGION=us-east-1"
        "TR_SES_FROM_EMAIL=alerts@alerts.trustedrouter.com"
        "TR_SES_FROM_NAME=TrustedRouter Alerts"
        "TR_SES_ALERT_FROM_EMAIL=alerts@alerts.trustedrouter.com"
        "TR_SES_ALERT_FROM_NAME=TrustedRouter Alerts"
        "TR_SES_ALERT_CONFIGURATION_SET=trustedrouter-alerts"
      ) ;;
  esac
}

prior_traffic_json() {
  local prior="$1"
  python3 "$STATE_TOOL" traffic-state "$prior"
}

expected_maps() {
  local env_file="$1" secret_file="$2"
  printf '%s\n' "${SURFACE_ENV[@]}" | jq -Rn '
    [inputs | capture("^(?<name>[^=]+)=(?<value>.*)$")] | from_entries
  ' >"$env_file"
  printf '%s\n' "${SURFACE_SECRETS[@]}" | jq -Rn '
    [inputs | capture("^(?<name>[^=]+)=(?<resource>.*):(?<version>[1-9][0-9]*)$")
      | {key: .name, value: {resource: .resource, version: .version}}] | from_entries
  ' >"$secret_file"
  chmod 600 "$env_file" "$secret_file"
}

verify_service_postcondition() {
  local surface="$1" service="$2" region="$3" candidate="$4" prior_exists="$5" prior="$6" adopted="$7"
  local service_json expected_env expected_secrets service_iam actual_traffic expected_traffic
  service_json="${WORK_DIR}/candidate-${surface}-${region}.json"
  expected_env="${WORK_DIR}/expected-env-${surface}-${region}.json"
  expected_secrets="${WORK_DIR}/expected-secret-${surface}-${region}.json"
  service_iam="${WORK_DIR}/candidate-iam-${surface}-${region}.json"
  gc run services describe "$service" --region="$region" --format=json >"$service_json"
  gc run services get-iam-policy "$service" --region="$region" --format=json >"$service_iam"
  if ! jq -e '
      [.bindings[]? | select(any(.members[]?; . == "allUsers"))
        | {role, condition: (.condition // null),
           allUsersCount: ([.members[]? | select(. == "allUsers")] | length)}]
      == [{role:"roles/run.invoker",condition:null,allUsersCount:1}]
    ' "$service_iam" >/dev/null; then
    echo "ERROR: ${service}/${region} lacks the exact unauthenticated LB invocation grant" >&2
    return 1
  fi
  expected_maps "$expected_env" "$expected_secrets"
  python3 - "$service_json" "$expected_env" "$expected_secrets" \
    "$surface" "$candidate" "$(surface_account "$surface")" \
    "$TR_CLOUD_RUN_INGRESS" "$CONCURRENCY" "$MIN_INSTANCES" "$MAX_INSTANCES" \
    "$TIMEOUT_SECONDS" "$DEFAULT_URL_DISABLED" "$prior_exists" "$adopted" "$IMAGE" "$MEMORY" \
    1 8080 "$CLOUD_RUN_NETWORK" "$CLOUD_RUN_SUBNET" private-ranges-only \
    /ready 0 10 10 18 "$PROJECT_ID" <<'PY' || return 1
from __future__ import annotations
import json
import sys
from pathlib import Path

(
    service_path, env_path, secret_path, surface, candidate, expected_account,
    expected_ingress, concurrency, minimum, maximum, timeout, default_disabled,
    prior_exists, adopted, expected_image, memory, cpu, port, network, subnet, vpc_egress,
    probe_path, probe_initial_delay, probe_timeout, probe_period,
    probe_failures, project,
) = sys.argv[1:]
service = json.loads(Path(service_path).read_text())
expected_env = json.loads(Path(env_path).read_text())
expected_secrets = json.loads(Path(secret_path).read_text())
metadata = service.get("metadata") or {}
annotations = metadata.get("annotations") or {}
spec = service.get("spec") or {}
template = spec.get("template") or {}
template_metadata = template.get("metadata") or {}
template_annotations = template_metadata.get("annotations") or {}
template_spec = template.get("spec") or {}
status = service.get("status") or {}
if status.get("latestCreatedRevisionName") != candidate:
    raise SystemExit(f"{surface}: latest created revision is not {candidate}")
if status.get("latestReadyRevisionName") != candidate:
    raise SystemExit(f"{surface}: candidate is not latest Ready")
if not any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])):
    raise SystemExit(f"{surface}: Cloud Run Ready condition failed")
if metadata.get("generation") is None or status.get("observedGeneration") is None:
    raise SystemExit(f"{surface}: Cloud Run generation metadata is absent")
if str(status.get("observedGeneration")) != str(metadata.get("generation")):
    raise SystemExit(f"{surface}: Cloud Run has not observed the desired generation")
if annotations.get("run.googleapis.com/ingress") != expected_ingress:
    raise SystemExit(f"{surface}: ingress postcondition failed")
ingress_status = annotations.get("run.googleapis.com/ingress-status")
if ingress_status != expected_ingress:
    raise SystemExit(f"{surface}: effective ingress differs from desired ingress")
default_annotation = annotations.get("run.googleapis.com/default-url-disabled")
if default_disabled == "true" and str(default_annotation).lower() != "true":
    raise SystemExit(f"{surface}: default URL disablement annotation drifted")
if default_disabled == "false" and str(default_annotation or "").lower() not in {"", "false"}:
    raise SystemExit(f"{surface}: internal default URL annotation drifted")
if template_spec.get("serviceAccountName") != expected_account:
    raise SystemExit(f"{surface}: runtime service account postcondition failed")
if int(template_spec.get("containerConcurrency", -1)) != int(concurrency):
    raise SystemExit(f"{surface}: concurrency postcondition failed")
actual_timeout = str(template_spec.get("timeoutSeconds", "")).removesuffix("s")
if actual_timeout != timeout:
    raise SystemExit(f"{surface}: timeout postcondition failed")
actual_min = template_annotations.get("autoscaling.knative.dev/minScale")
actual_max = template_annotations.get("autoscaling.knative.dev/maxScale")
if str(actual_min) != minimum or str(actual_max) != maximum:
    raise SystemExit(f"{surface}: revision scaling postcondition failed")
service_max = (spec.get("scaling") or {}).get("maxInstanceCount")
if service_max is None:
    service_max = annotations.get("run.googleapis.com/maxScale")
if str(service_max) != maximum:
    raise SystemExit(f"{surface}: service-level max postcondition failed")
containers = template_spec.get("containers") or []
if len(containers) != 1:
    raise SystemExit(f"{surface}: service must contain exactly one application container")
if template_spec.get("volumes") not in (None, []):
    raise SystemExit(f"{surface}: unexpected service volumes are forbidden")
if template_spec.get("initContainers") not in (None, []):
    raise SystemExit(f"{surface}: init containers are forbidden")
container = containers[0]
if container.get("volumeMounts") not in (None, []):
    raise SystemExit(f"{surface}: unexpected volume mounts are forbidden")
if container.get("command") not in (None, []) or container.get("args") not in (None, []):
    raise SystemExit(f"{surface}: container command/args override is forbidden")
if container.get("image") != expected_image:
    raise SystemExit(f"{surface}: immutable image digest postcondition failed")
ports = container.get("ports") or []
if len(ports) != 1 or int(ports[0].get("containerPort", -1)) != int(port):
    raise SystemExit(f"{surface}: container port postcondition failed")
limits = (container.get("resources") or {}).get("limits") or {}
if str(limits.get("memory")) != memory:
    raise SystemExit(f"{surface}: memory postcondition failed")
actual_cpu = str(limits.get("cpu") or "")
if actual_cpu not in {cpu, f"{int(cpu) * 1000}m"}:
    raise SystemExit(f"{surface}: CPU postcondition failed")
network_interfaces_raw = template_annotations.get("run.googleapis.com/network-interfaces")
try:
    network_interfaces = json.loads(network_interfaces_raw)
except (TypeError, ValueError):
    raise SystemExit(f"{surface}: VPC network annotation is invalid") from None
if not isinstance(network_interfaces, list) or len(network_interfaces) != 1:
    raise SystemExit(f"{surface}: VPC network interface count differs")

def exact_resource(value: object, kind: str, expected: str) -> bool:
    text = str(value or "").rstrip("/")
    if text == expected:
        return True
    if f"/projects/{project}/" not in f"/{text}":
        return False
    suffix = f"/{kind}/{expected}"
    return text.endswith(suffix)

interface = network_interfaces[0]
if not exact_resource(interface.get("network"), "networks", network):
    raise SystemExit(f"{surface}: VPC network postcondition failed")
if not exact_resource(interface.get("subnetwork"), "subnetworks", subnet):
    raise SystemExit(f"{surface}: VPC subnet postcondition failed")
if template_annotations.get("run.googleapis.com/vpc-access-egress") != vpc_egress:
    raise SystemExit(f"{surface}: VPC egress postcondition failed")
probe = container.get("startupProbe") or {}
http_get = probe.get("httpGet") or {}
if http_get.get("path") != probe_path:
    raise SystemExit(f"{surface}: /ready startup probe is missing")
if http_get.get("port") is not None and int(http_get["port"]) != int(port):
    raise SystemExit(f"{surface}: startup probe port differs")
expected_probe = {
    "initialDelaySeconds": int(probe_initial_delay),
    "timeoutSeconds": int(probe_timeout),
    "periodSeconds": int(probe_period),
    "failureThreshold": int(probe_failures),
}
for name, expected in expected_probe.items():
    if int(probe.get(name, -1)) != expected:
        raise SystemExit(f"{surface}: startup probe {name} differs")
actual_values = {}
actual_secrets = {}
for item in container.get("env") or []:
    name = item.get("name")
    if "valueFrom" in item:
        reference = ((item.get("valueFrom") or {}).get("secretKeyRef") or {})
        resource = str(reference.get("name") or "").split("/")[-1]
        version = str(reference.get("key") or reference.get("version") or "")
        if not version.isdigit() or version.startswith("0"):
            raise SystemExit(f"{surface}: secret reference is not pinned to a numeric version")
        actual_secrets[name] = {"resource": resource, "version": version}
    else:
        actual_values[name] = str(item.get("value", ""))
if actual_values != expected_env or actual_secrets != expected_secrets:
    raise SystemExit(f"{surface}: exact environment/secret allowlist postcondition failed")
url = status.get("url") or ""
if default_disabled == "true" and url:
    raise SystemExit(f"{surface}: external-origin rejection failed; default URL still exists")
if default_disabled == "false" and not url:
    raise SystemExit(f"{surface}: private synthetic path requires the internal default URL")
candidate_percent = sum(
    int(item.get("percent") or 0)
    for item in status.get("traffic") or []
    if item.get("revisionName") == candidate
)
if prior_exists == "true" and adopted != "true" and candidate_percent != 0:
    raise SystemExit(f"{surface}: staged candidate unexpectedly received traffic")
if prior_exists == "false" or adopted == "true":
    def exact_sole_candidate(items):
        return (
            len(items) == 1
            and items[0].get("revisionName") == candidate
            and int(items[0].get("percent") or 0) == 100
            and not items[0].get("tag")
            and not items[0].get("latestRevision", False)
        )
    if not exact_sole_candidate(spec.get("traffic") or []):
        raise SystemExit(f"{surface}: new companion desired traffic is not the sole candidate")
    if not exact_sole_candidate(status.get("traffic") or []):
        raise SystemExit(f"{surface}: new companion observed traffic is not the sole candidate")
PY
  actual_traffic="$(python3 "$STATE_TOOL" traffic-state "$service_json")" || {
    echo "ERROR: ${surface}/${region} staged traffic is malformed or unconverged" >&2
    return 1
  }
  if [ "$prior_exists" = true ]; then
    expected_traffic="$(prior_traffic_json "$prior")" || return 1
    if ! python3 - "$expected_traffic" "$actual_traffic" <<'PY'
import json
import sys

expected = json.loads(sys.argv[1])
actual = json.loads(sys.argv[2])
if actual != expected:
    raise SystemExit("staged deploy changed prior traffic allocation or tags")
PY
    then
      echo "ERROR: ${surface}/${region} changed its prior traffic during staging" >&2
      return 1
    fi
  fi
  python3 "$STATE_TOOL" hash-service "$service_json"
}

stage_one() {
  local surface="$1" region="$2" service account candidate prior prior_exists traffic
  local set_env set_secrets postcondition_hash default_url_arg traffic_arg adopted=false
  local journal_record="" journal_state="" recorded_hash="" revision_rc=0 revision_description=""
  service="$(surface_service "$surface")"
  account="$(surface_account "$surface")"
  prior="$(prior_json_path "$surface" "$region")"
  if [ "$ROLLOUT_MODE" = initial_split ]; then
    journal_record="$(stage_journal_read service "$surface" "$region")" || return 1
    prior_exists="$(jq -r .prior_exists <<<"$journal_record")" || return 1
    adopted="$(jq -r .adopted_bootstrap <<<"$journal_record")" || return 1
    candidate="$(jq -er .candidate_revision <<<"$journal_record")" || return 1
    journal_state="$(jq -er .state <<<"$journal_record")" || return 1
    if [ "$prior_exists" = true ]; then
      traffic="$(prior_traffic_json "$prior")"
    else
      traffic='[]'
    fi
  else
    if [ -f "$prior" ]; then
      prior_exists=true
      traffic="$(prior_traffic_json "$prior")"
    else
      prior_exists=false
      traffic='[]'
    fi
    candidate="${service}-${REVISION_SUFFIX}"
  fi
  surface_env_vars "$surface" "$region"
  surface_secret_bindings "$surface"
  set_env="$(IFS='|'; echo "^|^${SURFACE_ENV[*]}")"
  set_secrets="$(IFS=,; echo "${SURFACE_SECRETS[*]}")"
  if [ "$DEFAULT_URL_DISABLED" = true ]; then default_url_arg=--no-default-url; else default_url_arg=--default-url; fi
  traffic_arg=""
  if [ "$prior_exists" = true ]; then
    traffic_arg=--no-traffic
  fi
  if [ "$ROLLOUT_MODE" = initial_split ] && [ "$journal_state" = pending ]; then
    stage_journal_transition service "$surface" "$region" pending deploy_intent
    journal_state=deploy_intent
  fi
  if [ "$ROLLOUT_MODE" = initial_split ] && [ "$journal_state" = staged ]; then
    postcondition_hash="$(verify_service_postcondition \
      "$surface" "$service" "$region" "$candidate" "$prior_exists" "$prior" "$adopted")" || return 1
    recorded_hash="$(jq -er .postcondition_sha256 <<<"$journal_record")" || return 1
    [ "$postcondition_hash" = "$recorded_hash" ] || {
      echo "ERROR: staged service drifted from the initial-stage journal: ${service}/${region}" >&2
      return 1
    }
    log "resuming verified staged ${surface} service ${service}/${candidate} in ${region}"
  else
    if [ "$adopted" = true ]; then
      log "adopting verified bootstrap ${surface} service ${service}/${candidate} in ${region}"
    else
      revision_description="$(gc run revisions describe "$candidate" \
        --region="$region" --format=json 2>&1)" || revision_rc=$?
      if [ "$revision_rc" = 0 ]; then
        [ "$ROLLOUT_MODE" = initial_split ] && [ "$journal_state" = deploy_intent ] || {
          echo "ERROR: refusing to adopt unjournaled candidate ${candidate}/${region}" >&2
          return 1
        }
        log "inspecting exact recorded candidate ${service}/${candidate} before resume"
      else
        case "$revision_description" in
          *NOT_FOUND*|*"not found"*) ;;
          *) echo "ERROR: cannot determine candidate state: ${candidate}/${region}" >&2; return 1 ;;
        esac
        if [ "$prior_exists" = true ]; then
          log "staging ${surface} as ${service}/${candidate} in ${region} with zero requested traffic"
        else
          log "creating off-map ${surface} service ${service}/${candidate} in ${region} at its sole revision"
        fi
        if ! gc run deploy "$service" \
          --region="$region" \
          --image="$IMAGE" \
          --revision-suffix="$REVISION_SUFFIX" \
          ${traffic_arg:+"$traffic_arg"} \
          --allow-unauthenticated \
          --ingress="$TR_CLOUD_RUN_INGRESS" \
          "$default_url_arg" \
          --service-account="$account" \
          --port=8080 \
          --cpu=1 \
          --memory="$MEMORY" \
          --concurrency="$CONCURRENCY" \
          --min-instances="$MIN_INSTANCES" \
          --max-instances="$MAX_INSTANCES" \
          --max="$MAX_INSTANCES" \
          --timeout="${TIMEOUT_SECONDS}s" \
          --network="$CLOUD_RUN_NETWORK" \
          --subnet="$CLOUD_RUN_SUBNET" \
          --vpc-egress=private-ranges-only \
          --startup-probe="httpGet.path=/ready,initialDelaySeconds=0,timeoutSeconds=10,periodSeconds=10,failureThreshold=18" \
          --deploy-health-check \
          --set-env-vars="$set_env" \
          --set-secrets="$set_secrets" \
            --quiet >/dev/null; then
          log "deploy command exited non-zero; inspecting immutable postconditions"
        fi
      fi
    fi
    postcondition_hash="$(verify_service_postcondition \
      "$surface" "$service" "$region" "$candidate" "$prior_exists" "$prior" "$adopted")" || return 1
    if [ "$ROLLOUT_MODE" = initial_split ]; then
      stage_journal_transition service "$surface" "$region" deploy_intent staged "$postcondition_hash"
      journal_state=staged
    fi
  fi
  jq -cn \
    --arg surface "$surface" \
    --arg name "$service" \
    --arg region "$region" \
    --argjson prior_exists "$prior_exists" \
    --argjson prior_traffic "$traffic" \
    --argjson adopted_bootstrap "$adopted" \
    --arg candidate_revision "$candidate" \
    --arg runtime_service_account "$account" \
    --arg ingress "$TR_CLOUD_RUN_INGRESS" \
    --argjson default_url_disabled "$DEFAULT_URL_DISABLED" \
    --argjson concurrency "$CONCURRENCY" \
    --argjson min_instances "$MIN_INSTANCES" \
    --argjson service_max_instances "$MAX_INSTANCES" \
    --argjson revision_max_instances "$MAX_INSTANCES" \
    --argjson timeout_seconds "$TIMEOUT_SECONDS" \
    --arg memory "$MEMORY" \
    --argjson cpu 1 \
    --argjson container_port 8080 \
    --arg vpc_network "$CLOUD_RUN_NETWORK" \
    --arg vpc_subnet "$CLOUD_RUN_SUBNET" \
    --arg vpc_egress private-ranges-only \
    --arg startup_probe_path /ready \
    --argjson startup_probe_initial_delay_seconds 0 \
    --argjson startup_probe_timeout_seconds 10 \
    --argjson startup_probe_period_seconds 10 \
    --argjson startup_probe_failure_threshold 18 \
    --argjson max_request_body_bytes "$MAX_REQUEST_BODY_BYTES" \
    --argjson max_in_flight_request_body_bytes "$MAX_IN_FLIGHT_REQUEST_BODY_BYTES" \
    --argjson max_concurrent_request_bodies "$MAX_CONCURRENT_REQUEST_BODIES" \
    --argjson request_body_read_timeout_seconds "$REQUEST_BODY_READ_TIMEOUT_SECONDS" \
    --arg postcondition_sha256 "$postcondition_hash" \
    '{surface:$surface,name:$name,region:$region,prior_exists:$prior_exists,
      prior_traffic:$prior_traffic,adopted_bootstrap:$adopted_bootstrap,
      candidate_revision:$candidate_revision,
      runtime_service_account:$runtime_service_account,ingress:$ingress,
      default_url_disabled:$default_url_disabled,concurrency:$concurrency,
      min_instances:$min_instances,service_max_instances:$service_max_instances,
      revision_max_instances:$revision_max_instances,timeout_seconds:$timeout_seconds,
      memory:$memory,cpu:$cpu,container_port:$container_port,
      vpc_network:$vpc_network,vpc_subnet:$vpc_subnet,vpc_egress:$vpc_egress,
      startup_probe_path:$startup_probe_path,
      startup_probe_initial_delay_seconds:$startup_probe_initial_delay_seconds,
      startup_probe_timeout_seconds:$startup_probe_timeout_seconds,
      startup_probe_period_seconds:$startup_probe_period_seconds,
      startup_probe_failure_threshold:$startup_probe_failure_threshold,
      max_request_body_bytes:$max_request_body_bytes,
      max_in_flight_request_body_bytes:$max_in_flight_request_body_bytes,
      max_concurrent_request_bodies:$max_concurrent_request_bodies,
      request_body_read_timeout_seconds:$request_body_read_timeout_seconds,
      postcondition_sha256:$postcondition_sha256}' >>"$ENTRIES_FILE"
}

for surface in "${SURFACES[@]}"; do
  while IFS= read -r region; do
    stage_one "$surface" "$region"
  done < <(surface_region_lines "$surface")
done

PRIOR_HASH="$(python3 "$STATE_TOOL" hash-url-map "$PRIOR_URL_MAP")"
CANDIDATE_HASH="$(python3 "$STATE_TOOL" hash-url-map "$CANDIDATE_URL_MAP")"
if [ "$ROLLOUT_MODE" = existing_split ] && [ "$PRIOR_HASH" != "$CANDIDATE_HASH" ]; then
  echo "ERROR: existing split URL map would change during a regional traffic ramp" >&2
  exit 1
fi

if [ "$ROLLOUT_MODE" = initial_split ]; then
  non_staged="$(python3 - "$STAGE_JOURNAL" <<'PY'
import json
import sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(sum(item["state"] != "staged" for item in value["service_states"]))
PY
)" || exit 1
  [ "$non_staged" = 0 ] || {
    echo "ERROR: initial-stage journal has unsettled services before manifest publication" >&2
    exit 1
  }
  stage_phase="$(jq -er .phase "$STAGE_JOURNAL")" || exit 1
  if [ "$stage_phase" = services ]; then
    stage_journal_set_phase services manifest_intent
    stage_phase=manifest_intent
  fi
else
  stage_phase=""
fi

if [ ! -e "$MANIFEST" ]; then
  python3 "$STATE_TOOL" build-manifest \
    --manifest "$MANIFEST" \
    --entries "$ENTRIES_FILE" \
    --rollout-mode "$ROLLOUT_MODE" \
    --project-id "$PROJECT_ID" \
    --image "$IMAGE" \
    --release "$RELEASE" \
    --created-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --regions "$REGION_CSV" \
    --primary-region "$TR_PRIMARY_REGION" \
    --gateway-regions "$GATEWAY_REGION_CSV" \
    --internal-regions "$INTERNAL_ALLOWED_REGION_CSV" \
    --bootstrap-artifact-sha256 "$BOOTSTRAP_ARTIFACT_SHA256" \
    --legacy-hardening-artifact-sha256 "$LEGACY_HARDENING_ARTIFACT_SHA256" \
    --frontend-attestation-sha256 "$FRONTEND_ATTESTATION_SHA256" \
    --legacy-fallback "$LEGACY_FALLBACK_FILE" \
    --domains "$DOMAINS" \
    --preserved-hosts "$PRESERVED_HOSTS" \
    --url-map-name "$URL_MAP_NAME" \
    --https-proxy "$HTTPS_PROXY" \
    --prior-snapshot "$PRIOR_URL_MAP_NAME" \
    --candidate-snapshot "$CANDIDATE_URL_MAP_NAME" \
    --prior-sha256 "$PRIOR_HASH" \
    --candidate-sha256 "$CANDIDATE_HASH"
elif [ "$ROLLOUT_MODE" != initial_split ] || \
    { [ "$stage_phase" != manifest_intent ] && [ "$stage_phase" != complete ]; }; then
  echo "ERROR: rollout manifest exists without a journaled publication intent" >&2
  exit 1
fi

python3 "$STATE_TOOL" validate-manifest "$MANIFEST"
if [ "$ROLLOUT_MODE" = initial_split ]; then
  manifest_sha="$(python3 - "$MANIFEST" <<'PY'
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)" || exit 1
  recorded_manifest_sha="$(jq -r '.manifest_sha256 // ""' "$STAGE_JOURNAL")"
  if [ "$stage_phase" = complete ]; then
    [ "$recorded_manifest_sha" = "$manifest_sha" ] || {
      echo "ERROR: completed initial-stage manifest hash drifted" >&2
      exit 1
    }
  else
    stage_journal_set_phase manifest_intent complete "" "$manifest_sha"
  fi
fi
log "six-surface staging is Ready and postverified; promotion manifest: ${MANIFEST}"
log "no Cloud Run traffic or HTTPS URL-map route was changed by this command"
