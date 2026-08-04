# Sign in with TrustedRouter

"Sign in with TrustedRouter" lets a third-party app authenticate a user with
their TrustedRouter account and receive a **user-scoped API key** — so LLM
calls the app makes are billed to *that user's* TrustedRouter credits, not the
app's. It's the OpenRouter-style OAuth **PKCE** flow, plus an identity layer so
the app also learns *who* signed in (email/profile).

This is the recommended way for an app to let users "bring their own
TrustedRouter account."

## At a glance

```
app (public client)                         TrustedRouter
  │  1. make PKCE pair (verifier, challenge)
  │  2. open browser → GET /auth?callback_url=…&code_challenge=…
  │ ───────────────────────────────────────────────────────────▶ consent
  │                              (sign in, fund, choose cap, approve)
  │  3. ◀── redirect: callback_url?code=…&user_id=…&state=…
  │  4. POST /auth/keys {code, code_verifier}  (no auth header)
  │ ───────────────────────────────────────────────────────────▶
  │  5. ◀── { key: "sk-tr-v1-…", user_id, identity:{sub,email,…}, data }
  │  6. (optional) GET /auth/userinfo  Authorization: Bearer <key>
  │ ───────────────────────────────────────────────────────────▶
  │  7. ◀── { data:{ sub, email, email_verified, wallet_address, … } }
  ▼
use `key` for /v1/chat/completions etc. — billed to the signed-in user.
```

Because of PKCE, intercepting the redirect `code` is useless without the
`code_verifier` the app kept, so native and SPA apps need **no client secret**.

## Account creation, funding, and the app limit

The browser flow handles first-time TrustedRouter users without sending them
through the general console onboarding:

1. The user signs in with Google, GitHub, or MetaMask.
2. A new account created from this delegated flow starts with **$0 in credit**.
   It does not mint an extra management API key.
3. The consent screen offers **$5**, **$20** (selected by default), or **$100**
   in credits. Stripe saves the card to the user's TrustedRouter account for
   faster future top ups and optional auto refill. The user can remove it from
   the Credits page.
4. The user reviews and edits the maximum spend for this app. If the app
   requested `limit=5&usage_limit_type=monthly`, the form starts at $5 per
   month, but the user remains in control.
5. Approval creates one inference-only key. The app cannot manage billing,
   workspaces, provider keys, or other API keys.

Existing users see their current available balance. Funding is optional when
that balance is already sufficient. A user may also approve at $0 when they
intend to use BYOK, but prepaid model calls will fail until credits arrive.

## Endpoints

OAuth control base: `https://trustedrouter.com/v1`.

Inference base for the delegated key: `https://api.quillrouter.com/v1`
(also available as `https://api.trustedrouter.com/v1`). The browser sign-in,
consent, billing, and code exchange stay on the control plane. Prompt traffic
uses the attested API plane.

### `GET /auth` — authorize + consent (browser)
Query params (only `callback_url` is required):

| param | meaning |
|---|---|
| `callback_url` | where TR redirects after approval. Use `https://`, a native custom scheme with PKCE S256, or `http://localhost:3000` / `127.0.0.1:3000` for a loopback client. Web ports are limited to 443 or 3000. Carry your CSRF `state` *inside* this URL's query. |
| `code_challenge` | base64url(SHA-256(`code_verifier`)), padding stripped |
| `code_challenge_method` | `S256` (default) or `plain` |
| `key_label` | label shown on the issued key (defaults to the callback host) |
| `limit` | optional spend cap in dollars for the issued key |
| `usage_limit_type` | `daily` \| `weekly` \| `monthly` (resets the cap) |
| `expires_at` | optional ISO-8601 expiry for the issued key |

If the user isn't signed in, `/auth` serves a sign-in page. After sign-in they
can add credits, edit the app limit, and approve. TrustedRouter then redirects
to `callback_url?code=…&user_id=…` plus your embedded `state`.

### `POST /auth/keys` — exchange code → key (+ identity)
Public client — **send no `Authorization` header.**
```json
{ "code": "auth_code-…", "code_verifier": "…", "code_challenge_method": "S256" }
```
Returns:
```json
{
  "key": "sk-tr-v1-…",
  "user_id": "usr_…",
  "identity": { "sub": "usr_…", "email": "you@example.com",
                "email_verified": true, "wallet_address": null },
  "data": { /* key metadata */ }
}
```
One-time: a code can be exchanged once and expires after ~10 minutes.

### `GET /auth/userinfo` — who is this key's user
`Authorization: Bearer <the issued key>` (works with a delegated key or a
console session).
```json
{ "data": { "sub": "usr_…", "email": "you@example.com",
            "email_verified": true, "wallet_address": null,
            "workspace_id": "ws_…", "created_at": "…" } }
```

## SDK usage

All three official TrustedRouter SDKs have first-class Sign in with
TrustedRouter support. You do not need to implement PKCE, state validation, URL
construction, or token exchange from scratch:

- [Python SDK](https://github.com/Lore-Hex/trusted-router-py):
  `create_oauth_authorization`, `exchange_oauth_key`, and `fetch_userinfo`
- [TypeScript SDK](https://github.com/Lore-Hex/trusted-router-js):
  `BrowserOAuthFlow` and the lower-level OAuth client methods
- [Swift SDK](https://github.com/jperla/trusted-router-swift):
  `TrustedRouterOAuth`, `oauthAuthorizeURL`, and `exchangeOAuthKey`

Each SDK implements the same wire protocol and returns the delegated key plus
the signed-in TrustedRouter identity.

### Python (`trustedrouter`)
```python
from trustedrouter import create_oauth_authorization, exchange_oauth_key, fetch_userinfo

# 1. before redirect — keep auth.code_verifier + auth.state in the user's session
auth = create_oauth_authorization(
    callback_url="https://myapp.com/auth/callback",
    key_label="My App", limit="5", usage_limit_type="monthly",
)
redirect_to(auth.url)

# 2. in your /auth/callback handler (validate state == saved state first)
token = exchange_oauth_key(code=request.args["code"], code_verifier=saved_verifier)
store_for_user(token.key, token.identity)          # token.identity = {sub,email,…}

# 3. anytime
who = fetch_userinfo(api_key=token.key)            # {sub, email, …}
```
Async variants: `exchange_oauth_key_async`, `fetch_userinfo_async`.

### JavaScript (`@lore-hex/trusted-router`)
Browser SPA — the `./oauth` convenience flow handles `sessionStorage` + state:
```js
import { BrowserOAuthFlow } from "@lore-hex/trusted-router/oauth";

const flow = new BrowserOAuthFlow(`${location.origin}/auth/callback`);
// sign-in button:
const { url } = await flow.initiate({ keyLabel: "My App", limit: "5" });
location.assign(url);
// /auth/callback:
const { key, user_id, identity } = await flow.handleCallback();   // validates state

// later:
const { data } = await new TrustedRouter({ apiKey: key }).userInfo();
```
Lower-level (`createOAuthAuthorization` / `exchangeOAuthKey`) is also exported.

### Swift (`TrustedRouter`)
Native iOS/macOS — `ASWebAuthenticationSession`:
```swift
import TrustedRouter
let oauth = TrustedRouterOAuth()
let token = try await oauth.authenticate(
    callbackURL: "myapp://auth/callback",
    presentationContextProvider: self)            // token.key, token.identity
let who = try await fetchUserInfo(apiKey: token.key)
```
The pure pieces (`PKCEChallenge`, `oauthAuthorizeURL`, `exchangeOAuthKey`,
`fetchUserInfo`) build on every platform **including Linux**, for cross-platform
GUI apps (e.g. Lore games on QuillUI) that can't use `ASWebAuthenticationSession`.

### Desktop / cross-platform (loopback redirect)
GUI toolkits that aren't native AppKit/UIKit (QuillUI/GTK, Electron, CLIs) use a
**loopback** flow: set `callback_url=http://localhost:3000/callback`, open the
system browser to the authorize URL, run a tiny local HTTP server on port 3000
to catch the `?code=…`, then exchange it. (Port 3000 is allowlisted by the
backend for exactly this.)

## Consumers
- Third-party web, native, desktop, and CLI applications use the same flow to
  request a user-approved inference key without handling provider credentials.
- **Lore web** signs in with this flow and uses the delegated key for game
  generation.
- **Lore games (macOS, QuillUI)** uses the Swift SDK loopback flow.

## Security notes
- PKCE `S256` is required for native custom-scheme callbacks and recommended
  for every public client. The `plain` method exists only for wire
  compatibility with older clients. Never send the `code_verifier` in the
  authorize URL, only in the exchange.
- Always generate a random `state`, embed it in `callback_url`, and verify it on
  the callback (the SDK flow helpers do this).
- `callback_url` must be HTTPS, a PKCE-protected native custom scheme, or the
  supported loopback URL; credentials and unsafe schemes are rejected.
- Issued keys are inference-only (never management) and honor the optional
  `limit` / `usage_limit_type` / `expires_at` the user approves. Request a
  conservative default and let the user make the final choice.
- Never put the delegated key in a URL, browser analytics, logs, or client-side
  durable storage. A server-backed app should exchange and store it server-side.
