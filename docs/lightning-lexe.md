# Lexe backend preparation

Reviewed September 16, 2026. This is a backend-only migration. Do not change
LightningRouter's pages, QR presentation, API-key login, model selection, USD
balances, pricing, or existing 10% FX buffer.

## Current state

The operator created a dedicated **unfunded mainnet wallet** with the official
`lexe-sdk==0.1.19`, with SGX enabled. It is separate from the LND wallet.
The checkout credential grants only `read_info`, `read_payments`, and `receive`,
expires after 90 days, and passed effective-permission checks. No withdrawal,
channel-management, or admin authority was granted to checkout.

A disposable, unpublished 1,000-sat invoice was created, looked up, canceled,
and canceled again successfully using that restricted credential. No payment
was sent or received. Wallet balance and channel count remained zero.
The checksum-verified v0.4.20 sidecar also passed the live read-only preflight
with node version 0.10.4. The temporary local sidecar was then stopped.
On September 17, the production adapter also recovered a fresh, never-updated
pending invoice through the real node's `updated_payments` endpoint, matched
the signed invoice exactly, and canceled it. This verifies the recovery cursor
and creation-feed semantics independently of the test double. No funds moved.

The runtime now supports Lexe behind `LR_INVOICE_BACKEND=lexe`. A code merge is
not activation or proof of successful receipt. Use the staged rollout below.
Existing Bitcoin Core, LND, channel, backups, BTCPay, and crediting remain intact.
The prior monitoring-only rollout is independent of this migration.

## Owner setup and recovery

Use a dedicated private directory outside disposable Git worktrees. The setup
tool writes `seedphrase.txt` before registering the wallet, with owner-only
permissions, and never prints it. It saves `receive-client.txt` separately and
writes a non-secret `wallet.json` receipt. Reruns reuse the seed and credential.
Missing or inconsistent recovery material is a stop condition, not permission
to generate a replacement. A client creation with a lost response requires
reconciliation by the root holder rather than creating more credentials.

```sh
uv run --no-project --with lexe-sdk==0.1.19 --with httpx \
  python -m scripts.lightning.lexe_setup \
  --state-dir /absolute/private/lightningrouter-lexe --create-wallet
```

Before funding, the owner must securely back up the seed independently of this
computer and confirm recovery in the Lexe app. Do not paste the seed into chat,
email, a GitHub issue, or a CI secret. Never import the existing LND seed into
Lexe. Checkout receives only the revocable receive credential. Keep the root
seed off Cloud Run and out of the future sidecar container. Owner-controlled
node upgrades/provisioning remain separate from payment processing.

Install only the receive credential in the dedicated production Secret Manager
resource. Rotate it before expiry. Do not treat
Lexe's planned credential budgets as an available spending control.

## Sidecar check

Use a pinned official sidecar on loopback, with an isolated data directory and
no root-seed environment variables. Do not expose its port publicly. Sidecar
`lexe-sidecar-v0.4.20` was the latest published release during this review.
The Linux x86_64 release archive SHA-256 is
`3417ce147e1d0bd9db7e328dd42526338a6dbf874762700a010806e7b09e1f49`.
The macOS arm64 archive SHA-256 is
`57a403f18bff1d835e565a77fe6aa98b6c967ab0ed8790d4f3003658e105fc8c`.
Recheck release provenance before future upgrades; do not use an unpinned
curl-to-shell installer.

```sh
lexe-sidecar --listen-addr 127.0.0.1:5393 \
  --client-credentials-path /absolute/private/lightningrouter-lexe/receive-client.txt \
  --data-dir /absolute/private/lightningrouter-lexe/sidecar

uv run python -m scripts.lightning.lexe_preflight \
  --expected-user-pk <wallet-public-key-from-wallet.json>
```

The preflight makes only four GETs. It rejects root/spend/admin credentials,
extra or unknown permissions, an incorrect wallet, short-lived credentials,
redirects, oversized responses, and non-loopback endpoints. Its output omits
balances, invoice data, keys, peer IDs, and channel IDs. Existing inbound
capacity is metadata, not a guarantee of JIT payment delivery. The reported
enclave measurement is not independently verified by this helper; attestation
is the official SDK's responsibility. Passing it never enables production.

## Funding adapter requirements

The current adapter intentionally relies on an LND-specific contract. Do not
replace its base URL with Lexe's URL or fabricate an LND-compatible identity.

1. Persist the backend and wallet identity on each invoice. Existing invoices
   always finish on LND; changing a global backend must not redirect lookups.
2. Store Lexe's actual payment hash and payment index before publishing its QR.
   Lexe generates these server-side, unlike our deterministic LND preimage.
   The reviewed create request has no idempotency field. Use durable creation
   ownership and recovery correlation; after an ambiguous timeout, do not
   blindly create another invoice. Ask Lexe about a native idempotency key.
3. Preserve the frozen USD quote and expiry. Validate the decoded BOLT11
   network, amount, hash, and expiry before returning it to the browser.
4. Verify completed inbound invoice status via authenticated reads, not a
   browser callback or webhook alone. Verify the returned identity and payment
   preimage/hash relationship. Poll for recovery across worker restarts.
5. Keep the promised gross customer credit unchanged. Lexe's completed inbound
   amount is net of skimmed receiving fees. Reconcile gross, net, and fee in
   integer millisatoshis and record the provider fee separately. Do not feed
   net receipts into the current underpayment check or charge users again.
   REST represents fractional sats exactly; the Python convenience API uses
   whole sats, so it is not the production accounting representation.
6. Preserve cancel-versus-settle race handling. A failed cancel may mean a
   payment is already claiming; read its authoritative status. Do not free the
   active invoice slot or erase a quote just because cancellation was requested.
7. Credit the existing USD ledger exactly once using verified payment identity.
   Create new customer accounts only after payment. A backend timeout must not
   provision a customer or cause a second credit.
8. Treat JIT liquidity as a distinct readiness capability. A fresh Lexe wallet
   can receive without an existing channel, so the LND capacity gate cannot be
   reused unchanged. Test actual JIT receipts and expose provider failures to
   the existing alerts without changing the customer interface.

Test unknown-create recovery, duplicate callbacks, concurrent refresh/cancel,
fee rounding, underpayment/overpayment, stale FX quotes, worker restart, revoked
credentials, and mixed LND/Lexe reconciliation. Run the same credit conformance
cases against both backends. The SDK/sidecar must fail closed on attestation
failure; never disable verification to make an integration test pass.

## Cost and cutover gates

Published receive pricing is 0.5%. Hosting is currently free. Channel and
liquidity fees are currently waived, not guaranteed free forever. Absorb the
receiving fee within the existing buffer rather than modifying the UI/quote.
Confirm commercial terms, JIT limits, downtime behavior, and future fee caps
with Lexe before activation. No Loop or automatic fund transfers are enabled.

Before switching new invoices: independent seed backup and recovery check,
reviewed adapter/migration and rollback, live receiving canary with an approved
small amount, exact USD credit assertion, and provider-fee reconciliation.
Then direct only new invoices to Lexe and keep LND reconciliation running.
Do not close a channel, move the existing BTC, or delete either wallet as part
of an application deploy. Those require an explicit funds migration decision.

## Runtime and rollout

`lr_invoices.backend` and `wallet_id` bind each invoice permanently. Legacy
rows default to LND. A Lexe intent has no payment hash until a verified remote
invoice is durably bound. `create_started_at` commits before the single remote
create call and never expires. A lost response is recovered using a unique
personal note and at most ten pages of payment updates. An unresolved intent
requires review, never a blind create retry. A completed authenticated recovery
scan with zero matches may retire an entirely unbound, unpublished intent only
after the original quote expiry plus 120 seconds. Conditional database guards
prevent retirement racing a binding or settlement. The terminal
`creation_absent` audit code does not keep checkout or health permanently
blocked; provider outages, truncated scans, duplicate matches and published
invoices remain fail-closed. The provider index is distinct
from the legacy numeric settlement field, which holds Lexe's finalized-at
timestamp for Lexe receipts. `provider_fee_msat` records fees separately in
both the invoice and deposit. Existing USD-credit idempotency is unchanged.

The pinned sidecar runs as the non-root funding UID, bound only to
`127.0.0.1:5393`. The launcher supervises both processes and stops the other
when one exits. The sidecar receives an allowlisted environment, not the
checkout/SQL credentials. Its container-local cache is disposable; it is not
the node, seed, channel database, or source of credit truth. SGX verification
remains on. No root-seed, spend, admin, or channel capability is deployed.

Deployment sequence:

1. Run full root and funding gates, including PostgreSQL contracts and Docker
   build. Merge green CI. Build an immutable image from the committed Git tree.
2. Install receive credential version 1 as
   `lightning-router-lexe-receive-client`, granting only the existing funding
   runtime identity access to that one secret. No seed is uploaded.
3. Run `python -m scripts.lightning.deploy_lexe --account <deploy-identity>
   --image <immutable-image> --suffix lexe<release>-bridge --backend lnd --apply`.
   This migrates separately and stages the dual-backend revision without
   traffic. Verify Ready, then promote it and check public health and an
   unpaid create/cancel flow. New invoices still use LND.
4. Retire pre-Lexe Cloud Run revisions only after the bridge is serving 100%
   and healthy. Old workers must not reconcile Lexe rows. Keep the old image
   recorded, but all subsequent rollback revisions must use the dual-backend
   code. Existing Bitcoin Core, LND, BTCPay, channel and backups stay running.
5. Stage the same image with `--backend lexe` and a new suffix. The deployment
   guard refuses this while pre-Lexe funding revisions exist. Verify dependency
   preflight, promote, and run the public unpaid create/cancel smoke.
6. Greg pays a small invoice through the unchanged website. Verify settled
   gross equals net plus fee, the promised USD credit is applied exactly once,
   and an API call works. Until that payment lands, report invoice-path
   verification separately from funded end-to-end verification.

Rollback changes **new invoices** to `LR_INVOICE_BACKEND=lnd` on this same
dual-backend image. Keep the Lexe wallet setting and receive credential mounted
so existing Lexe invoices still reconcile. Never deploy the old LND-only code
after the first Lexe intent. Never close or move the old channel as rollback.

An expired/revoked receive credential or a failed attestation fails closed.
The existing liquidity heartbeat now reports `backend=lexe`, `jit_liquidity`
and receive-authority readiness, not a fabricated amount of inbound capacity.
It does not prove an actual payment succeeded. Payment/reconciliation errors
continue to alert through the existing funding alerts. The credential expires
in December 2026; rotate ahead of that date using a new scoped credential and
explicit secret-version rollout.

## References

- [Lexe authentication and scopes](https://docs.lexe.tech/authentication/)
- [Python SDK](https://docs.lexe.tech/python/quickstart/)
- [Sidecar API](https://docs.lexe.tech/sidecar/api-reference/)
- [Pricing and future liquidity fees](https://docs.lexe.app/pricing/)
- [Sidecar releases](https://github.com/lexe-app/lexe-sidecar-sdk/releases)
- [Reviewed source snapshot](https://github.com/lexe-app/lexe-public/tree/bcabbd3d703e337a96727042a49379101feca25f)
- [Current LND monitoring](lightning-liquidity.md)
