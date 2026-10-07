# Abliterate

Provider slug: `abliterate`. Base URL: `https://abliterate.ai/api/v1`.
This is **abliterate.ai**, not abliteration.ai.

The operator key is `ABLITERATE_API_KEY` locally and
`trustedrouter-abliterate-api-key` in the cloud secret stores. It must not be
mounted into the control plane or committed to source control.

## Catalog

| TrustedRouter model | Input USD/M | Output USD/M |
| --- | ---: | ---: |
| `abliterate/abliterate-0.3-fast` | 0.50 | 1.00 |
| `abliterate/abliterate-0.3-balanced` | 0.90 | 2.00 |
| `abliterate/abliterate-0.3-clever` | 2.50 | 5.00 |
| `abliterate/abliterated-research-0.1` | 2.50 | 20.00 |

These are upstream rates, before TrustedRouter fees, verified October 2, 2026.
Discovery reads the provider's `/models` endpoint and joins the current
[documentation prices](https://abliterate.ai/docs). The refresh reads public
documentation assets as data; it never executes them. Missing or ambiguous
prices fail closed. No context size, tool-input support, or structured-output
capability is inferred from the model names.

## Estimated Billing

On October 6, 2026, the operator approved conservative estimated billing for
these four routes because JSON and SSE still omit token usage. When a successful
prepaid response has no usage, the enclave buffers its local input and output
token estimates by up to 2x. It never adds more than the authorized request
budget permits, and the output buffer cannot exceed the requested output limit
(512 when unspecified). The published per-token rates themselves are unchanged.

Responses expose `usage_estimated=true`, with the same counts sent to settlement.
`/v1/models` exposes the policy as `trustedrouter.usage_estimation`. Provider and
model pages disclose it. Verified provider usage takes precedence if it becomes
available; partial provider usage receives no extra buffer. BYOK, interrupted,
failed, and empty responses do not receive the buffer. Settlement retries reuse
the same counts and existing idempotency protocol.

The buffer is a billing policy, not a guarantee about upstream cost: hidden
reasoning, research, internal tool calls, and tokenizer differences cannot be
verified without upstream usage. Monitor provider invoices against collected
revenue. Do not advertise these counts as exact or infer new model capabilities.

Deploy the gateway policy before publishing active routes. Only the four
reviewed models can enter this policy; new models require review. Refreshes
still require public prices and successful non-empty response canaries. A
timeout, missing price, or failed canary keeps the affected route unavailable.

The October 6 direct check returned successful JSON and SSE for Fast, Balanced,
and Clever. Research returned about 3,900 characters with `max_tokens=16`, so
it retains the separate `upstream-output-limit-unenforced` operator hold.
Estimated billing does not authorize unbounded generation. Do not clear this
hold until the provider enforces output limits and bounded canaries verify it.

## Privacy

[Service terms](https://abliterate.ai/terms) claim local browser chat history
and no server-side retention after completion. They also say requests transit
another inference provider. That downstream retention and attestation are not
established. Do not label these routes Confidential, E2EE, or end-to-end ZDR.
