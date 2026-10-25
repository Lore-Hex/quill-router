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
