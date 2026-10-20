# P6 first-window activation — round 2

This package prepares and records the first-window actions in §15.8. It does
not establish a production PASS. The example plan is a review worksheet: its
zero SHAs and every REVIEW entry must be replaced and independently reviewed.
The dates, ceilings and blocked-day target in that worksheet are proposed
inputs, not measurements or approved capacity. No native trial, release or
probe was run while authoring it.

## Commands and records

Use the worktree's Python. `release`, `probe` and `trial` are also available
through `scripts.async_settle.shadow_v2_window`. All outputs are new directories;
existing output, including BLOCKED results, is never overwritten. Changed
originals or plans require a new review and independent digest.

| Command | Record / done means | Credential |
|---|---|---|
| `shadow_v2_activation validate-plan --plan PLAN` | Closed schema and finite arithmetic checked; REVIEW is still unapproved | None |
| `shadow_v2_window release --plan PLAN --phase shadow-opt-in --out OUT [--enclave-root DIR]` | Router/enclave patches, exact pins and `gh pr create` commands ready for review; production is unchanged | None |
| Same, `--phase shadow-opt-out` | Cohort/policy removed, enclave shadow off; attachment destination retained for close/export/retention | None |
| Same, `--wait --plan-sha256 DIGEST --polls N` | Read existing exact-SHA workflows and jobs; retain originals. Pending, failed or incomplete checks remain incomplete | GitHub Actions read; platform read for supplied original sources |
| `shadow_v2_window probe --plan PLAN --phase warmup\|window\|stop --out OUT` | Reviewable Scheduler commands, no process executed | None |
| Same, `--apply --plan-sha256 DIGEST` | Existing dedicated job matches released image/command/reference/account, one task and parallelism one; schedule create/update/resume or pause observed | Existing gcloud identity and existing job's compute service account |
| `shadow_v2_activation native --plan PLAN --plan-sha256 DIGEST --out OUT` | Native responses retained through bounded Pass/Query; delivery completeness requires independent source record | GitHub Actions read; Monitoring/logs/registry read |
| `shadow_v2_window trial --plan PLAN --plan-sha256 DIGEST --out OUT` | Separate isolated-resource approval checked; **P6NativeQualificationHarness remains incomplete** | None for approval check; isolated trial identity blocked on harness |
| Existing `collect`, `review-inputs`, `pack` | Load the digest-bound C2 recipe from `artifacts.bundle_plan`, then invoke existing implementations | Offline for captured inputs |
| Existing `shadow_report --policy shadow-v2 ...` | Existing gate result with independent approved bundle digest | None |

The activation plan and C2 evidence recipe are separate records. The activation
plan never embeds an open-ended recipe object: its closed schema contains a
path/digest reference. The existing recipe and source parsers retain their own
validation and whole-policy requirements. `native` retains original pages and
indexes; it does not invent the C1 source mappings or change a captured recipe.
The reviewed mapping of those originals into the C2 recipe is still required.

`release` prepares code changes, then observes the normal workflow after the
operator's merge. It does not dispatch a workflow. This follows the P6 assignment
where §15.8's earlier wording says “dispatches.” No other wording in the governing
design was changed except the requested A3-R4 Inventory sentence.

## Closed plan

`release/first-window-plan.schema.json` is generated from
`async_settle_shadow_v2_activation_plan.Plan`; every object forbids extra keys.
The loader also rejects duplicate JSON keys, coercions, out-of-root paths,
invalid dates/order, mismatched cap/surge/K totals, excessive launch overlap,
short windows/horizons and excessive pass/call/byte limits. A live command
requires `--plan-sha256` from the independent reviewer, `review=approved`, real
full release SHAs, images and workflow run IDs, and no remaining REVIEW values.
A digest of one's own draft is not a review.

| Section | Contents |
|---|---|
| releases | Full router/enclave SHAs, digest-pinned images, repository and pin source file |
| workspace / attachment_bucket / enclave_regions | Exactly workspace `162e441a-5d13-414f-92bd-7c6cd805faa3`, retained attachment destination and full enclave inventory |
| schedule | N=1, configured regional caps/surge/publishers, fleet totals/K, finite launch/end/churn records, trial/warm-up/window/horizon dates, ≥30-day retention |
| limits / passes | ≤100 records/page, ≤10,000 records/pass, ≤10-second calls, zero retries, ≤256 KiB page; finite total passes, calls, output bytes, trial ≤1 hour, measured CPU allowance and blocked-day target reference |
| workflows | Required CI/attestation/rollout/health, reviewed workflow filenames, selected run IDs and exact required job names; CI/rollout/health for both releases and enclave attestation |
| probe | Existing dedicated jobs/scheduler names, cadence, repetitions, finite invocation/request/spend allowances, existing compute service account, Secret Manager **name only** |
| sources / archives | Exact resource, UTC interval, parser and URL/filter; independent completeness-record reference; exact archive/run/release/digest/byte bounds |
| checkpoint / artifacts / trial | Gate stage, new output paths, C2 recipe reference, isolated resource/approval/measurement recipe and named harness |

The worksheet uses caps 12 × four router regions = 48, surge 48, primary K=24.
These come from the checkout's declared per-revision ceiling and proposed
overlap; they must be compared with the released serving configuration before
approval. N=1 nominal observer reads are 1.25/router/s and pending rows are
250.25/router/s: at 96 configured simultaneous routers the projections are
120 reads/s and 24,024 pending rows/s. Actual measurements may increase them.
The proposed probe allowance is 6,000 invocations, 200,000 requests and
100,000,000 microdollars; the example cadence/intervals schedule at most 5,730
invocations before the required stop. These are draft allowances, not funding
or actual spend records. The 532 explicit launches describe the proposed baseline and overlap/churn
population; they do not assert that production has those lifetimes. The bundle
owner must consume/compare this schedule before removing its finite-window seam.

## Exact phase pins

| Pin | shadow-opt-in | shadow-opt-out |
|---|---|---|
| Router `TR_ASYNC_SETTLE_SHADOW_POLICY` | `shadow-v2` | empty |
| Router `TR_ASYNC_SETTLE_SHADOW_WORKSPACES` | `162e441a-5d13-414f-92bd-7c6cd805faa3` | empty |
| Router `TR_ASYNC_SETTLE_SHADOW_HEALTH_REGION` | `us-central1` | empty |
| Router `TR_ASYNC_SETTLE_SHADOW_ATTACHMENT_BUCKET` | `tr-async-settle-shadow-attachments-quill-cloud-proxy` | Same retained bucket |
| Router `TR_ASYNC_SETTLE_ENABLED` | `false` | `false` |
| Router `TR_ASYNC_SETTLE_PROTECTION` | `false` | `false` |
| Router `TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED` | `false` | `false` |
| Enclave `TR_ASYNC_SETTLE_SHADOW` | `on` | `off` |
| Enclave `TR_ASYNC_SETTLE_NEGOTIATE` | `off` | `off` |

Opt-out records must also prove actual close/result/export and retained losses;
a pin patch alone does not prove closure. Never clear the attachment destination
before the retained export and retention work completes.

## First live dry-run command list

```sh
PY=.venv/bin/python
PLAN=release/first-window-plan.json
OUT=/tmp/p6-first-live-dry-run
$PY -m scripts.async_settle.shadow_v2_activation validate-plan --plan "$PLAN"
$PY -m scripts.provision_async_settle_shadow_probe_workspace
$PY -m scripts.async_settle.shadow_v2_window release --plan "$PLAN" \
  --phase shadow-opt-in --out "$OUT/opt-in" --enclave-root /path/to/reviewed-enclave-checkout
$PY -m scripts.async_settle.shadow_v2_window release --plan "$PLAN" \
  --phase shadow-opt-out --out "$OUT/opt-out" --enclave-root /path/to/reviewed-enclave-checkout
$PY -m scripts.async_settle.shadow_v2_window probe --plan "$PLAN" --phase warmup --out "$OUT/warmup"
$PY -m scripts.async_settle.shadow_v2_window probe --plan "$PLAN" --phase window --out "$OUT/window"
$PY -m scripts.async_settle.shadow_v2_window probe --plan "$PLAN" --phase stop --out "$OUT/stop"
```

The provisioning command is its existing read-only default and needs its normal
platform credentials. Other commands above are offline. The enclave path is an
operator-supplied checkout, not a production path. No `gh pr create` command is
executed by this dry-run. The operator applies the emitted patches through normal
PRs, CI and release; after merge, the plan must name the **new merged releases**
and exact workflow runs, followed by a fresh independent approval digest.

## §15.8 ordered operator checklist

| Step and exact command | Done means | Credential |
|---|---|---|
| 1. §9.1–2, exact release CI and original dormant records | Complete dormant rollout/attestation and four money-off pins; no missing producer; old BLOCKED/reset records retained | Release/platform read and GitHub read |
| 2. Existing provisioning command above; existing cohort check if needed | ONE typed local Credits workspace; separately approved funding/key delivery; both routes/variants, provider/candidate/fallback choices reviewed | Existing provisioning identity; no values in plan/report |
| 3. `shadow_v2_activation validate-plan --plan "$PLAN"` and independent review | Finite resources/dates/launch/cost/retention approved; boot source witnesses complete; measured blocked-day target | Read access for source checks; approval offline |
| 4. `shadow_v2_window trial --plan "$PLAN" --plan-sha256 "$PLAN_SHA" --out "$DIR/trial"`; then collect/review/pack pre | Actual isolated writer/transport/CPU/shutdown originals, approved linked pre/post, six boot mechanisms and `fleet-budgets-pre` PASS. **Blocked on harness seam** | Isolated trial writer identity (runner missing); native/platform read; approval and gate offline |
| 5. `release --phase shadow-opt-in` prepare; operator PR/merge; same command `--wait --plan-sha256 "$PLAN_SHA"` with new output | Completed CI/attestation/rollout/health and effective pins in every attested region. **Blocked on live-pin/attestation seams** | Operator PR/merge identity; tool uses GitHub and release/platform read |
| 6. `probe --phase warmup`, review; repeat `--apply --plan-sha256 "$PLAN_SHA"`; pause with `probe --phase stop --apply --plan-sha256 "$PLAN_SHA"` at warm-up end | Both routes/variants/all enclave paths exercised; actual booking regions known, T=A and B=C=0; writers truly closed before collection | Scheduler/job identity; platform read |
| 7. `collect --stage warmup`, `review-inputs`, human review, `pack`; transport then fleet-budgets | Both PASS, same approved pre, full pre and prerequisite graphs retained before seed | Collection read; review/gates offline |
| 8. `probe --phase window`, then approved `--apply`; daily collect/review/pack | Fresh durable seed strictly after prerequisites; real booking-region seeds; new boot ledgers without resetting original seed | Scheduler and collection read |
| 9. On BLOCKED retain results; `probe --phase stop --apply`; reviewed `release --phase shadow-opt-out` when required | No erased loss; actual close/export retained; repair/prerequisites before a fresh positive/window | Scheduler/release; diagnosis offline |
| 10. Stop at approved window end; collect through each forward horizon; final review/pack and shadow-serving | ≥604800 continuous seconds, complete frozen graph/report PASS, ≥30-day retention/cleanup and production health verified | Final read; review/gate offline |
| 11. Keep money flags off and archive plan, approvals, outputs and originals | Shadow window only; N=2, excluded traffic and money pilots remain separate work | None |

Commands abbreviated in the table use the same module, plan and unique `--out`
arguments as above. Existing bundle commands are:

```sh
$PY -m scripts.async_settle.shadow_v2_window collect --plan "$PLAN" --stage pre --out "$DIR/pre"
$PY -m scripts.async_settle.shadow_v2_window review-inputs --plan "$PLAN" --input "$DIR/pre"
$PY -m scripts.async_settle.shadow_v2_window pack --plan "$PLAN" --input "$DIR/pre" --out "$DIR/pre-approved"
v2_gate() {
  $PY -m scripts.async_settle.shadow_report --policy shadow-v2 --gate "$1" \
    --bundle "$2/bundle.json" --output-dir "$2/gate-$1" \
    --release-plan "$2/loading/release-plan.json" --release-plan-sha256 "$V2_APPROVED_SHA" \
    --binding-keys "$2/loading/binding-public.json" --certificate-keys "$2/loading/certificate-public.json"
}
v2_gate fleet-budgets-pre "$DIR/pre-approved"
# Repeat collect/review/pack with stage warmup and new paths, then:
v2_gate transport "$DIR/warmup-approved"
v2_gate fleet-budgets "$DIR/warmup-approved"
# Daily and final window packs retain the complete prerequisite graphs:
v2_gate shadow-serving "$DIR/window-approved"
```

`V2_APPROVED_SHA` is the independent bundle review digest; `PLAN_SHA` approves
the activation plan. They are different records and are never interchangeable.
The tool cannot close a writer or turn an EOF into delivery completeness.

## Approval records and remaining seams

| Record | Required author and contents | Current record |
|---|---|---|
| Activation plan | Joseph/delegate; exact bytes/digest, releases/resources, finite costs and dates, source bindings, stop time and retention | REVIEW; no approval supplied |
| Trial | Separate approval for isolated resource, ≤1-hour interval, native work and cost; actual results and measured target | REVIEW; no executed trial |
| Pre / warm-up / each window pack | Existing APPROVAL/REVIEW_RECORD plus independently communicated release-plan digest, exact originals and predecessor graph | Not supplied |
| Release | Operator PR/merge and exact CI/attestation/rollout/health originals; observed pins/regions | Preparation only |
| Stop / rollback / archive | Actual pauses, close/results/export, retained losses and cleanup/retention verification | Not executed |

P6NativeQualificationHarness: the existing `pack_trial` functions and
`CapturedBudget.build` consume and compare already-produced records. There is
no native qualification runner entry point to call in this checkout. `trial`
reports this missing producer before starting native work; window source reads
are not trial measurements, and fixtures never stand in for a completed trial.

P6EnclavePinSource: provide the reviewed enclave checkout and exact pin-file
shape to generate its patch (quoted assignments or existing GCP `tee-env-` metadata). P6LivePinProjection: router `/health` is only
`{"status":"ok"}`; effective per-revision pins require an additional existing
read-only deployment projection and reviewed parser. P6AttestedRegionVerification:
the local trust-drift tool explicitly does not perform full attestation checking;
the canonical `tools/verify-attestation.py` exists in the enclave repository,
but its live path has a 40-second socket allowance and no P6 Pass/original-call
accounting integration. A reviewed release-pinned adapter must preserve the
ten-second bound and retain its inputs/results. Original trust/registry and
regional attestation records are retained, but release cannot report complete
without the missing verifier/projection integration.

P6SourceDeliveryBinding: A3-C1 must supply the reviewed independent process
source, kill-before-startup/restart witnesses and delivery boundaries. Aggregate
Monitoring and surviving boot records never become the process roster.
P6NativeRecipeMapping: the operator/C1/C2 owner must bind native original indexes
to the captured recipe; P6 does not fabricate that mapping. A3-C2 retains its
P2-02/P2-03/P2-04 generator/allocation/matrix work. A3-E/gates retains the supplied
review's frozen capacity, multi-bundle and loading limitations. These block
activation even when the P6 action commands work.

P6ProbeImageBinding (P6 / reviewed deployment owner): the existing deploy workflow
passes a short-SHA image tag to `_async_settle_shadow_probe_jobs.sh`, while the
probe preflight requires the plan's exact digest. A tagged job is refused before
Scheduler changes. The reviewed release path must establish a digest-pinned job
or supply an equivalent retained platform binding; P6 does not update the job.
A registry lookup alone cannot establish the image for future executions:
[Cloud Run job tags are resolved at execution](https://codelabs.developers.google.com/codelabs/codelabs/cloud-run-jobs#9).

Scheduler jobs recur; `stop` at the reviewed end is an explicit operator step,
not an automatic task performed by this tool. The job's existing cadence and
repetition configuration must agree with the phase plan. [Cloud Scheduler's
five-field schedule](https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules)
contains no year or end timestamp. Native readers follow the official
[Monitoring time-series interface](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list)
and [GitHub artifact metadata/download interface](https://docs.github.com/en/rest/actions/artifacts).

## Round 2 record checks

The attachment destination is fixed in the package; the plan repeats that value.
K equals the primary region's configured cap plus surge, with publishers agreeing
with that complete eligible population. This plan has no exclusion source.
Pin sources and default output parents resolve beneath their supplied root;
archive output paths use the same check.

Native passes charge the requested page allowance and call before each read,
including rejected pages. `native-budget.json` records reserved and accepted
records separately, with exact remaining call and record allowances. A response
whose count cannot be established within the requested allowance stops the pass.
A bounded page with a mismatched release still consumes its allowance. GitHub
job/artifact pages require distinct ids and a constant total through completion;
the release observer applies the same population check to its job pages.
