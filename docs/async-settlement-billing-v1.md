# Frozen billing v1 (PR 1)

This is a dormant contract. No existing route, schema, authorization, reserve,
settle, refund or outbox implementation imports it. No activation flag is needed
in this PR. Nothing is deployed or wired into responses by this change.

## Scope and caller responsibilities

`build_snapshot(effective_endpoints, requested)` freezes detached customer
prices. Its inputs must be the effective endpoint records at authorization time,
including ordinary catalog markup already embedded in the rates. It resolves
missing cache rates using today's provider defaults; explicit zero stays zero.
`evaluate(snapshot, selected_endpoint, usage, observed)` has no catalog reads and
checks observed exclusions again. Both phases must populate the feature facts;
these pure types do not discover request features or authenticate callers.

The first adapter allowlist is **OpenAI and Anthropic**. Both have final-usage
normalization and real-settlement differential cases. Chat Completions and
Responses, streaming and non-streaming, are supported. Cache read/write,
reasoning subsets and ordinary context tiers are included. Other adapters must
add normalization differential evidence before joining the allowlist.

Eligibility requires typed, local, ordinary Credits. Requested or observed app
or custom markup, receipt/request fees, custom/user models, tool/search/image/
video costs, service-tier pricing (anything besides absent/default), partner or
Liberty pricing, native batch, fusion/Polyphemus, or private tier bases are
excluded. BYOK/non-Credits, spend/regional leases, federated/deferred-home
settlement, unsupported routes/endpoints, unknown price programs, malformed
usage and overflow fail closed. Unknown feature fields are rejected. An
excluded request must use the existing synchronous path in a later integration
PR; this module does not change that path now.

There is deliberately no estimate/hold/cap input to the evaluator. Trust-tier
handoff caps concern admission, not a clamp on an ordinary customer's charge.
The snapshot's `charge_cap` is explicitly null. Journal admission and caps are
later PRs.

## Arithmetic and the tier decision

Every component is independently rounded half-up:

```
(tokens * rate_micro_per_million + 500000) // 1000000
```

Sum uncached input, cached read, cache creation and output. The request-fee
field is retained but v1 requires zero. Apply the one-micro minimum only when
positive usage has a positive effective rate (or a positive fee in a future
version). A reasoning subset is informational and never added to output.
OpenAI prompt totals include cache subsets; Anthropic input excludes them.
Cache subsets greater than an inclusive prompt or reasoning greater than
output are malformed, rather than silently clamped.

Tier selection uses total prompt including cache, inclusive upper boundaries,
and **the last tier if none matches**, even if that last tier has a finite
boundary. Empty tiers use base rates. Boundaries must increase and an unbounded
tier must be last.

This matches the **committed router charge**, not the old Go helper's base-rate
fallback:

1. `routes/internal/gateway.py::_settle_gateway_authorization` normalizes usage
   with `services/settle_outbox_apply.py::normalized_prompt_accounting` and calls
   `_endpoint_cost_microdollars` for ordinary Credits.
2. `_endpoint_cost_microdollars` builds the Stage D price document and calls
   `_endpoint_cost_microdollars_from_document`.
3. `stage_d.py::endpoint_cost_microdollars_from_candidate` uses
   `tiers[-1].get("rates")` when no tier matches.
4. The gateway passes that amount to typed finalize and stores the same
   `actual_cost_micro` in `SettleOutboxRow`. `apply_frozen_settle` replays that
   amount without repricing.

The `tier_last_fallback` literal is 21 tokens at the last tier's 2,000,000 rate
with finite boundaries 10/20 and a different base rate: **42 microdollars**.
The differential test asserts 42 in the actual typed reservation, credit and
key counters, and generation after both inline and recovered settlement.

## Wire and hash contract

All models are frozen; nested collections are tuples and nested records are
also frozen. Extra fields, unknown versions/semantics, floats/strings/bools as
integer counts or rates, negatives and out-of-range integers are rejected.
Identifiers are bounded ASCII; the envelope has only typed, bounded accounting
metadata, never arbitrary metadata, prompts, completions or credentials.

All monetary/count values range from 0 through **9,223,372,036,854,775,807**.
Normalization sums, token-rate products, the addition of 500,000, and component
sums must also fit signed int64. Overflow is an explicit error even when Python
could evaluate the expression. Go must use checked operations and decode JSON
integers directly into integer types. The maximum safe rounding-numerator
literal evaluates `(9223372036854275807 + 500000) // 1000000` to
**9,223,372,036,854**. Zero usage may carry maximum rates and zero rates may carry
maximum usage.

Canonical bytes are compact ASCII JSON, lexically sorted object keys, preserved
array order, all declared default fields included, no whitespace/newline,
integer decimal notation, and explicit nulls. Candidates are unique and sorted
by ASCII endpoint ID. SHA-256 is lowercase hex. Hashes are returned separately
by `canonical_hash`; there is no self-referential hash field inside a snapshot
or envelope. A wire response can attach the snapshot hash beside the canonical
payload. Duplicate JSON keys are rejected by `parse_snapshot`.

The terminal envelope binds authorization, prospective generation, workspace,
key, invocation nonce, local billing authority, journal region/epoch, selected
endpoint, snapshot version/hash, normalized usage, exact charge, terminal kind,
route and streaming status. `validate_envelope` verifies price/hash consistency;
it does **not** replace ticket authentication or persisted-authorization
identity checks, which belong to later PRs. A refund must carry zero charge.
Accepted/duplicate outcomes carry a payload hash and `settlement_status=pending`;
rejections cannot claim durable acceptance. Pending never means committed.

## Fixture and executable evidence

`tests/fixtures/async_settlement/billing_v1.json` is the versioned shared fixture.
Its adjacent `.schema.json` describes the harness, including intentionally
invalid snapshot/usage/context objects and the allowed error codes. `FixtureSchema`
validates it and pins its schema;
the production models validate positive wire payloads. The fixture byte SHA-256
is pinned in `tests/test_billing_snapshot.py` (727 cases):
`a748ef09cfbd6bdfb2f84fb0b4a05af7030e6a1a6c69e2a54cf20a096bfcef4b`.
All amounts and normalized usage
are literal test data; the evaluator never generates expectations. Envelope
hash literals were produced from those literal payloads with standard JSON and
SHA-256, independently of the billing module.

Harness validation codes are `invalid_snapshot` (snapshot parsing/validation),
`invalid_usage` (raw usage validation), and `invalid_context` (requested or
observed eligibility validation, before build/evaluation). These are fixture
categories, not new evaluator exceptions. The tier rejection vectors use
`invalid_snapshot` with valid rates and boundaries `[null, 1]`, `[10, 10]`, and
`[20, 10]`. Both `invalid_context` vectors contain `new_unhandled_fee: 1000000`;
eligibility validation runs inside their expected-error handling. Remaining
exclusion codes match the existing eligibility/evaluation error reasons.
The fixture file uses deterministic two-space-indented ASCII JSON with a final
newline; this readable file format is distinct from canonical wire bytes above.

Copy the exact JSON bytes into the enclave testdata in PR 2 and pin both repo
revisions in the cross-repository gate then. This router-only PR does not claim
Go parity or install a cross-repository gate before the Go consumer exists.

The differential test uses the existing fake Spanner store and actual gateway
settle, typed finalize, counters, generation and outbox drain. Only catalog
inputs and a one-shot finalize crash are replaced. All 28 original positive evaluation goldens run
through synchronous settlement, inline outbox settlement and outbox recovery;
all assert the committed amount and retry idempotency. Catalog changes/removal
are exercised after durable enqueue, when today's outbox already promises
frozen pricing. Local evaluation remains unchanged after catalog mutation.
Using billing v1 for a snapshot-bearing synchronous fallback after a catalog
change is explicitly later route wiring, not claimed implemented in PR 1.

The mutation tests compile modified source only into module memory and restore
its dictionary in `finally`. The rule manifest is
`tests/fixtures/async_settlement/billing_v1.rules.json`; it records exact scoped
source edits and selected literal names. Controls run the selected vectors
against the baseline, require every selected vector to fail under mutation,
then rerun them after restoration. A different validation error does not count
as a kill for model-validation controls. Exact-reason controls also detect the
wrong error code; positive controls detect incorrect charges or rejections.
These are **selected-vector counts**, not claims that unrelated vectors are
unaffected by a shared-type mutation.

## Fixture operations and coverage boundaries

The original 717 case objects remain byte-identical, in their original order.
Tests reconstruct the Round 2 and Round 3 files and verify both original hashes.
The appended cases use explicit operations:

| Operation | Literal input and assertion |
|---|---|
| Original evaluation case (no `operation`) | Existing snapshot, usage, eligibility, literal charge/normalization/hash. Requested exclusions now build with a valid endpoint so empty candidates cannot mask a missing eligibility check. |
| `snapshot_json` | Raw JSON text; duplicate root, candidate, rate and tier keys survive decoding. Baseline object, rates and tiers are otherwise valid. |
| `model` | A complete input for the named DTO; one invalid field, omitted required field, extra field, or violated invariant. |
| `envelope` | Complete literal terminal envelope plus literal snapshot; validates DTO and binding. Includes positive settle/refund hashes, wrong hash/charge/endpoint and evaluator-result comparison. |
| `acceptance` | Complete literal outcome; all five statuses, every durable missing-field combination, and rejection hash-only, pending-only and both. |
| `type` | Named public primitive (`UInt`, `Identity`, `Digest`, `SettlementMode`), with strictness, limits and positive boundaries. |
| `field` | The **actual declared field schema** on a named DTO, before cross-field validators. In Python this is `model_fields[field].rebuild_annotation()`, not a duplicated test-only definition. |
| `builder` | Literal candidate and optional endpoint overrides converted to the existing endpoint dataclass without coercion, then frozen. Tests pre-conversion validation and nullable tier inputs. |
| `checked` | Literal intermediate integer at the public checked-arithmetic boundary. |

PR 2 must implement every operation, including field-schema projections and
builder pre-conversion checks. Running just the original evaluation loop is
insufficient. `invalid_snapshot`, `invalid_context`, `invalid_usage`,
`invalid_envelope`, `invalid_acceptance`, `invalid_evaluation`, `invalid_type`,
and `invalid_builder` are harness validation categories. Binding/arithmetic
errors assert exact `snapshot_hash_mismatch`, `charge_mismatch`,
`unsupported_endpoint`, or `arithmetic_overflow` reasons. They do not change
production exception types. Round 4 `string_type` expectations assert exactly one
validation error at the named field (or the field-schema root), rejecting numeric
identifiers and digests. Expectations are literal; positive hashes use
standard sorted compact ASCII JSON + SHA-256, not evaluator-generated results.

The explicit wire limits are **1–64 candidates**, **0–64 tiers per candidate**,
**1–512 identifier characters**, **1–64 nonce characters**, **64 lowercase hex
digest characters**, and **0–2^63−1 integer counts/rates/amounts**. Integer
versions are exactly integer `1`, excluding boolean and floating-point `1`.
Identity and nonce alphabets are the source regexes; newline, Unicode and
out-of-alphabet characters are rejected. Positive vectors include every
numeric endpoint, both string lengths, 64 candidates, 64 tiers, null tier
boundaries, both settlement modes, both providers and all acceptance statuses.

### Overlapping checks and non-JSON behavior

A single syntactic deletion is not always an independently observable semantic
mutation. The audit explicitly records these cases:

| Source rule / overlap | Fixture evidence | Mutation control |
|---|---|---|
| `Identity` / nonce minimum length is also enforced by regex `+` | Empty rejection plus length-1 acceptance | Corresponding minimum-length control removes `min_length` **and** changes regex `+` to `*`; only empty-string behavior changes, alphabet and maximum remain. |
| Candidate prompt-convention Literal is implied by provider agreement | Field `candidate_prompt_convention_unknown`; both provider disagreement model inputs | `candidate_prompt_convention_literal` independently tests the declared enum; `provider_prompt_agreement` tests the relationship. |
| Acceptance pending Literal is implied by durable/rejection validators | `acceptanceoutcome_settlement_status_unknown` field input; complete durable/rejection inputs | `acceptanceoutcome_settlement_status_literal`, `acceptance_durable`, `acceptance_rejection`. |
| Rejected outcome with both hash and pending is rejected by **both** conditions | Three `acceptance_*_both` cases | `acceptance_rejection_both` removes only this rejected combination from both overlapping guards; partial acknowledgement and durable checks remain active. |
| Candidate request-fee bounds are implied by its zero-fee rule; model-ID minimum length and string type overlap the provider-prefix validator | Field-schema boundary vectors plus `candidate_model_id_string_type`; `nonzero_endpoint_fee`, `model_provider_prefix` | Field lower/upper/nonempty/string-type controls isolate declarations without weakening the complete-object rules. |
| `NormalizedUsage` total bounds / sum-overflow guard can be implied by nonnegative component bounds and equality | Every field lower/upper vector; `normalized_sum_overflow`; direct checked bounds | Field-schema controls isolate each declaration; `checked_overflow` isolates the helper domain; `normalized_total` isolates equality. `normalized_native_overflow` uses `normalized_sum_wraparound`: MAX + MAX + 2 wraps to zero in int64, matching the otherwise-valid declared total. It is rejected before wrapping. A plain Python check deletion remains redundant with mathematical equality. |
| Checked product overflow is also caught by the immediately following checked rounding numerator | `multiply_overflow`, `multiply_wraparound`, `rounding_add_overflow`, checked bounds | `multiplication_native_overflow` simulates omission with native signed-int64 wrapping; deleting only `checked()` in arbitrary-precision Python is equivalent because the next guard still catches it. `overflow_rounding` independently kills removal of the rounding-offset guard. |
| Checked cost-sum overflow cannot occur with four already-checked int64 products rounded per million | Shared checked bounds; component rounding literals | `checked_overflow` / `checked_negative` prove helper domain; `aggregate_rounding` proves independent rounding. No unreachable sum-overflow wire witness is claimed. |
| Cached and Anthropic prompt sums have observable exact overflow reasons | `cached_sum_overflow`, `normalization_overflow` | `cached_sum_guard`, `prompt_sum_guard` fail when downstream validation changes the error category. |
| `validate_envelope` usage comparison is algebraically redundant for valid DTOs with the current evaluator | `envelope_evaluated_usage_mismatch` with valid literal envelope and explicit `evaluated_usage` test input | `envelope_usage_match` deletes only the usage comparison. The harness injects a valid but different evaluation result at the evaluator boundary. This is an explicit **fault-injection** vector, not a wire metadata field or an otherwise reachable malformed envelope. |
| `Frozen.validate_default=True` has no invalid default in v1 | All omitted optional fields/defaults exercised by fixtures | No deletion kill is possible with current valid defaults; field schemas and default-positive cases cover the effective contract. |
| `integer_versions`: dict/presence guards; parser delegates JSON syntax/object decoding to JSON/Pydantic | Raw malformed syntax, array/null/empty object; version bool/float and required-field cases | Typed-version and required-field controls cover semantic rejection. Guarding access to absent fields is implementation plumbing, not another rejection rule. |
| Frozen objects and detached/copying semantics | Python assignment tests for nested rates and envelope usage; builder detachment/sorting test | `frozen_assignment`: red 2/2 → green 2/2. **`frozen=True` cannot be expressed as JSON. PR 2 needs its own Go copy/immutability/aliasing test outside ordinary JSON validation.** |

All explicit `raise` sites are covered: eligibility; integer versions; request
fee; provider convention/model prefix; nonfinal unbounded and nonincreasing
tiers; candidate order; normalized consistency; nonzero refund; both acceptance
conditions; arithmetic domain; duplicate JSON keys; evaluation endpoint/cache
subset; envelope hash/endpoint/charge/usage. Every DTO (including `Frozen`,
`Evaluation`, `RawUsage` and `NormalizedUsage`) has an extra-field control.
Every required DTO field, explicit nonnull field/primitive, Literal/enum, strict boolean/UInt binding, Identity/
Digest binding and collection limit appears in the inventory below. Library
JSON syntax failures also have shared literal rejection cases. The evaluator,
normalizer, builder and production imports are unchanged.

## Validation rule → fixture → mutation inventory

There are **377 executable controls**: 367 scoped validation controls, the nine
previous arithmetic/contract controls, and one Python-only immutability
control. The following table contains one row per control. `N/N → N/N` means
red selected vectors under the indicated mutation, then green selected vectors
after restoration. Positive boundary references are acceptance evidence;
rejection controls deliberately leave those boundaries intact. The overlap
table above identifies implementation sites sharing one semantic rule.

The inventory check requires each string, constrained integer and strict boolean
field (including nullable bindings) to have a scoped control selecting a literal
wrong-type input at that field. A primitive-only test, a null-only rejection, or
an unrelated rule entry cannot satisfy that requirement. Ten inventory regression
tests remove each new string-type control independently and require failure.
For the ten Round 4 controls, the mutation harness also checks all 726 unselected
cases remain green while only the selected numeric-rejection vector fails.

| Rule | Fixture case name(s) | Mutation control | Red selected → green selected |
|---|---|---|---|
| aggregate rounding | `component_half_up` | `aggregate_rounding` | 1/1 → 1/1 |
| reasoning double added | `reasoning_subset` | `reasoning_double_added` | 1/1 → 1/1 |
| wrong cache convention | `openai_cache` | `wrong_cache_convention` | 1/1 → 1/1 |
| exclusive tier boundary | `tier_at` | `exclusive_tier_boundary` | 1/1 → 1/1 |
| missing minimum | `one_micro_minimum` | `missing_minimum` | 1/1 → 1/1 |
| unconditional hold clamp | `no_estimate_clamp` | `unconditional_hold_clamp` | 1/1 → 1/1 |
| accept excluded fee | `nonzero_endpoint_fee` | `accept_excluded_fee` | 1/1 → 1/1 |
| skip tier validation | `tier_nonfinal_unbounded`, `tier_duplicate_boundary`, `tier_descending_boundaries` | `skip_tier_validation` | 3/3 → 3/3 |
| ignore unknown eligibility fields | `unknown_context_field_requested`, `unknown_context_field_observed` | `ignore_unknown_eligibility_fields` | 2/2 → 2/2 |
| Parser rejects duplicate keys at every object depth | `duplicate_json_root`, `duplicate_json_candidate`, `duplicate_json_rate`, `duplicate_json_tier`, `raw_json_valid` | `duplicate_json_keys` | 4/4 → 4/4 |
| Frozen: extra="forbid" | `extra_frozen` | `extra_frozen` | 1/1 → 1/1 |
| Eligibility: extra="forbid" | `extra_eligibility` | `extra_eligibility` | 1/1 → 1/1 |
| Rates: extra="forbid" | `extra_rates` | `extra_rates` | 1/1 → 1/1 |
| Tier: extra="forbid" | `extra_tier` | `extra_tier` | 1/1 → 1/1 |
| Candidate: extra="forbid" | `extra_candidate` | `extra_candidate` | 1/1 → 1/1 |
| BillingSnapshot: extra="forbid" | `extra_billingsnapshot` | `extra_billingsnapshot` | 1/1 → 1/1 |
| RawUsage: extra="forbid" | `extra_rawusage` | `extra_rawusage` | 1/1 → 1/1 |
| NormalizedUsage: extra="forbid" | `extra_normalizedusage` | `extra_normalizedusage` | 1/1 → 1/1 |
| Evaluation: extra="forbid" | `extra_evaluation` | `extra_evaluation` | 1/1 → 1/1 |
| TerminalEnvelope: extra="forbid" | `extra_terminalenvelope` | `extra_terminalenvelope` | 1/1 → 1/1 |
| AcceptanceOutcome: extra="forbid" | `extra_acceptanceoutcome` | `extra_acceptanceoutcome` | 1/1 → 1/1 |
| UInt: lower bound 0 | `uint_negative_negative`, `uint_negative_valid_0`, `uint_negative_valid_1` | `uint_negative` | 1/1 → 1/1 |
| UInt: upper bound signed int64 | `uint_overflow_overflow`, `uint_overflow_valid_0`, `uint_overflow_valid_1` | `uint_overflow` | 1/1 → 1/1 |
| UInt: strict integer | `uint_float_float`, `uint_float_string`, `uint_float_bool`, `uint_float_valid_0`, `uint_float_valid_1` | `uint_float` | 3/3 → 3/3 |
| Identity: minimum length 1 | `identity_empty_empty`, `identity_empty_valid_0`, `identity_empty_valid_1` | `identity_empty` | 1/1 → 1/1 |
| Identity: maximum length 512 | `identity_long_long`, `identity_long_valid_0`, `identity_long_valid_1` | `identity_long` | 1/1 → 1/1 |
| Identity: ASCII alphabet | `identity_unicode_unicode`, `identity_unicode_space`, `identity_unicode_newline`, `identity_disallowed_punctuation`, `identity_disallowed_quote`, `identity_disallowed_angle`, `identity_unicode_valid_0`, `identity_unicode_valid_1`, `identity_unicode_valid_2` | `identity_unicode` | 6/6 → 6/6 |
| Digest: 64 lowercase hex digits | `digest_short_short`, `digest_short_long`, `digest_short_uppercase`, `digest_short_nonhex`, `digest_short_newline`, `digest_short_valid_0` | `digest_short` | 5/5 → 5/5 |
| UInt: base type / allowed values | `type_uint` | `type_uint` | 1/1 → 1/1 |
| Identity: base type / allowed values | `type_identity` | `type_identity` | 1/1 → 1/1 |
| Digest: base type / allowed values | `type_digest` | `type_digest` | 1/1 → 1/1 |
| SettlementMode: base type / allowed values | `type_settlementmode` | `type_settlementmode` | 1/1 → 1/1 |
| Eligibility.typed: strict boolean | `eligibility_typed_int`, `eligibility_typed_string` | `eligibility_typed_strict_bool` | 2/2 → 2/2 |
| Eligibility.streamed: strict boolean | `eligibility_streamed_int`, `eligibility_streamed_string` | `eligibility_streamed_strict_bool` | 2/2 → 2/2 |
| Eligibility.app_markup: binds strict UInt | `eligibility_app_markup_float`, `eligibility_app_markup_string`, `eligibility_app_markup_bool` | `eligibility_app_markup_strict` | 3/3 → 3/3 |
| Eligibility.custom_markup: binds strict UInt | `eligibility_custom_markup_float`, `eligibility_custom_markup_string`, `eligibility_custom_markup_bool` | `eligibility_custom_markup_strict` | 3/3 → 3/3 |
| Eligibility.receipt_fee: binds strict UInt | `eligibility_receipt_fee_float`, `eligibility_receipt_fee_string`, `eligibility_receipt_fee_bool` | `eligibility_receipt_fee_strict` | 3/3 → 3/3 |
| Eligibility.request_fee: binds strict UInt | `eligibility_request_fee_float`, `eligibility_request_fee_string`, `eligibility_request_fee_bool` | `eligibility_request_fee_strict` | 3/3 → 3/3 |
| Eligibility.custom_model: strict boolean | `eligibility_custom_model_int`, `eligibility_custom_model_string` | `eligibility_custom_model_strict_bool` | 2/2 → 2/2 |
| Eligibility.user_model: strict boolean | `eligibility_user_model_int`, `eligibility_user_model_string` | `eligibility_user_model_strict_bool` | 2/2 → 2/2 |
| Eligibility.tool_cost: strict boolean | `eligibility_tool_cost_int`, `eligibility_tool_cost_string` | `eligibility_tool_cost_strict_bool` | 2/2 → 2/2 |
| Eligibility.search_cost: strict boolean | `eligibility_search_cost_int`, `eligibility_search_cost_string` | `eligibility_search_cost_strict_bool` | 2/2 → 2/2 |
| Eligibility.image_cost: strict boolean | `eligibility_image_cost_int`, `eligibility_image_cost_string` | `eligibility_image_cost_strict_bool` | 2/2 → 2/2 |
| Eligibility.video_cost: strict boolean | `eligibility_video_cost_int`, `eligibility_video_cost_string` | `eligibility_video_cost_strict_bool` | 2/2 → 2/2 |
| Eligibility.partner: strict boolean | `eligibility_partner_int`, `eligibility_partner_string` | `eligibility_partner_strict_bool` | 2/2 → 2/2 |
| Eligibility.liberty: strict boolean | `eligibility_liberty_int`, `eligibility_liberty_string` | `eligibility_liberty_strict_bool` | 2/2 → 2/2 |
| Eligibility.native_batch: strict boolean | `eligibility_native_batch_int`, `eligibility_native_batch_string` | `eligibility_native_batch_strict_bool` | 2/2 → 2/2 |
| Eligibility.fusion: strict boolean | `eligibility_fusion_int`, `eligibility_fusion_string` | `eligibility_fusion_strict_bool` | 2/2 → 2/2 |
| Eligibility.polyphemus: strict boolean | `eligibility_polyphemus_int`, `eligibility_polyphemus_string` | `eligibility_polyphemus_strict_bool` | 2/2 → 2/2 |
| Eligibility.private_tier_basis: strict boolean | `eligibility_private_tier_basis_int`, `eligibility_private_tier_basis_string` | `eligibility_private_tier_basis_strict_bool` | 2/2 → 2/2 |
| Rates.input_micro_per_million: binds strict UInt | `rates_input_micro_per_million_float`, `rates_input_micro_per_million_string`, `rates_input_micro_per_million_bool` | `rates_input_micro_per_million_strict` | 3/3 → 3/3 |
| Rates.cached_input_micro_per_million: binds strict UInt | `rates_cached_input_micro_per_million_float`, `rates_cached_input_micro_per_million_string`, `rates_cached_input_micro_per_million_bool` | `rates_cached_input_micro_per_million_strict` | 3/3 → 3/3 |
| Rates.cache_creation_micro_per_million: binds strict UInt | `rates_cache_creation_micro_per_million_float`, `rates_cache_creation_micro_per_million_string`, `rates_cache_creation_micro_per_million_bool` | `rates_cache_creation_micro_per_million_strict` | 3/3 → 3/3 |
| Rates.output_micro_per_million: binds strict UInt | `rates_output_micro_per_million_float`, `rates_output_micro_per_million_string`, `rates_output_micro_per_million_bool` | `rates_output_micro_per_million_strict` | 3/3 → 3/3 |
| Tier.max_prompt_tokens: binds strict UInt | `tier_max_prompt_tokens_float`, `tier_max_prompt_tokens_string`, `tier_max_prompt_tokens_bool` | `tier_max_prompt_tokens_strict` | 3/3 → 3/3 |
| Candidate.endpoint_id: binds Identity | `candidate_endpoint_id_ascii`, `candidate_endpoint_id_identity_punctuation` | `candidate_endpoint_id_identity` | 2/2 → 2/2 |
| Candidate.provider: Literal["openai", "anthropic"] | `candidate_provider_literal` | `candidate_provider_literal` | 1/1 → 1/1 |
| Candidate.model_id: binds Identity | `candidate_model_id_ascii`, `candidate_model_id_identity_punctuation` | `candidate_model_id_identity` | 2/2 → 2/2 |
| Candidate.usage_type: Literal["Credits"] | `candidate_usage_type_literal` | `candidate_usage_type_literal` | 1/1 → 1/1 |
| Candidate.price_history_version: Literal[1] | `candidate_price_history_version_literal` | `candidate_price_history_version_literal` | 1/1 → 1/1 |
| Candidate.request_fee_micro: binds strict UInt | `candidate_request_fee_micro_float`, `candidate_request_fee_micro_string`, `candidate_request_fee_micro_bool` | `candidate_request_fee_micro_strict` | 3/3 → 3/3 |
| Candidate.rounding: Literal["half_up_per_million"] | `candidate_rounding_literal` | `candidate_rounding_literal` | 1/1 → 1/1 |
| Candidate.output_convention: Literal["includes_reasoning"] | `candidate_output_convention_literal` | `candidate_output_convention_literal` | 1/1 → 1/1 |
| BillingSnapshot.v: Literal[1] | `billingsnapshot_v_literal` | `billingsnapshot_v_literal` | 1/1 → 1/1 |
| BillingSnapshot.kind: Literal["credits_endpoint"] | `billingsnapshot_kind_literal` | `billingsnapshot_kind_literal` | 1/1 → 1/1 |
| BillingSnapshot.minimum_charge: Literal["one_micro_if_positive"] | `billingsnapshot_minimum_charge_literal` | `billingsnapshot_minimum_charge_literal` | 1/1 → 1/1 |
| BillingSnapshot.charge_cap: None | `billingsnapshot_charge_cap_literal` | `billingsnapshot_charge_cap_literal` | 1/1 → 1/1 |
| BillingSnapshot.tier_basis: Literal["total_prompt"] | `billingsnapshot_tier_basis_literal` | `billingsnapshot_tier_basis_literal` | 1/1 → 1/1 |
| BillingSnapshot.tier_boundary: Literal["inclusive"] | `billingsnapshot_tier_boundary_literal` | `billingsnapshot_tier_boundary_literal` | 1/1 → 1/1 |
| BillingSnapshot.tier_fallback: Literal["last_tier"] | `billingsnapshot_tier_fallback_literal` | `billingsnapshot_tier_fallback_literal` | 1/1 → 1/1 |
| RawUsage.input_tokens: binds strict UInt | `rawusage_input_tokens_float`, `rawusage_input_tokens_string`, `rawusage_input_tokens_bool` | `rawusage_input_tokens_strict` | 3/3 → 3/3 |
| RawUsage.output_tokens: binds strict UInt | `rawusage_output_tokens_float`, `rawusage_output_tokens_string`, `rawusage_output_tokens_bool` | `rawusage_output_tokens_strict` | 3/3 → 3/3 |
| RawUsage.cache_read_tokens: binds strict UInt | `rawusage_cache_read_tokens_float`, `rawusage_cache_read_tokens_string`, `rawusage_cache_read_tokens_bool` | `rawusage_cache_read_tokens_strict` | 3/3 → 3/3 |
| RawUsage.cache_creation_tokens: binds strict UInt | `rawusage_cache_creation_tokens_float`, `rawusage_cache_creation_tokens_string`, `rawusage_cache_creation_tokens_bool` | `rawusage_cache_creation_tokens_strict` | 3/3 → 3/3 |
| RawUsage.reasoning_tokens: binds strict UInt | `rawusage_reasoning_tokens_float`, `rawusage_reasoning_tokens_string`, `rawusage_reasoning_tokens_bool` | `rawusage_reasoning_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.uncached_input_tokens: binds strict UInt | `normalizedusage_uncached_input_tokens_float`, `normalizedusage_uncached_input_tokens_string`, `normalizedusage_uncached_input_tokens_bool` | `normalizedusage_uncached_input_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.total_prompt_tokens: binds strict UInt | `normalizedusage_total_prompt_tokens_float`, `normalizedusage_total_prompt_tokens_string`, `normalizedusage_total_prompt_tokens_bool` | `normalizedusage_total_prompt_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.output_tokens: binds strict UInt | `normalizedusage_output_tokens_float`, `normalizedusage_output_tokens_string`, `normalizedusage_output_tokens_bool` | `normalizedusage_output_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.cache_read_tokens: binds strict UInt | `normalizedusage_cache_read_tokens_float`, `normalizedusage_cache_read_tokens_string`, `normalizedusage_cache_read_tokens_bool` | `normalizedusage_cache_read_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.cache_creation_tokens: binds strict UInt | `normalizedusage_cache_creation_tokens_float`, `normalizedusage_cache_creation_tokens_string`, `normalizedusage_cache_creation_tokens_bool` | `normalizedusage_cache_creation_tokens_strict` | 3/3 → 3/3 |
| NormalizedUsage.reasoning_tokens: binds strict UInt | `normalizedusage_reasoning_tokens_float`, `normalizedusage_reasoning_tokens_string`, `normalizedusage_reasoning_tokens_bool` | `normalizedusage_reasoning_tokens_strict` | 3/3 → 3/3 |
| Evaluation.charge_micro: binds strict UInt | `evaluation_charge_micro_float`, `evaluation_charge_micro_string`, `evaluation_charge_micro_bool` | `evaluation_charge_micro_strict` | 3/3 → 3/3 |
| TerminalEnvelope.v: Literal[1] | `terminalenvelope_v_literal` | `terminalenvelope_v_literal` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: binds Identity | `terminalenvelope_authorization_id_ascii`, `terminalenvelope_authorization_id_identity_punctuation` | `terminalenvelope_authorization_id_identity` | 2/2 → 2/2 |
| TerminalEnvelope.generation_id: binds Identity | `terminalenvelope_generation_id_ascii`, `terminalenvelope_generation_id_identity_punctuation` | `terminalenvelope_generation_id_identity` | 2/2 → 2/2 |
| TerminalEnvelope.workspace_id: binds Identity | `terminalenvelope_workspace_id_ascii`, `terminalenvelope_workspace_id_identity_punctuation` | `terminalenvelope_workspace_id_identity` | 2/2 → 2/2 |
| TerminalEnvelope.key_id: binds Identity | `terminalenvelope_key_id_ascii`, `terminalenvelope_key_id_identity_punctuation` | `terminalenvelope_key_id_identity` | 2/2 → 2/2 |
| TerminalEnvelope.invocation_nonce: empty | `terminalenvelope_invocation_nonce_empty`, `terminalenvelope_invocation_nonce_empty_valid` | `terminalenvelope_invocation_nonce_empty` | 1/1 → 1/1 |
| TerminalEnvelope.invocation_nonce: long | `terminalenvelope_invocation_nonce_long`, `terminalenvelope_invocation_nonce_long_valid` | `terminalenvelope_invocation_nonce_long` | 1/1 → 1/1 |
| TerminalEnvelope.invocation_nonce: allowed ASCII alphabet | `terminalenvelope_invocation_nonce_ascii`, `nonce_disallowed_dot`, `nonce_disallowed_slash`, `nonce_disallowed_space`, `nonce_disallowed_newline`, `terminalenvelope_invocation_nonce_ascii_valid`, `nonce_allowed_alphabet` | `terminalenvelope_invocation_nonce_ascii` | 5/5 → 5/5 |
| TerminalEnvelope.billing_authority: Literal["local"] | `terminalenvelope_billing_authority_literal` | `terminalenvelope_billing_authority_literal` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: binds Identity | `terminalenvelope_journal_region_ascii`, `terminalenvelope_journal_region_identity_punctuation` | `terminalenvelope_journal_region_identity` | 2/2 → 2/2 |
| TerminalEnvelope.epoch: binds strict UInt | `terminalenvelope_epoch_float`, `terminalenvelope_epoch_string`, `terminalenvelope_epoch_bool` | `terminalenvelope_epoch_strict` | 3/3 → 3/3 |
| TerminalEnvelope.selected_endpoint: binds Identity | `terminalenvelope_selected_endpoint_ascii`, `terminalenvelope_selected_endpoint_identity_punctuation` | `terminalenvelope_selected_endpoint_identity` | 2/2 → 2/2 |
| TerminalEnvelope.snapshot_version: Literal[1] | `terminalenvelope_snapshot_version_literal` | `terminalenvelope_snapshot_version_literal` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_hash: binds Digest | `terminalenvelope_snapshot_hash_digest` | `terminalenvelope_snapshot_hash_digest` | 1/1 → 1/1 |
| TerminalEnvelope.charge_micro: binds strict UInt | `terminalenvelope_charge_micro_float`, `terminalenvelope_charge_micro_string`, `terminalenvelope_charge_micro_bool` | `terminalenvelope_charge_micro_strict` | 3/3 → 3/3 |
| TerminalEnvelope.terminal_kind: Literal["settle", "refund"] | `terminalenvelope_terminal_kind_literal` | `terminalenvelope_terminal_kind_literal` | 1/1 → 1/1 |
| TerminalEnvelope.route_type: Literal["chat.completions", "responses"] | `terminalenvelope_route_type_literal` | `terminalenvelope_route_type_literal` | 1/1 → 1/1 |
| TerminalEnvelope.streamed: strict boolean | `terminalenvelope_streamed_int`, `terminalenvelope_streamed_string` | `terminalenvelope_streamed_strict_bool` | 2/2 → 2/2 |
| AcceptanceOutcome.status: AcceptanceStatus | `acceptanceoutcome_status_literal` | `acceptanceoutcome_status_literal` | 1/1 → 1/1 |
| AcceptanceOutcome.payload_hash: binds Digest | `acceptanceoutcome_payload_hash_digest` | `acceptanceoutcome_payload_hash_digest` | 1/1 → 1/1 |
| BillingSnapshot.v: integer_versions rejects bool/float | `billingsnapshot_v_typed_bool`, `billingsnapshot_v_typed_float` | `billingsnapshot_v_integer` | 2/2 → 2/2 |
| Candidate.price_history_version: integer_versions rejects bool/float | `candidate_price_history_version_typed_bool`, `candidate_price_history_version_typed_float` | `candidate_price_history_version_integer` | 2/2 → 2/2 |
| TerminalEnvelope.v: integer_versions rejects bool/float | `terminalenvelope_v_typed_bool`, `terminalenvelope_v_typed_float` | `terminalenvelope_v_integer` | 2/2 → 2/2 |
| TerminalEnvelope.snapshot_version: integer_versions rejects bool/float | `terminalenvelope_snapshot_version_typed_bool`, `terminalenvelope_snapshot_version_typed_float` | `terminalenvelope_snapshot_version_integer` | 2/2 → 2/2 |
| candidates: at most 64 | `candidates_65`, `candidates_64` | `candidates_max` | 1/1 → 1/1 |
| tiers: at most 64 | `tiers_65`, `tiers_64` | `tiers_max` | 1/1 → 1/1 |
| Candidates: at least one | `candidates_empty` | `candidates_min` | 1/1 → 1/1 |
| Candidate endpoint IDs unique and sorted | `candidates_duplicate`, `candidates_unsorted` | `candidate_order` | 2/2 → 2/2 |
| Provider determines prompt convention | `provider_prompt_agreement`, `anthropic_prompt_agreement` | `provider_prompt_agreement` | 2/2 → 2/2 |
| Model identifier begins with provider/ prefix | `model_provider_prefix` | `model_provider_prefix` | 1/1 → 1/1 |
| Unbounded tier must be last | `tier_nonfinal_unbounded` | `tier_unbounded_last` | 1/1 → 1/1 |
| Finite tier boundaries strictly increase | `tier_duplicate_boundary`, `tier_descending_boundaries` | `tier_increasing` | 2/2 → 2/2 |
| envelope wrong hash | `envelope_wrong_hash` | `envelope_wrong_hash` | 1/1 → 1/1 |
| envelope wrong charge | `envelope_wrong_charge` | `envelope_wrong_charge` | 1/1 → 1/1 |
| envelope missing endpoint | `envelope_missing_endpoint` | `envelope_missing_endpoint` | 1/1 → 1/1 |
| envelope refund nonzero | `envelope_refund_nonzero` | `envelope_refund_nonzero` | 1/1 → 1/1 |
| Normalized prompt equals uncached + read + creation | `normalized_total_mismatch` | `normalized_total` | 1/1 → 1/1 |
| Normalized reasoning <= output | `normalized_reasoning_subset` | `normalized_reasoning` | 1/1 → 1/1 |
| checked(): value < 0 | `checked_negative` | `checked_negative` | 1/1 → 1/1 |
| checked(): value > MAX_INT | `checked_overflow` | `checked_overflow` | 1/1 → 1/1 |
| Accepted/duplicate requires both payload hash and pending status | `acceptance_accepted_missing_both`, `acceptance_accepted_missing_hash`, `acceptance_accepted_missing_pending`, `acceptance_duplicate_missing_both`, `acceptance_duplicate_missing_hash`, `acceptance_duplicate_missing_pending` | `acceptance_durable` | 6/6 → 6/6 |
| Rejection cannot acknowledge either hash or pending | `acceptance_sync_required_hash_only`, `acceptance_sync_required_pending_only`, `acceptance_conflict_hash_only`, `acceptance_conflict_pending_only`, `acceptance_invalid_hash_only`, `acceptance_invalid_pending_only` | `acceptance_rejection` | 6/6 → 6/6 |
| Eligibility exclusion: untyped | `eligibility_untyped_requested`, `eligibility_untyped_observed` | `eligibility_untyped` | 2/2 → 2/2 |
| Eligibility exclusion: non_credits | `eligibility_non_credits_requested`, `eligibility_non_credits_observed` | `eligibility_non_credits` | 2/2 → 2/2 |
| Eligibility exclusion: settlement_authority | `eligibility_settlement_authority_requested`, `eligibility_settlement_authority_observed` | `eligibility_settlement_authority` | 2/2 → 2/2 |
| Eligibility exclusion: unsupported_route | `eligibility_unsupported_route_requested`, `eligibility_unsupported_route_observed` | `eligibility_unsupported_route` | 2/2 → 2/2 |
| Eligibility exclusion: service_tier | `eligibility_service_tier_requested`, `eligibility_service_tier_observed` | `eligibility_service_tier` | 2/2 → 2/2 |
| Eligibility exclusion: app_markup | `eligibility_app_markup_requested`, `eligibility_app_markup_observed` | `eligibility_app_markup` | 2/2 → 2/2 |
| Eligibility exclusion: custom_markup | `eligibility_custom_markup_requested`, `eligibility_custom_markup_observed` | `eligibility_custom_markup` | 2/2 → 2/2 |
| Eligibility exclusion: receipt_fee | `eligibility_receipt_fee_requested`, `eligibility_receipt_fee_observed` | `eligibility_receipt_fee` | 2/2 → 2/2 |
| Eligibility exclusion: request_fee | `eligibility_request_fee_requested`, `eligibility_request_fee_observed` | `eligibility_request_fee` | 2/2 → 2/2 |
| Eligibility exclusion: custom_model | `eligibility_custom_model_requested`, `eligibility_custom_model_observed` | `eligibility_custom_model` | 2/2 → 2/2 |
| Eligibility exclusion: user_model | `eligibility_user_model_requested`, `eligibility_user_model_observed` | `eligibility_user_model` | 2/2 → 2/2 |
| Eligibility exclusion: tool_cost | `eligibility_tool_cost_requested`, `eligibility_tool_cost_observed` | `eligibility_tool_cost` | 2/2 → 2/2 |
| Eligibility exclusion: search_cost | `eligibility_search_cost_requested`, `eligibility_search_cost_observed` | `eligibility_search_cost` | 2/2 → 2/2 |
| Eligibility exclusion: image_cost | `eligibility_image_cost_requested`, `eligibility_image_cost_observed` | `eligibility_image_cost` | 2/2 → 2/2 |
| Eligibility exclusion: video_cost | `eligibility_video_cost_requested`, `eligibility_video_cost_observed` | `eligibility_video_cost` | 2/2 → 2/2 |
| Eligibility exclusion: partner | `eligibility_partner_requested`, `eligibility_partner_observed` | `eligibility_partner` | 2/2 → 2/2 |
| Eligibility exclusion: liberty | `eligibility_liberty_requested`, `eligibility_liberty_observed` | `eligibility_liberty` | 2/2 → 2/2 |
| Eligibility exclusion: native_batch | `eligibility_native_batch_requested`, `eligibility_native_batch_observed` | `eligibility_native_batch` | 2/2 → 2/2 |
| Eligibility exclusion: fusion | `eligibility_fusion_requested`, `eligibility_fusion_observed` | `eligibility_fusion` | 2/2 → 2/2 |
| Eligibility exclusion: polyphemus | `eligibility_polyphemus_requested`, `eligibility_polyphemus_observed` | `eligibility_polyphemus` | 2/2 → 2/2 |
| Eligibility exclusion: private_tier_basis | `eligibility_private_tier_basis_requested`, `eligibility_private_tier_basis_observed` | `eligibility_private_tier_basis` | 2/2 → 2/2 |
| require_eligible raises exclusion reason | `eligibility_untyped_requested`, `eligibility_untyped_observed` | `require_eligible` | 2/2 → 2/2 |
| Evaluation rejects selected endpoint absent from snapshot | `unknown_endpoint` | `evaluate_selected` | 1/1 → 1/1 |
| build_snapshot strict/bounded endpoint prompt_price_microdollars_per_million_tokens | `builder_prompt_price_microdollars_per_million_tokens_float`, `builder_prompt_price_microdollars_per_million_tokens_string`, `builder_prompt_price_microdollars_per_million_tokens_bool` | `builder_prompt_price_microdollars_per_million_tokens` | 3/3 → 3/3 |
| build_snapshot strict/bounded endpoint completion_price_microdollars_per_million_tokens | `builder_completion_price_microdollars_per_million_tokens_float`, `builder_completion_price_microdollars_per_million_tokens_string`, `builder_completion_price_microdollars_per_million_tokens_bool` | `builder_completion_price_microdollars_per_million_tokens` | 3/3 → 3/3 |
| build_snapshot strict/bounded endpoint request_price_microdollars | `builder_request_price_microdollars_float`, `builder_request_price_microdollars_string`, `builder_request_price_microdollars_bool` | `builder_request_price_microdollars` | 3/3 → 3/3 |
| Eligibility.usage_type: string type | `eligibility_usage_type_string_type` | `eligibility_usage_type_string_type` | 1/1 → 1/1 |
| Eligibility.authority: string type | `eligibility_authority_string_type` | `eligibility_authority_string_type` | 1/1 → 1/1 |
| Eligibility.route_type: string type | `eligibility_route_type_string_type` | `eligibility_route_type_string_type` | 1/1 → 1/1 |
| Eligibility.service_tier: string type | `eligibility_service_tier_string_type` | `eligibility_service_tier_string_type` | 1/1 → 1/1 |
| Eligibility.app_markup: UInt lower bound (field schema) | `eligibility_app_markup_lower` | `eligibility_app_markup_lower` | 1/1 → 1/1 |
| Eligibility.app_markup: UInt upper bound (field schema) | `eligibility_app_markup_upper` | `eligibility_app_markup_upper` | 1/1 → 1/1 |
| Eligibility.app_markup: UInt nonnull | `eligibility_app_markup_null` | `eligibility_app_markup_nonnull` | 1/1 → 1/1 |
| Eligibility.custom_markup: UInt lower bound (field schema) | `eligibility_custom_markup_lower` | `eligibility_custom_markup_lower` | 1/1 → 1/1 |
| Eligibility.custom_markup: UInt upper bound (field schema) | `eligibility_custom_markup_upper` | `eligibility_custom_markup_upper` | 1/1 → 1/1 |
| Eligibility.custom_markup: UInt nonnull | `eligibility_custom_markup_null` | `eligibility_custom_markup_nonnull` | 1/1 → 1/1 |
| Eligibility.receipt_fee: UInt lower bound (field schema) | `eligibility_receipt_fee_lower` | `eligibility_receipt_fee_lower` | 1/1 → 1/1 |
| Eligibility.receipt_fee: UInt upper bound (field schema) | `eligibility_receipt_fee_upper` | `eligibility_receipt_fee_upper` | 1/1 → 1/1 |
| Eligibility.receipt_fee: UInt nonnull | `eligibility_receipt_fee_null` | `eligibility_receipt_fee_nonnull` | 1/1 → 1/1 |
| Eligibility.request_fee: UInt lower bound (field schema) | `eligibility_request_fee_lower` | `eligibility_request_fee_lower` | 1/1 → 1/1 |
| Eligibility.request_fee: UInt upper bound (field schema) | `eligibility_request_fee_upper` | `eligibility_request_fee_upper` | 1/1 → 1/1 |
| Eligibility.request_fee: UInt nonnull | `eligibility_request_fee_null` | `eligibility_request_fee_nonnull` | 1/1 → 1/1 |
| Rates.input_micro_per_million: UInt lower bound (field schema) | `rates_input_micro_per_million_lower`, `rates_input_micro_per_million_wire_negative` | `rates_input_micro_per_million_lower` | 2/2 → 2/2 |
| Rates.input_micro_per_million: UInt upper bound (field schema) | `rates_input_micro_per_million_upper`, `rates_input_micro_per_million_wire_too_large` | `rates_input_micro_per_million_upper` | 2/2 → 2/2 |
| Rates.input_micro_per_million: UInt nonnull | `rates_input_micro_per_million_null` | `rates_input_micro_per_million_nonnull` | 1/1 → 1/1 |
| Rates.cached_input_micro_per_million: UInt lower bound (field schema) | `rates_cached_input_micro_per_million_lower`, `rates_cached_input_micro_per_million_wire_negative` | `rates_cached_input_micro_per_million_lower` | 2/2 → 2/2 |
| Rates.cached_input_micro_per_million: UInt upper bound (field schema) | `rates_cached_input_micro_per_million_upper`, `rates_cached_input_micro_per_million_wire_too_large` | `rates_cached_input_micro_per_million_upper` | 2/2 → 2/2 |
| Rates.cached_input_micro_per_million: UInt nonnull | `rates_cached_input_micro_per_million_null` | `rates_cached_input_micro_per_million_nonnull` | 1/1 → 1/1 |
| Rates.cache_creation_micro_per_million: UInt lower bound (field schema) | `rates_cache_creation_micro_per_million_lower`, `rates_cache_creation_micro_per_million_wire_negative` | `rates_cache_creation_micro_per_million_lower` | 2/2 → 2/2 |
| Rates.cache_creation_micro_per_million: UInt upper bound (field schema) | `rates_cache_creation_micro_per_million_upper`, `rates_cache_creation_micro_per_million_wire_too_large` | `rates_cache_creation_micro_per_million_upper` | 2/2 → 2/2 |
| Rates.cache_creation_micro_per_million: UInt nonnull | `rates_cache_creation_micro_per_million_null` | `rates_cache_creation_micro_per_million_nonnull` | 1/1 → 1/1 |
| Rates.output_micro_per_million: UInt lower bound (field schema) | `rates_output_micro_per_million_lower`, `rates_output_micro_per_million_wire_negative` | `rates_output_micro_per_million_lower` | 2/2 → 2/2 |
| Rates.output_micro_per_million: UInt upper bound (field schema) | `rates_output_micro_per_million_upper`, `rates_output_micro_per_million_wire_too_large` | `rates_output_micro_per_million_upper` | 2/2 → 2/2 |
| Rates.output_micro_per_million: UInt nonnull | `rates_output_micro_per_million_null` | `rates_output_micro_per_million_nonnull` | 1/1 → 1/1 |
| Tier.max_prompt_tokens: UInt lower bound (field schema) | `tier_max_prompt_tokens_lower`, `tier_max_prompt_tokens_wire_negative` | `tier_max_prompt_tokens_lower` | 2/2 → 2/2 |
| Tier.max_prompt_tokens: UInt upper bound (field schema) | `tier_max_prompt_tokens_upper`, `tier_max_prompt_tokens_wire_too_large` | `tier_max_prompt_tokens_upper` | 2/2 → 2/2 |
| Candidate.endpoint_id: Identity min length | `candidate_endpoint_id_min` | `candidate_endpoint_id_min` | 1/1 → 1/1 |
| Candidate.endpoint_id: Identity max length | `candidate_endpoint_id_max` | `candidate_endpoint_id_max` | 1/1 → 1/1 |
| Candidate.model_id: Identity min length | `candidate_model_id_min` | `candidate_model_id_min` | 1/1 → 1/1 |
| Candidate.model_id: Identity max length | `candidate_model_id_max` | `candidate_model_id_max` | 1/1 → 1/1 |
| Candidate.request_fee_micro: UInt lower bound (field schema) | `candidate_request_fee_micro_lower` | `candidate_request_fee_micro_lower` | 1/1 → 1/1 |
| Candidate.request_fee_micro: UInt upper bound (field schema) | `candidate_request_fee_micro_upper` | `candidate_request_fee_micro_upper` | 1/1 → 1/1 |
| Candidate.request_fee_micro: UInt nonnull | `candidate_request_fee_micro_null` | `candidate_request_fee_micro_nonnull` | 1/1 → 1/1 |
| Candidate.prompt_convention: literal enum (also implied by provider agreement) | `candidate_prompt_convention_unknown` | `candidate_prompt_convention_literal` | 1/1 → 1/1 |
| RawUsage.input_tokens: UInt lower bound (field schema) | `rawusage_input_tokens_lower`, `rawusage_input_tokens_wire_negative` | `rawusage_input_tokens_lower` | 2/2 → 2/2 |
| RawUsage.input_tokens: UInt upper bound (field schema) | `rawusage_input_tokens_upper`, `rawusage_input_tokens_wire_too_large` | `rawusage_input_tokens_upper` | 2/2 → 2/2 |
| RawUsage.input_tokens: UInt nonnull | `rawusage_input_tokens_null` | `rawusage_input_tokens_nonnull` | 1/1 → 1/1 |
| RawUsage.output_tokens: UInt lower bound (field schema) | `rawusage_output_tokens_lower` | `rawusage_output_tokens_lower` | 1/1 → 1/1 |
| RawUsage.output_tokens: UInt upper bound (field schema) | `rawusage_output_tokens_upper` | `rawusage_output_tokens_upper` | 1/1 → 1/1 |
| RawUsage.output_tokens: UInt nonnull | `rawusage_output_tokens_null` | `rawusage_output_tokens_nonnull` | 1/1 → 1/1 |
| RawUsage.cache_read_tokens: UInt lower bound (field schema) | `rawusage_cache_read_tokens_lower` | `rawusage_cache_read_tokens_lower` | 1/1 → 1/1 |
| RawUsage.cache_read_tokens: UInt upper bound (field schema) | `rawusage_cache_read_tokens_upper` | `rawusage_cache_read_tokens_upper` | 1/1 → 1/1 |
| RawUsage.cache_read_tokens: UInt nonnull | `rawusage_cache_read_tokens_null` | `rawusage_cache_read_tokens_nonnull` | 1/1 → 1/1 |
| RawUsage.cache_creation_tokens: UInt lower bound (field schema) | `rawusage_cache_creation_tokens_lower` | `rawusage_cache_creation_tokens_lower` | 1/1 → 1/1 |
| RawUsage.cache_creation_tokens: UInt upper bound (field schema) | `rawusage_cache_creation_tokens_upper` | `rawusage_cache_creation_tokens_upper` | 1/1 → 1/1 |
| RawUsage.cache_creation_tokens: UInt nonnull | `rawusage_cache_creation_tokens_null` | `rawusage_cache_creation_tokens_nonnull` | 1/1 → 1/1 |
| RawUsage.reasoning_tokens: UInt lower bound (field schema) | `rawusage_reasoning_tokens_lower` | `rawusage_reasoning_tokens_lower` | 1/1 → 1/1 |
| RawUsage.reasoning_tokens: UInt upper bound (field schema) | `rawusage_reasoning_tokens_upper` | `rawusage_reasoning_tokens_upper` | 1/1 → 1/1 |
| RawUsage.reasoning_tokens: UInt nonnull | `rawusage_reasoning_tokens_null` | `rawusage_reasoning_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.uncached_input_tokens: UInt lower bound (field schema) | `normalizedusage_uncached_input_tokens_lower` | `normalizedusage_uncached_input_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.uncached_input_tokens: UInt upper bound (field schema) | `normalizedusage_uncached_input_tokens_upper` | `normalizedusage_uncached_input_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.uncached_input_tokens: UInt nonnull | `normalizedusage_uncached_input_tokens_null` | `normalizedusage_uncached_input_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.total_prompt_tokens: UInt lower bound (field schema) | `normalizedusage_total_prompt_tokens_lower` | `normalizedusage_total_prompt_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.total_prompt_tokens: UInt upper bound (field schema) | `normalizedusage_total_prompt_tokens_upper` | `normalizedusage_total_prompt_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.total_prompt_tokens: UInt nonnull | `normalizedusage_total_prompt_tokens_null` | `normalizedusage_total_prompt_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.output_tokens: UInt lower bound (field schema) | `normalizedusage_output_tokens_lower` | `normalizedusage_output_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.output_tokens: UInt upper bound (field schema) | `normalizedusage_output_tokens_upper` | `normalizedusage_output_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.output_tokens: UInt nonnull | `normalizedusage_output_tokens_null` | `normalizedusage_output_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.cache_read_tokens: UInt lower bound (field schema) | `normalizedusage_cache_read_tokens_lower` | `normalizedusage_cache_read_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.cache_read_tokens: UInt upper bound (field schema) | `normalizedusage_cache_read_tokens_upper` | `normalizedusage_cache_read_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.cache_read_tokens: UInt nonnull | `normalizedusage_cache_read_tokens_null` | `normalizedusage_cache_read_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.cache_creation_tokens: UInt lower bound (field schema) | `normalizedusage_cache_creation_tokens_lower` | `normalizedusage_cache_creation_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.cache_creation_tokens: UInt upper bound (field schema) | `normalizedusage_cache_creation_tokens_upper` | `normalizedusage_cache_creation_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.cache_creation_tokens: UInt nonnull | `normalizedusage_cache_creation_tokens_null` | `normalizedusage_cache_creation_tokens_nonnull` | 1/1 → 1/1 |
| NormalizedUsage.reasoning_tokens: UInt lower bound (field schema) | `normalizedusage_reasoning_tokens_lower` | `normalizedusage_reasoning_tokens_lower` | 1/1 → 1/1 |
| NormalizedUsage.reasoning_tokens: UInt upper bound (field schema) | `normalizedusage_reasoning_tokens_upper` | `normalizedusage_reasoning_tokens_upper` | 1/1 → 1/1 |
| NormalizedUsage.reasoning_tokens: UInt nonnull | `normalizedusage_reasoning_tokens_null` | `normalizedusage_reasoning_tokens_nonnull` | 1/1 → 1/1 |
| Evaluation.charge_micro: UInt lower bound (field schema) | `evaluation_charge_micro_lower`, `evaluation_charge_micro_wire_negative` | `evaluation_charge_micro_lower` | 2/2 → 2/2 |
| Evaluation.charge_micro: UInt upper bound (field schema) | `evaluation_charge_micro_upper`, `evaluation_charge_micro_wire_too_large` | `evaluation_charge_micro_upper` | 2/2 → 2/2 |
| Evaluation.charge_micro: UInt nonnull | `evaluation_charge_micro_null` | `evaluation_charge_micro_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: Identity min length | `terminalenvelope_authorization_id_min` | `terminalenvelope_authorization_id_min` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: Identity max length | `terminalenvelope_authorization_id_max` | `terminalenvelope_authorization_id_max` | 1/1 → 1/1 |
| TerminalEnvelope.generation_id: Identity min length | `terminalenvelope_generation_id_min` | `terminalenvelope_generation_id_min` | 1/1 → 1/1 |
| TerminalEnvelope.generation_id: Identity max length | `terminalenvelope_generation_id_max` | `terminalenvelope_generation_id_max` | 1/1 → 1/1 |
| TerminalEnvelope.workspace_id: Identity min length | `terminalenvelope_workspace_id_min` | `terminalenvelope_workspace_id_min` | 1/1 → 1/1 |
| TerminalEnvelope.workspace_id: Identity max length | `terminalenvelope_workspace_id_max` | `terminalenvelope_workspace_id_max` | 1/1 → 1/1 |
| TerminalEnvelope.key_id: Identity min length | `terminalenvelope_key_id_min` | `terminalenvelope_key_id_min` | 1/1 → 1/1 |
| TerminalEnvelope.key_id: Identity max length | `terminalenvelope_key_id_max` | `terminalenvelope_key_id_max` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: Identity min length | `terminalenvelope_journal_region_min` | `terminalenvelope_journal_region_min` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: Identity max length | `terminalenvelope_journal_region_max` | `terminalenvelope_journal_region_max` | 1/1 → 1/1 |
| TerminalEnvelope.epoch: UInt lower bound (field schema) | `terminalenvelope_epoch_lower`, `terminalenvelope_epoch_wire_negative` | `terminalenvelope_epoch_lower` | 2/2 → 2/2 |
| TerminalEnvelope.epoch: UInt upper bound (field schema) | `terminalenvelope_epoch_upper`, `terminalenvelope_epoch_wire_too_large` | `terminalenvelope_epoch_upper` | 2/2 → 2/2 |
| TerminalEnvelope.epoch: UInt nonnull | `terminalenvelope_epoch_null` | `terminalenvelope_epoch_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.selected_endpoint: Identity min length | `terminalenvelope_selected_endpoint_min` | `terminalenvelope_selected_endpoint_min` | 1/1 → 1/1 |
| TerminalEnvelope.selected_endpoint: Identity max length | `terminalenvelope_selected_endpoint_max` | `terminalenvelope_selected_endpoint_max` | 1/1 → 1/1 |
| TerminalEnvelope.charge_micro: UInt lower bound (field schema) | `terminalenvelope_charge_micro_lower` | `terminalenvelope_charge_micro_lower` | 1/1 → 1/1 |
| TerminalEnvelope.charge_micro: UInt upper bound (field schema) | `terminalenvelope_charge_micro_upper` | `terminalenvelope_charge_micro_upper` | 1/1 → 1/1 |
| TerminalEnvelope.charge_micro: UInt nonnull | `terminalenvelope_charge_micro_null` | `terminalenvelope_charge_micro_nonnull` | 1/1 → 1/1 |
| AcceptanceOutcome.settlement_status: pending or null (also implied by durable validator) | `acceptanceoutcome_settlement_status_unknown` | `acceptanceoutcome_settlement_status_literal` | 1/1 → 1/1 |
| build_snapshot validates tier input_micro_per_million | `builder_tier_input_micro_per_million_float`, `builder_tier_input_micro_per_million_string`, `builder_tier_input_micro_per_million_bool` | `builder_tier_input_micro_per_million` | 3/3 → 3/3 |
| build_snapshot validates tier output_micro_per_million | `builder_tier_output_micro_per_million_float`, `builder_tier_output_micro_per_million_string`, `builder_tier_output_micro_per_million_bool` | `builder_tier_output_micro_per_million` | 3/3 → 3/3 |
| build_snapshot validates tier cached_input_micro_per_million | `builder_tier_cached_input_micro_per_million_float`, `builder_tier_cached_input_micro_per_million_string`, `builder_tier_cached_input_micro_per_million_bool` | `builder_tier_cached_input_micro_per_million` | 3/3 → 3/3 |
| Checked addition of rounding offset | `rounding_add_overflow` | `overflow_rounding` | 1/1 → 1/1 |
| Envelope usage must equal evaluator result (fault injection) | `envelope_evaluated_usage_mismatch` | `envelope_usage_match` | 1/1 → 1/1 |
| Inclusive cache subsets cannot exceed prompt; exact malformed_usage code | `cache_exceeds_prompt` | `cache_subset` | 1/1 → 1/1 |
| Checked cache-read + cache-creation sum | `cached_sum_overflow` | `cached_sum_guard` | 1/1 → 1/1 |
| Checked Anthropic uncached + cached sum | `normalization_overflow` | `prompt_sum_guard` | 1/1 → 1/1 |
| Finite last-tier fallback rather than base rate | `tier_last_fallback` | `last_tier_fallback` | 1/1 → 1/1 |
| Unbounded tier and missing cached rate are allowed | `builder_nullable_tier_inputs` | `builder_optional_tier_inputs` | 1/1 → 1/1 |
| Rates.input_micro_per_million: required | `rates_input_micro_per_million_required` | `rates_input_micro_per_million_required` | 1/1 → 1/1 |
| Rates.cached_input_micro_per_million: required | `rates_cached_input_micro_per_million_required` | `rates_cached_input_micro_per_million_required` | 1/1 → 1/1 |
| Rates.cache_creation_micro_per_million: required | `rates_cache_creation_micro_per_million_required` | `rates_cache_creation_micro_per_million_required` | 1/1 → 1/1 |
| Rates.output_micro_per_million: required | `rates_output_micro_per_million_required` | `rates_output_micro_per_million_required` | 1/1 → 1/1 |
| Tier.max_prompt_tokens: required | `tier_max_prompt_tokens_required` | `tier_max_prompt_tokens_required` | 1/1 → 1/1 |
| Tier.rates: required | `tier_rates_required` | `tier_rates_required` | 1/1 → 1/1 |
| Tier.rates: nested Rates record | `tier_rates_object` | `tier_rates_object` | 1/1 → 1/1 |
| Candidate.endpoint_id: required | `candidate_endpoint_id_required` | `candidate_endpoint_id_required` | 1/1 → 1/1 |
| Candidate.provider: required | `candidate_provider_required` | `candidate_provider_required` | 1/1 → 1/1 |
| Candidate.model_id: required | `candidate_model_id_required` | `candidate_model_id_required` | 1/1 → 1/1 |
| Candidate.usage_type: required | `candidate_usage_type_required` | `candidate_usage_type_required` | 1/1 → 1/1 |
| Candidate.price_history_version: required | `candidate_price_history_version_required` | `candidate_price_history_version_required` | 1/1 → 1/1 |
| Candidate.rates: required | `candidate_rates_required` | `candidate_rates_required` | 1/1 → 1/1 |
| Candidate.rates: nested Rates record | `candidate_rates_object` | `candidate_rates_object` | 1/1 → 1/1 |
| Candidate.tiers: required | `candidate_tiers_required` | `candidate_tiers_required` | 1/1 → 1/1 |
| Candidate.request_fee_micro: required | `candidate_request_fee_micro_required` | `candidate_request_fee_micro_required` | 1/1 → 1/1 |
| Candidate.rounding: required | `candidate_rounding_required` | `candidate_rounding_required` | 1/1 → 1/1 |
| Candidate.prompt_convention: required | `candidate_prompt_convention_required` | `candidate_prompt_convention_required` | 1/1 → 1/1 |
| Candidate.output_convention: required | `candidate_output_convention_required` | `candidate_output_convention_required` | 1/1 → 1/1 |
| BillingSnapshot.v: required | `billingsnapshot_v_required` | `billingsnapshot_v_required` | 1/1 → 1/1 |
| BillingSnapshot.kind: required | `billingsnapshot_kind_required` | `billingsnapshot_kind_required` | 1/1 → 1/1 |
| BillingSnapshot.candidates: required | `billingsnapshot_candidates_required` | `billingsnapshot_candidates_required` | 1/1 → 1/1 |
| BillingSnapshot.minimum_charge: required | `billingsnapshot_minimum_charge_required` | `billingsnapshot_minimum_charge_required` | 1/1 → 1/1 |
| BillingSnapshot.charge_cap: required | `billingsnapshot_charge_cap_required` | `billingsnapshot_charge_cap_required` | 1/1 → 1/1 |
| BillingSnapshot.tier_basis: required | `billingsnapshot_tier_basis_required` | `billingsnapshot_tier_basis_required` | 1/1 → 1/1 |
| BillingSnapshot.tier_boundary: required | `billingsnapshot_tier_boundary_required` | `billingsnapshot_tier_boundary_required` | 1/1 → 1/1 |
| BillingSnapshot.tier_fallback: required | `billingsnapshot_tier_fallback_required` | `billingsnapshot_tier_fallback_required` | 1/1 → 1/1 |
| RawUsage.input_tokens: required | `rawusage_input_tokens_required` | `rawusage_input_tokens_required` | 1/1 → 1/1 |
| RawUsage.output_tokens: required | `rawusage_output_tokens_required` | `rawusage_output_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.uncached_input_tokens: required | `normalizedusage_uncached_input_tokens_required` | `normalizedusage_uncached_input_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.total_prompt_tokens: required | `normalizedusage_total_prompt_tokens_required` | `normalizedusage_total_prompt_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.output_tokens: required | `normalizedusage_output_tokens_required` | `normalizedusage_output_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.cache_read_tokens: required | `normalizedusage_cache_read_tokens_required` | `normalizedusage_cache_read_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.cache_creation_tokens: required | `normalizedusage_cache_creation_tokens_required` | `normalizedusage_cache_creation_tokens_required` | 1/1 → 1/1 |
| NormalizedUsage.reasoning_tokens: required | `normalizedusage_reasoning_tokens_required` | `normalizedusage_reasoning_tokens_required` | 1/1 → 1/1 |
| Evaluation.usage: required | `evaluation_usage_required` | `evaluation_usage_required` | 1/1 → 1/1 |
| Evaluation.usage: nested NormalizedUsage record | `evaluation_usage_object` | `evaluation_usage_object` | 1/1 → 1/1 |
| Evaluation.charge_micro: required | `evaluation_charge_micro_required` | `evaluation_charge_micro_required` | 1/1 → 1/1 |
| TerminalEnvelope.v: required | `terminalenvelope_v_required` | `terminalenvelope_v_required` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: required | `terminalenvelope_authorization_id_required` | `terminalenvelope_authorization_id_required` | 1/1 → 1/1 |
| TerminalEnvelope.generation_id: required | `terminalenvelope_generation_id_required` | `terminalenvelope_generation_id_required` | 1/1 → 1/1 |
| TerminalEnvelope.workspace_id: required | `terminalenvelope_workspace_id_required` | `terminalenvelope_workspace_id_required` | 1/1 → 1/1 |
| TerminalEnvelope.key_id: required | `terminalenvelope_key_id_required` | `terminalenvelope_key_id_required` | 1/1 → 1/1 |
| TerminalEnvelope.invocation_nonce: required | `terminalenvelope_invocation_nonce_required` | `terminalenvelope_invocation_nonce_required` | 1/1 → 1/1 |
| TerminalEnvelope.billing_authority: required | `terminalenvelope_billing_authority_required` | `terminalenvelope_billing_authority_required` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: required | `terminalenvelope_journal_region_required` | `terminalenvelope_journal_region_required` | 1/1 → 1/1 |
| TerminalEnvelope.epoch: required | `terminalenvelope_epoch_required` | `terminalenvelope_epoch_required` | 1/1 → 1/1 |
| TerminalEnvelope.selected_endpoint: required | `terminalenvelope_selected_endpoint_required` | `terminalenvelope_selected_endpoint_required` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_version: required | `terminalenvelope_snapshot_version_required` | `terminalenvelope_snapshot_version_required` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_hash: required | `terminalenvelope_snapshot_hash_required` | `terminalenvelope_snapshot_hash_required` | 1/1 → 1/1 |
| TerminalEnvelope.usage: required | `terminalenvelope_usage_required` | `terminalenvelope_usage_required` | 1/1 → 1/1 |
| TerminalEnvelope.usage: nested NormalizedUsage record | `terminalenvelope_usage_object` | `terminalenvelope_usage_object` | 1/1 → 1/1 |
| TerminalEnvelope.charge_micro: required | `terminalenvelope_charge_micro_required` | `terminalenvelope_charge_micro_required` | 1/1 → 1/1 |
| TerminalEnvelope.terminal_kind: required | `terminalenvelope_terminal_kind_required` | `terminalenvelope_terminal_kind_required` | 1/1 → 1/1 |
| TerminalEnvelope.route_type: required | `terminalenvelope_route_type_required` | `terminalenvelope_route_type_required` | 1/1 → 1/1 |
| TerminalEnvelope.streamed: required | `terminalenvelope_streamed_required` | `terminalenvelope_streamed_required` | 1/1 → 1/1 |
| AcceptanceOutcome.status: required | `acceptanceoutcome_status_required` | `acceptanceoutcome_status_required` | 1/1 → 1/1 |
| Rejected outcomes cannot carry BOTH hash and pending (overlapping guards) | `acceptance_sync_required_both`, `acceptance_conflict_both`, `acceptance_invalid_both` | `acceptance_rejection_both` | 3/3 → 3/3 |
| Frozen nested assignment (Python only) | `test_builder_copies_and_sorts_and_freezes`, `test_envelope_binding_and_refund` | `frozen_assignment` | 2/2 → 2/2 |
| Normalized component sum rejects int64 wraparound to a valid total | `normalized_sum_wraparound` | `normalized_native_overflow` | 1/1 → 1/1 |
| Token-rate multiplication must reject overflow before native int64 wrapping | `multiply_wraparound` | `multiplication_native_overflow` | 1/1 → 1/1 |
| Eligibility.typed: nonnull Annotated[bool, Field(strict=True)] | `eligibility_typed_nonnull_null` | `eligibility_typed_nonnull` | 1/1 → 1/1 |
| Eligibility.usage_type: nonnull str | `eligibility_usage_type_nonnull_null` | `eligibility_usage_type_nonnull` | 1/1 → 1/1 |
| Eligibility.authority: nonnull str | `eligibility_authority_nonnull_null` | `eligibility_authority_nonnull` | 1/1 → 1/1 |
| Eligibility.route_type: nonnull str | `eligibility_route_type_nonnull_null` | `eligibility_route_type_nonnull` | 1/1 → 1/1 |
| Eligibility.streamed: nonnull Annotated[bool, Field(strict=True)] | `eligibility_streamed_nonnull_null` | `eligibility_streamed_nonnull` | 1/1 → 1/1 |
| Eligibility.custom_model: nonnull Annotated[bool, Field(strict=True)] | `eligibility_custom_model_nonnull_null` | `eligibility_custom_model_nonnull` | 1/1 → 1/1 |
| Eligibility.user_model: nonnull Annotated[bool, Field(strict=True)] | `eligibility_user_model_nonnull_null` | `eligibility_user_model_nonnull` | 1/1 → 1/1 |
| Eligibility.tool_cost: nonnull Annotated[bool, Field(strict=True)] | `eligibility_tool_cost_nonnull_null` | `eligibility_tool_cost_nonnull` | 1/1 → 1/1 |
| Eligibility.search_cost: nonnull Annotated[bool, Field(strict=True)] | `eligibility_search_cost_nonnull_null` | `eligibility_search_cost_nonnull` | 1/1 → 1/1 |
| Eligibility.image_cost: nonnull Annotated[bool, Field(strict=True)] | `eligibility_image_cost_nonnull_null` | `eligibility_image_cost_nonnull` | 1/1 → 1/1 |
| Eligibility.video_cost: nonnull Annotated[bool, Field(strict=True)] | `eligibility_video_cost_nonnull_null` | `eligibility_video_cost_nonnull` | 1/1 → 1/1 |
| Eligibility.partner: nonnull Annotated[bool, Field(strict=True)] | `eligibility_partner_nonnull_null` | `eligibility_partner_nonnull` | 1/1 → 1/1 |
| Eligibility.liberty: nonnull Annotated[bool, Field(strict=True)] | `eligibility_liberty_nonnull_null` | `eligibility_liberty_nonnull` | 1/1 → 1/1 |
| Eligibility.native_batch: nonnull Annotated[bool, Field(strict=True)] | `eligibility_native_batch_nonnull_null` | `eligibility_native_batch_nonnull` | 1/1 → 1/1 |
| Eligibility.fusion: nonnull Annotated[bool, Field(strict=True)] | `eligibility_fusion_nonnull_null` | `eligibility_fusion_nonnull` | 1/1 → 1/1 |
| Eligibility.polyphemus: nonnull Annotated[bool, Field(strict=True)] | `eligibility_polyphemus_nonnull_null` | `eligibility_polyphemus_nonnull` | 1/1 → 1/1 |
| Eligibility.private_tier_basis: nonnull Annotated[bool, Field(strict=True)] | `eligibility_private_tier_basis_nonnull_null` | `eligibility_private_tier_basis_nonnull` | 1/1 → 1/1 |
| Candidate.endpoint_id: nonnull Identity | `candidate_endpoint_id_nonnull_null` | `candidate_endpoint_id_nonnull` | 1/1 → 1/1 |
| Candidate.provider: nonnull Literal["openai", "anthropic"] | `candidate_provider_nonnull_null` | `candidate_provider_nonnull` | 1/1 → 1/1 |
| Candidate.model_id: nonnull Identity | `candidate_model_id_nonnull_null` | `candidate_model_id_nonnull` | 1/1 → 1/1 |
| Candidate.usage_type: nonnull Literal["Credits"] | `candidate_usage_type_nonnull_null` | `candidate_usage_type_nonnull` | 1/1 → 1/1 |
| Candidate.price_history_version: nonnull Literal[1] | `candidate_price_history_version_nonnull_null` | `candidate_price_history_version_nonnull` | 1/1 → 1/1 |
| Candidate.tiers: nonnull tuple[Tier, ...] | `candidate_tiers_nonnull_null` | `candidate_tiers_nonnull` | 1/1 → 1/1 |
| Candidate.rounding: nonnull Literal["half_up_per_million"] | `candidate_rounding_nonnull_null` | `candidate_rounding_nonnull` | 1/1 → 1/1 |
| Candidate.prompt_convention: nonnull Literal["includes_cache", "excludes_cache"] | `candidate_prompt_convention_nonnull_null` | `candidate_prompt_convention_nonnull` | 1/1 → 1/1 |
| Candidate.output_convention: nonnull Literal["includes_reasoning"] | `candidate_output_convention_nonnull_null` | `candidate_output_convention_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.v: nonnull Literal[1] | `billingsnapshot_v_nonnull_null` | `billingsnapshot_v_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.kind: nonnull Literal["credits_endpoint"] | `billingsnapshot_kind_nonnull_null` | `billingsnapshot_kind_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.candidates: nonnull tuple[Candidate, ...] | `billingsnapshot_candidates_nonnull_null` | `billingsnapshot_candidates_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.minimum_charge: nonnull Literal["one_micro_if_positive"] | `billingsnapshot_minimum_charge_nonnull_null` | `billingsnapshot_minimum_charge_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.tier_basis: nonnull Literal["total_prompt"] | `billingsnapshot_tier_basis_nonnull_null` | `billingsnapshot_tier_basis_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.tier_boundary: nonnull Literal["inclusive"] | `billingsnapshot_tier_boundary_nonnull_null` | `billingsnapshot_tier_boundary_nonnull` | 1/1 → 1/1 |
| BillingSnapshot.tier_fallback: nonnull Literal["last_tier"] | `billingsnapshot_tier_fallback_nonnull_null` | `billingsnapshot_tier_fallback_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.v: nonnull Literal[1] | `terminalenvelope_v_nonnull_null` | `terminalenvelope_v_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: nonnull Identity | `terminalenvelope_authorization_id_nonnull_null` | `terminalenvelope_authorization_id_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.generation_id: nonnull Identity | `terminalenvelope_generation_id_nonnull_null` | `terminalenvelope_generation_id_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.workspace_id: nonnull Identity | `terminalenvelope_workspace_id_nonnull_null` | `terminalenvelope_workspace_id_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.key_id: nonnull Identity | `terminalenvelope_key_id_nonnull_null` | `terminalenvelope_key_id_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.invocation_nonce: nonnull Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | `terminalenvelope_invocation_nonce_nonnull_null` | `terminalenvelope_invocation_nonce_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.billing_authority: nonnull Literal["local"] | `terminalenvelope_billing_authority_nonnull_null` | `terminalenvelope_billing_authority_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: nonnull Identity | `terminalenvelope_journal_region_nonnull_null` | `terminalenvelope_journal_region_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.selected_endpoint: nonnull Identity | `terminalenvelope_selected_endpoint_nonnull_null` | `terminalenvelope_selected_endpoint_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_version: nonnull Literal[1] | `terminalenvelope_snapshot_version_nonnull_null` | `terminalenvelope_snapshot_version_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_hash: nonnull Digest | `terminalenvelope_snapshot_hash_nonnull_null` | `terminalenvelope_snapshot_hash_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.terminal_kind: nonnull Literal["settle", "refund"] | `terminalenvelope_terminal_kind_nonnull_null` | `terminalenvelope_terminal_kind_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.route_type: nonnull Literal["chat.completions", "responses"] | `terminalenvelope_route_type_nonnull_null` | `terminalenvelope_route_type_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.streamed: nonnull Annotated[bool, Field(strict=True)] | `terminalenvelope_streamed_nonnull_null` | `terminalenvelope_streamed_nonnull` | 1/1 → 1/1 |
| AcceptanceOutcome.status: nonnull AcceptanceStatus | `acceptanceoutcome_status_nonnull_null` | `acceptanceoutcome_status_nonnull` | 1/1 → 1/1 |
| TerminalEnvelope.invocation_nonce: string input type | `terminalenvelope_nonce_string_type` | `terminalenvelope_nonce_string_type` | 1/1 → 1/1 |
| Identity: nonnull primitive | `type_identity_nonnull` | `type_identity_nonnull` | 1/1 → 1/1 |
| Digest: nonnull primitive | `type_digest_nonnull` | `type_digest_nonnull` | 1/1 → 1/1 |
| SettlementMode: nonnull primitive | `type_settlementmode_nonnull` | `type_settlementmode_nonnull` | 1/1 → 1/1 |
| Frozen/Eligibility: null is not an empty object with valid defaults | `frozen_null_object`, `eligibility_null_object` | `model_root_nonnull` | 2/2 → 2/2 |
| Candidate.endpoint_id: string input type | `candidate_endpoint_id_string_type` | `candidate_endpoint_id_string_type` | 1/1 → 1/1 |
| Candidate.model_id: string input type | `candidate_model_id_string_type` | `candidate_model_id_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.authorization_id: string input type | `terminalenvelope_authorization_id_string_type` | `terminalenvelope_authorization_id_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.generation_id: string input type | `terminalenvelope_generation_id_string_type` | `terminalenvelope_generation_id_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.workspace_id: string input type | `terminalenvelope_workspace_id_string_type` | `terminalenvelope_workspace_id_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.key_id: string input type | `terminalenvelope_key_id_string_type` | `terminalenvelope_key_id_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.journal_region: string input type | `terminalenvelope_journal_region_string_type` | `terminalenvelope_journal_region_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.selected_endpoint: string input type | `terminalenvelope_selected_endpoint_string_type` | `terminalenvelope_selected_endpoint_string_type` | 1/1 → 1/1 |
| TerminalEnvelope.snapshot_hash: string input type | `terminalenvelope_snapshot_hash_string_type` | `terminalenvelope_snapshot_hash_string_type` | 1/1 → 1/1 |
| AcceptanceOutcome.payload_hash: string input type | `acceptanceoutcome_payload_hash_string_type` | `acceptanceoutcome_payload_hash_string_type` | 1/1 → 1/1 |

## Original 102 fixture cases (preserved)

| Case | Literal charge or exclusion |
|---|---|
| `component_half_up` | 2 microdollars |
| `one_micro_minimum` | 1 microdollars |
| `zero_usage` | 0 microdollars |
| `zero_rates` | 0 microdollars |
| `openai_cache` | 13 microdollars |
| `anthropic_cache` | 13 microdollars |
| `reasoning_subset` | 13 microdollars |
| `openai_cache_creation` | 16 microdollars |
| `anthropic_cache_creation` | 16 microdollars |
| `tier_below` | 9 microdollars |
| `tier_at` | 10 microdollars |
| `tier_above` | 22 microdollars |
| `tier_last_fallback` | 42 microdollars |
| `tier_total_includes_cache` | 16 microdollars |
| `cache_rate_missing` | 14 microdollars |
| `cache_rate_explicit` | 13 microdollars |
| `cache_rate_explicit_zero` | 12 microdollars |
| `fallback_winner` | 13 microdollars |
| `no_estimate_clamp` | 13 microdollars |
| `catalog_changed_after_authorize` | 13 microdollars |
| `endpoint_removed_after_authorize` | 13 microdollars |
| `chat.completions_nonstream` | 13 microdollars |
| `chat.completions_stream` | 13 microdollars |
| `responses_nonstream` | 13 microdollars |
| `responses_stream` | 13 microdollars |
| `max_tokens_zero_rate` | 0 microdollars |
| `max_rate_zero_usage` | 0 microdollars |
| `max_safe_rounding_numerator` | 9223372036854 microdollars |
| `multiply_overflow` | arithmetic_overflow |
| `rounding_add_overflow` | arithmetic_overflow |
| `normalization_overflow` | arithmetic_overflow |
| `negative_count` | invalid_usage |
| `float_count` | invalid_usage |
| `string_count` | invalid_usage |
| `bool_count` | invalid_usage |
| `too_large_count` | invalid_usage |
| `null_count` | invalid_usage |
| `cache_exceeds_prompt` | malformed_usage |
| `reasoning_exceeds_output` | malformed_usage |
| `unknown_usage_field` | invalid_usage |
| `exclude_app_markup_requested` | app_markup |
| `exclude_app_markup_observed` | app_markup |
| `exclude_custom_markup_requested` | custom_markup |
| `exclude_custom_markup_observed` | custom_markup |
| `exclude_receipt_fee_requested` | receipt_fee |
| `exclude_receipt_fee_observed` | receipt_fee |
| `exclude_request_fee_requested` | request_fee |
| `exclude_request_fee_observed` | request_fee |
| `exclude_custom_model_requested` | custom_model |
| `exclude_custom_model_observed` | custom_model |
| `exclude_user_model_requested` | user_model |
| `exclude_user_model_observed` | user_model |
| `exclude_tool_cost_requested` | tool_cost |
| `exclude_tool_cost_observed` | tool_cost |
| `exclude_search_cost_requested` | search_cost |
| `exclude_search_cost_observed` | search_cost |
| `exclude_image_cost_requested` | image_cost |
| `exclude_image_cost_observed` | image_cost |
| `exclude_video_cost_requested` | video_cost |
| `exclude_video_cost_observed` | video_cost |
| `exclude_partner_requested` | partner |
| `exclude_partner_observed` | partner |
| `exclude_liberty_requested` | liberty |
| `exclude_liberty_observed` | liberty |
| `exclude_native_batch_requested` | native_batch |
| `exclude_native_batch_observed` | native_batch |
| `exclude_fusion_requested` | fusion |
| `exclude_fusion_observed` | fusion |
| `exclude_polyphemus_requested` | polyphemus |
| `exclude_polyphemus_observed` | polyphemus |
| `exclude_private_tier_basis_requested` | private_tier_basis |
| `exclude_private_tier_basis_observed` | private_tier_basis |
| `exclude_untyped` | untyped |
| `exclude_byok` | non_credits |
| `exclude_other_billing` | non_credits |
| `exclude_priority` | service_tier |
| `exclude_auto` | service_tier |
| `exclude_flex` | service_tier |
| `exclude_unsupported_route` | unsupported_route |
| `exclude_spend_lease` | settlement_authority |
| `exclude_regional_lease` | settlement_authority |
| `exclude_federated` | settlement_authority |
| `exclude_deferred_home` | settlement_authority |
| `unknown_endpoint` | unsupported_endpoint |
| `unknown_version` | invalid_snapshot |
| `bool_version` | invalid_snapshot |
| `unknown_price_version` | invalid_snapshot |
| `nonzero_endpoint_fee` | invalid_snapshot |
| `negative_rate` | invalid_snapshot |
| `overflow_rate` | invalid_snapshot |
| `unknown_rounding` | invalid_snapshot |
| `unknown_minimum` | invalid_snapshot |
| `clamp_program` | invalid_snapshot |
| `unknown_tier_basis` | invalid_snapshot |
| `unknown_adapter` | invalid_snapshot |
| `unknown_output_convention` | invalid_snapshot |
| `custom_model_identity` | invalid_snapshot |
| `tier_nonfinal_unbounded` | invalid_snapshot |
| `tier_duplicate_boundary` | invalid_snapshot |
| `tier_descending_boundaries` | invalid_snapshot |
| `unknown_context_field_requested` | invalid_context |
| `unknown_context_field_observed` | invalid_context |

## Round 4 local verification (2026-09-28)

The narrow string-type coverage gap is closed with ten numeric-rejection vectors
and ten scoped annotation-widening controls. Nine inputs are otherwise-valid
complete DTOs; `Candidate.model_id` uses the existing field-schema operation to
isolate its declaration from the provider-prefix validator. Each new expectation
asserts exactly `string_type` at that binding.

- Shared fixture: **727 cases**. SHA-256:
  `a748ef09cfbd6bdfb2f84fb0b4a05af7030e6a1a6c69e2a54cf20a096bfcef4b`.
  All original **717 cases are byte-identical**; reconstruction matches the
  Round 3 file and its pinned SHA-256. The Round 2 reconstruction also passes.
- Each new control: **red 1/1 selected → green 1/1 selected**. Independently
  running the complete corpus against each of the ten mutations leaves exactly
  its new vector failing, with **726/727 passing**; restoration gives **727/727**.
  Per-binding evidence appears in the inventory table and external logs.
- Requested regression command: **2,155 passed**, 820 warnings, in 101.89 seconds.
  The final mutation-only run (including the unselected-case assertion) passed
  **388 tests** in 4.65 seconds. All **377 mutation controls** emitted
  **540/540 red selected → 540/540 green selected** checks. Ten additional
  inventory deletion tests pass; they are not counted as evaluator controls.
- Repository-wide `ruff check --no-cache .`: **All checks passed!**
- `mypy src/trusted_router`: **Success: no issues found in 401 source files**.
- Evaluator bytes are unchanged, SHA-256:
  `7c01f7896eb8ce23968b1230d0967eb2204529ab42e903d9697ea771ec166758`.
- This round used the explicitly requested regression selection. The broader
  full-suite and coverage results below are historical Round 3 evidence.

Changes are limited to this document, the fixture/schema/rule manifest and the
contract/mutation tests. No git writes were performed; everything remains
uncommitted. Logs and before/after audit material are under
`/private/tmp/billing-v1-round4`; no scratch files were added to the worktree.

## Round 3 local verification (2026-09-28)

Changes are confined to this document, the literal fixture/schema, the new rule
manifest and the two contract/mutation test modules. No git writes, production
imports, evaluator changes, or settlement differential changes were made.

- Shared fixture: **717 cases**, including all original 102 byte-identical cases.
  SHA-256: `949e7eab8be042cb3d8044c0ba087ff0c9e98c96c7894e2f0c3ebd27b2db185b`.
- Final requested regression selection: **2,125 passed** (112.83 seconds), including
  all 84 original real-settlement differential cases. All **367 mutation
  controls** emitted **530/530 red selected → 530/530 green selected** checks,
  with per-control counts recorded in the inventory; raw evidence is retained in the external verification directory.
- The inventory consistency check also passes after its final strengthening:
  exact table rows match the rule manifest, and every declared DTO/field must
  have an inventory entry.
- Repository-wide `ruff check --no-cache .`: **All checks passed!**
- `mypy src/trusted_router`: **Success: no issues found in 401 source files**.
- Fresh application creation: **393 routes**, with `billing_snapshot` absent
  from `sys.modules`. Source search finds no production imports.
- Evaluator byte SHA-256 remains
  `7c01f7896eb8ce23968b1230d0967eb2204529ab42e903d9697ea771ec166758`.
- Completed full-suite sweep plus final focused update: **16,099 passed,
  491 skipped, 12 xfailed** across **16,602 unique final-tree test IDs**. A
  collection/JUnit union audit found **zero missing and zero extra IDs**.
  This is a recovered sweep, **not one uninterrupted green invocation**.
- The initial isolated two-worker run completed with **15,840 passed,
  491 skipped, 12 xfailed, 7 failed, 2 errors** (5,673.36 seconds). Seven
  unsuccessful cases were HTTP 408 request-body timeouts. One worker crashed
  during the service-surface routing test. All eight passed in a fresh serial
  run, which also validated every final billing fixture and mutation control.
- The ninth failure was the unknown-cloud rollout-verifier test: the temporary
  checkout had acquired an empty Python 3.11 `.venv` from a child `uv` invocation;
  the script selected it and failed with `ModuleNotFoundError: pydantic`.
  Pointing **only the external copy's** `.venv` at the prescribed existing
  interpreter resolved it. The verifier then passed. No application/test gate
  or source code was changed to resolve these nine results.
- The initial full run used an earlier fixture revision; the final focused
  run added/updated all changed contract cases and controls. The JUnit audit
  accounts for the **250 additional final-tree nodes** explicitly.
- Fresh repository line/branch coverage: **73.6699%**, passing
  `coverage report --fail-under=70`. The initial completed broad run alone
  measured **73.1675%** despite the worker crash.
- A separate **unmutated** billing run passed **740 tests** and measured
  **100% line and branch coverage** (254 statements, 82 branches). This avoids
  attributing mutant-source execution to baseline billing coverage.

Checks use `PYTHONPATH=src` and
`/Users/jperla/josh/repos/tr/quill-router/.venv/bin/python`, with bytecode and
pytest cache writes disabled. Logs, mutation evidence, pytest temporary files,
coverage and the isolated full-suite source copy live under
`/private/tmp/billing-v1-round3`; nothing temporary was added to the worktree.

## Round 2 local verification (2026-09-28)

Added five literal rejection vectors (102 total), constrained the fixture
schema's error-code list, and fixed the malformed-tier unit test to use valid
rates and match the tier-ordering error. The new `invalid_context` harness
branch validates eligibility inside expected-error handling for both phases.
The evaluator source is byte-for-byte unchanged; source search finds no
production imports of `billing_snapshot`.

- Exact requested regression selection: **1,152 passed** (136.14 seconds).
- All nine mutation controls: **9 passed**. Skipping tier validation gives
  **red 3/3 selected → green 3/3 selected**; ignoring unknown eligibility fields
  gives **red 2/2 selected → green 2/2 selected**. All seven existing controls
  still give red 1/1 selected → green 1/1 selected.
- Repository-wide `ruff check --no-cache .`: **All checks passed!**
- `mypy src/trusted_router`: **Success: no issues found in 401 source files**.
- An additional full CI-scope run (`not provider_health`, four workers, coverage)
  was interrupted under severe machine memory pressure after **4,383 passed,
  443 skipped, 12 xfailed, 5 failed, 2 errors** (1,159.19 seconds). All seven
  failures/errors were HTTP 408 request-body timeouts in existing client-events,
  core API, and credit-transfer tests; a fresh serial rerun of those exact seven
  cases **passed in 3.09 seconds**. This is not a completed full-suite result,
  and this interrupted run did not produce a new coverage measurement.

Checks use the requested existing Python interpreter with bytecode and pytest
cache writes disabled. Logs and caches live under `/private/tmp/billing-v1-round2`.
The additional full CI-scope attempt and its serial rerun used an external
source copy so generated assets could not modify this worktree. No git writes or production changes were made.

## Round 1 local verification (2026-09-27)

Base commit: `67655db55cb0863a80854eb0e51879500840e5c6` (also the observed
`origin/main`). No git writes, route wiring, existing production-file edits,
`tests/conftest.py` edits, or `storage_gcp_authorize.py` edits were made.

Round 1 fixture SHA-256 (superseded by the Round 2 pin above):
`58a78e294ff5a1f39f0beadf73e3d4caecfde4754af86452fc7488385685ba91`.

- Exact requested regression selection, including the final new tests:
  **1,187 passed**. Its 84 differential cases cover all 28 positive goldens
  through synchronous, inline-outbox and recovered-outbox commits.
- Every mutation: **red 1/1 selected → green 1/1 selected**, with module state
  restored in `finally`. The seven metatests pass.
- Full CI-scope collection (`not provider_health`): **15,621 unique cases**
  accounted for by a collection/checkpoint/JUnit audit, with final outcomes
  **15,119 passed, 490 skipped, 12 expected failures**. This was a recovered,
  chunked sweep, not a single uninterrupted green invocation. One initial
  failure came from a child `uv` process trying to write the sandbox-blocked
  home cache; four others were HTTP 408 request-body timeouts in the long run.
  All five passed on a clean base-commit archive and in fresh current-tree runs
  with an external cache and the existing interpreter (`UV_NO_SYNC=1`).
- Postgres/Spanner server conformance was not configured; those cases skipped.
  Existing memory and fake-backed checks ran. The time-sensitive
  `provider_health` case was deselected, as in CI.
- The interrupted sweep did not save coverage. Completed chunks plus fresh
  regression, gateway and billing runs retain **71.23% repository coverage**,
  passing `coverage report --fail-under=70`. This is a conservative measurement
  that omits the lost prefix data. The unmutated billing module independently
  measures **98%** (121 pure-contract tests).
- Repository-wide `ruff check .` passes. Both `mypy src/trusted_router` and
  CI's default `mypy` pass: **401 source files**.
- CI's `npx tsc`, generated `dashboard.js` parity, `npx eslint frontend/src`,
  and `npx stylelint "src/trusted_router/static/*.css"` pass under Node 20.
  Frontend dependencies and compilation were confined to an identical temporary
  source copy. CI declares no formatter command. The BYOK format-ordering gate
  also passes without network reads.

Python checks used
`/Users/jperla/josh/repos/tr/quill-router/.venv/bin/python`, with `PYTHONPATH=src`,
bytecode disabled and pytest's cache provider disabled. Caches, baseline copy,
coverage files, JUnit reports, audit and logs live under
`/private/tmp/billing-v1-checks`, outside the worktree.
