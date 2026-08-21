#!/usr/bin/env bash
# Phase 1: enable GCP APIs and provision Spanner + Bigtable.
# Idempotent — skip-if-exists for every step. Safe to re-run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

validate_runtime_service_accounts

GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID="${TR_GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID:-tr-google-data-manager}"
GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT="${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

ensure_runtime_service_account() {
  local service_account="$1"
  local display_name="$2"
  local account_id="${service_account%@*}"
  local describe_error=""
  if ! describe_error="$(gc iam service-accounts describe "$service_account" \
      --format='value(email)' 2>&1)"; then
    if [[ "$describe_error" != *"NOT_FOUND"* ]] &&
       [[ "$describe_error" != *"not found"* ]]; then
      echo "ERROR: cannot determine whether ${service_account} exists: ${describe_error}" >&2
      return 1
    fi
    gc iam service-accounts create "$account_id" \
      --display-name="$display_name" \
      --description="Dedicated Cloud Run runtime identity for ${display_name}" \
      --quiet
  fi

  local actual_email
  local disabled
  actual_email="$(gc iam service-accounts describe "$service_account" \
    --format='value(email)')"
  disabled="$(gc iam service-accounts describe "$service_account" \
    --format='value(disabled)')"
  if [ "$actual_email" != "$service_account" ]; then
    echo "ERROR: runtime service account post-verification failed: ${service_account}" >&2
    return 1
  fi
  case "$disabled" in
    ""|False|false|0) ;;
    *)
      echo "ERROR: runtime service account is disabled: ${service_account}" >&2
      return 1
      ;;
  esac
}

policy_roles_for_member() {
  local member="$1"
  shift
  iam_direct_binding_tokens_for_member "$member" "$@"
}

verify_only_resource_role() {
  local label="$1"
  local member="$2"
  local expected_role="$3"
  shift 3
  verify_exact_unconditional_roles "$label" "$member" "$expected_role" "$@"
}

verify_resource_role_present() {
  local label="$1"
  local member="$2"
  local role="$3"
  shift 3
  if ! iam_member_has_unconditional_role "$member" "$role" "$@"; then
    echo "ERROR: ${member} is missing desired ${label} binding ${role}" >&2
    return 1
  fi
}

remove_project_role_if_present() {
  local member="$1"
  local role="$2"
  local roles
  roles="$(policy_roles_for_member "$member" gc projects get-iam-policy "$PROJECT_ID")" || {
    echo "ERROR: cannot read project IAM while checking ${member}" >&2
    return 1
  }
  if grep -Fxq "$role" <<<"$roles"; then
    gc projects remove-iam-policy-binding "$PROJECT_ID" \
      --member="$member" \
      --role="$role" \
      --quiet >/dev/null
  fi
  roles="$(policy_roles_for_member "$member" gc projects get-iam-policy "$PROJECT_ID")" || {
    echo "ERROR: cannot post-verify project IAM for ${member}" >&2
    return 1
  }
  if grep -Fxq "$role" <<<"$roles"; then
    echo "ERROR: ${member} retains forbidden project-wide ${role}" >&2
    return 1
  fi
}

remove_spanner_role_if_present() {
  local member="$1"
  local role="$2"
  local roles
  roles="$(policy_roles_for_member "$member" gc spanner databases get-iam-policy \
    "$SPANNER_DATABASE_ID" --instance="$SPANNER_INSTANCE_ID")" || {
    echo "ERROR: cannot read Spanner database IAM for ${member}" >&2
    return 1
  }
  if grep -Fxq "$role" <<<"$roles"; then
    gc spanner databases remove-iam-policy-binding "$SPANNER_DATABASE_ID" \
      --instance="$SPANNER_INSTANCE_ID" \
      --member="$member" \
      --role="$role" \
      --quiet >/dev/null
  fi
}

remove_bigtable_role_if_present() {
  local member="$1"
  local role="$2"
  local roles
  roles="$(policy_roles_for_member "$member" gc bigtable instances get-iam-policy \
    "$BIGTABLE_INSTANCE_ID")" || {
    echo "ERROR: cannot read Bigtable instance IAM for ${member}" >&2
    return 1
  }
  if grep -Fxq "$role" <<<"$roles"; then
    gc bigtable instances remove-iam-policy-binding "$BIGTABLE_INSTANCE_ID" \
      --member="$member" \
      --role="$role" \
      --quiet >/dev/null
  fi
}

remove_kms_role_if_present() {
  local member="$1"
  local role="$2"
  local key_id="${3:-$BYOK_KMS_KEY_ID}"
  local roles
  roles="$(policy_roles_for_member "$member" gc kms keys get-iam-policy \
    "$key_id" --keyring="$KMS_KEYRING_ID" --location="$REGION")" || {
    echo "ERROR: cannot read ${key_id} KMS IAM for ${member}" >&2
    return 1
  }
  if grep -Fxq "$role" <<<"$roles"; then
    gc kms keys remove-iam-policy-binding "$key_id" \
      --keyring="$KMS_KEYRING_ID" \
      --location="$REGION" \
      --member="$member" \
      --role="$role" \
      --quiet >/dev/null
  fi
}

verify_synthetic_service_account_policy() {
  local phase="$1"
  local policy_json=""
  case "$phase" in
    preflight|post) ;;
    *) echo "ERROR: invalid synthetic service-account IAM phase ${phase}" >&2; return 2 ;;
  esac
  policy_json="$(gc iam service-accounts get-iam-policy \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || return 1
  if ! printf '%s' "$policy_json" | python3 -c '
import json
import sys

phase, deploy_member = sys.argv[1:3]
policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
expected = [{"role": "roles/iam.serviceAccountUser", "members": [deploy_member]}]
if phase == "preflight" and bindings not in ([], expected):
    raise SystemExit("synthetic service-account IAM has unsafe preexisting bindings")
if phase == "post" and bindings != expected:
    raise SystemExit("synthetic service-account IAM differs from exact deploy-only actAs")
' "$phase" "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"; then
    echo "ERROR: dedicated synthetic identity IAM is not the exact deploy-only actAs policy" >&2
    return 1
  fi
}

verify_synthetic_data_iam_empty() {
  local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"
  local key_id=""
  verify_exact_unconditional_roles \
    "synthetic identity project" "$member" "" \
    gc projects get-iam-policy "$PROJECT_ID" || return 1
  verify_identity_resource_manager_ancestors_empty \
    "synthetic identity" "$SYNTHETIC_RUN_SERVICE_ACCOUNT" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Spanner instance" "$member" "" \
    gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Spanner database" "$member" "" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Bigtable instance" "$member" "" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Bigtable generation table" "$member" "" \
    gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity KMS key ring" "$member" "" \
    gc kms keyrings get-iam-policy "$KMS_KEYRING_ID" \
    --location="$REGION" || return 1
  for key_id in "$BYOK_KMS_KEY_ID" "$GOOGLE_ADS_KMS_KEY_ID"; do
    verify_exact_unconditional_roles \
      "synthetic identity KMS key ${key_id}" "$member" "" \
      gc kms keys get-iam-policy "$key_id" \
      --keyring="$KMS_KEYRING_ID" --location="$REGION" || return 1
  done
}

verify_legacy_runtime_retirement_ready() {
  if [ "$CONSOLE_SERVICE" = "$LEGACY_CONSOLE_SERVICE" ]; then
    echo "ERROR: cannot retire legacy IAM; split console ${CONSOLE_SERVICE} aliases the legacy monolith" >&2
    return 1
  fi
  local -a services=(
    "$PUBLIC_SERVICE"
    "$ACTIONS_SERVICE"
    "$CONSOLE_SERVICE"
    "$CHAT_SERVICE"
    "$WEBHOOKS_SERVICE"
    "$INTERNAL_SERVICE"
  )
  local -a surfaces=(public actions console chat webhooks internal)
  local -a identities=(
    "$PUBLIC_RUN_SERVICE_ACCOUNT"
    "$ACTIONS_RUN_SERVICE_ACCOUNT"
    "$CONSOLE_RUN_SERVICE_ACCOUNT"
    "$CHAT_RUN_SERVICE_ACCOUNT"
    "$WEBHOOKS_RUN_SERVICE_ACCOUNT"
    "$INTERNAL_RUN_SERVICE_ACCOUNT"
  )
  local -a regions
  local region
  local index
  local service_json
  local revisions
  local revision
  local revision_json
  IFS=',' read -ra regions <<<"$TR_CONTROL_PLANE_REGIONS"
  for region in "${regions[@]}"; do
    [ -n "$region" ] || continue
    for index in 0 1 2 3 4 5; do
      service_json="$(gc run services describe "${services[$index]}" \
        --region="$region" --format=json)" || {
        echo "ERROR: cannot retire legacy IAM; ${services[$index]} is absent in ${region}" >&2
        return 1
      }
      revisions="$(printf '%s' "$service_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
conditions = data.get("status", {}).get("conditions", [])
if not any(
    item.get("type") == "Ready"
    and str(item.get("status", "")).casefold() == "true"
    for item in conditions
):
    raise SystemExit("service is not Ready")
traffic = [
    item
    for item in data.get("status", {}).get("traffic", [])
    if int(item.get("percent", 0) or 0) > 0
]
if not traffic or sum(int(item.get("percent", 0) or 0) for item in traffic) != 100:
    raise SystemExit("serving traffic is absent or does not total 100 percent")
for item in traffic:
    revision = item.get("revisionName")
    if not revision:
        raise SystemExit("serving traffic does not name an immutable revision")
    print(revision)
')" || {
        echo "ERROR: cannot retire legacy IAM; ${services[$index]} traffic is unsafe in ${region}" >&2
        return 1
      }
      for revision in $revisions; do
        revision_json="$(gc run revisions describe "$revision" \
          --region="$region" --format=json)" || {
          echo "ERROR: cannot inspect serving revision ${revision} in ${region}" >&2
          return 1
        }
        if ! printf '%s' "$revision_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_surface, expected_identity = sys.argv[1:3]
spec = data.get("spec", {})
actual_identity = spec.get("serviceAccountName")
if actual_identity != expected_identity:
    raise SystemExit(
        f"service account is {actual_identity!r}, expected {expected_identity!r}"
    )
containers = spec.get("containers", [])
if len(containers) != 1:
    raise SystemExit("revision must have exactly one container")
surface = next(
    (
        item.get("value")
        for item in containers[0].get("env", [])
        if item.get("name") == "TR_SERVICE_SURFACE"
    ),
    None,
)
if surface != expected_surface:
    raise SystemExit(f"surface is {surface!r}, expected {expected_surface!r}")
' "${surfaces[$index]}" "${identities[$index]}"; then
          echo "ERROR: cannot retire legacy IAM; serving revision ${revision} is not isolated" >&2
          return 1
        fi
      done
    done
  done
}

verify_identity_resource_manager_ancestors_empty() {
  local label="$1"
  local service_account="$2"
  local member="serviceAccount:${service_account}"
  local ancestors_json
  local ancestors
  local ancestor_type
  local ancestor_id

  ancestors_json="$(gc projects get-ancestors "$PROJECT_ID" --format=json)" || {
    echo "ERROR: cannot inventory Resource Manager ancestors for ${label}" >&2
    return 1
  }
  ancestors="$(printf '%s' "$ancestors_json" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("project ancestor inventory is not a list")
seen = set()
projects = []
organizations = []
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("project ancestor entry is not an object")
    kind = str(item.get("type") or "").casefold()
    identifier = str(item.get("id") or "")
    if kind not in {"project", "folder", "organization"} or not identifier:
        raise SystemExit("project ancestor entry has an invalid type or id")
    key = (kind, identifier)
    if key in seen:
        raise SystemExit("project ancestor inventory contains a duplicate")
    seen.add(key)
    if kind == "project":
        projects.append(identifier)
    elif kind == "organization":
        organizations.append(identifier)
    print(kind, identifier, sep="\t")
if len(projects) != 1 or projects[0] not in set(sys.argv[1:3]):
    raise SystemExit("project ancestor inventory does not identify this project")
if len(organizations) > 1:
    raise SystemExit("project ancestor inventory has multiple organizations")
' "$PROJECT_ID" "$PROJECT_NUMBER")" || {
    echo "ERROR: malformed Resource Manager ancestor inventory for ${label}" >&2
    return 1
  }

  while IFS=$'\t' read -r ancestor_type ancestor_id; do
    [ -n "$ancestor_id" ] || continue
    case "$ancestor_type" in
      project) ;;
      folder)
        verify_exact_unconditional_roles \
          "${label} inherited folder ${ancestor_id}" "$member" "" \
          gc resource-manager folders get-iam-policy "$ancestor_id" \
          --format=json || return 1
        ;;
      organization)
        verify_exact_unconditional_roles \
          "${label} inherited organization ${ancestor_id}" "$member" "" \
          gc organizations get-iam-policy "$ancestor_id" \
          --format=json || return 1
        ;;
      *)
        echo "ERROR: unsupported Resource Manager ancestor ${ancestor_type}/${ancestor_id}" >&2
        return 1
        ;;
    esac
  done <<<"$ancestors"
}

verify_synthetic_retirement_identity_ready() {
  local expected_identity="tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com"
  local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"
  local account_json
  local account_policy
  local key_id

  if [ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" != "$expected_identity" ]; then
    echo "ERROR: legacy IAM retirement requires the canonical dedicated synthetic identity ${expected_identity}" >&2
    return 1
  fi
  if [ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" = "$RUN_SERVICE_ACCOUNT" ]; then
    echo "ERROR: legacy IAM retirement cannot reuse RUN_SERVICE_ACCOUNT for synthetic Jobs" >&2
    return 1
  fi

  account_json="$(gc iam service-accounts describe \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || {
    echo "ERROR: dedicated synthetic identity is not provisioned; separate narrow IAM approval is required" >&2
    return 1
  }
  if ! printf '%s' "$account_json" | python3 -c '
import json
import sys

account = json.load(sys.stdin)
if account.get("email") != sys.argv[1] or account.get("disabled", False) is not False:
    raise SystemExit("synthetic identity is missing, disabled, or renamed")
' "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; then
    echo "ERROR: dedicated synthetic identity is missing, disabled, or renamed" >&2
    return 1
  fi

  account_policy="$(gc iam service-accounts get-iam-policy \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || {
    echo "ERROR: cannot read dedicated synthetic identity IAM" >&2
    return 1
  }
  if ! printf '%s' "$account_policy" | python3 -c '
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
if (binding.get("members") or []) != [sys.argv[1]]:
    raise SystemExit("synthetic identity actAs member inventory differs")
' "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"; then
    echo "ERROR: dedicated synthetic identity IAM is not the narrow reviewed policy" >&2
    return 1
  fi

  verify_exact_unconditional_roles \
    "synthetic identity project" "$member" "" \
    gc projects get-iam-policy "$PROJECT_ID" || return 1
  verify_identity_resource_manager_ancestors_empty \
    "synthetic identity" "$SYNTHETIC_RUN_SERVICE_ACCOUNT" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Spanner instance" "$member" "" \
    gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Spanner database" "$member" "" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Bigtable instance" "$member" "" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity Bigtable generation table" "$member" "" \
    gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" || return 1
  verify_exact_unconditional_roles \
    "synthetic identity KMS key ring" "$member" "" \
    gc kms keyrings get-iam-policy "$KMS_KEYRING_ID" \
    --location="$REGION" || return 1
  for key_id in "$BYOK_KMS_KEY_ID" "$GOOGLE_ADS_KMS_KEY_ID"; do
    verify_exact_unconditional_roles \
      "synthetic identity KMS key ${key_id}" "$member" "" \
      gc kms keys get-iam-policy "$key_id" \
      --keyring="$KMS_KEYRING_ID" --location="$REGION" || return 1
  done
}

verify_legacy_synthetic_secret_access_ready() {
  local expected_names="|trustedrouter-observer-internal-token|trustedrouter-synthetic-monitor-api-key|"
  local seen_names="|"
  local inventory_json
  local inventory
  local secret_name
  local policy_json
  local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"

  inventory_json="$(gc secrets list --format=json)" || {
    echo "ERROR: cannot inventory every Secret Manager resource before legacy IAM retirement" >&2
    return 1
  }
  inventory="$(printf '%s' "$inventory_json" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("secret inventory is not a list")
names = []
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("secret inventory entry is not an object")
    raw_name = item.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        raise SystemExit("secret inventory entry has no name")
    name = raw_name.rstrip("/").rsplit("/", 1)[-1]
    if not name:
        raise SystemExit("secret inventory entry has an invalid name")
    names.append(name)
if len(names) != len(set(names)):
    raise SystemExit("secret inventory contains duplicate names")
print("\n".join(sorted(names)))
')" || {
    echo "ERROR: malformed Secret Manager inventory before legacy IAM retirement" >&2
    return 1
  }

  while IFS= read -r secret_name; do
    [ -n "$secret_name" ] || continue
    case "$expected_names" in
      *"|${secret_name}|"*)
        seen_names="${seen_names}${secret_name}|"
        ;;
      *)
        verify_exact_unconditional_roles \
          "non-synthetic secret ${secret_name}" "$member" "" \
          gc secrets get-iam-policy "$secret_name" || return 1
        continue
        ;;
    esac
    policy_json="$(gc secrets get-iam-policy "$secret_name" --format=json)" || {
      echo "ERROR: cannot read synthetic secret IAM for ${secret_name}" >&2
      return 1
    }
    if ! printf '%s' "$policy_json" | python3 -c '
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
    raise SystemExit("synthetic secret IAM consumer inventory differs")
' "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
      "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"; then
      echo "ERROR: ${secret_name} must have exactly the internal and dedicated synthetic consumers" >&2
      return 1
    fi
  done <<<"$inventory"

  for secret_name in \
    trustedrouter-observer-internal-token \
    trustedrouter-synthetic-monitor-api-key; do
    case "$seen_names" in
      *"|${secret_name}|"*) ;;
      *)
        echo "ERROR: required synthetic secret ${secret_name} is absent from the project inventory" >&2
        return 1
        ;;
    esac
  done
}

synthetic_job_inventory_lines() {
  local -a monitor_regions
  local monitor_region
  IFS=',' read -ra monitor_regions <<<"$TR_SYNTHETIC_MONITOR_REGIONS"
  for monitor_region in "${monitor_regions[@]}"; do
    printf '%s\t%s\t%s\n' \
      "$monitor_region" \
      "trusted-router-synthetic-${monitor_region//[^a-zA-Z0-9-]/-}" \
      "trusted-router-synthetic-${monitor_region//[^a-zA-Z0-9-]/-}-every-three-minutes"
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

cloud_run_inventory_lines() {
  local kind="$1"
  local resources_json
  resources_json="$(gc run "$kind" list --format=json)" || {
    echo "ERROR: cannot list Cloud Run ${kind} in all regions" >&2
    return 1
  }
  printf '%s' "$resources_json" | python3 -c '
import json
import sys

kind = sys.argv[1]
items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit(f"Cloud Run {kind} inventory is not a list")
seen = set()
for item in items:
    metadata = item.get("metadata", {}) or {}
    raw_name = metadata.get("name") or item.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        raise SystemExit(f"Cloud Run {kind} inventory entry has no name")
    name = raw_name.rsplit("/", 1)[-1]
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    region = (
        labels.get("cloud.googleapis.com/location")
        or annotations.get("run.googleapis.com/region")
        or item.get("location")
    )
    if not region and "/locations/" in raw_name:
        region = raw_name.split("/locations/", 1)[1].split("/", 1)[0]
    if not isinstance(region, str) or not region:
        raise SystemExit(f"Cloud Run {kind} inventory cannot resolve region for {name}")
    key = (region, name)
    if key in seen:
        raise SystemExit(f"Cloud Run {kind} inventory repeats {region}/{name}")
    seen.add(key)
    print(region, name, sep="\t")
' "$kind"
}

cloud_run_job_service_account() {
  local job_name="$1"
  local region="$2"
  local job_json
  job_json="$(gc run jobs describe "$job_name" --region="$region" --format=json)" || {
    echo "ERROR: cannot inspect Cloud Run job ${region}/${job_name}" >&2
    return 1
  }
  printf '%s' "$job_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
root = data.get("spec", {}).get("template", {})
values = []
def visit(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"serviceAccount", "serviceAccountName"} and isinstance(child, str):
                values.append(child)
            elif isinstance(child, (dict, list)):
                visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)
visit(root)
values = sorted(set(values))
if len(values) != 1:
    raise SystemExit(f"job execution template exposes {len(values)} service accounts")
print(values[0])
'
}

verify_legacy_cloud_run_service_inventory() {
  local inventory
  local region
  local service_name
  local service_json
  local revisions
  local revision
  local revision_json
  inventory="$(cloud_run_inventory_lines services)" || return 1
  while IFS=$'\t' read -r region service_name; do
    [ -n "$service_name" ] || continue
    service_json="$(gc run services describe "$service_name" \
      --region="$region" --format=json)" || {
      echo "ERROR: cannot inspect inventoried Cloud Run service ${region}/${service_name}" >&2
      return 1
    }
    revisions="$(printf '%s' "$service_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
legacy = sys.argv[1]
template = data.get("spec", {}).get("template", {})
pod_spec = template.get("spec", template)
if pod_spec.get("serviceAccountName") == legacy or pod_spec.get("serviceAccount") == legacy:
    raise SystemExit("service template still names the legacy runtime identity")
for item in data.get("status", {}).get("traffic", []) or []:
    if int(item.get("percent", 0) or 0) <= 0:
        continue
    revision = item.get("revisionName")
    if not revision:
        raise SystemExit("serving traffic does not name an immutable revision")
    print(revision)
' "$RUN_SERVICE_ACCOUNT")" || {
      echo "ERROR: legacy IAM retirement inventory rejected ${region}/${service_name}" >&2
      return 1
    }
    while IFS= read -r revision; do
      [ -n "$revision" ] || continue
      revision_json="$(gc run revisions describe "$revision" \
        --region="$region" --format=json)" || {
        echo "ERROR: cannot inspect serving revision ${region}/${revision}" >&2
        return 1
      }
      if ! printf '%s' "$revision_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
spec = data.get("spec", {})
identity = spec.get("serviceAccountName") or spec.get("serviceAccount")
if identity == sys.argv[1]:
    raise SystemExit("serving revision still names the legacy runtime identity")
' "$RUN_SERVICE_ACCOUNT"; then
        echo "ERROR: cannot retire legacy IAM while ${region}/${revision} still serves" >&2
        return 1
      fi
    done <<<"$revisions"
  done <<<"$inventory"
}

verify_legacy_synthetic_jobs_ready() {
  local expected_inventory
  local actual_inventory
  local expected_keys="|"
  local seen_keys="|"
  local region
  local job_name
  local scheduler_name
  local key
  local identity
  local job_policy_json
  local scheduler_json
  local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"
  expected_inventory="$(synthetic_job_inventory_lines)" || return 1
  while IFS=$'\t' read -r region job_name scheduler_name; do
    [ -n "$job_name" ] || continue
    key="${region}/${job_name}"
    case "$expected_keys" in
      *"|${key}|"*)
        echo "ERROR: synthetic job inventory repeats ${key}" >&2
        return 1
        ;;
    esac
    expected_keys="${expected_keys}${key}|"
  done <<<"$expected_inventory"

  actual_inventory="$(cloud_run_inventory_lines jobs)" || return 1
  while IFS=$'\t' read -r region job_name; do
    [ -n "$job_name" ] || continue
    key="${region}/${job_name}"
    identity="$(cloud_run_job_service_account "$job_name" "$region")" || return 1
    if [ "$identity" = "$RUN_SERVICE_ACCOUNT" ]; then
      echo "ERROR: Cloud Run job ${key} still uses legacy identity ${RUN_SERVICE_ACCOUNT}" >&2
      return 1
    fi
    case "$expected_keys" in
      *"|${key}|"*)
        if [ "$identity" != "$SYNTHETIC_RUN_SERVICE_ACCOUNT" ]; then
          echo "ERROR: canonical synthetic job ${key} uses ${identity}, expected ${SYNTHETIC_RUN_SERVICE_ACCOUNT}" >&2
          return 1
        fi
        seen_keys="${seen_keys}${key}|"
        ;;
    esac
  done <<<"$actual_inventory"

  while IFS=$'\t' read -r region job_name scheduler_name; do
    [ -n "$job_name" ] || continue
    key="${region}/${job_name}"
    case "$seen_keys" in
      *"|${key}|"*) ;;
      *)
        echo "ERROR: canonical synthetic job ${key} is absent from the all-region inventory" >&2
        return 1
        ;;
    esac
    job_policy_json="$(gc run jobs get-iam-policy "$job_name" \
      --region="$region" --format=json)" || {
      echo "ERROR: cannot read canonical synthetic job IAM for ${key}" >&2
      return 1
    }
    if ! printf '%s' "$job_policy_json" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic job IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/run.invoker":
    raise SystemExit("synthetic job IAM role differs")
if binding.get("condition") is not None:
    raise SystemExit("synthetic job invoker grant is conditional")
members = binding.get("members") or []
if members != [sys.argv[1]]:
    raise SystemExit("synthetic job invoker inventory differs")
' "$member"; then
      echo "ERROR: canonical synthetic job ${key} must have only the dedicated synthetic invoker" >&2
      return 1
    fi
    scheduler_json="$(gc scheduler jobs describe "$scheduler_name" \
      --location="$region" --format=json)" || {
      echo "ERROR: canonical synthetic scheduler ${region}/${scheduler_name} is absent" >&2
      return 1
    }
    if ! printf '%s' "$scheduler_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_identity, expected_uri = sys.argv[1:3]
target = data.get("httpTarget", {}) or {}
oauth = target.get("oauthToken", {}) or {}
if oauth.get("serviceAccountEmail") != expected_identity:
    raise SystemExit("scheduler OAuth identity is wrong")
if target.get("uri") != expected_uri:
    raise SystemExit("scheduler run URI is wrong")
if str(target.get("httpMethod", "")).upper() != "POST":
    raise SystemExit("scheduler HTTP method is not POST")
if str(data.get("state", "")).upper() != "ENABLED":
    raise SystemExit("scheduler is not enabled")
' "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
      "https://${region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"; then
      echo "ERROR: canonical synthetic scheduler ${region}/${scheduler_name} is unsafe" >&2
      return 1
    fi
  done <<<"$expected_inventory"
}

describe_iam_role_definition() {
  local role="$1"
  local scope
  local role_id
  case "$role" in
    roles/*)
      gc iam roles describe "$role" --format=json
      ;;
    projects/*/roles/*)
      scope="${role#projects/}"
      scope="${scope%%/roles/*}"
      role_id="${role##*/}"
      if [ "$scope" != "$PROJECT_ID" ]; then
        echo "ERROR: project policy references custom role from unexpected project ${scope}" >&2
        return 1
      fi
      gc iam roles describe "$role_id" --format=json
      ;;
    organizations/*/roles/*)
      scope="${role#organizations/}"
      scope="${scope%%/roles/*}"
      role_id="${role##*/}"
      gcloud --billing-project "$PROJECT_ID" iam roles describe "$role_id" \
        --organization="$scope" --format=json
      ;;
    *)
      echo "ERROR: cannot resolve IAM role definition ${role}" >&2
      return 1
      ;;
  esac
}

verify_deploy_identity_has_no_project_data_roles() {
  local member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"
  local tokens
  local token
  local role
  local role_json
  verify_no_direct_roles \
    "project" \
    "$member" \
    "roles/secretmanager.secretAccessor,roles/secretmanager.admin,roles/spanner.databaseReader,roles/spanner.databaseUser,roles/bigtable.reader,roles/bigtable.user,roles/aiplatform.user,roles/cloudkms.cryptoKeyEncrypter,roles/cloudkms.cryptoKeyDecrypter,roles/cloudkms.cryptoKeyEncrypterDecrypter,roles/iam.serviceAccountUser,roles/iam.serviceAccountTokenCreator,roles/editor,roles/owner" \
    gc projects get-iam-policy "$PROJECT_ID"
  tokens="$(iam_direct_binding_tokens_for_member "$member" \
    gc projects get-iam-policy "$PROJECT_ID")" || {
    echo "ERROR: cannot inventory deploy identity project roles" >&2
    return 1
  }
  while IFS= read -r token; do
    [ -n "$token" ] || continue
    role="${token#conditional:}"
    role_json="$(describe_iam_role_definition "$role")" || {
      echo "ERROR: cannot audit permissions in deploy identity role ${role}" >&2
      return 1
    }
    if ! printf '%s' "$role_json" | python3 -c '
import json
import sys

role = json.load(sys.stdin)
forbidden = {
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.getAccessToken",
    "secretmanager.versions.access",
    "spanner.databases.read",
    "spanner.databases.write",
    "spanner.sessions.create",
    "bigtable.tables.readRows",
    "bigtable.tables.mutateRows",
    "cloudkms.cryptoKeyVersions.useToDecrypt",
    "cloudkms.cryptoKeyVersions.useToEncrypt",
    "aiplatform.endpoints.predict",
}
present = sorted(forbidden.intersection(role.get("includedPermissions", []) or []))
if present:
    raise SystemExit("forbidden permissions: " + ",".join(present))
'; then
      echo "ERROR: deploy identity role ${role} contains actAs, token-minting, or runtime data permissions" >&2
      return 1
    fi
  done <<<"$tokens"
}

preflight_identity_iam_removal_targets() {
  local label="$1"
  local service_account="$2"
  local member="serviceAccount:${service_account}"
  local project_roles="roles/secretmanager.secretAccessor,roles/spanner.databaseReader,roles/spanner.databaseUser,roles/bigtable.reader,roles/bigtable.user,roles/aiplatform.user,roles/run.developer,roles/serviceusage.serviceUsageConsumer,roles/editor,roles/owner"
  local database_roles="roles/spanner.databaseReader,roles/spanner.databaseUser"
  local bigtable_roles="roles/bigtable.reader,roles/bigtable.user"
  local kms_roles="roles/cloudkms.cryptoKeyEncrypter,roles/cloudkms.cryptoKeyDecrypter,roles/cloudkms.cryptoKeyEncrypterDecrypter"
  local key_id

  preflight_reconcilable_direct_roles \
    "${label} project" "$member" "$project_roles" \
    gc projects get-iam-policy "$PROJECT_ID"
  verify_exact_unconditional_roles \
    "${label} Spanner instance" "$member" "" \
    gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID"
  preflight_reconcilable_direct_roles \
    "${label} Spanner database" "$member" "$database_roles" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
  preflight_reconcilable_direct_roles \
    "${label} Bigtable instance" "$member" "$bigtable_roles" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
  verify_exact_unconditional_roles \
    "${label} Bigtable generation table" "$member" "" \
    gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID"
  verify_exact_unconditional_roles \
    "${label} KMS key ring" "$member" "" \
    gc kms keyrings get-iam-policy "$KMS_KEYRING_ID" --location="$REGION"
  for key_id in "$BYOK_KMS_KEY_ID" "$GOOGLE_ADS_KMS_KEY_ID"; do
    preflight_reconcilable_direct_roles \
      "${label} KMS key ${key_id}" "$member" "$kms_roles" \
      gc kms keys get-iam-policy "$key_id" \
      --keyring="$KMS_KEYRING_ID" --location="$REGION"
  done
}

verify_identity_ancestor_scopes_empty() {
  local label="$1"
  local service_account="$2"
  local member="serviceAccount:${service_account}"
  verify_exact_unconditional_roles \
    "${label} Spanner instance" "$member" "" \
    gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID"
  verify_exact_unconditional_roles \
    "${label} Bigtable generation table" "$member" "" \
    gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID"
  verify_exact_unconditional_roles \
    "${label} KMS key ring" "$member" "" \
    gc kms keyrings get-iam-policy "$KMS_KEYRING_ID" --location="$REGION"
}

verify_deploy_actas_inventory() {
  local google_data_manager_service_account="$1"
  local phase="${2:-post}"
  local deploy_member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"
  local allowed="|"
  local seen="|"
  local accounts_json
  local accounts
  local service_account
  local expected
  local bindings
  local runtime_policy

  case "$phase" in
    preflight|post) ;;
    *)
      echo "ERROR: unknown deploy actAs audit phase ${phase}" >&2
      return 1
      ;;
  esac

  for service_account in \
    "${RUNTIME_SERVICE_ACCOUNTS[@]}" \
    "$google_data_manager_service_account" \
    "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; do
    allowed="${allowed}${service_account}|"
  done
  accounts_json="$(gc iam service-accounts list --format=json)" || {
    echo "ERROR: cannot inventory project service accounts for deploy actAs audit" >&2
    return 1
  }
  accounts="$(printf '%s' "$accounts_json" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("service-account inventory is not a list")
emails = []
for item in items:
    email = item.get("email")
    if not isinstance(email, str) or not email:
        raise SystemExit("service-account inventory entry has no email")
    emails.append(email)
if len(set(emails)) != len(emails):
    raise SystemExit("service-account inventory contains duplicate emails")
print("\n".join(sorted(emails)))
')" || {
    echo "ERROR: malformed project service-account inventory" >&2
    return 1
  }
  while IFS= read -r service_account; do
    [ -n "$service_account" ] || continue
    case "$allowed" in
      *"|${service_account}|"*)
        expected="roles/iam.serviceAccountUser"
        seen="${seen}${service_account}|"
        ;;
      *) expected="" ;;
    esac
    if is_runtime_service_account "$service_account"; then
      runtime_policy="$(gc iam service-accounts get-iam-policy \
        "$service_account" --format=json)" || {
        echo "ERROR: cannot read complete runtime service-account policy ${service_account}" >&2
        return 1
      }
      if ! printf '%s' "$runtime_policy" | \
          verify_runtime_service_account_policy_json \
            "$service_account" "$phase"; then
        echo "ERROR: unsafe direct IAM policy on runtime service account ${service_account}" >&2
        return 1
      fi
      continue
    fi
    if [ "$phase" = "preflight" ] && [ -n "$expected" ]; then
      bindings="$(iam_direct_binding_tokens_for_member "$deploy_member" \
        gc iam service-accounts get-iam-policy "$service_account")" || {
        echo "ERROR: cannot preflight deploy identity access on ${service_account}" >&2
        return 1
      }
      if [ -n "$bindings" ] && [ "$bindings" != "$expected" ]; then
        echo "ERROR: deploy identity has unsafe preexisting bindings on ${service_account}: ${bindings}" >&2
        return 1
      fi
    else
      verify_exact_unconditional_roles \
        "deploy identity access on service account ${service_account}" \
        "$deploy_member" "$expected" \
        gc iam service-accounts get-iam-policy "$service_account"
    fi
  done <<<"$accounts"
  for service_account in \
    "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
    case "$seen" in
      *"|${service_account}|"*) ;;
      *)
        echo "ERROR: actAs target ${service_account} is absent from service-account inventory" >&2
        return 1
        ;;
    esac
  done
  case "$seen" in
    *"|${SYNTHETIC_RUN_SERVICE_ACCOUNT}|"*) ;;
    *)
      echo "ERROR: approved synthetic actAs target ${SYNTHETIC_RUN_SERVICE_ACCOUNT} is absent from service-account inventory" >&2
      return 1
      ;;
  esac
  if [ "$phase" = "post" ]; then
    case "$seen" in
      *"|${google_data_manager_service_account}|"*) ;;
      *)
        echo "ERROR: actAs target ${google_data_manager_service_account} is absent from service-account inventory" >&2
        return 1
        ;;
    esac
  fi
  verify_deploy_identity_has_no_project_data_roles
}

log "enabling required GCP APIs"
gc services enable \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudkms.googleapis.com \
  datamanager.googleapis.com \
  spanner.googleapis.com \
  bigtableadmin.googleapis.com \
  cloudbuild.googleapis.com

log "ensuring six dedicated Cloud Run runtime identities and synthetic Job identity"
ensure_runtime_service_account "$PUBLIC_RUN_SERVICE_ACCOUNT" "TrustedRouter public web"
ensure_runtime_service_account "$ACTIONS_RUN_SERVICE_ACCOUNT" "TrustedRouter anonymous actions"
ensure_runtime_service_account "$CONSOLE_RUN_SERVICE_ACCOUNT" "TrustedRouter console"
ensure_runtime_service_account "$CHAT_RUN_SERVICE_ACCOUNT" "TrustedRouter chat proxy"
ensure_runtime_service_account "$WEBHOOKS_RUN_SERVICE_ACCOUNT" "TrustedRouter webhooks"
ensure_runtime_service_account "$INTERNAL_RUN_SERVICE_ACCOUNT" "TrustedRouter internal billing"
ensure_runtime_service_account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" "TrustedRouter synthetic jobs"

log "ensuring Spanner instance/database"
if ! gc spanner instances describe "$SPANNER_INSTANCE_ID" >/dev/null 2>&1; then
  gc spanner instances create "$SPANNER_INSTANCE_ID" \
    --config="$SPANNER_CONFIG" \
    --edition="$SPANNER_EDITION" \
    --description="TrustedRouter ledger" \
    --processing-units="$SPANNER_PROCESSING_UNITS"
fi
if ! gc spanner databases describe "$SPANNER_DATABASE_ID" --instance="$SPANNER_INSTANCE_ID" >/dev/null 2>&1; then
  gc spanner databases create "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --database-dialect=GOOGLE_STANDARD_SQL \
    --ddl='CREATE TABLE tr_entities (kind STRING(64) NOT NULL, id STRING(512) NOT NULL, body STRING(MAX) NOT NULL, updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)) PRIMARY KEY (kind, id)'
fi
if [ "$(gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(versionRetentionPeriod)')" != "7d" ]; then
  gc spanner databases ddl update "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --ddl="ALTER DATABASE \`${SPANNER_DATABASE_ID}\` SET OPTIONS (version_retention_period = '7d')"
fi
if [ "$(gc spanner databases describe "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" --format='value(enableDropProtection)')" != "True" ]; then
  gc spanner databases update "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --enable-drop-protection \
    --quiet
fi

log "ensuring Bigtable instance/table"
if ! gc bigtable instances describe "$BIGTABLE_INSTANCE_ID" >/dev/null 2>&1; then
  gc bigtable instances create "$BIGTABLE_INSTANCE_ID" \
    --display-name="TrustedRouter logs" \
    --instance-type="$BIGTABLE_INSTANCE_TYPE" \
    --cluster="$BIGTABLE_CLUSTER_ID" \
    --cluster-zone="${ZONE:-${REGION}-a}" \
    --cluster-num-nodes=1
fi
if ! gc bigtable instances tables describe "$BIGTABLE_GENERATION_TABLE" --instance="$BIGTABLE_INSTANCE_ID" >/dev/null 2>&1; then
  gc bigtable instances tables create "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" \
    --column-families=m
fi

# Retention migrations need only table-schema reads and column-family updates.
# Keep that capability table-scoped and separate from row-data access.
BIGTABLE_SCHEMA_ROLE_ID="${TR_BIGTABLE_SCHEMA_ROLE_ID:-trustedRouterBigtableSchemaManager}"
BIGTABLE_SCHEMA_ROLE="projects/${PROJECT_ID}/roles/${BIGTABLE_SCHEMA_ROLE_ID}"
OPS_SERVICE_ACCOUNT="${TR_OPS_SERVICE_ACCOUNT:-tr-ops-local@${PROJECT_ID}.iam.gserviceaccount.com}"
if ! gc iam roles describe "$BIGTABLE_SCHEMA_ROLE_ID" >/dev/null 2>&1; then
  gc iam roles create "$BIGTABLE_SCHEMA_ROLE_ID" \
    --title="TrustedRouter Bigtable Schema Manager" \
    --description="May read table schema and update column-family GC policies; no row data access." \
    --permissions=bigtable.tables.get,bigtable.tables.update \
    --stage=GA \
    --quiet
fi
for service_account in "$DEPLOY_SERVICE_ACCOUNT" "$OPS_SERVICE_ACCOUNT"; do
  gc bigtable tables add-iam-policy-binding "$BIGTABLE_GENERATION_TABLE" \
    --instance="$BIGTABLE_INSTANCE_ID" \
    --member="serviceAccount:${service_account}" \
    --role="$BIGTABLE_SCHEMA_ROLE" \
    --quiet >/dev/null
done

log "ensuring BYOK envelope KMS key"
if ! gc kms keyrings describe "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keyrings create "$KMS_KEYRING_ID" --location "$REGION"
fi
if ! gc kms keys describe "$BYOK_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keys create "$BYOK_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" \
    --location "$REGION" \
    --purpose=encryption
fi

# Google Ads click identifiers use a separate envelope key. The conversion
# worker can unwrap this key but never receives permission to unwrap BYOK keys.
if ! gc kms keys describe "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" --location "$REGION" >/dev/null 2>&1; then
  gc kms keys create "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring "$KMS_KEYRING_ID" \
    --location "$REGION" \
    --purpose=encryption
fi

# Inventory every direct binding that the reconciliation code can touch before
# its first removal. This makes drift a no-mutation failure instead of leaving
# the first few identities partially stripped when a later target surprises us.
log "preflighting all runtime IAM removal targets and direct ancestor policies"
if [ "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" = "1" ]; then
  verify_synthetic_retirement_identity_ready
fi
verify_deploy_actas_inventory "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" preflight
verify_synthetic_service_account_policy preflight
verify_synthetic_data_iam_empty
for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  preflight_identity_iam_removal_targets "split runtime" "$service_account"
done
if [ "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" = "1" ]; then
  log "preflighting complete Cloud Run inventory before legacy IAM retirement"
  verify_legacy_runtime_retirement_ready
  verify_legacy_cloud_run_service_inventory
  verify_legacy_synthetic_secret_access_ready
  verify_legacy_synthetic_jobs_ready
  preflight_identity_iam_removal_targets "legacy runtime" "$RUN_SERVICE_ACCOUNT"
fi

# Install and verify every desired six-runtime grant before removing a live
# obsolete grant on the same resource. IAM propagation can lag a successful
# setIamPolicy response, so a remove-then-add transition creates a real serving
# outage even when the final matrix is correct.
log "granting and verifying desired runtime IAM before obsolete-role cleanup"
gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyEncrypter" \
  --quiet >/dev/null
verify_resource_role_present \
  "Google Ads KMS key" "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyEncrypter" \
  gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"

for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$CONSOLE_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  member="serviceAccount:${service_account}"
  ensure_project_role "$member" "roles/serviceusage.serviceUsageConsumer"
  verify_resource_role_present \
    "project" "$member" "roles/serviceusage.serviceUsageConsumer" \
    gc projects get-iam-policy "$PROJECT_ID"
done

for service_account in "$PUBLIC_RUN_SERVICE_ACCOUNT" "$CHAT_RUN_SERVICE_ACCOUNT"; do
  member="serviceAccount:${service_account}"
  gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --member="$member" \
    --role="roles/spanner.databaseReader" \
    --quiet >/dev/null
  verify_resource_role_present \
    "Spanner database" "$member" "roles/spanner.databaseReader" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
done
for service_account in \
  "$CONSOLE_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  member="serviceAccount:${service_account}"
  gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --member="$member" \
    --role="roles/spanner.databaseUser" \
    --quiet >/dev/null
  verify_resource_role_present \
    "Spanner database" "$member" "roles/spanner.databaseUser" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
done

for service_account in "$PUBLIC_RUN_SERVICE_ACCOUNT" "$CONSOLE_RUN_SERVICE_ACCOUNT"; do
  member="serviceAccount:${service_account}"
  gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID" \
    --member="$member" \
    --role="roles/bigtable.reader" \
    --quiet >/dev/null
  verify_resource_role_present \
    "Bigtable instance" "$member" "roles/bigtable.reader" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
done
member="serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}"
gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID" \
  --member="$member" \
  --role="roles/bigtable.user" \
  --quiet >/dev/null
verify_resource_role_present \
  "Bigtable instance" "$member" "roles/bigtable.user" \
  gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"

gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" \
  --location="$REGION" \
  --member="serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" \
  --quiet >/dev/null
verify_resource_role_present \
  "BYOK KMS key" "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyEncrypterDecrypter" \
  gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" \
  --location="$REGION" \
  --member="serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyDecrypter" \
  --quiet >/dev/null
verify_resource_role_present \
  "BYOK KMS key" "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyDecrypter" \
  gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"

for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  member="serviceAccount:${service_account}"
  for role in \
    roles/cloudkms.cryptoKeyEncrypter \
    roles/cloudkms.cryptoKeyDecrypter \
    roles/cloudkms.cryptoKeyEncrypterDecrypter; do
    if [ "$service_account" = "$CONSOLE_RUN_SERVICE_ACCOUNT" ] && \
       [ "$role" = "roles/cloudkms.cryptoKeyEncrypter" ]; then
      continue
    fi
    remove_kms_role_if_present "$member" "$role" "$GOOGLE_ADS_KMS_KEY_ID"
  done
done
verify_only_resource_role \
  "Google Ads KMS key" "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyEncrypter" \
  gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$ACTIONS_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "Google Ads KMS key" "serviceAccount:${service_account}" "" \
    gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring="$KMS_KEYRING_ID" --location="$REGION"
done

# Cloud Run role isolation. Data roles bind directly to the one database,
# Bigtable instance, or KMS key. Owner-provisioned direct secret access is
# verified per resource by secrets.sh. The only project-level runtime role
# retained here is the non-data service-usage permission required to call
# enabled Google APIs.
log "removing broad legacy and split-runtime project data roles"
for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  member="serviceAccount:${service_account}"
  remove_project_role_if_present "$member" "roles/secretmanager.secretAccessor"
  remove_project_role_if_present "$member" "roles/spanner.databaseReader"
  remove_project_role_if_present "$member" "roles/spanner.databaseUser"
  remove_project_role_if_present "$member" "roles/bigtable.reader"
  remove_project_role_if_present "$member" "roles/bigtable.user"
  remove_project_role_if_present "$member" "roles/aiplatform.user"
  remove_project_role_if_present "$member" "roles/run.developer"
  if [ "$service_account" = "$ACTIONS_RUN_SERVICE_ACCOUNT" ]; then
    remove_project_role_if_present "$member" "roles/serviceusage.serviceUsageConsumer"
  fi
  remove_project_role_if_present "$member" "roles/editor"
  remove_project_role_if_present "$member" "roles/owner"
  expected_spanner_role=""
  case "$service_account" in
    "$PUBLIC_RUN_SERVICE_ACCOUNT"|"$CHAT_RUN_SERVICE_ACCOUNT")
      expected_spanner_role="roles/spanner.databaseReader"
      remove_spanner_role_if_present "$member" "roles/spanner.databaseUser"
      ;;
    "$CONSOLE_RUN_SERVICE_ACCOUNT"|"$WEBHOOKS_RUN_SERVICE_ACCOUNT"|"$INTERNAL_RUN_SERVICE_ACCOUNT")
      expected_spanner_role="roles/spanner.databaseUser"
      remove_spanner_role_if_present "$member" "roles/spanner.databaseReader"
      ;;
    "$ACTIONS_RUN_SERVICE_ACCOUNT")
      remove_spanner_role_if_present "$member" "roles/spanner.databaseReader"
      remove_spanner_role_if_present "$member" "roles/spanner.databaseUser"
      ;;
  esac
  verify_only_resource_role \
    "Spanner database" "$member" "$expected_spanner_role" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
  expected_bigtable_role=""
  case "$service_account" in
    "$PUBLIC_RUN_SERVICE_ACCOUNT"|"$CONSOLE_RUN_SERVICE_ACCOUNT")
      expected_bigtable_role="roles/bigtable.reader"
      remove_bigtable_role_if_present "$member" "roles/bigtable.user"
      ;;
    "$INTERNAL_RUN_SERVICE_ACCOUNT")
      expected_bigtable_role="roles/bigtable.user"
      remove_bigtable_role_if_present "$member" "roles/bigtable.reader"
      ;;
    "$ACTIONS_RUN_SERVICE_ACCOUNT"|"$CHAT_RUN_SERVICE_ACCOUNT"|"$WEBHOOKS_RUN_SERVICE_ACCOUNT")
      remove_bigtable_role_if_present "$member" "roles/bigtable.reader"
      remove_bigtable_role_if_present "$member" "roles/bigtable.user"
      ;;
  esac
  verify_only_resource_role \
    "Bigtable instance" "$member" "$expected_bigtable_role" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
  expected_project_role="roles/serviceusage.serviceUsageConsumer"
  if [ "$service_account" = "$ACTIONS_RUN_SERVICE_ACCOUNT" ]; then
    expected_project_role=""
  fi
  verify_only_resource_role \
    "project" "$member" "$expected_project_role" \
    gc projects get-iam-policy "$PROJECT_ID"
done
if [ "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" = "1" ]; then
  member="serviceAccount:${RUN_SERVICE_ACCOUNT}"
  remove_project_role_if_present "$member" "roles/secretmanager.secretAccessor"
  remove_project_role_if_present "$member" "roles/spanner.databaseReader"
  remove_project_role_if_present "$member" "roles/spanner.databaseUser"
  remove_project_role_if_present "$member" "roles/bigtable.reader"
  remove_project_role_if_present "$member" "roles/bigtable.user"
  remove_project_role_if_present "$member" "roles/aiplatform.user"
  remove_project_role_if_present "$member" "roles/run.developer"
  remove_project_role_if_present "$member" "roles/serviceusage.serviceUsageConsumer"
  remove_project_role_if_present "$member" "roles/editor"
  remove_project_role_if_present "$member" "roles/owner"
  remove_spanner_role_if_present "$member" "roles/spanner.databaseReader"
  remove_spanner_role_if_present "$member" "roles/spanner.databaseUser"
  verify_only_resource_role \
    "retired Spanner database" "$member" "" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
  remove_bigtable_role_if_present "$member" "roles/bigtable.reader"
  remove_bigtable_role_if_present "$member" "roles/bigtable.user"
  verify_only_resource_role \
    "retired Bigtable instance" "$member" "" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
  for key_id in "$BYOK_KMS_KEY_ID" "$GOOGLE_ADS_KMS_KEY_ID"; do
    for role in \
      roles/cloudkms.cryptoKeyEncrypter \
      roles/cloudkms.cryptoKeyDecrypter \
      roles/cloudkms.cryptoKeyEncrypterDecrypter; do
      remove_kms_role_if_present "$member" "$role" "$key_id"
    done
    verify_only_resource_role \
      "retired ${key_id} KMS key" "$member" "" \
      gc kms keys get-iam-policy "$key_id" \
      --keyring="$KMS_KEYRING_ID" --location="$REGION"
  done
  verify_identity_ancestor_scopes_empty "retired legacy runtime" "$RUN_SERVICE_ACCOUNT"
  verify_only_resource_role \
    "retired project" "$member" "" \
    gc projects get-iam-policy "$PROJECT_ID"
else
  log "legacy runtime IAM retirement deferred until combined traffic is zero"
fi

for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$CONSOLE_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  ensure_project_role \
    "serviceAccount:${service_account}" \
    "roles/serviceusage.serviceUsageConsumer"
done
for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$CONSOLE_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "project" \
    "serviceAccount:${service_account}" \
    "roles/serviceusage.serviceUsageConsumer" \
    gc projects get-iam-policy "$PROJECT_ID"
done
verify_only_resource_role \
  "project" \
  "serviceAccount:${ACTIONS_RUN_SERVICE_ACCOUNT}" \
  "" \
  gc projects get-iam-policy "$PROJECT_ID"

log "granting deploy identity actAs on exactly six runtime identities"
for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  gc iam service-accounts add-iam-policy-binding "$service_account" \
    --member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet >/dev/null
  verify_only_resource_role \
    "runtime service-account actAs" \
    "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
    "roles/iam.serviceAccountUser" \
    gc iam service-accounts get-iam-policy "$service_account"
done
gc iam service-accounts add-iam-policy-binding "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
  --member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --condition=None \
  --quiet >/dev/null
verify_synthetic_service_account_policy post
verify_synthetic_data_iam_empty

log "post-verifying database-scoped Spanner access"

verify_only_resource_role \
  "Spanner database" "serviceAccount:${PUBLIC_RUN_SERVICE_ACCOUNT}" \
  "roles/spanner.databaseReader" \
  gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID"
verify_only_resource_role \
  "Spanner database" "serviceAccount:${CHAT_RUN_SERVICE_ACCOUNT}" \
  "roles/spanner.databaseReader" \
  gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID"
for service_account in \
  "$CONSOLE_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "Spanner database" "serviceAccount:${service_account}" \
    "roles/spanner.databaseUser" \
    gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID"
done
verify_only_resource_role \
  "Spanner database" "serviceAccount:${ACTIONS_RUN_SERVICE_ACCOUNT}" "" \
  gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID"

log "post-verifying instance-scoped Bigtable access"
verify_only_resource_role \
  "Bigtable instance" "serviceAccount:${PUBLIC_RUN_SERVICE_ACCOUNT}" \
  "roles/bigtable.reader" \
  gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
verify_only_resource_role \
  "Bigtable instance" "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/bigtable.reader" \
  gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
verify_only_resource_role \
  "Bigtable instance" "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
  "roles/bigtable.user" \
  gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
for service_account in \
  "$ACTIONS_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "Bigtable instance" "serviceAccount:${service_account}" "" \
    gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"
done

log "reconciling key-scoped BYOK KMS access"
for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  member="serviceAccount:${service_account}"
  for role in \
    roles/cloudkms.cryptoKeyEncrypter \
    roles/cloudkms.cryptoKeyDecrypter \
    roles/cloudkms.cryptoKeyEncrypterDecrypter; do
    if [ "$service_account" = "$CONSOLE_RUN_SERVICE_ACCOUNT" ] && \
       [ "$role" = "roles/cloudkms.cryptoKeyEncrypterDecrypter" ]; then
      continue
    fi
    if [ "$service_account" = "$INTERNAL_RUN_SERVICE_ACCOUNT" ] && \
       [ "$role" = "roles/cloudkms.cryptoKeyDecrypter" ]; then
      continue
    fi
    remove_kms_role_if_present "$member" "$role"
  done
done
verify_only_resource_role \
  "BYOK KMS key" "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyEncrypterDecrypter" \
  gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
verify_only_resource_role \
  "BYOK KMS key" "serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyDecrypter" \
  gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$ACTIONS_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "BYOK KMS key" "serviceAccount:${service_account}" "" \
    gc kms keys get-iam-policy "$BYOK_KMS_KEY_ID" \
    --keyring="$KMS_KEYRING_ID" --location="$REGION"
done

# Metadata-only Google Ads conversion worker. It can read the durable Spanner
# outbox and unwrap only the dedicated Google-click envelope key. It has no
# Bigtable, Secret Manager, provider-key, or BYOK-key decrypt permission.
if ! gc iam service-accounts describe \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" >/dev/null 2>&1; then
  gc iam service-accounts create "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT_ID" \
    --display-name="TrustedRouter Google Data Manager" \
    --description="Uploads encrypted-click signup, activation, and purchase conversions to Google Ads" \
    --quiet
fi
remove_project_role_if_present \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/spanner.databaseUser"
gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" \
  --member="serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  --role="roles/spanner.databaseUser" \
  --quiet >/dev/null
verify_only_resource_role \
  "Spanner database" \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/spanner.databaseUser" \
  gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID"
ensure_project_role \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/serviceusage.serviceUsageConsumer"
gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring "$KMS_KEYRING_ID" \
  --location "$REGION" \
  --member="serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  --role="roles/cloudkms.cryptoKeyDecrypter" \
  --quiet >/dev/null
verify_only_resource_role \
  "Google Ads KMS key" \
  "serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyEncrypter" \
  gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
verify_only_resource_role \
  "Google Ads KMS key" \
  "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" \
  "roles/cloudkms.cryptoKeyDecrypter" \
  gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
  --keyring="$KMS_KEYRING_ID" --location="$REGION"
for service_account in \
  "$PUBLIC_RUN_SERVICE_ACCOUNT" \
  "$ACTIONS_RUN_SERVICE_ACCOUNT" \
  "$CHAT_RUN_SERVICE_ACCOUNT" \
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT" \
  "$INTERNAL_RUN_SERVICE_ACCOUNT"; do
  verify_only_resource_role \
    "Google Ads KMS key" "serviceAccount:${service_account}" "" \
    gc kms keys get-iam-policy "$GOOGLE_ADS_KMS_KEY_ID" \
    --keyring="$KMS_KEYRING_ID" --location="$REGION"
done
for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  verify_identity_ancestor_scopes_empty "split runtime" "$service_account"
done
gc iam service-accounts add-iam-policy-binding \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
  --member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null
gc iam service-accounts add-iam-policy-binding \
  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet >/dev/null

log "auditing deploy actAs bindings without removing operator-managed grants"
verify_deploy_actas_inventory "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" post
