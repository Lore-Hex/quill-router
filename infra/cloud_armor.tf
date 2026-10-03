# Cloud Armor edge policies for the global HTTPS load balancer
# (trusted-router-control-map) and the Lightning funding site.
#
# These policies predate this root. Operators created them with the gcloud
# commands in docs/operations/*-surface-split.md and the Lightning runbook, and
# tuned some rules by hand afterwards. The blocks below mirror the live
# policies exactly as read on 2026-10-03, so adopting them changes nothing.
#
# The deploy identity that applies this root (tr-deploy) can read and attach
# security policies but cannot create or change them, on purpose: a
# compromised deploy pipeline must not be able to weaken the edge. A pull
# request that changes a rule here therefore needs an owner to apply it with
# their own credentials before it merges (see "Cloud Armor policies" in
# README.md); otherwise the post-merge apply fails on the permission check.
#
# Attachments: the backend services belong to the deploy scripts
# (scripts/deploy/public_surface_edge.sh, internal_surface_edge.sh), not to
# this root. The check blocks at the end only verify, on every plan, that each
# backend still uses its policy.
#
# Two live descriptions still say "previewed" from the rollout. Every rule is
# enforced (preview = false). They are kept verbatim so that adoption is a
# no-op; correct them in a later, owner-applied change.

import {
  to = google_compute_security_policy.legacy_edge
  id = "projects/quill-cloud-proxy/global/securityPolicies/trusted-router-legacy-edge"
}

resource "google_compute_security_policy" "legacy_edge" {
  name        = "trusted-router-legacy-edge"
  description = "TrustedRouter edge controls on the legacy combined backend; all-path per-source ceiling enforced, host and route-shape rules previewed"
  type        = "CLOUD_ARMOR"

  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable = true
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 900
    description = "Reject hosts outside canonical and marketing aliases (preview on legacy backend)"
    preview     = false
    match {
      expr {
        expression = "!has(request.headers['host']) || !request.headers['host'].lower().matches('^(?:trustedrouter|allyrouter|uptimerouter)[.]com$|^(?:www|status|trust)[.](?:trustedrouter|allyrouter|uptimerouter)[.]com$|^(?:eu|status-us|status-eu)[.]trustedrouter[.]com$')"
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1000
    description = "Browser inference proxy per-client throttle"
    preview     = false
    match {
      expr {
        expression = "request.path.startsWith('/chat-proxy/')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 120
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1100
    description = "State-changing request per-client throttle"
    preview     = false
    match {
      expr {
        expression = "request.method != 'GET' && request.method != 'HEAD' && request.method != 'OPTIONS' && !request.path.startsWith('/internal/') && !request.path.startsWith('/v1/internal/')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 300
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1150
    description = "Gateway plane: bounded well above legitimate ~29/min per enclave IP, so an unauthenticated flood at the enclave cannot 429 real authorization via the tighter 1200 ceiling"
    preview     = false
    match {
      expr {
        expression = "request.path.startsWith('/internal/') || request.path.startsWith('/v1/internal/')"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 12000
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1200
    description = "All-path per-source safety ceiling"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 2400
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "default rule"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = google_compute_security_policy.public_edge
  id = "projects/quill-cloud-proxy/global/securityPolicies/trusted-router-public-edge"
}

resource "google_compute_security_policy" "public_edge" {
  name        = "trusted-router-public-edge"
  description = "T1 public website edge"
  type        = "CLOUD_ARMOR"

  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable = true
    }
  }

  rule {
    action      = "deny(403)"
    priority    = 900
    description = "Reject non-canonical hosts"
    preview     = false
    match {
      expr {
        expression = "!has(request.headers['host']) || !request.headers['host'].lower().matches('^(?:trustedrouter|allyrouter|uptimerouter)[.]com$|^(?:www|status|trust)[.](?:trustedrouter|allyrouter|uptimerouter)[.]com$|^(?:eu|status-us|status-eu)[.]trustedrouter[.]com$')"
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1200
    description = "T1 per-source ceiling"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 2400
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "default rule"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = google_compute_security_policy.internal_edge
  id = "projects/quill-cloud-proxy/global/securityPolicies/trusted-router-internal-edge"
}

resource "google_compute_security_policy" "internal_edge" {
  name        = "trusted-router-internal-edge"
  description = "Internal machine-to-machine plane edge"
  type        = "CLOUD_ARMOR"

  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable = true
    }
  }

  rule {
    action      = "throttle"
    priority    = 1200
    description = "Per-source ceiling, headroom above the measured ~30/min per gateway IP"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 6000
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "default rule"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = google_compute_security_policy.lightning_funding
  id = "projects/quill-cloud-proxy/global/securityPolicies/lightning-router-funding"
}

resource "google_compute_security_policy" "lightning_funding" {
  name        = "lightning-router-funding"
  description = "Lightning funding ingress limits"
  type        = "CLOUD_ARMOR"

  rule {
    action      = "throttle"
    priority    = 900
    description = ""
    preview     = false
    match {
      expr {
        expression = "request.method == 'POST' && (request.path == '/api/invoices' || (request.path == '/api/l402/funding' && (!has(request.headers['authorization']) || request.headers['authorization'] == '')))"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 20
        interval_sec = 900
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 950
    description = ""
    preview     = false
    match {
      expr {
        expression = "request.path == '/api/account' || request.path == '/api/usage' || request.path == '/api/feedback' || request.path == '/api/l402/funding'"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 20
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "throttle"
    priority    = 1000
    description = ""
    preview     = false
    match {
      expr {
        expression = "true"
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = 180
        interval_sec = 60
      }
    }
  }

  rule {
    action      = "allow"
    priority    = 2147483647
    description = "default rule"
    preview     = false
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

check "control_backend_uses_legacy_edge" {
  data "google_compute_backend_service" "control" {
    name = "trusted-router-control-backend"
  }

  assert {
    condition     = endswith(coalesce(data.google_compute_backend_service.control.security_policy, "-"), "/securityPolicies/${google_compute_security_policy.legacy_edge.name}")
    error_message = "trusted-router-control-backend no longer uses the trusted-router-legacy-edge Cloud Armor policy."
  }
}

check "public_backend_uses_public_edge" {
  data "google_compute_backend_service" "public" {
    name = "trusted-router-public-backend"
  }

  assert {
    condition     = endswith(coalesce(data.google_compute_backend_service.public.security_policy, "-"), "/securityPolicies/${google_compute_security_policy.public_edge.name}")
    error_message = "trusted-router-public-backend no longer uses the trusted-router-public-edge Cloud Armor policy."
  }
}

check "lightning_backend_uses_lightning_funding" {
  data "google_compute_backend_service" "lightning" {
    name = "lightning-router-web"
  }

  assert {
    condition     = endswith(coalesce(data.google_compute_backend_service.lightning.security_policy, "-"), "/securityPolicies/${google_compute_security_policy.lightning_funding.name}")
    error_message = "lightning-router-web no longer uses the lightning-router-funding Cloud Armor policy."
  }
}
