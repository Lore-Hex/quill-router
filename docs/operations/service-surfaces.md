# Service-surface bulkheads

The TrustedRouter FastAPI image runs as an explicit, fail-closed process role.
Deployed `combined` mode is rejected by default. The sole temporary exception
is the guarded GCP compatibility bridge described under Integration status;
it exists only because the #714 enforcement landed before the #712 production
split and requires a second, explicit opt-in.

| Surface | Internet ownership | Background ownership | Capacity contract |
|---|---|---|---|
| `public` | Marketing, static pages, status, public catalog | None | concurrency 4, max 10, min 0, Spanner pool 1 |
| `actions` | Exact anonymous `POST /support/inquiry` and `POST /trustedos/inquiry` handlers | None | concurrency 4, max 2, min 0, 30s timeout, no shared Store |
| `control` | Login, existing-user console, account management, checkout, signed external webhooks, MCP and the authenticated browser proxy | Activation reminders | concurrency 4, max 20, warm min 1, Spanner pool 2 |
| `internal` | Gateway authorize/settle/refund, federation, drains and synthetic callbacks | Deferred settlement, synthetic monitor and remediator | concurrency 8, max 50, warm min 2, Spanner pool 8 |
| `observer` | AWS/Azure status, public catalog, and authenticated synthetic ingest | AWS: existing EventBridge rule; Azure: in-process synthetic monitor and remediator, with exactly one replica | AWS max 4; Azure max 1 while either loop is enabled |

The GCP global load balancer applies the same path contract only to the exact
managed apex, `www`, `status`, and `trust` hosts for `trustedrouter.com`,
`allyrouter.com`, and `uptimerouter.com`, plus the enumerated TrustedRouter
regional/status hosts. The transformer refuses a first-party wildcard rule:
`api*`, AWS, Azure, attested, alerting, and other operational subdomains must
remain on their existing front doors.
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

The rollout also sets the non-secret
`TR_GOOGLE_OAUTH_LOGIN_AVAILABLE` and
`TR_GITHUB_OAUTH_LOGIN_AVAILABLE` flags with identical values on `public` and
`control`. They let marketing pages
render the control-owned login links without copying either OAuth client secret
into the public process; a deployed public process rejects an omitted flag and
control rejects any supplied value that disagrees with its credentials.
The public `/openapi.json` is a deterministic, pre-serialized build asset made
from a credential-free combined route inventory, with all `/internal/*` RPCs
and unreachable components removed. Its drift test runs the generator in an
isolated memory/test environment. The API reference therefore keeps documenting
customer control and inference routes without mounting them on the public
service or rebuilding a large schema during attack-driven cold starts.

Each deployed surface also needs a distinct runtime identity and database role.
In particular, the public/actions identities cannot retain the default service
account's project-wide Secret Manager or storage grants, and the AWS/Azure
observer cannot use an administrator database principal. Provider keys and
other secrets read directly from process environment (rather than `Settings`)
must be stripped and post-verified too. The route split is not a compromise
boundary until these IAM/data-plane checks pass.

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
not be imported manually against the production URL map. The same patch must
create and verify the four least-privilege runtime identities; an
environment-only secret allowlist is insufficient.

Because #714's enforcement landed before #712's production topology, the
legacy service would otherwise refuse to start before the companion services
and URL map exist. Until the full cutover in #712 lands, the guarded GCP
workflow therefore explicitly sets
`TR_SERVICE_SURFACE=combined` together with the temporary
`TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true` bridge. The application and rollout
both reject deployed combined mode without that second opt-in. The bridge also
requires `TR_RATE_LIMIT_ENABLED=false`: the legacy backend does not yet receive
the edge-overwritten client identity, so #714's process limiter would otherwise
collapse all Internet traffic into one bucket. Its existing synthetic and
Sentry callers continue using the legacy internal gateway token only on this
explicit bridge; split `internal` and `observer` services remain restricted to
their dedicated observer token. The bridge also preserves the legacy
session-aware `/bedrock-group-buy` page and its form return URLs; split
`public` remains anonymous/cache-safe and split `control` owns the private
`/bedrock-group-buy/manage` page. The two bounded inquiry/support form limiters
likewise retain their pre-#714 socket-client identity only on the bridge;
split `actions` continues to require the edge-overwritten identity contract.
Remove the flag, rate-limit exception, legacy credential, form-identity and
group-buy selections, and workflow wiring when #712 installs the split edge
identity, services, and callers together. This is not an alternative
production topology.

The regional-quota reconciler Cloud Run Job also declares an explicit
`TR_SERVICE_SURFACE=control`. It is a one-shot `worker` CLI and mounts no HTTP
routes; `control` is the narrow role compatible with its existing Sentry and
account-ledger storage bindings without granting either an internal gateway or
observer credential. Worker validation skips the interactive control
service's Stripe, attribution-cookie, and OAuth requirements.
