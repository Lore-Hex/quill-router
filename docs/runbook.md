# TrustedRouter Operator Runbook

Keyed by symptom → action. When a customer reports something or a synthetic
monitor pages, find the matching section and follow the steps. Every entry
came from a real incident; the linked commits are the receipts.

Index:
- [Router-core four-nines page fires](#router-core-page)
- [Drain or disable one gateway region](#region-drain)
- [Spanner or ClickHouse is degraded](#storage-degraded)
- [Provider returns 502 "provider error" via the gateway](#provider-502)
- [Provider returns sustained 429 "rate limit exceeded"](#provider-429)
- [Provider returns 401 "Invalid API key" via the gateway](#provider-401)
- [Smoke test for a provider returns "gateway authorization failed" 400](#gateway-auth-400)
- [GCP enclave deploy keeps auto-rolling back europe-west4](#eu-rollback)
- [GCP enclave deploy fails with "unrecognized arguments: --min-ready"](#min-ready)
- [Hourly price bot commits but TR catalog stays stale](#bot-doesnt-deploy)
- [Production deployment mutex](#deployment-mutex)
- [Canary-cloud bake ladder](#cloud-bake-ladder)
- [Status page shows a region "down" but the region is actually healthy](#stale-status)
- [`refresh.py` reports "too_many_failures" locally](#local-refresh-fails)
- [A provider serves a model but TR's `/v1/models` doesn't list it](#missing-model)
- [Adding a brand-new provider to TR](#new-provider)
- [Adding a model to an existing provider](#new-model)
- [Rotating a provider API key](#rotate-key)
- [Spinning up Phala / RedPill again after a key issue](#phala-revive)
- [Settle outbox: flip, verify, monitor, roll back](#settle-outbox)
- [Credit ledger operations (single typed book)](#credit-ledger)
- [Sentry "Aborted ... deadlock/wounded" burst on gateway authorize](#authorize-deadlock-burst)
- [One workspace 503s "Workspace billing is paused" (interrupted reshard)](#reshard-interrupted)
- [DNS-vendor-split symptoms (Cloudflare vs Cloud DNS)](#dns-vendor-split)
- [Adding a cloud (and when it is allowed to be called done)](#adding-a-cloud)

---

## <a id="router-core-page"></a>Router-core four-nines page fires

Scope first: router-core means attested TLS reachability, API key validation,
gateway authorization, route-candidate fallback, and durable settle/refund. It
does not include marketing pages, dashboard UX, docs, trust page, or a single
upstream provider outage when fallback remains available.

Immediate triage:
1. Open `https://status.trustedrouter.com/status.json` and inspect
   `data.slo_classes.router_core`. Do not use `overall_status` from an old
   cached page if the JSON is fresher.
2. Identify whether the bad class is `tls_health`, `attestation_nonce`,
   `gateway_authorize_settle`, or `provider_fallback`.
3. Smoke the regional host directly:
   ```bash
   TR_SMOKE_BASE_URL=https://api-<region>.quillrouter.com/v1 \
     uv run python scripts/smoke_e2e.py
   ```
4. If only one region fails, drain it and let SDK/global failover carry
   traffic. If every region fails, treat it as a global prompt-path incident.
5. Never route prompt traffic to a non-attested fallback. A hard 503 is better
   than silently dropping the trust guarantee.

Paging thresholds:
- 5m or 1h router-core burn rate >= 14.4x: page immediately.
- 6h burn rate >= 6x: page during waking hours unless customer impact is
  visible.
- 24h burn rate >= 3x: create an incident review item.

## <a id="region-drain"></a>Drain or disable one gateway region

Use this when a region-specific enclave deploy, regional provider key, or local
network path is failing while at least two other attested regions are healthy.

1. Confirm the region is failing with direct regional smoke.
2. Remove or downweight the region in Cloudflare DNS-only load balancing. Do
   not enable orange-cloud proxying for the prompt path.
3. Keep the regional hostname published for debugging, but stop sending
   convenience/global traffic to it.
4. Verify SDK failover by forcing a request to fail against the bad region and
   observing retry to a healthy region.
5. Roll back or redeploy the bad regional revision only after the other regions
   are stable.

Provider emergency disable:
1. Disable the provider route in the catalog or provider capability config.
2. Confirm `trustedrouter/auto`, `trustedrouter/cheap`, and
   `trustedrouter/monitor` still have at least three independent candidates if
   they are advertised as high availability.
3. Watch the affected provider row on `/status` and `/leaderboard`, not
   `router_core`, for the remaining provider impact.

## <a id="storage-degraded"></a>Spanner or ClickHouse is degraded

Spanner remains the source of truth for billing and settlement. Content-free
activity and status analytics are delivered from a durable Spanner outbox to
replicated ClickHouse. Bigtable is only a temporary migration mirror and is not
part of the `spanner-clickhouse` runtime.

Spanner degraded:
1. Check whether regional quota leases can continue authorizing bounded spend.
2. If leases cannot be refreshed and holds cannot be made safely, fail closed
   for prepaid requests rather than granting unlimited credit.
3. BYOK requests may continue only if they do not require prepaid credit holds
   and key-limit enforcement is still local/leased.
4. After recovery, reconcile reservations and stuck authorizations.

ClickHouse degraded:
1. Keep inference and Spanner settlement alive. Never make ClickHouse part of
   the synchronous prompt or billing path.
2. Check `tr_operational_analytics_outbox` and `tr_analytics_outbox` oldest-row
   lag. The drainer must catch up before either queue's retention window. The
   first of those is published without credentials as `analytics.drain_lag_seconds`
   in every cloud's `/status.json`; `PYTHONPATH=src python3 -m
   clickhouse.check_fleet_analytics_freshness` reads it for the whole fleet.
   A `not_configured` reason means that deployment has no outbox wired at all,
   which is a different problem from a stopped drain.
3. Check all three ClickHouse replicas, disk capacity, and Keeper delay.
4. Start `tr-clickhouse-operational-ingest.service`, then run
   `clickhouse.verify_spanner_delivery` and confirm no missing or mismatched
   generation rows.
5. Verify `/activity`, `/status`, `/leaderboard`, and provider rollups recover.

---

## <a id="provider-502"></a>Provider returns 502 "provider error" via the gateway

`{"error":{"message":"provider error","status":502}}` from `api.quillrouter.com`.

**First check**: the enclave logs surface the real upstream error. From a
machine with `gcloud` auth:

```bash
gcloud --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  logging read \
  'resource.type="gce_instance" AND logName=~"confidential-space" AND jsonPayload.MESSAGE=~"chat_collect_failed"' \
  --limit=10 --project=quill-cloud-proxy --freshness=5m \
  --format='value(timestamp,jsonPayload.MESSAGE)'
```

Common upstream patterns and their fix sections:
- `http 401: ...Invalid API key` → [Provider 401](#provider-401)
- `http 429: ...Rate limit exceeded` → [Provider 429](#provider-429)
- `http 400: failed to find the model: <bare>` → enclave is stripping the
  author prefix; the provider expects a different native id. See
  `enclave-go/internal/llm/byok.go::directModelID` + the per-provider map
  (`parasailModelMap`, `gmiModelMap`, etc.). Pattern shipped in
  `f8823e8` (gemma-4) and `9471ab5` (comprehensive audit).
- `http 404: <provider's "model not found" JSON>` → same as above.

If you see a 200 outcome interleaved with the 502s in the logs, it's a
provider capacity issue (transient 429s, retry tail). Don't change code;
monitor.

---

## <a id="provider-429"></a>Provider returns sustained 429 "rate limit exceeded"

Upstream capacity issue, not a TR bug. Pattern observed for:
- Parasail's gemma-4-31b-it (2026-05-11 onwards)
- Phala's deepseek-v3.2 (intermittent 2026-05-13)

Options in order of preference:
1. Wait — most are minute-scale capacity blips.
2. If sustained > 1 hour, email the provider (the contacts in
   `scripts/deploy/secrets.sh` comments are stale; check Slack/email).
3. If the model has another provider available, TR's auto-router will
   pick a healthy alternative. Customers pinning `provider.only=[X]`
   will see the 429 surface — that's by design.

Do NOT add retry-on-429 to the enclave. Upstream 429s mean "back off";
we honor them.

---

## <a id="provider-401"></a>Provider returns 401 "Invalid API key" via the gateway

The enclave fetched a key from Secret Manager at boot and that key is
rejected by the upstream.

Steps:
1. Confirm the secret name in `tools/deploy-gcp-mig.sh` (search for
   `QUILL_<PROVIDER>_SECRET`).
2. Pull the live value and try a direct curl:
   ```bash
   KEY=$(gcloud --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
     secrets versions access latest \
     --secret=trustedrouter-<provider>-api-key \
     --project=quill-cloud-proxy)
   curl -sS https://<provider-host>/v1/chat/completions \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"model":"<known-good>","messages":[{"role":"user","content":"hi"}],"max_tokens":4}'
   unset KEY
   ```
3. If the curl also 401s, the key in Secret Manager is wrong:
   - Get a fresh key from the provider's dashboard.
   - Add it to `~/.quill_cloud_keys.private` under the appropriate var name.
   - Run `bash scripts/deploy/secrets.sh` to push to GCP Secret Manager.
   - Redeploy the enclave (next bot run or manual workflow dispatch).
4. If the curl 200s but the gateway 401s, the enclave is using an OLDER
   value (it caches at boot). The next enclave deploy picks up the
   rotated key. Manually trigger one if urgent.

If the 401 surfaces with a *correct-looking* key, consider whether the
provider has tier-scoped keys. Phala did this on 2026-05-13: their
catalog-read tier works for `/v1/models` but chat-completions needs a
separate confidential-AI key from `cloud.phala.com`. See
[Phala revive](#phala-revive).

---

## <a id="gateway-auth-400"></a>Smoke test for a provider returns "gateway authorization failed" 400

`{"error":{"message":"gateway authorization failed","status":400}}` from
`api.quillrouter.com`. The TR catalog has NO endpoint for
`<model>@<provider>/prepaid` — the route doesn't exist before the request
even reaches the enclave.

Root cause: `scripts/pricing/providers/<provider>.py` didn't price the
model, so `scripts/pricing/refresh.py` dropped its endpoint from the
snapshot. Pattern observed 2026-05-13 for Phala with only 3 endpoints
priced out of ~20 the provider actually serves.

Fix:
1. Confirm the provider's `/v1/models` lists the model and what its
   native id is.
2. Add the native id to `_NATIVE_TO_OR_ID` in
   `scripts/pricing/providers/<provider>.py`.
3. Ensure the upstream returns pricing in the `/v1/models` response (most
   do; if not, the scraper needs a static `_RATES_USD_PER_M` like
   `parasail.py`).
4. Push the scraper change. The hourly bot picks up the new model on
   its next run; `deploy.yml` auto-rolls within ~20 min thanks to the
   workflow_dispatch fan-out (`65ceb7c`).
5. Verify via gateway smoke after deploy.

Also confirm the enclave's `<provider>ModelMap` in
`enclave-go/internal/llm/byok.go` has the right OR-canonical →
provider-native mapping. Without it, the strip-author fallback sends
the wrong id and the upstream 4xx's.

---

## <a id="eu-rollback"></a>GCP enclave deploy keeps auto-rolling back europe-west4

Should not happen after `7b735e8` (2026-05-12). If it does:

1. Check whether the deploy used the old workflow (cross-region final
   watchdog) or the new one (per-region post-stable canary). Inspect
   `gh run view <run_id> --log | grep -E "watchdog|wait-until --stable"`.
2. The new pattern is: roll → `wait-until --stable` → 3-min canary →
   per-region rollback if canary fails. If the workflow is missing
   the `wait-until` step, someone reverted `7b735e8`.

Historical context: the root cause was timing — the cross-region final
watchdog overlapped eu's drain phase by construction because us deploys
first. Per-region post-stable canary eliminates the overlap.

---

## <a id="min-ready"></a>GCP enclave deploy fails with "unrecognized arguments: --min-ready"

Means someone re-added `--min-ready=120s` to a `rolling-action replace`
call. That flag is INVALID on the `replace` subcommand (only valid on
`start-update`). Revert per `2071a92`.

The TLS-readiness gap that `--min-ready` was trying to close is already
absorbed by the `wait-until --stable` step in the workflow.

---

## <a id="bot-doesnt-deploy"></a>Hourly price bot commits but TR catalog stays stale

Should not happen after `65ceb7c` (2026-05-13). The bot now explicitly
`gh workflow run deploy.yml` after pushing a snapshot diff.

If you see a bot commit on `main` but no corresponding `deploy.yml` run:
1. Check the bot run's last step "Commit and push if changed" for the
   `dispatched deploy.yml for new snapshot` log line.
2. If missing, the workflow likely lost its `actions: write` permission
   or the `gh` invocation failed silently. Inspect the workflow's
   `permissions:` block.

Fallback: manually dispatch with `gh workflow run deploy.yml -R Lore-Hex/quill-router`.

The reason for the workaround: GHA's loop-prevention says commits
pushed by `GITHUB_TOKEN` don't trigger `push:` events. `workflow_dispatch`
events from `GITHUB_TOKEN` DO fire workflows — that's the exception
we exploit.

---

## <a id="deployment-mutex"></a>Production deployment mutex

The GCP control-plane deploy workflow, direct `rollout.sh` runs, manual
`staged_traffic.sh` traffic shifts, and the AWS and Azure control-plane scripts
share one generation-fenced lock object in GCS. This is a fleet-wide,
one-cloud-at-a-time guard: a GCP, AWS, or Azure control-plane rollout blocks the
other two until it releases the object. The `cloud` field is holder metadata,
not a separate lock namespace; creating one object per cloud would permit the
overlap this guard exists to prevent.

Manual AWS and Azure operators must have an authenticated `gcloud` CLI with
object access to `tr-deploy-mutex-quill-cloud-proxy` in addition to their
cloud's own CLI credentials. `TR_DEPLOY_MUTEX_CLOUD` accepts `gcp`, `aws`, or
`azure` and defaults to `gcp`; the AWS and Azure control-plane scripts set it
themselves. Inspect the current metadata-only holder record, including its
cloud, without changing it:

```bash
bash scripts/deploy/deploy_mutex.sh status
```

Break glass only after running `status`, checking that the recorded owner is no
longer active, and confirming that no GitHub Actions or manual production deploy
is still running. Use the generation printed by `status`; the precondition keeps
this command from deleting a replacement lock acquired after the inspection:

```bash
gcloud storage rm \
  gs://tr-deploy-mutex-quill-cloud-proxy/locks/trusted-router-production.json \
  --if-generation-match=GENERATION
```

Normal recovery does not require manual removal: locks expire after 90 minutes,
and the next acquirer can take over an expired generation safely.

In deploy workflow status, **deployed** now means the primary is live: all four
regional revisions are warm, and `us-central1` has completed its 10/50/100 ramp
and unchanged three-minute canary (normally about nine minutes). The
`rollout-secondaries` follow-on job imports the same generation fence, keeps the
same lock held while the three secondary ramps and regional-quota reconciler
converge, and releases it only after that work finishes or fails. Thus another
cloud's rollout cannot interleave in the middle of GCP convergence.
`verify-cloud-complete` still gates full-cloud convergence after the follow-on;
the public-surface companion remains outside the mutex and starts only after
the locked convergence job finishes.

---

## <a id="cloud-bake-ladder"></a>Canary-cloud bake ladder

The fleet is intentionally allowed to run different commits for days. Any one
cloud may receive a fresh commit quickly for production testing, but fresh code
must never be serving everywhere at once. When one cloud receives fresh code,
at least one *other* cloud must keep serving a commit that is 24 hours old or
older. That trailing cloud is the lifeboat. Promoting the candidate to a second
cloud is allowed only after the candidate has been present in first-parent
`origin/main` history for at least 24 hours and its code line has already
carried production traffic on one known cloud. Commit age is printed for
context but is not authority because an operator can backdate it.

`scripts/deploy/cloud_bake_gate.sh` enforces this rule after the fleet mutex is
acquired and before AWS or Azure makes its first mutation. The default bake is
24 hours; `TR_CLOUD_BAKE_HOURS` accepts an integer from 1 through 720. Both
modes also require `https://trustedrouter.com/status.json` to report
`overall_status=up`.

To test fresh code on Azure now, use canary mode:

```bash
TR_CLOUD_DEPLOY_MODE=canary \
  bash scripts/deploy/azure_control_plane.sh
```

Canary mode has no candidate-age requirement. It prints every cloud's serving
commit and age before choosing a lifeboat. A typical table looks like:

```text
cloud  serving_sha  age_hours  classification
gcp    9ac7812      1h         fresh-exempt (gcp auto-deploys; ineligible as lifeboat)
azure  7b42d10      50h        baked-target
aws    6e211aa      96h        LIFEBOAT
```

The target cloud is never allowed to count as its own lifeboat. GCP cannot be a
lifeboat either: every merge auto-deploys there without this fleet gate, so a
routine merge could revoke that safety copy minutes later. The lifeboat must be
a gated cloud, AWS or Azure. An `UNKNOWN` row means the cloud CLI failed or its
short SHA could not be resolved after `git fetch --quiet origin main`; unknown
clouds cannot be lifeboats.

After the candidate has baked, promote it to another cloud with the default
mode (no mode variable is needed):

```bash
bash scripts/deploy/aws_eu_control_plane.sh
```

Promote mode requires the checkout's `HEAD` to be an ancestor of the newest
first-parent `origin/main` commit old enough to satisfy the bake threshold, and
also an ancestor of at least one known currently serving cloud commit. The
first check proves when the candidate was merged; the second proves its code
line actually carried traffic and has not been reverted. Lineage containment
is deliberately the traffic definition: a commit included in a deployed batch
counts as baked with that batch even if that individual commit never served
alone.

Break glass only with a non-empty incident reason. The gate still performs and
prints every real check before it proceeds, so the reason and the unsafe state
remain visible in the operator log:

```bash
TR_CLOUD_BAKE_OVERRIDE="INC-742: restore Azure after regional outage" \
  bash scripts/deploy/azure_control_plane.sh
```

An empty `TR_CLOUD_BAKE_OVERRIDE` does nothing. GCP does not call this gate:
each merge auto-deploys through GCP's staged regional rollout, so GCP is usually
the freshest cloud and is never eligible as the canary lifeboat. AWS or Azure
must be left trailing as the gated lifeboat, and operators run this gate when
moving that commit onto another cloud.

---

## <a id="stale-status"></a>Status page shows a region "down" but the region is actually healthy

`https://trustedrouter.com/status.json` shows `effective_status: down`
for a region but smoke tests against `api-<region>.quillrouter.com`
succeed.

Most likely a synthetic monitor problem, not a TR problem.

1. Check `scripts/deploy/synthetic.sh` deployed a Cloud Run Job per
   `TR_REGIONS`. The synthetic monitor for each region runs on Cloud
   Scheduler (cron `* * * * *`).
2. Look at the monitor's logs:
   ```bash
   gcloud run jobs executions list \
     --job=trusted-router-synthetic-<region> \
     --region=<region> --project=quill-cloud-proxy --limit=3
   ```
3. If executions are failing, the monitor's API key (`TR_SYNTHETIC_MONITOR_API_KEY`)
   may have rotated. Check the env var on the job and the secret in
   Secret Manager.

Per-region probe spec: `attestation_nonce`, `openai_sdk_pong`,
`tls_health`, plus `responses_pong` from the primary region. Source:
`src/trusted_router/synthetic/probes.py`.

---

## <a id="local-refresh-fails"></a>`refresh.py` reports "too_many_failures" locally

`pricing.refresh.too_many_failures count=N limit=2 failures=[(provider, "401 Unauthorized")...]`

Means the local shell didn't export the provider API key envs that
the scrapers need. The CI bot pulls them from Secret Manager; locally
you need to export them from `~/.quill_cloud_keys.private` first:

```bash
set -a
source <(grep -E '^(TOGETHER|PARASAIL|LIGHTNING|GMI|DEEPINFRA|PHALA_CONFIDENTIAL)_API_KEY=' ~/.quill_cloud_keys.private)
set +a
cd /Users/jperla/claude/quill-router
uv run python -m scripts.pricing.refresh
```

Don't commit refresh.py output from a local run — the bot does it
hourly with the full key set.

---

## <a id="missing-model"></a>A provider serves a model but TR's `/v1/models` doesn't list it

The OR snapshot doesn't have an endpoint for `<model>@<provider>`. Two
possible causes:

1. **OR's `/endpoints` feed doesn't list the provider for that model.**
   Many newer providers (Parasail, Lightning, GMI, DeepInfra) aren't
   always in OR's endpoint listings. Fix: ensure the provider is in
   `scripts/ingest_openrouter_catalog.py::PROVIDER_NAME_TO_SLUG` AND
   `scripts/pricing/providers/<provider>.py` lists the OR-canonical
   id in `_NATIVE_TO_OR_ID` with a rate in `_RATES_USD_PER_M`. The
   scraper's synthetic endpoint creation in `refresh.py::_merge_snapshot`
   fills the gap.

2. **The OR snapshot is stale.** Re-run ingest:
   ```bash
   uv run python scripts/ingest_openrouter_catalog.py
   ```
   Then push — the hourly bot will overlay scraper prices on top.

---

## <a id="new-provider"></a>Adding a brand-new provider to TR

Worked example: 2026-05-11 added Parasail, Lightning AI, GMI Cloud,
DeepInfra in one batch (`f8823e8` chain).

Touchpoints, in order:
1. `src/trusted_router/catalog.py`:
   - `PROVIDERS` dict — add a `Provider(slug=..., name=..., supports_prepaid=True)`
   - `GATEWAY_PREPAID_PROVIDER_SLUGS` — add the slug
2. `scripts/pricing/providers/<provider>.py` — new scraper (template:
   copy `gmi.py` for API-direct, `parasail.py` for operator-pasted rates).
3. `scripts/ingest_openrouter_catalog.py::PROVIDER_NAME_TO_SLUG` — add
   the OR-side provider name → slug mapping.
4. `scripts/deploy/secrets.sh` — add `ensure_secret_from_env_file` for
   the new API key.
5. `.github/workflows/refresh-prices.yml` — add the new
   `<PROVIDER>_API_KEY` to the per-secret pull loop.
6. `enclave-go/internal/llm/byok.go`:
   - `directBaseURL(provider)` case for the upstream host
   - `providerNativeModelMaps` registration if native ids ≠ OR canonical
   - new `<provider>ModelMap` if needed
   - `byok_test.go` — add at least one `TestPerProviderNativeMaps` case
7. `enclave-go/internal/llm/multi.go` — wire the new client + struct field.
8. `enclave-go/internal/types/types.go` — add the `<Provider>APIKey string` field.
9. `enclave-go/internal/bootstrap/bootstrap_gcp.go` — fetch the new secret.
10. `tools/deploy-gcp-mig.sh` — `QUILL_<PROVIDER>_SECRET` default + tee-env entry.
11. Add the key to `~/.quill_cloud_keys.private`, then run `scripts/deploy/secrets.sh`.

Then commit, deploy. After the deploy, smoke a known-good model to
verify routing.

---

## <a id="new-model"></a>Adding a model to an existing provider

Pure scraper edit:
1. Add `<native_id>: <or_canonical>` to `_NATIVE_TO_OR_ID` in
   `scripts/pricing/providers/<provider>.py`.
2. If the provider's `/v1/models` doesn't include pricing, also add to
   `_RATES_USD_PER_M`.
3. If the provider's native id differs in shape from OR canonical
   (case, slug rewrite, etc.), add the inverse to
   `enclave-go/internal/llm/byok.go::<provider>ModelMap`.
4. Push. Bot picks it up next hour; auto-deploy rolls.

---

## <a id="rotate-key"></a>Rotating a provider API key

1. Update the value in `~/.quill_cloud_keys.private` (or wherever you keep
   the canonical local copy).
2. `bash scripts/deploy/secrets.sh` — pushes to GCP Secret Manager.
3. Redeploy the GCP enclave. Secret Manager values are read at boot.

For OAuth/Stripe/non-LLM secrets, only step 1+2 needed; the Cloud Run
service re-reads on next deploy.

---

## <a id="phala-revive"></a>Spinning up Phala / RedPill again after a key issue

Phala has TWO key tiers behind the same `api.redpill.ai` host:
- **Upstream pass-through tier**: model ids like `openai/gpt-5.5`,
  `anthropic/claude-haiku-4.5`. Needs a "redpill" key — TR doesn't have
  one.
- **GPU-TEE-attested confidential AI tier**: model ids like
  `phala/gpt-oss-120b`, `phala/deepseek-v3.2`. Needs a confidential
  key from `cloud.phala.com` dashboard.

TR uses tier 2. The key lives in:
- `~/.quill_cloud_keys.private` as `PHALA_CONFIDENTIAL_API_KEY`
- GCP Secret Manager as `trustedrouter-phala-confidential-api-key`

If Phala 401s after a re-enable:
1. Run a direct probe with the keyfile value against `api.redpill.ai/v1/chat/completions`
   with a `phala/<model>` id. If 200, secret is fine; rebuild enclave.
2. If 401, get a fresh confidential-tier key from `cloud.phala.com` and
   follow [Rotate a key](#rotate-key).
3. Email Yan @ Phala (`leechael@phala.network`) if Phala-side has issues.

Confidential AI docs:
https://docs.phala.com/phala-cloud/confidential-ai/confidential-model/confidential-ai-api

---

## <a id="settle-outbox"></a>Settle outbox: flip, verify, monitor, roll back

Durably recover completed charges whose settle intent was recorded but whose
inline settle result was lost. See `docs/design/durable-settle-outbox.md`.
The correctness spine is the reaper guard; drain cadence affects latency only.

The flip is config-as-code. Add `TR_SETTLE_OUTBOX_ENABLED=true` to the
`ENV_VARS` array in `scripts/deploy/rollout.sh`, then merge to `main`.

That merge is the production flip:
1. CI gates the change.
2. `rollout.sh` creates Cloud Run revisions with `--no-traffic`.
3. `staged_traffic.sh` ramps traffic by named revision.
4. Watchdog canaries auto-roll traffic back on failure.
5. Cold regions keep their previous revision on a normal merge. After the
   hot-region rollout completes, run the deploy workflow via
   `workflow_dispatch` with `deploy_cold_regions=true` to bring them to the
   same revision: `gh workflow run deploy.yml -f deploy_cold_regions=true`.
   The interim mixed state is safe: a flag-off region simply keeps the old
   byte-identical settle path, its charges just aren't outbox-protected yet.

**WARNING**: never flip this with a bare
`gcloud run services update --update-env-vars`. Cloud Run traffic is pinned to
named revisions here; template-only env changes can serve ZERO requests. This
was learned on 2026-07-04. Always verify the env on the SERVING revision:

```bash
gcloud run services describe trusted-router --region=us-central1 \
  --project=quill-cloud-proxy --format="value(spec.traffic)"
gcloud run revisions describe <pinned-revision> --region=us-central1 \
  --project=quill-cloud-proxy --format="value(spec.containers[0].env)" \
  | tr ';' '\n' | grep OUTBOX
```

After the deploy workflow completes, verify rows flow and complete inline:

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT status, intent_kind, COUNT(*) n FROM tr_settle_outbox GROUP BY 1,2"
```

Expect `done` to grow with settle traffic. Expect `pending` near zero at steady
state; pending rows are in-flight or crash-orphaned and freeze their holds by
design. Replayed settles (`already_settled`) never enqueue, so an empty table
under replay-only traffic is normal.

Verify there are no alert lines — in AXIOM, not Cloud Logging (app logs at
or above `TR_AXIOM_LOG_LEVEL` ship to Axiom only; see Monitoring signals
below): search the `trusted-router-logs` dataset for `"ALERT settle outbox"`.
Equivalent state-based check that needs no log access at all — dead rows are
the alert-worthy terminal state:

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT COUNT(*) FROM tr_settle_outbox WHERE status='dead'"
```

Spot-check settle latency is unchanged in `httpRequest.latency` for
`/internal/gateway/settle`.

Do not locate a canary authorization by scanning `tr_gateway_authorization.payload`.
The payload JSON is intentionally not indexed; a production scan can consume millions
of row reads and compete with customer billing traffic. Use the bounded verifier, which
resolves the existing idempotency index and then uses primary-key reads:

```bash
uv run python scripts/verify_gateway_authorization.py \
  --workspace-id <workspace-id> \
  --key-hash <key-hash> \
  --idempotency-key <canary-idempotency-key>
```

When the authorization ID is already available, prefer
`--authorization-id <gwa-id>`; that path is primary-key-only. The verifier emits only
billing and routing metadata. It never emits prompt or response content.

Resume the drain after the flip. The job already exists and is paused:

```bash
gcloud scheduler jobs resume trusted-router-settle-outbox-drain \
  --location=us-central1 --project=quill-cloud-proxy
```

Every 5 min it POSTs
`/v1/internal/gateway/settle-outbox/drain?limit=100` with the internal-token
header and returns `{claimed, outcomes, recovered_micro, purged, reaped}`. It also
purges `done` rows older than 30 days; it never purges `pending`, `dead`, or
`release_approved`. The drain also reclaims expired abandoned reservation holds
(limit 200/tick); frozen `pending`/`dead`-guarded holds are never reaped.

Outcome cheat-sheet:

| Outcome | Action |
| --- | --- |
| `settled_now` | Recovered charge; info log only. |
| `already_settled_with_charge` | Benign done; review low-priority flags from log warnings. |
| `already_settled_legacy` | Benign done; review low-priority flags from log warnings. |
| `already_released_free` on a settle row | DEAD plus `ALERT settle outbox lost charge`; invariant violation. Investigate. A human may set `status='release_approved'` to let the reaper free the hold only after confirming the charge is genuinely unrecoverable. |
| `reservation_missing` | Dead plus alert; investigate missing reservation state. |
| `invalid_row` | Dead; no page. |
| `park_typed_unavailable` | Typed-store outage; retries without burning attempts. |
| `resolved_zero_cost_elsewhere` | Benign $0 race (reaper free-release or a refund won); done, no page. The activity index is attempted first whenever the row carries a generation, so this outcome means the index SUCCEEDED; if it fails the row stays `activity_pending` and keeps its payload. |
| `activity_pending` | Rolling-legacy only: charge committed but its historical Bigtable activity-index write failed. New typed rows atomically enqueue ClickHouse delivery and cannot enter this state because of a mirror failure. |

`activity_pending` is the one outcome where the terminal transition is NOT a
money problem. The charge already committed in Spanner (the billing source of
truth) *before* the index attempt. It is reached both from a fresh `SETTLED`
finalize and from the `ALREADY_SETTLED` replay branches, so seeing it on a
replay is normal. The customer is billed correctly; only the per-request
Bigtable activity row is missing, so the request may be absent from their
activity view.

This section is retained only to drain rows created by revisions predating the
Spanner operational outbox. Once the retirement gate has confirmed there are no
such rows, a Bigtable outage cannot create new `activity_pending` work.

Two things about this outcome are easy to get wrong:

**The window measures continuous unrepaired-activity time, not row age.** It
starts at the first `ACTIVITY_PENDING` observation and is carried forward in the
park note (`last_error` = `bigtable activity index pending since=<ts>`). A row
that sat behind a typed-store outage for a day and then fails its Bigtable write
once has a *fresh* window — the clock is about the activity failure, not the
row. A `park_typed_unavailable` **preserves** an existing stamp rather than
clobbering it, so typed-outage time counts toward the window and the six hours
is a genuine bound; without that, alternating activity and typed failures would
reset it forever, since `park()` never burns attempts. A generic apply error
does rewrite `last_error` and restart the window, but that path burns an
attempt, so `max_attempts=8` bounds it. Read an expired row precisely: activity
stayed unrepaired for six hours and the most recent index attempt failed. It
does NOT prove Bigtable was failing throughout — typed-outage time ages the
stamp too, so some of that window may be time Bigtable was never attempted.
Check Bigtable health directly rather than inferring it from the alert.

**The row goes `dead`, not `done` — and that is the point.** `mark(done=True)`
NULLs `settle_body`, and for a gateway/typed request that payload is normally
the only repair evidence there is: typed finalize skips the generic
`generation` / `generation_by_workspace` entity writes (the legacy
request-record compatibility fallback still writes them, so a rolling-legacy
workspace may have them), and
`POST /internal/reconcile/generation-activity` repairs by scanning
`generation_by_workspace`. **That endpoint therefore repairs nothing for these
rows** — it is for legacy `add()` callers. Do not reach for it here. `dead`
preserves `settle_body`, stops the drain re-doing the full apply every 60s, and
puts the row in the `status='dead'` queue that is already monitored above.
Retention stays pinned (`terminal_at` NULL) on the reservation and gateway
authorization, which is correct: those records are the evidence a human needs.
Pinning is now bounded by operator response instead of unbounded.

Repairing `ALERT settle outbox activity repair expired` (the alert carries
`authorization_id`, `generation_id`, `request_id`, `reservation_id`):

1. Fix the underlying Bigtable problem first — check the
   `bigtable.activity_index_write_failed` logs for this `generation_id` and the
   [Spanner or Bigtable is degraded](#storage-degraded) section. Re-driving the
   row before Bigtable is healthy just fails again.
2. Re-arm the row. `settle_body` is intact on a `dead` row, so the drain can
   simply retry it — `due()` selects `status='pending' AND next_attempt_at <= now`:

   ```bash
   gcloud spanner databases execute-sql trusted-router \
     --instance=trusted-router-nam6 --project=quill-cloud-proxy \
     --sql="UPDATE tr_settle_outbox SET status='pending', next_attempt_at=CURRENT_TIMESTAMP(),
            lease_owner=NULL, leased_until=NULL, last_error=NULL
            WHERE authorization_id='<authorization_id>' AND intent_kind='settle'
            AND status='dead' AND last_error='activity_repair_expired'"
   ```

   The `last_error='activity_repair_expired'` predicate is load-bearing: it scopes
   the re-arm to THIS cause, so a mistyped or stale authorization id cannot
   silently resurrect an `already_released_free`, `reservation_missing`, or
   `invalid_row` dead row — those are money questions that must stay frozen for a
   human. If the statement reports 0 rows updated, you have the wrong row or the
   wrong cause; do not widen the predicate to make it match.

   Clearing `last_error` restarts the repair window, which is what you want after
   a fix. Replay is safe: the row hits the reservation claim gate, sees the prior
   charge, and only retries the index — it will not double-charge.
3. Confirm the row reached `done` and the request appears in the workspace's
   activity view.

Monitoring signals:

App log routing is a trap here, so know it exactly. INFO settle-outbox lines
such as `reaped N expired reservations` and `recovered settle charge` do ship
to Axiom via the scoped `trusted_router.*` package logger
(`TR_AXIOM_LOG_LEVEL`, default INFO), so on-call can search for them there.
`init_axiom()` lowers only the package logger and leaves root at uvicorn's
WARNING. Third-party INFO still does not ship because it gates on root's
WARNING. App records still never appear in Cloud Logging: once `init_axiom()`
attaches the root Axiom handler, `logging.lastResort` stops mirroring app
records to stderr. Search alerts and app INFO in Axiom
(`TR_AXIOM_DATASET=trusted-router-logs`), not `gcloud logging`. Cloud Logging
carries only platform request logs and uvicorn/unhandled-exception stderr
tracebacks. Judge reap/drain health by state, never by log lines:

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT COUNTIF(settled=false) open_holds,
         COUNTIF(settled=false AND expires_at < CURRENT_TIMESTAMP()) expired_open
         FROM tr_reservation"
```

`expired_open` should trend to near zero and stay there. New expirations from
abandoned requests are reclaimed within a few ticks.

Drain tick latency in request logs is a health signal: ~0.1s means nothing to
do; 15-40s means it is actively reaping a backlog, one claim transaction per
reaped hold. Sustained 40s+ ticks with `expired_open` not falling means
investigate for a silent per-row failure.

A persistently large `reaped` count means upstream abandonment (enclave crashes
or client disconnects before settle); investigate the enclave, not the drain.

Rollback normally by reverting the `TR_SETTLE_OUTBOX_ENABLED=true` line in
`scripts/deploy/rollout.sh` and merging. The pipeline redeploys flag-off; the
settle path is byte-identical. A normal merge never deploys cold regions: if
the cold-region dispatch was run for the flip, run
`gh workflow run deploy.yml -f deploy_cold_regions=true` again after the
revert merge's hot-region rollout completes so cold regions also return to
flag-off.

Emergency rollback in the same minute: move traffic to the previous pinned
revision in every affected region, then pause the scheduler:

```bash
gcloud run services update-traffic trusted-router --region=<r> \
  --to-revisions=<previous-pinned-revision>=100 --project=quill-cloud-proxy
gcloud scheduler jobs pause trusted-router-settle-outbox-drain \
  --location=us-central1 --project=quill-cloud-proxy
```

Find the previous pinned revision with `gcloud run revisions list`. Pending
and dead rows left behind keep their holds frozen; they are safe and resolve on
the next flip or via `release_approved`.

---

## <a id="credit-ledger"></a>Credit ledger operations (single typed book)

As of 2026-07 the JSON credit ledger is **retired**. Money lives in exactly one
book: the typed Spanner tables `tr_credit_balance` (workspace credit, keyed
`(workspace_id, shard)`) and `tr_key_limit` (per-key spend caps + usage, keyed
`(key_hash, shard)`), written only by conditional DML (reserve/release/finalize/
rebalance). The JSON `credit` / `api_key` entities in `tr_entities` are
**metadata only** now (auto-refill config, Stripe ids, key name/flags) — their
old money fields are stale and must never be read for money. There is no mirror,
no `backsync`, no dual-book `compare`, and no rollback-to-legacy: emergency
rollback is redeploying the previous revision (the typed book is authoritative
across the flip).

**Inspect a workspace's live balance** (sums all active shards):

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT SUM(total_credits) credits, SUM(total_usage) usage,
         SUM(reserved) reserved, SUM(total_credits-total_usage-reserved) available
         FROM tr_credit_balance WHERE workspace_id='<ws>'"
```

Never read money from the JSON `credit` row. App and operator money reads go
through `live_credit_summary`, which reads `tr_credit_balance` in production and
fails closed if the configured typed shard set is incomplete.

**The two standing tripwires (kept when the reconcile tooling was deleted):**

- `audit_typed_invariants` — the daily audit (`.github/workflows/typed-audit.yml`,
  11:43 UTC; a failing run alerts). It is now purely typed-INTERNAL: `reserved`
  equals the sum of that workspace/key's open typed-origin holds (both
  directions — it also flags an orphan open-hold group with no typed row), and
  `reserved >= 0`. It does NOT compare against JSON (that book is dead), so a
  stale JSON total can never false-alarm it. A failure means real drift between
  the reserved counter and live holds — investigate, do not just re-run.
- `repair_typed_reserved` — the fix for a drifted `reserved` (e.g. holds the
  reaper freed without decrementing under some past bug). Recomputes `reserved`
  from live open holds. Run read-only/dry first, then `--apply`. It still refuses
  nonzero-shard rows it can't reconcile — do not force it.

**Grant credit**: use `scripts/grant_credit.py` or the Stripe webhook path. Both
go typed-direct (`credit_workspace_typed_direct`) and are idempotent on a
`stripe_event` row, distributing the delta across active shards. The operator
command is dry-run-first and reports the authoritative available balance:

```bash
uv run python scripts/grant_credit.py \
  --email user@example.com --amount 100 \
  --event-id manual_grant_YYYY_MM_DD_reason --apply
```

Do not hand-write `tr_credit_balance`.

**Retired JSON-field cleanup**: `scripts/cleanup_legacy_credit_json.py` verifies
the typed invariant and every configured shard before removing the three stale
money keys. It preserves the credit row's Stripe, auto-refill, shard, and future
metadata. Run once without flags, review, then run with `--apply`. Re-running is
idempotent.

**Legacy retention backfill** (issue #357): `tr_reservation` rows written before
`terminal_at` arming shipped have it NULL, and Spanner's
`ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))` never deletes a
NULL-timestamp row — as of 2026-07-30 that was 1.17M settled rows (~97% of the
settled table) permanently exempt from the TTL. One-off, idempotent repair:

```bash
PYTHONPATH=src uv run python scripts/backfill_reservation_terminal_at.py
```

reports candidates / guard-excluded counts (dry run); add `--apply` to arm
`terminal_at = now` in batches (`--batch`, default 5000). Two predicates in the
UPDATE are load-bearing and must never be widened: `settled` (an open hold must
NEVER get a TTL fuse — the reaper owns its lifecycle) and the
`NOT EXISTS (... tr_settle_outbox ... status IN ('pending','dead'))` guard (a
frozen intent's evidence must not age out under it; the script reports such rows
as excluded and stops rather than spinning). Backfilled rows age out ~30 days
after the run. Run it off the deploy path in a low-traffic window — bulk DML
overlapping a rolling deploy produces the
[Aborted/wounded burst](#authorize-deadlock-burst). Safe to re-run any time;
once the debt drains the candidate count stays ~0 because steady-state arming
is structural.

---

## <a id="authorize-deadlock-burst"></a>Sentry "Aborted ... deadlock/wounded" burst on gateway authorize

Symptom: Sentry issues on `gateway_authorize` / `gateway_settle` /
`authorize_atomic` with Spanner messages like "Deadlock with higher priority
transaction" or "wounded by a higher priority transaction", in a burst. Each
event is one request whose retry loop exhausted its 20s wall-clock budget
(`TXN_BUDGET_SECONDS`, well under the 30s enclave HTTP timeout). Scattered
singles are retry-tail noise; bursts deserve triage.

Note (2026-07): the client impact of these is now a retryable **503 +
`Retry-After`**, not a 500 — a global `Aborted` handler maps the exhausted
transaction to `service_unavailable`, and the enclave's settlement-retry queue
absorbs the settle side. The Sentry `Aborted` groups (`QUILL-ROUTER-8/K/D/E/H`)
are marked resolved and will auto-reopen as *regressed* if the handler ever
stops catching one — so a NEW unhandled `Aborted` 500 means the handler
regressed, not just contention.

1. Check for operational churn first. Was a deploy rolling, or was DDL being
   applied? Schema changes wound in-flight read-write transactions. Receipt:
   the 2026-07-04 21:25-21:31 UTC burst was `migrate_typed_counters.sh` DDL
   applied while the Increment-4 deploy was still rolling. Rule: apply
   operator DDL only when no deploy is in flight, in a low-traffic window, and
   expect a brief Aborted blip even then. Pre-announce it so the page does not
   stall the rollout.

2. If there is no churn, it is almost certainly one hot tenant. The Sentry
   message names the conflict row:
   `conflict on keys with prefix [<workspace_id>,0] ... tr_credit_balance` or
   `[<key_hash>,0] ... tr_key_limit`. Every concurrent request from one tenant
   serializes on those two shard-0 singleton rows.

3. Profile the tenant read-only:

   ```bash
   gcloud spanner databases execute-sql trusted-router \
     --instance=trusted-router-nam6 --project=quill-cloud-proxy \
     --sql="SELECT workspace_id, COUNTIF(settled=false) open_holds, COUNT(*) total
            FROM tr_reservation WHERE key_hash='<key_hash>' GROUP BY 1"
   ```

   Also inspect the `tr_key_limit` / `tr_credit_balance` shard rows for
   reserved/usage on the named `<key_hash>` and `<workspace_id>`.

4. Stop any in-progress rollout and measure the full customer-facing burst.
   Do not assume the enclave absorbed it. Receipt: on 2026-08-20 one capped
   key generated a shard-zero retry storm with 1,667 billing-path 503s across
   four regions in 18 minutes even though Cloud Run readiness stayed green.
   Restarting Cloud Run does not repair a hot Spanner row. Rollback only when
   the regional billing gate ties failures to a new revision; otherwise move
   directly to the guarded online split and verify the affected customer's
   subsequent authorize/settle results.

Structural fix if a tenant does this chronically: **shard spreading is now
operable** (as of the 2026-07 credit/key row-sharding work). A hot workspace's
credit and per-key-usage rows can be split across N sub-ledgers via the guarded
operator (`.github/workflows/reshard-billing-workspace.yml` →
`scripts/shard_workspace.py`, two-phase pause → drain → atomic transition →
unpause; requires an explicit `--apply`). The authorize reject path also does a
lock-free precheck + bounded repair so no-move verdicts no longer take
whole-shard-set write locks. Do NOT hand-set shard columns; always go through
the operator. Exact lifetime key caps are escrowed across those rows while
retaining the precise global limit; a large fragmented hold uses one atomic
cold-path rebalance. Before activating spreading on any workspace, confirm the
credit-shard rebalance and exact-cap escrow fixes are deployed. A negative
per-shard headroom from an overage settle must return a clean 402, never a
`_RebalanceInvariantError` 500.

---

## <a id="reshard-interrupted"></a>One workspace 503s "Workspace billing is paused" (interrupted reshard)

Symptom: every request from exactly ONE workspace returns
`503 Workspace billing is paused` with `Retry-After: 30`, while every other
tenant is healthy. Key creation for that workspace fails the same way
(`assert_workspace_billing_active` guards authorize/validate and every
key-minting path). Settle is deliberately NOT guarded, so in-flight work still
finalizes rather than stranding money. It does not follow that holds always
reach zero — see the frozen-hold case below.

The near-certain cause is a **reshard that ran `prepare --apply` but never
reached `finish`**: the runner died, the workflow was cancelled, or the operator
walked away. Once `prepare --apply` has paused the workspace, every subsequent
exit — success and failure alike — leaves it paused on purpose. This is
fail-safe, not a bug: an unverified shard set must not take live traffic. (A
dry run, without `--apply`, returns before pausing and cannot cause this.)

Confirm the cause before touching anything. The pause reason names it:

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT body FROM tr_entities WHERE kind='workspace' AND id='<workspace_id>'"
```

`body` is the workspace JSON; read its `billing_paused` and
`billing_pause_reason` fields. `"billing_pause_reason": "credit-row reshard
prepare"` is the interrupted-reshard signature. Any other reason means someone
paused this workspace for a different purpose — stop and find out why before
unpausing.

**Recovery.** Read the shard state first (read-only, safe at any time — this is
the `status` operation of `.github/workflows/reshard-billing-workspace.yml`, or
locally):

```bash
PYTHONPATH=src uv run python scripts/shard_workspace.py status --workspace <workspace_id> --shards <N>
```

`<N>` must be the SAME target shard count the interrupted run used. It prints
`current_shards`, `target_shards`, `ready`, `at_target`, `applied`, the typed
totals, open reservations, and a `BLOCKED:` line per unmet precondition, then one
line per API key. Booleans print as `True`/`False`.

**Read `at_target`, not `ready`.** `ready` only means "nothing blocks a
reshard" — a drained, healthy, paused workspace still at 1 shard is `ready=True`
against a target of 16. `at_target` is the one that says the ledger actually
adopted the target count. (`applied` is always `False` here: `status` only
inspects, it never applies.) Then:

- **`at_target=True` and `ready=True` on the credit row and every key** → the
  transition landed; only the unpause is missing. Run `finish --apply` with the
  same `--shards <N>` and the same `--preserve-open-holds` value. `finish`
  re-verifies the whole shard set and only then clears `billing_paused`,
  refusing with `ERROR: refusing to unpause; ...` if anything is unclean or not
  at the target.
- **`at_target=False`** → the transition did not complete. Re-run
  `prepare --apply` with the same arguments; it is idempotent. The usual blocker
  is open holds that had not drained, and since settle keeps running while
  paused, waiting a few minutes and re-running is normally enough. Then run
  `finish --apply`. Exit **2** means the *credit ledger* was still draining
  (retry shortly, nothing is wrong); an API-key drain blocker exits **1**, as do
  argument and workspace-resolution errors. So read the printed `BLOCKED:` lines
  rather than the exit code alone — a `wait for drain` reason on a key line is
  just as retriable as one on the credit line, despite the different code.
- **`at_target=True` but `ready=False`** → the transition DID land and
  verification found something else wrong. Re-running `prepare` will not help;
  it re-inspects and returns the same unready status. Read the `BLOCKED:` lines
  and fix the named condition.

**If the holds never drain, stop waiting and check the settle outbox.** A
`pending` or `dead` outbox row deliberately excludes its reservation from the
reaper (`_REAP_SCAN_GUARDED_SQL`), so a frozen row pins an unsettled hold
indefinitely and `wait for drain` can never succeed on its own:

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 --project=quill-cloud-proxy \
  --sql="SELECT o.authorization_id, o.intent_kind, o.status, o.last_error
         FROM tr_settle_outbox o JOIN tr_reservation r
           ON r.authorization_id = o.authorization_id
         WHERE r.workspace_id='<workspace_id>' AND r.settled=false
           AND o.status IN ('pending','dead')"
```

Resolve those rows first — see
[Settle outbox](#settle-outbox) — then re-run `prepare --apply`. This is a
correctness feature, not a deadlock to force past: the hold is frozen because a
money question about it is still open.

Mutating operations need `--apply` locally, or `confirmation: APPLY` in the
workflow. They differ: locally, omitting `--apply` runs a real dry run
(`finish` without `--apply` performs the complete verification and prints
`DRY-RUN: would unpause this verified workspace` without touching the pause —
the ideal rehearsal). In the *workflow*, omitting `confirmation: APPLY` on a
mutating operation is refused outright with
`Mutation refused: type APPLY in confirmation` and exit 2 — it does not dry-run.
Dispatch `operation: status` for a read-only look via the workflow.

`finish` is the ONLY way back to serving traffic. Do not hand-clear
`billing_paused` and do not hand-set shard columns — both bypass the shard-set
verification that is the entire point of the two-phase design, and a workspace
serving on an unverified shard set can under-count spend across sub-ledgers.

One thing that looks like this but is not: the workflow serializes on
`concurrency: production-billing-workspace-reshard`, so a second dispatch waits
rather than racing a half-finished workspace — a queued run is expected, not a
symptom. Exact lifetime caps no longer pin a key to shard zero: the operator
partitions the cap into escrow sub-budgets whose sum remains the configured
limit. A capped key that still reports one shard after a 16-shard operation is
therefore incomplete and must not be unpaused by hand.

---

## <a id="dns-vendor-split"></a>DNS-vendor-split symptoms (Cloudflare vs Cloud DNS)

Cloudflare and Google Cloud DNS are both authoritative for
trustedrouter.com (Stage 4f multi-vendor design). When their record
sets drift, real-user impact looks like:

- Trust page intermittently broken (some users see the right page,
  others see a 404 / wrong content)
- Google Search Console domain verification fails
- Cloudflare emails "trustedrouter.com no longer using our nameservers"
- Some endpoints intermittently NXDOMAIN

**Diagnose**:

```bash
# Compare both vendors side by side:
for ns in ns-cloud-b1.googledomains.com dom.ns.cloudflare.com; do
  echo "=== $ns ==="
  for record in trustedrouter.com trust.trustedrouter.com www.trustedrouter.com; do
    cn=$(dig +short CNAME $record @$ns)
    a=$(dig +short A $record @$ns)
    echo "  $record: A=$a CNAME=$cn"
  done
  echo "  apex TXT: $(dig +short TXT trustedrouter.com @$ns | head -1)"
  echo "  apex NS:  $(dig +short NS trustedrouter.com @$ns | wc -l) records"
done

# Which public resolvers cache which vendor:
for r in 1.1.1.1 8.8.8.8 9.9.9.9; do
  echo "  $r → trust = $(dig +short trust.trustedrouter.com @$r | head -1)"
done
```

Both vendors should return identical answers for every record;
each vendor's apex NS should list all 6 NS (4 Google + 2 Cloudflare).
Public resolvers should all agree on every name.

---

**Fix**:

The fast one-shot path that brings Cloud DNS into sync with
Cloudflare:

```bash
cd /Users/jperla/claude/quill-cloud-proxy
gcloud config set account josephjavierperla@tt.live  # needs DNS admin
bash tools/fix-trustedrouter-dns.sh
```

The durable pin (do this once after the one-shot):

```bash
cd /Users/jperla/claude/quill-cloud-proxy/tools/dns
# Follow README.md to set up env vars + import existing records.
terraform plan      # should be "No changes" once imports are clean
```

After that, all DNS changes go through `terraform apply` and both
vendors stay in sync atomically.

**Don't fix it by**:
- Removing Cloud DNS NS from the registrar (loses Stage 4f vendor
  redundancy — Cloudflare-only means Cloudflare-outage = TR-outage)
- Hand-editing one vendor and not the other (caused this in the
  first place; Terraform pin prevents recurrence)
- Setting different TTLs across vendors (cache lifetime divergence
  multiplies resolver-state randomness)

---

## Search Console / Bing Webmaster Tools

TrustedRouter should be verified in both Google Search Console and Bing
Webmaster Tools at the domain level.

Canonical crawl assets:

- `https://trustedrouter.com/robots.txt`
- `https://trustedrouter.com/sitemap.xml`
- `https://trustedrouter.com/llms.txt`
- `https://trustedrouter.com/docs/llms.txt`
- `https://trustedrouter.com/docs/llms-full.txt`
- `https://trustedrouter.com/360a02e48445d297f9612a4c3fef878b.txt`

Submit only the sitemap index, not every child sitemap:

```text
https://trustedrouter.com/sitemap.xml
```

Bing-compatible fast indexing uses IndexNow:

```text
key: 360a02e48445d297f9612a4c3fef878b
keyLocation: https://trustedrouter.com/360a02e48445d297f9612a4c3fef878b.txt
endpoint: https://api.indexnow.org/indexnow
```

If domain verification fails, diagnose DNS vendor drift before changing
application code. Google and Bing should both see the same TXT records
from Cloud DNS and Cloudflare if both are authoritative. If Bing offers
an HTML meta verification value instead of DNS, prefer DNS. Only add a
meta tag to the public templates as a temporary fallback, and remove it
after DNS verification works.

After deploys that add SEO pages:

1. Fetch `/robots.txt`, `/sitemap.xml`, and `/llms.txt`.
2. Submit changed URLs for recrawl in Bing and Google.
3. Check `/docs/llms-full.txt` still lists model/provider pages and does
   not contain secrets.
4. Follow `docs/marketing/llm-seo-opportunities.md` for Ahrefs exports
   and new page prioritization.

---

## <a id="adding-a-cloud"></a>Adding a cloud (and when it is allowed to be called done)

**Symptom this prevents:** a cloud that serves traffic, shows an all-green
status page, and records none of its operational history — because the process
that moves rows out of its outbox was never installed, and the only alarm for
that is emitted by the missing process. AWS-EU ran that way from 2026-08-02 to
2026-08-17: 470,897 undelivered rows, `activity_generations` empty, no page.

**The rule: a cloud is not in service until rows are observed moving.**

Check any cloud, from anywhere, with no credentials:

```bash
bash scripts/deploy/verify_cloud_complete.sh aws     # or azure, gcp
```

It exits non-zero until all five stages hold, each naming its own fix:

| # | Stage | Fix when it fails |
|---|---|---|
| a | in the fleet freshness registry (registered, not watched — that workflow has no schedule trigger yet) | add a `FleetAnalyticsEndpoint` in `src/trusted_router/operational_analytics_fleet.py` |
| b | `/status.json` has the `analytics` section | deploy a control plane whose status snapshot publishes `drain_lag_seconds` |
| c | `analytics.available` is true | the control plane cannot read its outbox — check its database connection |
| d | `drain_lag_seconds` under bound | the drain is stopped or behind: `bash scripts/deploy/aws_eu_clickhouse_drain_install.sh` |
| e | control-plane outbox enabled | set `TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true` in that cloud's deploy script |

Stage (e) is a static read of that deploy script **in your working tree**, not
of the revision the cloud is running: it tells you what a deploy from this
checkout would set, and stages (b)–(d) are the evidence about the running
service.

**There is no way to excuse a stage.** No waiver, no exemption field, no flag,
no environment variable. A cloud that cannot be checked is NOT VERIFIED and the
run exits non-zero with the reason printed. (An earlier revision had an
`analytics_absent_reason` that waived "structural" blockers, plus the machinery
to decide which failures counted as structural. Review found bugs inside that
machinery twice, the second set introduced by the fix for the first, so it is
gone.) `TR_MAX_DRAIN_LAG_SECONDS` and `TR_STATUS_URL` are read only so the
script can tell you loudly that it is ignoring them.

Exit codes (`scripts/deploy/cloud_complete_gate.sh` turns each into the same
words for every bound script):

| code | meaning |
|---|---|
| 0 | `VERIFIED` — every stage was measured and held. The banner then says what that does *not* establish, which is that rows were seen moving |
| 5 | `NOT YET OBSERVABLE` — the page parses and carries no `analytics` section, so the question cannot be asked from outside yet. Its own code because it is the state a cloud is in before its control plane publishes the section, and the run that installs a drain hits it by construction |
| 1 | `NOT VERIFIED`, for everything else, with the reason printed: a stage failed, the page did not answer 200, the body was not the status document, the cloud is unknown, the arguments were wrong |

The AWS and Azure bring-up and control-plane scripts end by running this, so an
exit of 0 from one of them means the check passed — which is a statement about
what the check measures, not a certificate that the cloud works. Read the
banner: it lists the five stages and then says, every time, that rows moving is
not among them.

That binding is not taken on trust. Those scripts — GCP's included — are
executed end to end against a stub `PATH` in
`tests/test_deploy_script_execution.py`, which asserts each one calls the gate,
cannot exit 0 over a failing gate, runs no cloud CLI after the gate answered,
and passes both exit codes through unchanged. The one exception is
`aws_eu_clickhouse_drain_install.sh`, whose SSM-heavy middle cannot be stubbed
honestly — its tail is claimed, not proven, and `ROLLOUT_REGISTRY` says so.
Which scripts are in which list is checked for exact set equality against
`docs/storage-portability/multi-cloud-separation.md`, so a script cannot quietly
lose its behavioural coverage while the docs still call it proven.

**GCP's exempt file is `rollout.sh`, not GCP:** `rollout.sh` runs inside
`.github/workflows/deploy.yml`, and ending it here would put a fetch of
`trustedrouter.com/status.json` in the middle of deploying the cloud that serves
it — the deploy that repairs an outage would abort partway. GCP is instead
checked out of band by `scripts/deploy/verify_gcp_complete.sh`, which the
`verify-cloud-complete` job in that same workflow runs as its whole body, and
which the behavioural harness executes like every other bound script. Coverage,
exactly: every run in which the `deploy` job ran, whatever its result —
including a deploy that failed partway having already mutated production. It
does NOT cover a run that skipped `deploy`, and `migrate-schema` and
`sync-runtime-secrets` mutate production before `deploy` gets there. You can
always run it yourself, from anywhere, with no credentials:

```bash
bash scripts/deploy/verify_gcp_complete.sh
```

If a script exits non-zero it prints the exact next command; run it and re-run
the script, which is idempotent. Do not work around the exit code — that is the
failure mode this exists to stop.

**Do not read the fleet's state out of this runbook — run the gate.** A cloud
starts answering when a control plane built from the publisher is deployed to
it, and exits 5 until then. The last reading taken while editing this section
was `gcp` VERIFIED with `aws` and `azure` both at 5, and it is a note about a
moment rather than a claim about now. Azure additionally fails stage (e): it has
no operational-analytics outbox at all. See
`docs/storage-portability/multi-cloud-separation.md` §7 for the full definition
of done and the checklist for a new cloud.

**Last check that cannot be automated from outside:** once a cloud passes, look
at the count from inside it, twice, ten minutes apart:

```bash
clickhouse-client --query 'SELECT count() FROM activity_generations'
```

Two numbers, the second larger. A drained outbox and a disabled outbox both
publish `drain_lag_seconds: 0.0`; only the count tells them apart.

---

## <a id="useful-one-liners"></a>Useful one-liners

Live phala model list:
```bash
PHALA_KEY=$(grep -E "^PHALA_CONFIDENTIAL_API_KEY=" ~/.quill_cloud_keys.private | sed 's/^[^=]*=//' | tr -d '\n')
curl -sS https://api.redpill.ai/v1/models -H "Authorization: Bearer $PHALA_KEY" | jq -r '.data[].id'
unset PHALA_KEY
```

Smoke a gateway provider+model end-to-end:
```bash
SMOKE_KEY=$(gcloud --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  secrets versions access latest \
  --secret=trustedrouter-synthetic-monitor-api-key --project=quill-cloud-proxy)
curl -sS -X POST https://api.quillrouter.com/v1/chat/completions \
  -H "Authorization: Bearer $SMOKE_KEY" -H "Content-Type: application/json" \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":4,"provider":{"only":["<provider>"]}}'
```

TR catalog endpoint count by provider in the deployed snapshot:
```bash
python3 -c "
import json
with open('src/trusted_router/data/openrouter_snapshot.json') as f:
    s = json.load(f)
from collections import Counter
c = Counter()
for m in s.get('models', []):
    for ep in m.get('endpoints', []):
        c[(ep.get('provider_name') or 'unknown').lower()] += 1
for prov, n in sorted(c.items(), key=lambda kv: -kv[1]):
    print(f'{prov:20s} {n}')
"
```

Per-region MIG status (GCP enclave):
```bash
for entry in \
  us-central1:quill-enclave-mig-us \
  us-east4:quill-enclave-mig-useast4 \
  europe-west4:quill-enclave-mig-eu \
  southamerica-east1:quill-enclave-mig-sa; do
  region=${entry%%:*}
  mig=${entry#*:}
  echo "=== ${region} ==="
  gcloud compute instance-groups managed describe "${mig}" \
    --region="${region}" --project=quill-cloud-proxy \
    --format='value(versions[0].instanceTemplate,targetSize,status.isStable)'
done
```
