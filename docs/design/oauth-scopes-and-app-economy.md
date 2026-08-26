# Sign in with TrustedRouter: scopes, verification disclosure, and the app economy

Status: DRAFT (design in progress, 2026-08-25). Increments merge separately.

## What this delivers

1. **Scopes** on the OAuth / "Sign in with TrustedRouter" flow, enforced
   fail-closed on delegated keys.
2. **Verification-level disclosure**: an app can learn how verified the
   signing-in user is — `none < email < phone < identity` — resolved from
   state TrustedRouter already maintains (email verification, phone
   verification via SMS/voice code, Veriff full-ID verification from the
   Custom Models program).
3. **App registry + markup economy**: a registered app can add a percentage
   markup on top of token costs for requests made through its delegated
   keys. The signing-in user pays the marked-up total from their credits;
   **30% of the markup is TrustedRouter's; 70% is credited to the app
   owner's earnings wallet in TR credits** — the same
   `tr_earnings_balance` wallet, idempotent-credit machinery, and 70/30
   basis-points split the Custom Models program already runs in
   production (`custom_model_billing.py`, `credit_user_earnings`,
   `transfer_earnings_to_workspace`).
4. **The four hand-off gaps** closed or explicitly contracted:
   - delegated keys can read a balance summary (new scope, no management
     surface exposure);
   - the flow gets a conformant OAuth 2.1/PKCE surface (standard
     parameter names, token endpoint semantics, error envelope);
   - scopes exist and are stated on the consent screen (no more
     full-workspace implicit grant);
   - `app_id` reaches the ledger (authorization → reservation →
     generation → activity), unlocking attribution and the markup
     payout itself.

## Design decisions (Fable, using the precedents in this repo)

### Scopes: small, real, enforced

`ApiKey.scopes: list[str] | None`. `None` = legacy unscoped key (full
current behavior, unchanged). A key minted through the OAuth flow always
gets explicit scopes. Enforcement is fail-closed for scoped keys at the
auth dependency chokepoints; unscoped keys behave exactly as today.

Scope vocabulary v1 (deliberately tiny):

| scope | grants |
| --- | --- |
| `inference` | chat/completions, messages, responses, embeddings via the attested gateway; `/v1/models`, `/v1/generation` (own requests) |
| `profile` | `/v1/oauth/userinfo`: user id (app-scoped surrogate), email, `verification_level` |
| `balance:read` | `/v1/credits/summary`: remaining prepaid credits only (single number + currency), NOT the management `/v1/credits` payload |
| `activity:read` | `/v1/activity` filtered to generations attributed to THIS app |

Default consent request: `inference profile`. Anything else must be asked
for explicitly in the authorize URL and is listed on the consent page.
No management scopes exist at all in v1 — key creation, BYOK, broadcast,
checkout stay session/management-key only.

### Verification level: expose, don't rebuild

`verification_level(user) -> "none" | "email" | "phone" | "identity"`,
computed (not stored) as the highest of: `email_verified`,
`phone_verified`, `identity_status == "approved"`. Exposed via the
`profile` scope in the userinfo response and in the token-exchange
response. The ladder is ordered; apps can gate on `>=`.

### App registry: first-class, minimal

`OAuthApp` entity: `id` (slug), `owner_user_id`, `name`, `redirect_uris`
(exact-match allowlist), `logo_url?`, `markup_basis_points` (0..30_000,
i.e. 0–300%), `created_at`, `suspended`. Managed by session auth in the
console. **Registering an app requires the owner to hold FULL identity
verification (`verification_level == "identity"`, Veriff-approved)** —
decided by Joseph 2026-08-25: apps act on other users' wallets and mint
delegated keys, so the accountability floor is a verified legal
identity, not just a phone. The registration/edit endpoints fail closed
(403 naming the requirement) for anyone below it; the Veriff flow the
Custom Models program ships is the path to satisfy it. Suspending an
app (operator or owner) immediately stops NEW consent grants and new
gateway authorizations through its delegated keys; requests already
authorized settle under their frozen terms (markup charged, payout
credited) — the money path never reads app status at settle, and the
abuse window is bounded by in-flight requests only. Revoking payouts for
settled abuse is an operator ledger action, not a settle-path branch.

The authorize URL gains `client_id` (the app id). The consent page shows
the app name, the requested scopes, and — when markup > 0 — a plain
sentence: "This app adds N% on top of TrustedRouter token costs."
Informed consent is the product; hiding the markup would poison the
whole economy.

### Markup money flow: mirror Custom Models exactly

- **Authorize** (hold): estimate becomes
  `base_estimate + base_estimate * markup_bps // 10_000` so the hold
  covers the marked-up total. Authorization stamps `app_id`,
  `app_markup_basis_points`, `app_owner_user_id` (frozen at authorize —
  a later markup change never reprices in-flight requests).
- **Settle**: `markup_micro = base_cost * markup_bps // 10_000`;
  user is charged `base_cost + markup_micro` (this is `actual_cost` in
  typed billing — flows through tr_credit_balance/tr_key_limit exactly
  like any charge). Owner payout =
  `markup_micro * 7000 // 10_000` credited via `credit_user_earnings`
  with event id `app_markup_payout_event_id(authorization.id)` —
  idempotent, best-effort (never fails settle), same
  `_credit_user_model_payout_safely` shape. TR's 30% is the remainder:
  it is simply revenue that was charged and not paid out (floor division
  rounds in TR's favor, matching Custom Models).
- Refunds/failures: no payout (payout only on successful settle, like
  Custom Models). BYOK requests: markup applies to the TR-billed
  portion only; if base cost is 0, markup is 0 — no minimum-fee
  invention in v1.

Implementation facts (verified against main 62a7ae16):
- Estimate insertion point is ONE line: `gateway.py:458`
  (`estimate = model_estimate + additional_cost_reservation`); settle
  side after `:1863`, BEFORE the cap/overrun checks at `:1876`/`:1946`/
  `:1962` — both sides must mark up consistently or every marked-up
  request trips `billing.settlement_exceeded_reservation` and the
  regional-lease cap claws the markup back.
- Settle takes an arbitrary `actual_micro` end to end
  (`release_credit` books `total_usage + @actual` with the hold
  invariant on `reserved >= @hold`), and the settle-outbox drain replays
  `actual_cost_micro` verbatim — a marked-up charge survives repair
  with no extra work.
- Payout uses the STRONGEST existing tier: the in-transaction
  `_apply_user_model_payout_tx` shape (idempotent via
  `INSERT OR IGNORE INTO tr_credit_movement`, PK
  `(account_id, movement_id)`), with movement `kind="app_markup_payout"`
  (kind is STRING(40) free-form — no DDL) and event id
  `app_markup_payout:{authorization_id}`. A sibling of
  `scripts/reconcile_custom_model_payouts.py` covers missed payouts.
- Settle never re-reads the ApiKey — freezing
  `app_id`/`app_markup_basis_points`/`app_owner_user_id` on the
  authorization at authorize is mandatory, not stylistic (kwargs beside
  the `user_model_*` freeze at `gateway.py:670-707` / `:842-890`).
- `_SETTLE_REPAIR_FIELDS` is an allowlist filter over the frozen outbox
  body: markup fields must be injected AFTER `_settle_repair_metadata`,
  copying the `USER_MODEL_PAYOUT_SETTLE_FIELD` ordering at
  `gateway.py:2032-2040`.
- New dataclass fields need NO Spanner DDL (`tr_gateway_authorization` /
  `tr_generation` store JSON payloads). ClickHouse analytics gains an
  `app_id` column as a follow-up, not in the money increment.
- The non-gateway local inference path (`routes/inference.py:795`) pays
  custom-model owners too, but production registers no control-plane
  inference routes (security boundary): **app markup applies on the
  attested gateway path ONLY**, stated in docs.
- A vestigial `OAuthAuthorizationCode.app_id: int`
  (sha256(callback_url) prefix, dropped at key mint) exists today; the
  registry replaces it with the real app slug, and the mint path stops
  dropping it.

### app_id to the ledger

`ApiKey.app_id` (set when minted via OAuth exchange) → stamped onto the
gateway authorization at authorize → carried into `Generation`
(`from_settle_body`) and the activity surface; `to_openrouter_generation`
stops hardcoding `app_id: None` (storage_models.py:970). Analytics
events pick it up for free once it is on Generation.

### User control: authorized apps and budgets

Users must be able to SEE every app they have authorized and control its
spend, in one place (Joseph, 2026-08-25). Enforcement free-rides on
machinery that already exists — per-key spend-window limits
(daily/weekly/monthly, shipped 2026-07) and the exact lifetime cap
(`limit_microdollars`) — because each app grant mints its own delegated
key. This increment is plumbing and surface, not new billing code:

- **Consent-time budget**: the consent page includes a monthly budget
  selector — presets ($5 / $20 / $100 / no limit), default **$20/month
  preselected** — written as a monthly window limit on the minted key.
  With markup in the picture, an unlimited default would be
  user-hostile; a visible, editable default is the honest middle. The
  app can SUGGEST a budget via an optional `suggested_monthly_budget`
  authorize parameter (shown, never silently applied above the default).
- **Authorized-apps management** (session auth):
  `GET /v1/oauth/authorized-apps` — the user's delegated keys grouped by
  app: app name + logo, scopes, granted date, this-month spend (from the
  key's window usage), budget, markup % at grant time.
  `PATCH .../{key_hash}/budget` — change the monthly window limit /
  lifetime cap. `DELETE .../{key_hash}` — revoke the grant (deletes the
  delegated key; the app's access ends immediately).
  Console page "Authorized apps" renders exactly this, with
  change-budget and revoke as one-click actions.
- Budget exhaustion returns the existing spend-window 402/429 shape the
  key-window feature already defines; apps see it like any spend cap.

### Conformant OAuth surface

Keep the existing endpoints working. Add/normalize:
- `GET /oauth/authorize` accepting standard `response_type=code`,
  `client_id`, `redirect_uri`, `scope`, `state`,
  `code_challenge(+_method=S256)`;
- `POST /oauth/token` (`application/x-www-form-urlencoded`,
  `grant_type=authorization_code`, `code`, `code_verifier`,
  `client_id`, `redirect_uri`) returning
  `{access_token, token_type: "bearer", scope, trustedrouter: {...verification_level, app_id...}}`
  — the access token IS the delegated TR API key (opaque token; that is
  conformant), no refresh tokens in v1 (keys are long-lived; revocation
  via console; `expires_in` omitted);
- RFC 6749 error envelope (`{"error": "invalid_grant", ...}`) on the
  token endpoint.
The old `/auth/keys` exchange stays as an alias until SDKs migrate.

### Security hardening pulled INTO scope (explorer findings, 2026-08-25)

The current flow has holes beyond the hand-off's four gaps; each is
assigned to an increment rather than left floating:

- **No redirect-URI allowlisting exists** — `_validate_callback_url` is
  a shape check; ANY https host passes, and there is no registered-app
  record to match against. The registry's exact-match `redirect_uris`
  (B) is therefore a security fix, not just plumbing. The conformant
  endpoints (D) additionally echo-check `redirect_uri` at token
  exchange per RFC 6749 §4.1.3.
- **PKCE is optional for https/loopback callbacks and `plain` is
  accepted.** The new `/oauth/authorize` (D) requires S256 for every
  public client per OAuth 2.1; the legacy `/auth` alias keeps its
  current behavior until sunset, stated in docs.
- **`/auth/approve` has no CSRF token** and trusts the POSTed
  `callback_url`. D adds a per-consent CSRF token and re-derives the
  authorization parameters from a server-side consent record instead of
  trusting hidden form fields; the Stripe top-up round-trip stops
  carrying `code_challenge` through checkout URLs for the same reason.
- **No `state` support** and a non-standard `user_id` appended to the
  redirect. D: `state` round-trips verbatim; new flow appends only
  `code` (+`state`).
- **Token endpoint nonconformance** (JSON body, missing `grant_type` /
  `client_id` / `redirect_uri`, 403-instead-of-400 `invalid_grant`,
  house error envelope, no `Cache-Control: no-store`): all D, on the
  new endpoint only.
- **Two divergent identity payloads** (exchange vs `/auth/userinfo`):
  unified in A behind `profile` scope with `verification_level`.
- **Re-grant proliferation**: today re-authorizing mints an additional
  permanent key, unfindable per-app. With `app_id` on keys (B), a new
  grant for the same (app, user, workspace) **revokes and replaces**
  the previous delegated key by default (E) — one live key per grant
  triple, which is also what makes the authorized-apps page truthful.
- **The unauthenticated token endpoint** is covered only by ingress IP
  limits; D adds a dedicated per-IP bucket for `/oauth/token` +
  `/auth/keys` at a tight limit (code exchange is rare and cheap).
- `/auth/keys/code` (headless management-auth code mint,
  OpenRouter-compat) stays, but is documented as outside the OAuth
  conformance surface.

### Gaps deliberately NOT closed in v1

- No refresh-token rotation (delegated keys are revocable, long-lived).
- No dynamic client registration (console-only app registration).
- No incremental consent UI (re-authorize with more scopes replaces the
  key's scopes only via a fresh grant).
- Phone/ID verification flows themselves: already exist (SMS/voice
  code, Veriff) — this project only exposes their state.

## Increments

- **A. Scopes + enforcement + verification level + userinfo + balance
  summary.** No money changes. Closes gaps (a), (c), and item 1.
- **B. App registry + `client_id` in the flow + consent page + app_id
  threading to authorization/Generation/activity.** Closes gap (d).
- **C. Markup at authorize/settle + earnings payout + console earnings
  view line items.** Item 2. Money-path change: full codex-review-until-
  clean treatment, golden settle tests, mutation-verified.
- **D. Conformant `/oauth/authorize` + `/oauth/token` + consent page
  (scopes + markup disclosure + budget selector) + docs page + hand-off
  doc update.** Closes gap (b).
- **E. Authorized-apps management: `GET /v1/oauth/authorized-apps`,
  budget PATCH, revoke DELETE, console page.** Depends on B (app
  attribution); enforcement is the existing key-window machinery.

Each increment: spec → codex implements → mutation-verify → adversarial
review until a clean round → merge on green CI.
