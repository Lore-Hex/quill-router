# LightningRouter retired

LightningRouter was taken offline at the owner's request on October 22, 2026.
The website, invoice checkout, Lexe sidecar, dedicated jobs, public load balancer,
DNS service records and LightningRouter alert policies have been removed.
Deployment and activation entrypoints are disabled. Older setup documents and
the experiment source are historical recovery references, not launch instructions.

Existing TrustedRouter API keys, customer accounts and paid USD credits are not
revoked or deleted. Customers can continue using their keys with
`https://api.trustedrouter.com/v1`.

## Financial preservation

The final bounded invoice audit found 20 settled invoices, all credited, and
229 canceled invoices. There were no open/accepted invoices, paid-but-uncredited
invoices or review items. No balances, receipts or customer credentials were edited.

The dedicated payment database has a final on-demand backup and is stopped.
Keep its ledger, encrypted pending-key records and checkout-secret recovery
material together. Keep Lexe owner recovery material and the legacy LND wallet
and channel backups. These are financial records, not active product services.
No Bitcoin transfer or channel close is part of this retirement. Do not destroy
wallet disks or backups until the owner has recovered any remaining funds.

The unrelated model provider named `lightning` remains supported. Retiring the
Bitcoin funding product does not retire that provider or its API key.

## Removed code

On November 18, 2026 the TrustedRouter side of the product was deleted: the
USD credit bridge (`LightningCredits`, the Lightning key and payment bindings in
every store backend, the `/internal/lightning/*` funding routes, the
`/lightning/feedback` route and `TR_LIGHTNING_FUNDING_TOKEN`), and the disabled
deployment and activation tools (website, Lexe sidecar, BTCPay, funding-node
connection, alert reconciliation, invoice retention, public mirror export), with
the archived-source workflow and its Dependabot entry. None of it could run
after October 22. Commit `a49bab0237890fc0957f344cfe81f66543ab92df` is the last
one that contains it.

Kept on purpose:

- Ledger history. `lightning` stays a valid trust-event provider (database
  CHECK constraints, `trust_tiers.py`) so the 20 credited invoices stay
  readable. `scripts/lightning/*_provenance.sql` are the reviewed migrations
  behind the live constraint.
- Wallet node and recovery tooling (`scripts/lightning/bitcoin_node.py`,
  `bitcoin_startup.sh`, `lnd_install.py`, `lnd_node.py`, `verify_recovery.py`),
  the setup documents and `experiments/lightning_router`, until the owner has
  recovered any remaining funds. The experiment source defines the format of
  the payment database's encrypted records.
- The `/lightning/feedback` URL-map rule in
  `scripts/deploy/service_surface_url_map.py`. It sends the path to the control
  service, which returns 404. Removing it changes the control rule's path set,
  which the internal-surface edge script matches against the live URL map.
