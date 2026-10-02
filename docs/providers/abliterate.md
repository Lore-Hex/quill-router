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

## Activation Gate

Direct Fast canaries returned HTTP 200 and `PONG` in both JSON and SSE, but
neither included token usage. SSE also omitted usage with
`stream_options.include_usage=true`. Balanced SSE also returned content and
`[DONE]` without usage. Research SSE likewise completed without usage; Clever
exceeded the 75-second canary read timeout. These are response-format/accounting gaps, not evidence
of downtime. The manifests retain an explicit `upstream-usage-unavailable`
operator hold; an ordinary successful PONG must not remove it.

Before enabling prepaid routing, obtain and test upstream `prompt_tokens` and
`completion_tokens` for both response modes, including any internally billed
reasoning, research, escalation, and tool calls. Verify output limits and error
termination. Then remove the reviewed hold, refresh the manifest with keyed
canaries, deploy the gateway first, and test pinned requests through TR.

## Privacy

[Service terms](https://abliterate.ai/terms) claim local browser chat history
and no server-side retention after completion. They also say requests transit
another inference provider. That downstream retention and attestation are not
established. Do not label these routes Confidential, E2EE, or end-to-end ZDR.
