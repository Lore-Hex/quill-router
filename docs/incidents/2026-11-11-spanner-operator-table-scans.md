# November 11 Spanner CPU, latency and API-failure alerts: whole-table operator scans

## Evidence

All timestamps are UTC on 2026-11-11 (November 10 evening in California).

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

Someone with direct SQL access ran whole-table searches on the serving
instance at the default (HIGH) priority. A statement with no key or index
predicate reads every row no matter how few it returns, and at HIGH
priority it competes with admission and billing reads for the same CPU.
This is the September 6 incident again
([2026-09-06-spanner-operator-search-cpu.md](2026-09-06-spanner-operator-search-cpu.md)),
on more tables and for longer.

Two things let it recur:

- The written rule named one table. AGENTS.md prohibited searching
  `tr_entities.body`; a reader scanning `tr_generation` or
  `tr_gateway_authorization` could believe the rule did not apply.
- The caller cannot be identified. The project records only `ADMIN_READ`
  audit entries; Spanner data reads are not logged. The September 6 note
  recorded the same gap. A search of every agent transcript and command log
  on the operator workstation found none of these statements, for this
  window or for the three earlier ones, so they were issued from somewhere
  else. Do not attribute them to a named person or agent by inference.

## Remediation

- AGENTS.md now states the rule for every production table and every
  whole-table shape (pattern match, JSON predicate, unfiltered count,
  whole-table `GROUP BY`), and requires naming the key or index a statement
  seeks on before running it.
- `infra/spanner_audit.tf` (separate pull request) declares `DATA_READ`
  audit records for the Spanner API, exempting the serving fleet's service
  accounts so that the volume is operator, CI and vendor traffic only. The
  next occurrence will carry a principal.

The scans stopped at 03:38 before any change. No alert threshold,
suppression, instance capacity or IAM grant was changed.

## Not done

- Direct SQL access is still not IAM-enforced. The September 6 note called
  a reviewed query broker a separate access-design change; it still is, and
  the audit records are the evidence that change needs: which principals
  actually issue ad hoc SQL.
- There is no approved path for a whole-table question that is not request
  analytics. Until one exists (an offline copy, or independent compute),
  the answer is to ask the operator, not to run it against the serving
  instance.
