# Taking Python off the request hot path

Joseph asked on 2026-10-18: get rid of the Python control plane's slow hot
path. This is step 9 of the design's rollout
(`fast-admission-and-batched-settlement.md` §8), the end state of §4.1,
§4.4, §4.10 and §4.11: the enclave's admission, heartbeats and terminals are
answered by the Go fast path, and Python keeps routing policy, the catalog,
keys and everything off the hot path. It is cut into stages that each move
real traffic behind the pilot's per-workspace switch and each have a way
back, as the pilot does (`fast-admission-pilot.md`).

Why it is needed, from the capacity models of 2026-10-18: today's path
tops out at about 1.5 to 4.5T tokens a month, at the control plane's Cloud
Run cap (12 instances a region, concurrency 8) and the Spanner autoscaler's
10 nodes, with 2 to 3 commits a generation. The pilot's shape, Python
calling the front door, adds a commit to that and keeps Python's CPU on
every request. 100T needs the enclave calling the fast path directly, the
request records off Spanner (§9) and ClickHouse sharded; 1Q needs those and
per-workspace shards.

## 1. What the enclave can reach

The enclave calls exactly one billing authority, `https://trustedrouter.com`
(`quill-cloud-proxy` `internal/trustedrouter/endpoints.go`,
`validateControlPlaneEndpoint`); any other host is refused, and on AWS the
only control-plane address is a vsock tunnel compiled into the image and
measured into PCR0 (`http_client_aws.go`). Its paths are
`/internal/gateway/authorize`, `heartbeat`, `settle`, `refund` and
`authorizations/{id}/disposition`, authenticated by the internal token and
an Ed25519 boot signature over method, path and body
(`spend_lease.go`, `gateway_boot.py`).

So "the enclave calls the fast path" means the Go service answers behind
the same host and the same paths, with Python's JSON and error envelopes,
and the switch is a route at the control load balancer
(`infra/control_lb_proxy.tf`) plus the per-workspace decision inside Go. No
enclave image changes for the first increments, and no re-attestation of
the fleet.

The enclave consumes from authorize: `authorization_id`, `generation_id`,
`route_candidates` with their prices and BYOK references,
`candidate_prices` and `cap_micro` for heartbeats, `stage_d.eligible`,
`candidate_cost_reporting`, `tags` with `request_metadata_version`,
`broadcast_destinations`, the contract fields it checks and refunds on
(`receipt_fee_basis_points`, `additional_cost_reservation_microdollars`,
`estimated_cost_microdollars`, `video_token_billing`), and the error
envelopes it parses (401, 402, 403, 429, 503). Everything Go answers must
be byte-compatible on these; a shadow comparator proves it before any
request is taken (§8 step 6).

## 2. What Python computes per authorize today

In one request (`routes/internal/gateway.py`, `_authorize_gateway_sync_impl`):
the key, workspace, BYOK rows and boot row in one strong Spanner read
(`gateway_api_key_auth_context`); boot verification against the accepted
image digests; key caps, windows and `budget_strict`; the workspace's pause
and debt; idempotency key and fingerprint; the candidate endpoints
(catalog, region availability, BYOK, named decision chains, service tier,
health ranking with session affinity, `allow_fallbacks`); the estimate as
the largest candidate's tiered, scheduled price plus the receipt fee, the
custom-model markup, the additional cost and the app markup; the Stage D
eligibility and pricing snapshot; broadcast destinations; then one
read-write transaction that reserves credit on one shard row, reserves the
key, inserts the reservation and the authorization. The leased pilot
variant inserts the authorization (with the envelope) and a zero-hold
reservation carrying the idempotency scope, and reserves nothing.

The Go front door today takes the workspace and the estimate from its
caller and holds under a lease. It has no boot verification, no routing
snapshot, no key or workspace reads on the request path, no Stage D
payload, no caller authentication (network only, W5), and speaks the
spike's wire, not Python's.

## 3. Stages

Every stage: a pull request series with tests, a shadow or comparator
before any traffic, the pilot's per-workspace switch, the pilot's audit and
watch, and a way back that needs no deploy.

### E1. A Go edge in front, Python decides

- **What moves:** a Go edge on the fast path nodes, on a port of its own
  (W5 keeps `/v1` and `/owner` fleet-only), terminates the enclave's
  `/internal/gateway/*` for GCP enclaves behind `trustedrouter.com`: checks
  the internal token, verifies the boot signature (the header, a registered
  and verified boot from the `spend_lease_boot` rows, that boot's key over
  the method, the path and the exact body), and proxies the request to
  Python unchanged, returning Python's bytes unchanged. The signature
  alone: Python's further questions, whether the body names the key the
  signature is bound to and whether the boot's image digest is accepted,
  stay Python's until E2, where the edge decides and needs them.
- **Python keeps:** everything.
- **Where it runs:** on each node, beside the front door, when the unit
  passes `-edge-address` (its own port), `-edge-upstream` (Python's
  internal surface) and `-edge-token-secret` (the internal token, a pinned
  Secret Manager version, the same secret Python holds); `-edge-boot-wait`
  bounds the boot read. Nothing passes them yet. The edge itself answers
  `/internal/gateway/edge/health` without a token: 200 `{"status":"serving"}`
  while serving, 503 `{"status":"leaving"}` once the node begins leaving.
- **Switch and way back:** the load balancer route's weight. The edge
  decides nothing in E1, so it needs no "pass everything" flag. Nothing
  durable changes. The balancer cannot see a workspace, so the route sends a
  share of every workspace's authorize, not only the pilot's (#1903); every
  region's share lands on the us-central1 edge and Python.
- **Measured:** the edge's added latency (under 1 ms p50); the verdict's
  parity with Python's own check of the same signature
  (`verify_boot_signature`), logged on any difference;
  error-rate parity; the edge draining like a gateway at retirement and
  through a deploy (§4.12, W8).
- **Why first:** it proves the host, TLS, load balancer, caller
  authentication and deploy story before any money logic moves. The nodes
  need a load-balancer backend that follows membership, never addresses.

### E2. Go authorizes the cohort; Python keeps settle, heartbeat, records

- **What moves:** for a request that passes Go's cohort predicate (the
  pilot's `pilot_reason`, plus every candidate priced in credits), the
  edge looks the key up from a key snapshot, checks the workspace's state
  from a cache (pause causes, debt, trust tier and latch: the grant's own
  read), evaluates the routing snapshot for candidates, prices and the
  estimate, admits at the owner, and answers in Python's shape: the
  generation ID by Python's rule, `route_candidates`,
  `candidate_cost_reporting` by Python's predicate, `stage_d` not eligible
  (streams stay out), tags, broadcast destinations.
- **What Go still writes, synchronously:** the typed authorization row with
  the envelope and the zero-hold reservation row with the idempotency
  scope, exactly what `_authorize_gateway_leased` writes. Python's settle,
  heartbeat, disposition and reaper all key off that row, and the enclave's
  settle can arrive within milliseconds. One insert-only commit on
  uncontended rows replaces today's updates of the credit and key rows. It
  is not yet §4.4's warm path that writes nothing; it keeps every downstream
  contract and makes the way back trivial.
- **Out of cohort, or any cold or stale cache:** proxied to Python
  unchanged. Python's own pilot client stays on, so both deciders produce
  the same row shape; the per-workspace switch is one table with one
  meaning for both.
- **Switch and way back:** Go's "decide" flag per workspace, region and
  cloud; the load balancer route; `fastpathctl disable`, which drains the
  leases. Python settles Go-authorized rows as it settles its own leased
  rows today.
- **Measured:** in shadow first, Go computes and Python decides, with a
  comparator on candidates (ids and order), the estimate to the
  microdollar, the Stage D payload, the response bytes the enclave decodes
  and the error class; then in the pilot, authorize latency at the enclave
  (`AuthorizeDone`), Spanner commits a generation (target one insert), 402,
  503 and 429 parity, key-revocation lag, the passthrough rate from stale
  caches.

### E3. Streams: heartbeats to Go

- **Prerequisites, in the enclave (§9, §4.11):** the registration carries
  `capabilities.usage_heartbeat` (Python stores it since #1519; the enclave
  at `origin/main` sends none, so every stream is on Python today), and
  Python's declaration requirement is turned on once every accepted digest
  declares.
- **What moves:** Go decides Stage D by Python's rule, carries
  `candidate_prices` and `cap_micro` from the snapshot, and answers
  `/internal/gateway/heartbeat` in the enclave's wire, pricing the running
  usage from the frozen candidate with Python's half-up arithmetic, then
  hands it to the owner as a heartbeat. Python's heartbeat route stays for
  rows Python authorized.
- **Switch and way back:** Go's heartbeat switch answers `retry`, which
  ends running streams as today's 503 does; per workspace.
- **Measured:** first-heartbeat latency, heartbeat p99 against the
  enclave's 5 s budget, the reap rate, cap-reply mismatches.

### E4. Terminals to Go; records and key usage to the auditor

- **What moves:** Go answers settle and refund for its own authorizations:
  prices the charge from the envelope's frozen prices (§9's decision),
  publishes the full request record (the generation, activity and benchmark
  content Python writes today) to the record topic, forwards the terminal
  to the owner, and answers `settled`, `already_settled` or
  `intent_durable` in Python's shape. The auditor writes the authorization,
  generation and activity rows and books per-key usage to `tr_key_limit`
  (§4.6, §4.9; #1842 has the pilot's finalize doing it meanwhile). Only now
  is E2's synchronous insert dropped, per workspace, after the auditor has
  written records for a full retention window; the owner's
  invocation-nonce scope (§4.3) replaces the reservation's.
- **The enclave must change:** it sends an idempotency key on every
  authorize, so §4.3's "keyed requests stay on Python" cannot go by the
  key's presence; it must mark the client's. Its settle retry queue is in
  memory (1,024 jobs, six attempts), so the durable settle outbox (§9) is
  part of this stage.
- **Switch and way back:** per-workspace "terminals to Python", safe only
  while E2's insert still exists.
- **Measured:** record lag through the stager and auditor, the `pending`
  disposition rate, commits a generation (the auditor's batches only),
  per-key usage against the counter reconciler, charge parity against
  Python's shadow pricing.

### E5. The other clouds, then widening

AWS and Azure enclaves reach only their own plane and `trustedrouter.com`,
and their money lives on separate Postgres deployments that do not
federate. Either their GCP-ledger workspaces' traffic is routed to the GCP
fast path over the public host, or each cloud runs a fast path against its
own ledger. That is a design decision, not an increment; they stay on
Python until it is made. Then the §4.11 list, one at a time behind the
switch: capped, windowed and `budget_strict` keys behind the key-status
fence (§4.6), the priority and auto tiers, receipt fees, the remaining
route types. Python's authorize then retires for the cohort and stays as the
long-tail backend behind the edge.

## 4. The hard parts

1. **The routing snapshot (§4.10).** Go must reproduce Python's candidates
   and response from a signed, versioned snapshot: endpoints with provider,
   usage type, privacy posture and modalities; price tiers with their
   effective schedule; request fees and rounding; provider-to-region
   availability; aliases and canonical ids; decision chains; the health
   ranking's inputs. Per-workspace inputs under a maximum age: BYOK
   configuration, broadcast destinations, flags. Signed by the control plane
   with a key in Secret Manager (W6's pattern), with a freshness limit that
   stops admission when passed. Expect the first shadow rounds to differ on
   ordering from the health rank and session affinity, which is routing, not
   money: decide early whether Go reproduces the rank or carries a coarser
   order.
2. **Pricing parity to the microdollar.** The estimate is the largest
   candidate's tiered, scheduled, tier-aware price plus four fee layers;
   settle prices from the frozen snapshot. Build the Go evaluator from the
   billing-snapshot work (§4.13's `billingv1` evaluator and its shared
   cases) and add golden vectors for the estimate generated by Python; the
   comparator counts a catalog change between authorize and settle apart.
3. **Key revocation.** Python reads the key strongly on every request;
   Go's snapshot turns that into a maximum age (§4.6). A change feed, a
   Spanner change stream or a versioned revocation list Python publishes,
   and the per-workspace key-status fence, come before capped keys move.
   Bulk deletion (#1496) is the stress case.
4. **Idempotency keys.** E2 keeps the pilot's zero-hold scope row, so
   nothing changes; E4 needs the enclave's mark and the owner's nonce scope.
5. **Records and key usage to the auditor.** The full record Python sends
   the front door is a stub today; the auditor must write the generation,
   activity and authorization rows from the record topic, book key usage
   (creating a missing `tr_key_limit` row rather than failing), and
   lookups must answer from winners and records. This is the largest move
   and why E2 keeps a synchronous row.
6. **Boot verification in Go:** the boot registry and the signed digest
   policy with Python's 60 s refresh; heartbeat and disposition requests
   are signed over method and path, so the paths must not change behind the
   edge.
7. **Secrets in the response.** `route_candidates` carry encrypted BYOK
   secrets and broadcast destinations carry encrypted keys. The snapshot
   must not embed them; Go reads them from Spanner under the per-workspace
   cache.
8. **Caller authentication on the fleet.** `/v1` and `/owner` have none
   (network only). The edge is on its own port with its own firewall rule,
   or its own instance group.

## 5. Open questions

- Whether the control load balancer can take a VM backend group in
  `us-central1` by path without changing certificates (W6 and §4.12's
  receipt-key publication).
- The 402 against 503 when a balance's headroom sits in leases (§4.4, §8
  step 4) is not done; the enclave caches a 402 for 5 s per credential.
- Trust-tier enforcement in the grant before a workspace beyond the pilot
  joins (§4.11, §8 step 8); what leases do about a stale trust
  reconciliation (§9).
- The pricing basis for non-streaming and receipt-fee requests, envelope or
  catalog at settle (§9, §4.13): E2's parity does not depend on it, E4's
  does.
- Python's per-process guards, the exhausted-keys and exhausted-workspaces
  sets and the per-key in-flight gate, have no Go counterpart; Go must not
  learn "exhausted" from a lease's `busy`.
- Which Spanner roles the edge needs for key, boot and workspace reads
  (W5), and whether the key snapshot is Python's artefact or Go's read with
  change-stream invalidation.

## 6. Order of work

E1 first, as a pull request series: the edge's token and boot checks with
Python's own test vectors, the proxy, the load balancer backend and route
in Terraform, the drain at retirement, then the route: a 1% share of every
workspace's authorize, since the balancer cannot route by workspace. E2's shadow comparator and the routing snapshot
compiler in Python start alongside, since the snapshot's format is the long
pole. Nothing beyond E1 is scheduled until E1 has run a week on the pilot
cohort with parity.
