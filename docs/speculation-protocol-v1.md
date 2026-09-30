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
ones. Header JSON need not be sorted, but duplicate/extra/missing fields fail.
After signature verification and exact schema/type/charset validation, payload
bytes must equal sorted-key, compact JSON of the parsed claims. Canonicalization
is defined only on the valid v1 value domain: never on language-specific float
or Unicode encoder behavior. The exponent-number literal refuses as `integer`;
the raw UTF-8 non-ASCII string literal refuses as `string`.

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

## Frozen fixtures and review notes

`tests/fixtures/speculation_v1/_generate.py` contains fixed public test-only
Ed25519 seeds, literal claims, literal expectations and the original verdict
and provider-wire bytes. It imports no production module. Tests never execute
or import it. Run it manually only for a deliberate two-repository contract
update. `manifest.json` pins all wire/vector/rule files; the test hard-codes
its SHA-256. PR 2 must copy the exact final JSON bytes, not independently mint
an equivalent bundle. Provider-wire file's single final LF is excluded from
the request hash.

The planning seed manifest was
`a76e70236e2c97e83eaf2ca4a24d1d7633a6a8aa1b7eb91bb1444b0c22b36362`.
The final manifest, precise seed differences, literal counts, mutation evidence
and gate output are recorded below after verification. No git writes or deploy
are part of this PR. `grep -rn speculation_protocol src/` must show only the
module itself.

### Final fixture pin and seed byte differences

Final manifest SHA-256:
`cf4961055cc4d7944a16b6160860b34fab05d4c8966ce3b19cfd041a601da648`.

| File | Seed bytes | Final bytes | Byte comparison |
|---|---:|---:|---|
| provider-wire.json | 105 | 105 | Identical; SHA `5ad0733472da01485383c34554a027bf1e35876888b4c94c48469f51d3a95cd8` |
| verdict-vectors.json | 13621 | 13621 | Identical; SHA `f1cb95a0dafaad18da7e7633ddd2b38e6cbd5526ff1320e9b639e8eef927db3f` |
| grant-permit-tokens.json | 8416 | 10221 | SHA changed from `fa8e67dd1278ba569586a3b97d551cc31c755c64ff13da643459ef9d4146fad5` to `0bc017b4ad0e55ad8bc13fcd61967a9ce36d942d0dd4e5cdaedeeaeb8e5f48db` |
| manifest.json | 334 | 517 | Token-file pin replaced; protocol-vectors.json and rules.json pins added |

All `grant_claims` and all seed `expected` values are identical. The real grant's
99-byte header segment and 1763-byte payload segment are unchanged; its 86-byte
signature changes. The shadow's 108-byte header changes `kid` from
`issuer-fixture` to `shadow-fixture`; its 1763-byte payload is unchanged and its
86-byte signature changes. The descriptor's 87-byte header is unchanged; its
608-byte payload changes only `grant_sha256`, and its 86-byte signature changes.
The outer JSON repeats these compact bytes and updated descriptor claims.

The dependent compact-grant hash changes from
`39578719b2dd0a0e3928f4dd7326f7a7f3e7368df5d22463a864a4fe83364ee7` to
`ebace849f7d56f32eafee693310ba9cda13ab17f571b950e476aaeee13389055`.
The accepted marker's compact-descriptor hash changes from
`58204c390d5a2684a72c09648857abfdaaa9a52cb4cf2e12bde02c2580c26d77` to
`846aec09fd7b9e7e581a6b017e451a2e33ac941d7f4f3cd23f211beacaf5a8e5`.
The request hash remains
`75e9f95d9e202c66fcffb7d4c7aa7c88e3dfb80a51e2cddc7539a6e461188f88`.

The trusted issuer and boot public keys are replaced to match the recorded
seeds; `shadow-fixture` (`shadow-grant`) and `boot-other` (`descriptor`, negative
binding tests) are added. The bundle also adds literal `context` and
`authorization` inputs. These are the only added top-level fields. Outer JSON
remains sorted, two-space-indented UTF-8 with one final LF. New files are the
protocol vectors, guard rules and the two standalone maintainer scripts.

| Literal category | Count |
|---|---:|
| Grant / strict parser / context / deadline | 150 |
| Descriptor | 24 |
| Acceptance marker / ordinary authorization | 24 |
| Renewal ordering | 12 |
| Cost arithmetic | 11 |
| Allowance arithmetic | 6 |
| Descriptor replay | 2 |
| Additional taxonomy refusals | 2 |
| Unchanged planning verdicts | 24 |
| **Total independently expected vectors** | **255** |

Four additional tests check manifest/file pins, the complete seed contract,
rule coverage and absence of runtime call sites: **259 tests** total.

### Mutation gate

`_mutate.py` makes a temporary copy of the module, tests and fixture bundle for
each run; it never checks out or modifies worktree code. All temporary copies
are removed. An initial run timed out during pytest startup; the final run
disables unrelated plugin autoload for these pure tests. All 12 mutants are red,
none survived and none is build-broken.

| Mutant | Named test | Result | Failing assertion text |
|---|---|---|---|
| dry-run accepted as real | `test_literal[dry_run_cannot_dispatch]` | red | `E       AssertionError: dry_run_cannot_dispatch: expected 'dry_run_cannot_dispatch', got 'allowed'` |
| ignore key epoch | `test_literal[binding_key_epoch]` | red | `E       AssertionError: binding_key_epoch: expected 'binding', got 'allowed'` |
| ignore boot binding | `test_literal[binding_boot_id]` | red | `E       AssertionError: binding_boot_id: expected 'binding', got 'allowed'` |
| ignore route binding | `test_literal[route_endpoint_id]` | red | `E       AssertionError: route_endpoint_id: expected 'route', got 'allowed'` |
| classify every 402 as workspace | `test_verdict[lifetime_limit]` | red | `E           AssertionError: lifetime_limit.durable_scope: expected 'key', got 'workspace'` |
| classify every 429 as key | `test_verdict[rate_unknown]` | red | `E           AssertionError: rate_unknown.durable_scope: expected 'workspace', got 'key'` |
| round B down | `test_literal[money_fractional]` | red | `E       AssertionError: money_fractional: expected 2, got 0` |
| accept shadow-purpose key for real grant | `test_literal[real_type_shadow_key]` | red | `E       AssertionError: real_type_shadow_key: expected 'purpose', got 'allowed'` |
| skip canonical-payload check | `test_literal[payload_whitespace]` | red | `E       AssertionError: payload_whitespace: expected 'canonical_payload', got 'allowed'` |
| accept padded base64 | `test_literal[signature_padded]` | red | `E       AssertionError: signature_padded: expected 'base64', got 'allowed'` |
| allow bool as int | `test_literal[bool_integer]` | red | `E       AssertionError: bool_integer: expected 'integer', got 'allowed'` |
| change one fixture byte | `test_fixture_pins` | red | `E           AssertionError: fixture pin mismatch: provider-wire.json` |
