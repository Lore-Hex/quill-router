#!/usr/bin/env bash
# Keep runtime-only provider credentials outside the GitHub deploy identity.
# Every deploy proves the deploy identity cannot read these values. IAM repair
# is deliberately operator-only: giving CI setIamPolicy would let compromised
# workflow code grant itself accessor and defeat the isolation boundary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

TR_DEPLOY_SA="${TR_DEPLOY_SA:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
SECRET_LIST="${SCRIPT_DIR}/runtime_only_provider_secrets.txt"
DEPLOY_MEMBER="serviceAccount:${TR_DEPLOY_SA}"
REMEDIATE_IAM="${TR_REMEDIATE_RUNTIME_ONLY_SECRET_IAM:-0}"
MAX_ATTEMPTS="${TR_SECRET_IAM_MAX_ATTEMPTS:-3}"

retry_capture() {
  local description="$1"
  shift
  local attempt output
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    if output="$("$@" </dev/null 2>/dev/null)"; then
      printf '%s' "$output"
      return 0
    fi
    if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
      log "${description} failed (attempt ${attempt}/${MAX_ATTEMPTS}); retrying"
      sleep $((attempt * 2))
    fi
  done
  echo "ERROR: ${description} failed after ${MAX_ATTEMPTS} attempts" >&2
  return 1
}

retry_quiet() {
  local description="$1"
  shift
  retry_capture "$description" "$@" >/dev/null
}

secret_accessor_members() {
  local secret_name="$1"
  local attempt error_file output
  error_file="$(mktemp)"
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    if output="$(gc secrets get-iam-policy "$secret_name" \
      --flatten="bindings[].members" \
      --filter="bindings.role:roles/secretmanager.secretAccessor" \
      --format="value(bindings.members)" </dev/null 2>"$error_file")"; then
      rm -f "$error_file"
      printf '%s' "$output"
      return 0
    fi
    if grep -Eqi 'PERMISSION_DENIED|permission.*denied' "$error_file"; then
      rm -f "$error_file"
      return 3
    fi
    if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
      log "read IAM policy for ${secret_name} failed (attempt ${attempt}/${MAX_ATTEMPTS}); retrying"
      sleep $((attempt * 2))
    fi
  done
  rm -f "$error_file"
  echo "ERROR: read IAM policy for ${secret_name} failed after ${MAX_ATTEMPTS} attempts" >&2
  return 2
}

verify_effective_access_denied() {
  local secret_name="$1"
  local attempt output
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    if output="$(gc secrets versions access latest --secret="$secret_name" </dev/null 2>&1)"; then
      # Distinct status lets an authorized operator repair the binding without
      # ever printing or retaining the secret value captured in `output`.
      unset output
      return 10
    fi
    if printf '%s' "$output" | grep -Eqi \
      'PERMISSION_DENIED|secretmanager\.versions\.access.*denied'; then
      return 0
    fi
    if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
      log "effective-access check for ${secret_name} was inconclusive (attempt ${attempt}/${MAX_ATTEMPTS}); retrying"
      sleep $((attempt * 2))
    fi
  done
  echo "ERROR: could not prove effective access is denied for ${secret_name}" >&2
  return 1
}

active_gcloud_account() {
  retry_capture "read active gcloud account" \
    gc auth list --filter="status:ACTIVE" --format="value(account)" --limit=1
}

assert_operator_repair_identity() {
  local active_account
  active_account="$(active_gcloud_account)"
  if [ -z "$active_account" ] || [ "$active_account" = "$TR_DEPLOY_SA" ]; then
    echo "ERROR: IAM remediation requires an operator identity, never ${TR_DEPLOY_SA}" >&2
    return 1
  fi
}

if [ "$REMEDIATE_IAM" = "1" ]; then
  assert_operator_repair_identity
  while IFS= read -r secret_name <&3; do
    [ -n "$secret_name" ] || continue
    members="$(secret_accessor_members "$secret_name")" || exit "$?"
    if printf '%s\n' "$members" | grep -Fqx "$DEPLOY_MEMBER"; then
      log "revoking ${TR_DEPLOY_SA} accessor on ${secret_name}"
      retry_quiet "remove deploy accessor from ${secret_name}" \
        gc secrets remove-iam-policy-binding "$secret_name" \
        --member="$DEPLOY_MEMBER" \
        --role="roles/secretmanager.secretAccessor" \
        --all \
        --quiet
    else
      log "deploy accessor already absent on ${secret_name}"
    fi

    members="$(secret_accessor_members "$secret_name")" || exit "$?"
    if printf '%s\n' "$members" | grep -Fqx "$DEPLOY_MEMBER"; then
      echo "ERROR: ${TR_DEPLOY_SA} still has accessor on ${secret_name}" >&2
      exit 1
    fi
  done 3< "$SECRET_LIST"
  log "direct per-secret bindings are absent; run the deploy verifier to prove effective denial"
  exit 0
fi

active_account="$(active_gcloud_account)"
if [ "$active_account" != "$TR_DEPLOY_SA" ]; then
  echo "ERROR: isolation verification must run as ${TR_DEPLOY_SA}, not ${active_account:-<none>}" >&2
  exit 1
fi

while IFS= read -r secret_name <&3; do
  [ -n "$secret_name" ] || continue
  if verify_effective_access_denied "$secret_name"; then
    log "deploy access denied for runtime-only secret ${secret_name}"
  else
    access_status="$?"
    if [ "$access_status" -eq 10 ]; then
      echo "ERROR: ${TR_DEPLOY_SA} has effective access to ${secret_name}" >&2
      echo "Repair with an authorized operator: TR_REMEDIATE_RUNTIME_ONLY_SECRET_IAM=1 $0" >&2
      exit 1
    fi
    exit "$access_status"
  fi
done 3< "$SECRET_LIST"
