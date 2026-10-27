# E2 G15: the edge decides async-v1 authorizes

Status: design. A sibling of
[`fast-admission-e2-decide.md`](fast-admission-e2-decide.md) (the E2
contract, "the contract" below), which today passes every authorize
carrying `X-TR-Settlement-Mode` to Python (`header.settlement_async`,
the contract's §3). This document says what Python answers differently
for such a request, what of it Go can reproduce, which way to go, and the
pull requests. It changes no code; the PRs of §6 do.

Citations are against quill-router `origin/main` at `df813de21` and
quill-cloud-proxy `main` at `3c7270e` (`enclave-go/...` paths are that
repository's).

## 1. Recommendation

1. The edge decides async-v1 requests while Python's async admission is
   off, and answers exactly Python's admission-off bytes: `async_eligible:
   false`, plus the frozen `billing_snapshot` and its hash when Python
   would freeze one. No ticket, no status URL, no signer at the edge.
2. That is Python's own answer to every async-v1 request in production
   today (admission is off fleet-wide, §5), and the enclave settles it
   through the ordinary synchronous path (§3.5). Nothing on the money path
   changes. The snapshot is derived from the routing snapshot's price
   epochs, plus one published validity fact per epoch (§3.2).
3. When the edge policy says admission is on, an async-v1 request passes
   with `policy.async_admission`, so every ticket stays Python's. A
   disagreement during a Python rollout that changes admission is a missed
   optimisation, never money (§3.6).
4. Async deciding lands dark: G15c composes and compares async-v1 requests
   but answers none until a published switch, default off, is turned on
   after its own soak (§6.2). Signing tickets at the edge (option (b)) is a
   follow-on design (G16), started when the operator schedules admission
   on; issuing tickets after the fact (option (c)) is rejected (§4).
5. Most observed async-v1 requests also name a `cache_affinity_key` (§5).
   The edge decides an affinity-keyed request in the cold order like any
   other (the contract's §1 item 5, which retired `body.cache_affinity`),
   so G15b and G15c decide async-v1 requests naming a key in the cold
   order too.

## 2. What an async-v1 200 adds

### 2.1 Where

`_gateway_authorize_response` (`gateway.py:3127`) builds the additions
only when the header list is exactly `["async-v1"]`: one value, that case,
no surrounding whitespace (`gateway.py:3179-3188`; `async_settle.py:290-291`
checks again). It splices them into the 200 between `stage_d` and `tags`
(`gateway.py:3247-3249`):

```
... "native_batch_eligible", "stage_d", <async additions>, "tags", "custom_model", "route_candidates"
```

then `timing` last (the contract's §6). Any other header list (absent,
`sync`, two values, another case) adds nothing: the 200 is the sync one.
The additions are rendered by the same pydantic-core path as the rest of
the 200 (the contract's §6): insertion order, compact, integers only (none
of these fields is a float).

### 2.2 The three shapes

`authorize_additions` (`async_settle.py:286-331`) and
`snapshot_projection` (`async_settle.py:243-283`) produce one of three
shapes. `result` starts as `{"async_eligible": False}` and is extended
with `dict.update`, so `async_eligible` keeps the first position even
when its value changes (`async_settle.py:247`, `257`, `277-280`).

| Shape | Members, in order | When (§2.3 has every branch) |
|---|---|---|
| A, false only | `async_eligible: false` | any pre-check, exclusion, snapshot, authority or authorization guard fails |
| B, admission off | `async_eligible: false`, `billing_snapshot`, `billing_snapshot_hash` | every check passes and `async_settle_admission_enabled` is false (`async_settle.py:255-258`); an absent `invocation_nonce` does not prevent it |
| C, admission on | `async_eligible: <bool>`, `billing_snapshot`, `billing_snapshot_hash`, `settlement_ticket`, `settlement_status_url` | every check passes, admission is on and signing succeeds (`async_settle.py:259-280`); `async_eligible` is the pilot and admission answer, and the ticket is signed even when it is false |

**`billing_snapshot`** is `BillingSnapshot.model_dump(mode="json")`, in
field declaration order (`billing_snapshot.py:256-264`), each candidate
in `Candidate`'s order (`billing_snapshot.py:220-231`) and each `rates` in
`Rates`'s (`billing_snapshot.py:208-212`), candidates sorted by
`endpoint_id` (`billing_snapshot.py:471`), not in route order. One
candidate, as Python renders it (catalog prices, rendered with the
functions above):

```
{"async_eligible":false,"billing_snapshot":{"v":1,"kind":"credits_endpoint","candidates":[{"endpoint_id":"anthropic/claude-sonnet-4.5@anthropic/prepaid","provider":"anthropic","model_id":"anthropic/claude-sonnet-4.5","usage_type":"Credits","price_history_version":1,"rates":{"input_micro_per_million":3165000,"cached_input_micro_per_million":316500,"cache_creation_micro_per_million":3956250,"output_micro_per_million":15825000},"tiers":[{"max_prompt_tokens":null,"rates":{"input_micro_per_million":3165000,"cached_input_micro_per_million":316500,"cache_creation_micro_per_million":3956250,"output_micro_per_million":15825000}}],"request_fee_micro":0,"rounding":"half_up_per_million","prompt_convention":"excludes_cache","output_convention":"includes_reasoning"}],"minimum_charge":"one_micro_if_positive","charge_cap":null,"tier_basis":"total_prompt","tier_boundary":"inclusive","tier_fallback":"last_tier"},"billing_snapshot_hash":"8f702eba68248339abd1190bc3053a55b79247e7ec7ccb8a95d7212d961193f0"}
```

**`billing_snapshot_hash`** is the sha256 hex of `canonical_bytes`:
`json.dumps(model_dump, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False)` (`billing_snapshot.py:361-370`),
keys sorted at every depth. So the 200 carries the snapshot in
declaration order and the hash of its sorted-key encoding; Go needs both
encoders (G1's ordered and canonical `pyjson` encoders, `pyjson/encode.go:129`,
are exactly these). The DTO holds only int64 integers, ASCII identifiers,
arrays, fixed strings and `null`, no floats.

**`settlement_ticket`** (shape C only) is a compact JWS:
`b64url(canonical(header)) "." b64url(canonical(claims)) "." b64url(sig)`,
Ed25519 over the first two parts, header exactly
`{"alg":"EdDSA","kid":KID,"typ":"tr-async-settle-v1"}`
(`async_settle_ticket.py:103-109`, `TYP` at `21`), canonical being sorted
keys and compact ASCII (`detached_jws.py:60-62`). The 19 claims
(`TicketClaims`, `async_settle_ticket.py:27-46`) and where each comes
from (`async_settle.py:265-275`):

| Claim | Value |
|---|---|
| `authorization_id`, `generation_id` | the authorization's id and its uuid5 generation id (`storage_models.py:1440`) |
| `workspace_id`, `key_id` | the authorization's workspace and key hash |
| `invocation_nonce` | the body's; a missing nonce fails `TicketClaims` validation, and the `except` turns the whole result into shape A (`async_settle.py:281-282`) |
| `billing_authority` | `"local"` |
| `journal_region`, `epoch` | `runtime.effective_journal_region`, `runtime.epoch` |
| `snapshot_version`, `snapshot_hash` | `1`, the hash above |
| `route_type`, `streamed` | from the request's eligibility facts |
| `reservation_id` | `credit_reservation_id` |
| `settle_origin` | `"typed"` |
| `async_eligible` | the pilot and admission answer |
| `iss`, `aud` | the signer's configured issuer and audience |
| `iat`, `exp` | `int(time.time())` and `iat + async_settle_ticket_ttl_seconds` (at most 300, `async_settle_ticket.py:23`, `config.py:1021`) |

**`settlement_status_url`** is `"/v1/settlements/" +
quote(authorization_id + ".settle", safe="")` (`async_settle.py:280`).

### 2.3 Every branch

For a request that reaches `_gateway_authorize_response`, in Python's
order:

| Additions | Conditions |
|---|---|
| `{}` | the header list is anything but exactly `["async-v1"]` (`async_settle.py:290`; `gateway.py:3180`) |
| shape A | a replay; any undeclared top-level body member (`body.model_extra`); `route_type` absent or other than `chat.completions` and `responses`; `input_modalities` present at all, `null` included (`model_fields_set`); a `requested_parameters` entry outside the 15 ordinary parameters (`async_settle.py:297-310`) |
| shape A | `Eligibility` invalid, or an exclusion (`billing_snapshot.py:181-205`): untyped records, usage other than `Credits`, authority other than `local` (a federated workspace or key is `federated`), an unsupported route, a `service_tier` other than absent or `default`, or any nonzero or true markup, fee, custom or user model, tool, search, image or video cost, `partner` (a `model` not starting `openai/` or `anthropic/`, `async_settle.py:324`), liberty, native batch, fusion, polyphemus or private-tier fact. `streamed` itself is not an exclusion |
| shape A | the snapshot fails to build or validate: a raw price value that is not a strict non-negative int64 (`billing_snapshot.py:446-457`); a candidate's provider other than `openai` or `anthropic`, a model id not under its provider, a usage, version or convention the DTO refuses, a request fee; tiers out of order, an unbounded tier not last, more than 64 tiers; no candidate, more than 64, duplicates (`billing_snapshot.py:220-271`, `436-474`; raised inside `projection` and caught, `async_settle.py:235-240`) |
| shape A | no runtime or no signer, `epoch < 1`, an empty effective journal region, no `credit_reservation_id`, an authorization `settlement` other than `local`, or an authorization already settled (`async_settle.py:251-254`); and any exception in construction, hashing or projection (`async_settle.py:239-240`, `281-282`, `330-331`) |
| shape B | everything above passes and admission is off (`async_settle.py:255-258`), with or without an `invocation_nonce` |
| shape C | admission on and signing succeeds; invalid claims, a missing nonce among them, give shape A instead (`async_settle.py:259-282`) |

In the cohort the authorization guards always hold: Go's authorization
has a reservation, `settlement` `"local"` and `settled` false (the
contract's §5), and Go never answers a replay. Streams pass
(`body.stream`) before any of this.

### 2.4 What the shadow hooks add

`_v2_authorize_hook` (`gateway.py:3802-3815`) can extend the same
`async_additions` dict in place with `billing_shadow_policy` and
`billing_shadow_binding` (`async_settle_shadow_cohort.py:169-170`), and
then the authorization's `invocation_nonce`
(`async_settle_shadow_cohort_runtime.py:261-270`, `345`); the v1 shadow
adds `billing_shadow_binding` (`async_settle_shadow.py:139-168`). Neither
changes `async_eligible`, the snapshot or its hash. Both run only for a
workspace in `async_settle_shadow_workspaces` (`in_v2_cohort`,
`config.py:2503-2505`; `gateway.py:3195`).

The rule: a request whose workspace is in a shadow cohort passes. The edge
already does this for every request of such a workspace, async or not
(`policy.async_settle_shadow`, `fastpath/internal/edge/decide.go:88`,
`622`), and G15 keeps it, so the shapes above are the whole of what the
edge's async-v1 200 adds.

### 2.5 The rows do not change

The additions are computed after the insert, from the authorization in
memory, and write nothing (`projection`'s docstring, "AFTER the
successful durable authorization", `async_settle.py:234`). The
fingerprint reads the body only (the contract's §7). So the two rows of
the contract's §5 are the same bytes with or without the header, and the
shadow's row read-back applies to async-v1 requests unchanged.

## 3. The inputs, and whether Go can have them

### 3.1 Request facts

All from the body Go already parses strictly (the contract's §3, §8.2):
the header list (`gwauth.Headers.SettlementMode`, `request.go:26`,
filled at `decide.go:540`), replay (Go never answers one), undeclared
members (the strict parse knows the declared set of
`GatewayAuthorizeRequest`, `schemas.py:397-453`), `route_type`,
`input_modalities` presence, `requested_parameters`, `model`, `stream`
(always false in the cohort). G15b records the four Go does not keep yet.

### 3.2 The frozen billing snapshot

Python: `build_snapshot([effective_endpoint(e, at=effective_at) for e in
candidates], requested)` (`async_settle.py:326-327`), where `effective_at`
is the authorization's price instant `pricing_effective_at`
(`gateway.py:1498`, passed at `2357`). `build_snapshot` first checks every
raw price value of the effective endpoint as a strict non-negative int64
(`TypeAdapter(UInt)`, `billing_snapshot.py:446-457`: prompt, completion
and request prices, and each tier's bound, prompt, completion and cached
prompt prices), then builds each candidate from
`stage_d.endpoint_pricing_candidate` plus `provider`, `model_id`,
`usage_type` and the two conventions (`billing_snapshot.py:458-468`;
`stage_d.py:120-161`).

Go: the routing snapshot's `epochs[].price` is exactly
`stage_d.endpoint_pricing_candidate(effective_endpoint(e, at=t))` at the
epoch's start (`routing-snapshot-v1.md:152-158`;
`routing_snapshot.py:686-700`; `routingsnapshot.Epoch.Price`,
`fastpath/internal/routingsnapshot/snapshot.go:139-170`), and the price at
`t` is the last epoch with `starts_at <= t`. The compiler compares the
effective endpoint at each whole minute of each epoch with the epoch's
start and refuses the snapshot on any change (`routing_snapshot.py:655-684`);
that is a guard on the hourly assumption, not a check of every instant (a
price moving at 12:59:30 and back by 13:00 would not be seen), the same
assumption the estimate already rests on. Each endpoint carries
`provider`, `model_id` and `usage_type` (`snapshot.go:112-127`). The
evaluator reads that price at the request's one clock reading (the
contract's decision 15), which is the edge's `pricing_effective_at`.

**The lost fact.** `endpoint_pricing_candidate` converts with `int(...)`
(`stage_d.py:129-137`, `159`), so a malformed catalog value is coerced
before publication: a `request_price_microdollars` of `0.5` is published as
`0` (`routing_snapshot.py:689`), while `build_snapshot` refuses the raw
float and Python answers shape A. Tier values coerce the same way. Go,
building from the published price, would answer shape B. That is a parity
defect, not a charging path (shape B carries `async_eligible: false`), but
the snapshot must carry the fact. The fix, in G15b's scope (Python half in
P5, Go half in G15b):

- factor `build_snapshot`'s raw checks (`billing_snapshot.py:446-457`)
  into one function, `raw_prices_valid(endpoint) -> bool`, that
  `build_snapshot` calls (refusing as today) and the compiler calls on
  the same effective endpoint it prices each epoch from;
- publish it per epoch as `billing_valid` (bool) beside `price`;
- Go answers shape A for a request any of whose candidates has
  `billing_valid` false at the price instant, exactly where Python's
  `build_snapshot` raises.

With that fact every monetary input is present: base and tier input,
output, cache-read and cache-creation rates, tier bounds, request fee,
version and rounding; provider, model and usage; the conventions follow
from the provider. What Go must still check itself (G15b), each failure
being shape A: the provider set, `model_id` under `provider + "/"`, a zero
request fee, the prompt convention by provider, tier order with an
unbounded tier only last, at most 64 tiers and 64 candidates,
`endpoint_id` matching the `Identity` pattern, every integer in 0 to
2^63-1, candidates unique, sorted by `endpoint_id`
(`billing_snapshot.py:39-41`, `220-271`). The ids are ASCII by that
pattern, so Go's byte-order sort equals Python's.

### 3.3 Eligibility facts

`Eligibility` (`async_settle.py:311-325`; fields at
`billing_snapshot.py:149-178`, exclusions at `181-199`), for the cohort:

| Fact | Python's source | In the cohort |
|---|---|---|
| `typed` | a typed billing store and `request_record_write_mode == "typed"` (`gateway.py:3182-3185`) | process setting: published (§3.4) |
| `usage_type` | the authorization's | `Credits` |
| `authority` | `"federated"` for a federated workspace or key, else the authorization's `settlement` | `local` (federated passes, the contract's §3) |
| `route_type`, `streamed` | body | from the body; streams pass |
| `service_tier` | body | none (`body.service_tier` passes) |
| `app_markup`, `custom_markup` | the authorization's basis points | 0 |
| `receipt_fee` | `max(inference_receipt, receipt_fee_basis_points)` | 0 (`body.priced_extra` passes) |
| `custom_model`, `user_model` | authorization ids | false (`body.custom_model` passes) |
| `tool_cost` | `additional_cost_reservation_microdollars` nonzero | false |
| `native_batch` | the authorization's | false |
| `partner` | `model` not under `openai/` or `anthropic/` | from the body |
| `request_fee`, `search_cost`, `image_cost`, `video_cost`, `liberty`, `fusion`, `polyphemus`, `private_tier_basis` | defaults | false or 0 |

Every fact but `typed` comes from the body or the authorization row Go
already composes.

### 3.4 The runtime: signer, epoch, journal region

`Runtime` (`async_settle.py:178-190`) is built once per process at app
construction (`main.py:320-323`, `load_runtime`, `async_settle.py:193-228`):

- **signer**: an Ed25519 key read from `async_settle_ticket_private_key_file`
  with kid, issuer and audience from settings, refused if it equals the
  speculation shadow key (`async_settle.py:195-216`). Production mounts
  it from Secret Manager with kid `tr-async-settle-2026-10a`, issuer
  `https://api.trustedrouter.com`, audience `router-settlement`
  (`scripts/deploy/rollout.sh:753-756`). A failed load leaves `None`,
  which makes every answer shape A.
- **epoch**: `async_settle_authority_epoch`, an operator pin, 0 meaning
  unprovisioned (`config.py:1022-1024`); production pins 1
  (`rollout.sh:757`).
- **effective journal region**: `primary_region`
  (`async_settle.py:227-228`, `186-190`).

All three change only with a deploy. The edge policy (the contract's §4)
can carry what the edge needs. P5 publishes one object, `async_settle`,
of four booleans, nothing more:

| Field | Value |
|---|---|
| `admission_enabled` | `settings.async_settle_admission_enabled` (`config.py:1530-1533`) |
| `ticket_authority` | `runtime.signer is not None and runtime.epoch >= 1 and runtime.effective_journal_region != ""`, the publishing process's (`async_settle.py:251-252`) |
| `typed_records` | the `typed` expression of `gateway.py:3182-3185` |
| `edge_decide` | a new setting, `fastpath_edge_decide_async`, default false, rendered by `rollout.sh` as a literal `false` until I4 (§6.2) |

The publisher is installed before the runtime (`main.py:313-323`), so P5
reads `app.state.async_settle` when it signs, not when it installs, and
P5 extends Python's own strict policy schema (`EdgePolicyV1`,
`src/trusted_router/edge_policy.py:91`) with the member. Option (b)
would add the epoch, the journal region, the pilot set, the cap and the
TTL (§4).

### 3.5 Admission

`runtime.admission.eligible(workspace, cap)` (`async_settle.py:64-106`)
answers true only if, within the last 5 s (`CACHE_SECONDS`, `35`):

- one strong read of the workspace's pending and dead settle-outbox
  exposure (a `SUM` over at most 1,000 rows) and its shard-0 credit row's
  `trust_tier`, unlatched and unpaused
  (`storage_gcp_async_admission.py:71-104`), with the exposure at most the
  pilot cap or the tier's cap (`TIER_CAPS`, `async_settle.py:36`); and
- the fleet drain health record (`tr_entities` kind
  `settle_drain_control`, id `fleet-v1`,
  `storage_gcp_async_admission.py:107`, `136`), read at most once a second,
  complete, observed and heartbeated less than 5 s ago by the wall clock,
  p95 drain age at most 5 s (`decode_health`, `async_settle.py:166-175`).

Plus `async_settle_pilot_allows(workspace)` (`config.py:1527-1528`).

Could the edge answer it? Not from what it holds: its credit cache has the
tier and pause causes but a 15 s maximum age, three times the bound; the
outbox exposure and the health record are reads it does not make. It
would need two new reads at Python's cadence (per decided workspace every
5 s, and the health row every second per node), which is option (b)'s
cost.

Is `async_eligible: false` always safe? In money, yes. The enclave binds
async settlement only when `async_eligible` is true and the ticket
verifies (`bindAsyncAuthorization`, `enclave-go/internal/trustedrouter/async_settlement.go:83-141`;
it returns at `84-86` before parsing the snapshot at `90`). An unbound
authorization makes `tryAsyncSettlement` return `(nil, nil)` at once
(`async_settlement.go:175-178`), and settle continues to the ordinary
synchronous settle (`client.go:1240`), which is what every async-v1
request does today. A shadow-mode enclave (`TR_ASYNC_SETTLE_SHADOW=on`,
which sends the header on every chat and responses authorize,
`client.go:1007-1011`) rejects an answer without a binding and settles
synchronously (`async_settle_shadow.go:187-204`). And the settler re-checks
the pilot set, the claim and admission itself at settle
(`async_settle_handler.py:280-287`), so an authorize-time answer is
advisory: a stale `true` costs a `sync_required` and the enclave's
snapshot-sync fallback (`async_settlement.go:237-243`, `379-381`), a stale
`false` costs only the async benefit. What `false` gives up is product,
not money: the caller settles synchronously.

### 3.6 How stale the edge's view of admission can be

Each Python process publishes its own settings and runtime (§3.4), signs
a body at most every 15 s, and the edge keeps the newest `issued_at` it has
verified, for at most 60 s (`edge_policy.py:57-58`; `fastpath/internal/edge/policy.go:47`,
`175-190`, `231`). The edge picks by issuance time, not by configuration
generation. So the true property is:

- **Outside a Python rollout**, every instance publishes the same
  `admission_enabled`, and the edge's view is at most 60 s old (15 s
  nominally; a policy past `not_after` makes every request pass).
- **During a Python rollout that changes admission**, old and new
  revisions both publish fresh bodies, and the edge can hold either. For
  the rollout's whole duration plus 60 s, the edge's answer can disagree
  with some serving instances. Turning admission on: an old instance
  signs a fresh off-policy, the edge answers `async_eligible: false` to a
  request a new instance would have ticketed. Turning it off: the edge
  can hold an old instance's fresh on-policy and pass a request, which
  Python then answers (with a ticket if an old instance takes it, without
  one if a new instance does): Python answering, not the edge.

The operator rule: **the disagreement is a missed optimisation only,
never money, and needs no interlock.** The only answer the edge gives on
an off-policy is shape A or B, both `async_eligible: false`, which binds
nothing and settles through the synchronous path every async-v1 request
uses today (§3.5); a request the edge passes is answered by Python as
before. A rollout costs some requests their async settlement for its
duration, nothing else. For measurement only, a Python rollout that
changes admission during the I3 soak restarts the soak's sample (§6.2),
because it mixes shape C into the comparison (`async_python_admitted`).

## 4. The options

### 4.1 (a) Decide, answer without a ticket (recommended, as G15)

The edge composes Python's admission-off answer: shape B when every §2.3
check passes and the policy says `ticket_authority` and `typed_records`,
else shape A. When the policy says `admission_enabled`, the request passes
(`policy.async_admission`) so Python signs.

Legal: shapes A and B are Python's own answers to valid async-v1 requests
(§2.2, §2.3), and with admission off they are the only answers Python
gives (§5). The enclave handles both as sync (§3.5).

Money risk: none new. The enclave never reads `billing_snapshot` unless
`async_eligible` is true (`async_settlement.go:84-86`; it parses the
snapshot only after that, at `90`), so a wrong snapshot byte from Go
cannot move money while admission is off; it is a parity defect, which the
shadow counts and the gate refuses (§4.4). The rows are unchanged (§2.5).

Trust risk: none new. No key at the edge, no new reads.

Limit: once admission is on, async-v1 requests pass again, and if
async-v1 is then the default, I2's coverage falls back to the sync
minority. That is the trigger for (b).

A variant, rejected: answer shape A even with admission on. Legal (§2.3)
and safe in money (§3.5), but it silently demotes every edge-decided
caller to synchronous settlement, which is the opposite of the rollout's
purpose, and the shadow could not tell a correct demotion from a wrong
one.

### 4.2 (b) The edge signs tickets with its own key (G16, later)

What it takes:

- **The settler's trust.** The settler verifies against exactly one
  key, its own signer (`[runtime.signer.trusted]`,
  `async_settle_handler.py:100-102` and `279`), and binds `iss` to that
  key's (`async_settle_handler.py:276-278`). It needs a keyring: several
  `TrustedKey`s by kid, each with its issuer, `verify` and `verify_ticket`
  already taking a sequence (`async_settle_ticket.py:58-91`,
  `detached_jws.py:138-152`). The enclave already holds a keyring,
  `{"kid":"issuer~base64url_key"}` in `TR_ASYNC_SETTLE_TICKET_PUBLIC_KEYS`
  (`async_settlement.go:446-505`), and binds `claims.Iss` to the kid's
  issuer (`async_settlement.go:107`).
- **The edge's signer.** Its own Ed25519 key, its own kid and issuer, a
  Secret Manager mount on the fastpath nodes, never the routing-snapshot
  key (a separate purpose, as `load_runtime` refuses sharing,
  `async_settle.py:198-210`).
- **Authority facts.** Epoch and journal region must equal the settler's
  own at verify time (`async_settle_handler.py:276-279`): published in the
  policy. The pilot set, cap and TTL: published.
- **Admission.** The two reads of §3.5 at the edge, within 5 s.

Risks:

- **Money.** A ticket binds the snapshot hash, so it binds the prices an
  async settle charges for that reservation (`async_settle_handler.py:112`,
  `118-133`; the settler never consults the catalog). A holder of the
  edge's key could therefore settle any open reservation at prices of its
  choosing. The nodes already insert authorization and reservation rows,
  but a synchronous settle prices from Python's catalog, not from anything
  the edge wrote, so this is a strictly new power and the edge joins the
  billing trust boundary.
- **Rotation.** Add the new kid to the settler's keyring and every
  enclave's before the edge signs with it. Retire a kid only after every
  reservation it signed for is terminal (7,200 s TTL plus the reaper), not
  after the ticket TTL: the enclave's snapshot-sync fallback presents the
  same ticket after it expires (`verify_lookup_ticket`,
  `async_settle_ticket.py:76-91`, used for every path at
  `async_settle_handler.py:232-235`), and a ticket the settler no longer
  verifies is a 401, which the enclave treats as "snapshot sync rejected",
  not as a fallback to the legacy settle (`async_settlement.go:268-283`).
- **Revocation.** Dropping the kid from the settler's keyring revokes at
  once and fails every in-flight async settle signed with it (above). The
  enclave keyring changes only with an enclave deploy.
- **Rollout skew.** §3.6's disagreement would then cut both ways: an
  edge holding an on-policy would ticket requests an old instance treats
  as off. G16 must settle that (a configuration generation in the policy,
  or tickets only after every publisher agrees).

### 4.3 (c) Python issues tickets for edge-decided authorizations afterwards

The enclave binds async settlement only from the authorize 200
(`client.go:1021`, `async_settlement.go:83-141`). A ticket issued later
needs either a new enclave call to fetch it (a round trip to Python per
request, the latency E2 removes) or a ticketless settle path that trusts
the authorization row and recomputes the snapshot from the catalog at the
row's `created_at` (a new trust model and an enclave change). Without
either, (c) is (a). Rejected.

### 4.4 What the shadow compares

The full shadow runs only for a request the edge passes on: an answered
one returns before it (`fastpath/internal/edge/edge.go:450-455`). So
async-v1 requests are compared while they pass, which is why G15c lands
with async deciding off (§6.2): an exactly-one `async-v1` request is then
composed in full and passes after the compose with `async.off`, a
decision gate like the decide row's, and the shadow compares it:

- **`agree_full`** additionally requires the async members byte for byte
  in their position (between `stage_d` and `tags`). Shapes A and B have
  no nondeterministic member and do not depend on the ids, so no
  restaging is needed for them.
- **`differ.async_eligible`**, **`differ.billing_snapshot`** (the snapshot
  bytes; Go's and Python's price instants falling in different epochs is
  explained the way `differ.estimate` is, the contract's §11),
  **`differ.billing_snapshot_hash`** (snapshot bytes equal, hash not: an
  encoder defect, critical), **`differ.async_shape`** (Python's shape is
  not Go's: A against B is a request, catalog or runtime fact
  disagreeing). Python answering shape C means Python's admission was on
  while the edge's policy said off (§3.6): counted
  `async_python_admitted` and not compared.
- **Affinity requests.** An async-v1 request naming a
  `cache_affinity_key` is compared like any other (the contract's §11):
  `agree_full`, or `differ`, a warm re-rank of Go's cold order named
  `differ.affinity_warm` (`shadow_full.go`, `compareAnswer`). Affinity can change the
  candidate set, not only its order: with `allow_fallbacks` false Python
  re-ranks before keeping one endpoint (`authorize_candidates.py:415-422`),
  and the warm comparator accepts that substitution
  (`fastpath/internal/edge/affinity.go:109-133`, `warmOf`). The billing
  snapshot is built from the candidates kept, so a warm answer's async
  members can differ from the cold ones in kind, not only in bytes: with
  `model` `openai/gpt-5-mini`, fallbacks off, the cold choice
  `openai/gpt-5-mini@openai/prepaid` (a valid billing candidate) and the
  remembered `openai/gpt-5-mini@azure/prepaid` (provider `azure`, refused
  by `Candidate`), Python's warm 200 carries shape A where the cold one
  carries B. So the shadow compares an affinity answer's async members
  against Go's async additions rebuilt from the candidates the comparison
  used: the cold candidates for `agree_full`, `warmOf`'s candidates for
  `differ.affinity_warm` (the same restaging that comparison already does
  for the rest of the 200), and the cold candidates for any other `differ`.
- **The rows:** compared as for any request (§2.5).
- **Counts** (G15c), each a map on the shadow's count line, keyed so that
  every I3 figure is one printed number. The population `pop` is `plain`
  (no `cache_affinity_key`) or `affinity`:
  - `async_seen.<pop>`: every followed request whose header list is
    exactly `["async-v1"]`, counted as soon as the strict parse has read
    the body, whatever reason it later passes with or whether Python's
    answer is read; `async_seen.unparsed` for one whose body the edge did
    not read (a boot failure, or a body the parse refuses before it knows
    the affinity key).
  - `async_compared.<pop>.agree` and `async_compared.<pop>.differ`: an
    async-v1 request that Go composed and whose Python 200 was read and
    compared, by whether the async members alone matched (rebuilt as above
    for `affinity`), independent of the whole 200's class. A request
    unread, refused, stale or passed before the compose is in neither.
  - `async_fields.<pop>.<field>`: each async member that differed
    (`differ.async_eligible`, `differ.billing_snapshot`,
    `differ.billing_snapshot_hash`, `differ.async_shape`), for both
    populations, an affinity `differ` included.
  - `async_shape.<pop>.<shape>`: Go's composed shape (`snapshot`,
    `false_only.<reason>`), and `async_python_admitted.<pop>` for a
    Python shape C (not compared).

For shape C, when (b) exists, the rules G16 must keep: decode both
tickets; the header equal but `kid`; the claims equal but `iat`, `exp`,
`iss` and `async_eligible`, with `exp - iat` equal to the published TTL,
`iat` within the request's arrival and its answer (a second's skew either
way), `iss` the signing key's; each signature verified with its own key;
`async_eligible` compared as an admission answer, its disagreements
classed by the admission inputs' ages, not as `differ`;
`settlement_status_url` equal after restaging under Python's id. `iat`,
`exp` and the signature are nondeterministic and never compared as bytes.

## 5. Measurements (production, read-only)

From the edge's own count lines (`fastpath edge decide shadow:` in
`fastpath_journal`), both nodes, from 2026-10-26T15:17Z (the first line of
the release pinned with G13, which counts header passes of affinity
requests under `affinity`) to 18:00Z. Lines of the earlier release are not
used: it counted an affinity request that carried the header as
`body.cache_affinity`, which hid the header (the apparent jump at 15:17Z
is that accounting change; the fleet's async-v1 volume, the enclave's
async log events per hour, was flat).

Two settlement-header pass shares over that release's two maps, each over
one denominator. The second is of that release only: since the edge
decides affinity requests in the cold order (the contract's §1 item 5),
the shadow counts them in `classes`, so the first share alone covers
every request, and it is the one G15c's watch prints:

| Figure | Numerator | Denominator | Value |
|---|---|---|---|
| `settlement_header_pass_share` | `classes` entries ending `header.settlement_async` | `followed` (the watch's sum of `classes`, `cmd/fastpathwatch/edge.go:177`) | 2.4% |
| `affinity_settlement_header_pass_share` | `affinity` entries ending `header.settlement_async` | the sum of `affinity` | 45.2% |

Over both maps together, settlement-header passes were 14.7% of all
results, and 88% of them were affinity requests. They are the share of
results passed under that reason, and only that:

- any `X-TR-Settlement-Mode` value counts (`gwauth/cohort_request.go:114-117`),
  `sync` and repeated values included, which Python treats as no header;
- a request with the header that trips an earlier reason is counted under
  that reason (`body.stream`, `body.internal_model`, `boot.*`,
  `cohort_request.go:72-118`), and one whose Python answer was not read
  under `unread.*` (`shadow_full.go:490-502`; none in the window).

So they say neither how much traffic is async-v1 nor how much I2 leaves
out because of the header. G15c's `async_seen.<pop>` (§4.4) counts exact
`async-v1` before any exclusion, and the async-v1 figure is
`async_seen_share` = (`async_seen.plain` + `async_seen.affinity` +
`async_seen.unparsed`) / (the sum of `classes` + the sum of `affinity`).
The denominator is every result the shadow followed, both maps: not the
watch's `followed`, which summed `classes` only and so left out every
affinity result. Since the edge decides affinity requests in the cold
order (the contract's §1 item 5), the shadow keeps no `affinity` map and
`followed` is every result, so the denominator is `followed` and the
second share above is of the earlier releases only.

**Python's `async_eligible`:** the gateway logs no per-request async
outcome. It answers true for no request today, by construction and by
the logs:

- production pins `TR_ASYNC_SETTLE_ENABLED=false` and
  `TR_ASYNC_SETTLE_PROTECTION=false` (`rollout.sh:745-746`), so
  `async_settle_admission_enabled` is false and `snapshot_projection`
  returns before any admission answer (`async_settle.py:256-258`);
- no `async_drain.admission` line (logged on each health read, which
  happens only with admission on, `async_settle.py:71-82`, `221-225`) and
  no `metric=async_settle_` line (the settle handler's, reachable only
  with protection on, `routes/settlements.py:35`) in the 7 days to
  2026-10-26T18:00Z;
- every enclave async event in the 24 h to 2026-10-26T18:00Z was
  `shadow_rejected` `proof_signature` (a shadow-mode enclave given no
  binding, §3.5); none was `async_negotiated`.

So 0% true; Python's answers are shapes A and B, exactly what G15
reproduces.

## 6. Pull requests

### 6.1 The series

Same conventions as the contract's §11: each merges dark. Go learns to
read every new member before Python publishes it, because the parser
refuses a missing or an unknown member
(`routingsnapshot/snapshot.go:429-465`): an old node would keep its last
copy until `not_after` and then pass every request (`cache.policy` or
`cache.snapshot`). New goldens live in a new file that only the PRs that
read them consume; the existing `authorize_cases_v1.json` changes only in
G15c, together with the Go that reads it.

| PR | What | Files | Goldens | Gate to merge |
|---|---|---|---|---|
| G15a | The policy parser accepts an optional `async_settle` object (four bools) and the snapshot parser an optional per-epoch `billing_valid` (bool). Absent is unknown; `null`, a non-bool or any other unknown member still refuses | `fastpath/internal/routingsnapshot/edgepolicy.go`, `snapshot.go`, their tests | hand-built bodies with and without each member | tests; absent accepted, `null` and a fifth member refused |
| P5 | Python publishes both, after G15a is pinned on every node: `async_settle` (§3.4; `EdgePolicyV1` extended, the runtime read at signing, the `fastpath_edge_decide_async` setting, `false` in `rollout.sh`); `raw_prices_valid` factored out of `build_snapshot` and published per epoch as `billing_valid` (§3.2) | `src/trusted_router/edge_policy.py`, `services/edge_policy_publisher.py`, `billing_snapshot.py`, `routing_snapshot.py`, `config.py`, `scripts/deploy/rollout.sh`, their tests; the contract's §4 table and `routing-snapshot-v1.md` | `edge_policy_v1.{body,manifest}.json` and the routing snapshot body and manifest regenerated by their generators; a compiler test with a `0.5` request price and a float tier rate publishing `billing_valid: false` | the nodes' pinned release includes G15a (`fastpathwatch edge` shows no `cache.policy` or `cache.snapshot` rise after the deploy) |
| P6 | Python async goldens in a new file Go does not read yet: the generator runs Python's authorize with `X-TR-Settlement-Mode: async-v1` and a test runtime (a test ticket key, epoch 1, a journal region, admission off) | `scripts/fastpath/authorize_vectors.py`, `tests/test_fastpath_authorize_vectors.py` | `fastpath/testdata/authorize_async_cases_v1.json`, the same case shape as `authorize_cases_v1.json`: shape B (one and several candidates, a tiered price, candidates out of route order, no `invocation_nonce`, and a mixed-provider set, `openai/gpt-4.1@openai/prepaid` with `anthropic/claude-sonnet-4.5@anthropic/prepaid`, which `build_snapshot` accepts since each candidate is validated alone); shape A for each §2.3 branch the cohort can reach (an extra member, each non-chat route type, absent `route_type`, `input_modalities` present and `null`, a non-ordinary parameter, a partner model, a candidate set including `openai/gpt-5-mini@azure/prepaid`, whose provider `Candidate` refuses, `billing_valid` false, `ticket_authority` false, `typed_records` false); the affinity case of §4.4 (`openai/gpt-5-mini`, fallbacks off, a remembered `@azure/prepaid`): Python's warm 200 in shape A beside the cold one in shape B, seeded as the contract's §10.1 affinity cases seed a session; admission on as pass `policy.async_admission`; `edge_decide` false as pass `async.off`; the file's header carries Python's ordinary-parameter set | the generator's own test; every expected 200 from Python's real `_gateway_authorize_response` |
| G15b | `gwauth`: the request records undeclared members, `input_modalities` presence, `requested_parameters` and the exact header; `async.go` builds the billing snapshot from the composed candidates' epoch prices and `billing_valid`, validates it as §3.2, hashes it with the canonical encoder and returns shape A (with its reason) or B; the renderer splices it after `stage_d` | `fastpath/internal/gwauth/request.go`, `async.go`, `async_test.go`, `response.go` | P6's file, compose only | every P6 case byte for byte through the compose; a Go test holds the ordinary set equal to P6's; fuzzing the builder against the cases' inputs |
| G15c | `edge`: an exactly-one `async-v1` request no longer passes at the request stage; `policy.async_admission` and `async.unpublished` (no `async_settle` or no `billing_valid`) sit with the keyless policy gates; `async.off` is a decision gate after the compose while `edge_decide` is false; the shadow compares the async members and counts them (§4.4); `fastpathwatch edge` prints `settlement_header_pass_share`, `async_seen_share` over `followed` (§5; affinity requests are counted in `classes`, the contract's §1 item 5), the daily rates of `async_compared` and `async_compared.plain`, and every §6.2 I3 figure as one number | `fastpath/internal/gwauth/cohort_request.go`, `fastpath/internal/edge/decide.go`, `shadow_full.go`, `fastpath/cmd/fastpathwatch/edge.go`, `docs/runbooks/fastpath-watch.md`; `scripts/fastpath/authorize_vectors.py` | `authorize_cases_v1.json`: its one exact-`async-v1` case (`pass:header.settlement_async`) is regenerated with header `sync`, still that pass, in this same PR; P6's file now also through `Decide` | the decide-path tests; G11's budget test unchanged (the snapshot is built in the compose, no I/O) |
| I3 | Re-pin the nodes to G15c with `edge_decide` false; soak (§6.2) | node attributes | | §6.2 |
| I4 | `fastpath_edge_decide_async=true` in `rollout.sh`, a Python deploy; the decide-path runbook gains the async rows | `scripts/deploy/rollout.sh`, `docs/runbooks/fastpath-edge-decide.md` | | §6.2's I3 gate |
| G16 | Design only: option (b), when the operator schedules admission on | a sibling design doc | | |

### 6.2 Activation, and the I2 gate text

**Until G15c is pinned**, I2's gate reads as it does, with one sentence
added to the contract's "I2 gates": requests carrying
`X-TR-Settlement-Mode` pass with `header.settlement_async` and are not
comparable; the I2 decision records the settlement-header pass share of
§5, `settlement_header_pass_share` (those passes in `classes` over
`followed`, affinity requests among them now that the shadow counts them
there), read from the shadow's count lines as §5 reads it until G15c's
watch prints it. It is a pass share under one reason, not the async-v1
share of traffic and not the share I2 does not cover (§5).

**The dark sequence.** G15c lands with async deciding off, and only a
published switch turns it on:

1. G15c merged; `edge_decide` is false everywhere (`rollout.sh`'s
   literal, P5). An exactly-one `async-v1` request is composed and passes
   with `async.off`, even for a workspace whose decide row is on, so I2's
   decided workspaces keep answering sync requests and keep passing async
   ones.
2. I3: re-pin the nodes to G15c (the contract's re-pin rule restarts the
   I2 sample, and this soak's). The shadow compares every async-v1
   request it follows.
3. The I3 gate, read with `fastpathwatch edge -since` the last node's
   restart onto G15c, over the same span and digest rules as the
   contract's I2 sample. Each line is one number the watch prints (§4.4):
   - `async_compared` (the sum of `async_compared.*.*`) at least 2,000;
   - `async_compared.plain` (`async_compared.plain.agree` plus
     `async_compared.plain.differ`) at least 250;
   - `async_compared.plain.agree` over `async_compared.plain`, and
     `async_compared.affinity.agree` over `async_compared.affinity`, each
     at least 99.5%, every difference explained;
   - `async_fields.*.differ.billing_snapshot_hash` zero, in both
     populations;
   - `async_fields.*.differ.billing_snapshot`, `...differ.async_eligible`
     and `...differ.async_shape` zero beyond the explained price-epoch
     boundary cases;
   - `async_python_admitted` (both populations) zero: admission off
     throughout; a Python rollout that changes admission restarts the
     sample (§3.6);
   - the cohort's own I2 bars still met over all the span's comparable
     requests, sync and async-v1 alike as the shadow counts them (the
     contract's "I2 gates": every bar but its 3,000-comparable minimum,
     which this gate's minimums replace).

   This gate is I2's too (decided 2026-10-27; the contract, "With G15c
   pinned, I3's gate is I2's"): the sync-only I2 sample did not accrue
   on traffic that is mostly async-v1 or from the other planes.

   Why 250 plain: the async members come from one builder over the
   composed candidates in both populations, so the 2,000 test the builder;
   the plain minimum tests what only plain requests exercise, the gates
   that decide them and the whole 200 around the members. Its
   sensitivity, not a confidence level: a defect that changes the async
   members of 1% of plain requests appears at least once in 250 plain
   comparisons with probability 91.9% (1 - 0.99^250), and the
   zero-unexplained-difference lines above fail the gate on its first
   occurrence; the 99.5% agreement bar alone (at least two differences in
   250) catches it with probability 71.4%. As an illustration only, not
   evidence of the future mix: if 12% of async comparisons were plain, as
   12% of §5's settlement-header passes were, 2,000 in all would bring
   about 240 plain, and 500 plain would need about 4,167 in all.

   Duration: projected, not assumed. After the first full day of I3 at
   the current edge weight, the I3 runbook entry reads two daily rates,
   `async_compared` per day and `async_compared.plain` per day, and
   projects two dates from them separately: the day the total reaches
   2,000 and the day the plain count reaches 250. The soak's projected end
   is the later of the two. A plain rate of zero has no finite completion
   date. A projected end more than 14 days after the re-pin, or no finite
   one, goes back to this design for review rather than lowering the
   bars. The §5 shares cannot stand in for these rates: they count passes,
   not requests Go would compose and compare (most followed results pass
   `evaluate.pass` before the compose, and the settlement-header passes
   include `sync`).
4. I2 and I4 together, once the gate above is green: first
   `fastpath_edge_decide_async=true` through a Python deploy, which
   decides nothing while no workspace's decide row is on; then the
   decide runbook's `decide on`, an operator-owned workspace first, then
   the pilot workspace, each answering both its sync and its async-v1
   requests from then on. The
   edge takes it from the first policy it verifies from a new instance;
   during the rollout some fresh policies still say off, which only
   passes (§3.6), and if every fetch fails the held copy expires within
   60 s and the edge passes everything. Decided async-v1 requests are no longer compared for decided
   workspaces (they are answered, §4.4); workspaces without a decide row
   keep feeding the comparison (`decide.off`).
5. The off switches: `fastpathctl decide off` for a workspace (seconds,
   the contract's decision 17), or `fastpath_edge_decide_async=false`
   (a Python deploy; the edge stops deciding async at its first verified
   policy from a new instance, or when its copy expires), or admission on (async-v1 passes
   `policy.async_admission`).

Turning admission on later makes async-v1 requests pass
`policy.async_admission` again, which the watch shows as `async.admission`
growing; I2 stays valid for what it decides.
