# Holds the control plane released with no terminal from the gateway
# (docs/runbook.md, "Holds released without a terminal";
# docs/design/fast-admission-streams.md section 13).
#
# A gateway request ends in a settle or a refund. A hold that has neither is
# released by one of two rules, with nothing booked, and each one is nearly
# always a gateway exit that authorized and then sent no terminal. Until the
# release the customer's credit is locked, so it is worth a person's look
# the same day:
#
#   - expired: the reaper, at the reservation's expiry, two hours after the
#     authorization (services/settle_outbox_drain.py). There are tens of
#     these a week today, and the policy notifies on them on purpose: each
#     is a hold a customer waited two hours for.
#   - unstarted_stream: the early rule, minutes after the authorization, for
#     a stream the gateway heartbeats that never sent its first heartbeat
#     (trusted_router/unstarted_release.py). None while the rule is not on.
#
# The drain writes one line for each hold released, naming the counter and
# nothing else: no workspace, key, authorization or amount. Each rule has a
# log-based metric, the count of holds released, and a policy that notifies
# at most once an hour while lines keep coming.
#
# Not counted: a stream that had started and then lost its gateway. The
# reaper ends it at its last heartbeat's snapshot and charges that snapshot;
# its own line says how many (gateway.reaped ... snapshot_bookings=N).
#
# Two more lines are the early rule's evidence, read by the gate that turns
# the rule on (docs/runbook.md, "Early release of unstarted holds"):
#
#   - contradiction: a request the rule covers that turned out to be alive
#     (a first heartbeat after the rule's deadline, or a settle with a
#     charge for a heartbeated stream with no heartbeat on it). The rule
#     rests on neither happening, so one line is an error to look at before
#     the rule is turned on, and a reason to turn it off after.
#   - observation_failed: an observation that did not complete (an observer
#     that could not finish, or a pass that did not examine every row it was
#     meant to). A window with one of these is not evidence of no
#     contradiction.
#
# The control plane's application logger writes to stderr as
# "LEVEL logger message" (trusted_router.main, _ApplicationConsoleFormatter),
# which Cloud Run keeps as the entry's textPayload. Each filter matches the
# level, the logger and the counter's name with the space after it, so no
# other line, and no longer name, matches. The settle drain worker's output
# boundary keeps a listed counter's name in the same place
# (services/settle_drain_observability.py), so the one filter matches the
# line on an API service and on the worker.

locals {
  hold_release_lines = {
    expired = {
      payload  = "WARNING trusted_router.services.settle_outbox_drain metric=gateway_hold_released_expired_total "
      title    = "TR Holds: released at expiry"
      severity = "WARNING"
      period   = "3600s"
      what     = "The reaper released a hold at its reservation's expiry, two hours after the authorization, with no settle or refund from the gateway and nothing booked. Expect tens a week until the gateway's exits are fixed; each is a hold a customer waited two hours for."
    }
    unstarted_stream = {
      payload  = "WARNING trusted_router.services.settle_outbox_drain metric=gateway_hold_released_unstarted_stream_total "
      title    = "TR Holds: released early, stream never started"
      severity = "WARNING"
      period   = "3600s"
      what     = "The early rule gave back the hold of a heartbeated stream that never sent its first heartbeat, with no settle or refund from the gateway and nothing booked."
    }
    contradiction = {
      payload  = "ERROR trusted_router.unstarted_release metric=unstarted_release_contradiction_total "
      title    = "TR Holds: early release contradicted"
      severity = "ERROR"
      period   = "300s"
      what     = "A request the early rule covers turned out to be alive: a first heartbeat after the rule's deadline, or a settle with a charge and no heartbeat."
    }
    observation_failed = {
      payload  = "WARNING trusted_router.unstarted_release metric=unstarted_release_observation_failed_total "
      title    = "TR Holds: early release evidence incomplete"
      severity = "WARNING"
      period   = "3600s"
      what     = "An observation the early rule is judged by did not complete: an observer on the heartbeat or settle path could not finish, or a pass did not examine every row it was meant to. The window it falls in is not evidence that the rule is safe to turn on."
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
  description = "${each.value.what} One line a hold, a request or a pass (docs/runbook.md, Holds released without a terminal)."
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
  # day, not a page: an hour, as is evidence that did not complete. A
  # contradiction is the fast path's five minutes.
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
