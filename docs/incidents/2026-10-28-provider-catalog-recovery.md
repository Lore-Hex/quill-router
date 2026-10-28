# October 28 provider catalog recovery

## Confirmed causes

- fal added a third H3 Max resolution and comma-separated post-promotion
  prices. The two-resolution parser rejected the unchanged 480p/768p prices,
  preventing a fresh catalog timestamp. Accept the published punctuation and
  spacing while retaining ambiguity and promotion guards. A fresh authenticated
  fetch passed with standard rates of $0.05/second and $0.08/second.
- StreamLake's exact `Qwen3-VL-235B-A22B-Thinking` inference route returned
  HTTP 400 `UnavailableModel` on October 28. Retire only that provider/model
  pair, observation-dated. Do not replace it with different weights.
- Pearl's `google/gemma-4-31B-it` returned HTTP 404 `model_not_found` and is
  absent from its current authenticated catalog. Hold that exact route even
  if discovery relists it. Other providers remain available.
- NVIDIA's `meta/llama-3.2-90b-vision-instruct` timed out on a direct 90-second
  probe after repeated production failures. The same credential returned a
  valid response from Nemotron Lightning. Mark the exact vision route as a
  recoverable failed canary, not a retirement or provider-wide credential hold.
  The existing refresh must obtain a successful canary before restoring it.
- The refresh candidate in Actions run 40649310638 exposed a generic Makora
  chat listing for `cloudflare/clef-flash`. Catalog merging made it inherit
  Regolo's hosted decision capability and bypass Regolo's regional restriction.
  Reject chat-typed Clef listings before constructing endpoints. Keep the
  reviewed decision adapter and regional checks intact.
- A historical SambaNova test coupled a MiniMax tombstone to three unrelated
  routes remaining unhealthy forever. Keep the tombstone assertions but allow
  unrelated routes to recover through their existing canary checks.

## Verification and boundaries

The fal punctuation, ambiguous-price, StreamLake retirement, Pearl hold and
generic Clef regression tests were observed failing before their fixes.
Provider probes used tiny synthetic requests, not customer content.

This change does not weaken Sentry alerts, deadlines, billing, attestation,
or fallback restrictions. It does not repair the separate European process
stall or implement the unsupported `include_reasoning` compatibility alias.
The process stall needs its own watchdog/diagnostic release and investigation.
Successful provider fallback warnings remain visible.
