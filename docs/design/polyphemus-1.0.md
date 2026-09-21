# Polyphemus 1.0

Status: implementation in progress; not enabled in production.

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

## Remaining release work

- Provision the credential separately in each cloud's secret store.
- Run full repository and cloud-variant gates and Claude CLI Opus review.
- Release through the documented regional rollout paths and verify production
  Responses calls, charges, streaming, attestation, and regional health.
