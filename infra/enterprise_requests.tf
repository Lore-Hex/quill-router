# Private first-party form submissions, not a marketing analytics feed. No
# topic-level retention, retained acknowledgements, push endpoint or export.
resource "google_pubsub_topic" "enterprise_requests" {
  project = local.gcp_project_id
  name    = "tr-enterprise-requests"
  message_storage_policy {
    allowed_persistence_regions = ["us-central1"]
  }
}

resource "google_pubsub_subscription" "enterprise_requests_clickhouse" {
  project                    = local.gcp_project_id
  name                       = "tr-enterprise-requests-clickhouse"
  topic                      = google_pubsub_topic.enterprise_requests.id
  ack_deadline_seconds       = 90
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_topic_iam_member" "enterprise_requests_publish" {
  project = local.gcp_project_id
  topic   = google_pubsub_topic.enterprise_requests.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:trusted-router-control-run@${local.gcp_project_id}.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "enterprise_requests_drain" {
  project      = local.gcp_project_id
  subscription = google_pubsub_subscription.enterprise_requests_clickhouse.name
  role         = "roles/pubsub.subscriber"
  member       = local.clickhouse_node_member
}

resource "google_monitoring_alert_policy" "enterprise_request_backlog" {
  project               = local.gcp_project_id
  display_name          = "TR enterprise inquiries: ClickHouse capture delayed"
  combiner              = "OR"
  notification_channels = local.fastpath_alert_channels
  conditions {
    display_name = "Oldest unacknowledged inquiry exceeds 30 minutes"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.label.subscription_id = \"${google_pubsub_subscription.enterprise_requests_clickhouse.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1800
      duration        = "300s"
    }
  }
}

# Separate delivery and identity: the CRM worker cannot read ClickHouse,
# publish fake submissions, or acknowledge the archive's queue.
resource "google_pubsub_subscription" "enterprise_requests_twenty" {
  project                    = local.gcp_project_id
  name                       = "tr-enterprise-requests-twenty"
  topic                      = google_pubsub_topic.enterprise_requests.id
  ack_deadline_seconds       = 90
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  expiration_policy {
    ttl = ""
  }
}

resource "google_service_account" "enterprise_twenty" {
  project      = local.gcp_project_id
  account_id   = "tr-enterprise-twenty"
  display_name = "Enterprise form to Twenty People"
}

resource "google_service_account" "enterprise_twenty_timer" {
  project      = local.gcp_project_id
  account_id   = "tr-enterprise-twenty-timer"
  display_name = "Invoke the enterprise Twenty job"
}

resource "google_pubsub_subscription_iam_member" "enterprise_twenty_pull" {
  project      = local.gcp_project_id
  subscription = google_pubsub_subscription.enterprise_requests_twenty.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.enterprise_twenty.email}"
}

# Only the container is managed here. The key value must never enter tfstate.
resource "google_secret_manager_secret" "enterprise_twenty_key" {
  project   = local.gcp_project_id
  secret_id = "tr-enterprise-twenty-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "enterprise_twenty_key_read" {
  project   = local.gcp_project_id
  secret_id = google_secret_manager_secret.enterprise_twenty_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.enterprise_twenty.email}"
}

resource "google_monitoring_alert_policy" "enterprise_twenty_backlog" {
  project               = local.gcp_project_id
  display_name          = "TR enterprise inquiries: Twenty sync delayed"
  combiner              = "OR"
  notification_channels = local.fastpath_alert_channels
  conditions {
    display_name = "Unacknowledged CRM request older than 30 minutes"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND resource.label.subscription_id = \"${google_pubsub_subscription.enterprise_requests_twenty.name}\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1800
      duration        = "300s"
    }
  }
}
