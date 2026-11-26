# CI and production rollout latency

## Emergency GCP control-plane release

Merge the fix to `main`. From this repository:

```sh
python3 -m scripts.deploy.release_train incident-plan   # optional, read-only
gh workflow run deploy.yml --ref main -f emergency_incident=INC-123
```

Use the actual incident ID. In Actions, choose **Deploy TR control plane → Run workflow**,
select **main**, fill in **emergency_incident**, and leave the other options off.
Follow the **Emergency main** run. Do not include customer information in the ID.

This is a forward release of the exact main revision captured at dispatch, not a restart
or a downgrade. It reuses the normal push's immutable image. When that commit never
triggered a push build, the incident run builds it using the same Cloud Build job.
Missing evidence from an existing build, a failed source release, or non-green CI
does not authorize using a different image. If main advances before selection, inspect
the new head and dispatch again. Selection rechecks the digest, source run/attempt
and CI under the production reservation. The selected-release receipt records the
incident, controller, source build, digest and CI run.

Once the incident is ready and queued, **waiting automatic releases yield before taking
the cloud reservation**. Active rollouts, pinned manual releases, website publications,
and other clouds' reservations are not interrupted. Expect queue time if one is active;
there is no guaranteed incident-to-serving time. Normal canaries, rollback protection,
billing checks, migrations, drains and regional verification all still run. Look for
the primary traffic ramp to see **first traffic**; all-region completion is later.

This is not the legacy `hotfix=true` override, an enclave release, or an AWS/Azure deploy.
Don't use it to test an outage on production: the plan command never dispatches or writes.

## November 25: publish analytics snapshots alongside primary rollout

The last step of the schema job was an analytics worker upload, not a schema
migration. Its measured step times were 145s, 90s and 76s in full releases
[45505800208](https://github.com/Lore-Hex/quill-router/actions/runs/45505800208),
[45523293985](https://github.com/Lore-Hex/quill-router/actions/runs/45523293985)
and [45531631746](https://github.com/Lore-Hex/quill-router/actions/runs/45531631746).
All that time preceded the first API warmup. The publisher now uses its own
runner, checks out the selected commit, asserts the existing GCP reservation,
and starts alongside the primary once schema, readiness and ancestry pass.
Finalization still joins it. A publication error still produces a warning;
an interrupted job is not a clean release.

To measure the effect, compare time from cloud admission to primary warmup
on **traffic-moving** runs. Check that snapshot publication and the primary
overlap and that the worker reports all five snapshot products. Don't count
queue time as a saving, or subtract the upload from a release where a different
gate was slower. Schema changes are still serial and complete before either
branch starts. Traffic ramps, canaries and revision retention are unchanged.

## Publishing just the TrustedRouter website

Merge a PR that modifies existing homepage HTML or copy
(`src/trusted_router/templates/homepage/*.html` or `content.json`), homepage
`application.css` or `components.css`, or the public `docs.html`, `about.html`
or `blog_index.html` template. Tests and repository documentation can accompany
the change. An eligible push publishes the site in the four GCP regions
without replacing API revisions. From the checkout, preview the classification
of the most recent commit with `python3 -m scripts.deploy.public_docs scope`.

For an explicit **website or nothing** run on an eligible main commit:

```sh
gh workflow run deploy.yml --ref main -f website_only=true
```

Or select **Deploy TR control plane → Run workflow → website_only**. Leave
hotfix and track_main off. CI for the exact main commit and the immutable build
must pass. The publisher checks the diff from *every serving source*, acquires
the same exclusive production lock and rechecks before mutation. A mixed change,
a new/renamed file, an application or routing change, or an unshipped backend
change prevents website-only publication. Normal pushes can fall back to the
full rollout; this explicit mode cannot. Look for the `public-release-*` receipt
after regional smoke checks, including the website pages and both stylesheets.
This is for trustedrouter.com, not the separate Token Exchange site.

Full routed releases still deploy and smoke the public service unconditionally.
The public companion job's **Website redeploy comparison** summary records
image, source and configuration differences across all four regions. Missing
evidence is unknown, not a match. The report has no deploy output and does not
check IAM or resolve secret contents; do not use it as skip authorization.

## November 24 measurements

The default-clock billing proof runs in eight groups of four workers. Its
refreshed weights are the per-test medians from green main runs 45363497140,
45375079247, 45445735259 and 45452805369. All eight timing artifacts from
each run contained the same 1,614 distinct cases in total. To reproduce:
download `durations-oracle-default-*` under one directory per run and use
`merge_shard_durations.py --profile oracle-default` with that parent directory.
Keep the xdist group suffixes; a database group's cases still run serially
within each shard. No case, worker, gate or timeout changed.

Two later runs were withheld. Replaying pytest-split's least-duration
algorithm with the old weights reproduced the actual assignments exactly;
1,422 cases moved with the new weights. Using each held-out run's recorded
per-case times:

| Held-out main CI run | Old busiest group | New busiest group |
| --- | ---: | ---: |
| [45460981383](https://github.com/Lore-Hex/quill-router/actions/runs/45460981383) | 4,330 test-seconds | 3,535 test-seconds |
| [45465786675](https://github.com/Lore-Hex/quill-router/actions/runs/45465786675) | 2,792 test-seconds | 2,622 test-seconds |

These are sums of test time, not wall time. A slow host can make *every* case
on its shard look expensive. Compare the slowest proof job and end-to-end CI
p50/p95 over 20 green main runs that actually execute all eight groups;
report queued time and retries separately from test time.

### Where production releases wait

Five completed GCP releases actually moved traffic: 45183533023, 45324503100,
45457186924, 45465786712 and [45494956262](https://github.com/Lore-Hex/quill-router/actions/runs/45494956262).
Exclude prepared runs that yield to another release. Measured job wall time:

| Phase | Range | Interpretation |
| --- | ---: | --- |
| Image build | 2.0–2.4 min | Already starts alongside CI; usually not the critical path. |
| Schema job | 3.7–5.1 min | Overlaps regional readiness and secret sync. |
| Primary rollout | 28.9–34.6 min | Includes 19.9–25.3 min in the staged traffic step. |
| Secondary rollout | 24.3–24.5 min | Three supervised drains already overlap. |
| Public service rollout | 6.2–7.6 min | Starts after secondary convergence. |

The first optimization to trial is in the public service, not the canary.
Its routed preflight reads the same service twice per region: once for its
serving revision and once for ingress and probe tags. Use one fresh snapshot
per region for those three fields, then fetch the immutable revision. Time
that read-only phase separately; it cannot save the entire public rollout.
An independent next experiment is to fetch those four snapshots concurrently
under the release lock. Before enabling it, test a split-traffic region, a
denied read, and a stale snapshot; no region should be changed on failure.

Keep traffic changes serial on the public service: ingress is service-wide
even for a no-traffic deployment, and the recovery marker tracks one
in-flight promotion. Keep the full primary observation and rollback windows.
For a larger saving on backend-only releases, build the public service as a
separate immutable artifact. First run the proposed skip decision in report-only
mode: compare its digest, public docs/source, runtime configuration and traffic
state in every region, including operator pins. Unknown or different means
the existing rollout. Only exact matches could skip the writes and retain
the public smoke. The ceiling on an eligible release is the 6–8 minute public
job minus verification and whichever parallel finalization gate finishes
last. We have not measured how many releases would qualify.
Measure time to primary serving and time to four-region plus public
convergence separately; users can already be on the new revision while its
predecessor is retained. Don't add overlapping job times to claim savings.

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

Refresh with at least three independent successful runs per profile. A run
whose changes do not reach the post-cutover suite runs no post-cutover shard,
so take post-cutover runs from runs that ran it, or dispatch
post-cutover-daily.yml:

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
completed every production gate. An image always stays bound to its build
commit. A final ancestry check still prevents deploying
a revision removed from main. Migrations, secret synchronization, attestation,
regional/public checks and durable lease finalization remain production-gated.

Cloud release reconciliation recognizes reusable-job names (rollout / JOB)
and still requires all GCP production gates before AWS/Azure promotion.

### Release trains at cloud admission

The shared cloud wait can last hours. Selecting a successor before that wait
left prepared changes behind while an obsolete release took another full turn
(2026-11-12, issue #2551). Main-tracking releases now freeze a train **after**
acquiring the shared cloud reservation, before schema or secret changes.

Each successful build publishes an immutable `prepared-release-ATTEMPT`
artifact containing its full commit, tag, image digest and build run/attempt.
The selector chooses the newest prepared descendant with exact-commit green
CI on main. Unknown/unready successors wait; a failed read of delivery history
refuses selection. Lookups have a four-minute total deadline and inspect at
most 100 recent deploy runs, skipping failed/cancelled builds. The selector
rechecks main ancestry and the selected commit's CI before closing the train.

Workflow and reservation-controller changes form a train boundary. The
initiating and selected revisions must have identical entry/production
workflows, selector and mutex/coordinator scripts. A changed controller ships
in its own workflow; an older controller cannot execute a newer image under
different rollout rules. Explicit pinned manual releases remain pinned.

All migrations, secret synchronization, primary/secondary/public rollouts,
worker and completeness checks use the selected **full** commit and immutable
image together. New commits arriving after selection wait for the next train.
Nothing cancels an active rollout or shortens canaries, retention or rollback
protection. A `selected-release-ATTEMPT` receipt records the controller and
actual selected release. Reconciliation promotes that selected release to
AWS/Azure only after every production gate completes, never the initiating SHA.

Later queued runs detect a fully delivered train and skip already-shipped
work. Selection failures before any mutation release the reservation cleanly
while remaining red. Unexpected mutations or rollout failures still retain
the reservation for recovery. Failed-job retries may reuse an earlier build
artifact from the same run and commit; artifact/run/attempt provenance is
validated and duplicate identities are refused.

This batches control-plane changes; it does not make the shared gateway lease
FIFO or reduce a gateway rollout's own duration. Existing queued workflows keep
their original code until this controller is merged. Measure end-to-end queue
time separately from the train's actual production rollout.

### Automatic price dispatches follow main

The price publisher explicitly dispatches deploy.yml because its GITHUB_TOKEN
push does not trigger another workflow. Previously this took the pinned-manual
path, bypassing both supersession and the post-admission ancestry check. On
2026-11-10 an obsolete price snapshot started another full rollout even though
a newer green release already had a prepared image.

Price dispatches now set `track_main=true`. Both checks apply as on a push;
only a prepared, green, still-on-main successor can displace the snapshot.
The wrapper encodes this boolean and its exact SHA in its run name so successor
discovery can recognize such dispatches without guessing from the actor.
Pinned manual dispatches retain the default `track_main=false` behavior and
are not successors. `track_main=true` cannot be combined with `hotfix=true`.

A snapshot push no longer dispatches a rollout of its own. The snapshot lives
under `src/`, so it rides the next control-plane rollout. The refresh job's
last step runs `scripts/deploy/price_snapshot_deploy.py` on every hourly run
and dispatches deploy.yml, still with `track_main=true`, only once the oldest
undelivered snapshot has waited four hours while no deploy run carries it or
exists for main's head. Prices reach production later; code no longer queues
behind a rollout that ships nothing but prices.

Route holds are the exception (2026-11-24). A hold committed by the refresh
did nothing until a rollout carried it, so a failing route kept serving
customers for three to seven hours. A snapshot whose route history gained a
net endpoint or model removal, or a retirement now in effect, is dispatched
at once, mixed snapshots included. It still dispatches at most once per main
head, only while no deploy.yml run on main is in flight (so hourly holds
coalesce into one rollout rather than queueing several), and only once
main's head has a green CI run of its own; the rollout's CI gate, mutex,
lease order and bake rules are unchanged.

No active rollout is interrupted and no lease is cleared. Cancellation remains
push-only with its existing preparation/test-start/window guards. Workflows
already queued from older source retain their old behavior; this change does
not retroactively rewrite them. CI, image identity, holdback, attestation,
billing, regional/public/worker checks and finalization remain unchanged.

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
the proof oracle's longest shard (26 to 28 minutes), so skipping them frees a
runner rather than shortening the run. Branch protection requires the CI
summary, which needs proofs, so a pull request cannot merge with it red. The
cost that remains: two green pull requests can combine into a main where it is
red, and main's push runs no longer show that. main learns of it from the next
price refresh that runs the full matrix, whose publication it then holds, or
from the daily run, the one run on main scheduled to check it. A schedule is not a guarantee: since October, this
repository's daily schedules have started three to seven hours late (the first
daily run, due at 04:47 UTC on November 7, started at 11:21), and under load
GitHub may skip one. With coverage cheaper, the default suite runs in eight
shards instead of twelve. On #2377's run they took 8 to 14 minutes each, still
inside that longest shard, with four fewer jobs in the queue and 91
runner-minutes against 110 to 126.

### Heavy jobs run when what they read changes (2026-11-16)

This replaces the push skip and the daily run above. The TLA+ model checks,
the proof oracle, the post-cutover suite and the three fast path jobs carry a
flag each, which ci.yml's scope job computes with scripts/ci/commit_scope.py
from the changed paths, on pull requests, pushes and dispatches alike. Each
job runs only when a file its flag names changed: for a pull request since its
base, for a push or dispatch since the newest ancestor-or-self of its base
whose ci.yml run passed. The docstring of commit_scope.py lists each flag's
files and why. When only Python outside src/ (tests, helpers, scripts)
changed otherwise, the post-cutover suite runs in one shard on the test files
that change can reach: each changed test, the tests that import a changed
module directly or through others, and the tests that name it. A test is
code that can depend on the catalog without naming anything scheduled. A
changed Python file that reaches no test, a Python line naming the lifecycle,
and a changed data file a test could load expectations from (JSON, YAML,
TOML, CSV, text and the like) run the whole suite; other code and
configuration run it when a changed line names the lifecycle. The
post-cutover suite runs on pull requests and dispatches, not on a push to
main: its red means a future date will break, and the pull request already
ran it.
post-cutover-daily.yml has no schedule any more; it runs by hand and is the
one place the oracle runs at the post-cutover clock, since no endpoint the
oracle settles changes between the clocks in either leg
(tests/test_async_settle_proof_oracle.py holds that for the live and the
frozen provider_lifecycle, and 666 pairs of default and post-cutover shards
over 31 hours never disagreed). The deploy gates count only a push or
dispatch run of ci.yml for their commit, never a pull_request run. A dispatch without
scope=paths and a pull request labeled ci:full run the complete matrix. The
scope job reads the label through the API when it runs, and adding a label
starts no run (a `labeled` trigger would cancel the pull request's run on any
label): add it, then push or re-run every job of the latest run with
`gh run rerun <run-id>`.

Measured over 31 hours before the change, CI used about 56,600 job-minutes a
day. Replaying the rules over the last 100 merged pull requests, the oracle
would have run for 40, the post-cutover suite for 65 whole and 22 more in one
shard, the fast path jobs for 34 and the model checks for 4; over 147 push
runs, with the comparison widened past bases that had not yet passed (98 of
them), the oracle for 77%, the fast path jobs for 47% and the model checks
for 13% (a push runs no post-cutover suite). With the measured per-job
minutes that is about 22,000 job-minutes a day fewer (39%): 8,600 from the
oracle's post-cutover clock on pull requests, 5,100 from its default clock on
pull requests, 2,800 from the post-cutover suite, and 1,300 and 1,600 from
the model checks and the fast path jobs on pull requests. These are estimates
from one window's cadence, not a measurement of the new workflow.

## Validation and measurement

Normal releases have three bounded shortcuts:

* Selection reuses the previous main-push selector's immutable receipt. A run
  ID, attempt, commit or update timestamp mismatch triggers fresh job reads.
  Missing evidence, a changed verifier, or changed main ancestry causes a cold
  scan. The log separates total historical rows from reused rows.
* The typed-counter and fast-path migrations batch named table/column checks
  against live Spanner metadata. The snapshot lasts one script invocation and
  is invalidated before any DDL. Index readiness, timestamp options, backfills
  and fresh reads on a miss still run.
* For changes confined to reviewed standalone worker entry points, the public
  site can retain its last smoke-tested image for up to one day. The four
  serving revisions, public invoker permissions, configuration, secret version
  metadata, runtime layers and live API release must match the evidence.
  Anything unknown uses the ordinary public rollout. The public edge smoke
  always runs. Only the exact control-plane evidence URL follows the API; the
  gateway attestation documents still follow the trust/public surface.

The first eligible release seeds the receipts. Do not infer a production
speedup from a cold run or mix worker-only releases with full website releases.

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
