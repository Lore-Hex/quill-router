# Speculation protocol v1 — PR 1 (R)

This pure module defines a wire contract; it has no gateway, Settings, STORE,
storage, network, signing service, production call site, flag or migration.
Verification permits a bounded start only. It creates no customer credit,
authorization, settlement authority, provider execution or output permission.
Stage D's ordinary pre-header handoff remains a later integration requirement.

## Wire and trust

Compact Ed25519 JWS uses exactly three unpadded canonical base64url segments.
The protected header contains exactly `alg`, `kid`, `typ`; `alg` is `EdDSA`.
Verification signs the received header/payload segments, never reconstructed
ones. The protected header MUST be the canonical encoding of exactly
`{alg, kid, typ}`: ASCII-sorted keys, compact separators, no whitespace, escapes
or trailing newline. The byte rule below rejects escapes and nonprintable or
non-ASCII bytes as `json` before parsing. Other noncanonical encodings fail
`canonical_header` after header schema validation and before algorithm/type/key selection.
After signature verification and exact schema/type/charset validation, payload
bytes must equal sorted-key, compact JSON of the parsed claims. Canonicalization
is defined only on the valid v1 value domain: never on language-specific float
or Unicode encoder behavior. The exponent-number literal refuses as `integer`;
the raw UTF-8 non-ASCII string literal refuses as `json`.

All v1 objects have exact field sets, including nested route/history/permits.
Integer claims require a JSON integer, not boolean or float, in `[0, 2^63)`.
NaN, infinities, duplicate keys, exponent/negative-zero/noncanonical encodings
are refused. `route.stage_d` is the sole boolean grant claim and must be true.
Strings are nonempty printable ASCII U+0020–U+007E excluding double quote,
backslash, `<`, `>` and `&`. This alphabet needs neither JSON escapes nor Go
HTML escapes. Hash claims are exactly 64 lowercase hexadecimal characters.
Base64url accepts only `[A-Za-z0-9_-]`, without padding or whitespace; decoding
and re-encoding must match, including unused trailing bits. Tokens are limited
to 65,536 characters. Unknown versions, algorithms and key IDs fail closed.

Trusted keys are supplied by the caller. `kid` selects exactly one configured
key, whose purpose must match the verifier:

| Type | Required purpose |
|---|---|
| `speculation-eligibility+jws` | `grant` |
| `speculation-eligibility-shadow+jws` | `shadow-grant` |
| `speculation-descriptor+jws` | `descriptor` |

Issuer, audience, environment and plane are pinned on each grant key. Descriptor
key ID must equal the attested grant boot ID. No JWK/key in a token establishes
trust. The public fixture seeds are strictly test-only and must never appear in
a runtime trust manifest. A shadow verifier can evaluate eligibility, but its
result cannot validate a dispatch descriptor, even one referencing its exact hash.

## Pure APIs and caller responsibilities

`verify_grant` accepts compact bytes as an ASCII string, trusted keys, integer
`now`, and the current authenticated context. Context requires workspace/key,
lookup digest, boot/stable slot, region, generation, both policy epochs, image
policy version, the entire currently approved route and `tier_ceiling_micro`.
All bindings and every route field must match. The route includes endpoint,
provider/model, catalog/policy hashes, privacy, adapter/Stage-D capability,
certified token bounds, vendor rates, mandatory fees and pricing deadline.
The caller must enforce local health/allowlists, current paid provenance,
revocation and allocation state; these cannot be discovered by a pure parser.

The signed paid headroom must be at least 5,000,000 microdollars and tier must
be 2 or 3. History needs at least 20 distinct successes in a window starting no
more than 600 seconds before issuance, latest success within 30 seconds of
issuance, and a clean interval of at least 900 seconds. Sequence cannot be below
count. The issuer must deduplicate retries before signing these attestations;
this module cannot reconstruct events from a count. Missing provenance/history
is a refusal, not an assumption of eligibility.

`VerifiedGrant` and `VerifiedDescriptor` store immutable payload bytes. Their
`claims` properties return fresh copies. They are process-local validated values,
not serializable trust credentials; callers must obtain them from the verifiers.

`verify_descriptor` binds the boot signer, full compact grant hash, permit
ordinal/ceiling, workspace/key/epochs/boot, exact provider request bytes,
execution ID and existing invocation nonce, endpoint and policy. The request
hash is of the exact frozen wire bytes; do not reserialize on retry. Descriptors
may be processed after the physical-start deadline: that deadline governs the
start, not later ordinary authorize completion. The caller verifies start time
at the physical-send boundary separately.

`verify_acceptance` takes an authenticated router response, verified descriptor
and independently validated durable ordinary authorization. The response's
`authorization` mapping must match that durable mapping. A missing marker is
`ordinary` only with valid invocation/identity/billing fields. A present null,
scalar, malformed or mismatched marker is an error. Marked acceptance binds
hash, nonce, authorization ID, endpoint, policy, normal identity and ordinary
billing, and requires Stage D. Acceptance alone never opens the output gate.

`renewal_verdict` allows an exact replay without restoring permits. New grants
need a distinct ID, strictly higher generation, nondecreasing issue time,
workspace/key epochs and history sequence, and the same identity/boot/domain.
`descriptor_replay` accepts only byte-identical retries; reuse of a permit for
a different invocation conflicts. Callers retain consumed ordinals and deny
fences separately; neither helper allocates rights, reopens health nor refunds
unknown exposure. Durable/concurrent replay enforcement belongs to later PRs.

## Integer arithmetic and time

Design §3 specifies **separate** upward rounding:

`B = ceil(input_bound * input_rate / 1_000_000) + ceil(output_limit * output_rate / 1_000_000) + fees`.

The seed's integral components also yield 4608 with a combined ceiling, but the
fractional `(1,1,1,1,0)` literal yields **2**, distinguishing the formulas.
Each multiplication and total must fit signed int64. Ceil uses quotient plus a
nonzero-remainder bit, avoiding overflow from adding 999,999. Grants require
`0 < B <= permit ceiling <= per-request ceiling <= 10,000`, prompt bound at most
8192 and explicit output bound at most 512. The arithmetic helper itself can
return larger costs; grant eligibility enforces the pilot cap.

`W = min(tier_ceiling_micro // 100, paid_headroom_micro // 10, 1_000_000)`.
Allowances round down. The signed grant's total permits must fit W. The caller
must account for all other issued rights and retained losses; W is not reset by
verification, renewal, restart, expiry or cancellation.

Times are integer epoch seconds. TTL must be positive and at most 30 seconds.
Start is allowed exactly when `iat <= now < start_deadline`, where
`start_deadline = min(start_before, exp - 2, key_expires_at - 2,
price_expires_at - 2, trust_fresh_until - 2)`. Receipt time never moves this
absolute deadline. A downstream monotonic conversion must preserve the remaining
duration without positive clock-skew extension. The seed starts at +27, refuses
at +28; shortening bounds each have before/at literals.

## Authorize verdict taxonomy

`classify_verdict` accepts only normalized authenticated router denials and
resolved identities; provider responses are rejected as taxonomy inputs.
Every denial discards its own execution. Resolved key-state and verified
key-budget reasons select key scope before generic 402 handling. Workspace
billing/trust/abuse/payment/pause reasons select workspace scope even on 5xx.
Other resolved billing 402s select workspace. A 429 with explicit resolved key
scope selects key; workspace/absent/ambiguous scope selects workspace.
Unresolved credentials cannot select a workspace. Other 4xx select neither;
infrastructure/timeout/transport errors close a local key/boot breaker without
inventing a durable health latch.

For applicable real rights, a scoped denial requires a durable commit; storage
failure substitutes 503. Shadow-only traffic has no added commit dependency.
The 24 planning verdict vectors remain byte-identical. Actual gateway-error
normalization, durable latches, retry sequencing and rights applicability are
future integration work.

## Normative decoding and first-error order

Return the first failure below. Never derive field traversal order from JSON
member order, a language map iterator, or struct layout. Tables are sequential;
within a row, checks and fields run left to right. A call to a shared parser or
schema completes before the next row. A failed check stops validation.

### JSON and compact parsing

| Order | Check | Error |
|---|---|---|
| 1 | Compact input is a string of at most 65,536 characters with exactly two dots | `compact` |
| 2 | Decode protected-header segment using the base64url procedure below | `base64` |
| 3 | Apply the byte rule to the entire header | `json` |
| 4 | Header depth preflight and whole-segment syntax; then duplicates; then numeric tokens | `json`, `duplicate_key`, `integer` |
| 5 | Header exact fields; strings in order `alg kid typ` | `fields`, `string` |
| 6 | Received header bytes equal canonical encoding | `canonical_header` |
| 7 | `alg == EdDSA` | `algorithm` |
| 8 | `typ` equals the requested real/shadow/descriptor type | `type` |
| 9 | Exactly one configured key has this `kid` | `key` |
| 10 | Selected key purpose equals the requested purpose | `purpose` |
| 11 | Decode payload segment | `base64` |
| 12 | Apply the byte rule to the entire payload, before signature decoding/verification | `json` |
| 13 | Decode signature segment | `base64` |
| 14 | Decode configured public key; verify Ed25519 | `signature` |
| 15 | Payload depth preflight and whole-segment syntax; then duplicates; then numeric tokens; require object root | `json`, `duplicate_key`, `integer`, `fields` |

Within each JSON segment, precedence is base64 → byte rule (`json`) → depth
and whole syntax (`json`) → `duplicate_key` → numeric/schema/type checks →
canonical encoding → operation-specific checks. Payload signature verification
still precedes payload syntax and semantics, but follows its byte rule.

Base64url segments MUST be nonempty and match `[A-Za-z0-9_-]+`. CR and LF are
forbidden, as are padding and whitespace. Decode and unpadded re-encode; the
result MUST reproduce the received segment exactly, including unused bits.
The configured public-key encoding is checked the same way, but any failure
there is `signature`. Use pure Ed25519 without prehash or context. Invalid
public-key length, signature length, or verification fails `signature`.
Implementations MUST NOT panic.

The JSON procedure is:

1. Immediately after base64url decoding, before ANY JSON parsing, require every
   byte to lie in printable ASCII `0x20–0x7E`, excluding backslash `0x5C`.
   Otherwise return `json`. This applies to both header and payload, including
   member names, string values and whitespace. Tabs, newlines, DEL, UTF-8
   non-ASCII, invalid UTF-8, and every escape spelling fail here. Valid canonical
   tokens need no escapes and are unaffected.
2. Scan container nesting outside quoted strings. Backslashes are already
   forbidden, so there is no escape state. Root counts as one container; depth
   greater than 16 is `json`. This bounded preflight precedes parsing.
3. Validate the WHOLE segment's syntax before any value-level refusal. The first
   pass retains raw numeric token text without integer/float conversion. Object
   hooks record duplicates without raising. Malformed numbers (`1e`, `1.0e`,
   `-`, `01`, `1.`, `.5`), missing delimiters and trailing input return `json`,
   even after a duplicate object, huge integer, float or constant. As in prior
   versions of this contract, the tokenizer recognizes `NaN`, `Infinity`, and
   `-Infinity` as complete constant tokens, deferring their refusal to step 5;
   this does not allow malformed syntax elsewhere in the segment.
4. After successful whole-segment syntax validation, reject duplicate names at
   any depth as `duplicate_key`. Names are escape-free ASCII, so their decoded
   and literal identities agree. Duplicates precede all numeric/schema checks.
5. Validate numeric tokens from left to right: fractional/exponent forms and
   the three constants above fail `integer`. Integers have at most 19 digits
   excluding a minus sign and value in `[0, 9223372036854775807]`. Check token
   length BEFORE integer conversion, independent of interpreter string limits.
   Negative values fail `integer`; `-0` becomes zero and later fails canonical
   equality. Never convert numbers to float64.
6. Schema validation follows. Names are case-sensitive; unknown and missing
   fields fail `fields`. Ignored fields and zero defaults are forbidden.
   Booleans, nulls, strings and other nonintegers in integer slots fail `integer`.
   A boolean never equals an integer or floating value.

Decoded strings must be nonempty and in the alphabet above. Escaped ASCII and
surrogate names/values have already failed the byte rule as `json`. External
caller strings still undergo the same string charset checks and fail `string`.
After schema/type/charset validation, bytes MUST equal recursively
ASCII-key-sorted compact JSON: preserve array order, decimal integers and
lowercase booleans. No escapes or trailing newline are possible.

Caller-supplied values (bindings, route, authorization) MUST be JSON trees.
A caller value in which any container (object or array) is reachable more than once, whether by sharing or by a cycle, is not a JSON tree and never compares equal. Empty containers carry no identity for this rule (they have no children, and Go's decoder gives every empty array one shared address), so a shared empty object or array compares by value.

Comparisons are pure boolean, type-sensitive, depth-unbounded and independent
of member order. They never produce an error code of their own. Implementations
MUST use one iterative lockstep walk with a separate visited-container identity
set for each input. The first repeated container in either input returns false.
Otherwise compare exact scalar types and values, object key sets and their
corresponding values, and array lengths and elements in order. Each container
is expanded at most once, giving O(size of the two inputs) work and space;
there is no comparison-depth limit. Sharing between the two inputs is allowed
when each input individually is a tree.

External bindings use one structural, type-sensitive equality: booleans,
integers and floating values are distinct; objects have equal keys and
recursively equal values; arrays have equal length and element order. Current
context route is schema-validated before route equality. External data has the
JSON value domain; malformed caller types or invalid process-local verified
objects that otherwise cause runtime errors refuse `input`. This fallback
never replaces a more specific check already reached. Public APIs must return
normally or raise `ProtocolError`, never leak parser/runtime exceptions for
malformed data. Resource exhaustion of the host and arbitrary executable
Python objects are outside the wire contract.

### Schema field traversal

Every object first checks its exact field set (`fields`), then ALL strings in
the listed order (`string`), then ALL integers in the listed order (`integer`),
then the nested checks. Hash syntax is checked later, where specified.

| Object | String order | Integer order | Nested order |
|---|---|---|---|
| Header | `alg kid typ` | — | — |
| Grant | `iss aud environment plane workspace_id key_id lookup_digest boot_id stable_slot_id region grant_id` | `v generation workspace_epoch key_epoch image_policy_version tier paid_headroom_micro iat exp start_before key_expires_at trust_fresh_until per_request_ceiling_micro` | route, history, permits |
| Route (signed and context) | `endpoint_id provider upstream_model region routing_policy_hash catalog_hash privacy input_bound_method` | `adapter_capability_version input_bound output_limit input_rate_micro_per_m output_rate_micro_per_m maximum_request_fees_micro price_expires_at` | `stage_d` must be boolean (`stage_d`) |
| History | — | `clean_since count last_success_at sequence window_start` | — |
| Permit | — | `ordinal b_micro` | — |
| Descriptor | `grant_id grant_sha256 execution_id invocation_nonce request_sha256 routing_policy_hash endpoint_id workspace_id key_id boot_id` | `v ordinal b_micro workspace_epoch key_epoch` | — |
| Acceptance marker | `descriptor_sha256 invocation_nonce authorization_id endpoint_id routing_policy_hash` | `v` | — |

Grant permits must be a nonempty array (`permits`); schema-check each element
in array order. The ordered schema field names also define each exact field
set, together with the named nested fields. The entire grant schema finishes
before canonical, version or semantic checks.

### Grant validation

| Order | Check | Error |
|---|---|---|
| 1 | `now` is bounded integer | `integer` |
| 2 | Shared compact/header/signature/payload procedure | as above |
| 3 | Entire grant schema | as above |
| 4 | Canonical payload | `canonical_payload` |
| 5 | `v == 1` | `version` |
| 6 | Issuer-key pins in order `iss aud environment plane` | `identity` |
| 7 | Lookup digest lowercase SHA-256 syntax | `hash` |
| 8 | Context contains and exactly matches `workspace_id key_id lookup_digest boot_id stable_slot_id region generation workspace_epoch key_epoch image_policy_version` | `binding` |
| 9 | Route hashes in order `routing_policy_hash catalog_hash` | `hash` |
| 10 | Signed route Stage D is true | `stage_d` |
| 11 | Entire context-route schema | `fields`, `string`, `integer`, `stage_d` |
| 12 | Exact route equality, then route region equals grant region | `route` |
| 13 | Positive input bound ≤8192, then positive output limit ≤512 | `token_bound` |
| 14 | Adapter version positive | `adapter` |
| 15 | Tier is 2 or 3 | `tier` |
| 16 | Paid headroom ≥5,000,000 | `paid_headroom` |
| 17 | History count ≥20, sequence ≥count | `history_count` |
| 18 | Window start ≥iat−600; window start ≤last success ≤iat; last success ≥iat−30; clean since ≤iat−900 | `history_time` |
| 19 | `0 < exp−iat ≤30`, then `iat < start_before` | `lifetime` |
| 20 | `iat ≤ now < min(start_before, exp−2, key_expiry−2, price_expiry−2, trust_fresh_until−2)` | `start_window` |
| 21 | `0 < per_request_ceiling ≤10000` | `ceiling` |
| 22 | Cost arguments in signature order; input product/total then output product/total fit int64 | `integer`, `overflow` |
| 23 | `0 < B ≤ per_request_ceiling` | `cost` |
| 24 | Each permit in array order: ordinal unique, then `B ≤ b_micro ≤ ceiling` | `ordinal`, `permit_cost` |
| 25 | Context contains trusted tier ceiling | `tier_ceiling` |
| 26 | Tier ceiling then headroom are bounded integers; calculate W | `integer` |
| 27 | SUM of permit ceilings ≤W | `allowance` |

The arithmetic helpers validate arguments in function signature order. B checks
product then accumulated total for each component, input before output. W uses
integer floors and the independent $1 cap. No frozen money, tier, headroom,
history or lifetime semantics changed in round 2.

### Descriptor validation

| Order | Check | Error |
|---|---|---|
| 1 | Verified grant is real | `dry_run_cannot_dispatch` |
| 2 | Shared compact/header/signature/payload procedure | as above |
| 3 | Descriptor schema, canonical payload, version | schema codes, `canonical_payload`, `version` |
| 4 | Hash syntax in order `grant_sha256 request_sha256 routing_policy_hash` | `hash` |
| 5 | Signer kid equals grant boot | `descriptor_boot` |
| 6 | Exact `grant_id workspace_id key_id boot_id workspace_epoch key_epoch` | `descriptor_binding` |
| 7 | Hash of exact compact grant bytes | `grant_hash` |
| 8 | Hash of exact request bytes | `request_hash` |
| 9 | Execution ID, then invocation nonce | `invocation` |
| 10 | Endpoint, then routing policy hash | `descriptor_route` |
| 11 | One allocated permit matches ordinal AND cost | `descriptor_permit` |

### Acceptance validation

| Order | Check | Error/result |
|---|---|---|
| 1 | Structural typed equality of response authorization and independently supplied authorization | `authorization` |
| 2 | Authorization matches descriptor `invocation_nonce workspace_id key_id` | `authorization` |
| 3 | Authorization ID string | `string` |
| 4 | Ordinary billing mode | `authorization` |
| 5 | Marker key absent | return `ordinary` |
| 6 | Marker schema, then version | schema codes, `version` |
| 7 | Hash syntax `descriptor_sha256 routing_policy_hash` | `hash` |
| 8 | Hash of exact compact descriptor | `descriptor_hash` |
| 9 | Marker equals descriptor `invocation_nonce endpoint_id routing_policy_hash` | `marker_binding` |
| 10 | Authorization matches marker (or descriptor when field absent from marker) in order `authorization_id invocation_nonce endpoint_id routing_policy_hash workspace_id key_id` | `authorization` |
| 11 | Ordinary billing and Stage D exactly true | `authorization` |
| 12 | All checks passed | return `accepted` |

Missing markers do not bypass steps 1–4. Marker null/scalar/empty object fails
schema; it is not absence. Acceptance does not grant output or billing authority.

### Renewal and descriptor replay

| Order | Check | Error/result |
|---|---|---|
| 1 | Exact previous/candidate compact grant equality | return `replay` |
| 2 | Same real/shadow domain | `renewal` |
| 3 | Exact identity in order `workspace_id key_id lookup_digest boot_id stable_slot_id iss aud environment plane region` | `renewal` |
| 4 | Distinct grant ID, higher generation, nondecreasing iat, workspace epoch, key epoch, history sequence (in that order) | `renewal` |
| 5 | All checks passed | return `renewed` |

Inputs are already verified grants. Exact replay changes no rights. Descriptor
replay has one check: byte-identical compact descriptor → `replay`, otherwise
`replay_conflict`. The fixtures verify both grants before exercising renewal;
identity-change fixtures independently match the candidate context and issuer
pins, so no earlier verification error masks renewal.

### Verdict classification and normalization

The classifier consumes one normalized reason, never a list of simultaneous
reasons. Upstream normalization MUST choose a resolved workspace state reason
before a resolved key state reason, then rate limiting, then infrastructure or
request-only reasons. Generic status and `rate_scope` are secondary hints,
not an additional authenticated state reason. For example, key revoked +
workspace rate scope on 429 is key; billing paused + key rate scope is workspace.
If two authenticated state reasons apply, the caller passes the workspace
reason. The caller must not disguise the workspace state as a generic status.

| Order | Check/decision | Error/result |
|---|---|---|
| 1 | Source equals `authenticated_router` | `verdict_source` |
| 2 | Status is bounded integer, then 400–599 inclusive | `integer`, `verdict_status` |
| 3 | Key reason with resolved workspace AND key | key scope |
| 4 | Resolved workspace and workspace reason OR generic 402 | workspace scope |
| 5 | 429 and resolved workspace: explicit key rate scope AND resolved key | key scope; otherwise workspace |
| 6 | Otherwise | no durable scope |
| 7 | Only with no durable scope: status ≥500 OR reason `authorize_timeout`, `transport_error`, `infrastructure_error` | local key/boot breaker; otherwise none |

All outputs discard the current execution. Any durable scope requires commit
for real rights and substitutes 503 on storage failure; no scope preserves
status. Shadow-only commit requirement is always false. Unresolved identities
cannot select their missing scope. State reasons override generic 5xx and
rate-scope hints. Conflicting-hint literals pin each direction of this rule.

## Go implementation notes

These standard-library pitfalls were verified in review; PR 2 must preserve
the contract rather than relying on library defaults:

| Pitfall | Required implementation / fixture evidence |
|---|---|
| `encoding/json` interface numbers default to float64 | Use `Decoder.UseNumber` or `Decoder.Token` to retain raw number text before validation; validate whole syntax before bounded integer conversion; `overflow_integer`, `payload_number_huge`, `float_integer`, `exponent`, `context_coercion_input_rate_micro_per_m` |
| Struct field matching is case-insensitive | Compare decoded exact names; `case_sensitive` |
| Unknown fields are ignored | Exact field sets, including nested objects; `unknown_field`, `unknown_route_field`, `unknown_permit_field` |
| Duplicate keys are accepted | Use a token-level pass (`json.Decoder.Token`) with a name set per object; record duplicates and defer refusal until whole syntax succeeds, before numeric checks; `header_duplicate`, `nested_duplicate`, parser precedence literals |
| Struct serialization is not automatically sorted; `Encoder.Encode` appends newline | Use explicit canonical encoding; `header_reordered`, `header_whitespace`, `payload_unsorted`; `header_escaped` and `trailing_newline` now fail the byte rule |
| Invalid UTF-8 and unpaired surrogate escapes are replaced | The byte rule makes UTF-8, escape and surrogate handling unreachable in `encoding/json`; `invalid_utf8`, `header_escaped`, `header_surrogate_names`, `payload_surrogate_names`, `surrogate` |
| NaN syntax is normally a parser error | Recognize these raw constant tokens without raising, validate the entire segment, then duplicates, then map constants to `integer`; `nan`, `header_number_infinity`, `payload_number_minus_infinity` |
| `RawURLEncoding.Strict()` still ignores CR/LF | Alphabet precheck before decode; `base64_cr`, `base64_lf`, `signature_padded`, `base64_trailing_bits` |
| `ed25519.Verify` panics on wrong public-key length | Explicit size validation; `key_short_public`, `signature_bitflip` |
| Decoder nesting limits differ | Root-counted 16-container preflight; `depth16`, `depth17`, `depth_in_string`, `depth_siblings` |
| Caller comparisons inherit signed-JSON depth limits or map traversal errors | Never compare caller values with a depth cutoff or by map iteration that can short-circuit into different codes. Use an explicit stack and separate per-input visited-container identity sets; any repeated container (sharing or cycle) compares false; `acceptance_depth_130_*`, `acceptance_depth_300_*`. The signed-JSON depth-16 rule does not apply to caller comparisons. |

## Frozen fixtures and mutation evidence

The fixture signer `_generate.py` never imports `trusted_router`; expected
verdicts, costs and boundaries are literal authoring inputs. Tests never import
or run it or `_inventory.py`. The latter inspects source only to locate mutation
edits, never to calculate expectations. Regeneration is a deliberate offline
step and changes the manifest pin. Planning grant claims, provider-wire bytes,
and the original 24 verdict vectors remain unchanged.

The guard inventory combines frozen `rules.json` entries outside `_equal` with
`tests/speculation_equality_rules.json`, which replaces the retired pair-based
comparator edits without changing any fixture bytes. Each entry names a concrete
source edit, function, and selected literal or Python-only case. `_mutate.py` runs the WHOLE inventory against
disposable module/fixture copies and first confirms the unmodified harness.
Compile/import failures count as build-broken, not red. A mutant is red only
when a corpus test fails; all corpus tests run even after a failure. Only a
whole-corpus pass can survive. The structural inventory-completeness test uses
the original module, since deliberately changing a predicate must not count as
behavioral evidence. Every other test uses the mutant. The test guard checks every require
site and branch has an executable inventory entry; atomic comparisons, reason
memberships, binding fields and monetary operations have additional entries.
The complete run table is in [speculation-protocol-v1-mutations.md](speculation-protocol-v1-mutations.md).

Protocol fuzzing exercises every public API parameter, nested/cyclic external
values, signed arbitrary parser bytes, huge numbers and deep JSON. Integer-limit
checks run with Python limits 640, 4300 and disabled. These assert crash freedom;
they do not infer expected verdicts for the frozen bundle. A separate test
continues to prohibit production imports/call sites.
