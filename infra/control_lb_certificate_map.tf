# Certificate map for the control load balancer. It serves the Certificate
# Manager certificates in control_lb_certificates.tf by hostname: one entry for
# each domain and one for its wildcard, which covers www and the brand
# subdomains. Clients that send no hostname get trustedrouter.com's
# certificate. Nothing here attaches the map to the production proxy
# (trusted-router-control-https-proxy); that switch is a later change.

resource "google_certificate_manager_certificate_map" "control" {
  name        = "control"
  description = "Certificates for the control load balancer (trusted-router-control-https-proxy)"
}

resource "google_certificate_manager_certificate_map_entry" "control" {
  for_each = merge(
    { for d in keys(local.control_certificate_domains) : d => d },
    { for d in keys(local.control_certificate_domains) : "*.${d}" => d },
  )
  name         = "control-${replace(replace(each.key, "*.", "wildcard-"), ".", "-")}"
  description  = "${each.key} on the control load balancer"
  map          = google_certificate_manager_certificate_map.control.name
  hostname     = each.key
  certificates = [google_certificate_manager_certificate.control[each.value].id]
}

resource "google_certificate_manager_certificate_map_entry" "control_primary" {
  name         = "control-primary"
  description  = "Clients that send no hostname on the control load balancer"
  map          = google_certificate_manager_certificate_map.control.name
  matcher      = "PRIMARY"
  certificates = [google_certificate_manager_certificate.control["trustedrouter.com"].id]
}
