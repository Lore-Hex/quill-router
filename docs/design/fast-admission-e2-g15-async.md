# E2 G15: the edge decides async-v1 authorizes

Status: design. A sibling of
[`fast-admission-e2-decide.md`](fast-admission-e2-decide.md) (the E2
contract, "the contract" below), which today passes every authorize
carrying `X-TR-Settlement-Mode` to Python (`header.settlement_async`,
the contract's §3). This document says what Python answers differently
for such a request, what of it Go can reproduce, which way to go, and the
pull requests. It changes no code; G15a to G15d below do.

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
   through the ordinary synchronous path (§4 (a)). Nothing on the money
   path changes; the snapshot is derived from the routing snapshot's
   price epochs the edge already holds (§3.2).
3. When the edge policy says admission is on, an async-v1 request passes
   with `policy.async_admission`, so every ticket stays Python's.
4. Signing tickets at the edge (option (b)) is a follow-on design (G16),
   started when the operator schedules admission on; issuing tickets after
   the fact (option (c)) is rejected (§4).
5. The full shadow compares the added fields byte for byte (§4.4). Until
   G15c is pinned and soaked, I2 covers requests without the header only,
   and says so (§6.2).

## 2. What an async-v1 200 adds

### 2.1 Where

`_gateway_authorize_response` (`gateway.py:3127`) builds the additions
only when the header list is exactly `["async-v1"]`
(`gateway.py:3179-3188`; `async_settle.py:290-291` checks again), and
splices them into the 200 between `stage_d` and `tags`
(`gateway.py:3247-3249`):

```
... "native_batch_eligible", "stage_d", <async additions>, "tags", "custom_model", "route_candidates"
```

then `timing` last (the contract's §6). Any other header value (absent,
`sync`, two values) adds nothing: the 200 is the sync one. The additions
are rendered by the same pydantic-core path as the rest of the 200 (the
contract's §6): insertion order, compact, integers only (none of these
fields is a float).

### 2.2 The three shapes

`authorize_additions` (`async_settle.py:286-331`) and
`snapshot_projection` (`async_settle.py:243-283`) produce one of three
shapes. `result` starts as `{"async_eligible": False}` and is extended
with `dict.update`, so `async_eligible` keeps the first position even
when its value changes (`async_settle.py:247`, `257`, `277-280`).

| Shape | Members, in order | When |
|---|---|---|
| A, false only | `async_eligible: false` | any pre-check, exclusion, snapshot or authority failure (§2.3) |
| B, admission off | `async_eligible: false`, `billing_snapshot`, `billing_snapshot_hash` | eligible and authority present, `async_settle_admission_enabled` false (`async_settle.py:255-258`) |
| C, admission on | `async_eligible: <bool>`, `billing_snapshot`, `billing_snapshot_hash`, `settlement_ticket`, `settlement_status_url` | eligible, authority present, admission on (`async_settle.py:259-280`); `async_eligible` is the admission answer, and the ticket is signed even when it is false |

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
ensure_ascii=True, allow_nan=False)` (`billing_snapshot.py:361-370`). So
the 200 carries the snapshot in declaration order and the hash of its
sorted-key encoding; Go needs both encoders (G1's ordered and canonical
`pyjson` encoders are exactly these).

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
| `async_eligible` | the admission answer |
| `iss`, `aud` | the signer's configured issuer and audience |
| `iat`, `exp` | `int(time.time())` and `iat + async_settle_ticket_ttl_seconds` (at most 300, `async_settle_ticket.py:23`, `config.py:1021`) |

**`settlement_status_url`** is `"/v1/settlements/" +
quote(authorization_id + ".settle", safe="")` (`async_settle.py:280`).

### 2.3 Shape A, Python's own answer for valid requests

Python answers shape A, a 200 with no snapshot and no ticket, to many
valid async-v1 requests:

- a replay (`async_settle.py:297-298`);
- any undeclared top-level body member (`body.model_extra`, same line);
- `route_type` other than `chat.completions` or `responses`, including
  absent (same line);
- `input_modalities` present at all, even `null` (`model_fields_set`,
  `async_settle.py:301-302`);
- a `requested_parameters` entry outside the 15 ordinary parameters
  (`async_settle.py:304-310`);
- any eligibility exclusion (`billing_snapshot.py:181-205`), notably
  `partner`: a `model` not starting `openai/` or `anthropic/`
  (`async_settle.py:324`);
- a candidate the billing contract refuses: a provider other than
  `openai` or `anthropic`, a model id not under its provider, a request
  fee, tiers out of order, more than 64 candidates
  (`billing_snapshot.py:220-271`, raised inside `projection` and caught,
  `async_settle.py:235-240`);
- no ticket authority: no signer loaded, `epoch < 1`, an empty journal
  region (`async_settle.py:251-254`);
- with admission on, a missing `invocation_nonce` (above).

### 2.4 What the shadow hooks add

`_v2_authorize_hook` (`gateway.py:3802-3815`) can extend the same
`async_additions` dict in place with `billing_shadow_policy`,
`billing_shadow_binding` or `billing_shadow_exclusion`, and
`invocation_nonce` (`async_settle_shadow_cohort_runtime.py:261-270`,
`341-345`); the v1 shadow adds `billing_shadow_binding`
(`async_settle_shadow.py:139-168`). Both run only for a workspace in
`async_settle_shadow_workspaces` (`in_v2_cohort`, `config.py:2503-2505`;
`gateway.py:3195`), and the edge already passes every such workspace
(`policy.async_settle_shadow`, `fastpath/internal/edge/decide.go:88`,
`622`). For the cohort they add nothing, and G15 keeps that pass.

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
(`gateway.py:1498`, passed at `2357`) and each candidate is
`stage_d.endpoint_pricing_candidate` of the effective endpoint plus
`provider`, `model_id`, `usage_type` and the two conventions
(`billing_snapshot.py:458-468`; `stage_d.py:120-161`).

Go: the routing snapshot's `epochs[].price` is exactly
`stage_d.endpoint_pricing_candidate(effective_endpoint(e, at=t))` at the
epoch's start (`routing-snapshot-v1.md:152-158`;
`routing_snapshot.py:686-700`; `routingsnapshot.Epoch.Price`,
`fastpath/internal/routingsnapshot/snapshot.go:139-170`), the price at `t`
is the last epoch with `starts_at <= t`, and the compiler refuses a
snapshot in which any price moves inside an epoch
(`routing_snapshot.py:655-684`). Each endpoint carries `provider`,
`model_id` and `usage_type` (`snapshot.go:112-127`). The evaluator
already reads that price for the estimate at the request's one clock
reading (the contract's decision 15), which is the edge's
`pricing_effective_at`. So the billing snapshot is a pure function of the
candidates Go composes and data the v1 snapshot already carries; it needs
nothing from G14's extension (the parameter table and privacy tiers).

What Go must mirror exactly (G15b), each failure being shape A:
`Candidate.provider` in `{openai, anthropic}`, `model_id` starting with
`provider + "/"`, `request_fee_micro` zero, the prompt convention by
provider, tier boundaries strictly increasing with an unbounded tier only
last, at most 64 tiers and 64 candidates, `endpoint_id` matching the
`Identity` pattern, every integer in 0 to 2^63-1, and candidates unique and
sorted by `endpoint_id` (`billing_snapshot.py:39-41`, `220-271`). The ids
are ASCII by that pattern, so Go's byte order sort equals Python's.

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

All three change only with a deploy. The edge policy (the contract's §4:
signed, at most 60 s old, published by a Python instance) can carry them.
For G15 the edge needs only whether the publishing process would freeze a
snapshot, so P5 publishes three booleans, nothing more:

| Field | Value |
|---|---|
| `async_settle.admission_enabled` | `settings.async_settle_admission_enabled` (`config.py:1530-1533`) |
| `async_settle.ticket_authority` | `runtime.signer is not None and runtime.epoch >= 1 and runtime.effective_journal_region != ""`, the publishing process's (`async_settle.py:251-252`) |
| `async_settle.typed_records` | the `typed` expression of `gateway.py:3182-3185` |

The answering instance and the publishing instance can differ only if one
instance failed to load its key; the shadow sees that as
`differ.async_shape` (§4.4). Option (b) would add the epoch, the journal
region, the pilot set, the cap and the TTL (§4).

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
verifies (`bindAsyncAuthorization`, `enclave-go/internal/trustedrouter/async_settlement.go:83-141`,
the first return at `84-86`); an unbound authorization makes
`tryAsyncSettlement` return at once (`async_settlement.go:175-178`) and
the request settles through the ordinary synchronous settle, which is
what every async-v1 request does today. A shadow-mode enclave
(`TR_ASYNC_SETTLE_SHADOW=on`, which sends the header on every chat and
responses authorize, `client.go:1007-1011`) rejects an answer without a
binding and settles synchronously (`async_settle_shadow.go:187-204`). And
the settler re-checks the pilot set, the claim and admission itself at
settle (`async_settle_handler.py:280-287`), so an authorize-time answer
is advisory: a stale `true` costs a `sync_required` and the enclave's
snapshot-sync fallback (`async_settlement.go:237-243`, `379-381`), a
stale `false` costs only the async benefit. What `false` gives up is
product, not money: the caller settles synchronously.

## 4. The options

### 4.1 (a) Decide, answer without a ticket (recommended, as G15)

The edge composes Python's admission-off answer: shape B when every
§2.3 check passes and the policy says `ticket_authority` and
`typed_records`, else shape A. When the policy says `admission_enabled`,
the request passes (`policy.async_admission`) so Python signs.

Legal: shape A and shape B are Python's own answers to valid async-v1
requests (§2.2, §2.3), and with admission off they are the only answers
Python gives (§5). The enclave handles both as sync (§3.5).

Money risk: none new. The enclave never reads `billing_snapshot` unless
`async_eligible` is true (`async_settlement.go:84-86`; it parses the
snapshot only after that, at `90`),
so a wrong snapshot byte from Go cannot move money while admission is
off; it is a parity defect, which the shadow counts and the gate refuses
(§4.4). The rows are unchanged (§2.5).

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

### 4.3 (c) Python issues tickets for edge-decided authorizations afterwards

The enclave binds async settlement only from the authorize 200
(`client.go:1021`, `async_settlement.go:83-141`). A ticket issued later
needs either a new enclave call to fetch it (a round trip to Python per
request, the latency E2 removes) or a ticketless settle path that trusts
the authorization row and recomputes the snapshot from the catalog at the
row's `created_at` (a new trust model and an enclave change). Without
either, (c) is (a). Rejected.

### 4.4 What the shadow compares

G15c moves exactly-one `async-v1` from the request-stage pass to the
compose, so the full shadow (the contract's §11) composes and compares
these requests like any other:

- **`agree_full`** additionally requires the async members byte for byte
  in their position (between `stage_d` and `tags`). Shapes A and B have
  no nondeterministic member and do not depend on the ids, so no
  restaging is needed for them.
- **`differ.async_eligible`**, **`differ.billing_snapshot`** (the snapshot
  bytes; Go's and Python's price instants falling in different epochs is
  explained the way `differ.estimate` is, the contract's §11),
  **`differ.billing_snapshot_hash`** (snapshot bytes equal, hash not: an
  encoder defect, critical), **`differ.async_shape`** (Python's shape is
  not Go's: A against B is a request fact or a runtime fact disagreeing;
  Python answering shape C means Python's admission was on while the
  policy said off, counted `python_async_admitted` and not compared, a
  policy-staleness signal bounded by the policy's 60 s).
- The rows: compared as for any request (§2.5).
- **Counts:** each comparable async-v1 request is also counted by shape
  under `async=` on the count line (`async.snapshot`,
  `async.false_only.<reason>`, `async.admission`), so the watch prints the
  async share and its agreement apart.

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
`fastpath_journal`), 2026-10-25T18:00Z to 2026-10-26T18:00Z, both nodes,
both the cohort's classes and `affinity=`. The shadow counts a request
once, under the first pass reason it trips, and
`header.settlement_async` comes after `boot.*` and every other request
reason (`gwauth/cohort_request.go:72-118`), so a stream or an internal
model carrying the header is counted under those: the figures are lower
bounds.

| Window | Header over all followed | Over those whose body the edge read | Over those that reached the header check |
|---|---|---|---|
| whole 24 h | 8.6% | 10.6% | 14.3% |
| before 15:00Z | 0.2% | 0.3% | 0.5% |
| 15:00Z to 18:00Z | 14.5% | 16.1% | 19.6% |

The mix the edge follows changed at about 15:00Z; the fleet's async-v1
volume did not (the enclave's async log events per hour were flat), so
the later window is the current mix and matches the operator's estimate
of about 20%.

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
- every enclave async event in the 24 h was `shadow_rejected`
  `proof_signature` (a shadow-mode enclave given no binding, §3.5); none
  was `async_negotiated`.

So 0% true; Python's answers are shapes A and B, exactly what G15
reproduces.

## 6. Pull requests

### 6.1 The series

Same conventions as the contract's §11: each merges dark. G15a before P5
(the parser refuses a missing or an unknown member,
`routingsnapshot/snapshot.go:429-465`, so an old node would hold no
policy and pass every request with `cache.policy`).

| PR | What | Files | Goldens | Gate to merge |
|---|---|---|---|---|
| G15a | The policy parser accepts an optional `async_settle` object (three bools); absent is `nil`, and an async-v1 request then passes `policy.async_unpublished`. Any other unknown member still refuses | `fastpath/internal/routingsnapshot/edgepolicy.go`, `edgepolicy_test.go` | a hand-built body with and without the member | tests; the parser refuses a fourth member, a non-bool, a null |
| P5 | Python publishes `async_settle` (§3.4), after G15a is pinned on every node | `src/trusted_router/services/edge_policy_publisher.py`, `tests/test_edge_policy_publish.py`, the contract's §4 table | `fastpath/testdata/edge_policy_v1.{body,manifest}.json` regenerated by `scripts/fastpath/edge_policy_vectors.py` | the nodes' pinned release includes G15a (`fastpathwatch edge` shows no `cache.policy` rise after deploy) |
| P6 | Python authorize goldens with the header: the generator runs Python's authorize with `X-TR-Settlement-Mode: async-v1` and a test runtime (a test ticket key, epoch 1, a journal region, admission off) | `scripts/fastpath/authorize_vectors.py`, `tests/test_fastpath_authorize_vectors.py` | `fastpath/testdata/authorize_cases_v1.json`: shape B (one and several candidates, a tiered price, both providers, candidates out of route order); shape A for each §2.3 reason (an extra member, each non-chat route type, absent `route_type`, `input_modalities` present and null, a non-ordinary parameter, a partner model, a mixed-provider candidate set, `ticket_authority` false, `typed_records` false); admission on as pass `policy.async_admission`; two header values and `sync` as pass `header.settlement_async`; the header carries Python's ordinary-parameter set for a Go test to hold equal | the generator's own test; every case's expected bytes from Python's real `_gateway_authorize_response` |
| G15b | `gwauth`: the request records undeclared members, `input_modalities` presence, `requested_parameters` and the exact header; `async.go` builds the billing snapshot from the composed candidates' epoch prices, validates it as §3.2, hashes it with the canonical encoder and returns shape A (with its reason) or B; the renderer splices it after `stage_d` | `fastpath/internal/gwauth/request.go`, `async.go`, `async_test.go`, `response.go` | P6's cases | every P6 case byte for byte; a Go test holds the ordinary set equal to P6's; fuzzing the snapshot builder against the cases' inputs |
| G15c | `edge`: exactly one `async-v1` no longer passes at the request stage; the policy gates `policy.async_unpublished` and `policy.async_admission` sit with the keyless policy gates; the shadow compares the async members and counts `async=` (§4.4); `fastpathwatch edge` prints the async share, its agreement and `python_async_admitted` | `fastpath/internal/gwauth/cohort_request.go`, `fastpath/internal/edge/decide.go`, `shadow_full.go`, `fastpath/cmd/fastpathwatch`, `docs/runbooks/fastpath-watch.md` | P6's cases through `Decide` | the decide-path tests; G11's budget test unchanged (the snapshot is built in the compose, no I/O) |
| I3 | Re-pin the nodes to G15c; soak (§6.2) | node attributes | | §6.2 |
| G16 | Design only: option (b), when the operator schedules admission on | a sibling design doc | | |

### 6.2 The I2 gate text

Until G15c is pinned, I2's gate reads as it does, with one sentence
added to the contract's "I2 gates": requests carrying
`X-TR-Settlement-Mode` pass with `header.settlement_async` and are not
comparable; their share of `followed`
(`python_answered_go_passed.header.settlement_async` and
`passed.header.settlement_async`, cohort and `affinity=` together) is
read from the shadow's count lines as §5 reads it, until G15c's watch
prints it, and
the I2 decision records that share as the part of the edge's traffic
I2 does not cover.

After G15c is pinned, the sample restarts (a re-pin, the contract's
rule), and the async requests join it with their own bar inside the same
sample: at least 2,000 comparable async-v1 requests (about the async share
of the 10,000), `agree_full` at least 99.5% of them, zero
`differ.billing_snapshot_hash`, zero unexplained `differ.billing_snapshot`
and `differ.async_eligible`, and `python_async_admitted` zero (admission
off throughout the soak). Turning admission on later makes async-v1
requests pass `policy.async_admission` again, which the watch shows as the
async share moving back to passes; I2 stays valid for what it decides.
