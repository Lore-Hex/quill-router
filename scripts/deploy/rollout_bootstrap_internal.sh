#!/usr/bin/env bash
# Forward-only prerequisite for the first six-surface split.
#
# Bootstrap mode creates and promotes only the canonical private internal
# service. Verification mode is read-only and is called by rollout.sh before
# an initial split may reconcile a backend or deploy a revision.

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/deploy/rollout_bootstrap_internal.sh --artifact PATH
  bash scripts/deploy/rollout_bootstrap_internal.sh --verify-artifact PATH --expected-image IMAGE@sha256:DIGEST
EOF
  exit 2
}

MODE=""
ARTIFACT=""
EXPECTED_IMAGE=""
case "$#:${1:-}" in
  2:--artifact)
    MODE=bootstrap
    ARTIFACT="$2"
    ;;
  4:--verify-artifact)
    [ "$3" = --expected-image ] || usage
    MODE=verify
    ARTIFACT="$2"
    EXPECTED_IMAGE="$4"
    ;;
  *) usage ;;
esac
[ -n "$ARTIFACT" ] || usage

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
STATE_TOOL="${SCRIPT_DIR}/rollout_state.py"

if [ "$MODE" = bootstrap ]; then
  BOOTSTRAP_OPERATION_ID="${TR_ROLLOUT_OPERATION_ID:-}"
  if ! [[ "$BOOTSTRAP_OPERATION_ID" =~ ^[A-Za-z0-9._:-]{8,160}$ ]]; then
    echo "ERROR: internal bootstrap requires TR_ROLLOUT_OPERATION_ID" >&2
    exit 2
  fi
  BOOTSTRAP_OPERATION_LOCK="${TR_ROLLOUT_LOCAL_LOCK_PATH:-${TMPDIR:-/tmp}/trusted-router-${PROJECT_ID}.stage.lock}"
  if [ "${TR_ROLLOUT_LOCAL_LOCK_HELD:-}" != "$BOOTSTRAP_OPERATION_LOCK" ]; then
    export TR_ROLLOUT_LOCAL_LOCK_HELD="$BOOTSTRAP_OPERATION_LOCK"
    exec python3 "${SCRIPT_DIR}/rollout_local_lock.py" \
      "$BOOTSTRAP_OPERATION_LOCK" -- /bin/bash "$0" "$@"
  fi
fi

for command_name in gcloud jq python3; do
  command -v "$command_name" >/dev/null || {
    echo "ERROR: required command is missing: ${command_name}" >&2
    exit 1
  }
done

[ "$INTERNAL_SERVICE" = trusted-router-billing ] || {
  echo "ERROR: internal bootstrap requires trusted-router-billing" >&2
  exit 1
}
BOOTSTRAP_LEGACY_CONSOLE_SERVICE="${LEGACY_CONSOLE_SERVICE:-${TR_LEGACY_CONSOLE_SERVICE:-$SERVICE}}"
[ "$BOOTSTRAP_LEGACY_CONSOLE_SERVICE" = trusted-router ] || {
  echo "ERROR: internal bootstrap requires the canonical legacy monolith" >&2
  exit 1
}
[ "$INTERNAL_RUN_SERVICE_ACCOUNT" = "tr-internal@${PROJECT_ID}.iam.gserviceaccount.com" ] || {
  echo "ERROR: internal bootstrap requires the canonical dedicated identity" >&2
  exit 1
}
SYNTHETIC_RUN_SERVICE_ACCOUNT="${TR_SYNTHETIC_RUN_SERVICE_ACCOUNT:-tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com}"
[ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" = "tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com" ] || {
  echo "ERROR: synthetic cutover requires the canonical dedicated identity" >&2
  exit 1
}
CLOUD_RUN_NETWORK="${TR_CLOUD_RUN_NETWORK:-default}"
CLOUD_RUN_SUBNET="${TR_CLOUD_RUN_SUBNET:-default}"
[ "$CLOUD_RUN_NETWORK" = default ] && [ "$CLOUD_RUN_SUBNET" = default ] || {
  echo "ERROR: internal bootstrap pins the reviewed default VPC network/subnet" >&2
  exit 1
}
SYNTHETIC_NETWORK="${TR_SYNTHETIC_NETWORK:-default}"
SYNTHETIC_SUBNET="${TR_SYNTHETIC_SUBNET:-default}"
for resource_name in "$SYNTHETIC_NETWORK" "$SYNTHETIC_SUBNET"; do
  [[ "$resource_name" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || {
    echo "ERROR: synthetic VPC network/subnet name is invalid" >&2
    exit 1
  }
done

validate_region() {
  local label="$1" value="$2"
  [[ "$value" =~ ^[a-z]+-[a-z0-9]+[0-9]$ ]] || {
    echo "ERROR: ${label} contains invalid region ${value:-<empty>}" >&2
    return 1
  }
}

BOOTSTRAP_REGIONS=()
add_bootstrap_region() {
  local region="$1" existing
  validate_region "bootstrap inventory" "$region"
  for existing in "${BOOTSTRAP_REGIONS[@]-}"; do
    [ "$existing" = "$region" ] && return 0
  done
  BOOTSTRAP_REGIONS+=("$region")
}

IFS=',' read -r -a CONTROL_REGIONS <<<"$TR_CONTROL_PLANE_REGIONS"
[ "${#CONTROL_REGIONS[@]}" -gt 0 ] || {
  echo "ERROR: control-plane region inventory is empty" >&2
  exit 1
}
for region in "${CONTROL_REGIONS[@]}"; do add_bootstrap_region "$region"; done
IFS=',' read -r -a MONITOR_REGIONS <<<"$TR_SYNTHETIC_MONITOR_REGIONS"
[ "${#MONITOR_REGIONS[@]}" -gt 0 ] || {
  echo "ERROR: synthetic monitor region inventory is empty" >&2
  exit 1
}
for region in "${MONITOR_REGIONS[@]}"; do add_bootstrap_region "$region"; done
for region in \
  "$TR_SYNTHETIC_THROUGHPUT_REGION" \
  "$TR_SYNTHETIC_IMAGE_REGION" \
  "$TR_SYNTHETIC_VIDEO_REGION"; do
  add_bootstrap_region "$region"
done
BOOTSTRAP_REGION_CSV="$(IFS=,; echo "${BOOTSTRAP_REGIONS[*]}")"
MONITOR_REGION_CSV="$(IFS=,; echo "${MONITOR_REGIONS[*]}")"

if [ "$MODE" = verify ]; then
  [ -d "$(dirname "$ARTIFACT")" ] || {
    echo "ERROR: bootstrap artifact directory is absent" >&2
    exit 1
  }
  ARTIFACT_DIR="$(cd "$(dirname "$ARTIFACT")" && pwd)"
else
  ARTIFACT_DIR="$(mkdir -p "$(dirname "$ARTIFACT")" && cd "$(dirname "$ARTIFACT")" && pwd)"
fi
ARTIFACT="${ARTIFACT_DIR}/$(basename "$ARTIFACT")"
JOURNAL="${ARTIFACT}.state"

read_journal_revision_suffix() {
  python3 - "$JOURNAL" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit("bootstrap state must be a regular non-symlink file")
if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
    raise SystemExit("bootstrap state must have mode 0600")
value = json.loads(path.read_text(encoding="utf-8"))
suffix = value.get("revision_suffix") if isinstance(value, dict) else None
if not isinstance(suffix, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,34}[a-z0-9]", suffix):
    raise SystemExit("bootstrap state revision suffix is invalid")
print(suffix)
PY
}

synthetic_inventory_lines() {
  local region
  for region in "${MONITOR_REGIONS[@]}"; do
    printf '%s\t%s\t%s\n' \
      "$region" \
      "trusted-router-synthetic-${region}" \
      "trusted-router-synthetic-${region}-every-three-minutes"
  done
  printf '%s\t%s\t%s\n' \
    "$TR_SYNTHETIC_THROUGHPUT_REGION" \
    "trusted-router-throughput-${TR_SYNTHETIC_THROUGHPUT_REGION}" \
    "trusted-router-throughput-${TR_SYNTHETIC_THROUGHPUT_REGION}-every-five-minutes"
  printf '%s\t%s\t%s\n' \
    "$TR_SYNTHETIC_IMAGE_REGION" \
    "trusted-router-image-generation-${TR_SYNTHETIC_IMAGE_REGION}" \
    "trusted-router-image-generation-${TR_SYNTHETIC_IMAGE_REGION}-every-six-hours"
  printf '%s\t%s\t%s\n' \
    "$TR_SYNTHETIC_VIDEO_REGION" \
    "trusted-router-video-generation-${TR_SYNTHETIC_VIDEO_REGION}" \
    "trusted-router-video-generation-${TR_SYNTHETIC_VIDEO_REGION}-daily"
}

validate_artifact_and_emit_services() {
  local expected_image="$1"
  python3 - "$ARTIFACT" "$PROJECT_ID" "$expected_image" \
    "$BOOTSTRAP_REGION_CSV" "$MONITOR_REGION_CSV" \
    "$TR_SYNTHETIC_THROUGHPUT_REGION" "$TR_SYNTHETIC_IMAGE_REGION" \
    "$TR_SYNTHETIC_VIDEO_REGION" <<'PY'
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

(
    raw_path,
    project,
    image,
    raw_regions,
    raw_monitor_regions,
    throughput_region,
    image_region,
    video_region,
) = sys.argv[1:]
path = Path(raw_path)
if path.is_symlink() or not path.is_file():
    raise SystemExit("bootstrap artifact must be a regular non-symlink file")
if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
    raise SystemExit("bootstrap artifact must have mode 0600")
value = json.loads(path.read_text(encoding="utf-8"))
fields = {
    "schema_version", "kind", "project_id", "image", "release",
    "created_at", "regions", "internal_service", "runtime_service_account",
    "synthetic_service_account", "ingress", "default_url_enabled",
    "synthetic_inventory", "data_mode", "services",
}
if not isinstance(value, dict) or set(value) != fields:
    raise SystemExit("bootstrap artifact fields differ from schema v1")
if value["schema_version"] != 1 or value["kind"] != "trusted-router-internal-bootstrap":
    raise SystemExit("bootstrap artifact schema/kind is unsupported")
if value["project_id"] != project or value["image"] != image:
    raise SystemExit("bootstrap artifact is not bound to this project/image")
if not re.fullmatch(r"[^\s,|@]+@sha256:[0-9a-f]{64}", value["image"]):
    raise SystemExit("bootstrap image is not immutable")
if value["internal_service"] != "trusted-router-billing":
    raise SystemExit("bootstrap artifact names the wrong internal service")
if value["runtime_service_account"] != f"tr-internal@{project}.iam.gserviceaccount.com":
    raise SystemExit("bootstrap artifact names the wrong runtime identity")
if value["synthetic_service_account"] != f"tr-synthetic@{project}.iam.gserviceaccount.com":
    raise SystemExit("bootstrap artifact names the wrong synthetic identity")
if value["ingress"] != "internal-and-cloud-load-balancing" or value["default_url_enabled"] is not True:
    raise SystemExit("bootstrap artifact private-origin contract differs")
if not isinstance(value["release"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value["release"]):
    raise SystemExit("bootstrap release is invalid")
if not isinstance(value["created_at"], str) or not re.fullmatch(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["created_at"]
):
    raise SystemExit("bootstrap timestamp is invalid")
regions = raw_regions.split(",")
if value["regions"] != regions:
    raise SystemExit("bootstrap artifact region inventory differs")
expected_synthetic = {
    "monitor_regions": raw_monitor_regions.split(","),
    "throughput_region": throughput_region,
    "image_region": image_region,
    "video_region": video_region,
}
if value["synthetic_inventory"] != expected_synthetic:
    raise SystemExit("bootstrap artifact synthetic inventory differs")
data_mode = value["data_mode"]
data_mode_fields = {
    "storage_backend", "analytics_read_mode", "request_record_write_mode",
    "generation_records_enabled", "bigtable_mirror_writes_enabled",
    "analytics_dual_read_started_at", "analytics_clickhouse_primary_started_at",
    "operational_clickhouse_url", "operational_clickhouse_user",
    "operational_clickhouse_database",
}
if not isinstance(data_mode, dict) or set(data_mode) != data_mode_fields:
    raise SystemExit("bootstrap artifact data mode fields differ")
mode = (
    data_mode["storage_backend"],
    data_mode["analytics_read_mode"],
    data_mode["request_record_write_mode"],
    data_mode["generation_records_enabled"],
    data_mode["bigtable_mirror_writes_enabled"],
)
if mode == ("spanner-bigtable", "bigtable", "typed", "true", "true"):
    if any(data_mode[name] for name in (
        "operational_clickhouse_url", "operational_clickhouse_user",
        "operational_clickhouse_database",
    )):
        raise SystemExit("bootstrap artifact Bigtable mode has ClickHouse fields")
elif mode == ("spanner-clickhouse", "clickhouse-only", "typed", "true", "false"):
    if data_mode["operational_clickhouse_user"] != "tr_control_read" or data_mode["operational_clickhouse_database"] != "tr":
        raise SystemExit("bootstrap artifact ClickHouse identity/database differs")
    parsed = urlsplit(data_mode["operational_clickhouse_url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("bootstrap artifact ClickHouse URL is malformed")
    try:
        private = ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        private = parsed.hostname.endswith(".internal")
    if not private:
        raise SystemExit("bootstrap artifact ClickHouse URL is not private")
else:
    raise SystemExit("bootstrap artifact data mode is not reviewed")
for field in ("analytics_dual_read_started_at", "analytics_clickhouse_primary_started_at"):
    timestamp = data_mode[field]
    if not isinstance(timestamp, str) or (timestamp and not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp
    )):
        raise SystemExit(f"bootstrap artifact {field} is invalid")
services = value["services"]
if not isinstance(services, list) or len(services) != len(regions):
    raise SystemExit("bootstrap artifact service inventory differs")
seen = set()
for service in services:
    if not isinstance(service, dict) or set(service) != {
        "region", "revision", "postcondition_sha256"
    }:
        raise SystemExit("bootstrap service fields differ")
    region = service["region"]
    revision = service["revision"]
    digest = service["postcondition_sha256"]
    if region not in regions or region in seen:
        raise SystemExit("bootstrap service region is missing or duplicated")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}", revision):
        raise SystemExit("bootstrap revision is invalid")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("bootstrap service digest is invalid")
    seen.add(region)
    print(region, revision, digest, sep="\t")
if seen != set(regions):
    raise SystemExit("bootstrap service inventory is incomplete")
PY
}

verify_live_internal_service() {
  local region="$1" revision="$2" expected_hash="$3"
  local service_json service_path service_secret_file service_iam actual_hash
  local env_name secret version version_json enabled_version
  service_json="$(gc run services describe "$INTERNAL_SERVICE" \
    --region="$region" --format=json)" || {
      echo "ERROR: bootstrapped internal service is absent in ${region}" >&2
      return 1
    }
  service_iam="$(gc run services get-iam-policy "$INTERNAL_SERVICE" \
    --region="$region" --format=json)" || return 1
  jq -e '
    [.bindings[]? | select(any(.members[]?; . == "allUsers"))
      | {role, condition: (.condition // null),
         allUsersCount: ([.members[]? | select(. == "allUsers")] | length)}]
    == [{role:"roles/run.invoker",condition:null,allUsersCount:1}]
  ' <<<"$service_iam" >/dev/null || {
    echo "ERROR: internal bootstrap invoker IAM drifted in ${region}" >&2
    return 1
  }
  service_path="$(mktemp "${TMPDIR:-/tmp}/tr-internal-live-XXXXXX")"
  service_secret_file="$(mktemp "${TMPDIR:-/tmp}/tr-internal-live-secrets-XXXXXX")"
  printf '%s' "$service_json" >"$service_path"
  if ! python3 - "$service_path" "$INTERNAL_SERVICE" "$revision" \
    "$INTERNAL_RUN_SERVICE_ACCOUNT" "$EXPECTED_IMAGE" \
    "https://${INTERNAL_SERVICE}-${PROJECT_NUMBER}.${region}.run.app" \
    "$service_secret_file" "$ARTIFACT_RELEASE" "$PROJECT_ID" "$TR_REGIONS" \
    "$TR_PRIMARY_REGION" "$SPANNER_INSTANCE_ID" "$SPANNER_DATABASE_ID" \
    "$BIGTABLE_INSTANCE_ID" "$BIGTABLE_GENERATION_TABLE" \
    "$BYOK_KMS_KEY_NAME" "$CLOUD_RUN_NETWORK" "$CLOUD_RUN_SUBNET" \
    "$ARTIFACT_STORAGE_BACKEND" "$ARTIFACT_ANALYTICS_READ_MODE" \
    "$ARTIFACT_REQUEST_RECORD_WRITE_MODE" \
    "$ARTIFACT_GENERATION_RECORDS_ENABLED" \
    "$ARTIFACT_BIGTABLE_MIRROR_WRITES_ENABLED" \
    "$ARTIFACT_ANALYTICS_DUAL_READ_STARTED_AT" \
    "$ARTIFACT_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" \
    "$ARTIFACT_OPERATIONAL_CLICKHOUSE_URL" \
    "$ARTIFACT_OPERATIONAL_CLICKHOUSE_USER" \
    "$ARTIFACT_OPERATIONAL_CLICKHOUSE_DATABASE" <<'PY'
import json
import sys
from pathlib import Path

(
    path,
    expected_service,
    expected_revision,
    expected_identity,
    expected_image,
    expected_url,
    secret_output,
    expected_release,
    project,
    regions,
    primary_region,
    spanner_instance,
    spanner_database,
    bigtable_instance,
    generation_table,
    byok_key,
    expected_network,
    expected_subnet,
    storage_backend,
    analytics_read_mode,
    request_record_write_mode,
    generation_records_enabled,
    bigtable_mirror_writes_enabled,
    analytics_dual_read_started_at,
    analytics_clickhouse_primary_started_at,
    operational_clickhouse_url,
    operational_clickhouse_user,
    operational_clickhouse_database,
) = sys.argv[1:]
data = json.loads(Path(path).read_text(encoding="utf-8"))
metadata = data.get("metadata") or {}
annotations = metadata.get("annotations") or {}
spec = data.get("spec") or {}
template = spec.get("template") or {}
template_spec = template.get("spec") or {}
status = data.get("status") or {}
if metadata.get("name") not in {None, expected_service}:
    raise SystemExit("internal service name differs")
if not any(
    item.get("type") == "Ready" and str(item.get("status", "")).lower() == "true"
    for item in status.get("conditions", [])
):
    raise SystemExit("internal service is not Ready")
if metadata.get("generation") is None or str(status.get("observedGeneration")) != str(metadata["generation"]):
    raise SystemExit("internal service generation is not observed")
if status.get("latestReadyRevisionName") != expected_revision:
    raise SystemExit("bootstrapped revision is no longer latest Ready")
def exact_sole_target(items, revision):
    return (
        len(items) == 1
        and items[0].get("revisionName") == revision
        and int(items[0].get("percent", 0) or 0) == 100
        and not items[0].get("tag")
        and not items[0].get("latestRevision", False)
    )

if not exact_sole_target(spec.get("traffic") or [], expected_revision):
    raise SystemExit("bootstrapped revision is not the sole desired traffic target")
if not exact_sole_target(status.get("traffic") or [], expected_revision):
    raise SystemExit("bootstrapped revision is not the sole 100% target")
if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
    raise SystemExit("internal ingress differs")
if annotations.get("run.googleapis.com/ingress-status") != "internal-and-cloud-load-balancing":
    raise SystemExit("effective internal ingress differs")
if str(annotations.get("run.googleapis.com/default-url-disabled", "false")).lower() not in {"", "false"}:
    raise SystemExit("internal default URL is disabled")
if status.get("url") != expected_url:
    raise SystemExit("internal private default URL differs")
if template_spec.get("serviceAccountName") != expected_identity:
    raise SystemExit("internal runtime identity differs")
if int(template_spec.get("containerConcurrency", -1)) != 8:
    raise SystemExit("internal concurrency differs")
if str(template_spec.get("timeoutSeconds", "")).removesuffix("s") != "300":
    raise SystemExit("internal timeout differs")
template_annotations = (template.get("metadata") or {}).get("annotations") or {}
if str(template_annotations.get("autoscaling.knative.dev/minScale")) != "2":
    raise SystemExit("internal minimum differs")
if str(template_annotations.get("autoscaling.knative.dev/maxScale")) != "50":
    raise SystemExit("internal revision maximum differs")
if str((spec.get("scaling") or {}).get("maxInstanceCount")) != "50":
    raise SystemExit("internal service maximum differs")
containers = template_spec.get("containers") or []
if len(containers) != 1:
    raise SystemExit("internal revision must have exactly one container")
container = containers[0]
if container.get("image") != expected_image:
    raise SystemExit("internal bootstrap image differs")
if container.get("command") not in (None, []):
    raise SystemExit("internal container command differs")
if container.get("args") not in (None, []):
    raise SystemExit("internal container arguments differ")
if container.get("volumeMounts") not in (None, []):
    raise SystemExit("internal container volume mounts differ")
if template_spec.get("volumes") not in (None, []):
    raise SystemExit("internal volumes differ")
if template_spec.get("initContainers") not in (None, []):
    raise SystemExit("internal init containers differ")
ports = container.get("ports") or []
if len(ports) != 1 or int(ports[0].get("containerPort", -1)) != 8080:
    raise SystemExit("internal container port differs")
limits = (container.get("resources") or {}).get("limits") or {}
if str(limits.get("memory")) != "2Gi":
    raise SystemExit("internal memory differs")
if str(limits.get("cpu") or "") not in {"1", "1000m"}:
    raise SystemExit("internal CPU differs")
network_raw = template_annotations.get("run.googleapis.com/network-interfaces")
try:
    interfaces = json.loads(network_raw)
except (TypeError, ValueError):
    raise SystemExit("internal VPC network annotation is invalid") from None
if not isinstance(interfaces, list) or len(interfaces) != 1:
    raise SystemExit("internal VPC interface inventory differs")

def exact_resource(value, kind, expected):
    text = str(value or "").rstrip("/")
    return text == expected or (
        f"/projects/{project}/" in f"/{text}"
        and text.endswith(f"/{kind}/{expected}")
    )

if not exact_resource(interfaces[0].get("network"), "networks", expected_network):
    raise SystemExit("internal VPC network differs")
if not exact_resource(interfaces[0].get("subnetwork"), "subnetworks", expected_subnet):
    raise SystemExit("internal VPC subnet differs")
if template_annotations.get("run.googleapis.com/vpc-access-egress") != "private-ranges-only":
    raise SystemExit("internal VPC egress differs")
probe = container.get("startupProbe") or {}
http_get = probe.get("httpGet") or {}
expected_probe = {
    "initialDelaySeconds": 0,
    "timeoutSeconds": 10,
    "periodSeconds": 10,
    "failureThreshold": 18,
}
if http_get.get("path") != "/ready":
    raise SystemExit("internal /ready startup probe is absent")
if http_get.get("port") is not None and int(http_get["port"]) != 8080:
    raise SystemExit("internal startup probe port differs")
for field, expected in expected_probe.items():
    if int(probe.get(field, -1)) != expected:
        raise SystemExit(f"internal startup probe {field} differs")
env_items = container.get("env", []) or []
env_names = [item.get("name") for item in env_items]
if any(not name for name in env_names) or len(env_names) != len(set(env_names)):
    raise SystemExit("internal environment contains missing or duplicate names")
env = {item["name"]: item for item in env_items}
expected_plain = {
    "TR_ENVIRONMENT": "production",
    "TR_RELEASE": expected_release,
    "TR_SERVICE_SURFACE": "internal",
    "TR_API_BASE_URL": "https://api.trustedrouter.com/v1",
    "TR_TRUSTED_DOMAIN": "trustedrouter.com",
    "TR_TRUSTED_DOMAIN_ALIASES": "allyrouter.com,uptimerouter.com",
    "TR_GCP_PROJECT_ID": project,
    "TR_REGIONS": regions,
    "TR_PRIMARY_REGION": primary_region,
    "TR_ENABLE_LIVE_PROVIDERS": "false",
    "TR_RATE_LIMIT_CLIENT_IP_MODE": "edge_header",
    "TR_MAX_REQUEST_BODY_BYTES": "33554432",
    "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES": "67108864",
    "TR_MAX_CONCURRENT_REQUEST_BODIES": "4",
    "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS": "30",
    "TR_REMEDIATOR_IN_PROCESS_ENABLED": "false",
    "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS": "0",
    "TR_ACTIVATION_REMINDER_INTERVAL_SECONDS": "0",
    "TR_REGIONAL_QUOTA_LEASES_ENABLED": "false",
    "TR_STORAGE_BACKEND": storage_backend,
    "TR_SPANNER_INSTANCE_ID": spanner_instance,
    "TR_SPANNER_DATABASE_ID": spanner_database,
    "TR_BIGTABLE_INSTANCE_ID": bigtable_instance,
    "TR_BIGTABLE_GENERATION_TABLE": generation_table,
    "TR_SPANNER_POOL_SIZE": "8",
    "TR_BIGTABLE_MIRROR_WRITES_ENABLED": bigtable_mirror_writes_enabled,
    "TR_GENERATION_RECORDS_ENABLED": generation_records_enabled,
    "TR_ANALYTICS_READ_MODE": analytics_read_mode,
    "TR_ANALYTICS_DUAL_READ_STARTED_AT": analytics_dual_read_started_at,
    "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT": analytics_clickhouse_primary_started_at,
    "TR_REQUEST_RECORD_WRITE_MODE": request_record_write_mode,
    "TR_SETTLE_OUTBOX_ENABLED": "true",
    "TR_ANALYTICS_OUTBOX_ENABLED": "true",
    "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "true",
    "TR_BYOK_KMS_KEY_NAME": byok_key,
    "TR_AWS_REGION": "us-east-1",
    "TR_SES_FROM_EMAIL": "alerts@alerts.trustedrouter.com",
    "TR_SES_FROM_NAME": "TrustedRouter Alerts",
    "TR_SES_ALERT_FROM_EMAIL": "alerts@alerts.trustedrouter.com",
    "TR_SES_ALERT_FROM_NAME": "TrustedRouter Alerts",
    "TR_SES_ALERT_CONFIGURATION_SET": "trustedrouter-alerts",
}
if analytics_read_mode != "bigtable":
    expected_plain.update({
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL": operational_clickhouse_url,
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": operational_clickhouse_user,
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": operational_clickhouse_database,
    })
actual_plain = {
    name: str(item.get("value", ""))
    for name, item in env.items()
    if "valueFrom" not in item
}
if actual_plain != expected_plain:
    raise SystemExit("internal exact plain environment differs")
expected_secrets = {
    "TR_SENTRY_DSN": "trustedrouter-sentry-dsn",
    "TR_INTERNAL_GATEWAY_TOKEN": "trustedrouter-internal-gateway-token",
    "TR_OBSERVER_INTERNAL_TOKEN": "trustedrouter-observer-internal-token",
    "TR_SYNTHETIC_MONITOR_API_KEY": "trustedrouter-synthetic-monitor-api-key",
    "TR_STRIPE_SECRET_KEY": "trustedrouter-internal-stripe-payment-intents-key",
    "TR_AWS_ACCESS_KEY_ID": "trustedrouter-internal-ses-access-key-id",
    "TR_AWS_SECRET_ACCESS_KEY": "trustedrouter-internal-ses-secret-access-key",
}
if analytics_read_mode != "bigtable":
    expected_secrets["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD"] = (
        "trustedrouter-clickhouse-control-read-password"
    )
actual_secrets = {}
actual_secret_versions = {}
for name, item in env.items():
    if "valueFrom" not in item:
        continue
    reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
    resource = str(reference.get("name") or reference.get("secret") or "").split("/")[-1]
    version = str(reference.get("key") or reference.get("version") or "")
    if not version.isdigit() or version.startswith("0"):
        raise SystemExit(f"internal secret {name} is not pinned numerically")
    actual_secrets[name] = resource
    actual_secret_versions[name] = version
if actual_secrets != expected_secrets:
    raise SystemExit("internal bootstrap secret allowlist differs")
Path(secret_output).write_text(
    "".join(
        f"{name}\t{actual_secrets[name]}\t{actual_secret_versions[name]}\n"
        for name in sorted(expected_secrets)
    ),
    encoding="utf-8",
)
PY
  then
    rm -f "$service_path" "$service_secret_file"
    return 1
  fi
  while IFS=$'\t' read -r env_name secret version; do
    [ -n "$env_name" ] || continue
    version_json="$(gc secrets versions describe "$version" \
      --secret="$secret" --format=json)" || {
        rm -f "$service_path" "$service_secret_file"
        return 1
      }
    enabled_version="$(jq -er 'select(.state == "ENABLED") | .name | split("/")[-1]' \
      <<<"$version_json")" || {
        echo "ERROR: bootstrapped internal secret ${secret}:${version} is not enabled" >&2
        rm -f "$service_path" "$service_secret_file"
        return 1
      }
    [ "$enabled_version" = "$version" ] || {
      echo "ERROR: bootstrapped internal secret version resolution differs for ${secret}" >&2
      rm -f "$service_path" "$service_secret_file"
      return 1
    }
  done <"$service_secret_file"
  actual_hash="$(python3 "$STATE_TOOL" hash-service "$service_path")"
  rm -f "$service_path" "$service_secret_file"
  [ "$actual_hash" = "$expected_hash" ] || {
    echo "ERROR: bootstrapped internal service hash drifted in ${region}" >&2
    return 1
  }
}

verify_synthetic_job_target() {
  local region="$1" job_name="$2" scheduler_name="$3"
  local expected_base expected_run_uri job_json job_path job_secret_file
  local scheduler_json scheduler_path env_name secret version version_json enabled_version
  local secret_policy job_iam
  expected_base="https://${INTERNAL_SERVICE}-${PROJECT_NUMBER}.${region}.run.app"
  expected_run_uri="https://${region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"
  job_json="$(gc run jobs describe "$job_name" --region="$region" --format=json)" || {
    echo "ERROR: synthetic job ${region}/${job_name} is absent" >&2
    return 1
  }
  job_iam="$(gc run jobs get-iam-policy "$job_name" \
    --region="$region" --format=json)" || return 1
  if ! printf '%s' "$job_iam" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic Job IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/run.invoker" or binding.get("condition") is not None:
    raise SystemExit("synthetic Job invoker binding differs")
members = binding.get("members") or []
if members != [sys.argv[1]]:
    raise SystemExit("synthetic Job invoker member inventory differs")
' "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"; then
    echo "ERROR: synthetic Cloud Run Job ${region}/${job_name} IAM differs" >&2
    return 1
  fi
  job_path="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-job-XXXXXX")"
  job_secret_file="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-secrets-XXXXXX")"
  printf '%s' "$job_json" >"$job_path"
  if ! python3 - "$job_path" "$job_name" "$region" \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" "$expected_base" \
    "$SYNTHETIC_NETWORK" "$SYNTHETIC_SUBNET" "$PROJECT_ID" \
    "$job_secret_file" "$EXPECTED_IMAGE" "$ARTIFACT_RELEASE" "$TR_REGIONS" \
    "$TR_PRIMARY_REGION" "$REGION" "$SPANNER_INSTANCE_ID" \
    "$SPANNER_DATABASE_ID" "$BIGTABLE_INSTANCE_ID" \
    "$BIGTABLE_GENERATION_TABLE" "$MONITOR_REGION_CSV" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

(
    path,
    job_name,
    region,
    expected_identity,
    expected_base,
    expected_network,
    expected_subnet,
    project,
    secret_output,
    expected_image,
    expected_release,
    gateway_regions,
    primary_region,
    vertex_location,
    spanner_instance,
    spanner_database,
    bigtable_instance,
    generation_table,
    raw_monitor_regions,
) = sys.argv[1:]
data = json.loads(Path(path).read_text(encoding="utf-8"))
containers = []
container_specs = []
identities = []
network_annotations = []
task_counts = []
parallelisms = []
def visit(value):
    if isinstance(value, dict):
        annotations = value.get("annotations")
        if isinstance(annotations, dict) and any(
            name in annotations
            for name in (
                "run.googleapis.com/network-interfaces",
                "run.googleapis.com/vpc-access-egress",
            )
        ):
            network_annotations.append(annotations)
        for key, child in value.items():
            if key in {"serviceAccount", "serviceAccountName"} and isinstance(child, str):
                identities.append(child)
            if key == "containers" and isinstance(child, list):
                containers.extend(child)
                container_specs.append(value)
            if key == "taskCount":
                task_counts.append(child)
            if key == "parallelism":
                parallelisms.append(child)
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)
visit(data.get("spec") or {})
reported_name = str((data.get("metadata") or {}).get("name") or "").rstrip("/").split("/")[-1]
if reported_name and reported_name != job_name:
    raise SystemExit("synthetic job name differs")
if sorted(set(identities)) != [expected_identity]:
    raise SystemExit("synthetic job identity differs")
if len(containers) != 1 or len(container_specs) != 1:
    raise SystemExit("synthetic job must expose exactly one container")
if task_counts != [1] or parallelisms != [1]:
    raise SystemExit("synthetic job task fan-out differs")
job_spec = container_specs[0]
container = containers[0]
if container.get("image") != expected_image:
    raise SystemExit("synthetic job image differs")
if container.get("command") != ["/app/.venv/bin/python"]:
    raise SystemExit("synthetic job command differs")
if container.get("volumeMounts") not in (None, []):
    raise SystemExit("synthetic job volume mounts differ")
if job_spec.get("volumes") not in (None, []):
    raise SystemExit("synthetic job volumes differ")
if job_spec.get("initContainers") not in (None, []):
    raise SystemExit("synthetic job init containers differ")
if int(job_spec.get("maxRetries", -1)) != 0:
    raise SystemExit("synthetic job retry contract differs")

if job_name == f"trusted-router-synthetic-{region}":
    family = "monitor"
    expected_module = "trusted_router.synthetic.cli"
    expected_cpu, expected_memory, expected_timeout = "2", "1Gi", "300"
elif job_name == f"trusted-router-throughput-{region}":
    family = "throughput"
    expected_module = "trusted_router.synthetic.cli"
    expected_cpu, expected_memory, expected_timeout = "1", "1Gi", "300"
elif job_name == f"trusted-router-image-generation-{region}":
    family = "image"
    expected_module = "trusted_router.synthetic.image_generation"
    expected_cpu, expected_memory, expected_timeout = "1", "512Mi", "300"
elif job_name == f"trusted-router-video-generation-{region}":
    family = "video"
    expected_module = "trusted_router.synthetic.video_generation"
    expected_cpu, expected_memory, expected_timeout = "1", "512Mi", "1200"
else:
    raise SystemExit("synthetic job family differs")
if container.get("args") != ["-m", expected_module]:
    raise SystemExit("synthetic job arguments differ")
limits = (container.get("resources") or {}).get("limits") or {}
actual_cpu = str(limits.get("cpu") or "")
if actual_cpu not in {expected_cpu, f"{int(expected_cpu) * 1000}m"}:
    raise SystemExit("synthetic job CPU differs")
if str(limits.get("memory") or "") != expected_memory:
    raise SystemExit("synthetic job memory differs")
if str(job_spec.get("timeoutSeconds", "")).removesuffix("s") != expected_timeout:
    raise SystemExit("synthetic job timeout differs")
if len(network_annotations) != 1:
    raise SystemExit("synthetic job VPC annotation inventory differs")
annotations = network_annotations[0]
try:
    interfaces = json.loads(annotations.get("run.googleapis.com/network-interfaces"))
except (TypeError, ValueError):
    raise SystemExit("synthetic job VPC network annotation is invalid") from None
if not isinstance(interfaces, list) or len(interfaces) != 1:
    raise SystemExit("synthetic job VPC interface count differs")

def exact_resource(value, kind, expected):
    text = str(value or "").rstrip("/")
    if text == expected:
        return True
    if f"/projects/{project}/" not in f"/{text}":
        return False
    return text.endswith(f"/{kind}/{expected}")

interface = interfaces[0]
if not exact_resource(interface.get("network"), "networks", expected_network):
    raise SystemExit("synthetic job VPC network differs")
if not exact_resource(interface.get("subnetwork"), "subnetworks", expected_subnet):
    raise SystemExit("synthetic job VPC subnet differs")
if annotations.get("run.googleapis.com/vpc-access-egress") != "private-ranges-only":
    raise SystemExit("synthetic job VPC egress differs")
env_items = container.get("env", []) or []
env_names = [item.get("name") for item in env_items]
if any(not name for name in env_names) or len(env_names) != len(set(env_names)):
    raise SystemExit("synthetic job environment contains missing or duplicate names")
env = {item["name"]: item for item in env_items}
expected_ingest = expected_base + "/v1/internal/synthetic/samples"
if env.get("TR_SYNTHETIC_INGEST_URL", {}).get("value") != expected_ingest:
    raise SystemExit("synthetic ingest URL does not target internal")
expected_plain = {
    "TR_ENVIRONMENT": "worker",
    "TR_SERVICE_SURFACE": "observer",
    "TR_RELEASE": expected_release,
    "TR_ENABLE_LIVE_PROVIDERS": "false",
    "TR_API_BASE_URL": "https://api.trustedrouter.com/v1",
    "TR_TRUSTED_DOMAIN": "trustedrouter.com",
    "TR_STORAGE_BACKEND": "spanner-bigtable",
    "TR_GCP_PROJECT_ID": project,
    "TR_SPANNER_INSTANCE_ID": spanner_instance,
    "TR_SPANNER_DATABASE_ID": spanner_database,
    "TR_BIGTABLE_INSTANCE_ID": bigtable_instance,
    "TR_BIGTABLE_GENERATION_TABLE": generation_table,
    "TR_REGIONS": gateway_regions,
    "TR_PRIMARY_REGION": primary_region,
    "TR_SYNTHETIC_MONITOR_MODEL": "trustedrouter/monitor",
    "TR_SYNTHETIC_MONITOR_TIMEOUT_SECONDS": "30",
    "TR_SYNTHETIC_CONTROL_PLANE_URL": "https://trustedrouter.com",
    "TR_SYNTHETIC_RUNS_PER_INVOCATION": "1",
    "TR_SYNTHETIC_RUN_SPACING_SECONDS": "0",
    "VERTEX_PROJECT_ID": project,
    "VERTEX_LOCATION": vertex_location,
    "TR_SYNTHETIC_MONITOR_REGION": region,
    "TR_SYNTHETIC_INGEST_URL": expected_ingest,
    "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL": expected_base,
}
if family == "monitor":
    monitor_regions = raw_monitor_regions.split(",")
    if region not in monitor_regions:
        raise SystemExit("synthetic monitor region is outside inventory")
    expected_plain.update({
        "TR_SYNTHETIC_BENCHMARK_INGEST_URL": expected_base + "/v1/internal/synthetic/benchmark",
        "TR_SYNTHETIC_ROUTE_HEALTH_URL": expected_base + "/v1/internal/synthetic/route-health",
        "TR_SYNTHETIC_BILLING_CONCURRENCY": "2",
        "TR_SYNTHETIC_START_DELAY_SECONDS": str(monitor_regions.index(region) * 20),
        "TR_SYNTHETIC_ROTATION_ENABLED": "true",
        "TR_SYNTHETIC_ROTATION_PER_PASS": "2",
        "TR_SYNTHETIC_THROUGHPUT_ENABLED": "false",
        "TR_SYNTHETIC_THROUGHPUT_ONLY": "false",
    })
    if region == primary_region:
        expected_plain["TR_SYNTHETIC_REMEDIATOR_URL"] = expected_base + "/v1/internal/synthetic/remediate"
elif family == "throughput":
    expected_plain.update({
        "TR_SYNTHETIC_BENCHMARK_INGEST_URL": expected_base + "/v1/internal/synthetic/benchmark",
        "TR_SYNTHETIC_ROUTE_HEALTH_URL": expected_base + "/v1/internal/synthetic/route-health",
        "TR_SYNTHETIC_BILLING_CONCURRENCY": "1",
        "TR_SYNTHETIC_START_DELAY_SECONDS": "0",
        "TR_SYNTHETIC_ROTATION_ENABLED": "false",
        "TR_SYNTHETIC_ROTATION_PER_PASS": "0",
        "TR_SYNTHETIC_THROUGHPUT_ENABLED": "true",
        "TR_SYNTHETIC_THROUGHPUT_ONLY": "true",
        "TR_SYNTHETIC_THROUGHPUT_REGION": region,
        "TR_SYNTHETIC_THROUGHPUT_ROUTE_LIMIT": "200",
        "TR_SYNTHETIC_THROUGHPUT_MAX_TOKENS": "512",
        "TR_SYNTHETIC_THROUGHPUT_MINIMUM_OUTPUT_TOKENS": "128",
        "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_SECONDS": "90",
        "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_CEILING_SECONDS": "210",
        "TR_SYNTHETIC_THROUGHPUT_INTERVAL_SECONDS": "300",
    })
elif family == "image":
    expected_plain.update({
        "TR_SYNTHETIC_IMAGE_MODEL": "google/gemini-3.1-flash-image-preview",
        "TR_SYNTHETIC_IMAGE_PROVIDER": "google-ai-studio",
        "TR_SYNTHETIC_IMAGE_TIMEOUT_SECONDS": "120",
        "TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS": "2",
    })
else:
    expected_plain.update({
        "TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS": "900",
        "TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS": "5",
    })
actual_plain = {
    name: str(item.get("value", ""))
    for name, item in env.items()
    if "valueFrom" not in item
}
if actual_plain != expected_plain:
    raise SystemExit("synthetic job exact plain environment differs")
expected_secrets = {
    "TR_OBSERVER_INTERNAL_TOKEN": "trustedrouter-observer-internal-token",
    "TR_SYNTHETIC_MONITOR_API_KEY": "trustedrouter-synthetic-monitor-api-key",
}
actual_secrets = {}
for name, item in env.items():
    if "valueFrom" not in item:
        continue
    reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
    resource = str(reference.get("name") or reference.get("secret") or "").split("/")[-1]
    version = str(reference.get("key") or reference.get("version") or "")
    if not version.isdigit() or version.startswith("0"):
        raise SystemExit(f"synthetic Job secret {name} is not pinned numerically")
    actual_secrets[name] = {"resource": resource, "version": version}
if {name: item["resource"] for name, item in actual_secrets.items()} != expected_secrets:
    raise SystemExit("synthetic Job exact secret allowlist differs")
Path(secret_output).write_text(
    "".join(
        f"{name}\t{actual_secrets[name]['resource']}\t{actual_secrets[name]['version']}\n"
        for name in sorted(expected_secrets)
    ),
    encoding="utf-8",
)
PY
  then
    rm -f "$job_path" "$job_secret_file"
    return 1
  fi
  while IFS=$'\t' read -r env_name secret version; do
    [ -n "$env_name" ] || continue
    version_json="$(gc secrets versions describe "$version" \
      --secret="$secret" --format=json)" || {
        rm -f "$job_path" "$job_secret_file"
        return 1
      }
    enabled_version="$(jq -er 'select(.state == "ENABLED") | .name | split("/")[-1]' \
      <<<"$version_json")" || {
        echo "ERROR: synthetic Job secret ${secret}:${version} is not enabled" >&2
        rm -f "$job_path" "$job_secret_file"
        return 1
      }
    [ "$enabled_version" = "$version" ] || {
      echo "ERROR: synthetic Job secret version resolution differs for ${secret}" >&2
      rm -f "$job_path" "$job_secret_file"
      return 1
    }
    secret_policy="$(gc secrets get-iam-policy "$secret" --format=json)" || {
      rm -f "$job_path" "$job_secret_file"
      return 1
    }
    if ! printf '%s' "$secret_policy" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
expected = sorted(sys.argv[1:])
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic secret IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/secretmanager.secretAccessor":
    raise SystemExit("synthetic secret IAM role differs")
if binding.get("condition") is not None:
    raise SystemExit("synthetic secret IAM grant is conditional")
members = binding.get("members") or []
if len(members) != len(set(members)) or sorted(members) != expected:
    raise SystemExit("synthetic secret IAM member inventory differs")
' "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
      "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"; then
      echo "ERROR: ${secret} does not have the exact two-consumer IAM policy" >&2
      rm -f "$job_path" "$job_secret_file"
      return 1
    fi
  done <"$job_secret_file"
  rm -f "$job_path" "$job_secret_file"
  scheduler_json="$(gc scheduler jobs describe "$scheduler_name" \
    --location="$region" --format=json)" || {
      echo "ERROR: synthetic scheduler ${region}/${scheduler_name} is absent" >&2
      return 1
    }
  scheduler_path="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-scheduler-XXXXXX")"
  printf '%s' "$scheduler_json" >"$scheduler_path"
  if ! python3 - "$scheduler_path" "$scheduler_name" "$job_name" \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" "$expected_run_uri" <<'PY'
import json
import sys
from pathlib import Path

path, expected_name, job_name, expected_identity, expected_uri = sys.argv[1:]
data = json.loads(Path(path).read_text(encoding="utf-8"))
reported_name = str(data.get("name") or "").rstrip("/").split("/")[-1]
if reported_name != expected_name:
    raise SystemExit("synthetic Scheduler name differs")
target = data.get("httpTarget") or {}
oauth = target.get("oauthToken") or {}
if oauth.get("serviceAccountEmail") != expected_identity:
    raise SystemExit("synthetic Scheduler OAuth identity differs")
if set(oauth) != {"serviceAccountEmail"}:
    raise SystemExit("synthetic Scheduler OAuth scope fields differ")
if target.get("oidcToken") not in (None, {}):
    raise SystemExit("synthetic Scheduler has an unexpected OIDC audience")
if target.get("uri") != expected_uri:
    raise SystemExit("synthetic Scheduler target differs")
if str(target.get("httpMethod", "")).upper() != "POST":
    raise SystemExit("synthetic Scheduler method differs")
if str(data.get("state", "")).upper() != "ENABLED":
    raise SystemExit("synthetic Scheduler is not enabled")
if job_name.startswith("trusted-router-synthetic-"):
    expected_schedule = "*/3 * * * *"
elif job_name.startswith("trusted-router-throughput-"):
    expected_schedule = "*/5 * * * *"
elif job_name.startswith("trusted-router-image-generation-"):
    expected_schedule = "17 */6 * * *"
elif job_name.startswith("trusted-router-video-generation-"):
    expected_schedule = "41 9 * * *"
else:
    raise SystemExit("synthetic Scheduler family differs")
if data.get("schedule") != expected_schedule:
    raise SystemExit("synthetic Scheduler cadence differs")
if data.get("timeZone") != "Etc/UTC":
    raise SystemExit("synthetic Scheduler time zone differs")
headers = target.get("headers") or {}
if headers not in ({}, {"User-Agent": "Google-Cloud-Scheduler"}):
    raise SystemExit("synthetic Scheduler headers differ")
if target.get("body") not in (None, ""):
    raise SystemExit("synthetic Scheduler RunJob body differs")

def duration_seconds(value):
    text = str(value or "")
    if not text.endswith("s"):
        raise SystemExit("synthetic Scheduler duration is malformed")
    return float(text[:-1])

if duration_seconds(data.get("attemptDeadline")) != 300:
    raise SystemExit("synthetic Scheduler attempt deadline differs")
retry = data.get("retryConfig") or {}
if set(retry) != {
    "retryCount", "maxRetryDuration", "minBackoffDuration",
    "maxBackoffDuration", "maxDoublings",
}:
    raise SystemExit("synthetic Scheduler retry fields differ")
if int(retry.get("retryCount", -1)) != 0 or int(retry.get("maxDoublings", -1)) != 3:
    raise SystemExit("synthetic Scheduler retry count/doublings differ")
if duration_seconds(retry.get("maxRetryDuration")) != 0:
    raise SystemExit("synthetic Scheduler retry duration differs")
if duration_seconds(retry.get("minBackoffDuration")) != 5:
    raise SystemExit("synthetic Scheduler minimum backoff differs")
if duration_seconds(retry.get("maxBackoffDuration")) != 60:
    raise SystemExit("synthetic Scheduler maximum backoff differs")
PY
  then
    rm -f "$scheduler_path"
    return 1
  fi
  rm -f "$scheduler_path"
}

verify_exact_synthetic_inventories() {
  local expected_file actual_file inventory_file region
  expected_file="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-expected-XXXXXX")"
  actual_file="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-actual-XXXXXX")"
  synthetic_inventory_lines >"$expected_file"
  for region in "${BOOTSTRAP_REGIONS[@]}"; do
    inventory_file="$(mktemp "${TMPDIR:-/tmp}/tr-synthetic-inventory-XXXXXX")"
    if ! gc run jobs list --region="$region" --format=json >"$inventory_file"; then
      rm -f "$expected_file" "$actual_file" "$inventory_file"
      return 1
    fi
    if ! python3 - "$region" "$inventory_file" <<'PY' >>"$actual_file"
import json
import sys
from pathlib import Path

region, path = sys.argv[1:]
for item in json.loads(Path(path).read_text(encoding="utf-8")):
    metadata = item.get("metadata") or {}
    name = str(metadata.get("name") or item.get("name") or "").split("/")[-1]
    if name.startswith((
        "trusted-router-synthetic-", "trusted-router-throughput-",
        "trusted-router-image-generation-", "trusted-router-video-generation-",
    )):
        print(region, name, sep="\t")
PY
    then
      rm -f "$expected_file" "$actual_file" "$inventory_file"
      return 1
    fi
    rm -f "$inventory_file"
  done
  python3 - "$expected_file" "$actual_file" <<'PY'
import sys
from pathlib import Path

expected = {
    tuple(line.split("\t")[:2])
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line
}
actual_rows = [
    tuple(line.split("\t"))
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line
]
if len(actual_rows) != len(set(actual_rows)) or set(actual_rows) != expected:
    raise SystemExit("synthetic Cloud Run Job inventory differs")
PY
  rm -f "$actual_file"
  : >"$actual_file"
  for region in "${BOOTSTRAP_REGIONS[@]}"; do
    inventory_file="$(mktemp "${TMPDIR:-/tmp}/tr-scheduler-inventory-XXXXXX")"
    if ! gc scheduler jobs list --location="$region" --format=json >"$inventory_file"; then
      rm -f "$expected_file" "$actual_file" "$inventory_file"
      return 1
    fi
    if ! python3 - "$region" "$inventory_file" <<'PY' >>"$actual_file"
import json
import sys
from pathlib import Path

region, path = sys.argv[1:]
for item in json.loads(Path(path).read_text(encoding="utf-8")):
    raw = str(item.get("name") or (item.get("metadata") or {}).get("name") or "")
    name = raw.split("/")[-1]
    if name.startswith((
        "trusted-router-synthetic-", "trusted-router-throughput-",
        "trusted-router-image-generation-", "trusted-router-video-generation-",
    )):
        print(region, name, sep="\t")
PY
    then
      rm -f "$expected_file" "$actual_file" "$inventory_file"
      return 1
    fi
    rm -f "$inventory_file"
  done
  python3 - "$expected_file" "$actual_file" <<'PY'
import sys
from pathlib import Path

expected = {
    (parts[0], parts[2])
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line
    for parts in [line.split("\t")]
}
actual_rows = [
    tuple(line.split("\t"))
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line
]
if len(actual_rows) != len(set(actual_rows)) or set(actual_rows) != expected:
    raise SystemExit("synthetic Cloud Scheduler inventory differs")
PY
  rm -f "$expected_file" "$actual_file"
}

verify_private_origin_connectivity() {
  local zone="${TR_PRIVATE_RUN_APP_DNS_ZONE:-trusted-router-private-run-app}"
  local region subnet_json zone_json apex_json wildcard_json
  for region in "${BOOTSTRAP_REGIONS[@]}"; do
    subnet_json="$(gc compute networks subnets describe "$SYNTHETIC_SUBNET" \
      --region="$region" --format=json)" || return 1
    jq -e --arg network "$SYNTHETIC_NETWORK" '
      .privateIpGoogleAccess == true and
      ((.network // "") | rtrimstr("/") | endswith("/networks/" + $network))
    ' <<<"$subnet_json" >/dev/null || {
      echo "ERROR: private internal origin lacks PGA in ${region}" >&2
      return 1
    }
  done
  zone_json="$(gc dns managed-zones describe "$zone" --format=json)" || return 1
  jq -e --arg network "$SYNTHETIC_NETWORK" '
    .dnsName == "run.app." and .visibility == "private" and
    any(.privateVisibilityConfig.networks[]?;
      (.networkUrl // "") | rtrimstr("/") | endswith("/networks/" + $network))
  ' <<<"$zone_json" >/dev/null || return 1
  apex_json="$(gc dns record-sets describe run.app. --zone="$zone" --type=A --format=json)" || return 1
  jq -e '(.rrdatas | sort) == [
    "199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"
  ]' <<<"$apex_json" >/dev/null || return 1
  wildcard_json="$(gc dns record-sets describe '*.run.app.' \
    --zone="$zone" --type=CNAME --format=json)" || return 1
  jq -e '.rrdatas == ["run.app."]' <<<"$wildcard_json" >/dev/null
}

verify_synthetic_identity_contract() {
  local synthetic_account_json synthetic_account_policy ancestor_rows
  local ancestor_type ancestor_id
  synthetic_account_json="$(gc iam service-accounts describe \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || return 1
  jq -e --arg email "$SYNTHETIC_RUN_SERVICE_ACCOUNT" '
    .email == $email and ((.disabled // false) == false)
  ' <<<"$synthetic_account_json" >/dev/null || {
    echo "ERROR: synthetic Job identity is absent or disabled" >&2
    return 1
  }
  synthetic_account_policy="$(gc iam service-accounts get-iam-policy \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || return 1
  if ! printf '%s' "$synthetic_account_policy" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic identity IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/iam.serviceAccountUser":
    raise SystemExit("synthetic identity actAs role differs")
if binding.get("condition") is not None:
    raise SystemExit("synthetic identity actAs grant is conditional")
members = binding.get("members") or []
if members != [sys.argv[1]]:
    raise SystemExit("synthetic identity actAs member inventory differs")
' "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"; then
    echo "ERROR: synthetic Job identity IAM is not the narrow reviewed policy" >&2
    return 1
  fi
  verify_exact_unconditional_roles \
    "project IAM roles on the synthetic Job identity" \
    "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}" \
    "" \
    gc projects get-iam-policy "$PROJECT_ID" || return 1
  ancestor_rows="$(gc projects get-ancestors "$PROJECT_ID" --format=json | \
    python3 -c '
import json
import re
import sys

items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("synthetic identity ancestor inventory is malformed")
seen = set()
has_project = False
for item in items:
    if not isinstance(item, dict) or set(item) < {"type", "id"}:
        raise SystemExit("synthetic identity ancestor inventory is malformed")
    kind = item["type"]
    identifier = str(item["id"])
    if kind not in {"project", "folder", "organization"} or not re.fullmatch(r"[A-Za-z0-9._:-]+", identifier):
        raise SystemExit("synthetic identity ancestor entry is invalid")
    pair = (kind, identifier)
    if pair in seen:
        raise SystemExit("synthetic identity ancestor is duplicated")
    seen.add(pair)
    has_project = has_project or (kind == "project" and identifier == sys.argv[1])
    print(kind, identifier, sep="\t")
if not has_project:
    raise SystemExit("synthetic identity ancestor inventory omits project")
' "$PROJECT_ID")" || return 1
  while IFS=$'\t' read -r ancestor_type ancestor_id; do
    [ -n "$ancestor_type" ] || continue
    case "$ancestor_type" in
      project)
        [ "$ancestor_id" = "$PROJECT_ID" ] || {
          echo "ERROR: synthetic identity ancestor project differs" >&2
          return 1
        }
        ;;
      folder)
        verify_exact_unconditional_roles \
          "folder ancestor IAM roles on the synthetic Job identity" \
          "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}" "" \
          gc resource-manager folders get-iam-policy "$ancestor_id" || return 1
        ;;
      organization)
        verify_exact_unconditional_roles \
          "organization ancestor IAM roles on the synthetic Job identity" \
          "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}" "" \
          gc organizations get-iam-policy "$ancestor_id" || return 1
        ;;
      *) return 1 ;;
    esac
  done <<<"$ancestor_rows"
}

preflight_legacy_data_mode() {
  local region service_json revision revision_json region_state expected_state=""
  for region in "${CONTROL_REGIONS[@]}"; do
    service_json="$(gc run services describe "$BOOTSTRAP_LEGACY_CONSOLE_SERVICE" \
      --region="$region" --format=json)" || return 1
    revision="$(printf '%s' "$service_json" | python3 -c '
import json
import sys

service = json.load(sys.stdin)
traffic = service.get("status", {}).get("traffic", []) or []
live = [item for item in traffic if int(item.get("percent", 0) or 0) > 0]
if len(live) != 1 or int(live[0].get("percent", 0)) != 100:
    raise SystemExit("legacy console traffic is not one unambiguous 100% revision")
revision = live[0].get("revisionName")
if not isinstance(revision, str) or not revision:
    raise SystemExit("legacy console serving revision is absent")
print(revision)
')" || return 1
    revision_json="$(gc run revisions describe "$revision" \
      --region="$region" --format=json)" || return 1
    region_state="$(printf '%s' "$revision_json" | python3 -c '
import json
import sys

revision = json.load(sys.stdin)
containers = (revision.get("spec") or {}).get("containers") or []
if len(containers) != 1:
    raise SystemExit("legacy console serving container inventory differs")
env = {
    item.get("name"): str(item.get("value", ""))
    for item in containers[0].get("env", []) or []
    if item.get("name") and "valueFrom" not in item
}
secrets = {
    item.get("name"): {
        "resource": str(
            (((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") or "")
        ).split("/")[-1],
        "version": str(
            (((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("key")
            or ((item.get("valueFrom") or {}).get("secretKeyRef") or {}).get("version")
            or "")
        ),
    }
    for item in containers[0].get("env", []) or []
    if item.get("name") and "valueFrom" in item
}
value = {
    "storage_backend": env.get("TR_STORAGE_BACKEND", "spanner-bigtable"),
    "analytics_read_mode": env.get("TR_ANALYTICS_READ_MODE", "bigtable"),
    "request_record_write_mode": env.get("TR_REQUEST_RECORD_WRITE_MODE", ""),
    "generation_records_enabled": env.get("TR_GENERATION_RECORDS_ENABLED", "true"),
    "bigtable_mirror_writes_enabled": env.get("TR_BIGTABLE_MIRROR_WRITES_ENABLED", "true"),
    "analytics_dual_read_started_at": env.get("TR_ANALYTICS_DUAL_READ_STARTED_AT", ""),
    "analytics_clickhouse_primary_started_at": env.get("TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT", ""),
    "operational_clickhouse_url": env.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL", ""),
    "operational_clickhouse_user": env.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER", ""),
    "operational_clickhouse_database": env.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE", ""),
    "operational_clickhouse_password_secret": secrets.get(
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD", {}
    ).get("resource", ""),
    "operational_clickhouse_password_version": secrets.get(
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD", {}
    ).get("version", ""),
}
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
')" || return 1
    if [ -z "$expected_state" ]; then
      expected_state="$region_state"
    elif [ "$region_state" != "$expected_state" ]; then
      echo "ERROR: legacy console data mode differs across control regions" >&2
      return 1
    fi
  done
  BOOTSTRAP_STORAGE_BACKEND="$(jq -er '.storage_backend' <<<"$expected_state")"
  BOOTSTRAP_ANALYTICS_READ_MODE="$(jq -er '.analytics_read_mode' <<<"$expected_state")"
  BOOTSTRAP_REQUEST_RECORD_WRITE_MODE="$(jq -er '.request_record_write_mode' <<<"$expected_state")"
  BOOTSTRAP_GENERATION_RECORDS_ENABLED="$(jq -er '.generation_records_enabled' <<<"$expected_state")"
  BOOTSTRAP_BIGTABLE_MIRROR_WRITES_ENABLED="$(jq -er '.bigtable_mirror_writes_enabled' <<<"$expected_state")"
  BOOTSTRAP_ANALYTICS_DUAL_READ_STARTED_AT="$(jq -r '.analytics_dual_read_started_at' <<<"$expected_state")"
  BOOTSTRAP_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(jq -r '.analytics_clickhouse_primary_started_at' <<<"$expected_state")"
  BOOTSTRAP_OPERATIONAL_CLICKHOUSE_URL="$(jq -r '.operational_clickhouse_url' <<<"$expected_state")"
  BOOTSTRAP_OPERATIONAL_CLICKHOUSE_USER="$(jq -r '.operational_clickhouse_user' <<<"$expected_state")"
  BOOTSTRAP_OPERATIONAL_CLICKHOUSE_DATABASE="$(jq -r '.operational_clickhouse_database' <<<"$expected_state")"
  BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_SECRET="$(jq -r '.operational_clickhouse_password_secret' <<<"$expected_state")"
  BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_VERSION="$(jq -r '.operational_clickhouse_password_version' <<<"$expected_state")"
  case "$BOOTSTRAP_STORAGE_BACKEND:$BOOTSTRAP_ANALYTICS_READ_MODE:$BOOTSTRAP_REQUEST_RECORD_WRITE_MODE:$BOOTSTRAP_GENERATION_RECORDS_ENABLED:$BOOTSTRAP_BIGTABLE_MIRROR_WRITES_ENABLED" in
    spanner-bigtable:bigtable:typed:true:true)
      [ -z "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_URL" ] && \
        [ -z "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_USER" ] && \
        [ -z "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_DATABASE" ] && \
        [ -z "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_SECRET" ] && \
        [ -z "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_VERSION" ] || {
          echo "ERROR: Bigtable mode has stale operational ClickHouse bindings" >&2
          return 1
        }
      ;;
    spanner-clickhouse:clickhouse-only:typed:true:false)
      [ "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_USER" = tr_control_read ] && \
        [ "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_DATABASE" = tr ] && \
        [ "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_SECRET" = trustedrouter-clickhouse-control-read-password ] && \
        [[ "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_VERSION" =~ ^[1-9][0-9]*$ ]] || {
          echo "ERROR: serving ClickHouse credentials differ from the canonical internal contract" >&2
          return 1
        }
      python3 - "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_URL" <<'PY' || return 1
import ipaddress
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("serving operational ClickHouse URL is malformed")
try:
    private = ipaddress.ip_address(parsed.hostname).is_private
except ValueError:
    private = parsed.hostname.endswith(".internal")
if not private:
    raise SystemExit("serving operational ClickHouse URL is not private")
PY
      ;;
    *)
      echo "ERROR: legacy data mode is not a reviewed bootstrap contract" >&2
      return 1
      ;;
  esac
}

verify_artifact_live() {
  local artifact_rows region revision digest
  artifact_rows="$(validate_artifact_and_emit_services "$EXPECTED_IMAGE")" || return 1
  ARTIFACT_RELEASE="$(jq -er '.release' "$ARTIFACT")" || return 1
  ARTIFACT_STORAGE_BACKEND="$(jq -er '.data_mode.storage_backend' "$ARTIFACT")"
  ARTIFACT_ANALYTICS_READ_MODE="$(jq -er '.data_mode.analytics_read_mode' "$ARTIFACT")"
  ARTIFACT_REQUEST_RECORD_WRITE_MODE="$(jq -er '.data_mode.request_record_write_mode' "$ARTIFACT")"
  ARTIFACT_GENERATION_RECORDS_ENABLED="$(jq -er '.data_mode.generation_records_enabled' "$ARTIFACT")"
  ARTIFACT_BIGTABLE_MIRROR_WRITES_ENABLED="$(jq -er '.data_mode.bigtable_mirror_writes_enabled' "$ARTIFACT")"
  ARTIFACT_ANALYTICS_DUAL_READ_STARTED_AT="$(jq -r '.data_mode.analytics_dual_read_started_at' "$ARTIFACT")"
  ARTIFACT_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(jq -r '.data_mode.analytics_clickhouse_primary_started_at' "$ARTIFACT")"
  ARTIFACT_OPERATIONAL_CLICKHOUSE_URL="$(jq -r '.data_mode.operational_clickhouse_url' "$ARTIFACT")"
  ARTIFACT_OPERATIONAL_CLICKHOUSE_USER="$(jq -r '.data_mode.operational_clickhouse_user' "$ARTIFACT")"
  ARTIFACT_OPERATIONAL_CLICKHOUSE_DATABASE="$(jq -r '.data_mode.operational_clickhouse_database' "$ARTIFACT")"
  bash "${SCRIPT_DIR}/rollout_iam_verify.sh" --project "$PROJECT_ID"
  verify_synthetic_identity_contract
  verify_private_origin_connectivity
  while IFS=$'\t' read -r region revision digest; do
    [ -n "$region" ] || continue
    verify_live_internal_service "$region" "$revision" "$digest"
  done <<<"$artifact_rows"
  verify_exact_synthetic_inventories
  while IFS=$'\t' read -r region job_name scheduler_name; do
    [ -n "$region" ] || continue
    verify_synthetic_job_target "$region" "$job_name" "$scheduler_name"
  done < <(synthetic_inventory_lines)
  log "internal bootstrap artifact and synthetic cutover are verified"
}

if [ "$MODE" = verify ]; then
  [[ "$EXPECTED_IMAGE" =~ ^[^,\|[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
    echo "ERROR: bootstrap verification requires an immutable expected image" >&2
    exit 1
  }
  verify_artifact_live
  exit 0
fi

[ ! -e "$ARTIFACT" ] || {
  echo "ERROR: refusing to overwrite internal bootstrap artifact ${ARTIFACT}" >&2
  exit 1
}

[ -n "${TR_LEGACY_HARDENING_ARTIFACT:-}" ] || {
  echo "ERROR: internal bootstrap requires TR_LEGACY_HARDENING_ARTIFACT" >&2
  exit 1
}
bash "${SCRIPT_DIR}/rollout_legacy_harden.sh" \
  --verify-artifact "$TR_LEGACY_HARDENING_ARTIFACT" || exit 1

preflight_initial_split_boundary() {
  local https_proxy="${TR_HTTPS_PROXY:-trusted-router-control-https-proxy}"
  local url_map_name url_map_json
  [[ "$https_proxy" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
    echo "ERROR: internal bootstrap HTTPS proxy name is invalid" >&2
    return 1
  }
  url_map_name="$(gc compute target-https-proxies describe "$https_proxy" \
    --global --format='value(urlMap.basename())')" || return 1
  [[ "$url_map_name" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
    echo "ERROR: internal bootstrap could not resolve the active HTTPS URL map" >&2
    return 1
  }
  url_map_json="$(gc compute url-maps describe "$url_map_name" \
    --global --format=json)" || return 1
  if jq -e 'any(.pathMatchers[]?; .name == "trusted-router-service-surfaces")' \
      <<<"$url_map_json" >/dev/null; then
    echo "ERROR: internal bootstrap is initial-split-only; the six-surface URL map is already active" >&2
    return 1
  fi
}
preflight_initial_split_boundary

# Use the same complete six-runtime IAM/Secret Manager inventory gate as the
# main stage. Bootstrap is allowed to create a revision only after that
# verifier proves the current project is already least-privilege.
bash "${SCRIPT_DIR}/rollout_iam_verify.sh" --project "$PROJECT_ID"
# This script never creates or grants the seventh identity. Its separately
# approved least-privilege bootstrap must exist before any internal revision is
# staged, otherwise the synthetic cutover has no safe runtime principal.
verify_synthetic_identity_contract
preflight_legacy_data_mode

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tr-internal-bootstrap-XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
ENTRIES_FILE="${WORK_DIR}/services.jsonl"
: >"$ENTRIES_FILE"
chmod 600 "$ENTRIES_FILE"

RELEASE="${TR_RELEASE:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
[[ "$RELEASE" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || {
  echo "ERROR: bootstrap release is invalid" >&2
  exit 1
}
if [ -n "${TR_INTERNAL_BOOTSTRAP_REVISION_SUFFIX:-}" ]; then
  REVISION_SUFFIX="$TR_INTERNAL_BOOTSTRAP_REVISION_SUFFIX"
elif [ -e "$JOURNAL" ] || [ -L "$JOURNAL" ]; then
  # Recovery without an explicit override must resume the exact recorded
  # revision name. Generating a fresh suffix here would turn a retry into a
  # second bootstrap cohort.
  REVISION_SUFFIX="$(read_journal_revision_suffix)"
else
  REVISION_SUFFIX="ib$(date -u +%Y%m%d%H%M%S)-${GITHUB_RUN_ATTEMPT:-0}"
fi
[[ "$REVISION_SUFFIX" =~ ^[a-z][a-z0-9-]{0,34}[a-z0-9]$ ]] || {
  echo "ERROR: internal bootstrap revision suffix is invalid" >&2
  exit 1
}

IMAGE_METADATA="$(gc artifacts docker images describe "$IMAGE" --format=json)" || {
  echo "ERROR: bootstrap image does not exist: ${IMAGE}" >&2
  exit 1
}
IMAGE="$(jq -er '
  .image_summary.fully_qualified_digest //
  .imageSummary.fullyQualifiedDigest //
  .fully_qualified_digest //
  .fullyQualifiedDigest
' <<<"$IMAGE_METADATA")" || {
  echo "ERROR: bootstrap image did not resolve to a digest" >&2
  exit 1
}
[[ "$IMAGE" =~ ^[^,\|[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "ERROR: bootstrap image digest is invalid" >&2
  exit 1
}

account_json="$(gc iam service-accounts describe "$INTERNAL_RUN_SERVICE_ACCOUNT" --format=json)"
jq -e --arg email "$INTERNAL_RUN_SERVICE_ACCOUNT" '
  .email == $email and ((.disabled // false) == false)
' <<<"$account_json" >/dev/null || {
  echo "ERROR: internal runtime service account is missing or disabled" >&2
  exit 1
}
account_policy="$(gc iam service-accounts get-iam-policy \
  "$INTERNAL_RUN_SERVICE_ACCOUNT" --format=json)"
jq -e --arg member "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" '
  ([.bindings[]? | select(any(.members[]?; . == $member))
    | {role, condition: (.condition // null)}] | unique)
  == [{role:"roles/iam.serviceAccountUser",condition:null}]
' <<<"$account_policy" >/dev/null || {
  echo "ERROR: deploy identity lacks exact actAs on internal runtime identity" >&2
  exit 1
}

SECRET_BINDINGS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token"
  "TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token"
  "TR_SYNTHETIC_MONITOR_API_KEY=trustedrouter-synthetic-monitor-api-key"
  "TR_STRIPE_SECRET_KEY=trustedrouter-internal-stripe-payment-intents-key"
  "TR_AWS_ACCESS_KEY_ID=trustedrouter-internal-ses-access-key-id"
  "TR_AWS_SECRET_ACCESS_KEY=trustedrouter-internal-ses-secret-access-key"
)
if [ "$BOOTSTRAP_ANALYTICS_READ_MODE" != bigtable ]; then
  SECRET_BINDINGS+=(
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD=trustedrouter-clickhouse-control-read-password"
  )
fi
PINNED_SECRETS=()
for binding in "${SECRET_BINDINGS[@]}"; do
  env_name="${binding%%=*}"
  secret="${binding#*=}"
  gc secrets describe "$secret" >/dev/null
  if [ "$env_name" = TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD ]; then
    requested_version="$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_PASSWORD_VERSION"
  else
    requested_version=latest
  fi
  version_json="$(gc secrets versions describe "$requested_version" \
    --secret="$secret" --format=json)"
  version="$(jq -er 'select(.state == "ENABLED") | .name | split("/")[-1]' \
    <<<"$version_json")" || {
      echo "ERROR: bootstrap secret ${secret}:${requested_version} is not enabled" >&2
      exit 1
    }
  [[ "$version" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERROR: bootstrap secret ${secret} did not resolve numerically" >&2
    exit 1
  }
  if [ "$requested_version" != latest ] && [ "$version" != "$requested_version" ]; then
    echo "ERROR: serving ClickHouse password version resolution differs" >&2
    exit 1
  fi
  secret_policy="$(gc secrets get-iam-policy "$secret" --format=json)"
  jq -e --arg member "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" '
    ([.bindings[]? | select(any(.members[]?; . == $member))
      | {role, condition: (.condition // null)}] | unique)
    == [{role:"roles/secretmanager.secretAccessor",condition:null}]
  ' <<<"$secret_policy" >/dev/null || {
    echo "ERROR: internal identity lacks exact secretAccessor on ${secret}" >&2
    exit 1
  }
  PINNED_SECRETS+=("${env_name}=${secret}:${version}")
done

verify_private_origin_connectivity

ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_RELEASE=${RELEASE}"
  "TR_SERVICE_SURFACE=internal"
  "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_TRUSTED_DOMAIN_ALIASES=allyrouter.com,uptimerouter.com"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_REGIONS=${TR_REGIONS}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_RATE_LIMIT_CLIENT_IP_MODE=edge_header"
  "TR_MAX_REQUEST_BODY_BYTES=33554432"
  "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES=67108864"
  "TR_MAX_CONCURRENT_REQUEST_BODIES=4"
  "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS=30"
  "TR_REMEDIATOR_IN_PROCESS_ENABLED=false"
  "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=0"
  "TR_ACTIVATION_REMINDER_INTERVAL_SECONDS=0"
  "TR_REGIONAL_QUOTA_LEASES_ENABLED=false"
  "TR_STORAGE_BACKEND=${BOOTSTRAP_STORAGE_BACKEND}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_SPANNER_POOL_SIZE=8"
  "TR_BIGTABLE_MIRROR_WRITES_ENABLED=${BOOTSTRAP_BIGTABLE_MIRROR_WRITES_ENABLED}"
  "TR_GENERATION_RECORDS_ENABLED=${BOOTSTRAP_GENERATION_RECORDS_ENABLED}"
  "TR_ANALYTICS_READ_MODE=${BOOTSTRAP_ANALYTICS_READ_MODE}"
  "TR_ANALYTICS_DUAL_READ_STARTED_AT=${BOOTSTRAP_ANALYTICS_DUAL_READ_STARTED_AT}"
  "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT=${BOOTSTRAP_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT}"
  "TR_REQUEST_RECORD_WRITE_MODE=${BOOTSTRAP_REQUEST_RECORD_WRITE_MODE}"
  "TR_SETTLE_OUTBOX_ENABLED=true"
  "TR_ANALYTICS_OUTBOX_ENABLED=true"
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
  "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
  "TR_AWS_REGION=us-east-1"
  "TR_SES_FROM_EMAIL=alerts@alerts.trustedrouter.com"
  "TR_SES_FROM_NAME=TrustedRouter Alerts"
  "TR_SES_ALERT_FROM_EMAIL=alerts@alerts.trustedrouter.com"
  "TR_SES_ALERT_FROM_NAME=TrustedRouter Alerts"
  "TR_SES_ALERT_CONFIGURATION_SET=trustedrouter-alerts"
)
if [ "$BOOTSTRAP_ANALYTICS_READ_MODE" != bigtable ]; then
  ENV_VARS+=(
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=${BOOTSTRAP_OPERATIONAL_CLICKHOUSE_URL}"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=${BOOTSTRAP_OPERATIONAL_CLICKHOUSE_USER}"
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=${BOOTSTRAP_OPERATIONAL_CLICKHOUSE_DATABASE}"
  )
fi
SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"
SET_SECRETS="$(IFS=,; echo "${PINNED_SECRETS[*]}")"

EXPECTED_ENV_FILE="${WORK_DIR}/expected-env.json"
EXPECTED_SECRET_FILE="${WORK_DIR}/expected-secrets.json"
printf '%s\n' "${ENV_VARS[@]}" | jq -Rn '
  [inputs | capture("^(?<name>[^=]+)=(?<value>.*)$")] | from_entries
' >"$EXPECTED_ENV_FILE"
printf '%s\n' "${PINNED_SECRETS[@]}" | jq -Rn '
  [inputs | capture("^(?<name>[^=]+)=(?<resource>.*):(?<version>[1-9][0-9]*)$")
    | {key: .name, value: {resource: .resource, version: .version}}] | from_entries
' >"$EXPECTED_SECRET_FILE"
chmod 600 "$EXPECTED_ENV_FILE" "$EXPECTED_SECRET_FILE"

verify_bootstrap_candidate() {
  local region="$1" candidate="$2" require_serving="$3"
  local service_json service_path service_iam
  service_path="${WORK_DIR}/service-${region}.json"
  service_json="$(gc run services describe "$INTERNAL_SERVICE" \
    --region="$region" --format=json)" || return 1
  printf '%s' "$service_json" >"$service_path"
  service_iam="$(gc run services get-iam-policy "$INTERNAL_SERVICE" \
    --region="$region" --format=json)" || return 1
  jq -e '
    [.bindings[]? | select(any(.members[]?; . == "allUsers"))
      | {role, condition: (.condition // null),
         allUsersCount: ([.members[]? | select(. == "allUsers")] | length)}]
    == [{role:"roles/run.invoker",condition:null,allUsersCount:1}]
  ' <<<"$service_iam" >/dev/null || return 1
  if ! python3 - "$service_path" "$EXPECTED_ENV_FILE" "$EXPECTED_SECRET_FILE" \
    "$candidate" "$INTERNAL_RUN_SERVICE_ACCOUNT" "$IMAGE" 2Gi 1 8080 \
    "$CLOUD_RUN_NETWORK" "$CLOUD_RUN_SUBNET" private-ranges-only \
    /ready 0 10 10 18 "$PROJECT_ID" "$require_serving" \
    "https://${INTERNAL_SERVICE}-${PROJECT_NUMBER}.${region}.run.app" <<'PY'
import json
import sys
from pathlib import Path

(
    service_path,
    env_path,
    secret_path,
    candidate,
    account,
    expected_image,
    memory,
    cpu,
    port,
    network,
    subnet,
    vpc_egress,
    probe_path,
    probe_initial_delay,
    probe_timeout,
    probe_period,
    probe_failures,
    project,
    require_serving,
    expected_url,
) = sys.argv[1:]
service = json.loads(Path(service_path).read_text(encoding="utf-8"))
expected_env = json.loads(Path(env_path).read_text(encoding="utf-8"))
expected_secrets = json.loads(Path(secret_path).read_text(encoding="utf-8"))
metadata = service.get("metadata") or {}
annotations = metadata.get("annotations") or {}
spec = service.get("spec") or {}
template = spec.get("template") or {}
template_annotations = (template.get("metadata") or {}).get("annotations") or {}
template_spec = template.get("spec") or {}
status = service.get("status") or {}
if status.get("latestCreatedRevisionName") != candidate or status.get("latestReadyRevisionName") != candidate:
    raise SystemExit("internal bootstrap candidate is not latest Ready")
if not any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])):
    raise SystemExit("internal bootstrap candidate is not Ready")
if str(status.get("observedGeneration")) != str(metadata.get("generation")):
    raise SystemExit("internal bootstrap generation is unobserved")
if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
    raise SystemExit("internal bootstrap ingress differs")
if annotations.get("run.googleapis.com/ingress-status") != "internal-and-cloud-load-balancing":
    raise SystemExit("internal bootstrap effective ingress differs")
if str(annotations.get("run.googleapis.com/default-url-disabled", "false")).lower() not in {"", "false"}:
    raise SystemExit("internal bootstrap default URL is disabled")
if status.get("url") != expected_url:
    raise SystemExit("internal bootstrap default URL differs")
if template_spec.get("serviceAccountName") != account:
    raise SystemExit("internal bootstrap identity differs")
if int(template_spec.get("containerConcurrency", -1)) != 8:
    raise SystemExit("internal bootstrap concurrency differs")
if str(template_spec.get("timeoutSeconds", "")).removesuffix("s") != "300":
    raise SystemExit("internal bootstrap timeout differs")
if str(template_annotations.get("autoscaling.knative.dev/minScale")) != "2":
    raise SystemExit("internal bootstrap minimum differs")
if str(template_annotations.get("autoscaling.knative.dev/maxScale")) != "50":
    raise SystemExit("internal bootstrap revision maximum differs")
if str((spec.get("scaling") or {}).get("maxInstanceCount")) != "50":
    raise SystemExit("internal bootstrap service maximum differs")
containers = template_spec.get("containers") or []
if len(containers) != 1:
    raise SystemExit("internal bootstrap container count differs")
container = containers[0]
if container.get("image") != expected_image:
    raise SystemExit("internal bootstrap image differs")
if container.get("command") not in (None, []):
    raise SystemExit("internal bootstrap container command differs")
if container.get("args") not in (None, []):
    raise SystemExit("internal bootstrap container arguments differ")
if container.get("volumeMounts") not in (None, []):
    raise SystemExit("internal bootstrap container volume mounts differ")
if template_spec.get("volumes") not in (None, []):
    raise SystemExit("internal bootstrap volumes differ")
if template_spec.get("initContainers") not in (None, []):
    raise SystemExit("internal bootstrap init containers differ")
ports = container.get("ports") or []
if len(ports) != 1 or int(ports[0].get("containerPort", -1)) != int(port):
    raise SystemExit("internal bootstrap container port differs")
limits = (container.get("resources") or {}).get("limits") or {}
if str(limits.get("memory")) != memory:
    raise SystemExit("internal bootstrap memory differs")
actual_cpu = str(limits.get("cpu") or "")
if actual_cpu not in {cpu, f"{int(cpu) * 1000}m"}:
    raise SystemExit("internal bootstrap CPU differs")
network_interfaces_raw = template_annotations.get("run.googleapis.com/network-interfaces")
try:
    network_interfaces = json.loads(network_interfaces_raw)
except (TypeError, ValueError):
    raise SystemExit("internal bootstrap VPC network annotation is invalid") from None
if not isinstance(network_interfaces, list) or len(network_interfaces) != 1:
    raise SystemExit("internal bootstrap VPC network interface count differs")

def exact_resource(value, kind, expected):
    text = str(value or "").rstrip("/")
    if text == expected:
        return True
    if f"/projects/{project}/" not in f"/{text}":
        return False
    return text.endswith(f"/{kind}/{expected}")

interface = network_interfaces[0]
if not exact_resource(interface.get("network"), "networks", network):
    raise SystemExit("internal bootstrap VPC network differs")
if not exact_resource(interface.get("subnetwork"), "subnetworks", subnet):
    raise SystemExit("internal bootstrap VPC subnet differs")
if template_annotations.get("run.googleapis.com/vpc-access-egress") != vpc_egress:
    raise SystemExit("internal bootstrap VPC egress differs")
probe = container.get("startupProbe") or {}
http_get = probe.get("httpGet") or {}
if http_get.get("path") != probe_path:
    raise SystemExit("internal bootstrap /ready probe is absent")
if http_get.get("port") is not None and int(http_get["port"]) != int(port):
    raise SystemExit("internal bootstrap startup probe port differs")
expected_probe = {
    "initialDelaySeconds": int(probe_initial_delay),
    "timeoutSeconds": int(probe_timeout),
    "periodSeconds": int(probe_period),
    "failureThreshold": int(probe_failures),
}
for name, expected in expected_probe.items():
    if int(probe.get(name, -1)) != expected:
        raise SystemExit(f"internal bootstrap startup probe {name} differs")
env_items = container.get("env", []) or []
env_names = [item.get("name") for item in env_items]
if any(not name for name in env_names) or len(env_names) != len(set(env_names)):
    raise SystemExit("internal bootstrap environment contains missing or duplicate names")
actual_env = {}
actual_secrets = {}
for item in env_items:
    name = item.get("name")
    if "valueFrom" not in item:
        actual_env[name] = str(item.get("value", ""))
        continue
    reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
    actual_secrets[name] = {
        "resource": str(reference.get("name") or "").split("/")[-1],
        "version": str(reference.get("key") or reference.get("version") or ""),
    }
if actual_env != expected_env or actual_secrets != expected_secrets:
    raise SystemExit("internal bootstrap exact env/secret allowlist differs")
positive = [item for item in status.get("traffic", []) if int(item.get("percent", 0) or 0) > 0]
if require_serving == "true":
    def exact_sole_target(items):
        return (
            len(items) == 1
            and items[0].get("revisionName") == candidate
            and int(items[0].get("percent", 0) or 0) == 100
            and not items[0].get("tag")
            and not items[0].get("latestRevision", False)
        )
    if not exact_sole_target(spec.get("traffic") or []):
        raise SystemExit("internal bootstrap desired traffic inventory differs")
    if not exact_sole_target(status.get("traffic") or []):
        raise SystemExit("internal bootstrap candidate is not sole 100% target")
else:
    candidate_percent = sum(int(item.get("percent", 0) or 0) for item in positive if item.get("revisionName") == candidate)
    if candidate_percent not in {0, 100}:
        raise SystemExit("internal bootstrap candidate has partial pre-promotion traffic")
PY
  then
    return 1
  fi
  python3 "$STATE_TOOL" hash-service "$service_path"
}

validate_journal_and_emit_states() {
  python3 - "$JOURNAL" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
    "$REVISION_SUFFIX" "$BOOTSTRAP_REGION_CSV" "$INTERNAL_SERVICE" \
    "$INTERNAL_RUN_SERVICE_ACCOUNT" "$BOOTSTRAP_OPERATION_ID" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

(
    raw_path,
    project,
    image,
    release,
    revision_suffix,
    raw_regions,
    service,
    account,
    operation_id,
) = sys.argv[1:]
path = Path(raw_path)
if path.is_symlink() or not path.is_file():
    raise SystemExit("bootstrap state must be a regular non-symlink file")
if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
    raise SystemExit("bootstrap state must have mode 0600")
value = json.loads(path.read_text(encoding="utf-8"))
fields = {
    "schema_version", "kind", "project_id", "image", "release",
    "created_at", "revision_suffix", "regions", "internal_service",
    "runtime_service_account", "operation_id", "region_states",
}
if not isinstance(value, dict) or set(value) != fields:
    raise SystemExit("bootstrap state fields differ from schema v1")
if value["schema_version"] != 1 or value["kind"] != "trusted-router-internal-bootstrap-state":
    raise SystemExit("bootstrap state schema/kind is unsupported")
expected = {
    "project_id": project,
    "image": image,
    "release": release,
    "revision_suffix": revision_suffix,
    "regions": raw_regions.split(","),
    "internal_service": service,
    "runtime_service_account": account,
    "operation_id": operation_id,
}
for field, expected_value in expected.items():
    if value[field] != expected_value:
        raise SystemExit(f"bootstrap state {field} differs")
if not isinstance(value["created_at"], str) or not re.fullmatch(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["created_at"]
):
    raise SystemExit("bootstrap state timestamp is invalid")
states = value["region_states"]
regions = expected["regions"]
if not isinstance(states, list) or len(states) != len(regions):
    raise SystemExit("bootstrap state region inventory differs")
seen = set()
allowed_states = {"pending", "deploy_intent", "deployed", "traffic_intent", "settled"}
for item in states:
    if not isinstance(item, dict) or set(item) != {"region", "state"}:
        raise SystemExit("bootstrap region state fields differ")
    region = item["region"]
    state = item["state"]
    if region not in regions or region in seen or state not in allowed_states:
        raise SystemExit("bootstrap region state is invalid")
    seen.add(region)
    print(region, state, sep="\t")
if seen != set(regions):
    raise SystemExit("bootstrap state region inventory is incomplete")
PY
}

create_bootstrap_journal() {
  python3 - "$JOURNAL" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
    "$REVISION_SUFFIX" "$BOOTSTRAP_REGION_CSV" "$INTERNAL_SERVICE" \
    "$INTERNAL_RUN_SERVICE_ACCOUNT" "$BOOTSTRAP_OPERATION_ID" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

(
    raw_path,
    project,
    image,
    release,
    revision_suffix,
    raw_regions,
    service,
    account,
    operation_id,
    created_at,
) = sys.argv[1:]
path = Path(raw_path)
value = {
    "schema_version": 1,
    "kind": "trusted-router-internal-bootstrap-state",
    "project_id": project,
    "image": image,
    "release": release,
    "created_at": created_at,
    "revision_suffix": revision_suffix,
    "regions": raw_regions.split(","),
    "internal_service": service,
    "runtime_service_account": account,
    "operation_id": operation_id,
    "region_states": [
        {"region": region, "state": "pending"}
        for region in raw_regions.split(",")
    ],
}
descriptor, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    # link() is the atomic no-replace publish operation: concurrent bootstrap
    # attempts cannot overwrite or adopt one another's provenance state.
    os.link(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    os.unlink(temporary)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
}

journal_region_state() {
  local wanted_region="$1"
  validate_journal_and_emit_states | while IFS=$'\t' read -r region state; do
    if [ "$region" = "$wanted_region" ]; then
      printf '%s\n' "$state"
    fi
    true
  done
}

journal_transition() {
  local region="$1" old_state="$2" new_state="$3"
  validate_journal_and_emit_states >/dev/null
  python3 - "$JOURNAL" "$region" "$old_state" "$new_state" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
region, old_state, new_state = sys.argv[2:]
allowed = {
    ("pending", "deploy_intent"),
    ("deploy_intent", "deployed"),
    ("deployed", "traffic_intent"),
    ("traffic_intent", "settled"),
}
if (old_state, new_state) not in allowed:
    raise SystemExit("invalid bootstrap state transition")
value = json.loads(path.read_text(encoding="utf-8"))
matches = [item for item in value["region_states"] if item["region"] == region]
if len(matches) != 1 or matches[0]["state"] != old_state:
    raise SystemExit("bootstrap state changed concurrently")
matches[0]["state"] = new_state
descriptor, temporary = tempfile.mkstemp(
    dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
}

internal_service_presence() {
  local region="$1" output_path error_path
  output_path="${WORK_DIR}/presence-${region}.json"
  error_path="${WORK_DIR}/presence-${region}.err"
  if gc run services describe "$INTERNAL_SERVICE" \
      --region="$region" --format=json >"$output_path" 2>"$error_path"; then
    printf '%s\n' present
    return 0
  fi
  if grep -Eqi 'NOT_FOUND|not[ -]found|does not exist' "$error_path"; then
    printf '%s\n' absent
    return 0
  fi
  cat "$error_path" >&2
  echo "ERROR: could not prove internal service absence in ${region}" >&2
  return 1
}

preflight_bootstrap_provenance() {
  local region state presence rows
  if [ ! -e "$JOURNAL" ] && [ ! -L "$JOURNAL" ]; then
    # A new journal is permitted only when the entire reviewed region cohort
    # is absent. Any existing service is unrecorded and must be investigated,
    # never adopted by matching its current shape after the fact.
    for region in "${BOOTSTRAP_REGIONS[@]}"; do
      presence="$(internal_service_presence "$region")" || return 1
      [ "$presence" = absent ] || {
        echo "ERROR: unrecorded internal service exists in ${region}; refusing bootstrap" >&2
        return 1
      }
    done
    create_bootstrap_journal
  fi

  rows="$(validate_journal_and_emit_states)" || return 1
  while IFS=$'\t' read -r region state; do
    [ -n "$region" ] || continue
    presence="$(internal_service_presence "$region")" || return 1
    case "$state:$presence" in
      pending:absent|deploy_intent:absent)
        ;;
      deploy_intent:present|deployed:present|traffic_intent:present)
        verify_bootstrap_candidate "$region" \
          "${INTERNAL_SERVICE}-${REVISION_SUFFIX}" false >/dev/null || {
          echo "ERROR: recorded internal bootstrap cohort differs in ${region}" >&2
          return 1
        }
        ;;
      settled:present)
        verify_bootstrap_candidate "$region" \
          "${INTERNAL_SERVICE}-${REVISION_SUFFIX}" true >/dev/null || {
          echo "ERROR: settled internal bootstrap cohort differs in ${region}" >&2
          return 1
        }
        ;;
      pending:present)
        echo "ERROR: unrecorded internal service appeared in ${region}" >&2
        return 1
        ;;
      *)
        echo "ERROR: recorded internal bootstrap service is absent in ${region}" >&2
        return 1
        ;;
    esac
  done <<<"$rows"
}

preflight_bootstrap_provenance

for region in "${BOOTSTRAP_REGIONS[@]}"; do
  candidate="${INTERNAL_SERVICE}-${REVISION_SUFFIX}"
  journal_state="$(journal_region_state "$region")"
  [ -n "$journal_state" ] || {
    echo "ERROR: bootstrap journal omits ${region}" >&2
    exit 1
  }
  if [ "$journal_state" = pending ]; then
    journal_transition "$region" pending deploy_intent
    journal_state=deploy_intent
  fi
  if [ "$journal_state" = deploy_intent ]; then
    bootstrap_presence="$(internal_service_presence "$region")" || exit 1
    if [ "$bootstrap_presence" = absent ]; then
      log "creating private internal bootstrap ${candidate} in ${region} at its sole revision"
      # Cloud Run rejects --no-traffic on initial service creation. This
      # service is private, absent from the legacy URL map, and its first
      # revision is therefore safely created as the sole 100% target.
      if ! gc run deploy "$INTERNAL_SERVICE" \
        --region="$region" \
        --image="$IMAGE" \
        --revision-suffix="$REVISION_SUFFIX" \
        --allow-unauthenticated \
        --ingress=internal-and-cloud-load-balancing \
        --default-url \
        --service-account="$INTERNAL_RUN_SERVICE_ACCOUNT" \
        --port=8080 \
        --memory=2Gi \
        --cpu=1 \
        --concurrency=8 \
        --min-instances=2 \
        --max-instances=50 \
        --max=50 \
        --timeout=300s \
        --network="$CLOUD_RUN_NETWORK" \
        --subnet="$CLOUD_RUN_SUBNET" \
        --vpc-egress=private-ranges-only \
        --startup-probe="httpGet.path=/ready,initialDelaySeconds=0,timeoutSeconds=10,periodSeconds=10,failureThreshold=18" \
        --deploy-health-check \
        --set-env-vars="$SET_ENV_VARS" \
        --set-secrets="$SET_SECRETS" \
        --quiet >/dev/null; then
        log "internal bootstrap deploy exited non-zero; inspecting exact postconditions"
      fi
    else
      log "resuming exact recorded internal bootstrap ${candidate} in ${region}"
    fi
    verify_bootstrap_candidate "$region" "$candidate" false >/dev/null
    journal_transition "$region" deploy_intent deployed
    journal_state=deployed
  fi
  if [ "$journal_state" = deployed ]; then
    verify_bootstrap_candidate "$region" "$candidate" false >/dev/null
    journal_transition "$region" deployed traffic_intent
    journal_state=traffic_intent
  fi
  if [ "$journal_state" = traffic_intent ]; then
    if ! gc run services update-traffic "$INTERNAL_SERVICE" \
      --region="$region" --clear-tags \
      --to-revisions="${candidate}=100" --quiet >/dev/null; then
      log "internal bootstrap traffic command exited non-zero; inspecting exact postconditions"
    fi
    service_hash="$(verify_bootstrap_candidate "$region" "$candidate" true)"
    journal_transition "$region" traffic_intent settled
    journal_state=settled
  fi
  [ "$journal_state" = settled ] || {
    echo "ERROR: bootstrap journal did not settle ${region}" >&2
    exit 1
  }
  service_hash="$(verify_bootstrap_candidate "$region" "$candidate" true)"
  python3 - "$region" "$candidate" "$service_hash" <<'PY' >>"$ENTRIES_FILE"
import json
import sys
print(json.dumps({
    "region": sys.argv[1],
    "revision": sys.argv[2],
    "postcondition_sha256": sys.argv[3],
}, sort_keys=True, separators=(",", ":")))
PY
done

python3 - "$ARTIFACT" "$PROJECT_ID" "$IMAGE" "$RELEASE" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BOOTSTRAP_REGION_CSV" \
  "$INTERNAL_SERVICE" "$INTERNAL_RUN_SERVICE_ACCOUNT" \
  "$SYNTHETIC_RUN_SERVICE_ACCOUNT" "$MONITOR_REGION_CSV" \
  "$TR_SYNTHETIC_THROUGHPUT_REGION" "$TR_SYNTHETIC_IMAGE_REGION" \
  "$TR_SYNTHETIC_VIDEO_REGION" "$BOOTSTRAP_STORAGE_BACKEND" \
  "$BOOTSTRAP_ANALYTICS_READ_MODE" "$BOOTSTRAP_REQUEST_RECORD_WRITE_MODE" \
  "$BOOTSTRAP_GENERATION_RECORDS_ENABLED" \
  "$BOOTSTRAP_BIGTABLE_MIRROR_WRITES_ENABLED" \
  "$BOOTSTRAP_ANALYTICS_DUAL_READ_STARTED_AT" \
  "$BOOTSTRAP_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" \
  "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_URL" \
  "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_USER" \
  "$BOOTSTRAP_OPERATIONAL_CLICKHOUSE_DATABASE" "$ENTRIES_FILE" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

(
    artifact,
    project,
    image,
    release,
    created_at,
    raw_regions,
    service,
    account,
    synthetic_account,
    raw_monitor_regions,
    throughput_region,
    image_region,
    video_region,
    storage_backend,
    analytics_read_mode,
    request_record_write_mode,
    generation_records_enabled,
    bigtable_mirror_writes_enabled,
    analytics_dual_read_started_at,
    analytics_clickhouse_primary_started_at,
    operational_clickhouse_url,
    operational_clickhouse_user,
    operational_clickhouse_database,
    entries_path,
) = sys.argv[1:]
entries = [
    json.loads(line)
    for line in Path(entries_path).read_text(encoding="utf-8").splitlines()
    if line
]
value = {
    "schema_version": 1,
    "kind": "trusted-router-internal-bootstrap",
    "project_id": project,
    "image": image,
    "release": release,
    "created_at": created_at,
    "regions": raw_regions.split(","),
    "internal_service": service,
    "runtime_service_account": account,
    "synthetic_service_account": synthetic_account,
    "ingress": "internal-and-cloud-load-balancing",
    "default_url_enabled": True,
    "synthetic_inventory": {
        "monitor_regions": raw_monitor_regions.split(","),
        "throughput_region": throughput_region,
        "image_region": image_region,
        "video_region": video_region,
    },
    "data_mode": {
        "storage_backend": storage_backend,
        "analytics_read_mode": analytics_read_mode,
        "request_record_write_mode": request_record_write_mode,
        "generation_records_enabled": generation_records_enabled,
        "bigtable_mirror_writes_enabled": bigtable_mirror_writes_enabled,
        "analytics_dual_read_started_at": analytics_dual_read_started_at,
        "analytics_clickhouse_primary_started_at": analytics_clickhouse_primary_started_at,
        "operational_clickhouse_url": operational_clickhouse_url,
        "operational_clickhouse_user": operational_clickhouse_user,
        "operational_clickhouse_database": operational_clickhouse_database,
    },
    "services": entries,
}
path = Path(artifact)
descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
validate_artifact_and_emit_services "$IMAGE" >/dev/null
log "internal bootstrap is serving in every configured/synthetic region"
log "durable non-secret bootstrap artifact: ${ARTIFACT}"
