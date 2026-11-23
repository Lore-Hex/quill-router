# Holds the control plane released with no terminal from the gateway
# (docs/runbook.md, "Holds released without a terminal").
#
# A gateway request ends in a settle or a refund. A hold that has neither is
# released by the reaper at the reservation's expiry, two hours after the
# authorization (services/settle_outbox_drain.py), with nothing booked. Each
# one is nearly always a gateway exit that authorized and then sent no
# terminal, and until the release the customer's credit is locked, so it is
# worth a person's look the same day.
#
# The drain writes one line for each hold released, naming the counter and
# nothing else: no workspace, key, authorization or amount. The log-based
# metric is the count of holds released, and the policy notifies at most
# once an hour while lines keep coming. There are tens of these a week
# today, and the policy notifies on them on purpose: each is a hold a
# customer waited two hours for.
#
# Not counted: a stream that had started and then lost its gateway. The
# reaper ends it at its last heartbeat's snapshot and charges that snapshot;
# its own line says how many (gateway.reaped ... snapshot_bookings=N).
#
# The control plane's application logger writes to stderr as
# "LEVEL logger message" (trusted_router.main, _ApplicationConsoleFormatter),
# which Cloud Run keeps as the entry's textPayload. The filter matches the
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
  description = "${each.value.what} One line a hold (docs/runbook.md, Holds released without a terminal)."
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
  # day, not a page: an hour.
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
