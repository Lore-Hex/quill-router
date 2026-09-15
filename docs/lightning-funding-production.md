# LightningRouter funding deployment

The website funds normal TrustedRouter USD credits. It does not proxy prompts,
hold a BTC inference balance, or automatically sell received BTC. Coding agents
use `https://api.trustedrouter.com/v1`, whose TLS terminates in the attested API.

## Boundaries

* `lightning-router-web` runs as its own non-root Cloud Run identity with 512 MiB,
  one warm instance, two maximum, and always-allocated CPU for reconciliation.
* `lightning-router-funding` is a separate PostgreSQL instance. Its runtime role
  has invoice-table DML only. A separate migration identity owns the schema.
  Fourteen backups and seven days of point-in-time recovery are enabled. Disk
  starts at 10 GB and auto-growth is capped at 100 GB.
* Cloud SQL connections use the authenticated Cloud Run socket. There are no
  authorized public database client networks.
* The node remains in the isolated `tr-lightning` network. Only funding-subnet
  traffic from `10.92.1.0/26` may reach its private TLS REST listener at
  `10.92.0.2:8080`. Public traffic cannot reach this listener.
* The web macaroon permits exactly GetInfo, ListChannels, AddInvoice,
  LookupInvoice, and CancelInvoice. It cannot send money, open channels,
  unlock the wallet, or create another macaroon. Wallet keys never leave LND.
* The funding token is separate from gateway, observer, and operator tokens.
  It is mounted only in the funding service and the internal billing surface.
* Cloud Armor limits invoice creation by client IP. The application separately
  limits each key, rather than grouping all customers behind Cloud Run's proxy.

## Payment invariants

An unpaid checkout is not a TrustedRouter account. Browser-generated pending
keys are encrypted for recovery before an invoice is issued. Only an LND
SETTLED invoice can provision a zero-grant account and deliver USD credits.
The settled invoice is a durable outbox. Delivery retries preserve its payment
hash, USD amount, and account, including after a crash or browser disconnect.

New invoices apply a 10% FX buffer to Coinbase's BTC/USD spot quote: 90% of
the quoted BTC value becomes USD credits. For example, $10 of credits requires
about $11.11 of BTC at the quoted spot rate, rounded up to a whole satoshi.
This is a 10% margin on the BTC value, not a 10% surcharge on credits. The
website discloses the buffer and gross quoted value before payment. The rate
is cached for at most 60 seconds and frozen for each invoice. Old invoices
retain their original rate and zero buffer; balances are never revalued.

The stored `usd_per_btc` is the effective credit rate, after the buffer. The
additive invoice `fx_margin_bps` column is disclosure metadata, defaulting to
zero for legacy invoices. Older settlement workers still use the same stored
effective rate during rollback. Never reapply a current margin at settlement.
The buffer is not a guaranteed realized FX profit: received BTC is not sold
automatically, and later price changes and conversion costs remain separate.

New invoices fail closed when the database, billing bridge, node sync, or
receiving capacity is unavailable. Already-issued invoices remain recoverable.
Channel balance is a capacity check, not a guarantee that every payer has a route.

Invoices remain limited to $1,000. The authenticated receipt endpoint accepts
up to $2,002 to accommodate satoshi rounding and up to twice the quoted payment
at the maximum supported exchange rate. Actual received value is recorded and
credited once at the frozen rate. A larger payment is retained in full but enters
`credit_amount_limit` operator review; it is not truncated, discarded, or used
to create an account before review. This receipt ceiling is not an invoice limit
increase. Deploy the matching billing-bridge ceiling before the funding worker.

Permanent delivery/configuration rejections receive stable, non-sensitive review
codes and five-minute retry backoff. Transient failures remain retryable. The
website hides the QR and shows a review message for permanent failures. `/health`
becomes degraded for any review condition or any paid invoice uncredited for
120 seconds. `inference_configured` means an inference backend is configured,
not that a paid inference smoke has passed.

Reconciliation emits aggregate delivery health every minute, without keys,
payment hashes, preimages or request bodies. Install the dedicated payment-review,
worker-failure and missing-heartbeat alerts with:

```sh
python -m scripts.lightning.reliability --account=OPERATOR --apply
```

The installer requires the existing verified on-call channel, does not broaden
IAM, and does not change unrelated alarms. Review/stuck-payment alerts repeat no
more than every five minutes. A ten-minute missing heartbeat detects an absent
worker even if the HTTP process still answers. Monitor the paid/uncredited count
after deployments; never resolve an alert by dropping its outbox row.

Quote fetches are single-flight and fail fast rather than queueing behind an
upstream stall. An upstream failure has a five-second negative cache. Per-key
rate limits replenish gradually rather than resetting at a wall-clock boundary.

## Bounded retention

Hourly PostgreSQL maintenance removes at most 100 canceled, unpaid, error-free
invoices older than 30 days, 100 orphan unfunded checkouts, and 100 expired rate
limit identities per pass. Paid or unresolved rows and the deposit ledger are
never pruned by this job. For node-local cleanup, install from a clean committed
checkout:

```sh
python -m scripts.lightning.install_maintenance --account=OPERATOR --apply
```

This installs `tr-lnd-invoice-cleanup.timer` without restarting LND. Each hourly
pass scans at most 100 records and deletes at most 10 website invoices that LND
confirms canceled, unpaid, and expired for over 45 days. It uses its own local
ListInvoices/DeleteCanceledInvoice-only macaroon, never the web credential.
The cursor is durable; paid, open, accepted and unrelated invoices remain.
The installer performs a dry run, not a wallet or channel operation. The timer
will not physically compact the LND database; its existing startup compaction
policy handles reusable space without an automatic wallet restart.

## Release sequence

1. Run root CI and the experiment's Python, PostgreSQL, JavaScript, and browser
   tests. Build the experiment from a clean committed Git archive, never a
   mutable working directory. Resolve the image to an immutable digest.
2. Apply the existing `scripts/lightning/spanner_provenance.sql` upgrade once,
   preserving the old check until the wider check has been validated.
3. Run `python -m scripts.lightning.connect_funding_node --account=OPERATOR`.
   This checks there are no in-flight HTLCs, preserves loopback RPC access,
   verifies the private TLS certificate, and proves wallet RPC access is denied.
4. Run `python -m scripts.lightning.activate_web bootstrap --account=OPERATOR --apply`.
   No credentials are printed or passed as command-line arguments.
5. Deploy the billing bridge through normal control-plane CI and rollout gates.
6. Run `python -m scripts.lightning.activate_web deploy --account=OPERATOR
   --image=IMMUTABLE_DIGEST --apply`. The migration job must succeed first.
   Web startup verifies the database, authenticated billing bridge, and LND
   before becoming ready. A failed new revision must not replace the old one.
7. Verify public health, HTTPS browser invoice creation, cancellation, and
   recovery. Have the operator or tester pay a small real invoice, then verify
   its USD balance and a real inference request. Never simulate settlement in
   production or call an outgoing-payment RPC as part of deployment.

For Greg's first test, use a small USD amount, keep the tab open until the key
appears, and retain that key. It is the account credential; there is no email
recovery. Top up with the same key to keep using the same balance.

## Recovery

Disabling new invoice creation must not disable reconciliation of paid invoices.
Keep the database and checkout secret together when restoring; the encrypted
pending keys and deterministic invoice identities depend on that secret. Never
rotate it in place while invoices are pending. Node seed/channel recovery is
separate and remains governed by the receiving-node runbook.

Startup pins a non-reversible checkout-secret identifier in the funding DB and
validates existing recovery material before the initial pin. A changed secret
fails startup. Restore the correct secret version rather than replacing it to
get a deployment through. Secret provisioning refuses differing existing values.
Only the invoice macaroon and TLS certificate support explicit replacement via
`connect_funding_node --rotate-node-secrets`; redeploy the funding web revision
afterward and verify the wallet-RPC denial again. The node tool does not revoke
the old macaroon automatically; coordinate revocation after successful rollout.

An issued invoice missing from LND is a recovery incident, not permission to
recreate its payment request. Keep its database row and frozen quote, reconcile
the node/backup state, and restore the same invoice evidence before crediting.
Settlement identity is the payment hash, not LND's node-local settlement index.
A restored node may reuse an index for another payment without duplicate credit.
Do not fabricate a SETTLED record as a workaround. Verify restored state in an
isolated drill before switching live node storage; never run two active wallets
from the same channel state.

The first real paid-invoice and paid-key smoke are release evidence, not
something a mocked test can establish. Record their result without storing the
API key, preimage, wallet seed, or macaroon in logs or this repository.
