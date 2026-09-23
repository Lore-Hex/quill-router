# Lightning receiving liquidity

New production invoices use Lexe. BTCPay is stopped; the existing LND node is
retained for legacy invoices and funds, not new receiving capacity. The LND
capacity and replenishment guidance below applies only when LND is the active
backend. Do not fund or restart the old node to address a Lexe readiness alert.

When active, BTCPay uses the existing LND node; it does not replenish a channel automatically.
Keep its invoice-only macaroon and Greg's Observer role unchanged. Do not replace
the funded node, restore an old channel database, or add a second wallet daemon.

## Monitoring

The funding worker checks the active backend once per minute, even with no
customer activity. With Lexe it verifies receive-only authority and wallet
identity through the attesting sidecar. Its successful heartbeat includes
`backend=lexe`, `jit_liquidity=true` and `receive_authority_ready=true`, not a
channel balance. A readiness failure does not establish insufficient liquidity;
successful readiness does not prove end-to-end payment settlement either.

The alert is named **LightningRouter: payment receiving needs attention**.
The historical `lightning.liquidity_check_failed` event name remains for alert
compatibility. Inspect the backend before following any LND instructions. The
installer renames the old alert in place rather than creating a duplicate.
Read [the Lexe runbook](lightning-lexe.md) for the current receive path.

For an active LND backend, the funding worker checks LND once per minute.
It uses only GetInfo and ListChannels, already available to its invoice-only
credential. Internal logs contain sync, active channel count, inbound balance
after reserves and pending HTLCs, and the largest individual channel's receiving
capacity. The latter also respects the receiving in-flight limit. Neither is a
guarantee of a route from every payer, and channel capacities are not added
together to promise a single payment will work.

The existing on-call channel alerts below 300,000 sats of LND receiving capacity
or on a failed check of either backend, limited to one notification per hour. A separate ten-minute
absence alert detects a stopped check. Liquidity failures do not prevent the
worker from crediting already-paid invoices. New invoice capacity checks remain
in place. Customer identities, invoices, preimages, peer IDs and wallet
credentials are never exported in these logs or the public health response.

After deploying the funding image, reconcile alerts using the separately
authenticated deployment identity:

```sh
uv run python -m scripts.lightning.reliability --account DEPLOY_IDENTITY --apply
```

The installer installs log-based failure alerts first, then requires a fresh
production liquidity heartbeat before completing absence-alert initialization.
A missing sample is not a healthy zero-usage period.

## Replenishment design (blocked, not activated)

Use Lightning Labs Loop Out alongside LND on `tr-bitcoin-1`, not in the public
BTCPay VM. Received BTC moves from the channel to a fresh address belonging to
our existing LND wallet, making room for more inbound payments. It stays BTC;
this does not sell it for USD or send it to Coinbase. Do not use circular
rebalancing for a node with only one channel.

Desired limits, not yet proven enforceable with upstream AutoLoop:

* Trigger at approximately 300,000 sats usable inbound.
* Maximum 250,000 sats moved per swap, one in flight at a time.
* Target ordinary swap fees at 1% and 5,000 sats per seven-day interval. These
  are NOT hard ceilings in the reviewed upstream implementation; see below.
* Only Loop Out, no channel purchases, Loop In or external payout addresses.
* Alert and stop when fees, route availability or provider minimums prevent a
  swap. Never silently raise limits to obtain liquidity.

Reviewed upstream: Loop `v0.35.0-beta`, commit
`91ab84a03240bf262ac594b9a9c5e04faea96174`. Its parameters persist in its database,
unlike the older documentation's statement that they reset on restart. Do not
reset the budget timestamp or delete the database on deployments.

### Upstream review blockers

Do not enable upstream AutoLoop with the proposed limits and promise a hard
spending cap. Review found three mismatches in the pinned implementation:

* The sweep batcher can use up to 20% of the swept amount in a fee spike. The
  AutoLoop admission budget does not cap that recovery spending. A 250,000-sat
  swap could therefore expose roughly 50,000 sats to sweep fees alone.
* Already-dispatched sticky retries can create more swaps after
  `autoloop=false`, without rechecking that setting or the remaining budget.
  Disabling AutoLoop is not a complete stop-new-spending control.
* Percentage-mode admission reserves 100 times the quoted miner fee. A small
  budget can consequently reject otherwise affordable swaps. Raising the
  budget to bypass this would also change the approved exposure.

Use an independently reviewed, durable admission controller with native
AutoLoop disabled, or upstream fixes with equivalent regression coverage.
Every new attempt must reserve its worst-case exposure, check the pause state
and record its outcome before any retry. Approval must cover the separate
recovery-fee reserve. Do not cap recovery blindly and risk losing the principal.
This controller has not been built or deployed, and no Loop daemon or spending
credential was installed during this monitoring change.

Activation prerequisites:

1. Obtain explicit budget approval. Verify release hashes/signatures before
   installing binaries. Check the live service minimum and quote against the
   approved maximum; do not perform a swap just to test it without approval.
2. Isolate the daemon on the node with loopback-only authenticated RPC, a
   dedicated OS user, separate narrowly scoped LND capability, and durable
   swap state plus recovery backups. No admin macaroon in BTCPay or checkout.
3. Set `maxl402cost=0` and `maxl402fee=0` initially. Even a quote/startup can
   request a paid L402 token; those payments are separate from AutoLoop's swap
   budget. If required, explicitly authorize and account for that cost rather
   than letting the defaults spend outside the agreed ceiling.
4. Keep native AutoLoop disabled. Inspect existing rules, fee state and payout
   accounts; refuse unexpected state. Verify all new dispatch goes through
   the reviewed controller and always returns to the existing LND wallet.
5. Scope replenishment to the existing peer. For the observed 750,000-sat
   channel and 7,500-sat reserve, 41% incoming corresponds to 300,000 sats usable
   inbound. Recalculate if capacity/reserves change, rather than applying the
   percentage to unrelated channels. Reserve at least 10% outgoing.
6. Test restart persistence, budget exhaustion, no eligible route, unavailable
   node, duplicate/in-flight prevention and recovery of swap state. Monitor
   actual fees and pending swaps, not only inbound balance. A backup failure
   must prevent new swaps, but must not stop recovery of an in-flight swap.
7. Enable the reviewed controller only after the above passes and limits are
   approved. Test that pause stops NEW attempts, including retries, while
   recovery of admitted swaps continues. Do not kill a daemon that still needs
   to sweep funds or discard its database.

An exhausted single channel cannot guarantee availability, even with AutoLoop.
A second independent inbound channel needs a separate purchase/capital decision.

Sources: [BTCPay liquidity options](https://docs.btcpayserver.org/LightningNetwork/),
[AutoLoop limits](https://docs.lightning.engineering/lightning-network-tools/loop/autoloop),
[pinned Loop CLI](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/cmd/loop/liquidity.go),
[parameters and sticky retries](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/liquidity/liquidity.go),
[fee reservations](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/liquidity/fees.go),
[sweep fee clamping](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/sweepbatcher/sweep_batch.go),
[sweep ratio](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/utils/fees.go).
