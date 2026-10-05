# Provider recovery, October 5, 2026

## StreamLake

The adapter only parsed three KAT models and tested one shared canary. Once
those exact models retired, a provider-wide refresh exclusion prevented any
replacement from being discovered. The OpenAI-shaped inference gateway does
not implement `/models`; it returns `Missing Action parameter`.

Discovery now joins the provider's official model overview and USD/M pricing
tables. Each new or unhealthy model must pass its own paid-path canary with
authoritative integer usage and a nonempty message (including reasoning).
The hourly workflow again loads the StreamLake credential. Exact KAT
retirements remain enforced; a future release cannot revive them.

Sources:
- https://www.streamlake.ai/document/DOC/mh1gbfvrdn6hpbzxixv
- https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce

The bounded live refresh found 35 priced catalog models: 28 passed and seven
failed. Failures remain quarantined. A separate DeepSeek V4.1 Flash probe
returned `EndpointNotFound`; a published name is not proof of availability.
The immutable V4 Pro 0813 leaf retains its existing reviewed provider set;
its StreamLake manifest row does not change older combo routing.

Prices are selected by header, not column position. MiniMax M3 retains both
context tiers at 512K. Automatic prefix-cache prices are distinct from
explicit cache-read/write API rates. Unsupported mixed thinking-mode and
explicit-cache-only prices are not guessed.

## SambaNova

All six currently advertised models passed direct inference with integer
prompt, completion and total counts: DeepSeek V3.1, V3.2, Llama 3.3 70B,
MiniMax M3, Gemma 4 31B and GPT OSS 120B. The deployed GCP secret matches the
tested local key. The October 3 inference rejection is no longer reproducible;
the provider-wide Credits hold can be removed. Crusoe's separate hold remains.
Future SambaNova discovery canaries require valid usage, not just HTTP 200.

## Krea

Authentication to `/jobs` succeeds. The current OpenAPI publishes Krea 2
Medium at $0.03 per text-to-image request. A real generation attempt returns
HTTP 402: the API balance is separate from workspace compute credits and
requires funding. Keep the existing route dark until a completed image canary.
No customer credit or guessed image result substitutes for upstream funding.

## Abliterate

Fast non-streaming and streaming requests succeed with text but no usage.
The paid-route hold remains on all four configured models. Cached/input/output
and internal tool or escalation usage must be authoritative before release.

No credentials, customer prompts or generated customer content are recorded
here. Claude CLI reported `loggedIn=false`; no Claude review was performed.
