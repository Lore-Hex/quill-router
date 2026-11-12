# Price refresh observer scheduling gap

## Evidence

The production `price-refresh-watchdog` reported a missed execution at
2026-11-03T00:30:12Z. Its last completed check-in was 2026-11-02T22:29:59Z.
The observer workflow succeeds when it executes, but subsequent hourly runs
were absent. Other repository schedules did run, so this is not evidence of
a repository-wide outage or a proven GitHub incident.

The architectural fault was sharing GitHub's best-effort schedule between
the refresh and its observer. Manual dispatch recovered the heartbeat only
temporarily. Extending the Sentry grace period would hide missing checks.

## Repair

An hourly systemd timer on the existing `roach-gcp` monitoring host executes
the same bounded catalog validation and failure-reporting logic. It reads
public GitHub workflow metadata without a GitHub token and uses only an
ingestion credential for Sentry. It cannot publish catalog changes, change
GitHub issues, access billing or obtain provider credentials.

The service runs as a dynamic unprivileged user with read-only system files,
private temporary storage, a 96 MB memory cap, a nine-minute execution limit,
and metadata-service access blocked. The cooldown ledger is atomically stored
in its private state directory. Stable Sentry event IDs and the existing
six-hour cooldown prevent duplicate reporting by independent observers.

Only this external observer owns the watchdog heartbeat. The GitHub workflow
continues reporting completion failures and updating its bot-owned ledger,
but cannot mask a stopped external monitor. A stale catalog still alerts;
this change does not pretend to execute a missed refresh.

## Deployment and verification

After the commit is tested and merged, use a deployment identity to run:

```sh
bash scripts/ops/install_price_refresh_observer.sh <full-merged-sha>
```

The installer archives committed objects, backs up affected files, installs
only this observer, starts one real check, and enables the timer. It refuses
the read-only operations credential and changes no IAM. Confirm service
`Result=success`, timer active, a durable state file, and an accepted paired
Sentry check-in. Confirm the next scheduled check without manual dispatch.

Rollback restores the saved files from `/var/backups/tr-price-refresh-*` and
the previous heartbeat ownership. Do not disable both observers or resolve
the watchdog incident until a real check succeeds.

## First-start DNS correction

The initial service start failed before enabling its timer. The host resolver
listed `169.254.169.254`, so the service's intentional metadata-address block
also blocked DNS. The same observer completed successfully outside that network
restriction. The host already runs systemd-resolved on `127.0.0.53`.

The service now bind-mounts the existing stub resolver configuration over
`/etc/resolv.conf` in its own mount namespace, with an explicit dependency on
systemd-resolved. Host-wide DNS and both metadata-address blocks are unchanged.
An isolated production-host check resolved GitHub using this mapping and could
not connect to metadata port 80. A regression test failed on the original unit
before the mapping was added. Installation checks that the resolver is active
before replacing any service files.

## Host deleted, 2026-11-11

`roach-gcp` was stopped and then deleted on 2026-11-11 with the rest of the
retired SREChat hosts. Nothing else on it was needed, and the observer was not
moved first. The `price-refresh-watchdog` monitor reported its first missed
check-in at 12:23 that day and one every hour after that.
The GitHub workflow kept reporting refresh failures, so failed refreshes were
still alerted. Only the watchdog, and the stale-catalog check between GitHub's
irregular scheduled runs, were missing.

The installer now takes the host as `OBSERVER_HOST` and `OBSERVER_ZONE` and
defaults to `tr-clickhouse-3`, a ClickHouse standby that runs no worker units.
It has systemd-resolved with the stub resolver this unit binds, Python 3.11
(the observer runs under it), and outbound HTTPS through Cloud NAT. The unit
keeps every restriction listed above. It runs as a dynamic user that cannot read
the node's ClickHouse password file or reach the metadata server. It can still
open loopback and VPC addresses, including the local ClickHouse and Keeper
ports; ClickHouse data needs the password. Denying those addresses as well
would be a further hardening step, to be tested on the host first.

The cooldown ledger was on the deleted disk. The first run on the new host
starts with an empty ledger, so an ongoing error class can alert once more;
stable event IDs keep that to one event per class.

Before deleting or rebuilding any host, search the repository for its name.
