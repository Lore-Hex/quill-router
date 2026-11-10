# November 10 billing path 5xx: one instance lost Spanner for about 85 seconds

All times are UTC. Raw logs and tenant identifiers are not published here.

**The cause is not established.** Two mechanisms fit the request logs. A
second source, Sentry Logs, makes one of them (a hung credential refresh)
unlikely and leaves the other (one connection that stopped answering), without
proving it. This change adds the log lines that will show which it is next
time, and takes the credential refresh off the request path in the common
case. It does not claim to prevent a recurrence.

## Evidence

**TR Gateway: billing path 5xx** opened at 12:36:53.

One control-plane instance in us-central1, started at 12:00:35, served
Spanner-backed requests normally until 12:35:25. From 12:35:28 to 12:36:49
every Spanner RPC it issued reached its client deadline: the 500 ms admission
reads, the 2 second key lookups, and the 20 second request budget. Requests
succeeded again from 12:36:51. The instance was not restarted.

- 24 internal gateway requests on that instance returned 503 after about 20
  seconds each: 11 heartbeats, 6 authorizes, 4 settles, 2 key lookups and 1
  video job claim. What the gateway returned to the callers behind the six
  authorizes was not traced.
- The seven other us-central1 instances served the same routes normally
  throughout.
- The instance answered `/health` in the window, and the event-loop watchdog
  wrote nothing. The process was running; this is not the event-loop freeze
  of earlier incidents.
- Every failure was `DeadlineExceeded`. There was no `UNAVAILABLE`, which a
  refused or reset connection would have produced.
- Spanner's own request metric counted three `deadline_exceeded` and one
  `cancelled` call in that minute. The instance failed about 200 RPCs. Almost
  none of them reached Spanner.
- For about six seconds after recovery the first requests were slow (the
  Spanner part of a settle took 0.4 to 2.0 seconds against 64 ms before the
  stall) and authorizes reported a cold affinity cache.
- The admission-read failure streak on this instance was the longest of the
  week. The week's other long streaks are spread over 15 to 60 minutes and
  are a different shape.

## What fits

The RPCs did not fail fast and did not reach the server. Something between
the application and the Google front end held them until their deadlines, on
one instance, for a bounded time, and then let go. Two mechanisms do that.

**An open connection that stopped answering.** The instance has one HTTP/2
connection to the front end for all of its Spanner calls. If that connection
stays open but stops carrying streams, each call waits for its deadline, and
that goes on until gRPC gives up on the connection and dials a new one. gRPC
closes a connection whose ping goes unanswered. With the SDK's settings the
keepalive ping bounds that at about 140 seconds after the last byte received
(a ping after 120 seconds of silence, 20 seconds to answer it), and other
pings gRPC sends while calls are in flight can close it sooner. Which timer
ended this stall is not known, and its length does not single one out. Slow
first requests and a cold cache afterward are what a new connection looks
like. Nothing logged the connection's state.

**A credential refresh that hung.** google-auth refreshes an access token
once it is within 3 minutes 45 seconds of expiry, on the path of whichever
RPC notices, and that RPC waits for the result. A refresh against an
instance metadata server that does not answer takes about 30 seconds to give
up (five attempts, three seconds each, with backoff). That is longer than
the 20 second request budget, so every RPC would end in `DeadlineExceeded`
without leaving the process, which matches the server-side count.

Ruled out: a connection that failed to re-establish (that surfaces as
`UNAVAILABLE`), and a rate-limited metadata server (google-auth retries those
quickly and they do not hang).

## What Sentry Logs adds

google-auth and gRPC report through their own loggers. Those reach neither
Cloud Run logs (the control plane attaches its console handler to the
`trusted_router` logger only) nor the log shipper (it drops the `google` and
`grpc` namespaces). Their WARNING records are forwarded to Sentry Logs, with
a limiter in each process of three records per distinct message and 50 per
hour.

google-auth logs a warning for each metadata-server attempt that fails, three
seconds after the attempt starts. So a hung refresh would have produced one
within a few seconds of the stall beginning.

- Sentry Logs accepted new warnings from the stalled instance from 12:35:28
  through 12:35:55 (the admission-read failures and the first 503s). Its
  limiter was open for the first 27 seconds of the stall.
- No record from a `google` or `grpc` logger arrived in the window.
- None arrived in the seven days before either, while records from another
  third-party logger (uvicorn's) did. The path works; these loggers were
  silent.

That makes the refresh candidate unlikely. It is negative evidence read
through a rate-limited pipeline, not proof, and it says nothing positive
about the connection: gRPC handles an unanswered ping in its core, which
does not write to these loggers.

## Change

`trusted_router.storage_gcp_transport_watch`, applied to the main store's
database at construction.

Reported, under the `trusted_router` logger, in the instance's own logs:

- `spanner channel state=READY previous=CONNECTING previous_ms=… client=…`
  on every connectivity transition of the Spanner channel that gRPC reports.
  The watch observes; it does not ask the channel to connect. gRPC samples
  the state a few times a second, so a state that comes and goes between two
  samples is not reported.
- `spanner credential refresh ok elapsed_ms=… inline=… ttl_s=… suppressed=…
  client=…` for refreshes: always, at WARNING, when one took a second or
  longer, and at most once every five seconds otherwise.
- `spanner credential refresh failed error_class=… elapsed_ms=… inline=…
  suppressed=… client=…` for failures, at most once every five seconds.

`inline` says whether an RPC was waiting on the refresh. `suppressed` counts
the lines of that kind held back since the last one.

These lines carry fixed words, class names and numbers: no exception text, no
token, no endpoint.

Changed: the main store's Spanner credentials now use google-auth's
non-blocking refresh. A token inside its refresh window is still valid, so
the RPC is sent with it and the refresh runs on a worker thread. The mode has
to be set on the transport's credentials, because the SDK signs with a scoped
copy of the client's and the copy does not inherit it. This takes away a
mechanism that produces the same symptom. By the evidence above it was
probably not the one at work here.

In that mode google-auth logs a failed background refresh itself, on its own
logger and with the exception's text. That line does not reach Cloud Run logs
or the log shipper. It does reach Sentry Logs, through the scrubber.

## What this does not do

- It does not bound a stall on an unresponsive connection. gRPC's own pings
  do that. Shortening the keepalive interval risks the front end closing
  connections for pinging too often, and resetting the channel from the
  application on a failure streak is the fleet-wide reset the October 15
  report advised against on thin evidence. The channel lines are what would
  justify either.
- It covers the first failed refresh only. After one background refresh
  fails, google-auth goes back to refreshing on the RPC's path, which is the
  behavior before this change. A metadata server that hangs for 85 seconds
  would still stall the instance once the first attempt has given up, for
  roughly the remainder.
- It does not restart the instance. A liveness check that depends on the
  database could recycle healthy capacity during a shared outage.
- The smaller Spanner clients (Google Ads export, shadow observation, shadow
  export) are not watched.

No flag. The way back is a revert.

## Reading the next occurrence

For one instance, during a window in which its Spanner RPCs all reach their
deadlines:

| Seen in the instance's logs | Reading |
|---|---|
| No `spanner channel state` line during the stall, then a transition away from `READY` and back as it ends | The connection stopped answering and gRPC replaced it. `previous_ms` on the line that leaves `READY`, against the time of the last good request, says how long gRPC took. |
| No channel transition logged, with `spanner credential refresh failed` or a slow `ok` line, `elapsed_ms` in the tens of thousands | The refresh hung. |
| No channel transition logged and no refresh line | Neither candidate. The stall is above the transport or below the connection in a way these lines do not see. |

Then search Sentry Logs for the window by logger name (`google.auth*`,
`grpc*`). A metadata-server warning there supports the second row from a
source this module does not write.

[Issue #1813](https://github.com/Lore-Hex/quill-router/issues/1813) stays
open. The October 15 and October 22 stalls it tracks had a different shape:
some calls succeeded during them.
