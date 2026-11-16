# CI identities: rollout and the later phases

Every GitHub Actions workflow that reaches Google Cloud used to run as
`tr-deploy@quill-cloud-proxy`, which is close to a project administrator. The
workload identity provider now sorts each admitted workflow into a tier
(`infra/gcp_wif.tf`, `infra/ci_identities.tf`, and "Workflow identities" in
`infra/README.md`):

| Tier | Account | Workflows |
|---|---|---|
| `prices` | `tr-ci-prices` | `refresh-prices.yml` |
| `audit` | `tr-ci-audit` | `typed-audit.yml` |
| `probe` | `tr-ci-probe` | `cloud-security-baseline.yml`, `daily-embeddings-probe.yml`, `mirror-repo.yml` |
| `dns` | `enclave-dns-reconciler` | quill-cloud-proxy's `reconcile-enclave-dns.yml` |
| `deploy` | `tr-deploy` | every other admitted workflow |

What steps 1 to 4 achieve, and what they leave open:

- Each of the four small accounts accepts its own tier and nothing else. The
  moved workflows ask for their small account, so a mistake or a compromise in
  one of them has that account's reach, not a deploy's.
- `tr-deploy` is not confined yet. Besides the deploy tier it keeps two
  bindings made by hand, one for the whole of each repository, and through
  them it still accepts every admitted workflow that asks for it. Until phase
  5 removes them, only the workflow files and their tests keep a small-tier
  workflow off the deploy identity.
- A fifth account is reached through the same provider and is outside the
  tiers: `tr-batch-release`, which one job of quill-cloud-proxy's
  `deploy-enclave-gcp.yml` asks for. Its binding names the token's subject
  (that repository's `batch-release` environment). No step here changes the
  subject mapping or that binding.

The order matters: a workflow that asks for an account before the account, its
access and its tier binding exist fails at its first Google Cloud call, and
every one of the moved workflows runs on a schedule.

The plan counts below were counted from the Terraform. No plan has been run
for this change, so read the real plan before answering yes, and treat a
different count as a question to settle, not as an error in the plan.

## When an apply runs

`infra-apply` plans and applies the whole root, as `tr-deploy`, on each of:

- a merge to `main` that touches `infra/**`;
- a push to `main` that touches `sites/token-exchange/markets.json`;
- a dispatch of `infra-apply.yml` with `operation=terraform`.

Any of the three counts as "an apply of `main`" in the steps below.

## 1. An owner applies what the deploy identity cannot

`infra-apply` runs as `tr-deploy`, which can neither add a project custom role
nor write the project's IAM policy. From the Terraform pull request's branch,
rebased on `main`, with an owner's credentials and the Terraform version CI
pins (`.github/actions/setup-terraform`):

```bash
terraform -chdir=infra init
terraform -chdir=infra apply -target=google_project_iam_member.ci_tier
```

Expected: `7 to add, 0 to change, 0 to destroy`.

- `google_project_iam_custom_role.ci_tier` and
  `google_project_iam_member.ci_tier`, each for `"prices"` and `"probe"`. These
  four are what only an owner can apply.
- `google_service_account.ci_tier` for `"audit"`, `"prices"` and `"probe"`. A
  binding needs its member to exist, and a targeted apply takes the account
  resource whole, so the audit account comes with the other two.

Terraform warns that resource targeting is in effect, which is expected here.
Anything else in the plan is a reason to stop.

Check:

```bash
gcloud iam roles describe trustedRouterCiProbe --project=quill-cloud-proxy \
  --format='value(includedPermissions)'
gcloud iam roles describe trustedRouterCiPrices --project=quill-cloud-proxy \
  --format='value(includedPermissions)'
gcloud projects get-iam-policy quill-cloud-proxy --flatten='bindings[].members' \
  --filter='bindings.members:tr-ci-' --format='table(bindings.role,bindings.members)'
gcloud iam service-accounts list --project=quill-cloud-proxy \
  --filter='email~^tr-ci-' --format='value(email)'
```

The policy query prints two rows: `trustedRouterCiPrices` for `tr-ci-prices`
and `trustedRouterCiProbe` for `tr-ci-probe`. The last command prints the
three accounts. None of them can do anything yet that matters: nothing may
impersonate them.

Merge the pull request right after this. `main` does not declare these seven
resources until it merges, so an apply of `main` in between plans to remove
them. That run does not stop there: Terraform applies everything else it
planned, fails on the two project bindings, which the deploy identity cannot
remove, and exits 1. The run then shows as failed although the change that
started it has landed. Afterwards run the four checks above and the targeted
apply again; it puts back whatever is missing. Run it again as well if the
pull request's permission lists change after this step.

## 2. Save the provider, then merge the Terraform pull request

Keep the provider as it is now, so that step 3 has something to compare with
and to restore from:

```bash
gcloud iam workload-identity-pools providers describe github \
  --project=quill-cloud-proxy --location=global \
  --workload-identity-pool=github-actions --format=json > provider-before.json
```

Then merge. `infra-apply` applies the rest. Expected, with the secret list as
it was in November 2026: `96 to add, 1 to change, 0 to destroy`.

- `google_iam_workload_identity_pool_provider.github` changed in place: the
  mapping gains `attribute.tier`, and the condition lists the same workflows
  as before in sorted order.
- five `google_service_account_iam_member.ci_tier`, one per tier;
- 87 secret bindings, one database binding and three bucket bindings.

No workflow asks for a new account yet. What does change for every workflow
is the provider itself, which step 3 checks first. If the apply stops partway,
for example because Google refuses the tier expression, the provider is as it
was and the workflows keep running; correct the file and merge again.

## 3. Verify before any workflow moves

### The provider still exchanges tokens

Every workflow of both repositories exchanges its token at this provider, and
`infra-apply` is one of them. An expression Google accepted but cannot
evaluate would refuse them all, including the workflow that could repair it.
Do not judge this from runs that already exist: the DNS reconciler is
scheduled every five minutes, but GitHub starts it hours apart, so its latest
run is usually older than the apply. Start one run in each repository now and
read those two:

```bash
gh workflow run infra-apply.yml --repo Lore-Hex/quill-router --ref main -f operation=status
gh workflow run reconcile-enclave-dns.yml --repo Lore-Hex/quill-cloud-proxy --ref main

gh run list --repo Lore-Hex/quill-router --workflow infra-apply.yml \
  --event workflow_dispatch --limit 1 --json databaseId,createdAt,status,conclusion
gh run list --repo Lore-Hex/quill-cloud-proxy --workflow reconcile-enclave-dns.yml \
  --event workflow_dispatch --limit 1 --json databaseId,createdAt,status,conclusion
```

Check that each `createdAt` is later than the apply's end, then follow each
run with `gh run watch <databaseId> --exit-status`. `operation=status`
authenticates as `tr-deploy`, reads the deployment journal and changes
nothing. The reconciler still asks for `tr-deploy` at this point. Both must
get past their authentication step and their first `gcloud` call.

If either cannot authenticate, an owner puts the previous mapping back by
hand. This touches nothing but the mapping, and `provider-before.json` shows
what it was:

```bash
gcloud iam workload-identity-pools providers update-oidc github \
  --project=quill-cloud-proxy --location=global \
  --workload-identity-pool=github-actions \
  --attribute-mapping='google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.ref=assertion.ref,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner'
```

Start the two runs again to confirm. Then correct `infra/gcp_wif.tf` in a pull
request before the next apply of `main`, which would otherwise put the failing
expression back. Do not revert the whole change instead: the deploy identity
cannot remove the owner's project bindings, so that apply would fail.

### The bindings are in place

```bash
gcloud iam workload-identity-pools providers describe github \
  --project=quill-cloud-proxy --location=global \
  --workload-identity-pool=github-actions --format='yaml(attributeMapping)'

for account in tr-deploy tr-ci-prices tr-ci-audit tr-ci-probe enclave-dns-reconciler; do
  gcloud iam service-accounts get-iam-policy \
    "${account}@quill-cloud-proxy.iam.gserviceaccount.com" \
    --project=quill-cloud-proxy --format='value(bindings.members)'
done
```

The provider shows `attribute.tier`. Each account shows its
`.../attribute.tier/<tier>` member, and `tr-deploy` still shows its two
`.../attribute.repository/...` members as well. Then read one binding of each
kind:

```bash
gcloud secrets get-iam-policy trustedrouter-together-api-key --project=quill-cloud-proxy
gcloud spanner databases get-iam-policy trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy
gcloud storage buckets get-iam-policy gs://quill-cloud-proxy-git-mirror
```

The database policy lists `tr-ci-audit` under `roles/spanner.databaseReader`
and does not list `tr-ci-probe` or `tr-ci-prices` at all.

## 4. Merge the workflow pull requests and run each moved workflow once

One pull request in quill-router (five workflows) and one in quill-cloud-proxy
(the DNS reconciler). `cloud-security-baseline.yml` runs on its own merge.
Dispatch the others:

```bash
for workflow in daily-embeddings-probe mirror-repo refresh-prices typed-audit; do
  gh workflow run "${workflow}.yml" --repo Lore-Hex/quill-router --ref main
done
gh workflow run reconcile-enclave-dns.yml --repo Lore-Hex/quill-cloud-proxy --ref main
```

What to read in each run:

- Any of them: `Permission 'iam.serviceAccounts.getAccessToken' denied` at the
  first `gcloud` or client call means the tier binding or the mapping is not
  in place. Go back to step 3.
- `cloud-security-baseline`: `=== GCP baseline OK`. A `could not read`,
  `cannot read gs://` or `metric ... missing` line that the last run as
  `tr-deploy` did not have is a missing permission, not drift. Add it to
  `ci_probe_project_permissions`.
- `daily-embeddings-probe`: it reads the monitor key and the probe passes.
- `mirror-repo`: `mirror complete`.
- `refresh-prices`: no `Provider refresh secret unavailable` error, and the
  run does not newly hold `google-vertex` for review (its canaries call Vertex
  AI as `tr-ci-prices`).
- `typed-audit`: both jobs read the production database as `tr-ci-audit`. On
  2026-11-15 the `spanner-schema-drift` job already failed on `main`, after
  reading the schema, with
  `CANNOT AUDIT: INDEXES tr_workspace_directory_source/PRIMARY_KEY`. That
  failure is not this change. A `PERMISSION_DENIED` is.
- `reconcile-enclave-dns`: green.

To back out, revert the workflow pull request. `tr-deploy` still accepts every
admitted workflow until phase 5, so the reverted workflows run as before. The
new accounts and their access can stay.

## 5. Later: remove the deploy identity's repository-wide bindings

Until this is done, any admitted workflow can still ask for `tr-deploy` and
get it. Do it once every moved workflow has run on its own account through its
schedule, not only by dispatch.

A green deploy does not show which binding admitted it while both exist. So
remove one repository's binding, check that a deploy-tier workflow of that
repository still authenticates, and put the binding back if it does not. Pick
a moment with no deploy in progress in either repository.

```bash
ACCOUNT=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com
POOL=principalSet://iam.googleapis.com/projects/44325983244/locations/global/workloadIdentityPools/github-actions

# The deploy tier's member must be listed before anything is removed.
gcloud iam service-accounts get-iam-policy "$ACCOUNT" --project=quill-cloud-proxy

gcloud iam service-accounts remove-iam-policy-binding "$ACCOUNT" \
  --project=quill-cloud-proxy --role=roles/iam.workloadIdentityUser \
  --member="$POOL/attribute.repository/Lore-Hex/quill-router"
```

A change to an IAM policy takes some minutes to reach every server. A check
started at once can still be admitted by the binding that was just removed,
and so prove nothing. Wait about ten minutes, then:

```bash
gh workflow run infra-apply.yml --repo Lore-Hex/quill-router --ref main -f operation=status
```

Read that run, as in step 3. Run the check a second time and read that run as
well. If either cannot authenticate, run the removal command again with
`add-iam-policy-binding` in place of `remove-iam-policy-binding`, and stop.

Only when both runs have passed, remove the binding for
`Lore-Hex/quill-cloud-proxy` in the same way: remove, wait about ten minutes,
check twice. That repository has no read-only deploy-tier workflow. Dispatch
`deploy-enclave-dns-reconciler.yml`, which rebuilds the reconciler's image
from `main` and redeploys its job, or remove the binding ten minutes or more
before a release you were going to make anyway.

Then correct the text that describes these bindings as still present:
`infra/gcp_wif.tf`, `infra/ci_identities.tf`, `infra/README.md`, this file,
and in quill-cloud-proxy `reconcile-enclave-dns.yml` and
`tools/test_rollout_safety.py`.

## 6. Later: take identity administration away from the deploy identity

`tr-deploy` holds `roles/iam.serviceAccountAdmin` and
`roles/iam.workloadIdentityPoolAdmin`. With them, whatever runs as `tr-deploy`
can rewrite who may impersonate any service account and which workflows the
provider admits, so the tiers hold only while no deploy-tier workflow is
compromised or mistaken. Without them, a change to the tiers or to a service
account is applied by an owner before it merges, as a Cloud Armor rule is.

Reads keep working: `roles/viewer` lets the deploy identity read service
accounts, their IAM policies and the provider, so an `infra-apply` plan that
changes none of them still passes.

Before removing `roles/iam.serviceAccountAdmin`:

- Check that no script a workflow runs as `tr-deploy` makes a service account
  or sets one's IAM policy (`gcloud iam service-accounts create`,
  `add-iam-policy-binding`). In November 2026 a search found those calls only
  in scripts an owner or operator runs by hand: `scripts/deploy/infra.sh`,
  `wif_setup.sh`, `clickhouse_cluster.sh` and `spanner_reliability.sh`, and in
  the owner instructions that `public_surface.sh` and `internal_surface.sh`
  print. That was a search, not a trace of every deploy path.
- Keep `roles/iam.serviceAccountUser`. Deploys start services and instances
  as other accounts.
- Expect `google_service_account.*` and `google_service_account_iam_member.*`
  in this root to need an owner's targeted apply whenever they change.

`roles/iam.serviceAccountAdmin` is declared in
`infra/fastpath_deploy_roles.tf`, so the cloud and the file have to change in
one step:

1. Open a pull request that removes `"roles/iam.serviceAccountAdmin"` from
   `fastpath_deploy_roles`.
2. From that branch, an owner runs

   ```bash
   terraform -chdir=infra apply \
     -target='google_project_iam_member.fastpath_deploy["roles/iam.serviceAccountAdmin"]'
   ```

   Expected: `0 to add, 0 to change, 1 to destroy`. It removes the binding
   from the project and from the state together.
3. Merge right after it. `infra-apply` then plans no change for the binding.

Either half alone makes every later apply exit 1, including unrelated ones. If
the binding is removed by hand while `main` still declares it, the next apply
plans to add it and fails on it, because the deploy identity cannot write
project IAM. If the entry is removed from `main` while the binding is still
live, the next apply plans to remove it and fails for the same reason.

`roles/iam.workloadIdentityPoolAdmin` is not in Terraform, so an owner removes
it directly:

```bash
gcloud projects remove-iam-policy-binding quill-cloud-proxy \
  --member=serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityPoolAdmin
```

From then on a pull request that changes the provider (a workflow added to a
tier list, or moved) is applied by an owner from its branch before it merges:

```bash
terraform -chdir=infra apply -target=google_iam_workload_identity_pool_provider.github
```

If it merges first, the post-merge apply fails on the permission check; apply
it as an owner and rerun the workflow.

After both removals the deploy identity still administers the IAM policies of
secrets, buckets and the database, which its deploys use. Narrowing those is a
separate piece of work.

## Later, smaller

Found while doing this and left as they are:

- `tr-ci-probe` administers the mirror bucket (`roles/storage.admin` on
  `quill-cloud-proxy-git-mirror`); a custom role with bucket read and update
  and object write would be narrower.
- `scripts/deploy/secrets.sh` still gives `tr-deploy` each refresh secret, and
  about ten provider tests require those lines; the refresh no longer uses
  them.
- `cloud-staleness.yml` is in the deploy list and does not authenticate to
  Google Cloud; it can leave the list.
- `scripts/deploy/gcp_security_baseline_check.sh` computes key age with BSD
  `date -j`, which the Linux runner rejects; the fallback is the current time,
  so there every key reads as zero days old.
