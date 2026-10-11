# Shadow audit follow-up after regional deployment

## Production evidence

Release `f9bb2ec68f879167bef8f783af50b35970840df1` reached 100% of
control-plane traffic in all four regions by 2026-10-11T00:06:06Z.
Async settlement remained disabled. October 10 counters contain 26 comparison
attempts and 26 persisted samples for that release, with no comparison drops.
This is improvement, not complete recovery.

The new Brazil revision still logged callback deadlines at 504-507 ms during
staging and after receiving traffic. Europe also logged a 505 ms callback
deadline after deployment. At UTC midnight, Europe and Brazil logged commit
deadlines after approximately 312-499 ms within individual transactions.
Bounded control-plane HTTP 5xx queries were empty. These observations concern
the background evidence writer, not proof of failed customer settlement.

Counter region labels currently use the primary region even on other serving
regions; PR #1667 tracks that separate issue. Use Cloud Run resource labels
for regional attribution, not these counter labels.

## Reproduced defects

1. A 600 ms read, 50 ms read and 200 ms commit fit inside the existing one-second
   total budget. The separate 500 ms read cap rejects that work prematurely.
   Four real-SDK cases failed before the fix, including slow first and second
   reads and genuinely over-budget commit controls.
2. Normal midnight maintenance closes yesterday's counter and opens today's
   counter in separate transactions sharing one second. Two individually
   healthy 550 ms transactions cause the second to fail at its 450 ms remainder.
   The real-SDK rollover test reproduced that exact commit diagnostic before
   the scheduling repair.

The production stage-only logs cannot prove which SQL statement exceeded its
cap. These tests establish concrete defects consistent with the observed
timings; they do not attribute every Spanner API alarm to these paths.

## Repair and preserved limits

- All shadow RPCs may use the remaining shared budget, never a renewed second.
  Standalone operator reads retain their shorter caller deadline.
- After a successful normal old-day close, defer additional counter writes
  until the next maintenance pass. New-day counters remain in bounded memory.
- Keep the five-second maintenance cadence, single worker, LOW priority,
  retention fence, no application retry, sticky failure evidence, and unchanged
  customer billing behavior. Shutdown's explicit final-close path is unchanged.
- Keep assertions for failed-close retention, concurrent observer accounting,
  in-flight old-day work, timeout bounds and SDK restart behavior. Observer
  lifecycle tests now allow the next maintenance pass to register the new day.

Deployment still requires green CI and normal guarded regional rollout.
Recovery requires post-deploy durable comparisons and clean regional diagnostics;
do not reset historical gaps or loosen production alerts.
