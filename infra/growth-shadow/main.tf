terraform {
  required_version = ">= 1.6.0"
  backend "gcs" {
    bucket = "tr-infra-tfstate-quill-cloud-proxy"
    prefix = "growth-partition-shadow"
  }
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

locals {
  project = "quill-cloud-proxy"
  region  = "us-central1"
  source  = "quill-cloud-proxy-growth-sync"
}

provider "google" {
  project = local.project
  region  = local.region
}

variable "verified_image" {
  type        = string
  description = "Digest built by the green deploy-growth-sync workflow from a commit containing partition_shadow."
  validation {
    condition     = can(regex("^us-central1-docker[.]pkg[.]dev/quill-cloud-proxy/trusted-router/growth-sync@sha256:[0-9a-f]{64}$", var.verified_image))
    error_message = "A pinned growth-sync image digest, not a tag, is required."
  }
}

resource "google_service_account" "worker" {
  account_id   = "tr-growth-partition-shadow"
  display_name = "Read-only growth verification; no marketing dataset token"
}

resource "google_service_account" "trigger" {
  account_id   = "tr-growth-shadow-trigger"
  display_name = "Invoke only the growth partition verifier"
}

resource "google_storage_bucket" "shadow" {
  name                        = "quill-cloud-proxy-growth-partition-shadow"
  location                    = local.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    condition { age = 21 }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "source" {
  bucket = local.source
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker.email}"
  condition {
    title      = "only_committed_legacy_checkpoint"
    expression = "resource.name == 'projects/_/buckets/${local.source}/objects/state.json.gz'"
  }
}

resource "google_storage_bucket_iam_member" "sink" {
  bucket = google_storage_bucket.shadow.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_cloud_run_v2_job" "shadow" {
  name     = "trusted-router-growth-partition-shadow"
  location = local.region
  template {
    parallelism = 1
    task_count  = 1
    template {
      service_account = google_service_account.worker.email
      timeout         = "240s"
      max_retries     = 0
      containers {
        image   = var.verified_image
        command = ["python"]
        args    = ["-m", "scripts.axiom_growth.partition_shadow"]
        resources {
          limits = { cpu = "1", memory = "2Gi" }
        }
        env {
          name  = "GROWTH_STATE_BUCKET"
          value = local.source
        }
        env {
          name  = "GROWTH_SHADOW_BUCKET"
          value = google_storage_bucket.shadow.name
        }
      }
    }
  }
}

resource "google_cloud_run_v2_job_iam_member" "invoke" {
  location = local.region
  name     = google_cloud_run_v2_job.shadow.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.trigger.email}"
}

resource "google_cloud_scheduler_job" "verify" {
  name        = "trusted-router-growth-partition-shadow"
  region      = local.region
  schedule    = "2-59/5 * * * *"
  time_zone   = "Etc/UTC"
  description = "Independent shadow; no customer-visible Axiom writes"
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${local.project}/locations/${local.region}/jobs/${google_cloud_run_v2_job.shadow.name}:run"
    oauth_token {
      service_account_email = google_service_account.trigger.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.invoke, google_storage_bucket_iam_member.source, google_storage_bucket_iam_member.sink]
}
