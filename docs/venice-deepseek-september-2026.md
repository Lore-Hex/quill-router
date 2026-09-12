# Venice DeepSeek Flash Migration

Venice's customer notice retires `deepseek-v4-flash` on September 15, 2026
and names `deepseek-v4-1-flash` as its replacement. The notice does not give
an hour or time zone. TrustedRouter conservatively stops selecting the old
Venice route at **2026-09-15 00:00 UTC**.

## Model Selection

- Existing TrustedRouter model: `deepseek/deepseek-v4-flash`.
- Replacement TrustedRouter model: `deepseek/deepseek-v4-1-flash`.
- Replacement Venice upstream ID: `deepseek-v4-1-flash`.

The hourly discovery feed already added the replacement. Explicit V4
requests do not silently become V4.1 requests. Other providers serving V4
remain eligible subject to the caller's routing constraints. Venice's
dated `0731`, `0731-fast`, and E2EE variants are not named by this notice
and are not retired by this change. GLM 5.3 Flash is not being retired.
Venice's recommendation is not independent evidence that V4.1 is better
for every workload.

## Verified Catalog Data

On September 12, Venice's public [text models API](https://api.venice.ai/api/v1/models?type=text)
reported the replacement online, with text and image input, function
calling, reasoning, and structured output. Context is 1,000,000 tokens;
maximum completion is 131,072 tokens.

The API reports upstream USD per million tokens: $0.375 input, $1.50
output, and $0.0075 cached input. These are provider prices, before
TrustedRouter's markup, and remain controlled by regular price refreshes.
Do not copy the old V4 prices onto V4.1. The API advertises neither TEE
attestation nor E2EE for this model; its vision support does not change
that privacy classification.

See also Venice's [replacement model page](https://venice.ai/models/deepseek-v4-1-flash).

## Enforcement

The effective-dated guard applies during catalog ingestion, live endpoint
selection, and pricing refresh. An already-running process or stale
provider feed cannot restore the old route after the cutoff. The manifest
keeps the migration metadata for operators.

Regression tests cover the exact boundary, provider and version isolation,
no silent weight substitution, stale-feed exclusion, replacement native ID,
integer prices, and image/tool/structured-output discovery metadata.
