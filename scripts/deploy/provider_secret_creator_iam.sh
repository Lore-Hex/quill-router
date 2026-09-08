#!/usr/bin/env bash
# Run explicitly as a project IAM administrator, never from the deploy workflow.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
ROLE_ID="trustedRouterProviderSecretCreator"
ROLE_NAME="projects/${PROJECT_ID}/roles/${ROLE_ID}"
MEMBER="serviceAccount:tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
APPLY=0

if [ "$#" -eq 1 ] && [ "$1" = "--apply" ]; then
  APPLY=1
elif [ "$#" -ne 0 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

gc() { gcloud --project "$PROJECT_ID" "$@"; }
run() {
  if [ "$APPLY" -eq 0 ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Inventory reads must succeed before any mutation. A denied read is not absence.
existing_role="$(gc iam roles list --filter="name=${ROLE_NAME}" --format='value(name)')"
bound_role="$(gc projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.role=${ROLE_NAME} AND bindings.members=${MEMBER}" \
  --format='value(bindings.role)')"

operation=create
if [ "$existing_role" = "$ROLE_NAME" ]; then
  operation=update
fi
run gc iam roles "$operation" "$ROLE_ID" \
  --title="TrustedRouter Provider Secret Creator" \
  --description="Create secret containers only. Version writes and reads require separate per-secret grants; no IAM management." \
  --permissions=secretmanager.secrets.create --stage=GA --quiet

if [ "$bound_role" != "$ROLE_NAME" ]; then
  run gc projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$MEMBER" --role="$ROLE_NAME" --condition=None --quiet
fi

if [ "$APPLY" -eq 1 ]; then
  echo "Provider secret creation granted to ${MEMBER}"
fi
