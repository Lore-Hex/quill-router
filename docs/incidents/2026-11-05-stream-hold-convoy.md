# November 5 Spanner contention: whole-budget stream holds on one credit row

## Evidence

**TR Spanner: transaction contention** fired twice at 09:11Z and 09:12Z:
more than 100 aborted commits a minute, and lock wait over two seconds per
second. Both cleared within three minutes. Aborted commits ran from 08:56Z
to 09:06Z, highest at 08:56Z and 08:57Z. No request returned 5xx in that
window. The only error statuses on `/internal/gateway/authorize` were 402,
429 and a few 401 and 400.

`SPANNER_SYS.LOCK_STATS_TOP_10MINUTE` named one row for almost all of the
lock wait in the 09:00Z and 09:10Z intervals: one workspace's single
`tr_credit_balance` shard, then its key's `tr_key_limit` row. The waiting
transactions were `tr_authorize`, `tr_refund_one_commit`,
`tr_refund_finalize` and `tr_finalize`. In `TXN_STATS_TOP_10MINUTE` for
09:00Z, `tr_authorize` aborted more often than it committed.

The workspace streamed one model with a large output budget against a
small balance. Each stream held its whole budget's price, about twenty
times what a completed stream cost, so the balance covered only a handful
of streams in flight while the client ran several times that many. The
rest were refused as `billing.insufficient_credits` (402), with the
logged `reserved_microdollars` equal to the estimate. Most admitted streams
failed fast at the provider with a 4xx and were refunded. That freed their
holds for the next requests, so the read-only precheck kept finding room
and sending requests back into the read-write authorize.

## Mechanism

#2065 bounded the credit hold of a catalog request at `HOLD_OUTPUT_TOKENS`
(16,384) output tokens, but a stream kept its estimate as its hold. At the
time, the Stage D heartbeat reader then in service bounded a stream's
running charge by the reservation row's hold, so a lower hold could have
cut a stream during a mixed-version deploy. #2065 moved that reader to the
estimate (`storage_gcp_stage_d._cap_micro`), and every plane has run it
since.

With the whole budget held, a client sending a large `max_tokens` exhausts
a small balance after a few concurrent streams. Every request beyond that
point is an authorize that either refuses after locking the row or wins a
hold another request just released. Every fast failure is a refund on the
same row. A single credit shard serializes all of it. This is the hot-row
class of the September 2 and September 27 incidents, made worse because
the client was refused on money it had.

## Repair

A stream now takes the same bounded hold as any catalog request, in
`authorize_candidates.finish_candidates` and in the Go evaluator alike. Its
estimate is unchanged and is still the Stage D cap it is cut at
(`cap_micro`). For the stream shape in this incident, the hold falls from
the whole budget's price to the prompt plus 16,384 output tokens.

A lease still cuts a stream at its hold. Both front doors keep a stream
whose hold is below its estimate out of the lease cohort (`stream.hold`).
So a pilot stream with an output budget above 16,384 tokens goes back to
Python until an admission carries the ceiling beside the hold
(`fast-admission-streams.md` §11 item 1, revised today). A pilot stream
within the bound stays on the lease.

Accounting: a stream's uncovered cost while in flight,
`max(0, actual - reservation)`, can now be larger, by at most the estimate
minus the reservation. Every other catalog request has had the same bound
since #2065 (`fast-admission-e2-decide.md`, "The two figures").

## The other alert

**TR Spanner: API failures** at 06:18Z: about 20 `deadline_exceeded` and
`cancelled` streaming reads from 06:07Z to 06:13Z, plus aborted commits.
The hottest rows at 06:10Z were one key's `tr_key_limit` shards, under
`tr_settle_leased`, and the fast-path pilot's lease rows. This was a burst
of settles on a high-volume pilot key. The failed reads were reads inside
those transactions that ran past their deadlines while the rows were locked.
The policy fires at about one failure a minute for five minutes. It cleared
in four minutes, with no 5xx. No change is made for it here.
