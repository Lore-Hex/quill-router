# The control load balancer's URL map, adopted from the live map on
# 2026-10-19 (docs/design/fast-admission-enclave-direct.md, E1): every host
# the control plane and the token exchange sites answer on, routed to the
# public and control Cloud Run backends and the exchange sites' bucket. The
# backend services, the backend bucket and the forwarding rule are not
# managed here and are named by their self links. The edge's route for the
# enclave's calls is added here once this map is state.
#
# The exchange sites' rules come from sites/token-exchange/markets.json, one
# matcher for a market's domain and one for its aliases' redirects, in the
# file's order, as sites/token-exchange/deploy.py wrote them before this map
# was Terraform's. Adding a market is editing that file and merging; the
# site's publish uploads assets and certificates and routes nothing.
#
# Path matchers are a list, so their order is the live map's: the control
# plane's first, then each market's two. Host rules and paths are sets.

locals {
  exchange_markets = jsondecode(file("${path.module}/../sites/token-exchange/markets.json"))
  exchange_bucket  = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
  exchange_pages = [
    { path = "/", file = "index.html" },
    { path = "/index.html", file = "index.html" },
    { path = "/robots.txt", file = "robots.txt" },
    { path = "/sitemap.xml", file = "sitemap.xml" },
  ]
  exchange_matchers = flatten([
    for market in local.exchange_markets : [
      {
        name     = "exchange-${market.slug}"
        hosts    = [market.domain]
        slug     = market.slug
        redirect = null
      },
      {
        name     = "exchange-${market.slug}-aliases"
        hosts    = concat(["www.${market.domain}"], market.aliases, [for alias in market.aliases : "www.${alias}"])
        slug     = market.slug
        redirect = market.domain
      },
    ]
  ])
}

import {
  to = google_compute_url_map.control
  id = "projects/quill-cloud-proxy/global/urlMaps/trusted-router-control-map"
}

resource "google_compute_url_map" "control" {
  name            = "trusted-router-control-map"
  default_service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"

  host_rule {
    hosts = [
      "allyrouter.com", "eu.trustedrouter.com", "status-eu.trustedrouter.com", "status-us.trustedrouter.com",
      "status.allyrouter.com", "status.trustedrouter.com", "status.uptimerouter.com", "trust.allyrouter.com",
      "trust.trustedrouter.com", "trust.uptimerouter.com", "trustedrouter.com", "uptimerouter.com",
      "www.allyrouter.com", "www.trustedrouter.com", "www.uptimerouter.com",
    ]
    path_matcher = "trusted-router-service-surfaces"
  }

  dynamic "host_rule" {
    for_each = local.exchange_matchers
    content {
      hosts        = host_rule.value.hosts
      path_matcher = host_rule.value.name
    }
  }

  path_matcher {
    name            = "trusted-router-service-surfaces"
    default_service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"

    path_rule {
      paths = [
        "/analytics/events", "/notify/texml", "/v1/health", "/v1/ready", "/v1/coverage/openrouter",
        "/v1/analytics/events", "/v1/embeddings/models", "/images/models", "/images/models/*",
        "/v1/images/models", "/v1/images/models/*", "/v1/models", "/v1/models/count", "/v1/models/picker",
        "/v1/models/*", "/v1/notify/texml", "/v1/endpoints/zdr", "/v1/regions", "/v1/providers",
      ]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"
    }
    path_rule {
      paths   = ["/support/inquiry", "/trustedos/inquiry", "/token-exchange/brief"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
    path_rule {
      paths = [
        "/v1", "/v1/*", "/models/user", "/v1/models/user", "/bedrock-group-buy", "/bedrock-group-buy/*",
        "/google_oauth_callback", "/github_oauth_callback", "/console", "/console/*", "/provider", "/provider/*",
        "/mcp", "/chat-proxy", "/chat-proxy/*", "/auth", "/auth/*", "/byok", "/byok/*", "/credits", "/credits/*",
        "/billing", "/billing/*", "/broadcast", "/broadcast/*", "/custom-models", "/custom-models/*",
        "/user-models", "/user-models/*", "/key", "/keys", "/keys/*", "/activity", "/generation", "/generation/*",
        "/client-events", "/workspaces", "/workspaces/*", "/organization", "/organization/*", "/chat/completions",
        "/messages", "/embeddings", "/responses", "/audio", "/audio/*", "/rerank", "/videos", "/videos/*",
        "/private", "/private/*", "/guardrails", "/guardrails/*", "/analytics", "/analytics/*",
        "/classifications", "/classifications/*", "/datasets", "/datasets/*", "/files", "/files/*", "/images",
        "/images/*", "/model", "/model/*", "/observability", "/observability/*", "/presets", "/presets/*",
        "/scim", "/scim/*", "/signup", "/notify", "/notify/*", "/internal/stripe/webhook",
        "/internal/paypal/webhook", "/internal/adyen/webhook", "/internal/veriff/webhook",
        "/internal/ses/notifications", "/internal/chat/issue-browser-key", "/v1/internal/stripe/webhook",
        "/v1/internal/paypal/webhook", "/v1/internal/adyen/webhook", "/v1/internal/veriff/webhook",
        "/v1/internal/ses/notifications", "/v1/internal/chat/issue-browser-key",
      ]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
    path_rule {
      paths   = ["/internal", "/internal/*", "/v1/internal", "/v1/internal/*"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
    # The enclave's authorize (docs/design/fast-admission-enclave-direct.md,
    # E1): a share of it through the fast path's edge, which checks the boot
    # signature and passes the call to Python unchanged with its verdict;
    # Python still decides, and compares the verdict on this route only.
    # Authorize alone: the enclave retries it (three attempts within 28 s),
    # so a 503 from an unhealthy edge, which the weighted split does not
    # fail over, is retried and lands on the control backend almost surely;
    # the terminals (settle, refund, heartbeat, disposition) are not all
    # retried, and the maintenance drains run longer than the edge
    # backend's 8 s. The edge runs in us-central1 only while the control
    # backend serves every region, so the share is small; its weight at 0
    # is the way back.
    path_rule {
      paths = ["/internal/gateway/authorize"]
      route_action {
        weighted_backend_services {
          backend_service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
          weight          = 100 - local.fastpath_edge_route_weight
        }
        weighted_backend_services {
          backend_service = google_compute_backend_service.fastpath_edge.id
          weight          = local.fastpath_edge_route_weight
        }
      }
    }
  }

  dynamic "path_matcher" {
    for_each = local.exchange_matchers
    content {
      name            = path_matcher.value.name
      default_service = path_matcher.value.redirect == null ? local.exchange_bucket : null

      dynamic "default_url_redirect" {
        for_each = path_matcher.value.redirect == null ? [] : [path_matcher.value.redirect]
        content {
          host_redirect          = default_url_redirect.value
          https_redirect         = true
          redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
          strip_query            = false
        }
      }

      dynamic "path_rule" {
        for_each = path_matcher.value.redirect == null ? local.exchange_pages : []
        content {
          paths   = [path_rule.value.path]
          service = local.exchange_bucket
          route_action {
            url_rewrite {
              path_prefix_rewrite = "/${path_matcher.value.slug}/${path_rule.value.file}"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # Every host the company answers on routes through this map: a plan
    # that would delete or replace it fails.
    prevent_destroy = true
  }
}
