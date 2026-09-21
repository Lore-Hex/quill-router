# Polyphemus 1.0

Status: merged; production verification recorded below (2026-09-21).

`trustedrouter/polyphemus-1.0` is a Responses-only named model. Telluvian
recommends a concrete model, and TrustedRouter authorizes and executes that
model using its ordinary credits, provider routing, and settlement paths.

## Pricing decision (2026-09-21)

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
- Request: `messages` string and `xPerf: 0.9` for this named version.
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
