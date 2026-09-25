# TEMPORARY. A second frontend for the control URL map that serves only the
# certificate map, on an address of its own that no DNS name points to. It
# exists to check every hostname with
#   curl --resolve <host>:443:<address> https://<host>/
# before the production proxy switches to the map, and is removed once that
# check passes. It uses the production proxy's SSL policy and TLS early data
# setting.

resource "google_compute_global_address" "control_certificate_map_test" {
  name        = "control-certificate-map-test"
  description = "Temporary: certificate map test frontend"
}

resource "google_compute_target_https_proxy" "control_certificate_map_test" {
  name            = "control-certificate-map-test"
  description     = "Temporary: certificate map test frontend"
  url_map         = "projects/${local.gcp_project_id}/global/urlMaps/trusted-router-control-map"
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.control.id}"
  ssl_policy      = "projects/${local.gcp_project_id}/global/sslPolicies/tr-min-tls12"
  tls_early_data  = "DISABLED"

  # Every hostname must have its map entry before the frontend accepts traffic.
  depends_on = [
    google_certificate_manager_certificate_map_entry.control,
    google_certificate_manager_certificate_map_entry.control_primary,
  ]
}

resource "google_compute_global_forwarding_rule" "control_certificate_map_test" {
  name                  = "control-certificate-map-test"
  description           = "Temporary: certificate map test frontend"
  target                = google_compute_target_https_proxy.control_certificate_map_test.id
  ip_address            = google_compute_global_address.control_certificate_map_test.id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

output "control_certificate_map_test_address" {
  description = "Temporary test frontend address for curl --resolve"
  value       = google_compute_global_address.control_certificate_map_test.address
}
