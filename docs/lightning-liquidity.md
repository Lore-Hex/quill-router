# Lightning receiving liquidity

BTCPay uses the existing LND node; it does not replenish a channel automatically.
Keep its invoice-only macaroon and Greg's Observer role unchanged. Do not replace
the funded node, restore an old channel database, or add a second wallet daemon.

## Monitoring

The funding worker checks LND once per minute, even with no customer activity.
It uses only GetInfo and ListChannels, already available to its invoice-only
credential. Internal logs contain sync, active channel count, inbound balance
after reserves and pending HTLCs, and the largest individual channel's receiving
capacity. The latter also respects the receiving in-flight limit. Neither is a
guarantee of a route from every payer, and channel capacities are not added
together to promise a single payment will work.

The existing on-call channel alerts below 300,000 sats of receiving capacity or
on a failed check, limited to one notification per hour. A separate ten-minute
absence alert detects a stopped check. Liquidity failures do not prevent the
worker from crediting already-paid invoices. New invoice capacity checks remain
in place. Customer identities, invoices, preimages, peer IDs and wallet
credentials are never exported in these logs or the public health response.

After deploying the funding image, reconcile alerts using the separately
authenticated deployment identity:

```sh
uv run python -m scripts.lightning.reliability --account DEPLOY_IDENTITY --apply
```

The installer requires a fresh production liquidity heartbeat before it can
claim success. A missing sample is not a healthy zero-usage period.

## Proposed replenishment policy (not activated)

Use Lightning Labs Loop Out alongside LND on `tr-bitcoin-1`, not in the public
BTCPay VM. Received BTC moves from the channel to a fresh address belonging to
our existing LND wallet, making room for more inbound payments. It stays BTC;
this does not sell it for USD or send it to Coinbase. Do not use circular
rebalancing for a node with only one channel.

Proposed limits awaiting operator approval:

* Trigger at approximately 300,000 sats usable inbound.
* Maximum 250,000 sats moved per swap, one in flight at a time.
* Total swap fees at most 1% and 5,000 sats per seven-day budget interval.
* Only Loop Out, no channel purchases, Loop In or external payout addresses.
* Alert and stop when fees, route availability or provider minimums prevent a
  swap. Never silently raise limits to obtain liquidity.

Reviewed upstream: Loop `v0.35.0-beta`, commit
`91ab84a03240bf262ac594b9a9c5e04faea96174`. Its parameters persist in its database,
unlike the older documentation's statement that they reset on restart. Do not
reset the budget timestamp or delete the database on deployments.

Activation checklist:

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
4. Configure with AutoLoop OFF first: `--autoloop=false --easyautoloop=false
   --feepercent=1 --autobudget=5000 --autobudgetrefreshperiod=604800s
   --autoinflight=1 --maxamt=250000 --sweepconf=100 --failurebackoff=86400
   --destaddr=default`. Clear any preexisting rules or external account
   selection only after inspecting them; refuse unexpected state.
5. Add one Loop Out rule for the existing peer. For the observed 750,000-sat
   channel and 7,500-sat reserve, 41% incoming corresponds to 300,000 sats usable
   inbound. Recalculate if capacity/reserves change, rather than applying the
   percentage to unrelated channels. Reserve at least 10% outgoing. Read back
   parameters and inspect `suggestswaps` while still disabled.
6. Test restart persistence, budget exhaustion, no eligible route, unavailable
   node, duplicate/in-flight prevention and recovery of swap state. Monitor
   actual fees and pending swaps, not only inbound balance. A backup failure
   must prevent new swaps, but must not stop recovery of an in-flight swap.
7. Enable AutoLoop only after the above passes and limits are approved. To
   pause new swaps use `loop setparams --autoloop=false`; do not kill a daemon
   that still needs to sweep funds or discard its database.

An exhausted single channel cannot guarantee availability, even with AutoLoop.
A second independent inbound channel needs a separate purchase/capital decision.

Sources: [BTCPay liquidity options](https://docs.btcpayserver.org/LightningNetwork/),
[AutoLoop limits](https://docs.lightning.engineering/lightning-network-tools/loop/autoloop),
[pinned Loop CLI](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/cmd/loop/liquidity.go),
[parameter persistence](https://github.com/lightninglabs/loop/blob/v0.35.0-beta/liquidity/liquidity.go).
