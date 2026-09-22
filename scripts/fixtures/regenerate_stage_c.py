"""Regenerate Stage C wire bytes and their signed manifest with the public test seed.

Run: uv run python -m scripts.fixtures.regenerate_stage_c
The checked-in lease catalog and accepted response are frozen wire templates,
not a view of today's mutable catalog. Never use this seed outside tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.config import Settings
from trusted_router.receipt_keys import b64url_encode
from trusted_router.routing import normalize_routing_inputs
from trusted_router.spend_lease_admission import ADMISSION_REFUSAL_REASONS
from trusted_router.spend_leases import boot_auth_digest
from trusted_router.stage_d import endpoint_pricing_document

ROOT = Path(__file__).resolve().parents[2] / "tests/fixtures/stage_c"
RAW_TEST_KEY = "sk-tr-v1-" + b64url_encode(bytes(range(32)))
RESOLVED_KEY_ID = "key_" + b64url_encode(bytes(range(18)))


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def regenerate() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex((ROOT / "admission_receipt_ed25519_seed.hex").read_text()))

    def read(name: str) -> Any:
        return json.loads((ROOT / name).read_bytes())

    def write(name: str, value: Any) -> None:
        (ROOT / name).write_bytes(canonical(value))

    def sign(header: Any, claims: Any) -> str:
        signing_input = f"{b64url_encode(canonical(header))}.{b64url_encode(canonical(claims))}"
        return f"{signing_input}.{b64url_encode(private.sign(signing_input.encode()))}"

    body = read("receipt_bearing_authorize_request.json")
    body.pop("api_key_hash", None)
    body.update(
        api_key_lookup_hash=hashlib.sha256(RAW_TEST_KEY.encode()).hexdigest(),
        stream=True, invocation_nonce="stage-c-fixture-invocation-67",
        tags={"environment": "wire-test", "purpose": "stage-c-parity"},
        app="Stage C wire fixture", http_referer="https://example.test/stage-c",
        app_categories=["testing"], user="fixture-user", session_id="fixture-session",
        metadata={"attribution": "stage-c-fixture"},
    )
    body["provider"].update(
        max_price={"prompt": 10, "completion": 20}, jurisdiction="us",
        usage="credits", usage_type="credits", billing="credits",
    )
    normalized = normalize_routing_inputs(body, Settings(environment="test"), resolved_region="us-central1")
    (ROOT / "normalized_routing_inputs.json").write_bytes(normalized.canonical_json())
    (ROOT / "normalized_routing_inputs.sha256").write_text(normalized.routing_policy_hash)
    lease = read("authoritative_lease_payload.json")
    receipt = read("admission_receipt_payload.json")
    for claims in (lease, receipt):
        claims.update(key_hash=RESOLVED_KEY_ID, routing_policy_hash=normalized.routing_policy_hash)
    lease_token = sign(read("authoritative_lease_protected_header.json"), lease)
    receipt_token = sign(read("admission_receipt_protected_header.json"), receipt)
    write("authoritative_lease_payload.json", lease)
    write("admission_receipt_payload.json", receipt)
    (ROOT / "authoritative_lease_compact.jws").write_text(lease_token)
    (ROOT / "admission_receipt_compact.jws").write_text(receipt_token)
    body["spend_lease_admission"] = receipt_token
    write("receipt_bearing_authorize_request.json", body)
    boot_signature = private.sign(boot_auth_digest("POST", "/internal/gateway/authorize", canonical(body)))
    (ROOT / "receipt_bearing_authorize_boot_auth.txt").write_text(f"kid={receipt['boot_kid']},sig={b64url_encode(boot_signature)}")
    accepted = read("admission_accepted_response.json")
    data = accepted["data"]
    data["api_key_hash"] = RESOLVED_KEY_ID
    data["tags"] = body["tags"]
    data["spend_lease"]["token"] = lease_token
    data["spend_lease_admission"]["receipt_hash"] = hashlib.sha256(receipt_token.encode()).hexdigest()
    data["stage_d"] = {"eligible": True, "reason": "ok"}
    data["cap_micro"] = receipt["enclave_estimate_micro"]
    endpoints = [SimpleNamespace(
        id=candidate["endpoint_id"], provider=candidate["provider"],
        prompt_price_microdollars_per_million_tokens=candidate["input_price_micro_per_mtok"],
        completion_price_microdollars_per_million_tokens=candidate["output_price_micro_per_mtok"],
        request_price_microdollars=candidate["request_price_micro"], price_tiers=(),
    ) for candidate in lease["catalog"]["candidates"]]
    data["candidate_prices"] = endpoint_pricing_document(endpoints)["candidates"]
    write("admission_accepted_response.json", accepted)
    for reason in ADMISSION_REFUSAL_REASONS:
        write(f"admission_rejected_{reason}.json", {"error": {
            "code": 409, "message": "Spend-lease admission was rejected", "reason": reason,
            "source": "router", "type": "admission_rejected",
        }})
    manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in ROOT.iterdir()
                if path.is_file() and not path.name.startswith("wire_manifest.")}
    write("wire_manifest.json", manifest)
    (ROOT / "wire_manifest.ed25519").write_text(b64url_encode(private.sign(canonical(manifest))))


if __name__ == "__main__":
    regenerate()
