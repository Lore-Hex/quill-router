# Cloud alerts, October 18, 2026

All times are UTC. This investigation separates recovered events from verified
repairs. Customer identifiers and raw logs are not included in this document.

## Latest Spanner latency notification

The commit-p99 alert opened at 06:18:10 with a value of 1.002687 seconds against
the unchanged one-second threshold and resolved at 06:19:27. No Cloud Run
HTTP 5xx was found in the bounded 06:08-06:25 window.

Bounded minute transaction statistics show normal authorize and settlement
average commit times around 21-26 ms. Some fast-path renew/commit operations
contended on lease rows earlier in the interval, including 5.91 aggregate
lock-wait seconds on one lease during the minute ending 06:14. This identifies
contention, but does not establish which operation produced the notified tail
latency. An average is not a substitute for the missing percentile breakdown.

Subsequent transaction histograms and lock samples identified unnecessary
expiry reads and revocation writes in the auditor commit, conflicting with
lease renewal. See [the lease-lock investigation](2026-10-18-fastpath-lease-locks.md)
for both alert windows and the repair in PR #1839. The renewed operator login
also permits the deployment checks; the read-only ops identity was not broadened.
No latency threshold, timeout, or billing invariant was relaxed.

## Earlier Spanner API failures

The notification at October 17 01:21:39 resolved at 01:22:37. Logs around the
incident contain transaction aborts and deadline exhaustion concentrated on
one workspace's credit shard 0 and one API key's shard 0. The hour ending
02:00 recorded approximately 9,089 aggregate lock-wait seconds on the credit
row and 11,294 on the key row. The finalization path had 1,134 aborts across
1,220 attempts; these are retry attempts, not counts of affected customers.

No Cloud Run HTTP 5xx was found from 00:55 through 01:30. This does not prove
there was no latency, rejected request, or delayed settlement. Current exact-key
reads show the implicated workspace and non-strict key both configured for 16
shards, a weekly cap still present, no outstanding credit reservations, and no
negative shard balances. The investigation did not change the customer's cap,
strictness, funding, or counters. Historical request-to-shard attribution and
the reason traffic concentrated on shard 0 still need verification.

## Fast-path pilot audit

Two October 17 04:04/04:11 events were explicitly marked alert-path exercises.
The 05:13 events were real audit failures: the supplied funding baseline was
zero while the typed credit rows contained positive funding. Current indexed
reads corroborate the positive funding and nonnegative available balances.
This is not evidence that money disappeared.

After operator reauthentication, direct VM inspection confirmed the hourly
wrapper was running continuously since October 17 05:15:23. Its indexed funding
read rejects unreadable or nonnumeric output instead of defaulting to zero.
Every recorded hourly audit for both pilot workspaces passed through October 18
18:18:26. The first successful record at October 17 05:15:13 predates the current
loop. The earlier 05:13 caller that explicitly supplied zero is not identified;
this investigation did not modify the wrapper or customer balances. Preserve
the VM-local loop when replacing the fast-path service.

## Confirmed repair: product-notice permission

At 04:15:32 the retirement preview failed in Cloud Run because the SES send-only
user lacked permission for the new notices identity. Domain verification,
DKIM, and custom MAIL FROM were successful. The two existing sender-policy
statements authorized sign-in and alert mail only.

Applied the separate, exact-sender policy documented in
[the notices runbook](../runbooks/ses-notices.md). IAM simulation reproduced
the denial before the repair, then allowed both required resource ARNs;
alternate From addresses still failed. Readback matched the checked-in policy.
No new CI IAM permissions, customer email rollout, or suppression changes were
made. An operator-only canary using the application's actual SES sender identity
and notices configuration set was accepted with HTTP 200 by SES, addressed only
to Amazon's success mailbox simulator. Message ID:
`010001a15075c243-4a9fd9b8-2367-49ec-9f46-6ebb1eaf00d6-000000`.
This verifies real send authorization, not delivery to a customer's inbox.

## Serving and rollout checks

All four GCP control-plane services were Ready at 100% on their healthy
revisions. Public website/API health, status JSON, and leaderboard returned
200; the status snapshot reported up. Cost-free cloud-local coordinator checks
also passed for GCP, AWS, and Azure.

Deploy run 39101272872 subsequently stopped during its no-traffic warmup because
Azure failed a fresh holdback check. The same check passed during this
investigation. No traffic moved. Its completed owner and unchanged, healthy
regional services permit recovery of that run's GCP reservation through the
reviewed coordinator workflow. The separate manual AWS gateway reservation
must not be cleared or borrowed.

The later recovery workflow 39107703646 completed successfully. At October 18
20:46, public status was up and a bounded Cloud Run check found no error-level
or HTTP 5xx records since 20:00. The fast-path lock repair is merged but not
deployed: release-pin PR #1844 is held after its CI exposed the separate
[subscription shutdown cycle](2026-10-18-settlelog-shutdown.md), repaired in
PR #1848. Neither production node was drained or replaced during these checks.
