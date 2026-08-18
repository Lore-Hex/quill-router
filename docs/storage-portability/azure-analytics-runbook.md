# Azure operational analytics: the operator's runbook

**Status:** written 2026-08-18, alongside the scripts. **Nothing in it has been
executed.** Every command below is what the code does; the Azure ARM control
plane was unreachable from the machine that wrote this (an expired session, not
a permission), so no resource named here has been observed to exist. Read the
"what working looks like" line at each stage and believe the output, not this
page.

This is the ordered procedure for giving the Azure cloud an
operational-analytics pipeline: two ClickHouse nodes, a scoped database role, a
drain that writes both copies before it deletes anything, and — LAST — the flag
that starts producing rows.

## Why the order is the whole document

From 2026-08-02 to 2026-08-17 the AWS-EU cloud ran with the producer on and the
consumer missing. 470,897 rows piled into the outbox, `SELECT count() FROM
activity_generations` returned 0, and every status page was green. Nothing
reported it, because the alarm that bounds outbox growth is emitted BY the drain
that did not exist.

Enabling `TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED` before stages 1–5 are done
reproduces that on purpose. It is the last stage here for exactly that reason,
and `azure_control_plane.sh` computes the flag from whether a ClickHouse target
exists rather than taking it from you.

## What the finished thing is

| | |
|---|---|
| uaenorth (Dubai) | `tr-azure-clickhouse-uaenorth` in `vnet-prod`, and the drain runs here |
| southeastasia (Singapore) | `tr-azure-clickhouse-southeastasia` in its own VNet, joined by global VNet peering |
| What each holds | **The same rows.** This is a durability replica, not a residency split |
| What crosses a cloud boundary | Nothing. Azure rows stay in Azure; no row goes to GCP or AWS, and none arrives from them |
| Cost | ≈ $270/month: two `Standard_E2s_v5` ($113.15/mo each at uaenorth prices read 2026-08-18), two 128 GiB Premium SSDs ($21.50 each), two public IPs (~$3.65 each), plus $0.25/GB of peering traffic — one copy of each drained batch, both sides billed |

One drain, not two. Two drains against one outbox would each DELETE rows the
other had not yet written, and every row would land on exactly one node — which
looks like replication and is the precise opposite of it.

---

## Stage 0 — before you start

```bash
az login --tenant 2abe2fae-5c28-491d-af5a-6255b33e534e
az account show --query '{name:name, id:id}' -o json
az group list -o table
```

**Working:** the subscription is `2fc83893-ca6c-48e4-b090-8860fba33d33` and
`az group list` returns rows.

**Not working:** `AADSTS50132: The session is not valid due the following
reasons: password expiration or recent password change`. The cached profile
answers `az account show` from disk and every ARM call fails. Re-run `az login`;
there is no non-interactive path in this subscription — no service principal
exists.

While you are here, take the readings this plan was written without, because
three of its defaults are inferences:

```bash
az network vnet show -g tr-azure -n vnet-prod \
  --query '{cidrs:addressSpace.addressPrefixes, subnets:subnets[].{n:name,p:addressPrefix}}' -o json
az containerapp show -g tr-azure -n tr-azure-vnet --query 'properties.template.containers[0].env' -o json
az network private-dns zone list -o table
az vm list-skus -l uaenorth --size Standard_E --all -o table
az vm list-skus -l southeastasia --size Standard_E --all -o table
```

If `10.61.3.0/24` is taken, set `PRIMARY_SUBNET_CIDR` — the script checks and
refuses rather than colliding. If `Standard_E2s_v5` is not offered in
southeastasia, set `VM_SIZE` for that invocation; the two nodes do not have to
match.

---

## Stage 1 — the uaenorth node

```bash
bash scripts/deploy/azure_clickhouse.sh uaenorth
```

Creates the subnet and its NSG, a user-assigned identity, a Key Vault with the
ClickHouse password and the drain's future Postgres password, the VM, and then
applies `clickhouse/006` + `clickhouse/009` and counts the tables.

**Working:** the report block prints a private address and `tables the drain
writes, present on this node: 8`, and the script then exits **5** with `NOT YET
OBSERVABLE`. Exit 5 is the expected answer at this stage — no control plane
publishes an `analytics` section yet — and the script is not permitted to
pretend otherwise by exiting 0.

**Not working:**

* `FATAL: no VNet 'vnet-prod'` — wrong subscription or the network was renamed.
  The script will not create production networking on a typo.
* `subnet 10.61.3.0/24 overlaps existing subnets [...]` — pick a free range with
  `PRIMARY_SUBNET_CIDR`.
* `remote step did not complete: apply and verify the schema` — cloud-init has
  not finished, or it failed. `az vm run-command invoke -g tr-azure -n
  tr-azure-clickhouse-uaenorth --command-id RunShellScript --scripts "tail -50
  /var/log/cloud-init-output.log"`. The usual causes are the identity not yet
  having Key Vault read (it retries for five minutes, then gives up) and apt
  being unable to reach `packages.clickhouse.com`, which means the node has no
  egress.
* ClickHouse is running but refuses the password — check ownership of
  `/etc/clickhouse-server/users.d/default-password.xml`. It must be
  `clickhouse:clickhouse`; a root-owned file is unreadable to the server after
  it drops privileges and the process dies in `UsersConfigAccessStorage::load`
  with a stack trace that never names the permission.

## Stage 2 — the southeastasia node and the peering

```bash
bash scripts/deploy/azure_clickhouse.sh southeastasia
```

Same shape, plus: its own resource group, VNet and vault; global VNet peering
created **from both ends**; and a grant letting the uaenorth node's identity
read this region's ClickHouse password.

**Working:** `peering peer-to-uaenorth: Connected` and `peering
peer-to-southeastasia: Connected`, both printed. Then exit 5 again.

**Not working:**

* `peering ... is 'Initiated', not Connected` — only one half exists. A
  one-sided peering drops every packet while both resources look healthy. Let
  the script create the other half, or delete both and re-run.
* `no identity 'tr-azure-analytics-uaenorth-id'` — you ran this before stage 1.
  Order matters here and nowhere else.
* `PEER_VNET_CIDR ... overlaps the primary VNet` — global peering cannot join
  overlapping address spaces at all.

## Stage 3 — the scoped Postgres role

The drain must **not** log in as `tradmin`. `tradmin` can read `tr_entities`,
which holds raw member emails and workspace ids — the identifiers
`analytics_surrogate()` exists to keep off the analytics host. The role it uses
gets `SELECT, DELETE` on one table and nothing else.

The password already exists, in Key Vault, generated by stage 1 and never read
by it. Pipe it into `psql`; do not paste it, do not put it in a file, and do not
pass it as `-v drain_password=...` (that is argv, and argv is readable by every
local user through `ps`).

```bash
MYIP=$(curl -fsS https://api.ipify.org)
az postgres flexible-server firewall-rule create -g tr-azure -s tr-azure-pg \
  --name tmp-drain-role --start-ip-address "$MYIP" --end-ip-address "$MYIP"

{
  printf "\\set drain_password '"
  az keyvault secret show --vault-name tr-azure-analytics-kv \
    -n drain-postgres-password --query value -o tsv | tr -d '\n'
  printf "'\n"
  cat scripts/deploy/sql/azure_operational_outbox_drain_role.sql
} | psql "host=tr-azure-pg.postgres.database.azure.com port=5432 \
          user=tradmin dbname=trustedrouter sslmode=require" \
      -v ON_ERROR_STOP=1 -f -

az postgres flexible-server firewall-rule delete -g tr-azure -s tr-azure-pg \
  --name tmp-drain-role --yes
```

**Working:** the last statement prints one row with `can_select_outbox = t`,
`can_delete_outbox = t`, `can_read_entities_MUST_BE_FALSE = f`, and the three
role attributes false.

**Not working:**

* `psql: could not connect` — the temporary firewall rule takes a few seconds,
  and `tr-azure-pg` has no other public rule. Wait, retry, and remember to
  delete the rule afterwards whatever happens. A temporary widening that is
  skipped on the failure path is how it becomes permanent.
* `can_delete_outbox = f` — the quietest possible failure if you leave it:
  every row is delivered, every metric reads healthy, and the outbox grows
  forever because nothing removes anything. Stage 4 refuses to install for
  exactly this.
* `can_read_entities_MUST_BE_FALSE = t` — the role inherited a broad grant.
  Find it (`\dp tr_entities`) and remove it; do not proceed.

## Stage 4 — the drain

```bash
bash scripts/deploy/azure_clickhouse_drain_install.sh
```

Preflights the private DNS path and the second node, ships the repo's current
code in ~15 chunks, builds a venv, imports it in staging before swapping,
writes `/etc/tr-clickhouse-ingest-postgres.env` from secrets **the node
fetches**, proves the role is scoped, installs the unit, and prints the journal
and two row counts.

**Working:** `copies=2 degraded_targets=-` in the metrics line, and both
ClickHouse counts printed. They will be **0**, and that is correct — nothing is
being enqueued yet. Then exit 5.

**Not working:**

* `no private DNS zone 'privatelink.postgres.database.azure.com'`, or a zone not
  linked to `vnet-prod` — the node would resolve the *public* address of a
  server with zero firewall rules and time out every connection while the unit
  reported itself active. Create and link the zone; this is the single most
  likely blocker in the whole runbook and the reason it is checked first.
* `cannot find the southeastasia node` — stage 2 has not run. If one copy is
  genuinely what you want, say so out loud: `REQUIRE_REPLICA=0`. A silent
  single-target drain deletes rows the second node never received and logs
  `copies=1`, identical to a deliberate one-node deployment.
* `the drain role can read tr_entities` — go back to stage 3.
* the unit is `failed` with status **78** — `CONFIG_EXIT_CODE`. The environment
  file is wrong and `RestartPreventExitStatus` stopped it deliberately rather
  than crash-loop every five seconds while the outbox grew.
  `journalctl -u tr-clickhouse-operational-ingest-postgres | grep config_invalid`
  names the variable. A `*_REPLICA*` name this drain does not recognise is
  refused rather than ignored, because an ignored replica setting looks exactly
  like a working two-copy deployment.
* rollback: `systemctl disable --now tr-clickhouse-operational-ingest-postgres`
  and `mv /opt/tr-clickhouse.previous /opt/tr-clickhouse`. Nothing is lost by
  stopping the drain — undelivered rows stay in the outbox.

## Stage 5 — watch it do nothing, on purpose

Before turning the producer on, confirm the consumer is alive and idle:

```bash
az vm run-command invoke -g tr-azure -n tr-azure-clickhouse-uaenorth \
  --command-id RunShellScript \
  --scripts "journalctl -u tr-clickhouse-operational-ingest-postgres -n 30 --no-pager"
```

**Working:** a metrics line every couple of seconds with `rows=0`,
`drain_lag_seconds=-1.000` or `0.000`, `copies=2`, `degraded_targets=-`.

**Not working:** no metrics lines at all means the loop is not running; a
`degraded_targets=southeastasia` before any traffic means the peering or that
node's NSG is wrong, and it is far cheaper to fix now than under load.

## Stage 6 — the flag, and only now

```bash
bash scripts/deploy/azure_control_plane.sh
```

There is nothing to edit. The script finds the node's private address and the
vault secret, and sets `TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true` because
they exist. If it prints `no ClickHouse target ... analytics disabled for this
service`, stop: a stage above did not do what you think it did.

**Working:** `analytics ON: outbox enabled, ClickHouse at http://10.61.x.x:8123`,
the deploy completes, and the app comes up on a new revision **by digest** (a
mutable tag would let Container Apps re-use the old image with the new
environment).

**Not working:** if the deploy succeeds and the outbox line said `false`, you
have just deployed a control plane that enqueues nothing. That is safe, and it
is not done.

## Stage 7 — prove rows move

Two numbers, ten minutes apart. No status page substitutes for this.

```bash
az vm run-command invoke -g tr-azure -n tr-azure-clickhouse-uaenorth \
  --command-id RunShellScript --scripts \
  'set -a; . /etc/tr-clickhouse-ingest-postgres.env; set +a;
   CLICKHOUSE_PASSWORD="$CH_PASSWORD" clickhouse-client --user default --database default \
     --query "SELECT count() FROM activity_generations";
   CLICKHOUSE_PASSWORD="$CH_REPLICA_PASSWORD" clickhouse-client \
     --host "$TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST" \
     --user default --database default \
     --query "SELECT count() FROM activity_generations"'
```

**Working:** both numbers non-zero, both rising, and close to each other.

**Not working:** uaenorth rising and southeastasia flat means the fan-out is
failing and — importantly — nothing is being deleted, so the outbox is
absorbing the backlog rather than losing it. Look for `degraded_targets=` and
`backlog_alarm` in the journal. The two supported responses are to restore the
second node, or to remove its `_REPLICA_` variables and restart, after which
that node is permanently behind and needs an out-of-band backfill.

## Stage 8 — from outside

```bash
bash scripts/deploy/verify_cloud_complete.sh azure
curl -s https://azure.trustedrouter.com/status.json | jq .data.analytics
```

**Working:** `VERIFIED — azure passed every stage`, and the `analytics` object
carries `available: true` with a `drain_lag_seconds` under the bound the drain
itself alarms on.

**Not working:**

* exit **5**, `NOT YET OBSERVABLE` — the deployed revision predates stage 6, or
  the deploy did not take. Check that the running revision is the digest that
  was just pushed.
* exit **1** with stage (c) failing — `analytics.available` is false. The
  control plane could not read its own outbox; that is a database problem, not a
  drain problem.
* exit **1** with stage (d) failing — the lag is past the bound. The drain is
  behind or dead. Go back to stage 5's journal.

## Stage 9 — start watching it

`src/trusted_router/operational_analytics_fleet.py` already carries
`expects_outbox=True` for Azure: it landed in the commit that built this
pipeline, which means the fleet check FAILS for Azure from that commit until
stage 6 has actually run. That is deliberate — the alternative is an entry that
records an expected absence while a pipeline sits half-built — but it does mean
the failure is load-bearing information rather than noise, and it should be red
for hours, not weeks.

Once all three clouds publish a live lag, the last thing to do is give
`.github/workflows/check-analytics-freshness.yml` a `schedule:` trigger. Today
it ships with `workflow_dispatch` as its only trigger, deliberately and in its
own header, so *nothing is watching any of this on a schedule* — including the
cloud you just finished.

## What this pipeline still cannot tell you

* Nothing here proves a row is *correct*, only that it arrived.
* The rollup and snapshot jobs (`synthetic_status_rollups`,
  `client_availability_rollups`, `public_analytics_snapshots`) are GCP timers.
  On Azure nothing writes them, so both nodes hold those tables empty and the
  second node is a complete copy of what the drain writes — not of everything
  the schema can hold.
* Stage (e) of the gate is a static read of a file in your working tree.
  Anybody who wants to beat it can. It is kept because the alternative needs
  production credentials, and a check that needs those is a check that does not
  get run.
