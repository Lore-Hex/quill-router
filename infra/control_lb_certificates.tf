# Certificate Manager certificates for the control load balancer
# (trusted-router-control-https-proxy, 35.241.14.18). This file only issues
# certificates: nothing here attaches them to a proxy, so applying it changes
# no traffic. A certificate map and the proxy switch come in later changes.
#
# One certificate per domain. Each is authorized by DNS: Certificate Manager
# checks a CNAME at _acme-challenge.<domain>, which must stay in place for
# every renewal. One DNS authorization covers a domain and its wildcard, so
# each certificate lists the domain and *.<domain>; the wildcard covers www and
# the brand subdomains (status, trust, eu, ...).
#
# Token Exchange domains are listed in token_exchange_certificate_domains.json.
# sites/token-exchange/test_exchange.py fails when a market in markets.json has
# no entry there. A domain leaves the list only by an explicit edit here; see
# "Control load balancer certificates" in README.md. A new domain's Cloud DNS
# zone is created by `sites/token-exchange/deploy.py dns`, outside Terraform,
# and must exist before this root is applied with it.

locals {
  exchange_domains = jsondecode(file("${path.module}/token_exchange_certificate_domains.json"))

  # Domain => where its DNS is hosted. allyrouter.com and uptimerouter.com are
  # the SDKs' failover domains and live in Route 53 on purpose.
  control_certificate_domains = merge(
    { for d in local.exchange_domains : d => { cloud_dns_zone = "exchange-${replace(d, ".", "-")}", route53 = false } },
    {
      "trustedrouter.com" = { cloud_dns_zone = "trustedrouter-com", route53 = false }
      "allyrouter.com"    = { cloud_dns_zone = null, route53 = true }
      "uptimerouter.com"  = { cloud_dns_zone = null, route53 = true }
    },
  )
}

resource "google_certificate_manager_dns_authorization" "control" {
  for_each    = local.control_certificate_domains
  name        = "control-${replace(each.key, ".", "-")}"
  domain      = each.key
  type        = "FIXED_RECORD"
  description = "Control load balancer certificate for ${each.key}"
}

resource "google_dns_record_set" "control_certificate_authorization" {
  for_each     = { for d, v in local.control_certificate_domains : d => v if !v.route53 }
  managed_zone = each.value.cloud_dns_zone
  name         = google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].name
  type         = google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].type
  ttl          = 300
  rrdatas      = [google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].data]
}

data "aws_route53_zone" "control_certificate" {
  for_each = { for d, v in local.control_certificate_domains : d => v if v.route53 }
  name     = each.key
}

resource "aws_route53_record" "control_certificate_authorization" {
  for_each = { for d, v in local.control_certificate_domains : d => v if v.route53 }
  zone_id  = data.aws_route53_zone.control_certificate[each.key].zone_id
  name     = google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].name
  type     = google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].type
  ttl      = 300
  records  = [google_certificate_manager_dns_authorization.control[each.key].dns_resource_record[0].data]
}

resource "google_certificate_manager_certificate" "control" {
  for_each    = local.control_certificate_domains
  name        = "control-${replace(each.key, ".", "-")}"
  description = "Control load balancer certificate for ${each.key} and *.${each.key}"

  managed {
    domains            = [each.key, "*.${each.key}"]
    dns_authorizations = [google_certificate_manager_dns_authorization.control[each.key].id]
  }

  # Certificates are created after the validation records exist and destroyed
  # before them. If a certificate cannot be destroyed, its record is not removed.
  depends_on = [
    google_dns_record_set.control_certificate_authorization,
    aws_route53_record.control_certificate_authorization,
  ]
}

# The account that applies this root needs Certificate Manager to manage the
# resources above. It can read but not change project IAM, so an owner grants
# this role; declaring it here makes the grant visible and drift-checked.
resource "google_project_iam_member" "tr_deploy_certificate_manager" {
  project = local.gcp_project_id
  role    = "roles/certificatemanager.editor"
  member  = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}

import {
  to = google_project_iam_member.tr_deploy_certificate_manager
  id = "quill-cloud-proxy roles/certificatemanager.editor serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
}
