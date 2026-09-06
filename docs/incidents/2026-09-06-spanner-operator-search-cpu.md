# Spanner CPU spike from administrative payment searches

## Evidence

All timestamps below are UTC, on 2026-09-06 (September 5 evening in California).

- Three literal Stripe PaymentIntent searches completed in the 03:15 and 03:16
  query-statistics buckets. Each searched `tr_entities` with a leading-wildcard
  `body LIKE` or `id LIKE`, without a leading primary-key predicate, and used
  `LIMIT 5`.
- The three searches consumed 30.38, 30.26 and 30.14 CPU-seconds respectively;
  each took approximately 35 seconds wall time.
- Top-query CPU was normally about 7-8 seconds per minute. At 03:15 it was
  67.30 CPU-seconds, of which 60.63 came from these searches; at 03:16 it was
  45.80, of which 30.14 came from the searches.
- The high-priority CPU alert opened at approximately 03:23, reporting 61.03%
  against the unchanged 45% threshold. Its recovery notification approximately
  78 seconds later reported 19.73%.
- The alert definition uses five-minute maximum alignment and a five-minute
  condition duration. Email time is not the start time of the expensive query.
- No internal gateway billing HTTP 5xx was found in Cloud Run request logs
  between 03:10 and 03:26. This does not assert that every customer request was
  successful; fast authorization 429s require separate classification.
- No recurrence of these body searches was found in the bounded system
  statistics after 03:17 at the follow-up check.

## Root cause

A read-only administrative lookup was treated as harmless because it returned
at most five rows. The result limit did not bound work: finding an absent or
unindexed substring still scans the legacy entity table. The payment processor
already supports direct retrieval by the identifiers being investigated.

The accounts associated with the searched payments were resolved privately by
an exact Stripe GET and complete-key workspace/user reads. They are subjects
of the investigation, not proof that those customers caused the incident.
Payment IDs, customer emails and workspace IDs are deliberately omitted here.

Spanner data-access audit records for the original query window were not
available to the incident reader, so the original authenticated caller remains
unverified. Do not attribute the queries to a named person or agent by inference.

## Remediation

- Added `scripts.inspect_payment_owner`: one exact Stripe GET and at most two
  complete-primary-key Spanner reads. There is no arbitrary-SQL option and no
  fallback body search when metadata is absent.
- All Spanner reads use LOW priority, the `tr_ops_payment_owner` request tag,
  five-second RPC deadlines and no automatic retries. Session creation and
  deletion also have explicit deadlines and no retries.
- Use the generated RPC client, not the high-level session manager. A live
  test exposed unbounded SDK session setup and an unnecessary background
  metrics exporter with a read-only credential; neither is used by the helper.
- The helper emits start/end/failure receipts, including its own operator
  service-account identity, without keys, payment response bodies, or card data.
  Email output requires `--include-email`.
- Added explicit guidance in AGENTS.md, a Claude entry point, and the Spanner
  runbook. Tested complete-key constraints, privacy, failure paths, session
  lifecycle and the absence of retry/search fallbacks.

The spike recovered before remediation. No customer balance, application
deployment, alert threshold, suppression, or IAM grant was changed. This tool
is a safe operational path, not an IAM-enforced restriction on arbitrary SQL.
Replacing broad operator SQL access with a reviewed query broker is a separate
access-design change; existing audits must be migrated before removing access.
