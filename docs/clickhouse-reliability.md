# ClickHouse reliability and long-term analytics

This is the canonical production runbook for TrustedRouter provider analytics.
It supersedes the single-node and shadow-read descriptions in
`docs/storage-portability/`.

## Scope and data policy

ClickHouse stores bounded request metadata used by provider dashboards,
leaderboards, and route-health analysis. It does not store prompts, model
outputs, API keys, BYOK secrets, authorization headers, or workspace IDs.
Spanner remains the source of truth for billing and the durable analytics
outbox. A ClickHouse failure must not fail inference or settlement.

Long-term analytics uses tiers instead of retaining every hot row forever:

| Tier | Retention | Purpose |
|---|---:|---|
| Provider and activity raw rows | 400 days | Recent investigation and exact re-aggregation |
| Raw synthetic probe rows | 14 days | Fine-grained status diagnosis before compaction |
| Synthetic status rollups | 24 months | Public reliability history |
| Hourly provider rollups | 3 years | Detailed trends and provider operations |
| Daily and monthly provider rollups | No TTL | Long-term product and provider analytics |
| Verified Parquet archive in GCS | 7 years | Immutable, portable raw history and disaster recovery |
| Persistent-disk snapshots | 30 daily snapshots | Fast node recovery |

The 400-day raw TTL has not been shortened. Changing it requires a separate
retention decision after archive restore drills and rollup consumers are
verified.

## Production topology

The GCP deployment is one logical shard with three synchronous ClickHouse
replicas and a three-voter embedded Keeper quorum:

| Node | Zone | Disk |
|---|---|---:|
| `tr-clickhouse-1` | `us-central1-a` | 500 GB SSD |
| `tr-clickhouse-2` | `us-central1-b` | 500 GB SSD |
| `tr-clickhouse-3` | `us-central1-c` | 500 GB SSD |

All nodes have no external IP, VM deletion protection, non-auto-deleting boot
disks, the Google Ops Agent, and the daily snapshot policy. The canonical raw
table uses `ReplicatedReplacingMergeTree`; exact readers use `FINAL` because
the Spanner outbox is at-least-once.

Customer-facing readers use the regional internal passthrough load balancer:

```text
tr-clickhouse-ilb (10.128.0.96:8123)
  -> tr-clickhouse-1
  -> tr-clickhouse-2
  -> tr-clickhouse-3
```

The forwarding rule allows global VPC access so all control-plane regions can
reach it. The read-only `tr_provider_read` account can select only the raw and
rollup tables. Ingestion, reconciliation, archive, and rollup workers run on
one node, the publisher: the instance whose `tr-clickhouse-role` metadata is
`publisher` (node 1 unless a takeover moved them). Every worker unit on every
node checks that key before it starts, so only the publisher runs them. Their
durable input remains in Spanner if the publisher is temporarily down.

The pre-migration local node-1 table is retained as
`provider_benchmark_samples_local_backup`. Do not delete it until a later,
explicitly reviewed cleanup.

## Credentials

On Postgres control planes, `TR_RECORD_READ_BACKEND` defaults to `postgres`,
preserving the existing unimplemented activity and usage reads. Deploy AWS
and Azure with that default, then flip to `clickhouse` per plane, AWS Paris
first, after verifying delivery and drain lag. The shared GCP readers use
that plane's `TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL`, `_USER`, `_PASSWORD`
and `_DATABASE` settings (`tr_control_read` and `default` on the standalone
nodes). Settings validation refuses `clickhouse` without the operational
ClickHouse URL and password, so standby regions without a reader stay on `postgres`.
Monitor workspace activity p50/p99, drain lag, the delivery verifier and
`/activity` 5xx before advancing to Azure. Set the backend back to `postgres`
to restore today's behaviour; record and outbox writes continue in either
mode. Generations that never reached ClickHouse are re-enqueued with
`clickhouse.repair_postgres_activity` (see
[Repairing missing Postgres activity](#repairing-missing-postgres-activity)).

For AWS ECS, `EXPECTED_RECORD_READ_BACKEND` in `aws_ecs_control_plane.sh`
is the switch, overridable with `TR_RECORD_READ_BACKEND_EU_WEST_3` and
`TR_RECORD_READ_BACKEND_EU_WEST_1`. Paris defaults to `clickhouse`, Ireland to
`postgres`. The execution role reads the reader's secret through
`tr-cp-clickhouse-control-reader` (`infra/aws_ecs_control_reader.tf`, a grant
on the secret's exact ARN); a task referencing the secret without that grant
fails to start. With `clickhouse`, the release sets
`tr_control_read` and the reader secret ARN resolved by `describe-secret`, and
refuses unless the cloned task reads database `default` (the reader is granted
`default.*` only). Ireland stays on `postgres`. Every release applies the
default in code, in either direction, so the default is the only lasting
switch: an environment override lasts until the next release, which the
automatic catch-up may start at any time. To revert Paris, land a change
setting its default back to `postgres`. That restores Postgres record reads
only: a reverted Paris keeps `tr_control_read` and the reader secret, which
the console's client reliability, retirement notices and the client watch
also read through. A broken reader breaks those too, and the revert does
not bring them back. So the enabling change first checks that
`tr_control_read` can log in to the Paris node.

Control-plane operational reads use `tr_control_read`. On GCP,
`scripts/deploy/clickhouse_control_reader.sh` installs the account with
table-scoped SELECT grants in `tr`; its password lives in Secret Manager as
`trustedrouter-clickhouse-control-read-password`.

The standalone Paris and Azure nodes store their analytics in `default`.
Their provisioners install the same read-only profile with only
`GRANT SELECT ON default.*`, restricted to the local VPC/VNet and loopback:

| Cloud | Reader password secret | Writer password secret |
|---|---|---|
| AWS (eu-west-3 Secrets Manager) | `quill/tr-eu-clickhouse-read-password` | `quill/tr-eu-clickhouse-password` |
| Azure (Key Vault `trquillkv`) | `tr-azure-clickhouse-read-password` | `tr-azure-clickhouse-password` |

Run `aws_eu_clickhouse.sh` or `azure_clickhouse.sh` before redeploying the
corresponding control plane. Each creates a missing reader secret, reuses an
existing value, and installs `users.d/tr-control-reader.xml` on new and
existing nodes. The XML contains a SHA-256 password hash and is owned by
`clickhouse` with mode `0640`; unchanged configuration does not restart the
server. AWS fetches and hashes the reader secret on the node with boto3 under
its instance profile; user-data and SSM commands carry only the secret name,
never the password or hash. The role receives `GetSecretValue` on that secret's
exact ARN before an existing node is converged. Azure fetches the reader
secret on the node using its managed identity, and creates reader or writer
secrets only on an explicit Key Vault `SecretNotFound`; other lookup failures
stop provisioning without creating a new version. The control-plane deploy
scripts select the reader secret by reference, without retrieving its value.

The Postgres outbox drains retain `default` and the writer secrets. Stockholm
is a second drain target, not a configured control-plane read endpoint; its
regional writer credential is unchanged. The AWS ECS release script migrates
reader credentials only where the regional record-read expectation is `clickhouse`;
all other ClickHouse settings are cloned from the running task.

## Activity generation lookup schema

The `by_generation_id` projection finds every stored copy of a generation ID
before the full rows are read by `(tenant_id, created_at, generation_id)` with
`FINAL`. The inner lookup uses the projection's primary index rather than
scanning the whole activity table.

| Topology | Migration |
|---|---|
| GCP replicated cluster (`tr`) | `clickhouse/023_activity_generation_id_projection_replicated.sql`, applied through `scripts/deploy/clickhouse_replicated_migrate.sh` |
| AWS Paris, AWS Stockholm and Azure standalone nodes (`default`) | `clickhouse/024_activity_generation_id_projection_single_node.sql` |

`scripts/deploy/_clickhouse_single_node_schema.sh` derives the standalone set
from `*_single_node.sql` in filename order, so 024 follows 006/009/011/013.
It uses unqualified table names and requires no Keeper or `ON CLUSTER` DDL.
Both variants use `ADD PROJECTION IF NOT EXISTS` and can be applied again.
`deduplicate_merge_projection_mode = 'rebuild'` keeps the projection correct
when `ReplacingMergeTree` merges redelivered rows. `MATERIALIZE PROJECTION`
also builds it for existing parts as a background mutation; check
`system.mutations` for completion and `system.projection_parts` for active
`by_generation_id` parts before considering the projection built.

Keep `lightweight_mutation_projection_mode` at its default, `throw`.
Lightweight `DELETE` is refused with code 344: on 26.7.1.1315, `rebuild` could
return deleted rows through the projection until a merge, while `drop` lost
the touched part's projection even after a forced merge. Use
`ALTER TABLE ... DELETE`, which rewrites the projection with the part.

## Durable archive

`clickhouse/archive_daily.py` exports each closed UTC day with `FINAL` for
provider benchmarks, tenant activity, raw synthetic probes, and synthetic
status rollups. It computes a full row-set fingerprint, writes Parquet, reads
the Parquet back with `clickhouse-local`, and compares row count, hash sum, and
hash XOR before publishing a manifest.

Objects are immutable and revisioned:

```text
gs://quill-cloud-proxy-tr-clickhouse-archive/
  raw/<dataset>/day=YYYY-MM-DD/
    _latest.json
    revisions/<fingerprint>/manifest.json
    revisions/<fingerprint>/part-*.parquet
```

A rerun with unchanged data is a no-op. Late reconciled rows produce a new
immutable revision and atomically advance `_latest.json`. Bucket versioning,
uniform access, and public-access prevention are enabled. The daily service
rechecks seven closed days so late rows are captured. A separate daily restore
drill downloads every part for the previous closed day, validates object
SHA256 values, parses each Parquet file, and must reproduce the source
fingerprint before updating `archive-restore.json`.

## Aggregate tiers

`clickhouse/rollup_analytics.py` builds hourly, daily, and monthly tables from
raw `FINAL` rows. It never uses an additive materialized view because replayed
outbox events would double-count.

For each partition, the worker:

1. Recomputes into a staging table.
2. Verifies `sum(attempts)` equals the exact source row count.
3. Atomically replaces the live partition.
4. Verifies the published partition again.

The hourly timer rebuilds recent closed hours. The daily timer rebuilds daily
partitions and publishes monthly rows only after a complete calendar month
closes. Backfills are idempotent.

## Deployment

Run each script without `--apply` first. The mutation order is:

```bash
scripts/deploy/clickhouse_reliability.sh --apply
scripts/deploy/clickhouse_resize_disk.sh --apply
scripts/deploy/clickhouse_cluster.sh --apply
scripts/deploy/clickhouse_live_ingestion.sh
scripts/deploy/rollout.sh
```

`clickhouse_live_ingestion.sh` before `rollout.sh` is a runtime dependency,
not a convention. Since 2026-09-05 the control plane's `/status.json` analytics
section on Spanner is built from the heartbeat row the VM poller writes (see
"Public drain-lag signal" below) and publishes `reason: poller_stale` until one
exists. Deploy the VM first, confirm the row landed —
`SELECT body FROM tr_entities WHERE kind='operational_outbox_heartbeat'` returns
one row — and only then roll the control plane. Reversed, gcp's analytics
section flips to `poller_stale` until the first heartbeat lands and
`check_fleet_analytics_freshness` fails, by design: an unobserved lag is not 0.

Bigtable analytics are retired (2026-09-28). `rollout.sh` pins every
control-plane revision to `spanner-clickhouse`; the public and internal
surfaces follow the control plane and refuse an older legacy revision; the
synthetic, trust and backfill jobs run `spanner-clickhouse` as workers (no
analytics reads, so no ClickHouse credentials). The analytics Bigtable
client, mirror writes, read paths, the node repair units (`reconcile`,
`synthetic-reconcile`, `operational-parity`) and the cutover ladder are
deleted from the code (2026-09-29). The table's final `m`/`rollup` export
lives in the ClickHouse archive bucket under `bigtable-final-export/`; the
instance is destroyed through Terraform once that export is verified.

`clickhouse_cluster.sh` stages all Keeper configs before restarts, starts the
two new voters together, migrates only after full-fingerprint parity, pauses
the ingester for the final delta, and exposes the load balancer only after the
canonical replicated table is healthy. Routine control-plane rollouts discover
the internal load-balancer address and the replica addresses dynamically. The
read clients get the load balancer first, then each replica in reverse name
order, and move to the next only when a connection cannot be opened or the
endpoint answers 502, 503 or 504. A failed or empty lookup fails the rollout.

Never restart or deploy all three ClickHouse nodes together. Change one zone,
wait for replica queue and load-balancer health to recover, then continue.

### October 2026 security update

All provisioners pin ClickHouse `26.8.36.3`, the LTS fix for
[CVE-2026-116366](https://clickhouse.com/docs/resources/changelogs/security-changelog).
Updating a provisioner does not upgrade an existing server. Use a separate
rolling maintenance operation, never rerun a provisioning script to patch a node.

1. Verify recovery snapshots and archive evidence before changing a server.
2. Check all three GCP Keeper voters, one leader with two synced followers,
   every replicated table writable, and zero replication delay and queue.
3. Upgrade one non-publisher node's `clickhouse-common-static`,
   `clickhouse-server`, and `clickhouse-client` to the exact pinned version.
   Preserve configuration files. Do not change schema or retention during this work.
4. Reboot that node to activate its already-installed security kernel, then
   require the full quorum, replica catch-up, SQL reads, and healthy load-balancer
   membership before touching the next node. Process the publisher last.
5. AWS and Azure standalone nodes require their own snapshots and separate
   maintenance windows. Their durable outboxes retain analytics during a restart;
   confirm drain recovery and the public status/leaderboard after each cloud.

Stop on any failed gate. A completed package installation is not proof that the
running service or kernel has changed. Confirm `SELECT version()`, `uname -r`,
worker/timer recovery and analytics freshness after reboot. Keep recovery
snapshots until the upgraded fleet is verified. Do not attempt an in-place
database downgrade if startup fails; preserve the disk and use the documented
snapshot/replica recovery procedure.

### Node configuration: `/tr_health` and system-log retention

`scripts/deploy/clickhouse_node_config.sh` installs the files in
`scripts/deploy/clickhouse-node-config/` on tr-clickhouse-3, -2 and then -1:
the `/tr_health` handler and its `tr_health` user (G2 in
`docs/design/clickhouse-high-availability.md`) and the system-log TTLs (G5).
It refuses to restart a node whose replicas or Keeper voter are unhealthy,
waits after each restart until `/tr_health` answers 200 and the voter has
rejoined, and stops at the first failure.

```bash
scripts/deploy/clickhouse_node_config.sh
scripts/deploy/clickhouse_node_config.sh --apply
scripts/deploy/clickhouse_node_config.sh --apply --drop-renamed-logs
```

The first run only reports. The restart renames each changed log table to
`<log>_0`, which keeps the old data, cannot take a TTL and is read-only. The
third command drops those copies on every node to free the disk; run it once
the second has finished cleanly. Check a node by hand with
`curl -s -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8123/tr_health?user=tr_health'`
on it, which must print 200.

## Health checks

Check load-balancer backends:

```bash
gcloud compute backend-services get-health tr-clickhouse-http \
  --project quill-cloud-proxy --region us-central1
```

On every replica, require no queue, no delay, and no read-only replica:

```sql
SYSTEM SYNC REPLICA provider_benchmark_samples;
SELECT queue_size, absolute_delay, is_readonly
FROM system.replicas
WHERE database = 'tr' AND table = 'provider_benchmark_samples';
```

Use a fixed cutoff at least five minutes in the past when comparing live
fingerprints, otherwise concurrent ingestion can make healthy replicas appear
different. Compare this on all three nodes:

```sql
SELECT
  count(),
  sum(cityHash64(tuple(*))),
  groupBitXor(cityHash64(tuple(*)))
FROM provider_benchmark_samples FINAL
WHERE created_at < toDateTime64('<fixed UTC cutoff>', 3, 'UTC');
```

The controlled zone-failure test is:

```bash
scripts/deploy/clickhouse_failover_smoke.sh
scripts/deploy/clickhouse_failover_smoke.sh --apply
```

The apply mode arms a remote automatic restart before stopping one node, runs
20 SQL reads through the load balancer with two zones healthy, restores the
node, waits for 3/3 health, and synchronizes the replica.

### Generation delivery verifiers

`clickhouse.verify_spanner_delivery` compares typed Spanner generations with
`activity_generations FINAL` on GCP. Its scheduled unit is
`tr-clickhouse-spanner-delivery.service`; its JSON history is
`/var/lib/tr-clickhouse-ingest/spanner-delivery.jsonl`.

`clickhouse.verify_postgres_delivery` is the AWS/Azure counterpart. It compares
generation IDs, total cost in microdollars, prompt/completion/cached/reasoning
token counts, model, provider, status, and creation time (within one second).
It reports the same `sampled`, `found`, `missing`, `mismatched`, `duplicated`,
ID samples (at most 20), `mismatch_fields`, and `ok` fields, plus `pending`,
`extra`, and coverage fields. A missing row younger than `--grace-seconds`
(default 300) is pending. Grace uses the later of `created_at` and the entity's
`updated_at`, so a late settle gets time to drain. Extra IDs count only inside
the requested half-open `created_at` window; older ClickHouse history is expected,
and an ID the scan did not see is rechecked by primary key before it counts: a
settle inside the window that landed after the scan passed its ID is reported
as `settled_after_scan`, not as extra.
Exit 0 means a complete, clean check; discrepancies, incomplete coverage, and
query failures exit nonzero. No row values or driver error text are printed.

Postgres generation writes currently leave `indexed_at` NULL. Until a
generation timestamp index is populated, this verifier reads bounded pages
through the `(kind, id)` primary key and filters `created_at` in memory. It
never uses an unindexed JSON timestamp predicate. `--scan-limit` (default
50000) bounds the source rows inspected, plus one key-only continuation probe;
`--limit` (default 5000) bounds the generations compared. A cap produces
`complete: false` and cannot report success. `extra_checked: false` means the
source coverage was incomplete, so ClickHouse rows were not classified as
extra. This is not an indexed day-range scan: narrowing the window alone does
not reduce source history reads. A populated timestamp index is needed for
efficient complete checks of larger histories.

ClickHouse lookups use server-bound IDs in chunks of 500 and the
`by_generation_id` projection, which must be installed/materialized on each
standalone node as part of R0. The extra-ID check reads only the requested
time window with `LIMIT limit + 1`. Every ClickHouse query also has a one-million
row read ceiling and a 30-second execution ceiling; exceeding either fails
the check rather than allowing unbounded work.

Run from a host with private access to both databases, as the `tr_verifier`
Postgres role. That role reads one view, `tr_verifier_generations`:
`tr_entities` filtered to `kind = 'generation'` and projected to the compared
fields. It has no privilege on `tr_entities` itself, which holds every entity
kind, and none on any other table. The drain's outbox-only role cannot read
the view. The view and the role are defined in
`scripts/deploy/aws_eu_tr_verifier_role.sql`. Connection handling is reused
from the Postgres drain: DSQL mints an IAM token for the configured role, and
Azure uses libpq's `PGPASSWORD` or password file. Keep passwords in the
protected environment or password file, never in the DSN or command arguments.
The checkout's drain uses `clickhouse-client`; this verifier reuses the
operational analytics HTTP reader with the drain's `CH_PASSWORD`, `_USER`, and
`_DATABASE` settings. `TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD` takes
precedence for a separate read credential. Set `_URL` to one node's HTTP
endpoint (default localhost:8123), and check each durable copy separately with
its own credentials and history file.

#### AWS: installed on tr-eu-clickhouse-1

`scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh` installs the
verifier. It is separate from the drain installer on purpose: the verifier is
optional, so its prerequisites must never make a drain install fail. It runs
from the drain's `/opt/tr-clickhouse` tree, virtualenv and environment file.
It ships no code and refuses if that tree is missing or cannot import the
verifier. The drain installer keeps the verifier's `httpx` dependency, stops
the verifier's timer before swapping the tree, and starts the timer again if
it was enabled. It does not import the verifier, so a verifier defect cannot
fail a drain install. Its summary names the verifier script as the next step,
and it never runs it.

| What | Where |
| --- | --- |
| Service (oneshot, user `tr-clickhouse-ingest`) | `tr-clickhouse-postgres-delivery.service` |
| Timer (`OnCalendar=hourly`, `Persistent=true`, 24 h lookback) | `tr-clickhouse-postgres-delivery.timer` |
| DSN override, `user=tr_verifier`, no secret | `/etc/tr-clickhouse-postgres-delivery.env` |
| IAM auth, region, ClickHouse credentials | the drain's `/etc/tr-clickhouse-ingest-postgres.env` |
| History, one JSON line per completed check, 16 days kept | `/var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl` |

The unit's arguments are `--lookback-hours 24 --scan-limit 1000000
--history-file <history>`. None of them is secret. `--limit` keeps its default
of 5000.

Both caps must stay above what they bound, or every run reports
`complete: false` and exits 1. That failure is about coverage. It is not a
delivery gap.

- `--scan-limit` must exceed the plane's total number of generation entities,
  not just one day's. Until a generation timestamp index exists, the source
  scan walks all generation history through the primary key (see above).
- `--limit` must exceed the number of generations created in one 24 h window.

ClickHouse adds a third ceiling. Every verifier query may read at most
1,000,000 rows. The window query (`fetch_window`) reads `activity_generations
FINAL` for the 24 h window, and that read is pruned only by the table's
monthly partition. So once one month of `activity_generations` holds more than
1,000,000 rows, every run fails loudly with a query error, not silently.

To check them, read the coverage fields of the last history line:

```bash
tail -1 /var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["complete"], r["scanned"], r["sampled"])'
```

- With `complete: true`, `scanned` is the plane's total generation-entity
  count and `sampled` is the window's. Raise `--scan-limit` in the unit well
  before `scanned` approaches it.
- With `complete: false` and `sampled` equal to `--limit`, the window cap was
  hit.
- With `complete: false` and `scanned` equal to `--scan-limit`, the scan cap
  was hit.

Where to read results:

- `tail -1 /var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl` shows the
  last check. `"ok": true` is clean.
- `systemctl show tr-clickhouse-postgres-delivery.service -p Result,ExecMainStatus`
  shows the last run's exit status. `0` is clean and `1` is a discrepancy,
  incomplete coverage, or a query failure. A failed run leaves the unit
  `failed` until the next one.
- `journalctl -u tr-clickhouse-postgres-delivery.service -o cat` holds the same
  summary line per run. A query failure writes no history line. It prints only
  `{"ok": false, "error": "<exception class>"}` here.

Operator order, first install:

1. Install the drain from a commit that carries the verifier:
   `bash scripts/deploy/aws_eu_clickhouse_drain_install.sh`, or the
   `drain-install` mode of `deploy-aws-control-plane.yml`. This step does not
   depend on any of the steps below.
2. The node role needs `dsql:DbConnect` on the cluster (not `DbConnectAdmin`).
   It already has that grant for `tr_drain`: statement `DsqlOutboxDrain` in
   the inline policy that `scripts/deploy/aws_eu_clickhouse.sh` puts. The node
   role is managed by that script, not by Terraform. IAM cannot narrow
   `DbConnect` to one Postgres role. The `AWS IAM GRANT` mapping in the SQL is
   the per-role boundary.
3. As the DSQL admin, run the reviewed SQL once. It can be re-run safely.
   A re-run also repairs a drifted view: the view is dropped and created again,
   because `CREATE OR REPLACE` cannot narrow it, and the grant is reapplied.

   ```bash
   PGPASSWORD="$(aws dsql generate-db-connect-admin-auth-token \
       --region eu-west-3 --hostname <cluster>.dsql.eu-west-3.on.aws)" \
   psql "host=<cluster>.dsql.eu-west-3.on.aws port=5432 dbname=postgres user=admin sslmode=require" \
     -v node_role_arn=arn:aws:iam::<account>:role/<node role> \
     -f scripts/deploy/aws_eu_tr_verifier_role.sql
   ```

   The verifier installer prints this command with the real values filled in,
   followed by the SQL, when it finds the role missing.
4. Run `bash scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh`. It
   does these things in order:
   1. It checks `dsql:DbConnect`.
   2. It checks that the drain tree is present and imports the verifier.
   3. It connects from the node as `tr_verifier`, passing the DSN directly, so
      nothing is written before this check. The role must satisfy all of:
      - it is `tr_verifier` and is a member of no role;
      - it holds SELECT on `tr_verifier_generations` and no other privilege
        on it;
      - the view's columns and its `pg_get_viewdef` definition match the
        reviewed SQL, which reads `tr_entities` filtered to
        `kind = 'generation'`. The comparison ignores the `::text` casts that
        Postgres adds when it stores a definition;
      - it holds no table or column privilege of any kind on any other
        relation. This check ignores `pg_catalog`, `information_schema`, and
        the schemas that hold AWS or system metadata rather than tr data:
        DSQL's `sys`, `pg_toast`, and the per-session `pg_temp*` schemas.

      The check ends with one of three results:
      - **Reviewed:** the install continues.
      - **Problems found:** each problem is listed by role or relation name.
      - **Could not check:** a connection, token or throttling error, or a
        role or view that does not exist yet. Only the exception class is
        reported.
   4. It writes the DSN override.
   5. It installs and enables the timer.
   6. It runs the verifier once. If an hourly run is already in progress, it
      waits for that run to finish first. It then requires exit 0,
      `Result=success`, and a history line from this run with `"ok": true`.

   Each SSM step is polled until it finishes, with a 45-minute deadline. A
   poll that fails with `InvocationDoesNotExist` is retried, because the
   command is not registered yet. Any other poll error, such as AccessDenied
   or expired credentials, stops the script at once and names the error class.
   The script exits non-zero at the first failure.

   - On a first install, a failure in steps 1 to 3 writes nothing on the node.
   - On a refresh, files and units from the earlier install stay where they
     are.
   - If the scope check finds problems, it also runs `systemctl disable --now`
     on the timer, so an over-broad role stops being used. If disabling fails,
     the script says so, still prints the operator step, and exits non-zero.
   - If the check could not run, nothing changes and an installed timer keeps
     running, because a transient error is not evidence that the role is
     wrong. The script still prints the operator step, since a missing role or
     view looks like this on a first install.
   - The timer stays enabled after a failed first run, so a real discrepancy
     keeps being recorded every hour.

#### Azure: not installed yet (follow-up)

Nothing deploys the verifier on Azure. That follow-up needs the
`tr_verifier_generations` view and a password-authenticated `tr_verifier`
role on Azure Postgres, a password file or protected environment for it, and
a unit with its own history file. A manual run, with the verifier's Postgres
password and the node's ClickHouse environment already injected:

```bash
export TR_POSTGRES_DSN='host=<postgres-host> port=5432 user=tr_verifier dbname=trustedrouter sslmode=require'
export TR_POSTGRES_IAM_AUTH=''
PYTHONPATH=src python3 -m clickhouse.verify_postgres_delivery \
  --lookback-hours 24 --grace-seconds 300 --limit 5000 --scan-limit 1000000 \
  --history-file /var/lib/tr-clickhouse-ingest/postgres-delivery-azure.jsonl
```

Use `--start 2026-10-18T00:00:00Z --end 2026-10-19T00:00:00Z` for a fixed
window. The verifier does not repair records, delete outbox rows, or change
settlement writes.

### Repairing missing Postgres activity

`clickhouse.repair_postgres_activity` is the Postgres counterpart of the
Spanner-only `trusted_router.activity_delivery_repair_cli`. It finds the
generation entities that ClickHouse `activity_generations` does not hold and
inserts each one's activity row into `tr_operational_analytics_outbox`. The
drain then delivers them like any other row, to every configured node.

- **What it repairs.** Either a `created_at` window (`--start`, inclusive, and
  `--end`, exclusive), or the ids given with `--generation-id` (repeatable).
  Discovery uses the delivery verifier's own readers, so "missing" means what
  the verifier means, including `--grace-seconds` (default 300): a row still
  inside the grace counts as `pending` and is left for the drain. `--scan-limit`
  (default 1,000,000) bounds the primary-key scan, as in the verifier. If it is
  hit, `complete` is false and the run exits 1.
- **What it writes.** It calls the settle path's own
  `PostgresOperationalAnalyticsOutbox.enqueue_activity_tx` on the stored
  `Generation`. So the payload, shard and stable event id are the ones settle
  writes, under the same `ON CONFLICT DO NOTHING`. Re-running it while a row is
  still queued is a no-op. After the drain has taken a row, a re-run delivers it
  again, and `ReplacingMergeTree` collapses the copy. The tool writes outbox
  rows and nothing else. It never deletes anything, and it never touches
  settlement, reservation or balance rows.
- **Entities older than a field.** A stored body decodes exactly as
  `PostgresStore` reads it, and a missing key takes the `Generation` default.
  The August 2026 entities predate the client and `synthetic` fields
  (2026-08-17), `app_id` (2026-08-26), and the markup amounts (2026-08-27 and
  2026-09-02). They get `app_id` null, both markups 0, `synthetic` false and
  null client fields. That is what those generations were: no registered app,
  no markup charged, no client context recorded. Nothing is invented.
- **Safety.** Dry run is the default and writes nothing. `--apply` writes.
  `--limit` (default 500) bounds one run. When more remain, the output carries
  `next_after_id` and `resume`; run the same command again with
  `--after-id <next_after_id>`. Output is one JSON line of counts and at most 20
  ids (`selected_ids`, `not_found_ids`). It never prints row values, payloads
  or driver error text. A failure prints only `{"ok": false, "error": "<class>"}`.
  It exits 0 only when coverage was complete and every selected id was found.

**Privileges: operator-run, as the DSQL admin.** The repair must read whole
generation entities from `tr_entities` and INSERT into the outbox. No scoped
role has both: `tr_drain` holds SELECT and DELETE on the outbox, and
`tr_verifier` holds SELECT on a narrow view. Creating a standing role that can
read every generation body, for a one-off, would widen the plane for good. So
an operator runs the repair as `admin`, the role the control plane already uses
to write these rows at settle. The admin token is minted in the process from
the operator's own AWS identity, which needs `dsql:DbConnectAdmin`. It is never
printed and never stored on disk. The run happens on the operator's machine,
not on the ClickHouse node: that node must not hold an admin-equivalent
credential (see the drain module's docstring). ClickHouse is reached through an
SSM port forward, using the drain's `default` credential. The read-only
`tr_control_read` profile runs with `readonly=1`, which rejects the
`max_rows_to_read` and `max_execution_time` bounds every verifier query sets.
The repair sends ClickHouse only those bounded SELECTs. The Spanner tool is
likewise operator-run with the application's credentials.

AWS Paris, from a checkout of a commit that carries the tool, after
`uv sync --frozen`. You need the AWS CLI with the Session Manager plugin.
`<cluster>` is `CLUSTER_ID` in `scripts/deploy/aws_eu_clickhouse_drain_install.sh`.

```bash
# Terminal 1: forward the Paris node's ClickHouse HTTP port to localhost:18123.
INSTANCE_ID="$(aws ec2 describe-instances --region eu-west-3 \
  --filters Name=tag:Name,Values=tr-eu-clickhouse-1 Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
aws ssm start-session --region eu-west-3 --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8123"],"localPortNumber":["18123"]}'

# Terminal 2: the environment. No secret is put in argv or on disk.
export TR_POSTGRES_DSN='host=<cluster>.dsql.eu-west-3.on.aws port=5432 user=admin dbname=postgres sslmode=require'
export TR_POSTGRES_IAM_AUTH=aws-dsql TR_POSTGRES_IAM_REGION=eu-west-3
export TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=http://127.0.0.1:18123
export TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=default TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=default
export TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD="$(aws secretsmanager get-secret-value \
  --region eu-west-3 --secret-id quill/tr-eu-clickhouse-password --query SecretString --output text)"

# Use the same window as the verifier run that counted the gap (here the
# plane's whole history), so the dry run's "missing" equals that count before
# anything is applied; a narrower window can leave ids outside it.
# Dry run: counts and the first ids. Writes nothing.
PYTHONPATH=src uv run python -m clickhouse.repair_postgres_activity \
  --start 2026-06-01T00:00:00Z --end <the verifier run's end, UTC>

# Apply, at most --limit per run. Repeat with --after-id <next_after_id> while
# the output names one.
PYTHONPATH=src uv run python -m clickhouse.repair_postgres_activity \
  --start 2026-06-01T00:00:00Z --end <the verifier run's end, UTC> --apply

unset TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD
```

Each run scans all generation history through the primary key, as the
verifier does, and checks every in-window id against ClickHouse.

**Confirm.** After the apply, wait until the drain has emptied the outbox
(`analytics.drain_lag_seconds` back to its usual level), then run the dry run
again: `missing` must be 0. The proof is a clean wide-window verifier run on
the node, in the same environment as the timer, covering the whole repaired
window:

```bash
sudo systemd-run --pipe --wait --quiet -p User=tr-clickhouse-ingest \
  -p WorkingDirectory=/opt/tr-clickhouse \
  -p EnvironmentFile=/etc/tr-clickhouse-ingest-postgres.env \
  -p EnvironmentFile=/etc/tr-clickhouse-postgres-delivery.env \
  /opt/tr-clickhouse/venv/bin/python -m clickhouse.verify_postgres_delivery \
  --start 2026-08-01T00:00:00Z --end <now, UTC> --limit 1000000 --scan-limit 1000000 \
  --history-file /var/lib/tr-clickhouse-ingest/postgres-delivery-repair.jsonl
```

It must print `"ok": true` with `complete: true` and `missing`, `extra`,
`mismatched` and `duplicated` all 0. `--limit` has to exceed the number of
generations in the window, or the run reports `complete: false`. The
ClickHouse read ceiling of 1,000,000 rows per query applies, as described
above. The repair and the verifier each check one node. Check the Stockholm
copy separately, as the verifier section says. If Stockholm lacks rows that
Paris holds, run the repair with the port forward pointed at the Stockholm node.

## Restore procedures

### Replace one failed replica

1. Remove or drain only the failed backend. Keep two healthy nodes serving.
2. Preserve the failed disk for investigation.
3. Provision a replacement VM in the same zone with the same private identity.
4. Install the cluster config and canonical replicated table.
5. Let ClickHouse fetch parts from a healthy replica.
6. Require queue zero, fixed-cutoff fingerprint parity, and healthy load-balancer state.
7. Re-enable the backend.

### Move the workers to another node

Two hosts must never publish at once: a rollup that replaces a partition can
overwrite fresher results with older ones. The steps and their reasons are G1
in `docs/design/clickhouse-high-availability.md`. Run each command without
`--apply` first; it prints its plan.

```bash
# Once: set the roles (node 1 publisher) and install the fence on every node.
scripts/deploy/clickhouse_worker_role.sh fence --apply
# Once per standby, and after each worker change: install its units disabled.
scripts/deploy/clickhouse_worker_role.sh standby --node tr-clickhouse-2 --apply
# Takeover. Add --from-unreachable when the publisher cannot be reached; the
# script then stops its VM and requires TERMINATED before continuing.
scripts/deploy/clickhouse_worker_role.sh takeover --to tr-clickhouse-2 --apply
```

The takeover fences the old publisher (role `standby`, then its workers
disabled, or its VM stopped), kills the worker user's queries on every
reachable replica, waits until the new publisher has pulled and applied every
replication log entry, and only then makes it the publisher and starts its
workers. If the sync barrier fails because only the old publisher holds a part,
stop: recover that node or its disk first. Then confirm the drain lag on
`/status.json` recovers. A returning node stays `standby`; failback is the same
takeover in the other direction.

### Restore from a disk snapshot

1. Create a new disk from the newest valid `tr-clickhouse-daily-snapshots` snapshot.
2. Attach it to an isolated replacement VM with no external IP.
3. Start ClickHouse without registering it in the load balancer.
4. Verify table metadata and a fixed-cutoff fingerprint against a healthy replica.
5. Rejoin it as a replica, synchronize, then add it to the backend group.

### Restore from Parquet

1. Read each day's `_latest.json` and its immutable manifest.
2. Verify every object's SHA256 and the manifest's row-set fingerprint.
3. Load parts into a new staging table with the current schema.
4. Compare per-day fingerprints with the manifests.
5. Replicate the staged data, verify all replicas, then rename it into service.
6. Replay the remaining Spanner outbox and run reconciliation.

Parquet is the cross-version and cross-cloud recovery source. Disk snapshots
are the faster same-platform recovery source.

## Drain freshness: the out-of-band signal

Every in-band signal for the outbox drain — the metrics line, `degraded_targets=`,
and the `backlog_alarm` that is the only bound on outbox growth — is emitted by
the drain process itself. A drain that was never installed cannot alarm about
not existing. That is not hypothetical: on AWS-EU no unit had ever been
installed, 470,370 rows accumulated in `tr_operational_analytics_outbox` between
2026-08-02 and 2026-08-17, and nothing reported it. GCP was healthy throughout,
so the fleet looked healthy.

So every control plane publishes the signal itself, in its already-public
`/status.json`:

```json
"analytics": {
  "available": true,
  "backend": "postgres",
  "drain_lag_seconds": 12.5,
  "outbox_depth": null,
  "generated_at": "2026-08-17T12:00:00Z"
}
```

Those five keys are the whole contract, pinned by a test. The oldest row's own
timestamp is deliberately not among them: nothing read it, and it is
`generated_at` minus the lag in any case.

`drain_lag_seconds` is the age of the oldest **undelivered** outbox row. Rows
are deleted only after every configured ClickHouse target has accepted them, so
this is an end-to-end statement about the whole pipeline, and it is observable
from outside the VPC — which the private ClickHouse nodes are not. A read
failure publishes `{"available": false, "reason": ...}`; the key is never
omitted and a stale number is never re-served.

Cost: one seek per status-cache miss. Postgres/DSQL uses
`tr_operational_analytics_outbox_enqueued_at_idx`. Spanner does not read the
outbox at all: its per-shard head read walks the deleted-row versions the drain
leaves behind (823-957 ms CPU per call, measured 2026-09-05), so the VM poller
publishes a heartbeat row into `tr_entities` after each pass — the oldest live
`commit_ts` it saw, floored by its committed deletes — and the control plane
point-reads that row, publishing `reason: poller_stale` when it is absent or
when its Spanner commit time (`updated_at`, not the poller's own `observed_at`)
is older than 180 s, rather than scanning. A dead poller therefore shows on
`/status.json` within 180 s plus the page's 60 s per-process analytics cache.
The heartbeat needs no DDL, but it does fix the rollout order (VM poller first;
see Deployment above). `outbox_depth` is deliberately
optional — `count(*)` over a large backlog is the expensive question and the
lag already answers the important one.

`.github/workflows/check-analytics-freshness.yml` reads it with no credentials
for **every** cloud in
`src/trusted_router/operational_analytics_fleet.py:ANALYTICS_FRESHNESS_FLEET`.
`tests/test_analytics_freshness_registry.py` fails if that registry disagrees,
in either direction, with the union of every table in this repo that declares a
deployment (`deployment_sources()`: the BYOK attestation tables,
`regions.MULTICLOUD_REGION_GEO`, the `external_live_regions` /
`marketing_regions` settings, and the `synthetic_fleet_peers` list every cloud
already polls), so a fourth deployment cannot exist without a drain-freshness
signal or a written reason it has none — whichever of those tables it lands in
first. Each source must also be non-empty, since a union over an empty source
is satisfied by anything. Missing section, unavailable, stale, unreachable,
over-lag, a plane answering with the wrong storage backend, and a run that
measured no cloud at all are all failures — never skips.

Values read back off a remote page (`reason`, `backend`) are narrowed through
the publisher's own vocabulary before they are printed. The problems file is
pasted verbatim into a public GitHub issue, so an unnarrowed value would let
whatever answered choose text in an issue in this repository.

**It ships with `workflow_dispatch` as its only trigger, on purpose.**
Publishing the field in this repo is not the same as serving it: merging main
auto-deploys the GCP control plane only, while AWS-EU and Azure are hand-run
scripts. A cron enabled before those deploys land files an issue every morning
about clouds nobody redeployed, and a check that cries wolf is a check people
learn to ignore (the same failure the client-telemetry check's
`CANARY_COUNT_GATE_FROM` ramp-up guard exists to avoid). A `push:` trigger is
the same hazard with a shorter fuse — it fires on the merge that lands the
publisher, when no plane serves the section yet, and opens that issue an hour
after merge instead of a morning after. Deploy all three, confirm each
`/status.json` returns an `analytics` object, run the job once by
`workflow_dispatch`, then enable both triggers in the one commit the workflow
header spells out.

Two registry states are neither pass nor fail, and are printed as `(unchecked)`
on every run rather than skipped: a cloud with no public status page
(`reason=`), and a cloud deliberately declared to run no outbox
(`expects_outbox=False`). All deployed clouds currently expect an outbox,
including Azure; Azure's deploy refuses to replace that live pipeline with an
outbox-off revision when discovery fails. A future outbox-free entry becomes a
**failure** the day it publishes a real lag, which is the day it needs watching.

To ask about one cloud during an incident:

```bash
PYTHONPATH=src python3 -m clickhouse.check_fleet_analytics_freshness --cloud aws
```

## Alerts and capacity

Cloud Monitoring pages on node unavailability and disk use at 75 percent.
The ClickHouse server, archive and restore timers, Spanner delivery verifier,
rollup timers, and ingester must also be checked during incident response.
Alert delivery uses the verified TrustedRouter infrastructure notification
channel.

At 75 percent disk use, do not merely expand forever. Measure rows per second,
compressed bytes per row, parts, merge backlog, query latency, Keeper latency,
and archive lag. The current topology scales reads across three replicas but
has one logical write shard. Before sustained load approaches one shard's
tested limit:

1. Add time-first projections or purpose-built rollups for expensive scans.
2. Benchmark the expected row rate with the real schema and query mix.
3. Add shards with three replicas per shard and route inserts by stable event hash.
4. Keep provider/model queries distributed so a hot model cannot own one shard.
5. Validate rebalancing and restore procedures before raising production traffic.

The GCS Parquet archive separates long-term history from hot disk capacity.
It can later be queried with ClickHouse, BigQuery external tables, or DuckDB
without expanding the production cluster just to retain old rows.
