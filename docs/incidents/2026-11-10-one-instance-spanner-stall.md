# November 10 billing path 5xx: one instance lost Spanner for 85 seconds

All times are UTC. Raw logs and tenant identifiers are not published here.

**The cause is not established.** The evidence narrows it to two candidates
and cannot separate them. This change adds the two log lines that will, and
removes one of the two mechanisms for the common case. It does not claim to
prevent a recurrence.

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
stays open but stops carrying streams, each call waits for its deadline. The
SDK sends a keepalive ping after 120 seconds without traffic and gives it 20
seconds, so gRPC tears the connection down and redials somewhere between 20
and 140 seconds into such a stall. An 85 second stall, slow first requests
and a cold cache afterward all fit a reconnect. That is suggestive. It is not
proof: nothing logged the connection's state.

**A credential refresh that hung.** google-auth refreshes an access token
once it is within 3 minutes 45 seconds of expiry, on the path of whichever
RPC notices, and that RPC waits for the result. A refresh against an
instance metadata server that does not answer takes about 30 seconds to give
up (five attempts, three seconds each, with backoff). That is longer than
the 20 second request budget, so every RPC would end in `DeadlineExceeded`
without leaving the process, which matches the server-side count. Nothing
logged the refresh either.

Ruled out: a connection that failed to re-establish (that surfaces as
`UNAVAILABLE`), a rate-limited metadata server (google-auth retries those
quickly and they do not hang), and a path that dropped packets for the whole
window with keepalive working (that also ends in `UNAVAILABLE`).

Neither google-auth nor gRPC can be read from our logs. The control plane
attaches its console handler to the `trusted_router` logger only, and the
log shipper drops the `google` and `grpc` namespaces as noise. The absence
of a metadata-server warning in the window is therefore not evidence against
the second candidate.

## Change

`trusted_router.storage_gcp_transport_watch`, applied to the main store's
database at construction.

Reported, under the `trusted_router` logger:

- `spanner channel state=READY previous=CONNECTING previous_ms=…` on every
  connectivity transition of the Spanner channel. The watch observes; it does
  not ask the channel to connect.
- `spanner credential refresh ok elapsed_ms=… inline=… ttl_s=…` for every
  refresh, at WARNING when it took a second or longer, and
  `spanner credential refresh failed error_class=… elapsed_ms=… inline=…
  suppressed=…` for failures, at most once every five seconds. `inline` says
  whether an RPC was waiting on it.

Only fixed words, class names and numbers are logged. No exception text, no
token, no endpoint.

Changed: the main store's Spanner credentials now use google-auth's
non-blocking refresh. A token inside its refresh window is still valid, so
the RPC is sent with it and the refresh runs on a worker thread. The mode has
to be set on the transport's credentials, because the SDK signs with a scoped
copy of the client's and the copy does not inherit it.

## What this does not do

- It does not bound a stall on an unresponsive connection. Keepalive already
  bounds that at about 140 seconds. Shortening the keepalive interval risks
  the front end closing connections for pinging too often, and resetting the
  channel from the application on a failure streak is the fleet-wide reset
  the October 15 report advised against on thin evidence.
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
| No `spanner channel state` line during the stall, then a transition away from `READY` and back as it ends | The connection stopped answering and keepalive replaced it. |
| Channel `READY` throughout, with `spanner credential refresh failed` or a slow `ok` line, `elapsed_ms` in the tens of thousands | The refresh hung. |
| Channel `READY` throughout and no refresh line | Neither candidate. The stall is above the transport or below the connection in a way these lines do not see. |

[Issue #1813](https://github.com/Lore-Hex/quill-router/issues/1813) stays
open. The October 15 and October 22 stalls it tracks had a different shape:
some calls succeeded during them.
