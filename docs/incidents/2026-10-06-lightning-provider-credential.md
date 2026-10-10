# Lightning AI operator credential rejection, October 6, 2026

## Confirmed Cause

This incident affects the Lightning AI inference provider (`lightning`), not
LightningRouter's BTC funding service or Lexe. LightningRouter's public health
check reports ready, no uncredited payments and no review backlog; a bounded
48-hour Cloud Run error-log read found no funding-service errors.

At initial investigation, the deployed `trustedrouter-lightning-api-key` secret
matched the old local `LIGHTNING_API_KEY`. Direct requests with that credential to
`https://lightning.ai/api/v1/chat/completions` returned HTTP 401 and
`unauthorized` for all three tested models:

* `lightning-ai/glm-5.3-flash`
* `openai/gpt-5.2-2025-12-11`
* `lightning-ai/gemma-4-31B-it`

Both authenticated and unauthenticated `GET /api/v1/models` returned HTTP 200.
The listing included all three models. The inference credential is rejected;
the surviving evidence does not distinguish expiration, revocation, or another
account-side credential restriction. Do not describe this as insufficient
credits or provider downtime without further evidence.

Recent gateway logs show the same fast upstream 4xx failures across multiple
models and gateway regions. Sentry groups QUILL-ROUTER-9B and QUILL-ROUTER-9E
report sustained configured-route authentication failures. These aggregate
probe alerts are not proof that a particular customer's request failed.

## Why It Remained Routable

Lightning's hourly discovery refresh reads a public catalog, not an
authenticated inference health check. It continued to find priced models even
with a rejected operator credential. The configuration alerts correctly caught
the failures, but do not automatically mutate routing policy. No reviewed
prepaid-account hold had been set for Lightning AI.

## Credential Repair

The operator replaced `LIGHTNING_API_KEY` on October 6. Minimal direct paid-path
canaries with that replacement returned HTTP 200 with output for GPT-5.2 and
hosted Gemma. At 2026-10-06T13:12:26.916156Z the replacement was published as
version 3 of the existing `trustedrouter-lightning-api-key` secret; a readback
confirmed it matches the local key. No other secrets or IAM bindings changed.

The temporary provider-wide hold was prepared locally but never deployed. It
was removed after the replacement credential passed those canaries. Regression
tests simulate a rejected credential, including fresh explicit discovery, and
verify independent BYOK and other providers remain available. The existing
Google passthrough exclusion remains unchanged.

GCP gateways read this secret at boot. At rotation time an existing guarded
gateway rollout was active (Actions run 37454912788), with another release
queued. Do not start a competing rollout or mistake secret publication for
fleet-wide recovery. Confirm boot-time pickup and provider-pinned production
canaries before closing the authentication incident. The queued release's CI
was red on a Go test data race; do not bypass that gate to refresh credentials.
A provider-pinned US West production canary after rotation still returned
HTTP 401 (`provider_error`), confirming old credentials remain in running
gateways. Its request ID is
`lightning-key-rotation-fca9994cc6a244f2959e34b74a70e9e9`.

The concurrent gateway release task was notified and asked to run the same
tiny, no-fallback Gemma canary in each refreshed region after its normal
guarded rollout. Gateway PR #466 passed CI and GitGuardian, then merged as
`e90dc7e432b23f602f2e5734f9a4f52520b63b84` at 13:24:24Z. Its automatic
deployment is Actions run 37470541349. Its rollout and secret pickup are still
pending verification, not completed recovery.

## Remaining Provider Failure

With the working replacement key, `lightning-ai/glm-5.3-flash` returns HTTP 500
containing an upstream `404 page not found`, in both streaming and non-streaming
requests. The model remains listed in Lightning's public catalog. This is
distinct from the old key's 401. Lightning must repair or retire that endpoint;
a successful catalog fetch or key rotation does not demonstrate its recovery.
Keep the model-specific issue open until inference succeeds. No alert
thresholds, billing rules, funding service, or attestation checks were weakened.

Provider escalation: a key that successfully runs
`openai/gpt-5.2-2025-12-11` and `lightning-ai/gemma-4-31B-it` fails specifically
for the advertised `lightning-ai/glm-5.3-flash` route at
`POST https://lightning.ai/api/v1/chat/completions`. Both `stream=true` and
`stream=false`, with a 32-token cap, return HTTP 500 containing
`status code: 404` and `404 page not found`. Ask Lightning to repair the
upstream route or remove it from discovery. Do not send credentials with the
escalation.

## Validation

Ruff and mypy passed. The focused Lightning pricing, routing and incident
suite passed all 33 tests. The full suite completed with 22,569 passing,
1,199 skipped, 12 expected failures and two failures. Both failures also
reproduce on unmodified main (`702bbf782`): MiniMax M3's advertised context
window and the 1M-context alias membership assertion. They are unrelated to
the credential rotation; no runtime catalog policy changed in this patch.
