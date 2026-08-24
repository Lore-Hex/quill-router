#!/usr/bin/env python3
"""Destructive, opt-in production smoke for the customer BYOK lifecycle.

The smoke creates an isolated account/workspace, uploads one provider key,
forces two attested BYOK inference calls around an envelope rotation, verifies
settlement and credit isolation, deletes the BYOK configuration, and proves
the deleted credential can no longer authorize inference.

The raw provider key is accepted only through an environment variable and is
never printed. The workspace, inference key, and BYOK row are cleaned up on a
best-effort basis even when an assertion fails.
"""

from __future__ import annotations

import argparse
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_CONTROL_BASE = "https://trustedrouter.com/v1"
DEFAULT_API_BASE = "https://api.trustedrouter.com/v1"


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class SmokeConfig:
    control_base: str
    api_base: str
    provider: str
    model: str
    provider_key: str
    timeout_seconds: float


def _headers(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def _assert_secret_absent(value: Any, raw_secret: str, *, context: str) -> None:
    if raw_secret and raw_secret in str(value):
        raise SmokeFailure(f"{context} leaked the raw provider key")


def _require_status(
    response: httpx.Response,
    expected: set[int],
    *,
    context: str,
    raw_secret: str,
) -> dict[str, Any]:
    _assert_secret_absent(response.text, raw_secret, context=context)
    if response.status_code not in expected:
        raise SmokeFailure(
            f"{context} returned HTTP {response.status_code}: {response.text[:500]}"
        )
    if not response.content:
        return {}
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{context} returned a non-object JSON response")
    _assert_secret_absent(payload, raw_secret, context=context)
    return payload


def _chat_body(config: SmokeConfig, marker: str) -> dict[str, Any]:
    return {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with exactly BYOK_OK. Test marker: {marker}",
            }
        ],
        "max_tokens": 32,
        "temperature": 0,
        "provider": {
            "only": [config.provider],
            "usage": "byok",
            "allow_fallbacks": False,
        },
    }


def _assert_byok_route(payload: dict[str, Any], config: SmokeConfig) -> str:
    route = payload.get("trustedrouter")
    usage = payload.get("usage")
    provider_usage = usage.get("provider_usage") if isinstance(usage, dict) else None
    if not isinstance(route, dict) or not route.get("selected_provider"):
        route = provider_usage
    if not isinstance(route, dict):
        raise SmokeFailure(
            "chat response omitted routing metadata "
            f"(top-level keys={sorted(payload)}, "
            f"usage keys={sorted(usage) if isinstance(usage, dict) else []})"
        )
    if route.get("selected_provider") != config.provider:
        raise SmokeFailure(
            f"chat selected provider {route.get('selected_provider')!r}, "
            f"expected {config.provider!r}; route keys={sorted(route)}"
        )
    if str(route.get("usage_type") or "").upper() != "BYOK":
        raise SmokeFailure(f"chat settled as {route.get('usage_type')!r}, expected BYOK")
    generation_id = route.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise SmokeFailure("chat response omitted the settled generation ID")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SmokeFailure("chat response did not contain a completion")
    return generation_id


def _available_microdollars(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SmokeFailure("credits response omitted data")
    value = data.get("available_microdollars")
    if not isinstance(value, int):
        raise SmokeFailure("credits response omitted integer available_microdollars")
    return value


def _byok_usage_microdollars(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SmokeFailure("key response omitted data")
    value = data.get("byok_usage_microdollars")
    if not isinstance(value, int):
        raise SmokeFailure("key response omitted integer byok_usage_microdollars")
    return value


def _wait_for_generation(
    client: httpx.Client,
    *,
    control_base: str,
    inference_key: str,
    generation_id: str,
    raw_secret: str,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = client.get(
            f"{control_base}/generation",
            headers=_headers(inference_key),
            params={"id": generation_id},
        )
        if response.status_code == 200:
            return _require_status(
                response,
                {200},
                context="generation lookup",
                raw_secret=raw_secret,
            )
        _assert_secret_absent(
            response.text,
            raw_secret,
            context="generation lookup",
        )
        if response.status_code != 404:
            return _require_status(
                response,
                {200},
                context="generation lookup",
                raw_secret=raw_secret,
            )
        if time.monotonic() >= deadline:
            raise SmokeFailure(
                f"generation {generation_id} remained unavailable for "
                f"{timeout_seconds:g} seconds"
            )
        time.sleep(0.5)


def run_smoke(config: SmokeConfig) -> None:
    unique = f"{int(time.time())}-{secrets.token_hex(4)}"
    email = f"byok-smoke-{unique}@example.com"
    marker_one = f"byok-create-{unique}"
    marker_two = f"byok-rotate-{unique}"
    management_key = ""
    inference_key = ""
    inference_key_hash = ""
    workspace_id = ""

    timeout = httpx.Timeout(config.timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        try:
            signup = client.post(
                f"{config.control_base}/signup",
                json={"email": email, "name": "BYOK production smoke"},
            )
            signup_payload = _require_status(
                signup,
                {201},
                context="signup",
                raw_secret=config.provider_key,
            )
            signup_data = signup_payload["data"]
            management_key = str(signup_data["key"])
            workspace_id = str(signup_data["workspace_id"])
            print("1/8 isolated workspace created")

            created_key = client.post(
                f"{config.control_base}/keys",
                headers=_headers(management_key),
                json={
                    "name": "BYOK production smoke",
                    "include_byok_in_limit": True,
                    "tags": {"purpose": "byok-production-smoke"},
                },
            )
            key_payload = _require_status(
                created_key,
                {201},
                context="inference key creation",
                raw_secret=config.provider_key,
            )
            inference_key = str(key_payload["key"])
            inference_key_hash = str(key_payload["data"]["hash"])

            credits_before = _require_status(
                client.get(
                    f"{config.control_base}/credits",
                    headers=_headers(management_key),
                ),
                {200},
                context="credits before BYOK",
                raw_secret=config.provider_key,
            )
            available_before = _available_microdollars(credits_before)

            uploaded = _require_status(
                client.put(
                    f"{config.control_base}/byok/providers/{config.provider}",
                    headers=_headers(management_key),
                    json={"api_key": config.provider_key},
                ),
                {201},
                context="BYOK upload",
                raw_secret=config.provider_key,
            )
            byok_data = uploaded.get("data")
            if not isinstance(byok_data, dict):
                raise SmokeFailure("BYOK upload omitted data")
            if byok_data.get("secret_storage") != "envelope":
                raise SmokeFailure("BYOK upload was not stored as an envelope")
            if not str(byok_data.get("secret_ref") or "").startswith("byok://"):
                raise SmokeFailure("BYOK upload did not return an opaque byok:// reference")

            listed = _require_status(
                client.get(
                    f"{config.control_base}/byok/providers",
                    headers=_headers(management_key),
                ),
                {200},
                context="BYOK list",
                raw_secret=config.provider_key,
            )
            _assert_secret_absent(listed, config.provider_key, context="BYOK list")
            print("2/8 provider key envelope stored and management responses redacted")

            first_chat = _require_status(
                client.post(
                    f"{config.api_base}/chat/completions",
                    headers={
                        **_headers(inference_key),
                        "Idempotency-Key": marker_one,
                    },
                    json=_chat_body(config, marker_one),
                ),
                {200},
                context="first attested BYOK chat",
                raw_secret=config.provider_key,
            )
            first_generation_id = _assert_byok_route(first_chat, config)
            print("3/8 attested provider-forced BYOK inference succeeded")

            first_generation = _wait_for_generation(
                client,
                control_base=config.control_base,
                inference_key=inference_key,
                generation_id=first_generation_id,
                raw_secret=config.provider_key,
            )
            generation_data = first_generation.get("data")
            if not isinstance(generation_data, dict):
                raise SmokeFailure("generation lookup omitted data")
            if str(generation_data.get("usage_type") or "").upper() != "BYOK":
                raise SmokeFailure("stored generation was not classified as BYOK")
            if not generation_data.get("provider_name"):
                raise SmokeFailure("stored generation omitted its provider name")

            first_key_stats = _require_status(
                client.get(
                    f"{config.control_base}/key",
                    headers=_headers(inference_key),
                ),
                {200},
                context="first key usage lookup",
                raw_secret=config.provider_key,
            )
            first_byok_usage = _byok_usage_microdollars(first_key_stats)
            if first_byok_usage <= 0:
                raise SmokeFailure("BYOK inference did not increment BYOK key usage")

            credits_after_first = _require_status(
                client.get(
                    f"{config.control_base}/credits",
                    headers=_headers(management_key),
                ),
                {200},
                context="credits after first BYOK call",
                raw_secret=config.provider_key,
            )
            if _available_microdollars(credits_after_first) != available_before:
                raise SmokeFailure("BYOK inference changed the prepaid credit balance")
            print("4/8 generation settled as BYOK without consuming prepaid credits")

            rotated = _require_status(
                client.put(
                    f"{config.control_base}/byok/providers/{config.provider}",
                    headers=_headers(management_key),
                    json={"api_key": config.provider_key},
                ),
                {200},
                context="BYOK rotation",
                raw_secret=config.provider_key,
            )
            rotated_data = rotated.get("data")
            if not isinstance(rotated_data, dict):
                raise SmokeFailure("BYOK rotation omitted data")
            if rotated_data.get("updated_at") == byok_data.get("updated_at"):
                raise SmokeFailure("BYOK rotation did not update the envelope version")

            second_chat = _require_status(
                client.post(
                    f"{config.api_base}/chat/completions",
                    headers={
                        **_headers(inference_key),
                        "Idempotency-Key": marker_two,
                    },
                    json=_chat_body(config, marker_two),
                ),
                {200},
                context="rotated attested BYOK chat",
                raw_secret=config.provider_key,
            )
            second_generation_id = _assert_byok_route(second_chat, config)
            if second_generation_id == first_generation_id:
                raise SmokeFailure("rotated call reused the first generation ID")

            second_key_stats = _require_status(
                client.get(
                    f"{config.control_base}/key",
                    headers=_headers(inference_key),
                ),
                {200},
                context="second key usage lookup",
                raw_secret=config.provider_key,
            )
            if _byok_usage_microdollars(second_key_stats) <= first_byok_usage:
                raise SmokeFailure("rotated BYOK call did not settle additional BYOK usage")
            print("5/8 rotated envelope was accepted and settled independently")

            deleted = _require_status(
                client.delete(
                    f"{config.control_base}/byok/providers/{config.provider}",
                    headers=_headers(management_key),
                ),
                {200},
                context="BYOK deletion",
                raw_secret=config.provider_key,
            )
            if deleted.get("data", {}).get("deleted") is not True:
                raise SmokeFailure("BYOK deletion did not confirm deletion")

            _require_status(
                client.get(
                    f"{config.control_base}/byok/providers/{config.provider}",
                    headers=_headers(management_key),
                ),
                {404},
                context="deleted BYOK lookup",
                raw_secret=config.provider_key,
            )
            print("6/8 provider key deleted and no longer returned")

            after_delete = client.post(
                f"{config.api_base}/chat/completions",
                headers={
                    **_headers(inference_key),
                    "Idempotency-Key": f"byok-delete-{unique}",
                },
                json=_chat_body(config, f"byok-delete-{unique}"),
            )
            _assert_secret_absent(
                after_delete.text,
                config.provider_key,
                context="post-delete inference",
            )
            if 200 <= after_delete.status_code < 300:
                raise SmokeFailure("deleted BYOK key still authorized inference")
            print("7/8 deleted BYOK key fails closed at authorization")

            credits_final = _require_status(
                client.get(
                    f"{config.control_base}/credits",
                    headers=_headers(management_key),
                ),
                {200},
                context="final credits lookup",
                raw_secret=config.provider_key,
            )
            if _available_microdollars(credits_final) != available_before:
                raise SmokeFailure("BYOK lifecycle changed the prepaid credit balance")
            print("8/8 prepaid balance remained unchanged")
        finally:
            if management_key:
                if workspace_id:
                    client.delete(
                        f"{config.control_base}/byok/providers/{config.provider}",
                        headers=_headers(management_key),
                    )
                if inference_key_hash:
                    client.delete(
                        f"{config.control_base}/keys/{inference_key_hash}",
                        headers=_headers(management_key),
                    )
                if workspace_id:
                    client.delete(
                        f"{config.control_base}/workspaces/{workspace_id}",
                        headers=_headers(management_key),
                    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="cerebras")
    parser.add_argument("--model", default="cerebras/gpt-oss-120b")
    parser.add_argument("--provider-key-env", default="CEREBRAS_API_KEY")
    parser.add_argument("--control-base", default=DEFAULT_CONTROL_BASE)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    if os.environ.get("TR_BYOK_PROD_SMOKE") != "1":
        raise SystemExit("Set TR_BYOK_PROD_SMOKE=1 to permit production mutations")
    args = parse_args()
    provider_key = os.environ.get(args.provider_key_env, "").strip()
    if not provider_key:
        raise SystemExit(f"{args.provider_key_env} is required")
    config = SmokeConfig(
        control_base=args.control_base.rstrip("/"),
        api_base=args.api_base.rstrip("/"),
        provider=args.provider.strip().lower(),
        model=args.model.strip(),
        provider_key=provider_key,
        timeout_seconds=args.timeout_seconds,
    )
    run_smoke(config)
    print("BYOK production smoke passed; isolated workspace cleanup attempted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
