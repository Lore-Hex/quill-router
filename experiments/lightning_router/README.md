# LightningRouter funding experiment

**Not launched. Mainnet deposits are deliberately disabled.** The Bitcoin node is
syncing; no LND wallet or inbound Lightning liquidity exists yet. This package
implements the funding page and payment-account foundation, not an inference
proxy. Its keys do not yet work against TrustedRouter or a LightningRouter API.

## Product behavior

White page, invoice QR first, USD amount with approximate BTC beneath, optional
existing key, balance, model selector, then OpenCode / Crush / OMP setup tabs.
The QR fixes an exact whole-satoshi amount for 15 minutes. Changing the USD amount
hides the old QR until cancellation and replacement complete. USD balance is an
estimate; the source of truth is integer millisatoshis (100 billion per BTC).
The full amount LND actually settles is credited, including overpayment.

An API key is the only credential. New keys are generated with Web Crypto before
an invoice is requested and saved in sessionStorage first, so a lost HTTP response
or page reload cannot discard the pending key. The page reveals it after payment.
Existing keys immediately show their account balance. No email, password, Google,
wallet signature, or username. No free credit. Closing the tab loses its local
copy; the user must retain the key. There is no email-based recovery.

For this first build, existing keys means **LightningRouter `sk-lr-v1-` keys**.
TrustedRouter `sk-tr-v1-` keys and USD workspaces remain separate. Supporting those
requires an explicit dual-currency account contract, not currency relabeling.

## Boundaries

* API keys are HMAC-SHA256 indexed using a persistent server secret. Raw keys are
  never in the database, URL, HTML source, public catalog, logs, or LND memo.
* The client keeps its bearer only in tab-scoped sessionStorage and sends it in
  Authorization. This is an intentional key-as-login design. CSP blocks remote
  scripts and framing; there are no analytics SDKs on this surface.
* A durable invoice row binds the key hash, immutable payment hash, amount, and
  idempotency key before contacting LND. The preimage is derived with a separate
  HMAC domain from the persistent server secret and internal invoice ID.
* LND is authoritative. Only `SETTLED` with validated `amt_paid_msat` credits the
  ledger. Invoice, immutable deposit, and balance commit in one DB transaction.
  Duplicate notifications and concurrent polling cannot double-credit.
* Changing accounts first cancels the old invoice in LND. If payment wins the
  race, the old account is credited and its new key is preserved for the user.
  No payment ever credits whichever text happens to be in a browser field.
* A bounded reconciliation worker processes unresolved invoices independently of
  browser polling. Errors retain retryable records and log only invoice IDs and
  exception classes, not upstream response text or credentials.
* SQLite is for local tests. PostgreSQL runs the same money contract. This is a
  separate database, never TrustedRouter's existing money tables.
* The funding site is **not** a prompt proxy. All future inference TLS still
  terminates in the attested gateway, not on the Bitcoin VM or this web app.

## Run locally

From this directory:

```sh
uv sync --frozen
npm ci --ignore-scripts
npm run build
uv run uvicorn lightning_router.app:from_environment --factory --host 127.0.0.1 --port 8095 --no-access-log
```

This shows the real prelaunch state, a live public BTC/USD estimate, and the
public prepaid/chat/tool-compatible model catalog. No payment or inference is
performed. Public quotes use Coinbase BTC-USD spot, cached for 60 seconds.
Invoices lock the quote once; a quote failure does not hide an existing BTC balance.

## Test

```sh
uv run ruff check .
uv run mypy
uv run pytest --cov=lightning_router --cov-fail-under=70
npm test
npx playwright install chromium
npm run test:browser
```

The browser harness is in `tests/browser_server.py`, uses fake LND and rates, and
only binds to loopback. Its payment controls are test-only and are never imported
by `lightning_router.app`. To use installed Chrome for local browser tests, set
`LR_BROWSER_CHANNEL=chrome`. Screenshots are ignored, not uploaded as artifacts.

PostgreSQL conformance uses a dedicated disposable loopback DB:

```sh
docker run --detach --rm --name lr-funding-test-pg \
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=lightning_router_test \
  -p 127.0.0.1:55439:5432 postgres:17
LR_TEST_POSTGRES_URL=postgresql+psycopg://postgres:local-test-only@127.0.0.1:55439/lightning_router_test uv run pytest
docker stop lr-funding-test-pg
```

Tests refuse non-loopback URLs or any other database name before resetting tables.

## Mainnet launch blockers

1. Finish and verify Bitcoin IBD. Install a pinned, verified LND build, validate
   prune-mode backend compatibility, create and back up the wallet, configure
   channel backup/watchtower strategy, and provision inbound liquidity. Neither
   invoice settlement nor channel funds have been tested on a real LND node yet.
2. Deploy the separate PostgreSQL ledger with backup/restore checks and a stable
   secret (including secure restore of that secret). Run schema migration as a
   separate deploy action, not automatically in every web worker.
3. Integrate `sk-lr-v1-` auth with the attested gateway's authorization, reserve,
   settle and refund contract. USD model prices need a per-request FX quote,
   an exact integer-msat hold, frozen conversion through settlement, and durable
   idempotent reconciliation. Keep USD and BTC accounts separate. **Do not** route
   prompts through this funding server to avoid doing this integration.
4. Introduce real readiness evidence before replacing the hard mainnet guard in
   `app.py`: wallet sync, adequate receiving capacity, DB health, API billing and
   attestation smokes, and successful independent LND regtest payment coverage.
5. Bind `lightningrouter.ai` website HTTPS and `api.lightningrouter.ai` L4/attested
   HTTPS separately. Cloud DNS zone `lightningrouter-ai` exists; no website or API
   A/AAAA records have been created. Do not point the public domain at Bitcoin RPC.
6. Harden public ingress with connection/IP/body limits, cleanup of expired
   anti-abuse counters and unfunded accounts, and a monitored dedicated worker.
   Wire persistent ledger/worker failures to the existing ops channel without
   exporting keys or invoice credentials. No Sentry integration exists here yet.
7. Run all three actual coding clients through paid test keys. The setup formats
   are based on their current primary documentation and tested as configuration
   output, not claimed as successful production inference calls.

The code cannot enable mainnet funding merely by setting an environment flag.
`LR_PAYMENTS_ENABLED=true` is accepted only with `LR_NETWORK=regtest`; certificate
verification and a scoped invoice macaroon are mandatory. Keep LND's wallet/admin
credentials off the web app and its VM. Never broaden the existing ops identity.

## References

* [Bitcoin node deployment](../../docs/bitcoin-node.md)
* [LND invoice creation](https://lightning.engineering/api-docs/api/lnd/lightning/add-invoice/)
* [LND invoice lookup](https://lightning.engineering/api-docs/api/lnd/lightning/lookup-invoice/)
* [LND cancellation](https://lightning.engineering/api-docs/api/lnd/invoices/cancel-invoice/)
* [OpenCode providers](https://opencode.ai/docs/providers/)
* [Crush configuration](https://github.com/charmbracelet/crush/blob/main/docs/config/README.md)
* [OMP models configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md)
