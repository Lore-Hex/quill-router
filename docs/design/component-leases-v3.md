# Component leases (journal schema v3)

Goal: the gcp control-plane deploy and the gcp gateway rollout run at the same
time. Today they share one `gcp` lease, which was held 83% of the time from
2026-11-09 to 11-13, so each waits through the other's whole rollout.

This implements steps S2 and S3 of `per-component-leases.md` (#2559). Step S1,
a control-plane canary that measures the control plane, is #2560.

| Piece | Where |
|---|---|
| Schema v3, component leases, `lanes`, `upgrade`, `downgrade` | `scripts/deploy/cloud_rollout.py` (this repository) |
| Gateway gates hold, not fail, while the control plane may be the cause | quill-cloud-proxy `tools/gate_hold.py`, `tools/gate-hold-lib.sh` |
| Coordinator re-pin | quill-cloud-proxy `tools/cloud-rollout-source.json` |

## Journal rules

A journal is v2 or v3. A fresh journal is v2, and it stays v2 until an
operator runs `upgrade`. Under v2 the new code behaves exactly as before: one
lease per cloud, whatever its component.

Under v3:

1. Leases are keyed `cloud/component`. Each lease keeps its own fence; `assert`,
   `release` and `recover` find a lease by its fence, so the CLI is unchanged.
2. The same component on one cloud is exclusive.
3. `control-plane` and `gateway` may hold leases at once only on a cloud in
   `CONCURRENT_CLOUDS`, which is `{"gcp"}`. On aws and azure every component
   stays exclusive: only gcp's gateway gates are attributable (below).
4. `public-surface` is exclusive with every other component on its cloud.
5. A `blocked` lease of any component stops every other admission on its
   cloud until it is recovered. A failed deploy left that cloud in an unknown
   state.
6. The two-cloud cap counts distinct clouds. A second component on a cloud
   that already holds a lease takes no new slot.
7. The holdback is a cloud with no lease of any component. It is still probed
   whole (`probe_cloud`: both services), and a second grant re-checks that it
   is unchanged.
8. FIFO: an earlier live waiter blocks a request when their components
   conflict on the same cloud (rules 2 to 4). Waiters for the other lane do not.
   Earlier waiters for other new clouds still take free cloud slots first.
9. `release` and `recover` probe only the releasing component
   (`probe_component`). This answers E5: a gateway regression can no longer
   hold a finished control-plane lease, nor the reverse. Under v2 they still
   probe the whole cloud.
10. Every grant increments `epochs[key]`. `lanes --cloud C` prints, for each
    component, whether it holds a lease, its state and its epoch. Equal epochs
    and no lease at two reads mean no lease of that component existed between
    them. This is what the gateway's gates read.
11. A check (`assert`) depends on its own lease and the holdback. Another
    component's lease coming or going does not invalidate it.
12. A v3 journal that no client could write (conflicting leases, three
    clouds, a repeated fence, a key that does not match its lease, bad
    epochs) is refused.

Unchanged: fencing by operation id, no reclaim by time, generation CAS writes,
blocked leases need proof of a stopped owner, the queue and its observation
margins.

## The gateway's gates (S2)

Gateway gates that traverse the control plane, all in quill-cloud-proxy:

- the per-instance gate before DNS (`verify-region-before-dns.sh`: inference,
  streaming, settled-evidence lookup);
- the PONG gate (`wait-region-synthetic-up.sh` and `synthetic_gate_status.py`);
- the one-shot status.json presence check in `roll-secondary-region.sh`;
- image discovery after promotion;
- the canary (`watchdog.py`). It reads only `tls_health` and
  `attestation_nonce`, which the enclave answers itself, but it reads them from
  status.json, which arrives through the control plane's ingest path.

Each gate runs unchanged inside `gate_hold`. After each attempt
`gate_hold.py decide` returns `pass`, `gateway:<why>` or
`inconclusive:<why>`:

- **Pass.** Only when the gate passed. One addition: a canary that passed while
  the control plane moved must also show a fresh `tls_health` and
  `attestation_nonce` row for the region from every monitor. Otherwise a
  stalled ingest path would show the canary only old rows.
- **The gateway's failure, at once, as before**, when either:
  - the failure cannot come from the control plane: a failed canary; a
    pre-DNS failure outside the request path (attestation, image, instance
    count, DNS); or the latest fresh enclave-only probe for the region is down;
  - or the control plane was provably still and healthy for the gate's whole
    window: no control-plane or public-surface lease at either end, equal lane
    epochs, status.json readable, no fresh control-plane probe
    (`gateway_authorize`, `gateway_settle`, `gateway_authorize_settle`,
    `control_plane_health`) down, and every monitor ingested a row.
- **Inconclusive otherwise.** The region stays as the gate left it: drained
  from canonical DNS, or with its new instances not yet published. Nothing is
  promoted, and the whole gate re-runs with a fresh window. Once the region
  has been held for `TR_CP_HOLD_MAX_SECONDS` (default 1800) in total, across
  all its gates, the gate fails closed with its own status, and the
  ordinary recovery runs.

`verify-region-before-dns.sh` exits 65 (`CP_DEPENDENT_STATUS`) for
request-path failures, so attestation failures stay immediate. If the
classifier itself cannot run, the gate's own verdict stands, as before.

Every gate stays exactly as strict for the component it measures. Under v2,
`lanes` shows only the gateway's own lease, so every verdict is the same as
today's.

## Interleavings and why each verdict stays attributable

| Control plane doing | Gateway doing | Allowed | Why the verdicts stay attributable |
|---|---|---|---|
| migrate, warm revisions at 0% | anything | yes | No traffic or signal change. The gateway's passes are unchanged. Its failures hold until the bound, because a control-plane lease is held. |
| canary or ramp in us-central1 | draining or rolling us-central1 | yes | The control-plane verdict reads only probes sent straight to the control plane (#2560); enclave-only probes are inconclusive there. The gateway's canary reads only enclave-answered probes, and must see fresh ones while the control plane moves. |
| canary or ramp | PONG gates, pre-DNS gate, discovery, any region | yes | A request-path failure while a control-plane lease is held, or was granted during the window, is inconclusive. The region stays drained and the gate re-runs; it passes only when the gate itself passes, or fails closed after the bound. |
| public surface change (standalone `public-surface` lease) | anything on gcp | no | Rule 4. |
| `verify-cloud-complete`, release | gateway rolling | yes | `verify_gcp_complete.sh` reads only analytics fields of status.json. Release probes only the control plane (rule 9). |
| failed, lease blocked | gateway rolling | the running gateway continues; no new gcp admission | Rule 5. The gateway's gates see a held (blocked) control-plane lane, so its request-path failures hold, then fail closed. |
| anything | DNS reconcile, drains | yes | Enclave DNS is gateway-only. The control plane never writes it, and its gates do not read the enclave's DNS or health. |
| any | release | yes | The gateway's release probes only `api.trustedrouter.com/health` (rule 9). |

**The public-surface step inside a control-plane run.** This differs from the
#2559 table. `public-surface-companion` runs under the run's
`gcp/control-plane` fence and so overlaps the gateway lane. What it changes is
the `trusted-router-public` Cloud Run service. `public_surface.sh` says it
changes no URL map, and the gateway's authorize and settle calls are routed
to `trusted-router-control-backend` and the us-central1 terminal backend, not
to that service. It does serve status.json, which the gateway's gates read.
That is covered because its whole window is inside a held control-plane lane:
an unreadable page or a failing gate is inconclusive, and a canary must see
fresh rows. URL-map and other public-surface work outside a control-plane
run takes the `public-surface` lease, which stays exclusive.

## Cut-over (E6)

Pinned v2 clients reject a v3 journal ("legacy or unknown deployment lock
exists") before any write. `tests/test_cloud_rollout_v3.py` runs that exact
pinned file (sha256 `0f7b668b...`) against a v3 journal and checks it refuses
every command.

They do not fail mid-rollout, because `upgrade` requires an empty journal. A
v2 client holds no lease and has no waiter at that moment, so the first thing
it can meet is a refused admission. It never meets a refused assert or release.

Order:

1. Merge this quill-router change. The journal stays v2 and behavior is
   unchanged. It deploys the control plane (`scripts/deploy/**`).
2. Merge the quill-cloud-proxy change. It re-pins the coordinator to the
   quill-router merge commit (same file digest) and adds `gate_hold`. It
   triggers a gateway rollout. Under v2 every gate verdict is unchanged.
3. Wait until every gateway run that can still start uses the new pin:
   `gh run list --workflow deploy-enclave-gcp.yml` shows no queued or
   in-progress run older than step 2's merge. Every quill-router run that can
   still start is past step 1. A train run takes the newest commit, so older
   queued runs are superseded.
4. Run `infra-apply` with operation `lanes` to check, then
   `upgrade-journal` with confirmation `APPLY`. It is refused unless the
   journal is empty; retry at a gap.
5. Watch the first overlapping runs: `lanes` and the `gate_hold` annotations.

Rollback: `downgrade-journal`, also refused unless the journal is empty. The
v3 code keeps working on a v2 journal, so the re-pin can stay. If an old pin
must come back, downgrade first.

A v2 client that starts after step 4 fails at admission, before it mutates
anything. It needs a run on a commit with the new pin.

## Not verified

- **How long `control_plane_core` stays down during a bad control-plane ramp.**
  The 30-minute bound assumes the control plane's own canary and drain watch
  (3 and 15 minutes) roll back a bad revision well inside it. It is a
  measured worst case for neither.
- **Starvation inside the monitor.** The monitor's ledger probes share a
  semaphore with the gateway-bound probes, and a pass ingests only when all
  its probes finish. A hanging gateway could in principle push a pass past
  the 300-second job timeout and silence the control-plane rows. Measured
  2026-11-08 to 11-13 over 7 gateway rollouts (387 executions per monitor):
  no failed execution, and the slowest was 255 s, against 300 s.
- **The gateway's own rollout cost while held.** A held region stays
  drained, at most 30 minutes longer per region. Step timeouts are raised
  by the same amount.
- **aws and azure gateways.** They are not concurrent (rule 3); their gates
  have the same coupling and are not yet attributable.
