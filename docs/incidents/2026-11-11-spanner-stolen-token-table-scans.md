# November 11 Spanner CPU, latency and API-failure alerts: whole-table scans with a stolen service-account token

## Evidence

All timestamps are UTC on 2026-11-11 (November 10 evening in California)
unless a date is given. Raw logs and account identifiers are not published
here.

Three policies opened within two minutes of each other and resolved on their
own:

| Policy | Opened | Resolved |
|---|---|---|
| Unexpected API failures exceed one per minute for 5 minutes | 03:37 | 03:46 |
| Any request p99 exceeds 3 seconds for 10 minutes | 03:38 | 03:47 |
| High-priority CPU above 45% for 5 minutes (62.3%) | 03:38 | 03:39 |

`SPANNER_SYS.QUERY_STATS_TOP_MINUTE`, statements with no request tag that
scanned more than 100,000 rows per execution:

| Minutes | Statements per minute | CPU-seconds per minute |
|---|---|---|
| 03:22 to 03:23 | 1 to 2 | 4 to 5 |
| 03:24 to 03:27 | 1 to 4 | 12 to 39 |
| 03:29 to 03:30 | 1 | 87 to 90 |
| 03:31 to 03:38 | 1 to 16 | 105 to 303 |
| 03:39 onward | none | none |

Over the same half hour all tagged application statements together used 3
to 9 CPU-seconds per minute.

Two families of statements made up the load. Neither carries a request tag,
and no application code path issues either:

- One parameterized count over `tr_entities` filtered by a `JSON_VALUE`
  predicate on `body`, with no key predicate. It ran 15 times, one or two at
  a time, from 03:29 to 03:38. Each execution read all 9.25 million rows and
  took up to 102 seconds. Total: 1,284 CPU-seconds and 138.7 million rows.
- `REGEXP_CONTAINS` predicates over the text columns of seven tables
  (`tr_entities`, `tr_gateway_authorization`, `tr_generation`,
  `tr_settle_outbox`, `tr_reservation`, `tr_lease_record`,
  `tr_lease_winners`), each reading the whole table: between 191,000 and
  9.25 million rows per execution.

The application paid for it in reads, not in commits. In the 03:31 minute
492 statement executions failed, 7 timed out and 2 were cancelled. The
`tr_async_admission` read had 8 failures and 6 timeouts across the window.
`TXN_STATS_TOTAL_MINUTE` stayed ordinary throughout: 390 to 990 commit
attempts a minute, 0 to 6 aborts, about 0.015 seconds average commit
latency.

The same statistics show this is not the first time. Untagged statements
scanning more than a million rows per execution, by hour, over the previous
three days:

| Hour ending | Statements | CPU-seconds | Tables |
|---|---|---|---|
| 11-09 09:00 | 19 | 520 | `tr_generation`, `tr_gateway_authorization`, `tr_entities` |
| 11-10 07:00 | 4 | 16 | `tr_gateway_authorization` |
| 11-10 10:00 | 1 | 2 | `tr_entities` |

Those three stayed under the alert thresholds. A further single daily scan
of `tr_reservation` near 19:00 costs 2 to 23 CPU-seconds and is a separate,
scheduled reader.

## Root cause

The statements came from outside. An unknown party had obtained an access
token for the service account the ClickHouse analytics nodes run as, and used
it against the production database from 2026-11-09 07:12 until the account
was disabled at about 05:10 on 11-11. That account
held a database-wide read and write role, granted by a provisioning script and
not declared in Terraform. No penetration test was commissioned.

The chain, proven from disk images of the affected machines:

1. A chat server used for an ended operations experiment was reachable from
   the internet with federation turned on, and ran inside the production
   network. Federation makes the server fetch URLs named by other servers, so
   from 2026-11-08 22:30 outsiders used it to make requests to internal
   addresses.
2. Through it they reached the internal ClickHouse load balancer. ClickHouse's
   built-in `default` user had an empty password and no network restriction:
   the pinned ClickHouse packages ignore the installer's password setting in a
   non-interactive install, and nothing checked the result on disk.
3. As `default`, ClickHouse's `url()` function fetched the node's metadata
   server token, which carried the analytics service account.

With that token the party searched whole tables for credentials and wrote
credit to an account it had created. The scans in the tables above are those
searches: the three earlier hours (11-09 09:00, 11-10 07:00 and 10:00) and the
03:22 to 03:38 window on 11-11 all fall inside the period the token was in
use.

## Remediation

All on 2026-11-11:

- The service account was disabled, its database role removed and the
  ClickHouse nodes stopped (about 05:10). The role was restored when the
  account was re-enabled (below); narrowing it is listed under Not done. Disk snapshots of every affected
  machine were taken before any other change.
- The chat servers were destroyed on every cloud and the chat integration
  removed, with its network entry points.
- ClickHouse nodes were hardened (#2486) before restart: the `default` user is
  removed, `url()` and `remote()` may reach no host, the ClickHouse process is
  blocked from the metadata server, and both provisioning and the
  configuration roll refuse to finish unless an anonymous query is refused.
  The service account was re-enabled only after that was verified on every
  node.
- The credit the party wrote was removed with an audited negative grant, and
  its account and the automated accounts created through the same sign-up
  method were paused. That sign-up method is turned off.
- Outbound fetches of user-supplied URLs are restricted to public addresses
  (#2479).
- AGENTS.md now states the read rule for every production table and every
  whole-table shape (pattern match, JSON predicate, unfiltered count,
  whole-table `GROUP BY`), and requires naming the key or index a statement
  seeks on before running it. The scans here were not an operator's, but the
  rule had the same gap the September 6 note found, and a stolen credential
  is not the only way such a statement reaches the serving instance.

No alert threshold, suppression or instance capacity was changed.

## Not done

- Narrowing the analytics account to a table-scoped database role declared
  in Terraform, and enabling `DATA_READ` audit records, are in progress.
- A ClickHouse credential rotation is in progress (#2487).
- Direct SQL access is still not IAM-enforced. The September 6 note called a
  reviewed query broker a separate access-design change; it still is.
