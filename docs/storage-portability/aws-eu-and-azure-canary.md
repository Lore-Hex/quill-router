# AWS-EU (production) and Azure (canary)

**Status:** decided 2026-07-30. Implements
[`multi-cloud-separation.md`](multi-cloud-separation.md). Read that first — each cloud is a
standalone deployment, identity federates, credits do not.

Two very different things, deliberately:

| | Azure | AWS |
|---|---|---|
| Purpose | **Deploy-pipeline canary** | **Production EU region** |
| Regions | one | two EU member states |
| Availability target | none | **99.99%** |
| Database | Flexible Server B1ms | Aurora DSQL, multi-region |
| Advertised? | never | **yes — EU-specific** |

Azure exists so that AWS is not the first place a broken assumption is discovered. Deploy to
Azure first, always.

---

## 1. AWS is EU-only, and that has to be literally true

The product claim is data residency. A claim like that is worth nothing if it is
approximately true, so the constraints are hard:

* **Regions must be EU member states.** Ireland (`eu-west-1`), Frankfurt (`eu-central-1`),
  Paris (`eu-west-3`), Stockholm (`eu-north-1`). Aurora DSQL is available in all four.
* **London (`eu-west-2`) is not the EU.** Post-Brexit the UK is a third country under GDPR.
  It is *available* for DSQL, which makes it an easy mistake. Do not use it.
* **Every tier stays in-region**: compute, database, analytics (ClickHouse), logs, backups,
  and secrets.

### What "EU-only" does NOT cover, and must not be implied

**Provider calls leave the EU.** If a user routes to a model hosted in `us-east`, the prompt
goes there. The honest claim is therefore about **the deployment**, not the inference:

> Your account, credits, API keys, usage history and analytics are stored and processed
> exclusively in the EU.

If we want to claim EU-only *inference* as well, that is a separate feature: a filtered
catalog exposing only EU-hosted provider endpoints. Worth building, but it is not implied by
this deployment and must not be advertised until it exists.

This separation is only credible **because** of the standalone-cloud decision. Under the
superseded shared-Spanner design, every EU request would have round-tripped to a US database,
and no residency claim would have been possible at all.

---

## 2. The four-nines target, honestly

**Four nines is 52.6 minutes of downtime per year.** It is an architectural property, not an
effort level.

### Serial composition is the trap

Four components each at 99.99% multiply to **99.96%** — about 3.5 hours a year. You cannot
reach four nines by assembling four "four-nines" services in a line. The failure-prone tiers
must be **parallel**.

| Tier | Design | Why |
|---|---|---|
| Aurora DSQL | **multi-region** | Single-region DSQL is 99.9%, which caps the whole product at three nines. Multi-region is 99.99%. This is the binding constraint. |
| Compute | active in **both** EU regions | Parallel: combined failure ≈ 1e-8 |
| Load balancer | one per region | Parallel |
| DNS | health-checked failover | Route 53 hosted-zone SLA is 100% |

Everything except the database must be *better* than 99.99% so it does not drag the product
below the database's own ceiling.

### Three caveats to state out loud

1. **An SLA is a refund promise, not a probability distribution.** Measured availability is
   the number that matters, which is why the per-cloud status page is part of this work rather
   than an afterthought.
2. **One bad deploy can spend a large slice of 52.6 minutes.** The rollout pipeline — staged
   traffic, health gates, fast rollback — matters as much as the infrastructure. GCP's
   `rollout.sh` already does 10/50/100 staged traffic; AWS must match it before it can claim
   four nines.
3. **Define "up" before promising it.** If AWS-EU serves inference, availability inherits
   upstream providers, which are individually nowhere near four nines — that is what the
   fallback machinery exists for. The control plane and status surface can hold four nines;
   an individual provider route cannot, and the SLO must say which it is measuring.

### Proposed topology

* **Primary pair:** `eu-west-1` (Ireland) + `eu-central-1` (Frankfurt). Two member states,
  ~25ms apart.
* **Witness:** `eu-west-3` (Paris). DSQL multi-region needs a witness region; keeping it in
  the EU keeps the residency claim intact.
* Compute active in both primaries, DNS health-checked between them.

---

## 3. Azure is a canary and must stay one

One region, no HA, no failover, no attestation, no inference. **Not on any SLO. Never
advertised.** Its entire job is to answer "does the image build, ship, boot, reach its own
database, and serve its own status page on a non-GCP cloud?" before that question is asked
somewhere expensive.

**Database choice deviates from the ADR on purpose.**
[`multi-cloud-separation.md`](multi-cloud-separation.md) recommends Cosmos DB for PostgreSQL
(Citus) for a *production* Azure region, because Citus distributes on `workspace_id`. The
canary uses plain **Flexible Server Burstable B1ms** instead: Citus carries a substantial
minimum node cost, and what is under test here is the pipeline, not sharding. `PostgresStore`
already passes conformance against stock Postgres, so this remains a faithful test of the
application. If Azure ever becomes production, re-validate on Citus with the conformance suite
first.

---

## 4. What actually blocked this, and the fix

`create_store` supported only `memory` and `spanner-bigtable`. **`postgres` was not
selectable**, so no amount of cloud infrastructure could have run the app. Fixed here:
`TR_STORAGE_BACKEND=postgres` + `TR_POSTGRES_DSN`, which fails loudly when the DSN is missing
rather than starting and dying on first query.

Booting the real app on Postgres locally then gave the honest picture:

| Route | Result |
|---|---|
| `/health` | 200 |
| `/` | 200 |
| `/status.json` | `NotImplementedError: synthetic_probe_samples` |

Tracing the status path showed it needs exactly **three** `Store` methods —
`record_synthetic_probe_sample`, `synthetic_probe_samples`, `synthetic_rollups`. That is the
whole per-cloud status page, and it is increment 2.

This is the pattern worth repeating: boot the thing locally and read the actual error, rather
than reasoning about what a deployment might need.

---

## 5. Order of work

1. **Wire `postgres` into `create_store`.** Done.
2. **PostgresStore increment 2** — the three synthetic methods, with conformance tests that
   run on `memory`, `postgres` and `spanner-pg`.
3. **Azure canary up**, `/status.json` green, its own uptime page.
4. **Spanner cleanup (#334)** before AWS, so 14.8M rows of mostly-garbage are not the shape
   that gets replicated into a new cloud.
5. **AWS-EU**: DSQL multi-region across Ireland + Frankfurt, conformance suite against the
   live cluster, then compute in both regions with staged rollout.
6. **AWS-EU status page + SLO**, measuring what it actually promises.
7. Only then advertise EU residency, and only the claim in §1 that is actually true.

**Inference on AWS-EU needs `reserve`/`settle`/`refund`**, which are still stubs. Until those
land and pass conformance, AWS-EU is a control plane, not a router.
