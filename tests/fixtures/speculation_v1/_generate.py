"""Offline, test-only fixture signer. Never imported or executed by tests.

Run explicitly with the repository Python environment. Expected outcomes below
are reviewed literals; no production imports and no production parser oracle.
Seeds are public test material, unsuitable for any deployed trust manifest.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).parent
REAL = "speculation-eligibility+jws"
SHADOW = "speculation-eligibility-shadow+jws"
DESCRIPTOR = "speculation-descriptor+jws"
SEEDS = {
    "issuer-fixture": "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f",
    "shadow-fixture": "202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f",
    "boot-a": "404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f",
    "boot-other": "606162636465666768696a6b6c6d6e6f707172737475767778797a7b7c7d7e7f",
}
KEYS = {kid: Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        for kid, seed in SEEDS.items()}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(claims, kid="issuer-fixture", typ=REAL, header=None, raw=None, segment=None):
    h = b64(canonical(header if header is not None else {"alg": "EdDSA", "kid": kid, "typ": typ}))
    p = segment if segment is not None else b64(raw if raw is not None else canonical(claims))
    message = h + "." + p
    return message + "." + b64(KEYS[kid].sign(message.encode("ascii")))


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


BASE = {'expected': {'b_micro': 4608,
              'customer_settlement_mode': 'ordinary',
              'descriptor_signature_valid': True,
              'real_signature_valid': True,
              'real_start_allowed': True,
              'shadow_dispatch_allowed': False,
              'shadow_signature_valid': True,
              'start_at_1700000027': True,
              'start_at_1700000028': False,
              'workspace_allowance_micro': 250000},
 'fixture_version': 1,
 'grant_claims': {'aud': 'fixture-speculation',
                  'boot_id': 'boot-a',
                  'environment': 'test',
                  'exp': 1700000030,
                  'generation': 7,
                  'grant_id': 'g1',
                  'history': {'clean_since': 1699999099,
                              'count': 20,
                              'last_success_at': 1699999999,
                              'sequence': 20,
                              'window_start': 1699999400},
                  'iat': 1700000000,
                  'image_policy_version': 1,
                  'iss': 'fixture-router',
                  'key_epoch': 5,
                  'key_expires_at': 1700003600,
                  'key_id': 'k1',
                  'lookup_digest': '853bab448f7a802bd58136ffc29727189a74eaac72025a76cdc9b11058a6e7d0',
                  'paid_headroom_micro': 5000000,
                  'per_request_ceiling_micro': 10000,
                  'permits': [{'b_micro': 4608, 'ordinal': 0}, {'b_micro': 4608, 'ordinal': 1}],
                  'plane': 'gcp-fixture',
                  'region': 'us-central1',
                  'route': {'adapter_capability_version': 1,
                            'catalog_hash': '7fc58396c2dcf1684849862201f847ad76a2efe04111a3224a2be7aa02d23e42',
                            'endpoint_id': 'ep1',
                            'input_bound': 8192,
                            'input_bound_method': 'certified-fixture-bound-v1',
                            'input_rate_micro_per_m': 500000,
                            'maximum_request_fees_micro': 0,
                            'output_limit': 512,
                            'output_rate_micro_per_m': 1000000,
                            'price_expires_at': 1700000120,
                            'privacy': 'default',
                            'provider': 'fixture-provider',
                            'region': 'us-central1',
                            'routing_policy_hash': 'a5593a324b738638e898a04cc2edb314973b0b5312742cb371db7df43d8e691c',
                            'stage_d': True,
                            'upstream_model': 'fixture-text'},
                  'stable_slot_id': 'slot-a',
                  'start_before': 1700000028,
                  'tier': 2,
                  'trust_fresh_until': 1700000120,
                  'v': 1,
                  'workspace_epoch': 3,
                  'workspace_id': 'w1'},
 'now': 1700000000,
 'permit_descriptor_claims': {'b_micro': 4608,
                              'boot_id': 'boot-a',
                              'endpoint_id': 'ep1',
                              'execution_id': 'x1',
                              'grant_id': 'g1',
                              'grant_sha256': '39578719b2dd0a0e3928f4dd7326f7a7f3e7368df5d22463a864a4fe83364ee7',
                              'invocation_nonce': 'n1',
                              'key_epoch': 5,
                              'key_id': 'k1',
                              'ordinal': 0,
                              'request_sha256': '75e9f95d9e202c66fcffb7d4c7aa7c88e3dfb80a51e2cddc7539a6e461188f88',
                              'routing_policy_hash': 'a5593a324b738638e898a04cc2edb314973b0b5312742cb371db7df43d8e691c',
                              'v': 1,
                              'workspace_epoch': 3,
                              'workspace_id': 'w1'},
 'provider_wire_exact_bytes': 'UTF-8 file bytes excluding the single final LF',
 'provider_wire_file': 'provider-wire.json',
 'speculation_accepted': {'authorization_id': 'a1',
                          'descriptor_sha256': '58204c390d5a2684a72c09648857abfdaaa9a52cb4cf2e12bde02c2580c26d77',
                          'endpoint_id': 'ep1',
                          'invocation_nonce': 'n1',
                          'routing_policy_hash': 'a5593a324b738638e898a04cc2edb314973b0b5312742cb371db7df43d8e691c',
                          'v': 1}}

VERDICTS = '{\n  "fixture_version": 1,\n  "note": "R8 must map actual router errors to these reasons using resolved identity; authenticated state reasons take precedence over generic 5xx. Provider errors never enter this taxonomy.",\n  "reason_namespace": "normalized_authenticated_authorize_reason_v1",\n  "vectors": [\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "credit_exhausted",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "credit"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "billing_denied",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "ambiguous_billing"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "trust_ineligible",\n        "source": "authenticated_router",\n        "status": 403,\n        "workspace_id": "w1"\n      },\n      "name": "trust"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "abuse_latched",\n        "source": "authenticated_router",\n        "status": 403,\n        "workspace_id": "w1"\n      },\n      "name": "abuse"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "payment_failed",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "payment"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "trust_reconciliation_stale",\n        "source": "authenticated_router",\n        "status": 503,\n        "workspace_id": "w1"\n      },\n      "name": "trust_stale"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "workspace_paused",\n        "source": "authenticated_router",\n        "status": 503,\n        "workspace_id": "w1"\n      },\n      "name": "paused"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_revoked",\n        "source": "authenticated_router",\n        "status": 401,\n        "workspace_id": "w1"\n      },\n      "name": "revoked"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_disabled",\n        "source": "authenticated_router",\n        "status": 401,\n        "workspace_id": "w1"\n      },\n      "name": "disabled"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_expired",\n        "source": "authenticated_router",\n        "status": 401,\n        "workspace_id": "w1"\n      },\n      "name": "expired"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_invalid",\n        "source": "authenticated_router",\n        "status": 403,\n        "workspace_id": "w1"\n      },\n      "name": "invalid_resolved"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_limit_exceeded",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "lifetime_limit"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_window_limit_exceeded",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "window_limit"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_strict_limit_exceeded",\n        "source": "authenticated_router",\n        "status": 402,\n        "workspace_id": "w1"\n      },\n      "name": "strict_limit"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "key_spend_limit_imposed",\n        "source": "authenticated_router",\n        "status": 403,\n        "workspace_id": "w1"\n      },\n      "name": "new_limit"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 401\n      },\n      "input": {\n        "key_id": null,\n        "rate_scope": null,\n        "reason": "invalid_api_key",\n        "source": "authenticated_router",\n        "status": 401,\n        "workspace_id": null\n      },\n      "name": "invalid_unresolved"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "key",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": "key",\n        "reason": "rate_limited",\n        "source": "authenticated_router",\n        "status": 429,\n        "workspace_id": "w1"\n      },\n      "name": "rate_key"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": "workspace",\n        "reason": "rate_limited",\n        "source": "authenticated_router",\n        "status": 429,\n        "workspace_id": "w1"\n      },\n      "name": "rate_workspace"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": true,\n        "discard_this_execution": true,\n        "durable_scope": "workspace",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "rate_limited",\n        "source": "authenticated_router",\n        "status": 429,\n        "workspace_id": "w1"\n      },\n      "name": "rate_unknown"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 400\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "invalid_request",\n        "source": "authenticated_router",\n        "status": 400,\n        "workspace_id": "w1"\n      },\n      "name": "request_validation"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 403\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "request_policy_rejected",\n        "source": "authenticated_router",\n        "status": 403,\n        "workspace_id": "w1"\n      },\n      "name": "request_policy"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "none",\n        "storage_failure_status_with_real_rights": 404\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "unsupported_route",\n        "source": "authenticated_router",\n        "status": 404,\n        "workspace_id": "w1"\n      },\n      "name": "request_route"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "key_boot",\n        "storage_failure_status_with_real_rights": 503\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "infrastructure_error",\n        "source": "authenticated_router",\n        "status": 503,\n        "workspace_id": "w1"\n      },\n      "name": "infrastructure"\n    },\n    {\n      "expected": {\n        "commit_required_shadow_only": false,\n        "commit_required_with_real_rights": false,\n        "discard_this_execution": true,\n        "durable_scope": "none",\n        "local_infrastructure_breaker": "key_boot",\n        "storage_failure_status_with_real_rights": 504\n      },\n      "input": {\n        "key_id": "k1",\n        "rate_scope": null,\n        "reason": "authorize_timeout",\n        "source": "authenticated_router",\n        "status": 504,\n        "workspace_id": "w1"\n      },\n      "name": "timeout"\n    }\n  ]\n}\n'
WIRE = '{"max_tokens":512,"messages":[{"content":"fixture","role":"user"}],"model":"fixture-text","stream":true}\n'

# Hash literals are deliberately updated only after independently re-signing.
GRANT_HASH = "ebace849f7d56f32eafee693310ba9cda13ab17f571b950e476aaeee13389055"
DESCRIPTOR_HASH = "846aec09fd7b9e7e581a6b017e451a2e33ac941d7f4f3cd23f211beacaf5a8e5"


def main():
    bundle = copy.deepcopy(BASE)
    grant = bundle["grant_claims"]
    real = sign(grant)
    shadow = sign(grant, "shadow-fixture", SHADOW)
    actual_grant_hash = hashlib.sha256(real.encode()).hexdigest()
    assert actual_grant_hash == GRANT_HASH
    descriptor = bundle["permit_descriptor_claims"]
    descriptor["grant_sha256"] = GRANT_HASH
    signed_descriptor = sign(descriptor, "boot-a", DESCRIPTOR)
    actual_descriptor_hash = hashlib.sha256(signed_descriptor.encode()).hexdigest()
    assert actual_descriptor_hash == DESCRIPTOR_HASH
    bundle["speculation_accepted"]["descriptor_sha256"] = DESCRIPTOR_HASH
    bundle.update(real_grant_jws=real, shadow_grant_jws=shadow,
                  permit_descriptor_jws=signed_descriptor)
    trusted_keys = []
    for kid, purpose in [("issuer-fixture", "grant"), ("shadow-fixture", "shadow-grant"),
                         ("boot-a", "descriptor"), ("boot-other", "descriptor")]:
        key = {"kid": kid, "purpose": purpose,
               "public_key_b64url": b64(KEYS[kid].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}
        if purpose != "descriptor":
            key.update(iss="fixture-router", aud="fixture-speculation", environment="test", plane="gcp-fixture")
        trusted_keys.append(key)
    bundle["trusted_test_keys"] = trusted_keys
    context = {k: grant[k] for k in (
        "workspace_id key_id lookup_digest boot_id stable_slot_id region generation "
        "workspace_epoch key_epoch image_policy_version").split()}
    context.update(route=copy.deepcopy(grant["route"]), tier_ceiling_micro=25_000_000)
    bundle["context"] = context
    authorization = {
        "authorization_id": "a1", "invocation_nonce": "n1", "endpoint_id": "ep1",
        "routing_policy_hash": "a5593a324b738638e898a04cc2edb314973b0b5312742cb371db7df43d8e691c",
        "workspace_id": "w1", "key_id": "k1", "billing_mode": "ordinary", "stage_d": True,
    }
    bundle["authorization"] = authorization
    cases = []

    def add(name, category, expected, **inputs):
        cases.append({"name": name, "category": category, "expected": expected, **inputs})

    def grant_case(name, expected, changes=None, *, ctx=None, now=1700000000,
                   shadow_mode=False, kid="issuer-fixture", typ=REAL, **encoding):
        c = copy.deepcopy(grant)
        for path, value in (changes or {}).items():
            target = c
            parts = path.split(".")
            for part in parts[:-1]:
                target = target[part]
            if value == "__DELETE__":
                del target[parts[-1]]
            else:
                target[parts[-1]] = value
        add(name, "grant", expected, token=sign(c, kid, typ, **encoding),
            context=ctx if ctx is not None else context, now=now, shadow=shadow_mode)

    grant_case("real_valid", "allowed")
    grant_case("shadow_valid", "allowed", kid="shadow-fixture", typ=SHADOW, shadow_mode=True)
    grant_case("dry_run_type_as_real", "type", kid="shadow-fixture", typ=SHADOW)
    grant_case("real_type_as_shadow", "type", shadow_mode=True)
    grant_case("shadow_type_grant_key", "purpose", typ=SHADOW, shadow_mode=True)
    grant_case("real_type_shadow_key", "purpose", kid="shadow-fixture")
    grant_case("grant_descriptor_key", "purpose", kid="boot-a")
    for field in ("iss", "aud", "environment", "plane"):
        grant_case("identity_" + field, "identity", {field: "wrong"})
    for field in ("workspace_id", "key_id", "lookup_digest", "boot_id", "stable_slot_id", "region",
                  "generation", "workspace_epoch", "key_epoch", "image_policy_version"):
        ctx = copy.deepcopy(context)
        ctx[field] = 999 if isinstance(ctx[field], int) else "wrong"
        grant_case("binding_" + field, "binding", ctx=ctx)
    ctx = copy.deepcopy(context)
    del ctx["key_id"]
    grant_case("missing_context_binding", "binding", ctx=ctx)
    ctx = copy.deepcopy(context)
    ctx["image_policy_version"] = True
    grant_case("bool_context_binding", "binding", ctx=ctx)
    for field in grant["route"]:
        ctx = copy.deepcopy(context)
        ctx["route"][field] = "wrong"
        grant_case("route_" + field, "route", ctx=ctx)
    for name, changes, expected in [
        ("unknown_version", {"v": 2}, "version"),
        ("unknown_field", {"extra": 1}, "fields"),
        ("unknown_route_field", {"route.extra": 1}, "fields"),
        ("missing_history", {"history": "__DELETE__"}, "fields"),
        ("missing_provenance", {"paid_headroom_micro": "__DELETE__"}, "fields"),
        ("missing_nested_history", {"history.sequence": "__DELETE__"}, "fields"),
        ("history_19", {"history.count": 19}, "history_count"),
        ("history_retry_dedupe", {"history.count": 20, "history.sequence": 19}, "history_count"),
        ("stale_last_success", {"history.last_success_at": 1699999969}, "history_time"),
        ("future_last_success", {"history.last_success_at": 1700000001}, "history_time"),
        ("old_history_window", {"history.window_start": 1699999399}, "history_time"),
        ("future_history_window", {"history.window_start": 1700000000}, "history_time"),
        ("unclean_history", {"history.clean_since": 1699999101}, "history_time"),
        ("tier_one", {"tier": 1}, "tier"),
        ("unpaid", {"paid_headroom_micro": 4999999}, "paid_headroom"),
        ("ttl_too_long", {"exp": 1700000031}, "lifetime"),
        ("ttl_zero", {"exp": 1700000000}, "lifetime"),
        ("start_before_issue", {"start_before": 1700000000}, "lifetime"),
        ("duplicate_ordinal", {"permits": [{"ordinal": 0, "b_micro": 4608}, {"ordinal": 0, "b_micro": 4608}]}, "ordinal"),
        ("negative_money", {"per_request_ceiling_micro": -1}, "integer"),
        ("over_cap_money", {"per_request_ceiling_micro": 10001}, "ceiling"),
        ("zero_ceiling", {"per_request_ceiling_micro": 0}, "ceiling"),
        ("underfunded_permit", {"permits": [{"ordinal": 0, "b_micro": 4607}]}, "permit_cost"),
        ("over_cap_permit", {"permits": [{"ordinal": 0, "b_micro": 10001}]}, "permit_cost"),
        ("empty_permits", {"permits": []}, "permits"),
        ("scalar_permits", {"permits": 1}, "permits"),
        ("unknown_permit_field", {"permits": [{"ordinal": 0, "b_micro": 4608, "extra": 1}]}, "fields"),
        ("overflow_integer", {"iat": 9223372036854775808}, "integer"),
        ("bool_integer", {"v": True}, "integer"),
        ("float_integer", {"v": 1.0}, "integer"),
        ("null_integer", {"v": None}, "integer"),
        ("invalid_hash", {"lookup_digest": "A" * 64}, "hash"),
        ("stage_d_false", {"route.stage_d": False}, "stage_d"),
        ("stage_d_integer", {"route.stage_d": 1}, "stage_d"),
    ]:
        grant_case(name, expected, changes)
    # Both sides of every exclusive shortening deadline. Receipt never resets iat.
    for name, changes in [
        ("expiry", {"exp": 1700000010}),
        ("key", {"key_expires_at": 1700000010}),
        ("price", {"route.price_expires_at": 1700000010}),
        ("trust", {"trust_fresh_until": 1700000010}),
        ("explicit", {"start_before": 1700000008}),
    ]:
        ctx = copy.deepcopy(context)
        if name == "price":
            ctx["route"]["price_expires_at"] = 1700000010
        grant_case("short_" + name + "_before", "allowed", changes, ctx=ctx, now=1700000007)
        grant_case("short_" + name + "_at", "start_window", changes, ctx=ctx, now=1700000008)
    grant_case("start_plus_27", "allowed", now=1700000027)
    grant_case("start_plus_28", "start_window", now=1700000028)
    grant_case("late_receipt", "start_window", now=1700000030)
    grant_case("future_iat", "start_window", now=1699999999)
    grant_case("bool_now", "integer", now=True)
    grant_case("history_20_boundary", "allowed", {"history.last_success_at": 1699999970, "history.clean_since": 1699999100})
    for name, value in [("quote", 'x"'), ("backslash", 'x\\'), ("less", "x<"), ("greater", "x>"),
                        ("ampersand", "x&"), ("control", "x\n"), ("unicode", "caf\u00e9"), ("del", "x\x7f"),
                        ("empty", ""), ("null", None)]:
        grant_case("string_" + name, "string", {"grant_id": value})
    grant_case("safe_ascii", "allowed", {"grant_id": " !#$%'()*+,-./012:;=?@AZ[]^_`az{|}~"})
    for name, raw, expected in [
        ("payload_whitespace", json.dumps(grant, sort_keys=True).encode(), "canonical_payload"),
        ("payload_unsorted", json.dumps(dict(reversed(list(grant.items()))), separators=(",", ":")).encode(), "canonical_payload"),
        ("payload_escaped_ascii", canonical(grant).replace(b'"w1"', b'"\\u00771"'), "canonical_payload"),
        ("payload_duplicate", canonical(grant).replace(b'"v":1', b'"v":1,"v":1'), "duplicate_key"),
        ("nested_duplicate", canonical(grant).replace(b'"count":20', b'"count":20,"count":20'), "duplicate_key"),
        ("nan", canonical(grant).replace(b'"v":1', b'"v":NaN'), "integer"),
        ("infinity", canonical(grant).replace(b'"v":1', b'"v":Infinity'), "integer"),
        ("negative_infinity", canonical(grant).replace(b'"v":1', b'"v":-Infinity'), "integer"),
        ("raw_unicode", canonical(grant).replace(b'"g1"', '"caf\u00e9"'.encode("utf-8")), "string"),
        ("json_bad", b'{', "json"), ("json_utf8", b'\xff', "json"),
        ("json_scalar", b'1', "fields"),
        ("negative_zero", canonical(grant).replace(b'"v":1', b'"v":-0'), "canonical_payload"),
        ("exponent", canonical(grant).replace(b'"v":1', b'"v":1e0'), "integer"),
    ]:
        grant_case(name, expected, raw=raw)
    for name, header, expected in [
        ("algorithm_none", {"alg": "none", "kid": "issuer-fixture", "typ": REAL}, "algorithm"),
        ("algorithm_rs256", {"alg": "RS256", "kid": "issuer-fixture", "typ": REAL}, "algorithm"),
        ("unknown_kid", {"alg": "EdDSA", "kid": "missing", "typ": REAL}, "key"),
        ("header_extra", {"alg": "EdDSA", "kid": "issuer-fixture", "typ": REAL, "jwk": {}}, "fields"),
        ("header_missing", {"alg": "EdDSA", "kid": "issuer-fixture"}, "fields"),
        ("header_scalar", 1, "fields"),
        ("unknown_type", {"alg": "EdDSA", "kid": "issuer-fixture", "typ": "other"}, "type"),
    ]:
        grant_case(name, expected, header=header)
    for name, token, expected in [
        ("signature_bitflip", real[:-1] + ("A" if real[-1] != "A" else "Q"), "signature"),
        ("compact_missing", "abc", "compact"), ("compact_extra", real + ".x", "compact"),
        ("base64_empty", "." + real.split(".")[1] + ".AA", "base64"),
        ("base64_length", "A." + real.split(".")[1] + ".AA", "base64"),
        ("base64_plus", "+." + real.split(".")[1] + ".AA", "base64"),
        ("base64_slash", "/." + real.split(".")[1] + ".AA", "base64"),
        ("base64_space", " ." + real.split(".")[1] + ".AA", "base64"),
        ("base64_trailing_bits", real[:-1] + chr(ord(real[-1]) + 1), "base64"),
        ("signature_padded", real + "==", "base64"),
    ]:
        add(name, "grant", expected, token=token, context=context, now=1700000000, shadow=False)
    grant_case("payload_padded", "base64", segment=b64(canonical(grant)) + "=")
    # Additional route arithmetic guards use a matching independently trusted route.
    for name, route_changes, expected in [
        ("input_bound_over", {"input_bound": 8193}, "token_bound"),
        ("output_bound_over", {"output_limit": 513}, "token_bound"),
        ("input_bound_zero", {"input_bound": 0}, "token_bound"),
        ("adapter_zero", {"adapter_capability_version": 0}, "adapter"),
        ("route_region_disagrees", {"region": "other"}, "route"),
        ("route_hash_bad", {"catalog_hash": "bad"}, "hash"),
        ("cost_overflow", {"input_rate_micro_per_m": 9223372036854775807}, "overflow"),
        ("cost_zero", {"input_rate_micro_per_m": 0, "output_rate_micro_per_m": 0}, "cost"),
        ("cost_10000", {"maximum_request_fees_micro": 5392}, "allowed"),
        ("cost_10001", {"maximum_request_fees_micro": 5393}, "cost"),
    ]:
        ctx = copy.deepcopy(context)
        ctx["route"].update(route_changes)
        changes = {"route." + k: v for k, v in route_changes.items()}
        if name == "cost_10000":
            changes["permits"] = [{"ordinal": 0, "b_micro": 10000}]
        grant_case(name, expected, changes, ctx=ctx)
    ctx = copy.deepcopy(context)
    ctx["tier_ceiling_micro"] = 100
    grant_case("allowance_exceeded", "allowance", ctx=ctx)
    ctx = copy.deepcopy(context)
    del ctx["tier_ceiling_micro"]
    grant_case("missing_tier_ceiling", "tier_ceiling", ctx=ctx)

    for name, args, expected in [
        ("seed", [8192, 500000, 512, 1000000, 0], 4608),
        ("fractional", [1, 1, 1, 1, 0], 2),
        ("exact_10000", [1, 10000000000, 0, 0, 0], 10000),
        ("above_10000", [1, 10000000001, 0, 0, 0], 10001),
        ("fees", [1, 1, 1, 1, 7], 9),
        ("zero", [0, 0, 0, 0, 0], 0),
        ("product_overflow", [2, 9223372036854775807, 0, 0, 0], "overflow"),
        ("sum_overflow", [1, 1, 0, 0, 9223372036854775807], "overflow"),
        ("int_max", [1, 9223372036854775807, 0, 0, 0], 9223372036855),
        ("bool", [True, 1, 0, 0, 0], "integer"),
        ("negative", [-1, 1, 0, 0, 0], "integer"),
    ]:
        add("money_" + name, "cost", expected, args=args)
    for name, args, expected in [
        ("tier2", [25000000, 5000000], 250000),
        ("tier3", [100000000, 20000000], 1000000),
        ("odd_headroom", [25000000, 1234567], 123456),
        ("odd_tier", [25000099, 5000000], 250000),
        ("zero", [1, 1], 0), ("bool", [True, 1], "integer"),
    ]:
        add("allowance_" + name, "allowance", expected, args=args)

    def descriptor_case(name, expected, changes=None, *, kid="boot-a", typ=DESCRIPTOR,
                        use_shadow=False, wire=None, nonce="n1", execution="x1"):
        d = copy.deepcopy(descriptor)
        d.update(changes or {})
        add(name, "descriptor", expected, token=sign(d, kid, typ),
            grant="shadow" if use_shadow else "real", wire=WIRE[:-1] if wire is None else wire,
            nonce=nonce, execution=execution)
    descriptor_case("descriptor_valid", "allowed")
    descriptor_case("dry_run_cannot_dispatch", "dry_run_cannot_dispatch",
                    {"grant_sha256": hashlib.sha256(shadow.encode()).hexdigest()}, use_shadow=True)
    descriptor_case("descriptor_shadow_hash", "grant_hash",
                    {"grant_sha256": hashlib.sha256(shadow.encode()).hexdigest()})
    descriptor_case("descriptor_purpose", "purpose", kid="issuer-fixture")
    descriptor_case("descriptor_wrong_boot_signer", "descriptor_boot", kid="boot-other")
    descriptor_case("descriptor_wrong_type", "type", typ=REAL)
    descriptor_case("descriptor_version", "version", {"v": 2})
    descriptor_case("descriptor_unknown_field", "fields", {"extra": 1})
    for field in ("grant_id", "workspace_id", "key_id", "boot_id", "workspace_epoch", "key_epoch"):
        descriptor_case("descriptor_" + field, "descriptor_binding",
                        {field: 999 if isinstance(descriptor[field], int) else "wrong"})
    for field, expected in [("grant_sha256", "grant_hash"), ("request_sha256", "request_hash"),
                             ("routing_policy_hash", "descriptor_route")]:
        descriptor_case("descriptor_" + field, expected, {field: "0" * 64})
    descriptor_case("descriptor_hash_malformed", "hash", {"request_sha256": "bad"})
    descriptor_case("descriptor_endpoint", "descriptor_route", {"endpoint_id": "other"})
    descriptor_case("descriptor_nonce", "invocation", nonce="other")
    descriptor_case("descriptor_execution", "invocation", execution="other")
    descriptor_case("descriptor_wire_lf", "request_hash", wire=WIRE)
    descriptor_case("descriptor_ordinal", "descriptor_permit", {"ordinal": 2})
    descriptor_case("descriptor_cost", "descriptor_permit", {"b_micro": 4609})

    response = {"speculation_accepted": bundle["speculation_accepted"], "authorization": authorization}
    add("marker_valid", "marker", "accepted", response=response, authorization=authorization)
    add("marker_missing", "marker", "ordinary", response={"authorization": authorization}, authorization=authorization)
    for name, marker, expected in [
        ("null", None, "fields"), ("scalar", 1, "fields"), ("malformed", {}, "fields"),
        ("version", {**bundle["speculation_accepted"], "v": 2}, "version"),
        ("hash", {**bundle["speculation_accepted"], "descriptor_sha256": "0" * 64}, "descriptor_hash"),
        ("hash_format", {**bundle["speculation_accepted"], "descriptor_sha256": "bad"}, "hash"),
    ]:
        add("marker_" + name, "marker", expected,
            response={"speculation_accepted": marker, "authorization": authorization}, authorization=authorization)
    for field in ("invocation_nonce", "endpoint_id", "routing_policy_hash", "authorization_id"):
        marker = copy.deepcopy(bundle["speculation_accepted"])
        marker[field] = "0" * 64 if field == "routing_policy_hash" else "wrong"
        add("marker_" + field, "marker", "authorization" if field == "authorization_id" else "marker_binding",
            response={"speculation_accepted": marker, "authorization": authorization}, authorization=authorization)
    for field in authorization:
        auth = copy.deepcopy(authorization)
        auth[field] = "wrong"
        add("authorization_" + field, "marker", "authorization",
            response={**response, "authorization": auth}, authorization=auth)
    add("authorization_response_disagrees", "marker", "authorization",
        response={**response, "authorization": {}}, authorization=authorization)

    add("renewal_exact_replay", "renewal", "replay", previous=real, candidate=real,
        previous_context=context, candidate_context=context, shadow=False)
    newer = copy.deepcopy(grant)
    newer.update(grant_id="g2", generation=8)
    for name, changes, expected in [
        ("forward", {}, "renewed"), ("same_generation", {"generation": 7}, "renewal"),
        ("older_generation", {"generation": 6}, "renewal"),
        ("same_grant_id", {"grant_id": "g1"}, "renewal"),
        ("old_workspace_epoch", {"workspace_epoch": 2}, "renewal"),
        ("old_key_epoch", {"key_epoch": 4}, "renewal"),
        ("boot_changed", {"boot_id": "boot-other"}, "renewal"),
        ("old_iat", {"iat": 1699999999, "exp": 1700000029}, "renewal"),
        ("higher_epochs", {"workspace_epoch": 4, "key_epoch": 6}, "renewed"),
    ]:
        c = {**newer, **changes}
        ctx = copy.deepcopy(context)
        ctx.update({k: c[k] for k in context if k in c and k != "route"})
        add("renewal_" + name, "renewal", expected, previous=real, candidate=sign(c),
            previous_context=context, candidate_context=ctx, shadow=False)
    add("renewal_wrong_domain", "renewal", "renewal", previous=real, candidate=shadow,
        previous_context=context, candidate_context=context, shadow=True)
    old_sequence = copy.deepcopy(grant)
    old_sequence["history"]["sequence"] = 21
    ctx = {**context, "generation": 8}
    add("renewal_old_history_sequence", "renewal", "renewal", previous=sign(old_sequence), candidate=sign(newer),
        previous_context=context, candidate_context=ctx, shadow=False)
    add("descriptor_exact_retry", "replay", "replay", candidate=signed_descriptor, nonce="n1", execution="x1")
    d = {**descriptor, "invocation_nonce": "n2", "execution_id": "x2"}
    add("descriptor_permit_reuse", "replay", "replay_conflict", candidate=sign(d, "boot-a", DESCRIPTOR), nonce="n2", execution="x2")

    # Trust-config failures are frozen inputs, not inferred from a runtime key loader.
    for name, keys, expected in [
        ("key_missing", [], "key"),
        ("key_ambiguous", trusted_keys + [trusted_keys[0]], "key"),
        ("key_wrong_public", [{**trusted_keys[0], "public_key_b64url": trusted_keys[1]["public_key_b64url"]}], "signature"),
        ("key_short_public", [{**trusted_keys[0], "public_key_b64url": "AA"}], "signature"),
    ]:
        add(name, "grant", expected, token=real, context=context, now=1700000000, shadow=False, keys=keys)
    h, p, sig = real.split(".")
    raw_header = b'{"alg":"EdDSA","alg":"EdDSA","kid":"issuer-fixture","typ":"speculation-eligibility+jws"}'
    add("header_duplicate", "grant", "duplicate_key", token=b64(raw_header) + "." + p + "." + sig,
        context=context, now=1700000000, shadow=False)
    add("header_padded", "grant", "base64", token=h + "=" + "." + p + "." + sig,
        context=context, now=1700000000, shadow=False)
    for name, auth, expected in [
        ("missing_nonce", {**authorization, "invocation_nonce": "other"}, "authorization"),
        ("missing_auth_id", {**authorization, "authorization_id": None}, "string"),
        ("missing_valid_route_skew", {**authorization, "endpoint_id": "new-route", "stage_d": False}, "ordinary"),
    ]:
        add("marker_" + name, "marker", expected, response={"authorization": auth}, authorization=auth)
    for name, inputs, expected in [
        ("provider_source", {"source": "provider", "status": 429, "reason": "rate_limited", "workspace_id": "w1", "key_id": "k1", "rate_scope": "key"}, "verdict_source"),
        ("success_status", {"source": "authenticated_router", "status": 200, "reason": "ok", "workspace_id": "w1", "key_id": "k1", "rate_scope": None}, "verdict_status"),
    ]:
        add(name, "verdict_extra", expected, input=inputs)

    write("grant-permit-tokens.json", bundle)
    (ROOT / "verdict-vectors.json").write_text(VERDICTS)
    (ROOT / "provider-wire.json").write_text(WIRE)
    write("protocol-vectors.json", {"fixture_version": 1, "cases": cases})
    # Every vector names a concrete guard mutation; verdict guards include the
    # seed names verbatim. The executable gate exercises the requested subset.
    rules = [{"guard": c["name"], "mutation": "remove or invert " + c["name"] + " guard",
              "literal_case": c["name"], "category": c["category"]} for c in cases]
    rules += [{"guard": "verdict_" + v["name"], "mutation": "change scope/commit/breaker classification",
               "literal_case": v["name"], "category": "verdict"} for v in json.loads(VERDICTS)["vectors"]]
    rules.append({"guard": "fixture_pin", "mutation": "change one fixture byte", "literal_case": "fixture_pins", "category": "pin"})
    guard_rules = [
        ("integer", "replace type(v) is int with isinstance(v, int)", "bool_integer"),
        ("string", "allow the excluded printable-ASCII characters", "string_less"),
        ("hash", "remove lowercase SHA-256 validation", "invalid_hash"),
        ("fields", "ignore unknown v1 fields", "unknown_field"),
        ("duplicate_key", "last duplicate JSON key wins", "payload_duplicate"),
        ("json", "treat malformed JSON as empty claims", "json_bad"),
        ("base64_alphabet", "strip padding before decoding", "signature_padded"),
        ("base64_trailing_bits", "omit re-encode equality", "base64_trailing_bits"),
        ("compact", "ignore surplus compact segments", "compact_extra"),
        ("algorithm", "skip alg allowlist", "algorithm_none"),
        ("type", "accept shadow typ in real verifier", "dry_run_type_as_real"),
        ("trusted_key", "accept unknown or duplicate kid", "key_ambiguous"),
        ("purpose", "accept shadow-grant purpose for real grants", "real_type_shadow_key"),
        ("signature", "skip Ed25519 signature verification", "signature_bitflip"),
        ("canonical_payload", "skip canonical payload equality", "payload_whitespace"),
        ("version", "accept unknown version", "unknown_version"),
        ("product_overflow", "allow overflowing signed-int64 product", "money_product_overflow"),
        ("sum_overflow", "allow overflowing signed-int64 total", "money_sum_overflow"),
        ("cost_rounding", "floor token-component cost", "money_fractional"),
        ("allowance_rounding", "ceil headroom divided by ten", "allowance_odd_headroom"),
        ("stage_d", "allow disabled Stage D", "stage_d_false"),
        ("route_region", "skip grant/route region equality", "route_region_disagrees"),
        ("token_bound", "allow unbounded output", "output_bound_over"),
        ("adapter", "accept zero adapter version", "adapter_zero"),
        ("tier", "admit tier one", "tier_one"),
        ("paid_headroom", "admit less than five paid dollars", "unpaid"),
        ("history_count", "lower twenty-success minimum", "history_19"),
        ("history_sequence", "count replayed successes", "history_retry_dedupe"),
        ("history_time", "ignore last-success freshness", "stale_last_success"),
        ("lifetime", "allow grants longer than thirty seconds", "ttl_too_long"),
        ("iat", "permit a start before issuance", "future_iat"),
        ("start_expiry", "include the exclusive start boundary", "start_plus_28"),
        ("start_key", "ignore key expiry margin", "short_key_at"),
        ("start_price", "ignore signed pricing margin", "short_price_at"),
        ("start_trust", "ignore trust freshness margin", "short_trust_at"),
        ("start_explicit", "ignore explicit start_before", "short_explicit_at"),
        ("ceiling", "allow per-request ceilings above 10000", "over_cap_money"),
        ("cost", "admit B above ceiling", "cost_10001"),
        ("permits", "allow an empty grant", "empty_permits"),
        ("ordinal", "ignore duplicate permit ordinals", "duplicate_ordinal"),
        ("permit_cost", "allow underfunded permits", "underfunded_permit"),
        ("tier_ceiling", "invent missing trusted tier ceiling", "missing_tier_ceiling"),
        ("allowance", "ignore summed permit allocation", "allowance_exceeded"),
        ("dry_run_cannot_dispatch", "accept a descriptor referring to shadow rights", "dry_run_cannot_dispatch"),
        ("descriptor_boot", "accept another boot signing key", "descriptor_wrong_boot_signer"),
        ("grant_hash", "skip compact-grant digest binding", "descriptor_shadow_hash"),
        ("request_hash", "rehash reserialized provider bytes", "descriptor_wire_lf"),
        ("invocation", "ignore existing invocation nonce", "descriptor_nonce"),
        ("descriptor_route", "ignore descriptor endpoint", "descriptor_endpoint"),
        ("descriptor_permit", "accept an unallocated ordinal", "descriptor_ordinal"),
        ("authorization", "skip normal authorization identity binding", "authorization_key_id"),
        ("marker_presence", "treat null marker as missing", "marker_null"),
        ("descriptor_hash", "ignore full compact descriptor hash", "marker_hash"),
        ("marker_binding", "ignore accepted marker nonce", "marker_invocation_nonce"),
        ("renewal", "let an older key epoch replace newer rights", "renewal_old_key_epoch"),
        ("replay_conflict", "reuse ordinal for another execution", "descriptor_permit_reuse"),
        ("verdict_source", "classify provider failures as router denials", "provider_source"),
        ("verdict_status", "accept successes as denials", "success_status"),
        ("verdict_402_precedence", "classify every 402 as workspace", "lifetime_limit"),
        ("verdict_429_scope", "classify every 429 as key", "rate_unknown"),
    ]
    rules += [{"guard": guard, "mutation": mutation, "literal_case": case, "category": "guard"}
              for guard, mutation, case in guard_rules]
    write("rules.json", {"fixture_version": 1, "rules": rules})
    files = ["grant-permit-tokens.json", "verdict-vectors.json", "provider-wire.json", "protocol-vectors.json", "rules.json"]
    write("manifest.json", {"fixture_version": 1,
                           "files": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files}})
    print("MANIFEST", hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
