# Gemini 4 Carbon admission

Verified 2026-11-10. Public model ID: `google/gemini-4-carbon`.
Native ID on both Google AI Studio and Vertex: `gemini-4-carbon`.

- AI Studio's authenticated models API lists Carbon with inputTokenLimit
  1,048,576 and outputTokenLimit 32,768. The published context is a unified
  input/output budget, not one million input plus one million output.
- Vertex's authenticated publisher lookup confirms GA. Native generateContent
  in our project returned PONG, STOP and valid usage on both providers.
- AI Studio's OpenAI-compatible streaming request returned PONG, usage and
  `[DONE]`. A forced tool call returned the requested function and arguments.
- Advertise the compatible API's 32,768 output limit, not the separate native
  long-decode continuation API's million-token maximum. No long-decode support
  is claimed by this addition.
- Standard upstream input/output/cached-input rates per million are
  $2/$10/$0.10 through November 30, then $4/$20/$0.20 from December 1 (UTC).
  Vertex publishes the introductory effective rates as credits-back pricing.
  Customer pricing retains TR's existing markup. Batch, contributor and
  priority rates are not used as Standard prices.
- Date selection happens outside the rewriteable parser; malformed or
  discontinuous schedules fail closed. Spike approvals match exact reviewed
  routes, full old/new tariffs and the effective date only.
- Google fixes Carbon's sampling: the Vertex model card lists temperature
  1.0, topP 0.95 and topK 64 as "fixed; custom values ignored", and the
  Gemini API guide says Gemini 4 and Gemini 3.6+ models ignore custom
  `temperature`, `top_p` and `top_k` and return deprecation warning headers.
  Both Google manifests therefore carry an exhaustive `supported_parameters`
  array for Carbon. Native exhaustive arrays outrank the OpenRouter snapshot,
  whose Google endpoints still list `temperature`/`top_p`/`seed`/`stop`; the
  hourly refresh preserves the array (2026-11-10 22:22Z refresh failed CI
  when the snapshot first listed Carbon and widened its parameters).
- Carbon is not approved for strict ZDR or Confidential. No existing provider
  privacy flags or automatic/combo model preferences were changed.

Sources:

- https://ai.google.dev/gemini-api/docs/models/gemini-4-carbon
- https://ai.google.dev/gemini-api/docs/pricing#gemini-4-carbon
- https://ai.google.dev/gemini-api/docs/changelog
- https://cloud.google.com/vertex-ai/generative-ai/pricing
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/4-carbon
- https://aistudio.google.com/llms-full.txt (text generation guide note on
  Gemini 4 sampling parameters)

Regression coverage: discovery, independently parsed Standard tariffs,
November/December boundary, malformed schedules, sandbox whitelist, scoped
price-spike approvals, both catalog routes and privacy exclusion.
