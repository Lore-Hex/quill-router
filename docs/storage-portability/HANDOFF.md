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
| Per-cloud *control-plane* independence — AWS | **Code merged** (qcp#111): AWS dials its own plane first, falls back only on a dial failure. **Needs an EIF rebuild + PCR0 re-pin to take effect** (§4.5) |
| Per-cloud *control-plane* independence — Azure | **Not yet.** Client machinery is inherited; the base-URL flip lives on the azure branch and changes HOST_DATA (§4.5) |
| Postgres can serve `authorize` | **Fixed**, router#452 — 11 gateway-reachable methods used to raise (§4.6) |

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
| GCP | `api.trustedrouter.com` | Confidential Space, Google-signed OIDC JWT | `image_digest` — CHECK IT, do not trust this doc: it rolled twice on 2026-08-05 alone (873c2a37 → 4cf0aa71 → …). Compare `/status.json` against trust.trustedrouter.com | `trustedrouter.com`, 451 models |
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

### 4.5 Control-plane independence — AWS code merged, Azure still to do

**The problem.** Every enclave dialled `https://trustedrouter.com` and failed CLOSED if it was
unreachable: authorization is on the request path (`main.go:631`), so an error there ends the
request. Each cloud's availability was therefore the PRODUCT of its own uptime and the
canonical plane's — adding AWS regions could not raise AWS above whatever GCP was doing.
Fail-closed is right in itself (serving unauthorized inference is free inference and a lost
usage record); the defect was *where the authorizer lived*.

**What shipped (quill-cloud-proxy#111).** Two halves:

* `TR_CONTROL_PLANE_BASE_URL` is now an ORDERED, comma-separated list. AWS deploys as
  `https://aws.trustedrouter.com/v1,https://trustedrouter.com/v1`, so a normal AWS request
  never touches another cloud. The parent passes the value through verbatim
  (`quill_parent/config.py` treats it as an opaque string), so no parent change was needed,
  and a single value behaves exactly as before.
* On a **dial failure** the client moves to the next endpoint instead of erroring.

**The safety rule, because this is the part to not get wrong.** These planes have SEPARATE
databases and the idempotency key does not travel between them, so re-sending `authorize` can
escrow twice and `settle` can bill twice. Failover is permitted ONLY when the request provably
reached no server. `net/http` runs `DialContext` before writing a single request byte, so a
dial error cannot have produced a reservation; everything after is ambiguous — notably a
connection dropped mid-response, where the server HAS processed the request and only the
answer was lost.

Classifying by error SHAPE was rejected: on AWS the dialer is a vsock dial whose failures are
plain syscall errors, not `*net.OpError`, so a shape check would miss the most likely real
failure — the parent's vsock-proxy being down. The dialer is wrapped instead
(`markDialFailures`) and its errors tagged. See `enclave-go/internal/trustedrouter/endpoints.go`.

**NOT DONE — merging #111 is not the same as the routing being live.** `aws.trustedrouter.com`
joined `trControlPlaneTunnels`, which is compiled into the binary, inside the EIF, and
therefore MEASURED BY PCR0. AWS needs an **EIF rebuild and a fleet-wide PCR0 re-pin** before
any of this takes effect. Both planes are deliberately in the allowlist even though only one
is primary: adding a host later costs another rebuild + re-pin, so the expensive measured part
was done once and generously, and the preference ORDER stays ordinary config.

**Azure still to do.** It inherits the client machinery (it builds under `!cloud_aws`), but
`tools/deploy-azure-aci.sh` lives on the `azure-attestation` branch, and its
`TR_CONTROL_PLANE_BASE_URL` is measured into the CCE policy → HOST_DATA. Flipping it means
regenerating the policy, rebinding the Key Vault SKR release policy, and rotating every
`--expected-hostdata` pin — publishing the incoming value into the accepted set BEFORE
cutover, or you recreate the attestation-verifier rollout deadlock. Gated on §4.6 item 4.

**Still true after all of this:** a request that reaches the fallback is being served by
another cloud's plane. That is strictly better than failing, but it is not independence — it
is a floor. Independence is the FIRST entry being healthy, which is what the per-cloud status
pages should be measuring.

### 4.6 Blockers, and where each one now stands

1. **Postgres could not serve `authorize` at all — FIXED (router#452).** Eleven
   gateway-reachable methods raised `NotImplementedError`; four from `gateway.py`, seven from
   the video-job queue. Both peer planes run `TR_STORAGE_BACKEND=postgres` while every enclave
   dialled GCP (Spanner), so that path had never served a real request. Worse,
   `list_broadcast_destinations` is called at `gateway.py:616`, AFTER the escrow commits, so
   every attempt would have stranded a reservation. A static guard in
   `tests/test_store_protocol_conformance.py` now fails if any enclave-facing route calls a
   method Postgres refuses, and `tests/conformance/test_video_job_semantics.py` pins the
   behaviour on every backend.
2. **TLS-resumption attestation mis-bind — FIXED, DEPLOYED, VERIFIED (qcp#108).** Reproduced
   live on three GCP replicas: a resumed TLS 1.3 session attested to whichever hostname last
   completed a FULL handshake. Fixed via `Server.singleCert` plus disabling session tickets on
   the ACME path. Re-running the reproduction against production now shows
   `session_reused=False`, so the defect is unreachable rather than merely unlikely. The trap
   worth remembering: deleting the pre-seed outright — the "obvious" fix — would have 503'd
   the entire AWS fleet, because `NewSelfSigned` never calls `GetCertificate` and the pre-seed
   is that path's only leaf writer.
3. **ACME has no fallback — STILL OPEN.** `NewACME` returns autocert's error and the handshake
   dies with alert 80, so a Let's Encrypt outage is a TOTAL TLS outage on GCP and Azure, and
   `reconcile-enclave-dns.py` health-gates on the attestation verifier — so an LE outage makes
   the reconciler DRAIN a healthy fleet. AWS is immune only because it uses a self-signed
   attested cert. Fix: fall back to an in-TEE self-signed cert, and ship the alarm in the same
   PR — the fallback makes a CA outage *quieter*, so without an explicit page it runs
   undetected. `tools/check-public-tls.py` expects a public CA cert on all 16 names and will go
   red the moment the fallback fires; split it in the same change.
4. **Azure's measurement is published nowhere — STILL OPEN.** The trust page carries GCP's
   `image_digest` and AWS's PCR0 but zero Azure hostdata, and the verifier correctly refuses an
   MAA token without `--expected-hostdata` (MAA attests ANY caller's hardware, so an unrelated
   confidential container attesting against the same instance yields a genuine-but-wrong
   token). Today the only way to get the pin is `az container show` against the subscription,
   which no third party has — so Azure attestation is unverifiable from outside. This also
   blocks the Azure cutover in §4.5, which needs a published accepted-set to rotate through.
5. **Four AWS upstreams were unreachable — FIXED (qcp#109).** Port 8042 was assigned twice;
   `inference.makora.com` had an enclave tunnel and no parent proxy at all; two tinfoil
   verification hosts had units but no `vsock-proxy.yaml` address entry.
   `tools/test_vsock_port_map.py` now enforces all four invariants in CI.

### 4.6b Measurement: no cloud can currently DEMONSTRATE a nines number

Separate from whether the systems are reliable. Measured 2026-08-05: AWS reports
`gateway_overhead_sample_count: 4`, Azure `2`, GCP `1-4`, with ~100 samples total in each
`status.json`. Three nines means one bad sample in a thousand — at that cadence 99.9% is not
distinguishable from 99% or from noise, so any per-cloud figure today is an assertion rather
than a measurement.

Two things to check before trusting any green:
* Azure carries samples with status `unknown` and `error_type
  "reuse_not_measurable_request_rejected"`. If `unknown` is excluded from the uptime maths then
  some failures cost nothing, which is a fake nine.
* Azure's rendered `/status` says "Trust degraded" four times while its `status.json` says
  `overall=up`. One of those surfaces is lying; find out which before reporting either.

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

**A test can be green for the wrong reason, and the money tests are where that bites.**
The failover safety test in `enclave-go/internal/trustedrouter/endpoints_test.go` originally
asserted that a 500 from the primary control plane did not fall through to the secondary. It
passed — and it also passed with the safety rule DELETED, because `net/http` returns
`(resp, nil)` for any HTTP status, so a 500 never reaches the failover branch at all. The test
was decorative. It now uses a listener that reads the request then drops the connection, which
is the real double-bill case, and it goes red under mutation. **Mutate every test that guards
money: if deleting the rule leaves it green, it guards nothing.**

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
