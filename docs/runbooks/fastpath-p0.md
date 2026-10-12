# P0, the dark install

Stage P0 of `docs/design/fast-admission-production-rollout.md`: the fast
path's infrastructure exists and two nodes run with the workspace
allow-list empty, so nothing admits. `infra/fastpath.tf` creates it, and
`infra-apply.yml` applies it when the change merges: the settle log and
record topics, the auditor's and stager's subscriptions, an archive
subscription and bucket for each topic, the nodes' service account and its
roles, the envelope key's secret, the releases bucket, and the two nodes,
`tr-fastpath-1` (us-central1-a) and `tr-fastpath-2` (us-central1-b), on the
default network behind the project's NAT with no external address.

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
   the startup script installs it:

   ```bash
   gcloud compute instances reset tr-fastpath-1 --zone=us-central1-a --project=quill-cloud-proxy
   gcloud compute instances reset tr-fastpath-2 --zone=us-central1-b --project=quill-cloud-proxy
   ```

   A node with no release pinned stays stopped and says so in its journal.
   Once a node holds leases it is replaced, never reset
   (`fastpath-deploy.md`).

3. Roles only an owner can grant, if the apply refuses them: the nodes'
   `roles/logging.logWriter` and `roles/monitoring.metricWriter` on the
   project are the only project-level bindings.

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
