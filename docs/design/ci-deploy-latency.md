# CI and production rollout latency

## Baseline (2026-10-27)

Three successful main CI runs were profiled: 40599396957, 40623176778 and
40625099457. Default test jobs ranged from 12m28s to 28m48s, with the two
latest runs ranging from 14m19s to 28m48s. This is runner wall time, including
setup; test-duration weights measure only individual tests. The default suite
uses coverage, while the post-cutover suite does not, so those profiles cannot
be merged into a single set of weights.

Deploy run 40573913926 attempt 2 took about 118 minutes after admission to the
workflow: 104s building, 31m22s primary, 67m29s secondaries and 8m32s public
surfaces, plus admission/finalization. Its earlier CI failure and queue wait
made the end-to-end delay much longer. Cloud reservations can add further
waiting and remain an independent safety gate.

## Changes

### Supervised overlapping retention

The primary rollout is unchanged. Secondary traffic ramps remain serial, but
each completed ramp retains a separate worker for its full retention window.
Before admitting the next region, the worker must complete an additional
one-minute canary at 100% and the billing-error gate. Its atomic handoff names
the region, new revision and current deployment operation.

The old-revision drain tag, minimum capacity, readiness checks, full 15-minute
retention and rollback monitor remain owned by the worker. The original final
canary and billing gate still run. The coordinator joins all three workers
before any workflow completion, public-surface rollout or lease finalization.
At most three secondary workers run; there is never more than one ramp being
admitted at a time. Direct use of ramp_secondaries.sh remains serial.

A failed worker stops further admissions and signals other workers to refuse
new traffic shifts. Existing drains finish under their monitors. Cancellation
or a 35-minute per-worker timeout reaps the process groups, leaves drain tags
in place and retains the cloud reservation for explicit recovery. Regional
rollback requires the current deployment fence. Operator holds, revision
ordering and the cross-cloud healthy-holdback rule remain unchanged.

### Multi-run test weights

Default and post-cutover timings use separate committed profiles, derived from
all three successful runs. Of 42,436 observed tests, 41,577 had three samples,
846 had two and 13 had one; 6,271 IDs were absent from the previous weights.
Use the median for three or more samples and the maximum for sparse new cases.
Preserve previously known weights for tests not observed in these runs.

Refresh with at least three independent successful runs per profile:

```sh
gh run download RUN_ID -p 'durations-*' -D /tmp/ci-durations/RUN_ID
uv run python scripts/ci/merge_shard_durations.py /tmp/ci-durations .test_durations .test_durations --profile default
uv run python scripts/ci/merge_shard_durations.py /tmp/ci-durations .test_durations .test_durations-post-cutover --profile post-cutover
```

Coverage, shard/worker counts, xdist loadgroup, database serialization and all
oracle gates remain intact. Oracle timings are now recorded independently for
future multi-run balancing; they are not mixed into full-suite weights.

### JavaScript harness isolation

The failing exchange analytics subprocess exceeded its existing 10-second
timeout on one runner. Thirty isolated reproductions completed in 53-64ms,
and the harness has no live network calls or real scheduled timers. The
original stall's cause is not proven; runner contention is a hypothesis.

Batch the six pages into one Node startup per test group, with a fresh VM for
each page. Preserve every end-to-end collector/export assertion and the
10-second subprocess deadline. Bound script and callback execution to one
second within each VM. Startup and completion diagnostics distinguish slow
process startup from a stalled page on recurrence. There are no retries or
failure suppression.

### Separate preparation from serialized production

deploy.yml checks exact-commit CI and builds an immutable image digest outside
the production queue. It calls deploy-production.yml only when both succeed.
The reusable workflow retains the existing deploy-trusted-router concurrency
group, with cancel-in-progress false and queue max. GitHub retains up to 100
prepared runs instead of the previous single pending run. This also avoids an
older, slow build replacing the pending newer release.

On admission, revalidate CI, checkout identity and image digest. Before taking
the cloud reservation, an older push can yield only to a newer, still-on-main
push with green CI and a completed image build, or a newer release that already
completed every production gate. It never retargets an image to a different
commit. After taking the reservation, a newly arriving push does not cancel or
supersede the active rollout. A final ancestry check still prevents deploying
a revision removed from main. Migrations, secret synchronization, attestation,
regional/public checks and durable lease finalization remain production-gated.

Cloud release reconciliation recognizes reusable-job names (rollout / JOB)
and still requires all GCP production gates before AWS/Azure promotion.

## Validation and measurement

Tests cover process handoffs, late failures, serial fallback, malformed tokens,
worker cleanup, complete retention, stale fences, primary isolation, prepared
successor eligibility, out-of-order preparation and cloud gate reconciliation.
The DDL carrier registry follows the moved production statements; only the two
exact generated timing files are excluded from its code scan.

GitHub documents queue max at
https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency.
The locally available actionlint 1.7.12 predates that key. Its other checks pass;
do not remove queue protection or broadly disable validation to satisfy an old
schema. GitHub's acceptance of the workflow must also be checked on rollout.

Compare at least three new green runs against the baseline, separately for
each clock and oracle matrix. Record slowest job, max/min spread, test execution
versus setup time, and reruns. For deployment, record preparation, queue/lease
wait, primary, secondary ramps, retained minutes per region and final gates.
Overlapping three 15-minute waits should remove roughly two waits, less the
additional handoff canaries. This is an estimate, not a measured production
improvement. Shortening retention or skipping checks is not an optimization.
