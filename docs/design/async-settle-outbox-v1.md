# Async settle on the Spanner settle outbox, v1

Status: **design for flag-off implementation, 2026-10-06**. Joseph approved the
direction on 2026-10-06: “go, start the design doc and the contract PRs.” This
pass changes documentation only; it does not activate async settlement.

This replaces the 2026-10-05 input sketch and its Bigtable-journal proposal.
The September 27/29 decisions carried forward from that sketch are D1 (explicit
`X-TR-Settlement-Mode: async-v1` opt-in and `trusted_router_settlement`), D2
(2 s completion budget, at most 500 ms durable handoff), D3 (at most 300 s
revocation delay), exact local cost, and $5/$25/$100 admission thresholds with
synchronous fallback. Those decisions are supplied design inputs, not claims
that current main implements them. Corrections to the sketch are called out below.

Source pins inspected, without fetching or modifying branches:

| Source | Revision / evidence |
|---|---|
| Router main in this worktree | `420b99526aa57c0c5734881cb053c9c5d4840baf` |
| Router #1390 | `origin/async-settle/pr1-billing-v1`, `7f64630bacb0545357976a21072ccbe225c1d55f`; read with `git show`, especially `docs/async-settlement-billing-v1.md`, `src/trusted_router/billing_snapshot.py`, and `tests/fixtures/async_settlement/billing_v1{,.rules,.schema}.json` |
| quill-cloud-proxy #393 | locally available `origin/async-settle/pr2-go-evaluator`, `f1c5cfeb43f0e1138820f8e08cc85c5a65bd976b`; `enclave-go/internal/billingv1/{README.md,types.go,evaluate.go,validate.go,canonical.go}`, optional metadata in `internal/trustedrouter/client.go` |
| Live outbox contract | [durable-settle-outbox.md](durable-settle-outbox.md), especially MF4/MF5, §7 one-commit addendum; current code takes precedence over its historical “increments remain” header |

Throughout, router source paths are relative to `src/trusted_router/`. New
symbols, columns, metrics and fixtures below are **proposed extensions**, not
names of already shipped APIs. Neither contract PR activates a request path.

## 1. Problem and measured baseline

`routes/internal/gateway.py::_settle_gateway_authorization` reads authorization,
normalizes actual usage, prices it, and invokes typed finalize. The one-commit
happy path in `storage_gcp_authorize.py::typed_finalize_atomic` reads the
reservation and sends a Batch DML containing the claim, authorization finalize,
resolved intent, generation/activity, benchmark and credit/key releases, then
commits. Releases are last, credit before key. There are **four sequential
Spanner operations**, of which **one is a read-write commit**. Fallbacks cost more.

The sketch reports measurements from **2026-10-05 07:03–07:33Z**:

| Router region | Settle p50 (ms) | Interpretation |
|---|---:|---|
| `us-central1` | 103 | Post-invoke overhead ≈ settle |
| `us-west1` | 142 | Same |
| `us-east4` | 272 | Same |
| `europe-west4` | 703 | Same |

These are preserved measurements from the supplied sketch. No raw trace export
for that window was found in the inspected repository; they are not a new
benchmark or a fleet-wide percentile claim. Code supports the operation shape;
it does not prove the regional timing attribution.

Async settlement moves finalize off the response path after one durable intent
commit. It does **not** eliminate the finalization work, reservation locks or
counter updates. It removes waiting for them from successful async responses.

## 2. Goals / non-goals

Goals:

- Commit responsibility for exact actual usage before the final response byte;
  return the exact amount, explicitly `pending`, until booking is confirmed.
- Reuse the native Spanner outbox, frozen amount, first-writer reservation claim,
  lease fence, reaper interlock and retention. No second settlement queue.
- Preserve existing behavior with the flag off, absent opt-in, or an unsupported
  request. D2 includes verification, handoff and fallback in one completion
  budget; timeouts cannot manufacture durable acceptance.
- Keep admission and revocation synchronous; observe drain health and bound
  additional admission using the tier policy, with its limitations in §6.

Non-goals: changing money arithmetic; changing authorize's reservation,
identity, routing or billing semantics; speculative invocation; Bigtable
journaling; receipts in v1; batching multiple customers' money into one
transaction. Authorize **does gain response metadata and an eligibility read**;
“unchanged authorize” does not mean zero new CPU or zero cache-miss I/O.

The exactly-priceable cohort is the intersection of #1390's `Eligibility`,
`Candidate`, `build_snapshot`, `evaluate` and async operational eligibility:
**typed, local, ordinary Credits; OpenAI/Anthropic adapters; chat.completions
and responses, streamed or non-streamed; exact final usage**. Ordinary catalog
markup already embedded in effective rates, cache read/write, reasoning as a
subset of output, and ordinary context tiers are supported.

Exclude payouts, partner/Liberty billing, BYOK/non-Credits, video/image/tool/
search costs, native batch, custom/user models, app/custom markups, receipts
(including receipt requests even when their fee is zero), request fees,
nondefault service-tier pricing, private tier bases, fusion/Polyphemus, unusual
or unknown pricing programs, untyped reservations, spend/regional leases and
federated/deferred-home settlement. Also exclude estimated/missing usage,
malformed counts, overflow, unsupported adapters/routes and unknown features.
`/decide` is **not** in #1390's route enum; it remains synchronous pending §10.
Claims that this is “the bulk of traffic” require a shadow denominator.

## 3. Wire contract

### 3.1 Authorize and signed bindings

PR B builds the detached snapshot from **effective customer prices** after
successful authorize. `billing_snapshot` is the unchanged v1 DTO;
`billing_snapshot_hash = canonical_hash(snapshot)`. `settlement_ticket` is the
router's Ed25519 signature over the snapshot hash **and authorization context**;
it is not a signature from the enclave. `async_eligible` is an admission hint,
not authority to bypass authentication or book an arbitrary charge.

Use the Ed25519/canonical-JSON construction pattern in
`services/speculation_shadow.py::ShadowSigner.sign` and verification primitives
in `speculation_protocol.py::_verify`. Extend with a separate
`typ=tr-async-settle-v1`, key purpose and claims validator. Do **not** reuse a
shadow grant as a settlement ticket or call its grant validator unchanged.
Signing keys and verification keyrings load outside the request path.

The ticket binds authorization, reservation, deterministic generation ID
(`storage_models.py::generation_id_for_authorization`), workspace, key ID,
invocation nonce, typed origin, local authority, region/epoch, route/streamed,
v1 snapshot hash, eligibility and validity interval. `journal_region`/`epoch`
retain #1390's envelope vocabulary; they identify local settlement authority,
not a Bigtable journal or permission to bill in another cloud. Fresh acceptance
requires current trusted issuer/epoch and an unexpired ticket. Expired tickets
can identify an existing intent for authenticated retry; they cannot create one.
Long invocations whose tickets expire use snapshot-bearing synchronous settle.

The following is the **literal addition projection** of an authorize response;
existing response fields remain as defined by `GatewayAuthorizeResponse` in
`schemas.py`. The numeric example is #1390's `component_half_up`: one input and
one output token at 500,000 microdollars/million independently round to **2**.
The ticket is a real test signature (public test seed = 32 bytes `01`, never a
production key), verified at `1791244801`. Public key, base64url: `iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w`.
Compact JWS uses sorted compact ASCII JSON and unpadded base64url; claims are
literal below so implementations can verify bindings, not just price hashes.

```json
{
  "authorization_id": "auth-v1",
  "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
  "workspace_id": "ws-v1",
  "key_id": "key-v1",
  "invocation_nonce": "nonce-v1",
  "billing_authority": "local",
  "journal_region": "us-central1",
  "epoch": 1,
  "snapshot_version": 1,
  "snapshot_hash": "39526c7fde5f32d133358b9e61ef0fff17e006f6c7f87fc5ec6dd211206c50ee",
  "route_type": "chat.completions",
  "streamed": false,
  "reservation_id": "res-v1",
  "settle_origin": "typed",
  "async_eligible": true,
  "iss": "router-fixture",
  "aud": "router-settlement",
  "iat": 1791244800,
  "exp": 1791245100
}
```

```json
{
  "data": {
    "authorization_id": "auth-v1",
    "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
    "billing_snapshot": {
      "v": 1,
      "kind": "credits_endpoint",
      "candidates": [
        {
          "endpoint_id": "openai/billing-v1@openai/prepaid",
          "model_id": "openai/billing-v1",
          "provider": "openai",
          "usage_type": "Credits",
          "price_history_version": 1,
          "rates": {
            "input_micro_per_million": 500000,
            "output_micro_per_million": 500000,
            "cached_input_micro_per_million": 0,
            "cache_creation_micro_per_million": 0
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
    "billing_snapshot_hash": "39526c7fde5f32d133358b9e61ef0fff17e006f6c7f87fc5ec6dd211206c50ee",
    "settlement_ticket": "eyJhbGciOiJFZERTQSIsImtpZCI6ImFzeW5jLXYxLWZpeHR1cmUiLCJ0eXAiOiJ0ci1hc3luYy1zZXR0bGUtdjEifQ.eyJhc3luY19lbGlnaWJsZSI6dHJ1ZSwiYXVkIjoicm91dGVyLXNldHRsZW1lbnQiLCJhdXRob3JpemF0aW9uX2lkIjoiYXV0aC12MSIsImJpbGxpbmdfYXV0aG9yaXR5IjoibG9jYWwiLCJlcG9jaCI6MSwiZXhwIjoxNzkxMjQ1MTAwLCJnZW5lcmF0aW9uX2lkIjoiZ2VuLWM3YTczNDk4ZGQ4YTVkNTlhNzA1ZjQ4MjA3MGM5ZTU2IiwiaWF0IjoxNzkxMjQ0ODAwLCJpbnZvY2F0aW9uX25vbmNlIjoibm9uY2UtdjEiLCJpc3MiOiJyb3V0ZXItZml4dHVyZSIsImpvdXJuYWxfcmVnaW9uIjoidXMtY2VudHJhbDEiLCJrZXlfaWQiOiJrZXktdjEiLCJyZXNlcnZhdGlvbl9pZCI6InJlcy12MSIsInJvdXRlX3R5cGUiOiJjaGF0LmNvbXBsZXRpb25zIiwic2V0dGxlX29yaWdpbiI6InR5cGVkIiwic25hcHNob3RfaGFzaCI6IjM5NTI2YzdmZGU1ZjMyZDEzMzM1OGI5ZTYxZWYwZmZmMTdlMDA2ZjZjN2Y4N2ZjNWVjNmRkMjExMjA2YzUwZWUiLCJzbmFwc2hvdF92ZXJzaW9uIjoxLCJzdHJlYW1lZCI6ZmFsc2UsIndvcmtzcGFjZV9pZCI6IndzLXYxIn0.ghFdyi8nXmEDGVemPaaExEoR7zKg01E7Nw0MCtOZgSKDD3k3tdDYR9E-sFAtA2vH19pkfUPm3ws05k9HDbuLCA",
    "async_eligible": true,
    "settlement_status_url": "/v1/settlements/auth-v1.settle"
  }
}
```

Unsupported snapshot construction omits snapshot/ticket and sets
`async_eligible=false`; it must not fail an otherwise valid synchronous
authorize. #393 already stores optional `BillingSnapshot`, `SettlementTicket`,
`GenerationID`, `SettlementMode`, `SettlementStatusURL` without enabling async;
PR E adds the eligibility/hash parsing and actual acceptance behavior.
The contract's `SettlementMode` values remain `sync|async`; `async-v1` is the
HTTP negotiation value, not a new value silently added to that DTO.

### 3.2 Async settle request and verification

Existing logical route: `POST /internal/gateway/settle` (the deployment mounts
internal routes under `/v1` too; use the same base URL convention as today's
client). Preserve `require_internal_gateway` authentication. Opt-in requires:

```http
X-TR-Settlement-Mode: async-v1
Content-Type: application/json
```

Full new request body, rather than the lenient legacy `GatewaySettleRequest`:

```json
{
  "billing_snapshot": {
    "v": 1,
    "kind": "credits_endpoint",
    "candidates": [
      {
        "endpoint_id": "openai/billing-v1@openai/prepaid",
        "model_id": "openai/billing-v1",
        "provider": "openai",
        "usage_type": "Credits",
        "price_history_version": 1,
        "rates": {
          "input_micro_per_million": 500000,
          "output_micro_per_million": 500000,
          "cached_input_micro_per_million": 0,
          "cache_creation_micro_per_million": 0
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
  "settlement_ticket": "eyJhbGciOiJFZERTQSIsImtpZCI6ImFzeW5jLXYxLWZpeHR1cmUiLCJ0eXAiOiJ0ci1hc3luYy1zZXR0bGUtdjEifQ.eyJhc3luY19lbGlnaWJsZSI6dHJ1ZSwiYXVkIjoicm91dGVyLXNldHRsZW1lbnQiLCJhdXRob3JpemF0aW9uX2lkIjoiYXV0aC12MSIsImJpbGxpbmdfYXV0aG9yaXR5IjoibG9jYWwiLCJlcG9jaCI6MSwiZXhwIjoxNzkxMjQ1MTAwLCJnZW5lcmF0aW9uX2lkIjoiZ2VuLWM3YTczNDk4ZGQ4YTVkNTlhNzA1ZjQ4MjA3MGM5ZTU2IiwiaWF0IjoxNzkxMjQ0ODAwLCJpbnZvY2F0aW9uX25vbmNlIjoibm9uY2UtdjEiLCJpc3MiOiJyb3V0ZXItZml4dHVyZSIsImpvdXJuYWxfcmVnaW9uIjoidXMtY2VudHJhbDEiLCJrZXlfaWQiOiJrZXktdjEiLCJyZXNlcnZhdGlvbl9pZCI6InJlcy12MSIsInJvdXRlX3R5cGUiOiJjaGF0LmNvbXBsZXRpb25zIiwic2V0dGxlX29yaWdpbiI6InR5cGVkIiwic25hcHNob3RfaGFzaCI6IjM5NTI2YzdmZGU1ZjMyZDEzMzM1OGI5ZTYxZWYwZmZmMTdlMDA2ZjZjN2Y4N2ZjNWVjNmRkMjExMjA2YzUwZWUiLCJzbmFwc2hvdF92ZXJzaW9uIjoxLCJzdHJlYW1lZCI6ZmFsc2UsIndvcmtzcGFjZV9pZCI6IndzLXYxIn0.ghFdyi8nXmEDGVemPaaExEoR7zKg01E7Nw0MCtOZgSKDD3k3tdDYR9E-sFAtA2vH19pkfUPm3ws05k9HDbuLCA",
  "raw_usage": {
    "input_tokens": 1,
    "output_tokens": 1,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "reasoning_tokens": 0
  },
  "observed": {},
  "terminal": {
    "v": 1,
    "authorization_id": "auth-v1",
    "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
    "workspace_id": "ws-v1",
    "key_id": "key-v1",
    "invocation_nonce": "nonce-v1",
    "billing_authority": "local",
    "journal_region": "us-central1",
    "epoch": 1,
    "snapshot_version": 1,
    "terminal_kind": "settle",
    "route_type": "chat.completions",
    "streamed": false,
    "selected_endpoint": "openai/billing-v1@openai/prepaid",
    "snapshot_hash": "39526c7fde5f32d133358b9e61ef0fff17e006f6c7f87fc5ec6dd211206c50ee",
    "usage": {
      "uncached_input_tokens": 1,
      "total_prompt_tokens": 1,
      "output_tokens": 1,
      "cache_read_tokens": 0,
      "cache_creation_tokens": 0,
      "reasoning_tokens": 0
    },
    "charge_micro": 2
  }
}
```

`terminal` is the **unchanged #1390 `TerminalEnvelope`**, including
`snapshot_hash`, normalized exact usage and `charge_micro`. `raw_usage` is the
v1 `RawUsage`; `observed` is `Eligibility` (the empty fixture means its declared
ordinary defaults, not permission for production to omit discovered features).
The integration must populate requested facts at authorize and observed facts
from attested adapter execution. No prompt, response text, credentials or
arbitrary metadata enters this body or the outbox.

Strictly decode the whole outer body (BOM-free UTF-8, no duplicate keys at any
depth, no floats/bools/strings for integers, no unknown fields). Use #1390's
supported parsing APIs for each DTO; do not pass the outer object through an
unsupported Pydantic JSON nesting path that bypasses `strict_json_loads`.
Verify ticket signature/purpose/issuer/audience/expiry/authority and every
identity binding. Compute canonical snapshot digest, check candidate membership,
normalize raw usage once, call `evaluate`, compare normalized usage and exact
charge with `terminal`, then `validate_envelope`. Mismatch is an error, never
permission to accept the enclave's number. There are no catalog reads here.

V1 arithmetic is unchanged: checked int64, half-up per component, positive
one-micro minimum, inclusive total-prompt tiers and **last-tier fallback**.
`charge_cap:null` remains null; admission caps never clamp money. The Go
`billingv1.Evaluate` and Python evaluator consume the same fixture bytes,
currently SHA-256 `4aedf13e4ba30b4d1f0767f829e790c37ce3f957a39c15d1eb6aff8c8734fd81`
(759 cases). The sketch's 93,561 zero-disagreement probes are not substantiated
by the inspected branch README/artifacts; retain the executable fixture gate,
and attach that probe report before citing it as release evidence.

### 3.3 Responses and cross-repo literals

Proposed **HTTP 202**, only after the acceptance transaction commits:

```json
{
  "data": {
    "acceptance": {
      "status": "accepted",
      "payload_hash": "9965b739ebab44a1ab6ec7a947ce66cbf764da5dc11d5dd2d49f77f1e9d02386",
      "settlement_status": "pending"
    },
    "trusted_router_settlement": {
      "v": 1,
      "settlement_id": "auth-v1.settle",
      "settlement_status": "pending",
      "cost_microdollars": 2,
      "status_url": "/v1/settlements/auth-v1.settle",
      "poll_after_ms": 1000
    }
  }
}
```

The enclave copies the following object to the **top-level** client response;
it does not nest it ambiguously inside `trusted_router.routing`. For streaming,
PR E emits this object in the final metadata event before completion/`[DONE]`.
Existing routing metadata keeps its current location. No signed receipt is
issued for this object.

```json
{
  "trusted_router_settlement": {
    "v": 1,
    "settlement_id": "auth-v1.settle",
    "settlement_status": "pending",
    "cost_microdollars": 2,
    "status_url": "/v1/settlements/auth-v1.settle",
    "poll_after_ms": 1000
  }
}
```

`cost_microdollars` is the exact accepted frozen amount, **not proof that the
ledger is settled**. The enclave verifies it and the acceptance payload hash
against its terminal envelope before emitting the object. A pending acceptance
must not masquerade as today's committed settlement response.

Proposed **HTTP 200** when valid inputs are not admitted to async and **no new
intent has been accepted** (flag off, unsupported cohort, cap, expired ticket
for fresh insertion, unavailable/stale admission data, drain unhealthy):

```json
{
  "data": {
    "acceptance": {
      "status": "sync_required",
      "payload_hash": null,
      "settlement_status": null
    },
    "reason": "drain_unhealthy"
  }
}
```

Other bounded `reason` values: `disabled`, `not_eligible`, `cap_exceeded`,
`ticket_expired`, `unsupported_cohort`, `admission_stale`,
`reservation_not_open`. PR C pins each literal. Retry synchronously within the
remaining D2 budget using the **same snapshot/usage**; for a priceable request,
sync fallback must book the same frozen evaluator amount despite catalog
changes. That is new route wiring, explicitly not supplied by PR #1390. An
excluded feature requiring additional pricing uses the legacy full pricing
path and must never expose a prematurely claimed exact v1 total.

Literal error fixture in the style of
`tests/fixtures/speculation_v1/authorize-error-envelopes.json`:

```json
{
  "fixture_version": 1,
  "cases": {
    "invalid_snapshot": {
      "path": "/internal/gateway/settle",
      "status": 400,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":400,\"message\":\"Invalid async settlement snapshot\",\"type\":\"bad_request\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "invalid_signature": {
      "path": "/internal/gateway/settle",
      "status": 401,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":401,\"message\":\"Invalid settlement ticket\",\"type\":\"unauthorized\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "charge_mismatch": {
      "path": "/internal/gateway/settle",
      "status": 409,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":409,\"message\":\"Async settlement amount mismatch\",\"type\":\"conflict\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "payload_conflict": {
      "path": "/internal/gateway/settle",
      "status": 409,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":409,\"message\":\"Settlement intent already exists with a different payload\",\"type\":\"conflict\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "storage_unavailable": {
      "path": "/internal/gateway/settle",
      "status": 503,
      "content_type": "application/json",
      "retry_after": "1",
      "body_exact": "{\"error\":{\"code\":503,\"message\":\"Persistent storage is temporarily unavailable; retry.\",\"type\":\"service_unavailable\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "not_found": {
      "path": "/v1/settlements/auth-v1.settle",
      "status": 404,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":404,\"message\":\"Settlement not found\",\"type\":\"not_found\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    }
  }
}
```

The error types reuse `types.py::ErrorType`; messages other than the copied
storage-unavailable envelope are proposed async literals. Invalid input/signature/hash/charge fails closed;
`sync_required` is not a catch-all for corrupt data. Errors never acknowledge
`pending`. Timing values above are frozen test values; only timing is normalized
in dynamic tests. A 503 or disconnected response after commit is **unknown
outcome**, not `sync_required` and not proof of absence.

PRs B/C/E copy these literals into a versioned `async_settlement` wire fixture
in both repositories and pin exact bytes/hash as the speculation fixture does.
They add literal variants for duplicate pending/done, refund races, expiry,
wrong identity, disabled mode and all reasons. Do not generate expected money
with the evaluator under test. The present pass creates no fixture files.

### 3.4 Identity, duplicates and status

`settlement_id` is the reversible pair `<authorization_id>.<intent_kind>`;
split at the final dot and allow only `settle|refund`; URL-encode the path
component. Preserve native ID column bounds even though the pure v1 DTO permits
longer identities. It is an identifier, not
a bearer capability. This preserves the existing PRIMARY KEY
**`(authorization_id, intent_kind)`**, not authorization ID alone.

**ALREADY_EXISTS on the authorization's same-kind intent → return the existing
intent**, never create a second ID or charge. `SpannerSettleOutbox.enqueue`
already has `preserve_existing=True`; use that path and `get(aid, kind)` rather
than refresh-latest. Exact duplicate pending returns 202 with acceptance status
`duplicate` and the original hash/amount/ID; resolved duplicates return 200
with the current settlement object. Conflicting payload returns 409 with no
mutation. Persist the canonical terminal payload hash independently of
`settle_body`, which is cleared on done. A legacy row lacking that hash needs
bounded persisted-state reconciliation, not overwrite or guessed equality.

New `GET /v1/settlements/{id}` uses normal caller authentication, workspace
ownership authorization and complete primary-key reads; cross-workspace and
unknown IDs both return the literal 404. It never scans JSON bodies or treats
knowledge of the ID as access. Proposed successful responses:

```json
{
  "pending": {
    "data": {
      "trusted_router_settlement": {
        "v": 1,
        "settlement_id": "auth-v1.settle",
        "settlement_status": "pending",
        "cost_microdollars": 2,
        "status_url": "/v1/settlements/auth-v1.settle",
        "poll_after_ms": 1000
      }
    }
  },
  "settled": {
    "data": {
      "trusted_router_settlement": {
        "v": 1,
        "settlement_id": "auth-v1.settle",
        "settlement_status": "settled",
        "cost_microdollars": 2,
        "status_url": "/v1/settlements/auth-v1.settle",
        "poll_after_ms": null
      }
    }
  },
  "refunded": {
    "data": {
      "trusted_router_settlement": {
        "v": 1,
        "settlement_id": "auth-v1.refund",
        "settlement_status": "refunded",
        "cost_microdollars": 0,
        "status_url": "/v1/settlements/auth-v1.refund",
        "poll_after_ms": null
      }
    }
  }
}
```

The JSON above is a fixture map of three separate HTTP 200 response bodies.
Pending polls use 1,000 ms; terminal responses use `poll_after_ms:null`.
Status reads the reservation/authorization winner, not merely outbox `done`:
`done` can mean another intent won, and `pending` can mean money already booked
but repair/mark remains. Report the confirmed winning amount, refund only when
zero-charge refund actually won, and pending until that can be established.
A dead/unapplied intent stays pending with operator alerting, never falsely
settled; 60-second breaches remain visible. A later refund does not undo an
already committed charge through this API. Terminal records follow the existing
30-day retention window; after purge, 404 carries no claim about past billing.

## 4. Router request path

Dispatch the new mode before the legacy lenient parser/full settle handler;
absent opt-in continues `_settle_gateway_authorization` unchanged. Do not
perform its authorization/catalog read and then claim a read-free async path.
Signed authorization context supplies request identity, prices and reservation
reference; the transaction below checks the still-open reservation atomically.

| Step | Operation | Durable effect |
|---|---|---|
| 1 | Authenticate; strict parse; verify ticket/hash/bindings and admission freshness | None |
| 2 | Python v1 `evaluate` + `validate_envelope`; compare exact usage/amount | None; pure CPU |
| 3 | One Batch DML, one transaction, one commit, tag **`tr_async_settle_enqueue`** (new; refund `tr_async_refund_enqueue`) | Pending intent and repair-retention interlocks only |
| 4 | Return 202 after confirmed commit | Router owns drain responsibility |
| Replay | Duplicate INSERT / unknown response → retry same key and point-read existing row | Existing intent wins; no new charge |

Reuse `storage_gcp_settle_outbox.py::intent_insert_statements(...,
resolved=False, next_attempt_at=now)` and `intent_insert_counts`. Its exact
statement sequence today is:

1. `INSERT INTO tr_settle_outbox (<INSERT_COLUMNS>) VALUES (<binds>)`.
2. `storage_gcp_request_records.py::gateway_authorization_retention_clear_statement`:
   `UPDATE tr_gateway_authorization SET terminal_at=NULL WHERE authorization_id=@authorization_id AND terminal_at IS NOT NULL`.
3. `storage_gcp_counter_dml.py::reservation_retention_clear_statement`:
   `UPDATE tr_reservation SET terminal_at=NULL WHERE reservation_id=@rid AND terminal_at IS NOT NULL`.

Keep all three. Insert has row-count 1; retention updates allow 0 or 1. Set
`status=pending`, `attempts=0`, `next_attempt_at=now`, `terminal_at=NULL`, null
lease fields, typed origin, Credits, exact `actual_cost_micro`, reservation,
endpoint/model, content-free repair usage. Preserve generated `queue_shard`
and `tr_settle_outbox_due_v2`; do not insert the ordinary inline 60 s delay.

**Required extension for truthful acceptance:** append a conditional admission
statement in the same transaction, proposed `async_reservation_admission_statement`:

```sql
UPDATE tr_reservation SET terminal_at=NULL
WHERE reservation_id=@rid AND authorization_id=@aid AND settled=false
```

Require row-count **exactly 1**, otherwise roll back the whole transaction and
resolve the existing winner through bounded reads/sync handling. This preserves
the original builder/retention statements and adds no pre-transaction read.
It takes a reservation lock, not a credit/key lock. The existing reaper guard
prevents release **after** an intent exists; it does not revive a reservation
already reaped before insertion. A blind INSERT alone can acknowledge an
unbookable charge. The atomic predicate closes that hole and also handles a
concurrent synchronous winner. It must be modeled by the fake and proven on
Spanner; a timestamp check in Python is insufficient.

Extend the native row additively with bounded `async_version`, `workspace_id`,
`snapshot_hash`, and `payload_hash` fields, retained after `settle_body` is
cleared. This supports ownership, immutable retry and indexed admission. Add a
sparse workspace pending/dead index for §6; maintain its membership in the
existing intent insert/mark transactions. Do not introduce a counter row.
The typed repair body maps normalized/raw v1 counts to today's
`GatewaySettleRequest` fields without normalizing twice. Store the digest as
evidence; drain does not require the catalog or signature to reprice.

The commit must **not** claim/book the reservation, set authorization settled,
write generation/activity/benchmark rows, release holds, update `tr_credit_balance`
or `tr_key_limit`, or call external side effects. Retention-only authorization/
reservation writes are intentional and already part of enqueue. Thus “one
INSERT commit” means **one commit containing the intent INSERT**, not literally
one statement or one RPC.

Use `storage_gcp_io.py::run_in_transaction_with_retry` with the new stable tag
and a remaining handoff budget ≤500 ms (including retries); existing defaults
are not D2. `enqueue` needs a budget/tag option or a narrow wrapper around its
same statement builders and preserve-existing behavior. Close/rollback failed
attempts before retry/classification as the one-commit finalize path does;
do not leave timed-out transactions holding locks.

| Region | Sync happy-path ops / commits | Async happy-path ops / commits | Measured sync p50 ms | Sketch async estimate ms |
|---|---|---|---:|---:|
| us-central1 | 4 / 1 | 2 / 1 | 103 | ~35 |
| us-west1 | 4 / 1 | 2 / 1 | 142 | ~45 |
| us-east4 | 4 / 1 | 2 / 1 | 272 | ~60 |
| europe-west4 | 4 / 1 | 2 / 1 | 703 | ~140 |

Async's two operations are **Batch DML + commit** with transaction begin
piggybacked where supported, not the sketch's single commit RPC. Duplicate
reads, admission-cache misses, begin-session behavior and retries add work;
measure actual `spanner_rpcs`. These estimates were based on the sketch's
single-RPC assumption and are **unvalidated targets**, not predicted results
for this DML design. A mutations-only commit would be a separate storage proof
and must not drop retention or the admission interlock to achieve a metric.

The existing synchronous one-commit path stays unchanged:
`typed_finalize_atomic(settle_outbox_intent=...)` →
`resolved_intent_statements` → `intent_insert_statements(resolved=True)` plus
`done_retention_statements`, in tag `tr_settle_one_commit`/
`tr_refund_one_commit`. It creates a **done** row, attempts=1, no due time,
cleared body, together with booking. Async enqueue must not invoke that mode.

## 5. Drain

Reuse these entry points and correctness primitives:

| Component | Reused unchanged | Extension |
|---|---|---|
| `gateway_settle_outbox_drain`, `/internal/gateway/settle-outbox/drain` | Internal authentication; `drain_settle_outbox` entry point | Continuous/frequent worker scheduling and health reporting |
| `SpannerSettleOutbox.due`, `claim`, `_claim_one` | Forced sharded sparse due index; conditional owner/expiry lease claim | Bounded worker concurrency and small claim batches; tune budget/lease |
| `services/settle_outbox_apply.py::apply_frozen_settle`, `_apply_typed` | Stored typed origin; exact frozen amount; rich outcomes; no full HTTP handler replay | Additional async metadata must not affect pricing or money |
| `typed_store.typed_finalize_gateway` → `typed_finalize_atomic` | Claim → authorization/generation/activity → credit release → key release; `tr_finalize`/`tr_refund_finalize` | No money algorithm changes |
| `services/settle_outbox_drain.py::_resolve_row`, `SpannerSettleOutbox.mark`, `park` | Lease-fenced done/dead/retry/park; repair retention | Maintain additive admission-index membership and metrics |
| `settle_atomic`, `reap_expired_reservations` in `storage_gcp_authorize.py` | In-transaction pending/dead guard and reservation first-writer claim | No weakening; async insertion adds the complementary §4 check |
| `purge_done`, retention helpers | Done retention 30 days; pending/dead/release_approved never auto-purged | Keep compact async identity/hash through same window |

There is **no `leased` status**: claimed rows remain pending with `lease_owner`
and `leased_until`. A worker applies then resolves with its owner fence. If it
crashes after booking but before `mark`, replay disambiguates the reservation and
marks without charging twice. `mark_done_unleased_tx` cannot resolve a
worker-owned row; the drain owns its final mark. `settled_now` and confirmed
already-charged outcomes can finish; released-free nonzero charge, missing
reservation or exhausted attempts must remain visible as failures. Preserve
parking for typed unavailability and activity repair, deterministic-error
handling and manual `release_approved` semantics.

**MF5: frozen amount wins.** Neither deploys, changed catalog prices, endpoint
removal nor a later evaluator disagreement can rewrite `actual_cost_micro`.
Optional diagnostic comparison uses captured inputs outside the apply primitive;
it alerts on mismatch, never reprices an accepted intent. The first-writer
reservation gate, not the lease alone, prevents double booking. Competing
settle/refund intents retain the existing winner semantics.

The current worker is a recovery mechanism: `docs/runbook.md#settle-outbox`
describes a **five-minute** scheduler; `drain_settle_outbox` loops sequentially,
clamps limit to 500, budgets 240 s and leases for 300 s. It does not batch
credit/key releases across requests. Scaling means more bounded independent
apply work, not new money batching. Retain a watchdog schedule, deploy warm
workers, poll at subsecond/one-second cadence, claim only work that can start
promptly and distribute scans across the existing 16 queue shards. Tune leases
and pass budgets together; preserve fencing and avoid claiming a batch whose
tail waits for lease expiration. Keep purge/reap housekeeping at its existing
bounded cadence rather than multiplying its load on every fast poll.

Proposed SLO: **95% finalized ≤5 s, 100% ≤60 s** from acceptance commit to
confirmed booking and durable repair completion. Measure both booking and
outbox completion separately so a delayed mark is visible. “100%” is a proposed
operating objective/alert threshold, not a guarantee through a database outage;
today's 300 s lease alone contradicts it. PR D must demonstrate crash reclaim
within the target or obtain Joseph's revised target before enablement.

Load-test per-row service time and measured arrival rate λ; choose worker
concurrency c with sustained service rate μ(c) > peak λ plus recovery margin,
then prove burst backlog clears within 60 s. No invented throughput number.
Stop scaling when Spanner contention worsens rather than removing lock fences.

Proposed metrics: pending age p50/p95/max (including leased/dead unresolved
work), backlog count/frozen microdollars per authority and workspace, completed
latency, accepted rate, drain throughput, deferred/park/dead counts, lease loss,
oldest unresolved intent, admission data age, sync-fallback rate by reason,
unknown outcomes, and amount/hash mismatches. Export aggregate fleet metrics
without key IDs; use bounded indexed workspace diagnostics. Unhealthy/stale
signals flip new eligibility off; zero samples is healthy only with a fresh
worker heartbeat and confirmed empty backlog.

Side effects require explicit scope: `apply_frozen_settle` does not replay
budget emails, metadata broadcast or inline auto-refill. The outbox has a
separate auto-refill attachment/lease mechanism; preserve it where already
supported or exclude dependent pilot traffic. Do not promise side-effect
parity from the money primitive alone. No Bigtable work is added; typed activity
uses its existing atomic operational-analytics delivery intent.

## 6. Exposure bound

The authorize reservation remains held until the ordinary drain finalize:
`storage_gcp_authorize.py::authorize_atomic`'s credit admission checks
`total_credits - total_usage - reserved >= estimate` on the existing credit
shards (`storage_gcp_authorize.py`). Async acceptance releases nothing. Thus
the **existing reservation-based balance bound** continues to hold; it is not
an exact-actual-spend bound when actuals exceed estimates.

Proposed admission predicate, in addition to §2's priceable cohort, flag and
client opt-in:

```
async_eligible = paid tier in {2, 3}
                 AND fresh workspace pending frozen sum <= cap
                 AND fresh fleet drain p95 age <= 5 seconds
```

Read pending/dead unresolved amounts with a **workspace-keyed sparse index**
and bounded COUNT/SUM query or a process cache no older than **5 s**. Missing,
stale or failed reads mean false. The existing due index is not workspace-keyed
and the row does not currently carry workspace ownership; §4's schema/index
extension is necessary. Include leased rows and dead unresolved amounts; do
not drop failures from exposure totals. Recheck cached admission health at
settle so an old authorize hint cannot bypass the backlog flip. A cache hit
adds no RPC; a cache miss does. Do not scan `tr_entities.body`, join all history
or claim an indexed SUM has constant cost; bound work and fail closed on budget.

Carry $5/$25/$100 as admission thresholds, never billing caps. Existing
`services/speculation_shadow.py::TIER_CEILINGS` maps **tier 2 → $25, tier 3 →
$100**; its $5 check is **minimum paid headroom**, not a third implemented cap.
For this design, propose a $5 pilot override on either eligible tier, then
$25/$100 when approved. Tier 1 stays ineligible. The sketch's mapping of all
three numbers to “trust-tier caps” is therefore not an existing repo contract;
Joseph must confirm the intended $5 policy (§10).

**The sketch's claimed strict invariant and worst case are false.** Even an
uncached strong SUM at authorize does not serialize concurrent admissions, and
invocations may run before inserting an intent. With cache staleness, multiple
replicas and already-issued authorizations, pending spend can exceed the cap by
many requests, not just one request's actual-minus-estimate.

For one episode let C be the threshold and I all authorized/in-flight work
whose completion can enter async before the admission flip is observed. A
conservative guard bound is `pending <= C + sum(actual_i for i in I)` (drains
only reduce this). Separately, the existing balance bound has possible overrun
`sum(max(0, actual_i - reserved_i))`, not one request's overrun. Without a bound
on I and its actuals, **there is no numerical C-only worst-case exposure claim**.
The held-credit invariant is useful independently of the tier lag guard.

No per-request counter row: it would recreate a workspace hot write and add
new billing coordination. The chosen predicate is a cheap admission/lag guard,
not a linearizable cap. A strict hard cap would need separately reviewed
coordination or bounded permits/escrow including outstanding authorizations;
that conflicts with treating this cached SUM as sufficient. Keep the pilot
flag off until Joseph accepts the guard semantics or asks for that separate
proof. This is an unresolved design condition, not permission to silently
relax the approved exposure requirement.

## 7. Failure modes and crash points

| Boundary / failure | Required behavior |
|---|---|
| Router crash before INSERT transaction commits | No 202; enclave retries identical body/key. A retry can reattempt async, or sync with the same frozen price after a definitive rejection. Pre-handoff loss still depends on redelivery. |
| Crash after commit, before response | Intent is durable. Retry hits ALREADY_EXISTS; point-read original hash/state; worker drains. Never allocate another settlement ID. |
| Commit response lost / deadline | Outcome unknown. Retry the same INSERT/identity; an existing row resolves the ambiguity. A point-read miss alone does not prove an in-flight commit cannot land. Do not convert transport ambiguity into a fresh unrelated sync charge. |
| Enclave crash after pending | Outbox responsibility survives. The client may not receive the completion, but the charge is still drained; status is recoverable by ID/authorization. |
| Enclave crash before handoff | No outbox guarantee. Existing `cmd/enclave/settlement_retry.go` is a bounded in-memory channel (1,024 jobs, six attempts by default), not durable storage. The sketch overstates recovery if it assumes this survives enclave loss. |
| Duplicate same body | Immutable existing intent, original exact amount; no refresh. Duplicate done returns confirmed result, not fresh pending. |
| Corrected/different payload | 409; do not use enqueue's default refresh-latest. Accepted async input is immutable across sync retries, rolling old clients and drain. PR C must fence **all** later writers to async-tagged rows, not only the new handler. |
| Sync fallback overlaps accepted intent | Existing intent's amount wins. Snapshot-bearing sync retry must first reconcile same-key intent and may finish that frozen intent, never reprice/refresh it. Legacy entry paths must respect an async-tagged row. |
| Reaper or sync settles before insertion | §4 conditional reservation admission fails and rolls back INSERT. Resolve the winner; never promise exact pending when the charge cannot be booked. |
| INSERT wins before reaper | Existing in-transaction pending/dead guard suppresses reaping. Do not rely on scheduling order or expiration margins. |
| Drain crash before finalize commit | Lease expires, another worker retries; holds remain. |
| Drain crash after finalize, before mark | Reservation already claimed; richer apply outcome confirms winner; fenced mark repairs state/retention without charging again. |
| Lease expires while old worker runs | First-writer claim protects money; owner-conditioned mark/park prevents stale worker state overwrite. |
| Backlog/SLO failure | Authorize eligibility false; settle admission false for fresh inserts; sync fallback. Continue draining accepted work through rollback/flag-off. |
| Pause, abuse latch, key delete | Continue synchronous authorization/revocation checks; no cached eligible bit can authorize new inference. D3: propagation to subsequent authorize ≤300 s, verified across caches/replicas. Finish already accepted charges even if the key is deleted. |
| Refund | Same native intent path with `intent_kind=refund`, zero charge and route-bound terminal kind. Distinct PK from settle; first reservation claimant wins. A refund after a charge requires the existing adjustment process, not an invented reversal. |
| Typed store unavailable / deterministic error | Preserve current park/dead/alert policy and held reservation. Never silently route to legacy or mark a lost charge recovered. |

D3 concerns new authorization eligibility, not cancelling work already executed
or preventing its settlement. Ticket expiration is not a revocation system.
An audit of all pause/key/abuse cache lifetimes plus regional fault tests is
required; “authorize is synchronous” alone is not evidence of a 300 s bound.

For refund literals, the request uses `/internal/gateway/refund` and the same
header/strict body with `terminal_kind=refund`, `charge_micro=0`; a mismatched
route/kind is invalid. The resulting settlement ID ends `.refund`. Refund
success reports refunded only after the reservation actually finalized at zero;
a settle winner reports its committed amount and review flag. This is the
existing outbox polarity contract, not new refund arithmetic.

The two budgets share one monotonic deadline: try handoff within 500 ms and
complete handling within 2 s total. A queue or retry attempt is not durable
handoff. If neither committed sync nor confirmed pending is available by D2,
do not emit a successful final completion that claims acceptance; preserve the
retry identity and surface the existing failure/incomplete-response behavior.
For streams, bytes already sent cannot be retracted; pin the final error event
and absence of a successful completion marker in PR E tests.

## 8. Shadow phase

Run **seven consecutive days** with async admission disabled. For opted-in,
otherwise eligible traffic, capture signed-snapshot inputs, normalize/evaluate
with the new Python and enclave Go paths, and compare them to the actual
synchronous booked amount. **Sync books; shadow never inserts a second pending
intent or calls finalize.** Ordinary synchronous outbox behavior remains active.

Collect eligibility/exclusion denominators by adapter/route/streaming, normalized
usage, snapshot/payload hashes, integer charge deltas, predicted admission,
cache data age and settle timing. Content-free evidence only. Exercise
catalog changes, price tiers/cache boundaries, retries, refunds and zero usage
with fixtures even if rare in shadow traffic. Shadow alone cannot prove pending
drain throughput: add isolated load/crash tests with real Spanner semantics.

Exit criteria:

- Zero unexplained exact-amount, normalization, signature/hash or identity
  disagreements; 100% of admitted samples have evaluable final usage.
- Shared 759-case fixture/hash pin and wire fixtures pass in both repositories;
  rebased revisions and artifact hashes recorded. No reliance on the unlocated
  93,561-probe report.
- Frozen-main and pricing-matrix differentials show identical counters,
  reservation winner and generation amount; all crash/mutation gates in §9 pass.
- Demonstrate D2, D3, drain throughput/burst recovery and agreed SLO, including
  worker crashes, unknown commits and stale admission. Publish region p50/p95/p99
  rather than declaring the sketch's forecast achieved.
- Resolve exposure policy and pilot side-effect exclusions; show no flag-off
  change, no duplicate booking/free-release, truthful terminal status and a
  rollback that disables new async admission while draining existing intents.

A correctness mismatch resets the seven-day clean window after the fix. Missing
traffic coverage requires fixture/load evidence, not a claim of zero failures.

## 9. PR sequence (flag-off)

| PR | Scope | Gate before activation |
|---|---|---|
| **A** | Land router **#1390** and enclave **#393 as-is**, rebased on main; retain their dormant contract/evaluator scope | Cross-repo fixture bytes, all operations and hash pins agree; no live path imports/effects introduced |
| **B** | Router authorize snapshot/hash/signature/`async_eligible` additions; additive native-row/index support for cheap admission; separate issuer purpose | Existing authorize/reserve behavior unchanged; requested exclusion completeness, owner bindings, expiry and stale-data tests |
| **C** | Router async-v1 handler, snapshot-bearing same-amount sync fallback, status endpoint, immutable duplicate handling, atomic still-open check, metrics and budgets | Literal wires pinned; no credit/key writes during enqueue; reaper/sync-before-insert race proof; no header means frozen-main behavior |
| **D** | Drain scaling, small batches/lease-budget tuning, health admission flip and rollout config | Measure throughput, 5/60 s target or approved revision; reaper/retention/fence unchanged; fast polling does not multiply housekeeping |
| **E** | Enclave opt-in negotiation, evaluator use, accept pending/`trusted_router_settlement`, exact fallback/retry identity and stream completion behavior | Attested image rollout through reviewed CI/attestation/verification gates; no merely parsed mode activates behavior |
| **F** | Complete proof set and seven-day shadow report (tests accompany B–E too) | Frozen-main settle oracle; sync-vs-async pricing matrix; crash-point tests; mutation audit; reviewed cap semantics and D3 |
| **G** | Explicit pilot opt-in for Joseph's workspace; gradual enablement | Prior gates green; real region measurements, health verification, rollback exercised; never label queued rollout deployed |

Proof set details:

- Follow `tests/test_settle_c1_oracle.py` and `tests/fakes/settle_c1_main.py`:
  freeze a named main revision and AST/source hashes, compare money state and
  actual entry-path operations, not a newly rewritten “expected” algorithm.
- Run every positive billing matrix case through sync, async enqueue+drain,
  duplicate and snapshot-bearing sync fallback. Compare credit/key usage,
  released holds, reservation/authorization finalization, generation amount,
  exact hash and outbox retention. Include catalog change/removal, rollover,
  debt, deleted keys, zero usage, cache conventions and tier-last fallback.
- Fault injection at each §7 boundary, including lost commit responses,
  reaper-before-insert, concurrent sibling refund, worker lease takeover,
  done-body clearing, TTL, expired-ticket replay and stale multi-replica caps.
- Mutation audit must go red when removing INSERT uniqueness, preserve-existing,
  payload equality, signature/binding, exact amount comparison, admission
  predicate, lease fence, reaper guard, retention clears or cap arithmetic.
  Extend `tests/fakes/spanner.py` to assert new SQL predicates; substring
  matching must not hide a deleted safety rule. Use emulator/integration
  concurrency evidence for serialization, not only a sequential fake.
- Each implementation PR runs full repo gates: `uv run ruff check .`,
  `uv run mypy`, `uv run pytest -q`, coverage ≥70%, relevant conformance and
  Go tests. Patch backend classes, never the module-global STORE proxy.
  These are implementation gates, not results claimed by this documentation pass.

No production code, flags, DDL, deployment or git writes are part of this pass.
Billing review, CI, attestation and rollout gates remain in force for later PRs.

## 10. Open questions for Joseph

1. **Pilot workspace:** which workspace/key and expected peak concurrency? Can
   the first pilot exclude auto-refill-dependent traffic and metadata-broadcast/
   budget-notification expectations, given the drain's narrower side effects?
2. **`pending` for `/decide`:** is eventual settlement acceptable there? If yes,
   commission the route/envelope/evaluator extension and fixtures; #1390 as-is
   supports only chat.completions/responses, so v1 initially stays sync for decide.
3. **SLO numbers:** approve 95% ≤5 s / 100% ≤60 s as operating targets, including
   crash reclaim and repair completion, or choose alternatives. The existing
   five-minute scheduler and 300 s leases cannot meet them. Is 60 s an alert
   threshold with explicit outage accounting rather than an absolute promise?
4. **Cap policy (enablement blocker):** accept $25/$100 as a cached admission/lag
   guard for paid tiers 2/3, with a $5 pilot override, or require a strict hard
   exposure cap? The latter needs a separate coordinated bound. What did the
   approved $5 threshold mean? Current code uses $5 as paid-headroom floor.
5. **Missing evidence:** supply the raw 2026-10-05 timing export and the claimed
   93,561-probe parity report if they should be release evidence. Neither was
   found in the inspected sources; the doc currently attributes the timings to
   the sketch and relies on the versioned 759-case contract.

Repository gaps to close in the named PRs, not additional policy questions:
new ticket key purpose/config and epoch provenance (B); workspace index and
strict raw request parser (B/C); immutable metadata across body clearing and
all legacy retry writers (C); same-snapshot sync fallback and owner-checked
status (C); worker reclaim/housekeeping and capacity evidence (D); final stream
error/metadata fixtures and attested rollout (E); all-cache D3 audit (F).

**Sketch corrections recorded:** router signs the snapshot, not the enclave;
v1 freezes prices, not a charge cap; cohort excludes decide and more than the
sketch's abbreviated list; PRs are dormant; ALREADY_EXISTS is per authorization
and kind, with immutable async input instead of default refresh; leased is not
a status; existing enqueue includes retention writes and Batch DML; atomic
still-open admission is needed when reaping wins first; drain is per-intent,
not cross-request release batching; current cadence cannot meet the proposed
SLO; authorize cache misses add work; cached caps do not imply a strict cap or
one-request overrun bound; retries before handoff are not durable enclave
storage; sync fallback needs new frozen-price wiring; refunds are first-writer
zero-charge finalization, not automatic charge reversal; and latency/probe
claims are not independently established by the inspected artifacts.


### PR C flag-off SQL and additive-schema compatibility

Admission and protection are independent. `async_settle_enabled` admits new
work; `async_settle_protection` protects accepted work. Both default to false
and rollout pins `TR_ASYNC_SETTLE_ENABLED=false` and
`TR_ASYNC_SETTLE_PROTECTION=false` explicitly, never inheriting either value.
Settings rejects admission on with protection off. Runtime admission checks
also require both flags, even if settings were mutated without validation.
Disabling admission stops ticket issuance and async-v1 acceptance; detached
snapshot metadata remains available. Accepted rows still drain.

With **both false**, the frozen PR B claim/refresh/INSERT SQL, parameters,
types and RPC counts remain identical, including deferred-retention claims and
unsuccessful synchronous finalize. Protection alone gates the claim's
`NOT EXISTS` primary-key range read by authorization ID, the immutable refresh
predicate, and winner reconciliation. Pending/dead rows are protected (leased
work remains pending); release-approved and abandoned rows permit the
operator-approved release. Additive INSERT construction is unchanged.

| Gated site | Classification | Reason |
|---|---|---|
| `services/async_settle.snapshot_projection` | Admission (both flags) | Issue tickets and read eligibility only when new work may be accepted. |
| `routes/settlements.AsyncSettlementRoute` | Admission (both flags) | Opt-in async-v1/snapshot-sync dispatch; ordinary legacy parsing remains unchanged. |
| `services/async_settle_handler._handle` | Admission (both flags) | New async INSERT acceptance; direct disabled retries may resolve existing rows. |
| `storage_gcp.SpannerStore.__init__` outbox construction | Protection | Supplies the immutable refresh predicate in `SpannerSettleOutbox.enqueue`. |
| `services/settle_outbox_drain.spanner_settle_outbox` | Protection | The same immutable predicate for gateway and drain outbox instances. |
| `storage_gcp.typed_finalize_gateway` | Protection | Claim fence in the generic typed finalizer. |
| `storage_gcp.typed_finalize_gateway_authorization_result` | Protection | Claim fence in the durable two-commit finalizer. |
| `storage_gcp.typed_settle_one_commit_result` | Protection | Claim fence in the one-commit finalizer. |
| `storage_gcp.reap_expired_reservations` | Protection | Propagates the async fence alongside the existing reaper guard. |
| `storage_gcp.reap_expired_reservations_result` | Protection | Same protection for the richer reaper result path. |
| `gateway._settle_gateway_authorization`, durable async enqueue winner | Protection | Discards repriced legacy inputs and applies the accepted amount. |
| `gateway._settle_gateway_authorization`, rejected claim reconciliation | Protection | Resolves the async winner even with admission and ordinary outbox admission off. |
| `storage_gcp_counter_dml.claim_reservation_statement` | Protection argument | Emits extra predicate/parameters only when `async_fence` is true. |
| `storage_gcp_settle_outbox.SpannerSettleOutbox.enqueue` | Protection argument | Emits `AND async_version IS NULL` only when `_async_fence` is true. |

The gateway winner reads are necessary for correctness **after** acceptance;
they must not follow admission. Status lookup and accepted-work drain remain
available without an admission gate.

Only safe disable order (apply to every serving replica before proceeding):

1. Turn admission off, leaving protection on. No new tickets or async acceptances.
2. Keep draining until **no** `tr_settle_outbox` row with `async_version=1` is
   pending/dead. Wait for in-flight admission requests from old revisions too.
   Dead rows require normal operator resolution; they are not drained merely
   by disabling admission.
3. Turn protection off only after the zero-backlog check succeeds.

For each workspace ever admitted, use the bounded workspace-index check below
with `@ws` bound as STRING, a strong snapshot, low priority, no retries and a
short deadline (as in `storage_gcp_async_admission.read_admission`). The admission
reader's broader pending/dead count also safely establishes zero when its trust
row is available; missing/unavailable/truncated results are never proof of zero.
Keep the complete admitted-workspace roster: one workspace's zero is not fleet
clearance. Do not scan entity bodies or infer the roster from an unbounded scan.

```sql
SELECT COUNT(*) AS unresolved_async
FROM (
  SELECT authorization_id
  FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_workspace_status}
  WHERE workspace_id=@ws AND status IN ('pending', 'dead') AND async_version=1
  LIMIT 1001
)
```

Any positive count blocks protection disablement; 1001 is a bounded sentinel,
not an exact backlog size. A read failure also blocks disablement.

Outbox reads use the additive 22-column projection for both flag states; the old
18-field tuple path is gone. This is safe because `.github/workflows/deploy.yml`
runs `migrate-schema` before the deploy job invokes `rollout.sh`. Rolling back
code still works with additive nullable columns. The legacy INSERT remains
byte-identical; the async INSERT adds its four typed metadata columns in the
shared builder, without editing an already-built SQL string.

Async batch statements stop 50 ms before the handoff deadline to leave cleanup
time. Commit may use the remaining budget. A reply at 499 ms can confirm
acceptance; one at 501 ms cannot produce 202. Cleanup attempts one bounded Rollback RPC per discarded transaction, including
after an unexpectedly late wakeup: its async floor is 50 ms, not the legacy two
seconds. A per-transaction attempted marker is set before the RPC; a lost reply
cannot give the outer disposer another floor merely because SDK `rolled_back`
is still false. A scheduler/RPC overrun can therefore extend
best-effort cleanup past 500 ms; it never extends the acceptance deadline.
