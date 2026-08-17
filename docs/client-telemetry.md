# Client-observed reliability telemetry — the cross-repo contract

**Status:** contract v1 (2026-08-17). Implemented in this order: enclave request ids → control-plane settle
context → this repo's ClickHouse tables/ingester → enclave header channel → beacon ingest → Python SDK →
surfaces. Other SDKs implement from THIS document; each SDK PR states the contract version it implements.
The executable source of the enums is `src/trusted_router/client_context.py` (header/settle vocabulary) and
`src/trusted_router/client_events_schema.py` (beacon schema). If this document and those modules disagree,
the modules win and this document has a bug — fix the document.

## 1. Why this exists

Every reliability number TrustedRouter publishes is measured from the inside: synthetic probes and the
enclave's own `elapsed_seconds` / `first_token_seconds`. A request that dies in DNS, TLS, or a connect
timeout — or a stream that stalls after the enclave already settled — leaves **no record anywhere**: settle
exists only on success, refund only for failures the enclave *saw*. Those invisible failures are what a
customer means by "you were down". This contract lets clients report what they observed, so we can
(a) verify our uptime claims from the customer's side and (b) improve.

Prior art, checked from installed source: OpenAI and Anthropic SDKs send client telemetry **only as
request headers** on the call the user already made (`X-Stainless-Lang/-Package-Version/-OS/-Arch/-Runtime`,
per-request `x-stainless-retry-count`, timeouts; Anthropic also a closed `x-stainless-helper` vocabulary).
No phone-home, no OTel, no client-side TTFT. Both read a request id off every response (`x-request-id`
for OpenAI, `request-id` for Anthropic). Our design keeps that header channel (and captures the vendor
headers our enclave was discarding) and adds a beacon channel, because a header can never describe a
request that never arrived.

## 2. Principles (non-negotiable)

1. **Content-free by construction.** Every string on the wire is a closed enum or an anchored regex with a
   hard length. `extra="forbid"` at every level. There is **no free-text field** anywhere in the beacon.
   The trust page publishes cosign-signed binary claims (`prompt_output_storage:false`,
   `control_plane_prompt_access:false`); nothing here may weaken them.
2. **Never on the money path.** Telemetry never fails a request. The enclave drops invalid headers with a
   stderr line, never a 4xx. The control plane soft-validates settle extras, never 400s. The beacon is
   fire-and-forget outside the SDK retry engine, bounded, single-shot per flush, kill-switchable.
   Client context is **never** sent on `/internal/gateway/authorize` (its idempotency fingerprint hashes every
   body key; a per-attempt object would 409 SDK retries).
3. **Honest numbers.** Availability is computed only from exact per-minute counters at logical-request
   granularity — never from sampled records. The TR-fault classification is versioned in one module.
   Exclusions are disclosed. `client_observed` has its own id and is never mixed into `router_core`.
4. **Efficient.** Header ≈ 14 B on a first attempt, ≤ 160 B on retries; response ids ≈ 70 B; beacon O(1)
   POSTs per process-minute regardless of RPS; one Spanner transaction per POST.

## 3. Header channel (every client, zero extra requests)

### 3.1 What the enclave reads (`enclave-go/cmd/enclave/http_io.go` `readRequest`)
All optional; invalid ⇒ field dropped + `enclave.client_context_dropped reason=<enum> request_log_id=…`
on stderr; never a 4xx.

| header | who sends it | accepted | normalised field |
|---|---|---|---|
| `user-agent` (≤256 B read, never stored raw) | everyone | `trusted-router-(py\|js\|go\|rust\|java\|swift)/SEMVER( runtime/ver)?` or `(OpenAI\|Anthropic)/(Python\|JS\|Go\|Java\|…) SEMVER` | `sdk`, `sdk_version`, `runtime` |
| `x-stainless-lang / -runtime / -runtime-version / -os / -arch` | vendor SDKs (already sent) | allowlists → enums else `other` | `lang`, `runtime`, `os`, `arch` |
| `x-stainless-retry-count` | vendor SDKs (already sent) | int 0..99 | `attempt` |
| `x-stainless-timeout` / `x-stainless-read-timeout` | vendor SDKs | float seconds → int ms 1..3 600 000 | `timeout_ms` |
| `x-tr-client` | TrustedRouter SDKs, **every attempt** | grammar §3.2, ≤160 B | attempt, prev_*, since_first_ms, stream, failover_used |

`source` = `tr` if `x-tr-client` parsed, else `stainless` if any x-stainless-* parsed, else `none`.

### 3.2 `x-tr-client` v1 grammar (TrustedRouter SDKs)
```
x-tr-client = "v=1" *( ";" key "=" value )       ; keys unique; unknown key or bad value ⇒ whole header dropped
value = [a-z0-9_]{1,24}                          ; total header ≤ 160 bytes
a  = attempt index 0..99 (0 = first attempt; same semantics as x-stainless-retry-count)
po = previous attempt outcome: none | http_error | transport_error | timeout | stream_broken
pc = previous error class (ErrorClass, §5.3) or none
ph = previous host: apex | ally | uptime | us_central1 | us_east4 | europe_west4 | control | custom | none
pm = previous attempt elapsed ms 0..3600000
sm = ms since the first attempt started 0..3600000
s  = 0|1 streaming
fo = 0|1 candidate index advanced at least once during this logical request
```
- First attempt: `v=1;a=0;s=1`. Retry after a connect timeout on the apex that moved to an alias:
  `v=1;a=1;po=transport_error;pc=connect_timeout;ph=apex;pm=10012;sm=10530;s=1;fo=1`.
- Static identity (SDK name/version/runtime/OS) rides the existing `User-Agent`, not this header.
- **Not sent to custom base URLs** (a self-hosted gateway is not TrustedRouter's to measure) and **not sent
  on control-plane calls**. Not sent when telemetry is opted out (§6.4).

### 3.3 Response correlation (enclave → every client)
Every response — success, error, stream head, 413/431, `/health` — carries **both**
`x-request-id: rlog_<32hex>` and `request-id: rlog_<32hex>` (OpenAI SDKs read the first, Anthropic SDKs the
second). The value is the enclave's per-request audit id (`enclave.request_start/request_end` lines). SDKs
should read `x-request-id` and put it in `ClientAttempt.request_id` (§5.4). The enclave forwards the same id
to the control plane on settle and refund as `gateway_request_id`, which lands on `activity_generations` —
that is the client↔server join key. Do not ship idempotency keys in telemetry (they are the exactly-once
settlement token and are not joinable in ClickHouse).

### 3.4 What the enclave forwards (settle and refund bodies only)
```json
"gateway_request_id": "rlog_…",
"client": {"v":1,"source":"tr","sdk":"tr-py","sdk_version":"0.6.0","lang":"python","runtime":"cpython/3.12.1",
           "os":"macos","arch":"arm64","timeout_ms":120000,"attempt":1,"prev_outcome":"transport_error",
           "prev_error_class":"connect_timeout","prev_host":"apex","prev_elapsed_ms":10012,"since_first_ms":10530,
           "stream":true,"failover_used":true}
```
Control plane: `GatewayClientContext(_Strict)` soft-validates (drop + warning, never 4xx), stores on
`Generation` → `activity_generations` `client_*` columns; keeps `client` OUT of customer broadcast bodies;
refund path logs one bounded line. Canary traffic is detected server-side (`metadata.trustedrouter_synthetic`,
app `TrustedRouter Synthetic`, monitor workspace) — no client honesty required.

## 4. Beacon channel (TrustedRouter SDKs; Python first)

`POST https://trustedrouter.com/v1/client-events` (also `/client-events`). Control plane, i.e. a *different
deployment* from the inference plane — an inference-plane outage is reported in near real time. If the
control plane itself is down, the SDK keeps counting locally (24 h of minute counters, byte-capped) and drains
the backlog on recovery; the server assigns time from ages, so late data lands in the right minutes.

- Auth: `Authorization: Bearer sk-tr-…` (an ordinary inference key; `InferencePrincipal`).
- Body ≤ 65 536 bytes (413 above), ≤100 `events`, ≤200 `counters`; JSON per §5.
- Response `202`:
  `{"data":{"accepted_events":n,"accepted_counters":m,"dropped":k},"policy":{"success_sample_rate":0.01,"flush_seconds":30,"pause_seconds":0}}`.
  The SDK applies `policy` **only when it reduces volume** (lower rate, longer flush, pause ∈ [0, 86400]).
- Errors: 400 schema (drop the batch, keep going), 401/403/404/410 (disable telemetry for the process),
  413 (drop the batch; the SDK's caps were violated — a bug), 429 (+Retry-After: back off), 503 (+Retry-After:
  back off). Kill switch: `pause_seconds` in a 202, or `x-tr-telemetry: off` (disable for the process).
- Never retry a flush; never send from inside the retry engine; never block a user request; bounded memory.
- No CORS in v1 (server-side SDKs only). Browser JS is a later, separate decision.

## 5. Beacon schema v1 (mirrors `src/trusted_router/client_events_schema.py`)

### 5.1 Batch
```
ClientEventsBatch:
  schema_version: 1
  batch_id:       ^[0-9a-f]{32}$          # SDK-minted per POST
  instance_id:    ^[0-9a-f]{16}$          # SDK-minted per process
  seq:            int ≥ 0                 # per-process POST counter (gap = server-side loss detection)
  sdk:            {name: tr-py|tr-js|tr-go|tr-rust|tr-java|tr-swift; version: SEMVER ≤32;
                   lang: python|js|go|rust|java|swift; runtime: ^[a-z]{1,10}/[0-9A-Za-z.+-]{1,24}$;
                   os: linux|macos|windows|ios|android|freebsd|other; arch: x64|x32|arm|arm64|wasm|other}
  synthetic:      bool                    # true iff the requests carried metadata.trustedrouter_synthetic
  dropped_since_last: int ≥ 0             # events/counters the SDK dropped since the previous POST
  events:   [ClientRequestEvent] ≤100
  counters: [ClientMinuteCounter] ≤200    # at least one of events/counters non-empty
```
Nothing else. No tenant/workspace/key/user/session ids (identity comes from the principal), no IPs, no
hostnames (hosts are an enum), no idempotency keys, no message text, no timestamps (durations/ages only —
the server assigns wall time).

### 5.2 Enums (closed)
```
Host       = apex | ally | uptime | us_central1 | us_east4 | europe_west4 | control | custom
Endpoint   = chat_completions | messages | responses | embeddings | images | videos | models | fusion
           | control_other | inference_other
Outcome    = ok | http_error | transport_error | timeout | stream_broken | aborted
FinalOutcome = Outcome | exhausted
ErrorClass = dns | tls | connect_refused | connect_timeout | connect_error | read_timeout | write_timeout
           | pool_timeout | protocol_error | reset | io_error | proxy_error | stream_stalled | unknown
ErrorSource = router | provider | unknown        # from the error body's "source" field
TimeoutPhase = none | connect | first_byte | idle | total
HttpStatusClass = none | 2xx | 4xx | 429 | 5xx
LatencyBucket = lt100 | lt200 | lt400 | lt800 | lt1600 | lt3200 | lt6400 | lt12800 | lt25600 | lt51200
              | lt102400 | ge102400                # ms; upper-bound-exclusive
```
Host mapping (SDK-side): `api.trustedrouter.com`→apex, `api.allyrouter.com`→ally, `api.uptimerouter.com`→uptime,
`api-<region>.quillrouter.com`→region enum, `trustedrouter.com`→control, anything else→custom.

### 5.3 Per-request event (sampled diagnostics)
```
ClientRequestEvent:
  age_ms:            0..86 400 000        # request completion → flush; server: created_at = received_at − age_ms
  plane:             inference | control
  endpoint:          Endpoint
  method:            GET | POST | PUT | PATCH | DELETE
  streaming:         bool
  provider_pinned:   bool                 # request pinned a provider/models list
  model:             ^[A-Za-z0-9._:/~@-]{1,128}$ | null   # server maps to catalog id or "other"
  attempts:          [ClientAttempt] 1..16
  final_outcome:     FinalOutcome
  final_http_status: 100..599 | null
  total_ms:          0..3 600 000
  ttft_ms:           0..3 600 000 | null  # first SSE event / first body byte for streams
  failover_used:     bool
  timeout_phase:     TimeoutPhase
  configured_timeout_ms: 1..3 600 000 | null
  sample_rate:       (0, 1]
  sample_reason:     failure | retried | slow | random
ClientAttempt:
  index:        0..99
  host:         Host
  outcome:      Outcome
  http_status:  100..599 | null
  error_class:  ErrorClass | null
  error_source: ErrorSource | null
  should_retry: true | false | absent     # x-should-retry as observed
  retry_after_ms: 0..3 600 000 | null
  elapsed_ms:   0..3 600 000
  ttfb_ms:      0..3 600 000 | null       # headers received
  request_id:   ^rlog_[0-9a-f]{32}$ | null   # from x-request-id
  moved:        bool                      # candidate index advanced after this attempt
```
Sampling: 100 % of events with `final_outcome ≠ ok`; 100 % of ok events with `len(attempts) > 1` or
`failover_used`; 100 % of ok events with `total_ms > 30 000`; `success_sample_rate` (default 0.01) of the rest.

### 5.4 Per-minute counters (exact; the only source of availability numbers)
```
ClientMinuteCounter:
  window_start_age_ms: 0..86 400 000     # start of the client minute, as an offset before the flush
  level:        attempt | request         # attempt-level: one row per (host, …) tried; request-level: final outcome
  endpoint:     Endpoint
  streaming:    bool
  host:         Host                      # attempt: host tried; request: final host
  outcome:      FinalOutcome
  error_class:  ErrorClass | null
  http_status_class: HttpStatusClass
  timeout_phase: TimeoutPhase
  timeout_floor_met: bool                 # configured connect ≥10 s / first-byte ≥60 s / idle ≥30 s
  provider_pinned: bool
  requests:     1..10 000 000
  attempts:     ≥0
  failover_used: ≥0
  first_attempt_success: ≥0
  total_ms_hist:      {LatencyBucket: count}
  first_event_ms_hist: {LatencyBucket: count}
```
The counter key is exactly the fields above minus the counts and histograms (`model` is deliberately not
part of it). A process keeps ≤256 keys per minute window; when a new key would exceed the cap it is folded —
first `error_class → unknown`, then `endpoint → inference_other` — so the counts are still exact, only
coarser. `dropped_since_last` is never incremented by folding.

## 6. SDK implementation guide

### 6.1 Where the facts live (one emit point per SDK; do not add a second)
| SDK | policy kernel | single engine loop (emit here) | header assembly (add `x-tr-client` here) | out-of-engine single-shot precedent (attach the beacon sender here) |
|---|---|---|---|---|
| py | `_retry.py` `RetryController` | `_transport.py` four drivers | `_client_sync.py` buffered headers AND `_requests.py` `_build_stream_request` (two sites!) | raw `self._client.get` in `_client_sync.py` (status/attestation/trust) |
| js | `internal/transport.js` kernel fns | `performRequest` (`transport.js`) | `buildHeaders` (`transport.js`) | `internal/trust.js fetchTrustRelease` |
| go | `retry_policy.go` | `transport.go do()` | `newHTTPRequest` (`transport.go`) | `transport.go absoluteRequest` |
| rust | `transport/policy.rs` | `transport/engine.rs Client::execute` | `transport/headers.rs request_headers` | `transport/mod.rs credential_free_json` |
| java | `internal/RetryPolicy.java` (`AttemptFacts`) | `internal/Transport.java executeUrls` | `internal/RequestFactory.java buildRequest` | **none — add a bypass** (`executeAbsolute` rides the loop) |
| swift | `Transport/RetryPolicy.swift` | `Transport/TransportEngine.swift withTransportRetries` | `Core/TrustedRouterClient.swift buildHeaders` | `rawRequest` (single-shot by contract) |

Capture the transport error **class before** each SDK's flattening call (py `_errors.py _transport_retry_error`,
js `transportError`, go `transportRetryError`, java `Transport.java` IOException catch, swift the bare `catch`) —
after those lines only a message string remains. TTFT is only observable in the SSE decoder
(py `_sse.py`, js `internal/sse.js`, go `sse.go`, rust `sse.rs`, java `EventStream.java`, swift `SSEParser.swift`).

### 6.2 Reporter requirements (every SDK)
- Own HTTP client, never the SDK's engine or the user's injected client; single background worker
  (py daemon thread; js `setTimeout(...).unref()` + `beforeExit`; go goroutine + `Close()`; rust `tokio::spawn`;
  java daemon single-thread executor; swift detached `Task`); lazily started on first record, never at import.
- Bounded: events ≤1 000 (drop oldest success first, then oldest failure — count every drop); counter keys
  ≤256 per minute window; closed windows retained ≤24 h under a byte cap (~512 KiB; oldest first);
  flush at 30 s or ≥50 events or ≥60 KB or process exit (≤2 s, single attempt); backlog drains in successive
  ≤100/≤200 batches spaced by the flush interval.
- One POST per flush, no retries; 429/503 → exponential backoff 60 s → 10 min honouring Retry-After ≤ 600;
  400/401/403/404/410 → disable for the process; `policy` applied only if it reduces volume;
  `x-tr-telemetry: off` → disable.
- Never send for control-plane calls; the beacon POST itself is never traced.
- Fork safety where applicable (py `os.register_at_fork(after_in_child=reset)`).

### 6.3 Config surface (every SDK)
- Constructor/builder: `telemetry: bool | None` and `telemetry_sample_rate: float` beside `regional_failover`.
- Env: `TRUSTEDROUTER_TELEMETRY` ∈ {`0`,`false`,`off`,`no`} disables; `DO_NOT_TRACK=1` disables;
  `TRUSTEDROUTER_TELEMETRY_DEBUG=1` echoes each batch JSON to stderr before send (a trust feature).
- Precedence: explicit arg > `TRUSTEDROUTER_TELEMETRY` > `DO_NOT_TRACK` > default.
- **Default on only when** the control host is `trustedrouter.com` (or a subdomain) AND the inference base
  is a known TrustedRouter host. Custom control planes / custom base URLs default off. No URL env var.
- Opt-out disables **both** the `x-tr-client` header and the beacon (User-Agent stays).

### 6.4 Tests every SDK must add (parity)
- Header on attempt 0 (`v=1;a=0;…`); after a 503 the alias attempt carries `po=http_error;ph=apex;fo=1`
  (extend the existing alias-failover test); custom base URL → no header, no beacon; control-plane calls →
  no header, no beacon.
- Transport-error classification for the common classes; stream first item → ttft; mid-body error →
  `stream_broken`; caller abort → `aborted`.
- Failures always recorded; successes sampled; counters exact.
- Reporter: bounded drops; 24 h retention across a failed flush; 429 backs off; 400 disables; exit ≤ 2 s;
  the SDK's own fake transport sees zero `/client-events` calls; opt-out precedence; no worker when off.
- Privacy: serialised batch keys ⊆ schema; injected prompt text never appears; custom hostnames never appear.
- Parity constants pinned in each SDK's parity test: beacon path `/client-events`, schema version 1, the
  Host/Endpoint/Outcome/ErrorClass enums.

## 7. Server-side data model and retention (ClickHouse, `clickhouse/008_client_events_replicated.sql`)
- `activity_generations` gains `gateway_request_id`, `synthetic`, `client_*` columns (header channel).
- `client_request_events` (raw sampled records) — TTL **90 days**; ORDER BY (tenant_id, created_at, event_id);
  `event_id` = sha256(tenant_id, batch_id, "r", index) computed server-side.
- `client_minute_counters` (exact) — TTL **180 days**.
- `client_availability_rollups` (5m/hour/day; fleet + tenant scopes; `methodology_version`) — TTL **24 months**.
- `operational_outbox_quarantine` — poison rows parked, never crash-loop the drainer — TTL 30 days.
- Tenant/key ids are one-way surrogates (`analytics_surrogate`); raw ids never leave Spanner.
- Raw client tables are **not** archived to Parquet (explicit decision); rollups are the retained artefact.

## 8. Methodology v1 — how "client-observed availability" is computed (published on `/docs/telemetry`)
- Unit = one logical SDK call. Source = `client_minute_counters` (exact). Retries never counted twice;
  failover-rescued requests are successes, with `failover_used` and `first_attempt_success` published.
- `tr_fault` (counts against us; `client_reliability.classify_tr_fault`, `METHODOLOGY_VERSION = 1`):
  transport_error with class ∈ {dns, tls, connect_refused, connect_timeout, connect_error, reset, io_error,
  protocol_error} on a non-custom host; http 5xx from any source **except** `provider_pinned` with
  `error_source=provider`; timeout with phase ∈ {connect, first_byte} and `timeout_floor_met`; stream_broken;
  stream_stalled with `timeout_floor_met`; unknown (conservative).
- Excluded from the denominator (counted and disclosed): aborted; 4xx and 429; pool_timeout / proxy_error;
  anything on `custom` hosts; timeouts below floor; `timeout_phase=total`.
- `availability = successes / (successes + tr_fault)`. Per-host attempt-level `attempt_tr_fault/attempts` is
  used for alerting only.
- Fleet: exclude synthetic (server-side); per-tenant cap 25 % of the window; publish percentages only if
  requests ≥ 1 000 and distinct tenants ≥ 3; coverage (telemetry successes ÷ `activity_generations`
  non-synthetic rows) always shown. Time = server-assigned; late batches land in the right minute; rollups
  recompute the trailing 6 h every 5 min (matching the late-arrival cap); `age_ms > 6 h` excluded from rollups.
- Own id `client_observed`; never inside `router_core` or `SLO_DEFINITIONS`. Calibration gate: 14 clean days
  before any percentage is published.

## 9. Rollout ordering (why it is not arbitrary)
1. Enclave request ids (independent). 2. Control plane accepts settle context (extras reach customer broadcast
webhooks otherwise). 3. ClickHouse node: DDL + tolerant ingester (poison-row crash loop otherwise) — before
the control plane emits new outbox kinds or declares new activity columns. 4. Enclave header channel.
5. Beacon ingest (flag default off → flip after smoke + load test). 6. Canary + alerts + freshness (same phase).
7. Python SDK; header-only PRs in the other five SDKs. 8. Surfaces. 9. Calibration → publication.
Enum vocabulary grows **server-first**; SDKs last. Enclave merges auto-deploy production.

### Calibration

Run the read-only weekly report on `tr-clickhouse-1` with
`CH_PASSWORD=... python3 -m clickhouse.calibrate_client_availability --days 7 --json-out /tmp/cal.json`.
The report compares five-minute client and `router_core` outcomes (putting
`client_down_server_up` first), audits `gateway_request_id` joins and RTT, lists tenant and SDK-version
outliers, and records the publication gates for each closed UTC day. Flip `published` only after 14
consecutive clean days: per-tenant/hour counter and generation successes agree within 1%; on incident-free
days client availability is no more than 0.05 percentage points below synthetic; every day has at least the
configured negative-control count (200 by default); and every day has at least 1,000 requests from at least
three tenants.

## 10. What NOT to do
No OTel/metrics runtime in SDKs or enclave; no free-text fields; no beacon retries; no idempotency keys in
telemetry; no forging `x-stainless-*`; no `client` on authorize; no client fields on `/v1/generation`;
no `client_observed` inside `SLO_DEFINITIONS`; no beacons in a second SDK until the Python contract has been
live and calibrated; no CORS in v1.
