# Shared trace ID blocked multi-call settlement

## Cause

The Stage D schema made `gateway_request_id` globally unique in the typed
authorization table. It is a parent audit trace, not a billing idempotency key.
Responses web search, fusion, and other multi-call requests can legitimately
settle several different authorizations under the same trace. The first
settlement succeeded; the next rolled back at the unique index. Background
retries hit the deterministic conflict repeatedly and could exhaust the drain's
HTTP deadline. The former schema test explicitly expected uniqueness; the
in-memory Spanner fake did not enforce it during settlement.

Production evidence on September 5 identified 24 outstanding web-search final
calls, all in the operator's own workspace. Preserve exact customer attribution
in private incident records, not this public document. No prompt/output was
inspected. A separate successful call sharing a trace is not evidence that the
failed authorization has been charged.

## Repair sequence

1. Run `scripts/deploy/migrate_gateway_request_index.sh --prepare` using the
   deployment identity and the existing project/instance/database environment.
   This creates a sparse nonunique `tr_gateway_authorization_by_trace_id` index
   without removing the existing unique index or changing billing rows.
2. Deploy the new readers to every active control-plane region. They use the
   new index with `LIMIT 2`. A request with several authorizations returns
   `409 ambiguous_gateway_request_id` instead of attributing one call to the
   whole request. Use the authorization-specific disposition route for a leg.
3. After all readers are verified, run the script with `--retire-unique`.
   Only the obsolete index is dropped. Reservation claims, authorization primary
   keys, and true idempotency indexes remain unchanged. Do not roll back to a
   reader that requires the old index after this step.
4. Re-arm only the reviewed pending/dead settlement rows whose preserved error
   is this exact index violation. Leave frozen costs and settlement bodies
   unchanged. Use the normal drain in small bounded passes. Verify each
   authorization/reservation/outbox and exactly one generation, with no held
   credit remaining for those requests. Do not mark rows done or edit balances
   manually.

## Regression evidence

- The old evidence reader returned 200 for an ambiguous trace; its regression
  failed before the fix and returns 409 afterward.
- A real loopback Spanner emulator reproduces the old unique-index rejection,
  retains it during prepare, and settles both rows exactly once after retirement.
- A typed billing test settles two calls under one trace, releases both holds,
  and proves replay does not move any counter.
- Executed migration tests cover preparation, retirement, idempotence, and
  refusal to retire before the nonunique index is ready.
- Memory/Postgres lookup tests cover absent, single, and ambiguous traces.

No alert thresholds, retention policies, upstream requests, or billing formulas
are changed by this repair.
