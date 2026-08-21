#!/usr/bin/env bash
# Read-only IAM and mounted-secret verification for the six-surface rollout.
#
# This gate intentionally owns no reconciliation path.  Every gcloud command
# below is a describe, list, or get-iam-policy operation so it is safe to run
# both before staging and before every promotion step.

set -euo pipefail

usage() {
  echo "usage: $0 --project PROJECT [--manifest PATH]" >&2
}

PROJECT_ARGUMENT=""
MANIFEST_PATH=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -z "$PROJECT_ARGUMENT" ] || { echo "ERROR: --project may be specified only once" >&2; exit 2; }
      PROJECT_ARGUMENT="$2"
      shift 2
      ;;
    --manifest)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -z "$MANIFEST_PATH" ] || { echo "ERROR: --manifest may be specified only once" >&2; exit 2; }
      MANIFEST_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ "$PROJECT_ARGUMENT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || {
  echo "ERROR: --project must be a canonical GCP project identifier" >&2
  exit 2
}
if [ -n "$MANIFEST_PATH" ] && [ ! -f "$MANIFEST_PATH" ]; then
  echo "ERROR: --manifest must name a readable regular file" >&2
  exit 2
fi

PROJECT_ID="$PROJECT_ARGUMENT"
export PROJECT_ID
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

SURFACES=(public actions console chat webhooks internal)

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

for surface in "${SURFACES[@]}"; do
  expected_account="tr-${surface}@${PROJECT_ID}.iam.gserviceaccount.com"
  [ "$(surface_account "$surface")" = "$expected_account" ] || {
    echo "ERROR: six-surface IAM verification requires canonical runtime identities" >&2
    exit 1
  }
done
if [ "${#RUNTIME_SERVICE_ACCOUNTS[@]}" -ne 6 ]; then
  echo "ERROR: runtime service-account inventory is not the canonical six-account set" >&2
  exit 1
fi

RUNTIME_CSV="$(IFS=,; echo "${RUNTIME_SERVICE_ACCOUNTS[*]}")"

ROLLOUT_REQUIRE_RECOVERY_BUNDLE="${TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE:-false}"
ROLLOUT_RECOVERY_GCS_PREFIX="${TR_ROLLOUT_RECOVERY_GCS_PREFIX:-}"
ROLLOUT_RECOVERY_GCS_ROLE="${TR_ROLLOUT_RECOVERY_GCS_ROLE:-}"
ROLLOUT_BUNDLE_GCS_URI="${TR_ROLLOUT_BUNDLE_GCS_URI:-}"
ROLLOUT_AUTHORITY_GCS_URI="${TR_ROLLOUT_AUTHORITY_GCS_URI:-}"
ROLLOUT_STATE_GCS_URI="${TR_ROLLOUT_STATE_GCS_URI:-}"
ROLLOUT_STATE_GCS_ROLE="${TR_ROLLOUT_STATE_GCS_ROLE:-}"
case "$ROLLOUT_REQUIRE_RECOVERY_BUNDLE" in
  true|false) ;;
  *)
    echo "ERROR: TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE must be true or false" >&2
    exit 2
    ;;
esac
ROLLOUT_RECOVERY_ACTIVE=false
if [ "$ROLLOUT_REQUIRE_RECOVERY_BUNDLE" = true ] ||
   [ -n "$ROLLOUT_RECOVERY_GCS_PREFIX$ROLLOUT_RECOVERY_GCS_ROLE$ROLLOUT_BUNDLE_GCS_URI$ROLLOUT_AUTHORITY_GCS_URI" ]; then
  ROLLOUT_RECOVERY_ACTIVE=true
fi
ROLLOUT_STATE_BUCKET=""
ROLLOUT_STATE_OBJECT=""
if [ "$ROLLOUT_RECOVERY_ACTIVE" = false ]; then
  if { [ -n "$ROLLOUT_STATE_GCS_URI" ] && [ -z "$ROLLOUT_STATE_GCS_ROLE" ]; } ||
     { [ -z "$ROLLOUT_STATE_GCS_URI" ] && [ -n "$ROLLOUT_STATE_GCS_ROLE" ]; }; then
    echo "ERROR: rollout journal URI and custom role must be configured together" >&2
    exit 1
  fi
fi
if [ "$ROLLOUT_RECOVERY_ACTIVE" = false ] && [ -n "$ROLLOUT_STATE_GCS_URI" ]; then
  if ! ROLLOUT_STATE_PARTS="$(python3 -c '
import re
import sys

uri, project, role = sys.argv[1:4]
match = re.fullmatch(
    r"gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])/"
    r"([A-Za-z0-9][A-Za-z0-9._/-]{0,1023})",
    uri,
)
if not match or "//" in match.group(2):
    raise SystemExit("invalid rollout journal URI")
if not re.fullmatch(
    rf"projects/{re.escape(project)}/roles/[A-Za-z0-9_.]{{3,64}}", role
):
    raise SystemExit("invalid rollout journal custom role")
print(match.group(1) + "\t" + match.group(2))
' "$ROLLOUT_STATE_GCS_URI" "$PROJECT_ID" "$ROLLOUT_STATE_GCS_ROLE" 2>/dev/null)"; then
    echo "ERROR: rollout journal URI or custom role is noncanonical" >&2
    exit 1
  fi
  IFS=$'\t' read -r ROLLOUT_STATE_BUCKET ROLLOUT_STATE_OBJECT <<<"$ROLLOUT_STATE_PARTS"
fi

ROLLOUT_RECOVERY_BUCKET=""
ROLLOUT_RECOVERY_OBJECT_PREFIX=""
ROLLOUT_RECOVERY_EPOCH=""
if [ "$ROLLOUT_RECOVERY_ACTIVE" = true ]; then
  if [ -z "$ROLLOUT_RECOVERY_GCS_PREFIX" ] ||
     [ -z "$ROLLOUT_RECOVERY_GCS_ROLE" ] ||
     [ -z "$ROLLOUT_BUNDLE_GCS_URI" ] ||
     [ -z "$ROLLOUT_AUTHORITY_GCS_URI" ] ||
     [ -z "$ROLLOUT_STATE_GCS_URI" ]; then
    echo "ERROR: recovery IAM verification requires prefix, role, bundle, authority, and state URIs" >&2
    exit 1
  fi
  if [ -n "$ROLLOUT_STATE_GCS_ROLE" ] &&
     [ "$ROLLOUT_STATE_GCS_ROLE" != "$ROLLOUT_RECOVERY_GCS_ROLE" ]; then
    echo "ERROR: legacy journal and recovery custom-role inputs disagree" >&2
    exit 1
  fi
  if ! ROLLOUT_RECOVERY_PARTS="$(python3 -c '
import re
import sys

prefix, bundle, authority, state, project, role = sys.argv[1:]
bucket_pattern = r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]"
prefix_match = re.fullmatch(
    rf"gs://({bucket_pattern})/(trusted-router-rollouts/{re.escape(project)})",
    prefix,
)
if not prefix_match:
    raise SystemExit("noncanonical recovery prefix")
bucket, object_prefix = prefix_match.groups()
release_prefix = f"{prefix}/releases/"
if not bundle.startswith(release_prefix):
    raise SystemExit("bundle is outside the recovery releases prefix")
epoch = bundle[len(release_prefix):]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,159}", epoch):
    raise SystemExit("bundle epoch is not unique and canonical")
if authority != f"{prefix}/authority.json":
    raise SystemExit("authority is outside the exact recovery object")
if state != f"{bundle}/promotion-state.json":
    raise SystemExit("state is outside the exact recovery bundle")
if not re.fullmatch(
    rf"projects/{re.escape(project)}/roles/[A-Za-z][A-Za-z0-9_.]{{2,63}}",
    role,
):
    raise SystemExit("recovery role is not project-local and canonical")
print("\t".join((bucket, object_prefix, epoch)))
' "$ROLLOUT_RECOVERY_GCS_PREFIX" "$ROLLOUT_BUNDLE_GCS_URI" \
    "$ROLLOUT_AUTHORITY_GCS_URI" "$ROLLOUT_STATE_GCS_URI" \
    "$PROJECT_ID" "$ROLLOUT_RECOVERY_GCS_ROLE" 2>/dev/null)"; then
    echo "ERROR: rollout recovery prefix, bundle, authority, state, or role is noncanonical" >&2
    exit 1
  fi
  IFS=$'\t' read -r ROLLOUT_RECOVERY_BUCKET \
    ROLLOUT_RECOVERY_OBJECT_PREFIX ROLLOUT_RECOVERY_EPOCH \
    <<<"$ROLLOUT_RECOVERY_PARTS"
fi

read_cloud_json() {
  local label="$1"
  shift
  local output
  if ! output="$("$@" 2>/dev/null)"; then
    echo "ERROR: cannot read ${label}" >&2
    return 1
  fi
  if ! printf '%s' "$output" | python3 -c '
import json
import sys

label = sys.argv[1]
try:
    value = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(f"{label}: response is not valid JSON") from None
if isinstance(value, dict):
    for binding in value.get("bindings") or []:
        members = binding.get("members") or []
        if any(member in {"allUsers", "allAuthenticatedUsers"} for member in members):
            raise SystemExit(f"{label}: public IAM principal is forbidden")
' "$label"
  then
    echo "ERROR: ${label} contains a public or malformed IAM policy" >&2
    return 1
  fi
  printf '%s' "$output"
}

describe_iam_role_definition_readonly() {
  local role="$1"
  local scope role_id
  case "$role" in
    roles/*)
      gc iam roles describe "$role" --format=json
      ;;
    projects/*/roles/*)
      scope="${role#projects/}"
      scope="${scope%%/roles/*}"
      role_id="${role##*/}"
      [ "$scope" = "$PROJECT_ID" ] || return 1
      gc iam roles describe "$role_id" --format=json
      ;;
    organizations/*/roles/*)
      scope="${role#organizations/}"
      scope="${scope%%/roles/*}"
      role_id="${role##*/}"
      [[ "$scope" =~ ^[0-9]+$ ]] || return 1
      gcloud --billing-project "$PROJECT_ID" iam roles describe "$role_id" \
        --organization="$scope" --format=json
      ;;
    *) return 1 ;;
  esac
}

# Verify all and only the expected direct bindings for the six runtime
# principals, plus no binding for the dedicated synthetic identity, on one
# resource policy. Bindings for unrelated administrators or service agents
# remain outside this verifier's ownership boundary.
verify_six_runtime_matrix_json() {
  local label="$1"
  local expected_csv="$2"
  python3 -c '
import json
import sys

label, accounts_csv, expected_csv, project = sys.argv[1:5]
accounts = accounts_csv.split(",")
if len(accounts) != 6 or len(set(accounts)) != 6:
    raise SystemExit(f"{label}: runtime identity inventory is not exactly six")

expected = {}
for item in expected_csv.split(","):
    surface, separator, role = item.partition("=")
    if not separator or surface in expected:
        raise SystemExit(f"{label}: invalid expected IAM matrix")
    expected[surface] = role
surfaces = ("public", "actions", "console", "chat", "webhooks", "internal")
if set(expected) != set(surfaces):
    raise SystemExit(f"{label}: expected IAM matrix does not cover six surfaces")

members = {
    "serviceAccount:" + account: expected[surface]
    for surface, account in zip(surfaces, accounts)
}
members[f"serviceAccount:tr-synthetic@{project}.iam.gserviceaccount.com"] = ""
policy = json.load(sys.stdin)
if not isinstance(policy, dict) or not isinstance(policy.get("bindings", []), list):
    raise SystemExit(f"{label}: malformed IAM policy")
found = {member: [] for member in members}
for binding in policy.get("bindings", []):
    if not isinstance(binding, dict):
        raise SystemExit(f"{label}: malformed IAM binding")
    role = binding.get("role")
    binding_members = binding.get("members", [])
    if not isinstance(role, str) or not isinstance(binding_members, list):
        raise SystemExit(f"{label}: malformed IAM binding")
    condition = binding.get("condition") if "condition" in binding else None
    for member in binding_members:
        if member in found:
            found[member].append((role, condition))

for member, wanted_role in members.items():
    wanted = [] if not wanted_role else [(wanted_role, None)]
    if found[member] != wanted:
        raise SystemExit(f"{label}: six-runtime IAM matrix drift")
' "$label" "$RUNTIME_CSV" "$expected_csv" "$PROJECT_ID"
}

verify_policy_from_cloud() {
  local label="$1"
  local expected_csv="$2"
  shift 2
  local policy
  policy="$(read_cloud_json "${label} IAM policy" "$@")" || return 1
  if ! printf '%s' "$policy" | verify_six_runtime_matrix_json "$label" "$expected_csv"; then
    echo "ERROR: ${label} has unsafe six-runtime IAM" >&2
    return 1
  fi
}

# Normalize a provider list response to one resource identifier per line.  The
# command that produced the response is already scoped to PROJECT_ID; full
# resource names are nevertheless checked again so a malformed/cross-project
# inventory cannot cause the verifier to audit the wrong object and miss the
# actual one.
normalize_project_inventory_json() {
  local label="$1"
  local kind="$2"
  local parent="$3"
  python3 -c '
import json
import re
import sys

label, kind, project, parent = sys.argv[1:5]
items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit(f"{label}: inventory is not a list")
identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
location_identifier = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
service_account_email = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,250}[A-Za-z0-9])?[.]gserviceaccount[.]com"
)

def resource_id(item):
    if not isinstance(item, dict):
        raise SystemExit(f"{label}: malformed inventory entry")
    value = item.get("name")
    if kind == "kms-location":
        value = item.get("locationId", value)
        prefix = f"projects/{project}/locations/"
    elif kind == "spanner-instance":
        prefix = f"projects/{project}/instances/"
    elif kind == "spanner-database":
        prefix = f"projects/{project}/instances/{parent}/databases/"
    elif kind == "bigtable-instance":
        prefix = f"projects/{project}/instances/"
    elif kind == "bigtable-table":
        prefix = f"projects/{project}/instances/{parent}/tables/"
    elif kind == "kms-keyring":
        prefix = f"projects/{project}/locations/{parent}/keyRings/"
    elif kind == "kms-key":
        location, separator, keyring = parent.partition("/")
        if not separator:
            raise SystemExit(f"{label}: malformed key parent")
        prefix = f"projects/{project}/locations/{location}/keyRings/{keyring}/cryptoKeys/"
    elif kind == "service-account":
        email = item.get("email")
        if not isinstance(email, str) or not service_account_email.fullmatch(email):
            raise SystemExit(f"{label}: malformed service-account email")
        name = item.get("name")
        unique_id = item.get("uniqueId")
        allowed_accounts = {email}
        if unique_id is not None:
            unique_id = str(unique_id)
            if not re.fullmatch(r"[0-9]{6,32}", unique_id):
                raise SystemExit(f"{label}: malformed service-account unique id")
            allowed_accounts.add(unique_id)
        if name is not None:
            prefixes = (
                f"projects/{project}/serviceAccounts/",
                "projects/-/serviceAccounts/",
            )
            matching_prefix = next(
                (prefix for prefix in prefixes if name.startswith(prefix)), None
            ) if isinstance(name, str) else None
            if matching_prefix is None or name[len(matching_prefix):] not in allowed_accounts:
                raise SystemExit(f"{label}: cross-project service-account entry")
        return email
    else:
        raise SystemExit(f"{label}: unknown inventory kind")
    if not isinstance(value, str):
        raise SystemExit(f"{label}: malformed resource name")
    if value.startswith("projects/"):
        if not value.startswith(prefix):
            raise SystemExit(f"{label}: cross-project or wrong-parent entry")
        value = value[len(prefix):]
    validator = location_identifier if kind == "kms-location" else identifier
    if not validator.fullmatch(value):
        raise SystemExit(f"{label}: invalid resource identifier")
    return value

values = [resource_id(item) for item in items]
if len(values) != len(set(values)):
    raise SystemExit(f"{label}: duplicate inventory entry")
print("\n".join(sorted(values)))
' "$label" "$kind" "$PROJECT_ID" "$parent"
}

# Project- and ancestor-level grants cannot be safely constrained to one
# runtime, secret, key, or recovery object. Resolve every role directly bound
# to the deploy principal and reject broad data access, token minting, or
# project-wide actAs. Narrow resource bindings are audited separately.
verify_deploy_broad_permissions_absent_json() {
  local label="$1"
  local policy="$2"
  local deploy_member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"
  local roles role role_json
  if ! roles="$(printf '%s' "$policy" | python3 -c '
import json
import sys

member = sys.argv[1]
policy = json.load(sys.stdin)
if not isinstance(policy, dict) or not isinstance(policy.get("bindings", []), list):
    raise SystemExit("malformed IAM policy")
roles = []
for binding in policy.get("bindings", []):
    if not isinstance(binding, dict):
        raise SystemExit("malformed IAM binding")
    role = binding.get("role")
    members = binding.get("members", [])
    if not isinstance(role, str) or not role or not isinstance(members, list):
        raise SystemExit("malformed IAM binding")
    if member in members:
        roles.append(role)
if len(set(roles)) != len(roles):
    raise SystemExit("deploy principal has duplicate role bindings")
print("\n".join(sorted(roles)))
' "$deploy_member")"; then
    echo "ERROR: cannot inspect deploy identity bindings at ${label}" >&2
    return 1
  fi
  while IFS= read -r role; do
    [ -n "$role" ] || continue
    role_json="$(read_cloud_json "deploy identity role definition" \
      describe_iam_role_definition_readonly "$role")" || return 1
    if ! printf '%s' "$role_json" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
permissions = value.get("includedPermissions")
if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
    raise SystemExit("role permission inventory is malformed")
forbidden = {
    "aiplatform.endpoints.predict",
    "bigtable.tables.mutateRows",
    "bigtable.tables.readRows",
    "cloudkms.cryptoKeyVersions.useToDecrypt",
    "cloudkms.cryptoKeyVersions.useToEncrypt",
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.getAccessToken",
    "secretmanager.versions.access",
    "spanner.databases.read",
    "spanner.databases.write",
    "spanner.sessions.create",
}
if forbidden.intersection(permissions) or any(
    permission.startswith("storage.objects.") for permission in permissions
):
    raise SystemExit("deploy role contains project/ancestor data or impersonation permission")
'; then
      echo "ERROR: deploy identity has a broad data or impersonation permission at ${label}" >&2
      return 1
    fi
  done <<<"$roles"
}

verify_journal_role_and_bucket_policy() {
  [ -n "$ROLLOUT_STATE_GCS_URI" ] || return 0
  local role_json bucket_policy expected_expression
  role_json="$(read_cloud_json "rollout journal custom-role definition" \
    describe_iam_role_definition_readonly "$ROLLOUT_STATE_GCS_ROLE")" || return 1
  if ! printf '%s' "$role_json" | python3 -c '
import json
import sys

expected_name = sys.argv[1]
value = json.load(sys.stdin)
if value.get("name") != expected_name:
    raise SystemExit("rollout journal custom role identity mismatch")
if value.get("deleted", False) is not False:
    raise SystemExit("rollout journal custom role is deleted")
permissions = value.get("includedPermissions")
expected = {
    "storage.objects.get",
    "storage.objects.create",
    "storage.objects.delete",
}
if not isinstance(permissions, list) or len(permissions) != len(set(permissions)):
    raise SystemExit("rollout journal custom role permission inventory is malformed")
if set(permissions) != expected:
    raise SystemExit("rollout journal custom role permissions drifted")
' "$ROLLOUT_STATE_GCS_ROLE"; then
    echo "ERROR: rollout journal custom role is not the exact three-permission role" >&2
    return 1
  fi

  bucket_policy="$(read_cloud_json "rollout journal bucket IAM policy" \
    gc storage buckets get-iam-policy "gs://${ROLLOUT_STATE_BUCKET}" --format=json)" || return 1
  expected_expression="resource.name == \"projects/_/buckets/${ROLLOUT_STATE_BUCKET}/objects/${ROLLOUT_STATE_OBJECT}\""
  if ! printf '%s' "$bucket_policy" | python3 -c '
import json
import sys

role, member, title, expression = sys.argv[1:5]
policy = json.load(sys.stdin)
if not isinstance(policy, dict) or not isinstance(policy.get("bindings", []), list):
    raise SystemExit("rollout journal bucket IAM policy is malformed")
role_bindings = []
member_bindings = []
for binding in policy.get("bindings", []):
    if not isinstance(binding, dict):
        raise SystemExit("rollout journal bucket IAM binding is malformed")
    binding_role = binding.get("role")
    members = binding.get("members", [])
    if not isinstance(binding_role, str) or not isinstance(members, list):
        raise SystemExit("rollout journal bucket IAM binding is malformed")
    if binding_role == role:
        role_bindings.append(binding)
    if member in members:
        member_bindings.append(binding)
if len(role_bindings) != 1 or len(member_bindings) != 1 or role_bindings[0] is not member_bindings[0]:
    raise SystemExit("rollout journal bucket binding is not unique")
binding = role_bindings[0]
if binding.get("members") != [member]:
    raise SystemExit("rollout journal bucket role has noncanonical members")
condition = binding.get("condition")
if not isinstance(condition, dict):
    raise SystemExit("rollout journal bucket role lacks its object condition")
if condition.get("title") != title or condition.get("expression") != expression:
    raise SystemExit("rollout journal bucket object condition drifted")
if set(condition) - {"title", "expression", "description"}:
    raise SystemExit("rollout journal bucket condition has unknown fields")
' "$ROLLOUT_STATE_GCS_ROLE" "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
    "trusted-router-rollout-journal" "$expected_expression"; then
    echo "ERROR: rollout journal bucket IAM is not the exact object-scoped binding" >&2
    return 1
  fi
}

verify_recovery_role_and_bucket_policy() {
  [ "$ROLLOUT_RECOVERY_ACTIVE" = true ] || return 0
  local role_json bucket_json bucket_policy
  local authority_resource bundle_resource_prefix expected_expression

  role_json="$(read_cloud_json "rollout recovery custom-role definition" \
    describe_iam_role_definition_readonly "$ROLLOUT_RECOVERY_GCS_ROLE")" || return 1
  if ! printf '%s' "$role_json" | python3 -c '
import json
import sys

expected_name = sys.argv[1]
value = json.load(sys.stdin)
if value.get("name") != expected_name:
    raise SystemExit("rollout recovery custom role identity mismatch")
if value.get("deleted", False) is not False:
    raise SystemExit("rollout recovery custom role is deleted")
permissions = value.get("includedPermissions")
expected = {
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
}
if not isinstance(permissions, list) or len(permissions) != len(set(permissions)):
    raise SystemExit("rollout recovery custom role permission inventory is malformed")
if set(permissions) != expected:
    raise SystemExit("rollout recovery custom role permissions drifted")
' "$ROLLOUT_RECOVERY_GCS_ROLE"; then
    echo "ERROR: rollout recovery custom role is not the exact three-permission role" >&2
    return 1
  fi

  bucket_json="$(read_cloud_json "rollout recovery bucket metadata" \
    gc storage buckets describe "gs://${ROLLOUT_RECOVERY_BUCKET}" \
      --format=json)" || return 1
  if ! printf '%s' "$bucket_json" | python3 -c '
import json
import sys

expected_name = sys.argv[1]
value = json.load(sys.stdin)
if value.get("name") != expected_name:
    raise SystemExit("rollout recovery bucket identity mismatch")
iam = value.get("iamConfiguration") or {}
uniform = iam.get("uniformBucketLevelAccess") or {}
if uniform.get("enabled") is not True:
    raise SystemExit("rollout recovery bucket lacks uniform access")
if str(iam.get("publicAccessPrevention", "")).lower() != "enforced":
    raise SystemExit("rollout recovery bucket lacks public-access prevention")
if (value.get("versioning") or {}).get("enabled") is not True:
    raise SystemExit("rollout recovery bucket lacks versioning")
retention = (value.get("retentionPolicy") or {}).get("retentionPeriod")
try:
    retention_seconds = int(retention)
except (TypeError, ValueError):
    raise SystemExit("rollout recovery bucket retention is malformed") from None
if retention_seconds < 7 * 24 * 60 * 60:
    raise SystemExit("rollout recovery bucket retention is shorter than seven days")
' "$ROLLOUT_RECOVERY_BUCKET"; then
    echo "ERROR: rollout recovery bucket protection contract drifted" >&2
    return 1
  fi

  authority_resource="projects/_/buckets/${ROLLOUT_RECOVERY_BUCKET}/objects/${ROLLOUT_RECOVERY_OBJECT_PREFIX}/authority.json"
  bundle_resource_prefix="projects/_/buckets/${ROLLOUT_RECOVERY_BUCKET}/objects/${ROLLOUT_RECOVERY_OBJECT_PREFIX}/releases/${ROLLOUT_RECOVERY_EPOCH}/"
  expected_expression="resource.name == \"${authority_resource}\" || resource.name.startsWith(\"${bundle_resource_prefix}\")"
  bucket_policy="$(read_cloud_json "rollout recovery bucket IAM policy" \
    gc storage buckets get-iam-policy "gs://${ROLLOUT_RECOVERY_BUCKET}" \
      --format=json)" || return 1
  if ! printf '%s' "$bucket_policy" | python3 -c '
import json
import sys

role, member, title, expression = sys.argv[1:5]
policy = json.load(sys.stdin)
bindings = policy.get("bindings")
if not isinstance(bindings, list) or len(bindings) != 1:
    raise SystemExit("recovery bucket must have one exact binding")
binding = bindings[0]
if not isinstance(binding, dict) or set(binding) != {"role", "members", "condition"}:
    raise SystemExit("recovery bucket binding shape drifted")
if binding.get("role") != role or binding.get("members") != [member]:
    raise SystemExit("recovery bucket role or principal drifted")
condition = binding.get("condition")
if not isinstance(condition, dict) or set(condition) != {"title", "expression"}:
    raise SystemExit("recovery bucket condition shape drifted")
if condition.get("title") != title or condition.get("expression") != expression:
    raise SystemExit("recovery bucket condition scope drifted")
' "$ROLLOUT_RECOVERY_GCS_ROLE" "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" \
    "trusted-router-rollout-recovery" "$expected_expression"; then
    echo "ERROR: rollout recovery bucket IAM is not the exact authority-and-bundle binding" >&2
    return 1
  fi
}

EMPTY_MATRIX="public=,actions=,console=,chat=,webhooks=,internal="
PROJECT_MATRIX="public=roles/serviceusage.serviceUsageConsumer,actions=,console=roles/serviceusage.serviceUsageConsumer,chat=roles/serviceusage.serviceUsageConsumer,webhooks=roles/serviceusage.serviceUsageConsumer,internal=roles/serviceusage.serviceUsageConsumer"
SPANNER_MATRIX="public=roles/spanner.databaseReader,actions=,console=roles/spanner.databaseUser,chat=roles/spanner.databaseReader,webhooks=roles/spanner.databaseUser,internal=roles/spanner.databaseUser"
BIGTABLE_MATRIX="public=roles/bigtable.reader,actions=,console=roles/bigtable.reader,chat=,webhooks=,internal=roles/bigtable.user"
BYOK_MATRIX="public=,actions=,console=roles/cloudkms.cryptoKeyEncrypterDecrypter,chat=,webhooks=,internal=roles/cloudkms.cryptoKeyDecrypter"
GOOGLE_ADS_MATRIX="public=,actions=,console=roles/cloudkms.cryptoKeyEncrypter,chat=,webhooks=,internal="

PROJECT_POLICY_JSON="$(read_cloud_json "project IAM policy" \
  gc projects get-iam-policy "$PROJECT_ID" --format=json)" || exit 1
if ! printf '%s' "$PROJECT_POLICY_JSON" | \
    verify_six_runtime_matrix_json "project" "$PROJECT_MATRIX"; then
  echo "ERROR: project has unsafe six-runtime IAM" >&2
  exit 1
fi
verify_deploy_broad_permissions_absent_json "project" "$PROJECT_POLICY_JSON"

# A named-resource audit is insufficient: a stale grant on an older database,
# table, key, or service account remains live.  Inventory every resource in the
# project and require the exact configured matrix only at the canonical target;
# all other resources must have zero six-runtime/synthetic bindings.  This is a
# read-only gate and deliberately owns no automatic cleanup path.
SPANNER_INSTANCES_JSON="$(read_cloud_json "Spanner instance inventory" \
  gc spanner instances list --format=json)" || exit 1
if ! SPANNER_INSTANCE_IDS="$(printf '%s' "$SPANNER_INSTANCES_JSON" | \
    normalize_project_inventory_json "Spanner instance" spanner-instance "")"; then
  echo "ERROR: Spanner instance inventory is malformed" >&2
  exit 1
fi
SPANNER_INSTANCE_FOUND=0
SPANNER_DATABASE_FOUND=0
while IFS= read -r spanner_instance; do
  [ -n "$spanner_instance" ] || continue
  [ "$spanner_instance" != "$SPANNER_INSTANCE_ID" ] || SPANNER_INSTANCE_FOUND=1
  verify_policy_from_cloud "Spanner inventory instance" "$EMPTY_MATRIX" \
    gc spanner instances get-iam-policy "$spanner_instance" --format=json

  spanner_databases_json="$(read_cloud_json "Spanner database inventory" \
    gc spanner databases list --instance="$spanner_instance" --format=json)" || exit 1
  if ! spanner_database_ids="$(printf '%s' "$spanner_databases_json" | \
      normalize_project_inventory_json "Spanner database" spanner-database \
        "$spanner_instance")"; then
    echo "ERROR: Spanner database inventory is malformed" >&2
    exit 1
  fi
  while IFS= read -r spanner_database; do
    [ -n "$spanner_database" ] || continue
    spanner_matrix="$EMPTY_MATRIX"
    if [ "$spanner_instance" = "$SPANNER_INSTANCE_ID" ] && \
       [ "$spanner_database" = "$SPANNER_DATABASE_ID" ]; then
      SPANNER_DATABASE_FOUND=1
      spanner_matrix="$SPANNER_MATRIX"
    fi
    verify_policy_from_cloud "Spanner inventory database" "$spanner_matrix" \
      gc spanner databases get-iam-policy "$spanner_database" \
        --instance="$spanner_instance" --format=json
  done <<<"$spanner_database_ids"
done <<<"$SPANNER_INSTANCE_IDS"
[ "$SPANNER_INSTANCE_FOUND" = 1 ] && [ "$SPANNER_DATABASE_FOUND" = 1 ] || {
  echo "ERROR: canonical Spanner instance/database is absent from the project inventory" >&2
  exit 1
}

BIGTABLE_INSTANCES_JSON="$(read_cloud_json "Bigtable instance inventory" \
  gc bigtable instances list --format=json)" || exit 1
if ! BIGTABLE_INSTANCE_IDS="$(printf '%s' "$BIGTABLE_INSTANCES_JSON" | \
    normalize_project_inventory_json "Bigtable instance" bigtable-instance "")"; then
  echo "ERROR: Bigtable instance inventory is malformed" >&2
  exit 1
fi
BIGTABLE_INSTANCE_FOUND=0
BIGTABLE_TABLE_FOUND=0
while IFS= read -r bigtable_instance; do
  [ -n "$bigtable_instance" ] || continue
  bigtable_matrix="$EMPTY_MATRIX"
  if [ "$bigtable_instance" = "$BIGTABLE_INSTANCE_ID" ]; then
    BIGTABLE_INSTANCE_FOUND=1
    bigtable_matrix="$BIGTABLE_MATRIX"
  fi
  verify_policy_from_cloud "Bigtable inventory instance" "$bigtable_matrix" \
    gc bigtable instances get-iam-policy "$bigtable_instance" --format=json

  bigtable_tables_json="$(read_cloud_json "Bigtable table inventory" \
    gc bigtable tables list --instances="$bigtable_instance" --format=json)" || exit 1
  if ! bigtable_table_ids="$(printf '%s' "$bigtable_tables_json" | \
      normalize_project_inventory_json "Bigtable table" bigtable-table \
        "$bigtable_instance")"; then
    echo "ERROR: Bigtable table inventory is malformed" >&2
    exit 1
  fi
  while IFS= read -r bigtable_table; do
    [ -n "$bigtable_table" ] || continue
    if [ "$bigtable_instance" = "$BIGTABLE_INSTANCE_ID" ] && \
       [ "$bigtable_table" = "$BIGTABLE_GENERATION_TABLE" ]; then
      BIGTABLE_TABLE_FOUND=1
    fi
    verify_policy_from_cloud "Bigtable inventory table" "$EMPTY_MATRIX" \
      gc bigtable tables get-iam-policy "$bigtable_table" \
        --instance="$bigtable_instance" --format=json
  done <<<"$bigtable_table_ids"
done <<<"$BIGTABLE_INSTANCE_IDS"
[ "$BIGTABLE_INSTANCE_FOUND" = 1 ] && [ "$BIGTABLE_TABLE_FOUND" = 1 ] || {
  echo "ERROR: canonical Bigtable instance/table is absent from the project inventory" >&2
  exit 1
}

KMS_LOCATIONS_JSON="$(read_cloud_json "KMS location inventory" \
  gc kms locations list --format=json)" || exit 1
if ! KMS_LOCATION_IDS="$(printf '%s' "$KMS_LOCATIONS_JSON" | \
    normalize_project_inventory_json "KMS location" kms-location "")"; then
  echo "ERROR: KMS location inventory is malformed" >&2
  exit 1
fi
KMS_LOCATION_FOUND=0
KMS_KEYRING_FOUND=0
BYOK_KEY_FOUND=0
GOOGLE_ADS_KEY_FOUND=0
while IFS= read -r kms_location; do
  [ -n "$kms_location" ] || continue
  [ "$kms_location" != "$REGION" ] || KMS_LOCATION_FOUND=1
  kms_keyrings_json="$(read_cloud_json "KMS keyring inventory" \
    gc kms keyrings list --location="$kms_location" --format=json)" || exit 1
  if ! kms_keyring_ids="$(printf '%s' "$kms_keyrings_json" | \
      normalize_project_inventory_json "KMS keyring" kms-keyring \
        "$kms_location")"; then
    echo "ERROR: KMS keyring inventory is malformed" >&2
    exit 1
  fi
  while IFS= read -r kms_keyring; do
    [ -n "$kms_keyring" ] || continue
    if [ "$kms_location" = "$REGION" ] && \
       [ "$kms_keyring" = "$KMS_KEYRING_ID" ]; then
      KMS_KEYRING_FOUND=1
    fi
    verify_policy_from_cloud "KMS inventory keyring" "$EMPTY_MATRIX" \
      gc kms keyrings get-iam-policy "$kms_keyring" \
        --location="$kms_location" --format=json

    kms_keys_json="$(read_cloud_json "KMS key inventory" \
      gc kms keys list --keyring="$kms_keyring" \
        --location="$kms_location" --format=json)" || exit 1
    if ! kms_key_ids="$(printf '%s' "$kms_keys_json" | \
        normalize_project_inventory_json "KMS key" kms-key \
          "${kms_location}/${kms_keyring}")"; then
      echo "ERROR: KMS key inventory is malformed" >&2
      exit 1
    fi
    while IFS= read -r kms_key; do
      [ -n "$kms_key" ] || continue
      kms_matrix="$EMPTY_MATRIX"
      if [ "$kms_location" = "$REGION" ] && \
         [ "$kms_keyring" = "$KMS_KEYRING_ID" ] && \
         [ "$kms_key" = "$BYOK_KMS_KEY_ID" ]; then
        BYOK_KEY_FOUND=1
        kms_matrix="$BYOK_MATRIX"
      elif [ "$kms_location" = "$REGION" ] && \
           [ "$kms_keyring" = "$KMS_KEYRING_ID" ] && \
           [ "$kms_key" = "$GOOGLE_ADS_KMS_KEY_ID" ]; then
        GOOGLE_ADS_KEY_FOUND=1
        kms_matrix="$GOOGLE_ADS_MATRIX"
      fi
      verify_policy_from_cloud "KMS inventory key" "$kms_matrix" \
        gc kms keys get-iam-policy "$kms_key" --keyring="$kms_keyring" \
          --location="$kms_location" --format=json
    done <<<"$kms_key_ids"
  done <<<"$kms_keyring_ids"
done <<<"$KMS_LOCATION_IDS"
[ "$KMS_LOCATION_FOUND" = 1 ] && [ "$KMS_KEYRING_FOUND" = 1 ] && \
  [ "$BYOK_KEY_FOUND" = 1 ] && [ "$GOOGLE_ADS_KEY_FOUND" = 1 ] || {
  echo "ERROR: canonical KMS location/keyring/keys are absent from the project inventory" >&2
  exit 1
}

SERVICE_ACCOUNTS_JSON="$(read_cloud_json "service-account inventory" \
  gc iam service-accounts list --format=json)" || exit 1
if ! SERVICE_ACCOUNT_EMAILS="$(printf '%s' "$SERVICE_ACCOUNTS_JSON" | \
    normalize_project_inventory_json "service account" service-account "")"; then
  echo "ERROR: project service-account inventory is malformed" >&2
  exit 1
fi
CANONICAL_SERVICE_ACCOUNT_INVENTORY="|"
for runtime_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}" \
  "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; do
  CANONICAL_SERVICE_ACCOUNT_INVENTORY="${CANONICAL_SERVICE_ACCOUNT_INVENTORY}${runtime_account}|"
  case $'\n'"$SERVICE_ACCOUNT_EMAILS"$'\n' in
    *$'\n'"${runtime_account}"$'\n'*) ;;
    *)
      echo "ERROR: a canonical split/synthetic identity is absent from the project inventory" >&2
      exit 1
      ;;
  esac
done
while IFS= read -r service_account; do
  [ -n "$service_account" ] || continue
  case "$CANONICAL_SERVICE_ACCOUNT_INVENTORY" in
    *"|${service_account}|"*) continue ;;
  esac
  verify_policy_from_cloud "other project service account" "$EMPTY_MATRIX" \
    gc iam service-accounts get-iam-policy "$service_account" --format=json
done <<<"$SERVICE_ACCOUNT_EMAILS"

# Project ancestors are an inherited privilege boundary.  A read failure is a
# hard failure because absence cannot otherwise be proven.
ANCESTORS_JSON="$(read_cloud_json "project ancestor inventory" \
  gc projects get-ancestors "$PROJECT_ID" --format=json)" || exit 1
if ! ANCESTOR_ROWS="$(printf '%s' "$ANCESTORS_JSON" | python3 -c '
import json
import re
import sys

items = json.load(sys.stdin)
expected_project = sys.argv[1]
if not isinstance(items, list):
    raise SystemExit("malformed ancestor inventory")
seen = set()
rows = []
for item in items:
    if not isinstance(item, dict):
        raise SystemExit("malformed ancestor inventory")
    kind = item.get("type")
    identifier = str(item.get("id", ""))
    if kind not in {"project", "folder", "organization"}:
        raise SystemExit("unknown ancestor type")
    if not re.fullmatch(r"[A-Za-z0-9-]+", identifier):
        raise SystemExit("invalid ancestor identifier")
    key = (kind, identifier)
    if key in seen:
        raise SystemExit("duplicate ancestor")
    seen.add(key)
    if kind != "project":
        rows.append(key)
project_rows = [key for key in seen if key[0] == "project"]
if project_rows != [("project", expected_project)]:
    raise SystemExit("project ancestor inventory does not identify the audited project")
print("\n".join("\t".join(row) for row in rows))
' "$PROJECT_ID")"; then
  echo "ERROR: project ancestor inventory is malformed" >&2
  exit 1
fi
while IFS=$'\t' read -r ancestor_type ancestor_id; do
  [ -n "$ancestor_type" ] || continue
  case "$ancestor_type" in
    folder)
      ancestor_policy="$(read_cloud_json "folder ancestor IAM policy" \
        gc resource-manager folders get-iam-policy "$ancestor_id" --format=json)" || exit 1
      if ! printf '%s' "$ancestor_policy" | \
          verify_six_runtime_matrix_json "folder ancestor" "$EMPTY_MATRIX"; then
        echo "ERROR: folder ancestor has unsafe six-runtime IAM" >&2
        exit 1
      fi
      verify_deploy_broad_permissions_absent_json "folder ancestor" "$ancestor_policy"
      ;;
    organization)
      ancestor_policy="$(read_cloud_json "organization ancestor IAM policy" \
        gc organizations get-iam-policy "$ancestor_id" --format=json)" || exit 1
      if ! printf '%s' "$ancestor_policy" | \
          verify_six_runtime_matrix_json "organization ancestor" "$EMPTY_MATRIX"; then
        echo "ERROR: organization ancestor has unsafe six-runtime IAM" >&2
        exit 1
      fi
      verify_deploy_broad_permissions_absent_json "organization ancestor" "$ancestor_policy"
      ;;
    *)
      echo "ERROR: project ancestor inventory is malformed" >&2
      exit 1
      ;;
  esac
done <<<"$ANCESTOR_ROWS"

if [ "$ROLLOUT_RECOVERY_ACTIVE" = true ]; then
  verify_recovery_role_and_bucket_policy
else
  verify_journal_role_and_bucket_policy
fi

synthetic_description="$(read_cloud_json "synthetic service-account description" \
  gc iam service-accounts describe "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
    --format=json)" || exit 1
if ! printf '%s' "$synthetic_description" | python3 -c '
import json
import sys

expected = sys.argv[1]
value = json.load(sys.stdin)
if value.get("email") != expected or value.get("disabled", False) is not False:
    raise SystemExit("synthetic identity is absent, renamed, or disabled")
' "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; then
  echo "ERROR: canonical synthetic service account is absent, renamed, or disabled" >&2
  exit 1
fi
synthetic_policy="$(read_cloud_json "synthetic service-account IAM policy" \
  gc iam service-accounts get-iam-policy "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
    --format=json)" || exit 1
if ! printf '%s' "$synthetic_policy" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
expected = [{
    "role": "roles/iam.serviceAccountUser",
    "members": [sys.argv[1]],
}]
bindings = policy.get("bindings") or []
if bindings != expected:
    raise SystemExit("synthetic identity actAs policy differs")
' "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"; then
  echo "ERROR: synthetic identity does not have the exact deploy-only actAs policy" >&2
  exit 1
fi

# The shared helper is the canonical complete-policy contract for runtime
# service accounts.  `post` requires exactly one unconditional deploy
# serviceAccountUser grant, permits only explicitly configured operators, and
# rejects every cross-runtime or otherwise unapproved direct principal.
for runtime_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
  runtime_description="$(read_cloud_json "runtime service-account description" \
    gc iam service-accounts describe "$runtime_account" --format=json)" || exit 1
  if ! printf '%s' "$runtime_description" | python3 -c '
import json
import sys

expected = sys.argv[1]
value = json.load(sys.stdin)
if value.get("email") != expected or value.get("disabled", False) is not False:
    raise SystemExit("runtime identity is absent, renamed, or disabled")
' "$runtime_account"; then
    echo "ERROR: a canonical runtime service account is absent, renamed, or disabled" >&2
    exit 1
  fi
  runtime_policy="$(read_cloud_json "runtime service-account IAM policy" \
    gc iam service-accounts get-iam-policy "$runtime_account" --format=json)" || exit 1
  if ! printf '%s' "$runtime_policy" | \
      verify_runtime_service_account_policy_json "$runtime_account" post; then
    echo "ERROR: a runtime service account has unsafe direct IAM" >&2
    exit 1
  fi
  if ! printf '%s' "$runtime_policy" | python3 -c '
import json
import sys

member = sys.argv[1]
policy = json.load(sys.stdin)
matches = [
    (binding.get("role"), binding.get("condition") if "condition" in binding else None)
    for binding in policy.get("bindings", [])
    if member in (binding.get("members") or [])
]
if matches:
    raise SystemExit("synthetic identity may not impersonate a split runtime")
' "serviceAccount:tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com"; then
    echo "ERROR: synthetic identity has a direct binding on a runtime service account" >&2
    exit 1
  fi
done

SECRETS_JSON="$(read_cloud_json "Secret Manager inventory" \
  gc secrets list --format=json)" || exit 1
if ! SECRET_NAMES="$(printf '%s' "$SECRETS_JSON" | python3 -c '
import json
import re
import sys

project = sys.argv[1]
items = json.load(sys.stdin)
if not isinstance(items, list):
    raise SystemExit("malformed secret inventory")
names = []
prefix = f"projects/{project}/secrets/"
for item in items:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        raise SystemExit("malformed secret inventory")
    name = item["name"]
    if name.startswith("projects/"):
        if not name.startswith(prefix):
            raise SystemExit("cross-project secret inventory entry")
        name = name[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name):
        raise SystemExit("invalid secret resource identifier")
    names.append(name)
if len(set(names)) != len(names):
    raise SystemExit("duplicate secret inventory entry")
print("\n".join(sorted(names)))
' "$PROJECT_ID")"; then
  echo "ERROR: Secret Manager inventory is malformed" >&2
  exit 1
fi

if ! printf '%s' "$TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON" | python3 -c '
import json
import sys

inventory = set(sys.argv[1].splitlines())
value = json.load(sys.stdin)
missing = sorted(set(value) - inventory)
if missing:
    raise SystemExit("preserved secret accessor allowlist references absent resources")
' "$SECRET_NAMES"; then
  echo "ERROR: explicit preserved secret accessor inventory is invalid" >&2
  exit 1
fi

SECRET_INVENTORY="|"
while IFS= read -r secret_name; do
  [ -n "$secret_name" ] || continue
  SECRET_INVENTORY="${SECRET_INVENTORY}${secret_name}|"
  if expected_surfaces="$(secret_expected_surfaces "$secret_name")"; then
    :
  else
    expected_surfaces=""
  fi
  secret_policy="$(read_cloud_json "Secret Manager inventory-item IAM policy" \
    gc secrets get-iam-policy "$secret_name" --format=json)" || exit 1
  if ! printf '%s' "$secret_policy" | \
      secret_iam_policy_contract_json verify "$secret_name" "$expected_surfaces"; then
    echo "ERROR: a Secret Manager resource has unsafe exact accessor IAM" >&2
    exit 1
  fi
done <<<"$SECRET_NAMES"

if [ -n "$MANIFEST_PATH" ]; then
  # The recovery manifest is intentionally non-secret.  Reject any injected
  # secret-like field before emitting the small, fixed candidate inventory.
  if ! MANIFEST_ROWS="$(python3 -c '
import json
import re
import sys

path, expected_project = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
except (OSError, json.JSONDecodeError):
    raise SystemExit("manifest is unreadable")

forbidden = re.compile(r"(?:^|_)(?:secret|token|password|credential|private_key)(?:_|$)", re.I)
def reject_sensitive_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if forbidden.search(str(key)):
                raise SystemExit("manifest contains a sensitive field")
            reject_sensitive_keys(item)
    elif isinstance(value, list):
        for item in value:
            reject_sensitive_keys(item)
reject_sensitive_keys(manifest)

if not isinstance(manifest, dict) or manifest.get("project_id") != expected_project:
    raise SystemExit("manifest project mismatch")
regions = manifest.get("regions")
internal_regions = manifest.get("internal_regions")
services = manifest.get("services")
if (
    not isinstance(regions, list)
    or not regions
    or not isinstance(internal_regions, list)
    or not internal_regions
    or not isinstance(services, list)
):
    raise SystemExit("manifest candidate inventory is malformed")
if len(set(regions)) != len(regions) or any(
    not isinstance(region, str) or not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region)
    for region in regions
):
    raise SystemExit("manifest region inventory is malformed")
if (
    len(set(internal_regions)) != len(internal_regions)
    or not set(regions).issubset(internal_regions)
    or any(
        not isinstance(region, str)
        or not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region)
        for region in internal_regions
    )
):
    raise SystemExit("manifest internal region inventory is malformed")

names = {
    "public": "trusted-router-public",
    "actions": "trusted-router-actions",
    "console": "trusted-router-console",
    "chat": "trusted-router-chat",
    "webhooks": "trusted-router-webhooks",
    "internal": "trusted-router-billing",
}
rows = []
seen = set()
for entry in services:
    if not isinstance(entry, dict):
        raise SystemExit("manifest candidate entry is malformed")
    surface = entry.get("surface")
    region = entry.get("region")
    name = entry.get("name")
    candidate = entry.get("candidate_revision")
    account = entry.get("runtime_service_account")
    allowed_regions = internal_regions if surface == "internal" else regions
    if surface not in names or region not in allowed_regions or name != names[surface]:
        raise SystemExit("manifest candidate entry is noncanonical")
    if account != f"tr-{surface}@{expected_project}.iam.gserviceaccount.com":
        raise SystemExit("manifest candidate identity is noncanonical")
    if not isinstance(candidate, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}", candidate):
        raise SystemExit("manifest candidate revision is malformed")
    if not candidate.startswith(name + "-"):
        raise SystemExit("manifest candidate revision does not belong to its service")
    key = (surface, region)
    if key in seen:
        raise SystemExit("manifest candidate inventory contains duplicates")
    seen.add(key)
    rows.append((surface, name, region, candidate, account))
expected = {
    (surface, region)
    for surface in names
    for region in (internal_regions if surface == "internal" else regions)
}
if seen != expected:
    raise SystemExit("manifest candidate inventory is not the complete surface-region matrix")
print("\n".join("\t".join(row) for row in sorted(rows)))
' "$MANIFEST_PATH" "$PROJECT_ID" 2>/dev/null)"; then
    echo "ERROR: rollout manifest candidate inventory is invalid" >&2
    exit 1
  fi

  while IFS=$'\t' read -r surface service_name region candidate account; do
    [ -n "$surface" ] || continue
    revision_json="$(read_cloud_json "candidate Cloud Run revision" \
      gc run revisions describe "$candidate" --region="$region" --format=json)" || exit 1
    if ! MOUNTED_REFS="$(printf '%s' "$revision_json" | python3 -c '
import json
import re
import sys

project, candidate, account = sys.argv[1:4]
revision = json.load(sys.stdin)
if not isinstance(revision, dict):
    raise SystemExit("candidate revision is malformed")
metadata = revision.get("metadata") or {}
spec = revision.get("spec") or {}
if metadata.get("name") != candidate or spec.get("serviceAccountName") != account:
    raise SystemExit("candidate revision identity mismatch")

prefix = f"projects/{project}/secrets/"
def normalize_name(value):
    if not isinstance(value, str):
        raise SystemExit("candidate secret reference is malformed")
    if value.startswith("projects/"):
        if not value.startswith(prefix):
            raise SystemExit("candidate has a cross-project secret reference")
        value = value[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        raise SystemExit("candidate secret reference is malformed")
    return value

def normalize_version(value):
    if isinstance(value, bool):
        raise SystemExit("candidate secret version is not numeric")
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise SystemExit("candidate secret version is not numeric")
    return value

refs = []
containers = spec.get("containers") or []
if not isinstance(containers, list) or not containers:
    raise SystemExit("candidate revision has no container inventory")
for container in containers:
    if not isinstance(container, dict):
        raise SystemExit("candidate container is malformed")
    for env in container.get("env") or []:
        if not isinstance(env, dict):
            raise SystemExit("candidate environment is malformed")
        reference = ((env.get("valueFrom") or {}).get("secretKeyRef"))
        if reference is None:
            continue
        if not isinstance(reference, dict):
            raise SystemExit("candidate secret reference is malformed")
        refs.append((normalize_name(reference.get("name")), normalize_version(
            reference.get("key", reference.get("version"))
        )))
for volume in spec.get("volumes") or []:
    if not isinstance(volume, dict):
        raise SystemExit("candidate volume is malformed")
    secret = volume.get("secret")
    if secret is None:
        continue
    if not isinstance(secret, dict):
        raise SystemExit("candidate secret volume is malformed")
    name = normalize_name(secret.get("secretName", secret.get("name")))
    items = secret.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit("candidate secret volume has an implicit version")
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit("candidate secret volume item is malformed")
        refs.append((name, normalize_version(item.get("key", item.get("version")))))
print("\n".join("\t".join(ref) for ref in sorted(set(refs))))
' "$PROJECT_ID" "$candidate" "$account" 2>/dev/null)"; then
      echo "ERROR: a candidate revision has malformed or unpinned mounted-secret metadata" >&2
      exit 1
    fi

    while IFS=$'\t' read -r mounted_secret mounted_version; do
      [ -n "$mounted_secret" ] || continue
      case "$SECRET_INVENTORY" in
        *"|${mounted_secret}|"*) ;;
        *)
          echo "ERROR: a candidate revision references a secret outside the verified project inventory" >&2
          exit 1
          ;;
      esac
      if mounted_owners="$(secret_expected_surfaces "$mounted_secret")"; then
        case " ${mounted_owners} " in
          *" ${surface} "*) ;;
          *)
            echo "ERROR: a candidate revision mounts a secret outside its declared surface ownership" >&2
            exit 1
            ;;
        esac
      else
        echo "ERROR: a candidate revision mounts a secret with no declared static owner set" >&2
        exit 1
      fi
      version_json="$(read_cloud_json "mounted secret-version status" \
        gc secrets versions describe "$mounted_version" \
          --secret="$mounted_secret" --format=json)" || exit 1
      if ! printf '%s' "$version_json" | python3 -c '
import json
import sys

project, secret, version = sys.argv[1:4]
value = json.load(sys.stdin)
if value.get("state") != "ENABLED":
    raise SystemExit("mounted secret version is not enabled")
name = value.get("name")
expected = f"projects/{project}/secrets/{secret}/versions/{version}"
if name is not None and name != expected:
    raise SystemExit("mounted secret version identity mismatch")
' "$PROJECT_ID" "$mounted_secret" "$mounted_version"; then
        echo "ERROR: a candidate revision has a disabled or mismatched mounted secret version" >&2
        exit 1
      fi
    done <<<"$MOUNTED_REFS"
  done <<<"$MANIFEST_ROWS"
fi

echo "six-surface rollout IAM verification passed"
