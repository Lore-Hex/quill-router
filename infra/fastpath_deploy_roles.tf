# What the deploy identity needs to create the fast path's install
# (infra/fastpath.tf), granted by hand on 2026-10-12 after the first apply
# of P0 failed for want of them (W5 of
# docs/design/fast-admission-production-rollout.md: the roles only an owner
# can grant are a short script Joseph runs), and imported here so they are
# reviewed and kept like everything else. Each is the narrowest predefined
# role that creates and binds the resources concerned; none edits the
# project's IAM policy, which stays by hand.
locals {
  fastpath_deploy_roles = toset([
    "roles/pubsub.admin",             # the topics and subscriptions, and their IAM
    "roles/iam.serviceAccountAdmin",  # the nodes' service account
    "roles/iam.serviceAccountUser",   # to start instances as that account
    "roles/secretmanager.admin",      # the envelope key's secret and its IAM
    "roles/storage.admin",            # the archive and release buckets and their IAM
    "roles/compute.instanceAdmin.v1", # the nodes
    "roles/spanner.databaseAdmin",    # the nodes' database role binding
    # The edge's firewall rule (infra/fastpath_edge_backend.tf): the first
    # apply of #1872 made the health check, the instance groups and the
    # backend service and stopped at the firewall for want of
    # compute.firewalls.create, which only this role carries among the
    # predefined ones. Granted by hand on 2026-10-20 and imported here.
    "roles/compute.securityAdmin",
  ])
  tr_deploy_member = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}

import {
  for_each = local.fastpath_deploy_roles
  to       = google_project_iam_member.fastpath_deploy[each.key]
  id       = "${local.gcp_project_id} ${each.key} ${local.tr_deploy_member}"
}

resource "google_project_iam_member" "fastpath_deploy" {
  for_each = local.fastpath_deploy_roles

  project = local.gcp_project_id
  role    = each.key
  member  = local.tr_deploy_member
}
