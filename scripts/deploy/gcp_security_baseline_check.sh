#!/usr/bin/env bash
# Read the GCP project's security controls and report drift. Changes nothing.
#
# WHY THERE IS NO --apply
# -----------------------
# There was going to be one. Capturing ground truth on 2026-08-17 killed it.
#
# soc2/gcp-hardening-2026-08-15.sh exists and its header says "none of this has
# been applied". That was true when written and is false now — critical_ssh,
# alerts, audit_logs, essential contacts and the ClickHouse SA swap have all
# been applied since. Re-running it today would stop a running production node
# to swap an identity it already has, rewrite two CIS metrics into semantically
# different filters, mint a duplicate notification channel on every invocation,
# re-introduce a ~$236/month logging cost regression, and revert a threshold
# recalibration made two days after it was written.
#
# The general shape, worth stating because it is not obvious: a one-shot
# hardening script becomes a hazard the moment its work is done, because it
# carries no memory of having run. These controls are not a snapshot to
# re-assert — they are a set of deliberate deviations from every default, and a
# converger that does not know which deviations are intentional will replace
# working controls with different ones.
#
# The most expensive example is not in this script's power to cause, and is
# recorded so nobody adds it: recreating the KMS key `acme-cache-envelope`
# under the same name would make every object in gs://quill-acme-cache
# permanently unreadable, because a key of the same name is a different key.
#
# So this reads. Convergence is a per-resource decision made by a person, with
# the reason written down.
set -uo pipefail

PROJECT="${PROJECT:-quill-cloud-proxy}"
ORG_ID="${ORG_ID:-256036015125}"
ACME_BUCKET="${ACME_BUCKET:-quill-acme-cache}"
ARCHIVE_BUCKET="${ARCHIVE_BUCKET:-quill-cloud-proxy-tr-clickhouse-archive}"
INGEST_THRESHOLD_BYTES="${INGEST_THRESHOLD_BYTES:-8589934592}"   # 8 GiB/day.

FAIL=0
ok(){    printf '  ok    %s\n' "$*"; }
drift(){ printf '  DRIFT %s\n' "$*"; FAIL=1; }
note(){  printf '  note  %s\n' "$*"; }
sec(){   printf '\n=== %s\n' "$*"; }
g(){ gcloud "$@" --project="$PROJECT" 2>/dev/null; }

# Read a project-scoped JSON resource without turning an authorization or CLI
# failure into "the resource is missing". Monitoring's channel and policy
# commands still live in the alpha component; a runner without that component
# used to return no JSON, and the checks below then reported every live alert
# as absent. The same ambiguity happens when a deliberately read-only operator
# identity lacks one list permission. Both are checker failures, not proof that
# production alerting disappeared.
read_project_json(){ # output-variable human-label gcloud-args...
  local output_variable="$1" label="$2" stderr_file output status=0 detail
  shift 2
  stderr_file="$(mktemp "${TMPDIR:-/tmp}/tr-gcp-baseline.XXXXXX")"
  output="$(gcloud "$@" --project="$PROJECT" --format=json 2>"$stderr_file")" || status=$?
  if [ "$status" -ne 0 ]; then
    detail="$(tr '\n' ' ' <"$stderr_file" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
    rm -f "$stderr_file"
    drift "could not read $label (gcloud exit $status): ${detail:-no diagnostic}"
    printf -v "$output_variable" '%s' '[]'
    return 1
  fi
  rm -f "$stderr_file"
  if ! jq -e . >/dev/null 2>&1 <<<"$output"; then
    drift "could not read $label: gcloud returned invalid JSON"
    printf -v "$output_variable" '%s' '[]'
    return 1
  fi
  printf -v "$output_variable" '%s' "$output"
}

# ---------------------------------------------------------------------------
# 1. Audit log configuration — and the cost guard on it.
# ---------------------------------------------------------------------------
sec "audit log configuration"
POLICY="$(g projects get-iam-policy "$PROJECT" --format=json)"
if [ -z "$POLICY" ]; then
  drift "could not read project IAM policy"
else
  TYPES="$(jq -r '[.auditConfigs[]? | select(.service=="allServices") | .auditLogConfigs[].logType] | sort | join(",")' <<<"$POLICY")"
  case "$TYPES" in
    "ADMIN_READ") ok "auditConfigs = ADMIN_READ (Admin Activity logs are always-on and free)" ;;
    "ADMIN_READ,DATA_WRITE")
      # The 2026-08-22 baseline enabled DATA_WRITE on allServices; at
      # production settle/authorize traffic every Spanner mutation became a
      # billed audit entry (~14 GiB/day, the 2026-08-26 ingestion alert).
      # All writes come from the control plane's own service accounts, so
      # per-row write audit re-records what app-level records already carry.
      # Removed deliberately 2026-08-26; Admin Activity + ADMIN_READ remain.
      drift "DATA_WRITE is enabled — ingestion-cost regression; removed deliberately 2026-08-26 (got: $TYPES)" ;;
    *DATA_READ*)
      # Not merely unexpected — expensive. DATA_READ was enabled and removed
      # the same day on measured cost: ingestion went ~0.07 -> ~0.80 GiB/hour,
      # about $236/month, because Spanner and Bigtable serve the inference
      # path so every metering read becomes a log line. If it is back, someone
      # re-ran the old hardening script.
      drift "DATA_READ is enabled — ~\$236/month regression; removed deliberately 2026-08-15 (got: $TYPES)" ;;
    "") drift "no auditConfigs on allServices — administrative activity is not being audited" ;;
    *)  drift "auditConfigs = $TYPES (want ADMIN_READ)" ;;
  esac
  jq -e '[.auditConfigs[]?.auditLogConfigs[]?.exemptedMembers // empty] | flatten | length > 0' <<<"$POLICY" >/dev/null 2>&1 \
    && drift "an auditLogConfig has exemptedMembers — some principal is exempt from audit logging" \
    || ok "no audit exemptions"
fi

# ---------------------------------------------------------------------------
# 2. CIS log-based metrics — filters compared VERBATIM.
# ---------------------------------------------------------------------------
# Existence is not the control; the filter is. A metric that exists but no
# longer matches produces an alert policy that never fires, which is
# indistinguishable from a quiet account. These strings are the live ones, and
# they differ from the old hardening script's — the script would write
# `iam.admin`/`CreateServiceAccount` for the first and would drop the
# `cloudsql.instances.delete` clause from the last.
sec "CIS log-based metrics"
check_metric(){ # name expected-filter
  local name="$1" want="$2" got
  got="$(g logging metrics describe "$name" --format='value(filter)')"
  if [ -z "$got" ]; then
    drift "metric $name missing"
  elif [ "$got" = "$want" ]; then
    ok "metric $name"
  else
    drift "metric $name FILTER CHANGED — may match nothing while its alert stays green"
    note "want: $want"
    note "got:  $got"
  fi
}
check_metric cis-iam-changes \
  'protoPayload.methodName="SetIamPolicy" AND protoPayload.serviceData.policyDelta.auditConfigDeltas:*'
check_metric cis-firewall-changes \
  'resource.type="gce_firewall_rule" AND (protoPayload.methodName:"compute.firewalls.patch" OR protoPayload.methodName:"compute.firewalls.insert" OR protoPayload.methodName:"compute.firewalls.delete")'
check_metric cis-route-changes \
  'resource.type="gce_route" AND (protoPayload.methodName:"compute.routes.delete" OR protoPayload.methodName:"compute.routes.insert")'
check_metric cis-sqlinstance-changes \
  'protoPayload.methodName="cloudsql.instances.update" OR protoPayload.methodName="cloudsql.instances.create" OR protoPayload.methodName="cloudsql.instances.delete"'

# ---------------------------------------------------------------------------
# 3. Alert policies, the channel they point at, and duplicates of it.
# ---------------------------------------------------------------------------
sec "alert policies and notification channel"
CHANNELS='[]'
if read_project_json CHANNELS "Monitoring notification channels" \
    alpha monitoring channels list; then
  CH_COUNT="$(jq -r '[.[] | select(.displayName=="TrustedRouter security alerts")] | length' <<<"$CHANNELS")"
  case "$CH_COUNT" in
    1) ok "exactly one 'TrustedRouter security alerts' channel" ;;
    0) drift "notification channel 'TrustedRouter security alerts' missing — the CIS alerts are deaf" ;;
    # This is the specific damage the old script does. `channels create` is not
    # idempotent, so each run mints another channel and repoints policies at the
    # new id, orphaning the previous one. Seeing >1 means it was re-run.
    *) drift "$CH_COUNT duplicate 'TrustedRouter security alerts' channels — the old hardening script was re-run" ;;
  esac
fi

POLICIES='[]'
if read_project_json POLICIES "Monitoring alert policies" \
    alpha monitoring policies list; then
  for want in "CIS: IAM configuration changes" "CIS: VPC firewall rule changes" \
              "CIS: VPC network route changes" "CIS: Cloud SQL instance configuration changes"; do
    if jq -e --arg d "$want" '[.[] | select(.displayName==$d and .enabled==true)] | length > 0' <<<"$POLICIES" >/dev/null 2>&1; then
      if jq -e --arg d "$want" '[.[] | select(.displayName==$d) | select((.notificationChannels // []) | length > 0)] | length > 0' <<<"$POLICIES" >/dev/null 2>&1; then
        ok "policy \"$want\""
      else
        # A policy with no channel is the quietest possible failure: it evaluates,
        # it opens incidents, and nobody is told.
        drift "policy \"$want\" has NO notification channel — it fires into nothing"
      fi
    else
      drift "policy \"$want\" missing or disabled"
    fi
  done

  THRESH="$(jq -r '[.[] | select(.displayName | startswith("TR Logging: ingestion volume spike")) | .conditions[].conditionThreshold.thresholdValue] | first // empty' <<<"$POLICIES")"
  if [ -z "$THRESH" ]; then
    drift "ingestion-spike cost guard missing"
  elif [ "${THRESH%.*}" = "$INGEST_THRESHOLD_BYTES" ]; then
    ok "ingestion cost guard at $((INGEST_THRESHOLD_BYTES/1073741824)) GiB/day"
  else
    # Raised 5 -> 8 GiB on 2026-08-17 because steady state is ~4.1 GiB/day and
    # 5 GiB sat only 20% above baseline. Anything that resets it to 5 has
    # re-applied a stale snapshot.
    note "ingestion threshold is $(( ${THRESH%.*} / 1073741824 )) GiB/day, expected $((INGEST_THRESHOLD_BYTES/1073741824)) GiB; raised 5 -> 8 on 2026-08-17 because steady state is ~4.1"
  fi
fi

# ---------------------------------------------------------------------------
# 4. SSH exposure: an absence control with an ordering constraint.
# ---------------------------------------------------------------------------
# default-allow-ssh (0.0.0.0/0:22) and default-allow-rdp were deleted. That is
# only survivable because tr-allow-iap-ssh-all exists, so this section checks
# the replacement FIRST — a report that says "ssh is closed" while the IAP path
# is also gone describes a locked-out project, not a hardened one.
sec "SSH exposure"
IAP_ALL="$(g compute firewall-rules describe tr-allow-iap-ssh-all --format='value(sourceRanges.list())')"
if [ "$IAP_ALL" = "35.235.240.0/20" ]; then
  ok "tr-allow-iap-ssh-all present (IAP range) — the access path that makes the deletions safe"
else
  drift "tr-allow-iap-ssh-all missing or wrong source range (got: '${IAP_ALL:-none}') — IAP SSH may be broken"
fi
# Expected absence must not be probed with `firewall-rules describe`: the API
# returns NOT_FOUND and records that successful security assertion as an ERROR
# audit event. Read the collection once and test exact names locally instead.
FIREWALL_NAMES="$(g compute firewall-rules list --format='value(name)')"
for r in default-allow-ssh default-allow-rdp; do
  if grep -Fxq "$r" <<<"$FIREWALL_NAMES"; then
    drift "$r EXISTS again — 0.0.0.0/0 to every instance in the network"
  else
    ok "$r absent"
  fi
done
if grep -Fxq allow-iap-ssh-tmp <<<"$FIREWALL_NAMES"; then
  # Created 2026-06-18, still live. Functionally identical to
  # tr-allow-iap-ssh-all, so it grants nothing extra — the textbook shape of a
  # temporary widening that became permanent. Reported, not failed.
  note "allow-iap-ssh-tmp still present (created 2026-06-18, redundant with tr-allow-iap-ssh-all)"
fi

# ---------------------------------------------------------------------------
# 5. Essential Contacts — org-scoped, and the query matters.
# ---------------------------------------------------------------------------
# `essential-contacts list --project=` returns [] here and that does NOT mean
# absent: contacts inherit downward from the organisation. A check written the
# obvious way reports this control missing, and an --apply acting on that would
# create a duplicate. `compute` resolves the effective set.
sec "essential contacts"
EC='[]'
if read_project_json EC "effective SECURITY Essential Contacts" \
    essential-contacts compute --notification-categories=security; then
  if jq -e 'length > 0' <<<"$EC" >/dev/null 2>&1; then
    ok "security contact: $(jq -r '.[0].email' <<<"$EC") (effective for project; may be inherited from org $ORG_ID)"
  else
    drift "no effective SECURITY essential contact for $PROJECT"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Buckets holding TLS material and the only analytics backup.
# ---------------------------------------------------------------------------
sec "storage"
ACME="$(gcloud storage buckets describe "gs://$ACME_BUCKET" --format=json 2>/dev/null)"
if [ -n "$ACME" ]; then
  # snake_case, not camelCase. A projection using camelCase names returns null
  # for every field, which reads exactly like "no protections exist" — that
  # mistake nearly put a fabricated critical finding into an audit document.
  jq -e '.default_kms_key != null' <<<"$ACME" >/dev/null && ok "acme cache CMEK-encrypted" \
    || drift "acme cache lost its CMEK key"
  jq -e '.soft_delete_policy.retentionDurationSeconds != null' <<<"$ACME" >/dev/null \
    && ok "acme cache soft-delete enabled" || drift "acme cache soft-delete disabled"
else
  drift "cannot read gs://$ACME_BUCKET"
fi
ARCH="$(gcloud storage buckets describe "gs://$ARCHIVE_BUCKET" --format=json 2>/dev/null)"
if [ -n "$ARCH" ]; then
  # 2555 days = 7 years. This is the only point-in-time backup of the analytics
  # estate; a generic lifecycle rule would start deleting it.
  jq -e '[.lifecycle_config.rule[]?.condition.age] | index(2555) != null' <<<"$ARCH" >/dev/null \
    && ok "archive 7-year retention rule intact" \
    || drift "archive lifecycle no longer has the 2555-day rule — the only PIT backup may be expiring early"
else
  drift "cannot read gs://$ARCHIVE_BUCKET"
fi

# ---------------------------------------------------------------------------
# 7. Service-account key age. Reported, never actioned.
# ---------------------------------------------------------------------------
# CIS wants user-managed keys rotated within 90 days. All three live keys are
# non-expiring (validBefore 9999-12-31) and every one is held OUTSIDE GCP — in
# CI, on the workstation, and in Azure — so deleting them from here breaks the
# holder. That makes this a reporting-only control by construction.
sec "service-account user-managed keys"
for sa in tr-deploy tr-ops-local tr-azure-acme-cache tr-clickhouse quill-workload enclave-dns-reconciler; do
  email="$sa@$PROJECT.iam.gserviceaccount.com"
  keys="$(g iam service-accounts keys list --iam-account="$email" --managed-by=user --format='value(name,validAfterTime)')"
  [ -z "$keys" ] && continue
  while read -r _ after; do
    [ -z "$after" ] && continue
    age=$(( ( $(date -u +%s) - $(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$after" +%s 2>/dev/null || echo "$(date -u +%s)") ) / 86400 ))
    if [ "$age" -gt 90 ]; then
      note "$sa user-managed key is ${age}d old (CIS wants <90d; key is held outside GCP so rotation is a coordinated change)"
    else
      ok "$sa key ${age}d old"
    fi
  done <<<"$keys"
done

echo
if [ "$FAIL" = 0 ]; then
  echo "=== GCP baseline OK"
else
  echo "=== GCP baseline DRIFT — see DRIFT lines above"
fi
exit "$FAIL"
