# Polyphemus 1.0

Status: September 23 selector tariff verified in all four GCP regions.
Standalone-cloud rollout gates remain in force. September 21 evidence below
describes the historical launch tariff, not the current token-based fee.

`trustedrouter/polyphemus-1.0` is a Responses-only named model. Telluvian
recommends a concrete model, and TrustedRouter authorizes and executes that
model using its ordinary credits, provider routing, and settlement paths.

## Selector pricing (2026-09-23)

Use `/v1/modelSelect` directly, not `telluvian/gallery-1`. Generation remains
on TrustedRouter's independently authorized provider routes. No Telluvian
generation or hallucination-verification charge is added.

The TR selector costs **$0.05 per million prompt tokens**, explicitly approved
by Joseph. This is the retail tariff, not an invoice-matched upstream cost.
Telluvian's [routing documentation](https://telluvian.ai/docs/routing), checked
September 23, now says its proposed $0.05/M routing charge is not yet enabled.
Do not silently change the approved TR tariff or infer invoice charges from it.
No selector output-token or fixed request fee is charged. Normal shared ledger
rounding applies, with a one-microdollar minimum for a successful selection:
1,000 tokens cost $0.00005; 100,000 tokens cost $0.005.

The live selector still returns no usage or cost fields. Until it does, token
usage is explicitly **estimated**, using the existing text estimator on the
exact serialized conversation and tool definitions sent as `messages`:
`max(1, floor(UTF-8 byte length / 4))`. This is not a claim that Telluvian uses
the same tokenizer. Settlement stores `usage_estimated=true`; responses expose
`selector_input_tokens`, `selector_usage_estimated`, and `selector_token_basis`.
Actual per-request upstream cost remains unreported by the selector API.
The selected model's own usage, cache accounting, and token prices are separate.
Workspace token totals include both metered stages, each identified by its
model/provider and route type. Public response input/output tokens still describe
generation only; selector tokens appear in `provider_usage`.
Standard catalog prompt-price previews include the selector rate on top of the
generation price envelope. Their sum is an estimate, not a single shared tokenizer.

Failed selection still refunds the entire selector hold and falls back to Auto
without a selector charge. Existing idempotency keys and settlement/refund
machinery are unchanged. Pre-meter enclaves report zero input and output tokens;
their settlements retain the legacy one-microdollar fee during rolling deploys
and durable retries. Deploy the control-plane tariff before the new enclave
meter; a mixed-version selector settlement is capped at its frozen hold, so a
one-microdollar admission cannot become a larger token charge. To roll back,
restore the complete legacy tariff (zero prompt rate plus one-microdollar request
fee), not an all-zero tariff: a successful selection must have a positive charge.

## Historical launch pricing (2026-09-21, superseded)

The customer selector fee is **one microdollar ($0.000001)** per successful
selection, the smallest positive amount representable in the existing ledger.
This implements Joseph's instruction to charge the minimum. It is not a
minimum token charge and does not replace the selected model's token pricing.

The response must expose selection and generation charges separately, with
their sum in total cost. Each stage must use the existing authorization,
settlement, and refund machinery. A failed selection charges no selector fee;
a successfully settled selection is not charged again if downstream generation
fails or settlement is retried. No new ledger or floating-point billing math.

This retail price is an explicit product decision, **not** a claim about
Telluvian's upstream cost. The live `/v1/modelSelect` response supplies no usage
or cost fields. Its public hallucination-probe price is not a selector price.
Do not record the absent upstream cost as zero or claim a known margin.

## Selector contract

- Fixed endpoint: `https://api.telluvian.ai/v1/modelSelect`.
- Server-side bearer key; never forwarded from the caller or logged.
- Request: `messages` string, `xPerf: 0.9`, and optional scoped `sessionId`.
- Observed response: `model`, `reasoning.effort`, and `sessionId`.
- Shared, cloud-appropriate HTTP transport for connection reuse.
- Five-second deadline, bounded request and response sizes, no redirects.
- One attempt. No automatic retry of 429s or ambiguous network errors without
  a documented upstream idempotency contract.
- On timeout, upstream failure, unavailable selector, or an unusable model
  recommendation, refund the selector admission and continue through
  `trustedrouter/auto`. Keep caller tools, reasoning, and provider constraints.
  The response retains the Polyphemus ID and reports the fallback reason and
  zero selector cost. Auto uses its existing concrete-model-only policy.
  Authentication, privacy, replay, cancellation, and unresolved settlement or
  refund failures do not trigger fallback.
- A returned model is a recommendation, never a URL or routing authority.
  Resolve it unambiguously against the current TrustedRouter catalog and
  reauthorize it under the caller's constraints. Never recurse into another
  orchestration or user-defined model from an upstream recommendation.

## Cache-aware conversations

Send the existing top-level `session_id` on Responses requests. Generate a UUID
once per conversation, then reuse it with the same TR API key on every turn:

```json
{
  "model": "trustedrouter/polyphemus-1.0",
  "session_id": "3f2b7c58-9d41-4e0a-9a7c-6f0b1c2d3e4f",
  "input": [
    {"role": "user", "content": "What is the capital of France?"},
    {"role": "assistant", "content": "Paris."},
    {"role": "user", "content": "And Germany?"}
  ]
}
```

TR accepts session strings up to the existing 256-character attribution limit.
The enclave derives a deterministic UUIDv8 from a domain-separated HMAC keyed
by the caller's API key, and sends that as Telluvian's `sessionId`. Different
keys cannot join one another's selector session by guessing a caller session
label. Neither the original label nor the API key is sent as session metadata
to Telluvian. API-key rotation starts a new selector session. IDs are stable
across enclave restarts and regions; no new database or in-memory session map
is used. A provider-returned session ID is not trusted as a caller identity.

Without `session_id`, the selector request omits `sessionId` and remains an
independent one-shot selection. Telluvian forgets sessions after an hour of
inactivity. A session is not an idempotency key: each new turn still requires
its own request identity, selection authorization, and normal billing. It does
not store the conversation for the client; send the current history every turn.
The session ID does not enter the metered `messages` payload.

`usage.provider_usage.selector_session_supplied` reports whether the selector
attempt included a session. It does not report a cache hit or claim savings.
Telluvian can consider estimated warm-cache costs when recommending a model,
but TR still chooses and authorizes the actual provider independently. A model
can move providers or fail over, so this does not pin an endpoint or guarantee
cache hits. Check actual generation cache-read usage and costs; keep stable
prefixes and use provider routing constraints when provider continuity matters.
Selector failure still falls back to Auto with the original caller session
attribution preserved and no selector fee.

## Selector tariff release evidence (2026-09-23)

- [Control plane and public catalog](https://github.com/Lore-Hex/quill-router/actions/runs/35892796982).
- [Four-region GCP enclave rollout](https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/35893275726).
- [Sanitized verification summary](https://github.com/Lore-Hex/quill-cloud-proxy/pull/370#issuecomment-5801686459).

Live probes verified 1,053 estimated selector tokens charged at 53 microdollars
in each GCP region. JSON totals reconciled in all four regions; streaming
totals reconciled in all three US regions and on the canonical hostname.
Europe retained the existing streaming receipt limitation in issue #358.
This evidence verifies the tariff, not the later cache-aware session change.

## Privacy

Joseph explicitly changed the launch requirement on September 21: launch with
**standard privacy, not ZDR**. Telluvian receives the conversation and function
tool definitions. No selector-specific ZDR or attested inference claim is made.
No-store, ZDR, and confidential/E2EE requests fail before sending that context.
Provider and jurisdiction filters apply to selector admission as well as the
downstream model; a filter excluding Telluvian cannot be silently discarded.

## Local evidence

Eleven live synthetic selector calls succeeded. In a ten-call sequential run,
the nine calls after connection setup took 149.62-162.06 ms, median 154.34 ms.
The first call in that run took 469.46 ms; the initial standalone probe took
938.72 ms. This is one prompt from one local machine, not a production SLO or
regional p95. Selection time is additional to model generation time.

All eleven recommended `gemini-3.8-flash` with reasoning effort `high`.
The unprefixed result needs catalog resolution to `google/gemini-3.8-flash`;
it must not be treated as a pre-authorized route.

## Release evidence (2026-09-21)

Implementation:
- [Router and billing, PR #1264](https://github.com/Lore-Hex/quill-router/pull/1264).
- [Enclave and automatic fallback, PR #353](https://github.com/Lore-Hex/quill-cloud-proxy/pull/353).
- [Pin rollout scripts to the built source, PR #355](https://github.com/Lore-Hex/quill-cloud-proxy/pull/355).
- [Final AWS and Azure attestation pins, PR #357](https://github.com/Lore-Hex/quill-cloud-proxy/pull/357).

The selector credential is provisioned separately in GCP Secret Manager,
AWS Secrets Manager, and Azure Key Vault. It is not fetched from GCP by the
AWS or Azure enclave at runtime.

Local validation passed ruff, mypy, the full router test suite (11,857 passed,
472 skipped, 11 expected failures), enclave cloud-variant CI, and Claude CLI
Opus review. Focused HTTP/SSE tests exercise both successful selection and
injected selector failure. They require exactly one selector refund and no
selector settlement on fallback, independent authorization of
`trustedrouter/auto`, and preservation of the public Polyphemus model ID.
Additional tests cover timeout, missing selector, unusable recommendation,
privacy rejection, replay, cancellation, and failed refund/settlement.

Live synthetic `PONG` requests passed through GCP US Central and US East,
AWS Paris and Ireland, and Azure Dubai and Sydney. JSON and SSE responses
completed, reported
`trustedrouter/polyphemus-1.0`, and reconciled 14 microdollars of generation
with the 1-microdollar selector fee: 15 microdollars total. These are tiny
functional probes, not a latency benchmark or a fixed per-request price.
A confidential request returned HTTP 400 before calling Telluvian.

GCP Europe also passed the non-streaming cost check. Two European SSE probes
completed with the correct answer but omitted generation/total cost because
the existing Stage D two-second settlement deadline expired. Metadata-only
logs confirm the idempotent retry succeeded on attempt 1, 2065 ms and 1426 ms
after enqueue. No unknown charge was reported as zero. This shared streaming
receipt limitation is tracked separately in
[enclave issue #358](https://github.com/Lore-Hex/quill-cloud-proxy/issues/358);
do not describe all streaming receipts as immediately complete.

Fallback was failure-injected locally, not by breaking the production
credential or upstream service. Live success does not by itself prove an
upstream-outage scenario; the deterministic integration tests provide that
coverage.

Deployment records:
- [GCP enclave rollout](https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/35622463339).
- [Router and public catalog rollout](https://github.com/Lore-Hex/quill-router/actions/runs/35628469273).

AWS and Azure regional deployments passed their attestation gates and were
narrowed to the new measurements. The temporary AWS canary was terminated.
The router workflow completed all backend, public-service, and cloud-completeness
gates. The public catalog lists the model with Standard privacy, no BYOK, and
`selection_fee_plus_selected_model_tokens` pricing. Its
[model page](https://trustedrouter.com/models/trustedrouter/polyphemus-1.0)
returns HTTP 200 and explains the selector and fallback charges.

Use the linked GCP workflow for the final regional rollout conclusion rather
than inferring deployment completeness from a successful single-instance smoke.
