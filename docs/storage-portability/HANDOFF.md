# HANDOFF — multi-cloud + ClickHouse analytics

**Last updated 2026-08-05.** Written for an agent taking this over cold. Read
[`multi-cloud-separation.md`](multi-cloud-separation.md) and
[`analytics-ingestion.md`](analytics-ingestion.md) for the *why*; this document is the *where
we are and what is next*.

> **Production topology update:** the canonical operational document is now
> [`../clickhouse-reliability.md`](../clickhouse-reliability.md). ClickHouse is
> no longer a single shadow node. Provider analytics reads use a private,
> three-zone replicated cluster. Historical stage descriptions below remain
> useful design context but are not the current runbook.

---

## 1. The program in one paragraph

TrustedRouter should run as a **standalone deployment on each cloud** — its own database,
credits, API keys, analytics. Identity federates across clouds; credits do not. GCP is the
existing deployment. AWS and Azure are next, both on the **same** `PostgresStore`. Analytics
moves off Bigtable onto **ClickHouse**, one instance per cloud, because Bigtable is an
operational wide-column store and was never a columnar warehouse.

---

## 2. Status

| Piece | State |
|---|---|
| Separation decision | **Decided**, #307 |
| `PostgresStore` | **Merged**, #310 — passes conformance |
| `PostgresStore` on Spanner PG dialect | **Proven**, #322 — 14/14 |
| ClickHouse cluster on GCP | **Live**: three zones, three Keeper voters, 500 GB SSD per replica |
| Backfill (historical) | **Done** — 200k+ rows, per-day parity with Bigtable |
| Live ingestion | **Live and verified**, durable Spanner outbox plus hourly repair |
| Route-health differential proof (stage 2 gate) | **Passes**, #350 |
| Provider portal read cutover | **Live** through private regional load balancer |
| Immutable Parquet archive | **Live**, verified daily revisions in GCS, seven-year retention |
| Hour/day/month rollups | **Live**, parity-gated atomic partition replacement |
| AWS deployment | **Serving and attesting** — Nitro enclave + Fargate control plane |
| Azure deployment | **Serving and attesting** — SEV-SNP/MAA enclave + Container App control plane |
| Per-cloud *control-plane* independence | **Not yet** — all three enclaves still dial GCP fail-closed (§4.5) |

### The single most useful result

**One `PostgresStore` covers every target.** Aurora DSQL (AWS), Cosmos DB for PostgreSQL /
Citus (Azure), *and* Spanner's PostgreSQL dialect (GCP) are all Postgres-wire. This was
verified, not assumed — the credit path's primitives were probed directly on Spanner PG
before trusting the suite:

| Primitive | Spanner PG |
|---|---|
| `jsonb` store + read-back | works |
| `INSERT ... ON CONFLICT DO NOTHING` | rowcount **1**, then **0** on replay |
| `ON CONFLICT ... DO UPDATE` | works |
| conditional `UPDATE` rowcount | works |
| `SELECT ... FOR UPDATE` | works |

The second row is exactly-once credit. Because of this, adding a cloud does **not** multiply
the "different money code per backend" risk — there is one implementation.

---

## 3. What is running in production right now

### 3.1 Three clouds, all serving and all attesting

Verified live 2026-08-05, each with a negative control (a deliberately wrong measurement must
fail, or the check proves nothing):

| | gateway | TEE + evidence | measurement | control plane |
|---|---|---|---|---|
| GCP | `api.trustedrouter.com` | Confidential Space, Google-signed OIDC JWT | `image_digest sha256:873c2a37…` — matches the trust page | `trustedrouter.com`, 451 models |
| AWS | `api-aws.trustedrouter.com` | Nitro, COSE_Sign1 → `aws-nitro-root.pem` in-repo | PCR0 | `aws.trustedrouter.com` → Global Accelerator `tr-eu-control-plane` → NLB → Fargate `tr-cp-euw1`/`tr-cp-euw3`, 448 models |
| Azure | `api-azure.trustedrouter.com` | SEV-SNP via MAA `trquilluaen.uaen.attest.azure.net` | `hostdata 1d3429b3eaaf66b1…` | `azure.trustedrouter.com`, 451 models |

Reproduce (Azure needs both pins; the hostdata must be derived **independently** of the token,
or you are just checking the token against itself):

```bash
python3 tools/verify-attestation.py --api-host api.trustedrouter.com
python3 tools/verify-attestation.py --api-host api-aws.trustedrouter.com --attested-cert-only
HD=$(az container show --name quill-enclave-uaenorth --resource-group TR-TEE-DUBAI \
      --query confidentialComputeProperties.ccePolicy -o tsv | base64 -d | shasum -a 256 | cut -d' ' -f1)
python3 tools/verify-attestation.py --api-host api-azure.trustedrouter.com \
  --expected-maa-issuer https://trquilluaen.uaen.attest.azure.net --expected-hostdata "$HD"
```

**The 448-vs-451 model drift is real**, not rounding: the AWS control plane is behind. Reconcile
before making it authoritative for its own traffic.

**Certificates differ by design.** AWS mints a self-signed cert *inside* the TEE
(`O=Quill Cloud (attested enclave)`) and clients verify by attestation — no CA in the trust
path at all. GCP and Azure use ACME/Let's Encrypt for browser friendliness, which is why §4.6
item 3 only affects those two.

**L4 passthrough is mandatory.** Global Accelerator and NLB never terminate TLS. Anything that
does — Azure Front Door, an ALB, a CDN — **voids attestation**, because the enclave mints the
leaf inside the TEE and the attestation document binds that exact leaf.

### ClickHouse cluster

* `tr-clickhouse-1/2/3` run in `us-central1-a/b/c`, each on `e2-standard-4`
  with a 500 GB SSD and no external IP.
* One logical shard has three `ReplicatedReplacingMergeTree` replicas and a
  three-voter embedded Keeper quorum.
* Provider analytics readers use private load balancer `tr-clickhouse-ilb`.
* Exact raw queries use `FINAL`. Recomputed hourly, daily, and monthly tables
  replace verified partitions instead of using replay-unsafe additive views.
* Daily disk snapshots retain 30 days. Verified immutable Parquet retains raw
  history for seven years. See the canonical runbook linked above.

### Live ingestion

App → Spanner outbox → ingester → ClickHouse.

* **Outbox** `tr_analytics_outbox`, PK `(shard, commit_ts, event_id)`, `allow_commit_timestamp`,
  16 shards, plus `ROW DELETION POLICY (OLDER_THAN(commit_ts, INTERVAL 7 DAY))`.
* **Enqueue** in `storage_gcp_generations.py`, gated on `TR_ANALYTICS_OUTBOX_ENABLED`
  (currently `true` in `scripts/deploy/rollout.sh`). Separate transaction from settle,
  best-effort, logs `loss_tolerated` and `repairable_via`.
* **Ingester** `clickhouse/ingest_outbox.py`, systemd `tr-clickhouse-ingest.service`.
* **Reconciler** `clickhouse/reconcile_benchmark_samples.py`, systemd timer, hourly.
* Deployed by `scripts/deploy/clickhouse_live_ingestion.sh` (refuses to run if the node has an
  external IP).

Verified 2026-07-30: rows flowed, outbox depth held at **0**, `drain_lag_seconds=0.000`,
`clickhouse_insert_errors_total=0`, and the ingester's `rows_ingested_total` matched the
ClickHouse row delta exactly.

Provider portal analytics now read ClickHouse through the private load
balancer. Spanner remains authoritative for billing, and ClickHouse remains
off the inference and settlement critical path.

### Keyless cross-cloud identity (provisioned, currently unused)

GCP has WIF pool `multicloud`, provider `aws-workloads` (AWS account `330422590279`), and SA
`tr-multicloud@quill-cloud-proxy` with **database-scoped** Spanner and **instance-scoped**
Bigtable — not project-level. `scripts/entrypoint.sh` materialises an `external_account`
config from a plain env var and **refuses key material**, so the seam cannot decay back into a
mirrored service-account key.

Under separation no *data* path needs this. It is for operational access (shared image
registry, bootstrap) only. **Do not build application data flow on it.**

---

## 4. Next tasks

**Priority order is now 4.6 → 4.5 → 4.1 → the analytics items.** This section was written when
the clouds did not exist yet, so 4.1–4.4 are listed first for historical continuity; they are
no longer what is blocking. Read **4.5 and 4.6 first** — everything else raises a per-cloud
number, while 4.5 is the term that currently caps AWS and Azure at GCP's availability no matter
how many regions get added.

### 4.1 Clean up Spanner before adding clouds — issue #334

`tr_entities` holds **14.8M rows**; real business data is *185 workspaces, 251 API keys*. Three
problems:

1. **~5.45M dead rows.** `reservation`, `reservation_idemp`,
   `gateway_authorization_idempotency` stopped being written **2026-06-26**, matching the typed
   ledger cutover. The typed tables took over; the entity copies were never deleted. Tell:
   typed `tr_gateway_authorization` = 7,806 rows vs its entity twin = 3,064,299.
2. **~9.4M rows still growing with no TTL.** The DDL's 7 row-deletion policies are on the
   *typed* tables; `tr_entities` has none. `generation` + `generation_by_workspace` (5.9M) also
   duplicate Bigtable — check for readers before retiring.
3. **`tr_settle_outbox` at 549,173 rows.** Drain-then-delete is not deleting.

**Do this first.** Otherwise the multi-cloud work replicates the bloat into every new cloud.
Confirm no readers, then batched/partitioned DML — never large unbatched DML during a rolling
deploy.

### 4.2 Route-health read cutover

The differential proof passes (#350, `clickhouse/prove_route_health.py`). The cutover itself is
a separate change: point `evaluate_route_health` at ClickHouse behind a setting, keep the
Bigtable path as fallback, and require a restore story before ClickHouse becomes load-bearing.

### 4.3 Leaderboard proof, then cutover

Materially harder than route health: provider-balanced capped sampling
(`benchmark_samples.py`), organic/synthetic exclusions, **exact nearest-rank percentiles**,
sustained-throughput fallback (`leaderboard.py`), top errors, last-seen, weighted provider
aggregation. `clickhouse/prove_leaderboard.py` exists as a starting point.

### 4.4 Fix the reconciler: it detects but does not repair

It reports drift and **exits 1**, so the systemd unit reports failure hourly with no repair
path. Wire the backfill in as remediation, or make the exit code meaningful and alert on it.

### 4.5 THE ONE THAT MATTERS NOW: every enclave still dials GCP, fail-closed

All three clouds serve and attest (§3.1). Independence does **not** follow, because every
enclave — GCP, AWS *and* Azure — sets `TR_CONTROL_PLANE_BASE_URL=https://trustedrouter.com`
(`deploy-gcp-mig.sh:162`, `deploy-aws-nitro.sh:808`, `deploy-azure-aci.sh:199`) and
authorization is fail-closed by design (`main.go:631` — `AuthorizeWithRoute` error → write
error → return). Fail-closed is **correct**: serving an unauthorized request is free inference
and a lost usage record. The defect is *where the authorizer lives*.

So today **AWS's and Azure's availability are capped by GCP's control plane**, and no DNS or
regional arrangement changes that. Client-side SDK failover buys nothing until this is fixed:
failing over to AWS during a GCP outage just reaches an enclave that dials GCP to authorize.

The per-cloud control planes already exist and serve (§3.1). What blocks the flip is that
**the control-plane address is inside the attestation measurement**:

* **AWS** — the permitted hosts are a *compiled-in* vsock allowlist,
  `enclave-go/internal/trustedrouter/http_client_aws.go:28`. A Nitro enclave has no network
  stack; it reaches the outside only through vsock tunnels to the parent, and that list lives
  in the binary inside the EIF, which is what **PCR0 measures**. The parent half
  (`write_vsock_unit` + the `vsock-proxy.yaml` address allowlist in `deploy-aws-nitro.sh`) is
  *not* measured — both halves must move together or the enclave simply cannot dial.
* **Azure** — `TR_CONTROL_PLANE_BASE_URL` is container env baked into the CCE policy, so it
  lands in `x-ms-sevsnpvm-hostdata`. Changing it invalidates the Key Vault SKR release policy
  **and** every `--expected-hostdata` pin. Wrong order → Key Vault 403 at boot → the container
  group exits. This deadlock has been hit on this project before.

Money correctness is **already solved**: federation plus deferred settlement (merged) let a
peer admit spend locally against a conditional-UPDATE cap and forward the debt to the home
ledger. What remains is a measurement-coordination problem, not an architecture problem.

**Do it once.** Measure a *stable per-cloud* name (`control-aws.` / `control-azure.`) rather
than a concrete endpoint, so future re-homing inside a cloud is a DNS change with no
measurement churn. `aws.trustedrouter.com` already resolves to the AWS control plane.

**Bake in backup names in the same rebuild.** `trustedrouter.com` is on Google Cloud DNS;
`uptimerouter.com` and `allyrouter.com` are on AWS Route 53 (zones `Z00893363GIOMU7Z8647K`,
`Z09662142UE0IQL51B13V`). An enclave that can only reach a control plane via a
`trustedrouter.com` name is stranded by a Cloud DNS failure even when the control plane is
healthy. Because the allowlist is measured, alternates **cannot** be added later without
another PCR0 rebuild and fleet-wide re-pin.

Note the allowlist only grants *permission* to dial; it does not retry. Ordered failover has
to be added in the client, and it needs a **per-operation** policy — retrying `authorize`
elsewhere can double-reserve and retrying `settle` can double-book. Do not ship blanket retry
on the money path.

### 4.6 Blockers that must land before the flip

1. **`PostgresStore` could not serve `authorize` at all** — PR #452. Eleven gateway-reachable
   methods still raised `NotImplementedError`; four from `gateway.py`, seven from the video job
   queue. Both peer planes run `TR_STORAGE_BACKEND=postgres` while every enclave dialled GCP
   (Spanner), so that path had **never served a real request**. Worse,
   `list_broadcast_destinations` is called at `gateway.py:616`, *after* `reserve_key_limit`,
   `STORE.reserve` and `create_gateway_authorization` commit — so every attempt would have
   stranded a credit reservation with no authorization id to settle or refund against.
2. **TLS session resumption attests the wrong certificate** — reproduced live on all three GCP
   replicas. `cert.go` pre-seeds each connection's leaf from a *process-global*, and Go's TLS
   1.3 server never calls `GetCertificate` on a resumed (PSK) handshake, so the pre-seed
   survives and becomes the attested leaf. Fails closed at the verifier, and
   `reconcile-enclave-dns.py` health-gates on that verifier — so a mis-bind can drain a healthy
   instance out of DNS. **Do not "just delete the pre-seed"**: `GetCertificate` exists only in
   `NewACME`; `NewSelfSigned` (the AWS path) never calls it, so the pre-seed is that path's
   only writer and removing it 503s the entire AWS fleet.
3. **ACME has no fallback** — `NewACME` returns autocert's error and the handshake dies with
   alert 80. Let's Encrypt is a *hard* availability dependency shared across GCP and Azure, and
   the DNS-health reconciler amplifies an LE outage into a fleet drain. AWS is immune only
   because it uses a self-signed attested cert.
4. **Azure's measurement is published nowhere.** The trust page carries GCP's `image_digest`
   and AWS's PCR0 but zero Azure hostdata, and the verifier correctly refuses an MAA token
   without `--expected-hostdata` (MAA will attest *any* caller's hardware, so an unrelated
   confidential container attesting against the same instance yields a genuine-but-wrong
   token). Today the only way to obtain the pin is `az container show` against the
   subscription, which no third party has — so Azure attestation is currently unverifiable
   from outside.

### 4.7 Per-cloud status pages

**No refactor needed** — the seam already exists at the CLI boundary:

* **Uptime** = `run_synthetic_once` (called from `routes/internal/synthetic.py` and
  `synthetic/cli.py`) → `SyntheticProbeSample` → status page. **Runs on every cloud**, because
  uptime is a property of *that deployment*; a global number would hide an AWS outage behind
  GCP's health.
* **Throughput** = `provider_rotation_probe` / `provider_throughput_probe` (called **only**
  from `synthetic/cli.py`) → `ProviderBenchmarkSample` → leaderboard. **GCP only**, because it
  measures the *providers*, who are identical from anywhere; splitting it would give three
  noisy small samples instead of one good one.

So AWS and Azure simply never schedule the throughput commands. No cross-cloud pipe.

---

## 5. Traps that have already cost real debugging time

**"Configured" is not "working", and this program's green signals lie in a specific way.**
Both peer control planes returned 200 on `/v1/models` and had green status pages while their
`authorize` path could not execute a single request. The status probes target
`api-aws`/`api-azure.trustedrouter.com` — the **enclaves** — and those dial `trustedrouter.com`,
so every green check was exercising GCP's Spanner store. Before trusting a peer signal, ask
*which backend did that request actually reach*.

**A conformance suite that skips proves nothing.** `tests/conformance`'s Postgres backend
`pytest.skip()`s unless `TR_CONFORMANCE_POSTGRES_DSN` is set, so it is normally not running.
Stand up the real thing — it takes seconds and it is how the eleven unimplemented methods were
actually verified fixed:

```bash
docker run -d --rm --name tr-conf-pg -e POSTGRES_PASSWORD=conf -e POSTGRES_DB=trconf -p 55433:5432 postgres:17-alpine
```

**A method that *exists* is not a method that *works*.** `_not_implemented` satisfies every
structural Protocol check and every mypy signature test, then raises at runtime. The guard for
this is now static (`tests/test_store_protocol_conformance.py`) precisely so it runs when the
behavioural backend is skipped.

**`_` is a LIKE wildcard.** Secondary-index ids are `<owner>#<rest>` and owner ids contain `_`,
so `id LIKE 'ws_abc#%'` also matches `wsXabc#…` — one tenant's prefix scan returning another
tenant's rows. Escape the prefix; `storage_postgres.py` has `_like_prefix` for this.

**Delete the old index row before writing the new one.** In the video-job queue, when
`next_poll_at` is unchanged the old and new due-ids are *equal*, so deleting second removes the
row just written and the job leaves the queue permanently.

**Attestation questions must be asked about the resumed handshake too.** An 18-probe
concurrent mixed-SNI test passed and was reported as proof that multi-hostname binding was
safe. It only ever opened *fresh* connections. On a TLS 1.3 **resumed** session the binding is
wrong (§4.6 item 2). RFC 9266 channel binding cannot detect it — the exporter is correct on a
resumed session, so it passes while the leaf is wrong.

**Splitting DNS providers per cloud is arithmetically negative for a subdomain.**
`api-azure.trustedrouter.com` is a subdomain of a Cloud-DNS-hosted apex, so a resolver must
traverse Cloud DNS to reach the Azure NS referral: two *serial* dependencies, not two parallel
ones. Three separate refutations killed this idea; it is recorded here so it is not
rediscovered as clever.

**Let's Encrypt rate limits key per hostname, not per registered domain.** The duplicate limit
is per identical identifier set and the failed-validation limit is per account per hostname;
only the 50/week limit keys on the registered domain and it fires ten times later. A new
*subdomain* gives exactly the same relief as a new registered domain, for free. And the shared
ACME cache is not a rate-limit device — it exists to distribute the TLS-ALPN-01 **challenge
token** across replicas.

**Running the test suite mutates tracked assets.** A full `pytest` leaves
`src/trusted_router/static/og/providers/{friendli.png,manifest.json}` dirty, which breaks a
`git rebase` mid-flow and would slip regenerated binaries into an unrelated `git commit -a`.
Three tests are also red at `HEAD` on a clean checkout
(`test_friendli_tombstones_second_miss_then_restores_annotations` and the two
`test_provider_branding` social-card tests) — verified in a detached worktree at `HEAD`, so
they are not anyone's local mess. The branding tests pass against the *regenerated* assets and
fail against the *committed* ones, which means local green depends on whether you have run the
suite before. Fix by making the generator tests write to `tmp_path`.

**`created_at` is not commit order.** The Bigtable row key is derived from *event* time. A row
committing late sorts behind an already-consumed range and is **missed forever**. This is why
live ingestion uses a commit-timestamp outbox. Never build a live cursor on `created_at`.

**`gen#` in Bigtable is keyed by generation ID, not time.** "Newest N by scan order" is a
random sample across all history. This produced a confident, wrong "data loss" conclusion.

**SQL NULL is three-valued.** `NULL IN (...)` is NULL, survives `NOT`, and `WHERE` then
**drops the row** — silently under-counting failures on rows with no HTTP status. Use
`ifNull()`. Cost a 6/525 mismatch on the leaderboard proof. Any Python→SQL predicate port needs
a differential test on real data; review-by-eye will not catch it.

**A ClickHouse MV is not covered by `ReplacingMergeTree`.** MVs run per INSERT block before
source replacement, so re-ingestion permanently inflates aggregates. Measured: 3 loads of
30,832 rows → 92,500 view samples.

**Route health takes newest-48 per route BEFORE filtering.** SQL that filters first and limits
second considers rows production never saw. Use `row_number() OVER (PARTITION BY provider,
model ORDER BY created_at DESC)` then filter in an outer query.

**A `--limit`ed backfill reports false MISMATCHes** for days the scan never reached —
`bigtable=0` there means "not scanned", not "missing".

**ClickHouse correctly refuses a plain IAP TCP tunnel.** Traffic arrives from Google's IAP
range while the `tr` user allows only `10.0.0.0/8` plus loopback, so it 403s even with the
right password. Use an SSH local forward so the connection originates from the node's own
loopback:

```bash
gcloud compute ssh tr-clickhouse-1 --zone us-central1-a --tunnel-through-iap -- -N -L 18123:localhost:8123
```

**`STORE` is monkeypatch-hostile.** It forwards via `__getattr__`, so `monkeypatch.setattr(STORE, ...)`
installs an instance attribute that teardown "restores" as a method bound to a dead store —
poisoning every later test. Patch the **class**. Issue #333.

**Starlette answers 405 from the router without raising**, so Sentry's 5xx-only default never
saw it. A console form POSTed to a GET-only route and failed silently for users indefinitely.
`failed_request_status_codes` now includes 405.

**`.notice` is styled green.** `.notice.bad` had no rule, so every console error message
rendered as a success. Fixed, but check computed styles rather than assuming a class exists.

---

## 6. Credentials and access

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/tr-ops-local.json"
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$HOME/.config/gcloud/tr-ops-local.json"
```

**You usually need both.** The first is for the client libraries; the second is for the
`gcloud` CLI itself — without it, gcloud uses the operator's reauth-prone user credentials and
fails non-interactively. Do **not** export these in a shell profile; that hijacks the
operator's own interactive gcloud.

SA `tr-ops-local@quill-cloud-proxy` holds instance-scoped Spanner `databaseAdmin` +
`databaseUser`, instance-scoped Bigtable `user`, and project-level `compute.instanceAdmin.v1`,
`iap.tunnelResourceAccessor`, `secretmanager.secretAccessor`. It does **not** hold Cloud Run
read or project-level IAM admin — ask a human for those.

Node access is IAP only:

```bash
gcloud compute ssh tr-clickhouse-1 --zone us-central1-a --project quill-cloud-proxy --tunnel-through-iap
```
