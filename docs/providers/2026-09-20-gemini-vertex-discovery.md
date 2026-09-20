# Gemini and Vertex discovery repair

## Root cause

Gemini 3.8 Flash was already in the public catalog through both AI Studio and
the OpenRouter-derived Vertex snapshot. The problem was the source of truth:
the hourly Gemini adapter discovered AI Studio models, while Vertex's native
supplement contained only Gemini 3.6 Flash. It repriced that supplement from
AI Studio and had no independent Vertex discovery or account-access gate.

The refresh now treats the two Google products independently:

- AI Studio keeps its existing models API, pricing page and key.
- Vertex lists the Google publisher catalog using the existing workflow ADC
  identity and `VERTEX_PROJECT_ID=quill-cloud-proxy`.
- Vertex reads standard global input, output and cached-input prices from its
  own pricing page. Context tiers and dated discounts are preserved. Priority,
  batch, non-global, audio and cache-storage prices are not mixed into chat.
- Every new or previously held supported chat route must return PONG, STOP and
  integer token usage from the project's global generateContent endpoint.
- Discovery failures cannot manufacture models. Failed canaries, missing
  prices, missing model limits and specialized APIs remain explicitly held.
  Manual safety holds survive refreshes. Repeated disappearance uses the shared
  tombstone and mass-prune guards.

AI Studio supplies public model-limit metadata, not Vertex availability or
prices. A genuinely Vertex-only model without verified limits stays held for
review. The job does not grant IAM roles or alter the two products' privacy
classifications.

## Live verification

On 2026-09-20 UTC, the native feed returned 26 Gemini IDs. Nine new/held chat
canaries passed using the same service-account identity as the price-refresh
workflow. Together with previously verified Gemini 3.6 Flash, ten chat routes
have complete current Vertex pricing and verified access:

- Gemini 2.5 Flash, Flash-Lite and Pro
- Gemini 3.1 Flash-Lite and Pro Preview
- Gemini 3.5 Flash and Flash-Lite
- Gemini 3.6 Flash, 3.7 Flash and 3.8 Flash

Gemini 3.8 Flash Cyber was absent from the publisher feed and returned HTTP 404
(not found or project access unavailable) on a direct probe. It is not published
as an available Vertex route. The current Gemini 3 Flash Preview pricing table
lacks a complete output-price row; that route is held instead of borrowing a
different model's rate. Non-chat publisher IDs remain classified but unroutable
through this adapter.

Regression coverage includes product-price isolation, model-name discovery,
pagination, malformed feeds, scheduled rates, cache/context tiers, bad/empty/
thought-only canary responses, failed-probe recovery, manual holds and actual
runtime catalog admission. Credentials and response content are not recorded.
