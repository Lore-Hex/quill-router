# LightningRouter funding experiment

`https://lightningrouter.ai` is the public funding website. Production wiring uses
a synced Bitcoin/LND node, invoice-only credentials, private verified TLS,
dedicated PostgreSQL storage, and the TrustedRouter USD credit bridge. Check
`/health` for live payment readiness; never infer it from this document.

See the [production deployment runbook](../../docs/lightning-funding-production.md)
for activation, permissions, backups and recovery. Browser tests use a fake
backend; a real paid-invoice test must be recorded separately.

## Product behavior

The homepage introduces key-only funding and links to `/usage`, `/pricing`,
`/terms`, and `/privacy`. A saved funded key's balance is read before refreshing
its invoice, and a balance refresh control is available independently.
The usage page reads lifetime prepaid/BYOK spending, reservations and key limits
through the existing key-scoped `/v1/key` introspection endpoint. It does not
grant management access or list other workspace keys' requests. Reading usage
never creates a Lightning invoice or provisions an account. Unknown totals are
shown as unavailable rather than zero. No synthetic daily charts are generated
from lifetime counters.

Pricing uses Decimal conversion of the public catalog's per-token prices,
including cache, request and minimum-charge fields where published. The table
labels rates as "from" because a selected endpoint or tier can cost more. Prices
already include TR's token markup; the separate 10% Lightning FX buffer remains
disclosed at checkout and on the pricing page. Search renders 100 rows at a time.

Model output capacity is distinct from the client's default output budget.
The public catalog currently publishes null maximum output values. Reviewed
DeepSeek metadata fills V4.1 Flash and the current V4 Pro IDs with a 393,216-token
maximum and a 65,536-token thinking-mode default, as documented on
https://api-docs.deepseek.com/api/create-chat-completion/ (reviewed 2026-09-15).
Crush receives `default_max_tokens=65536`; OpenCode and OMP receive the documented
model capacity. Other unknown limits are omitted from snippets, not mislabeled
as 4,096. Explicit catalog limits take precedence and are clamped to context.
Provider endpoints may impose narrower limits than the model's native service.

The local legal pages adapt existing Lore Hex Corp service terms and disclose
Lightning-specific key recovery, FX conversion, payment metadata and provider
privacy boundaries. They are not an external legal compliance certification.

White page, invoice QR first, USD amount with approximate BTC beneath, optional
existing key, USD balance, then Trusted Cowork / OpenCode / Crush / OMP setup tabs.
Trusted Cowork is selected first and links to its official download page. The
manual `Use an API key` flow accepts the same funded key; its copy button is
disabled until a key is connected or credit delivery completes. Keys never
appear in the download URL. Cowork keeps its confidential-model policy and
selects models inside the app. The model selector applies to the three CLI
setup tabs; switching tabs preserves that selection and checkout.
Changing the amount hides the old QR until cancellation and replacement finish.
If the cancellation response is lost, both the LND adapter and browser verify
the invoice state before continuing. An unresolved invoice keeps its original
key and request ID. Payment winning the race credits the original key.

There is no global reasoning dropdown. `lightning_router/reasoning.py` associates
exact reviewed model IDs with native controls, distinct values, source links,
review dates, defaults and separately verified chat-compatible setup fields.
New releases and provider-specific aliases do not inherit guessed values from a
name prefix or the catalog's coarse `reasoning_effort` boolean. Unreviewed models
are explicitly unverified, not declared incapable of reasoning.

For reviewed chat effort models, generated settings set the documented default:
OpenCode `options.reasoningEffort` and model variants, Crush selected-model
`reasoning_effort` plus `reasoning_levels` and `default_reasoning_effort`, and OMP
model-specific `thinking.efforts` plus an explicit launch effort. OpenCode's
unsupported built-in variants are disabled, since it merges rather than replaces
them. OMP's `off` is distinguished from named effort; DeepSeek and GLM configs do
not invent an off level. Provider-hosted variants can differ from native models.

**Known adapter gaps are not advertised as working controls.** TR's current chat
adapter maps Anthropic low/medium/high to 1024/4096/8192 thinking tokens, not native
`output_config.effort`. Native effort defaults to high where supported; that does
not imply thinking is on. These snippets omit that misleading override and show
the limitation. `/v1/messages` preserves native output_config, but these CLI tabs
use chat completions. Gemini Vertex and Google-compatible mappings also differ;
their native reference is shown without forcing unsupported effort settings.
MiniMax M3 has disabled/adaptive thinking, while M2.x thinking is always on.
No backend behavior or payment logic is changed by these setup references.

Node tests use the production Python profile registry and the actual pinned
OpenAI-compatible AI SDK with a fake HTTP transport. They prove the generated
effort reaches the HTTP body, not merely a config string. Crush and OMP config
tests plus their source review are not a live end-to-end provider test.
References: [OpenCode](https://opencode.ai/docs/models/#configure-models),
[Crush](https://github.com/charmbracelet/crush/blob/main/internal/agent/coordinator.go),
[OMP](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md).

**BTC is a funding method, not a billing currency.** The invoice freezes a
BTC/USD quote for up to 15 minutes. On verified settlement, the actual received
amount, including overpayment, converts once to integer USD microdollars. Round
down only at the microdollar boundary. Expired quotes cannot create new invoices;
delayed LND creation cannot extend a quote. Already-paid invoices remain
recoverable after expiry.

The existing TrustedRouter USD ledger remains authoritative for balances and
inference charges. No BTC account balance, per-request FX conversion, or new
BTC reserve/settle/refund system. Later BTC price changes never revalue credits.
This conversion issues USD service credits; it does not automatically sell
treasury BTC on an exchange.

Keys use the existing `sk-tr-v1-` format. An existing key funds its workspace.
Opening the page or creating an unpaid invoice does not provision a user,
workspace, or API key in TrustedRouter. Only verified Lightning settlement
provisions the key-only account and applies its USD credits. There is no email,
password, username, OAuth, free grant, or management privilege. An expired or
canceled unpaid invoice never creates an account. A revoked existing key cannot
authenticate through stale invoice metadata.

The browser generates a random pending credential using Web Crypto and writes it
to sessionStorage before requesting the invoice. It is not a registered API key
or account until payment. A lost HTTP response or reload preserves checkout
access in that tab. It reveals the key and account only after USD credit delivery
is acknowledged. Keep the key: closing the tab
can lose the local copy and there is no email recovery.

## Boundaries

* Plaintext keys never enter the database, URLs, HTML source, logs, or LND memos.
  Pending credentials are AES-GCM encrypted using a domain-separated key and
  their HMAC fingerprint as authenticated context. This lets the worker finish
  a paid checkout without the browser. Ciphertext is cleared when the funded
  identity binding is saved. Unpaid checkouts live only in `lr_checkouts`, not
  the TrustedRouter identity store. Back up the server secret separately from
  the funding database; both are needed for pending-payment recovery.
* Checkout ownership, payment hash, idempotency key and quote bind durably before
  contacting LND. Retries recover the same invoice, not a new payment hash.
* Only LND `SETTLED` with validated `amt_paid_msat` produces a deposit. The invoice,
  immutable BTC receipt and frozen USD amount commit in one transaction.
* The settled invoice is a durable outbox until USD credit acknowledgement.
  `Credits.credit` must commit idempotently under `lightning:<payment_hash>` and
  reject changed destinations or amounts. Lost remote responses and crashes
  before local acknowledgement replay the same credit without adding it twice.
* The funding database has no spendable balance in either currency. Balance reads
  query the USD backend and do not call the exchange-rate API.
* Changing keys cancels the previous invoice first. If payment wins that race,
  it credits the original account and preserves that account's key.
* A bounded worker retries unresolved invoice and credit delivery records even
  after the browser closes. Logs contain invoice IDs and exception classes only.
* The funding web server is not a prompt proxy. Prompt TLS still terminates
  inside the attested gateway. No inference currency changes are needed there.

## Local preview

From this directory:

```sh
uv sync --frozen
npm ci --ignore-scripts
npm run build
uv run uvicorn lightning_router.app:from_environment --factory --host 127.0.0.1 --port 8095 --no-access-log
```

The default preview shows the real prelaunch state, public model catalog and
Coinbase BTC/USD spot estimates cached for 60 seconds. It accepts no payments.
There is no environment flag that silently enables a fake production ledger.

## Tests

```sh
uv run ruff check .
uv run mypy
uv run pytest --cov=lightning_router --cov-fail-under=70
npm test
npx playwright install chromium
npm run test:browser
```

For installed Chrome, set `LR_BROWSER_CHANNEL=chrome`. The loopback browser
harness uses fake LND and a fake USD credit backend. Its controls are never
imported by the deployed app. Screenshots are ignored rather than uploaded.

SQLite and PostgreSQL run the same funding receipt/outbox conformance tests:

```sh
docker run --detach --rm --name lr-funding-test-pg \
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=lightning_router_test \
  -p 127.0.0.1:55439:5432 postgres:17
LR_TEST_POSTGRES_URL=postgresql+psycopg://postgres:local-test-only@127.0.0.1:55439/lightning_router_test uv run pytest
docker stop lr-funding-test-pg
```

Tests refuse non-loopback database URLs or any other database name before resets.
The old BTC-balance prototype and pre-funded `lr_accounts` schemas were never
deployed. Use a new disposable local database; never relabel old BTC rows as USD.
The first production funding database must use the current `lr_checkouts` schema.

## Mainnet launch gates

1. Finish Bitcoin IBD; install pinned and verified LND; validate pruned-bitcoind
   compatibility; configure wallet/channel backups and recovery; provision
   inbound receiving liquidity. Test with real LND on regtest first.
2. Deploy persistent funding records and secrets with backup/restore checks.
   Schema migration is a separate deploy action, not web-worker startup.
3. Activate the tested `TrustedRouterCredits` HTTPS adapter and the dedicated
   `TR_LIGHTNING_FUNDING_TOKEN` on the internal billing surface only. The token
   must differ from every other service credential. No token means no access,
   including local/test mode. The bridge uses the existing USD credit operation
   and `lightning`/`invoice` payment provenance, not a grant or fake Stripe fact.
   Run `scripts/lightning/postgres_provenance.sql` before activation on an
   existing Postgres deployment, or the one-time
   `scripts/lightning/spanner_provenance.sql` upgrade on existing native Spanner.
   Both migrations widen only the provider constraint and preserve money rows.
   Run a small operator-paid invoice and paid-key inference smoke as the final
   launch verification. Never simulate a production settlement.
4. Add readiness gates for node sync, wallet availability, receiving capacity,
   DB health and verified credit delivery before removing the mainnet guard.
5. Website HTTPS is deployed on its own Cloud Run service and load balancer.
   Agent setup uses the attested `https://api.trustedrouter.com/v1` endpoint.
   `api.lightningrouter.ai` is not deployed. Do not point the public site or
   an API hostname at Bitcoin RPC.
6. Add ingress limits, expiry cleanup and monitored reconciliation. Retain
   redaction and keep wallet/admin macaroons off the web service.
7. Verify actual OpenCode, Crush and OMP requests. Current tests prove setup
   configuration output, not a working production inference endpoint.

## Payment-disabled web deployment

For production activation use `scripts/lightning/activate_web.py`, not the
prelaunch tool below, which deliberately disables payments.

`scripts/lightning/deploy_web.py --account=DEPLOY_IDENTITY` builds only committed
experiment files, deploys a 512 MiB Cloud Run service under a no-data-access
identity, and creates a separate HTTPS load balancer and DNS A record. It does
not modify TrustedRouter's URL map or install an inference proxy. Mainnet stays
disabled. Wait for the managed certificate to become ACTIVE and verify public
HTTPS, `/health`, `/api/config`, `/api/models`, and rejected invoice creation.

The USD bridge has conformance tests across memory, the native Spanner fake,
real local PostgreSQL and the Spanner PostgreSQL adapter for simultaneous provisioning, exactly-once deposits,
changed-amount/workspace conflicts, revocation, expiry and sub-cent precision.
`lightning_key` tombstones prevent deleted funding identities from being recreated;
`lightning_payment` point-key bindings are permanent payment replay protection.

## References

* [Bitcoin deployment](../../docs/bitcoin-node.md)
* [LND invoice creation](https://lightning.engineering/api-docs/api/lnd/lightning/add-invoice/)
* [LND invoice lookup](https://lightning.engineering/api-docs/api/lnd/lightning/lookup-invoice/)
* [LND cancellation](https://lightning.engineering/api-docs/api/lnd/invoices/cancel-invoice/)
* [OpenCode providers](https://opencode.ai/docs/providers/)
* [Crush configuration](https://github.com/charmbracelet/crush/blob/main/docs/config/README.md)
* [OMP models configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md)
