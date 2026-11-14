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
7. The holdback is a cloud with no lease of any component, probed whole
   (`probe_cloud`: both services). While the previous holdback is still
   outside the rollout it stays the holdback, and every grant requires it
   healthy and unchanged, with no fallback to another cloud. A fallback
   would let a change outside any lease be laundered by the second grant
   on gcp (review of #2568). A new holdback is chosen only when the previous
   one is itself being leased. Before that lease is granted, the previous
   holdback's release evidence (`/trust/control-plane.json`) is read twice
   on its own, with no health checks, so a failing service cannot hide a
   change (`observed_releases`). The grant is refused when the evidence is
   readable and differs or is invalid. The cloud is leasable only when the
   evidence is unreadable. The same holds under v2.
   - **Readable and differs or is invalid (refused):** any 2xx answer that
     is not a document carrying the protected release. That is a different
     release; a release that is missing, null, not a string or not a
     lowercase SHA (`"local"` when `TR_RELEASE` is dropped, `"eu"` or
     `"azure"` from an old revision); a body that is not a JSON object; a
     body that does not parse; a body over 1 MiB. One such answer in either
     read refuses.
   - **Unreadable (leasable, to repair a dead cloud):** the cloud served no
     document of its own. That is a transport failure (DNS, connect, TLS,
     timeout) or any answer that is not a 2xx: an HTTP error status (every
     4xx and 5xx: a 404, a 401 or 403, a 429) or a 3xx, which the
     coordinator never follows (300 and 304 lease like a redirect). The body
     of such an answer is never read as evidence. A 429 leases with the
     `Retry-After` our application sends (plain decimal seconds), with any
     other value and with none.
     - `fetch` converts a `Retry-After` on a 429, 502, 503 or 504 to a
       number only when it is one to nine ASCII digits
       (`RETRY_AFTER_DIGITS`), and retries only when that number is at most
       5. On any other value `fetch` does not retry: it gives up at once and
       raises the HTTP error, so the read served no document, and a
       protected holdback answering that way is leasable, the same as a
       plain 503 or `Retry-After: 37` under this rule. That covers a date,
       a longer decimal (leading zeros count toward the nine), and
       characters `str.isdigit()` passes but `int()` does not (the bytes
       0xB2, 0xB3 and 0xB9, which decode to superscript digits).
     - Before the review of #2577 this was a known edge. A decimal over
       Python's 4300 digit conversion limit, or those superscript digits,
       reached `int()`, and its ValueError counted as an answer without a
       release, so the grant was refused. The length and ASCII checks run
       before `int()` now, and `test_fetch_converts_only_a_short_ascii_retry_after`
       and the 4301 digit and superscript cases of
       `test_default_coordinator_leases_a_holdback_that_served_no_document`
       pin both.
   - A refusal writes nothing. It is not a deadlock on one condition: every
     lease that relies on this cloud as its holdback can itself be released
     or recovered. The check runs only while such a lease exists. Once each
     one is released or recovered the journal holds no lease, the holdback
     is cleared, and the cloud can be leased directly. `release` and
     `recover` read only what their own lease changed (rule 9; the whole
     cloud under v2) and never the holdback, so a changed holdback does not
     hold them.
   - **When that condition fails: the frozen journal.** Say gcp is leased
     and aws, its holdback, changes outside any lease. A gcp rollout that
     is already past its last `assert` still releases normally. Otherwise
     its next `assert` reads aws and fails, the deploy records a failed
     release, and the gcp lease ends `blocked`. Which step records it
     depends on who took the lease: `deploy_mutex_finish` for a lease the
     deploy script took itself, and for a lease the workflow took in
     `admit-cloud`, the "Finalize cloud reservation" step of
     `.github/workflows/deploy-production.yml` (the gateway rollout has its
     own `finalize-cloud` job in quill-cloud-proxy). From then on nothing is
     admitted on any cloud: gcp because its lease is still held, aws by
     this rule, and azure because every grant needs the holdback, aws,
     healthy and unchanged. (Under v2, and for a conflicting component
     under v3, the gcp refusal reads "already reserved"; rule 5 is what
     refuses gcp's other lane under v3. The outcome is the same.) Nothing
     times this out: a blocked lease is never reclaimed by age. There are
     two exits.
     1. **Recover the blocked lease.** `infra-apply` with operation
        `recover` (confirmation `APPLY`) frees it once its owner run is
        proven stopped and gcp passes its own probe. It asks nothing of
        aws. The journal is then empty and the holdback is cleared, so aws
        can be leased to put a reviewed release back on it. That is
        possible, not enforced: the journal keeps no memory of the change.
        gcp and azure can be leased too, and a new grant on gcp takes aws,
        if it passes its probe, as its holdback at whatever release it now
        serves.
     2. **The holdback's evidence stops differing.** If aws serves the
        protected release again in both reads, aws can be leased again.
        So can azure, but only if aws also passes its whole probe (this
        rule: the holdback healthy and unchanged). With aws serving the
        protected release while its `/health` or its gateway is down, only
        aws is admitted. If aws serves no document, aws alone can be, to
        repair it. A lease on aws makes azure the holdback, so it needs
        azure to pass its whole probe. gcp stays blocked either way until
        it is recovered.
   - **When exit 1 is closed too.** `recover` is refused while the owner
     run cannot be proven stopped, and while gcp fails its own probe (the
     failed rollout left it unhealthy). What that probe reads follows
     rule 9: for a control-plane lease, a failing API service probe closes
     exit 1 only under v3, and a gcp gateway that is down only under v2. No
     gcp rollout is admitted to repair it, as above. With aws still differing,
     the coordinator then admits nothing until one of three things happens
     by hand, outside any lease: what refuses the recovery is put right and
     the gcp lease is recovered; the protected release is put back on aws;
     or aws stops serving a document at all, after which aws alone can be
     leased (exit 2). That is two clouds in an unknown state at once, which
     the coordinator refuses rather than resolves. Rule 5 already demands
     the manual gcp repair for any blocked lease on an unhealthy cloud.
     What this rule adds is that aws and azure stay closed meanwhile, for
     as long as aws differs.
   - A response that breaks off mid-transfer, or is not HTTP at all, raises
     out of the coordinator: the command fails and writes nothing.
8. FIFO: an earlier live waiter blocks a request when their components
   conflict on the same cloud (rules 2 to 4). Waiters for the other lane do not.
   Earlier waiters for other new clouds still take free cloud slots first.
9. `release` and `recover` probe only the releasing component
   (`probe_component`). This answers E5: a gateway regression can no longer
   hold a finished control-plane lease, nor the reverse. Under v2 they still
   probe the whole cloud.
   - **gateway:** `api*.trustedrouter.com/health`.
   - **control-plane:** the API service the deploy changed. On gcp,
     `/health` and `/trust/control-plane.json` are served by the public
     service, so the probe also requires the API service's own structured
     401 from `/v1/internal/synthetic/health`, which the URL map routes to
     `trusted-router-control-backend`. No token is sent; the coordinator
     carries no secrets. All three planes answer it identically (checked
     2026-11-13).
     - **Or, on aws and azure only, the application's own limiter's 429.**
       There the plane host is the observer app itself, with the source
       limiter on in `untrusted` client-ip mode (`azure_control_plane.sh`,
       `aws_eu_control_plane.sh`). The limiter (`_rate_limit_request` in
       `src/trusted_router/middleware.py`) runs before internal auth. With
       no credential the probe lands in namespace `internal_ip` under the
       constant `untrusted` subject: one bucket per replica or task, shared
       by every unauthenticated `/internal/` and `/v1/internal/` request,
       routes that do not exist included (240 a minute per replica on
       azure, 6000 a minute per task on aws). Once anyone spends it, the
       application answers 429 instead of the 401. Until the review of
       #2577 the probe retried that after 1 s and 2 s and refused, so
       `release` and `recover` of a control-plane lease left it held, and a
       held lease there also blocks that cloud's gateway. About 4 requests
       a second per azure replica was enough. That failed closed, so it was
       a liveness problem that outside traffic could cause, not a safety
       one.
     - On those two planes (`SHARED_LIMITER_PLANES`) the probe passes that
       429 at once, matched byte for byte (`LIMITER_429`): status 429 and
       exactly the body
       `{"error":{"code":429,"message":"Rate limit exceeded","type":"rate_limited","source":"router"}}`.
       The limiter also sends `Content-Type: application/json`,
       `Retry-After` (what is left of the 60 s window) and the
       `X-RateLimit-*` headers; the probe does not check them. Any other
       429 is retried and refused as before: a proxy's or a load balancer's,
       HTML, an empty body, or the limiter's object reformatted, wrapped,
       or with a field changed, added or missing. So is that body at any
       other status.
       `test_the_api_probe_passes_both_answers_the_application_itself_sends`
       takes the 401 and the 429 from the real application, on the observer
       surface aws and azure run and on the combined one, so the pinned
       bytes cannot drift from what the limiter sends.
     - **gcp keeps the refusal.** gcp's control backend runs with the
       limiter off (`TR_RATE_LIMIT_ENABLED=false` in
       `scripts/deploy/rollout.sh`), so it never sends those bytes. Every
       other app surface does once its own bucket is spent, and the public
       and control surfaces would otherwise answer this path 404. So on gcp
       the limiter's 429 can only come from a service that is not the API
       service, and it is retried and refused as before #2581. gcp has no
       shared bucket for outside traffic to spend at the API service, so
       it does not need the pass.
     - The trade-off, on aws and azure only: while the bucket is spent the
       probe shows that the application answers, not that it refuses an
       unauthenticated call. That refusal adds nothing there beyond the
       release evidence and `/health` checks, because the plane host is the
       same process. The probe is a liveness check before a lease is freed,
       not a security gate; the release's own tests cover internal auth.
     - The probe reads an error body only when it decides the answer: a
       429, or the last attempt. A retried 502, 503 or 504 is not read, so
       one whose body breaks off mid-transfer (`http.client.IncompleteRead`,
       which is not an `OSError`) is retried as before. A body that breaks
       off when it is read refuses instead of raising out of the command.
   - **public-surface:** `/health` and the release evidence.
10. Every grant increments `epochs[key]`. `lanes --cloud C` prints, for each
    component, whether it holds a lease, its state and its epoch, plus the
    journal's `journal_id`. Equal epochs, the same journal id and no lease at
    two reads mean no lease of that component existed between them. This is
    what the gateway's gates read.
    - Epochs never go back: `downgrade` keeps them as `carried_epochs`
      (v2 clients preserve fields they do not know), and `upgrade` restores
      them.
    - Every `upgrade` writes a fresh `journal_id`, so even a downgrade and
      upgrade between two reads cannot repeat a snapshot (ABA).
11. A check (`assert`) depends on its own lease and the holdback. Another
    component's lease coming or going does not invalidate it.
12. A v3 journal that no client could write is refused: conflicting leases,
    three clouds, a repeated fence, a key that does not match its lease, an
    epoch key that is not `cloud/component`, a held lease whose epoch is 0
    or missing, or a missing journal id.

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

- **Pass.** Only when the gate passed. One addition: the canary reads an
  unreachable status page as `unknown` and passes. So when the control plane
  moved, or its lanes were unreadable, a canary pass also needs the latest
  fresh `tls_health` and `attestation_nonce` row for the region from every
  monitor. Each must be up. A failing one makes it the gateway's failure; a
  missing or not-up one makes it inconclusive.
- **The gateway's failure, at once, as before**, when either:
  - the failure cannot come from the control plane: a failed canary; a
    pre-DNS failure outside the request path (attestation, image, instance
    count, DNS); or the latest fresh enclave-only probe for the region is down;
  - or the control plane was provably still and healthy for the gate's whole
    window: no control-plane or public-surface lease at either end, equal lane
    epochs and the same journal id, status.json readable, no fresh
    control-plane probe (`gateway_authorize`, `gateway_settle`,
    `gateway_authorize_settle`, `control_plane_health`) down, and every
    monitor ingested a row.
- **Inconclusive otherwise.** The region stays as the gate left it: drained
  from canonical DNS, or with its new instances not yet published. Nothing is
  promoted, and the whole gate re-runs with a fresh window.

Every limit is a hard deadline. An attempt in flight when one is reached is
killed, together with its process group, after `GATE_HOLD_KILL_GRACE` (30 s)
for its own cleanup. The gate then fails closed with its own status, or 124.
The limits are:

- **An attempt cap per gate** (`TR_CP_HOLD_ATTEMPT_SECONDS`): 12 minutes for
  pre-DNS and PONG on us-central1 (the old step limits), 25 for a
  secondary's PONG, 10 for the canary, and 6 for discovery.
- **A hold bound per gate** (`TR_CP_HOLD_MAX_SECONDS`, 20 minutes), counted
  from that gate's first inconclusive verdict. The clock is per gate, so a
  gate that passed leaves nothing behind.
- **A budget per job** (`TR_CP_HOLD_BUDGET_SECONDS`, 20 minutes) for every
  hold of every gate of every region, including status.json presence waits.
  It is re-read every second while a gate holds, so overlapping holds (a
  wave's two regions) stop when their combined spend reaches it. If its
  records cannot be written, the gate fails closed.
- Both are sized by GitHub's 360-minute job limit. A control-plane fault that
  outlasts them fails the gateway closed.

**Forward deadlines.** The wave and US West each keep the old 55-minute
forward deadline (3300 s). It grows only by the hold seconds that region
actually spent, read from the budget records. Steps outside a hold keep
exactly the time they always had. After the forward deadline, recovery keeps
45 minutes. A wave region still stops at once when a peer fails or its
deadline passes, even mid-attempt or mid-hold.

**Timeouts.** Every rollout step has an enforced limit. A gate step's limit
covers one capped attempt, the hold, and the bounded journal and status
reads: 40, 40, 38 and 32 minutes for pre-DNS, PONG, canary and discovery on
us-central1. The wave and US West steps are 120 minutes each: the forward
deadline, the budget it may grow by, and recovery. Summed from enforced
limits only, the worst case (US West failing after the wave) is 353.5
minutes, under the job's 355-minute limit and GitHub's 360. quill-cloud-proxy
`tools/test_gate_hold.py` recomputes it from the workflow.

`verify-region-before-dns.sh` exits 65 (`CP_DEPENDENT_STATUS`) for
request-path failures, so attestation failures stay immediate. Two cases are
never re-run, because the gate has DNS and certificate side effects there:

- a first deployment, which swaps the cold CNAME;
- any run that swapped one. Before the swap, verify writes the file named
  by `GATE_HOLD_NO_RERUN_MARK`, which `gate_hold` never re-runs. This is
  keyed on the DNS state verify detects, so it also covers a failed first
  deployment whose MIG already exists. Such a run also turns 65 into 1.

Verify traps TERM and INT, so a killed run (hold deadline, peer failure, step
timeout) still restores the cold CNAME.

**Recovery** still verifies the restored template with the plain gate. If
that exits 65, the region stays drained and its state is `recovery-failed`,
so the finalizer never re-adds it. The operator message says the request path
failed, which the control plane may cause, and that re-running recovery once
trustedrouter.com is healthy verifies and re-adds the region.

If the classifier itself cannot run, a failed gate keeps its own status, and a
canary pass fails closed.

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

## Known gap: Terraform changes the URL map without a lease

`infra-apply` with operation `terraform` applies `infra/` (including
`control_lb_url_map.tf`) with no lease of any component, as it does today
under v2. A URL-map change while a gateway gate runs could change the
authorize or settle route that gate's requests take, with no lane showing it.
This is unchanged by v3, and it predates it: v2 did not lease Terraform
either. Closing it means the Terraform job taking the `gcp/public-surface`
lease, which is exclusive with both lanes. That is a separate change to
`infra-apply.yml`.

## Not verified

- **How long `control_plane_core` stays down during a bad control-plane ramp.**
  The 20-minute bound per gate (20 per job) assumes the control plane's own canary and drain watch
  (3 and 15 minutes) roll back a bad revision well inside it. It is a
  measured worst case for neither.
- **Starvation inside the monitor.** The monitor's ledger probes share a
  semaphore with the gateway-bound probes, and a pass ingests only when all
  its probes finish. A hanging gateway could in principle push a pass past
  the 300-second job timeout and silence the control-plane rows. Measured
  2026-11-08 to 11-13 over 7 gateway rollouts (387 executions per monitor):
  no failed execution, and the slowest was 255 s, against 300 s.
- **The gateway's own rollout cost while held.** A held region stays drained:
  at most 20 minutes per gate, and 20 minutes in total across the job.
- **aws and azure gateways.** They are not concurrent (rule 3); their gates
  have the same coupling and are not yet attributable.
