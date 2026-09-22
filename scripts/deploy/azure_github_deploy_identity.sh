#!/usr/bin/env bash
# One-time setup for .github/workflows/deploy-azure-control-plane.yml: the
# Azure identity that workflow logs in as.
#
# Run it once, by someone with Owner on the subscription (it creates an
# identity and assigns roles), from a shell where `az login` and
# `gh auth login` are done.
# Running it again is safe: every step looks before it creates.
#
# It creates, and grants, exactly this:
#   * user-assigned managed identity tr-github-deploy in resource group
#     tr-azure;
#   * a federated credential on it that accepts GitHub's OIDC token for
#     repo:Lore-Hex/quill-router:ref:refs/heads/main -- the subject the AWS
#     deploy role trusts (infra/aws_deploy_role.tf);
#   * Contributor on resource group tr-azure, where every resource the deploy
#     script changes lives (the Container App, the ACR build, the Postgres
#     firewall rule, the ClickHouse identity it assigns). Contributor cannot
#     create role assignments;
#   * Key Vault Secrets User on the single secret tr-azure-clickhouse-password
#     in vault trquillkv (resource group TR-TEE-DUBAI), which the deploy reads
#     to wire the app's Key Vault reference. No other secret in that vault;
#   * the repository variable AZURE_DEPLOY_CLIENT_ID, which the workflow reads.
set -euo pipefail

SUBSCRIPTION="${SUBSCRIPTION:-2fc83893-ca6c-48e4-b090-8860fba33d33}"
RG="${RG:-tr-azure}"
LOCATION="${LOCATION:-uaenorth}"
IDENTITY="${IDENTITY:-tr-github-deploy}"
REPO="${REPO:-Lore-Hex/quill-router}"
SUBJECT="repo:${REPO}:ref:refs/heads/main"
CREDENTIAL_NAME="${CREDENTIAL_NAME:-quill-router-main}"
VAULT="${VAULT:-trquillkv}"
VAULT_SECRET="${VAULT_SECRET:-tr-azure-clickhouse-password}"

log() { printf '%s\n' "$*" >&2; }

az account set --subscription "$SUBSCRIPTION"

if az identity show -g "$RG" -n "$IDENTITY" -o none 2>/dev/null; then
  log "identity $IDENTITY exists"
else
  log "creating identity $IDENTITY in $RG"
  az identity create -g "$RG" -n "$IDENTITY" -l "$LOCATION" -o none
fi
CLIENT_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)"
PRINCIPAL_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)"

if az identity federated-credential show -g "$RG" --identity-name "$IDENTITY" \
    -n "$CREDENTIAL_NAME" -o none 2>/dev/null; then
  log "federated credential $CREDENTIAL_NAME exists"
else
  log "trusting GitHub OIDC for $SUBJECT"
  az identity federated-credential create -g "$RG" --identity-name "$IDENTITY" \
    -n "$CREDENTIAL_NAME" \
    --issuer https://token.actions.githubusercontent.com \
    --subject "$SUBJECT" \
    --audiences api://AzureADTokenExchange -o none
fi

RG_SCOPE="$(az group show -n "$RG" --query id -o tsv)"
SECRET_SCOPE="$(az keyvault show -n "$VAULT" --query id -o tsv)/secrets/${VAULT_SECRET}"

assign() {
  local role="$1" scope="$2"
  if [ -n "$(az role assignment list --assignee "$PRINCIPAL_ID" --role "$role" \
      --scope "$scope" --query '[0].id' -o tsv)" ]; then
    log "role '$role' already assigned on ${scope##*/}"
    return 0
  fi
  log "assigning '$role' on ${scope##*/}"
  # Object id and principal type skip the Graph lookup, which fails for a
  # principal created seconds ago.
  az role assignment create --assignee-object-id "$PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$scope" -o none
}
assign Contributor "$RG_SCOPE"
assign "Key Vault Secrets User" "$SECRET_SCOPE"

gh variable set AZURE_DEPLOY_CLIENT_ID --repo "$REPO" --body "$CLIENT_ID"
log "done: $IDENTITY; its client id is now AZURE_DEPLOY_CLIENT_ID in $REPO."
log "Role assignments can take a few minutes to take effect before the first run."
