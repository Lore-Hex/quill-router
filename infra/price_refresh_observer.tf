# The independent price refresh observer's GitHub token
# (docs/price-refresh-alerting.md, "Host observer token").
#
# A fine-grained personal access token limited to Lore-Hex/quill-router with
# Actions: read and write. The observer on tr-clickhouse-3 uses it to read
# workflow runs (anonymous reads are limited to 60 an hour) and to dispatch
# refresh-prices.yml on main when GitHub dropped the schedule.
#
# The operator created the secret and added version 1 by hand on 2026-11-19.
# Versions are added by hand when the token is renewed, so no token is in this
# file or in Terraform state. Only the container is managed here.
#
# No IAM binding: the installer (scripts/ops/install_price_refresh_observer.sh)
# reads it with the operator's own gcloud identity, the owner login or
# tr-deploy, the same way it reads trustedrouter-sentry-dsn. Both already read
# every secret in the project, and both are allowed readers in
# infra/secret_reader_alert.tf. The host has no Google identity for it: the
# installer copies the value over IAP into a systemd credential file.
#
# Adopted by the import in infra/imports.tf.
resource "google_secret_manager_secret" "price_refresh_dispatch_token" {
  project   = local.gcp_project_id
  secret_id = "trustedrouter-price-refresh-dispatch-token"

  # As created (automatic). Changing replication would replace the secret and
  # destroy its versions.
  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}
