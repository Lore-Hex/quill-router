# shellcheck shell=bash
# Shared config + helpers for the deploy-gcp phase scripts. Sourced from
# scripts/deploy-gcp.sh (the orchestrator) and each phase under
# scripts/deploy/. Each phase script can also be run standalone for
# partial deploys (`scripts/deploy/rollout.sh` to redeploy without
# re-pushing secrets, etc.).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
REGION="${REGION:-us-central1}"
# Only enumerate regions where a real attested-gateway VM is deployed.
# The control plane (this Cloud Run service) gets deployed to each of
# these for low-latency dashboard reads, AND each entry shows up in
# /v1/regions which the SDK's region= shortcut resolves against. Adding
# a region without a backing gateway VM gives callers a TLS-broken
# `api-<region>.quillrouter.com` and weakens the trust story.
TR_REGIONS="${TR_REGIONS:-us-central1,us-east4,europe-west4,southamerica-east1}"
TR_PRIMARY_REGION="${TR_PRIMARY_REGION:-us-central1}"
# Cloud Run control-plane regions behind trustedrouter.com. This is broader
# than TR_REGIONS because cold control-plane regions can serve cached public
# pages without advertising non-existent regional attested gateway hostnames.
TR_CONTROL_PLANE_REGIONS="${TR_CONTROL_PLANE_REGIONS:-us-central1,us-east4,europe-west4,southamerica-east1}"
# Canonical regions for the four synthetic job families. infra.sh consumes
# the same inventory before retiring the legacy job identity, so changing a
# job region cannot silently leave the retirement preflight behind.
TR_SYNTHETIC_MONITOR_REGIONS="${TR_SYNTHETIC_MONITOR_REGIONS:-us-central1,europe-west4}"
TR_SYNTHETIC_THROUGHPUT_REGION="${TR_SYNTHETIC_THROUGHPUT_REGION:-us-central1}"
TR_SYNTHETIC_IMAGE_REGION="${TR_SYNTHETIC_IMAGE_REGION:-us-central1}"
TR_SYNTHETIC_VIDEO_REGION="${TR_SYNTHETIC_VIDEO_REGION:-us-central1}"
# Comma-separated subset of TR_REGIONS that should run with min_scale=1
# (always-on warm capacity). Anything in TR_REGIONS but NOT in
# TR_WARM_REGIONS gets min_scale=0 (scale-to-zero — ~$0/mo idle, cold-
# start tax on first request). Defaults to the regions where we run an
# attested enclave MIG. São Paulo is warm as well so its gateway does not pay
# a control-plane cold-start penalty on authorization or settlement.
TR_WARM_REGIONS="${TR_WARM_REGIONS:-us-central1,europe-west4,us-east4,southamerica-east1}"
# Cloud Run memory limit. 2Gi as of 2026-05-10.
#
# History of the bloat profile (RSS at idle, then under load):
#   pre-fix:   ~600-900 MB at concurrency=4 → OOM at 1Gi
#   post-fix:  ~150-250 MB at concurrency=2 → comfortable at 1Gi
#
# Where the bytes go (measured 2026-05-10 with tr_mem_profile2.py):
#   ~85 MB    google-cloud SDK imports (Spanner gRPC stubs, Bigtable,
#             KMS, protobuf descriptors) — unavoidable floor
#   ~50 MB    Spanner FixedSizePool(size=10) (SDK default) at first
#             use, ~5 MB per gRPC session × 10 sessions; reduced to
#             FixedSizePool(size=4) in storage_gcp.py → ~30 MB saved
#   ~20 MB    FastAPI + Pydantic + Starlette + uvicorn
#   ~25 MB    create_app() route registration (244 routes worth of
#             Pydantic dataclass shape metadata + dependency graphs)
#   ~50-200 MB peak per in-flight request × concurrency
#             (httpx connection pool + JSON parsing + gRPC streams).
#             Halved by `--concurrency=2` in rollout.sh.
#   ~10 MB    Sentry SDK breadcrumb + transport buffers
#
# Lazy-imported only when their first route is hit (not in startup):
#   eth_account (~13 MB)  — wallet OAuth route
#   boto3 (~3 MB)         — SES email send
#
# 2Gi is kept (not lowered to 1Gi) because the per-request peak under
# spiky bursts can still pin a single instance; the surplus is cheap.
TR_CLOUD_RUN_MEMORY="${TR_CLOUD_RUN_MEMORY:-2Gi}"
# The public service must only be reachable from the external Application Load
# Balancer or from an explicitly private Google path. Authentication is a
# separate control: public pages remain unauthenticated, but the run.app origin
# is not an Internet bypass around Cloud Armor.
TR_CLOUD_RUN_INGRESS="${TR_CLOUD_RUN_INGRESS:-internal-and-cloud-load-balancing}"
# The legacy combined service has trusted regional synthetic jobs that use its
# run.app URL through Private Google Access, so it keeps that URL by default.
# Split public/control/billing services override this independently and disable
# the URL when they have no trusted direct consumer.
TR_CLOUD_RUN_DISABLE_DEFAULT_URL="${TR_CLOUD_RUN_DISABLE_DEFAULT_URL:-0}"
# Service-level cost/saturation bulkhead. Split services set their own value;
# 20 per region is the safe intermediate ceiling for the legacy combined
# service. rollout.sh uses Cloud Run's mutable --max service cap, not the
# per-revision --max-instances setting, so staged traffic cannot double it.
TR_CLOUD_RUN_MAX_INSTANCES="${TR_CLOUD_RUN_MAX_INSTANCES:-20}"
TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM="${TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM:-0}"
SERVICE="${SERVICE:-trusted-router}"
# The split console is a companion service. Keep the old combined monolith
# under an explicit name so initial-migration discovery and rollback cannot
# accidentally treat it as the console-only target.
CONSOLE_SERVICE="${TR_CONSOLE_SERVICE:-${SERVICE}-console}"
LEGACY_CONSOLE_SERVICE="${TR_LEGACY_CONSOLE_SERVICE:-$SERVICE}"
PUBLIC_SERVICE="${TR_PUBLIC_SERVICE:-${SERVICE}-public}"
ACTIONS_SERVICE="${TR_ACTIONS_SERVICE:-${SERVICE}-actions}"
CHAT_SERVICE="${TR_CHAT_SERVICE:-${SERVICE}-chat}"
WEBHOOKS_SERVICE="${TR_WEBHOOKS_SERVICE:-${SERVICE}-webhooks}"
# TR_BILLING_SERVICE predates the process-role name `internal` and is also
# consumed by synthetic.sh. Keep it as a compatibility/input alias while the
# application revision itself remains TR_SERVICE_SURFACE=internal.
INTERNAL_SERVICE="${TR_INTERNAL_SERVICE:-${TR_BILLING_SERVICE:-${SERVICE}-billing}}"
TR_BILLING_SERVICE="${TR_BILLING_SERVICE:-$INTERNAL_SERVICE}"

# Each process role names a dedicated, pre-provisioned runtime identity.
# Provisioning and verification remain explicit operator prerequisites.
PUBLIC_RUN_SERVICE_ACCOUNT="${TR_PUBLIC_RUN_SERVICE_ACCOUNT:-tr-public@${PROJECT_ID}.iam.gserviceaccount.com}"
ACTIONS_RUN_SERVICE_ACCOUNT="${TR_ACTIONS_RUN_SERVICE_ACCOUNT:-tr-actions@${PROJECT_ID}.iam.gserviceaccount.com}"
CONSOLE_RUN_SERVICE_ACCOUNT="${TR_CONSOLE_RUN_SERVICE_ACCOUNT:-tr-console@${PROJECT_ID}.iam.gserviceaccount.com}"
CHAT_RUN_SERVICE_ACCOUNT="${TR_CHAT_RUN_SERVICE_ACCOUNT:-tr-chat@${PROJECT_ID}.iam.gserviceaccount.com}"
WEBHOOKS_RUN_SERVICE_ACCOUNT="${TR_WEBHOOKS_RUN_SERVICE_ACCOUNT:-tr-webhooks@${PROJECT_ID}.iam.gserviceaccount.com}"
INTERNAL_RUN_SERVICE_ACCOUNT="${TR_INTERNAL_RUN_SERVICE_ACCOUNT:-tr-internal@${PROJECT_ID}.iam.gserviceaccount.com}"
SYNTHETIC_RUN_SERVICE_ACCOUNT="${TR_SYNTHETIC_RUN_SERVICE_ACCOUNT:-tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com}"
DEPLOY_SERVICE_ACCOUNT="${TR_DEPLOY_SERVICE_ACCOUNT:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
# Exact direct principals, managed outside this deploy, that may impersonate a
# split runtime. Each listed member may hold only one unconditional
# roles/iam.serviceAccountUser binding. Runtime identities, the deploy identity,
# wildcard principals, TokenCreator, and arbitrary roles are never admitted by
# this escape hatch. Promotion can feed its already-fetched policy JSON into
# verify_runtime_service_account_policy_json below.
TR_RUNTIME_SERVICE_ACCOUNT_OPERATOR_MEMBERS="${TR_RUNTIME_SERVICE_ACCOUNT_OPERATOR_MEMBERS:-}"
# Exact non-runtime accessors that an owner has explicitly reviewed and wants
# to preserve while secrets.sh replaces resource policies. The value is JSON:
# {"secret-name":["serviceAccount:consumer@project.iam.gserviceaccount.com"]}.
# Conditions, public principals, runtime/synthetic/deploy identities, arbitrary
# roles, and wildcard secret names are deliberately unsupported.
TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON="${TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON:-}"
if [ -z "$TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON" ]; then
  TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON='{}'
fi
RUNTIME_SERVICE_ACCOUNTS=(
  "$PUBLIC_RUN_SERVICE_ACCOUNT"
  "$ACTIONS_RUN_SERVICE_ACCOUNT"
  "$CONSOLE_RUN_SERVICE_ACCOUNT"
  "$CHAT_RUN_SERVICE_ACCOUNT"
  "$WEBHOOKS_RUN_SERVICE_ACCOUNT"
  "$INTERNAL_RUN_SERVICE_ACCOUNT"
)
REPO="${REPO:-trusted-router}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
KEY_FILE="${TR_LOCAL_KEYS_FILE:-${HOME}/.quill_cloud_keys.private}"
SPANNER_INSTANCE_ID="${TR_SPANNER_INSTANCE_ID:-trusted-router-nam6}"
SPANNER_DATABASE_ID="${TR_SPANNER_DATABASE_ID:-trusted-router}"
SPANNER_CONFIG="${TR_SPANNER_CONFIG:-nam6}"
SPANNER_EDITION="${TR_SPANNER_EDITION:-ENTERPRISE_PLUS}"
SPANNER_PROCESSING_UNITS="${TR_SPANNER_PROCESSING_UNITS:-300}"
BIGTABLE_INSTANCE_ID="${TR_BIGTABLE_INSTANCE_ID:-trusted-router-logs}"
BIGTABLE_CLUSTER_ID="${TR_BIGTABLE_CLUSTER_ID:-trusted-router-logs-c1}"
BIGTABLE_APP_PROFILE_ID="${TR_BIGTABLE_APP_PROFILE_ID:-}"
BIGTABLE_GENERATION_TABLE="${TR_BIGTABLE_GENERATION_TABLE:-trustedrouter-generations}"
BIGTABLE_INSTANCE_TYPE="${TR_BIGTABLE_INSTANCE_TYPE:-PRODUCTION}"
KMS_KEYRING_ID="${TR_KMS_KEYRING_ID:-trusted-router}"
BYOK_KMS_KEY_ID="${TR_BYOK_KMS_KEY_ID:-byok-envelope}"
BYOK_KMS_KEY_NAME="${TR_BYOK_KMS_KEY_NAME:-projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING_ID}/cryptoKeys/${BYOK_KMS_KEY_ID}}"
GOOGLE_ADS_KMS_KEY_ID="${TR_GOOGLE_DATA_MANAGER_KMS_KEY_ID:-google-ads-click-envelope}"
GOOGLE_ADS_KMS_KEY_NAME="${TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME:-projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING_ID}/cryptoKeys/${GOOGLE_ADS_KMS_KEY_ID}}"
TRUST_FILE="${TRUST_FILE:-/Users/jperla/claude/quill-cloud-proxy/trust-page/gcp-release.json}"
TRUST_FILE_URL="${TRUST_FILE_URL:-https://trust.trustedrouter.com/trust/gcp-release.json}"

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
gc() { gcloud --project "$PROJECT_ID" "$@"; }

PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
# Compatibility for GCP synthetic jobs only. Cloud Run services must use one
# of the six RUNTIME_SERVICE_ACCOUNTS above. infra.sh references this legacy
# identity only in the guarded post-cutover retirement path; secrets.sh never
# grants it split-service access. Synthetic Scheduler jobs still name it
# directly and are verified separately.
RUN_SERVICE_ACCOUNT="${RUN_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

# Canonical Secret Manager ownership for the six FastAPI surfaces. Unknown
# resources intentionally return nonzero and therefore have zero runtime
# owners. Keep this single table shared by owner reconciliation and every
# read-only rollout gate.
secret_expected_surfaces() {
  case "$1" in
    trustedrouter-attribution-cookie-secret) echo "public console" ;;
    trustedrouter-sentry-dsn) echo "console chat webhooks internal" ;;
    trustedrouter-stripe-secret-key) echo "console" ;;
    trustedrouter-internal-stripe-payment-intents-key) echo "internal" ;;
    trustedrouter-stripe-webhook-secret) echo "webhooks" ;;
    trustedrouter-internal-gateway-token|trustedrouter-observer-internal-token|trustedrouter-synthetic-monitor-api-key|trustedrouter-federation-peer-token|trustedrouter-federation-home-token|trustedrouter-federation-credit-inbound-token|trustedrouter-federation-credit-peer-token|trustedrouter-federation-settlement-inbound-tokens|trustedrouter-federation-settlement-home-token) echo "internal" ;;
    trustedrouter-aws-access-key-id|trustedrouter-aws-secret-access-key) echo "actions console" ;;
    trustedrouter-internal-ses-access-key-id|trustedrouter-internal-ses-secret-access-key) echo "internal" ;;
    trustedrouter-ops-chat-webhook-secret) echo "actions" ;;
    trustedrouter-google-client-id|trustedrouter-google-client-secret|trustedrouter-google-alias-credentials-json|trustedrouter-github-client-id|trustedrouter-github-client-secret|trustedrouter-github-alias-credentials-json) echo "console" ;;
    trustedrouter-paypal-client-id|trustedrouter-paypal-client-secret) echo "console webhooks" ;;
    trustedrouter-paypal-webhook-id) echo "webhooks" ;;
    trustedrouter-adyen-test-api-key|trustedrouter-adyen-test-client-key) echo "console" ;;
    trustedrouter-adyen-test-hmac-key) echo "webhooks" ;;
    trustedrouter-adyen-test-reference-key) echo "console webhooks" ;;
    trustedrouter-veriff-api-key) echo "console" ;;
    trustedrouter-veriff-shared-secret-key) echo "webhooks" ;;
    trustedrouter-telnyx-api-key|trustedrouter-twilio-account-sid|trustedrouter-twilio-api-key-sid|trustedrouter-twilio-api-key-secret|trustedrouter-twilio-auth-token) echo "console" ;;
    trustedrouter-clickhouse-provider-read-password) echo "console" ;;
    trustedrouter-clickhouse-control-read-password) echo "public console internal" ;;
    trustedrouter-clickhouse-password) echo "console internal" ;;
    trustedrouter-axiom-api-token) echo "" ;;
    trustedrouter-anthropic-api-key|trustedrouter-openai-api-key|trustedrouter-openai-video-api-key|trustedrouter-gemini-api-key|trustedrouter-cerebras-api-key|trustedrouter-deepseek-api-key|trustedrouter-mistral-api-key|trustedrouter-kimi-api-key|trustedrouter-zai-api-key|trustedrouter-together-api-key|trustedrouter-fireworks-api-key|trustedrouter-deepinfra-api-key|trustedrouter-cohere-api-key|trustedrouter-voyage-api-key|trustedrouter-xiaomi-api-key|trustedrouter-grok-api-key|trustedrouter-novita-api-key|trustedrouter-phala-api-key|trustedrouter-phala-confidential-api-key|trustedrouter-siliconflow-api-key|trustedrouter-tinfoil-api-key|trustedrouter-venice-api-key|trustedrouter-nebius-api-key|trustedrouter-minimax-api-key|trustedrouter-friendli-api-key|trustedrouter-baseten-api-key|trustedrouter-thinking-machines-api-key|trustedrouter-wafer-api-key|trustedrouter-crusoe-api-key|trustedrouter-makora-api-key|trustedrouter-alibaba-api-key|trustedrouter-ltx-api-key|trustedrouter-runway-api-key|trustedrouter-kling-api-key|trustedrouter-chutes-api-key|trustedrouter-digitalocean-api-key|trustedrouter-cloudflare-workers-ai-api-token|trustedrouter-inceptron-api-key|trustedrouter-morph-api-key|trustedrouter-atlas-cloud-api-key|trustedrouter-streamlake-api-key|trustedrouter-neurometric-api-key|trustedrouter-engy-api-key|trustedrouter-pearl-api-key|trustedrouter-zero-g-api-key|trustedrouter-athena-worker-prompt-v1|trustedrouter-gcp-service-account-key-json) echo "" ;;
    *) return 1 ;;
  esac
}

# The deploy identity reads only the exact secrets consumed by the checked-in
# price refresh workflow. Historic operational/Veriff/Adyen/ClickHouse grants
# are intentionally absent and are removed by owner reconciliation.
deploy_service_account_owns_secret() {
  case "$1" in
    trustedrouter-tr-api-key-for-self-heal|trustedrouter-together-api-key|trustedrouter-parasail-api-key|trustedrouter-lightning-api-key|trustedrouter-gmi-api-key|trustedrouter-deepinfra-api-key|trustedrouter-phala-confidential-api-key|trustedrouter-siliconflow-api-key|trustedrouter-venice-api-key|trustedrouter-openai-api-key|trustedrouter-grok-api-key|trustedrouter-deepseek-api-key|trustedrouter-mistral-api-key|trustedrouter-zai-api-key|trustedrouter-cerebras-api-key|trustedrouter-kimi-api-key|trustedrouter-fireworks-api-key|trustedrouter-gemini-api-key|trustedrouter-novita-api-key|trustedrouter-nebius-api-key|trustedrouter-minimax-api-key|trustedrouter-crusoe-api-key|trustedrouter-friendli-api-key|trustedrouter-baseten-api-key|trustedrouter-telnyx-api-key|trustedrouter-wafer-api-key|trustedrouter-alibaba-api-key|trustedrouter-makora-api-key|trustedrouter-chutes-api-key|trustedrouter-digitalocean-api-key|trustedrouter-cloudflare-workers-ai-api-token|trustedrouter-inceptron-api-key|trustedrouter-morph-api-key|trustedrouter-atlas-cloud-api-key|trustedrouter-streamlake-api-key|trustedrouter-neurometric-api-key|trustedrouter-engy-api-key|trustedrouter-pearl-api-key|trustedrouter-zero-g-api-key) return 0 ;;
    *) return 1 ;;
  esac
}

synthetic_service_account_owns_secret() {
  case "$1" in
    trustedrouter-observer-internal-token|trustedrouter-synthetic-monitor-api-key) return 0 ;;
    *) return 1 ;;
  esac
}

# Read a Secret Manager policy on stdin. `verify` requires the canonical owners
# and permits only explicitly allowlisted unrelated accessors. `plan` emits
# narrowly targeted add/remove operations for the six runtimes, deploy,
# synthetic, and public principals. It never rewrites or removes an unrelated
# non-public principal; unallowlisted drift fails before mutation.
secret_iam_policy_contract_json() {
  local mode="$1"
  local secret_name="$2"
  local expected_surfaces="${3:-}"
  local deploy_expected=0
  local synthetic_expected=0
  local runtime_csv
  case "$mode" in
    verify|plan) ;;
    *) echo "ERROR: invalid secret IAM policy contract mode ${mode}" >&2; return 2 ;;
  esac
  deploy_service_account_owns_secret "$secret_name" && deploy_expected=1
  synthetic_service_account_owns_secret "$secret_name" && synthetic_expected=1
  runtime_csv="$(IFS=,; echo "${RUNTIME_SERVICE_ACCOUNTS[*]}")"
  python3 -c '
import json
import re
import sys

(
    mode,
    secret,
    expected_surfaces,
    accounts_csv,
    deploy_account,
    synthetic_account,
    deploy_expected,
    synthetic_expected,
    preserved_raw,
) = sys.argv[1:]
surfaces = ("public", "actions", "console", "chat", "webhooks", "internal")
accounts = accounts_csv.split(",")
if len(accounts) != 6 or len(set(accounts)) != 6:
    raise SystemExit("runtime identity inventory is not exactly six")
wanted_surfaces = expected_surfaces.split()
if len(set(wanted_surfaces)) != len(wanted_surfaces) or not set(wanted_surfaces).issubset(surfaces):
    raise SystemExit("secret runtime owner set is invalid")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", secret):
    raise SystemExit("secret resource identifier is invalid")

try:
    preserved = json.loads(preserved_raw)
except json.JSONDecodeError:
    raise SystemExit("TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON is invalid JSON") from None
if not isinstance(preserved, dict):
    raise SystemExit("TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON must be an object")

canonical_accounts = set(accounts) | {deploy_account, synthetic_account}
preserved_for_secret = []
valid_member_prefixes = (
    "user:",
    "group:",
    "serviceAccount:",
    "domain:",
    "principal://",
    "principalSet://",
)
for name, members in preserved.items():
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name):
        raise SystemExit("preserved secret accessor map has an invalid resource name")
    if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
        raise SystemExit(f"preserved secret accessor list for {name} is malformed")
    if len(members) != len(set(members)):
        raise SystemExit(f"preserved secret accessor list for {name} has duplicates")
    for member in members:
        if member in {"allUsers", "allAuthenticatedUsers"}:
            raise SystemExit("public principals cannot be preserved on secrets")
        if (
            member.startswith("deleted:")
            or not member.startswith(valid_member_prefixes)
            or member != member.strip()
            or any(character.isspace() for character in member)
        ):
            raise SystemExit(f"preserved secret accessor {member!r} is noncanonical")
        raw_account = member.removeprefix("serviceAccount:")
        if raw_account in canonical_accounts:
            raise SystemExit("canonical runtime/deploy/synthetic owners cannot be overridden")
    if name == secret:
        preserved_for_secret = members

canonical_wanted = {
    "serviceAccount:" + account
    for surface, account in zip(surfaces, accounts)
    if surface in wanted_surfaces
}
if deploy_expected == "1":
    canonical_wanted.add("serviceAccount:" + deploy_account)
if synthetic_expected == "1":
    canonical_wanted.add("serviceAccount:" + synthetic_account)
managed_members = {"serviceAccount:" + account for account in canonical_accounts}
preserved_members = set(preserved_for_secret)

policy = json.load(sys.stdin)
if not isinstance(policy, dict) or not isinstance(policy.get("bindings", []), list):
    raise SystemExit("Secret Manager IAM policy is malformed")
observed = []
for binding in policy.get("bindings", []):
    if not isinstance(binding, dict):
        raise SystemExit("Secret Manager IAM binding is malformed")
    role = binding.get("role")
    members = binding.get("members")
    condition = binding.get("condition") if "condition" in binding else None
    if not isinstance(role, str) or not isinstance(members, list) or not members:
        raise SystemExit("Secret Manager IAM binding is malformed")
    if len(members) != len(set(members)) or any(not isinstance(member, str) for member in members):
        raise SystemExit("Secret Manager IAM member inventory is malformed")
    for member in members:
        observed.append((role, member, condition))

present_managed = set()
present_preserved = set()
present_public = set()
operations = []
for role, member, condition in observed:
    if member in {"allUsers", "allAuthenticatedUsers"}:
        if condition is not None:
            raise SystemExit("conditional public secret IAM must be removed manually")
        public_entry = (role, member)
        if public_entry in present_public:
            raise SystemExit("public secret principal has duplicate bindings")
        present_public.add(public_entry)
        if mode == "plan":
            operations.append(("remove", role, member))
        else:
            raise SystemExit("public principal is forbidden on Secret Manager")
        continue
    if member in managed_members:
        if role != "roles/secretmanager.secretAccessor" or condition is not None:
            raise SystemExit("managed secret principal has a noncanonical role or condition")
        if member in present_managed:
            raise SystemExit("managed secret principal has duplicate bindings")
        present_managed.add(member)
        if member not in canonical_wanted:
            if mode == "plan":
                operations.append(("remove", role, member))
            else:
                raise SystemExit("managed non-owner retains secret access")
        continue
    if member not in preserved_members:
        raise SystemExit("unapproved unrelated secret principal must be explicitly allowlisted")
    if role != "roles/secretmanager.secretAccessor" or condition is not None:
        raise SystemExit("preserved unrelated secret principal must be an unconditional accessor")
    if member in present_preserved:
        raise SystemExit("preserved unrelated secret principal has duplicate bindings")
    present_preserved.add(member)

missing = sorted(canonical_wanted - present_managed)
if mode == "verify" and missing:
    raise SystemExit("canonical secret owner is missing")
if mode == "plan":
    operations.extend(
        ("add", "roles/secretmanager.secretAccessor", member)
        for member in missing
    )
    for action, role, member in sorted(operations):
        print(action, role, member, sep="\t")
' "$mode" "$secret_name" "$expected_surfaces" "$runtime_csv" \
    "$DEPLOY_SERVICE_ACCOUNT" "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
    "$deploy_expected" "$synthetic_expected" \
    "$TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON"
}

validate_nonempty_region_list() {
  local variable_name="$1"
  local value="$2"
  local seen="|"
  local -a regions
  local region
  case "$value" in
    ''|,*|*,|*,,*)
      echo "ERROR: ${variable_name} must be a nonempty comma-separated region list" >&2
      return 1
      ;;
  esac
  IFS=',' read -ra regions <<<"$value"
  for region in "${regions[@]}"; do
    if ! [[ "$region" =~ ^[a-z][a-z0-9-]*[a-z0-9]$ ]]; then
      echo "ERROR: ${variable_name} contains invalid region '${region}'" >&2
      return 1
    fi
    case "$seen" in
      *"|${region}|"*)
        echo "ERROR: ${variable_name} contains duplicate region ${region}" >&2
        return 1
        ;;
    esac
    seen="${seen}${region}|"
  done
}

# Emit every direct binding for an exact principal as a stable token. IAM
# conditions are deliberately preserved: flatten/value queries discard that
# distinction and can make a time- or resource-conditioned grant look like the
# unconditional binding a runtime contract requires.
iam_direct_binding_tokens_for_member() {
  local member="$1"
  shift
  local policy_json
  policy_json="$("$@" --format=json)" || return 1
  printf '%s' "$policy_json" | python3 -c '
import json
import sys

member = sys.argv[1]
policy = json.load(sys.stdin)
tokens = []
for binding in policy.get("bindings", []) or []:
    if member not in (binding.get("members", []) or []):
        continue
    role = binding.get("role")
    if not isinstance(role, str) or not role:
        raise SystemExit("direct IAM binding has no role")
    prefix = "conditional:" if "condition" in binding and binding["condition"] is not None else ""
    tokens.append(prefix + role)
for token in sorted(tokens):
    print(token)
' "$member"
}

iam_member_has_unconditional_role() {
  local member="$1"
  local role="$2"
  shift 2
  local tokens
  tokens="$(iam_direct_binding_tokens_for_member "$member" "$@")" || return 1
  grep -Fxq "$role" <<<"$tokens"
}

verify_exact_unconditional_roles() {
  local label="$1"
  local member="$2"
  local expected_csv="$3"
  shift 3
  local actual
  local expected
  actual="$(iam_direct_binding_tokens_for_member "$member" "$@")" || {
    echo "ERROR: cannot read ${label} IAM policy for ${member}" >&2
    return 1
  }
  expected="$(printf '%s' "$expected_csv" | tr ',' '\n' | sed '/^$/d' | LC_ALL=C sort)"
  if [ "$actual" != "$expected" ]; then
    echo "ERROR: ${member} has ${label} bindings '${actual:-<none>}', expected unconditional '${expected:-<none>}'" >&2
    return 1
  fi
}

# Before a removal pass, prove that every direct binding is both
# unconditional and in the finite set that the pass knows how to reconcile.
# Unknown/custom/conditional grants stop the run before its first mutation.
preflight_reconcilable_direct_roles() {
  local label="$1"
  local member="$2"
  local allowed_csv="$3"
  shift 3
  local tokens
  local token
  local role
  tokens="$(iam_direct_binding_tokens_for_member "$member" "$@")" || {
    echo "ERROR: cannot inventory ${label} IAM policy for ${member}" >&2
    return 1
  }
  while IFS= read -r token; do
    [ -n "$token" ] || continue
    case "$token" in
      conditional:*)
        echo "ERROR: ${member} has unsupported conditional ${label} binding ${token#conditional:}" >&2
        return 1
        ;;
    esac
    role="$token"
    case ",${allowed_csv}," in
      *",${role},"*) ;;
      *)
        echo "ERROR: ${member} has unreconcilable ${label} binding ${role}" >&2
        return 1
        ;;
    esac
  done <<<"$tokens"
}

verify_no_direct_roles() {
  local label="$1"
  local member="$2"
  local forbidden_csv="$3"
  shift 3
  local tokens
  local token
  local role
  tokens="$(iam_direct_binding_tokens_for_member "$member" "$@")" || {
    echo "ERROR: cannot inspect ${label} IAM policy for ${member}" >&2
    return 1
  }
  while IFS= read -r token; do
    [ -n "$token" ] || continue
    role="${token#conditional:}"
    case ",${forbidden_csv}," in
      *",${role},"*)
        echo "ERROR: ${member} retains forbidden ${label} binding ${token}" >&2
        return 1
        ;;
    esac
  done <<<"$tokens"
}

is_runtime_service_account() {
  local candidate="$1"
  local runtime_account
  for runtime_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
    if [ "$candidate" = "$runtime_account" ]; then
      return 0
    fi
  done
  return 1
}

# Validate the complete direct IAM policy on one of the six runtime service
# accounts. This pure JSON contract is shared so provisioning and promotion can
# apply identical semantics without flattening away conditions.
verify_runtime_service_account_policy_json() {
  local target_account="$1"
  local phase="$2"
  local operator_members_csv="${3:-$TR_RUNTIME_SERVICE_ACCOUNT_OPERATOR_MEMBERS}"
  python3 -c '
import json
import sys

target, phase, deploy_account, runtime_csv, operator_csv = sys.argv[1:6]
if phase not in {"preflight", "post"}:
    raise SystemExit(f"unknown runtime service-account policy phase {phase!r}")

deploy_member = "serviceAccount:" + deploy_account
runtime_members = {"serviceAccount:" + item for item in runtime_csv.split(",") if item}
if "serviceAccount:" + target not in runtime_members:
    raise SystemExit(f"policy target {target!r} is not one of the six runtimes")

operators = operator_csv.split(",") if operator_csv else []
if any(not item or item != item.strip() for item in operators):
    raise SystemExit("operator member allowlist contains an empty or padded item")
if len(set(operators)) != len(operators):
    raise SystemExit("operator member allowlist contains duplicates")
valid_prefixes = ("user:", "group:", "serviceAccount:", "domain:", "principal://", "principalSet://")
for operator in operators:
    if not operator.startswith(valid_prefixes):
        raise SystemExit(f"invalid operator IAM member {operator!r}")
    if operator == deploy_member or operator in runtime_members:
        raise SystemExit(f"operator allowlist must not contain deploy/runtime member {operator}")

policy = json.load(sys.stdin)
deploy_bindings = []
operator_bindings = []
for binding in policy.get("bindings", []) or []:
    role = binding.get("role")
    members = binding.get("members", []) or []
    if not isinstance(role, str) or not role:
        raise SystemExit("runtime service-account policy binding has no role")
    if not isinstance(members, list):
        raise SystemExit("runtime service-account policy binding members are not a list")
    condition = binding.get("condition") if "condition" in binding else None
    for member in members:
        if not isinstance(member, str) or not member:
            raise SystemExit("runtime service-account policy has an invalid member")
        entry = (role, condition)
        if member in runtime_members:
            raise SystemExit(
                f"split runtime principal {member} has a direct binding on {target}"
            )
        if member == deploy_member:
            deploy_bindings.append(entry)
            continue
        if member in operators:
            operator_bindings.append((member, role, condition))
            continue
        raise SystemExit(f"unapproved principal {member} has a direct binding on {target}")

expected = [("roles/iam.serviceAccountUser", None)]
if phase == "post" and deploy_bindings != expected:
    raise SystemExit(
        f"deploy principal bindings are {deploy_bindings!r}, expected {expected!r}"
    )
if phase == "preflight" and deploy_bindings not in ([], expected):
    raise SystemExit(f"unsafe preexisting deploy bindings {deploy_bindings!r}")
seen_operators = set()
for member, role, condition in operator_bindings:
    if member in seen_operators:
        raise SystemExit(f"operator {member} has duplicate direct bindings on {target}")
    seen_operators.add(member)
    if role != "roles/iam.serviceAccountUser" or condition is not None:
        raise SystemExit(
            f"operator {member} must have only unconditional serviceAccountUser"
        )
' "$target_account" "$phase" "$DEPLOY_SERVICE_ACCOUNT" \
    "$(IFS=,; echo "${RUNTIME_SERVICE_ACCOUNTS[*]}")" \
    "$operator_members_csv"
}

validate_runtime_service_accounts() {
  local expected_suffix="@${PROJECT_ID}.iam.gserviceaccount.com"
  local seen="|"
  local service_account
  local account_id
  for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do
    if [ "$service_account" = "$RUN_SERVICE_ACCOUNT" ]; then
      echo "ERROR: Cloud Run surface must not reuse legacy RUN_SERVICE_ACCOUNT" >&2
      return 1
    fi
    case "$service_account" in
      *"$expected_suffix") ;;
      *)
        echo "ERROR: runtime service account must belong to ${PROJECT_ID}: ${service_account}" >&2
        return 1
        ;;
    esac
    account_id="${service_account%@*}"
    if ! [[ "$account_id" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
      echo "ERROR: invalid runtime service account id: ${account_id}" >&2
      return 1
    fi
    case "$seen" in
      *"|${service_account}|"*)
        echo "ERROR: runtime service accounts must be distinct: ${service_account}" >&2
        return 1
        ;;
    esac
    seen="${seen}${service_account}|"
  done
  if [ "$DEPLOY_SERVICE_ACCOUNT" = "$PUBLIC_RUN_SERVICE_ACCOUNT" ] ||
     [ "$DEPLOY_SERVICE_ACCOUNT" = "$ACTIONS_RUN_SERVICE_ACCOUNT" ] ||
     [ "$DEPLOY_SERVICE_ACCOUNT" = "$CONSOLE_RUN_SERVICE_ACCOUNT" ] ||
     [ "$DEPLOY_SERVICE_ACCOUNT" = "$CHAT_RUN_SERVICE_ACCOUNT" ] ||
     [ "$DEPLOY_SERVICE_ACCOUNT" = "$WEBHOOKS_RUN_SERVICE_ACCOUNT" ] ||
     [ "$DEPLOY_SERVICE_ACCOUNT" = "$INTERNAL_RUN_SERVICE_ACCOUNT" ]; then
    echo "ERROR: deploy service account must not be a runtime identity" >&2
    return 1
  fi
  case "$DEPLOY_SERVICE_ACCOUNT" in
    *"$expected_suffix") ;;
    *)
      echo "ERROR: deploy service account must belong to ${PROJECT_ID}" >&2
      return 1
      ;;
  esac
  account_id="${DEPLOY_SERVICE_ACCOUNT%@*}"
  if ! [[ "$account_id" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
    echo "ERROR: invalid deploy service account id: ${account_id}" >&2
    return 1
  fi
  if [ "$DEPLOY_SERVICE_ACCOUNT" = "$RUN_SERVICE_ACCOUNT" ]; then
    echo "ERROR: deploy service account must not reuse legacy RUN_SERVICE_ACCOUNT" >&2
    return 1
  fi
  if [ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" != "tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com" ]; then
    echo "ERROR: synthetic jobs require canonical tr-synthetic identity in ${PROJECT_ID}" >&2
    return 1
  fi
  if [ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" = "$RUN_SERVICE_ACCOUNT" ] ||
     [ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" = "$DEPLOY_SERVICE_ACCOUNT" ] ||
     is_runtime_service_account "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; then
    echo "ERROR: synthetic job identity must be distinct from deploy, legacy, and six runtimes" >&2
    return 1
  fi
  if ! printf '%s' "$TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON" | python3 -c '
import json
import sys

try:
    value = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(1) from None
if not isinstance(value, dict):
    raise SystemExit(1)
'; then
    echo "ERROR: TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON must be a JSON object" >&2
    return 1
  fi
  case "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" in
    0|1) ;;
    *)
      echo "ERROR: TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM must be 0 or 1" >&2
      return 1
      ;;
  esac
  if [ "$TR_BILLING_SERVICE" != "$INTERNAL_SERVICE" ]; then
    echo "ERROR: TR_BILLING_SERVICE and TR_INTERNAL_SERVICE must resolve to the same Cloud Run service" >&2
    return 1
  fi
  validate_nonempty_region_list TR_CONTROL_PLANE_REGIONS "$TR_CONTROL_PLANE_REGIONS"
  validate_nonempty_region_list TR_SYNTHETIC_MONITOR_REGIONS "$TR_SYNTHETIC_MONITOR_REGIONS"
  validate_nonempty_region_list TR_SYNTHETIC_THROUGHPUT_REGION "$TR_SYNTHETIC_THROUGHPUT_REGION"
  validate_nonempty_region_list TR_SYNTHETIC_IMAGE_REGION "$TR_SYNTHETIC_IMAGE_REGION"
  validate_nonempty_region_list TR_SYNTHETIC_VIDEO_REGION "$TR_SYNTHETIC_VIDEO_REGION"
}

read_key_file_var() {
  local env_name="$1"
  shift || true
  if [ ! -f "$KEY_FILE" ]; then
    return 0
  fi
  python3 - "$KEY_FILE" "$env_name" "$@" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
wanted = sys.argv[2:]
values = {}
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
for name in wanted:
    if values.get(name):
        print(values[name])
        break
PY
}

ensure_secret_value() {
  local secret_name="$1"
  local value="$2"
  if ! gc secrets describe "$secret_name" >/dev/null 2>&1; then
    printf '%s' "$value" | gc secrets create "$secret_name" \
      --replication-policy=automatic \
      --data-file=-
  else
    local current
    current="$(gc secrets versions access latest --secret="$secret_name" 2>/dev/null || true)"
    if [ "$current" != "$value" ]; then
      printf '%s' "$value" | gc secrets versions add "$secret_name" --data-file=- >/dev/null
    fi
  fi
}

ensure_project_role() {
  local member="$1"
  local role="$2"
  if iam_member_has_unconditional_role \
      "$member" "$role" gc projects get-iam-policy "$PROJECT_ID" 2>/dev/null; then
    return 0
  fi

  # Retry only etag conflicts. Retrying an authorization failure creates a
  # burst of misleading ERROR audit entries and cannot make the call succeed.
  local attempt=0
  local max_attempts=6
  local last_stderr=""
  while [ "$attempt" -lt "$max_attempts" ]; do
    if last_stderr="$(gc projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$member" \
        --role="$role" \
        --condition=None \
        --quiet 2>&1 >/dev/null)"; then
      return 0
    fi
    attempt=$((attempt + 1))
    if echo "$last_stderr" | grep -qE 'PERMISSION_DENIED|setIamPolicy|Policy update access denied'; then
      break
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$attempt"
    fi
  done
  echo "ERROR: failed to bind ${role} to ${member} after ${attempt} attempt(s). Last stderr: ${last_stderr}" >&2
  return 1
}
