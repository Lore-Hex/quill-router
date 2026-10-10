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
