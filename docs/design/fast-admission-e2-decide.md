# E2 decide: Go authorizes the cohort

Status: contract, nothing built. This is the shared contract for E2 of
[`fast-admission-enclave-direct.md`](fast-admission-enclave-direct.md): the
Go edge answers an authorize for the cohort itself, writes the two rows
Python's leased path writes, and proxies everything else to Python
unchanged. Python keeps settle, heartbeat, disposition, the reaper and every
refusal. The pull requests below build against this document in parallel;
a change to anything here is a change to this document first.

Everything stays dark until the last step: each piece merges and deploys
behind a setting or flag that is off, and the edge decides for a workspace
only when a node flag, a decide row and Python's own pilot set all say so.
The two risks that decide the order of work are idempotency fingerprint
parity (a mismatch turns an enclave retry into a 409) and byte parity of the
authorization row payload that Python settles from.

Line citations are against `origin/main` at `bc5a19c16`.

## 1. What Go answers

A 200 for a request in the cohort, byte-compatible with Python's on what the
enclave decodes, and Python's exact 503 when its own insert's outcome is
unknown. Nothing else: every refusal Python would make, every request
outside the cohort, every cold or stale cache and every owner answer but
`Admitted` goes to Python unchanged, as E1 proxies today.

The flow, per request, in this order:

1. The E1 checks: internal token, boot signature (`edge.go:294`,
   `ServeHTTP`).
2. Parse the body as `GatewayAuthorizeRequest` strictly (§3, §8.2); any
   doubt passes.
3. Look up the cached facts: key by `api_key_lookup_hash`, workspace,
   edge policy, routing snapshot, decide copy. Any cold or stale cache
   passes and fills in the background.
4. Gate on the facts (§3).
5. `Snapshot.Evaluate`: `Answered`, every candidate priced in credits, no
   estimated-usage model.
6. Compose everything but the envelope (§5, §6).
7. `FrontDoor.Authorize` in process. Anything but `Admitted` passes.
8. Insert the two rows in one read-write transaction with a 1.5 s deadline
   (§8.4): `Inserted` and `Landed` answer 200; `ScopeTaken` and `Failed`
   refund and pass; `Unknown` reads the authorization back by id, answering 200 if it
   landed and Python's 503 (hold kept) if not.
9. Render: append `timing` (§6) and write the bytes.

## 2. Decisions

1. **What Go answers.** Only 200 admissions, plus Python's exact 503 for an
   unknown insert outcome: status 503, `Retry-After: 1`, body
   `{"error":{"code":503,"message":"Authorization unavailable; retry","type":"service_unavailable","source":...},"data":{"timing":{...}}}`
   as `api_error` renders it (`gateway.py:1794-1807`) and the timing
   decorator's `_scope` extends it with a top-level `data.timing` beside
   `error` (`gateway_timing.py:86-107`); the exact bytes, timing aside, are
   P1's `unavailable` golden case. Every refusal and every owner non-admit
   is proxied to Python unchanged.
2. **Key snapshot.** Go reads Spanner itself (the same `tr_entities` it
   reads boots from): a strong refresh every 5 s of each entry used in the
   last minute, 15 s maximum age, a cold miss passes, and a Python 401, 402
   or 403 (or a pause 503) for a cached lookup hash evicts the key and its
   workspace. No per-key publisher and no new signing key.
3. **Flow.** Gate on cached cohort facts, then the evaluator (`Answered`,
   all candidates Credits, no estimated-usage model), then compose, then
   `FrontDoor.Authorize` (non-`Admitted` passes), then the insert with a
   1.5 s deadline: `Inserted` and `Landed` answer; `ScopeTaken` refunds and
   passes; `Failed`
   refunds and passes; `Unknown` reads back by id (landed: 200, else 503
   with the hold kept). Which commit errors are which is §8.4: only an
   error that proves nothing applied is `Failed`. No scope pre-read: the
   unique index `tr_reservation_by_idemp` arbitrates. No key presented:
   mint a uuid4, as Python does (`gateway.py:1119-1120`).
4. **Fingerprint.** Pass any body with a JSON float or exponent anywhere, a
   declared field whose JSON type pydantic would coerce, duplicate keys,
   non-ASCII tags, an `http_referer` without `app` that fails the strict
   ASCII URL rule, a model or models entry `canonical_router_model_id`
   rewrites, or a native-batch or partner idempotency key. G10's row
   read-back compares fingerprints live.
5. **Health rank and affinity.** Go answers the cold order. A request with
   `cache_affinity_key` passes in E2; revisit if that is more than about
   30% of the cohort.
6. **Broadcast and BYOK.** Any broadcast destination pointer for the
   workspace passes; otherwise `broadcast_destinations` is `[]`. BYOK
   presence comes from entity ids alone; any BYOK candidate passes.
7. **Stage D.** Streams stay out. The reason comes from the edge policy's
   live Stage D settings and the boot's `declares_usage_heartbeat`. Any
   candidate whose model is in `ESTIMATED_USAGE_SNAPSHOT_MODELS`
   (`stage_d.py:31-34`, three ids) passes; Go hardcodes the three, P1's
   header carries Python's set, and a Go test holds the two equal.
8. **Accepted digests.** From P2's signed edge policy (the resolver's set
   union the break-glass override, `gateway.py:493-517`), maximum age
   60 s, stale passes. A boot is accepted iff its signature verified (E1),
   `body.api_key_lookup_hash` equals the resolved key's lookup hash, and its
   image digest is in the set (`gateway_boot.py:117-147`).
9. **Signing the policy.** With the routing-snapshot key, under a distinct
   manifest `typ` (`tr-edge-policy-v1`) and body `kind`
   (`tr-edge-policy`). Each verifier refuses the other's `typ`.
10. **Decide table.** `tr_fastpath_edge_decide(workspace_id, cloud,
    region)`; cloud is the edge plane's (`gcp`); region is
    `choose_region(body.region)`. Each node copies it every 5 s; a copy
    older than three intervals is all off. `tr_fastpath_workspace` stays the
    one switch: decide only chooses who decides, and `fastpathctl disable`
    does not touch decide rows.
11. **Python's pilot set gates too.** Decide only if the policy says
    `fastpath_pilot_enabled`, the workspace is in
    `fastpath_pilot_workspaces`, the edge's region is in
    `fastpath_pilot_regions`, and `records_durable`. Initial decide rows are
    us-central1 only; `rollout.sh` must render `settle_outbox_enabled` the
    same in every region before any other region gets a row.
12. **Busy is a pass, and the budgets fit the enclave's timeout.** `Busy`
    or an owner timeout passes, never a 503. The decide budget, 1.5 s, is
    the deadline of the context `FrontDoor.Authorize` gets, so it cuts
    each owner's `OwnerWait` short; past it the request passes. Once
    admitted, the insert has its own 1.5 s and a read-back 0.5 s (§8.4); a
    refund runs asynchronously with its own 1 s and never delays the pass
    (a refund not sent ends with the lease, as Python's does). Worst case:
    an answer 3.5 s after arrival (1.5 + 1.5 + 0.5); a pass forwarded 3.5 s
    after arrival (an `AlreadyExists` check after a full insert). The
    enclave allows 25 s for response headers per attempt
    (`ResponseHeaderTimeout`, quill-cloud-proxy
    `enclave-go/internal/trustedrouter/http_client_direct.go:37`) and 28 s
    for three attempts (`authorization_retry.go:22-28`,
    `spendlease.ReserveBudget`). Python spends at most its 20 s Spanner
    budget and keeps 5 s for the rest (`gateway.py:353-356`), so the worst
    forwarded pass, 3.5 s plus Python's 20 s, leaves 1.5 s of the 25 s
    rather than Python's 5 s. Python's 20 s is a wall-clock Spanner
    deadline set when `_authorize_gateway_sync` starts (`spanner_rpc_budget`,
    `storage_gcp_io.py:146-166`, applied at `gateway.py:871-873`); it
    excludes threadpool queueing before that and work after the last RPC,
    such as Python's own front-door call (up to 1.5 s,
    `fastpath_front_door_timeout_seconds`, `config.py:1046`), so a rare
    forwarded pass can still exceed 25 s. That is the tightest Go may go:
    G11 fails a test if any budget here grows, and G12 alerts on forwarded
    passes that spent more than 1 s at the edge and on any forwarded pass
    past 25 s in all.
13. **Response encoding.** Identity-encoded JSON, `Content-Type:
    application/json`, `Content-Length`, and the bytes FastAPI's pydantic-core
    fast path writes for the 200 (§6): insertion order, raw UTF-8,
    pydantic-core's float format. Error bodies (the 503) are Starlette's
    `JSONResponse`, `json.dumps`. Python's GZip middleware (`middleware.py:132`,
    `minimum_size=1_000`) compresses only for a caller that accepts gzip;
    Go never compresses.
14. **Per-key in-flight limit** at the edge, from the policy's
    `gateway_authorize_max_in_flight_per_key`; over the limit passes. Go
    never learns "exhausted" from `Busy`.
15. **One clock.** One edge clock reading per request is the price instant,
    `created_at` and the base of `expires_at`. An instant outside the
    snapshot's window passes.
16. **Imports.** `gwauth` may import `routingsnapshot`; the import rule
    (`routingsnapshot/rules_test.go:138`, `TestOnlyTheEdgeImportsTheEvaluator`)
    widens to the edge and `gwauth` only.
17. **Revocation lag is bounded, not zero.** Python reads the key, the
    workspace and the boot strongly on every request; Go reads them from
    caches. The bound: a disabled, expired or rescoped key, a paused or
    abuse-latched workspace, or a debt mark is honoured within 15 s (the
    facts' maximum age); a digest withdrawn from the accepted set within
    60 s, the same as Python's own resolver
    (`stage_d_policy_refresh_seconds`, 60, `config.py:1049`), because P2
    signs only right after a successful background refresh of the resolver
    (§4), so a body's set is current at `issued_at` and the edge holds it at
    most until `not_after`. A release-host outage therefore turns Go's
    deciding off after 60 s (availability only). Eviction is sooner when Python answers
    the key a 401, 402 or 403, or the workspace a pause 503, on any request
    the edge passes. The cohort is pilot workspaces only, and the operator's
    immediate lever is `fastpathctl decide-off-all` (honoured within one
    5 s copy interval). Before I2, `docs/runbooks/fastpath-turn-off.md`
    (G2's paragraph) makes `fastpathctl decide off` the first step of any
    abuse pause or key revocation for a decided workspace. A revocation feed
    (a change stream or a Python-published epoch) is E4's, with capped keys.
18. **Timing.** Every Python 200 ends `data` with `timing`
    (`@timed_gateway_async`, `gateway.py:573`; `gateway_timing.py:257-279`;
    required by `GatewayAuthorizeData`, `schemas.py:365`). Go emits the same
    six keys in the same order with its own integer milliseconds and RPC
    count (§6). The comparator checks the keys and their order and ignores
    the values.
19. **Internal models.** A request naming `trustedrouter/monitor` or an
    `internal_only` model in `model` or `models` passes: Python answers a
    key other than the monitor or probe key 403 (`gateway.py:1008`,
    `internal_model_access.py:31-48`). Go passes on the union of two sets:
    the edge policy's `internal_models` (§4, from the live catalog) and
    Go's hardcoded set (`trustedrouter/monitor`,
    `trustedrouter/fortanix-test` today). Python and Go deploy separately, so a Python release that adds
    an internal model reaches the snapshot before any new Go binary; the
    policy carries it within one refresh, and a stale policy already passes
    everything. The routing snapshot is published and polled live, so its
    v1 shape stays as it is. P1's header carries Python's set
    (`MONITOR_MODEL_ID` and every `internal_only` model,
    `catalog_registry.py:1867-1883`); a Go test holds the hardcoded set
    equal to it, so the fallback stays current too.

## 3. The cohort

A request is decided by Go only if every line holds. Each failing line has
a pass reason, `group.reason`, which the shadow and the watch count. A
golden pass case trips exactly one reason, so the order below only fixes
which reason a request tripping several is counted under.

**Request** (no I/O, G5):

| Reason | Passes when |
|---|---|
| `body.content_type` | not exactly one `Content-Type` whose media type is `application/json`, optionally with `charset=utf-8`; a missing header fails FastAPI's `strict_content_type`, which the app's `RequestValidationError` handler answers 400 `bad_request` (`main.py:632-639`) |
| `body.unparseable` | not one JSON object; not UTF-8, or a UTF-8 BOM (Python's `json.loads` accepts one); nesting deeper than 128 (Python's decoder or validation recursion is a 400); an integer literal over 4,000 digits (Python refuses over 4,300, a 400); over 4 MiB (Python's `max_request_body_bytes`, `config.py:1353`, a 413; the edge reads up to its own 16 MiB `maxBody`, `edge.go:58`) |
| `body.duplicate_key` | any object with a repeated key, at any depth |
| `body.float` | any number with a fraction or exponent, at any depth |
| `body.lax_type` | a declared field whose JSON type is not the schema's exact type (`"5"` for an int, `1` for a bool, a non-list for a list) |
| `body.strip_ambiguous` | a declared string (or list-of-strings entry) with any byte outside printable ASCII, where Python's and Go's whitespace could differ (`input_modalities` is a `Literal` list and is not stripped: `" text"` is a validation error, a 400, so `body.refused`) |
| `body.dict_key_strip` | a top-level key of `provider`, `trace`, `metadata` or `tags` that `str_strip_whitespace` would change (leading or trailing whitespace) or that has a byte outside printable ASCII. Pydantic strips those keys, last value winning on a collision (§7); Go passes rather than reproduce it |
| `body.refused` | anything the schema or `_gateway_authorize_body` refuses (Go never answers what Python would refuse; every validation error is a 400 through `main.py:632-639`) |
| `body.api_key_hash` | `api_key_hash` present (the lookup hash alone identifies the key in the cohort) |
| `body.stream` | `stream` true |
| `body.service_tier` | `service_tier` present |
| `body.cache_affinity` | `cache_affinity_key` present |
| `body.priced_extra` | `additional_cost_reservation_microdollars` nonzero, `inference_receipt` true, `video_resolution`, `speech_input_characters` or `transcription_duration_ms` present |
| `body.canonical_model` | `model` or a `models` entry not identity under `canonical_router_model_id` (`model_aliases.py:70-80`, applied by the schema at `schemas.py:440`) |
| `body.custom_model` | a custom or user-provided model in `model` or `models` |
| `body.internal_model` | `model` or a `models` entry, stripped, is in the policy's `internal_models` or Go's hardcoded set (decision 19) |
| `body.tags` | a tag key or value outside printable ASCII |
| `body.referer` | `http_referer` with no `app` that fails the strict ASCII URL rule (§7) |
| `body.evaluator_input` | anything `routing-snapshot-v1.md` "What Go evaluates" sends to Python (`shadow.go:927`, `requestOf`) |
| `header.idempotency_key` | an idempotency key (body or header) over 256 characters after strip, or with a native-batch or partner prefix; an `Idempotency-Key` header sent more than once (Starlette reads the first) or with a byte outside 0x20 to 0x7E (Starlette decodes latin-1 and `str.strip` strips Unicode whitespace) |
| `header.settlement_async` | `X-TR-Settlement-Mode: async-v1` (`gateway.py:2976`) |
| `header.video_providers` | `X-Quill-Video-Allowed-Providers` present |

**Caches:** `cache.policy`, `cache.snapshot`, `cache.key`,
`cache.workspace`, `cache.decide` (cold or past its maximum age).

**Key and workspace facts** (G3, G7): `key.<reason>` for an ineligible key
(§8.1 reasons), `workspace.missing` (no workspace row: Python's 403,
`gateway.py:971-972`), `workspace.paused` (any pause cause on the entity
or a credit row), `workspace.in_debt`, `workspace.tier` (latched or below
the grant's tier: the grant would refuse it anyway),
`workspace.federated`, `workspace.deleted`, `workspace.broadcast` (any
destination pointer), `tags.merged_invalid` (the merged tag map fails
`validate_tags`, or the key's stored tags are unusable: §6).

**Policy and decide** (G8, G2): `policy.pilot_off`,
`policy.not_pilot_workspace`, `policy.pilot_region`,
`policy.records_not_durable`, `policy.async_settle_shadow` (workspace in
`async_settle_shadow_workspaces`, which both of Python's async hooks
require, `gateway.py:2985-3000`), `policy.monitor_key`,
`policy.lookup_hash_mismatch`, `policy.digest_not_accepted`,
`policy.regions_mismatch` (the policy's regions disagree with the
snapshot's `settings`), `decide.off`.

**Evaluation:** `evaluate.<outcome>` for any result but `Answered`
(Python's refusals included), `evaluate.estimate_overflow` (the estimate
in microdollars is beyond int64: Python's leased path would write a value
the `INT64` columns cannot store, so Spanner refuses the commit; Go
passes), `candidates.byok`,
`candidates.estimated_usage`, `candidates.private_proxy` (the requested
model is a private proxy: Python's `response_model` would not be null),
`clock.window` (decision 15).

**Boot** (E1's check and decision 8, where Python answers without the
pilot): `boot.signature` (no `X-TR-Boot-Auth`, or one that does not parse
or verify, or more than one), `boot.unverified` (the boot row is not
verified), `boot.missing` (no boot row for the kid).

**Admission and insert:** `inflight.key`, `owner.off`, `owner.busy`,
`owner.invalid`, `owner.failed`, `owner.unreachable`, `budget.spent`,
`insert.scope_taken`, `insert.failed`.

This is Python's `pilot_reason` (`fastpath_frontdoor.py:379-426`) plus the
evaluator's exclusions, with every key cap, window, `budget_strict`,
`app_id`, federated key or workspace and the monitor key out.

## 4. Edge policy publication

Python publishes, signed, the live settings the edge must not guess. P2
builds it; G8 polls it.

**Route.** `GET /internal/fastpath/edge-policy` (and its `/v1` twin),
registered only when `TR_EDGE_POLICY_PUBLISH_ENABLED` is true (`rollout.sh`
renders it as a literal `false` until I1), on the surfaces the gateway
routes live on, authenticated by `require_internal_gateway`. Off, nothing
imports the publisher (a dormancy test checks it in a subprocess, as
`tests/test_routing_snapshot.py` does). `Cache-Control: no-store`. 503 with
`status` `signer-unavailable` when no usable key.

**Publication.** Exactly `{"v":1,"kind":"tr-edge-policy-publication","manifest":JWS,"body":B64}`,
compact JSON; `body` is the body's exact bytes in standard base64. The edge
hashes the decoded bytes and never re-encodes them. The publisher signs a
body at most once per `refresh_at` interval. A poll never waits on a
refresh or a signing: the first poll past `refresh_at` starts one
single-flight background job that calls `StageDPolicyResolver.refresh()`
(`stage_d_policy.py:238`: two HTTPS fetches with 5 s timeouts, a Sigstore
bundle check and the watermark, so at most four a minute on an instance
the edge polls) and, if it succeeds, signs a new body whose `issued_at` is
that instant. Every poll answers at once: the cached publication while
`now < not_after`, else 503 with `status` `digest-policy-unavailable`. A
failed refresh signs nothing new; the edge keeps its last copy until
`not_after` and then passes every request. With no release URL configured
there is nothing to refresh, and the set is the break-glass override alone,
as in Python. The expected side effect: an outage of the release host
(`trust.trustedrouter.com`, `config.py:1899`) turns Go's deciding off
after 60 s. That costs availability of the fast path only; Python decides
meanwhile.

**Body.** `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False)`, printable ASCII, no trailing newline.
Integers only (no floats), every field present, `null` only where the
table says, sets as sorted lists.

| Field | Type | Value |
|---|---|---|
| `v` | int | `1` |
| `kind` | string | `"tr-edge-policy"` |
| `plane` | `{cloud, region}` | the publisher's plane, as the snapshot's `plane` |
| `issued_at` | int, Unix s | the publisher's clock |
| `not_after` | int | `issued_at + 60` |
| `refresh_at` | int | `issued_at + 15` |
| `accepted_image_digests` | sorted strings | `StageDPolicyResolver.accepted_image_digests()` union `stage_d_accepted_gcp_digests` (`gateway.py:493-517`), read right after a synchronous `resolver.refresh()` (below); empty with no resolver |
| `stage_d` | object | `eligibility_enabled`, `heartbeat_enabled`, `require_heartbeat_declaration` (bools), `pilot_workspaces` (sorted): the live Settings, field names as the snapshot's `settings.stage_d` |
| `records_durable` | bool | `settle_outbox_enabled` and the activity outbox present, exactly `gateway.py:1703-1706` |
| `fastpath_pilot_enabled` | bool | Settings |
| `fastpath_pilot_workspaces` | sorted strings | `fastpath_pilot_workspace_ids` |
| `fastpath_pilot_regions` | sorted strings | `fastpath_pilot_region_ids` |
| `gateway_authorize_max_in_flight_per_key` | int | Settings (`config.py:701`) |
| `monitor_lookup_hash` | string or null | `monitor_lookup_hash(settings)` |
| `async_settle_shadow_policy` | string | Settings |
| `async_settle_shadow_workspaces` | sorted strings | `async_settle_shadow_workspace_ids` |
| `configured_regions` | strings, configured order | `regions.configured_regions` |
| `primary_region` | string | Settings |
| `multi_region_enabled` | bool | Settings |
| `internal_models` | sorted strings | `MONITOR_MODEL_ID` and every `MODELS` entry with `internal_only`, from the serving process's catalog (`internal_model_access.requests_internal_model`'s rule) |

The edge reads Stage D from the policy, not from the snapshot's
`settings.stage_d`, which is the compile instant's: the kill switch must
reach the edge within the policy's age. It passes when
`configured_regions`, `primary_region` or `multi_region_enabled` disagree
with what the held snapshot's `settings` imply (`regions`,
`primary_region`, `region_payload[].enabled`).

**Manifest.** A compact JWS as the snapshot's (`routing_snapshot_signing.py`):
header exactly `{"alg":"EdDSA","kid":KID,"typ":"tr-edge-policy-v1"}`,
purpose `routing-snapshot` (the key's own; the same pinned key, kid, issuer
`https://trustedrouter.com` and audience `tr-fastpath-edge`), claims exactly
`{v: 1, sha256, size, issued_at, not_after, plane, iss, aud}`, strict
non-negative integers. The verifier checks, in the snapshot verifier's
order: header, one accepted key of that kid, signature, the test key
refused outside test mode, claims canonical and equal to the signed
payload, `iss` and `aud` the key's, `aud` and `plane` the verifier's own,
`sha256` and `size` of the decoded body, the body's `v`, `kind`,
`issued_at`, `not_after` and `plane` equal to the claims', and `issued_at
<= now < not_after`. A snapshot verifier given a policy manifest refuses it
on `typ`, and the reverse; both directions are tests in P2 and G8.

**Edge behaviour.** Poll at `refresh_at` with an explicit 5 s request
timeout (G8); on failure back off from 2 s to 15 s; keep the last good copy until its `not_after`, then drop it (every
request passes with `cache.policy`). The URL must be on the edge upstream's
scheme and host, as the snapshot URL is.

**Golden files** (P2): `fastpath/testdata/edge_policy_v1.body.json` (exact
canonical bytes, no trailing newline, synthetic workspace ids and digests)
and `fastpath/testdata/edge_policy_v1.manifest.json`, shaped exactly like
`routing_snapshot_v1.manifest.json`: `{aud, generator, iss, jws, kid,
public_key_b64url, purpose, test_key: true}`, signed with the same test key
(`routing-snapshot-test-key-1`, derived in
`scripts/fastpath/routing_snapshot_vectors.py`), generated by
`scripts/fastpath/edge_policy_vectors.py`.

## 5. The two rows

Exactly what `_authorize_gateway_leased` writes (`storage_gcp.py:5018-5147`),
in one read-write transaction (Python tags it `tr_authorize_leased`,
`storage_gcp.py:5121`; Go tags it `tr_authorize_leased_edge`). Go writes
two `spanner.Insert` mutations; how each commit error maps to an outcome
is §8.4.

**`tr_reservation`** (`storage_gcp_counter_dml.py:680-685` columns,
`756-778` insert):

| Column | Value |
|---|---|
| `reservation_id` | hyphenated lowercase uuid4 |
| `workspace_id`, `key_hash` | the key's |
| `ws_shard`, `credit_shard`, `key_shard` | `0` (`UNSHARDED`) |
| `credit_reserved_micro`, `key_reserved_micro` | `0` |
| `hold_usage_type` | `"Credits"` |
| `authorization_id` | the authorization's |
| `idempotency_scope` | `f"{workspace_id}#{key_hash}#{sha256_hex(idempotency_key)}"` (`storage_gcp_keys.py:596-600`), never null in the cohort (a key is always presented or minted) |
| `idempotency_fingerprint` | §7 |
| `expires_at` | the clock reading plus 7200 s (`GATEWAY_RESERVATION_TTL_SECONDS`, `user_model_rules.py:17`). Python takes a second clock reading for it (`gateway.py:1668`), a few milliseconds after `created_at`; Go uses its one reading (decision 15), P1 pins both of Python's to the case's instant, and G10 compares `expires_at - created_at` as exactly 7200 s for Go and at least 7200 s for Python |
| `created_at` | the clock reading |

`settled` takes its default (false); `actual_micro`, `settled_usage_type`,
`terminal_at` are null.

**`tr_gateway_authorization`** (`storage_gcp_request_records.py:148-209`):
`authorization_id`, `workspace_id`, `key_hash`, `reservation_id`,
`model_id` (the first candidate's model), `provider` (the first candidate's),
`usage_type` `"Credits"`, `estimated_microdollars`, `settled` false,
`created_at` the clock reading, `terminal_at` null, `payload` below, and
the 13 typed columns (`storage_gcp_request_records.py:39-53`) all null but
`idempotency_fingerprint`, `stage_d_boot_kid` and `invocation_nonce` (null
when the body sends none).

**The payload** is `json_body(GatewayAuthorization)` (`storage_codec.py:25-37`):
`json.dumps(asdict, sort_keys=True, separators=(",", ":"))`, ASCII, with a
field dropped when it is `None` and its default is `None`, or `[]`/`{}`
with a list/dict default factory. Fields with a non-`None` default are
always written. For the cohort (`storage_models.py:887-990`), every key:

| Key | Value |
|---|---|
| `id` | `"gwa-" + uuid4().hex` (`gateway.py:2817-2819`) |
| `workspace_id`, `key_hash` | the key's |
| `model_id`, `provider`, `endpoint_id` | the first candidate's |
| `usage_type` | `"Credits"` |
| `estimated_microdollars` | the model estimate |
| `credit_reservation_id` | the reservation id |
| `settled` | `false` |
| `created_at` | the clock reading, `isoformat()` with `+00:00` as `Z`: no fraction when the microseconds are 0, else six digits |
| `requested_model_id` | `body.model` (identity-canonical, stripped) |
| `candidate_model_ids`, `candidate_endpoint_ids` | in candidate order |
| `region` | `choose_region(body.region)` |
| `idempotency_key` | presented (stripped) or minted |
| `idempotency_fingerprint` | §7 |
| `tags` | the merged map (§6), dropped when empty |
| `app_id`, `app_owner_user_id`, `custom_model_owner_user_id` | `""` |
| `app_markup_basis_points`, `receipt_fee_basis_points`, `custom_model_markup_basis_points`, `additional_cost_reservation_microdollars` | `0` |
| `native_batch_eligible` | `false` |
| `settlement` | `"local"` |
| `stage_d_reason` | §6 |
| `stage_d_prompt_tokens` | `estimated_input_tokens` (1 when omitted) |
| `stage_d_max_output_tokens` | `max_tokens`, else `max_completion_tokens`, else `max_output_tokens`, else 512 |
| `stage_d_boot_kid` | the boot's kid |
| `invocation_nonce` | when the body sends one |
| `fastpath_envelope` | `Authorized.Envelope` |
| `fastpath_end_of_life` | `Authorized.EndOfLife` truncated to microseconds and written with its own UTC offset, exactly what Python's `_parse_time` and `isoformat` make of Go's JSON for that value today (`fastpath_frontdoor.py:359-376`): `+00:00` becomes `Z`, any other offset stays `±HH:MM`; no fraction when the microseconds are 0 |

Absent, because `None` with a `None` default: `key_reserved_microdollars`,
`custom_model_id`, `custom_model_revision`, `user_provided_model_id`,
`user_provided_model_revision`, `user_model_prompt_price_microdollars_per_m`,
`user_model_completion_price_microdollars_per_m`, `user_model_owner_user_id`,
`video_pricing_snapshot`, `expires_at`, `started_at`, `heartbeat_seq`,
`heartbeat_at`, `heartbeat_hash`, `selected_endpoint_id`,
`delivered_usage`, `pricing_snapshot`, `gateway_request_id`,
`finalization_outcome`, every `finalized_*` field, and `invocation_nonce`
when the body sends none.

**Schema copies** (G2): `fastpath/schema/fastpath.sql` gains verbatim
copies of production's `tr_gateway_authorization` (the `CREATE TABLE` at
`migrate_request_retention.sh:92-106` with the columns
`migrate_spend_lease.sh:97-138` adds, retired spend-lease columns included)
and `tr_reservation_by_idemp` (`migrate_typed_counters.sh:148-151`). Every
read Go makes of these rows is by primary key (§8.4, G10), so no other
index is copied. `fastpath/schema/schema_test.go` holds each copy equal to the production
script, as `TestCreditBalanceIsProductions` does. The row-deletion policy
is not copied.

## 6. The response

`_gateway_authorize_response` (`gateway.py:2923-3066`) as FastAPI 0.146
renders it. The route returns a `dict` and declares `-> dict[str, Any]`
(`gateway.py:2421-2427`); FastAPI infers that as the response model, and
with the default response class it takes its fast path: the dict is
validated by `TypeAdapter(dict[str, Any])` and written by its `dump_json`
in pydantic-core (`fastapi/routing.py` `serialize_response`,
`dump_json=True`; `fastapi/_compat/v2.py` `serialize_json`), not by
Starlette's `JSONResponse`. Checked against `json.dumps(content,
ensure_ascii=False, separators=(",", ":"))` on this content: the same
compact separators, the same insertion order (the `Any` validation keeps
the dict's order), the same raw UTF-8 for non-ASCII (`\u2028` and
`\u007f` raw), the same escapes (`\"`, `\\`, `\b`, `\f`, `\n`,
`\r`, `\t`, other controls as `\u00XX`), integers of any size as
digits, no trailing newline; only floats differ (below). The headers are
`content-type: application/json` and `content-length`. The 503 and every
other error go through the exception handlers' `JSONResponse` instead
(`json.dumps`, `ensure_ascii=False`, `allow_nan=False`). One object,
`{"data": {...}}`, with these keys in this order for the cohort:

1. `authorization_id`
2. `generation_id`: `"gen-" + uuid5(NAMESPACE_URL, "trustedrouter:" + authorization_id).hex` (`storage_models.py:1440-1448`)
3. `workspace_id`, `api_key_hash` (the key's `hash`)
4. `model`, `upstream_model`, `endpoint_id`, `provider`, `provider_name`,
   `inference_location`, then `wafer_zdr_required: true` only for a Wafer
   endpoint with ZDR (`gateway.py:5463-5468`): all the first candidate's
5. `requested_model`, `response_model` (`null`), `hide_public_metadata`
   (`false`)
6. `usage_type`, `limit_usage_type`: `"Credits"`
7. `estimated_cost` (`microdollars / 1e6` in pydantic-core's float format, `money.py:26-33`, `64-66`), `estimated_cost_microdollars`
8. `credit_reservation_id`
9. `byok_secret_ref`, `byok_encrypted_secret`, `byok_cache_key`,
   `byok_key_hint`, `byok_provider`: all `null` (`gateway.py:5471-5479`)
10. `content_storage_enabled` (`false`), `region`, `regions` (the
    snapshot's `settings.region_payload`, each object in Python's key
    order: `id`, `name`, `primary`, `enabled`, `api_base_url`,
    `control_plane_url`), `broadcast_destinations` (`[]`)
11. `idempotent_replay` (`false`), `additional_cost_reservation_microdollars`
    (`0`), `receipt_fee_basis_points` (`0`), `candidate_cost_reporting`
    (`false`: `billing_pricing_snapshot` is `None` for a row with no
    pricing snapshot), `request_metadata_version` (`1`),
    `native_batch_eligible` (`false`)
12. `stage_d`: `{"eligible": false, "reason": R}`
13. `tags`, `custom_model` (`null`)
14. `route_candidates`: per candidate, in order (`gateway.py:5438-5461`):
    `endpoint_id`, `model`, `upstream_model`, `provider`, `provider_name`,
    `inference_location`, optional `wafer_zdr_required`, `usage_type`, the
    five `byok_*` (`null`), `region`
15. `timing`: `total_ms`, `spanner_rpcs`, `key_lookup_ms`, `routing_ms`,
    `store_ms`, `post_commit_ms`, non-negative integers, in that order
    (`GatewayTiming.snapshot`, `gateway_timing.py:54-60`), appended last by
    the decorator. Go's values: `total_ms` from the request's arrival at the
    edge to the render; `spanner_rpcs` the Spanner RPCs this request issued
    (the commit, and the read-back if any); `key_lookup_ms` the cache
    lookups (0 on a hit); `routing_ms` the gate, evaluation and compose;
    `store_ms` the admit and the insert; `post_commit_ms` the commit to the
    render. Values are diagnostics: the comparator ignores them.

`inference_location` is the snapshot endpoint's `response.inference_location`
re-emitted in Python's literal key order (`provider_locations.py:419-429`):
`advertised_regions`, `advertised_region_scope`, `catalog_updated_at`,
`provider_declared_locations`, `provider_declaration_reviewed_on`,
`serving_region`, `serving_region_status`, `region_pinning_enforced`,
`documentation_url`. The snapshot body is sorted, so the order is Go's to
restore; P1's goldens catch drift.

**`tags`** is `merge_tags(key.tags, body.tags)` (`request_tags.py:65-82`,
called at `gateway.py:998-1001`): the key's stored tags in stored order,
then the request's in body order, a request key overwriting in place (a
Python dict update keeps the first position). Go runs `validate_tags` over
the merged map exactly and passes with `tags.merged_invalid` on any
failure, which Python answers 400 `invalid_tags`: more than 50 entries; a
key outside 1 to 128 characters or with a reserved prefix (`aws:`,
`trustedrouter:`, case-folded); a value over 256 characters; a character
outside letters, digits, space and `+-=._:/@` (Go accepts only their ASCII
members and passes on anything else); more than 4096 bytes as
`json.dumps(merged, ensure_ascii=False, separators=(",", ":"))`. Stored
tags that are not an object count as absent, as `merge_tags` treats them;
an object with a non-string or non-ASCII entry is unusable
(`KeyFacts.TagsUsable` false) and passes.

**After a read-back** (an `Unknown` insert whose row landed, or an
`AlreadyExists` on our own authorization id, §8.4) the canonical bytes are
Python's for that case: Python answers from the authorization it read
back, whose `tags` decode from the sorted payload, so `tags` is emitted in
sorted key order; every other byte is as for `Inserted`. P1's
`unknown_landed` cases are the goldens.

**Stage D reason** R is `_stage_d_eligibility_reason` (`gateway.py:3069-3115`)
with the policy's settings: `stage_d_disabled`, `workspace_not_pilot`
(`stage_d.pilot_workspaces` non-empty and the workspace not in it),
`heartbeats_disabled`, `heartbeat_undeclared` (declaration required and
the boot does not declare), else `not_streaming`. `boot_not_accepted`
cannot occur: the cohort requires an accepted boot.

**The float twin.** `estimated_cost` is `float(m) / 1_000_000` as
pydantic-core writes it in the 200: the shortest round-trip digits, as
`repr`, but plain decimal from `1e-5` (not `1e-4`) up to `1e16`, and an
exponent with no zero padding: `1e-6`, `9e-6`, `0.00001`, `0.000099`,
`0.000482`, `1.0`, `0.0`, `1e+16`, where `repr` gives `1e-06`, `9e-06`,
`1e-05`, `9.9e-05`. For whole microdollars the two differ only for 1 to 99
(checked for every `m` below 2,000 and 20,000 random ones up to 10^15
with pydantic-core 2.46.3). G1's `pyjson` has both: `FormatFloatRepr`
(error bodies and fingerprints) and `FormatFloatPydantic` (the 200), a
follow-up to G1 (G1b, §11); `pyjson_vectors_v1.json` holds both.

## 7. Fingerprint and idempotency

**Idempotency key** (`gateway.py:2635-2644`): `body.idempotency_key` (after
the schema's strip and its 1 to 256 length check, a 400 otherwise) or else
the first `Idempotency-Key` header value as Starlette decodes it (latin-1),
then `str.strip()`; empty is none; over 256 characters is Python's 400.
Go reads the header only when it is sent once with every byte in 0x20 to
0x7E, and strips spaces; anything else passes (`header.idempotency_key`).
None presented: Go mints `str(uuid4())`, hyphenated.

**Material** (`_gateway_authorize_body`, `gateway.py:2667-2702`, as
`prepare_fingerprint` uses it for a non-media request, `1214-1235`):

1. `GatewayAuthorizeRequest.model_dump(exclude_none=True)`
   (`schemas.py:389-453`, `_Lenient`: `extra="allow"`,
   `str_strip_whitespace`): declared fields present and non-null, defaults
   included (`estimated_input_tokens: 1`, `inference_receipt: false`,
   `additional_cost_reservation_microdollars: 0`), `cache_affinity_key` and
   `cache_affinity_explicit` excluded. Declared `str` and `list[str]` fields
   are stripped; `input_modalities` (a `Literal` list) is not. The
   top-level keys of the declared `dict[str, Any]` fields (`provider`,
   `trace`, `metadata`, `tags`) are stripped too, a collision keeping the
   first key's position and the last value; their values and anything
   nested are untouched (Go passes on any such key a strip would change:
   `body.dict_key_strip`). Extras keep their keys unstripped and their
   values as decoded, but `exclude_none` drops a top-level extra whose value
   is `null` (a nested `null` stays).
2. Remove `invocation_nonce`; remove `additional_cost_reservation_microdollars`
   when zero and `inference_receipt` when false.
3. `tags`: `validate_tags(body.tags)` when present (an empty object stays
   `{}`), removed when absent.
4. Attribution (`request_attribution.py:53-77`): remove `user`,
   `session_id`, `trace`, `app`, `http_referer`, `app_categories`, then
   add back the validated non-null ones, and `app_categories` only when
   non-empty (`request_attribution.py:48-49`: `[]` is removed); `app` defaults to
   `urlparse(http_referer).hostname` when absent. Go applies that default
   only when the referer matches `^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][\x21-\x7e]*)?$`
   (lower-casing the host as `hostname` does); any other referer without
   `app` passes.
5. Remove `api_key_hash`, `api_key_lookup_hash`, `idempotency_key`,
   `spend_lease_admission`; add `workspace_id` and `key_hash`
   (`_gateway_authorize_fingerprint`, `gateway.py:2705-2747`).
6. `json.dumps(material, sort_keys=True, separators=(",", ":"),
   default=str)`, `ensure_ascii` on; the fingerprint is the sha256 hex of
   those bytes.

The cohort has no video, native-batch or partner key, so neither of the
fingerprint's dynamic-key removals applies. Go's canonical encoder (G1)
works over an order-preserving decoded tree with integers of any size
(`big.Int`) and `-0` as `0`; a float anywhere passes before encoding.

## 8. Go packages and structs

### 8.1 `gwstate` (G3, G7)

```go
package gwstate

type Tag struct{ Key, Value string }

// KeyFacts is what the cohort needs of one api_key_lookup row and its key.
type KeyFacts struct {
	LookupHash  string
	Eligible    bool
	Reason      string // "" when eligible; else the first of the reasons below
	Hash        string // ApiKey.hash: key_hash everywhere
	WorkspaceID string
	ExpiresAt   time.Time // zero when the key has none
	Tags        []Tag     // stored order; empty when the stored tags are not an object
	TagsUsable  bool      // false: an object with a non-string or non-ASCII entry
	ReadAt      time.Time
}

// WorkspaceFacts is the workspace's entity and credit rows, read only.
type WorkspaceFacts struct {
	WorkspaceID      string
	Missing          bool     // no workspace row: Python's 403
	PauseCauses      []string // the entity's and every credit row's, sorted, deduplicated
	InDebt           bool
	TrustTier        int64 // least over rows, a latched row counting 0
	Federated        bool
	Deleted          bool
	BYOKSlugs        []string // from byok entity ids "ws#slug", sorted; never a body
	BroadcastPointer bool     // any broadcast_destination_by_workspace id with prefix "ws#"
	ReadAt           time.Time
}
```

Key reasons, in Python's order: `missing` (no lookup row or key: Python
401), `disabled` (401), `expired` (401; evaluated at the request's instant;
a value Go cannot parse strictly is expired, as Python's `is_api_key_expired`
treats an unparseable one), `scope` (non-empty `scopes` without
`inference`: 403, `gateway.py:3274-3288`), `federated`
(`federated_home` set), `app` (`app_id` set), `key_cap`
(`limit_microdollars` set), `key_window` (any of `limit_daily_microdollars`,
`limit_weekly_microdollars`, `limit_monthly_microdollars` set, alert-only
or not), `budget_strict`.

### 8.2 `gwauth` request side (G5)

```go
package gwauth

// Headers are the headers authorize reads, every value as received.
type Headers struct {
	ContentType           []string // every Content-Type value (body.content_type)
	IdempotencyKey        []string
	SettlementMode        []string // X-TR-Settlement-Mode
	VideoAllowedProviders bool     // X-Quill-Video-Allowed-Providers present
}

// Request is a GatewayAuthorizeRequest as model_dump(exclude_none=True)
// holds it.
type Request struct {
	Dump                 pyjson.Object // §7 step 1, body order, strings stripped
	Routing              routingsnapshot.Request
	Model                string
	Models               []string // nil when absent
	APIKeyLookupHash     string
	BodyIdempotencyKey   *string
	InvocationNonce      *string
	Tags                 []Tag // body order
	HasTags              bool  // present, even {}
	EstimatedInputTokens int64 // 1 when omitted
	OutputEstimate       int64 // max_tokens, max_completion_tokens, max_output_tokens, else 512
	RequestedRegion      *string
}

// Pass is why a request goes to Python; "" is none.
type Pass struct{ Reason string }

func ParseRequest(body []byte, h Headers) (Request, Pass)

// Identity is the request's idempotency key, scope and fingerprint.
type Identity struct {
	IdempotencyKey string // presented (stripped) or minted
	Presented      bool
	Material       []byte // the bytes hashed
	Fingerprint    string
	Scope          string
}

func IdentityOf(r Request, h Headers, workspaceID, keyHash string, mint func() string) (Identity, Pass)

type Tag struct{ Key, Value string }

// MergeTags is merge_tags(key.tags, body.tags) validated whole (§6);
// any failure, or stored tags Go cannot read (keyTagsUsable false), is
// tags.merged_invalid.
func MergeTags(keyTags []Tag, keyTagsUsable bool, r Request) ([]Tag, Pass)

// NamesInternalModel: model or a models entry in Go's hardcoded set or the
// policy's internal_models (decision 19). ParseRequest applies the
// hardcoded set; G10 calls this with the policy's.
func (r Request) NamesInternalModel(policy []string) bool
```

`pyjson.Object` is G1's insertion-ordered decoded object. `gwauth` imports
`routingsnapshot` and `pyjson`, nothing from `edge`, and not `gwstate`
(which stays dark until G10): G10 converts `gwstate.Pair` to `gwauth.Tag`.
`ParseRequest` answers every §3 request reason; `body.referer` also passes
a bracketed (IPv6) netloc, whose `urlsplit` checks Go does not port, and
`body.evaluator_input` any route type outside the evaluator's chat arm and
any token count beyond int64. `evaluate.estimate_overflow` needs no code of
its own: `Snapshot.Evaluate` passes an estimate beyond int64 ("the estimate
exceeds int64"), and G10 counts that pass under this reason.

### 8.3 `gwauth` compose (G6) and rows (G2)

```go
// ComposeInput is everything Compose needs, as plain values.
type ComposeInput struct {
	At              time.Time // the one clock reading (decision 15)
	AuthorizationID string    // "gwa-" + 32 lowercase hex
	ReservationID   string    // hyphenated uuid4
	Identity        Identity
	WorkspaceID     string
	KeyHash         string
	KeyTags         []Tag // stored order
	Request         Request
	Region          string // choose_region(body.region)
	Regions         []Region
	Candidates      []Candidate // in order; every one Credits
	Estimate        int64       // Result.ModelEstimate
	StageDReason    string
	BootKid         string
	Envelope        string    // Authorized.Envelope
	EndOfLife       time.Time // Authorized.EndOfLife
}

type Region struct {
	ID, Name        string
	Primary         bool
	Enabled         bool
	APIBaseURL      string
	ControlPlaneURL *string
}

type Candidate struct {
	EndpointID, Model, UpstreamModel, Provider, ProviderName string
	InferenceLocation pyjson.Object // the snapshot's; Compose re-orders it
	WaferZDRRequired  bool
}

type Composed struct {
	GenerationID  string
	Authorization AuthorizationRow
	Reservation   ReservationRow
}

// Timing is §6 item 15, in that key order.
type Timing struct {
	TotalMS, SpannerRPCs, KeyLookupMS, RoutingMS, StoreMS, PostCommitMS int64
}

// Stage builds everything but the envelope before the admit; Finish adds it.
func Stage(in ComposeInput) (Staged, error)
func (s Staged) Finish(envelope string, endOfLife time.Time) (Composed, error)

// Render is the 200's bytes, timing last; readBack sorts tags (§6).
func (c Composed) Render(t Timing, readBack bool) []byte

// Unavailable is decision 1's 503 body, timing in the top-level data.
func Unavailable(t Timing) []byte
```

G2 adds the row types to `fastpath/internal/gwauth/rows.go` (types only,
so G6 and G9 build against them in parallel):

```go
type AuthorizationRow struct {
	AuthorizationID        string
	WorkspaceID            string
	KeyHash                string
	ReservationID          string
	ModelID                string
	Provider               string
	UsageType              string // "Credits"
	EstimatedMicrodollars  int64
	CreatedAt              time.Time
	Payload                string
	IdempotencyFingerprint string
	StageDBootKid          string
	InvocationNonce        *string // nil writes NULL
}

// The shards, the reserved amounts and settled take their §5 constants.
type ReservationRow struct {
	ReservationID          string
	WorkspaceID            string
	KeyHash                string
	HoldUsageType          string // "Credits"
	AuthorizationID        string
	IdempotencyScope       string
	IdempotencyFingerprint string
	ExpiresAt              time.Time
	CreatedAt              time.Time
}
```

### 8.4 Admission and store (G4, G9)

```go
package edge

// Admitter is the in-process front door, bound after admission() builds it.
type Admitter interface {
	Authorize(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized
	Refund(ctx context.Context, r frontdoor.RefundOf) frontdoor.TerminalAnswer
}
```

The edge is built before the front door (`service.go:334`, `edge.New`;
`admission()` at `546`, `frontdoor.New` at `616`), so `Config.Admitter` is
an atomic holder `admission()` fills; empty, every decide passes with
`owner.unreachable`. `AuthorizeOf` is `{Workspace, Request:
authorization id, Estimate, Stream: false, Boot: []byte(kid)}`
(`frontdoor.go:184-198`), as Python sends it (`gateway.py:5087-5093`). The
refund after `ScopeTaken` or `Failed` is `RefundOf{Envelope, Money:
[]byte(`{"charge":0,"estimate":0}`)}`, Python's
`_fastpath_release_after_failure` (`gateway.py:5104-5133`).

```go
package store

type InsertOutcome int

const (
	Inserted   InsertOutcome = iota
	Landed     // AlreadyExists, and the row under our authorization id is ours
	ScopeTaken // AlreadyExists, and our authorization id has no row
	Unknown    // the commit may have applied
	Failed     // proven not applied
)

func (s *Store) InsertLeasedAuthorization(ctx context.Context, a gwauth.AuthorizationRow, r gwauth.ReservationRow) InsertOutcome
func (s *Store) AuthorizationExists(ctx context.Context, authorizationID string) (bool, error)
```

The insert is one `client.ReadWriteTransactionWithOptions` (tag
`tr_authorize_leased_edge`) whose callback only buffers the two
`spanner.Insert` mutations and sets a flag; never `Apply`, and never
`ApplyAtLeastOnce`, which may replay a commit that applied. The client
retries `Aborted` itself within the deadline, as Python's
`run_in_transaction_with_retry` retries it within its budget
(`storage_gcp_io.py:338`). The deadline is 1.5 s (decision 12). The outcome, by the error the transaction
returns:

| Error | Outcome | Then |
|---|---|---|
| none | `Inserted` | answer |
| any, with the callback never run (no session, no transaction began); checked first | retried once within the deadline; still failing, `Failed` | refund, pass: nothing was sent, so this is never a 503 |
| `AlreadyExists` | `AuthorizationExists(our id)`, strong, with a 0.5 s deadline on a fresh context: true is `Landed`, false is `ScopeTaken`; a read error goes on as `Unknown` | `Landed` answers as after a read-back; `ScopeTaken` refunds and passes (Python replays or 409s). Python re-reads the scope the same way (`storage_gcp.py:5122-5129`) |
| `Aborted` (retries exhausted), `InvalidArgument`, `FailedPrecondition`, `NotFound`, `PermissionDenied`, `Unauthenticated`, `OutOfRange`, `Unimplemented` | `Failed` | refund, pass: Spanner applied nothing |
| `DeadlineExceeded`, `Canceled`, `Unavailable`, `Internal`, `Unknown`, `ResourceExhausted`, `DataLoss`, the context ended, any error that is not a gRPC status | `Unknown` | `AuthorizationExists` with a fresh 0.5 s deadline of its own: true answers as after a read-back; false or a read error answers decision 1's 503 and keeps the hold, as Python does (`storage_gcp.py:5130-5146`, `gateway.py:1794-1807`) |

A refund after an `Unknown` would let an enclave retry replay, through
Python, a row whose hold was given back; so nothing that can follow an
applied commit refunds. Every read is by primary key on
`tr_gateway_authorization`, so no extra index is needed.

**Where Go differs from Python on purpose.** An `Aborted` left after the
client's retries is `Failed` in Go (refund, pass); Python counts it
transient and answers 503 after an empty read-back. `ABORTED` never
applies a commit, so the refund is safe and the pass serves the request.
An `AlreadyExists` whose row is our own authorization is `Landed` in Go;
Python's `replay()` answers it as a `REPLAY` and the route then releases
the hold even though the row is its own (`gateway.py:1920-1926`), a latent Python bug, harmless
only because Python's ids make the case near impossible (noted for a
separate fix). The residual risk is a commit the client library retries
by itself after losing the first answer: Spanner then reports the
second attempt's `AlreadyExists`, which the own-id check turns into
`Landed`; G9 holds it with a fault-injection test that drops the first
commit's answer.

**G9 notes.** Classify with `spanner.ErrCode(err)`, which unwraps the
client's error types, not a raw `status.FromError`. Run the insert and
its read-back on `context.WithoutCancel(r.Context())` with only the
deadlines above, so an enclave that hangs up mid-commit cannot turn a
commit into `Canceled`.

## 9. The decide table (G2)

```sql
-- Who the edge decides for (docs/design/fast-admission-e2-decide.md).
-- No row, or decide false, is Python. tr_fastpath_workspace stays the
-- switch; this table only chooses who decides.
CREATE TABLE tr_fastpath_edge_decide (
  workspace_id STRING(64) NOT NULL,
  cloud STRING(16) NOT NULL,
  region STRING(32) NOT NULL,
  decide BOOL NOT NULL,
  changed_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
) PRIMARY KEY (workspace_id, cloud, region);
```

`store.SetDecide(ctx, workspace, cloud, region, on)`, `ReadDecide(ctx)`
(the whole table, one strong read), `DecideOffAll(ctx)` (every row false,
rows kept). `fastpathctl decide on|off|status WORKSPACE [-cloud gcp]
[-region us-central1]` and `fastpathctl decide-off-all`.
`scripts/deploy/migrate_fastpath.sh` gains this table only; the
production copies of §5 are left alone by the migration: they join the
`production` map in `fastpath/schema/migration_test.go` beside
`tr_reservation` and `tr_entities`, and its `created` pattern learns
`NULL_FILTERED`. An empty table is nobody deciding.

The way back, fastest first: `decide off` for one workspace, then
`decide-off-all`, then `fastpathctl disable` (the front door answers `Off`,
`frontdoor.go:224-226`, which passes), then the node attribute
`fastpath-edge-decide`.

## 10. Vector files

Common to every file: generated by a script under `scripts/fastpath/` with
`--print` (stdout) and default (write), held fresh by a staleness test under
`tests/`; a compact sorted header and one case per line, as
`routing_cases_v1.json` (`scripts/fastpath/routing_cases_vectors.py`,
`render`); cases sorted by a unique `name`; `labels` a sorted list from the
file's vocabulary; instants as integer Unix microseconds in fields ending
`_us`; byte strings that are valid UTF-8 (request bodies, responses,
payloads, entity bodies) as JSON strings holding exactly those bytes; header
pairs as `[[name, value], ...]` with lower-case names, in order. Every id,
clock and envelope is pinned. No workspace, key or digest is real.

### 10.1 `fastpath/testdata/authorize_cases_v1.json` (P1)

`scripts/fastpath/authorize_vectors.py`; `tests/test_fastpath_authorize_vectors.py`
checks staleness. Every case is an HTTP request through the ASGI app
(`trusted_router.main`, the internal surface, a test client), so the
goldens carry what the route function alone does not: FastAPI's body read
and content-type rule, the validation handler's 400, the timing decorator, the
exception handlers. The store is
`tests/fakes/spanner.make_fake_store(request_record_write_mode="typed", ...)`
(as `tests/test_fastpath_pilot_store.py:19-25`) and the front door the
`_Door` fake (`tests/test_fastpath_frontdoor.py`, as
`tests/test_fastpath_pilot_route.py` uses it). The generator pins
`uuid4`, both of Python's clock readings, and `perf_counter` in
`gateway_timing` (so every `_ms` is 0), and records `spanner_rpcs` as the
fake counted it. About 300 cases.

```
{"generator": "scripts/fastpath/authorize_vectors.py",
 "snapshot": "fastpath/testdata/routing_snapshot_v1.body.json",
 "snapshot_sha256": HEX,
 "plane": {"cloud": "gcp", "region": "us-central1"},
 "policy": EDGE_POLICY_BODY,            // §4 body, as an object
 "internal_models": [STR],              // sorted: MONITOR_MODEL_ID and every internal_only model
 "estimated_usage_models": [STR],       // sorted ESTIMATED_USAGE_SNAPSHOT_MODELS
 "internal_token": STR,                 // the TEST internal token every request carries
 "boot_test_key": BOOT_TEST_KEY,        // below
 "boot_auth": BOOT_AUTH,                // below
 "body_expand": STR,                    // the body_expand rule, in words (below)
 "volatile": ["data.timing"],           // compared by shape, not value
 "cases": [CASE, ...]}

BOOT_TEST_KEY = {"kid": STR, "jwk": {...},
                 "seed_hex": HEX,       // the Ed25519 private key's 32-byte seed
                 "seed_preimage": STR,  // seed_hex is sha256 of these UTF-8 bytes
                 "seed": STR}           // that rule, in words
BOOT_AUTH = {"source": STR,             // gateway_boot.py:53-58, boot_auth_digest
             "domain": "tr-authorize-v1", "method": "POST",
             "path": "/internal/gateway/authorize",
             "digest": STR, "signature": STR, "header": STR}   // the rule, in words

CASE = {
 "name": STR, "labels": [STR],
 "at_us": INT,
 "ids": {"authorization_id": STR, "reservation_id": STR, "minted_idempotency_key": STR},
 "admitted": {"envelope": STR, "end_of_life_us": INT,
              "end_of_life": STR},      // the owner's text as it answered (§5's offset rule)
 "insert": "inserted" | "unknown_landed" | "unknown_not_landed"
         | "failed_precondition" | "aborted",   // the last two: insert.failed passes
 "request": {"path": STR, "headers": [[STR, STR]], "body": STR,
             "body_expand": {"marker": STR, "repeat": STR, "count": INT}},   // optional
 "boot": {"kid": STR, "verified": BOOL, "image_digest": STR,
          "declares_usage_heartbeat": BOOL, "signature_ok": BOOL},
 "key": {"lookup_body": STR | null, "key_body": STR | null},
 "workspace": {"body": STR | null, "credit_rows": [CREDIT_ROW]},
 "byok_entity_ids": [STR], "broadcast_pointer_ids": [STR],
 "policy_patch": {...},                 // top-level fields replacing the header's
 "facts": {"key": KEY_FACTS, "workspace": WORKSPACE_FACTS},   // §10.3 shapes
 "expect": EXPECT}

CREDIT_ROW = {"shard": INT, "in_debt": BOOL, "trust_tier": INT,
              "trust_latched": BOOL, "billing_pause_causes": [STR]}

TIMING = {"total_ms": INT, "spanner_rpcs": INT, "key_lookup_ms": INT,
          "routing_ms": INT, "store_ms": INT, "post_commit_ms": INT}

EXPECT =
   {"outcome": "answer", "status": 200,
    "response": STR,                    // pre-gzip body
    "generation_id": STR, "estimate_microdollars": INT, "decide_region": STR,
    "fingerprint": HEX, "idempotency_scope": STR,
    "fingerprint_material": STR,        // the exact JSON Python hashed (§7)
    "timing": TIMING,                   // the values inside "response"
    "authorization_row": {               // every column of the table
      "authorization_id": STR, "workspace_id": STR, "key_hash": STR,
      "reservation_id": STR, "model_id": STR, "provider": STR,
      "usage_type": STR, "estimated_microdollars": INT, "settled": false,
      "created_at_us": INT, "terminal_at_us": null, "payload": STR,
      "idempotency_fingerprint": STR, "finalization_outcome": null,
      "finalized_cost_microdollars": null, "started_at_us": null,
      "heartbeat_seq": null, "heartbeat_at_us": null, "heartbeat_hash": null,
      "selected_endpoint_id": null, "delivered_usage": null,
      "pricing_snapshot": null, "stage_d_boot_kid": STR,
      "invocation_nonce": STR | null, "gateway_request_id": null},
    "reservation_row": {                 // RESERVATION_COLUMNS
      "reservation_id": STR, "workspace_id": STR, "key_hash": STR,
      "ws_shard": 0, "credit_shard": 0, "key_shard": 0,
      "credit_reserved_micro": 0, "key_reserved_micro": 0,
      "hold_usage_type": "Credits", "authorization_id": STR,
      "idempotency_scope": STR, "idempotency_fingerprint": STR,
      "expires_at_us": INT, "created_at_us": INT}}
 | {"outcome": "unavailable", "status": 503,
    "headers": [["retry-after", "1"]], "response": STR, "timing": TIMING}
 | {"outcome": "pass", "reason": STR,   // §3 vocabulary
    "python": {"status": INT, "type": STR | null,
               "leased": BOOL}}         // whether Python took the pilot's lease
```

**Signatures.** Every request whose `boot.signature_ok` is true carries
`x-tr-boot-auth: kid=<kid>,sig=<signature>`, made from the header alone:
the digest is `sha256(domain || method || path || sha256(body))`
(`gateway_boot.py:53-58`, `boot_auth_digest`): `domain` and `method` as
their ASCII bytes, `path` UTF-8, `sha256(body)` the 32 raw bytes of the
digest of the exact body sent, concatenated with no separators; the
signature is deterministic Ed25519 (RFC 8032) over those 32 bytes with
the key whose seed is `seed_hex`, written base64url without padding. The
test key is public by construction and refused outside test mode.

**`body_expand`.** A body too large to keep (the 4 MiB cause of
`body.unparseable`) is stored as a skeleton holding `marker` exactly once,
with `body_expand`; the body sent, signed and hashed is the skeleton with
that one marker replaced by `repeat` written `count` times. A Go test
expands it before use; the Python test sends the expanded bytes.

`"unknown_landed"` expects `answer`, with `tags` in sorted order (§6,
after a read-back); `"unknown_not_landed"` expects `unavailable`. Go's
tests render with the case's `timing` and compare bytes exactly. A pass
case trips exactly one §3 reason and records what Python then answered;
every pass reason has at least one case, `body.content_type`,
`body.unparseable` (each of its causes), `body.internal_model`,
`body.dict_key_strip`, `header.idempotency_key` (repeated, non-ASCII) and
`tags.merged_invalid` (count, bytes, corrupt stored tags) included. Labels: `stage_d.<reason>` (every cohort reason),
`wafer_zdr`, `multi_candidate`, `tags.key_only`, `tags.request_only`,
`tags.merge_overwrite`, `float.exponent`, `float.integral`,
`idempotency.presented_body`, `idempotency.presented_header`,
`idempotency.minted`, `invocation_nonce`, `region.requested`,
`region.default`, `insert.<insert>`, `read_back.sorted_tags`,
`end_of_life.offset` (an owner end of life with a non-UTC offset),
`pass.<reason>`.

The test fake keeps any integer, so a case whose estimate is beyond int64
(`evaluate.estimate_overflow`) records Python's 200 from the fake, where
real Spanner refuses the commit.

### 10.2 `fastpath/testdata/authorize_fingerprint_v1.json` (P1)

As many bodies as fit under 10 MB (about 14,000 at an enclave body's
size, a 64-hex lookup hash included). Each is sent through the ASGI app as
§10.1's are, on the cohort path: a pilot workspace, the owner admitting,
and every body signed by the TEST boot as §10.1's **Signatures** says. The
fingerprint and scope are read from the reservation row the request
wrote, and a refusal is the status and `error.type` the app answered.

```
{"generator": "scripts/fastpath/authorize_vectors.py",
 "sent_with_every_body": [[STR, STR]], // after the case's headers, in this order:
                                       // x-trustedrouter-internal-token, accept-encoding: identity,
                                       // x-tr-boot-auth (kid=<kid>,sig=<signature over the body>)
 "boot_test_key": BOOT_TEST_KEY, "boot_auth": BOOT_AUTH,   // §10.1's
 "policy": EDGE_POLICY_BODY,
 "cases": [CASE, ...]}

CASE = {
 "name": STR, "labels": [STR],
 "workspace_id": STR, "key_hash": STR,
 "headers": [[STR, STR]], "body": STR,
 "expect":
    {"outcome": "fingerprint", "fingerprint": HEX,
     "idempotency_key": STR | null,    // presented, stripped; null: Go mints
     "idempotency_scope": STR | null}  // null when none presented
  | {"outcome": "refused", "status": INT, "type": STR}}
```

Labels that require Go to pass (decision 4 and §3): `float_present`,
`lax_coercion`, `duplicate_key`, `strip_ambiguous`, `dict_key_strip`,
`non_ascii_tag`, `referer_not_strict`, `canonical_model_rewrite`,
`native_batch_key`, `partner_key`, `idempotency_key_too_long`,
`idempotency_header_repeated`, `idempotency_header_non_ascii`,
`content_type`, `deep_nesting`, `int_over_4000_digits`, `bom`,
`lone_surrogate` (a lone surrogate escape anywhere: §10.4 has Go refuse
one), `estimate_overflow` (the `estimated_microdollars` Python wrote is
beyond int64: `evaluate.estimate_overflow`).
Descriptive labels: `null_extra` (a top-level extra `null`, dropped),
`null_nested`, `app_categories_empty`, `input_modalities`, `extras`,
`nested_provider`, `nested_trace`, `nested_metadata`, `unicode_extra`,
`int_near_2_63`, `int_beyond_2_63`, `attribution`, `tags`, `tags_empty`,
`whitespace`, `models_list`, `allow_fallbacks_spelling`,
`idempotency_header`, `idempotency_body`, `invocation_nonce`, `defaults`.

The test's rule: a `fingerprint` case with no pass-required label must be
answered with exactly that fingerprint and scope; one with such a label
must pass; a `refused` case must never be answered (any reason). A
property test generates further bodies and holds Go to never answering
one Python refuses.

### 10.3 `fastpath/testdata/gateway_key_vectors_v1.json` (G3)

`scripts/fastpath/gateway_key_vectors.py` builds real `ApiKey` and
`Workspace` dataclasses, stores them as Python stores them, and applies
Python's own predicates (`is_api_key_expired`, `_assert_gateway_key_scope`,
`key_window_limits`, `workspace_billing_paused`);
`tests/test_fastpath_gateway_key_vectors.py` checks staleness.

```
{"generator": "scripts/fastpath/gateway_key_vectors.py", "cases": [CASE, ...]}

CASE = {
 "name": STR, "labels": [STR], "at_us": INT,
 "lookup_hash": STR,
 "lookup_body": STR | null, "key_body": STR | null, "workspace_body": STR | null,
 "credit_rows": [CREDIT_ROW],
 "byok_entity_ids": [STR], "broadcast_pointer_ids": [STR],
 "expect": {
   "key": KEY_FACTS, "workspace": WORKSPACE_FACTS,
   "python": {"status": INT | null, "type": STR | null}}}   // null: Python proceeds

KEY_FACTS = {"lookup_hash": STR, "eligible": BOOL, "reason": STR,
             "hash": STR, "workspace_id": STR, "expires_at_us": INT | null,
             "tags": [[STR, STR]], "tags_usable": BOOL}
WORKSPACE_FACTS = {"workspace_id": STR, "missing": BOOL,
                   "pause_causes": [STR], "in_debt": BOOL,
                   "trust_tier": INT, "federated": BOOL, "deleted": BOOL,
                   "byok_slugs": [STR], "broadcast_pointer": BOOL}
```

`ReadAt` is not in the vectors. Labels name the predicate a case exercises
(`disabled`, `expired.past`, `expired.unparseable`, `scope.inference`,
`scope.other`, `federated`, `app`, `key_cap`, `key_window.alert_only`,
`budget_strict`, `tags.order`, `tags.not_object`, `tags.non_string`,
`tags.non_ascii`, `workspace.missing`, `pause.entity`, `pause.credit_row`,
`debt`, `tier.latched`, ...).

### 10.4 `fastpath/testdata/pyjson_vectors_v1.json` (G1)

`scripts/fastpath/pyjson_vectors.py` under the repository's Python
(`.python-version`); `tests/test_fastpath_pyjson_vectors.py` checks
staleness.

```
{"generator": "scripts/fastpath/pyjson_vectors.py", "python": "3.12",
 "pydantic_core": "2.46.3",                             // the staleness test fails on an upgrade that changes a float
 "floats":    [{"bits": HEX16, "repr": STR,            // IEEE 754 binary64, big-endian hex; repr(float)
                "pydantic": STR}],                      // TypeAdapter(float).dump_json(x)
 "money":     [{"microdollars": INT, "repr": STR,      // repr(float(m) / 1_000_000)
                "pydantic": STR}],                      // as the 200 writes it
 "strings":   [{"name": STR, "labels": [STR], "source": STR,   // an ASCII JSON string literal
                "ascii": STR, "unicode": STR}],        // json.dumps(s), json.dumps(s, ensure_ascii=False)
 "canonical": [{"name": STR, "labels": [STR], "source": STR,
                "sorted": STR}],                        // json.dumps(json.loads(src), sort_keys=True, separators=(",", ":"), default=str)
 "ordered":   [{"name": STR, "labels": [STR], "source": STR,
                "unicode": STR,                         // json.dumps(json.loads(src), ensure_ascii=False, separators=(",", ":"))
                "pydantic": STR}],                      // TypeAdapter(dict[str, Any]).dump_json(json.loads(src)), objects only
 "isoformat": [{"us": INT, "text": STR}]}               // (EPOCH + timedelta(microseconds=us)).isoformat() with +00:00 as Z
```

Coverage: shortest repr around every exponent switch of both formats (`1e-5`, `1e-4`, `1e16`), subnormals, `-0.0`,
integral floats, `5e-324`, `1.7976931348623157e+308`; escapes (`\"`, `\\`,
`\b`, `\f`, `\n`, `\r`, `\t`, other controls as `\u00XX`, `\u007f` kept
literal by Python, non-BMP as surrogate pairs); integers past 2^63 and
2^64; `-0` as `0`; nested objects and arrays; insertion order kept by
`ordered`. Labels Go must refuse rather than encode: `lone_surrogate`,
`duplicate_key`, `non_finite` (a literal Python decodes to infinity),
`int_over_4000_digits`, `deep_nesting` (past 128 levels).

### 10.5 `fastpath/testdata/edge_policy_v1.{body,manifest}.json` (P2)

§4. `tests/test_edge_policy_publish.py` holds staleness, the signature
refusals (wrong `typ` both ways, test key outside test mode, plane,
audience, binding, freshness), and that the body is its own canonical
encoding.

## 11. Pull requests

"Dark": merged and deployed with no behaviour change. Same wave and group
letter: disjoint files, built in parallel.

| PR | Wave | What | Needs |
|---|---|---|---|
| PR0 | 0 | This document | |
| P1 | 1a | Python authorize goldens: §10.1, §10.2 | PR0 |
| P2 | 1b | Python edge-policy publisher and route, dark: §4, §10.5; a short section in `routing-snapshot-v1.md` | PR0 |
| G1 | 1c | `fastpath/internal/pyjson`: float repr, escaping, canonical and ordered encoders, isoformat; §10.4 | PR0 |
| G1b | 2 | `pyjson.FormatFloatPydantic` and the ordered encoder's pydantic-core mode for the 200; the `pydantic` fields of §10.4 | G1 |
| G2 | 1d | Schema copies, `tr_fastpath_edge_decide`, `store/decide.go`, `fastpathctl decide`, `gwauth/rows.go`; a paragraph in `docs/runbooks/fastpath-turn-off.md` | PR0 |
| G3 | 1e | `store/gatewaykey.go` (`ReadKeyContext`, one strong single-use read mirroring `storage_gcp.py:327-350`, BYOK ids only), `ReadCreditState`, `BootRecord.DeclaresUsageHeartbeat`, `gwstate/facts.go`; §10.3 | PR0 |
| G4 | 1f | `edge/admit.go`, `Config.Admitter`, the holder set in `admission()` | PR0 |
| G5 | 2 | `gwauth` request side: §7, §8.2, the request half of §3; widen the import rule | G1, P1 |
| G6 | 2 | `gwauth` compose: §5, §6, §8.3 | G1, G1b, G2, P1 |
| G7 | 2 | `gwstate/cache.go`: 100k LRU by lookup hash, per-workspace facts, single-flight background fill, strong refresh every 5 s of entries used in the last minute, stale past 15 s, negative 5 s, eviction on Python's 401, 402, 403 or pause 503, `Evict`, `EvictWorkspace` | G3 |
| G8 | 2 | `edge/policy.go` poller, `-edge-policy-url` | P2 |
| G9 | 2 | `store/gatewayauth.go`: §8.4 | G2 |
| P3 | 2 | Python settles Go-shaped rows (test only): settle, refund, disposition, reaper, replay (same key and fingerprint replays, another body 409s), drain | P1 |
| G10 | 3 | `edge/decide.go` pipeline and full-response shadow, `-edge-decide-shadow` (false) | G4 to G9 |
| I1 | 3 | Shadow on: node attributes, `TR_EDGE_POLICY_PUBLISH_ENABLED=true`; then at least 7 days of soak | G10 |
| G11 | 4 | Decide path: admit, insert, answer; per-lookup-hash in-flight limit; `-edge-decide` (false); emulator end-to-end test | G10 |
| G12 | 4 | fastpathwatch: decisions, passes by reason, unknown outcomes, `would_admit_python_refused`; runbook; alerts | G11 |
| P4 | 4 | Optional: an `X-TR-Edge-Fastpath` hint (`busy` or `off`, any client copy stripped) so Python skips its own pilot call | G11 |
| I2 | 5 | Decide on for the pilot, node attribute `fastpath-edge-decide=true`, `docs/runbooks/fastpath-edge-decide.md`; the `decide off` first step of decision 17 in place | G12, soak |

**The shadow (G10)** counts, per snapshot and policy digest:
`agree_full` (candidates and order, estimate in microdollars and its float
bytes, `stage_d`, the decoded response equal after substituting Go's ids
for Python's and ignoring `timing`'s values (its keys and their order are
compared), the generation id by rule from Python's `authorization_id`),
`differ.<field>`, `would_admit_python_refused.<status>.<type>` (critical),
`python_answered_go_passed.<reason>`, `row.<field>` (Python's rows read
back for one request in 20, at most 5 reads a second, against Go's
would-be rows; `expires_at` as §5 says), `stale.<cache>`. A Python 401,
402 or 403, or a pause 503, for a cached lookup hash evicts the key and its
workspace.

**I2 gates:** at least 7 days of shadow; `agree_full` at least 99.5% of
cohort-eligible requests with every difference explained; zero
`differ.estimate*`, zero `row.idempotency_fingerprint`, zero
`would_admit_python_refused` beyond key-revocation lag; the stale-pass rate
recorded. `decide on` for an operator-owned workspace first, then the pilot
workspace.
