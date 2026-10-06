"""Offline mutation authoring. Literal choices are manual, never verifier outputs.

AST inspection locates predicates, not expected verdicts. Tests never import this
file. Frozen rules.json plus tests/speculation_equality_rules.json contain
the executable before/after edits for the current implementation.
"""
import ast
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3] / 'src/trusted_router/speculation_protocol.py'

# Each require site must be explicitly assigned an isolating literal. Repeated
# sites with one code are listed in source traversal order.
SITES = {
    '_integer': ['bool_integer'], '_string': ['string_empty'], '_hash': ['invalid_hash'],
    '_object': ['unknown_field'], '_json': ['payload_duplicate'],
    '_parse_int': ['payload_number_huge'], '_depth': ['depth17'],
    '_b64decode': ['base64_empty', 'base64_trailing_bits'],
    '_verify': ['compact_extra', 'header_whitespace', 'algorithm_none', 'unknown_type',
                'key_ambiguous', 'real_type_shadow_key', 'depth16'],
    '_check_canonical': ['payload_whitespace'], '_route_schema': ['precedence_stage_schema_version'],
    '_grant_schema': ['empty_permits'],
    'cost_ceiling': ['money_product_overflow', 'money_sum_overflow'],
    'verify_grant': ['unknown_version', 'identity_iss', 'binding_key_id', 'stage_d_false',
                     'route_endpoint_id', 'route_region_disagrees', 'output_bound_over',
                     'adapter_zero', 'tier_one', 'unpaid', 'history_19', 'stale_last_success',
                     'ttl_too_long', 'start_plus_28', 'over_cap_money', 'cost_10001',
                     'duplicate_ordinal', 'underfunded_permit', 'missing_tier_ceiling', 'allowance_sum_not_max'],
    'verify_descriptor': ['dry_run_cannot_dispatch', 'descriptor_version', 'descriptor_wrong_boot_signer',
                          'descriptor_key_id', 'descriptor_shadow_hash', 'descriptor_wire_lf',
                          'descriptor_nonce', 'descriptor_endpoint', 'descriptor_ordinal'],
    'verify_acceptance': ['response_stage_d_integer', 'marker_unmarked_key', 'unmarked_spend_lease',
                          'marker_version', 'marker_hash', 'marker_invocation_nonce',
                          'marker_authorization_id', 'authorization_stage_d'],
    'renewal_verdict': ['renewal_domain_isolated', 'renewal_identity_workspace_id', 'renewal_same_generation'],
    'descriptor_replay': ['descriptor_permit_reuse'],
    'classify_verdict': ['provider_source', 'success_status'],
    '_json_bytes': ['payload_escaped_ascii'],
}


def inventory():
    source = SOURCE.read_text()
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    rows = []

    def add(function, before, after, case, guard=None, equivalent=None):
        node = functions[function]
        body = ast.get_source_segment(source, node)
        assert body.count(before) == 1, (function, before, body.count(before))
        row = {'guard': guard or function + ':' + before + ' => ' + after, 'function': function,
               'before': before, 'after': after, 'literal_case': case}
        if equivalent:
            row['equivalent'] = equivalent
        rows.append(row)

    for function, cases in SITES.items():
        calls = sorted((n for n in ast.walk(functions[function]) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == '_require'), key=lambda n: n.lineno)
        assert len(calls) == len(cases), (function, len(calls), len(cases))
        for call, case in zip(calls, cases, strict=True):
            before = ast.get_source_segment(source, call)
            add(function, before, 'pass', case)

    # Atomic conditions, reason memberships, loop-expanded identities, arithmetic,
    # exception translation and dispatch decisions need more than whole-guard deletion.
    atomic = [
        ('_integer', 'type(value) is int', 'isinstance(value, int)', 'bool_integer'),
        ('_integer', '0 <= value', 'True', 'money_negative'),
        ('_integer', 'value <= MAX_INT', 'True', 'overflow_integer'),
        ('_string', 'isinstance(value, str)', 'True', 'string_scalar'),
        ('_string', 'bool(value)', 'True', 'string_empty'),
        ('_string', '0x20 <= ord(c)', 'True', 'context_string_control'),
        ('_string', 'ord(c) <= 0x7E', 'True', 'context_string_unicode'),
        ('_string', "c not in '\\\\\"<>&'", 'True', 'string_less'),
        ('_hash', 'isinstance(value, str)', 'True', 'hash_type'),
        ('_object', 'isinstance(value, dict)', 'True', 'header_scalar'),
        ('_object', 'set(value) == set(', 'set(value) <= set(', 'missing_provenance'),
        ('_parse_int', '    _integer(number)', '    pass', 'header_number_negative'),
        ('_constant', 'raise ProtocolError("integer")', 'return 1', 'exponent'),
        ('_json', 'raise ProtocolError("json") from exc', 'raise ProtocolError("input") from exc', 'json_bad'),
        ('_json_bytes', 'byte != 0x5C', 'True', 'payload_escaped_ascii'),
        ('_json', '    _depth(text)', '    pass', 'depth17'),
        ('_b64decode', 'bool(value)', 'True', 'key_zero_public'),
        ('_b64decode', 'raise ProtocolError("base64") from exc', 'raise ProtocolError("input") from exc', 'base64_length'),
        ('_verify', 'isinstance(token, str)', 'True', 'compact_nonstring'),
        ('_verify', 'len(token) <= 65536', 'True', 'compact_too_long'),
        ('_verify', 'token.count(".") == 2', 'True', 'compact_extra'),
        ('_verify', 'key.kid == header["kid"]', 'True', 'real_valid'),
        ('_verify', 'signature, (h + "." + p).encode("ascii")', 'signature, b"wrong"', 'real_valid'),
        ('_verify', 'raise ProtocolError("signature") from exc', 'pass', 'signature_bitflip'),
        ('_grant_schema', 'isinstance(claims["permits"], list)', 'True', 'scalar_permits'),
        ('_grant_schema', 'bool(claims["permits"])', 'True', 'empty_permits'),
        ('cost_ceiling', 'product % 1_000_000 != 0', 'False', 'money_fractional'),
        ('cost_ceiling', 'total = maximum_request_fees_micro', 'total = 0', 'money_fees'),
        ('workspace_allowance', 'tier_ceiling_micro // 100', '(tier_ceiling_micro + 99) // 100', 'allowance_odd_tier'),
        ('workspace_allowance', 'paid_headroom_micro // 10', '(paid_headroom_micro + 9) // 10', 'allowance_odd_headroom'),
        ('workspace_allowance', ', 1_000_000)', ')', 'allowance_dollar_cap'),
        ('verify_grant', 'field in context and ', '', 'missing_context_binding'),
        ('verify_grant', '    _route_schema(context.get("route"))', '    pass', 'context_coercion_stage_d'),
        ('verify_grant', '0 < route["input_bound"]', 'True', 'input_bound_zero'),
        ('verify_grant', 'route["input_bound"] <= 8192', 'True', 'input_bound_over'),
        ('verify_grant', '0 < route["output_limit"]', 'True', 'output_bound_zero'),
        ('verify_grant', 'route["output_limit"] <= 512', 'True', 'output_bound_over'),
        ('verify_grant', 'claims["tier"] in (2, 3)', 'claims["tier"] >= 2', 'tier_four'),
        ('verify_grant', 'history["count"] >= 20', 'True', 'history_19'),
        ('verify_grant', 'history["sequence"] >= history["count"]', 'True', 'history_retry_dedupe'),
        ('verify_grant', 'issued - 600 <= history["window_start"]', 'True', 'old_history_window'),
        ('verify_grant', 'history["window_start"] <= history["last_success_at"]', 'True', 'future_history_window'),
        ('verify_grant', 'history["last_success_at"] <= issued', 'True', 'future_last_success'),
        ('verify_grant', 'history["last_success_at"] >= issued - 30', 'True', 'stale_last_success'),
        ('verify_grant', 'history["clean_since"] <= issued - 900', 'True', 'unclean_history'),
        ('verify_grant', '0 < claims["exp"] - issued', 'True', 'ttl_zero'),
        ('verify_grant', 'claims["exp"] - issued <= 30', 'True', 'ttl_too_long'),
        ('verify_grant', 'issued < claims["start_before"]', 'True', 'start_before_issue'),
        ('verify_grant', 'claims["start_before"], claims["exp"] - MARGIN', 'MAX_INT, claims["exp"] - MARGIN', 'short_explicit_at'),
        ('verify_grant', 'claims["exp"] - MARGIN', 'MAX_INT', 'short_expiry_at'),
        ('verify_grant', 'claims["key_expires_at"] - MARGIN', 'MAX_INT', 'short_key_at'),
        ('verify_grant', 'route["price_expires_at"] - MARGIN', 'MAX_INT', 'short_price_at'),
        ('verify_grant', 'claims["trust_fresh_until"] - MARGIN', 'MAX_INT', 'short_trust_at'),
        ('verify_grant', 'issued <= now', 'True', 'future_iat'),
        ('verify_grant', 'now < deadline', 'now <= deadline', 'start_plus_28'),
        ('verify_grant', '0 < ceiling', 'True', 'zero_ceiling'),
        ('verify_grant', 'ceiling <= 10_000', 'True', 'over_cap_money'),
        ('verify_grant', '0 < b_micro', 'True', 'cost_zero'),
        ('verify_grant', 'b_micro <= ceiling', 'True', 'cost_10001'),
        ('verify_grant', 'b_micro <= permit["b_micro"]', 'True', 'underfunded_permit'),
        ('verify_grant', 'permit["b_micro"] <= ceiling', 'True', 'over_cap_permit'),
        ('verify_grant', 'sum(p["b_micro"] for p in permits)', 'max(p["b_micro"] for p in permits)', 'allowance_sum_not_max'),
        ('verify_descriptor', '_equal(claims["execution_id"], execution_id)', 'True', 'descriptor_execution'),
        ('verify_descriptor', '_equal(claims["invocation_nonce"], invocation_nonce)', 'True', 'descriptor_nonce'),
        ('verify_descriptor', 'p["ordinal"] == claims["ordinal"]', 'True', 'descriptor_ordinal'),
        ('verify_descriptor', 'p["b_micro"] == claims["b_micro"]', 'True', 'descriptor_cost'),
        ('verify_acceptance', '"speculation_accepted" not in response', 'response.get("speculation_accepted") is None', 'marker_null'),
        ('verify_acceptance', 'authorization.get("stage_d") is True', 'True', 'authorization_stage_d'),
        ('renewal_verdict', 'previous.compact == candidate.compact', 'False', 'renewal_exact_replay'),
        ('renewal_verdict', 'new["grant_id"] != old["grant_id"]', 'True', 'renewal_same_grant_id'),
        ('renewal_verdict', 'new["generation"] > old["generation"]', 'True', 'renewal_same_generation'),
        ('renewal_verdict', 'new["iat"] >= old["iat"]', 'True', 'renewal_old_iat'),
        ('renewal_verdict', 'new["workspace_epoch"] >= old["workspace_epoch"]', 'True', 'renewal_old_workspace_epoch'),
        ('renewal_verdict', 'new["key_epoch"] >= old["key_epoch"]', 'True', 'renewal_old_key_epoch'),
        ('renewal_verdict', 'new["history"]["sequence"] >= old["history"]["sequence"]', 'True', 'renewal_old_history_sequence'),
        ('classify_verdict', '400 <= status', 'True', 'success_status'),
        ('classify_verdict', 'status <= 599', 'True', 'verdict_status_upper'),
        ('classify_verdict', 'reason in KEY_REASONS', 'False', 'verdict:lifetime_limit'),
        ('classify_verdict', 'reason in KEY_REASONS and workspace_id and key_id', 'reason in KEY_REASONS and key_id', 'verdict_key_without_workspace'),
        ('classify_verdict', 'reason in KEY_REASONS and workspace_id and key_id', 'reason in KEY_REASONS and workspace_id', 'verdict_key_without_key'),
        ('classify_verdict', 'workspace_id and (reason in WORKSPACE_REASONS or status == 402)', '(reason in WORKSPACE_REASONS or status == 402)', 'verdict_workspace_without_workspace'),
        ('classify_verdict', 'reason in WORKSPACE_REASONS', 'False', 'verdict_billing_paused'),
        ('classify_verdict', 'status == 402', 'False', 'verdict_reasonless_402'),
        ('classify_verdict', 'status == 429', 'False', 'verdict:rate_unknown'),
        ('classify_verdict', 'status == 429 and workspace_id', 'status == 429', 'verdict_rate_without_workspace'),
        ('classify_verdict', 'rate_scope == "key" and key_id', 'rate_scope == "key"', 'verdict_rate_key_missing_id'),
        ('classify_verdict', 'rate_scope == "key"', 'True', 'verdict:rate_unknown'),
        ('classify_verdict', 'scope == "none" and ', '', 'verdict_workspace_reason_500'),
        ('classify_verdict', 'status >= 500', 'False', 'verdict_generic_500'),
        ('classify_verdict', 'scope != "none"', 'False', 'verdict:credit'),
    ]
    # Two identical output scope predicates share one executable mutation.
    for function, before, after, case in atomic:
        if function == 'classify_verdict' and before == 'scope != "none"':
            before = '"commit_required_with_real_rights": scope != "none"'
            after = '"commit_required_with_real_rights": False'
        # Chain comparisons are AST predicates: remove an endpoint without breaking syntax.
        chains = {
            'value <= MAX_INT': ('0 <= value <= MAX_INT', '0 <= value'),
            'ord(c) <= 0x7E': ('0x20 <= ord(c) <= 0x7E', '0x20 <= ord(c)'),
            'now < deadline': ('issued <= now < deadline', 'issued <= now <= deadline'),
            'ceiling <= 10_000': ('0 < ceiling <= 10_000', '0 < ceiling'),
            'b_micro <= ceiling': ('0 < b_micro <= ceiling', '0 < b_micro'),
            'permit["b_micro"] <= ceiling': ('b_micro <= permit["b_micro"] <= ceiling', 'b_micro <= permit["b_micro"]'),
            'status <= 599': ('400 <= status <= 599', '400 <= status'),
            'route["input_bound"] <= 8192': ('0 < route["input_bound"] <= 8192', '0 < route["input_bound"]'),
            'route["output_limit"] <= 512': ('0 < route["output_limit"] <= 512', '0 < route["output_limit"]'),
            'history["window_start"] <= history["last_success_at"]': ('issued - 600 <= history["window_start"] <= history["last_success_at"] <= issued', 'issued - 600 <= history["window_start"] and history["last_success_at"] <= issued'),
            'history["last_success_at"] <= issued': ('issued - 600 <= history["window_start"] <= history["last_success_at"] <= issued', 'issued - 600 <= history["window_start"] <= history["last_success_at"]'),
            'claims["exp"] - issued <= 30': ('0 < claims["exp"] - issued <= 30', '0 < claims["exp"] - issued'),
        }
        guard = function + ':' + before + ' => ' + after
        if before in chains:
            before, after = chains[before]
        # Removing the left side of a chained comparison retains its right side.
        elif before in ('0 <= value', '0x20 <= ord(c)', '0 < route["input_bound"]', '0 < route["output_limit"]', 'issued - 600 <= history["window_start"]', '0 < claims["exp"] - issued', 'issued <= now', '0 < ceiling', '0 < b_micro', 'b_micro <= permit["b_micro"]', '400 <= status'):
            after = before.split(' <= ')[-1] if ' <= ' in before else before.split(' < ')[-1]
        add(function, before, after, case, guard)

    for function, loop, fields, prefix in [
        ('verify_grant', 'for field in ("iss", "aud", "environment", "plane"):', ['iss', 'aud', 'environment', 'plane'], 'identity_'),
        ('verify_grant', 'for field in BINDINGS:', 'workspace_id key_id lookup_digest boot_id stable_slot_id region generation workspace_epoch key_epoch image_policy_version'.split(), 'binding_'),
        ('verify_descriptor', 'for field in ("grant_id", "workspace_id", "key_id", "boot_id", "workspace_epoch", "key_epoch"):', 'grant_id workspace_id key_id boot_id workspace_epoch key_epoch'.split(), 'descriptor_'),
        ('renewal_verdict', 'for field in ("workspace_id", "key_id", "lookup_digest", "boot_id", "stable_slot_id",\n                  "iss", "aud", "environment", "plane", "region"):', 'workspace_id key_id lookup_digest boot_id stable_slot_id iss aud environment plane region'.split(), 'renewal_identity_'),
    ]:
        for field in fields:
            add(function, loop, loop + '\n        if field == ' + repr(field) + ':\n            continue', prefix + field, function + ':identity:' + field)
    # Caller graph mutations live outside the byte-frozen wire fixture inventory.
    for row in json.loads((SOURCE.parents[2] / 'tests/speculation_equality_rules.json').read_text()):
        add(row['function'], row['before'], row['after'], row['literal_case'])
    add('_depth', 'if quoted:', 'if False:', 'depth_in_string')
    # Closing quotes, opening quotes, opens and closes each have a distinguishing payload.
    add('_depth', "if char == '\"':\n                quoted = False", 'if False:\n                quoted = False', 'depth17_after_string')
    add('_depth', "elif char == '\"':\n            quoted = True", 'elif False:\n            quoted = True', 'depth_in_string')
    add('_depth', 'elif char in "[{":', 'elif False:', 'depth17')
    add('_depth', 'elif char in "]}":', 'elif False:', 'depth_siblings')
    add('_public', 'raise ProtocolError("input") from exc', 'raise', 'input_boundary')
    # Membership is a distinct policy predicate for EACH reason.
    for constant, mapping in [
        ('WORKSPACE_REASONS', dict(credit_exhausted='credit', billing_denied='ambiguous_billing', trust_ineligible='trust', trust_demoted='verdict_trust_demoted', abuse_latched='abuse', payment_failed='payment', trust_reconciliation_stale='trust_stale', workspace_paused='paused', billing_paused='verdict_billing_paused')),
        ('KEY_REASONS', dict(key_revoked='revoked', key_disabled='disabled', key_expired='expired', key_invalid='invalid_resolved', key_limit_exceeded='lifetime_limit', key_window_limit_exceeded='window_limit', key_strict_limit_exceeded='strict_limit', key_spend_limit_imposed='new_limit')),
    ]:
        for reason in mapping:
            # Use reason-only 403 literals so generic 402 cannot mask deletion.
            rows.append({'guard': constant + ':' + reason, 'function': '<module>',
                         'before': '"' + reason + '"', 'after': '"unused_' + reason + '"',
                         'literal_case': 'reason_' + reason})
    for reason in ('authorize_timeout', 'transport_error', 'infrastructure_error'):
        add('classify_verdict', '"'+reason+'"', '"unused_'+reason+'"', 'breaker_'+reason)
    add('classify_verdict', '503 if scope != "none" else status', 'status', 'verdict:credit')
    rows.append({'guard': 'fixture_pin', 'function': '<fixture>', 'before': 'fixture',
                 'after': 'fixturf', 'literal_case': 'fixture_pins'})
    add('_require', 'not condition', 'False', 'bool_integer')
    add('verify_grant', 'SHADOW_TYP if shadow else REAL_TYP', 'REAL_TYP', 'shadow_valid')
    add('verify_grant', '"shadow-grant" if shadow else "grant"', '"grant"', 'shadow_valid')
    for function, loop, mapping in [
        ('verify_grant', 'for field in ("routing_policy_hash", "catalog_hash"):', {'routing_policy_hash':'route_policy_hash_format', 'catalog_hash':'route_hash_bad'}),
        ('verify_descriptor', 'for field in ("grant_sha256", "request_sha256", "routing_policy_hash"):', {'grant_sha256':'descriptor_grant_hash_format', 'request_sha256':'descriptor_hash_malformed', 'routing_policy_hash':'descriptor_route_hash_format'}),
        ('verify_descriptor', 'for field in ("endpoint_id", "routing_policy_hash"):', {'endpoint_id':'descriptor_endpoint', 'routing_policy_hash':'descriptor_routing_policy_hash'}),
        ('verify_acceptance', 'for field in ("invocation_nonce", "workspace_id", "key_id"):', {f:'unmarked_'+f for f in ('invocation_nonce', 'workspace_id', 'key_id')}),
        ('verify_acceptance', 'for field in ("invocation_nonce", "endpoint_id", "routing_policy_hash"):', {f:'marker_'+f for f in ('invocation_nonce', 'endpoint_id', 'routing_policy_hash')}),
        ('verify_acceptance', 'for field in ("authorization_id", "invocation_nonce", "endpoint_id", "routing_policy_hash",\n                  "workspace_id", "key_id"):', {f:'authorization_'+f for f in ('authorization_id', 'invocation_nonce', 'endpoint_id', 'routing_policy_hash', 'workspace_id', 'key_id')}),
    ]:
        for field, case in mapping.items():
            equivalent = None
            if function == 'verify_acceptance' and 'authorization_id' in loop and field in ('invocation_nonce', 'workspace_id', 'key_id'):
                equivalent = 'Equivalent: the pre-marker loop already binds authorization ' + field + ' to the descriptor; marker nonce also binds to that descriptor before this loop.'
            add(function, loop, loop + '\n        if field == ' + repr(field) + ':\n            continue', case, function + ':field:' + field + ':' + loop, equivalent=equivalent)
    add('verify_acceptance', 'authorization.get("billing_mode") == "ordinary" and', 'True and', 'unmarked_spend_lease', equivalent='Equivalent: ordinary billing was already required before the marker-presence branch; no mutation or external call intervenes.')
    for row in rows:
        key = (row['function'], row['before'])
        equivalents = {
            ('_verify', '_require(isinstance(claims, dict), "fields")'):
                'Equivalent: both callers immediately call exact-object schema validation, which rejects every nondict as fields before accessing members.',
            ('_hash', 'isinstance(value, str)'):
                'Equivalent: every public path validates these claims with _string before _hash. Nonstrings cannot reach the redundant hash type conjunct.',
        }
        if key in equivalents:
            row['equivalent'] = equivalents[key]
    add('_json_bytes', '0x20 <= byte', '0 <= byte', 'trailing_newline')
    add('_json_bytes', '0x20 <= byte <= 0x7E', '0x20 <= byte <= 0xFF', 'raw_del')
    add('_verify', '    _json_bytes(payload)', '    pass', 'precedence_payload_bytes_signature')
    add('_json', 'parse_int=str, parse_float=str, parse_constant=str',
        'parse_int=_parse_int, parse_float=_constant, parse_constant=_constant',
        'payload_malformed_float_exponent')
    add('_pairs', 'duplicates.append(key in result)', 'pass', 'payload_duplicate')
    return rows
