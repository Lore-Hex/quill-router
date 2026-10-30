# Streams on the fast-path lease

Status: design, no code. Amends [`fast-admission-pilot.md`](fast-admission-pilot.md)
and [`fast-admission-e2-decide.md`](fast-admission-e2-decide.md) §3 and §5
("The two figures", "Streams"), which keep every streaming authorize out of
the lease cohort (Python `pilot_reason` "stream", Go `body.stream`). This
document says why that exclusion no longer has its stated cause, what still
keeps streams out, what Python and Go must change, how money stays bounded,
how parity is measured, and in what order it ships. Nothing here changes a
decision until §10's increment 4, behind a setting that is off.

Line citations are against quill-router `origin/main` at `c06417b7f` and
quill-cloud-proxy `origin/main` at `bb69aa0c` (prefixed `qcp:`).

## 1. Why

On production's edge today `body.stream` is about 26% of authorize passes,
the largest pass reason left after windowed keys
([`fast-admission-e2-key-windows.md`](fast-admission-e2-key-windows.md)).
How much of it this design can reach is measured first (§8.1): only streams
the enclave heartbeats can join.

## 2. The stated reason, and what really keeps streams out

### 2.1 The stated reason

The owner bounds a heartbeat's running charge by the lease's hold, so a
stream admitted with a hold below its spend ceiling would be cut at the hold:

- `owner.Admission`'s comment (`fastpath/internal/owner/requests.go:61-69`);
- e2-decide's lease row of "The two figures" table
  (`fast-admission-e2-decide.md:586`) and "Streams" (`:610-620`);
- batched settlement §4.4 step 3
  (`fast-admission-and-batched-settlement.md:599-605`): "until an admission
  carries e beside r".

### 2.2 A stream's hold is its ceiling

Since `6ea0f9eff`, `d15f3c0be` and `63b64d601`, a stream reserves its
estimate on both sides:

- **Python.** `finish_candidates` (`authorize_candidates.py:355`) prices
  the reservation at the whole output budget when `stream` is true
  (`:479-483`); the user-provided, partner and video branches set
  `model_reservation = model_estimate` for every request (`:430-454`).
  Every later adjustment applies the same function to both figures: the
  receipt fee (`routes/internal/gateway.py:1623-1625`), the custom-model
  markup (`:1632-1640`), the additional cost (`:1643-1647`) and the app
  markup (`:1672-1673`). So for a stream `reservation == estimate`.
  `_fastpath_admission` is called with `estimate=reservation`
  (`gateway.py:1816`) and the row is written with `estimate=estimate`
  (`:1849`), the row's `estimated_microdollars`.
- **Go.** `Evaluate` sets `reserved = usage` for `req.Stream`
  (`routingsnapshot/evaluate.go:417-427`), so `Result.Reservation ==
  Result.ModelEstimate`.
- **Goldens.** `authorize_vectors.py:1134-1140` records Python's stream
  reservation on the `pass:body.stream:hold_cap:*` cases;
  `test_fastpath_authorize_vectors.py:503-514` holds it equal to the
  matching answer's estimate and above that answer's reservation;
  `decide_test.go:1459` and `:1506` hold the evaluator's figures to it.

Precisely: for a stream, the front door's `AuthorizeOf.Estimate`, the
owner's `Admission.Estimate` and so the hold `h.estimate`, the row's
`estimated_microdollars`, `storage_gcp_stage_d._cap_micro`
(`storage_gcp_stage_d.py:298`) and the 200's `cap_micro`
(`gateway.py:3334-3356`) are one number, e. The owner refuses a running
charge above `h.estimate` (`requests.go:268-271`), the record refuses one
above its estimate (`record/record.go:299`), and the auditor's reap charges
`min(running, estimate)` (`auditor/runtime.go:634`). Each equals Stage D's
cap, `usage_exceeds_cap` at `storage_gcp_stage_d.py:142`. The stated cause
is gone, for as long as streams keep the whole-budget reservation; the
e2-decide "Streams" follow-up that would lower it gets a tripwire (§5.1).

### 2.3 What still keeps streams out

1. **No heartbeat reaches an owner.** `_heartbeat_gateway_sync`
   (`gateway.py:719-766`) runs only the typed store's
   `heartbeat_gateway_typed` on the row. Settle and refund forward a leased
   authorization (`gateway.py:4604`, `fastpath_settle.forward_terminal`);
   heartbeats do not, and `FrontDoorClient` has no heartbeat method. A
   leased stream's owner would see no heartbeat and reap or release it
   while it runs.
2. **Python cannot echo a deadline.** Every heartbeat after a hold's first
   must echo the deadline the last granted (`requests.go:268-269`). The
   enclave sends none (`qcp: internal/trustedrouter/stage_d.go:89-97`) and
   Python keeps no per-stream state outside the row.
3. **The owner refuses a running charge below the last**
   (`requests.go:269`, `hb.Running < h.running`). Today's rule checks each
   count, not the charge (`storage_gcp_stage_d.py:124-126`). A charge falls
   while every count rises when a cache-read count arrives after the prompt
   was reported: `normalized_delivered_prompt` moves those tokens from the
   input rate to the cheaper cached rate (`storage_gcp_stage_d.py:222-232`).
   Today that stream runs on; under the lease it would be cut.
4. **A late terminal after an owner reap never resolves.** A settle or
   refund answered `won` with kind `reap` is `Undecided`
   (`fastpath_settle.py:109-113`), so its intent stays pending forever.
   Non-streams never meet this: only a stream has a snapshot to reap.
5. **Python's reaper would book a leased stream.** It books a row's
   snapshot when `started_at` is set and the row has a pricing snapshot
   (`storage_gcp_authorize.py:1461-1465`). A leased non-stream never has
   `started_at`; a leased stream would, once its heartbeat writes the row,
   and the lease books the same snapshot.
6. **The edge cannot compose an eligible stream.** Its `stage_d` member is
   always `eligible: false` (`gwauth/response.go:145-148`),
   `StageDReasonOf` assumes no stream (`gwauth/staged.go:180-198`), and it
   writes no `pricing_snapshot`.

## 3. What exists today

### 3.1 Python's heartbeat

`POST /internal/gateway/heartbeat` (`gateway.py:2567`) answers 503 with
`Retry-After: 1` when `stage_d_heartbeat_enabled` is off (`:701-706`).
Otherwise `_heartbeat_gateway_sync` reads the row, checks the boot
signature against the row's `stage_d_boot_kid` (`_gateway_boot_auth_accepted`, `:769-796`), and runs
`heartbeat_gateway_atomic` (`storage_gcp_stage_d.py:48`) in one
transaction:

- `unknown_authorization` without a row or reservation; `already_terminal`
  when either is settled; `out_of_cohort` without a pricing snapshot or
  with `stream` false (`:77-93`);
- a lower seq, or the same seq with another hash, `stale_seq`; the same seq
  and hash a replay, answered from the stored usage (`:95-120`);
- a changed endpoint after seq 1 `endpoint_mismatch`, any count below the
  stored one `usage_regression`, more tokens than authorized or a running
  charge above `_cap_micro` `usage_exceeds_cap` (`:122-143`);
- writes `heartbeat_seq`, `heartbeat_at`, `heartbeat_hash`,
  `delivered_usage`, and at seq 1 `started_at` and `selected_endpoint_id`
  (`:144-173`), and renews the reservation's `expires_at` to now plus
  `heartbeat_grace_seconds`, 300 (`:174-187`).

It answers `{"accepted", "seq", "expires_at_ms", "cap_micro",
"running_micro"}` (`gateway.py:760-766`); a rejection is
`heartbeat_rejected` with the reason, 404 for `unknown_authorization`, 401
for `boot_not_accepted`, 409 otherwise (`:958-966`).

### 3.2 The enclave's side

- It heartbeats a stream only when its boot runs `QUILL_USAGE_HEARTBEAT`
  and the authorization says `stage_d.eligible`, on `chat.completions` and
  `responses` (`qcp: cmd/enclave/stage_d.go:64-69`).
- The first heartbeat is sent in `preHeader`, after the provider's first
  byte and before the response head (`qcp: cmd/enclave/main.go:2019`); a
  failure there answers 503 and sends no settle. Then one every 10 s or 256
  output tokens, each within a 5 s budget (`stage_d.go:18-23`), retried with
  the same signed bytes (`internal/trustedrouter/stage_d.go:152-195`).
- From an answer it reads `accepted` and `seq` (must equal the one sent),
  takes `cap_micro` only when it had none and logs a mismatch otherwise,
  and logs `running_micro` (`stage_d.go:325-337`). `expires_at_ms` is
  decoded and never read (`internal/trustedrouter/stage_d.go:102` is its
  only mention).
- A rejection with a reason from its closed set
  (`internal/trustedrouter/stage_d.go:109-126`) or any other failure stops
  the stream, which then settles what it delivered.
- Its boot declares `usage_heartbeat` exactly when it heartbeats, and never
  `stream_open_heartbeat` (`internal/trustedrouter/spend_lease.go:40-56`;
  `stage_d.go:49-53`).

### 3.3 Go's heartbeat path

- The front door serves `POST /v1/heartbeat` (`frontdoor/http.go:31`):
  `FrontDoor.Heartbeat(HeartbeatOf{Envelope, GatewaySeq, Hash, Usage,
  Running, Echoed, Basis})` (`frontdoor.go:271-298`) opens the envelope
  (`invalid` if it does not), and sends `OwnerHeartbeat` to the owner it
  names or through a peer (`peers.go:16-42`, `/peer/heartbeat`), else
  `retry`.
- `Local.Heartbeat` (`local.go:65-81`) maps the owner's errors: `accepted`
  with the deadline, `stale`, `rejected` (also an unknown hold),
  `deadline_passed`, `decided`, and `retry` for everything else, a lease it
  does not hold included.
- `Lease.Heartbeat` (`owner/requests.go:226-327`) applies the checks above,
  joins the stream's buffer to the lease's at its first heartbeat, grants
  `now + HeartbeatEvery` capped at the hold's end of life, and publishes a
  `Heartbeat` record whose first carries `Basis` and the boot binding
  (`:296-307`); the answer waits for the publish's acknowledgement before
  the cutoff (`heartbeatAnswer`).
- The owner reaps a heartbeated hold at its deadline plus the grace
  (`adopt.go:113-115`, `due` at `:370`), releases one that never
  heartbeated at its end of life plus the grace (`:116-118`), and releases
  a declared stream with no heartbeat issued after the first-heartbeat
  allowance (`releaseDue`, `:119`, `released` at `:246-249`), only for
  holds admitted with `OpenHeartbeat`.
- Defaults (`service/service.go:144-159`): `HeartbeatEvery` 30 s, the grace
  1 min, `FirstHeartbeat` 10 s, the hold's life `MaxLife + Window`, 2 h 20
  min 30 s. The load generator plays streams end to end
  (`loadgen/loadgen.go:621-665`).

### 3.4 Python's leased authorization

`_authorize_gateway_leased` (`storage_gcp.py:5033-5161`) writes, in one
commit, the full typed authorization row (pricing snapshot, `stage_d_*`
columns, candidates, model, fees, `fastpath_envelope`,
`fastpath_end_of_life` in the payload) and a reservation row that holds
nothing (credit 0, key 0) with today's 2 h expiry. A leased terminal is
made durable as an outbox intent, forwarded, and on `won` written with the
records in one commit (`gateway.py:5347-5452`). Python's reaper finalizes
an expired leased row `refunded` with nothing booked, which matches the
owner's uncharged release of a hold that never heartbeated.

## 4. The design

### 4.1 Shape

The enclave changes nothing. Its heartbeat reaches Python as today, through
the edge, which passes every path but authorize to Python
(`Edge.ServeHTTP`, `edge/edge.go:379`), so one Python change serves leases from both
front doors. When the row carries `fastpath_envelope`,
`_heartbeat_gateway_sync`:

1. runs today's `heartbeat_gateway_atomic` unchanged: every check, reason
   and replay of §3.1, the row's snapshot, the renewal of the zero
   reservation's expiry;
2. on `accepted`, sends the heartbeat to a front door (§4.2);
3. answers from both (§4.4).

The branch is chosen by the row, never by a setting: turning the switch off
must not orphan a stream already admitted under a lease.

The row transaction stays for now. It gives the leased heartbeat exactly
today's validity rules and rejection reasons, it keeps the row's snapshot
for the records of a reap (§6.4), and its expiry renewal keeps Python's
reaper off a live stream. It costs what a heartbeat costs today; removing
it is §9's.

### 4.2 What Python sends

`HeartbeatOf`, from the request, the row and the transaction's result:

| Field | Value | From |
|---|---|---|
| `Envelope` | `authorization.fastpath_envelope` | the row's payload |
| `GatewaySeq` | `body.seq` | the request |
| `Hash` | the 32 bytes of SHA-256 over today's canonical body, `json.dumps(body.model_dump(exclude_none=True), sort_keys=True, separators=(",", ":"))` (`gateway.py:743-747`); the row stores the same digest as hex | the request |
| `Usage` | `input_tokens + output_tokens` of `body.usage.canonical_counts()`; the transaction has already refused any count below the stored one, so the sum never falls | the request |
| `Running` | `HeartbeatResult.running_micro` (§4.3) | the transaction |
| `Echoed` | zero; `EchoGranted` true on every seq above 1 (§4.6) | |
| `Basis` | seq 1 only: §4.5's document | the row and the request |

`started_at_ms`, `elapsed_ms`, `selected_endpoint_id` and `stream` reach
the owner only through the hash and the basis: the owner validates no
field Python has not validated first.

A replay (`HeartbeatResult.replay`) is forwarded too, with the same seq,
hash and running charge: the owner may not have the first attempt. The
owner answers a replay it has as the heartbeat it repeats, with no second
publish (`requests.go:253-261`).

### 4.3 The running charge

Python prices it, the owner records it. `running_micro` is
`delivered_usage_charge_microdollars` (`storage_gcp_stage_d.py:253-286`):
the delivered counts priced at the selected endpoint through the
authorization's frozen prices (its billing snapshot when it has one, else
its pricing snapshot), then the receipt fee and the app markup (zero in the
cohort, §5.1). That is batched settlement §4.5's "the owner prices each
heartbeat once, through the same frozen fee layers as a settle"
(`fast-admission-and-batched-settlement.md:854-858`) and the price basis of
§4.13: the authorization's prices, which the row already holds. The same
function prices today's heartbeat, today's snapshot reap
(`storage_gcp_authorize.py:1487-1493`) and §6.4's leased reap record, so
the three cannot disagree.

### 4.4 What Python answers

The enclave sees today's contract: the same success shape, and only
reasons from its closed set.

| Transaction | Front door | Python's answer |
|---|---|---|
| rejected | not asked | today's 404, 401 or 409 with today's reason |
| accepted | `accepted` | 200 `{"accepted": true, "seq": seq, "expires_at_ms": Deadline in ms, "cap_micro": result.cap_micro, "running_micro": result.running_micro}` |
| accepted | `stale` | 409 `stale_seq` |
| accepted | `decided` | 409 `already_terminal` |
| accepted | `deadline_passed` | 409 `already_terminal` (§11 item 4) |
| accepted | `rejected` | 409 `out_of_cohort`, logged as `fastpath.heartbeat_divergence` |
| accepted | `invalid` | 409 `out_of_cohort`, logged and alerted: the envelope's seal failed |
| accepted | `retry`, or no answer in the budget | 503 `Retry-After: 1`, as the kill switch answers |

`cap_micro` is e on both paths (§2.2), so the enclave's mismatch log stays
quiet. `expires_at_ms` becomes the owner's deadline (about 30 s ahead)
instead of now plus 300 s; no enclave build reads it (§3.2). A `rejected`
after the transaction accepted means the two validations disagree. With
§4.6's changes and §5.1's tripwire every owner check is one the
transaction made first (seq, hash, basis, usage never falling, running at
most e), so only a defect or an envelope for a hold the lease never had
reaches it; it is counted, and the gate (§8.3) requires zero.

### 4.5 The first heartbeat and the full record

Batched settlement §4.9 (`:1335-1342`) has a stream's first heartbeat
publish its full authorization record and carry what a reap needs. Here:

- **Already written by Python's leased authorize:** the full authorization
  record, durable before the 200, in the typed row (§3.4): model, provider,
  candidates, pricing snapshot, fees, token bounds, boot kid, envelope.
  Python's records for every terminal are built from that row, so no
  record-topic copy is needed for Python, and Go's owner publishes none
  (`requests.go:296-307` hands over one record).
- **Published by the first heartbeat:** the owner's first heartbeat record
  carries `Basis` and the hold's boot binding (`record.go:86-91`), kept on
  the hold's row (`auditor/member.go:252`) and copied into any reap's full
  record (`auditor/runtime.go:636-638`), so the lease's own records rebuild
  a reap from the record topic's export alone. Python's `Basis` is the
  canonical JSON (sorted keys, no spaces) of:

  ```
  {"v": 1, "authorization_id", "workspace_id", "key_hash", "model_id",
   "provider", "endpoint_id": body.selected_endpoint_id,
   "price": the endpoint's entry from the frozen prices,
   "pricing_snapshot_sha256", "receipt_fee_basis_points",
   "app_markup_basis_points", "estimate", "stage_d_prompt_tokens",
   "stage_d_max_output_tokens", "boot": the base64 of the boot kid}
  ```

  It is a pure function of the row and seq 1's endpoint, so a replay of
  seq 1 sends the same bytes. One endpoint's price entry keeps it far under
  the owner's limit (`maxHeartbeat`, `requests.go:56-58`).

### 4.6 The Go changes this needs

Three, all optional on the wire and taken by owners before front doors and
by front doors before Python sends them (the order `OpenHeartbeat` used,
`frontdoor.go:194-200`):

1. **`EchoGranted bool`** (omitempty) on `HeartbeatOf`, `OwnerHeartbeat` and
   `owner.HeartbeatOf`. With it set and `Echoed` zero, the owner reads the
   echo as `h.deadline`, the deadline it last granted the hold, for both the
   zero-echo check (`requests.go:268-269`) and `deadline_passed`. That is
   exactly what the echo asserts: the gateway's answer was the last one
   granted. A gateway that lost an answer retries the same seq, which is a
   replay. Python would otherwise need a write per heartbeat to keep the
   deadline, or would fabricate one and void `deadline_passed`.
2. **A running charge may fall** while `Usage` does not: drop
   `hb.Running < h.running` (`requests.go:269`). Today's rule is per count
   (§2.3 item 3), and the owner then records the lower figure, which is the
   one today's reaper charges. `Running <= estimate` and `Usage`
   monotonicity stay. `TerminalOrder` (§5.1 of batched settlement) models
   no running-charge order, so no spec changes; a test pins the case.
3. **`POST /v1/disposition {Envelope}`** on the front door: opens the
   envelope and answers `store.Disposition` (`store/records.go:237-293`),
   `{Outcome, Cost, CostKnown}`, the read `appendTerminal` already does
   (`frontdoor.go:412`). Python's reaper uses it (§6.4) instead of reading
   the lease's tables itself.

### 4.7 Budget and capacity

A heartbeat cannot fall back to another path, as an authorize can, so it
gets its own permits in `FrontDoorClient` rather than sharing the 64
authorize workers (`fastpath_frontdoor.py:59`): a burst of authorizes must
not starve heartbeats into 503s that stop streams. Its budget is
`fastpath_front_door_timeout_seconds` (1.5 s), which with the transaction
fits one enclave retry inside the enclave's 5 s. A heartbeat may go to any
serving front door; it relays to the owner (§3.3).

## 5. The cohort and the switch

### 5.1 The rule

A stream joins the lease cohort only when all of these hold, on both front
doors, in addition to today's cohort:

1. `fastpath_pilot_streams` is on (§5.4);
2. Stage D's reason is `ok` (`_stage_d_eligibility_reason`,
   `gateway.py:3285-3331`): eligibility on, the Stage D pilot set, heartbeats
   on, an accepted boot, `chat.completions` or `responses`, every candidate
   in credits, standard pricing, no priority or auto tier, the typed
   backend. This is batched settlement §4.11's "streams the enclave does not
   heartbeat" stay synchronous (`:1440-1484`);
3. the boot declared `usage_heartbeat` (`boot_context["heartbeat_declared"]`,
   `gateway.py:1049-1050`), whatever `stage_d_require_heartbeat_declaration`
   says: a verified boot alone does not prove it heartbeats;
4. `reap_snapshot_booking_enabled` is on (`config.py:1128`): the lease
   always charges an abandoned stream its snapshot, so Python's path must
   too, or the lease would charge where Python does not (§7);
5. `reservation == estimate`, a tripwire: if the e2-decide "Streams"
   follow-up ever lowers a stream's reservation, streams fall out of the
   cohort instead of being cut at the hold.

`OpenHeartbeat` is not sent: no boot declares `stream_open_heartbeat`
(§3.2), so no leased stream is released by the first-heartbeat allowance.

### 5.2 Python

`pilot_reason` (`fastpath_frontdoor.py:432-481`) keeps `stream` as its
reason for a stream outside the rule, with the failing condition logged
beside it (`stream.switch`, `stream.stage_d:<reason>`,
`stream.undeclared`, `stream.snapshot_booking`, `stream.hold`), and passes
a stream that meets it on to the checks below. `_fastpath_admission`
(`gateway.py:5253`) gains the inputs; `stage_d_reason` and the boot context
are computed before it (`gateway.py:1688-1705`, `:1049`).

### 5.3 The edge

`gwauth`'s request cohort keeps `body.stream` (`cohort_request.go:75-76`)
unless the policy carries the member. With it, a stream goes on to the
facts, and `StageDReasonOf` grows Python's whole rule for streams (route,
candidate usage types, pricing kind, service tier, backend), counted under
`stream.stage_d:<reason>` when it is not `ok`. An `ok` stream composes:

- `stage_d: {"eligible": true, "reason": "ok"}`, then `candidate_prices` and
  `cap_micro` (`gateway.py:3349-3356`), spliced after
  `native_batch_eligible` as Python splices `**stage_d` (`:3259`); each
  entry is `endpoint_pricing_candidate`'s (`stage_d.py`), whose price
  members (`price_history_version`, `rates`, `tiers`, `request_fee_micro`,
  `rounding`) the async billing snapshot already writes
  (`gwauth/async.go:321-325`);
- the row's `pricing_snapshot`, `canonical_pricing_snapshot` of the
  candidates' pricing document (`gateway.py:1746-1753`, `stage_d.py:164-173`), byte for byte;
- the admit with `Estimate = Reservation`, refused (pass `stream.hold`)
  unless it equals `ModelEstimate`.

### 5.4 The switch

A Python setting `fastpath_pilot_streams: bool = False`, read in two
places so the cohort stays one definition (e2-decide decision 11):

1. Python's pilot: §5.2.
2. The edge policy: an optional member `fastpath_pilot_streams: true`,
   published only when the setting and `reap_snapshot_booking_enabled` are
   both on, absent otherwise, `false` and `null` refused, as
   `fastpath_pilot_key_windows` is (`edge_policy.py:56-62`, `:139-146`;
   `edge_policy_publisher.py:214-215`; `routingsnapshot/edgepolicy.go:38`,
   `:65-91`). Go ships first, accepting it absent or present; Python
   publishes after. A stale policy already passes everything.

## 6. Terminals, reaps and switches

### 6.1 Settle

Unchanged: Python prices the settle body, makes the intent durable and
forwards `/v1/settle` (`gateway.py:5347-5452`). The owner takes a charge
above the hold through the buffer or the drain's raise, as for any leased
settle. The enclave settles a stream that delivered anything, a stopped
one included (`BeforeTerminal`).

### 6.2 Refund

Unchanged. The enclave refunds only a stream that delivered nothing.

### 6.3 A terminal after the owner reaped

Decision 70 (`fast-admission-and-batched-settlement.md:881-882`): the
snapshot wins and a later terminal charges nothing more. `forward_terminal`
(`fastpath_settle.py:74`) answers `won` with kind `reap`, and `settled`
with outcome `reaped_snapshot`, as a decision: `reaped_snapshot`, booked
nothing in Python (the lease booked it), charge the lease's. The records
are §6.4's, not the settle's, as today's reaper writes the snapshot's and a
later settle answers `already_settled` with them. Shared by the route and
the drain.

### 6.4 A stream abandoned after heartbeats

The owner reaps the hold at its last deadline plus the grace (about 90 s
after the last heartbeat) at its last running charge; once the lease
drains, the auditor does it at a tick (`auditor/runtime.go:631-656`).
Python's row expires 300 s after the last heartbeat. Python's reaper:

- never books a row with `fastpath_envelope` (the guard on
  `snapshot_booked`, `storage_gcp_authorize.py:1463`);
- finalizes a leased row with no `started_at` as today, `refunded`;
- for a leased row with `started_at`, reads `/v1/disposition`:
  `reaped_snapshot` with a cost writes today's snapshot records (the
  generation from the row's `delivered_usage` and selected endpoint,
  `finish_reason: reaped_snapshot`, `settled_from: heartbeat`,
  `storage_gcp_authorize.py:1466-1531`) with the lease's cost, through
  `typed_finalize_leased_result` with nothing booked; `refunded` or
  `released` finalizes `refunded`; `settled` leaves it to the settle's
  intent; `pending` pushes the expiry out by the grace and alerts once the
  hold's end of life plus the grace and the drain's overdue allowance have
  passed.

It never sends a refund for a heartbeated stream: in a draining lease that
refund could precede the auditor's reap in the drain log and charge
nothing where today's reaper charges the snapshot.

### 6.5 A stream abandoned before its first heartbeat

No heartbeat issued, no settle (the enclave answers 503 and settles
nothing, §3.2). The owner releases the hold uncharged at its end of life
plus the grace; Python's reaper finalizes the row `refunded` at 2 h, as for
a leased non-stream. Both agree with today, which refunds an unstarted
stream. Until then the hold's e sits in the lease, not on the credit row: a
lease without room answers `busy`, which is Python's path for the pilot and
a pass for the edge, never a 402 or a 503 to the customer. The
first-heartbeat allowance needs `stream_open_heartbeat`, a quill-cloud-proxy
change outside this design (§9).

### 6.6 An owner that stops

Its heartbeats get `retry` from the front door and 503 from Python; the
enclave's retries run out inside 5 s and the stream stops at its next
heartbeat, then settles into the drain log (`recorded`, answered
`intent_durable`; the drain asks again). The auditor reaps open holds at
ticks. Batched settlement §4.5 accepts this cost (`:770-775`): a Python
crash cuts no stream today. A revoked or draining lease answers the same.

### 6.7 Switches

| Switch off | New streams | Leased streams running |
|---|---|---|
| `stage_d_heartbeat_enabled` | Stage D reason `heartbeats_disabled`: Python's path | every heartbeat 503 before the row or the front door (`gateway.py:701-706`): stopped at the next heartbeat, as today |
| `stage_d_eligibility_enabled` | `stage_d_disabled`: Python's path | keep heartbeating; the heartbeat route does not read it, as today |
| `fastpath_pilot_streams`, `fastpath_pilot_enabled` | Python's path (the edge within the policy's 60 s) | keep heartbeating: forwarding follows the row (§4.1) |
| the front door's workspace switch | `off`: Python's path | keep heartbeating: the owner asks the switch only at admission (`requests.go:143`) |
| `reap_snapshot_booking_enabled` | Python's path (§5.1 item 4) | the lease still reaps at the snapshot: turn the switch off and let leased streams end first |

## 7. Money safety

For the same stream, no path admits or charges more than Python's:

| Step | Python's path | Leased |
|---|---|---|
| admission | holds the reservation, e, against the balance | holds e against the lease's room, granted from the balance; `busy` falls back |
| running cap | `usage_exceeds_cap` above `_cap_micro`, e; the enclave cuts at `cap_micro`, e | the same transaction refuses first; the owner and the record refuse above `h.estimate`, e |
| settle | books the priced charge | the owner books the same priced charge; Python books nothing |
| reap with heartbeats | books the last snapshot's running charge, at most e, with `reap_snapshot_booking_enabled` | books `min(running, e)`, the same figure (§4.3), only with that switch on (§5.1) |
| reap without heartbeats | refunded | released uncharged |
| terminal after a reap | charges nothing more (Decision 70) | charges nothing more (§6.3) |
| double booking | one booking per reservation | Python's reaper never books a leased row (§6.4); Python's settle never books a leased one |

Residual risks:

1. **Owner crash cuts streams** (§6.6), which Python's path does not.
2. **Row and owner can disagree by one snapshot.** The row commits first;
   if the front door then fails, the row holds seq k and the lease k-1
   until the enclave's retry, a replay, lands it. If the enclave dies in
   between, the lease reaps at k-1's charge and Python's records carry k's
   counts with k-1's cost: at most one heartbeat's tokens (10 s or 256
   tokens) recorded uncharged, never charged twice.
3. **Heartbeat latency** grows by a front door round trip and a publish
   acknowledgement; the first heartbeat gates the response head, so
   time to first byte grows by the same. Measured in §8.3.
4. **A held e for up to 2 h 20 min** when a stream dies before its first
   byte (§6.5), until enclaves declare `stream_open_heartbeat`.
5. **The reservation tripwire.** Lowering stream reservations takes every
   stream off the lease until an admission carries e beside r (§11 item
   1).
6. **The edge's composition.** A stream's 200 and row carry the full price
   document; a byte off in `candidate_prices` changes what the enclave
   cuts at. §8.4's shadow compares both before the edge decides.

## 8. Shadow comparison and rollout gate

### 8.1 Measurement first

Increment 2's edge splits `body.stream` in the shadow's counts by the
Stage D reason the stream would get and the boot's declaration, still
passing every stream, so the reachable share of the 26% is known before
any leased stream exists.

### 8.2 What is compared today

Nothing for streams: the edge passes them at the request cohort, before
any decision, so the E2 full shadow (`edge/shadow_full.go`), which compares
Python's 200 byte for byte and the rows column by column, never sees one.
Heartbeats are never shadowed. A row Python's heartbeat reached first is
compared only on the columns heartbeats leave alone (`moved`,
`shadow_full.go:1148`; `settleStable`, `:1304`), which is most stream rows
by the time they are read.

### 8.3 Gate P: the Python pilot

The setting on for the pilot workspaces in one region; the edge's member
absent (Python publishes it only after gate P, or the edge's decide rows
stay off). Before the edge moves, over at least 7 days and 1,000 leased
streams settled or reaped:

- `fastpath.heartbeat_divergence` zero, and no `invalid`;
- every leased settle's winner charge equal to Python's priced charge (the
  existing "winner's charge differs" warning, `fastpath_settle.py:115`,
  zero);
- every leased reap's lease cost equal to the running charge Python prices
  from the row's last snapshot, except for risk 2's counted case;
- no leased intent pending past the drain's overdue allowance; every
  `pending` disposition explained;
- heartbeat 503 rate and first-heartbeat p99 within 1.5x Python's own over
  the same span, and no stream stopped by a front door `retry` outside a
  deploy or a stop;
- the reaper's leased outcomes counted: `refunded`, `reaped_snapshot`,
  `pending`.

### 8.4 The edge shadow for streams

Increment 5 composes eligible streams in the shadow only, still passing
them, and the full shadow compares Python's 200, `stage_d`,
`candidate_prices` and `cap_micro` included, and the rows,
`pricing_snapshot` and the `stage_d_*` columns added to `settleStable` so a
heartbeated row still compares them. The authorize vectors gain stream
answer cases (eligible, each Stage D reason, the boot undeclared), with
Python's 200 and rows recorded, in `authorize_vectors.py`.

### 8.5 Gate E: the edge

e2-decide's I2 bar (`fast-admission-e2-decide.md:1714-1777`) over streams
alone, with zero differs in `candidate_prices`, `cap_micro`,
`pricing_snapshot` or `estimated_microdollars`; then Python publishes the
member and the decide rows choose where it applies. The way back, fastest
first: the member off (the policy's 60 s), the setting off, then e2-decide
§9's levers. Running leased streams finish on the lease either way.

## 9. Out of scope

- **Heartbeats without the row transaction.** Python would validate from
  the row read and the owner keep the state; that needs the owner to pin
  the endpoint and check each count, and Python's reap records to come from
  the lease's (`tr_lease_record`). It removes a commit per heartbeat, not an
  authorize pass.
- **`stream_open_heartbeat`**: a quill-cloud-proxy change, then Python
  sends `OpenHeartbeat` from `GatewayBoot.declares_stream_open_heartbeat`
  (`storage_models.py:2008`); the owners already take it.
- **Lowering stream reservations**, or an admission carrying e beside r.
- Streaming `/v1/messages` (never heartbeated), service tiers, markups,
  keys with caps: unchanged exclusions.

## 10. Increments

Each merges dark and deploys before the next. No leased stream exists
until increment 4's setting moves, so increments 1 to 3 run only on rows a
test seeds. Increment 1 is the forwarding; the setting itself lands with
the cohort in increment 4, so it can never admit a stream before the
terminal handling of increment 3 is deployed.

| # | Where | What | Files | Size |
|---|---|---|---|---|
| 1 | Python, dark | `FrontDoorClient.heartbeat` with its own permits (§4.7); `_heartbeat_gateway_sync`'s leased branch: transaction, forward, answer map (§4.2 to §4.5); the basis builder; the reaper never books a leased row (§6.4 first bullet); tests with a fake front door: each answer row of §4.4, replay forwarding, a row without an envelope untouched, the kill switch before the forward | `routes/internal/gateway.py`, `services/fastpath_frontdoor.py`, `services/fastpath_heartbeat.py` (new), `storage_gcp_authorize.py`, `tests/test_stage_d_heartbeat.py`, `tests/test_fastpath_frontdoor.py`, `tests/test_fastpath_pilot_route.py` | ~250 LOC + ~450 test |
| 2 | Go | `EchoGranted` and the falling running charge (§4.6 items 1, 2), owners before front doors; `POST /v1/disposition` (item 3); the edge policy's optional `fastpath_pilot_streams`; the shadow's stream split (§8.1); loadgen plays `EchoGranted` | `owner/requests.go`, `frontdoor/{frontdoor,answers,local,http}.go`, `routingsnapshot/edgepolicy.go`, `gwauth/staged.go`, `edge/shadow.go`, `loadgen/loadgen.go`, tests | ~200 LOC + ~400 test |
| 3 | Python, dark | `forward_terminal`'s reap and `reaped_snapshot` answers (§6.3); the reaper's leased-stream reconcile through `/v1/disposition` (§6.4); the drain the same; integration test against the Go service: lease a seeded stream, heartbeat, stop, reap, late settle | `services/fastpath_settle.py`, `services/fastpath_frontdoor.py`, `storage_gcp_authorize.py`, `storage_gcp.py`, `services/settle_outbox_apply.py`, tests | ~250 LOC + ~500 test |
| 4 | Python | the setting `fastpath_pilot_streams`; `pilot_reason`'s stream rule and sub-reasons (§5.1, §5.2); the edge policy publishes the member; runbook rows for gate P | `config.py`, `services/fastpath_frontdoor.py`, `routes/internal/gateway.py`, `edge_policy.py`, `services/edge_policy_publisher.py`, `docs/runbooks/*`, tests | ~120 LOC + ~300 test |
| 5 | Go + Python vectors | the edge's stream composition in the shadow only (§5.3, §8.4); `StageDReasonOf` for streams; `pricing_snapshot`; stream answer vectors | `gwauth/{staged,response,rows,payload}.go`, `edge/{decide,shadow_full}.go`, `scripts/fastpath/authorize_vectors.py`, testdata, tests | ~400 LOC + ~600 test |
| 6 | Go | the edge decides eligible streams when the member is present (the tripwire of §5.1 item 5) | `gwauth/cohort_request.go`, `edge/decide.go`, `edge/decide_path.go`, tests | ~80 LOC + ~200 test |

Then gate P (§8.3) after increment 4, gate E (§8.5) after 5, and the member
after 6. In all, six code PRs after this one, about 1,300 lines of
production code and 2,450 of tests and generators, the vector JSON aside.

## 11. Decisions

Decided by Joseph on 2026-10-30, on this design's first review:

1. **The tripwire now.** Leased streams require `reservation == estimate`
   (§5.1 item 5). e2-decide's "Streams" follow-up, lowering stream
   reservations, waits for a design that carries the ceiling beside the
   hold (e beside r in the admission, the envelope and the record).
2. **One row transaction per leased heartbeat** stays (§4.1): exact parity
   with today's validity rules and the row's snapshot for reap records.
   Dropping it is §9's, a design of its own.
3. **An owner crash cutting pilot streams is accepted** (§6.6), as batched
   settlement §4.5 accepts it.
4. **`deadline_passed` answers 409 `already_terminal`** (§4.4): the
   enclave has no reason for it, and a 503 would retry into the same
   answer.
5. **`stream_open_heartbeat` is requested from quill-cloud-proxy after
   gate P** (§6.5, §8.3); until then no leased stream is admitted with
   `OpenHeartbeat`.
6. **Increment 1 is keyed on the row's envelope, with no switch** (§10):
   the setting lands with the cohort in increment 4, after increment 3's
   terminal handling.
