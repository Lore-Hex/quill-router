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
