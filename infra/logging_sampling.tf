# Sampling of high-volume success logs in the _Default bucket. Ingestion is
# linear in requests (about 12 GB/day at 2026-10-01 volume, growing 3-10x a
# month), and almost all of it repeats what the system metrics already count.
# Each exclusion keeps a 10% sample of success lines and every failure:
#
# - Cloud Run request logs: the billing 5xx check (scripts/deploy/
#   assert_no_billing_5xx.sh), the billing 5xx alert and the slow-billing
#   log metric (trustedrouter_gateway_billing_slow) read only status >= 500
#   or latency >= 10s entries, which are never excluded. Cloud Run's own
#   request_count and latency metrics are not log-based.
# - Enclave launcher lines: tools/dx/enclave-logs.sh (quill-cloud-proxy) is
#   the only reader. request_end carries method, route, status, outcome,
#   timing and credential metadata, so accept/start lines are redundant.
#
# Measured on a 2-minute window before the change: request logs 1791 of 1816
# were fast successes; launcher success lines 5216 of 6385. Real failure lines
# (outcome=fail) were checked to not match the success clauses. Other sinks
# (tr-growth-source) receive their own copies and are unaffected.
resource "google_logging_project_exclusion" "sample_cloud_run_request_successes" {
  project     = local.gcp_project_id
  name        = "sample-cloud-run-request-successes"
  description = "Keep 10% of fast successful TrustedRouter request logs; keep every 4xx/5xx and every request of 10s or longer."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/run.googleapis.com%2Frequests"
    AND resource.labels.service_name=("trusted-router" OR "trusted-router-public")
    AND httpRequest.status<400
    AND httpRequest.latency<"10s"
    AND sample(insertId, 0.9)
  EOT
}

resource "google_logging_project_exclusion" "sample_enclave_success_lifecycle" {
  project     = local.gcp_project_id
  name        = "sample-enclave-success-lifecycle"
  description = "Keep 10% of enclave per-request success lines; keep every failure and every other launcher line."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/confidential-space-launcher"
    AND (
      jsonPayload.MESSAGE:"enclave.request_accept "
      OR jsonPayload.MESSAGE:"enclave.request_start "
      OR (jsonPayload.MESSAGE:"enclave.request_end " AND jsonPayload.MESSAGE:"status=200 outcome=\"ok\"")
      OR (jsonPayload.MESSAGE:"enclave.invoke_attempt " AND jsonPayload.MESSAGE:" outcome=ok ")
      OR (jsonPayload.MESSAGE:"enclave.invoke_complete " AND jsonPayload.MESSAGE:" outcome=ok ")
    )
    AND sample(insertId, 0.9)
  EOT
}

# infra-apply runs as tr-deploy, which holds logging.viewer only. This role
# lets it manage exclusions and nothing else in Logging (no sinks, buckets or
# log reads). An owner creates the role and binding once; both are imported.
import {
  to = google_project_iam_custom_role.log_exclusions
  id = "projects/quill-cloud-proxy/roles/trustedRouterLogExclusions"
}

resource "google_project_iam_custom_role" "log_exclusions" {
  project     = local.gcp_project_id
  role_id     = "trustedRouterLogExclusions"
  title       = "TrustedRouter log exclusions"
  description = "Manage _Default log exclusions only; no sinks, buckets or log reads."
  stage       = "GA"
  permissions = [
    "logging.exclusions.create",
    "logging.exclusions.delete",
    "logging.exclusions.get",
    "logging.exclusions.list",
    "logging.exclusions.update",
  ]
}

import {
  to = google_project_iam_member.tr_deploy_log_exclusions
  id = "quill-cloud-proxy projects/quill-cloud-proxy/roles/trustedRouterLogExclusions serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "tr_deploy_log_exclusions" {
  project = local.gcp_project_id
  role    = google_project_iam_custom_role.log_exclusions.id
  member  = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}
