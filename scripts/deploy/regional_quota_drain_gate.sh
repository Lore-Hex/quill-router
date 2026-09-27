#!/usr/bin/env bash
# Refuse to remove regional quota lease capability while escrow is still open.
#
# Capability off means no serving revision can settle, refund, or drain a
# regional hold, so this runs before any revision changes and proves, from the
# reconcilers' own completion records, that nothing is left to drain:
#   1. every serving revision, held regions included, already carries
#      TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=false;
#   2. the last N regional reconciler passes inspected nothing and left no
#      backlog, and all of them are recent;
#   3. the last N spend-lease reconciler passes had no open or dead rows.
# Once the reconciler schedule no longer exists the ledger has already been
# retired and the gate is a no-op. Dry-run and apply are the same: this script
# only reads.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"

SCHEDULER="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER:-trusted-router-regional-quota-reconcile}"
SCHEDULER_REGION="${TR_REGIONAL_QUOTA_RECONCILER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
REGIONAL_JOB_PREFIX="${TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX:-trusted-router-regional-quota-reconciler}"
SPEND_JOB_PREFIX="${TR_SPEND_LEASE_RECONCILER_JOB_PREFIX:-trusted-router-spend-lease-reconciler}"
# Both reconcilers run once a minute; five fresh empty passes is five minutes
# of proof, and freshness bounds how stale "fresh" may be.
REQUIRED_PASSES="${TR_LEDGER_DRAIN_REQUIRED_PASSES:-5}"
FRESHNESS="${TR_LEDGER_DRAIN_FRESHNESS:-15m}"

log() { printf '%s %s\n' '[regional_quota_drain_gate]' "$*" >&2; }

describe_stderr="$(mktemp "${TMPDIR:-/tmp}/ledger-drain-scheduler.XXXXXX")"
if gc scheduler jobs describe "$SCHEDULER" --location="$SCHEDULER_REGION" \
    --format='value(state)' >/dev/null 2>"$describe_stderr"; then
  rm -f "$describe_stderr"
elif grep -qE '(^|[[:space:]])NOT_FOUND([[:space:]:]|$)' "$describe_stderr"; then
  rm -f "$describe_stderr"
  log "reconciler schedule ${SCHEDULER} does not exist: the ledger is already retired"
  exit 0
else
  log "ERROR: cannot determine whether ${SCHEDULER} exists: $(<"$describe_stderr")"
  rm -f "$describe_stderr"
  exit 1
fi

# 1. Issuance is off on every serving revision, held regions included.
IFS=',' read -r -a regions <<<"$TR_CONTROL_PLANE_REGIONS"
for region in "${regions[@]}"; do
  revision_json=""
  status=0
  revision_json="$(regional_quota_active_revision_json "$region" false)" || status=$?
  if [ "$status" -eq 3 ]; then
    log "${region}: no serving revision; nothing to drain there"
    continue
  elif [ "$status" -ne 0 ]; then
    log "refusing ledger retirement: cannot read the serving revision in ${region}"
    exit 1
  fi
  marker="$(regional_quota_revision_env "$revision_json" TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED missing)"
  if [ "$marker" != "false" ]; then
    log "refusing ledger retirement: ${region} still serves TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=${marker}"
    exit 1
  fi
done

# 2 + 3. The reconcilers' own completion records. Both log one summary line per
# pass; the numbers are what we gate on, the rest of the line is ignored.
evidence() {
  local prefix="$1" marker="$2"
  gc logging read \
    "resource.type=\"cloud_run_job\" AND resource.labels.job_name:\"${prefix}\" AND textPayload:\"${marker}\"" \
    --freshness="$FRESHNESS" \
    --limit="$REQUIRED_PASSES" \
    --order=desc \
    --format=json
}

check_evidence() {
  local label="$1" marker="$2" json="$3"
  shift 3
  python3 - "$label" "$marker" "$REQUIRED_PASSES" "$json" "$@" <<'PY'
import json
import re
import sys

label, marker, required, raw = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
zero_fields = sys.argv[5:]
try:
    entries = json.loads(raw or "[]")
except ValueError:
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not JSON")
if not isinstance(entries, list):
    raise SystemExit(f"refusing ledger retirement: {label} evidence is not a list")
lines = []
for entry in entries:
    payload = entry.get("textPayload") if isinstance(entry, dict) else None
    if isinstance(payload, str) and marker in payload:
        lines.append(payload)
if len(lines) < required:
    raise SystemExit(
        f"refusing ledger retirement: {label} has {len(lines)} recent completion(s); "
        f"require {required} (is the reconciler running?)"
    )
for line in lines:
    values = dict(re.findall(r"\b([a-z_]+)=(\d+)\b", line))
    missing = [field for field in zero_fields if field not in values]
    if missing:
        raise SystemExit(
            f"refusing ledger retirement: {label} completion lacks {','.join(missing)}"
        )
    busy = {field: values[field] for field in zero_fields if values[field] != "0"}
    if busy:
        summary = " ".join(f"{k}={v}" for k, v in busy.items())
        raise SystemExit(f"refusing ledger retirement: {label} is not drained ({summary})")
print(f"{label}: {len(lines)} consecutive empty passes")
PY
}

regional_json="$(evidence "$REGIONAL_JOB_PREFIX" "regional_quota.reconcile_complete")"
check_evidence "regional quota reconciler" "regional_quota.reconcile_complete" "$regional_json" \
  inspected backlog remaining errors
spend_json="$(evidence "$SPEND_JOB_PREFIX" "spend_lease.reconcile_complete")"
check_evidence "spend-lease reconciler" "spend_lease.reconcile_complete" "$spend_json" \
  candidates open dead errors

log "ledger escrow is drained: ${REQUIRED_PASSES} consecutive empty passes on both reconcilers within ${FRESHNESS}"
