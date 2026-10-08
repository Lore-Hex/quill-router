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
`schemas.py`. This literal is regenerated from `tests/fixtures/async_settlement/authorize_v1_builder.json`: the frozen builder derives cache-read/cache-creation rates of 250,000/625,000 for this endpoint. One ordinary input and one output token at 500,000 microdollars/million independently round to **2**. `authorize_v1.json` retains the older zero-cache-rate literal for signature parity; neither fixture is changed.
The ticket is a real test signature (public test seed = 32 bytes `01`, never a
production key), verified at `1791244801`. Public key, base64url: `iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w`.
Compact JWS uses sorted compact ASCII JSON and unpadded base64url; claims are
literal below so implementations can verify bindings, not just price hashes.

```json
{
  "async_eligible": true,
  "aud": "router-settlement",
  "authorization_id": "auth-v1",
  "billing_authority": "local",
  "epoch": 1,
  "exp": 1791245100,
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
}
```

```json
{
  "data": {
    "authorization_id": "auth-v1",
    "generation_id": "gen-c7a73498dd8a5d59a705f482070c9e56",
    "async_eligible": true,
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
    "billing_snapshot_hash": "cb8feaf08da381f0d356dcd8ed4c6577f1d44a130e7f3b8029647fa3814872b4",
    "settlement_ticket": "eyJhbGciOiJFZERTQSIsImtpZCI6ImFzeW5jLXYxLWZpeHR1cmUiLCJ0eXAiOiJ0ci1hc3luYy1zZXR0bGUtdjEifQ.eyJhc3luY19lbGlnaWJsZSI6dHJ1ZSwiYXVkIjoicm91dGVyLXNldHRsZW1lbnQiLCJhdXRob3JpemF0aW9uX2lkIjoiYXV0aC12MSIsImJpbGxpbmdfYXV0aG9yaXR5IjoibG9jYWwiLCJlcG9jaCI6MSwiZXhwIjoxNzkxMjQ1MTAwLCJnZW5lcmF0aW9uX2lkIjoiZ2VuLWM3YTczNDk4ZGQ4YTVkNTlhNzA1ZjQ4MjA3MGM5ZTU2IiwiaWF0IjoxNzkxMjQ0ODAwLCJpbnZvY2F0aW9uX25vbmNlIjoibm9uY2UtdjEiLCJpc3MiOiJyb3V0ZXItZml4dHVyZSIsImpvdXJuYWxfcmVnaW9uIjoidXMtY2VudHJhbDEiLCJrZXlfaWQiOiJrZXktdjEiLCJyZXNlcnZhdGlvbl9pZCI6InJlcy12MSIsInJvdXRlX3R5cGUiOiJjaGF0LmNvbXBsZXRpb25zIiwic2V0dGxlX29yaWdpbiI6InR5cGVkIiwic25hcHNob3RfaGFzaCI6ImNiOGZlYWYwOGRhMzgxZjBkMzU2ZGNkOGVkNGM2NTc3ZjFkNDRhMTMwZTdmM2I4MDI5NjQ3ZmEzODE0ODcyYjQiLCJzbmFwc2hvdF92ZXJzaW9uIjoxLCJzdHJlYW1lZCI6ZmFsc2UsIndvcmtzcGFjZV9pZCI6IndzLXYxIn0.zbBwIsn5jCD25enkaRK9idg0oVaoBROE8jTK3FxAxrj1iDns3HhYp_KyDjzLsIdhVkJJKhjGQU_s3F8Vm97QDA",
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
  "settlement_ticket": "eyJhbGciOiJFZERTQSIsImtpZCI6ImFzeW5jLXYxLWZpeHR1cmUiLCJ0eXAiOiJ0ci1hc3luYy1zZXR0bGUtdjEifQ.eyJhc3luY19lbGlnaWJsZSI6dHJ1ZSwiYXVkIjoicm91dGVyLXNldHRsZW1lbnQiLCJhdXRob3JpemF0aW9uX2lkIjoiYXV0aC12MSIsImJpbGxpbmdfYXV0aG9yaXR5IjoibG9jYWwiLCJlcG9jaCI6MSwiZXhwIjoxNzkxMjQ1MTAwLCJnZW5lcmF0aW9uX2lkIjoiZ2VuLWM3YTczNDk4ZGQ4YTVkNTlhNzA1ZjQ4MjA3MGM5ZTU2IiwiaWF0IjoxNzkxMjQ0ODAwLCJpbnZvY2F0aW9uX25vbmNlIjoibm9uY2UtdjEiLCJpc3MiOiJyb3V0ZXItZml4dHVyZSIsImpvdXJuYWxfcmVnaW9uIjoidXMtY2VudHJhbDEiLCJrZXlfaWQiOiJrZXktdjEiLCJyZXNlcnZhdGlvbl9pZCI6InJlcy12MSIsInJvdXRlX3R5cGUiOiJjaGF0LmNvbXBsZXRpb25zIiwic2V0dGxlX29yaWdpbiI6InR5cGVkIiwic25hcHNob3RfaGFzaCI6ImNiOGZlYWYwOGRhMzgxZjBkMzU2ZGNkOGVkNGM2NTc3ZjFkNDRhMTMwZTdmM2I4MDI5NjQ3ZmEzODE0ODcyYjQiLCJzbmFwc2hvdF92ZXJzaW9uIjoxLCJzdHJlYW1lZCI6ZmFsc2UsIndvcmtzcGFjZV9pZCI6IndzLXYxIn0.zbBwIsn5jCD25enkaRK9idg0oVaoBROE8jTK3FxAxrj1iDns3HhYp_KyDjzLsIdhVkJJKhjGQU_s3F8Vm97QDA",
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
      "payload_hash": "f5a8699841e40582b2c8d49702092328ba9e63991166b95da60c90f7da5fdf32",
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

A snapshot-bearing request with `X-TR-Settlement-Mode: sync` returns HTTP 200 after confirmed finalization, acceptance `duplicate`, settlement status `settled` or `refunded`, and `poll_after_ms:null`; a refund **that loses to a settled charge** adds `review_required:true` (a successful zero-charge refund does not).

```json
{
  "data": {
    "acceptance": {
      "status": "duplicate",
      "payload_hash": "f5a8699841e40582b2c8d49702092328ba9e63991166b95da60c90f7da5fdf32",
      "settlement_status": "settled"
    },
    "trusted_router_settlement": {
      "v": 1,
      "settlement_id": "auth-v1.settle",
      "settlement_status": "settled",
      "cost_microdollars": 2,
      "status_url": "/v1/settlements/auth-v1.settle",
      "poll_after_ms": null
    }
  }
}
```

```json
{
  "data": {
    "acceptance": {
      "status": "duplicate",
      "payload_hash": "b39848e23c2c264ee89f23ab757ebf8db3b209bb5ab1235facda8924c6f89dd7",
      "settlement_status": "refunded"
    },
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
      "path": "/v1/internal/gateway/settle",
      "status": 400,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":400,\"message\":\"Invalid async settlement snapshot\",\"type\":\"bad_request\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "invalid_signature": {
      "path": "/v1/internal/gateway/settle",
      "status": 401,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":401,\"message\":\"Invalid settlement ticket\",\"type\":\"unauthorized\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "charge_mismatch": {
      "path": "/v1/internal/gateway/settle",
      "status": 409,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":409,\"message\":\"Async settlement amount mismatch\",\"type\":\"conflict\",\"source\":\"router\",\"expected_cost_microdollars\":2,\"claimed_cost_microdollars\":3},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "payload_conflict": {
      "path": "/v1/internal/gateway/settle",
      "status": 409,
      "content_type": "application/json",
      "retry_after": null,
      "body_exact": "{\"error\":{\"code\":409,\"message\":\"Settlement intent already exists with a different payload\",\"type\":\"conflict\",\"source\":\"router\"},\"data\":{\"timing\":{\"total_ms\":0,\"spanner_rpcs\":0,\"key_lookup_ms\":0,\"routing_ms\":0,\"store_ms\":0,\"post_commit_ms\":0}}}"
    },
    "storage_unavailable": {
      "path": "/v1/internal/gateway/settle",
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
with the evaluator under test. PR F1 pins every JSON literal in this section to a fixture.

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
        "poll_after_ms": 1000,
        "created_at": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T00:00:00Z",
        "terminal_at": null
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
        "poll_after_ms": null,
        "created_at": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T00:00:00Z",
        "terminal_at": "2026-10-06T00:00:00Z"
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
        "poll_after_ms": null,
        "created_at": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T00:00:00Z",
        "terminal_at": "2026-10-06T00:00:00Z"
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
snapshot metadata remains available. Accepted rows still drain. With protection
on, snapshot-bearing sync retries still reconcile accepted intents or apply the
signed frozen price when no intent exists. Fresh async-v1 requests return the
§3.3 `sync_required` literal with reason `disabled`, without inserting an intent.

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
| `routes/settlements.AsyncSettlementRoute` | Protection | Strict async-v1/snapshot-sync recovery dispatch survives admission rollback; ordinary legacy parsing remains unchanged. |
| `services/async_settle_handler._handle` | Admission (both flags) | Fresh async INSERT acceptance; disabled retries may resolve existing rows, and snapshot-sync recovery retains the frozen price. |
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

### PR D dormant drain and health implementation

The unchanged scheduler path is frozen against main
`ecb794597f32b0775594f8944f40f935b7f07278`. Defaults preserve its SQL/parameters,
response, 500-row limit clamp, sequential apply, 240-second pass budget,
300-second lease and per-tick purge/reap. No scheduler, cron, warm-worker service
or job is deployed by PR D. PR G/operators must measure and approve enablement.
Protection remains independent of admission: accepted work drains during an
admission rollback, through the same frozen-money apply and resolution helpers.

| Setting (`TR_` environment prefix) | Default | Meaning and read point |
|---|---:|---|
| `settle_outbox_fast_drain_enabled` | false | Select fast pass at the drain entry; required by the callable warm loop. |
| `settle_outbox_poll_interval_seconds` | 300 | Warm-loop interruptible delay after a pass; unused in legacy mode. |
| `settle_outbox_health_publish_interval_seconds` | 2 | Fleet publisher cadence; validated positive and at most `CACHE_SECONDS=5`. Fast mode only. |
| `settle_outbox_claim_batch` | 500 | Upper bound on a claim wave; fast mode further clamps to available worker slots. Unused in legacy mode, which retains the caller's limit and 500 clamp. |
| `settle_outbox_worker_concurrency` | 1 | Maximum independent running apply slots per pass, validated 1–32; fast mode only. |
| `settle_outbox_lease_seconds` | 300 | Lease assigned by each running slot; fast mode only. |
| `settle_outbox_pass_budget_seconds` | 240 | Deadline for starting new work; must be below the lease in fast mode. Does not cancel in-flight money transactions. |

Rollout pins all seven defaults explicitly, never copying ambient/live settings.
`run_worker(settings, stop)` exposes the warm loop without installing a schedule.
It passes `stop` into `drain_pass` and every executor slot. The wave loop and
slots check it before starting claims; claims already in flight and their
applies resolve normally. Stop also interrupts the inter-pass polling wait.
Each executor slot claims at most one row after starting, using a rotating
existing queue shard. There is no queue of claimed executor tasks. A slow claim
that consumes the lease or pass budget leaves the row untouched for fenced
reclaim. Existing apply RPC/retry bounds still govern in-flight work; this code
does not assert an upper bound on transaction duration through an outage.

Health uses `tr_entities` primary key `(kind='settle_drain_control', id='fleet-v1')`
in each independent authority database. It is a fixed-key control record, not
a per-request counter. It requires no new table. Its JSON fields are `v`,
`authority`, `observed_at` (UTC epoch at read start), `worker_heartbeat` (UTC epoch
after the observation), `complete`, `sample_count`, `backlog_count`,
`frozen_micro`, `p50_age_seconds`, `p95_age_seconds`,
`oldest_unresolved_age_seconds`, and `dead_count`.
The exact schema is shared by publisher output, its previous-record reader,
and the admission reader/decoder:

| Field(s) | Validation |
|---|---|
| `v` | Exact int, equal to 1 (bool rejected) |
| `authority` | Exact str, equal to `local` |
| `complete` | Exact bool; only true is eligible |
| `sample_count`, `backlog_count`, `frozen_micro`, `dead_count` | Exact ints, non-negative; sample equals backlog; dead ≤ backlog |
| `observed_at`, `worker_heartbeat` | Exact int/float, finite and non-negative; observed ≤ heartbeat; eligibility requires both ages in [0, 5) seconds |
| `p50_age_seconds`, `p95_age_seconds`, `oldest_unresolved_age_seconds` | Exact int/float, finite, 0 ≤ p50 ≤ p95 ≤ oldest; eligibility requires p95 ≤ 5 |

No missing or extra keys, coercions, duplicate JSON keys, or non-finite numbers
are accepted. JSON is bounded to 4,096 UTF-8 bytes before parsing (and after
serialization for direct dict callers). Empty backlog requires zero frozen
micro and all three ages zero. Missing/wrong-kind/wrong-id records fail closed
through the exact primary-key lookup. Invalid raw observation timestamps,
amounts or statuses make the publication incomplete; sample and backlog counts
still match and partial amounts are never eligible. A malformed previous
record is ignored rather than trusted for newest-observation ordering.

Health uses this sparse covering index, including active leases and legacy
rows without workspace ownership:

```sql
ALTER TABLE tr_settle_outbox ADD COLUMN unresolved_at TIMESTAMP
AS (IF(status IN ('pending', 'dead'), COALESCE(created_at, TIMESTAMP '1970-01-01T00:00:00Z'), NULL)) STORED;
CREATE NULL_FILTERED INDEX tr_settle_outbox_unresolved
ON tr_settle_outbox (unresolved_at) STORING (actual_cost_micro, status);
```

[Spanner supports partial indexes using generated columns](https://docs.cloud.google.com/spanner/docs/generated-column/how-to).
The incremental logical index-operation cost is:

| Operation | New unresolved index maintenance |
|---|---|
| Inline already-done INSERT | 0 |
| Pending INSERT | +1 entry |
| Lease-only update | 0 |
| Retry / park | 0 |
| Pending → dead | Stored-status update |
| Pending → done | Delete 1 entry |
| Dead INSERT | +1 entry |

This maintenance applies even with fast mode off. The existing due index
already pays comparable maintenance on these transitions. “Done rows pay
nothing” applies only to the inline already-done INSERT, not a drain's
pending-to-done mark. The STORED expression is evaluated when its dependencies
change; zero index operations does not imply zero expression CPU. No money-path
INSERT or resolution statement is changed. Both schema additions are
independently idempotent and fail closed on migration errors. The existing
sparse due index cannot observe dead rows: their `next_attempt_at` is NULL.
Membership depends only on status. NULL `created_at` maps to the epoch sentinel,
so pending/dead rows remain counted, contribute frozen micro and sort oldest.
The publisher marks epoch-or-earlier timestamps incomplete, making admission
UNHEALTHY even when the unknown-age row is below the p95 sample rank.
The exact covering observation statement is:

```sql
SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_unresolved} WHERE unresolved_at IS NOT NULL ORDER BY unresolved_at LIMIT @limit
```

`@limit=10001` bounds the 0.2-second low-priority, no-retry query. More than
10,000 rows or invalid returned evidence means incomplete/ineligible; counts
and sums then describe only the bounded observation, never the full backlog.
Empty is healthy only after a complete fleet observation with a fresh heartbeat.

Before observing health, workers transactionally claim the complete control key
`(kind='settle_drain_control', id='health-publish-v1')` using the same cadence
primitive as housekeeping. At most one worker wins per publish interval
(default 2 seconds; positive and at most the consumer's 5-second freshness
bound). Losers skip observation and publication. Apply work precedes the claim,
so a lost or failed claim cannot block that pass's applies. A crashed winner
consumes its interval; failed publication ages out. Slow passes, failed claims
or observations can still cause stale evidence and fail-closed admission; the
interval setting alone does not guarantee freshness under those conditions.
Conditional publication preserves newest-observation-wins even if an older
winner finishes after the next interval's winner.

The deterministic cadence proof runs N concurrent workers on each of M polls:
(1, 9, 1-second poll, 2-second interval), (6, 17, 0.25-second poll, 2-second
interval), and (4, 21, 0.5-second poll, 5-second interval). It counts actual
observation queries, verifies at most `ceil(M * poll / interval)` fleet-wide,
checks that successive observations are separated by at least the interval,
and verifies the persisted claim and health timestamps. These are synthetic
correctness cases, not measured production capacity. Separate tests cover lost
and failed claims with successful apply, and a delayed older publication.

The consumer lazily refreshes health only when admission is enabled. Refreshes
are coalesced for one second per process, using one strong complete-primary-key
read (`timeout=0.2`, `retry=None`, `PRIORITY_LOW`); hits add no RPC. Missing,
invalid, future, unhealthy or five-second-old evidence is ineligible. Receipt
never rejuvenates the publisher's timestamp. The existing workspace admission
index/read remains bounded to 1,001 rows and retains its independent five-second
freshness rule. Turning admission off stops health reads, including already
constructed consumers; enabling it is a settings/application rollout.

Housekeeping has its own fixed control key, `housekeeping-v1`, containing the
last claimed UTC `observed_at`. A transaction admits at most one fast-mode
purge/reap pass per 300 seconds across replicas. A crashed claimant delays the
next pass until that interval ends. It does not change reaper SQL, the claim
fence, retention, purge semantics, or the legacy scheduler path. The unit proof
runs 31 polls over 300 seconds and observes only the passes at 0 and 300, then
races six independent callers for the next interval and observes one winner.
During mixed-mode rollout, the unchanged scheduler can still perform its
ordinary housekeeping; this gate bounds additional fast-worker housekeeping.

Structured aggregate logs add fleet pending age p50/p95/max, backlog frozen
micro/count and dead count with completeness, per-row apply-plus-resolution
service time and outcome,
confirmed outbox completion latency, pass elapsed time/claimed/outcome counts,
park/dead transitions, resolution fence misses, and consumer health data age.
They emit no key or workspace IDs. The per-workspace admission-miss log is
removed entirely; its count/sum would be a tenant diagnostic, not a fleet metric. A fence miss includes lease loss or a concurrently resolved
row; it is not falsely reported as a proven lease takeover. Existing handler
fallback/unknown-outcome logs remain the source for request-path outcomes.

TODO measurement instrumentation: accepted-arrival rate from enqueue commits;
booking-versus-repair completion timestamps; confirmed lease takeover attribution;
full unknown-commit disambiguation counts; amount/hash diagnostic mismatches;
and a joined fleet sync-fallback-by-reason report. Do not synthesize any of these
from incomplete drain observations. No price comparison changes an accepted
frozen amount.

### PR D measurement plan

Before selecting poll/batch/concurrency/lease/budget values or enabling fast mode:

1. Export bounded `SPANNER_SYS` finalize rates as an approximation to settle λ,
   plus async acceptance commit timestamps, explicit observation windows and
   peak/burst windows. Document what traffic the finalize approximation includes.
2. Export `async_drain.timing` for `settled_now` and other outcomes separately;
   measure per-row service distributions, pass overhead, booking latency and
   outbox completion/repair latency under each proposed concurrency. Include idle
   periods in arrival windows. Drain throughput alone is not an arrival rate.
3. Sweep bounded concurrency and small claim waves while measuring Spanner
   contention/retries, row service tails, claim time, resolution fence misses,
   confirmed lease loss and crash reclaim delay. Select lease and pass budget
   together from these measurements, with room for claim and resolution RPCs.
4. Measure publisher observation time, completeness/truncation, health read cost,
   heartbeat/data age, admission flips and the shared housekeeping cadence with
   multiple worker replicas and the watchdog scheduler present.
5. Use `scripts/async_settle/drain_capacity.py` on file exports to calculate λ,
   observed peak λ, service-time percentiles and an ideal independent-worker
   concurrency estimate with an explicitly supplied recovery margin and burst
   backlog/target. It prints `T_clear=B/(c/mean(service)-peak_lambda)`. Validate
   that estimate against measured μ(c); it does not model contention or certify
   an SLO. JSONL outbox exports must include independently measured
   `service_seconds`; creation-to-completion is latency, not service time.
6. Run crash/lease-loss and burst recovery trials against the proposed 5/60-second
   objective, including repair completion, or obtain an approved revision before
   enablement. No production rates or concurrency recommendations are filled in.

Design corrections found in PR D: §3.1's original zero-cache-rate literal was
not producible by the frozen builder; only that section now uses the builder
fixture. A due-index observation cannot establish fleet health because it omits
dead rows. The current five-minute cadence/300-second lease still cannot support
the proposed 5/60-second objective. Cached admission remains a lag guard, not a
strict exposure cap; PR D does not resolve the policy questions in §6/§10.

### PR F proof set, D3 propagation audit and cap semantics

This is **F1**, based on main `f83bbaac`. F2 owns the shadow comparator and
seven-day traffic report. This appendix is local correctness evidence, not
permission to activate async admission, a production measurement, or an
assertion that F/G's rollout gates have passed. Production code, schemas and
routes are unchanged. No git writes or deployments are part of F1.

#### Findings and literal corrections

- **F1-001 (four strict xfails: both records × settle/refund):** fresh snapshot-bearing sync
  fallback applies through the frozen outbox finalizer, which defers retention,
  but inserts no outbox row. Both reservation and authorization remain `terminal_at=NULL` after
  successful finalization. `finish()` calls `_resolve_row()` on the ephemeral
  row, so there is no durable mark to complete retention. Reproducer:
  `test_fresh_snapshot_sync_completes_retention`. Money is booked once; the
  missing retention completion needs a separate reviewed production fix. TTL
  cannot expire NULL timestamps; the unsettled-only reaper cannot repair these
  already-settled rows.
- The actual settle sync JSONResponse bytes, compacted with the literal's
  original key order, match the enclave #472 copy exactly, including
  `payload_hash=f5a8699841e40582b2c8d49702092328ba9e63991166b95da60c90f7da5fdf32`.
  The pretty-printed fixture has formatting whitespace; the test separately
  compares parsed JSON, canonical bytes, and unsorted compact wire bytes.
- A successful refund has `settlement_status=refunded`, amount 0, and **no**
  `review_required`. That flag is true only when a refund loses to an already
  settled charge (`async_settle_handler.py:174`). Adding it unconditionally to
  the literal would misdescribe the actual router. The two sync fixtures were
  generated by real HTTP handler calls with `auth-v1`/`res-v1`/`key-v1` seed
  identities, without rewriting responses.
- §3.2 now uses `request_v1.json`: cache-read/cache-creation rates
  250000/625000, builder-signed ticket and `cb8feaf0…` snapshot hash. §3.3's
  pending payload hash is `f5a869…`; it includes both sync reply literals.
  §3.4 includes the fixture's `created_at`, `updated_at`, `terminal_at`.
  `test_all_section_three_json_literals` parses **every** JSON fence in §3
  and compares the ordered collection to named fixtures/subobjects. The error
  envelope fixture is generated through the real HTTP routes with only
  `gateway_timing.perf_counter` fixed. `test_error_envelopes_real_http` pins every
  status, content type, Retry-After header and exact response byte sequence.
  Charge mismatch includes expected cost 2 and claimed cost 3 microdollars. Neither
  `authorize_v1.json` nor `billing_v1.json` was changed.

#### Frozen-main coverage

`tests/test_async_settle_proof_oracle.py::test_f83bbaac_complete_entry` executes
both frozen and live route registration, dispatch, settlement and drain using
actual Spanner fake transactions. Round 4 replaces the selected-function copies
and their exemption list with **one package snapshot** in
`tests/fakes/frozen_f83bbaac/package.tar.gz`.

The archive contains every Python module and text resource from the
`src/trusted_router` subtree at **f83bbaac** (Python, JSON/JSONL, HTML, TXT,
SQL, CSS and JavaScript). Static binary media are omitted; they are not used by
these requests. `pins.json` records the SHA-256 of the original bytes of each
file. `tests/fakes/frozen_package.py` independently pins the archive digest,
verifies its exact member set and every member digest, and compiles the unchanged
Python bytes under temporary snapshot paths, never live coverage paths.
Its importer redirects all absolute `trusted_router` imports into
`frozen_f83bbaac`; relative imports stay there. A missing snapshot import fails,
with no fallback to the live package. Settings, enums, schemas, dataclasses,
feature stores, captured IO callbacks, gateway, middleware and the HTTP app are
constructed independently in that namespace. No live production globals seed it.

`execution_guard(*harness)` profiles Python calls by module globals and source
path and exposed C-call events in **all threads, including raw `_thread`
workers**. On Python 3.12+ (verified separately on 3.14), entry calls
`threading.setprofile_all_threads(profile)` for already-running threads and
`threading.setprofile(profile)` for later `threading.Thread` workers. During the
scope it wraps `_thread.start_new_thread`, `_thread.start_joinable_thread` and
`threading._start_joinable_thread` when present, plus the 3.11
`threading._start_new_thread` alias. Each bootstrap installs `sys.setprofile`
before calling its target. A C-call fence rejects prebound raw starter aliases
that would bypass the bootstrap. Pending/active bootstraps are registered before
startup; guard exit performs a bounded five-second join while profiling remains
active, then rejects any unfinished worker. This covers AnyIO shutdown, which
signals its workers without joining them. Live events are retained by thread ID,
so a raw worker that finishes before the frame-based exit check is still detected.
Worker profiling continues through native exception cleanup, including
`sys.unraisablehook`. Main/default hooks and starter APIs are restored on
successful and failed guard exits.

Pre-existing application workers still refuse the frozen leg, and frame checks
reject leaked workers. The only infrastructure exceptions are xdist's active
execnet receiver stack and pytest-timeout's Timer target, not application thread
names; profiling still applies to them. The supplied worktree's resolved venv is
Python **3.11.15**: it lacks `setprofile_all_threads`, so it uses the
existing-worker refusal plus instrumented new-worker bootstraps. The retroactive
all-thread installation is specifically a Python 3.12+ guarantee. Live calls are
recorded even if application error handling swallows an exception.

At entry and exit the reference walk starts at **every module object** in the
frozen namespace, the fake Spanner module, explicit harness roots, and the
frozen leg's supplied call arguments (including the request body). It follows
`gc.get_referents()` recursively with a strong visited-id map. Cycles terminate;
the **2,000,000-object bound raises**, never silently truncates a walk. Retaining
the visited objects prevents identity reuse. There is no user-container protocol
iteration, overridden property evaluation or `__getstate__` invocation.

`tp_traverse` supplies the edges for dict keys/values, slots, closures, partials,
bound methods, mapping proxies, class dictionaries, descriptors, dataclass
fields/defaults and GC-visible C containers. Known atomic references require
supplements, each demonstrated by a test asserting that raw GC returns no
referents despite retaining a Python object. Native code member descriptors
expose `co_consts` and metadata, including string/bytes subclasses with held
callbacks. Native `datetime`/`time` descriptors expose `tzinfo`; native
`timezone` methods expose the retained offset and name. These accessors avoid
subclass properties. The atomic-constants, atomic-metadata and atomic-tzinfo
tests run on 3.11 and 3.14; removal mutations cover every demonstrated field.

Native frame references also need a supplement: CPython 3.14 omits a running
frame's locals from GC, and a detached suspended generator frame has the same
gap. These are held Python objects inside the existing reference scope.

| Reached object | Native traversal |
|---|---|
| Frame | Iterate `f_locals.values()` directly (the 3.13+ `FrameLocalsProxy`, without calling `locals()` or copying the traversal frame); follow `f_globals`, `f_back`, `f_code`, and `f_trace`. Skip `f_builtins`, the interpreter builtins namespace; explicit fields replace generic frame GC edges so that this skip is version independent. Globals retain the existing module-registry boundaries; the strong visited map bounds cycles. |
| Traceback | Follow `tb_frame` and `tb_next`, in addition to GC edges. Every reached frame receives the frame supplement. |
| Generator / coroutine / async generator | Follow `gi_frame` / `cr_frame` / `ag_frame`, respectively, in addition to GC edges. The native types cannot override these attributes. |
| Exception | Follow native `BaseException` descriptors for `__traceback__`, `__context__`, and `__cause__`, in addition to GC edges, without invoking subclass properties. |

The reference walker never enumerates interpreter stacks or calls
`inspect.currentframe()` / `sys._current_frames()`. A frame must be held by the
existing frozen-module, fake-IO, harness or call-argument roots (or by another
reached object/frame). The deliberately live comparison leg is never an audit
root. The separate worker-refusal/profile checks retain their existing stack
inspection. Every reached `f_code` gets the same live-filename check as any other
code object. Frame/traceback/generator skip mutations verify these paths.

Every reached function **and code object** is checked against the live source
path using native string comparisons; namespace provenance also rejects live
generated definitions. Changing a function's module label or overriding a
filename subclass's comparison methods does not hide its source filename.

Provenance reads use native class/module dictionaries and sealed function/cache
types. Actual-type checks avoid overridden `__class__` properties, and native
string comparisons avoid overridden namespace comparisons. This keeps the
audit from invoking a provenance property that removes a nested held cache
before the GC walk visits it; a dedicated negative witness and mutation retain
that previously undetected construction. Native dictionary-item iteration and
string comparisons prevent metadata-dictionary methods or metadata-key equality
from removing held references. Class labels are memoized only within one audit
using identity keys; graph edges and callable results are never memoized.

The graph has explicit **external process-registry boundaries**, represented by
object identity, not by container kinds: registered external module dictionaries,
`sys.modules`, the logging registry and its registered logger objects. Frozen and
fake-Spanner module dictionaries are included. An explicit root overrides a
registry boundary, including a module dictionary or registered logger supplied
directly. Synthetic function globals that are not registered external module
dictionaries are ordinary held state and are traversed. These boundaries are
necessary in a shared interpreter: even a clean frozen module reaches
`__builtins__ -> frozen_import -> globals -> sys -> sys.modules -> live modules`.
The boundary/override regression test documents this limitation; **unrestricted
reachability across these registries is not claimed**.

Reachable functools cache wrappers (`__wrapped__` and `cache_info`) are identified
by their sealed native type without attribute lookup, and collected
before clearing, including nested cache keys/results. Both router namespaces and
the harness participate in cache clearing. Only caches registered in typing's process-wide cleanup registry, including
`typing.Annotated`, are purged first because they retain the preceding live
leg's schemas; wrapped functions and captures remain inspected. On Python 3.14 typing looks up caches through a global registry, so the cleanup
registry supplies those otherwise unheld cache objects too. An explicitly
supplied cache root overrides that normalization. All other
cache state, including externally labelled callback caches, is inspected before
clearing. Every collected cache is then cleared through the native cache descriptor before
entering the frozen leg, even if the instance shadows its `cache_clear` method. A live wrapped function is rejected even
when a warmed cache would otherwise avoid executing its body. The per-callable
profiler remains the independent execution layer.

The reference scope is **all Python-visible GC referents plus the native frame
and atomic fields above from the roots, up to the explicit external-registry boundaries**, with an
asserting object bound. Thread execution is covered by all-thread profiling on
Python 3.12+, with guarded worker bootstraps and the existing-worker refusal on
older Python. The worktree venv is Python 3.11; the gate venv is Python 3.14.6.
The following exclusions explain the limits of that scope:

| Exclusion | Reason |
|---|---|
| `ctypes` / native memory outside Python-visible objects | `tp_traverse` cannot enumerate references an extension does not expose to Python's GC. |
| External processes and process registries across the boundaries above | Another process has a separate object graph; shared interpreter registries contain the deliberately live comparison leg and pytest infrastructure. Explicit roots override registry boundaries. |
| Computed lookups that resolve a name only at call time without holding a reference | There is no held object edge to traverse before the lookup executes. |

Python callbacks executed through these paths remain subject to the profiler in
the guarded process. The harness and profiler infrastructure are trusted.
Describe a missed case as an **undetected construction**.

All **40** principal constructions have detecting witnesses: the earlier 14,
plus a warmed cache inside a nested mapping in a slot, a live cache held in code
constants, a live cache in a frozen closure, a live callable in a frozen class
dictionary via a descriptor, and a live cache in a nested tuple inside a
frozenset inside a dataclass default. Code constants and closures are separate
cases. A further witness covers an unused live callable retained in a copied
fixture globals dictionary; the protected-header fixture now passes only its
required data, SDK key type and frozen classes to its callbacks. Further cases
require inspection of an external callback's cached live result and a shared
typing cache supplied as an explicit root, plus warmed live caches in the native
`tzinfo` fields of datetime and time objects. Seven more cover held caches in
code filename/name/qualified-name/line-table/exception-table metadata and
timezone offset/name objects; another covers overridden filename comparisons.
Another witness holds a live cache behind a provenance property that would
remove it if evaluated; two more cover metadata dictionaries and keys that would
remove a cached live result if their methods were invoked. Round 8 adds an
active frame attached to a frozen module, an exception traceback, a suspended
generator's `gi_frame`, a coroutine's `cr_frame`, and a traceback retained only
through exception `__context__`. All five require preflight reference detection.
The new cache witnesses use
independent warmed wrappers and remain dormant
inside the guard, so the reference layer itself must detect them. Mutation rows
stop individual graph kinds, remove the code supplement/bound/provenance check,
or remove the relevant thread/execution check, and must make the corresponding
witness fail. Additional existing tests cover partial arguments/keywords,
Pydantic validators, cached results, raw joinable APIs and native worker cleanup.

`test_production_import_fence` statically scans imports in **every** live Python
module, including imports inside functions and literal dynamic imports, banning
`tests` (and all children) and `frozen_f83bbaac`. A fresh interpreter separately
checks that importing `trusted_router` does not load the snapshot loader/alias.
The reviewer's copied-module reverse import is rejected by the same scanner and
is retained as a C-table mutation. Computed dynamic import strings are outside
the static fence; the fresh-import check covers only the package import path.
Only external library machinery remains shared: Python builtins/stdlib
(including dataclass generation, enum, decimal, JSON, datetime, threading),
FastAPI/Starlette/AnyIO/httpx for in-process HTTP, Pydantic for schemas/settings,
and installed Google/other SDK types. These supply runtime mechanisms, not live
router billing policy. The fake Spanner IO engine in `tests/fakes/spanner.py`
remains shared as the deterministic SQL interpreter; **real** SpannerStore and
feature-store methods all come from the snapshot. Test-owned clocks, lease UUID,
catalog inputs, crash injections and trace callbacks control both legs equally.
Fixture seed preparation happens before the comparison. The complete execution
inventory, per-callable source location and file pin are recorded in
[the Round-4 inventory](../async-settle-f83bbaac-inventory.md), with machine-readable
rows beside it. Inventory records are evidence, never an execution allowlist.

The differential compares response contents, actual SQL parameters and serialized
payloads, not merely execution labels. Both activity and benchmark outboxes are
enabled. The generation cost, finalized-input and generation TTL mutations remain
independent witnesses; Round 4 adds ordinary native-batch cost +1 and ordinary
partner-free classification mutations. Their frozen legs retain baseline amounts.

| Entry path | Flags: admission/protection | Oracle evidence |
|---|---|---|
| `/settle` and `/refund`, no negotiation header | false/false | F1 `complete_entry[*-no_header_off-*]` |
| Both routes, `async-v1` header and ordinary legacy body | false/false | F1 `complete_entry[*-header_off-*]` |
| Both routes, no header | false/true | F1 `complete_entry[*-no_header_protected-*]` |
| Snapshot header with protection on, admission off | false/true | F1 `test_f83bbaac_protected_header_rejection` (both routes), plus PR C `test_snapshot_dispatch_after_admission_rollback`; recovery/rejection is not legacy entry |
| Pre-C claim/refresh/enqueue and unresolved finalize | false/false | `test_async_settle_oracle.py::test_frozen_main_effects_and_operation_trace` (c2c8f606) |
| Dormant scheduler/error/park/dead/clamp/budget | off | `test_async_settle_drain_oracle.py::test_flag_off_frozen_drain_sql_state_response` (ecb79459) |
| Authorize/reserve and one-commit finalize | off | `test_async_settle_authorize_oracle.py`, `test_settle_c1_oracle.py` |

F1 has 28 positive cases × 2 routes × 3 flag/header states × 2 commit paths =
**336 full-path differentials**, plus two protected-header rejection differentials. Inline mode includes the atomic done INSERT. Repair mode
crashes after durable enqueue, advances the clock beyond the existing 60-second
initial delay, then drains through real finalize and retention. Comparisons
include response (excluding timing), intermediate and final durable dictionaries,
SQL **bytes**, parameters, types, batch boundaries, mutations and RPC deltas.
No expected billing algorithm is substituted for frozen behavior.

#### Four-path pricing evidence

| Paths | Compared fields | Scenario coverage |
|---|---|---|
| legacy sync; async enqueue+drain; duplicate same body then drain; fresh snapshot-sync | credit total_usage; key usage; both released holds; reservation actual/settled; authorization settled/cost/outcome/generation ID/finalized model; generation amount/model; intent model/selected endpoint; repair and persisted usage as detailed below | All 28 positive vectors, including zero usage/rates, cache conventions, tier boundaries and last-tier fallback |
| Same four paths | async payload/snapshot hash exactness; absence of hashes on legacy rows; done/body clearing/terminal_at; no outbox row for fresh snapshot-sync | Same 28 vectors; F1-001 separately records fresh-sync reservation retention failure |
| Same four paths | Same fields and window usage/negative balance where applicable | Five independent axes: catalog change, endpoint removal, debt, deleted key, day/week/month rollover |

Usage expectations come directly from each vector's `expected_normalized_usage`,
not from the handler, live normalizer or record builders. The assertion matrix
applies to all four paths for every positive vector and successful scenario:

| Usage component | Actual repair payload | Persisted generation | Authorization finalization record |
|---|---|---|---|
| Input | `actual_input_tokens`: uncached for Anthropic, total prompt otherwise; independently reconstruct both normalized input counts | `tokens_prompt` = normalized total prompt | `finalized_input_tokens` = normalized total prompt |
| Cached input | `cache_read_input_tokens` = normalized cache-read | `cached_input_tokens` | `finalized_cached_input_tokens` |
| Cache creation | `cache_creation_input_tokens` = normalized cache-creation; participates in reconstructed total/uncached input | Included in total prompt; no separate cache-creation field exists | Included in finalized input; no separate cache-creation field exists |
| Output | `actual_output_tokens` = normalized output | `tokens_completion` | `finalized_output_tokens` |
| Reasoning | `reasoning_tokens` = normalized reasoning | `reasoning_tokens` | `finalized_reasoning_tokens` |

The proof observes actual constructed `SettleOutboxRow` payloads on every path,
including legacy's inline-done intent and snapshot-sync's transient repair row.
For enqueue/drain and duplicate/drain it additionally reads the durable JSON
before completion clears it. Every observed payload is checked; duplicate replay
must preserve the whole durable state. Generation and authorization assertions
read back the stored records. Endpoint-removal's rejected legacy leg instead
requires unchanged state. This tests the existing storage shape without inventing
separate persisted cache-creation fields.

There are **33 four-path runs / 132 path executions**. Catalog-change axis:
legacy books 5 microdollars and the three snapshot paths book 2. Removal axis:
legacy returns HTTP 400 with unchanged durable state; snapshot paths book 2.
These are deliberate exact divergences, not forced equality. Fresh snapshot
sync creates no durable intent; legacy rows have no snapshot/payload metadata.
The deleted-key case removes the typed key row and key entity after authorize;
settlement still releases credit and finalizes the committed invocation.
The rollover axis ages all three key spend windows while retaining the held
credit reservation; the credit ledger itself is cumulative, without a period
reset. Existing C1 clock-boundary tests cover a rollover during a transaction.

#### §7 boundary map

Test IDs without a filename are in `test_async_settle_handler.py`, unless
prefixed F1 (`test_async_settle_proof_faults.py`). Existing evidence is referenced
rather than duplicated.

| §7 row | Test ID / limit of evidence |
|---|---|
| Crash before INSERT commit | F1 `test_transaction_fault_retry_identity[before_commit]` |
| Crash after commit before response | F1 `test_transaction_fault_retry_identity[after_commit]`; same-key replay |
| Lost commit response / unknown outcome | F1 `test_transaction_fault_retry_identity[lost_commit]`; `test_unknown_commit_retry_same_identity`; retry INSERT then original-state point-read |
| Enclave crash after pending | F1 fault test drains with no client continuation; `test_drain_and_status_ownership`; actual enclave process kill belongs to E/G |
| Enclave crash before handoff | Outside router durability; no INSERT means no router responsibility. E/G must prove client redelivery; F1 makes no durability claim |
| Duplicate same body | F1 four-path `duplicate`; `test_duplicate_conflict_expired_and_immutable` |
| Corrected/different payload | `test_duplicate_conflict_expired_and_immutable`, `test_accepted_amount_survives_evaluator_disagreement` |
| Sync fallback overlaps accepted | `test_sync_reconciles_accepted_intent_before_evaluation`, `test_sync_fallback_does_not_impersonate_worker_lease` |
| Reaper or sync before insertion | PR C `test_interleaving_sweep`, `test_concurrent_enqueue_vs_terminal`; native `test_native_overlapping_async_transactions` |
| INSERT before reaper | `test_enqueue_wins_reaper_and_legacy_fence`; native overlapping test |
| Drain crash before finalize | PR D `test_concurrent_claims_and_owner_crash_fences`; lease remains pending; stale owner cannot mark/park |
| Drain crash after finalize before mark | F1 `test_finalize_commit_crash_before_mark`: committed counters survive lease expiry; new worker repairs body clearing/retention without another charge |
| Lease expiry / old worker | PR D `test_concurrent_claims_and_owner_crash_fences` (strict `<`, equality cannot reclaim); `test_batch_tail_expired_claim_never_applied` |
| Backlog/SLO | F1 `test_two_replica_stale_cap`; PR D health freshness and adversarial health matrices; no measured SLO claimed |
| Pause / latch / key deletion | D3 table below; F1 deleted-key settlement axis; no claim that an already executed invocation is revoked |
| Refund / sibling winner | F1 `test_concurrent_sibling_refund_full_money`; native `test_native_sibling_refund_first_claimant`; PR C `test_sibling_refund_reports_charge_winner` |
| Typed store unavailable / deterministic error | F1 `test_typed_unavailable_holds_and_recovers`; `test_settle_outbox_drain.py::test_deterministic_apply_error_parks_without_burning_attempts`; frozen drain error/park/dead matrix |
| Ticket lifetime / expired lookup (additional §9) | `test_async_settle_ticket.py::test_ticket_lifetime_above_300s_is_rejected_at_signing_and_verification`; `test_expiry`; PR C expired duplicate and expired snapshot-sync tests |
| Done-body clearing (additional §9) | F1 four-path matrix; retention mutation below |

The fake sibling race overlaps completed money transactions immediately before commit.

The native sibling test starts both read-write transactions before either
claim, retries Spanner aborts, and verifies the single reservation winner and
actual amount. Its fake counterpart checks full ledger release and final
outbox polarity. The native test uses `native_emulator_resources`, skips without
an emulator, and must run in CI. A fake serialization model alone is not
native-Spanner evidence.

#### D3 propagation audit

Paths below are relative to `src/trusted_router`; line references refer to the
unchanged f83bbaac production sources. Bounds start **after the relevant write
commits**, and concern subsequent decisions, not requests already in flight.

| Cache or visibility boundary | TTL / lag | Pin / evidence | Bound or qualification |
|---|---|---|---|
| Local folded key/workspace lookup, `storage_gcp.py:2564`, gateway `:804`, `:812`, `:848` | No positive process cache; strong snapshot | Existing folded authorize operations tests; frozen authorize oracle | Next new strong read sees committed deletion/rotation/pause; an older in-flight read may finish |
| Credit reserve and pause predicate, `storage_gcp_authorize.py:605–637`, `:726` | Transactional authoritative counters; no balance cache | C1/authorize differentials; conformance | Serializes against counter writes; pause predicate follows the existing trust setting |
| Exhausted-key negative LRU, `storage_gcp_authorize.py:200`, `:236` | No time expiry; hit rechecks authoritative state | Existing key lifetime precheck tests | No cached positive authorization; cannot extend revoked access |
| Credit shard-count cache, `storage_gcp_credit_shards.py:19`, `:55`, `:138` | `DEFAULT_CACHE_TTL_SECONDS=60` | F1 `test_local_ttl_pins` | Layout only, not cached credit/trust values; old prefix can cause conservative denial |
| Workspace pending/trust/pause/latch admission, `services/async_settle.py:35`, `:83–105`; query `storage_gcp_async_admission.py:14–28` | `CACHE_SECONDS=5`, timestamp at read start | F1 local TTL / two-replica stale-cap tests; B freshness tests | Accepting old eligibility for strictly less than 5 s is intentional; stale/failed refresh is false |
| Fleet publish claim, worker settings and `storage_gcp_async_admission.py:97` | Default ≤2 s between claim opportunities | F1 local TTL pin; PR D fleet cadence tests | A scheduling interval is not a successful-publication latency guarantee |
| Fleet consume, `services/async_settle.py:71`; decode `:179` | At most one refresh per second; source timestamps expire at 5 s | F1 `test_auxiliary_cache_and_health_cadence_pins`; PR D non-rejuvenation tests | Healthy nominal publish+consume lag ≤3 s plus IO; failed/slow publication instead becomes ineligible at source age 5 s |
| Ticket validity, `async_settle_ticket.py` | `MAX_TTL_SECONDS=300` | F1 local TTL pin; existing signer/verifier lifetime tests | Not a revocation cache: expired lookup can identify existing work, never admit a new async intent |
| Legacy/fast worker lease, `services/settle_outbox_drain.py:38`, Settings | Default 300 s | F1 local TTL pin; PR D equality/takeover test | Recovery delay, not authorize visibility; current defaults do not prove the 60 s SLO |
| Outbox absence capability cache, `storage_gcp_authorize.py:1087` | Negative 5 s, positive until process restart | F1 auxiliary pin | Schema availability only; no customer authorization facts cached |
| Broadcast-empty cache, gateway `:302`, `:2699` | 60 s | F1 auxiliary pin; existing broadcast TTL tests | Notification side effect only; not authorization eligibility |
| Foreign-key metadata, `services/federation.py:55`, `:58`, gateway `:3102–3167` | Soft 900 s; hard 86400 s during home failure | F1 `test_federation_excluded_from_async` | **Outside D3 async scope**: nonlocal authority cannot obtain a ticket or new async acceptance (§2) |
| Federation negative lookup, `services/federation.py:62`, `:213` | 60 s | F1 auxiliary pin / federation exclusion invariant | **Outside D3 async scope**; can delay recognizing a newly valid foreign key |
| Spanner commit → new strong snapshot / transaction | Commit visibility; no application replication TTL | Code uses `snapshot()` without stale-read options; native conformance | Not a local wall-clock RPC/outage bound; prior in-flight snapshots may predate commit |
| Serving regional replicas of one authority | Independent process caches, same strong authority database | F1 two-cache disagreement model | Cache ages are per process, not a fleet cap or coordinated invalidation |
| Independent regional/cloud authority instances | No common credit ledger; federation metadata revalidation above | Standalone-deployment decision record | No invented global visibility guarantee; PR G regional fault measurements required |
| Mounted ticket trust/signing config and feature settings | Loaded into application runtime; rollout/restart propagation | B load-runtime and rollout pins | Not an API-key revocation TTL; no bounded fleet rollout time proven locally |

For the **local async cohort**, new key/workspace authorization has no positive
cache lag after a strong read; cached async eligibility/health can remain true
for **<5 seconds**. A conservative inventory bound including shard-layout and
negative-discovery delays is **60 seconds**, not a sum: those caches do not
serially refresh one another's positive authorization evidence. Healthy health
publication plus consumption is nominally 2+1 seconds, but source-age rejection,
not an IO latency assumption, provides the five-second fail-closed rule.

D3 applies to the **local async cohort**, not federation (§2). Both requested
nonlocal authority and a nonlocal reservation reject ticket issuance; even an
otherwise valid local ticket presented with federated observed authority cannot
obtain new async acceptance. Federation's cache TTLs therefore do not violate D3.
Configuration rollout has no local fleet bound, and local tests cannot measure
regional partitions, scheduler stalls or process convergence.
The actual serving configuration must be checked by PR G. Key deletion does
not cancel already accepted/executed billing; async settle verifies the bound
ticket and current admission evidence, not a fresh customer-key lookup. An
abuse latch changes trust/async eligibility; it is not necessarily a ban on
ordinary synchronous inference. The audit must not conflate these decisions.

#### Cap semantics and pins

`services/async_settle.py::TIER_CAPS` is exactly
`{2: 25_000_000, 3: 100_000_000}`. Tier 1 is ineligible. The effective threshold
is **`pilot_cap or TIER_CAPS[tier]`**, not `min(pilot_cap, tier_cap)`: zero selects
the tier threshold; any nonzero configured pilot threshold overrides it,
including an override above the tier value. Admission is **inclusive** at the
threshold (`pending_micro <= threshold`); the next microdollar is rejected.
`admission_reason` reports `not_eligible` for tier 1 and `cap_exceeded` above
that same threshold. F1 `test_cap_semantics` pins 3 tiers × 3 overrides × 3
boundary values = 27 cases, including a $200 override.

The query counts workspace-owned pending and dead rows, including active
leases, legacy rows with workspace ownership and NULL-created rows. It does
not count done rows or rows with NULL/different workspace ownership. Its inner
`LIMIT 1001` is a fail-closed sentinel above `ROW_LIMIT=1000`; it is not a
partial sum accepted as complete. F1's fake-backed count test and native
`test_native_admission_counts_leased_and_dead_excludes_terminal_and_null` pin
these facts. Five-second-old cache entries cannot authorize from stale facts;
a successful fresh read may re-enable eligibility. Read failures remain false.

As §6 states, **`pending <= C + sum(actual_i for i in I)` is an admission/lag
guard, not a linearizable cap**. Multiple serving caches and already-issued
work can exceed C by many requests. No numeric C-only exposure bound follows.
The $5 pilot versus $25/$100 tier policy, and guard versus coordinated hard cap,
remain Joseph's decision in §10 Q4. F1 changes no behavior or threshold.


#### Mutation witnesses

| Required class | Mutation witness |
|---|---|
| INSERT uniqueness | C `insert-uniqueness-fake`; native overlapping INSERT test |
| Preserve-existing | C `preserve-existing` |
| Payload equality | C `refresh-conflicting-payload`, `drop-hash-comparisons` |
| Signature/binding | B `skip-jws-purpose`, `drop-key-id-binding` |
| Exact amount | C `amount-comparison-removal` |
| Ordinary native-batch cost helper | C `oracle-native-cost-plus-one` |
| Ordinary partner billing classification | C `oracle-ordinary-partner-free` |
| Persisted selected model identity | C `persist-wrong-model` |
| Independent generation builder | C `oracle-generation-amount-plus-one` |
| Independent authorization finalization builder | C `oracle-finalization-input-plus-123` |
| Finalized output usage | C `handler-zero-output-usage` |
| Exact HTTP error literal | C `error-envelope-message` |
| Admission predicate | C `skip-admission-recheck`, `atomic-settled-predicate` |
| Lease fence | D `remove-claim-lease-fence`; PR D owner-conditioned mark/park checks |
| Reaper guard | C `reaper-guard`, `claim-not-exists` |
| Retention clearing / generation TTL | D `retention-body-clear`, `generation-future-terminal-at` |
| Cap arithmetic | B `cap-arithmetic-exclusive`, `pilot-min-instead-of-override` |

The executable tables contain B **10**, C **92**, and D **14** mutations
(**116 total**, retaining all 72 Round-6 rows). Round 7 adds 44 reference and
execution-guard mutations, including one for every principal construction.
Round 5 incorporates the ten
independent seed-4 reviewer edits, both money-changing thread/cache bridges,
and the production-import witness. Round 4 added both ordinary-cost
helper corruptions, witnessed by
`test_async_settle_proof_oracle.py::test_f83bbaac_complete_entry[inline-no_header_off-settle-component_half_up]`,
and the wrong-model intent, witnessed by
`test_async_settle_proof.py::test_four_path_billing_state[component_half_up]`.
The generation/finalization builder and handler output corruptions remain.
Collection/import errors never count as detected mutations. See the
[Round-7 verification report](../async-settle-pr-f1-round7.md) for current results
and the reviewer witness matrix; the Round-4 model-identity assertions remain.

The fake now explicitly requires the claim's `NOT EXISTS`, the atomic
reservation's `settled=false` (existing check retained), the enabled immutable
refresh fence, the sparse `unresolved_at IS NOT NULL` predicate (existing check
retained), both control primary-key predicates, and the admission `LIMIT 1001`
sentinel. `test_fake_rejects_dropped_predicate` executes the real builder output
first, then deletes one predicate at a time and requires the fake to reject it.
The refresh expectation is explicit on the test database, so historical
flag-off frozen SQL remains valid.
