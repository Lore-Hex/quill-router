# Holds the control plane gave back with no terminal from the gateway
# (docs/design/fast-admission-streams.md section 13;
# docs/runbook.md, "Holds released without a terminal").
#
# A gateway request ends in a settle or a refund. A hold that has neither is
# given back by one of two rules, and each release is nearly always a
# gateway exit that authorized and then sent no terminal, so it is worth a
# person's look the same day:
#
#   - expired: the reaper, at the reservation's expiry, two hours after the
#     authorization (services/settle_outbox_drain.py).
#   - unstarted_stream: the early rule, minutes after the authorization, for
#     a stream the gateway heartbeats that never sent its first heartbeat
#     (trusted_router/unstarted_release.py).
#
# The drain writes one line for each hold released, naming the rule's counter
# and nothing else: no workspace, key, authorization or amount. Each rule has
# a log-based metric, which is the count of holds released, and a policy that
# notifies at most once an hour while lines keep coming.
#
# A third line is the early rule's evidence: a request the rule covers that
# turned out to be alive (a first heartbeat after the rule's deadline, or a
# charging settle for a heartbeated stream with no heartbeat on it). The rule
# rests on neither happening, so one line is an error to look at before the
# rule is turned on, and a reason to turn it off after.
#
# The control plane's application logger writes to stderr as
# "LEVEL logger message" (trusted_router.main, _ApplicationConsoleFormatter),
# which Cloud Run keeps as the entry's textPayload. Each filter matches the
# level, the logger and the name with the space after it, so no other line,
# and no longer name, matches. The settle drain worker's output boundary
# keeps a listed counter's name in the same place
# (services/settle_drain_observability.py), so the one filter matches the
# line on an API service and on the worker.

locals {
  hold_release_lines = {
    expired = {
      payload  = "WARNING trusted_router.services.settle_outbox_drain metric=gateway_hold_released_expired_total "
      title    = "TR Holds: released at expiry"
      severity = "WARNING"
      period   = "3600s"
      what     = "The reaper gave a hold back at its reservation's expiry, two hours after the authorization, with no settle or refund from the gateway."
    }
    unstarted_stream = {
      payload  = "WARNING trusted_router.services.settle_outbox_drain metric=gateway_hold_released_unstarted_stream_total "
      title    = "TR Holds: released early, stream never started"
      severity = "WARNING"
      period   = "3600s"
      what     = "The early rule gave back the hold of a heartbeated stream that never sent its first heartbeat, with no settle or refund from the gateway."
    }
    contradiction = {
      payload  = "ERROR trusted_router.unstarted_release unstarted_release.contradiction "
      title    = "TR Holds: early release contradicted"
      severity = "ERROR"
      period   = "300s"
      what     = "A request the early rule covers turned out to be alive: a first heartbeat after the rule's deadline, or a charging settle with no heartbeat."
    }
  }
  hold_release_filters = {
    for kind, line in local.hold_release_lines : kind => join(" AND ", [
      "resource.type=\"cloud_run_revision\"",
      "textPayload:\"${line.payload}\"",
    ])
  }
}

resource "google_logging_metric" "hold_release" {
  for_each = local.hold_release_lines

  project     = local.gcp_project_id
  name        = "gateway_hold_release_${each.key}"
  description = "${each.value.what} One line a hold or a request (docs/runbook.md, Holds released without a terminal)."
  filter      = local.hold_release_filters[each.key]

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "hold_release" {
  for_each = local.hold_release_lines

  project      = local.gcp_project_id
  display_name = each.value.title
  combiner     = "OR"
  severity     = each.value.severity

  conditions {
    display_name = "The control plane wrote a ${each.key} hold release line"
    condition_matched_log {
      filter = local.hold_release_filters[each.key]
    }
  }

  # One notification per period while lines keep coming, the incident
  # closing on its own half an hour after the last line. A log-matched
  # policy must carry the rate limit. A release is a signal for the same
  # day, not a page: an hour. A contradiction is the fast path's five
  # minutes.
  alert_strategy {
    notification_rate_limit {
      period = each.value.period
    }
    auto_close = "1800s"
  }

  documentation {
    content   = "${each.value.what} The line names no customer; the count is the metric `logging.googleapis.com/user/gateway_hold_release_${each.key}`. What it means and what to do: docs/runbook.md (Holds released without a terminal) in Lore-Hex/quill-router."
    mime_type = "text/markdown"
  }

  notification_channels = local.fastpath_alert_channels
}
