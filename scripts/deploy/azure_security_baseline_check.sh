#!/usr/bin/env bash
# Read the Azure subscription's security controls and report drift. Changes nothing.
#
# Companion to gcp_security_baseline_check.sh; the reasoning for check-only is
# in that file's header and in soc2/29-cloud-baseline-ground-truth.md.
#
# The Azure-specific reason not to write --apply: soc2/azure-hardening-…sh
# REFERENCES the tr-audit-logs workspace and never creates it. Against a fresh
# subscription every --workspace flag fails; worse, a recreate would mint a new
# workspace with a new customerId and the PerGB2018 default of 30 days,
# silently replacing a non-default 365-day retention and orphaning all
# historical audit data, which does not migrate. Everything below hangs off
# that workspace, so it is checked first.
set -uo pipefail

SUB="${SUB:-2fc83893-ca6c-48e4-b090-8860fba33d33}"
WS_RG="${WS_RG:-tr-azure}"
WS_NAME="${WS_NAME:-tr-audit-logs}"
WS_RETENTION="${WS_RETENTION:-365}"
PG_PROD="${PG_PROD:-tr-azure-pg}"
PG_PROD_RG="${PG_PROD_RG:-tr-azure}"
PG_CANARY="${PG_CANARY:-tr-canary-apac-pg}"
PG_CANARY_RG="${PG_CANARY_RG:-tr-canary-apac}"
VAULT="${VAULT:-trquillkv}"

FAIL=0
ok(){    printf '  ok    %s\n' "$*"; }
drift(){ printf '  DRIFT %s\n' "$*"; FAIL=1; }
note(){  printf '  note  %s\n' "$*"; }
sec(){   printf '\n=== %s\n' "$*"; }
# Case-insensitive compare. ARM lowercases the resource-group segment of ids it
# returns (TR-TEE-DUBAI comes back tr-tee-dubai), so a literal string equality
# against how a resource is spelled in a script reports permanent false drift.
lc(){ tr '[:upper:]' '[:lower:]'; }

az account set --subscription "$SUB" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 1. The audit sink everything else depends on.
# ---------------------------------------------------------------------------
sec "log analytics workspace $WS_NAME"
WS_JSON="$(az monitor log-analytics workspace show -n "$WS_NAME" -g "$WS_RG" -o json 2>/dev/null)"
if [ -z "$WS_JSON" ]; then
  drift "workspace $WS_NAME missing — every diagnostic setting below has nowhere to ship"
  WS_ID=""
else
  WS_ID="$(jq -r '.id' <<<"$WS_JSON")"
  RET="$(jq -r '.retentionInDays' <<<"$WS_JSON")"
  if [ "$RET" = "$WS_RETENTION" ]; then
    ok "retention ${RET}d"
  else
    # 365 is NOT the PerGB2018 default (30). A workspace showing 30 has almost
    # certainly been recreated rather than reconfigured, which also means the
    # history is gone.
    drift "workspace retention is ${RET}d, want ${WS_RETENTION}d (30 = the SKU default, i.e. probably recreated)"
  fi
  # Three other workspaces have confusingly similar names and 120-day
  # retention. Matching on display name rather than full id is how a check ends
  # up validating the wrong sink.
  note "id: $WS_ID"
fi

# ---------------------------------------------------------------------------
# 2. Diagnostic settings. Shape matters as much as existence.
# ---------------------------------------------------------------------------
# `az monitor diagnostic-settings list` returns a BARE ARRAY, not {value:[...]}.
# Querying value[] returns empty, which reads as "no diagnostic settings" — a
# mistake already made once on this estate.
check_diag(){ # label resource-id setting-name required-category-group
  local label="$1" rid="$2" name="$3" want="$4" js
  js="$(az monitor diagnostic-settings list --resource "$rid" -o json 2>/dev/null)"
  if [ -z "$js" ] || ! jq -e --arg n "$name" '[.[] | select(.name==$n)] | length > 0' <<<"$js" >/dev/null 2>&1; then
    drift "$label: diagnostic setting '$name' missing"
    return
  fi
  local dest enabled
  dest="$(jq -r --arg n "$name" '.[] | select(.name==$n) | .workspaceId' <<<"$js" | lc)"
  enabled="$(jq -r --arg n "$name" --arg g "$want" \
    '.[] | select(.name==$n) | [.logs[]? | select(.categoryGroup==$g and .enabled==true)] | length' <<<"$js")"
  [ "$enabled" -ge 1 ] && ok "$label: '$name' ships $want" \
    || drift "$label: '$name' exists but categoryGroup '$want' is not enabled"
  if [ -n "$WS_ID" ] && [ "$dest" != "$(lc <<<"$WS_ID")" ]; then
    drift "$label: '$name' ships to a DIFFERENT workspace ($dest) — audit trail is split"
  fi
}
sec "diagnostic settings"
PG_PROD_ID="/subscriptions/$SUB/resourceGroups/$PG_PROD_RG/providers/Microsoft.DBforPostgreSQL/flexibleServers/$PG_PROD"
PG_CAN_ID="/subscriptions/$SUB/resourceGroups/$PG_CANARY_RG/providers/Microsoft.DBforPostgreSQL/flexibleServers/$PG_CANARY"
VAULT_ID="$(az keyvault show -n "$VAULT" --query id -o tsv 2>/dev/null)"

# NOTE on shape: pg-audit has categoryGroup allLogs ENABLED and audit DISABLED.
# allLogs is a superset that already contains the audit categories, so this is
# correct; turning audit on is redundant and converging to audit-only would
# REDUCE coverage. The check therefore asserts allLogs, not audit.
check_diag "postgres prod"   "$PG_PROD_ID" pg-audit allLogs
check_diag "postgres canary" "$PG_CAN_ID"  pg-audit allLogs
[ -n "$VAULT_ID" ] && check_diag "key vault" "$VAULT_ID" kv-audit audit \
  || drift "key vault $VAULT not found"

# Subscription activity log. This one DOES return {value:[...]}.
sec "subscription activity log"
SUBDIAG="$(az monitor diagnostic-settings subscription list -o json 2>/dev/null)"
# Guard the empty case explicitly. Feeding "" to jq is a parse error, not an
# empty result, so an unreadable subscription would crash the check rather than
# report it — the failure mode this whole script exists to avoid.
[ -z "$SUBDIAG" ] && SUBDIAG='{"value":[]}'
CATS="$(jq -r '[.value[]? | select(.name=="tr-activity-audit") | .logs[]? | select(.enabled==true) | .category] | sort | join(",")' <<<"$SUBDIAG" 2>/dev/null)"
WANT_CATS="Administrative,Alert,Autoscale,Policy,Recommendation,ResourceHealth,Security,ServiceHealth"
if [ "$CATS" = "$WANT_CATS" ]; then
  ok "tr-activity-audit ships all 8 categories"
elif [ -z "$CATS" ]; then
  drift "subscription diagnostic setting tr-activity-audit missing — the activity log is unretained"
else
  drift "tr-activity-audit categories drifted"
  note "want: $WANT_CATS"
  note "got:  $CATS"
fi
# Do NOT check location. The hardening script passes --location uaenorth and
# ARM stores location=global; comparing them reports drift forever.

# ---------------------------------------------------------------------------
# 3. PostgreSQL: exposure and throttling.
# ---------------------------------------------------------------------------
sec "postgresql"
for pair in "$PG_PROD:$PG_PROD_RG" "$PG_CANARY:$PG_CANARY_RG"; do
  s="${pair%%:*}"; rg="${pair##*:}"
  # The 0.0.0.0 rule (AllowAllAzureServicesAndResourcesWithinAzureIps) admitted
  # any VM in ANY Azure tenant. It was removed after log evidence of an
  # unrelated tenant reaching the auth layer. Zero rules is the control.
  n="$(az postgres flexible-server firewall-rule list -s "$s" -g "$rg" --query 'length(@)' -o tsv 2>/dev/null || echo ERR)"
  case "$n" in
    0)   ok "$s: 0 firewall rules (private endpoint only)" ;;
    ERR) drift "$s: cannot read firewall rules" ;;
    *)   drift "$s: $n firewall rule(s) present — public exposure has returned" ;;
  esac
  for p in connection_throttle.enable require_secure_transport; do
    v="$(az postgres flexible-server parameter show -s "$s" -g "$rg" -n "$p" --query value -o tsv 2>/dev/null || echo ERR)"
    [ "$v" = "on" ] && ok "$s: $p=on" || drift "$s: $p=$v (want on)"
  done
done

# ---------------------------------------------------------------------------
# 4. Key Vault.
# ---------------------------------------------------------------------------
sec "key vault $VAULT"
KV="$(az keyvault show -n "$VAULT" -o json 2>/dev/null)"
if [ -z "$KV" ]; then
  drift "vault $VAULT unreadable"
else
  jq -e '.properties.enableRbacAuthorization == true' <<<"$KV" >/dev/null \
    && ok "RBAC-only" || drift "vault is not RBAC-only (legacy access policies in play)"
  jq -e '.properties.enableSoftDelete == true' <<<"$KV" >/dev/null \
    && ok "soft delete" || drift "soft delete disabled"
  jq -e '.properties.enablePurgeProtection == true' <<<"$KV" >/dev/null \
    && ok "purge protection" || drift "purge protection disabled"
  # publicNetworkAccess is Enabled and networkAcls is null, deliberately: the
  # southeastasia confidential container group has subnetIds=null and no VNet
  # exists in that region, so a private endpoint or default-deny ACL is an
  # immediate outage on next boot. Recorded as accepted in the SoA, so this is
  # a note rather than drift — but a note that must not silently disappear.
  PNA="$(jq -r '.properties.publicNetworkAccess' <<<"$KV")"
  [ "$PNA" = "Enabled" ] && note "publicNetworkAccess=Enabled (accepted; closing it strands southeastasia)" \
    || note "publicNetworkAccess=$PNA — changed from the accepted position, confirm enclaves still boot"

  # The real least-privilege defect on this vault: Crypto Officer lets an
  # attested workload rewrite the release policy that constrains it.
  CO="$(az role assignment list --scope "$(jq -r .id <<<"$KV")" \
        --query "length([?roleDefinitionName=='Key Vault Crypto Officer'])" -o tsv 2>/dev/null || echo ERR)"
  case "$CO" in
    ERR) note "could not read role assignments" ;;
    0|1) ok "Key Vault Crypto Officer held by $CO principal(s)" ;;
    *)   note "Key Vault Crypto Officer held by $CO principals — enclave identities can rewrite their own SKR release policy (open finding)" ;;
  esac
fi

echo
if [ "$FAIL" = 0 ]; then
  echo "=== Azure baseline OK"
else
  echo "=== Azure baseline DRIFT — see DRIFT lines above"
fi
exit "$FAIL"
