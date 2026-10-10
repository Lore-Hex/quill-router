# PR F2 — async-settle shadow comparator

Status: **design appendix, round 3, 2026-10-06; dormant implementation required**.
This implements the measurement plan in
`docs/design/async-settle-outbox-v1.md:794` (§8) and its row F at line 837.
It does not edit that design or authorize enablement. All new names, limits,
algorithms and tests below are proposals, not claims of shipped behavior.

Source pins inspected read-only:

| Tree | Revision / citation convention |
|---|---|
| Router, this worktree | Round-3 read at `f429f37054ecd77fb98521ac482e0d868667c8f2`; round-1 executable baseline `f83bbaacb3e91271f7bac9ba26d826532f10962f`; paths below are repository-relative, including `src/trusted_router/` |
| Enclave PR E worktree | Round-4 HEAD `80de3a50f2a50b2cb5f8d35a8ed1ee42a1c0a06f`, rechecked in round 3; final citation audit in round 2. **E/** means `/Users/jperla/josh/repos/tr/wt/pr-e/`, not a router path. The local `docs/validation/` note is untracked; code citations take precedence. |
| Design and appendices read | `docs/design/async-settle-outbox-v1.md:56`, `:93`, `:693`, `:794`, `:828`; PR C compatibility `:909`, PR D implementation `:998`, PR D measurement plan `:1160` |

## 1. Invariants and numbered decisions

Seven consecutive days with **async admission disabled**. Sync books; shadow
never inserts a second pending intent or calls finalize. Preserve ordinary sync
outbox behavior, including its durable-but-not-yet-booked outcome. The latter
exists at `src/trusted_router/routes/internal/gateway.py:4133`; a successful
HTTP response alone is not a booked-amount oracle.

1. **D-S1 — workspace opt-in.** Add `async_settle_shadow_workspaces: str = ""`,
   parsed once into an immutable set of comma-separated, trimmed, nonempty
   workspace IDs; reject malformed IDs at startup, deduplicate, maximum 32.
   No wildcard, key-level alias or request-supplied override. Pin
   `TR_ASYNC_SETTLE_SHADOW_WORKSPACES=` in rollout beside the existing async
   pins (`scripts/deploy/rollout.sh:614`). Match the workspace from the existing
   server authorization, never from the envelope. Do not name the pilot;
   `docs/design/async-settle-outbox-v1.md:869` remains open.
2. **D-S2 — enclave switch and transport. Decision:** exact environment value
   `TR_ASYNC_SETTLE_SHADOW=on` enables shadow; every other value is off. Send
   `X-TR-Settlement-Mode: async-v1` only on authorize, and a bounded
   `X-TR-Settlement-Shadow` header on ordinary synchronous settle/refund. Reject the
   alternative top-level JSON field: `_Lenient` preserves extras, rather than
   discarding them (`src/trusted_router/schemas.py:51`), and settlement projects
   that model into side-effect data (`src/trusted_router/routes/internal/gateway.py:3470`,
   `:4365`). It would also violate the literal body-byte invariant.
3. **D-S3 — comparator isolation.** Preserve the existing parser, money
   transaction, amount, side effects and response. A post-outcome observer runs
   only for server-owned opted-in workspaces. It has no money-writing methods.
   It uses the billing DTO/evaluator APIs, not the async HTTP handler: the latter
   raises HTTP errors and can enqueue/apply (`src/trusted_router/services/async_settle_handler.py:83`,
   `:118`, `:197`, `:261`). Every shadow exception is contained and counted.
4. **D-S4 — evidence.** Use bounded `tr_entities` samples and day counter/control
   rows, only after the money outcome; never join the money transaction. There
   is already a `(kind,id)` table (`scripts/deploy/infra.sh:33`) and a fixed-key
   control-row pattern (`src/trusted_router/storage_gcp_async_admission.py:55`,
   `:86`). No new DDL or operational-analytics event kind.
5. **D-S5 — report authority.** Day-prefix evidence rows and completeness
   counters are the durable shadow record. Logs are diagnostic, not the report
   denominator. External proof artifacts remain necessary for the non-shadow
   exit gates (§6).
6. **D-S6 — preserve PR B. Decision:** retain snapshot production for an exact
   header on a non-opted workspace, including when the shadow set is empty.
   Reject gating existing projection on `opted_in OR admission`: it changes
   frozen-main response bytes and breaks the explicit metadata assertion in
   `tests/test_async_settle_authorize_oracle.py:47` (especially line 70).
   **“ZERO” means zero additional shadow CPU/RPC/serialization there**, apart
   from the unavoidable constant-time flag/set guard. Absolute zero existing
   snapshot cost for fleet-wide header traffic and byte-identical PR B behavior
   cannot both hold. This is a conflict in D-S6, not a hidden oracle exception.

Shadow mode must suppress async negotiation locally even if both enclave
switches are accidentally on, and count configuration conflict; it must never
bind `Authorization.async`. Today's binding requires negotiation, eligibility,
snapshot hash, a verified ticket and identity matching
(`E/enclave-go/internal/trustedrouter/async_settlement.go:79`, `:83`). Stream
behavior is selected by that binding (`E/enclave-go/cmd/enclave/main.go:2127`,
`:2296`). Shadow must leave those branches on their legacy side.
Router binding issuance/collection additionally requires admission disabled.
If admission is enabled, do not attach a shadow binding or claim shadow coverage;
ordinary PR B admission behavior remains independently governed by its flags.

## 2. Authorize provenance and wire contract

### 2.1 What PR B actually supplies

The response builder calls `authorize_additions` for the exact negotiation
header (`src/trusted_router/routes/internal/gateway.py:2776`). The replay call
passes no new price time (`:1122`); replay/unknown parameters are excluded by
`src/trusted_router/services/async_settle.py:286`. Effective endpoints are
built at the authorize price time (`:313`).

`snapshot_projection` first requires an eligible context, valid runtime signer,
positive epoch, region, local unsettled authorization and reservation; only
then does admission-off return snapshot/hash with no ticket
(`src/trusted_router/services/async_settle.py:235`). It does **not** sign this
flag-off projection or persist its hash. Production rollout currently pins the
key path empty and epoch zero (`scripts/deploy/rollout.sh:616`); merely adding
workspace opt-in cannot produce this metadata under those pins.

**Decision:** add an opt-in-only `billing_shadow_binding` authorize response
field, a compact Ed25519 JWS with `typ=tr-async-settle-shadow-v1`, verification
purpose `async-settle-shadow`, and audience `router-shadow`. Reject an unsigned
echoed hash or a process-local hash cache: neither independently authenticates
authorize-time prices across instances/restarts. This is a necessary addition
to D-S2 to satisfy §8's **signed-snapshot inputs**; the existing flag-off path
alone is insufficient. It is not `settlement_ticket`, cannot be submitted to
async admission, and has no billing authority.

Legacy internal-token authentication verifies a shared credential
(`src/trusted_router/routes/internal/_shared.py:53`), not an attested signature
over this diagnostic envelope. A current-catalog rebuild cannot authenticate
historical S0 after a correction; keep the separate signed binding even with
the independent legacy S0 arithmetic oracle (§3.1; effective-time lookup at
`src/trusted_router/catalog.py:352`).

Use the already-loaded Ed25519 material through a distinct shadow signer and
distinct `TrustedKey` purpose/audience descriptor; do not invoke
`TicketSigner.sign`. The key descriptor is at
`src/trusted_router/detached_jws.py:27`; `verify()` at `:138` checks type at
`:148` and purpose at `:152`. `canonical()` at `:60` only serializes.
Ticket signing and validators have a different type, purpose and 300-second lifetime
(`src/trusted_router/async_settle_ticket.py:20`, `:58`, `:76`, `:103`). Test rejection
of a shadow binding by **both** ticket validators even with the same public
key. Pilot rollout must explicitly provision the existing separate async key,
issuer, audience, kid and positive epoch under reviewed config-as-code; keep
`TR_ASYNC_SETTLE_ENABLED=false`. Default fleet pins stay empty/zero/off. Shadow
verification keys stay available for its 48-hour observation lifetime; this is
not an extension of ticket validity. The enclave can retain the proof opaque
and hash-check the snapshot; the router must verify the proof independently.

The binding has exactly the following claims (also encoded in the fixture JWS):
authorization, generation, workspace, key, nonce, reservation, local authority,
region, epoch, route, streamed, typed origin, snapshot version/hash, issuer,
audience, `iat`, `exp`, and `async_eligible=false`. Require `exp-iat=172800`,
`iat <= settle_received_at < exp`, expected issuer/authority and positive epoch.
Identity strings obey native ID bounds (64 for authorization/workspace/key/
reservation/nonce; generation/region at most 128). It attests prices and context
only. For the day key, authorization IDs additionally require
`[A-Za-z0-9_-]{1,64}`; an existing legacy ID outside that alphabet is excluded
from shadow at authorize, never rewritten. Requested feature checks remain
server-owned; terminal observed facts
must also agree with the legacy settle fields. Missing signer/context is
`snapshot_unavailable`, never permission to invent an epoch or silently remove
the signature check.

The enclave decodes only the bounded proof claims needed to construct its
diagnostic terminal (notably region/epoch); they remain untrusted metadata
until the router verifies them. Copy authorization identities from the normal
authorize response and reject inconsistent claim copies locally. Do not call
the async binder or treat an opaque proof as an admission capability.

### 2.2 Header and strict parsing

```http
POST /internal/gateway/authorize
X-TR-Settlement-Mode: async-v1

POST /internal/gateway/settle
Content-Type: application/json
X-TR-Settlement-Shadow: <unpadded-base64url(UTF-8 shadow envelope JSON)>
```

The same diagnostic header may accompany `/internal/gateway/refund`; that
legacy sender has its own body/send path
(`E/enclave-go/internal/trustedrouter/client.go:1223`, `:1257`).
There is **no `X-TR-Settlement-Mode` on shadow settle/refund**, including no `sync`
value. Both `sync` and `async-v1` can select the strict async recovery handler
when protection is on (`src/trusted_router/routes/settlements.py:31`). The
ordinary settle body is built and marshalled exactly as today
(`E/enclave-go/internal/trustedrouter/client.go:1074`, `:1150`, `:1402`). Add
the shadow header at the HTTP send seam (`:1348`), using a distinct request-local
context value; never inherit the authorize negotiation context into settle.
Reuse the same frozen header on identical retries and the existing pinned
authority. Never change retries or the return value because shadow failed.

Bounds: one header occurrence, at most **12,288 ASCII bytes**; decoded JSON at
most **8,192 bytes**, nesting at most 16, proof at most 2,048 ASCII bytes,
inline snapshot canonical bytes at most **6,144**. No compression or split headers.
Check encoded length before allocation/decode; require canonical unpadded
base64url (round-trip equality), strict UTF-8 without BOM, exactly one JSON
object, no duplicate keys at any depth, trailing data, unknown fields,
non-finite numbers, floats/strings/bools for integers or surrogate characters.
Limit integer lexemes before conversion; counts/micro are checked nonnegative
int64. IDs use the DTO identity alphabet plus the tighter limits above;
endpoint/model IDs at most 128. Never trim a snapshot or its candidate list to
fit: choose hash-only transport below. Existing DTO parsers/canonicalization are
at `src/trusted_router/billing_snapshot.py:361`, `:399`, `:420`; add the outer
size/depth/numeric bounds explicitly rather than assuming those APIs supply
every outer bound.

**Transport decision:** full-snapshot and hash-only are both v1 modes. Use full
only when all three size bounds pass; otherwise omit `billing_snapshot` entirely
(not null) and send the same signed binding, raw usage, observed facts, Go
terminal/hash and diagnostic fields. All other keys and bounds are unchanged.
The enclave still retains and evaluates the **whole** authorize snapshot;
only the return header omits it. PR B already returns the projection from all
fallback candidates (`src/trusted_router/routes/internal/gateway.py:2782`);
the proposed omission does not change that authorize response or the legacy
settle/refund body. Do not suppress binding issuance merely because an inline
snapshot would exceed 6,144 bytes. No supported fallback traffic is removed
from the denominator.

Review measurements are 645 canonical bytes for `request_v1.json`, maxima
1,041/1,884/2,726 for one/two/three candidates, 3,563 for the named four-model
OpenAI fallback `gpt-6-astra,gpt-5.6-sol,gpt-6.1-sol,gpt-5.5`, and 28,821 for
all 43 supported chat candidates (measurement record:
`/tmp/f2a-r2-probes.txt:12`, `:13`, `:15`; review:
`/Users/jperla/.claude/tr-briefs/review-prf2a-2-findings.txt:19`). These are
snapshot bytes before proof, terminal and base64 overhead; source inputs are
`src/trusted_router/routing.py:947`,
`src/trusted_router/routes/internal/gateway.py:2782`,
`src/trusted_router/billing_snapshot.py:361`, and the literal source is
`tests/fixtures/async_settlement/request_v1.json:1`. F2b/F2c must reproduce
these cases against their pinned catalog. The measured four-candidate envelope
is 5,492 decoded / 7,323 encoded bytes with regenerated signature and terminal
(`/tmp/f2a-r2-probes.txt:15`): **3,563 ≤ 6,144**, **5,492 ≤ 8,192** and
**ceil(4×5,492/3)=7,323 ≤ 12,288**. With that envelope's non-snapshot overhead
of `5,492−3,563=1,929`, even a 6,144-byte snapshot gives `8,073` decoded and
`10,764` encoded bytes. This overhead is measured, not a universal bound;
larger identities/proofs must pass each bound independently or use hash-only.
An 8,192-byte decoded object needs at most `ceil(4×8,192/3)=10,923` unpadded
base64url bytes (`src/trusted_router/detached_jws.py:124`); 12,288 is the outer
rejection/ingress-test cap, not permission to exceed the decoded cap.

**Hash-only S0 recovery:** after signature/identity verification, the router
uses the exact captured catalog view and effective prices at
**authorization.created_at**, with the original candidate IDs and
authorize eligibility, using the same reconstruction mechanism as S1 in §3
step 7. Preserve the builder's canonical candidate ordering
(`src/trusted_router/billing_snapshot.py:471`). Persisted creation/candidate fields are at
`src/trusted_router/storage_models.py:825`, `:827`, `:830`; effective-time
projection and eligibility are at `src/trusted_router/services/async_settle.py:295`,
`:313`, `src/trusted_router/catalog.py:352`, and snapshot construction/hash at
`src/trusted_router/billing_snapshot.py:436`, `:369`. Capture those server-owned
inputs at the existing observation seam; never substitute a newly routed
candidate set or infer missing eligibility as ordinary. A date alone does not
recover a historical catalog revision: no archive is assumed. Proceed only if
**H(rebuilt S0) == verified binding.snapshot_hash**, then require the terminal's
same hash and run the unchanged full-snapshot comparator. Missing exact view,
candidate/eligibility context, a removed endpoint, or a nonmatching hash yields
**`unevaluable/snapshot_reconstruction_failed`**, with null P/L0/frozen deltas;
it blocks/resets coverage even when B is known. A corrected current catalog is
not substitute signed S0, and cannot turn this failure into an explained delta.
S1 retains its separate booking-view provenance requirement (§3.1).

Bound reconstruction/retention to at most 128 candidates and 65,536 canonical
snapshot bytes in either mode (proposed local-work limits around
`src/trusted_router/billing_snapshot.py:436`, `:361`); the measured 43/28,821
case fits. Beyond those limits, record `snapshot_size` at authorize, omit only
the new binding and retain the sticky coverage failure even without a terminal.
If even hash-only exceeds an outer/proof bound, preserve the legacy outcome,
count the corresponding size rejection and fail coverage. Never send a trimmed
snapshot, a partial candidate rebuild, or an unsigned replacement hash.

**Before the clock starts**, record the pilot workspaces' authorization
candidate-count and canonical-snapshot/decoded-envelope/encoded-header size
distributions, maxima, mode fractions and every failed reconstruction/size
case, with pinned catalog/build and measurement interval. These must measure
actual authorization shapes, not infer traffic frequency from the catalog
(`src/trusted_router/routes/internal/gateway.py:2782`; review requirement at
`/Users/jperla/.claude/tr-briefs/review-prf2a-2-findings.txt:31`). Reconcile all
otherwise-supported attempts under §4; no single-model restriction or removed
many-candidate denominator. Transport is decided here; pilot feasibility is
an explicit measured enablement/clock precondition (§6).

Known server configuration supplies **no application-set total-header limit**:
`src/trusted_router/serve.py:104` leaves the HTTP parser and h11 incomplete-event
size at dependency defaults; `Dockerfile:37` selects this runner. The locked
dependencies are uvicorn 0.46.0, h11 0.16.0 and httptools 0.7.1
(`uv.lock:5135`, `:1636`, `:1699`; standard extra at `:5147`). Inspection of those
local dependency sources found auto selection prefers httptools; the h11
fallback defaults to **16,384 bytes of incomplete-event buffering**, not a
guaranteed per-header or end-to-end total limit. No larger header bound is
justified by this setting. Before the clock starts, rollout must record the
actual parser and every proxy/internal hop's limits, and demonstrate acceptance
of **maximum ordinary auth headers + a 12,288-byte shadow value**, including
header framing/other ordinary request headers and fragmented delivery, in every
pilot region. Also send valid maximum-decoded envelopes in both modes end to
end; the larger outer-cap probe may be rejected by the shadow decoded-size
check, but must reach Python and leave legacy settlement unaffected (§2.2;
`src/trusted_router/serve.py:104`).
These are required measurements, not established ingress acceptance.

With opt-in empty or server workspace absent from the set, do not decode, hash,
copy, log or count the header. The existing internal authentication and legacy
validation still apply (`src/trusted_router/routes/internal/gateway.py:525`).
For opted-in traffic, invalid shadow input is a **shadow rejection**, never
an HTTP rejection. Count exactly one primary reason per attempt:
`header_duplicate`, `header_size`, `base64`, `json_encoding`, `json_duplicate`,
`json_shape`, `integer`, `proof_signature`, `proof_expired`, `hash`, `identity`,
`raw_usage`, `go_failure`, `snapshot_size`. Do not echo rejected values or
exception text into evidence/logs. Infrastructure may reject a maliciously
oversized total HTTP header before Python runs; that cannot be made invisible
by this parser. F2c must obey the bound, and rollout must test the actual
ingress limit with maximum ordinary auth headers included.

### 2.3 Cross-repo literal

The following JSON block, including two-space indentation and one final LF,
is the proposed **entire contents** of
`tests/fixtures/async_settlement/shadow_v1.json`. F2c copies those exact bytes
to `enclave-go/internal/trustedrouter/testdata/async_settlement/shadow_v1.json`.
This pass creates neither fixture. The test-only signing seed is 32 bytes of
`0x01` (its public key is derived from it in the fixture test). Verify at
`1791244801`. The signed token itself is elided below because secret scanners
flag compact JWS literals; F2b regenerates it deterministically (kid
`shadow-v1-fixture`, typ `tr-async-settle-shadow-v1`, the claims listed in §2.1 with
iat 1791244800 and exp 1791417600) and pins the exact file bytes by the SHA-256 below.

The complete decoded protected header and claims of the elided token (plain
JSON; sign the canonical serialization of these claims under the test seed to
reproduce the exact token):

```json
{
  "claims": {
    "async_eligible": false,
    "aud": "router-shadow",
    "authorization_id": "auth-v1",
    "billing_authority": "local",
    "epoch": 1,
    "exp": 1791417600,
    "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
    "iat": 1791244800,
    "invocation_nonce": "nonce-v1",
    "iss": "router-fixture",
    "journal_region": "us-central1",
    "key_id": "key-v1",
    "reservation_id": "res-v1",
    "route_type": "chat.completions",
    "settle_origin": "typed",
    "snapshot_hash": "cb8feaf08da381f0d356dcd8ed4c6577f1d44a130e7f3b8029647fa3814872b4",
    "snapshot_version": 1,
    "streamed": false,
    "workspace_id": "ws-v1"
  },
  "protected": {
    "alg": "EdDSA",
    "kid": "shadow-v1-fixture",
    "typ": "tr-async-settle-shadow-v1"
  }
}
```
The snapshot uses the existing builder literal, with 250,000/625,000 cache
rates (`docs/design/async-settle-outbox-v1.md:123`).

```json
{
  "v": 1,
  "billing_shadow_binding": "<compact Ed25519 JWS over the §2.1 claims; elided — regenerated by F2b>",
  "billing_snapshot": {
    "v": 1,
    "kind": "credits_endpoint",
    "candidates": [
      {
        "endpoint_id": "openai/billing-v1@openai/prepaid",
        "provider": "openai",
        "model_id": "openai/billing-v1",
        "usage_type": "Credits",
        "price_history_version": 1,
        "rates": {
          "input_micro_per_million": 500000,
          "cached_input_micro_per_million": 250000,
          "cache_creation_micro_per_million": 625000,
          "output_micro_per_million": 500000
        },
        "tiers": [],
        "request_fee_micro": 0,
        "rounding": "half_up_per_million",
        "prompt_convention": "includes_cache",
        "output_convention": "includes_reasoning"
      }
    ],
    "minimum_charge": "one_micro_if_positive",
    "charge_cap": null,
    "tier_basis": "total_prompt",
    "tier_boundary": "inclusive",
    "tier_fallback": "last_tier"
  },
  "raw_usage": {
    "input_tokens": 1,
    "output_tokens": 1,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "reasoning_tokens": 0
  },
  "observed": {},
  "terminal": {
    "authorization_id": "auth-v1",
    "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
    "workspace_id": "ws-v1",
    "key_id": "key-v1",
    "invocation_nonce": "nonce-v1",
    "billing_authority": "local",
    "journal_region": "us-central1",
    "epoch": 1,
    "snapshot_version": 1,
    "snapshot_hash": "cb8feaf08da381f0d356dcd8ed4c6577f1d44a130e7f3b8029647fa3814872b4",
    "route_type": "chat.completions",
    "streamed": false,
    "v": 1,
    "terminal_kind": "settle",
    "selected_endpoint": "openai/billing-v1@openai/prepaid",
    "usage": {
      "uncached_input_tokens": 1,
      "total_prompt_tokens": 1,
      "output_tokens": 1,
      "cache_read_tokens": 0,
      "cache_creation_tokens": 0,
      "reasoning_tokens": 0
    },
    "charge_micro": 2
  },
  "payload_hash": "f5a8699841e40582b2c8d49702092328ba9e63991166b95da60c90f7da5fdf32",
  "go_error": null,
  "go_evaluator": "billing-v1",
  "go_revision": "cf82c77b02a8c07191f71e954196ab90d35294d3",
  "handoff_prepare_us": 1000
}
```

Literal file SHA-256 (with the real token in place): `68568699b000210413a20916ffa1f56093ab0223bb6a65d935ae453a9d96a28a`. Canonical wire serialization
(sort keys, compact separators, no final LF) is 2576 decoded bytes /
3435 base64url bytes; fixture whitespace is not sent in the header.
Canonical terminal hash: `f5a8699841e40582b2c8d49702092328ba9e63991166b95da60c90f7da5fdf32`.
The literal's `go_revision` is a fixed historical test value, not a claim that
that revision implements shadow; production records the actual F2c build.

The full-mode wire outer keys are exactly those in the literal; hash-only has
exactly that key set minus `billing_snapshot` (§2.2; snapshot hash is part of
`src/trusted_router/billing_snapshot.py:316`). No separate mode flag or null
snapshot is accepted. `go_error=null` requires a valid terminal and payload
hash. The failure variant retains binding and the mode's snapshot presence,
uses `terminal=null`, `payload_hash=null`, and one bounded `go_error`
enum: `usage_missing`, `usage_estimated`, `unsupported_observed`,
`malformed_usage`, `arithmetic_overflow`, `evaluator_failed`. `raw_usage` may
be null only on failure; never forward malformed raw values as arbitrary JSON.
Do not suppress a Go failure and then report only successful envelopes.
`go_evaluator` is fixed `billing-v1`; `go_revision` is the attested build SHA
(40 lowercase hex), not client input. `handoff_prepare_us` measures terminal
usage availability to just before final header serialization (evaluation and
DTO preparation); it excludes its own serialization/send/reply time and cannot
measure the reply to the request carrying it. No estimated end-to-end timing.
Production populates observed `Eligibility` facts; `{}` in this fixture means
the DTO's ordinary defaults (`src/trusted_router/billing_snapshot.py:149`).

F2c must capture provider tier **before** `applyCacheUsage` sanitizes it
(`E/enclave-go/cmd/enclave/main.go:2639`, `:2659`), at every stream/nonstream
call site (`:1701`, `:1804`, `:2013`, `:2479`, `:2552`). Retain a request-local
diagnostic fact separate from `Usage` and the legacy body. The shadow
`observed.service_tier` is absent only for a confirmed absent provider tier,
`default` only for an actual default, and a fixed `unsupported` sentinel for
every other nonempty provider tier (including `standard`, `priority`, `flex`,
`batch`, `scale`, unknown or oversized strings). Do not retain arbitrary provider
strings. Missing provider usage is `usage_missing`, not confirmed absence.
This follows Python's absent/default-only rule
(`src/trusted_router/billing_snapshot.py:190`); widening that rule for aliases
requires a separate reviewed change. Keep requested eligibility separately and
require both requested and observed to pass. Sanitized `Usage.ServiceTier` is
insufficient (`E/enclave-go/internal/trustedrouter/async_settlement.go:153`).
Unsupported observed tiers produce the bounded `unsupported_observed` failure
variant even when the unchanged legacy body retains requested/default tier.

## 3. Comparator algorithm and classification

1. At successful authorize, count the opted-in attempt before cohort/header
   exclusions, using only already-available facts. Count replay separately;
   never rebuild a new shadow binding for an old authorization. Freeze the
   same snapshot returned by PR B, sign its binding after the hold commits,
   and add only the optional binding field. No new authorize RPC.
2. At legacy settle, use the authorization already read for the per-key gate
   (`src/trusted_router/routes/internal/gateway.py:3375`) to check opt-in.
   Record a monotonic receive time and capture detached, bounded pricing
   references/legacy count facts for observation, without passing them back
   into the pricing function. This requires only a read-only observation seam
   at price selection, not a second pricing decision or money-store callback.
   Execute the unchanged legacy entry once. After its outcome, acquire the
   local rate/queue permit before copying a bounded envelope into the worker;
   refusal only increments the drop counter.
   Refund has a separate seam: `/internal/gateway/refund` calls
   `_settle_gateway_authorization(..., success=False)` directly, bypassing that
   per-key wrapper (`src/trusted_router/routes/internal/gateway.py:2308`,
   `:2316`). Add the same detached observation capture at its existing
   authorization read (`:3424`) and post-outcome submission around the refund
   entry, retaining request header/context outside the money API. Do not reroute
   refund through the settle gate or add a pre-response lookup. Infer attempted
   kind from the actual server entry's `success` value, never the header; require
   terminal kind to agree. Off/nonmember refund has the same zero-work guard.
3. Preserve its result or original exception. After the booking outcome, submit
   an immutable observation to a bounded worker. Parsing, proof verification,
   evaluation, evidence I/O and their failures cannot alter the response.
   For a result claiming finalized, use one complete-key authorization read
   to confirm `settled`, outcome and `finalized_cost_microdollars`; replays
   compare the original winner. Missing/nonfinal/failed read means booked
   amount **null**, never the proposed `actual_cost`, envelope or pending intent
   amount. The current replay projection reads finalized cost
   (`src/trusted_router/routes/internal/gateway.py:4579`, `:4614`). A deferred
   legacy result is `booking_pending`, not exact; report later resolution only
   through an explicit bounded report-time point read, keeping the original
   observation and original comparison time. No shadow finalize or redelivery.
   Sample persistence requires an already-confirmed money commit/winner. If
   the original result and bounded point read cannot establish one, emit only
   `booking_pending`/`booking_unknown` attempt counters; there is no sample
   transaction racing an unknown money commit. A finalized response followed
   by a failed amount read can produce an unevaluable sample with null amount.
   For the GCP pilot, use a narrow strong read of `settled`,
   `finalization_outcome`, `finalized_cost_microdollars` from
   `tr_gateway_authorization WHERE authorization_id=@authorization_id`, with
   explicit LOW priority/deadline/no retries. These typed fields and key lookup
   already exist (`src/trusted_router/storage_gcp_request_records.py:212`). Do
   not call the generic store getter for this extra shadow read: its typed miss
   can fall back to another store (`src/trusted_router/storage_gcp.py:4651`).
4. Parse strictly; verify the separate proof and every identity against the
   persisted authorization and server request route. Bind generation via the
   deterministic ID, reservation, key, nonce, workspace, local authority,
   epoch and candidate membership. If `billing_snapshot` is absent, reconstruct
   S0 under §2.2 before evaluation; failure is
   `unevaluable/snapshot_reconstruction_failed`, never permission to evaluate
   S1 as S0 (`src/trusted_router/billing_snapshot.py:369`, `:436`). Check
   `H(snapshot) == proof.snapshot_hash == terminal.snapshot_hash` and recompute
   `payload_hash = H(terminal)`. A digest echoed twice is not provenance.
5. Map raw counts from **the unchanged parsed legacy body** (actual-input/output
   aliases win; absent optional cache/reasoning counts mean zero) and require
   equality with envelope raw counts. Those aliases are defined at
   `src/trusted_router/schemas.py:608`. Compare route, stream, selected endpoint,
   sanitized body service tier, estimated flag and observed additional costs too.
   Check provider-tier eligibility separately using the pre-sanitization fact
   in §2.3: it need not equal the sanitized legacy tier, and sanitization must
   never upgrade an unsupported observation to eligible. Unknown or
   missing final usage is excluded/unevaluable, not coerced into exact zero.
6. Run Python `evaluate(snapshot, selected_endpoint, raw, observed)`, capture
   normalized usage and its usage charge. Set `python_micro` to that charge for
   **settle**, to **0 for refund**, even with positive usage; apply the same
   kind selection in the Go builder and to rebuilt expected amounts. Never
   overwrite the received Go charge with zero to conceal an invalid refund.
   Compare all six counts with
   the Go terminal, then `validate_envelope(snapshot, terminal)`. Do not lose
   the independently computed Python amount when validation rejects the Go
   charge. `go_micro=terminal.charge_micro`; `frozen_micro=python_micro` only
   after a verified frozen snapshot evaluates. Existing implementations are
   `src/trusted_router/billing_snapshot.py:477`, `:539`,
   `src/trusted_router/services/async_settle_handler.py:122`, and
   `E/enclave-go/internal/trustedrouter/async_settlement.go:157`, `:163`.
   Legacy likewise zeroes refunds (`src/trusted_router/routes/internal/gateway.py:3631`).
   For the failure variant, first verify the proof/snapshot and use the legacy
   endpoint plus proof context; do not dereference a null terminal. Run Python
   if valid raw usage is available. Python success against a Go evaluator
   failure on the same eligible input is `evaluator_disagreement`; agreed
   missing/estimated/unsupported input is an explicit exclusion or unevaluable
   coverage outcome. No nullable terminal can become a zero charge.
7. Rebuild a diagnostic settle-time snapshot from the exact catalog view used
   by that booking, at **authorization.created_at**, with the original candidate
   IDs and eligibility. Preserve candidate ordering. Capture references at
   pricing selection, or prove catalog immutability for the duration; a later
   background read of a refreshed catalog cannot explain an earlier booking.
   Record both hashes and `rebuilt_micro`. If the legacy path used a Stage D
   pricing document, identify that price source instead. It already takes
   precedence (`src/trusted_router/routes/internal/gateway.py:3593`;
   `src/trusted_router/stage_d.py:25`). If reconstructing the exact view is
   impossible, set rebuild unknown, never manufacture an explanation.
8. Compute signed integer deltas and classify by the precedence below; retain
   all bounded secondary reasons. Peek at the separate observation cache (§5)
   under a nonblocking lock, without calling `eligible()` or initiating a refresh. Emit one logical
   evidence sample, and update attempt/outcome counters separately (§4).

### 3.1 Exact arithmetic for catalog explanations

Let `S0` be the signed authorize snapshot, `S1` the diagnostic rebuild of the
booking's price source, `u` identical raw usage, and `e` the selected endpoint.
Let `k` be the verified attempted terminal kind and define
`E_k(S,e,u)=E(S,e,u).charge_micro` for settle, **0** for refund, after successful
normalization/eligibility evaluation (`src/trusted_router/billing_snapshot.py:539`).
Let `P=E_k(S0,e,u)`, `G=go_micro`, `R=E_k(S1,e,u)`, `B=booked_micro`.
Store `python_minus_go=P-G`, `booked_minus_frozen=B-P`,
`rebuilt_minus_frozen=R-P`, `booked_minus_rebuilt=B-R` (null if undefined).
Book-to-attempt deltas are undefined when attempted kind differs from the durable
winner's polarity; retain both amounts and kinds, but set those deltas null.

The first reservation claimant wins settle/refund races
(`src/trusted_router/storage_gcp_counter_dml.py:753`); the durable authorization
exposes the winner, including zero-cost settle versus refund
(`src/trusted_router/routes/internal/gateway.py:4579`, `:4597`). For same-kind
attempts compare B normally. For opposite-kind attempts, still validate P/G and
usage, but classify `requires_review/winner_polarity`, a coverage gap, not an
arithmetic disagreement or catalog explanation. Do not recompute P for the
winner kind using the loser's usage, turn B into zero for a losing refund, or
overwrite the winner sample. Unknown winner remains `booking_unknown`. A valid
refund winner with positive usage is exact at `P=G=B=0`; refund cannot have a
nonzero catalog-price delta. Missing refund usage remains unevaluable.

For each of uncached input, cache read, cache creation, output, cost component
is `checked(checked(tokens*rate)+500000)//1000000`; checked sums then have a
one-micro minimum iff a positive-rate component has positive usage. Select
inclusive total-prompt tier, falling back to last tier. Reasoning is a subset
of output, never a fifth charge. This is the existing evaluator arithmetic
(`src/trusted_router/billing_snapshot.py:484`). No float tolerance or percentage
allowance is permitted.

Define **L0**, an independent legacy-side S0 oracle: copy the selected signed
S0 candidate's rates, tiers, request fee and rounding into the existing legacy
frozen-candidate pricing function
`src/trusted_router/stage_d.py:161`. Feed it the **legacy** normalized counts
from `src/trusted_router/services/settle_outbox_apply.py:101`, with raw output
and cache counts and no private tier override. Verify the provider/convention
mapping and six-count agreement first; reject unsupported mapping. This oracle
uses `src/trusted_router/money.py:57` for rounding and Stage D's independent
tier/minimum logic (`src/trusted_router/stage_d.py:183`, `:211`), not
`billing_snapshot.evaluate`, its normalized output, or its arithmetic helpers.
It is a post-outcome pure diagnostic, never a second booking. Select L0=0 for
refund only after validating inputs. Persist `legacy_frozen_micro=L0` and oracle
identity `stage_d_candidate_v1`; missing/unsupported oracle yields null plus
`requires_review/legacy_oracle_unavailable`. F2b must prove the adapter copies
every pricing field without live-catalog substitution and pin the independent
legacy functions in its oracle/mutation manifest.

`explained-by-catalog-change` requires **all**: valid signature/hash/identity,
identical raw and normalized usage across legacy/Python/Go, attempted and winner
kind both settle, **`L0=P=G`**, `B != P`,
`H(S0) != H(S1)`, a supported ordinary price-only difference with unchanged
candidate identity/conventions, proven legacy price source, and **`B=R`** exactly.
`B-P=R-P` is retained as a reported delta, not independent evidence: it follows
from B=R. If L0 is unavailable, the delta is `requires_review`, never clean;
if L0 differs from P, it is `evaluator_disagreement` even when P=G and B=R.
A changed hash alone is insufficient; changes to an unused
candidate cannot explain a selected endpoint's amount mismatch. Added/removed
candidates, feature/cohort changes and unverifiable rebuilds are not allowlisted.

Example fixture: no Stage D billing document; one input plus one output token,
no cache, S0 input/output rates 500,000/500,000 → `L0=P=G=1+1=2`.
Before settle, the applicable catalog history is corrected to
1,500,000/500,000 → `R=B=2+1=3`; the hashes differ and both deltas are `+1`.
This is explained. A price scheduled **after** authorization does not qualify:
legacy lookup passes `effective_at=authorization.created_at`
(`src/trusted_router/routes/internal/gateway.py:3625`, `:5268`;
`src/trusted_router/catalog.py:352`). With a frozen Stage D document still at
S0, legacy books 2 despite live catalog 3: classify exact, recording observed
catalog drift if available. If legacy books 4, neither example explains it.

| Primary classification (first applicable) | Reason / rule | Window effect |
|---|---|---|
| `hash` | Invalid proof signature, wrong purpose, inline snapshot or terminal/payload hash mismatch; excludes failed hash-only S0 reconstruction (§2.2; `src/trusted_router/billing_snapshot.py:369`) | Correctness mismatch; reset after fix |
| `identity` | Validly parsed but wrong authorization/owner/nonce/reservation/endpoint/route/stream/authority | Correctness mismatch; reset after fix |
| `normalization` | Same eligible request yields different raw-body mapping or normalized counts; includes legacy clamping versus strict usage rejection | Correctness mismatch; reset after fix |
| `evaluator_disagreement` | Evaluable identical inputs, `P != G` or known `L0 != P`; same-kind unexplained `B != P` with complete oracle/rebuild evidence | Correctness mismatch; reset after fix |
| `requires_review` | Opposite winner polarity or an apparent catalog delta lacking the independent S0 oracle | Coverage failure; cannot count as clean or explained |
| `unevaluable` | Parse error, expired proof, missing usage, Go/Python failure, unknown booking, required rebuild unavailable; hash-only S0 unavailable/nonmatching is snapshot_reconstruction_failed (§2.2; `src/trusted_router/billing_snapshot.py:369`) | Coverage failure; cannot count clean time across the unresolved gap |
| `exact` | Verified inputs and normalized counts, same attempted/winner kind, `P=G=B`, no known L0 disagreement | Clean comparison; S1 rebuild failure alone need not invalidate independently proven equality; hash-only always requires recovered, hash-verified S0 (§2.2; `src/trusted_router/billing_snapshot.py:369`) |
| `explained-by-catalog-change` | All equations and provenance conditions above | Clean comparison with separately reported catalog-change count |

Explicit cohort exclusions are denominator outcomes, not exact comparisons.
If a purported eligible sample cannot evaluate, the “100% evaluable” gate fails.
Malformed excessive-cache usage can book via the legacy clamp
(`src/trusted_router/services/settle_outbox_apply.py:112`); never hide that as a
catalog explanation. Refunds need positive-usage and winner-race fixtures;
live failed-provider refund attempts without exact usage are unevaluable,
not invented evaluator successes. The current enclave refund body omits usage
(`E/enclave-go/internal/trustedrouter/client.go:1223`); preserve those bytes and
record `usage_missing` rather than interpreting schema defaults as observed zero.

## 4. Evidence schema, bounds and durability

### 4.1 Sample row

`kind='async_settle_shadow_sample'`, `id='<YYYY-MM-DD>/<authorization_id>'`.
Day is UTC **authorize day**, derived from the persisted authorization; it is
stable over retries crossing midnight. Observation/booking times retain their
own UTC dates. Authorization ID is at most 64 allowed ASCII characters and
cannot inject `/`. One authoritative sample per authorization/day, not per retry.
`updated_at` is UTC write time. Body has exactly the following top-level keys;
objects have exactly their listed keys, no arbitrary maps or free text.

| Body field | Exact type / contents |
|---|---|
| `v`, `policy_version` | integer 1; string `shadow-v1` |
| `authorization_id`, `authorization_day` | key components, string |
| `observed_at_us`, `authorize_at_us`, `booking_observed_at_us` | UTC epoch microseconds int64; booking nullable |
| `workspace_fingerprint` | existing analytics surrogate convention, 64 hex; no raw workspace/key IDs |
| `adapter`, `route_type`, `streamed` | `openai|anthropic|other|unknown`; `chat.completions|responses|other|unknown`; boolean or null for unknown |
| `model_id`, `endpoint_id` | server-validated catalog identifiers ≤128 bytes, otherwise null |
| `snapshot_hash`, `payload_hash`, `rebuilt_snapshot_hash` | 64 lowercase hex or null; invalid received strings never copied |
| `raw_usage` | null or exact five RawUsage integer fields |
| `python_usage`, `go_usage`, `legacy_usage` | null or exact six NormalizedUsage integer fields |
| `frozen_micro`, `python_micro`, `go_micro`, `booked_micro`, `rebuilt_micro`, `legacy_frozen_micro` | nonnegative int64 or null; kind-dependent expectations (§3.1), booked only from confirmed winner; independent L0 never substituted from P |
| `python_minus_go`, `booked_minus_frozen`, `rebuilt_minus_frozen`, `booked_minus_rebuilt` | signed int64 or null; no absolute-value loss of direction |
| `classification`, `reason_codes` | independent comparator classification (§3 enum), not admission evaluability; sorted unique reason list, maximum 8 fixed enum values from §§2–5; report excludes admission-unknown samples as specified below |
| `eligibility` | `{requested: boolean, observed: boolean|null, exclusion: enum|null}`; never an admission bit |
| `booking` | `{attempted_kind: settle|refund, outcome: settled|refunded|pending|unknown, source: finalized_authorization|none, price_source: catalog_at_authorize_time|stage_d_document|unknown}`; compare polarity before booking deltas (§3.1; `src/trusted_router/routes/internal/gateway.py:4597`) |
| `admission` | `{prediction: yes|no|unknown, reason: enum, tier: int|null, pending_micro: int|null, cap_micro: int|null, workspace_age_us: int|null, health_age_us: int|null, health_p95_us: int|null}` |
| `timing` | `{authorize_shadow_us, handoff_prepare_us, router_settle_us, booking_confirm_us, comparator_us, evidence_write_us}`; nonnegative int64 or null |
| `deployment` | `{region, instance, router_revision, go_revision, python_evaluator, go_evaluator}`; bounded strings ≤128, revisions 40 hex, evaluator names fixed |
| `provenance` | `{binding_verified: boolean, raw_matches_body: boolean, rebuild_matches_booking_view: boolean, snapshot_transport: full|hash_only|unknown, s0_reconstruction: not_needed|verified|failed|not_attempted, legacy_oracle: stage_d_candidate_v1|unavailable, fixture_sha256: digest}`; independent oracle at `src/trusted_router/stage_d.py:161`; reconstructed S0 hash gate at `src/trusted_router/billing_snapshot.py:369` (§2.2); malformed/unparsed input uses unknown/not_attempted |

Maximum serialized sample body **8,192 UTF-8 bytes**. Reject oversize evidence
with `evidence_size`; never truncate numeric facts, identifiers or reason lists
into a misleading valid sample. No prompts, outputs, arbitrary metadata/tags,
credentials, bearer keys, exception text, raw legacy body, full JWS or key ID
is persisted. The signed snapshot/envelope live only in bounded request/worker
memory; samples retain their hashes and usage/amount projections. This meets
capture/evaluate requirements, but is not an archive for arbitrary later
re-evaluation. The content-free surrogate convention is already defined at
`src/trusted_router/storage_operational_analytics.py:67`.

`evidence_write_us` is null in the row being written: its own commit duration
cannot be known before serialization. Publish the completed write duration in
the next counter flush histogram. Likewise `booking_confirm_us` is receive to
confirmed durable-winner observation, an upper bound on booking latency, not a
precise commit timestamp. `router_settle_us` includes unchanged synchronous
side effects. No subtraction of unsynchronized enclave/router wall clocks.
Per-sample `authorize_shadow_us` is null unless an independently correlated
observation is available; authorize's aggregate histogram supplies this gate
across instances without adding another field to the proof or an RPC join.

First insertion wins. Exact retry observations increment retry counters without
overwriting. On a same-key, same-kind conflicting payload or classification, retain the
first row and durably increment the counter's correctness/reset fields; do not
silently hide a later mismatch behind deduplication. A compact monotonic
`conflict_seen` counter entry is sufficient; no raw competing payload. Reports
check both samples and counters. Refund/settle races share this authorization
sample identity; opposite-polarity attempts increment `winner_polarity` coverage
counters without overwriting the winner or automatically calling a legitimate
race a correctness defect (§3.1; `src/trusted_router/storage_gcp_counter_dml.py:753`).
Same-kind changed payload/classification remains a sticky correctness conflict.
Never count the two polarities as two successful comparisons.

### 4.2 Counter rows, sampling and completeness

**Decision:** a per-instance token bucket, **2 observations/second, burst 10**,
is acquired before expensive comparator work; one worker, at most 32 pending
observations, total queued input at most 256 KiB. Count all opted-in attempts
and exclusions before sampling with bounded in-memory integer counters.
Use finite adapter/route/stream buckets (4×4×3 maximum), never model/tenant IDs
as metric labels. More traffic than the pilot budget is a coverage signal,
not permission to silently increase load.

Global per-authority UTC observation-day cap: **100,000 sample reservations**,
including failed writes, issued in blocks of 100 by a separate transaction on
`(kind='async_settle_shadow_control', id='<day>/cap-v1')`. Body exactly
`{v:1, limit:100000, reserved: integer, updated_at_us: integer}`. A process must
hold both a local token and an unexpired day permit before sample persistence.
Unused blocks are never reclaimed on crash; this conservatively preserves the
cap. No permit allocation, counter update or sample INSERT in the money
transaction. Stop sample work when capped, while continuing counters. There
are at most 1,000 successful block allocations per authority/day; failed
allocation attempts are additionally limited to one per five seconds per
instance, with unknown-commit blocks never reused. This is not a per-request
hot counter. Across late authorizations from another date, permits charge the
observation day; retention/report reads remain by authorization day.

Per-day counter row:
`kind='async_settle_shadow_counter'`, `id='<observation-day>/<instance-boot-id>'`.
Boot ID is a server-generated UUID, not a raw hostname. Proposed body bound is
**64 KiB**, increased to retain the dimensional evidence required by parent §8
(`docs/design/async-settle-outbox-v1.md:802`):

| Fields | Schema / semantics |
|---|---|
| `v`, `instance`, `region`, `router_revision`, `policy_version` | fixed version, boot ID, bounded deployment identity |
| `started_at_us`, `flushed_at_us`, `sequence`, `closed` | integer UTC times, monotonic cumulative flush sequence, boolean graceful close |
| `counts` | fixed bucket array; each entry `{adapter,route_type,streamed,authorize_attempts,authorize_fresh,authorize_replay,header_absent,requested_eligible,snapshot_sent,settle_attempts,refund_attempts,envelope_present,observed_attempts,observed_eligible,observed_ineligible,observed_unknown,evaluable,exact,explained,mismatch,requires_review,unevaluable}` with nonnegative int64 counts; observed denominator is unsampled |
| `exclusions`, `rejections`, `drops` | sparse arrays of `{phase,adapter,route_type,streamed,reason,count}`; phase `authorize|settle|refund|worker`; same finite dimensions as counts, unknown bucket when unavailable; at most 96 reason enums and 128 distinct dimension/phase/reason entries total across these arrays; drops include `rate_limit`, `queue_full`, `daily_cap`, `store_unavailable`, `evidence_size`, `worker_error` |
| `dimension_overflow`, `counter_overflow` | nonnegative count of events beyond sparse-entry/byte capacity and sticky boolean coverage failure; no silent aggregation into a reason-only bucket |
| `comparison_attempts`, `samples_inserted`, `duplicate_samples`, `conflicting_samples` | cumulative nonnegative integers; rates label attempts versus distinct samples explicitly |
| `booking_pending`, `booking_unknown` | cumulative attempts lacking a confirmed booking; never counted as exact or inferred from HTTP 200 |
| `first_evidence_at_us`, `last_mismatch_at_us`, `first_gap_at_us` | nullable UTC times; gap is sticky, not cleared by later successful flushing |
| `authorize_shadow_hist`, `evidence_write_hist` | fixed integer microsecond bucket counts: ≤100, 500, 1000, 2000, 5000, 10000, 50000, 200000, >200000 |
| `admission_observer` | bounded cumulative `{workspace_reads,health_reads,read_failures,missed_ticks,late_installs,max_consecutive_failures,degraded_seconds,prediction_yes,prediction_no,prediction_unknown}`; nonnegative int64 counts; `degraded_seconds` is cumulative seconds rounded up once per daily counter; exact field-set validation rejects older/mixed schemas; no workspace IDs; population/cost evidence for §5 (`src/trusted_router/storage_gcp_async_admission.py:29`, `:78`) |

Increment `observed_attempts` and provisionally `observed_unknown` for **every**
opted-in settle/refund before rate/sample admission. `observed_attempts` equals
`settle_attempts + refund_attempts`; eligible + ineligible + unknown equals
observed_attempts in every bucket. A bounded, verified diagnostic may move
that attempt from unknown to eligible/ineligible even if no sample is persisted.
Requests dropped before verification stay unknown; never infer provider
eligibility from a sanitized body or the sampled population (§2.3;
`E/enclave-go/cmd/enclave/main.go:2639`). Flush cumulative consistent partitions;
no parser/evaluator bypass of the rate limit to obtain this denominator. Every
rejection/exclusion/drop carries the captured dimensions and phase, including
attempts with no sample. Sparse/serialization overflow sets the sticky gap and
blocks the window; absence of a bucket is never evidence of zero. F2b's maximum
schema-size and report tests must cover this bounded representation and the
same reason in multiple adapter/route/stream/phase buckets (parent §8 at
`docs/design/async-settle-outbox-v1.md:802`).

The persisted sample `classification` and counter `evaluable`/outcome fields
are **independent comparator diagnostics**: arithmetic can be proven even when
admission observation is unknown. The report first reconciles those unmodified
fields and applies every comparator correctness/reset gate. Its public
`metrics.classification` and `denominators` then classify each durable
admission-unknown sample as `unevaluable`, removing it from exact, explained,
mismatch and evaluable totals in its own dimension. The original classifications
remain visible in `metrics.comparator_classification`. Per-writer
`metrics.admission_unknown_samples` reconciles those samples separately against
`admission_observer.prediction_unknown`; the latter also includes prediction
attempts whose sample was not inserted. Unknown admission never satisfies
`positive_sample` or seeds the clean clock. A proven comparator disagreement
still blocks/resets the window regardless of admission evaluability.

Flush cumulative counters at most once per five seconds per active instance,
including after rate/cap drops, with no synchronous request wait. Transactional
replace-only-if-sequence-increases makes a retried/out-of-order flush idempotent.
Final flush on graceful shutdown;
no unbounded retry queue. A per-day manifest row under shadow control, with
`id='<day>/manifest-v1'`, records
the serving instance roster, revisions, admission-disabled interval, flag
configuration fingerprint, first evidence time and completeness assessment.
Its roster comes from deployment inventory, not only successfully registered
writers. Maximum 4,096 counter writers/day; reaching it fails coverage closed.
The manifest body is bounded to 256 KiB and has exactly `v`, `day`,
`instance_boot_ids` (sorted unique UUIDs), `router_revisions`, `go_revisions`
(sorted unique SHA lists, ≤64 each), `configuration_sha256`,
`admission_disabled_from_us`, `admission_disabled_until_us`,
`first_evidence_at_us`, `completeness` (`complete|unknown|gap`),
`gap_intervals` (≤128 pairs of UTC microsecond bounds), and
`proof_manifest_sha256` (nullable). Missing intervals/end time are unknown,
not inferred off-state; overflow invalidates completeness. The report's
external proof manifest supplies the signed/reviewed deployment inventory,
test artifact hashes and policy decisions. It is not reconstructed from logs.
Obtaining a complete instance/traffic roster is itself a rollout proof
requirement, not an asserted Cloud Run API capability. If operators can prove
only revision-level traffic, but cannot account for an instance that died
before its first flush, completeness stays unknown. Do not let a manifest's
manually entered `complete` bypass that evidence requirement.

Authorize denominators count successful authorizations in the opted workspace,
including exclusions and replays; failed authorize requests are outside this
price-comparison cohort. Record that scope in the report. Authorize buckets
use the initially selected adapter; settle buckets use the actual selected
adapter, so fallback movement is not mistaken for missing traffic. Exclusion
enums include `billing_snapshot.exclusion`'s fixed reasons
(`src/trusted_router/billing_snapshot.py:181`) plus `unsupported_adapter`,
`unknown_parameters`, `replay`, `header_absent`, `snapshot_unavailable`,
`usage_missing`, `usage_estimated`, `malformed_usage` and
`arithmetic_overflow`. Additional diagnostic reasons are `booking_pending`,
`booking_unknown`, `rebuild_unavailable`, `snapshot_reconstruction_failed`,
`catalog_change`, `go_failure`,
`configuration_conflict`, `missing_envelope`, `snapshot_size`, `winner_polarity`,
`legacy_oracle_unavailable`, `counter_overflow`, and the §2 rejection/§4 drop
enums. Adding a reason requires a versioned schema/fixture update.
`snapshot_size` is recorded only for the §2.2 local-work cap, not a successful
hash-only fallback; `snapshot_reconstruction_failed` records failed hash-only
recovery. Each retains its authorize/settle/refund phase and dimensions and
fails coverage for otherwise-supported traffic
(§2.2; projection includes fallback candidates at
`src/trusted_router/routes/internal/gateway.py:2782`).

**D-S4 limitation:** best-effort, post-commit writes cannot guarantee durable
drop counts during database outage or crash between counter flushes. Record
known drops at the next successful flush; an unclosed writer, roster mismatch,
missing flush/manifest or unknown interval makes completeness **unknown** and
blocks a clean-window claim. Do not treat logger output, absence of rows or
process memory as proof of zero loss. Lossless every-attempt denominators
would require a durable pre-response handoff and violate the chosen isolation
and latency constraints. Daily loss counters are required, but their failure
must itself be represented as unknown coverage, never silently assumed zero.

No new stream is added to the operational outboxes: their current event kinds
are activity/synthetic/client_events
(`src/trusted_router/storage_operational_analytics.py:51`), and their activity
delivery can be inside the money transaction
(`src/trusted_router/storage_gcp_operational_analytics_outbox.py:71`,
`src/trusted_router/storage_postgres_operational_analytics_outbox.py:98`).
Reusing that transactional activity path for shadow would violate D-S4.

### 4.3 Retention and bounded reads

Keep samples, counters and manifests **30 days**; export a content-free report
with revision/fixture hashes before removal if longer review is needed. A
separate operator cleanup command deletes only explicit expired day ranges,
at most 200 complete keys/transaction, 5 transactions/second, 30-second pass
budget, low priority, no retries. Resume by last key. No schema TTL change,
request-triggered cleanup, body scan, wildcard kind discovery or unbatched DML.
Sample age uses the authorization-day key; counters/manifests use their day.
Reject shadow observations older than proof lifetime, so late writes cannot
repopulate already expired ranges.

```sql
SELECT id, body FROM tr_entities
WHERE kind=@kind AND id>=@day_start AND id<@next_day_start AND id>@after_id
ORDER BY id LIMIT @page_size
```

Bind kind to exactly one of the three named kinds, day bounds to
`YYYY-MM-DD/` and next calendar day's prefix, page size ≤200; first-page cursor
is empty. No LIKE or JSON predicate. Operator report reads retain a 0.2-second
deadline, LOW priority and no retries independently of the admission-reader budget
(`src/trusted_router/storage_gcp_async_settle_shadow.py:76`). Timeout/incomplete
pagination is a failed report, never a partial passing table. A report for a
seven-day observation interval reads the requested days plus up to two preceding
authorization-day prefixes for long calls; include all requested counter days.

## 5. Admission prediction, cost and latency budget

**Decision:** read-only peek of a separate shadow observation cache, populated
by the bounded timer below; reject calling the current `eligible()` for
shadow because it can issue workspace/health RPCs and takes a timed lock
(`src/trusted_router/services/async_settle.py:64`). Prediction is `yes` only
with valid tier 2/3, fresh nonnegative pending sum ≤ pilot override or
25,000,000/100,000,000 micro, health p95 ≤5 seconds and **both** data ages in
`[0,5)` seconds. Valid known ineligible tier/cap/unhealthy data gives `no`;
missing, stale, locked, truncated or unavailable evidence gives `unknown`
(operational admission would fail closed). Record reason and ages; never
refresh age on receipt. Code sources for caps/predicate are
`src/trusted_router/services/async_settle.py:35`, `:97`, `:166`.
Admission reason enum: `eligible`, `ineligible_tier`, `cap_exceeded`,
`drain_unhealthy`, `cache_missing`, `cache_stale`, `cache_busy`, `invalid_data`.

The existing runtime starts with empty workspace entries and no health
(`src/trusted_router/services/async_settle.py:59`); entries populate only inside
`eligible()` (`:83`), admission-off projection returns before that call (`:248`),
and construction omits the health reader (`:214`). Thus the old peek proposal
would yield **no known predictions**, not merely a low known fraction.

**F2b owner: the router implementation author must add and prove this observer
before enablement.** While admission is off and the opt-in set is nonempty,
start a lifecycle-owned timer with a separate cache/executor, maximum two
in-flight reads, no unbounded queue and no calls to `eligible()`. Once per four
seconds read each configured opted workspace, at most 32, using the existing
bounded indexed `read_admission` (`src/trusted_router/storage_gcp_async_admission.py:13`,
`:29`), plus fixed-key `read_health` **once per second** (`:78`). Reserve
health scheduling/limiter capacity separately within the shared limits; workspace
reads cannot starve it. Do not discover workspaces or
scan/publish fleet health from shadow. Rate-limit all reads to 10 starts/second,
burst two; spread reads across the cycle, at most one outstanding read per key,
skip overdue ticks without catch-up. Each read uses LOW priority, ≤500 ms and
no retry, as those readers already specify (`:33`, `:75`). Disable/stop the
timer and clear evidence on opt-in removal or admission enablement. Empty set
creates neither timer nor reads. Nonmembers never populate this cache.
The 2026-10-09 22:12Z regional probe measured São Paulo–nam6 at roughly
150 ms per round trip, with 200 ms deadlines failing 1/60 health reads and
4/60 admission reads, motivating the 500 ms read/control-transaction budgets.

**2026-10-10 bounded-failure amendment.** Three consecutive 60-second regional
passes against `trusted-router-nam6` at the 500 ms deadline showed one
health or admission timeout in four of six far-region passes, at sample indices
12–19 after startup (two passes had none), and none later or in US health/admission
reads. Europe-west4 health timed out at indices 12 and 17; southamerica-east1
admission at 19 and health at 17. Steady-state health medians were
141 ms in europe-west4 and 163 ms in southamerica-east1. This pattern is
consistent with a fresh gRPC connection being rebalanced/re-established by the
front end: about three round trips at roughly 150 ms each approaches the
500 ms deadline. Connection recycling can recur in long-lived processes; a
single tail event must not permanently disable observation. Read-only evidence:
`/Users/jperla/.claude/tr-briefs/fleet-pre/run-20261010T042056Z/probe/`,
`run-20261010T043214Z/probe/`, and `run-20261010T043905Z/probe/` under that same
`fleet-pre` directory. The reconnect explanation is an inference from timing,
not a transport trace.

A failed read, start more than 0.25 s late, or install more than 0.5 s after
read start immediately invalidates only the affected cache (health affects all
workspaces). Predictions remain `unknown/cache_stale` until the next clean
read/install; a result from a failed or older tick cannot restore freshness.
Track consecutive failed ticks separately for health and every workspace key:
health is not progressing at **3**, a workspace at **2** (its cadence is 4 s).
A clean subsequent tick resets that reader's streak. A late start followed by a
late install increments both event counters but is one failed tick. Successful
installs after a failed tick do not themselves fail merely because the previous
successful install was a cadence ago. No catch-up reads, retries or deadline
changes. `late_installs` counts installs beyond 0.5 s;
`max_consecutive_failures` records the maximum streak seen during the daily
counter's lifetime, including a streak continuing into it. `degraded_seconds`
records monotonic elapsed time with at least one reader at its degradation
threshold, counting overlapping reader intervals once; it does not depend on
traffic or `peek()` calls. Fold observer evidence before acknowledging a retiring
writer. Both counter snapshot passes may retire only days sealed by that
observer snapshot after accounting through their UTC end. A flush spanning
midnight must leave an unsealed day open for the next flush. Split elapsed
degradation and maximum streak at UTC midnight even if recovery occurs before
the next flush; a continuing streak belongs to both days.
Retain cumulative observer buckets until a sealed day's fold succeeds; repeated
or partially failed folds apply cumulative totals without duplicating evidence.
Retain at most three days of observer buckets, matching counter retention, with
sticky fail-closed coverage on overflow. Keep subsecond precision internally and round the
cumulative daily duration upward for integer-only evidence JSON, conservatively
adding less than one second per counter, not per flush. Isolated stale
predictions still count as unknowns.

Both the §5 fleet gate and §11 report require, **per instance counter**:

- `read_failures + missed_ticks + late_installs ≤ max(3, 0.01 × (health_reads + workspace_reads))`;
- `max_consecutive_failures ≤ 2`;
- `prediction_unknown ≤ 0.02 × (prediction_yes + prediction_no + prediction_unknown)`, with `prediction_yes + prediction_no > 0`;
- `degraded_seconds ≤ 0.01 × ((flushed_at_us - started_at_us) / 1e6)`.

The three-event floor admits an isolated reconnect even in short intervals;
the 1% event/time budgets keep tails exceptional, and the 2% unknown budget
bounds lost admission coverage while requiring positive known evidence.
Three consecutive failures block the historical gate even after live recovery;
two workspace failures may degrade live progress but must fit the time budget.
Outputs include each counter's event/read, unknown/prediction and degraded/time
ratios, numerators, denominators and maximum streak. These are availability
budgets, not correctness allowances: unknown admission predictions are
non-evaluable, never mismatches or positive clock seeds. The comparator and
its zero-correctness-disagreement rules are unchanged.

Cost per opted-in router instance is at most **N/4 + 1 bounded reads/second**
in steady state, **9/s at N=32**, bounded additionally by the rate limiter;
zero writes, zero request-triggered reads. Each workspace read inspects at most
1,001 indexed pending/dead rows plus its complete-key trust row
(`src/trusted_router/storage_gcp_async_admission.py:18`), so disclose up to
8,008 pending-row visits/second per instance at maximum N, not just RPC counts.
Fleet cost multiplies by the manifest's serving instance count, including
autoscaling and overlapping rollout revisions: at N=32 on I instances this is
**9I reads/s and 8,008I pending-row visits/s**, plus **8I trust-row and I
health-row visits/s** (`src/trusted_router/storage_gcp_async_admission.py:18`,
`:55`). **Before enablement**, validate a regional and whole-fleet budget
against measured Spanner headroom, the maximum serving-instance roster and
existing publisher/other load; record the approved instance cap and measured
CPU/read/latency impact. These per-instance limits are not a proven safe fleet
budget. Do not enable without that evidence, or automatically increase read
rates when cadence fails (parent load gate:
`docs/design/async-settle-outbox-v1.md:818`).

**Combined freshness budget (proposed gate, not a measured guarantee):** pin
the health publisher period P≤2 s (default 2 s at
`src/trusted_router/config.py:977`), publisher start-gap excess Jp≤0.25 s,
and observation-start-to-durable-publication Dp≤0.5 s. Its timestamp is read
start, not commit time (`src/trusted_router/storage_gcp_async_admission.py:137`,
`:165`, `:181`). Health polling period T=1 s, excess read-start gap Jr≤0.25 s,
read-start-to-cache-install Dr≤0.5 s, and publisher/reader wall-clock skew
allowance S≤0.25 s must together satisfy
**P+Jp+Dp+T+Jr+Dr+S ≤ 4.75 s < 5 s**. This bounds the age of the cached
publisher observation just before the next install for every phase offset,
after initial population. The same bound covers heartbeat age because it is
no earlier than observed_at (`src/trusted_router/services/async_settle.py:136`).
Workspace read-start age has its own **4+0.25+0.5=4.75 s < 5 s** bound using
the local monotonic clock. Scheduling excess includes limiter/executor delay;
install delay includes RPC and decoding. These are end-to-end requirements,
not consequences of an RPC timeout. Both chains leave **0.25 s** of margin
under the governing 5-second `CACHE_SECONDS` consumer limit. Preserve the actual
`[0,5)` age checks,
including rejection of future timestamps; clock anomalies remain unknown
(`src/trusted_router/services/async_settle.py:166`).

F2b must measure/prove these maxima under pilot scheduling/load, including
publisher progress and arbitrary read/publication phase, before enablement.
The component maxima describe cache observations accepted as fresh. Failed or
late ticks invalidate the affected cache and consume the bounded event/unknown
budgets above; they never justify a larger freshness or install bound.
If the budget is exceeded or unproven, observation coverage is unknown;
skip overdue ticks without catch-up, invalidate the affected cache, and block/reset
the clean interval when the per-counter tolerance above is exceeded.
**Successful RPCs alone do not satisfy the observation gate.** For the old
P=2/T=4 cadence, publish at 0,2,4,… and read at 1.5,5.5,9.5,… leaves cached
timestamp 0 stale at 5.1 despite durable timestamp 4; with T=1, reads at
1.5,2.5,3.5,… remove that recurring phase-only gap. F2b must test continuously
between reads, not just at successful read completion (review reproduction:
`/tmp/f2a-r2-probes.txt:18`; freshness check:
`src/trusted_router/services/async_settle.py:172`).

Timestamp workspace observations at read start. Preserve health's durable
observation/heartbeat ages and completeness when decoding
(`src/trusted_router/services/async_settle.py:166`); receiving stale data never
rejuvenates it. Missing/incomplete/stale health, absent or truncated workspace
rows, timeout, timer starvation, lock contention or startup produce `unknown`.
Fresh complete unhealthy data gives `no`; inspect validated health fields
before `decode_health` collapses unhealthy data to None (`:118`, `:173`).
The observer does not create the required fleet-health publisher. If no fresh
complete row exists, record the reason and fail prediction coverage. F2b must
prove lifecycle CPU/timer progress under the actual Cloud Run configuration;
a thread with no CPU after the response is insufficient. Until that proof and
a working health source exist, parent §8's prediction/cache-age requirement
(`docs/design/async-settle-outbox-v1.md:802`) is **unmet**, with F2b owning closure.
Require known prediction/age evidence for every admission-evaluable sample in
the clean interval. Unknown admission predictions are non-evaluable and must
fit the per-counter bounds above; they cannot start the clock. Comparator
unknowns and missing evidence still block/reset it; an all-null run cannot pass. Known
pending exposure with all traffic synchronous is not async-load capacity proof.

| Request / work | Additional CPU | Additional RPC / commit |
|---|---|---|
| Non-opted router authorize/settle, including empty set | Constant guard only; no shadow parsing, copying, hashing, counters or observer | 0 |
| Header-bearing non-opted authorize | Existing PR B snapshot work remains; **0 incremental shadow work** | 0 incremental |
| Opted fresh authorize with header | Existing snapshot build/hash plus one bounded shadow signature, optional-field serialization, counter increment | **0 synchronous, 0 per-request background RPC**; amortized counter flush only |
| Opted authorize exclusion/replay | Bounded reason classification/counters; no new snapshot for replay | 0 per-request |
| Opted-workspace observation timer | Bounded decode/cache update, two readers max; independent of request rate | ≤N/4+1 reads/s/instance, 10/s burst two limit, zero writes; §5 combined freshness/fleet gates apply (`src/trusted_router/storage_gcp_async_admission.py:29`, `:78`) |
| Enclave eligible shadow settle | Whole-snapshot Hash, Evaluate + ValidateEnvelope, encode full/hash-only header ≤12 KiB, monotonic timing (§2.2; `src/trusted_router/billing_snapshot.py:369`) | Same single legacy settle call/retry policy; no additional network call |
| Opted router settle before response | Constant observation capture/queue submission; no evaluator or evidence I/O | 0 on response path |
| Admitted comparator worker | Strict decode, signature/hash verification, Python Evaluate + ValidateEnvelope, independent legacy S0 oracle, bounded S0 recovery when hash-only plus S1 rebuild (reuse only for identical captured inputs), serialization (§2.2; `src/trusted_router/billing_snapshot.py:436`) | ≤1 complete-key booked-authorization read; no new catalog RPC; ≤1 separate sample transaction with same-key dedup read/insert; amortized cap/counter transactions |

“One transaction” is not “one RPC”: dedup uses a read and mutation commit;
SDK begin/commit overhead must be included in measured cost. All shadow storage
work has a shared **1 second** worker I/O budget, read RPC deadlines at
most 200 ms, and transaction-control/commit RPC deadlines at most 500 ms,
always capped by the remaining shared budget, LOW priority and no
application retry; skip when budget is gone. Use a dedicated single-worker
executor so shadow cannot fill the money executor's queue. Queueing adds no
per-request storage. This budget must be measured in every pilot region;
regional timeouts/drop rates invalidate coverage rather than being hidden.
The October 10 commit-budget correction separates point-read latency from a
replicated commit's latency. A 150-163 ms regional round trip leaves too little
headroom for commit processing under a 200 ms cap. This does not extend the
worker budget, add retries, or relax any evidence gate. Failures remain sticky
and unacknowledged; rate-limited diagnostics expose only a fixed stage/reason
and elapsed time, never exception text, SQL parameters, or evidence bodies.
Tie accepted observer work to bounded post-response ASGI task lifetime; do not
assume an unowned daemon thread will get CPU after a Cloud Run request ends.
Semaphore/queue admission is nonblocking, and the outer task catches failures
without interrupting existing post-commit tasks. Counter flushes piggyback on
these tasks at their cadence and on graceful shutdown. No traffic means no
claim of fresh observations; shutdown loss still follows §4's coverage rule.

**Authorize is the budget. Decision:** new overhead must be ≤1 ms p95 and
≤2 ms p99 CPU and ≤2 ms p99 wall at the real authorize response seam, with
zero added RPC on cache misses or failure. This is a proposed F2 acceptance
budget, not an existing measured SLO. Reject a design that spends the 500 ms
admission-reader timeout on authorize merely because it fits a larger RPC
deadline. Enclave header construction budget: ≤2 ms p99; router pre-response
observation capture: ≤100 µs p99; background comparator CPU ≤5 ms p99.
Benchmark warmed/cold processes and maximum envelopes; on local budget or
queue failure, count a drop and keep the sync response. Python scheduling and
GC cannot promise a hard microsecond deadline; percentile evidence and bounded
input/work are the gate, not a timer asserted to interrupt arbitrary work.

Fleet-wide enclave shadow sends the authorize header before it knows workspace
opt-in (`E/enclave-go/internal/trustedrouter/client.go:840`). It must do no
evaluation/header construction unless `billing_shadow_binding` is present.
The added authorize header and PR B's existing response metadata consume some
network/CPU even for non-opted traffic. Absolute fleet-wide zero overhead is
therefore infeasible under D-S2 plus byte-identical D-S6; measure and disclose
that baseline effect separately from F2's incremental router cost.

## 6. Report, seven-day window and exit criteria

Proposed `scripts/async_settle/shadow_report.py --day YYYY-MM-DD [--day ...]`
reads only the bounded day ranges above and an explicit proof manifest. Print:
coverage interval and completeness; authorize/settle/refund attempt denominators by
adapter/route/streaming; requested/observed eligibility and exclusion reasons;
distinct samples versus retries; exact/explained/mismatch/requires_review/unevaluable counts;
all dropped/rejected counts; signed delta histogram; prediction yes/no/unknown
and known fraction; cache-age/timing p50/p95/p99 with count/null count per region;
source/fixture/report hashes; clean-window start, reset dates and reason.
Strictly validate row sizes, exact schema/types, key/body identity, monotonic
counter sequences and arithmetic relationships before aggregation. Malformed,
oversized or contradictory stored evidence fails completeness; no coercion,
ignored bad rows, or zero-defaulting can turn a damaged report into PASS.
No inferred zeroes for absent buckets. Percentiles use nearest rank on sample
values; histogram-only counters report bucket bounds, never invented precision.
Delta bins: `0`, signed `1`, `2–10`, `11–100`, `101–1000`, `>1000` micro.

Clock starts only at the first **durable evaluable sample** after both
enablement steps: classification exact/explained, verified binding/hashes/usage,
same-kind confirmed booking, defined P/G/B and required oracle for explanation,
known admission prediction/ages, and complete deployment/counter coverage.
Require §2.2's pilot authorization size/mode distribution and actual-hop
maximum-auth-plus-shadow acceptance evidence, and §5's validated fleet load
and combined publisher/poll/scheduling freshness budgets, before this sample
can start the clock (`src/trusted_router/routes/internal/gateway.py:2782`,
`src/trusted_router/config.py:977`, `src/trusted_router/services/async_settle.py:172`).
F2b must enforce this positive predicate; a control/counter row, empty run,
all-null run or first unevaluable sample cannot start the clock (parent §8,
`docs/design/async-settle-outbox-v1.md:796`, `:802`, `:811`).
Success means ≥604,800 seconds of continuous complete
observation, not seven calendar filenames. Every correctness mismatch, even
an unsampled one counted in counters or a retry conflict, resets eligibility
for a clean interval. Start again at the first sample satisfying that predicate
after the fixed revision is serving and the mismatch is resolved; retain reset
timestamps/revisions. Resolution validity is fleet-wide: any supplied sample,
counter interval or daily revision roster showing the defective revision (or
an earlier revision) serving again invalidates that resolution and adds a
`revision_rollback` reset at the first such instant, before requested-day
filtering. Daily rosters conservatively cover the entire UTC day. Commit hashes
are opaque; reviewed resolution links define a partial order, so only the fixed
revision and its reviewed successors prove fix retention. Unordered revisions
also block; cycles are rejected. Restoring the fix after a rollback requires a
new reviewed resolution for that reset before the clock can restart.
Every accepted resolution is itself evidence that its defective revision and
its reviewed predecessors are defective: enforce its history even when the
underlying mismatch rows are absent. Each resolution must also have supporting
mismatch evidence or a rollback derived from supplied serving evidence at its
exact timestamp/revision. Missing support is a persistent `missing_support`
gap identifying that resolution; it cannot age out into a clean window.
Duplicate timestamp/revision resolution keys are rejected as ambiguous.
An evidence gap is not proof of a correctness bug, but cannot count as clean
time: conservative restart after coverage is restored. A router restart does
not erase durable history; continuation requires verified closed/flushed
writers, continuous manifest coverage and matching policy/fixture hashes.
Rates/caps that drop eligible samples block the zero-unexplained claim until
coverage is restored; do not extrapolate a sampled zero into fleet correctness.

| §8 bullet / requirement | Data field or external artifact | Pass rule |
|---|---|---|
| Seven days, admission disabled (`docs/design/async-settle-outbox-v1.md:796`) | sample observation times, day manifests, serving revision/flag roster, reset/gap timestamps | Continuous 604,800 seconds; every serving router admission off and enclave negotiate off; no unresolved gaps or correctness mismatches |
| Denominators/usage/hash/delta/prediction/cache/timing (`:802`) | counter buckets plus sample `*_usage`, hashes, deltas, `admission`, `timing`, `deployment` | Complete attempted-traffic denominator for opted workspaces; report missing/unknowns and per-instance §5 ratios; admission unknowns ≤2%, events ≤max(3,1% of reads), max streak ≤2, degraded time ≤1%; no invented fleet denominator outside opt-in |
| Zero unexplained amount/normalization/signature/hash/identity; all admitted evaluable (`:811`) | classifications, rejection/mismatch counters, `eligibility`, null amounts | Zero correctness disagreements; 100% of selected otherwise-eligible shadow observations evaluable; “admitted” here means admitted to comparison, **actual async admissions remain zero**; comparator unknown/drop coverage cannot pass; admission unknowns remain non-evaluable within §5 bounds |
| Shared 759-case/hash and wire fixtures (`:813`) | both CI artifact SHAs, evaluator and shadow literal pins, rebased revisions | Byte equality and both repo suites pass; report includes exact builds, no reliance on 93,561 probes |
| Frozen-main/pricing-matrix/crash/mutation gates (`:816`) | F2b/F2c oracle/mutation manifests plus F1/F proof artifacts | Identical response/counters/reservation winner/generation/side effects and operation traces; all intended mutations killed by assertions |
| D2, D3, throughput/burst/SLO (`:818`) | independent isolated Spanner load/crash, revocation and unknown-commit reports with regional p50/p95/p99 | All approved targets demonstrated independently; shadow synchronous timing is baseline only, never async handoff/drain capacity proof |
| Exposure, side-effect exclusions, rollback, truthful terminal status (`:821`) | Joseph's policy decisions, cohort manifest, rollback and pending/drain tests | Reviewed policy and exclusions, no duplicate booking/free release, tested admission-off/drain-on rollback; shadow does not satisfy these by itself |
| Rare catalog/tier/cache/retry/refund/zero cases (`:805`, `:825`) | fixture coverage matrix, observed bucket counts | Every required case covered by fixtures/load evidence when absent in traffic; missing production coverage explicitly marked |

D2 is ≤500 ms durable handoff and ≤2 seconds total in the parent design
(`docs/design/async-settle-outbox-v1.md:786`). PR E's inspected implementation
documents a shared **28-second** recovery budget
(`E/docs/validation/async-settle-pr-e.md:20`;
`E/enclave-go/internal/trustedrouter/settlement_retry.go:12`,
`E/enclave-go/internal/trustedrouter/async_settlement.go:216`). Do not claim D2
passes from that implementation or from this shadow report; reconciliation of
that budget is an external activation blocker. D3 and the 5/60-second drain
objective likewise require independent evidence, as the parent already says
(`docs/design/async-settle-outbox-v1.md:665`, `:774`, `:1184`).

### 6.1 Where production diagnostics actually go

`metric=async_settle_*` is a Python **INFO log**, not a registered numeric metric
(`src/trusted_router/services/async_settle_handler.py:30`). In this tree,
`create_app` installs a package INFO stderr handler after observability setup
(`src/trusted_router/main.py:176`, `:232`). Ordinary metric lines use its text
formatter; only acquisition events receive the special Cloud Logging JSON
projection (`:147`). The configured container destination is therefore stderr
→ Cloud Run container logs, not automatically a Cloud Monitoring time series.

With a token, records also propagate through the bounded Axiom queue
(`src/trusted_router/axiom_config.py:103`, `:161`, `:180`); rollout config names
dataset `trusted-router-logs` and the EU endpoint
(`scripts/deploy/rollout.sh:211`, `:515`). That queue can drop records
(`src/trusted_router/axiom_config.py:473`). No async-settle OTel meter/exporter
or Cloud Monitoring metric creation is wired in the inspected metric helper
or app startup (`src/trusted_router/services/async_settle_handler.py:30`,
`src/trusted_router/main.py:232`); a `metric=` prefix does not create one.

The supplied production observation “no Python logger lines” is **not verified
live in this pass**. The source itself describes the former root-handler failure
and contains a fix (`src/trusted_router/main.py:179`). Serving-revision presence,
Cloud Logging ingestion and Axiom delivery must be verified at rollout. Until
then there is no defensible assertion that these lines actually land in the
observed service. Evidence rows are the **only required durable shadow record**;
the report must work with all Python logging/exporters disabled. This design
does not add an OTel exporter or logging repair.

## 7. Failure modes and crash points

| Boundary / failure | Required behavior |
|---|---|
| Shadow store unavailable | Original money result/exception survives. Bound failed worker I/O, increment local `store_unavailable`, flush later if possible; missing flush gives unknown coverage. Never fall back to operational outbox or money transaction. |
| Envelope invalid/duplicate/oversize | Book legacy exactly as before; reject only shadow, fixed reason counter, no echoed content. Upstream HTTP size rejection is outside Python's guarantee. |
| Inline snapshot/terminal/payload/proof mismatch | Hash/signature classification, correctness reset; neither envelope charge nor rebuilt price can influence booking. Hash-only S0 recovery failure is the separate coverage case below (§2.2; `src/trusted_router/billing_snapshot.py:369`). |
| Identity mismatch | Count/reset; opt-in comes from server authorization so foreign-workspace envelope cannot activate collection. |
| S1 rebuild fails / catalog removed | With independently verified S0, preserve frozen Python/Go and confirmed booked comparison; exact can remain exact, otherwise `unevaluable/rebuild_unavailable`; never mark unexplained delta as catalog change (§3 step 7; `src/trusted_router/catalog.py:352`). |
| Hash-only S0 rebuild unavailable / hash differs | `unevaluable/snapshot_reconstruction_failed`, null P/L0/frozen deltas, coverage failure even with known B; no fallback to S1 or claimed catalog explanation (§2.2; `src/trusted_router/billing_snapshot.py:369`). |
| S0 legacy oracle unavailable / disagrees | Apparent catalog delta is `requires_review/legacy_oracle_unavailable`, or correctness mismatch if known L0 differs from P. B=R alone cannot validate S0 (`src/trusted_router/stage_d.py:161`; §3.1). |
| Supported snapshot exceeds inline cap | Send hash-only; recover and authenticate whole S0 or fail coverage. Only exceeding §2.2's local-work cap is `snapshot_size`; any outer/proof overflow still fails coverage with dimensions. Retain all candidate identities and PR B baseline (`src/trusted_router/routes/internal/gateway.py:2782`; §2.2). |
| Local rate/queue limit hit | Drop before expensive work, cumulative per-day `rate_limit`/`queue_full`; coverage fails for otherwise-eligible dropped traffic. |
| Daily cap hit / permit allocator fails | No sample write; `daily_cap`/`store_unavailable` counter; keep counters under their separate cadence. Never bypass cap to record the drop. |
| Enclave sends envelope without workspace opt-in | Ignore entirely, no parse/log/count/RPC. A non-opted counter would itself violate the zero-additional-work contract. |
| No binding/authorize snapshot or signer disabled | Enclave sends legacy only; router denominator records `snapshot_unavailable` or `header_absent`; clock cannot pass without evaluable evidence. An omitted return-header snapshot with valid binding is hash-only, not absence (§2.2; `src/trusted_router/services/async_settle.py:235`). |
| Router restart mid-window | Durable rows/reset history survive. Unclosed counter interval or missing roster creates a coverage gap; no automatic “clean since boot.” Lost cap permits are not reused. |
| Crash after money commit, before sample/counter write | Money remains booked, sample may be absent. Mark interval incomplete from writer/manifest reconciliation; no claim of lossless evidence. |
| Legacy returns durable intent, not booking | `booked_micro=null`, `booking_pending`; no comparison against intent amount; bounded later read may document winner but cannot backdate a clean comparison. |
| Legacy throws / caller cancels | Preserve original exception/cancellation and retry ownership. Observer may fail to run; denominator/completeness must show the gap. Never cancel or retry money on shadow's behalf. |
| Retry with changed payload or opposite winner | Existing first reservation claimant wins; same-kind payload conflict resets correctness, opposite-kind `winner_polarity` blocks coverage, first sample retained. No second pending intent (`src/trusted_router/storage_gcp_counter_dml.py:753`; §3.1). |
| Observer cannot populate / timer starved | Prediction unknown with ages/reason; no request refresh, no invented healthy zero. Self-recover on the next clean tick; block the window if §5 per-counter bounds or F2b observation/health proof are not met (`src/trusted_router/services/async_settle.py:59`, `:214`; §5). |
| Proof expires / key rotates / regional failover | Count unevaluable or identity failure as appropriate; never regenerate an old snapshot from a new catalog; retain verification keys for the observation lifetime. |
| Both enclave switches on / router admission enabled during window | Force local legacy shadow mode; configuration counter. Report rejects the interval if admission is enabled on any serving router. |

## 8. PR F2b — router implementation and proof

Scope: setting/off rollout pin; separate-purpose binding after authorize; bounded
header observer around legacy settle and refund; independent legacy S0 oracle;
timer-driven admission observation (§5); pure comparator/classifier; post-commit
sample/cap/counter writer; day-prefix report/cleanup; content-free diagnostics.
No modification to pricing formulas, transaction SQL, reservation gates,
`GatewaySettleRequest`, async handler dispatch or operational-outbox schema.

| Test group | Required assertion |
|---|---|
| Frozen-main authorize | Freeze this revision and source/AST hashes; empty set and nonmember across no/exact/duplicate/bad header, signer available/missing, admission off/on, replay/excluded/cohort. Exact response bytes and operation traces match PR B; existing header snapshot remains. Extend `tests/test_async_settle_authorize_oracle.py:33` and `:47`. |
| Frozen-main legacy settle | Valid/invalid/oversize shadow headers with empty set/nonmember: parser, response/error bytes, SQL/params/types/read/commit counts, full counters/holds/generation/outbox/side effects match frozen source. Extend operation oracles `tests/test_async_settle_oracle.py:25`, `:43` and `tests/test_settle_c1_oracle.py:14`. |
| Opted-in no interference | Inject every parser/evaluator/storage/queue/signature exception after real sync result; response and money state unchanged for success/replay/refund/deferred/error. Comparator cannot call finalize/enqueue/finish. Evidence transaction identity differs from money transaction and begins only after outcome. |
| Provenance and wire | Exact literal pin; strict parsing/bounds; altered snapshot plus recomputed self-hash still rejected by signed binding; valid foreign binding rejected; proof cannot pass either ticket validator; absent/malformed nonce or signer never replaced by defaults. |
| Arithmetic / explanation | Integer ±1 boundary, both prompt conventions, reasoning subset, inclusive/last tier, zero, overflow, changed applicable catalog, later scheduled price, Stage D frozen document, removed endpoint, unrelated candidate change, stale rebuilt view; require independent `L0=P=G` and `B=R`. Test missing oracle → requires_review; known L0 mismatch → disagreement; legacy candidate adapter field mapping and dependency separation (`src/trusted_router/stage_d.py:161`; §3.1). |
| Refund / winner | Positive exact usage refund: evaluate usage charge 2 but P=G=B=0; body without usage → usage_missing; exercise actual refund entry, empty/nonmember/opted guards and exceptions. Race settle/refund in both reservation-claim orders, delayed observations, both delivery orders, lost-ack replay and zero-cost settle winner: winner amount/polarity retained, cross-kind deltas null and requires_review, no false arithmetic disagreement or second booking (`src/trusted_router/routes/internal/gateway.py:2316`, `:4597`; §3.1). |
| Transport coverage | Reproduce 645/1,041/1,884/2,726/3,563/28,821-byte projections at pinned catalog; named four-model envelope passes full mode at 5,492/7,323 decoded/encoded bytes. All-43 case uses hash-only and passes only with exact original candidates/eligibility and canonical ordering and rebuilt S0 hash; missing view/context, catalog correction/removal, changed candidate set/order or hash mismatch yields snapshot_reconstruction_failed and blocks clock. Exercise outer-bound fallback even below inline cap, both modes at every boundary, no trimming, and local-work-cap snapshot_size with unsampled dimensions. Keep §2.3 literal hashes/lengths unchanged; add hash-only success/failure fixtures. Pilot authorization size distributions and maximum auth+12,288-byte shadow value with fragmented delivery must pass §2.2's actual-hop gate before clock start (`src/trusted_router/routing.py:947`, `src/trusted_router/billing_snapshot.py:369`). |
| Accounting and reporting | Caps across concurrent instances, dedup/retries/midnight, changed-payload retry, rate-before-evaluate, cumulative idempotent flush, counter failure, unclosed restart, unknown admission, percentile null counts, signed histograms and seven-day/reset/coverage logic. Same reason in multiple adapter/route/streaming/phase buckets with **no sample rows** must remain separate; observed partitions reconcile all settle/refund attempts, overflow blocks, and empty/counter-only/all-null evidence never starts clock. No log dependency (parent §8, `docs/design/async-settle-outbox-v1.md:802`, `:811`). |
| Admission observation | Fresh runtime with admission off must gain known predictions/ages from bounded timer only; `eligible()` spy never called. Empty/nonmember workspaces cause zero reads; N workspace reads/4 s plus 1 health read/s, concurrency/rate/deadline/index-row bounds. Reproduce old publish 0,2,4,… / read 1.5,5.5,… gap at 5.1; sweep all phases with 1 s reads, boundary scheduling/RPC/publication/skew delays and continuous between-read checks: ages stay <5 s within §5's 4.75 s budget. Successful RPCs with stale timestamps or excess scheduling delay still fail coverage; starvation, missing publisher, absent/stale/unhealthy/truncated data, restart/removal fail closed. Verify fleet budget and clock gate (§5; `src/trusted_router/config.py:977`, `src/trusted_router/services/async_settle.py:172`, `src/trusted_router/storage_gcp_async_admission.py:13`, `:78`). |
| Native storage | Emulator/integration transaction conflict tests for cap permits and sample uniqueness; exact indexed day bounds/pagination/cleanup; no body scans; unavailable storage cannot hold response. No new DDL. |
| Cost | Assert no added authorize RPC, nonmember shadow calls zero, no request-triggered cache refresh; measure §5 CPU/wall overhead including sign, independent oracle, maximum envelope and timer reads/row visits multiplied by instances; report region/build evidence. |

Use backend-class patches, not the module-global STORE proxy. All implementation
gates remain full `uv run ruff check .`, `uv run mypy`, `uv run pytest -q`,
coverage ≥70%, relevant conformance and mutation runs
(`docs/design/async-settle-outbox-v1.md:859`). New oracle baselines may name a
rebased revision, but must attach parent provenance/differential evidence rather
than absorb unexpected behavior into an exception list.

## 9. PR F2c — enclave implementation and cross-repo pins

Scope: exact `on` flag, optional opaque shadow binding/snapshot retention
separate from `Authorization.async`, pre-sanitization provider observations,
kind-dependent terminal Evaluate + ValidateEnvelope, bounded header on
settle/refund with failure variant, legacy-byte preservation. No pending
metadata or stream-order changes. Existing optional fields and binding are at
`E/enclave-go/internal/trustedrouter/client.go:477`, `:852`; evaluator and validation at
`E/enclave-go/internal/trustedrouter/async_settlement.go:157`, `:163`.

Pin off everywhere negotiation is pinned: AWS image
`E/enclave-go/Dockerfile.enclave:75`, GCP MIG metadata
`E/tools/deploy-gcp-mig.sh:566`, Azure measured environment
`E/tools/deploy-azure-aci.sh:565`. Add to all three GCP image
`tee.launch_policy.allow_env_override` lists:
`E/enclave-go/Dockerfile.enclave.gcp:43`,
`E/enclave-go/Dockerfile.enclave.gcp.anthropic:54`,
`E/enclave-go/Dockerfile.enclave.gcp.multi:93`. Keep NEGOTIATE off and preserve
existing keyring pins; shadow receiving an opaque proof does not require an
async acceptance keyring. Reviewed image/attestation rollout is still required.

| Test group | Required assertion |
|---|---|
| Off oracle | Both switches off, arbitrary optional wire metadata: frozen-main authorize/settle/refund bytes and all chat/Responses streaming frames unchanged. Extend `E/enclave-go/internal/trustedrouter/async_off_oracle_test.go:19`. |
| Header only | Shadow on/no ticket: authorize header present; no async binding; settle uses original body bytes, no mode header, one bounded shadow header. No async binding on both-flags-on or forged metadata. |
| Dual evaluator | Run all positive shared evaluator cases through shadow builder; same normalized terminal and charge as Python. Exercise unsupported/missing/estimated/overflow failures without hiding denominator. No evaluator changes or drift allowlist widening. |
| Provider observation through stream | Drive provider absent/default/standard/priority/flex/batch/scale/unknown/oversized tier and missing usage through actual chat and Responses stream-to-settle paths, with requested default and nondefault tiers. Assert pre-sanitization eligibility, unsupported failure envelope and dimensional denominator; legacy bodies/frames identical. Supplying a prebuilt Eligibility directly to the builder is insufficient (`E/enclave-go/cmd/enclave/main.go:2013`, `:2479`, `:2639`; §2.3). |
| Refund header / size | Exercise actual refund sender, preserve no-usage body bytes and usage_missing; independently pin positive-usage refund terminal with zero charge. Four-model full mode fits; all-43 uses hash-only with unchanged whole-snapshot Go evaluation. Omit only billing_snapshot on inline/outer full-mode overflow; retry bytes stable in both modes, failure variants keep mode, no candidate trimming. Local-work/proof/remaining outer overflow preserves legacy outcome and fails coverage (`E/enclave-go/internal/trustedrouter/client.go:1223`, `:1257`; §§2.2, 3.1). |
| Retries / failures | Identical body/header across retry; same authority; evaluator/encoding/size failure still sends legacy settle; no new retry or refund; no mutation of authorization shared with another request. |
| Streaming / metadata | All existing stream hooks stay legacy; client bytes, metadata location, completion and error/refund behavior match baseline for shadow on/off. No `trusted_router_settlement` introduced by shadow. |
| Deployment | Exact-value flag tests, boot/default precedence, every off pin and launch-policy list, maximum-header transport through the reviewed ingress. |

Shared billing fixture stays **759 cases**, SHA-256
`4aedf13e4ba30b4d1f0767f829e790c37ce3f957a39c15d1eb6aff8c8734fd81`, pinned at
`tests/test_billing_snapshot.py:21` and
`E/enclave-go/internal/billingv1/fixture_test.go:16`. Pin the new shadow fixture
bytes/hash (§2.3), canonical snapshot and terminal hashes, signing test public
key, decoded binding claims and expected integer 2 independently in both repos.
Add separate failure/changed-catalog/retry literals without changing existing
wire fixtures. Report exact SHA of the merged/rebased router and enclave and
attested artifact, not just PR numbers. A rebase is for the implementation
authors; this documentation pass makes no git writes.

## 10. Mutation gates

Each mutation must compile and reach a failing assertion; syntax/import failure
is not a kill. Keep the drift allowlist frozen; catalog explanation is a
specific checked arithmetic classification, never a new blanket tolerance.

| Mutation | Killing evidence |
|---|---|
| Book amount taken from envelope | Set Go charge to 999 for a legacy charge of 2; persisted money/generation must stay 2, response identical, comparator records disagreement. Exercise actual HTTP entry, not only pure helper. |
| Evidence written inside money transaction | Transaction trace rejects shadow kind in money read/write set; make evidence store fail and assert money still commits exactly once. |
| Shadow raises into response | Inject parser/signature/evaluator/worker submission failures after successful and failing legacy paths; assert original status/body/error and side effects unchanged. |
| Envelope accepted without hash check | Change snapshot rate and recompute terminal/self-hash while retaining old signed binding; must classify hash mismatch and reset. Also mutate only payload hash. In hash-only mode skip rebuilt-S0/binding equality: corrected catalog must kill the mutation with snapshot_reconstruction_failed and no clean sample (§2.2; `src/trusted_router/billing_snapshot.py:369`). |
| Opt-in check dropped | Valid and invalid shadow header on nonmember/empty set; spy asserts zero comparator/counter/key-read/evidence calls and exact baseline response. |
| Rate limit dropped | Burst beyond 10 at frozen time; assert only 10 comparator invocations/attempted sample writes and exact `rate_limit` remainder; advance clock to prove refill of 2/s. |
| Daily cap dropped | Two processes race final permit block; aggregate reservations ≤100,000; later attempts only count drops. |
| Catalog explanation reduced to unequal hashes | Wrong booked amount or altered unused candidate cannot pass explanation; independent L0 and exact B=R assertions fail (§3.1; `src/trusted_router/stage_d.py:161`). |
| Both new evaluators share a defect | Mutate Python and Go to truncate rather than round while preserving normalized usage. One input/output token at S0=500,000/500,000 gives corrupt P=G=1; corrected S1=1,000,000/1,000,000 gives R=B=2. Signed hashes/identities and B=R remain valid, but independent legacy L0=2 must force evaluator_disagreement, reset, and no explained count. Pin legacy oracle code unchanged; mutate the actual comparator inputs/execution in both implementation gates, not just an expected-value mock (`src/trusted_router/billing_snapshot.py:514`, `src/trusted_router/money.py:57`; §3.1). |
| Provider diagnostic captured after sanitization | Inject flex/batch/unknown into real provider stream; unchanged legacy body may retain default, but observed eligibility must stay unsupported and the report denominator must include it (`E/enclave-go/cmd/enclave/main.go:2639`; §2.3). |
| Restart clears mismatch / missing counters treated as zero | Report over restart with old durable mismatch or unclosed writer cannot print seven-day PASS. |
| Shadow binds async / changes body | No-ticket fixture plus both flags on: exact legacy body/frame oracle and zero async calls must fail the mutation. |

## 11. Landing, enablement and non-goals

Land **router F2b first**, tolerating/ignoring the header while off; land
**enclave F2c second**, shadow and negotiation pinned off. Reviewed enablement:
validate §5's fleet read/headroom and combined freshness budgets, provision
signer/epoch prerequisites with admission still disabled; router
workspace opt-in → enclave shadow on → first durable sample satisfying §6's
positive evaluability/coverage predicate, after §2.2's pilot authorization-size
measurements and actual-hop maximum-header acceptance gate, starts the clock (parent §8 at
`docs/design/async-settle-outbox-v1.md:811`). Confirm all serving revisions/pins,
observer/health progress and evidence writes; queued or partial
rollout is not enabled. Disabling shadow removes collection and preserves sync.
Do not alter accepted-work protection during shadow rollback; the independent
protection/admission rule remains (`docs/design/async-settle-outbox-v1.md:952`).

Non-goals: no new pending intent, finalize call, settlement ticket, async
admission, status endpoint, billing/settle-body change, DDL, operational outbox
event, money transaction instrumentation writes, price change, widened drift
allowlist, prompt/output storage, receipts, revised retry policy, new stream
metadata, or production deployment in this pass. A shadow binding is a
non-authorizing signature, not a renamed settlement capability.

## 12. Open items and limits of verification

For Joseph:

1. Pilot workspace/key and expected concurrency; the appendix deliberately
   supplies no workspace ID. Retain the parent exposure/side-effect policy
   questions (`docs/design/async-settle-outbox-v1.md:869`, `:879`).
2. May the seven-day clock overlap **PR G code landing dormant**, with actual
   async admission still disabled everywhere? Default here: landing alone is
   not activation, but count overlap only after that policy is confirmed and
   serving revisions/pins remain in the evidence manifest.

Answered 2026-10-09 (parent §10 "Decisions"): the pilot is workspace
`45819281-0ce9-4811-a0cd-c660ab3a116d`; the seven-day clock may overlap PR G
landing dormant under the conditions stated there.

Unverified by this source-only pass: production serving revision and real log
delivery; pilot identity/load; mounted signing material/epoch; maximum-header
ingress behavior; complete instance/traffic roster availability; all
CPU/latency/capacity numbers; 759-case execution in both
repos for the eventual F2 revisions; Spanner concurrency/load/crash results;
all-cache D3 proof; seven days of traffic, exposure-policy approval and dormant
PR G overlap. Proposed limits are engineering gates, not measured results.
In round 2, all enclave file:line citations were re-read against the HEAD in
the source-pin table, including evaluator/validation at `E/enclave-go/internal/trustedrouter/async_settlement.go:157`,
`:163` and deadline at `:216`; no enclave citation remains unverified. The
untracked validation note is corroborative only. Dependency-default inspection
does not establish deployed ingress limits (§2.2). The v1 full/hash-only transport
and limits are decided; actual pilot size distributions, maximum-header hop
acceptance and exact hash-only S0 recovery remain unverified enablement/clock
gates (`src/trusted_router/routes/internal/gateway.py:2782`,
`src/trusted_router/billing_snapshot.py:369`). Fleet read-budget acceptability
and §5's combined freshness maxima are also unverified; missing timer/health evidence remains an
F2b-owned prediction blocker (§5), not a policy question for Joseph.
The current source pins are local worktree observations, not confirmation that
PR E has merged or deployed.

Requirements infeasible **without the qualifications above**: signed inputs
from PR B's unsigned flag-off projection alone; absolute zero fleet-wide
non-opted cost while adding a negotiation header and preserving PR B bytes;
lossless denominators/drop counts across crashes with only best-effort
post-commit writes; end-to-end reply timing inside the request being measured;
and proof of async D2/drain/D3 from synchronous shadow traffic. The separate
binding, explicit baseline cost, conservative unknown-coverage handling,
honest timing definitions and external proof gates address these without
changing the money path. No passing production claim is made by this appendix.
