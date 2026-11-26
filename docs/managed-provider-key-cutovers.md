# Managed provider keys: use the primary first

Status: disabled unless explicitly enrolled. This is a TrustedRouter internal
funding-event contract, **not** a webhook schema published by fal or MiniMax.
There is no scheduled check, request replay, top-up, or transfer of funds.

* fal: keep using the primary until fal itself returns HTTP 402 on a managed
  image generation or video queue submission with that key. The gateway
  reports the rejection to the control plane that issued the authorization.
  Once it records the switch, new managed fal authorizations from that
  authority use the standby. Other independent authorities switch on their
  own first qualifying rejection. No billing reader, auto-refill event or
  balance lookup is required. The rejected submission is not replayed.
  Customer balance errors, BYOK calls, video polls, timeouts and other
  HTTP statuses do not switch keys. Existing video jobs keep their original key.
* MiniMax: verify the subscription has finally ended and any separately
  usable credit packs are exhausted. Require a successful primary entitlement
  observation with no remaining entitlements and positive standby PAYG funds.
  The [plan usage API](https://platform.minimax.io/subscribe/token-plan) reports
  resetting windows. A window limit, frozen card, empty response or login error
  isn't proof the subscription ended.

**MiniMax only: a verified event source is a deployment prerequisite.** No provider lifecycle adapter
is configured by this change. It must authenticate the actual account and
current closure state for the exact ordered credentials. A funding rejection
may wake the verifier to re-read that state; it cannot certify it. Re-evaluate
on subsequent relevant funding/usage events if an earlier one found credit
remaining. Do not sign a manually assembled production event to test this.

In the protected verifier, `scripts/provider_funding_evidence.py:qualify`
performs only bounded metadata GETs and signs an eligible observation. The
inference keys, billing reader keys and signing key never go to the control
plane. The handler `on_verified_closure` sends the signed receipt to all
three cloud authorities and checks each acknowledgement. The event source
must handle partial delivery; a partial result is not fleet-wide success. Use its
dedicated funding token, not a general gateway or operator credential.
Fresh evidence is required after a receipt expires; transport redelivery
does not submit another inference. Do not claim a fleet-wide cutover from
one acknowledgement.

The MiniMax funding endpoint verifies the signature, audience, pair, age and funding
facts, then records an irreversible marker in the authority's own store.
New managed authorizations read that marker by exact key. An unavailable
marker store is not permission to use the old key. BYOK is unchanged.
Retain old slot material for existing queued video jobs until they are drained.

Release all compatible control planes and gateways through normal CI and
attestation before enrolling a pair. Pin the exact generation in
`TR_MANAGED_KEY_GENERATIONS`. For MiniMax only, provision `TR_MANAGED_KEY_RECEIPT_PUBLIC_KEY`,
`TR_MANAGED_KEY_RECEIPT_AUDIENCE` and a scoped `TR_MANAGED_KEY_FUNDING_TOKEN`
through reviewed deployment configuration. Confirm all authorities issue the
explicit primary decision before loading the standby. For MiniMax, no producer, no
verified final-funding source, a missing account binding or an unfunded
standby means no activation. Never remove a promoted generation or roll a
serving authority back to an incompatible version as a way to undo a cutover.
