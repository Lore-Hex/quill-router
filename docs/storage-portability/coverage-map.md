# Coverage map: where deployments go, and why not everywhere

Companion to [`multi-cloud-separation.md`](multi-cloud-separation.md) and
[`aws-eu-and-azure-canary.md`](aws-eu-and-azure-canary.md).

---

## 1. Two different things are both called "more locations"

They have opposite cost profiles, and conflating them is expensive.

| | More **regions inside** one deployment | More **standalone deployments** |
|---|---|---|
| Database | one, spanning regions | one **per deployment** |
| Credits | one wallet | **a separate wallet each** |
| API keys | work everywhere in it | scoped to that deployment |
| Buys you | availability, in-region failover | jurisdiction / residency |
| Marginal cost | small | a whole operational surface |

Under the separation decision a standalone deployment is **a separate credit
balance by design**. That is correct and deliberate — but it means N deployments
is N wallets a user cannot move money between. Two clouds is already a support
burden the console has to make visible. Six would be a product.

**So: be generous with regions, deliberate with deployments.**

---

## 2. Latency diversity is not a control-plane problem

Worth separating, because it is the usual reason people ask for a wide map.

* **Inference latency** is a *gateway/enclave* concern, and that layer is already
  multi-region on GCP. Putting a control plane in Tokyo does not make a Tokyo
  user's tokens arrive faster.
* **The control plane** is signup, console, billing, status. It is not on the
  token path. Its location matters for **residency and jurisdiction**, not speed.

If the goal is "users everywhere feel fast", the answer is more *gateway*
regions. If the goal is "data for these customers stays in this jurisdiction",
the answer is a standalone deployment. They are different asks with different
mechanisms, and only the second one costs a wallet.

---

## 3. Facts, checked

**Aurora DSQL is broadly available**, so geography is not the constraint — but
the naive probe that produced that list had a **false positive**, and the
mechanism is worth knowing.

`aws dsql list-clusters` against a region the account has **not opted into**
does not fail the way an unsupported-service call fails, so a
grep-for-an-error probe scores it as available. `eu-south-1` (Milan) passed that
probe and is in fact `not-opted-in`, which only surfaced when
`aws ec2 describe-regions` was checked directly.

**Check `OptInStatus` before believing any region is usable:**

```bash
aws ec2 describe-regions --query "Regions[?starts_with(RegionName,'eu-')].{r:RegionName,s:OptInStatus}" --output text
```

EU regions usable with no opt-in today: `eu-west-1` Ireland, `eu-central-1`
Frankfurt, `eu-west-3` Paris, `eu-north-1` Stockholm. (`eu-west-2` is London and
is not the EU.) Opt-in regions such as Milan, Spain and Zurich need an explicit
account-level enable that does not take effect immediately.

**Azure exposes 40+ physical regions, but this subscription is capacity-restricted
per service and inconsistently**: Postgres refused in `westeurope`, fine in
`northeurope`; ACR the exact reverse; ACR Tasks not permitted at all. Any Azure
region must be **probed, not assumed**.

**`eu-west-2` (London) is not the EU.** Post-Brexit the UK is a third country
under GDPR. DSQL is available there, which makes it an easy and expensive
mistake.

---

## 4. Recommended map

### AWS-EU — the advertised product, one deployment, one wallet

Diversity **inside** the deployment, entirely within EU member states, so the
residency claim survives:

| Role | Region |
|---|---|
| DSQL primary | `eu-west-1` **Dublin** |
| DSQL peer | `eu-north-1` **Stockholm** |
| DSQL witness | `eu-west-3` **Paris** (metadata only) |
| Compute | Dublin + Stockholm (active/active) |

Milan was the first choice for spread and had to be abandoned: it is an opt-in
region and the enable does not take effect promptly. Dublin + Stockholm gives
Atlantic-west plus Nordic-north — a wider spread than the conventional
Dublin + Frankfurt pair, and it avoids crowding the Netherlands dot that GCP
already occupies. Peer latency is roughly 40ms against Frankfurt's 25ms, which
is the price of the spread and is paid on write quorum.

This is what buys four nines — see
[`aws-eu-and-azure-canary.md`](aws-eu-and-azure-canary.md) §2 for why serial
composition means the database tier sets the ceiling.

### Azure — canaries, several geographies, no wallets

Canaries hold no production data, serve no users, and are never advertised, so
they can be spread freely and cheaply. Three is enough to prove the deploy
pipeline is not accidentally region-specific:

| Canary | Region | Proves |
|---|---|---|
| `tr-canary-eu` | `swedencentral` + `northeurope` | **live today** |
| `tr-canary-us` | **`canadacentral`** | non-EU deploy path |
| `tr-canary-apac` | **`southeastasia`** or **`japaneast`** | far-from-home latency, different capacity pool |

Those three were probed on 2026-07-30 and all accept Burstable B1ms.
**`eastus2` is RESTRICTED** for this subscription, which is exactly why the
table names verified regions rather than plausible ones. Stand one up with:

```bash
CANARY=tr-canary-us LOCATION=canadacentral APP_LOCATION=canadacentral \
  bash scripts/deploy/azure_canary.sh
```

Every resource name derives from `CANARY`, so a new geography is one env var
rather than a forked script. Budget roughly $20-25/month each and tear them
down when they are not earning it.

Each is one region, B1ms, scale-to-low. Their whole value is catching
region-specific breakage before it reaches a real deployment — which has already
paid for itself once, since three separate Azure capacity restrictions showed up
in the first one.

### Further standalone deployments — gate on demand

`aws-us`, `aws-apac`, and similar are **standalone**: own database, own credits,
own status page. Each is a real product surface, so create one when a customer
needs that jurisdiction, not to fill in a map. The architecture makes adding one
mechanical; that is not a reason to add six.

---

## 5. What has to be true before any deployment is advertised

1. Conformance passes against that deployment's **actual** database, not a
   compatible one. Running it against real DSQL found three incompatibilities
   that "Postgres-wire compatible" had hidden.
2. It serves **its own** status page from **its own** database.
3. Its residency claim is literally true for every tier — compute, database,
   analytics, logs, backups, secrets.
4. `reserve` / `settle` / `refund` exist if it serves inference. Until they do,
   a deployment is a control plane, not a router, and must not carry an
   inference SLO.
