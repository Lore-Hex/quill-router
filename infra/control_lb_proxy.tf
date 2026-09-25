# The control load balancer's HTTPS proxy, behind forwarding rule
# trusted-router-control-https (35.241.14.18:443). It serves the certificate
# map from control_lb_certificate_map.tf. A proxy with a certificate map uses
# the map and not its classic certificates.
#
# The classic certificates stay attached until they are retired, and Terraform
# ignores that list. Removing certificate_map here is the rollback; see
# "Control load balancer certificates" in README.md for what it restores. The
# forwarding rule and the URL map are not managed here.

import {
  to = google_compute_target_https_proxy.control
  id = "projects/quill-cloud-proxy/global/targetHttpsProxies/trusted-router-control-https-proxy"
}

resource "google_compute_target_https_proxy" "control" {
  name            = "trusted-router-control-https-proxy"
  url_map         = "projects/${local.gcp_project_id}/global/urlMaps/trusted-router-control-map"
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.control.id}"
  ssl_policy      = "projects/${local.gcp_project_id}/global/sslPolicies/tr-min-tls12"
  tls_early_data  = "DISABLED"

  lifecycle {
    # While this block is in the configuration, a plan that would delete or
    # replace the proxy fails. Removing the block plans its deletion, so a
    # rollback removes certificate_map and keeps the block.
    prevent_destroy = true
    ignore_changes  = [ssl_certificates]
  }
}
