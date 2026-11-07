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

The primary rollout is unchanged. The three secondary traffic ramps start
together, after the primary's ramp and canary have passed on the same image,
and each retains a separate worker for its full retention window. Each worker
completes an additional one-minute canary at 100% and the billing-error gate
before its atomic handoff, which names the region, new revision and current
deployment operation and is checked as it appears. With the repository
variable TR_DEPLOY_SECONDARIES_SERIAL=1 the ramps are serial instead (#695):
a handoff admits the next region.

The old-revision drain tag, minimum capacity, readiness checks, full 15-minute
retention and rollback monitor remain owned by the worker. The original final
canary and billing gate still run. The coordinator joins all three workers
before any workflow completion, public-surface rollout or lease finalization.
At most three secondary workers run. Direct use of ramp_secondaries.sh
remains serial.

A failed worker, or a handoff that names the wrong revision or operation,
stops further admissions and signals other workers to refuse new traffic
shifts and roll back. On a worker timeout the coordinator gives the others
three minutes to do so before it stops them. Existing drains finish under
their monitors. Cancellation
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

Refresh with at least three independent successful runs per profile. A push to
main runs no post-cutover shard, so take post-cutover runs from pull requests or
from post-cutover-daily.yml:

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

### Coverage through sys.monitoring

The default suite's coverage pass ran on coverage.py's C tracer with branch
coverage, at about three times the plain suite's cost (#2368). Before Python
3.14, sys.monitoring cannot measure branches, so `[tool.coverage.run]` sets
`core = "sysmon"` and `branch = false`. The twelve default shards fell from a
median of 25.5 minutes to between 7 and 12 over the five green main runs that
followed. Two tests in
tests/test_ci_workflow.py keep a configuration change from bringing the
tracer back.

### Post-cutover suites leave the push run

A push to main no longer runs test-post-cutover or proof-oracle's
post-cutover clock. Together they were 14 of the run's 46 jobs, about 215
runner-minutes a commit, averaged over 20 green main runs. A red in them
means main will break on a scheduled date. That is no reason to hold the
deploy, which gate-on-ci holds until the push run concludes. They still run
on pull requests and on dispatches, including the price refresh's
publication gate. post-cutover-daily.yml runs both on main once a day,
outside every gate. commit_scope.py's summary accepts test-post-cutover's
skip on a push only.

The TLA+ model checks (proofs, 15 to 26 minutes) follow the same rule.
Outside proofs/ they read only whether the code and tests proofs/manifest.toml
names exist, and none of it says whether deployed code works. They ran beside
the proof oracle's longest shard (26 to 28 minutes), so skipping them frees a runner
rather than shortening the run. The cost: proofs is not a required check, so
a pull request can merge with it red, and main's push runs no longer show it.
main learns of it from the next price refresh that runs the full matrix,
whose publication it then holds, or from the daily run, the one run on main
sure to check it. With coverage cheaper, the default suite runs in eight
shards instead of twelve. On #2377's run they took 8 to 14 minutes each,
still inside that longest shard, with four fewer jobs in the queue and 91
runner-minutes against 110 to 126.

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
