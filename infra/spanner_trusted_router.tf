# The Cloud Spanner instance behind TrustedRouter's billing ledger, keys and
# request records. scripts/deploy/infra.sh created it on 2026-05-09; it is
# imported here so capacity changes go through review and infra-apply instead
# of CLI one-offs. Databases, schema and backups stay with the deploy scripts.
import {
  to = google_spanner_instance.trusted_router
  id = "projects/quill-cloud-proxy/instances/trusted-router-nam6"
}

resource "google_spanner_instance" "trusted_router" {
  project                      = local.gcp_project_id
  name                         = "trusted-router-nam6"
  config                       = "nam6"
  display_name                 = "TrustedRouter (nam6)"
  edition                      = "ENTERPRISE_PLUS"
  default_backup_schedule_type = "AUTOMATIC"

  # The managed autoscaler replaces the fixed 600 processing units.
  # Measured before the change (2026-10-01): high-priority CPU peaked at
  # 47-64% of hourly means over the prior week, above the multi-region target.
  # - 45% high-priority CPU is Google's scaling target for multi-region
  #   configurations: the headroom absorbs the loss of a region.
  # - 1000 processing units is the managed autoscaler's minimum floor.
  # - 10000 is the highest ceiling that floor allows (min >= 10% of max).
  autoscaling_config {
    autoscaling_limits {
      min_processing_units = 1000
      max_processing_units = 10000
    }
    autoscaling_targets {
      high_priority_cpu_utilization_percent = 45
      storage_utilization_percent           = 95
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# infra-apply runs as tr-deploy, whose project role (spanner.databaseAdmin)
# cannot update an instance. This role grants exactly what a capacity change
# needs, on this instance only: no delete, no IAM, no database access.
# Bootstrap: an owner creates the role and the binding once (the commands are
# in the PR that added this file); both are imported here after that.
import {
  to = google_project_iam_custom_role.spanner_capacity
  id = "projects/quill-cloud-proxy/roles/trustedRouterSpannerCapacity"
}

resource "google_project_iam_custom_role" "spanner_capacity" {
  project     = local.gcp_project_id
  role_id     = "trustedRouterSpannerCapacity"
  title       = "TrustedRouter Spanner capacity"
  description = "Read and resize the TrustedRouter Spanner instance; no delete, no IAM, no data."
  stage       = "GA"
  permissions = [
    "spanner.instanceOperations.get",
    "spanner.instanceOperations.list",
    "spanner.instances.get",
    "spanner.instances.update",
  ]
}

import {
  to = google_spanner_instance_iam_member.tr_deploy_capacity
  id = "projects/quill-cloud-proxy/instances/trusted-router-nam6 projects/quill-cloud-proxy/roles/trustedRouterSpannerCapacity serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
}

resource "google_spanner_instance_iam_member" "tr_deploy_capacity" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  role     = google_project_iam_custom_role.spanner_capacity.id
  member   = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}
