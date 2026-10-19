# The control load balancer's URL map, adopted from the live map on
# 2026-10-19 (docs/design/fast-admission-enclave-direct.md, E1): every host
# the control plane and the token exchange sites answer on, routed to the
# public and control Cloud Run backends and the exchange sites' bucket. The
# backend services, the backend bucket and the forwarding rule are not
# managed here and are named by their self links. The edge's route for the
# enclave's calls is added here once this map is state.
#
# Host rules and path matchers are in the live map's order; a path rule's
# paths too, since the API keeps them.

import {
  to = google_compute_url_map.control
  id = "projects/quill-cloud-proxy/global/urlMaps/trusted-router-control-map"
}

resource "google_compute_url_map" "control" {
  name            = "trusted-router-control-map"
  default_service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"

  host_rule {
    hosts        = ["allyrouter.com", "eu.trustedrouter.com", "status-eu.trustedrouter.com", "status-us.trustedrouter.com", "status.allyrouter.com", "status.trustedrouter.com", "status.uptimerouter.com", "trust.allyrouter.com", "trust.trustedrouter.com", "trust.uptimerouter.com", "trustedrouter.com", "uptimerouter.com", "www.allyrouter.com", "www.trustedrouter.com", "www.uptimerouter.com"]
    path_matcher = "trusted-router-service-surfaces"
  }

  host_rule {
    hosts        = ["thetokenexchange.com"]
    path_matcher = "exchange-global"
  }

  host_rule {
    hosts        = ["www.thetokenexchange.com", "thetoken.exchange", "www.thetoken.exchange"]
    path_matcher = "exchange-global-aliases"
  }

  host_rule {
    hosts        = ["nytokenexchange.com"]
    path_matcher = "exchange-new-york"
  }

  host_rule {
    hosts        = ["www.nytokenexchange.com", "newyorktoken.exchange", "nytoken.exchange", "www.newyorktoken.exchange", "www.nytoken.exchange"]
    path_matcher = "exchange-new-york-aliases"
  }

  host_rule {
    hosts        = ["chicagotokenexchange.com"]
    path_matcher = "exchange-chicago"
  }

  host_rule {
    hosts        = ["www.chicagotokenexchange.com", "chicagotoken.exchange", "www.chicagotoken.exchange"]
    path_matcher = "exchange-chicago-aliases"
  }

  host_rule {
    hosts        = ["sftokenexchange.com"]
    path_matcher = "exchange-san-francisco"
  }

  host_rule {
    hosts        = ["www.sftokenexchange.com", "sftoken.exchange", "www.sftoken.exchange"]
    path_matcher = "exchange-san-francisco-aliases"
  }

  host_rule {
    hosts        = ["texastokenexchange.com"]
    path_matcher = "exchange-texas"
  }

  host_rule {
    hosts        = ["www.texastokenexchange.com", "texastoken.exchange", "txtokenexchange.com", "txtoken.exchange", "www.texastoken.exchange", "www.txtokenexchange.com", "www.txtoken.exchange"]
    path_matcher = "exchange-texas-aliases"
  }

  host_rule {
    hosts        = ["usatokenexchange.com"]
    path_matcher = "exchange-united-states"
  }

  host_rule {
    hosts        = ["www.usatokenexchange.com", "usatoken.exchange", "ustoken.exchange", "www.usatoken.exchange", "www.ustoken.exchange"]
    path_matcher = "exchange-united-states-aliases"
  }

  host_rule {
    hosts        = ["eutokenexchange.com"]
    path_matcher = "exchange-europe"
  }

  host_rule {
    hosts        = ["www.eutokenexchange.com", "eurotokenexchange.com", "eutoken.exchange", "www.eurotokenexchange.com", "www.eutoken.exchange"]
    path_matcher = "exchange-europe-aliases"
  }

  host_rule {
    hosts        = ["londontoken.exchange"]
    path_matcher = "exchange-london"
  }

  host_rule {
    hosts        = ["www.londontoken.exchange", "ltokenexchange.com", "www.ltokenexchange.com"]
    path_matcher = "exchange-london-aliases"
  }

  host_rule {
    hosts        = ["dubaitokenexchange.com"]
    path_matcher = "exchange-dubai"
  }

  host_rule {
    hosts        = ["www.dubaitokenexchange.com", "dubaitoken.exchange", "www.dubaitoken.exchange"]
    path_matcher = "exchange-dubai-aliases"
  }

  host_rule {
    hosts        = ["riyadhtokenexchange.com"]
    path_matcher = "exchange-riyadh"
  }

  host_rule {
    hosts        = ["www.riyadhtokenexchange.com"]
    path_matcher = "exchange-riyadh-aliases"
  }

  host_rule {
    hosts        = ["hktokenexchange.com"]
    path_matcher = "exchange-hong-kong"
  }

  host_rule {
    hosts        = ["www.hktokenexchange.com", "hktoken.exchange", "hongkongtoken.exchange", "www.hktoken.exchange", "www.hongkongtoken.exchange"]
    path_matcher = "exchange-hong-kong-aliases"
  }

  host_rule {
    hosts        = ["shanghaitokenexchange.com"]
    path_matcher = "exchange-shanghai"
  }

  host_rule {
    hosts        = ["www.shanghaitokenexchange.com", "shanghaitoken.exchange", "www.shanghaitoken.exchange"]
    path_matcher = "exchange-shanghai-aliases"
  }

  host_rule {
    hosts        = ["tokyotokenexchange.com"]
    path_matcher = "exchange-tokyo"
  }

  host_rule {
    hosts        = ["www.tokyotokenexchange.com"]
    path_matcher = "exchange-tokyo-aliases"
  }

  path_matcher {
    name            = "trusted-router-service-surfaces"
    default_service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"
    path_rule {
      paths   = ["/analytics/events", "/notify/texml", "/v1/health", "/v1/ready", "/v1/coverage/openrouter", "/v1/analytics/events", "/v1/embeddings/models", "/images/models", "/images/models/*", "/v1/images/models", "/v1/images/models/*", "/v1/models", "/v1/models/count", "/v1/models/picker", "/v1/models/*", "/v1/notify/texml", "/v1/endpoints/zdr", "/v1/regions", "/v1/providers"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-public-backend"
    }
    path_rule {
      paths   = ["/support/inquiry", "/trustedos/inquiry", "/token-exchange/brief"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
    path_rule {
      paths   = ["/v1", "/v1/*", "/models/user", "/v1/models/user", "/bedrock-group-buy", "/bedrock-group-buy/*", "/google_oauth_callback", "/github_oauth_callback", "/console", "/console/*", "/provider", "/provider/*", "/mcp", "/chat-proxy", "/chat-proxy/*", "/auth", "/auth/*", "/byok", "/byok/*", "/credits", "/credits/*", "/billing", "/billing/*", "/broadcast", "/broadcast/*", "/custom-models", "/custom-models/*", "/user-models", "/user-models/*", "/key", "/keys", "/keys/*", "/activity", "/generation", "/generation/*", "/client-events", "/workspaces", "/workspaces/*", "/organization", "/organization/*", "/chat/completions", "/messages", "/embeddings", "/responses", "/audio", "/audio/*", "/rerank", "/videos", "/videos/*", "/private", "/private/*", "/guardrails", "/guardrails/*", "/analytics", "/analytics/*", "/classifications", "/classifications/*", "/datasets", "/datasets/*", "/files", "/files/*", "/images", "/images/*", "/model", "/model/*", "/observability", "/observability/*", "/presets", "/presets/*", "/scim", "/scim/*", "/signup", "/notify", "/notify/*", "/internal/stripe/webhook", "/internal/paypal/webhook", "/internal/adyen/webhook", "/internal/veriff/webhook", "/internal/ses/notifications", "/internal/chat/issue-browser-key", "/v1/internal/stripe/webhook", "/v1/internal/paypal/webhook", "/v1/internal/adyen/webhook", "/v1/internal/veriff/webhook", "/v1/internal/ses/notifications", "/v1/internal/chat/issue-browser-key"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
    path_rule {
      paths   = ["/internal", "/internal/*", "/v1/internal", "/v1/internal/*"]
      service = "projects/${local.gcp_project_id}/global/backendServices/trusted-router-control-backend"
    }
  }

  path_matcher {
    name            = "exchange-global"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/global/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/global/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/global/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/global/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-global-aliases"
    default_url_redirect {
      host_redirect          = "thetokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-new-york"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/new-york/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/new-york/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/new-york/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/new-york/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-new-york-aliases"
    default_url_redirect {
      host_redirect          = "nytokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-chicago"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/chicago/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/chicago/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/chicago/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/chicago/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-chicago-aliases"
    default_url_redirect {
      host_redirect          = "chicagotokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-san-francisco"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/san-francisco/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/san-francisco/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/san-francisco/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/san-francisco/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-san-francisco-aliases"
    default_url_redirect {
      host_redirect          = "sftokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-texas"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/texas/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/texas/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/texas/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/texas/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-texas-aliases"
    default_url_redirect {
      host_redirect          = "texastokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-united-states"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/united-states/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/united-states/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/united-states/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/united-states/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-united-states-aliases"
    default_url_redirect {
      host_redirect          = "usatokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-europe"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/europe/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/europe/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/europe/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/europe/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-europe-aliases"
    default_url_redirect {
      host_redirect          = "eutokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-london"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/london/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/london/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/london/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/london/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-london-aliases"
    default_url_redirect {
      host_redirect          = "londontoken.exchange"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-dubai"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/dubai/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/dubai/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/dubai/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/dubai/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-dubai-aliases"
    default_url_redirect {
      host_redirect          = "dubaitokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-riyadh"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/riyadh/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/riyadh/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/riyadh/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/riyadh/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-riyadh-aliases"
    default_url_redirect {
      host_redirect          = "riyadhtokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-hong-kong"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/hong-kong/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/hong-kong/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/hong-kong/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/hong-kong/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-hong-kong-aliases"
    default_url_redirect {
      host_redirect          = "hktokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-shanghai"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/shanghai/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/shanghai/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/shanghai/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/shanghai/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-shanghai-aliases"
    default_url_redirect {
      host_redirect          = "shanghaitokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  path_matcher {
    name            = "exchange-tokyo"
    default_service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
    path_rule {
      paths   = ["/"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/tokyo/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/index.html"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/tokyo/index.html"
        }
      }
    }
    path_rule {
      paths   = ["/robots.txt"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/tokyo/robots.txt"
        }
      }
    }
    path_rule {
      paths   = ["/sitemap.xml"]
      service = "projects/${local.gcp_project_id}/global/backendBuckets/token-exchange-static"
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/tokyo/sitemap.xml"
        }
      }
    }
  }

  path_matcher {
    name = "exchange-tokyo-aliases"
    default_url_redirect {
      host_redirect          = "tokyotokenexchange.com"
      https_redirect         = true
      redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
      strip_query            = false
    }
  }

  lifecycle {
    # Every host the company answers on routes through this map: a plan
    # that would delete or replace it fails.
    prevent_destroy = true
  }
}
