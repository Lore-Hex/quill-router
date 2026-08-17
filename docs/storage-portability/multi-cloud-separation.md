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
in-cloud count proves anything moves. When you finish a cloud, look once, from
inside:

    clickhouse-client --query 'SELECT count() FROM activity_generations'

and then again ten minutes later. Two numbers, the second larger. That is the
observation the rule above is named after; nothing in a status page substitutes
for it.

### The analytics stage is not optional

Bring-up is not a menu. Every cloud bring-up and control-plane deploy script
ends by running the check and exits non-zero when it fails —
`aws_eu_clickhouse.sh`, `aws_eu_north_clickhouse.sh`, `aws_eu_control_plane.sh`,
`aws_eu_clickhouse_drain_install.sh`, `azure_control_plane.sh`. Where a
remaining step genuinely needs a human (a cost decision, a password that only
exists on a node), the script prints the exact command **and exits non-zero**.
It never prints and returns 0; that behaviour is the outage.

There is exactly one way to run a cloud without an analytics pipeline: set
`analytics_absent_reason` on that cloud's entry in
`src/trusted_router/cloud_rollout_completeness.py`. That is a code change and
therefore a review, and the check keeps printing the blocker it is suppressing.
No cloud has one today.

### What is missing right now

**Azure has no operational-analytics outbox.** `azure_control_plane.sh` sets no
`TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED` at all, so the cloud enqueues nothing,
has nothing to drain, and stage (e) fails. It is a canary
(`aws-eu-and-azure-canary.md` §3) and it still counts: a canary whose operational
history is unrecorded cannot answer the question canaries exist to answer.

### Checklist for the next cloud

1. Add it to the deployment tables (`byok_v1_attestations.STANDALONE_CLOUDS`,
   `regions.MULTICLOUD_REGION_GEO`). CI now fails until steps 2 and 3 are done.
2. Add its public base URL to `Settings.synthetic_fleet_peers` so the fleet
   watches it.
3. Add a `CloudRollout` entry in
   `src/trusted_router/cloud_rollout_completeness.py` naming its control-plane
   deploy script and its drain install command.
4. Build the pipeline: an outbox (enabled in the control-plane script), a
   ClickHouse the cloud owns, and a drain installed as a supervised unit.
5. Run `bash scripts/deploy/verify_cloud_complete.sh <cloud>` until it exits 0,
   then watch two counts ten minutes apart from inside the cloud.
