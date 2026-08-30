#!/usr/bin/env bash
# Refresh code on existing pre-split synthetic jobs without changing their
# identity, network, scheduler, or secret bindings. The full synthetic.sh path
# remains reserved for the isolated observer/billing service deployment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

[ -n "${IMAGE:-}" ] || {
  echo "ERROR: IMAGE is required" >&2
  exit 2
}

release="${TR_SYNTHETIC_RELEASE:-${SHA:-}}"
if [ -z "$release" ]; then
  release="$(git rev-parse --short HEAD 2>/dev/null || true)"
fi
[ -n "$release" ] || {
  echo "ERROR: SHA or TR_SYNTHETIC_RELEASE is required" >&2
  exit 2
}
release="${release:0:7}"

if [ -n "${TR_BILLING_SERVICE:-}" ]; then
  echo "ERROR: split service is configured; use synthetic.sh" >&2
  exit 2
fi
if [ "${TR_ALLOW_DEPLOYED_COMBINED_SURFACE:-}" = "true" ]; then
  echo "ERROR: image refresh must not enable the combined-surface bridge" >&2
  exit 2
fi

gc artifacts docker images describe "$IMAGE" >/dev/null

jobs=(
  "us-central1:trusted-router-synthetic-us-central1:health"
  "europe-west4:trusted-router-synthetic-europe-west4:health"
  "us-central1:trusted-router-throughput-us-central1:worker"
  "us-central1:trusted-router-image-generation-us-central1:worker"
  "us-central1:trusted-router-video-generation-us-central1:worker"
)

for entry in "${jobs[@]}"; do
  IFS=: read -r job_region job_name job_kind <<<"$entry"
  before="$(gc run jobs describe "$job_name" --region "$job_region" --format=json)" || {
    echo "ERROR: existing synthetic job ${job_name} is required" >&2
    exit 1
  }

  surface="$(jq -r '
    [.spec.template.spec.template.spec.containers[0].env[]?
      | select(.name == "TR_SERVICE_SURFACE") | .value][0] // ""
  ' <<<"$before")"
  observer_secret_count="$(jq -r '
    [.spec.template.spec.template.spec.containers[0].env[]?
      | select(.name == "TR_OBSERVER_INTERNAL_TOKEN")]
    | length
  ' <<<"$before")"
  if [ "$surface" != "combined" ] || [ "$observer_secret_count" != "0" ]; then
    echo "ERROR: ${job_name} is not an approved pre-split combined job" >&2
    exit 1
  fi

  sensitive_before="$(jq -cS '{
    serviceAccountName: (.spec.template.spec.template.spec.serviceAccountName // null),
    vpcAccess: (.spec.template.spec.template.spec.vpcAccess // null),
    annotations: (.spec.template.spec.template.metadata.annotations // {}),
    secrets: ([.spec.template.spec.template.spec.containers[0].env[]?
      | select(.valueFrom != null)
      | {name, valueFrom}] | sort_by(.name))
  }' <<<"$before")"

  env_updates="TR_RELEASE=${release}"
  if [ "$job_kind" = "health" ]; then
    env_updates="${env_updates},TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=https://trustedrouter.com"
  fi

  log "refreshing existing synthetic job ${job_name} in ${job_region}"
  gc run jobs update "$job_name" \
    --region "$job_region" \
    --image "$IMAGE" \
    --update-env-vars "$env_updates" \
    --quiet >/dev/null

  after="$(gc run jobs describe "$job_name" --region "$job_region" --format=json)"
  sensitive_after="$(jq -cS '{
    serviceAccountName: (.spec.template.spec.template.spec.serviceAccountName // null),
    vpcAccess: (.spec.template.spec.template.spec.vpcAccess // null),
    annotations: (.spec.template.spec.template.metadata.annotations // {}),
    secrets: ([.spec.template.spec.template.spec.containers[0].env[]?
      | select(.valueFrom != null)
      | {name, valueFrom}] | sort_by(.name))
  }' <<<"$after")"
  if [ "$sensitive_after" != "$sensitive_before" ]; then
    echo "ERROR: ${job_name} identity, network, or secret bindings changed" >&2
    exit 1
  fi
done

log "synthetic image refresh complete; no identities, networks, schedulers, or secrets changed"
