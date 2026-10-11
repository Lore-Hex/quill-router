# Shadow regional read deadlines

## Evidence

At 20:28 UTC on October 10, the e007790 control-plane canary emitted
`shadow evidence transaction failed stage=callback reason=deadline_exceeded`
at 203-206 ms, repeatedly in southamerica-east1 and occasionally in
europe-west4. One us-east4 occurrence was also present. These were background
shadow-audit writes, not evidence that customer settlement failed. The bounded
control-plane HTTP 5xx log check remained empty. Aggregate Spanner API alarms
alone cannot attribute every failure to this observer.

The prior repair raised the commit cap but left point reads at 200 ms. The
earlier deterministic regional tests only exercised 141-163 ms reads. They
could not expose reads that exceed 200 ms while the complete transaction still
fits the one-second worker budget.

Canary logs also showed commit deadlines at 854-856 ms total elapsed time.
The separate 500 ms commit cap discarded the last approximately 145 ms of
the shared one-second budget. This was a second unnecessarily early timeout.

## Repair and safety

- Allow shadow point reads up to 500 ms, clipped by the remaining shared budget.
- Keep the one-second total worker budget. Commit/control RPCs can use its
  remaining time, without receiving a fresh budget after the reads.
- Keep LOW priority, no application retries, single-worker isolation, sticky
  evidence gaps, retirement fencing, and customer billing behavior unchanged.
- Keep operator day reads and cleanup scans at their existing 200 ms deadlines.
- Expand deterministic tests to 250 and 320 ms reads, short shared deadlines,
  and slow reads that still fail closed without retries or writes.

Before the production change, 21 cases in the expanded deadline tests failed
and 31 passed. In particular, two 320 ms reads plus a 300 ms commit should
finish in 940 ms but failed on the first read at 200 ms.
After fixing reads alone, four expanded commit cases still failed: 600 ms
commits following 141-163 ms reads should finish inside the shared budget;
1.2-second commits must stop at that budget, not earlier at the old sub-cap.

Deployment is not verification. Require healthy serving revisions, new durable
comparison samples, and regional post-release diagnostics before claiming this
incident resolved. The separate SDK transport-reset retry repair in PR #1668
is not included in this change.

## Second repair (2026-10-11)

After d99b0624a reached every region (revisions of f9bb2ec68, 23:37 UTC),
the failures fell from about 1.6 to about 0.9 per minute over the next 48
minutes (42 lines of `shadow evidence transaction failed`): callback reads
timing out at the 500 ms read cap from europe-west4 and southamerica-east1,
and commits at the remainder of the one-second transaction budget, 410 to
480 ms on average. The Spanner "API failures" policy's threshold is one per
minute, so the alert stayed within reach of a busy minute.

The remaining cause is the budget itself. A transaction from a secondary
region makes two leader reads and a cross-region commit, each measured at up
to about 500 ms, and an observation makes up to three such transactions, so
one second per transaction and one per observation are below what the
slowest region needs. The shadow is a background worker, so its budgets are
sized for that region: two seconds per transaction, a read up to one of
them and the commit the remainder, and three seconds per observation. The
operator day scans keep their 200 ms deadline. The deterministic tests
pin their expectations to the constants and add 450 ms reads with a
two-second budget.

Verification: zero such lines per region over thirty minutes on the new
revisions, and no new incident of the policy.
