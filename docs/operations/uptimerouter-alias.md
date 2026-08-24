# UptimeRouter operational alias

`uptimerouter.com` is an independent first-party alias for TrustedRouter. It
serves the complete website, status surface, trust surface, console, and API
without redirecting through a TrustedRouter or QuillRouter domain.

## Public hostnames

| Hostname | Target | TLS termination |
|---|---|---|
| `uptimerouter.com` | Multi-region control-plane load balancer | Google external HTTPS load balancer |
| `www.uptimerouter.com` | Control plane, then 308 to apex | Google external HTTPS load balancer |
| `status.uptimerouter.com` | Public status surface | Google external HTTPS load balancer |
| `trust.uptimerouter.com` | Public trust and attestation surface | Google external HTTPS load balancer |
| `api.uptimerouter.com` | Direct Route53 A records to attested gateways | Inside GCP Confidential Space |

The API record is not a CNAME. The gateway health reconciler copies the
attestation-gated healthy IP set into Route53 and freezes the last-good set on
any validation failure. Each gateway includes `api.uptimerouter.com` in its
in-enclave ACME allowlist.

The trust surface first reads the canonical published release and then falls
back to the independently hosted, committed release artifact on GitHub. Both
sources pass the same issuer, audience, repository, commit, image, and digest
validation. If neither validates, the trust endpoints return 503 rather than
silently serving an old embedded digest.

## DNS

Route53 hosted zone: `Z00893363GIOMU7Z8647K`

```text
ns-855.awsdns-42.net
ns-509.awsdns-63.com
ns-1544.awsdns-01.co.uk
ns-1243.awsdns-27.org
```

## Authentication

UptimeRouter has independent Google and GitHub OAuth clients. Both callbacks
remain on `uptimerouter.com`; signed state binds the initiating hostname and
host-only cookies keep sessions isolated from other product domains.

Run `scripts/deploy/ensure_uptimerouter_alias.sh` after DNS changes to
idempotently create and attach the Google-managed control-plane certificate.
