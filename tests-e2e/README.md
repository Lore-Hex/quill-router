# /chat Playwright suite

End-to-end tests for the trustedrouter.com/chat playground.
Validates the contract that the rest of the codebase can only
test the page render of — actual streaming, sign-in gating,
multi-model parallel send, picker keyboard nav, localStorage
persistence, the lot.

## Layout

```
tests-e2e/
├── playwright.config.ts    — Browser engines, webServer, fixtures wired here
├── fixtures/               — Shared helpers (sign-in, mocks, SSE generators)
│   ├── api-mock.ts         — page.route() interceptors for /v1/models, etc.
│   ├── sign-in.ts          — Plant a tr_session cookie via the test harness
│   ├── sse.ts              — Build standard OpenAI delta protocol streams
│   └── helpers.ts          — Wait-for-stream, get localStorage state, etc.
└── specs/                  — One file per surface
    ├── anonymous.spec.ts                  — No tokens fire without sign-in
    ├── single-model.spec.ts               — Sign in + Send + stream
    ├── multi-model.spec.ts                — Add + parallel-stream
    ├── model-picker.spec.ts               — Search, groups, filters, recent
    ├── sidebar.spec.ts                    — New / pin / delete / search / rename
    ├── persistence.spec.ts                — localStorage survives reload
    ├── per-message.spec.ts                — Copy / Edit / Regenerate / Branch
    ├── shortcuts.spec.ts                  — All keyboard shortcuts
    ├── streaming.spec.ts                  — Caret / dots / cost ticker
    ├── search.spec.ts                     — In-chat ⌘F
    ├── mobile.spec.ts                     — Mobile chrome + sidebar drawer
    ├── settings.spec.ts                   — Settings overlay
    ├── export-share.spec.ts               — JSON/MD download, URL share
    ├── stop.spec.ts                       — Send→Stop, Esc to abort
    ├── welcome-suggestions.spec.ts        — First-visit card, suggested prompts
    ├── code-blocks.spec.ts                — lang label, copy
    ├── reasoning-tools.spec.ts            — Thinking / tool_calls
    ├── attachments.spec.ts                — Image upload + preview
    └── system-prompt-params.spec.ts       — Toggle, sliders, presets
```

## Running locally

One-time setup:

```bash
cd tests-e2e
npm install
npm run install:browsers   # downloads Chromium / WebKit / Firefox
```

Run the full suite:

```bash
npm test                    # all browsers
npm test -- --project=chromium
npm run test:headed         # see the browser windows
npm run test:ui             # interactive UI mode
```

The TR server starts automatically per `playwright.config.ts`'s
`webServer` block — no need to run `uvicorn` manually. The
`memory` storage backend means no GCP/AWS auth is needed.

## What's NOT tested here

- Actual provider inference — all `/v1/chat/completions` calls are
  intercepted via `page.route()` and replied to with synthetic
  streams from `fixtures/sse.ts`. The chat playground is a CLIENT
  of the inference API; testing the API itself belongs in
  `tests/test_inference.py`.
- Real OAuth — `fixtures/sign-in.ts` plants the session cookie
  directly. End-to-end OAuth (Google / GitHub / MetaMask) is
  covered by `tests/test_oauth_and_console.py` against the
  upstream-mocked test client.
- Real Stripe checkout / webhook — separate suite.
