# TrustedRouter Operator Runbook

Keyed by symptom → action. When a customer reports something or a synthetic
monitor pages, find the matching section and follow the steps. Every entry
came from a real incident; the linked commits are the receipts.

Index:
- [Provider returns 502 "provider error" via the gateway](#provider-502)
- [Provider returns sustained 429 "rate limit exceeded"](#provider-429)
- [Provider returns 401 "Invalid API key" via the gateway](#provider-401)
- [Smoke test for a provider returns "gateway authorization failed" 400](#gateway-auth-400)
- [GCP enclave deploy keeps auto-rolling back europe-west4](#eu-rollback)
- [GCP enclave deploy fails with "unrecognized arguments: --min-ready"](#min-ready)
- [Hourly price bot commits but TR catalog stays stale](#bot-doesnt-deploy)
- [Status page shows a region "down" but the region is actually healthy](#stale-status)
- [`refresh.py` reports "too_many_failures" locally](#local-refresh-fails)
- [A provider serves a model but TR's `/v1/models` doesn't list it](#missing-model)
- [Adding a brand-new provider to TR](#new-provider)
- [Adding a model to an existing provider](#new-model)
- [Rotating a provider API key](#rotate-key)
- [Spinning up Phala / RedPill again after a key issue](#phala-revive)

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
   - Run `bash tools/sync-secrets-to-aws.sh --apply --secret trustedrouter-<provider>-api-key`
     to mirror to AWS.
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
8. `enclave-go/internal/llm/http_client_aws.go` — add the vsock tunnel entry.
9. `enclave-go/internal/types/types.go` — add the `<Provider>APIKey string` field.
10. `enclave-go/internal/bootstrap/bootstrap_gcp.go` + `parent/src/quill_parent/bootstrap_server.py` — fetch the new secret.
11. `tools/deploy-gcp-mig.sh` — `QUILL_<PROVIDER>_SECRET` default + tee-env entry.
12. `tools/deploy-aws-nitro.sh` — vsock-proxy.yaml allowlist + write_vsock_unit.
13. `tools/sync-secrets-to-aws.sh` — `SECRETS` allowlist.
14. Add the key to `~/.quill_cloud_keys.private`, run `secrets.sh`, then
    `sync-secrets-to-aws.sh --apply`.

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
3. `bash tools/sync-secrets-to-aws.sh --apply` — mirrors to AWS.
4. Redeploy enclave: GCP picks up automatically on next deploy; AWS via
   `bash tools/deploy-aws-nitro.sh --apply --phase compute` (rebuilds LT,
   triggers ASG instance refresh).

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
- AWS Secrets Manager as `quill/trustedrouter-phala-confidential-api-key`

If Phala 401s after a re-enable:
1. Run a direct probe with the keyfile value against `api.redpill.ai/v1/chat/completions`
   with a `phala/<model>` id. If 200, secret is fine; rebuild enclave.
2. If 401, get a fresh confidential-tier key from `cloud.phala.com` and
   follow [Rotate a key](#rotate-key).
3. Email Yan @ Phala (`leechael@phala.network`) if Phala-side has issues.

Confidential AI docs:
https://docs.phala.com/phala-cloud/confidential-ai/confidential-model/confidential-ai-api

---

## Useful one-liners

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
for region in us-central1 europe-west4 us-east4; do
  short=${region%%-*}
  echo "=== $region ==="
  gcloud compute instance-groups managed describe quill-enclave-mig-${short:0:2} \
    --region=$region --project=quill-cloud-proxy \
    --format='value(versions[0].instanceTemplate,targetSize,status.isStable)'
done
```
