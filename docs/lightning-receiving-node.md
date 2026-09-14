# Lightning receiving node

This is a merchant receiving node, not a public routing service. Enabling it
does not enable customer invoices on the website. Deposits remain disabled
until inbound capacity, the USD credit worker and the attested API are verified.

## Boundaries

* Dedicated `tr-lightning` VPC, no production network peering.
* Bitcoin RPC and ZMQ stay on loopback. Bitcoin Core has no wallet.
* LND peer traffic alone uses public TCP 9735. REST 8080 and gRPC 10009 are
  loopback-only, authenticated with macaroons and TLS.
* `rejecthtlc=true` disables third-party forwarding. `rejectpush=true` prevents
  accidental gifts when peers open channels. No operator spending is automated.
* Dedicated 20 GB SSD retains the wallet/channel database independently of the
  pruned Bitcoin disk. Neither data disk is auto-deleted with the VM.
* Systemd supplies the wallet unlock password from a root-only credential file.
  Automatic unlocking never enables automatic wallet creation.

## Recovery

Before initialization, the helper encrypts the 24-word seed and wallet password
using AES-256-GCM and an RSA-OAEP-SHA256 wrapped data key. Only the recovery public
key exists on the node. The recovery private key stays with the operator.
The encrypted seed must upload successfully before `InitWallet` can execute.
Initialization refuses to replace an existing seed or wallet.

`tr-lnd-backup.timer` checks the static channel backup every minute. Changed
backups are encrypted and uploaded to immutable, unique object names in
`gs://quill-cloud-proxy-lightning-recovery`. The VM identity has only bucket-scoped
`roles/storage.objectCreator`, with a Storage-only OAuth scope. Failed uploads
do not advance the saved digest and are retried on the next run.

The seed alone cannot recover funds in open channels. Keep the encrypted seed,
the latest static channel backup and the private recovery key independently
available. Static-channel recovery asks peers to close channels; it is not a
live database rollback. Never restore an old live channel database. Follow
[LND recovery guidance](https://docs.lightning.engineering/lightning-network-tools/lnd/disaster-recovery).

Local decryption verification (does not print or write plaintext):

```sh
uv run python -m scripts.lightning.verify_recovery \
  --key /private/operator/location/recovery-key.pem \
  --backup /private/operator/location/seed.encrypted.json --kind seed
```

This validates encryption and payload shape, not a destructive live-wallet
restore. Keep another offline copy of the private recovery key before material
funds accumulate. Loss of that key makes the encrypted backups unusable.

## Bringing up the node

Run only on `tr-bitcoin-1` after release verification, backup-bucket IAM and
dedicated disk provisioning. Install the reviewed, committed scripts and public
recovery key. Never put passwords, seeds or macaroons in instance metadata.

1. Run `lnd_node.py install --peer-ip=<reserved IPv4>` as root.
2. Update the Bitcoin bootstrap source in metadata and `/opt/tr-bitcoin/node.py`.
   Its optional `/etc/bitcoin/lightning.conf` include preserves RPC/ZMQ across
   subsequent boots. Regenerate Bitcoin config and restart Core cleanly.
3. Start LND with peer ingress still closed. Initialize once with
   `lnd_node.py initialize`. Download encrypted backups and verify decryption.
4. Restart LND to prove automatic unlocking. Start the backup timer, verify the
   first static channel backup and its off-node copy, then open only TCP 9735.
5. Use `lnd_node.py status` to get the public node ID and sync/channel state.

## Inbound liquidity

Greg opens a channel **from his node to ours**, funded on his side, with no push
amount. That remote balance is our receiving capacity, not money donated to us.
Provide the real `identity_pubkey@node.lightningrouter.ai:9735` from `getinfo`.
Agree capacity with Greg; do not confuse channel opening with paying a BTC
address or Lightning invoice. Wait for the channel's confirmations and verify
it is active with usable remote balance before accepting customer deposits.

## Launch gates still separate

An unlocked, synced node with a funded inbound channel does not prove the web
flow. Test the complete invoice settlement, once-only USD conversion, account
creation only after funding, key reuse, and inference before enabling payments.
Treat backup failures, insufficient liquidity and unsynced chain/graph as
readiness failures. Do not silently bypass those gates.
