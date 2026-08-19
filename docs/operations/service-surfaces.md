# Service-surface bulkheads

The TrustedRouter FastAPI image runs as an explicit, fail-closed process role.
Production must never use the local/test-only `combined` role.

| Surface | Internet ownership | Background ownership | Capacity contract |
|---|---|---|---|
| `public` | Marketing, static pages, status, public catalog | None | concurrency 4, max 10, min 0, Spanner pool 1 |
| `actions` | Exact anonymous `POST /support/inquiry` and `POST /trustedos/inquiry` handlers | None | concurrency 4, max 2, min 0, 30s timeout, no shared Store |
| `control` | Login, existing-user console, account management, checkout, signed external webhooks, MCP and the authenticated browser proxy | Activation reminders | concurrency 4, max 20, warm min 1, Spanner pool 2 |
| `internal` | Gateway authorize/settle/refund, federation, drains and synthetic callbacks | Deferred settlement, synthetic monitor and remediator | concurrency 8, max 50, warm min 2, Spanner pool 8 |
| `observer` | AWS/Azure status and public catalog | Synthetic monitor and remediator | concurrency 4, max 4 |

The GCP global load balancer applies the same path contract to the apex and
wildcard forms of `trustedrouter.com`, `allyrouter.com`, and `uptimerouter.com`.
The public service is the first-party default. The two exact inquiry POST paths
select the low-capacity actions service, explicit account paths select the
control service, and `/internal/*` selects billing except for the enumerated
signed-webhook and browser-key control exceptions. Both unprefixed and `/v1`
API forms are tested against the actual FastAPI route inventory. This preserves
the `https://trustedrouter.com(/v1)` URL measured into attested gateway images.

Cloud Run default service URLs are not an alternate route: each service uses
`internal-and-cloud-load-balancing` ingress. The load balancer is still
Internet-facing, so internal paths continue to require the internal gateway or
federation credential and external callbacks continue to require their
provider signature. Authentication must run before request-body or Store work.

## Secrets and shared storage

The public process receives no gateway, payment, email, Sentry, or BYOK secret.
Public and control share only a dedicated attribution-cookie secret so a
campaign cookie issued on a marketing page can be consumed at signup. Reusing
the internal gateway token for that purpose is rejected at startup. The actions
process receives only the SES sender credentials and operations-chat credential
needed by its two handlers; it receives no attribution, Store, gateway,
payment, Sentry, or BYOK credential.

The public service still has bounded, read-oriented access to shared data:

- status pages read synthetic samples and rollups;
- public user-model pages can read published model records;
- operational status can read the dedicated ClickHouse control-reader view.

This residual dependency is intentionally capped rather than claimed away. A
public instance has one Spanner session, at most ten public instances run per
region, `/ready` is process-only, and public/observer application rate limiting
never writes a durable counter. Public instances register no account, billing,
gateway, federation, webhook, or writer-worker route. Cloud CDN and the edge
policy remain the primary anonymous-traffic controls. A future dedicated
read-replica or static status snapshot can remove the remaining shared-database
read dependency without changing the route contract.

## Global account-creation brake

`TR_NEW_SIGNUPS_ENABLED` gates creation, not authentication. Setting it to
`false` blocks plain-email signup, first-time Google/GitHub OAuth (including
delegated signup), first-time wallet accounts, and workspace invites that would
create an unknown user. Returning OAuth, wallet, session, and API-key users
continue to sign in and operate normally.

Use the checked operator command; do not edit a Cloud Run service by hand:

```bash
bash scripts/deploy/set_new_signups.sh disable
bash scripts/deploy/set_new_signups.sh enable
```

The command preflights every region as a `control` service, creates a
no-traffic revision, explicitly moves 100% traffic to it, and verifies the
value on the serving revision. Normal rollouts read and preserve that live
value, so deploying code cannot accidentally reopen registration. AWS/Azure
observer deployments pin signups off and do not register account routes.

## Deployment and failure behavior

The initial split is ordered to avoid a compatibility outage:

1. Deploy public, actions, and billing companions successfully in every intended region.
2. Create and attach their independent NEGs/backends and capacity policies.
3. Import the tested four-service URL map while the old combined control
   revision can still serve every control path.
4. Deploy the new control-only revision and use the existing staged traffic
   workflow.

The URL map is never changed after a partial companion deployment. Public CDN
is enabled only on the public backend; actions, authenticated control, and
billing have independent non-CDN backends and autoscaling budgets. Exhausting
the anonymous rendering or form-submission budget therefore does not consume
their protected Cloud Run concurrency or instance quota. Spanner remains a
shared managed dependency, so sustained anonymous read pressure can still
compete below the isolated pools; monitor session count, aborts, latency, and
processing-unit saturation during an event.

The AWS App Runner and Azure Container Apps deployments use `observer`, not a
second account or billing authority. They retain synthetic ownership needed by
their status hosts but cannot expose signup, console, home-gateway settlement,
or federation-money routes. Their gateway authorize/settle dependency remains
the measured GCP same-host endpoint until gateway images and attestation pins
are deliberately changed together.

## Integration status

This change supplies the fail-closed application roles, four-backend URL-map
transformer, route-totality tests, signup operator switch, and regional
observer configuration. The production `scripts/deploy/rollout.sh` cutover is
intentionally a separate integration patch: it must deploy the `-public`,
`-actions`, and `-billing` companions in every region, apply the independent
capacity and secret allowlists above, preserve the serving control revision's
signup flag, and invoke `service_surface_url_map.py` before moving the legacy
service to `control`. Until that reviewed rollout patch lands, the helper must
not be imported manually against the production URL map.
