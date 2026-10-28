#!/usr/bin/env bash
# Dedicated worker only. Invoke within the reviewed GCP release coordinator,
# after the router image has passed CI/attestation/rollout gates and is serving.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_deploy_hold.sh"
REGION=us-central1
WORKER_SERVICE=trusted-router-settle-drain
if deploy_region_is_held "$REGION"; then
  echo 'settle worker region is held' >&2
  exit 1
fi
# Never acquire an independent release scope or bypass the parent's gates.
: "${TR_DEPLOY_MUTEX_OPERATION:?run inside the reviewed GCP release coordinator}"
[ "${TR_DEPLOY_MUTEX_CLOUD:-}" = gcp ]
source "${SCRIPT_DIR}/_lib.sh"
source "${SCRIPT_DIR}/deploy_mutex.sh"
source "${SCRIPT_DIR}/cloud_bake_gate.sh"
deploy_mutex_assert
cloud_bake_gate gcp
SERVICE=trusted-router
ROUTER_REVISION="$(active_revision_json "$REGION" false)"
PLAN="$(python3 "${SCRIPT_DIR}/settle_drain_config.py" <<<"$ROUTER_REVISION")"
plan_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<<"$PLAN"
}
IMAGE="$(plan_field image)"
RUNTIME_SA="$(plan_field service_account)"
SOURCE_REVISION="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["env"]["TR_SOURCE_REVISION"])' <<<"$PLAN")"
CI_RUNS="$(gh run list --repo Lore-Hex/quill-router --workflow ci.yml \
  --commit "$SOURCE_REVISION" --limit 1 --json headSha,status,conclusion)"
python3 -c '
import json,sys
runs=json.load(sys.stdin)
assert len(runs)==1 and runs[0]["headSha"]==sys.argv[1]
assert runs[0]["status"]=="completed" and runs[0]["conclusion"]=="success"
' "$SOURCE_REVISION" <<<"$CI_RUNS"
# Revision overlap is safe under the existing row/reservation and publisher
# fences. Do not require the serving worker to be disabled before replacement.
SET_ENV_VARS="$(python3 -c 'import json,sys; print("^|^"+"|".join(k+"="+v for k,v in json.load(sys.stdin)["env"].items()))' <<<"$PLAN")"
SET_SECRETS="$(python3 -c 'import json,sys; s=json.load(sys.stdin)["secrets"]["TR_SENTRY_DSN"]; print("TR_SENTRY_DSN="+s["name"]+":"+s["key"])' <<<"$PLAN")"
deploy_mutex_assert
# Only Sentry paging; no network reader credentials, LB routes, scheduler edits,
# router env writes or unauthenticated invoker grants.
gc run deploy "$WORKER_SERVICE" --region "$REGION" --image "$IMAGE" \
  --service-account "$RUNTIME_SA" --port 8080 --cpu 1 --memory 2Gi \
  --min 1 --max 1 --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --concurrency 1 --timeout 10 --ingress internal --no-allow-unauthenticated \
  --network "${TR_CLOUD_RUN_NETWORK:-default}" \
  --subnet "${TR_CLOUD_RUN_SUBNET:-default}" \
  --vpc-egress private-ranges-only \
  --set-secrets "$SET_SECRETS" --set-env-vars "$SET_ENV_VARS" \
  --startup-probe 'httpGet.path=/health,httpGet.port=8080,periodSeconds=5,timeoutSeconds=2,failureThreshold=12' \
  --liveness-probe 'httpGet.path=/health,httpGet.port=8080,periodSeconds=5,timeoutSeconds=2,failureThreshold=3' \
  --quiet
SERVICE="$WORKER_SERVICE"
ACTUAL="$(active_revision_json "$REGION" false)"
python3 -c '
import json,sys
actual=json.loads(sys.argv[1]); plan=json.load(sys.stdin)
spec=actual["spec"]; [container]=spec["containers"]
assert container["image"]==plan["image"] and spec["serviceAccountName"]==plan["service_account"]
assert {e["name"]: e["value"] for e in container["env"] if "value" in e}==plan["env"]
assert {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e}==plan["secrets"]
assert len(container["env"])==len(plan["env"])+len(plan["secrets"])
assert not spec.get("volumes")
' "$ACTUAL" <<<"$PLAN"
POLICY="$(gc run services get-iam-policy "$WORKER_SERVICE" --region "$REGION" --format=json)"
python3 -c '
import json,sys
assert not any(m in {"allUsers","allAuthenticatedUsers"}
 for b in json.load(sys.stdin).get("bindings",[]) for m in b.get("members",[]))
' <<<"$POLICY"
echo 'Worker configured. Acceptance still requires instance count, fleet-v1 freshness, idle Spanner rate/alerts, row latency, and a real fastpath-envelope retry reaching its private front door.'
