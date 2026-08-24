# GitHub is down, or the repository is gone

Two different failures that get conflated. They need different answers, and the
answer to one does nothing for the other.

## Which one is this?

| Symptom | This is | Go to |
|---|---|---|
| Actions failing, pushes rejected, github.com 5xx | an **outage** | [Shipping without Actions](#shipping-without-actions) |
| Repository or org missing, access revoked | **repository loss** | [Restoring from the mirror](#restoring-from-the-mirror) |

**An outage does not touch production.** Cloud Run keeps serving, the enclaves
keep attesting, ClickHouse keeps ingesting. Nothing customer-facing degrades
except GitHub OAuth sign-in, which is accepted: users who chose GitHub login
are already exposed to GitHub being down, and Google login is unaffected.

What an outage takes away is the ability to **ship**. The mirror does not help
with that — the code was never at risk, since every clone is a full copy.

## Shipping without Actions

```bash
scripts/deploy/break_glass_deploy.sh            # print the plan, change nothing
scripts/deploy/break_glass_deploy.sh --apply
```

Builds via Cloud Build (server side, no local Docker), rolls out to every
control-plane region using the same `rollout.sh` the workflow calls, and smoke
checks `/v1/models`.

**Before you run it, read what it skips.** The script prints this too, and the
list is in its header: no CI gate, no schema migrations, no staged canary, no
automatic rollback, no secret sync or Cloud Run Jobs. It prints the currently
serving revision per region before touching anything — **write those down**,
they are your rollback and you cannot look them up once the new revision is
live and you are in a hurry.

Use it for one thing: getting a known-good fix serving. Everything else waits.

## Restoring from the mirror

A verified bundle of every ref is uploaded daily to a versioned bucket by
`.github/workflows/mirror-repo.yml`, and the same script runs from a laptop.

```bash
gcloud storage cp gs://quill-cloud-proxy-git-mirror/quill-router/latest.bundle .
git clone latest.bundle quill-router
```

Dated bundles sit beside `latest.bundle`; versioning is on and noncurrent
generations are kept 365 days, so a bad overwrite is recoverable.

**Verified end to end on 2026-08-23**: restored from the bucket to 265
branches at the origin tip.

## Why the mirror is a bundle and not a second git host

A self-hosted GitLab on GCP was considered and rejected. It is a database, a
Redis, backups, upgrades and its own runners — a stateful system operated to
protect against a stateless dependency, and least trustworthy exactly when
first needed, because nothing exercises it. A bundle in GCS has no service to
keep alive and restores with `git clone`.

Cloud Source Repositories would be the obvious home and is closed to new
customers, which is why this depends on nothing but GCS.

## What the mirror check actually proves

`git bundle verify` is **not** sufficient, and this was measured: on a bundle
with seven bytes overwritten, `verify` **passed** and `git clone` **failed**.
verify only checks that prerequisite commits are satisfiable. The script
therefore clones every bundle into a scratch directory and compares both the
restored HEAD and the restored branch count against the source. A bundle that
restores the right HEAD while carrying one branch out of 265 is the failure
that check exists to catch.
