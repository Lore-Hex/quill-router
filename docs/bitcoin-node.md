# Bitcoin backend for LightningRouter

Provisioned 2026-09-14 in `quill-cloud-proxy`:

* VM: `tr-bitcoin-1`, `us-central1-a`, temporarily `e2-standard-8` (user approved).
* Target after full validation: `e2-medium`. Do not call header sync full sync.
* Disk: `tr-bitcoin-data`, 200 GB `pd-ssd`, attached with auto-delete disabled.
* VPC: `tr-lightning`, subnet `tr-lightning-us-central1`, no peering to production.
* No attached service account or scopes. No wallet. No Lightning daemon yet.
* All ingress closed. IAP SSH rule creation was denied to `tr-deploy`; it was
  NOT replaced by public SSH or wider IAM. Initial sync needs only outbound peers.
* Shielded secure boot, vTPM, integrity monitoring and deletion protection enabled.
* Core 31.1 is pinned by archive SHA256 and both Ava Chow and Michael Ford's
  valid release signatures. Build keys come from a pinned `guix.sigs` commit.
* Mainnet, 30,000 MiB block pruning, localhost cookie-authenticated RPC only.
  Pruning still downloads and validates chain history. It is not an archive node.

No customer payment can be accepted yet. This does not create a Lightning wallet,
fund channels, provide inbound liquidity, issue payment invoices or credit TR users.
Finish those and rehearse recovery before enabling mainnet checkout.

## Progress

`tr-bitcoin-status.timer` emits public chain metadata every five minutes, prefixed
`TR_BITCOIN_STATUS`. It reports `synced=true` only when mainnet is out of initial
block download, validated height equals header height, and the tip is recent.
The same object is written atomically to `/var/lib/tr-bitcoin/status.json`.

```bash
CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=/Users/jperla/.config/gcloud/tr-ops-local.json \
  gcloud compute instances get-serial-port-output tr-bitcoin-1 \
  --zone=us-central1-a --project=quill-cloud-proxy \
  | rg 'TR_BITCOIN_(STATUS|ERROR|BOOTSTRAP)'
```

Do not grant the ops identity write access. Do not expose RPC for monitoring.
Bitcoin logs are bounded locally; only compact status reports need serial export.

## Installer and resize

The guest startup script and bootstrap source are uploaded together as metadata
keys `startup-script` and `bitcoin-bootstrap`. There is no curl of a moving branch.
The bootstrap refuses to format a disk with an existing filesystem/signature and
refuses an unexpected device at the data mountpoint. Reboots reuse the chain.

Before resizing, obtain a fresh `synced=true` report and ensure no LND wallet has
been added. Gracefully stop Bitcoin, stop the VM, set its machine type, and start
it. Never delete or recreate the data disk. The daemon launch recalculates cache
from actual RAM on every start: 16 GB for initial sync, 512 MiB on the e2-medium.
The data path is unchanged. Resizing does not require downloading the chain again.

The temporary large VM is not a standing production sizing decision. Recheck its
progress and resize promptly; no automatic resize has been installed yet.

## Domain

User confirmed `lightningrouter.ai` (not the earlier misspelling).
Cloud DNS zone: `lightningrouter-ai`.

```
ns-cloud-e1.googledomains.com
ns-cloud-e2.googledomains.com
ns-cloud-e3.googledomains.com
ns-cloud-e4.googledomains.com
```

Only the zone/delegation is prepared. A/AAAA/CNAME records and the HTTPS payment
website still need deployment. The website must NOT point at Bitcoin RPC.

References: [Bitcoin Core release verification](https://bitcoincore.org/en/download/),
[memory tuning](https://github.com/bitcoin/bitcoin/blob/master/doc/reduce-memory.md),
[LND setup and inbound liquidity](https://docs.lightning.engineering/lightning-network-tools/lnd/first-steps-with-lnd).
