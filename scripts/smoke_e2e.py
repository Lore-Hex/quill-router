#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from typing import Any

import httpx

BASE_URL = os.environ.get("TR_SMOKE_BASE_URL", "http://127.0.0.1:18080/v1").rstrip("/")
USER_ID = os.environ.get("TR_SMOKE_USER", "smoke@example.com")
INTERNAL_TOKEN = os.environ.get("TR_SMOKE_INTERNAL_TOKEN")


class SmokeError(RuntimeError):
    pass


def request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    expect: int = 200,
) -> dict[str, Any]:
    req_headers = {"accept": "application/json"}
    if headers:
        req_headers.update(headers)
    response = httpx.request(method, f"{BASE_URL}{path}", headers=req_headers, json=body, timeout=30)
    parsed = response.json() if response.content else {}
    if response.status_code != expect:
        raise SmokeError(
            f"{method} {path}: expected {expect}, got {response.status_code}: {response.text}"
        )
    return parsed


def wait_for_health() -> None:
    for _ in range(60):
        try:
            request("GET", "/health")
            return
        except Exception:
            time.sleep(0.25)
    raise SmokeError(f"{BASE_URL}/health did not become reachable")


def main() -> int:
    wait_for_health()
    user_headers = {"x-trustedrouter-user": USER_ID}

    models_count = request("GET", "/models/count")["data"]["count"]
    if models_count < 5:
        raise SmokeError(f"expected at least 5 models, got {models_count}")

    coverage = request("GET", "/coverage/openrouter")["data"]
    if len(coverage) != 54:
        raise SmokeError(f"expected 54 OpenRouter route classifications, got {len(coverage)}")

    workspace = request("GET", "/workspaces", headers=user_headers)["data"][0]
    key_resp = request("POST", "/keys", headers=user_headers, body={"name": "e2e smoke"}, expect=201)
    raw_key = key_resp["key"]
    key_hash = key_resp["data"]["hash"]
    if key_resp["data"]["workspace_id"] != workspace["id"]:
        raise SmokeError("created key in unexpected workspace")

    prompt = "private smoke prompt"
    inference_headers = {"authorization": f"Bearer {raw_key}", "x-title": "Smoke App"}
    chat = request(
        "POST",
        "/chat/completions",
        headers=inference_headers,
        body={"model": "cerebras/llama3.1-8b", "messages": [{"role": "user", "content": prompt}]},
    )
    if chat["object"] != "chat.completion" or chat["trustedrouter"]["content_stored"] is not False:
        raise SmokeError("chat response shape/content storage policy failed")
    if prompt in json.dumps(chat):
        raise SmokeError("prompt leaked into chat response metadata")
    generation_id = chat["trustedrouter"]["generation_id"]

    activity_events = request("GET", "/activity?group_by=none", headers=user_headers)["data"]
    if not activity_events or activity_events[0]["content_stored"] is not False:
        raise SmokeError("activity event missing content_stored=false")
    if prompt in json.dumps(activity_events):
        raise SmokeError("prompt leaked into activity metadata")

    generation = request("GET", f"/generation?id={urllib.parse.quote(generation_id)}", headers=user_headers)["data"]
    if generation["id"] != generation_id:
        raise SmokeError("generation lookup returned wrong generation")
    request("GET", f"/generation/content?id={urllib.parse.quote(generation_id)}", headers=user_headers, expect=404)

    byok = request(
        "PUT",
        "/byok/providers/cerebras",
        headers=user_headers,
        body={"api_key": "csk-test-secret-value-9999"},
        expect=201,
    )["data"]
    if byok["key_hint"] != "csk-te...9999" or "csk-test-secret-value-9999" in json.dumps(byok):
        raise SmokeError("BYOK key handling leaked raw provider key")
    if byok.get("secret_storage") != "envelope" or not byok["secret_ref"].startswith("byok://"):
        raise SmokeError("BYOK raw key was not stored as an encrypted envelope")

    internal_headers = {}
    if INTERNAL_TOKEN:
        internal_headers["authorization"] = f"Bearer {INTERNAL_TOKEN}"
    authz = request(
        "POST",
        "/internal/gateway/authorize",
        headers=internal_headers,
        body={
            "api_key_hash": key_hash,
            "model": "cerebras/llama3.1-8b",
            "estimated_input_tokens": 12,
            "max_output_tokens": 4,
        },
    )["data"]
    if authz["usage_type"] != "BYOK" or authz["byok_key_hint"] != "csk-te...9999":
        raise SmokeError("gateway authorization did not return expected BYOK metadata")
    if not authz.get("byok_encrypted_secret"):
        raise SmokeError("gateway authorization did not include encrypted BYOK envelope")

    settle = request(
        "POST",
        "/internal/gateway/settle",
        headers=internal_headers,
        body={
            "authorization_id": authz["authorization_id"],
            "actual_input_tokens": 12,
            "actual_output_tokens": 2,
            "request_id": "smoke-gateway-1",
            "app": "attested-gateway-smoke",
            "elapsed_seconds": 0.25,
        },
    )["data"]
    if settle["settled"] is not True or not settle["generation_id"]:
        raise SmokeError("gateway settlement failed")

    checkout = request("POST", "/billing/checkout", headers=user_headers, body={"amount": 25}, expect=201)["data"]
    if checkout["mode"] not in {"mock", "stripe"}:
        raise SmokeError("billing checkout returned unexpected mode")

    print("E2E smoke OK")
    print(
        json.dumps(
            {
                "base_url": BASE_URL,
                "models_count": models_count,
                "coverage_routes": len(coverage),
                "workspace_id": workspace["id"],
                "key_hash": key_hash,
                "generation_id": generation_id,
                "gateway_generation_id": settle["generation_id"],
                "checkout_mode": checkout["mode"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"E2E smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
