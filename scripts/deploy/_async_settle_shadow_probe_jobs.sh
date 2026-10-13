# shellcheck shell=bash
# No shared infrastructure writes. Only dedicated jobs and their schedules.
async_settle_shadow_probe_jobs() {
  if ! gc secrets describe trustedrouter-async-settle-shadow-probe-api-key >/dev/null 2>&1; then
    echo "ERROR: dedicated shadow probe secret is required" >&2
    return 1
  fi
  gc artifacts docker images describe "$IMAGE" >/dev/null
  local cadence="${TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES:-4}"
  local repetitions="${TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS:-1}"
  case "$cadence" in 1|2|3|4|5|6|10|12|15|20|30) ;; *) return 2 ;; esac
  case "$repetitions" in [1-8]) ;; *) return 2 ;; esac
  local region job network_json network_flags flag verb run_uri
  local -a regions network_args
  IFS=',' read -ra regions <<< "${TR_SYNTHETIC_MONITOR_REGIONS:-us-central1,europe-west4}"
  for region in "${regions[@]}"; do
    case "$region" in ''|*[!a-z0-9-]*) return 2 ;; esac
    job="trusted-router-async-settle-shadow-probe-${region}"
    # Existing monitor is the regional egress authority. Describe only; absent
    # or malformed prerequisites stop before any mutation in this region.
    network_json="$(gc run jobs describe "trusted-router-synthetic-${region}" --region "$region" --format=json)" || return 1
    network_flags="$(printf '%s' "$network_json" | python3 -c '
import json, sys
annotations = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["metadata"].get("annotations", {})
interfaces = json.loads(annotations.get("run.googleapis.com/network-interfaces", "[]"))
connector = annotations.get("run.googleapis.com/vpc-access-connector")
if interfaces and connector: raise SystemExit("ambiguous monitor network")
if interfaces:
    if len(interfaces) != 1: raise SystemExit("ambiguous monitor network")
    for field, flag in (("network", "--network"), ("subnetwork", "--subnet")):
        value = interfaces[0][field]
        if not isinstance(value, str) or not value or any(c.isspace() for c in value): raise SystemExit("invalid monitor network")
        print(flag + "=" + value)
    tags = interfaces[0].get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag or any(c.isspace() for c in tag) for tag in tags): raise SystemExit("invalid monitor network tags")
    print("--network-tags=" + ",".join(tags) if tags else "--clear-network-tags")
elif connector:
    print("--vpc-connector=" + connector)
else:
    print("--clear-network")
if interfaces or connector:
    egress = annotations.get("run.googleapis.com/vpc-access-egress")
    if egress not in ("all-traffic", "private-ranges-only"): raise SystemExit("missing monitor egress")
    print("--vpc-egress=" + egress)
')" || return 1
    network_args=()
    while IFS= read -r flag; do network_args+=("$flag"); done <<< "$network_flags"
    gc run jobs deploy "$job" --region "$region" --image "$IMAGE" \
      --command=/app/.venv/bin/python \
      --args=-m,trusted_router.synthetic.cli,--async-settle-shadow-probe \
      --service-account "$RUN_SERVICE_ACCOUNT" "${network_args[@]}" \
      --set-env-vars "^|^TR_ENVIRONMENT=local|TR_SERVICE_SURFACE=internal|TR_STORAGE_BACKEND=memory|TR_ENABLE_LIVE_PROVIDERS=false|TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE=/var/run/secrets/shadow-probe/api-key|TR_SYNTHETIC_MONITOR_REGION=${region}|TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS=${repetitions}|TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES=${cadence}" \
      --set-secrets /var/run/secrets/shadow-probe/api-key=trustedrouter-async-settle-shadow-probe-api-key:latest \
      --tasks 1 --parallelism 1 --max-retries 0 --task-timeout "$((240 * repetitions + 60))s" --cpu 1 --memory 512Mi --quiet >/dev/null
    verb=create
    if gc scheduler jobs describe "${job}-scheduled" --location "$region" >/dev/null 2>&1; then verb=update; fi
    run_uri="https://${region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job}:run"
    gc scheduler jobs "$verb" http "${job}-scheduled" --location "$region" \
      --schedule "*/${cadence} * * * *" --uri "$run_uri" --http-method POST \
      --oauth-service-account-email "$RUN_SERVICE_ACCOUNT" --max-retry-attempts 0 --max-retry-duration 0s --quiet >/dev/null
  done
}
