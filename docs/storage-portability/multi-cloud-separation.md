# Each cloud is a separate TrustedRouter

**Status:** decided 2026-07-27. **Supersedes** the 2026-07-26 decision recorded
in `README.md` that "Spanner stays the system of record even when compute runs
on AWS/Azure."

---

## 1. Decision

A TrustedRouter deployment on AWS is a **standalone deployment**: its own
database, its own credits, its own API keys, its own analytics. Same for Azure.
They are not front-ends onto a GCP database.

Exactly one thing crosses cloud boundaries: **identity**. Everything else is
cloud-local.

| | Federates across clouds | Cloud-local |
|---|---|---|
| Who you are (email, OAuth subject, verified identity) | ✅ | |
| Credit balance | | ✅ |
| API keys | | ✅ |
| Workspaces, members, roles | | ✅ |
| Usage history, generations, analytics | | ✅ |
| Provider routing + catalog | | ✅ |

An API key issued on AWS authenticates only on AWS. Credit bought on GCP is
spendable only on GCP.

### Cloud-local security migrations

Schema and cryptographic-envelope migrations repeat independently in every
cloud. A successful GCP migration says nothing about the AWS or Azure database
or enclave. The canonical BYOK AAD v2 procedure and completion evidence live in
[`docs/design/byok-aad-v2-migration.md`](../design/byok-aad-v2-migration.md).

As of 2026-08-15, GCP, AWS, and Azure are complete through that plan's step 3:

| Cloud | v1/v2 readers | v2 writers | Step 3 result |
|---|---|---|---|
| GCP | deployed | deployed | 7 BYOK envelopes migrated; 0 v1 remain |
| AWS | deployed | deployed | 0 eligible rows; no write required |
| Azure | deployed | deployed | 0 eligible rows; no write required |

Step 4 is not complete. V1 read support remains in every cloud until the
retention-window condition in the migration plan is satisfied.

---

## 2. Why identity federates and money does not

This asymmetry looks arbitrary. It isn't — the two things are different kinds
of object.

**Identity is an assertion.** "This is joseph@example.com, signed by an issuer
you trust." Verifying it requires a public key and nothing else. Every cloud
can verify the same assertion independently, concurrently, while partitioned
from every other cloud, and they cannot disagree — because there is no shared
mutable state to disagree about.

**A credit balance is a mutable quantity with a conservation law.** If two
clouds can both spend from one balance, they must agree on ordering or the
balance is spent twice. There are only three ways to get that agreement:

1. **One authoritative store.** Then the clouds are not separate: every AWS
   request round-trips to GCP, GCP's outages become AWS's outages, and the
   cross-cloud hop lands on the *hot path* of authorize+settle. This is
   precisely the design being superseded.
2. **Cross-cloud consensus (2PC or similar).** Availability becomes the AND of
   both clouds — strictly worse than either alone. Latency becomes the max.
   Failure modes become the union. You pay for multi-cloud and receive less
   reliability than single-cloud.
3. **Partition the money.** Each cloud owns a disjoint slice. No coordination
   on the hot path. Correct under partition, because there is nothing to
   coordinate.

Only (3) preserves the reason for having separate clouds at all.

The analogy is exact: one passport, separate accounts at separate institutions.
Nobody expects a balance at one bank to be spendable at another because it is
the same person.

### If credit must move between clouds

It moves as an **explicit, asynchronous, idempotent transfer** with a
settlement record on both sides — debit here, credit there, reconciled, with a
visible in-flight state. Never a shared balance, never a synchronous
cross-cloud debit. This is the same shape as the settle outbox already in the
codebase (`storage_gcp_settle_outbox.py`), and it should reuse that thinking.

Not in scope now. Recorded so that "just let credits work everywhere" is
recognised as a request for a money-movement feature, not a config change.

---

## 3. The product consequence, which is not optional

**Separateness must be visible.** A user who tops up on GCP and then gets a 402
from their AWS key will file a support ticket every single time unless:

* the console shows balances **per cloud**, never one merged number;
* the 402 body names the cloud and the balance it checked;
* API keys are visibly scoped to the cloud that issued them.

A hidden partition is a permanent support tax. An explicit one is a feature —
it is what data residency and cloud sovereignty actually mean, and it is
sellable.

---

## 4. What this changes in the plan

**Promoted to critical path.** The `Store` protocol and the behavioural
conformance suite (`tests/conformance/`, PR #288) were a nice refactor when
Spanner was universal. Now they are the mechanism: a new cloud is a new `Store`
implementation that passes the conformance suite. Everything the suite does not
pin is a place where two clouds can silently diverge in behaviour — and one of
those behaviours is money.

Known gap, now urgent: `InMemoryStore` does not implement `TypedBillingStore`
and Spanner's legacy `reserve()` raises, so the two existing backends run
genuinely different money code and nothing pins which is right. A third and
fourth backend make that unacceptable.

**Demoted.** The cross-cloud Workload Identity Federation path (this branch)
was built to let AWS compute reach GCP Spanner. Under separation, no data path
needs it. It is kept because it is keyless, narrowly scoped, and still the
right way to do cross-cloud *operations* (bootstrap, image pull, ops tooling)
without a long-lived key — but nothing new should be built on top of it.

**Unchanged.** ClickHouse as the analytics backend, one per cloud — that is
already how it is built (self-hosted node, no managed dependency). Per-cloud
attestation (Confidential Space / Nitro / SEV-SNP) matters *more*, since each
cloud is now a standalone product surface rather than a failover target.

---

## 5. Database per cloud

The system of record needs: strong consistency, distributed transactions
spanning a workspace's rows, conditional writes for the credit invariants, and
secondary-index lookups. That is what Spanner gives us today.

| Cloud | System of record | Why |
|---|---|---|
| GCP | Spanner *(today)* | — |
| AWS | **Aurora DSQL** | The genuine Spanner analogue: distributed SQL, strong consistency, active-active, no failover step. Keeps the reserve/settle logic recognisably the same code rather than a rewrite. |
| Azure | **Cosmos DB for PostgreSQL** (Citus) | Distributed Postgres with transactions inside a distribution key. Our money code is per-workspace, so distributing by workspace id puts every transaction on one node. |

Rejected: **DynamoDB** as the system of record. It is the Bigtable analogue,
not the Spanner analogue — `TransactWriteItems` caps at 100 items and there is
no cross-partition SQL. It would force the billing logic to be rewritten in a
different idiom, which is the one thing worth avoiding when the logic in
question is money. Reasonable for the *operational* half (newest-N generation
lookups) if that is ever split out.

Rejected for Azure: **Cosmos DB (SQL API)** — transactional batches are limited
to a single logical partition, which is weaker than what reserve/settle needs.

**These are recommendations, not conclusions.** Each is validated the same way:
implement the `Store`, run the conformance suite, and only then argue about it.

### The unlock: both are Postgres

Aurora DSQL and Cosmos DB for PostgreSQL both speak the Postgres wire protocol.
So this is **not two backends — it is one**.

Write a single `PostgresStore`. Develop and test it against plain Postgres in a
container, which costs nothing, runs in CI as a service container, and gives the
conformance suite a *third real backend* to pin behaviour against — the thing
that catches divergence, including in the money code. Then deploy that same
implementation on Aurora DSQL for AWS and on Citus for Azure.

This turns "port the hardest code in the system, twice, against two cloud
databases we cannot run locally" into "write one SQL backend, test it locally,
deploy it twice." It is the difference between a tractable project and a
research project, and it is the reason to prefer these two managed services
over their more obvious cloud-native alternatives.

Where they diverge from stock Postgres — DSQL's optimistic concurrency and its
restrictions on some DDL, Citus requiring a distribution column on every
distributed table — those differences are narrow, known up front, and are the
first thing the conformance suite should be pointed at once a cluster exists.

---

## 6. Open questions

1. **Which issuer federates identity?** Today OAuth is Google and GitHub
   directly (`oauth_provider.py`). Under separation each cloud can verify those
   assertions independently with no shared state — which is the cheap and
   correct answer. A TrustedRouter-owned issuer would be needed only for
   email/password accounts, which need a home.
2. **Does a single account see all its clouds?** Listing "GCP: $X, AWS: $Y"
   requires reading N clouds from one console. That is a read-only fan-out and
   is safe; it must not become a write path.
3. **What is the canonical hostname per cloud?** `aws.trustedrouter.com` vs a
   region-style selector. Affects key scoping and the 402 message.
4. **Signup credit per cloud?** Granting the trial credit once per cloud
   multiplies the giveaway by the number of clouds.

---

## 7. Adding a cloud: the definition of done

> **A cloud is not in service until rows are observed moving through its
> analytics pipeline.** Not provisioned, not deployed, not "green on the status
> page" — moving.

This section exists because the previous, unwritten definition was "the
bring-up scripts finished", and on 2026-08-02 that definition was satisfied by
an AWS-EU cloud with no analytics pipeline at all. `aws_eu_clickhouse.sh` built
the Paris node and ended by printing

    Next: apply clickhouse/*.sql, then redeploy tr-eu with ...

A human ran it, read the echoes, and stopped. For fifteen days settle enqueued
operational rows into the DSQL outbox that nothing collected — 470,897 of them
by 2026-08-17, with `activity_generations` on the node still empty — and no
alarm fired, because the backlog alarm is emitted BY the drain that was never
installed. The pipeline is

    settle -> tr_operational_analytics_outbox (this cloud's OLTP db)
           -> drain -> this cloud's ClickHouse

and every stage after the first is invisible from outside the VPC, so "the
control plane is up" says nothing about any of them.

### The stages, in order

A cloud is done when `scripts/deploy/verify_cloud_complete.sh <cloud>` exits 0.
It needs no credentials — one public HTTPS GET and a text read of a deploy
script — so it can be run from a laptop, by a reviewer, at any time:

| # | Stage | Fails when |
|---|---|---|
| a | in the fleet freshness registry | nobody, on any schedule, reads this cloud's drain lag |
| b | `/status.json` carries the `analytics` section | the cloud publishes no answer to the question |
| c | `analytics.available` is true | the control plane cannot read its own outbox (**not** the same as an empty one) |
| d | `drain_lag_seconds` under the bound | rows are enqueued and nothing is deleting them — the AWS-EU shape exactly |
| e | the control plane's outbox is ENABLED | nothing is enqueued at all, so (c) and (d) pass over an empty pipe |

Stage (e) is not redundant with (d) and this is the subtle part: **a drained
outbox and a disabled outbox look identical from outside.** Both publish
`drain_lag_seconds: 0.0`. Stage (d) proves nothing is stuck; only (e) plus an
in-cloud count proves anything moves.

Stage (e) is also the one stage that reads a FILE rather than the cloud: it
parses that cloud's control-plane deploy script **in the working tree you run it
from**, because a check that needs `aws`/`az` credentials is a check that does
not get run. So it answers "would a deploy from this checkout enable the
outbox?", not "did the running service have it enabled?" — a local edit reads as
enabled, and a script that shipped a year ago reads the same as one deployed
this morning. The runtime evidence is (b)–(d), and the verifier prints exactly
what it read, and from which file, as a note under its outcome.

When you finish a cloud, look once, from inside:

    clickhouse-client --query 'SELECT count() FROM activity_generations'

and then again ten minutes later. Two numbers, the second larger. That is the
observation the rule above is named after; nothing in a status page substitutes
for it.

### The analytics stage is not optional (with one named exception)

Bring-up is not a menu. The AWS and Azure bring-up and control-plane deploy
scripts end by running the check and exit non-zero when it fails —
`aws_eu_clickhouse.sh`, `aws_eu_north_clickhouse.sh`, `aws_eu_control_plane.sh`,
`aws_eu_clickhouse_drain_install.sh`, `azure_control_plane.sh`. Where a
remaining step genuinely needs a human (a cost decision, a password that only
exists on a node), the script prints the exact command **and exits non-zero**.
It never prints and returns 0; that behaviour is the outage.

That list is not typed here twice: it is `deploy_scripts` on each `CloudRollout`
in `src/trusted_router/cloud_rollout_completeness.py`.

### How "this script runs the gate" is established — and where it is only claimed

This is worth reading before trusting the paragraph above, because the first
version of that binding was a lie of exactly the kind this whole section is
about. It was a regex: *does the string `verify_cloud_complete.sh <cloud>`
appear in the script's last N lines?* A heredoc body satisfies that. So does a
printed instruction. So does a commented-out line. The check that was written to
stop "printing the step counts as doing the step" was itself satisfied by
printing the step.

So the binding is now behavioural. `tests/test_deploy_script_execution.py` RUNS
each bound script to completion in a harness (`tests/deploy_script_harness.py`)
whose `PATH` is one directory: a recording stub for each cloud CLI, `curl`,
`ssh`, `systemctl`, `sleep` and the rest of a named list, plus a symlink to
every other entry of `/bin` and `/usr/bin`. `$HOME` and `$TMPDIR` are inside a
temp directory and the repository the scripts see is a copy. The isolation is by
NAME, and its boundary is that list — a script that reached the network through
some tool nobody thought to stub would reach it. What the harness guarantees is
what the assertions need: the gate is a recording stub that can be told to fail,
and every cloud CLI is a stub. It asserts three things about what each script
DID:

1. it called the gate, for its own cloud;
2. with the gate failing, the script exits non-zero;
3. it issues no further cloud CLI calls after the gate answered (the measured
   form of "the check has to be the last thing it does").

It also asserts that both of the gate's exit codes come out the far end
unchanged — and, for `aws_eu_north_clickhouse.sh`, that they do so for an
operator who has NOT set `TR_STOCKHOLM_REPLICA_WIRED`. That script ends by
refusing to claim the Stockholm replica is wired until somebody attests to it,
and that refusal used to overwrite the gate's status: on a first run, when
nobody has ever set the variable, a gate exit of 5 and a gate exit of 1 both
came out as 3. The propagation held only for the harness fixture, which supplies
the variable so the script can reach its own end.

**Proven by execution today:** `aws_eu_clickhouse.sh`,
`aws_eu_control_plane.sh`, `aws_eu_north_clickhouse.sh`,
`azure_control_plane.sh`.

**Not proven, and therefore only CLAIMED:**
`aws_eu_clickhouse_drain_install.sh`. Its middle ships a tarball to the node in
base64 chunks over SSM and then reads the drain's own journal back to establish
that rows moved; a stub SSM that answers `Status=Success` to everything would
make that verification an assertion about the stub. It is recorded as
`NOT_PROVEN` in `ROLLOUT_REGISTRY` with that reason, and a `NOT_PROVEN` entry
with a blank reason fails CI. What *is* proven about it is that the shared
fragment it calls — `scripts/deploy/cloud_complete_gate.sh` — returns the gate's
exit status unaltered for every code the gate can produce. What is not proven is
that a real run reaches that call.

A smaller true claim beats a larger false one; that is the whole thesis here, so
it applies to this section too.

**GCP is the exception, and it is named in code.** `rollout.sh` is not a script
a human runs; it is a step of the deploy job in `.github/workflows/deploy.yml`,
which runs on every merge to `main`. Ending *it* in this check would put a public
fetch of `trustedrouter.com/status.json` in the middle of deploying the cloud
that *serves* `trustedrouter.com` — the deploy that repairs an outage would
abort partway, because of the outage it repairs. So GCP carries a
`ScriptExemption` with that reason in `ROLLOUT_REGISTRY`. That is a statement
about which SCRIPTS end in the gate. It is not permission for GCP to skip a
stage; no such permission exists.

That exemption used to cite "the scheduled analytics freshness workflow" as what
checks GCP instead. That workflow ships with **no `schedule:` trigger**, on
purpose and in its own header — so the citation was to a control that does not
run, and the primary cloud had no automated completeness check at all behind a
sentence saying it did. The control is now the `verify-cloud-complete` job in
`.github/workflows/deploy.yml`. The exemption references that workflow and job
as structured data, and CI resolves the reference — an exemption citing a job
that is not there fails.

That job runs `if: always()` on `needs: [deploy]`, which is load-bearing and was
missing: with a bare `needs:`, GitHub SKIPS a job when its dependency fails, and
a deploy that failed PARTWAY has already mutated production. The check would
have been absent from exactly the runs where it mattered, behind a comment
saying it ran after every production mutation. It is skipped only when the
deploy job itself was skipped, i.e. when nothing was deployed.

Honest caveat, because this is a control that has never fired: it lands with
this change and has not yet run on a merge.

### There is no exemption

Earlier revisions of this design had one: `analytics_absent_reason` on a cloud's
entry in `src/trusted_router/cloud_rollout_completeness.py` waived the
"structural" blockers, a verdict taxonomy decided which failures counted as
structural, and a waived run printed `NOT VERIFIED` and exited 6.

It is gone, machinery and all. Two rounds of review found bugs inside it rather
than around it — one where the waiver path could not produce its own verdict at
all and failed as "the gate could not classify its own output", one where the
green banner was reachable on evidence this document says can never earn it —
and the second set were regressions introduced by the fixes for the first. A
mechanism whose failures upgrade a verdict has to earn its keep. This one did
not.

So: a cloud that cannot be checked is NOT VERIFIED. The run exits non-zero and
prints the reason. To run a cloud without an analytics pipeline, run it and let
the check say so; nothing in this repository will call it done.

### Exit codes

`scripts/deploy/cloud_complete_gate.sh` maps these to the same words for every
bound script, so an operator is never told to fix an install that did not fail:

| code | meaning |
|---|---|
| 0 | `VERIFIED` — every stage was measured and held |
| 5 | `NOT YET OBSERVABLE` — the page parses and carries no `analytics` section |
| 1 | `NOT VERIFIED` — everything else, with the reason printed: a stage failed, the page did not answer 200, the body was not the status document, the cloud is unknown, the arguments were wrong |

There were seven. The rest collapsed into 1, which says why in words rather than
in a number nobody looks up. 5 survives because it is the one non-zero answer an
operator must not read as "your install failed": it is the state every cloud is
in until a control plane that publishes the section is deployed, and the run
that INSTALLS a drain hits it by construction. All five bound scripts report it
in the same words, which is the reason the mapping is one shared file.

The verdict itself is the exit status of each stage's own process — nothing is
parsed out of a stream. That replaces a tab-separated sentinel line carrying one
of eight `kind` values, which replaced classification-by-first-word. Both
earlier contracts existed so that a passing run could be graded; with one green
outcome there is nothing to grade.

### What the check cannot do

Four limits, stated because a gate that is trusted past its reach is worse than
one that is not trusted at all:

* **It starts from the tables.** A cloud that is in none of the deployment
  tables `operational_analytics_fleet.deployment_sources()` reads — provisioned
  by hand, serving traffic, named nowhere in `src/` — is invisible to all of
  this, exactly as it is invisible to `/v1/regions` and the marketing map. Step 1
  of the checklist below is unavoidable *for a cloud somebody adds properly*; it
  is not a law of physics.
* **Stage (e) reads this checkout, not the deployed revision** (above). It is
  also the one judgement still made by reading text, and it is the weakest thing
  in the gate: somebody who wants to beat it can, exactly as the old script
  binding was beatable. It is kept because the alternative needs cloud
  credentials, and a check that needs production credentials is a check nobody
  runs.
* **It cannot see rows move.** No status page can: an empty outbox and a
  switched-off one publish the same number. That evidence is the two in-cloud
  counts, and every passing run says so in the banner — not as a downgrade
  earned by a stage, just as a fact about what the five stages are.
* **One bound script's tail is claimed, not proven** —
  `aws_eu_clickhouse_drain_install.sh`, above.

### What is missing right now

**No cloud publishes the `analytics` section yet.** Verified 2026-08-17 by
fetching all three status pages: `verify_cloud_complete.sh` exits 5 (NOT YET
OBSERVABLE) for `gcp`, `aws` and `azure`. The publisher ships in the same change
as this section; each cloud starts answering when a control plane built from it
is deployed there. Until then the gate is honest and red, which is the true
state of the fleet rather than a defect in the gate.

**Azure has no operational-analytics outbox.** `azure_control_plane.sh` sets no
`TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED` at all, so the cloud enqueues nothing,
has nothing to drain, and stage (e) fails. It is a canary
(`aws-eu-and-azure-canary.md` §3) and it still counts: a canary whose operational
history is unrecorded cannot answer the question canaries exist to answer.

### Checklist for the next cloud

1. Add it to the deployment tables (`byok_v1_attestations.STANDALONE_CLOUDS`,
   `regions.MULTICLOUD_REGION_GEO`, …). CI now fails until steps 2 and 3 are
   done.
2. Add a `FleetAnalyticsEndpoint` for it in
   `src/trusted_router/operational_analytics_fleet.py`, pointing at the public
   `/status.json` of its **control plane** — the deployment that holds the
   database connection, which is not always the friendliest hostname. (AWS's is
   the tr-eu App Runner service, not `aws.trustedrouter.com`.)
3. Add a `CloudRollout` entry in
   `src/trusted_router/cloud_rollout_completeness.py` naming its control-plane
   deploy script and its drain install command.
4. List every deploy script for it in that entry's `deploy_scripts`, and end
   each of those scripts with

       SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
       . "${SCRIPT_DIR}/cloud_complete_gate.sh"
       require_cloud_complete <cloud> "$(cat <<'NEXT'
       ...what to do about it...
       NEXT
       )"

   letting its exit status stand. Add a fixture for it in
   `tests/deploy_script_harness.py` so the behavioural test can run it; if it
   cannot be run honestly under stubs, mark it `NOT_PROVEN` with the reason and
   say so here. CI fails on a `NOT_PROVEN` entry with no reason.
5. Build the pipeline: an outbox (enabled in the control-plane script), a
   ClickHouse the cloud owns, and a drain installed as a supervised unit.
6. Run `bash scripts/deploy/verify_cloud_complete.sh <cloud>` until it exits 0
   and prints `VERIFIED`, then do the thing the banner tells you it did not do:
   watch two counts ten minutes apart from inside the cloud.
