# AllyRouter operational alias

`allyrouter.com` is an independent first-party alias for TrustedRouter. It is
served without redirecting so it remains usable if the canonical domain has a
registrar or DNS incident. Search-engine canonical tags still identify
`trustedrouter.com` as the canonical content source.

## Public hostnames

| Hostname | Target | TLS termination |
|---|---|---|
| `allyrouter.com` | TrustedRouter control-plane load balancer | Google external HTTPS load balancer |
| `www.allyrouter.com` | Control-plane load balancer, then 308 to apex | Google external HTTPS load balancer |
| `status.allyrouter.com` | Status surface on the control plane | Google external HTTPS load balancer |
| `trust.allyrouter.com` | Trust surface on the control plane | Google external HTTPS load balancer |
| `api.allyrouter.com` | Attested API regional TCP load balancers | Inside GCP Confidential Space |

The API alias must never be proxied through a CDN or terminated on the control
plane. Its ACME private key is generated and retained inside each attested
gateway workload.

## Route 53 delegation

The hosted zone is `Z09662142UE0IQL51B13V`. Its authoritative nameservers are:

```text
ns-1324.awsdns-37.org
ns-1917.awsdns-47.co.uk
ns-556.awsdns-05.net
ns-82.awsdns-10.com
```

Run `scripts/deploy/ensure_allyrouter_alias.sh` after DNS changes to create and
attach the control-plane certificate idempotently. Gateway deployment adds
`api.allyrouter.com` to the enclave ACME allowlist in every region.

## Auth behavior

Email and wallet sessions are host-only and work independently on either apex.
SIWE challenges bind to the apex that initiated the request. Google and GitHub
callbacks are also same-origin; each OAuth provider must list the AllyRouter
callback URI before enabling its button there.

## Registrar transfer

Route 53 can host DNS before it becomes the registrar. The current registrar's
60-day transfer lock must expire before starting the Route 53 Domains transfer.
Keep Route 53 nameservers unchanged during the transfer so there is no DNS
cutover. Never store the registrar authorization code in this repository.
