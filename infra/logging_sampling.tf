# Sampling of high-volume success logs in the _Default bucket. Ingestion is
# linear in requests (about 12 GB/day at 2026-10-01 volume, growing 3-10x a
# month), and almost all of it repeats what the system metrics already count.
# Each exclusion keeps a 10% sample of success lines and every failure:
#
# - Cloud Run request logs: the billing 5xx check (scripts/deploy/
#   assert_no_billing_5xx.sh), the billing 5xx alert and the slow-billing
#   log metric (trustedrouter_gateway_billing_slow) read only status >= 500
#   or latency >= 10s entries, which are never excluded; so does the
#   authorize 503 counter (trustedrouter_gateway_authorize_503). The app's
#   billing.lease_headroom_unavailable line the gate and its counter read
#   is a stderr line, which no exclusion here touches. Cloud Run's own
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
  # The launcher's journald export carries the line in jsonPayload.message
  # (lowercase). Field names are case-sensitive, and until 2026-11-16 this
  # filter read jsonPayload.MESSAGE only, so it matched nothing and kept every
  # success line. Both spellings are matched so an export change cannot
  # silently undo the sampling again.
  filter = <<-EOT
    logName="projects/quill-cloud-proxy/logs/confidential-space-launcher"
    AND (
      jsonPayload.message:"enclave.request_accept "
      OR jsonPayload.message:"enclave.request_start "
      OR (jsonPayload.message:"enclave.request_end " AND jsonPayload.message:"status=200 outcome=\"ok\"")
      OR (jsonPayload.message:"enclave.invoke_attempt " AND jsonPayload.message:" outcome=ok ")
      OR (jsonPayload.message:"enclave.invoke_complete " AND jsonPayload.message:" outcome=ok ")
      OR jsonPayload.MESSAGE:"enclave.request_accept "
      OR jsonPayload.MESSAGE:"enclave.request_start "
      OR (jsonPayload.MESSAGE:"enclave.request_end " AND jsonPayload.MESSAGE:"status=200 outcome=\"ok\"")
      OR (jsonPayload.MESSAGE:"enclave.invoke_attempt " AND jsonPayload.MESSAGE:" outcome=ok ")
      OR (jsonPayload.MESSAGE:"enclave.invoke_complete " AND jsonPayload.MESSAGE:" outcome=ok ")
    )
    AND sample(insertId, 0.9)
  EOT
}

# Stage D (fast-path streams) heartbeats: the enclave logs every heartbeat it
# sends and every one the owner accepts, per open stream every few seconds.
# On 2026-11-16 they were 82% of launcher bytes (one EU instance with many
# long streams), doubling project ingestion. Keep 1% of the two success lines;
# every other stage_d line (refusals, cap reached, failures) is kept whole.
resource "google_logging_project_exclusion" "sample_enclave_stage_d_heartbeats" {
  project     = local.gcp_project_id
  name        = "sample-enclave-stage-d-heartbeats"
  description = "Keep 1% of enclave Stage D heartbeat sent/accepted lines; keep every other stage_d line."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/confidential-space-launcher"
    AND (
      jsonPayload.message:"enclave.stage_d_heartbeat_sent "
      OR jsonPayload.message:"enclave.stage_d_heartbeat_accepted "
      OR jsonPayload.MESSAGE:"enclave.stage_d_heartbeat_sent "
      OR jsonPayload.MESSAGE:"enclave.stage_d_heartbeat_accepted "
    )
    AND sample(insertId, 0.99)
  EOT
}

# The four below were added when ingestion passed the ingestion cost guard
# (gcp_security_baseline_check.sh) after traffic grew. Each samples a
# high-volume log that nothing reads in full. Each filter was counted
# against a recent hour with its sample clause left out, and each failure
# form was read back from the same hour and matched none of them. Each also
# requires severity below WARNING, so an entry Cloud Logging or the app marks
# WARNING or higher is never sampled whatever its text says.

# The edge's load balancer request log: since the authorize route's weight
# reached 100, one line for every authorize, logged in full
# (fastpath_edge_backend.tf sets no sample rate), almost all of them fast
# successes. Readers: the edge_decide_pass_slow procedure in
# docs/runbooks/fastpath-alerts.md finds a pass slower than 1 s by its
# latency (kept: every call of 1 s or more) and compares the node's other
# calls of the same seconds (now a 10% sample); the 2026-10-22 authorize
# edge deadline incident read a 504 with statusDetails=backend_timeout
# (kept: every status of 400 or more, and every response the edge itself did
# not send, which statusDetails names). A Cloud Armor rule in preview is
# kept too. No log-based metric, alert or script reads this log. The
# rollout gates and the turn-off runbook read the balancer's own metrics
# (https/request_count, backend_request_count, backend_latencies), which are
# not log-based and are unaffected. A backend sample_rate would sample the
# failures as well, so the sampling is here.
resource "google_logging_project_exclusion" "sample_edge_lb_request_successes" {
  project     = local.gcp_project_id
  name        = "sample-edge-lb-request-successes"
  description = "Keep 10% of fast successful edge load balancer request logs; keep every 4xx/5xx, every call of 1s or longer, every response the edge did not send and every entry at WARNING or above."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/requests"
    AND resource.type="http_load_balancer"
    AND resource.labels.backend_service_name="tr-fastpath-edge-backend"
    AND severity<WARNING
    AND httpRequest.status<400
    AND httpRequest.latency<"1s"
    AND jsonPayload.statusDetails="response_sent_by_backend"
    AND NOT jsonPayload.previewSecurityPolicy:*
    AND sample(insertId, 0.9)
  EOT
}

# The Shielded VM integrity log. Compute Engine writes a
# tdxLaunchAttestationReportEvent, which carries the raw TD quote, for every
# quote a TDX guest asks for, and an enclave asks for one on every
# attestation request a client makes, each with its own nonce and TLS
# binding (quill-cloud-proxy enclave-go/internal/attestation). So these
# events are nearly all of the log's bytes and grow with client sessions.
# The measurements a quote carries (mrTd, the TCB levels) are fixed for a
# boot; only the report data, the nonce, differs. Nothing reads them: no log
# metric, alert, script or runbook names this log. The integrity signals are
# the boot reports (earlyBootReportEvent and lateBootReportEvent, with
# policyEvaluationPassed), two per boot; they, every startup or other event,
# and anything above NOTICE are kept whole, and a boot report that does not
# pass pages (infra/boot_integrity_alert.tf).
resource "google_logging_project_exclusion" "sample_tdx_quote_events" {
  project     = local.gcp_project_id
  name        = "sample-tdx-quote-events"
  description = "Keep 10% of per-quote TDX launch attestation report events; keep every boot report, every other integrity event and anything above NOTICE."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/compute.googleapis.com%2Fshielded_vm_integrity"
    AND jsonPayload.tdxLaunchAttestationReportEvent:*
    AND NOT jsonPayload.earlyBootReportEvent:*
    AND NOT jsonPayload.lateBootReportEvent:*
    AND severity<=NOTICE
    AND sample(insertId, 0.9)
  EOT
}

# Uvicorn's access line, one per request on the control plane's stdout:
# INFO: <client> - "POST /internal/gateway/heartbeat HTTP/1.1" 200 OK. It is
# nearly all of that stdout and repeats Cloud Run's request log (method,
# path, status), which keeps every failure and every request of 10 s or
# more with its latency, and 10% of the rest. The access line carries no
# latency, so a slow success is sampled here; the request log is where its
# latency is kept. Nothing reads the access line: the billing 5xx gate, its
# alert and the gateway log metrics read the request log and stderr lines,
# and no runbook queries stdout. The pattern names the line's whole shape,
# so a 4xx or 5xx line and any stdout line of another shape (a structured
# record, docs/design/async-settle-shadow-v2-runtime-harness-interface.md)
# are kept.
resource "google_logging_project_exclusion" "sample_control_plane_access_successes" {
  project     = local.gcp_project_id
  name        = "sample-control-plane-access-successes"
  description = "Keep 10% of the control plane's 2xx/3xx Uvicorn access lines (no latency in them: Cloud Run's request log keeps the latency of every failure and every request of 10s or more); keep every 4xx/5xx line, every other stdout line and every entry at WARNING or above."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/run.googleapis.com%2Fstdout"
    AND resource.type="cloud_run_revision"
    AND resource.labels.service_name=("trusted-router" OR "trusted-router-public")
    AND severity<WARNING
    AND textPayload=~"^INFO: +[^ ]+ - .[A-Z]+ [^ ]+ HTTP/[0-9.]+. [23][0-9][0-9] "
    AND sample(insertId, 0.9)
  EOT
}

# Per-request INFO lines on trusted-router's stderr that no log metric,
# alert, gate or runbook reads (infra/, scripts/, docs/ and
# quill-cloud-proxy, searched 2026-11-23), each sampled only in its routine
# form; every other form stays whole:
# - settle timing: success=True, one commit, boot_auth absent or verified,
#   and total_ms under 1000. success=False, commit_path=deferred
#   (docs/spanner-reliability.md), boot_auth=ignored_no_boot_kid and every
#   settle of 1 s or more stay.
# - typed finalize timing with one attempt and spanner_ms and index_ms both
#   under 1000. attempts counts only the store's outer retries, and Spanner
#   retries an aborted transaction inside the call without counting it
#   (storage_gcp.py says so beside the line), so an absorbed abort shows only
#   as time: the duration bound is what keeps it. A retried finalize stays.
# - gateway.authorize_affinity, all of it below WARNING: it has no failure
#   form, and the E2 design (docs/design/fast-admission-e2-decide.md) read
#   its shares over a sample, which a uniform 10% keeps.
# - gateway.idempotency_key_source for a gateway key from a boot that
#   declared it. Legacy and undeclared sources stay, and a mismatch is a
#   WARNING line of its own.
# - fastpath pilot passed over reason=key_cap, one per authorize of a
#   workspace whose key has a spend cap, so it follows that workspace's
#   bursts; docs/spanner-reliability.md needs only that the reason appears.
#   The stream reasons docs/runbooks/fastpath-streams.md counts stay.
# The 1 s bound: the settle and finalize lines have no slow threshold of
# their own, and the billing slow-call metric (trustedrouter_gateway_billing_slow)
# counts calls of 10 s or more. 1 s is the stricter bound: a stream's settle
# that takes more than 2 s already makes the enclave stop waiting for it
# (docs/incidents/2026-10-22-authorize-edge-deadline.md), and it matches the
# edge's 1 s budget above.
# Not matched: typed finalize speculation timing (its fallback fields keep
# their initial values when the transaction raises, so the routine form is
# also the failure form); every WARNING and ERROR line (Sentry takes those
# from the process, not from Cloud Logging); the billing.* lines the gateway
# log metrics and scripts/deploy/assert_no_billing_5xx.sh read; the metric=
# lines the key usage and hold release alerts read; the fastpath.* lines the
# control plane alerts read; client_observed.*; the fastpath pilot terminal
# lines scripts/backfill_leased_key_usage.py and the streams runbook read;
# the edge boot verdict comparisons (agree, differ, skipped) the edge
# route's parity evidence counted; and the async-settle metric= lines.
resource "google_logging_project_exclusion" "sample_control_plane_routine_lines" {
  project     = local.gcp_project_id
  name        = "sample-control-plane-routine-lines"
  description = "Keep 10% of the control plane's routine per-request timing and routing INFO lines under 1s; keep every failure form, every slow line, every entry at WARNING or above and every line a metric, alert or gate reads."
  filter      = <<-EOT
    logName="projects/quill-cloud-proxy/logs/run.googleapis.com%2Fstderr"
    AND resource.type="cloud_run_revision"
    AND resource.labels.service_name="trusted-router"
    AND severity<WARNING
    AND (
      (
        textPayload:"INFO trusted_router.routes.internal.gateway settle timing "
        AND textPayload:" success=True "
        AND textPayload=~" total_ms=[0-9]{1,3}[.][0-9] "
        AND textPayload:" commit_path=one_commit "
        AND (textPayload:" boot_auth=absent" OR textPayload:" boot_auth=verified")
      )
      OR (
        textPayload:"INFO trusted_router.storage_gcp typed finalize timing "
        AND textPayload=~" spanner_ms=[0-9]{1,3}[.][0-9] index_ms=[0-9]{1,3}[.][0-9] attempts=1$"
      )
      OR textPayload:"INFO trusted_router.routes.internal.gateway gateway.authorize_affinity "
      OR textPayload:"INFO trusted_router.routes.internal.gateway gateway.idempotency_key_source source=gateway declared=gateway "
      OR textPayload:"INFO trusted_router.routes.internal.gateway fastpath pilot passed over reason=key_cap "
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
