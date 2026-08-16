# Reverse-harness contract — serving a user-provided model to TrustedRouter

**Status:** v1 (2026-08-16). Implemented by the open-source `reverse-harness` (Apache 2,
TypeScript/Node reference: `npx @trustedrouter/reverse-harness`) and by any agent or machine that
wants to be a model. This document is the whole wire contract; the harness holds its own queue and
UI, TrustedRouter never sees them.

## 1. What you are

A **user-provided model** is an OpenAI-compatible HTTP endpoint you operate, registered under
`trustedrouter/user-<slug>` with a fixed price per million tokens. TrustedRouter's attested gateway
**pushes** requests to it (there is no pull/inbox); you answer, and you are paid 70% of the charge
in TrustedRouter credits into your earnings wallet (never cash). Three kinds share the contract:

| kind | who answers | price bounds (µ$/Mtok) | budgets connect / first-byte / idle / total |
|---|---|---|---|
| `machine` | a program | ≤ 1,000,000,000 ($1,000/Mtok) | 10 / 30 / 60 / 300 s |
| `agent` | an autonomous agent | ≤ 1,000,000,000 | 10 / 60 / 60 / 600 s |
| `human` | a person at a keyboard | 100,000,000,000 – 1,000,000,000,000 ($0.10 – $1.00 per token) | 10 / **300** / 120 / **900** s |

Your model is **not attested and not zero-data-retention**: TrustedRouter says so on your model's
page and on its trust page. Prompts and outputs are sent to you; what you do with them is between
you and your callers.

## 2. Registration (owner API, `Authorization: Bearer <management key>`)

Requires a verified account (email, phone, identity). `POST /v1/user-models`:

```json
{
  "name": "Ada, live", "slug": "ada-live", "kind": "human",
  "description": "A real person. Median first token ~40s.",
  "display_identity": "handle", "display_name": "ada",
  "endpoint_url": "https://ada.example.com/v1",
  "upstream_model_id": "ada",
  "endpoint_api_key": "optional-bearer-your-endpoint-checks",
  "supports_streaming": true,
  "heartbeat_interval_seconds": 30, "max_concurrency": 1,
  "prompt_price_microdollars_per_million_tokens": 100000000000,
  "completion_price_microdollars_per_million_tokens": 100000000000
}
```

`endpoint_url` must be **https**, resolve only to public addresses, and must not be a TrustedRouter
host. The response carries `signing_secret` **once** — store it; it is how you verify that a request
really came from TrustedRouter. `POST …/rotate-secrets` mints a new one (old signatures stop
verifying at the next request).

## 3. Being on the clock

| call | effect |
|---|---|
| `POST /v1/user-models/{id}/clock-in` | TrustedRouter **probes** your endpoint first (a signed canary request, §5) — 409 with the reason if it fails; then you are on the clock. Strikes from an earlier shift are cleared. |
| `POST …/heartbeat` | Extends your presence by `2 × heartbeat_interval_seconds`. Miss two intervals and you are treated as off the clock (no strike). Send it on a timer whenever you are on the clock. |
| `POST …/clock-out` | Off the clock immediately. Requests already in flight finish. |

Off the clock, callers get `503 model_off_the_clock`. **Three consecutive owner faults** (§7) clock
you out automatically; clock back in when you are ready.

## 4. The request you receive

`POST {endpoint_url}/chat/completions` (your `endpoint_url` with `/chat/completions` appended; no
redirects are followed — answer at that URL).

Headers:

```
Content-Type: application/json
Accept: text/event-stream, application/json
User-Agent: TrustedRouter/1.0
TR-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(signing_secret, "<t>." + body_bytes)>
Authorization: Bearer <endpoint_api_key>        (only if you registered one)
```

Body — the caller's OpenAI chat request, **allowlisted** to exactly these keys:
`messages, temperature, top_p, n, stop, max_tokens, max_completion_tokens, presence_penalty,
frequency_penalty, logit_bias, logprobs, top_logprobs, response_format, seed, tools, tool_choice,
parallel_tool_calls, reasoning_effort, metadata, stream_options` — plus two set by TrustedRouter:
`model` = your `upstream_model_id`, and `stream` = your registered `supports_streaming` (the
**owner** decides the transport, regardless of what the caller asked; TrustedRouter adapts). When
`stream` is `false`, `stream_options` is never sent. Nothing else about the caller (identity, keys,
routing preferences, TrustedRouter internals) is ever sent to you.

### Verify the signature

```
t, v1 = parse("TR-Signature")            # "t=1700000000,v1=a7597e2b…"
reject if |now - t| > 300 s               # replay window
expected = hex(HMAC_SHA256(signing_secret, str(t) + "." + raw_body_bytes))
reject unless constant_time_equal(expected, v1)
```

Sign the **exact bytes** you received; do not re-serialize. Test vector (secret
`test-signing-secret`, body `{"model":"demo","stream":false}`, t = 1700000000):
`t=1700000000,v1=a7597e2bfa4bc480b058f31a24542b3ab0c99fe6231ae15aa0498fd5bd1d4304`.

## 5. The canary (probe)

At clock-in — and whenever an operator asks — TrustedRouter sends a real signed request:
`messages: [{"role":"user","content":"Reply with the single word: pong"}], max_tokens: 16`. A
harness for humans should **auto-answer** it (any valid completion, e.g. `pong`) without waking the
person; the probe must complete within 20 s total and its body is capped at 64 KiB. A valid answer
resets your strike count.

## 6. What you answer

Exactly the OpenAI shape, in the transport you registered:

- **`supports_streaming: true`** — `Content-Type: text/event-stream`; `data: <chat.completion.chunk
  JSON>\n\n` frames; a final usage-only chunk (`"choices": []` with `usage`) is welcome; end with
  `data: [DONE]\n\n`. Type-as-you-go: each keystroke batch is a `delta.content` chunk. Lines are
  capped at 1 MiB and the whole stream at 64 MiB.
- **`supports_streaming: false`** — one `chat.completion` JSON body (`choices[0].message`,
  optional `usage`).

Include `usage` (`prompt_tokens`, `completion_tokens`) when you can; TrustedRouter estimates
otherwise. The charge is your usage at your price, **capped at what the caller authorized** (their
estimated prompt + `max_tokens` at your prices) — reporting more than that earns nothing extra.
Your `model` field is rewritten to the TrustedRouter id before the caller sees it.

**Declining is just an HTTP error.** There is no special decline protocol: return `4xx` (you
judged the request unacceptable) or `5xx` (you cannot serve right now). The caller sees a
TrustedRouter `502` naming the status; their hold is refunded.

## 7. What counts as your fault (strikes)

TrustedRouter records one **strike** per owner fault; three in a row clocks you out. Faults:
your endpoint unreachable / TLS or DNS failure (`connection_error`), first-byte / idle / total
budget exceeded (`user_model_timeout`), HTTP `5xx` (`provider_error`), unparsable body or stream
(`malformed_response`). **Not** faults: any `4xx` you return, the caller disconnecting, or a
TrustedRouter-side error. A successful answer resets the count; so do clock-in and a passing probe.

## 8. Timing, keepalives, disconnects

- While a streaming caller waits for your first byte, TrustedRouter sends them SSE comment
  keepalives; that wait is bounded only by your kind's **first-byte** budget (300 s for humans).
  After your first byte, the **idle** budget applies between bytes; **total** bounds everything.
- If the caller disconnects **before** any of your bytes reached them, the request is cancelled
  (your endpoint sees the connection close) and refunded; no strike. If they disconnect **after**
  receiving part of your answer, the delivered part is settled and you are paid for it.
- TrustedRouter measures your **time to first token** and total latency exactly as for any other
  model; a human's median TTFT is public on the model page. That is the point.

## 9. Concurrency

`max_concurrency` (default 4, humans usually 1) is enforced by TrustedRouter: beyond it callers get
`429` "at capacity" and are not dispatched to you. A slot is held from authorization to
settle/refund, at most one dispatch budget.

## 10. Getting paid

Every settled request credits **70% of the charge** (integer microdollars; TrustedRouter keeps the
rounding) to your earnings wallet, exactly once. Owners see the ledger and per-model earnings in
the console and can transfer earnings into any of their workspaces as spendable credits. Calling
your own model charges you full price and pays you 70% — it nets −30%.

## Appendix — minimal harness loop

```
on start:  POST clock-in (auto-answer the canary)              → on the clock
timer:     POST heartbeat every heartbeat_interval_seconds
on POST /chat/completions:
           verify TR-Signature; enqueue; show the human the messages
           human types → stream chunks (or one JSON body); or human clicks Decline → 4xx
on stop:   POST clock-out
```
