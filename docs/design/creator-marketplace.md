# Creator Model Marketplace — implementation design (handoff for codex)

**Written 2026-07-04. Audience: codex-cli/opus with fresh context. Read fully before touching anything — prod billing is involved in Track C.**

Decisions locked by Joseph 2026-07-04: (1) margin split = 50/50 of NEW margin only, existing 1.10× platform markup untouched; (2) v1 = prompt models only, no fine-tunes; (3) ≥1 eval report REQUIRED for public listing; (4) evals are heterogeneous per-model and CANNOT be platform-automated in v1 — creators/partners run them manually, platform stores + displays structured results, trust-based (`creator-reported`); (5) pricing = % markup + optional fixed per-call minimum (greater-of).

## 0. What exists (orient before coding)

- **CustomModel** (`src/trusted_router/storage_models.py:148-159`): `id "trustedrouter/user-{slug}"`, `owner_user_id`, `owner_workspace_id`, `name`, `base_model_id`, `hidden_prompt` (≤262,144 chars), `revision` (bumps on prompt change), `enabled`. CRUD: `routes/custom_models.py` (`/v1/custom-models*`), console: `routes/console/custom_models.py` + `templates/console/custom_models.html`. Storage: `storage_custom_models.py` (in-mem) + `storage_gcp_custom_models.py` (Spanner kind `custom_model`). Limits: 10/user, slug `^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])?$`. Base-model validation: `custom_model_rules.py` (no nesting, chat-capable, not monitor).
- **Request flow**: buyer calls custom id → `routes/internal/gateway.py` `authorize_gateway` (~line 86-99) resolves it, rewrites `body.model` to `base_model_id`, forwards `custom_model_id` + `custom_model_revision` to the attested enclave (separate Go repo `quill-cloud-proxy`), which injects `hidden_prompt`. `_force_custom_model_credit_routes` already forces prepaid-credit endpoints (BYOK excluded).
- **Money**: everything integer **microdollars**. `pricing.py:83-92` `_PRICE_MARKUP_RATIO=1.10` + $0.01/M floor = the published buyer base price. Authorize creates one-shot reservation (Spanner kinds `gateway_authorization`, `reservation`, `credit`); `_settle_gateway_authorization` (`gateway.py:496-715`) settles actual cost; `/internal/gateway/refund` reverses. Per-call records → Bigtable kinds `generation` + `generation_by_workspace` (id `"{ws}#{yyyy-mm-dd}#{ts}#gen-{id}"`).
- **Stripe**: checkout top-ups, saved cards, auto-refill (`services/stripe_billing.py`, `services/auto_refill.py`), webhook `/v1/internal/stripe/webhook` with `stripe_event` idempotency kind. **NO Connect code exists** — Connect is enabled in the Stripe Dashboard only.
- **Orchestration models**: primitives `synth|advisor|selector|mapreduce|subagent` + versioned aliases (socrates-1.1, aristotle-1.1, plato-pro-1.0, iris-1.0, prometheus-1.0, zeus-1.0). Sub-model ORDER tuples = data (`catalog_data.py:963-1138`, `ORCHESTRATION_PRIMITIVE_BY_MODEL_ID` at ~880). Step PROMPTS are hardcoded in enclave Go (`fusion.go`, `advisor.go`, `combo.go`, `subagent.go`). Custom models may already use an orchestration model as base.
- **Public pages**: `/models/{author}/{slug}` sections benchmarks/providers/performance/pricing/uptime/api (`dashboard.py`, `MODEL_SEO_SECTIONS`); `benchmark_scores.py` renders ONLY cited scores (class A/B/T). Console template idiom: `{% extends "console/_layout.html" %}`, `<section class="panel"><div class="panel-head">…` + `panel-body`, `pill`/`pill good`/`pill warn`, `console-flash`, forms POST to console routes with redirect+flash.
- **Settings flags**: plain `foo_enabled: bool = False` fields on `Settings` (`config.py`), env `TR_FOO_ENABLED`.
- **Jobs**: Cloud Run Jobs deployed via `scripts/deploy/synthetic.sh` pattern.

## 1. Feature flags (config.py — all default False)

```python
creator_commerce_enabled: bool = False        # Track C master: pricing fields accepted, surcharge math live
creator_connect_enabled: bool = False         # C5+: onboarding + webhook + payouts
marketplace_orch_config_enabled: bool = False # E1+: orchestration prompt suffixes accepted
enclave_supports_orch_prompts: bool = False   # E2: gate sending suffixes to enclave (flip after Go ships)
marketplace_public_enabled: bool = False      # E5/C6: public listing + public pages
```
Flag off ⇒ byte-identical behavior to today (snapshot-tested).

## 2. Data model changes

### 2.1 CustomModel — new fields (defaults = today's behavior; old rows MUST deserialize)
```python
visibility: str = "private"              # "private" | "unlisted" | "public"
pricing_markup_bps: int = 0              # 0..50_000 (500% cap)
pricing_fixed_microdollars: int = 0      # per-call minimum floor, 0..10_000_000 ($10 cap)
pricing_revision: int = 0                # bumps on pricing/visibility change (separate from `revision`)
orchestration_config: dict | None = None # §8; any change bumps `revision`
```
`revision` semantics extended: bumps on `hidden_prompt` OR `orchestration_config` OR `base_model_id` change (pricing changes bump only `pricing_revision`).

### 2.2 New Spanner kinds (existing pattern: kind, id, body JSON, commit-ts; serde modules mirror `storage_gcp_custom_models.py`)
- **`connect_account`** — id = `owner_user_id`. body: `{stripe_account_id, country, payouts_enabled: bool, details_submitted: bool, onboarding_status: "started|complete|disabled", requirements_due: [..], created_at, updated_at}`. Maintained by onboarding route + `account.updated` Connect webhook.
- **`payout_statement`** — id = `"{creator_user_id}#{yyyy-mm}"`. body: `{period, gross_margin_ud, creator_share_ud, adjustments_ud, carry_forward_in_ud, carry_forward_out_ud, payable_ud, stripe_transfer_id?, status: "finalized|paid|held|carried", finalized_at?, paid_at?, hold_reason?}`. Statement row transition to `finalized` inside a Spanner txn = the double-pay lock.
- **`custom_model_listing`** — id = custom_model_id. body: `{status: "draft|listed|delisted|suspended", listed_revision, author_handle, description (≤2000 chars), safety_review: {screen_version, verdict, categories, at}, listed_at?, delisted_at?, suspended_reason?}`. Kept separate from CustomModel so private CRUD is untouched.
- **`eval_report`** — id = `evr_{ulid}`. body (§6.1).

### 2.3 New Bigtable kind
- **`creator_earning`** — id = `"{creator_user_id}#{yyyy-mm}#{ts}#gen-{generation_id}"` (creator-keyed ⇒ payout job is one prefix scan per creator-month). body: `{custom_model_id, pricing_revision, buyer_workspace_id, base_price_ud, buyer_charge_ud, margin_ud, creator_share_ud, platform_share_ud, generation_id}`. Refund adjustments append `"...#adj-{id}"` rows with NEGATIVE amounts — never mutate existing rows.
- **`generation` row additions** (buyer-side): `custom_model_id?`, `base_price_microdollars?`, `surcharge_microdollars?` — explains the charge on buyer activity pages AND lets a reconciliation sweep rebuild lost accruals (generation write happens before accrual write).

## 3. Pricing engine — `src/trusted_router/services/creator_pricing.py` (NEW, pure, no I/O)

```python
@dataclass(frozen=True)
class CreatorCharge:
    buyer_charge_ud: int
    margin_ud: int
    creator_share_ud: int
    platform_share_ud: int

MAX_MARKUP_BPS = 50_000
MAX_FIXED_UD = 10_000_000

def compute_charge(base_price_ud: int, markup_bps: int, fixed_ud: int) -> CreatorCharge:
    """base_price_ud = the EXISTING settle-computed buyer price (post 1.10 markup,
    actual tokens incl. hidden prompt). Integer math only; floor division favors buyer;
    odd microdollar of margin goes to platform."""
    pct = base_price_ud * markup_bps // 10_000
    buyer_charge = max(fixed_ud, base_price_ud + pct)
    margin = buyer_charge - base_price_ud            # >= pct >= 0 ALWAYS
    creator = margin // 2
    return CreatorCharge(buyer_charge, margin, creator, margin - creator)

def validate_pricing(markup_bps: int, fixed_ud: int) -> None  # raises api_error(400)
def worst_case_charge(worst_case_base_ud: int, markup_bps: int, fixed_ud: int) -> int
    # = max(fixed_ud, worst_case_base_ud * (10_000 + markup_bps) // 10_000); monotone ⇒ reservation always covers settle
```
Invariants (property-test these): `buyer_charge >= base_price`; `creator + platform == margin`; `margin >= 0`; all outputs deterministic ints.
Worked examples (unit-test fixtures):
1. bps=2500, fixed=0, base=1,100,000 → charge 1,375,000; creator 137,500; platform 137,500.
2. bps=1000, fixed=50,000, base=9,900 → charge 50,000 (floor wins); margin 40,100 → 20,050/20,050.
3. Adversarial: bps=0, fixed=10,000, base=3,300,000 (500k-token call) → charge 3,300,000; margin 0; nobody underwater; console warns creators that bps=0 yields zero margin on big calls (recommend default 500 bps in the UI).

## 4. Authorize path (`routes/internal/gateway.py`, extend the custom-model block at ~86-99)

1. After resolving `custom_model`: **visibility check** — if `visibility == "private"` and `buyer workspace != owner_workspace_id` → 404 (today's behavior for everyone stays 404 until flag on). `unlisted`/`public` callable cross-workspace when `creator_commerce_enabled` AND listing status == `listed` (unlisted skips discovery, not access-gating: callable by anyone with the id).
2. **Snapshot pricing into the authorization body** (survives to settle; mid-flight price edits can't apply retroactively): `{"cm_markup_bps", "cm_fixed_ud", "cm_pricing_revision", "cm_creator_user_id", "cm_owner_call": bool}`. `cm_owner_call = (buyer_workspace == owner_workspace_id)` → forces surcharge 0 + no accrual (kills self-dealing at the root).
3. **Reservation sizing**: current worst-case base estimate `W` must ALSO add a hidden-prompt token estimate: `est_tokens = (len(hidden_prompt) + sum(len(s) for s in suffixes)) // 4`, priced at the base model's prompt rate. Then reserve `worst_case_charge(W, bps, fixed)`.
4. Orchestration suffix passthrough: §8.3.
5. Enclave payload otherwise unchanged — **the enclave never sees money**.

## 5. Settle + refund (`_settle_gateway_authorization`, `gateway.py:496-715`)

1. Compute `base_price_ud` exactly as today (no changes to `pricing.py`).
2. If snapshot present and not `cm_owner_call`: `charge = compute_charge(base_price_ud, snap.bps, snap.fixed)`; settle reservation for `charge.buyer_charge_ud` (single changed value inside the existing settle txn).
3. **Ordering: charge buyer first, accrue second.** After the Spanner settle txn commits: write `generation`/`generation_by_workspace` rows including the new fields, THEN append the `creator_earning` row. A crashed accrual is rebuildable from the generation row (add `scripts/reconcile_creator_earnings.py` sweep in C8); the reverse order would leak money.
4. Aborted streams (client disconnect): settle proceeds on actual delivered tokens with the full greater-of — otherwise buyers abort at 99% to dodge the floor.
5. Refund route: reverse `buyer_charge` (not just base) + append negative `#adj-` accrual row. Late refunds (post-payout) roll into next month's statement via adjustments.

## 6. Eval reports (v1: creator-reported data; NO platform runner)

### 6.1 `eval_report` body schema
```json
{
  "id": "evr_01H...", "custom_model_id": "trustedrouter/user-legal-reviewer",
  "model_revision": 7,
  "title": "Contract-clause extraction benchmark",
  "task_description": "500 real NDAs, extract 12 clause types...",   // ≤4000 chars
  "methodology_url": "https://... (optional)",
  "dataset": {"name": "internal-nda-500", "size": 500, "url": null},
  "metrics": [ {"name": "clause_f1", "value": 0.91, "unit": null, "n": 500, "ci_low": null, "ci_high": null} ],
  "baselines": [ {"label": "base model (kimi-k2.6, no prompt)", "metrics": [{"name": "clause_f1", "value": 0.78}]} ],
  "run_date": "2026-07-01",
  "artifact_urls": ["https://github.com/..."],
  "notes": "≤2000 chars",
  "verification_status": "creator-reported",   // enum now: creator-reported | platform-verified | attested (only first used in v1)
  "created_by_user_id": "...", "created_at": "..."
}
```
Validation: ≥1 metric; numeric values finite; urls http(s); title ≤120. Reports are IMMUTABLE (delete + re-create to fix; keeps provenance honest). Max 20 reports/model.

### 6.2 API (`routes/custom_models.py` additions, ManagementPrincipal auth, owner-only)
- `POST /v1/custom-models/{id}/eval-reports` → 201 report shape
- `GET  /v1/custom-models/{id}/eval-reports` → list
- `DELETE /v1/custom-models/{id}/eval-reports/{report_id}` → 204
Staleness is COMPUTED: report is `stale` iff `report.model_revision != model.revision` — returned in the shape, rendered as a banner; stale reports do NOT block continued listing in v1 (trust-based), but listing CREATION requires ≥1 fresh report.

### 6.3 Console UI (extend `templates/console/custom_models.html` — per-model card gets a tab strip)
Per-model card grows tabs: **Configure | Pricing | Eval reports | Listing** (plain anchors + server-rendered sections, matching existing form idiom; no SPA):
- **Eval reports tab**: table (title, key metric summary, run date, revision badge `rev 7` + `stale` pill when behind, delete button) + "Add eval report" form: title, task description (textarea), dataset name/size, metrics repeatable rows (name + value + n; JS `custom_models.js` add-row helper), baselines optional repeatable rows, run date (date input), methodology/artifact URLs, notes. Paste-friendly: also accept a single JSON textarea ("paste report JSON") that fills the form — this is the manual-partner workflow.

## 7. Listing + validation gate

### 7.1 State machine (`custom_model_listing`)
`draft → listed` (publish action) · `listed → delisted` (owner unpublish, or payouts disabled >14 days) · `listed → suspended` (admin/safety) · `delisted → listed` (re-publish re-runs the gate).

### 7.2 Publish gate (`services/listing_gate.py`, NEW — every check re-runs on every publish)
1. `creator_commerce_enabled` + `marketplace_public_enabled` flags on.
2. `connect_account.payouts_enabled == true` (no held-earnings liability, ever).
3. ≥1 eval report with `model_revision == model.revision`.
4. **Safety screen** (control-plane, fail-closed, `screen_version` recorded): (a) regex blocklist pass over hidden_prompt + suffixes + description (exfiltration/beacon URLs, jailbreak boilerplate, encoded payloads); (b) one moderation classification call through the platform's own router (cheap model, first-party moderation prompt; categories: illegal facilitation, CSAM, targeted hate, self-harm instruction, impersonation/deception); classifier error ⇒ publish blocked, retry later.
5. **Naming/impersonation** (extend `custom_model_rules.py`): author_handle + name + slug must not contain reserved substrings (`trustedrouter`, `official`, catalog vendor names, alias names socrates/aristotle/plato/iris/prometheus/zeus); Levenshtein ≤2 vs any catalog model id segment ⇒ reject; author_handle must not collide with existing `/models/{author}` catalog segments; slug immutable once first listed.
6. Pricing validated (§3 caps).

### 7.3 Public pages (`dashboard.py` + new `templates/public/community_model.html`)
- URL: `/models/{author_handle}/{slug}` (existing scheme; handle registry guarantees no first-party collision).
- Sections: header (name, author, description, `Community model` badge); **base-model disclosure** (link to base model page; if orchestration base: which steps carry creator customization — booleans only, NEVER content); **Eval results** — reports rendered with explicit provenance: `Reported by creator · {run_date} · rev {n}` styling clearly distinct from first-party cited scores (do NOT reuse class-A/B/T table component; new component `eval_report_card`); **Pricing** — buyer-facing: "base model usage + {pct}%" and/or "minimum ${fixed}/call", concrete example row (10K in / 1K out → $X); **API** section (curl with the model id).
- NOT shown, ever: hidden_prompt, suffixes, private notes. Add a render test asserting no prompt bytes appear in HTML/JSON.
- `noindex` + sitemap-excluded until: listed ≥14 days AND ≥100 settled cross-workspace calls from ≥3 workspaces (pattern: `PROVIDER_PERFORMANCE_INDEX_MIN_SAMPLES`).
- NO community leaderboard in v1 (heterogeneous evals aren't rankable). `/models` catalog page: listed community models appear ONLY under a separate "Community" section behind `marketplace_public_enabled`, never interleaved with first-party (snapshot-test first-party pages byte-unchanged).

## 8. Orchestration prompt config (append-only suffixes — never overrides)

Overrides are rejected: they'd let creators strip safety-relevant steps and would destroy the attested-baseline story. Creator text stays demarcated injected DATA, like hidden_prompt.

### 8.1 Schema (stored `CustomModel.orchestration_config`; valid ONLY when base resolves to an orchestration primitive/alias; any change bumps `revision`)
```json
{"version": 1, "steps": {
  "synth.draft": {"suffix": "..."}, "synth.synthesizer": {"suffix": "..."},
  "advisor.advise": {"suffix": "..."}, "advisor.final": {"suffix": "..."},
  "selector.select": {"suffix": "..."},
  "mapreduce.map": {"suffix": "..."}, "mapreduce.reduce": {"suffix": "..."},
  "subagent.plan": {"suffix": "..."}, "subagent.agent": {"suffix": "..."}}}
```
Step registry = new `ORCHESTRATION_STEP_IDS` in `catalog_data.py` (must mirror enclave step ids). Unknown keys → 400. Per-suffix ≤16,384 chars; SHARED budget: `len(hidden_prompt) + Σ len(suffix) ≤ 262,144`.

### 8.2 Console UI (Configure tab)
When base model is an orchestration primitive/alias: render one labeled textarea per step id valid for that primitive (resolve primitive via `ORCHESTRATION_PRIMITIVE_BY_MODEL_ID`), with helper text "Appended to the {step} step inside the attested gateway", live remaining-budget counter (JS), and the base's step list explained.

### 8.3 Gateway passthrough (gated on `enclave_supports_orch_prompts`)
Authorize adds to the enclave payload:
```json
"orchestration_prompts": {"version": 1, "steps": {"<step_id>": {"suffix": "..."}},
                          "config_sha256": "<hex of canonical JSON>"}
```

### 8.4 Enclave contract (ships as `docs/orchestration-prompts-enclave-contract.md`; Joseph implements in quill-cloud-proxy Go)
1. After composing each hardcoded step prompt, if payload carries a suffix for that step_id, append: `"\n\n<creator_instructions>\n" + suffix + "\n</creator_instructions>"` (fixed delimiter, part of attested source).
2. Absent/empty payload ⇒ **byte-identical prompts to today** (test this — it's the attestation invariant).
3. Unknown step ids: ignore + metric. 4. Suffix is opaque — no template expansion. 5. Re-validate lengths in-enclave; oversize ⇒ hard error, never truncate. 6. Unknown `version` ⇒ fail closed. 7. Log `config_sha256` (future co-signing hook).

## 9. Stripe Connect (Express + separate transfers; buyers keep prepaying platform credits)

### 9.1 Onboarding (`services/stripe_connect.py` + `routes/connect.py`, NEW)
- `POST /console/connect/onboard` (console session auth): create Express account (`capabilities: transfers`) if no `connect_account` row; persist; create Account Link (type `account_onboarding`, return/refresh → `/console/custom-models?connect=done|retry`); 302 to Stripe.
- Connect webhook: `POST /v1/internal/stripe/connect-webhook` — **separate signing secret** `TR_STRIPE_CONNECT_WEBHOOK_SECRET` (Secret Manager `trustedrouter-stripe-connect-webhook-secret`); handles `account.updated` → update `payouts_enabled`/`requirements_due`; reuse `stripe_event` idempotency kind. If payouts become disabled: statements → `held`, listings auto-delist after 14-day grace (banner + email in the grace window).
- Console UI: banner on the custom-models page when any model is `public`-intended but Connect incomplete ("Set up payouts to publish — 2 minutes with Stripe"); status chip (payouts active / action needed) + "Manage payouts" (Account Link type `account_update`).
- Unsupported countries: onboarding surface shows supported list (US/UK/EEA/CA/CH at launch) + waitlist copy; they simply can't publish (no held foreign earnings problem).

### 9.2 Monthly payout job (`jobs/creator_payouts_job.py`, Cloud Run Job, deploy cloned from synthetic.sh pattern; runs day 3 for prior month; also supports `--dry-run`)
Per creator (enumerate via custom_model rows with pricing set — bounded by 10/user):
1. Prefix-scan `creator_earning "{creator}#{yyyy-mm}#"` → sum shares + adjustments + prior `carry_forward_out`.
2. `payable < $25` OR negative → statement `status=carried`, roll forward. Else:
3. Spanner txn: write statement `status=finalized` (id uniqueness = the lock; if a statement already exists non-carried → skip, idempotent).
4. `stripe.Transfer.create(amount=payable_ud // 10_000 (cents, floor; sub-cent remainder → carry_forward_out), currency="usd", destination=acct, idempotency_key="payout-{creator}-{yyyy-mm}")` → mark `paid` + transfer id. Crash-rerun safe via statement status + Stripe idempotency key. **Kill-and-rerun test required.**
5. Fraud holds (C8): statement → `held` when (a) buyer-concentration: >70% of month margin from <3 payer workspaces or workspaces <30 days old; (b) card-fingerprint match between a buyer's top-up PaymentMethod and the creator's Connect person. `held` ≠ forfeit: console shows "under review", manual release CLI (`scripts/release_payout_hold.py`).
- 1099s: Express ⇒ Stripe files them; enable tax reporting in Dashboard (Joseph).
- Chargeback on a buyer top-up: reverse that workspace's accruals for the exposure window via negative adjustments (direct lookup: accrual rows carry buyer_workspace_id). Write off residual creator negatives < $5; larger → Transfer Reversal.

### 9.3 Creator earnings UI (console, new page `/console/earnings`)
- Header cards: available (unpaid accrued), last statement, lifetime paid.
- Per-model table this month: calls, GMV, margin, your share.
- Statements table: period, payable, status (`paid` w/ date, `carried`, `held` w/ "under review").
- Copy REQUIRED by research: show effective **% of GMV** (`margin/(2·charge)`) per model, not "50/50"; pricing editor shows a live take calculator: "at {bps}% markup, you earn ~{X}% of what buyers pay".

## 10. Buyer-facing UI touches
- Public listed-model page (§7.3) is the storefront.
- Activity page (`routes/console/activity.py`): generations on custom models show `base + creator fee` breakdown (new generation fields).
- `/v1/models`: listed community models appear with `"author"`, `"community": true`, and published pricing semantics (`pricing_mode: "markup_bps"|"floor"`, values) — gated on `marketplace_public_enabled`.

## 11. API surface summary (new/changed)
| Method+Path | Auth | Purpose |
|---|---|---|
| PATCH /v1/custom-models/{id} | owner | + pricing fields, visibility, orchestration_config |
| POST/GET/DELETE /v1/custom-models/{id}/eval-reports[/{rid}] | owner | eval report CRUD |
| POST /v1/custom-models/{id}/publish · /unpublish | owner | listing gate → state machine |
| GET /v1/community-models · GET /models/{author}/{slug} | public | discovery + storefront (flag-gated) |
| POST /console/connect/onboard · GET /console/earnings | console | Connect + earnings |
| POST /v1/internal/stripe/connect-webhook | stripe sig | account.updated |
| (internal) authorize/settle/refund | internal | snapshot + surcharge + accrual (§4-5) |

## 12. Testing requirements (repo test-pyramid rule: unit + functional + integration per feature)
- creator_pricing: worked examples 1-3, property tests (invariants §3), cap validation. NO floats anywhere (grep-test).
- Serde: old custom_model rows without new keys deserialize to defaults (fixture with a pre-migration JSON blob).
- Authorize/settle: cross-workspace charge math from snapshot; owner-call zero-surcharge; reservation ≥ settle invariant (property test over token counts); abort-stream; refund symmetry + negative adjustment row; conservation (`provider_cost + platform_markup + creator_share + platform_share == buyer_charge`).
- Payout job: month replay with adjustments + carry-forward + threshold; kill-and-rerun idempotency; dry-run mode makes zero writes/transfers.
- Listing gate: each check independently red/green; impersonation fixtures (socrates lookalikes, catalog edit-distance); fail-closed moderation error.
- Public pages: snapshot test first-party pages byte-unchanged with all flags on; prompt-leak grep test over rendered community page HTML+JSON; noindex threshold matrix.
- Orchestration config: step registry validation, shared budget, revision bump on every mutation, payload sha256 determinism, field ABSENT when `enclave_supports_orch_prompts=false`.
- Flags: every new route 404s / every new field rejected when its flag is off.

## 13. Rollout order
1. Land C1-C4 + E1-E3 dark (flags off) → 2. Joseph ships enclave Go per §8.4 → flip `enclave_supports_orch_prompts` + `marketplace_orch_config_enabled` → 3. Onboard launch partner: build their model, run their evals manually together, attach reports via console → 4. C5-C7 + E4-E5 land → flip `creator_commerce_enabled` + `creator_connect_enabled`, partner completes Connect onboarding → 5. Flip `marketplace_public_enabled`: partner model lists publicly with **payouts demonstrably live the same day** (the #1 GPT Store lesson) → 6. C8 fraud holds + reconcile sweep before opening publish beyond hand-picked founding creators (ryan/daniel/evan are the recruit list).

## 14. PR packets for codex (one branch each off origin/main; Claude reviews every diff pre-PR; merge-train after green)
| # | Packet | Files | Must-pass |
|---|---|---|---|
| C1 | pricing engine + flags | services/creator_pricing.py, config.py | §3 examples + property tests |
| C2 | schema + serde + PATCH validation | storage_models.py, storage_custom_models.py, storage_gcp_custom_models.py, storage_gcp_connect.py, storage_gcp_payouts.py, schemas.py, routes/custom_models.py | back-compat fixture; flag-off rejection |
| C3 | authorize snapshot + reservation + visibility | routes/internal/gateway.py | reservation-covers-settle property |
| C4 | settle/accrual/refund + generation fields | gateway.py, bigtable writers | conservation + ordering + refund tests |
| E1 | orch config storage + console Configure tab | storage/custom-model modules, custom_model_rules.py, catalog_data.py (step registry), console template+js | budget/registry/revision tests |
| E2 | authorize passthrough + enclave contract doc | gateway.py, docs/orchestration-prompts-enclave-contract.md | byte-exact payload; absent-when-flag-off |
| E3 | eval reports (entity+API+console tab) | new storage module, routes/custom_models.py, console template+js | immutability; staleness; JSON-paste path |
| E4 | listing gate + state machine + safety screen | services/listing_gate.py, custom_model_rules.py, routes | gate matrix; fail-closed; impersonation |
| C5 | Connect onboarding + webhook + console banner | services/stripe_connect.py, routes/connect.py, secrets.sh, rollout.sh | webhook idempotency; separate secret |
| C6 | publish ⇔ payouts_enabled + grace auto-delist | listing gate, webhook handler | state-machine functional tests |
| E5 | public pages + /v1/models community + activity breakdown | dashboard.py, templates/public/community_model.html, routes | first-party snapshot; prompt-leak; noindex |
| C7 | payout job | jobs/creator_payouts_job.py, deploy script | kill-and-rerun; dry-run; carry-forward |
| C8 | fraud holds + earnings page + reconcile sweep | job, routes/console/earnings, scripts/ | hold heuristics; release CLI; sweep rebuild |

## 15. Explicitly out of scope for v1 (designed-for, deferred)
Platform-run/standardized evals, eval billing, attested-eval claims + transparency log (the `verification_status` enum is the v2 hook), community leaderboard, fine-tune training/serving, BYOK marketplace calls (already force-credit), non-USD payouts.
