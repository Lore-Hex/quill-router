# P0, the dark install

Stage P0 of `docs/design/fast-admission-production-rollout.md`: the fast
path's infrastructure exists and two nodes run with the workspace
allow-list empty, so nothing admits. `infra/fastpath.tf` creates it, and
`infra-apply.yml` applies it when the change merges: the settle log and
record topics, the auditor's and stager's subscriptions, an archive
subscription and bucket for each topic, the nodes' service account and its
roles, the envelope key's secret, the releases bucket, and the two nodes,
`tr-fastpath-1` (us-central1-a) and `tr-fastpath-2` (us-central1-b) at P0,
replaced on 2026-10-20 by `tr-fastpath-3` and `tr-fastpath-4` in the same zones,
and those in turn by `tr-fastpath-5` and `tr-fastpath-6` (E2), then by
`tr-fastpath-7` and `tr-fastpath-8` (E2 stage I1, the edge policy), then by
`tr-fastpath-9` and `tr-fastpath-10` (E2 stage I2, the decide attribute), on
the default network behind the project's NAT with no external address.

## What is done by hand, once

1. The envelope key's first version, 48 random bytes (the node needs at
   least 32), which the nodes read at `.../versions/1`:

   ```bash
   openssl rand 48 | gcloud secrets versions add fastpath-envelope-key --project=quill-cloud-proxy --data-file=-
   ```

2. A release. `fastpath-release.yml` uploads every main commit whose CI
   passed to `gs://tr-fastpath-releases-quill-cloud-proxy/fastpath/<commit>/`;
   the first is the commit that merged the workflow. Pin it in
   `infra/fastpath.tf` (`fastpath_release`), merge, and reset both nodes so
   the startup script installs it. A node that already ran a release takes
   the new one by "Taking a pinned release" in `fastpath-deploy.md`, which
   checks the release its process runs, not the file that names it:

   ```bash
   gcloud compute instances reset tr-fastpath-7 --zone=us-central1-a --project=quill-cloud-proxy
   gcloud compute instances reset tr-fastpath-8 --zone=us-central1-b --project=quill-cloud-proxy
   ```

   A node with no release pinned stays stopped and says so in its journal.
   Once a node holds leases it is replaced, never reset
   (`fastpath-deploy.md`).

3. Roles only an owner can grant. The deploy identity's, once, before the
   first apply (`infra/fastpath_deploy_roles.tf` records them):

   ```bash
   for role in roles/pubsub.admin roles/iam.serviceAccountAdmin roles/iam.serviceAccountUser roles/secretmanager.admin roles/storage.admin roles/compute.instanceAdmin.v1 roles/spanner.databaseAdmin; do gcloud projects add-iam-policy-binding quill-cloud-proxy --member=serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com --role=$role --condition=None --quiet; done
   ```

   And the nodes', once the apply has created their service account, which
   Terraform does not bind because the deploy identity does not edit the
   project's IAM policy:

   ```bash
   for role in roles/logging.logWriter roles/monitoring.metricWriter; do gcloud projects add-iam-policy-binding quill-cloud-proxy --member=serviceAccount:tr-fastpath-node@quill-cloud-proxy.iam.gserviceaccount.com --role=$role --condition=None --quiet; done
   ```

## Exit checks

From inside the network (IAP SSH to a node), with `fastpathctl` at
`/opt/fastpath/fastpathctl`:

- Both nodes are members: `fastpathctl -database projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router node ADDRESS`
  says `"State": "serving"`, `"Live": true`, roles `owner`, `frontdoor` and
  `auditor`, for each node's `ADDRESS` (its internal IP, port 8080).
- The auditor holds its keys and commits nothing: `tr_lease` stays empty,
  and the only rows written are membership's (`tr_fastpath_member`).
- W1's refusals, seen from inside the network against a workspace that
  exists: an authorize for a workspace not on the list is refused by the
  front door and the owner, and `Store.Grant` refuses one too.
- W7's alerts fire once on an injected fault.
- The load is W3's baseline: each node's membership write once a second and
  the auditor's scans.

## Way back

Stop the nodes. No lease can exist, since no workspace is on the list.
