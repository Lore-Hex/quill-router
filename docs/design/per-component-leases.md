# Per-component gcp leases: not safe yet

Question: can the control plane (Cloud Run) and the enclave gateway (MIGs and
DNS) hold separate gcp leases, `gcp/control-plane` and `gcp/gateway`, and roll
at the same time? The prize is large. From 2026-11-09 to 11-13 the gcp lease
was busy 83% of the time; split into two lanes, the control plane alone would
be busy 53% and the gateway 30%, and the 20 hours that ten control-plane runs
spent waiting behind gateway rollouts would mostly disappear
(`deploy-fifo-admission.md`).

**Conclusion: not with today's checks.** Each rollout's verdict reads signals
the other rollout changes, and one of those couplings can make a broken
release pass. This document records the evidence and the smallest sequence
of changes that would make per-component leases safe. Nothing here is
implemented.

## Evidence

Collected 2026-11-13 from the code at `origin/main` in both repositories and
the live `https://trustedrouter.com/status.json`.

**E1. The control-plane canary's verdict for us-central1 is made only of
gateway probes.** The primary canary and every staged-ramp watch run
`scripts/deploy/watchdog.py --regions us-central1 --slo-class router_core`.
`fetch_per_region` takes `slo_classes.router_core.current_by_region["us-central1"]`
and falls back to `current.checks[]` rows whose `target_region` is
us-central1. Live, `router_core.current_by_region` has keys `global` and
`us-central1`, and the only router_core checks with `target_region`
us-central1 are `tls_health` and `attestation_nonce` (canonical and regional
targets). The router_core probes that exercise the control plane,
`gateway_authorize`, `gateway_settle` and `provider_fallback`, are emitted with
target `control-plane` and `target_region` null, so
`status._sample_region_keys` files them under `global`, which the canary
never reads.

**E2. Those probes never touch the control plane.** The enclave serves
`/health` entirely inside itself: "It does not authenticate, read storage,
call the control plane, mint an attestation token, or touch a provider"
(quill-cloud-proxy `enclave-go/cmd/enclave/main.go`). `/attestation` is also
answered by the enclave. Two consequences:
- A gateway drain or MIG replacement in us-central1 during the control-plane
  canary or a 10/50/100 ramp step reads as a control-plane failure and rolls
  back a healthy revision.
- Independently of concurrency, the control-plane canary cannot see a
  control-plane regression in us-central1 at all. That is worth fixing on its
  own (step S1).

**E3. A concurrent drain can make a broken control plane pass.** The
watchdog's baseline is per region, not per probe: a region that reads `down`
when the baseline is captured is excluded from rollback accounting for the
whole watch. A gateway rollout that has us-central1 down during that capture
removes us-central1 from the control-plane canary entirely.

**E4. The gateway's gates run through the control plane, and cannot be
pinned to a known-good one.** `tools/wait-region-synthetic-up.sh` requires
`openai_sdk_pong` and `responses_pong`, and `tools/verify-region-before-dns.sh`
sends a PONG through each new instance; both call the control plane for
authorize and settle. The enclave reaches the control plane at
`TR_CONTROL_PLANE_BASE_URL`, default `https://trustedrouter.com`, which is set
in the measured instance template (`tee-env-TR_CONTROL_PLANE_BASE_URL` in
`tools/deploy-gcp-mig.sh`), the public surface `public_surface.sh` manages.
Pointing probes at a pinned Cloud Run revision would change the attested
workload. A control-plane ramp or public-surface change during a gateway
region's gates therefore reads as a gateway failure.

**E5. Releases are coupled too.** `Coordinator.release()` and `recover()` call
`probe_cloud(cloud)`, which requires both the control plane's `/health` and
the gateway's `/health` for that cloud. A gateway regression would hold a
finished control-plane lease, and the reverse.

**E6. The journal cannot express it compatibly.** Leases are keyed by cloud,
and every pinned client requires `set(leases) <= {"gcp", "aws", "azure"}` and
at most two leases. quill-cloud-proxy runs the coordinator pinned by digest,
so a `gcp/gateway` key would make every gateway admission, assert and release
fail with "invalid release journal leases".

## What would make it safe, smallest first

**S1. The control-plane canary measures the control plane** (quill-router,
`watchdog.py` and `deploy-production.yml`). Add a `control_plane_core` class:
`gateway_authorize`, `gateway_settle`, `gateway_authorize_settle` and
`control_plane_health` count toward rollback (`provider_fallback` reports at
most degraded), keyed by `monitor_region`, since the us-central1 monitor
reaches the us-central1 control plane through the load balancer. Take the
baseline per probe type, so one class going down cannot blind another (E3).
Before switching, measure each monitor's false-down rate for these probes;
not done here. While no gateway lease is held the canary may keep reading the
gateway probes as well; while one is held it reads `control_plane_core` only.

**S2. Gateway gates refuse to blame themselves for a moving control plane**
(quill-cloud-proxy). A failed PONG gate counts against the gateway only if
`control_plane_core` reads up for the same monitor region over the same
window. Otherwise the result is inconclusive: the region stays drained (it
serves no canonical traffic), is not promoted, and is re-checked until the
control-plane lease is released, then fails closed after a bound. The
gateway watchdog (`tls_health`, `attestation_nonce`) is already gateway-only.

**S3. Coordinator schema v3** (quill-router, then a coordinated re-pin).
- Leases keyed `cloud/component` for `control-plane`, `gateway` and
  `public-surface`.
- The two-cloud cap counts distinct clouds. The holdback is a cloud with no
  lease of any component and is still probed for both components.
- The same component on one cloud stays exclusive. `public-surface` stays
  exclusive with both others on that cloud: it rewires the endpoint every
  gateway PONG goes through (E4) and the load balancer the control-plane
  canary reads.
- `release` and `recover` probe only the releasing component (E5).
- FIFO per `cloud/component`, plus cap FIFO across clouds.
- Version 3 makes older clients fail closed ("legacy or unknown deployment
  lock exists") rather than misparse it. Ship it by letting both repositories'
  queues drain, merging quill-router, and re-pinning quill-cloud-proxy in the
  same window.

**Interleavings this permits, and why each verdict stays attributable:**

| Control plane doing | Gateway doing | Allowed after S1 to S3 | Why |
|---|---|---|---|
| migrate, warm revisions at 0% | anything | yes | no traffic or signal change |
| canary or ramp in us-central1 | draining or rolling us-central1 | yes | the canary reads `control_plane_core` only (S1); billing probes bypass the enclave |
| canary or ramp | PONG gates in any region | yes | a PONG failure while `control_plane_core` is down is inconclusive (S2) |
| public surface change | PONG gates | no | exclusive (S3) |
| `verify-cloud-complete`, release | gateway rolling | yes | release probes only the control plane (S3) |
| anything | DNS reconcile, drains | yes | enclave DNS is gateway-only; the control plane never writes it |

## The public-surface step

`public-surface-companion` imports the run's fence, `public_surface.sh`
asserts it, and `finalize-cloud` waits for it, so it runs inside the gcp lease.
The runbook sentence saying it "remains outside the mutex" was stale and is
corrected with this change. Moving it outside the lease is **not safe**: it
mutates gcp production, the load balancer behind `trustedrouter.com`, which
the two-cloud rule must cover, and which the gateway depends on (E4). It can
only become separable as the `public-surface` component of S3, still exclusive
with the gateway.

## Not verified

The false-down rate of the control-plane probes per monitor (needed before S1
switches the rollback signal), and how long a control-plane ramp keeps
`control_plane_core` down when a revision is bad (the bound S2 waits for).
