# LightningRouter BTCPay Dashboard

BTCPay uses the existing Bitcoin Core and LND installation on `tr-bitcoin-1`.
It does not create a second Bitcoin node, LND wallet, channel, or funding service.
The existing LightningRouter checkout remains authoritative for USD credits.

## Isolation

- `btcpay.lightningrouter.ai`: separate `tr-btcpay-1` VM, `us-central1-a`,
  e2-medium, 30 GB disk. No attached service account or cloud API scopes.
- BTCPay 2.4.4, PostgreSQL 17, and Caddy are pinned by image digest.
- Only HTTPS and its HTTP ACME/redirect listener are public. SSH requires IAP
  and OS Login. PostgreSQL is on an internal container network; BTCPay's direct
  HTTP listener is loopback-only.
- LND stays on `10.92.0.2:8080`. The new firewall permits exactly the dashboard's
  `10.92.2.2/32` source, and BTCPay pins the existing LND TLS certificate.
- A separate macaroon, root key ID 31, permits exactly GetInfo, ListChannels,
  WalletBalance, ChannelBalance, ListInvoices, LookupInvoice, SubscribeInvoices,
  and AddInvoice. No payments, cancellations, withdrawals, peers, channels,
  macaroon administration, or seed access. Root key 23 used by checkout is untouched.
- No host-control interface, Docker socket, Bitcoin RPC, wallet seed, or LND
  admin credential is mounted into BTCPay.
- Database passwords, macaroon, administrator password, and account invitation
  are root-only files. Never put them in Git, command arguments, metadata, logs,
  screenshots, support tickets, or chat.

## Accounts

Joseph owns the separate server-admin login `security@trustedrouter.com`.
Greg uses `contact@taoeffect.com` and a custom **Observer** role on only the
LightningRouter store. The role can view store/invoice/report data and read
Lightning invoices; it cannot change settings, retrieve connection secrets,
create invoices, administer users, spend, or manage channels.

Public signup is disabled. Non-admin store creation is disabled. Greg receives
an account invitation and chooses his own password. Do not visit his invitation
on his behalf. Share the invitation through an encrypted channel. Joseph and
Greg should enroll their own authenticator or passkey after initial login.

Permission verification uses a temporary canary account with the same role,
then deletes it. It does not consume Greg's invitation or send a payment.

## Limits

Sharing LND does **not** import existing LightningRouter invoice/credit records
into BTCPay's store dashboard. The node invoice API can read existing LND
invoices, but the BTCPay store initially has no historical orders. A BTCPay
invoice does not automatically fund TrustedRouter credits. Do not replace the
live checkout without a separately reviewed settlement integration.

This is a Lightning-only dashboard. No BTCPay on-chain wallet or NBXplorer indexer
is configured; BTCPay may report its optional Bitcoin explorer as disconnected.
That is not Bitcoin Core/LND being offline. On-chain checkout is not enabled.

## Deployment

Use a separately authenticated deployment identity, not `tr-ops-local`.
Commit the reviewed source before applying; deployment archives Git objects and
refuses dirty source. Dry-run is the default:

```sh
uv run python -m scripts.lightning.btcpay.deploy provision --account=DEPLOY_EMAIL
uv run python -m scripts.lightning.btcpay.deploy provision --account=DEPLOY_EMAIL --apply
uv run python -m scripts.lightning.btcpay.deploy install --account=DEPLOY_EMAIL --apply
uv run python -m scripts.lightning.btcpay.deploy publish --account=DEPLOY_EMAIL --apply
```

`publish` first bootstraps through loopback, locks registration, tests the role,
and revokes the temporary administrator API key. Only then does it enable public
ingress. Credentials are captured into ignored mode-0600 `.private/btcpay/` files.
Never share `owner-access.json` with Greg; share only `greg-invitation.json`.

Database dumps run at 04:00 UTC; encrypted disk snapshots run at 05:00 UTC with
seven-day retention. These protect BTCPay configuration/invoice metadata, not
the existing LND wallet. Existing wallet/channel backups remain independent.
Restore into an isolated VM with no LND firewall access or public listeners.

## Rollback And Rotation

Remove only the `tr-btcpay-https` firewall rule to take this dashboard private.
Do not stop Bitcoin Core, LND, or the LightningRouter funding service. Disable
Greg's BTCPay account to revoke his access. To revoke the dashboard's node
credential, delete LND macaroon root key 31 with the existing node-admin procedure,
then remove `tr-btcpay-lnd`. Never delete root key 23 or wallet/channel files.

Updates must retain immutable image pins and rerun registration, role, TLS,
negative LND permission, backup, and live checkout-health checks. Do not use
unattended image upgrades.

Official references: [BTCPay releases](https://github.com/btcpayserver/btcpayserver/releases),
[Lightning setup](https://docs.btcpayserver.org/LightningNetwork-Setup/),
[Greenfield API](https://docs.btcpayserver.org/API/Greenfield/v1/).
