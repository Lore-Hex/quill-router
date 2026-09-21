#!/usr/bin/env bash
# Update the existing AWS observer fleet. No provisioning, IAM, DNS, key
# rotation, or App Runner changes. Mirror the CI-built GCP artifact into each
# native ECR, then roll one region at a time without dropping healthy capacity.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"
source "${SCRIPT_DIR}/deploy_mutex.sh"
source "${SCRIPT_DIR}/cloud_bake_gate.sh"
source "${SCRIPT_DIR}/cloud_complete_gate.sh"

die() { printf '%s\n' "[FAIL] $*" >&2; exit 1; }
log() { printf '%s\n' "$*" >&2; }
[ -z "$(git status --porcelain)" ] || die "refusing dirty checkout"
[ "${TR_CLOUD_BAKE_AWS_BACKEND:-ecs}" = ecs ] || die "this deployment requires the ECS fleet gate"
RELEASE="$(git rev-parse HEAD)"
# Required runtime configuration, checked against the cloned task by the
# builder below. This cannot turn an already-disabled outbox on silently.
TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true
# GitHub's shallow build checkout uses seven-character tags. Resolve it
# unambiguously in the full checkout before trusting the registry lookup.
IMAGE_TAG="${RELEASE:0:7}"
[ "$(git rev-parse --verify "${IMAGE_TAG}^{commit}")" = "$RELEASE" ] || die "ambiguous image tag"
CI="$(gh run list --repo Lore-Hex/quill-router --workflow ci.yml --commit "$RELEASE" \
  --limit 1 --json conclusion --jq '.[0].conclusion')"
[ "$CI" = success ] || die "CI must pass for this exact release"
[ "$(aws sts get-caller-identity --query Account --output text)" = 330422590279 ] \
  || die "wrong AWS account"

WORK="$(mktemp -d)"
export DOCKER_CONFIG="${WORK}/docker"
mkdir "$DOCKER_CONFIG"
ACTIVE_REGION=""
ACTIVE_SERVICE=""
PREVIOUS_DEFINITION=""
PREVIOUS_RELEASE=""
cleanup() {
  local rc=$?
  trap '' INT TERM
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ -n "$ACTIVE_REGION" ]; then
    log "rolling back ${ACTIVE_REGION} to ${PREVIOUS_DEFINITION}; later regions remain untouched"
    if aws ecs update-service --cluster tr-cp --service "$ACTIVE_SERVICE" \
        --task-definition "$PREVIOUS_DEFINITION" --region "$ACTIVE_REGION" \
        --query service.taskDefinition --output text >/dev/null \
      && aws ecs wait services-stable --cluster tr-cp --services "$ACTIVE_SERVICE" --region "$ACTIVE_REGION" \
      && [ "$(python3 "${SCRIPT_DIR}/cloud_serving_release.py" aws-region "$ACTIVE_REGION" "$ACTIVE_SERVICE")" = "$PREVIOUS_RELEASE" ]; then
      log "rollback stabilized in ${ACTIVE_REGION}"
    else
      log "[FAIL] rollback needs operator attention in ${ACTIVE_REGION}"
    fi
  fi
  rm -rf "$WORK"
  if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then deploy_mutex_release; fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TR_DEPLOY_MUTEX_CLOUD=aws
deploy_mutex_acquire
cloud_bake_gate aws
python3 "${SCRIPT_DIR}/cloud_serving_release.py" aws >/dev/null

SOURCE_REPO=us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router
SOURCE_DIGEST="$(gcloud artifacts docker images describe "${SOURCE_REPO}:${IMAGE_TAG}" \
  --format='value(image_summary.digest)')"
[[ "$SOURCE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid source image digest"
SOURCE_IMAGE="${SOURCE_REPO}@${SOURCE_DIGEST}"
gcloud auth print-access-token | docker login us-central1-docker.pkg.dev \
  --username oauth2accesstoken --password-stdin >/dev/null
docker pull --platform linux/amd64 "$SOURCE_IMAGE" >/dev/null

REGIONS=(eu-west-1 eu-west-3)
SERVICES=(tr-cp-euw1 tr-cp-euw3)
for region in "${REGIONS[@]}"; do
  registry="330422590279.dkr.ecr.${region}.amazonaws.com"
  aws ecr get-login-password --region "$region" \
    | docker login "$registry" --username AWS --password-stdin >/dev/null
  docker tag "$SOURCE_IMAGE" "${registry}/trusted-router:${RELEASE}"
  docker push "${registry}/trusted-router:${RELEASE}" >/dev/null
  mirrored="$(aws ecr describe-images --region "$region" --repository-name trusted-router \
    --image-ids "imageTag=${RELEASE}" --query 'imageDetails[0].imageDigest' --output text)"
  [ "$mirrored" = "$SOURCE_DIGEST" ] || die "image digest changed while mirroring to ${region}"
done

for index in "${!REGIONS[@]}"; do
  region="${REGIONS[$index]}"
  service="${SERVICES[$index]}"
  # Verify live tasks/targets immediately before touching this region.
  previous_release="$(python3 "${SCRIPT_DIR}/cloud_serving_release.py" aws-region "$region" "$service")"
  aws ecs describe-services --cluster tr-cp --services "$service" --region "$region" \
    --output json > "${WORK}/service.json"
  python3 - "${WORK}/service.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))["services"][0]
c = s.get("deploymentConfiguration") or {}
# ECS may add sibling fields to deploymentCircuitBreaker as the API evolves.
# Check each required protection so extra fields do not reject safe rollouts.
breaker = c.get("deploymentCircuitBreaker") or {}
checks = [
    ("deploymentController.type", (s.get("deploymentController") or {}).get("type") == "ECS"),
    ("strategy", c.get("strategy", "ROLLING") == "ROLLING"),
    ("minimumHealthyPercent", c.get("minimumHealthyPercent") == 100),
    ("maximumPercent", (c.get("maximumPercent") or 0) >= 200),
    ("deploymentCircuitBreaker.enable", breaker.get("enable") is True),
    ("deploymentCircuitBreaker.rollback", breaker.get("rollback") is True),
]
failed = [name for name, passed in checks if not passed]
if failed:
    raise SystemExit("refusing rollout without healthy-capacity and automatic rollback protections: "
                     + "; ".join(failed) + " (observed " + json.dumps(c, sort_keys=True)
                     + ", controller " + json.dumps(s.get("deploymentController"), sort_keys=True) + ")")
PY
  previous="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"][0]["taskDefinition"])' "${WORK}/service.json")"
  aws ecs describe-task-definition --task-definition "$previous" --include TAGS \
    --region "$region" --output json > "${WORK}/previous.json"
  image="330422590279.dkr.ecr.${region}.amazonaws.com/trusted-router@${SOURCE_DIGEST}"
  python3 "${SCRIPT_DIR}/prepare_ecs_release.py" "${WORK}/previous.json" "$image" \
    "$RELEASE" "${WORK}/next.json" "$TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"
  next="$(aws ecs register-task-definition --cli-input-json "file://${WORK}/next.json" \
    --region "$region" --query taskDefinition.taskDefinitionArn --output text)"
  [[ "$next" = arn:aws:ecs:* ]] || die "no registered task definition"
  ACTIVE_REGION="$region"
  ACTIVE_SERVICE="$service"
  PREVIOUS_DEFINITION="$previous"
  PREVIOUS_RELEASE="$previous_release"
  log "updating ${region} to ${RELEASE}; previous=${previous}"
  aws ecs update-service --cluster tr-cp --service "$service" --task-definition "$next" \
    --region "$region" --query service.taskDefinition --output text >/dev/null
  aws ecs wait services-stable --cluster tr-cp --services "$service" --region "$region"
  actual="$(python3 "${SCRIPT_DIR}/cloud_serving_release.py" aws-region "$region" "$service")"
  [ "$actual" = "$RELEASE" ] || die "${region} did not serve the requested release"
  ACTIVE_REGION=""
  log "verified ${region}: healthy targets and exact image/release"
done
[ "$(python3 "${SCRIPT_DIR}/cloud_serving_release.py" aws)" = "$RELEASE" ] \
  || die "final fleet verification failed"
require_cloud_complete aws
