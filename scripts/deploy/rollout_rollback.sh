#!/usr/bin/env bash
# Promote or roll back a staged six-surface Cloud Run release.
#
# The manifest contains no rendered environment or secret data.  It is safe to
# retain as an operator recovery record, but it is intentionally not printed by
# this helper.  Every mutating command is recorded before execution because a
# provider command may apply its mutation and still exit non-zero.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_edge_security.sh
source "${SCRIPT_DIR}/_edge_security.sh"
STATE_TOOL="${SCRIPT_DIR}/rollout_state.py"
JOURNAL_TOOL="${SCRIPT_DIR}/rollout_journal.py"
IAM_VERIFY="${SCRIPT_DIR}/rollout_iam_verify.sh"

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/deploy/rollout_rollback.sh promote MANIFEST
  bash scripts/deploy/rollout_rollback.sh promote-step MANIFEST primary|secondary|all 10|50|100
  bash scripts/deploy/rollout_rollback.sh verify MANIFEST [primary|secondary|all] [PERCENT]
  bash scripts/deploy/rollout_rollback.sh rollback MANIFEST

`promote` requires TR_ROLLOUT_SMOKE_COMMAND to name an executable callback. It
is called as CALLBACK MANIFEST PHASE PERCENT after each Admin-API-verified
phase. Existing phases are primary/secondary; initial phases are
initial-companions, initial-map, and initial-console. The initial transaction
never changes Cloud Run traffic: all companions are new and the verified
bootstrap internal revision is adopted at 100%, so the URL-map import is the
only serving cutover. Workflows may instead call
promote-step for an existing split, run authenticated LB/browser synthetics,
and invoke rollback on failure.
EOF
  exit 2
}

[ "$#" -ge 2 ] || usage
COMMAND="$1"
MANIFEST="$2"
shift 2

python3 "$STATE_TOOL" validate-manifest "$MANIFEST"
MANIFEST_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
PROJECT_ID="$(jq -er '.project_id' "$MANIFEST")"
ROLLOUT_MODE="$(jq -er '.rollout_mode' "$MANIFEST")"
URL_MAP_NAME="$(jq -er '.url_map.name' "$MANIFEST")"
HTTPS_PROXY="$(jq -er '.url_map.https_proxy' "$MANIFEST")"
PRIOR_URL_MAP="${MANIFEST_DIR}/$(jq -er '.url_map.prior_snapshot' "$MANIFEST")"
CANDIDATE_URL_MAP="${MANIFEST_DIR}/$(jq -er '.url_map.candidate_snapshot' "$MANIFEST")"
PROMOTION_STATE="${MANIFEST_DIR}/$(jq -er '.promotion_state' "$MANIFEST")"
EXPECTED_PRIOR_HASH="$(jq -er '.url_map.prior_sha256' "$MANIFEST")"
EXPECTED_CANDIDATE_HASH="$(jq -er '.url_map.candidate_sha256' "$MANIFEST")"
EXPECTED_FRONTEND_ATTESTATION_SHA256="$(jq -er '.frontend_attestation_sha256' "$MANIFEST")"
EXPECTED_LEGACY_HARDENING_SHA256="$(jq -r '.legacy_hardening_artifact_sha256 // ""' "$MANIFEST")"
FRONTEND_ATTESTATION="${MANIFEST}.frontend-attestation.json"
LEGACY_HARDENING_ARTIFACT="${MANIFEST}.legacy-hardening.json"
STATE_GCS_URI="${TR_ROLLOUT_STATE_GCS_URI:-}"
REQUIRE_DURABLE_STATE="${TR_ROLLOUT_REQUIRE_DURABLE_STATE:-true}"
REQUIRE_RECOVERY_BUNDLE="${TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE:-false}"
BUNDLE_GCS_URI="${TR_ROLLOUT_BUNDLE_GCS_URI:-}"
AUTHORITY_GCS_URI="${TR_ROLLOUT_AUTHORITY_GCS_URI:-}"
RECOVERY_GCS_PREFIX="${TR_ROLLOUT_RECOVERY_GCS_PREFIX:-}"
RECOVERY_GCS_ROLE="${TR_ROLLOUT_RECOVERY_GCS_ROLE:-}"
STATE_GCS_ROLE="${TR_ROLLOUT_STATE_GCS_ROLE:-}"
STATE_GCS_BUCKET=""
STATE_GCS_OBJECT=""
STATE_STORE_GENERATION=""
GENERATION_MARKER="${MANIFEST_DIR}/promotion-state.generation"
LEASE_OWNER="${TR_ROLLOUT_OPERATION_ID:-}"
LEASE_TTL_SECONDS="${TR_ROLLOUT_LEASE_TTL_SECONDS:-900}"
PROVIDER_MUTATION_TIMEOUT_SECONDS="${TR_ROLLOUT_PROVIDER_MUTATION_TIMEOUT_SECONDS:-300}"
LEASE_TAKEOVER="${TR_ROLLOUT_TAKEOVER_EXPIRED_LEASE:-false}"
LEASE_HELD=0
LEASE_OPERATION=""

# Cloud Run's replaceService request is asynchronous on the server. gcloud
# normally waits for it, so an ordinary client exit does not prove that the
# server-side mutation stopped. Wait one full provider mutation deadline before
# treating unchanged state as settled; the refreshed lease retains 15 seconds.
PROVIDER_SETTLE_SECONDS="${TR_ROLLOUT_PROVIDER_SETTLE_SECONDS:-$PROVIDER_MUTATION_TIMEOUT_SECONDS}"

artifact_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = os.lstat(path)
if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("rollout prerequisite artifact must be a regular mode-0600 file")
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
}

verify_prerequisite_artifacts() {
  local actual
  actual="$(artifact_sha256 "$FRONTEND_ATTESTATION")" || return 1
  [ "$actual" = "$EXPECTED_FRONTEND_ATTESTATION_SHA256" ] || {
    echo "ERROR: frontend attestation digest differs from the manifest" >&2
    return 1
  }
  python3 "${SCRIPT_DIR}/rollout_frontend_attest.py" \
    verify-artifact "$FRONTEND_ATTESTATION" || return 1
  if [ "$ROLLOUT_MODE" = initial_split ]; then
    [ -n "$EXPECTED_LEGACY_HARDENING_SHA256" ] || {
      echo "ERROR: initial manifest has no legacy-hardening artifact digest" >&2
      return 1
    }
    actual="$(artifact_sha256 "$LEGACY_HARDENING_ARTIFACT")" || return 1
    [ "$actual" = "$EXPECTED_LEGACY_HARDENING_SHA256" ] || {
      echo "ERROR: legacy-hardening artifact digest differs from the manifest" >&2
      return 1
    }
    TR_LEGACY_HARDENING_ARTIFACT="$LEGACY_HARDENING_ARTIFACT" \
      bash "${SCRIPT_DIR}/rollout_legacy_harden.sh" \
        --verify-artifact "$LEGACY_HARDENING_ARTIFACT" || return 1
  elif [ -n "$EXPECTED_LEGACY_HARDENING_SHA256" ]; then
    echo "ERROR: existing split unexpectedly binds legacy hardening" >&2
    return 1
  fi
}
case "$REQUIRE_DURABLE_STATE" in
  true|false) ;;
  *) echo "ERROR: TR_ROLLOUT_REQUIRE_DURABLE_STATE must be true or false" >&2; exit 2 ;;
esac
case "$REQUIRE_RECOVERY_BUNDLE:$LEASE_TAKEOVER" in
  true:true|true:false|false:true|false:false) ;;
  *) echo "ERROR: recovery bundle/lease takeover flags must be true or false" >&2; exit 2 ;;
esac
if [ "$REQUIRE_RECOVERY_BUNDLE" = true ]; then
  [ -n "$BUNDLE_GCS_URI" ] && [ -n "$AUTHORITY_GCS_URI" ] && \
    [ -n "$RECOVERY_GCS_PREFIX" ] && [ -n "$RECOVERY_GCS_ROLE" ] || {
    echo "ERROR: production recovery requires prefix, bundle, authority, and role inputs" >&2
    exit 1
  }
  python3 - "$RECOVERY_GCS_PREFIX" "$BUNDLE_GCS_URI" "$AUTHORITY_GCS_URI" <<'PY' || exit 1
import re
import sys

prefix, bundle, authority = sys.argv[1:]
match = re.fullmatch(
    r"gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])/([^\x00-\x1f\x7f]+)",
    prefix,
)
if (
    not match
    or prefix.endswith("/")
    or "//" in match.group(2)
    or any(part in {"", ".", ".."} for part in match.group(2).split("/"))
):
    raise SystemExit("TR_ROLLOUT_RECOVERY_GCS_PREFIX is not canonical")
bucket, object_prefix = match.groups()
expected_authority = f"gs://{bucket}/{object_prefix}/authority.json"
release_prefix = f"gs://{bucket}/{object_prefix}/releases/"
if authority != expected_authority:
    raise SystemExit("rollout authority URI is outside the dedicated recovery prefix")
if not bundle.startswith(release_prefix):
    raise SystemExit("rollout bundle URI is outside the dedicated releases prefix")
epoch = bundle[len(release_prefix):]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,159}", epoch):
    raise SystemExit("rollout bundle must use one unique canonical manifest epoch")
PY
  if [ -n "$STATE_GCS_ROLE" ] && [ "$STATE_GCS_ROLE" != "$RECOVERY_GCS_ROLE" ]; then
    echo "ERROR: legacy journal and recovery custom-role inputs disagree" >&2
    exit 1
  fi
  STATE_GCS_ROLE="$RECOVERY_GCS_ROLE"
  expected_state_uri="${BUNDLE_GCS_URI%/}/$(basename "$PROMOTION_STATE")"
  if [ -n "$STATE_GCS_URI" ] && [ "$STATE_GCS_URI" != "$expected_state_uri" ]; then
    echo "ERROR: rollout state URI is outside the manifest recovery bundle" >&2
    exit 1
  fi
  STATE_GCS_URI="$expected_state_uri"
fi
if [ "$REQUIRE_DURABLE_STATE" = true ] && [ -z "$STATE_GCS_URI" ]; then
  echo "ERROR: durable rollout state requires TR_ROLLOUT_STATE_GCS_URI" >&2
  exit 1
fi
if [ -n "$LEASE_OWNER" ] && ! [[ "$LEASE_OWNER" =~ ^[A-Za-z0-9._:-]{8,160}$ ]]; then
  echo "ERROR: TR_ROLLOUT_OPERATION_ID is invalid" >&2
  exit 1
fi
if ! [[ "$LEASE_TTL_SECONDS" =~ ^[0-9]+$ ]] || \
   [ "$LEASE_TTL_SECONDS" -lt 60 ] || [ "$LEASE_TTL_SECONDS" -gt 3600 ]; then
  echo "ERROR: TR_ROLLOUT_LEASE_TTL_SECONDS must be from 60 through 3600" >&2
  exit 2
fi
if ! [[ "$PROVIDER_MUTATION_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || \
   [ "$PROVIDER_MUTATION_TIMEOUT_SECONDS" -lt 5 ] || \
   [ "$PROVIDER_MUTATION_TIMEOUT_SECONDS" -ge $((LEASE_TTL_SECONDS - 15)) ]; then
  echo "ERROR: provider mutation timeout must be at least 5 seconds and end 15 seconds before lease expiry" >&2
  exit 2
fi
if ! [[ "$PROVIDER_SETTLE_SECONDS" =~ ^[0-9]+$ ]] || \
   [ "$PROVIDER_SETTLE_SECONDS" -lt 1 ] || \
   [ "$PROVIDER_SETTLE_SECONDS" -gt "$PROVIDER_MUTATION_TIMEOUT_SECONDS" ]; then
  echo "ERROR: provider settle window must be from 1 second through the provider mutation timeout" >&2
  exit 2
fi
if [ "$REQUIRE_DURABLE_STATE" = true ] && [ -z "$LEASE_OWNER" ] && \
   [ "$COMMAND" != verify ]; then
  echo "ERROR: durable rollout commands require TR_ROLLOUT_OPERATION_ID" >&2
  exit 1
fi
if [ -n "$STATE_GCS_URI" ]; then
  state_uri_parts="$(python3 - "$STATE_GCS_URI" <<'PY'
import re
import sys

match = re.fullmatch(r"gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])/([^\x00-\x1f\x7f]+)", sys.argv[1])
if not match or match.group(2).endswith("/") or "//" in match.group(2):
    raise SystemExit("TR_ROLLOUT_STATE_GCS_URI must name one canonical GCS object")
bucket, object_name = match.groups()
if any(part in {"", ".", ".."} for part in object_name.split("/")):
    raise SystemExit("TR_ROLLOUT_STATE_GCS_URI contains an unsafe object path")
print(bucket)
print(object_name)
PY
)" || exit 1
  STATE_GCS_BUCKET="${state_uri_parts%%$'\n'*}"
  STATE_GCS_OBJECT="${state_uri_parts#*$'\n'}"
  if ! [[ "$STATE_GCS_ROLE" =~ ^projects/${PROJECT_ID}/roles/[A-Za-z][A-Za-z0-9_.]{2,63}$ ]]; then
    echo "ERROR: durable rollout state requires a project-local TR_ROLLOUT_STATE_GCS_ROLE" >&2
    exit 1
  fi
fi
MANIFEST_SHA256="$(python3 - "$MANIFEST" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
PROMOTION_STARTED=0

[ -x "$IAM_VERIFY" ] || {
  echo "ERROR: six-runtime IAM verifier is missing or not executable" >&2
  exit 1
}

gc() { gcloud --project "$PROJECT_ID" "$@"; }

bounded_gc_mutation() {
  python3 - "$PROVIDER_MUTATION_TIMEOUT_SECONDS" "$PROJECT_ID" "$@" <<'PY'
import os
import signal
import subprocess
import sys

timeout = int(sys.argv[1])
command = ["gcloud", "--project", sys.argv[2], *sys.argv[3:]]
process = subprocess.Popen(command, start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=timeout))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    print(
        "ERROR: provider mutation exceeded its lease-bounded deadline; "
        "the durable mutation fence remains unresolved",
        file=sys.stderr,
    )
    raise SystemExit(124)
PY
}
log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

verify_access_contract() {
  bash "$IAM_VERIFY" --project "$PROJECT_ID" --manifest "$MANIFEST"
}

require_snapshot_hashes() {
  local prior_hash candidate_hash
  [ -f "$PRIOR_URL_MAP" ] || { echo "ERROR: prior URL-map snapshot is missing" >&2; return 1; }
  [ -f "$CANDIDATE_URL_MAP" ] || { echo "ERROR: candidate URL-map snapshot is missing" >&2; return 1; }
  prior_hash="$(python3 "$STATE_TOOL" hash-url-map "$PRIOR_URL_MAP")" || return 1
  candidate_hash="$(python3 "$STATE_TOOL" hash-url-map "$CANDIDATE_URL_MAP")" || return 1
  [ "$prior_hash" = "$EXPECTED_PRIOR_HASH" ] || {
    echo "ERROR: prior URL-map snapshot hash drifted" >&2
    return 1
  }
  [ "$candidate_hash" = "$EXPECTED_CANDIDATE_HASH" ] || {
    echo "ERROR: candidate URL-map snapshot hash drifted" >&2
    return 1
  }
}

current_url_map_hash() {
  local current
  current="$(mktemp "${TMPDIR:-/tmp}/tr-url-map-current-XXXXXX")"
  gc compute url-maps describe "$URL_MAP_NAME" --global --format=json >"$current" || return 1
  python3 "$STATE_TOOL" hash-url-map "$current" || return 1
  rm -f "$current"
}

verify_https_proxy_binding() {
  local live_map
  live_map="$(gc compute target-https-proxies describe "$HTTPS_PROXY" \
    --global --format='value(urlMap.basename())')" || return 1
  [ "$live_map" = "$URL_MAP_NAME" ] || {
    echo "ERROR: HTTPS proxy ${HTTPS_PROXY} no longer targets manifest map ${URL_MAP_NAME}" >&2
    return 1
  }
}

verify_legacy_fallback() {
  [ "$ROLLOUT_MODE" = initial_split ] || return 0
  local entries entry service region current expected_hash actual_hash
  local expected_generation actual_generation expected_traffic actual_traffic
  local backend expected_backend_hash actual_backend_hash backend_current
  local regions legacy_neg expected_revision expected_revision_hash revision_current
  local expected_iam_hash iam_current actual_iam_hash legacy_account legacy_refs
  local legacy_secret legacy_version secret_version_current secret_policy_current
  entries="$(jq -c '.legacy_fallback[]' "$MANIFEST")" || return 1
  [ -n "$entries" ] || {
    echo "ERROR: initial manifest has no legacy fallback cohort" >&2
    return 1
  }
  backend="$(jq -er '.legacy_fallback[0].backend' "$MANIFEST")" || return 1
  expected_backend_hash="$(jq -er '.legacy_fallback[0].backend_postcondition_sha256' "$MANIFEST")" || return 1
  if ! jq -e --arg backend "$backend" --arg digest "$expected_backend_hash" '
      all(.legacy_fallback[];
        .backend == $backend and .backend_postcondition_sha256 == $digest)
    ' "$MANIFEST" >/dev/null; then
    echo "ERROR: legacy fallback backend binding differs across regions" >&2
    return 1
  fi
  backend_current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-backend-current-XXXXXX")"
  gc compute backend-services describe "$backend" --global --format=json \
    >"$backend_current" || return 1
  actual_backend_hash="$(python3 "$STATE_TOOL" hash-resource "$backend_current")" || return 1
  [ "$actual_backend_hash" = "$expected_backend_hash" ] || {
    echo "ERROR: legacy fallback backend drifted" >&2
    return 1
  }
  regions="$(jq -r '.regions[]' "$MANIFEST")" || return 1
  python3 - "$backend_current" "$PROJECT_ID" "$regions" <<'PY' || return 1
import json
import sys
from pathlib import Path

backend = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
project = sys.argv[2]
regions = sys.argv[3].splitlines()
expected = {
    (
        f"https://www.googleapis.com/compute/v1/projects/{project}/regions/"
        f"{region}/networkEndpointGroups/trusted-router-control-neg"
    )
    for region in regions
}
actual = {item.get("group") for item in backend.get("backends") or []}
if actual != expected:
    raise SystemExit("legacy fallback backend regional NEG membership drifted")
PY
  rm -f "$backend_current"
  while IFS= read -r region; do
    [ -n "$region" ] || continue
    legacy_neg="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-neg-current-XXXXXX")"
    gc compute network-endpoint-groups describe trusted-router-control-neg \
      --region="$region" --format=json >"$legacy_neg" || return 1
    python3 - "$legacy_neg" <<'PY' || return 1
import json
import sys
from pathlib import Path

neg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if neg.get("networkEndpointType") not in {None, "SERVERLESS"}:
    raise SystemExit("legacy fallback NEG is not serverless")
if (neg.get("cloudRun") or {}) != {"service": "trusted-router"}:
    raise SystemExit("legacy fallback NEG target drifted")
PY
    rm -f "$legacy_neg"
  done <<<"$regions"
  while IFS= read -r entry; do
    service="$(jq -er '.service' <<<"$entry")" || return 1
    region="$(jq -er '.region' <<<"$entry")" || return 1
    expected_generation="$(jq -er '.generation' <<<"$entry")" || return 1
    expected_hash="$(jq -er '.postcondition_sha256' <<<"$entry")" || return 1
    expected_revision="$(jq -er '.serving_revision' <<<"$entry")" || return 1
    expected_revision_hash="$(jq -er '.serving_revision_sha256' <<<"$entry")" || return 1
    expected_iam_hash="$(jq -er '.invoker_iam_sha256' <<<"$entry")" || return 1
    expected_traffic="$(python3 - "$entry" <<'PY'
import json
import sys

print(json.dumps(json.loads(sys.argv[1])["traffic"], sort_keys=True, separators=(",", ":")))
PY
)" || return 1
    current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-service-current-XXXXXX")"
    gc run services describe "$service" --region="$region" --format=json \
      >"$current" || return 1
    revision_current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-revision-current-XXXXXX")"
    iam_current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-iam-current-XXXXXX")"
    legacy_refs="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-secret-refs-XXXXXX")"
    gc run revisions describe "$expected_revision" --region="$region" --format=json \
      >"$revision_current" || return 1
    gc run services get-iam-policy "$service" --region="$region" --format=json \
      >"$iam_current" || return 1
    actual_generation="$(jq -er '
      if (.metadata.generation | type) == "number" and
         .metadata.generation == .status.observedGeneration
      then .metadata.generation
      else error("legacy service generation is not observed") end
    ' "$current")" || return 1
    actual_hash="$(python3 "$STATE_TOOL" hash-service "$current")" || return 1
    actual_revision_hash="$(python3 "$STATE_TOOL" hash-revision "$revision_current")" || return 1
    actual_iam_hash="$(python3 "$STATE_TOOL" hash-iam-policy "$iam_current")" || return 1
    actual_traffic="$(python3 "$STATE_TOOL" traffic-state "$current" | python3 -c '
import json, sys
print(json.dumps(json.load(sys.stdin), sort_keys=True, separators=(",", ":")))
')" || return 1
    legacy_account="$(python3 - "$current" "$revision_current" "$iam_current" \
      "$expected_revision" "$legacy_refs" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

service = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
revision = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
policy = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
expected_revision = sys.argv[4]
refs_path = Path(sys.argv[5])
status = service.get("status") or {}
annotations = (service.get("metadata") or {}).get("annotations") or {}
if not any(
    item.get("type") == "Ready" and item.get("status") == "True"
    for item in status.get("conditions") or []
):
    raise SystemExit("legacy fallback service is not Ready")
if status.get("latestReadyRevisionName") != expected_revision:
    raise SystemExit("legacy fallback latest Ready revision drifted")
if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
    raise SystemExit("legacy fallback desired ingress is not LB-only")
if annotations.get("run.googleapis.com/ingress-status") != "internal-and-cloud-load-balancing":
    raise SystemExit("legacy fallback effective ingress is not LB-only")

revision_status = revision.get("status") or {}
if (revision.get("metadata") or {}).get("name") != expected_revision or not any(
    item.get("type") == "Ready" and item.get("status") == "True"
    for item in revision_status.get("conditions") or []
):
    raise SystemExit("legacy fallback serving revision is not Ready")
revision_spec = revision.get("spec") or {}
containers = revision_spec.get("containers") or []
account = revision_spec.get("serviceAccountName")
if len(containers) != 1 or not isinstance(account, str) or not account:
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
with refs_path.open("w", encoding="utf-8") as output:
    for resource, version in sorted(refs):
        output.write(f"{resource}\t{version}\n")
os.chmod(refs_path, 0o600)
print(account)
PY
)" || return 1
    rm -f "$current"
    if [ "$actual_generation" != "$expected_generation" ] || \
       [ "$actual_hash" != "$expected_hash" ] || \
       [ "$actual_revision_hash" != "$expected_revision_hash" ] || \
       [ "$actual_iam_hash" != "$expected_iam_hash" ] || \
       [ "$actual_traffic" != "$expected_traffic" ]; then
      echo "ERROR: legacy fallback ${service}/${region} drifted" >&2
      return 1
    fi
    while IFS=$'\t' read -r legacy_secret legacy_version; do
      [ -n "$legacy_secret" ] || continue
      secret_version_current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-secret-version-XXXXXX")"
      secret_policy_current="$(mktemp "${TMPDIR:-/tmp}/tr-legacy-secret-policy-XXXXXX")"
      gc secrets versions describe "$legacy_version" --secret="$legacy_secret" \
        --format=json >"$secret_version_current" || return 1
      gc secrets get-iam-policy "$legacy_secret" --format=json \
        >"$secret_policy_current" || return 1
      python3 - "$secret_version_current" "$secret_policy_current" \
        "serviceAccount:${legacy_account}" <<'PY' || return 1
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
      rm -f "$secret_version_current" "$secret_policy_current"
    done <"$legacy_refs"
    rm -f "$revision_current" "$iam_current" "$legacy_refs"
  done <<<"$entries"
}

assert_private_recovery_artifacts() {
  python3 - "$MANIFEST" "$PRIOR_URL_MAP" "$CANDIDATE_URL_MAP" "$PROMOTION_STATE" <<'PY' || return 1
import stat
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.exists():
        continue
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"rollout recovery artifact must be mode 0600: {path.name}")
PY
}

state_store_object_generation() {
  local metadata
  if metadata="$(gc storage objects describe "$STATE_GCS_URI" --format=json 2>&1)"; then
    python3 - "$metadata" <<'PY' || return 1
import json
import sys

generation = (json.loads(sys.argv[1]) or {}).get("generation")
try:
    generation = int(generation)
except (TypeError, ValueError):
    raise SystemExit("durable rollout journal has no numeric GCS generation") from None
if generation <= 0:
    raise SystemExit("durable rollout journal has an invalid GCS generation")
print(generation)
PY
    return
  fi
  case "$metadata" in
    *NOT_FOUND*|*"not found"*|*"No URLs matched"*) echo 0 ;;
    *) echo "ERROR: cannot inspect durable rollout journal metadata" >&2; return 1 ;;
  esac
}

state_store_pull() {
  [ -n "$STATE_GCS_URI" ] || return 0
  local generation temporary same
  generation="$(state_store_object_generation)" || return 1
  if [ "$generation" = 0 ]; then
    [ ! -e "$PROMOTION_STATE" ] || {
      echo "ERROR: local rollout journal exists but the configured durable object does not" >&2
      return 1
    }
    STATE_STORE_GENERATION=0
    return 0
  fi
  temporary="$(mktemp "${MANIFEST_DIR}/.promotion-state.remote.XXXXXX")"
  chmod 600 "$temporary"
  gc storage cp "$STATE_GCS_URI" "$temporary" --quiet >/dev/null || {
    rm -f "$temporary"
    echo "ERROR: cannot read the durable rollout journal" >&2
    return 1
  }
  promotion_state_file_is_valid "$temporary" || {
    rm -f "$temporary"
    echo "ERROR: durable rollout journal belongs to another or invalid manifest" >&2
    return 1
  }
  if [ -e "$PROMOTION_STATE" ]; then
    same="$(python3 - "$PROMOTION_STATE" "$temporary" <<'PY'
import sys
from pathlib import Path

print("true" if Path(sys.argv[1]).read_bytes() == Path(sys.argv[2]).read_bytes() else "false")
PY
)" || { rm -f "$temporary"; return 1; }
    if [ "$same" = true ]; then
      rm -f "$temporary"
    else
      # The generation-checked object is authoritative across hosted-runner
      # crashes and concurrent stale local workspaces.
      mv "$temporary" "$PROMOTION_STATE"
      chmod 600 "$PROMOTION_STATE"
    fi
  else
    mv "$temporary" "$PROMOTION_STATE"
    chmod 600 "$PROMOTION_STATE"
  fi
  STATE_STORE_GENERATION="$generation"
  printf '%s\n' "$generation" >"$GENERATION_MARKER" || return 1
  chmod 600 "$GENERATION_MARKER" || return 1
}

verify_recovery_authority() {
  [ "$REQUIRE_RECOVERY_BUNDLE" = true ] || return 0
  local authority_local
  authority_local="$(mktemp "${MANIFEST_DIR}/.authority-current.XXXXXX")"
  chmod 600 "$authority_local"
  gcloud storage cp "$AUTHORITY_GCS_URI" "$authority_local" --quiet >/dev/null || {
    rm -f "$authority_local"
    echo "ERROR: cannot read active rollout authority" >&2
    return 1
  }
  python3 "$JOURNAL_TOOL" authority-validate "$authority_local" "$MANIFEST" \
    active "$BUNDLE_GCS_URI" || {
    rm -f "$authority_local"
    return 1
  }
  rm -f "$authority_local"
}

preflight_durable_state_store() {
  [ -n "$STATE_GCS_URI" ] || return 0
  local bucket_json policy_json role_json deploy_member expected_resource
  bucket_json="$(gc storage buckets describe "gs://${STATE_GCS_BUCKET}" --format=json)" || {
    echo "ERROR: configured rollout journal bucket is not readable" >&2
    return 1
  }
  policy_json="$(gc storage buckets get-iam-policy "gs://${STATE_GCS_BUCKET}" --format=json)" || {
    echo "ERROR: configured rollout journal bucket IAM is not readable" >&2
    return 1
  }
  role_json="$(gc iam roles describe "$STATE_GCS_ROLE" --format=json)" || {
    echo "ERROR: configured rollout journal custom role is not readable" >&2
    return 1
  }
  deploy_member="serviceAccount:${TR_DEPLOY_SERVICE_ACCOUNT:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
  expected_resource="projects/_/buckets/${STATE_GCS_BUCKET}/objects/${STATE_GCS_OBJECT}"
  python3 - "$bucket_json" "$policy_json" "$role_json" "$STATE_GCS_ROLE" \
    "$deploy_member" "$expected_resource" <<'PY' || return 1
import json
import sys

bucket, policy, role = (json.loads(value) for value in sys.argv[1:4])
expected_role, deploy_member, expected_resource = sys.argv[4:]
iam = bucket.get("iamConfiguration") or {}
uniform = (iam.get("uniformBucketLevelAccess") or {}).get("enabled")
if uniform is not True or str(iam.get("publicAccessPrevention", "")).lower() != "enforced":
    raise SystemExit("rollout journal bucket must enforce uniform IAM and public access prevention")
if (bucket.get("versioning") or {}).get("enabled") is not True:
    raise SystemExit("rollout journal bucket must enable object versioning")
expected_condition = {
    "title": "trusted-router-rollout-journal",
    "expression": f'resource.name == "{expected_resource}"',
}
direct = []
for binding in policy.get("bindings", []) or []:
    members = binding.get("members", []) or []
    if deploy_member in members:
        condition = binding.get("condition") or {}
        direct.append(
            {
                "role": binding.get("role"),
                "members": members,
                "condition": {
                    "title": condition.get("title"),
                    "expression": condition.get("expression"),
                },
            }
        )
expected_binding = {
    "role": expected_role,
    "members": [deploy_member],
    "condition": expected_condition,
}
if direct != [expected_binding]:
    raise SystemExit("deploy identity must have one exact object-scoped journal binding")
permissions = sorted(role.get("includedPermissions") or [])
expected_permissions = sorted(
    ["storage.objects.create", "storage.objects.delete", "storage.objects.get"]
)
if permissions != expected_permissions or str(role.get("stage", "GA")) == "DISABLED":
    raise SystemExit("rollout journal custom role permissions are not least-privilege")
PY
  state_store_pull || return 1
  verify_recovery_authority || return 1
}

persist_state_candidate() {
  local candidate="$1" prior_generation="$STATE_STORE_GENERATION"
  local upload_failed=0 observed_generation remote same
  gc storage cp "$candidate" "$STATE_GCS_URI" \
    --if-generation-match="$prior_generation" --quiet >/dev/null || upload_failed=1
  observed_generation="$(state_store_object_generation)" || return 1
  if [ "$observed_generation" = 0 ] || [ "$observed_generation" -le "$prior_generation" ]; then
    echo "ERROR: durable rollout journal generation did not advance" >&2
    return 1
  fi
  remote="$(mktemp "${MANIFEST_DIR}/.promotion-state.verify.XXXXXX")"
  chmod 600 "$remote"
  gc storage cp "$STATE_GCS_URI" "$remote" --quiet >/dev/null || {
    rm -f "$remote"
    echo "ERROR: cannot verify the durable rollout journal write" >&2
    return 1
  }
  same="$(python3 - "$candidate" "$remote" <<'PY'
import sys
from pathlib import Path

print("true" if Path(sys.argv[1]).read_bytes() == Path(sys.argv[2]).read_bytes() else "false")
PY
)" || { rm -f "$remote"; return 1; }
  rm -f "$remote"
  [ "$same" = true ] || {
    echo "ERROR: durable rollout journal write lost its generation race" >&2
    return 1
  }
  if [ "$upload_failed" = 1 ]; then
    log "journal upload exited non-zero after applying; verified generation and content"
  fi
  mv "$candidate" "$PROMOTION_STATE"
  chmod 600 "$PROMOTION_STATE"
  STATE_STORE_GENERATION="$observed_generation"
  printf '%s\n' "$observed_generation" >"$GENERATION_MARKER" || return 1
  chmod 600 "$GENERATION_MARKER" || return 1
}

append_attempt_file() {
  local state_path="$1" operation="$2" surface="$3" service="$4" region="$5" target="$6"
  if [ "$LEASE_HELD" = 1 ]; then
    python3 "$JOURNAL_TOOL" append "$state_path" "$MANIFEST" \
      "$operation" "$surface" "$service" "$region" "$target" \
      --owner "$LEASE_OWNER" || return 1
  else
    python3 "$JOURNAL_TOOL" append "$state_path" "$MANIFEST" \
      "$operation" "$surface" "$service" "$region" "$target" || return 1
  fi
}

record_attempt() {
  local operation="$1" surface="$2" service="$3" region="$4" target="$5"
  local candidate
  if [ -z "$STATE_GCS_URI" ]; then
    append_attempt_file "$PROMOTION_STATE" "$operation" "$surface" "$service" "$region" "$target"
    return
  fi
  state_store_pull || return 1
  candidate="$(mktemp "${MANIFEST_DIR}/.promotion-state.candidate.XXXXXX")"
  chmod 600 "$candidate"
  if [ -e "$PROMOTION_STATE" ]; then
    cp "$PROMOTION_STATE" "$candidate"
  else
    rm -f "$candidate"
  fi
  append_attempt_file "$candidate" "$operation" "$surface" "$service" "$region" "$target" || {
    rm -f "$candidate"
    return 1
  }
  persist_state_candidate "$candidate" || {
    rm -f "$candidate"
    return 1
  }
}

acquire_operation_lease() {
  local operation="$1" candidate takeover_arg=""
  if [ -z "$LEASE_OWNER" ]; then
    LEASE_OWNER="local-${PPID}-$$-${RANDOM}"
  fi
  if [ "$LEASE_TAKEOVER" = true ]; then
    # Reconcile all live provider state while the expired lease still belongs
    # to its recorded owner. This is read-only and must succeed before a new
    # operation may replace that ownership record.
    preflight_candidates || return 1
    takeover_arg=--allow-expired-takeover
  fi
  if [ -z "$STATE_GCS_URI" ]; then
    python3 "$JOURNAL_TOOL" lease-acquire "$PROMOTION_STATE" "$MANIFEST" \
      "$LEASE_OWNER" "$operation" --ttl-seconds "$LEASE_TTL_SECONDS" \
      ${takeover_arg:+"$takeover_arg"} || return 1
  else
    state_store_pull || return 1
    candidate="$(mktemp "${MANIFEST_DIR}/.promotion-state.lease.XXXXXX")"
    chmod 600 "$candidate"
    if [ -e "$PROMOTION_STATE" ]; then cp "$PROMOTION_STATE" "$candidate"; else rm -f "$candidate"; fi
    python3 "$JOURNAL_TOOL" lease-acquire "$candidate" "$MANIFEST" \
      "$LEASE_OWNER" "$operation" --ttl-seconds "$LEASE_TTL_SECONDS" \
      ${takeover_arg:+"$takeover_arg"} || { rm -f "$candidate"; return 1; }
    persist_state_candidate "$candidate" || { rm -f "$candidate"; return 1; }
  fi
  LEASE_OPERATION="$operation"
  LEASE_HELD=1
}

refresh_operation_lease() {
  [ "$LEASE_HELD" = 1 ] || {
    echo "ERROR: provider mutation requires an active rollout operation lease" >&2
    return 1
  }
  local candidate
  verify_prerequisite_artifacts || return 1
  verify_recovery_authority || return 1
  if [ -z "$STATE_GCS_URI" ]; then
    python3 "$JOURNAL_TOOL" lease-refresh "$PROMOTION_STATE" "$MANIFEST" \
      "$LEASE_OWNER" --ttl-seconds "$LEASE_TTL_SECONDS" || return 1
  else
    state_store_pull || return 1
    candidate="$(mktemp "${MANIFEST_DIR}/.promotion-state.refresh.XXXXXX")"
    chmod 600 "$candidate"
    cp "$PROMOTION_STATE" "$candidate"
    python3 "$JOURNAL_TOOL" lease-refresh "$candidate" "$MANIFEST" \
      "$LEASE_OWNER" --ttl-seconds "$LEASE_TTL_SECONDS" || {
      rm -f "$candidate"
      return 1
    }
    persist_state_candidate "$candidate" || {
      rm -f "$candidate"
      return 1
    }
  fi
}

assert_operation_lease() {
  [ "$LEASE_HELD" = 1 ] || {
    echo "ERROR: provider mutation requires an active rollout operation lease" >&2
    return 1
  }
  if [ -n "$STATE_GCS_URI" ]; then
    state_store_pull || return 1
  fi
  python3 "$JOURNAL_TOOL" lease-assert "$PROMOTION_STATE" "$MANIFEST" \
    "$LEASE_OWNER" || return 1
  verify_recovery_authority || return 1
  verify_prerequisite_artifacts || return 1
}

transition_operation_mutation() {
  local transition="$1" operation="$2" candidate
  [ "$LEASE_HELD" = 1 ] || {
    echo "ERROR: provider mutation requires an active rollout operation lease" >&2
    return 1
  }
  case "$transition" in begin|end) ;;
    *) echo "ERROR: invalid provider mutation fence transition" >&2; return 2 ;;
  esac
  if [ -z "$STATE_GCS_URI" ]; then
    python3 "$JOURNAL_TOOL" "lease-mutation-${transition}" \
      "$PROMOTION_STATE" "$MANIFEST" "$LEASE_OWNER" "$operation" || return 1
    return 0
  fi
  state_store_pull || return 1
  candidate="$(mktemp "${MANIFEST_DIR}/.promotion-state.mutation.XXXXXX")"
  chmod 600 "$candidate"
  cp "$PROMOTION_STATE" "$candidate"
  python3 "$JOURNAL_TOOL" "lease-mutation-${transition}" \
    "$candidate" "$MANIFEST" "$LEASE_OWNER" "$operation" || {
    rm -f "$candidate"
    return 1
  }
  persist_state_candidate "$candidate" || {
    rm -f "$candidate"
    return 1
  }
}

begin_operation_mutation() {
  transition_operation_mutation begin "$1"
}

end_operation_mutation() {
  transition_operation_mutation end "$1"
}

release_operation_lease() {
  [ "$LEASE_HELD" = 1 ] || return 0
  local candidate
  if [ -z "$STATE_GCS_URI" ]; then
    python3 "$JOURNAL_TOOL" lease-release "$PROMOTION_STATE" "$MANIFEST" \
      "$LEASE_OWNER" || return 1
  else
    state_store_pull || return 1
    candidate="$(mktemp "${MANIFEST_DIR}/.promotion-state.release.XXXXXX")"
    chmod 600 "$candidate"
    cp "$PROMOTION_STATE" "$candidate"
    python3 "$JOURNAL_TOOL" lease-release "$candidate" "$MANIFEST" \
      "$LEASE_OWNER" || { rm -f "$candidate"; return 1; }
    persist_state_candidate "$candidate" || { rm -f "$candidate"; return 1; }
  fi
  LEASE_HELD=0
  LEASE_OPERATION=""
}

promotion_state_file_is_valid() {
  local state_path="$1"
  [ -f "$state_path" ] || return 1
  jq -e \
    --arg manifest "$MANIFEST_SHA256" \
    --arg project "$PROJECT_ID" \
    --arg map "$URL_MAP_NAME" \
    --arg prior "$EXPECTED_PRIOR_HASH" \
    --arg candidate "$EXPECTED_CANDIDATE_HASH" '
      .schema_version == 1 and .manifest_sha256 == $manifest and
      .project_id == $project and .url_map_name == $map and
      .prior_url_map_sha256 == $prior and
      .candidate_url_map_sha256 == $candidate and
      (.attempts | type == "array")
    ' "$state_path" >/dev/null
}

promotion_state_is_valid() {
  promotion_state_file_is_valid "$PROMOTION_STATE"
}

latest_map_operation() {
  promotion_state_is_valid || return 1
  python3 - "$PROMOTION_STATE" <<'PY' || return 1
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
operations = {
    "url-map",
    "url-map-state",
    "rollback-url-map",
    "rollback-url-map-state",
}
matches = [item for item in state["attempts"] if item.get("operation") in operations]
if matches:
    item = matches[-1]
    print(f"{item['operation']}|{item['target']}")
PY
}

latest_traffic_operation() {
  local entry="$1" service region
  promotion_state_is_valid || return 1
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  python3 - "$PROMOTION_STATE" "$service" "$region" <<'PY' || return 1
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
service, region = sys.argv[2:]
operations = {
    "traffic",
    "traffic-state",
    "rollback-traffic",
    "rollback-tags",
    "rollback-traffic-state",
}
matches = [
    item
    for item in state["attempts"]
    if item.get("operation") in operations
    and item.get("service") == service
    and item.get("region") == region
]
if matches:
    item = matches[-1]
    print(f"{item['operation']}|{item['target']}")
PY
}

candidate_map_is_owned_for_promotion() {
  [ "$(latest_map_operation)" = "url-map-state|candidate" ]
}

candidate_map_is_owned_for_rollback() {
  case "$(latest_map_operation)" in
    url-map\|candidate|url-map-state\|candidate|rollback-url-map\|prior) return 0 ;;
    *) return 1 ;;
  esac
}

candidate_traffic_is_owned() {
  local entry="$1" actual="$2"
  [ "$(latest_traffic_operation "$entry")" = "traffic-state|${actual}" ]
}

service_json() {
  local service="$1" region="$2" output="$3"
  gc run services describe "$service" --region="$region" --format=json >"$output" || return 1
}

assert_candidate_contract() {
  local entry="$1"
  local service region candidate expected_hash current_hash ready
  local current iam
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
  expected_hash="$(jq -er '.postcondition_sha256' <<<"$entry")" || return 1
  current="$(mktemp "${TMPDIR:-/tmp}/tr-service-current-XXXXXX")"
  service_json "$service" "$region" "$current" || return 1
  current_hash="$(python3 "$STATE_TOOL" hash-service "$current")" || return 1
  ready="$(jq -r --arg revision "$candidate" '
    if .status.latestReadyRevisionName == $revision and
       any(.status.conditions[]?; .type == "Ready" and .status == "True")
    then "true" else "false" end
  ' "$current")" || return 1
  python3 - "$entry" "$current" "$MANIFEST" <<'PY' || return 1
import json
import sys
from pathlib import Path
entry = json.loads(sys.argv[1])
service = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
manifest = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
metadata = service.get("metadata") or {}
annotations = metadata.get("annotations") or {}
spec = service.get("spec") or {}
template = spec.get("template") or {}
template_annotations = (template.get("metadata") or {}).get("annotations") or {}
template_spec = template.get("spec") or {}
status = service.get("status") or {}
if metadata.get("generation") is None or status.get("observedGeneration") is None:
    raise SystemExit("service generation metadata is absent")
if str(status.get("observedGeneration")) != str(metadata.get("generation")):
    raise SystemExit("service desired generation is not observed")
if annotations.get("run.googleapis.com/ingress") != entry["ingress"]:
    raise SystemExit("service ingress differs from manifest")
if annotations.get("run.googleapis.com/ingress-status") != entry["ingress"]:
    raise SystemExit("effective service ingress differs from desired ingress")
expected_default_url = "true" if entry["default_url_disabled"] else None
actual_default_url = annotations.get("run.googleapis.com/default-url-disabled")
if entry["default_url_disabled"]:
    if str(actual_default_url).lower() != expected_default_url:
        raise SystemExit("default URL disablement annotation differs from manifest")
elif str(actual_default_url or "").lower() not in {"", "false"}:
    raise SystemExit("internal default URL annotation differs from manifest")
if template_spec.get("serviceAccountName") != entry["runtime_service_account"]:
    raise SystemExit("runtime identity differs from manifest")
if int(template_spec.get("containerConcurrency", -1)) != entry["concurrency"]:
    raise SystemExit("concurrency differs from manifest")
if str(template_spec.get("timeoutSeconds", "")).removesuffix("s") != str(entry["timeout_seconds"]):
    raise SystemExit("timeout differs from manifest")
if str(template_annotations.get("autoscaling.knative.dev/minScale")) != str(entry["min_instances"]):
    raise SystemExit("revision min instances differs from manifest")
if str(template_annotations.get("autoscaling.knative.dev/maxScale")) != str(entry["revision_max_instances"]):
    raise SystemExit("revision max instances differs from manifest")
service_max = (spec.get("scaling") or {}).get("maxInstanceCount")
if service_max is None:
    service_max = annotations.get("run.googleapis.com/maxScale")
if str(service_max) != str(entry["service_max_instances"]):
    raise SystemExit("service max instances differs from manifest")
url = status.get("url") or ""
if entry["default_url_disabled"] and url:
    raise SystemExit("disabled default URL became externally addressable")
if not entry["default_url_disabled"] and not url:
    raise SystemExit("internal private default URL is absent")
containers = template_spec.get("containers") or []
if len(containers) != 1:
    raise SystemExit("service must contain exactly one application container")
if template_spec.get("volumes") not in (None, []):
    raise SystemExit("unexpected service volumes are forbidden")
if template_spec.get("initContainers") not in (None, []):
    raise SystemExit("init containers are forbidden")
container = containers[0]
if container.get("volumeMounts") not in (None, []):
    raise SystemExit("unexpected volume mounts are forbidden")
if container.get("command") not in (None, []) or container.get("args") not in (None, []):
    raise SystemExit("container command/args override is forbidden")
if container.get("image") != manifest["image"]:
    raise SystemExit("candidate image differs from the immutable manifest digest")
ports = container.get("ports") or []
if len(ports) != 1 or int(ports[0].get("containerPort", -1)) != entry["container_port"]:
    raise SystemExit("container port differs from manifest")
limits = (container.get("resources") or {}).get("limits") or {}
if str(limits.get("memory")) != entry["memory"]:
    raise SystemExit("memory differs from manifest")
actual_cpu = str(limits.get("cpu") or "")
if actual_cpu not in {str(entry["cpu"]), f'{entry["cpu"] * 1000}m'}:
    raise SystemExit("CPU differs from manifest")
network_interfaces_raw = template_annotations.get("run.googleapis.com/network-interfaces")
try:
    network_interfaces = json.loads(network_interfaces_raw)
except (TypeError, ValueError):
    raise SystemExit("VPC network annotation is invalid") from None
if not isinstance(network_interfaces, list) or len(network_interfaces) != 1:
    raise SystemExit("VPC network interface count differs from manifest")

def exact_resource(value, kind, expected):
    text = str(value or "").rstrip("/")
    if text == expected:
        return True
    if f'/projects/{manifest["project_id"]}/' not in f'/{text}':
        return False
    return text.endswith(f'/{kind}/{expected}')

interface = network_interfaces[0]
if not exact_resource(interface.get("network"), "networks", entry["vpc_network"]):
    raise SystemExit("VPC network differs from manifest")
if not exact_resource(interface.get("subnetwork"), "subnetworks", entry["vpc_subnet"]):
    raise SystemExit("VPC subnet differs from manifest")
if template_annotations.get("run.googleapis.com/vpc-access-egress") != entry["vpc_egress"]:
    raise SystemExit("VPC egress differs from manifest")
probe = container.get("startupProbe") or {}
http_get = probe.get("httpGet") or {}
if http_get.get("path") != entry["startup_probe_path"]:
    raise SystemExit("startup probe path differs from manifest")
if http_get.get("port") is not None and int(http_get["port"]) != entry["container_port"]:
    raise SystemExit("startup probe port differs from manifest")
expected_probe = {
    "initialDelaySeconds": entry["startup_probe_initial_delay_seconds"],
    "timeoutSeconds": entry["startup_probe_timeout_seconds"],
    "periodSeconds": entry["startup_probe_period_seconds"],
    "failureThreshold": entry["startup_probe_failure_threshold"],
}
for name, expected in expected_probe.items():
    if int(probe.get(name, -1)) != expected:
        raise SystemExit(f"startup probe {name} differs from manifest")
env = {
    item.get("name"): str(item.get("value", ""))
    for item in container.get("env") or []
    if item.get("name") and "valueFrom" not in item
}
expected_env = {
    "TR_SERVICE_SURFACE": entry["surface"],
    "TR_RATE_LIMIT_CLIENT_IP_MODE": "edge_header",
    "TR_MAX_REQUEST_BODY_BYTES": str(entry["max_request_body_bytes"]),
    "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES": str(
        entry["max_in_flight_request_body_bytes"]
    ),
    "TR_MAX_CONCURRENT_REQUEST_BODIES": str(
        entry["max_concurrent_request_bodies"]
    ),
    "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS": str(
        entry["request_body_read_timeout_seconds"]
    ),
    "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS": "0",
    "TR_REMEDIATOR_IN_PROCESS_ENABLED": "false",
}
for name, expected in expected_env.items():
    if env.get(name) != expected:
        raise SystemExit(f"runtime admission contract {name} differs from manifest")
PY
  iam="$(gc run services get-iam-policy "$service" --region="$region" --format=json)" || return 1
  jq -e '
    [.bindings[]? | select(any(.members[]?; . == "allUsers"))
      | {role, condition: (.condition // null),
         allUsersCount: ([.members[]? | select(. == "allUsers")] | length)}]
    == [{role:"roles/run.invoker",condition:null,allUsersCount:1}]
  ' <<<"$iam" >/dev/null || {
    echo "ERROR: ${service}/${region} unauthenticated LB invocation IAM drifted" >&2
    return 1
  }
  rm -f "$current"
  [ "$current_hash" = "$expected_hash" ] || {
    echo "ERROR: service postcondition drift for ${service} in ${region}" >&2
    return 1
  }
  [ "$ready" = "true" ] || {
    echo "ERROR: candidate ${candidate} is not the latest Ready revision" >&2
    return 1
  }
}

candidate_percent() {
  local service="$1" region="$2" candidate="$3"
  gc run services describe "$service" --region="$region" --format=json \
    | jq -r --arg revision "$candidate" '
        [.status.traffic[]? | select(.revisionName == $revision) | (.percent // 0)]
        | add // 0
      '
}

edge_name() {
  local surface="$1" kind="$2"
  case "${surface}:${kind}" in
    public:backend) echo trusted-router-public-backend ;;
    actions:backend) echo trusted-router-actions-backend ;;
    console:backend) echo trusted-router-console-backend ;;
    chat:backend) echo trusted-router-chat-backend ;;
    webhooks:backend) echo trusted-router-webhooks-backend ;;
    internal:backend) echo trusted-router-billing-backend ;;
    public:neg) echo trusted-router-public-neg ;;
    actions:neg) echo trusted-router-actions-neg ;;
    console:neg) echo trusted-router-console-neg ;;
    chat:neg) echo trusted-router-chat-neg ;;
    webhooks:neg) echo trusted-router-webhooks-neg ;;
    internal:neg) echo trusted-router-billing-neg ;;
    public:policy) echo trusted-router-public-edge ;;
    actions:policy) echo trusted-router-actions-edge ;;
    console:policy) echo trusted-router-console-edge ;;
    chat:policy) echo trusted-router-chat-edge ;;
    webhooks:policy) echo trusted-router-webhooks-edge ;;
    internal:policy) echo trusted-router-billing-edge ;;
    *) return 2 ;;
  esac
}

assert_no_unmanaged_url_map_references() {
  local names name map_json
  names="$(gc compute url-maps list --format='value(name)')" || return 1
  [ -n "$names" ] || {
    echo "ERROR: global URL-map inventory is empty" >&2
    return 1
  }
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    [[ "$name" =~ ^[a-z][a-z0-9-]{0,61}$ ]] || {
      echo "ERROR: global URL-map inventory contains a noncanonical name" >&2
      return 1
    }
    [ "$name" = "$URL_MAP_NAME" ] && continue
    map_json="$(mktemp "${TMPDIR:-/tmp}/tr-url-map-inventory-XXXXXX")"
    gc compute url-maps describe "$name" --global --format=json >"$map_json" || return 1
    python3 - "$name" "$map_json" <<'PY' || return 1
import json
import sys
from pathlib import Path

map_name, path = sys.argv[1:]
managed = {
    "trusted-router-public-backend",
    "trusted-router-actions-backend",
    "trusted-router-console-backend",
    "trusted-router-chat-backend",
    "trusted-router-webhooks-backend",
    "trusted-router-billing-backend",
}

def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value

for value in strings(json.loads(Path(path).read_text(encoding="utf-8"))):
    basename = value.rstrip("/").rsplit("/", 1)[-1]
    if basename in managed:
        raise SystemExit(
            f"managed backend {basename} is also reachable from URL map {map_name}"
        )
PY
    rm -f "$map_json"
  done <<<"$names"
}

assert_edge_contract() {
  local surface entry service backend neg policy timeout backend_json policy_file region neg_json regions
  local backend_payload policy_payload regions_csv
  regions="$(jq -r '.regions[]' "$MANIFEST")" || return 1
  regions_csv="${regions//$'\n'/,}"
  for surface in public actions console chat webhooks internal; do
    entry="$(jq -cer --arg surface "$surface" '[.services[] | select(.surface == $surface)][0]' "$MANIFEST")" || return 1
    service="$(jq -er '.name' <<<"$entry")" || return 1
    timeout="$(jq -er '.timeout_seconds' <<<"$entry")" || return 1
    backend="$(edge_name "$surface" backend)" || return 1
    neg="$(edge_name "$surface" neg)" || return 1
    policy="$(edge_name "$surface" policy)" || return 1
    backend_json="$(mktemp "${TMPDIR:-/tmp}/tr-backend-current-XXXXXX")"
    gc compute backend-services describe "$backend" --global --format=json >"$backend_json" || return 1
    backend_payload="$(<"$backend_json")"
    verify_edge_backend_contract_json "$surface" "$backend_payload" \
      "$PROJECT_ID" "$regions_csv" "$service" "$neg" "$policy" "$timeout" || return 1
    rm -f "$backend_json"
    policy_file="$(mktemp "${TMPDIR:-/tmp}/tr-policy-current-XXXXXX")"
    gc compute security-policies describe "$policy" --global --format=json >"$policy_file" || return 1
    policy_payload="$(<"$policy_file")"
    verify_cloud_armor_policy_contract_json "$policy_payload" || return 1
    rm -f "$policy_file"
    while IFS= read -r region; do
      neg_json="$(gc compute network-endpoint-groups describe "$neg" --region="$region" --format=json)" || return 1
      python3 - "$service" "$neg" "$region" "$neg_json" <<'PY' || return 1
import json
import sys

service, neg, region, raw = sys.argv[1:]
target = (json.loads(raw).get("cloudRun") or {})
if target != {"service": service}:
    raise SystemExit(
        f"{neg}/{region} must target exactly Cloud Run service {service} without tag/urlMask"
    )
PY
    done <<<"$regions"
  done
}

traffic_argument_for_percent() {
  local entry="$1" percent="$2"
  python3 - "$percent" "$entry" <<'PY' || return 1
from __future__ import annotations
import json
import math
import sys
from collections import defaultdict

target = int(sys.argv[1])
entry = json.loads(sys.argv[2])
candidate = entry["candidate_revision"]
if target == 100:
    print(f"{candidate}=100")
    raise SystemExit
prior_by_revision: dict[str, int] = defaultdict(int)
for item in entry["prior_traffic"]:
    if item["percent"] > 0:
        prior_by_revision[item["resolved_revision"]] += item["percent"]
prior = sorted(prior_by_revision.items())
if not prior:
    raise SystemExit("cannot ramp a previously absent service below 100 percent")
remaining = 100 - target
total = sum(percent for _, percent in prior)
raw = [remaining * percent / total for _, percent in prior]
assigned = [math.floor(value) for value in raw]
for index in sorted(range(len(raw)), key=lambda item: raw[item] - assigned[item], reverse=True)[: remaining - sum(assigned)]:
    assigned[index] += 1
parts = [f"{candidate}={target}"]
parts.extend(
    f"{revision}={share}"
    for (revision, _), share in zip(prior, assigned, strict=True)
    if share
)
print(",".join(parts))
PY
}

restore_traffic_argument() {
  local entry="$1"
  python3 - "$entry" <<'PY' || return 1
import json
import sys
from collections import defaultdict

entry = json.loads(sys.argv[1])
targets: dict[str, int] = defaultdict(int)
for item in entry["prior_traffic"]:
    if item["percent"] > 0:
        target = "LATEST" if item["latest_revision"] else item["revision"]
        targets[target] += item["percent"]
print(",".join(f"{target}={targets[target]}" for target in sorted(targets)))
PY
}

restore_tag_arguments() {
  local entry="$1"
  jq -r '[.prior_traffic[] | select(.tag != null and .tag != "")
    | "\(.tag)=\(if .latest_revision then "LATEST" else .revision end)"] | join(",")' <<<"$entry"
}

verify_exact_prior_traffic() {
  local entry="$1" service="$2" region="$3"
  local current actual expected
  current="$(mktemp "${TMPDIR:-/tmp}/tr-traffic-current-XXXXXX")"
  service_json "$service" "$region" "$current" || return 1
  # Sort object keys as well as target rows so semantically identical JSON is
  # not rejected merely because the manifest encoder used a different key
  # insertion order.
  actual="$(python3 "$STATE_TOOL" traffic-state "$current" | jq -Sc 'sort_by(.tag // "", .latest_revision, .revision // "", .percent)')" || return 1
  expected="$(jq -Sc '.prior_traffic | sort_by(.tag // "", .latest_revision, .revision // "", .percent)' <<<"$entry")" || return 1
  rm -f "$current"
  [ "$actual" = "$expected" ] || {
    echo "ERROR: exact prior traffic targets were not restored for ${service}/${region}" >&2
    return 1
  }
}

verify_candidate_allocation() {
  local entry="$1" expected="$2"
  local service region current
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  current="$(mktemp "${TMPDIR:-/tmp}/tr-allocation-current-XXXXXX")"
  service_json "$service" "$region" "$current" || return 1
  verify_candidate_allocation_file "$entry" "$expected" "$current" || {
    rm -f "$current"
    return 1
  }
  rm -f "$current"
  assert_candidate_contract "$entry" || return 1
}

verify_candidate_allocation_file() {
  local entry="$1" expected="$2" current="$3"
  python3 - "$entry" "$expected" "$current" "$STATE_TOOL" <<'PY' || return 1
from __future__ import annotations
import json
import math
import subprocess
import sys
from collections import defaultdict

entry = json.loads(sys.argv[1])
target = int(sys.argv[2])
service = json.load(open(sys.argv[3], encoding="utf-8"))
candidate = entry["candidate_revision"]
prior_by_revision: dict[str, int] = defaultdict(int)
for item in entry["prior_traffic"]:
    if item["percent"] > 0:
        prior_by_revision[item["resolved_revision"]] += item["percent"]
prior = sorted(prior_by_revision.items())
expected_positive: dict[str, int] = {candidate: target} if target else {}
remaining = 100 - target
if remaining:
    if not prior:
        raise SystemExit("previously absent service cannot have remaining prior traffic")
    total = sum(percent for _, percent in prior)
    raw = [remaining * percent / total for _, percent in prior]
    assigned = [math.floor(value) for value in raw]
    for index in sorted(
        range(len(raw)), key=lambda item: raw[item] - assigned[item], reverse=True
    )[: remaining - sum(assigned)]:
        assigned[index] += 1
    for (revision, _), share in zip(prior, assigned, strict=True):
        if share:
            expected_positive[revision] = (
                expected_positive.get(revision, 0) + share
            )
state = json.loads(
    subprocess.check_output(
        [sys.executable, sys.argv[4], "traffic-state", sys.argv[3]], text=True
    )
)
if any(item["latest_revision"] for item in state):
    raise SystemExit("live traffic unexpectedly contains a floating LATEST target")
actual_positive: dict[str, int] = defaultdict(int)
for item in state:
    if item["percent"] > 0:
        actual_positive[item["resolved_revision"]] += item["percent"]
if dict(actual_positive) != expected_positive:
    raise SystemExit(
        f"exact traffic allocation failed: {dict(actual_positive)!r} != {expected_positive!r}"
    )
actual_tags = sorted(
    (
        item["tag"],
        item["latest_revision"],
        item["revision"],
        item["resolved_revision"],
    )
    for item in state
    if item["tag"] is not None
)
expected_tags = sorted(
    (
        item["tag"],
        item["latest_revision"],
        item["revision"],
        item["resolved_revision"],
    )
    for item in entry["prior_traffic"]
    if item["tag"] is not None
)
if actual_tags != expected_tags:
    raise SystemExit(f"traffic tags drifted: {actual_tags!r} != {expected_tags!r}")
PY
}

validate_step_transition() {
  local cohort="$1" target="$2" entry actual entry_cohort previous entries service region candidate
  case "$target" in
    10) previous=0 ;;
    50) previous=10 ;;
    100) previous=50 ;;
    *) return 2 ;;
  esac
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    if entry_in_cohort "$entry" "$cohort"; then
      entry_cohort="$cohort"
    else
      entry_cohort=other
    fi
    service="$(jq -er '.name' <<<"$entry")" || return 1
    region="$(jq -er '.region' <<<"$entry")" || return 1
    candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
    actual="$(candidate_percent "$service" "$region" "$candidate")" || return 1
    if [ "$entry_cohort" = "$cohort" ]; then
      [ "$actual" = "$previous" ] || [ "$actual" = "$target" ] || {
        echo "ERROR: ${cohort} ${target}% step is out of order (found ${actual}%)" >&2
        return 1
      }
    elif [ "$cohort" = primary ]; then
      [ "$actual" = 0 ] || {
        echo "ERROR: secondary region moved before the primary cohort completed" >&2
        return 1
      }
    else
      [ "$actual" = 100 ] || {
        echo "ERROR: secondary ramp requires every primary candidate at 100%" >&2
        return 1
      }
    fi
  done <<<"$entries"
}

entry_in_cohort() {
  local entry="$1" cohort="$2"
  local region primary
  region="$(jq -er '.region' <<<"$entry")" || return 2
  primary="$(jq -er '.regions[0]' "$MANIFEST")" || return 2
  case "$cohort" in
    primary) [ "$region" = "$primary" ] ;;
    secondary) [ "$region" != "$primary" ] ;;
    all) return 0 ;;
    *) return 2 ;;
  esac
}

preflight_candidates() {
  local entry prior_exists adopted actual live_map_hash entries surface
  verify_prerequisite_artifacts || return 1
  require_snapshot_hashes || return 1
  verify_https_proxy_binding || return 1
  verify_legacy_fallback || return 1
  verify_access_contract || return 1
  assert_no_unmanaged_url_map_references || return 1
  assert_edge_contract || return 1
  live_map_hash="$(current_url_map_hash)" || return 1
  if [ "$ROLLOUT_MODE" = existing_split ]; then
    [ "$live_map_hash" = "$EXPECTED_PRIOR_HASH" ] || {
      echo "ERROR: existing-split URL map drifted" >&2
      return 1
    }
  elif [ "$live_map_hash" = "$EXPECTED_CANDIDATE_HASH" ]; then
    promotion_state_is_valid && candidate_map_is_owned_for_promotion || {
      echo "ERROR: candidate URL map is live without this manifest's recorded import attempt" >&2
      return 1
    }
  elif [ "$live_map_hash" != "$EXPECTED_PRIOR_HASH" ]; then
    echo "ERROR: initial-split URL map is neither the known prior nor candidate" >&2
    return 1
  fi
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    prior_exists="$(jq -r '.prior_exists' <<<"$entry")"
    adopted="$(jq -r '.adopted_bootstrap' <<<"$entry")"
    surface="$(jq -er '.surface' <<<"$entry")" || return 1
    actual="$(candidate_percent \
      "$(jq -er '.name' <<<"$entry")" \
      "$(jq -er '.region' <<<"$entry")" \
      "$(jq -er '.candidate_revision' <<<"$entry")")" || return 1
    if [ "$ROLLOUT_MODE" = "initial_split" ]; then
      case "$actual" in 0|100) ;; *)
        echo "ERROR: initial-split candidate traffic must be 0% or 100%" >&2
        return 1 ;;
      esac
    else
      case "$actual" in 0|10|50|100) ;; *)
        echo "ERROR: existing-split candidate traffic is outside the monotonic ramp" >&2
        return 1 ;;
      esac
    fi
    if [ "$ROLLOUT_MODE" = initial_split ]; then
      [ "$actual" = 100 ] || {
        echo "ERROR: every initial companion must be fully staged before URL-map cutover" >&2
        return 1
      }
      if [ "$prior_exists" = true ]; then
        [ "$surface" = internal ] && [ "$adopted" = true ] && \
          verify_exact_prior_traffic "$entry" \
            "$(jq -er '.name' <<<"$entry")" \
            "$(jq -er '.region' <<<"$entry")" >/dev/null 2>&1 || {
          echo "ERROR: only a manifest-bound exact bootstrap internal revision may preexist initially" >&2
          return 1
        }
      fi
    else
      if [ "$prior_exists" = "false" ] && [ "$actual" != "0" ] && [ "$actual" != "100" ]; then
        echo "ERROR: previously absent companion has an unexpected partial traffic split" >&2
        return 1
      fi
      if [ "$actual" != 0 ] && [ "$prior_exists" = true ]; then
        candidate_traffic_is_owned "$entry" "$actual" || {
          echo "ERROR: ${surface} candidate traffic lacks this manifest's completed promotion state" >&2
          return 1
        }
      fi
    fi
    verify_candidate_allocation "$entry" "$actual" || return 1
  done <<<"$entries"
}

update_entry_traffic() {
  local entry="$1" percent="$2"
  local service region surface candidate argument provider_status=0
  local precommand_service precommand_identity precommand_allocation precommand_percent
  local settled_service settled_identity settled_allocation
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  surface="$(jq -er '.surface' <<<"$entry")" || return 1
  candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
  local actual
  actual="$(candidate_percent "$service" "$region" "$candidate")" || return 1
  if [ "$actual" = "$percent" ]; then
    verify_candidate_allocation "$entry" "$percent" || return 1
    return
  fi
  if [ "$actual" -gt "$percent" ]; then
    echo "ERROR: refusing to demote ${service}/${region} from ${actual}% to ${percent}%" >&2
    return 1
  fi
  argument="$(traffic_argument_for_percent "$entry" "$percent")" || return 1
  record_attempt "traffic" "$surface" "$service" "$region" "$percent" || return 1
  refresh_operation_lease || return 1
  begin_operation_mutation traffic || return 1
  precommand_service="$(mktemp "${TMPDIR:-/tmp}/tr-traffic-precommand-XXXXXX")"
  service_json "$service" "$region" "$precommand_service" || return 1
  precommand_identity="$(python3 "$STATE_TOOL" service-generation "$precommand_service")" || return 1
  precommand_allocation="$(python3 "$STATE_TOOL" traffic-state "$precommand_service")" || return 1
  precommand_percent="$(python3 - "$candidate" "$precommand_allocation" <<'PY'
import json
import sys

candidate = sys.argv[1]
traffic = json.loads(sys.argv[2])
print(sum(item["percent"] for item in traffic if item["resolved_revision"] == candidate))
PY
)" || return 1
  verify_candidate_allocation_file \
    "$entry" "$precommand_percent" "$precommand_service" || return 1
  rm -f "$precommand_service"
  bounded_gc_mutation run services update-traffic "$service" \
      --region="$region" \
      --to-revisions="$argument" \
      --quiet >/dev/null || provider_status=$?
  if [ "$provider_status" -eq 124 ]; then
    return 1
  elif [ "$provider_status" -ne 0 ]; then
    log "traffic command exited non-zero; inspecting provider state after settle window"
    refresh_operation_lease || return 1
    sleep "$PROVIDER_SETTLE_SECONDS"
    assert_operation_lease || return 1
    settled_service="$(mktemp "${TMPDIR:-/tmp}/tr-traffic-settled-XXXXXX")"
    service_json "$service" "$region" "$settled_service" || return 1
    settled_identity="$(python3 "$STATE_TOOL" service-generation "$settled_service")" || return 1
    settled_allocation="$(python3 "$STATE_TOOL" traffic-state "$settled_service")" || return 1
    if verify_candidate_allocation_file "$entry" "$percent" "$settled_service"; then
      rm -f "$settled_service"
      assert_candidate_contract "$entry" || return 1
      end_operation_mutation traffic || return 1
      record_attempt "traffic-state" "$surface" "$service" "$region" "$percent" || return 1
      return 0
    fi
    if [ "$settled_identity" = "$precommand_identity" ] && \
       [ "$settled_allocation" = "$precommand_allocation" ] && \
       verify_candidate_allocation_file \
         "$entry" "$precommand_percent" "$settled_service"; then
      rm -f "$settled_service"
      assert_candidate_contract "$entry" || return 1
      end_operation_mutation traffic || return 1
      return 1
    fi
    rm -f "$settled_service"
    return 1
  fi
  assert_operation_lease || return 1
  verify_candidate_allocation "$entry" "$percent" || return 1
  end_operation_mutation traffic || return 1
  record_attempt "traffic-state" "$surface" "$service" "$region" "$percent" || return 1
}

promote_step() {
  local cohort="$1" percent="$2" embedded="${3:-}" entry entries
  case "$cohort" in primary|secondary|all) ;; *) usage ;; esac
  case "$percent" in 10|50|100) ;; *) usage ;; esac
  [ "$ROLLOUT_MODE" = existing_split ] || {
    echo "ERROR: promote-step is only valid for an existing six-surface split" >&2
    return 1
  }
  [ "$cohort" != all ] || {
    echo "ERROR: existing-split ramps must name primary or secondary explicitly" >&2
    return 1
  }
  preflight_candidates || return 1
  if [ "$embedded" != embedded ]; then
    require_smoke_callback || return 1
    run_smoke_callback preflight 0 || return 1
    preflight_candidates || return 1
  fi
  validate_step_transition "$cohort" "$percent" || return 1
  PROMOTION_STARTED=1
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    entry_in_cohort "$entry" "$cohort" || continue
    update_entry_traffic "$entry" "$percent" || return 1
  done <<<"$entries"
}

verify_initial_candidate_set() {
  local entry entries
  [ "$ROLLOUT_MODE" = initial_split ] || return 2
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    verify_candidate_allocation "$entry" 100 || return 1
  done <<<"$entries"
}

import_candidate_url_map() {
  local current_hash provider_status=0
  current_hash="$(current_url_map_hash)" || return 1
  if [ "$current_hash" = "$EXPECTED_CANDIDATE_HASH" ]; then
    verify_https_proxy_binding || return 1
    verify_legacy_fallback || return 1
    promotion_state_is_valid && candidate_map_is_owned_for_promotion || {
      echo "ERROR: candidate URL map became live without this rollout's current import ownership" >&2
      return 1
    }
    return
  fi
  [ "$current_hash" = "$EXPECTED_PRIOR_HASH" ] || {
    echo "ERROR: live URL map is neither manifest prior nor candidate; refusing overwrite" >&2
    return 1
  }
  verify_https_proxy_binding || return 1
  verify_legacy_fallback || return 1
  record_attempt "url-map" "" "$URL_MAP_NAME" "global" "candidate" || return 1
  refresh_operation_lease || return 1
  begin_operation_mutation url-map || return 1
  # The durable fence is now held. Re-read every off-map candidate immediately
  # before the provider import so a concurrent service/revision change cannot
  # ride through an earlier smoke or preflight check.
  if ! verify_initial_candidate_set || \
     [ "$(current_url_map_hash)" != "$EXPECTED_PRIOR_HASH" ] || \
     ! verify_https_proxy_binding || ! verify_legacy_fallback; then
    # No provider mutation has started, so it is safe to settle this fence
    # before returning the failed immediate precondition.
    end_operation_mutation url-map || return 1
    return 1
  fi
  bounded_gc_mutation compute url-maps import "$URL_MAP_NAME" --global \
      --source="$CANDIDATE_URL_MAP" --quiet >/dev/null || provider_status=$?
  if [ "$provider_status" -eq 124 ]; then
    return 1
  elif [ "$provider_status" -ne 0 ]; then
    log "URL-map import exited non-zero; inspecting provider state"
  fi
  assert_operation_lease || return 1
  local imported_hash
  imported_hash="$(current_url_map_hash)" || return 1
  verify_https_proxy_binding || return 1
  verify_legacy_fallback || return 1
  [ "$imported_hash" = "$EXPECTED_CANDIDATE_HASH" ] || {
    echo "ERROR: candidate URL-map postcondition failed" >&2
    return 1
  }
  verify_initial_candidate_set || return 1
  # Re-read the candidate immediately before settling the durable fence. This
  # catches a same-hash import race followed by a second out-of-band rewrite.
  [ "$(current_url_map_hash)" = "$EXPECTED_CANDIDATE_HASH" ] || return 1
  end_operation_mutation url-map || return 1
  record_attempt "url-map-state" "" "$URL_MAP_NAME" "global" "candidate" || return 1
}

require_smoke_callback() {
  local callback="${TR_ROLLOUT_SMOKE_COMMAND:-}"
  [ -n "$callback" ] || {
    echo "ERROR: promotion requires TR_ROLLOUT_SMOKE_COMMAND" >&2
    return 1
  }
  [ "$callback" = "${SCRIPT_DIR}/rollout_smoke.sh" ] || {
    echo "ERROR: rollout smoke callback must be the repository-owned script" >&2
    return 1
  }
  [ -f "$callback" ] && [ ! -L "$callback" ] && [ -x "$callback" ] || {
    echo "ERROR: repository rollout smoke callback is not a regular executable" >&2
    return 1
  }
}

run_smoke_callback() {
  local cohort="$1" percent="$2"
  local callback="${TR_ROLLOUT_SMOKE_COMMAND:-}"
  require_smoke_callback || return 1
  "$callback" "$MANIFEST" "$cohort" "$percent" || return 1
  verify_prerequisite_artifacts || return 1
  preflight_candidates
}

promote_initial_split() {
  local entry entries
  local live_hash
  live_hash="$(current_url_map_hash)" || return 1
  # The five web companions are brand-new, and internal is the exact private
  # bootstrap revision. They are already at their sole 100% revision while the
  # prior map still targets the untouched legacy monolith. Do not issue any
  # Cloud Run traffic mutation during the initial split.
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    verify_candidate_allocation "$entry" 100 || return 1
  done <<<"$entries"
  if [ "$live_hash" = "$EXPECTED_PRIOR_HASH" ]; then
    run_smoke_callback initial-companions 100 || return 1
    verify_access_contract || return 1
    preflight_candidates || return 1
    import_candidate_url_map || return 1
  elif [ "$live_hash" = "$EXPECTED_CANDIDATE_HASH" ]; then
    # A resume is valid only when the durable journal's current map operation
    # owns this exact candidate import. This also catches an identical
    # out-of-band import between the entry preflight and the callback.
    import_candidate_url_map || return 1
  else
    echo "ERROR: initial split URL map is neither prior nor candidate" >&2
    return 1
  fi
  run_smoke_callback initial-map 100 || return 1
  verify_access_contract || return 1
  preflight_candidates || return 1
  # Retain the explicit console phase for browser orchestration, but it is a
  # post-cutover verification callback rather than a traffic change.
  run_smoke_callback initial-console 100 || return 1
  preflight_candidates || return 1
}

cohort_percent_bounds() {
  local cohort="$1" entry actual entries service region candidate
  local minimum=101 maximum=-1
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    entry_in_cohort "$entry" "$cohort" || continue
    service="$(jq -er '.name' <<<"$entry")" || return 1
    region="$(jq -er '.region' <<<"$entry")" || return 1
    candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
    actual="$(candidate_percent "$service" "$region" "$candidate")" || return 1
    [ "$actual" -lt "$minimum" ] && minimum="$actual"
    [ "$actual" -gt "$maximum" ] && maximum="$actual"
  done <<<"$entries"
  [ "$maximum" -ge 0 ] || return 1
  printf '%s %s\n' "$minimum" "$maximum"
}

promote_existing_split() {
  [ "$EXPECTED_PRIOR_HASH" = "$EXPECTED_CANDIDATE_HASH" ] || {
    echo "ERROR: an existing split must keep the URL map unchanged during regional ramps" >&2
    return 1
  }
  local live_hash
  live_hash="$(current_url_map_hash)" || return 1
  [ "$live_hash" = "$EXPECTED_PRIOR_HASH" ] || {
    echo "ERROR: existing split URL map drifted before promotion" >&2
    return 1
  }
  local cohort percent bounds cohort_min cohort_max primary_bounds primary_min primary_max cohorts
  bounds="$(cohort_percent_bounds secondary)" || return 1
  read -r cohort_min cohort_max <<<"$bounds" || return 1
  cohorts="primary secondary"
  if [ "$cohort_max" -gt 0 ]; then
    primary_bounds="$(cohort_percent_bounds primary)" || return 1
    read -r primary_min primary_max <<<"$primary_bounds" || return 1
    [ "$primary_min" = 100 ] && [ "$primary_max" = 100 ] || {
      echo "ERROR: secondary resume requires the complete primary cohort at 100%" >&2
      return 1
    }
    cohorts="secondary"
  fi
  for cohort in $cohorts; do
    for percent in 10 50 100; do
      bounds="$(cohort_percent_bounds "$cohort")" || return 1
      read -r cohort_min cohort_max <<<"$bounds" || return 1
      if [ "$cohort_max" -gt "$percent" ]; then
        continue
      fi
      promote_step "$cohort" "$percent" embedded || return 1
      run_smoke_callback "$cohort" "$percent" || return 1
      preflight_candidates || return 1
    done
  done
}

restore_entry() {
  local entry="$1"
  local service region surface prior_exists traffic tags provider_status=0
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  surface="$(jq -er '.surface' <<<"$entry")" || return 1
  prior_exists="$(jq -er '.prior_exists' <<<"$entry")" || return 1
  [ "$prior_exists" = "true" ] || return 0
  if verify_exact_prior_traffic "$entry" "$service" "$region" >/dev/null 2>&1; then
    record_attempt "rollback-traffic-state" "$surface" "$service" "$region" "prior" || return 1
    return 0
  fi
  traffic="$(restore_traffic_argument "$entry")" || return 1
  [ -n "$traffic" ] || {
    echo "ERROR: preexisting ${service}/${region} has no captured prior traffic" >&2
    return 1
  }
  record_attempt "rollback-traffic" "$surface" "$service" "$region" "prior" || return 1
  refresh_operation_lease || return 1
  begin_operation_mutation rollback-traffic || return 1
  bounded_gc_mutation run services update-traffic "$service" \
      --region="$region" --to-revisions="$traffic" --quiet >/dev/null || \
      provider_status=$?
  if [ "$provider_status" -eq 124 ]; then
    return 1
  elif [ "$provider_status" -ne 0 ]; then
    log "rollback traffic command exited non-zero; inspecting provider state"
  fi
  assert_operation_lease || return 1
  # Tags are restored separately because gcloud models them independently
  # from --to-revisions. Exact traffic intent (including floating LATEST) is
  # verified after both mutations.
  tags="$(restore_tag_arguments "$entry")" || return 1
  if [ -n "$tags" ]; then
    record_attempt "rollback-tags" "$surface" "$service" "$region" "prior" || return 1
    refresh_operation_lease || return 1
    provider_status=0
    bounded_gc_mutation run services update-traffic "$service" --region="$region" \
        --set-tags="$tags" --quiet >/dev/null || provider_status=$?
    if [ "$provider_status" -eq 124 ]; then
      return 1
    elif [ "$provider_status" -ne 0 ]; then
      log "rollback tag command exited non-zero; inspecting provider state"
    fi
    assert_operation_lease || return 1
  else
    record_attempt "rollback-tags" "$surface" "$service" "$region" "clear" || return 1
    refresh_operation_lease || return 1
    provider_status=0
    bounded_gc_mutation run services update-traffic "$service" --region="$region" \
        --clear-tags --quiet >/dev/null || provider_status=$?
    if [ "$provider_status" -eq 124 ]; then
      return 1
    elif [ "$provider_status" -ne 0 ]; then
      log "rollback clear-tags command exited non-zero; inspecting provider state"
    fi
    assert_operation_lease || return 1
  fi
  verify_exact_prior_traffic "$entry" "$service" "$region" || return 1
  end_operation_mutation rollback-traffic || return 1
  record_attempt "rollback-traffic-state" "$surface" "$service" "$region" "prior" || return 1
}

traffic_was_attempted() {
  local entry="$1" latest
  latest="$(latest_traffic_operation "$entry")" || return 1
  case "$latest" in
    traffic\|*|traffic-state\|*|rollback-traffic\|*|rollback-tags\|*) return 0 ;;
    *) return 1 ;;
  esac
}

preflight_rollback_entry() {
  local entry="$1" service region candidate actual
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
  if verify_exact_prior_traffic "$entry" "$service" "$region" >/dev/null 2>&1; then
    return 0
  fi
  actual="$(candidate_percent "$service" "$region" "$candidate")" || return 1
  case "$actual" in 0|10|50|100) ;; *)
    echo "ERROR: ${service}/${region} is not in a known prior/candidate traffic state" >&2
    return 1 ;;
  esac
  verify_candidate_allocation "$entry" "$actual" || {
    echo "ERROR: ${service}/${region} changed outside this rollout; refusing rollback" >&2
    return 1
  }
}

preflight_unattempted_entry() {
  local entry="$1" service region prior_exists candidate actual
  service="$(jq -er '.name' <<<"$entry")" || return 1
  region="$(jq -er '.region' <<<"$entry")" || return 1
  prior_exists="$(jq -er '.prior_exists' <<<"$entry")" || return 1
  if [ "$prior_exists" = true ]; then
    verify_exact_prior_traffic "$entry" "$service" "$region" >/dev/null 2>&1 || {
      echo "ERROR: unattempted ${service}/${region} is not at its exact prior traffic state" >&2
      return 1
    }
    return 0
  fi
  candidate="$(jq -er '.candidate_revision' <<<"$entry")" || return 1
  actual="$(candidate_percent "$service" "$region" "$candidate")" || return 1
  case "$actual" in 0|100) ;; *)
    echo "ERROR: unattempted new companion ${service}/${region} has partial traffic" >&2
    return 1 ;;
  esac
  verify_candidate_allocation "$entry" "$actual" || return 1
}

rollback_manifest() {
  local current_hash entry restored_hash rollback_failed=0 attempted_entries all_entries
  local provider_status=0
  require_snapshot_hashes || return 1
  verify_https_proxy_binding || return 1
  current_hash="$(current_url_map_hash)" || return 1
  case "$current_hash" in
    "$EXPECTED_PRIOR_HASH") ;;
    "$EXPECTED_CANDIDATE_HASH")
      candidate_map_is_owned_for_rollback || {
        echo "ERROR: candidate URL map is live without a recorded rollout attempt" >&2
        return 1
      }
      ;;
    *)
      echo "ERROR: live URL map is an unknown third state; refusing rollback mutations" >&2
      return 1
      ;;
  esac

  if [ "$ROLLOUT_MODE" = initial_split ]; then
    # The split services are all new and have no prior traffic state. Their
    # health/config failure may be the reason for rollback, so do not make
    # recovery depend on them. Ownership of the candidate map plus the exact
    # proxy and legacy fallback cohort are the complete rollback authority.
    verify_legacy_fallback || return 1
    if [ "$current_hash" = "$EXPECTED_CANDIDATE_HASH" ]; then
      verify_https_proxy_binding || return 1
      verify_legacy_fallback || return 1
      record_attempt "rollback-url-map" "" "$URL_MAP_NAME" "global" "prior" || return 1
      refresh_operation_lease || return 1
      begin_operation_mutation rollback-url-map || return 1
      bounded_gc_mutation compute url-maps import "$URL_MAP_NAME" --global \
          --source="$PRIOR_URL_MAP" --quiet >/dev/null || provider_status=$?
      if [ "$provider_status" -eq 124 ]; then
        return 1
      elif [ "$provider_status" -ne 0 ]; then
        log "rollback URL-map import exited non-zero; inspecting provider state"
      fi
      assert_operation_lease || return 1
      restored_hash="$(current_url_map_hash)" || return 1
      verify_https_proxy_binding || return 1
      verify_legacy_fallback || return 1
      [ "$restored_hash" = "$EXPECTED_PRIOR_HASH" ] || {
        echo "ERROR: initial rollback did not restore the legacy URL map" >&2
        return 1
      }
      [ "$(current_url_map_hash)" = "$EXPECTED_PRIOR_HASH" ] || return 1
      end_operation_mutation rollback-url-map || return 1
      record_attempt "rollback-url-map-state" "" "$URL_MAP_NAME" "global" "prior" || return 1
    elif promotion_state_is_valid && [ -n "$(latest_map_operation)" ]; then
      record_attempt "rollback-url-map-state" "" "$URL_MAP_NAME" "global" "prior" || return 1
    fi
    log "initial rollback restored the verified legacy URL map; split services were untouched"
    return 0
  fi

  all_entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  if ! promotion_state_is_valid; then
    [ "$current_hash" = "$EXPECTED_PRIOR_HASH" ] || {
      echo "ERROR: rollback state is missing or invalid while the candidate URL map is live" >&2
      return 1
    }
    while IFS= read -r entry; do
      preflight_unattempted_entry "$entry" || {
        echo "ERROR: rollback state is missing or invalid with non-prior traffic live" >&2
        return 1
      }
    done <<<"$all_entries"
    log "no valid rollout attempts exist and every service remains at its safe staged/prior state"
    return 0
  fi
  attempted_entries=""
  while IFS= read -r entry; do
    if traffic_was_attempted "$entry"; then
      attempted_entries="${attempted_entries}${entry}"$'\n'
    else
      preflight_unattempted_entry "$entry" || return 1
    fi
  done <<<"$all_entries"
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    preflight_rollback_entry "$entry" || return 1
  done <<<"$attempted_entries"

  # Existing-split traffic rollback restores console before any other surface.
  # Initial split has no traffic attempts: restoring the prior map atomically
  # returns every managed host to the untouched legacy monolith.
  if [ "$ROLLOUT_MODE" = existing_split ]; then
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      [ "$(jq -r '.surface' <<<"$entry")" = console ] || continue
      restore_entry "$entry" || {
        echo "ERROR: console rollback failed; refusing remaining rollback" >&2
        return 1
      }
    done <<<"$attempted_entries"
  fi

  if [ "$current_hash" = "$EXPECTED_CANDIDATE_HASH" ] && \
     [ "$EXPECTED_CANDIDATE_HASH" != "$EXPECTED_PRIOR_HASH" ]; then
    verify_https_proxy_binding || return 1
    verify_legacy_fallback || return 1
    record_attempt "rollback-url-map" "" "$URL_MAP_NAME" "global" "prior" || return 1
    refresh_operation_lease || return 1
    provider_status=0
    begin_operation_mutation rollback-url-map || return 1
    bounded_gc_mutation compute url-maps import "$URL_MAP_NAME" --global \
        --source="$PRIOR_URL_MAP" --quiet >/dev/null || provider_status=$?
    if [ "$provider_status" -eq 124 ]; then
      return 1
    elif [ "$provider_status" -ne 0 ]; then
      log "rollback URL-map import exited non-zero; inspecting provider state"
    fi
    assert_operation_lease || return 1
    restored_hash="$(current_url_map_hash)" || return 1
    verify_https_proxy_binding || return 1
    verify_legacy_fallback || return 1
    [ "$restored_hash" = "$EXPECTED_PRIOR_HASH" ] || {
      echo "ERROR: prior URL map was not restored; companion rollback was not attempted" >&2
      return 1
    }
    [ "$(current_url_map_hash)" = "$EXPECTED_PRIOR_HASH" ] || return 1
    end_operation_mutation rollback-url-map || return 1
    record_attempt "rollback-url-map-state" "" "$URL_MAP_NAME" "global" "prior" || return 1
  elif promotion_state_is_valid && [ -n "$(latest_map_operation)" ]; then
    record_attempt "rollback-url-map-state" "" "$URL_MAP_NAME" "global" "prior" || return 1
  fi

  # Companion failures after the prior map is restored cannot affect public
  # routing, so attempt every recorded entry and report a partial failure.
  for surface in internal webhooks chat actions public; do
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      [ "$(jq -r '.surface' <<<"$entry")" = "$surface" ] || continue
      restore_entry "$entry" || rollback_failed=1
    done <<<"$attempted_entries"
  done
  [ "$rollback_failed" = 0 ] || {
    echo "ERROR: rollback restored routing but one or more attempted companion traffic states failed" >&2
    return 1
  }
  log "rollback restored the captured URL map and all preexisting service traffic"
}

verify_manifest_state() {
  local cohort="${1:-all}" expected="${2:-}" entry live_hash matched=0 entries
  case "$cohort" in primary|secondary|all) ;; *)
    echo "ERROR: verify cohort must be primary, secondary, or all" >&2
    return 2 ;;
  esac
  if [ -n "$expected" ]; then
    case "$expected" in 0|10|50|100) ;; *)
      echo "ERROR: verify percent must be 0, 10, 50, or 100" >&2
      return 2 ;;
    esac
  fi
  preflight_candidates || return 1
  live_hash="$(current_url_map_hash)" || return 1
  if [ "$ROLLOUT_MODE" = existing_split ]; then
    [ "$live_hash" = "$EXPECTED_PRIOR_HASH" ] || {
      echo "ERROR: existing-split URL map drifted during verification" >&2
      return 1
    }
  elif [ "$live_hash" != "$EXPECTED_PRIOR_HASH" ] && \
       [ "$live_hash" != "$EXPECTED_CANDIDATE_HASH" ]; then
    echo "ERROR: initial-split URL map is an unknown state during verification" >&2
    return 1
  elif [ "$cohort" = all ] && [ "$expected" = 100 ] && \
       [ "$live_hash" != "$EXPECTED_CANDIDATE_HASH" ]; then
    echo "ERROR: fully promoted initial split does not use the candidate URL map" >&2
    return 1
  fi
  entries="$(jq -c '.services[]' "$MANIFEST")" || return 1
  while IFS= read -r entry; do
    entry_in_cohort "$entry" "$cohort" || continue
    matched=$((matched + 1))
    assert_candidate_contract "$entry" || return 1
    if [ -n "$expected" ]; then
      verify_candidate_allocation "$entry" "$expected" || return 1
    fi
  done <<<"$entries"
  [ "$matched" -gt 0 ] || {
    echo "ERROR: verify cohort selected no service entries" >&2
    return 1
  }
}

assert_private_recovery_artifacts
preflight_durable_state_store

case "$COMMAND" in
  promote)
    [ "$#" -eq 0 ] || usage
    promotion_failed=0
    acquire_operation_lease "promote" || promotion_failed=1
    require_smoke_callback || promotion_failed=1
    if [ "$promotion_failed" = 0 ]; then
      preflight_candidates || promotion_failed=1
    fi
    if [ "$promotion_failed" = 0 ]; then
      run_smoke_callback preflight 0 || promotion_failed=1
    fi
    if [ "$promotion_failed" = 0 ]; then
      PROMOTION_STARTED=1
      if [ "$ROLLOUT_MODE" = "initial_split" ]; then
        promote_initial_split || promotion_failed=1
      else
        promote_existing_split || promotion_failed=1
      fi
    fi
    if [ "$promotion_failed" != 0 ]; then
      if [ "$PROMOTION_STARTED" = 1 ]; then
        log "promotion failed; attempting manifest rollback"
        rollback_manifest || log "ERROR: automatic rollback also failed"
      fi
      release_operation_lease || log "ERROR: failed to release rollout operation lease"
      exit 1
    fi
    release_operation_lease || exit 1
    ;;
  promote-step)
    [ "$#" -eq 2 ] || usage
    promotion_failed=0
    acquire_operation_lease "promote-step:$1:$2" || promotion_failed=1
    if [ "$promotion_failed" = 0 ]; then
      promote_step "$1" "$2" || promotion_failed=1
    fi
    if [ "$promotion_failed" != 0 ]; then
      if [ "$PROMOTION_STARTED" = 1 ]; then
        log "promotion step failed; attempting manifest rollback"
        rollback_manifest || log "ERROR: automatic rollback also failed"
      fi
      release_operation_lease || log "ERROR: failed to release rollout operation lease"
      exit 1
    fi
    release_operation_lease || exit 1
    ;;
  verify)
    [ "$#" -le 2 ] || usage
    verify_manifest_state "${1:-all}" "${2:-}"
    ;;
  rollback)
    [ "$#" -eq 0 ] || usage
    rollback_failed=0
    acquire_operation_lease "rollback" || rollback_failed=1
    if [ "$rollback_failed" = 0 ]; then
      rollback_manifest || rollback_failed=1
    fi
    release_operation_lease || rollback_failed=1
    [ "$rollback_failed" = 0 ] || exit 1
    ;;
  *) usage ;;
esac
