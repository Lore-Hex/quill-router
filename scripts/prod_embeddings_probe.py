#!/usr/bin/env python3
"""Small daily production embeddings probe.

Hits the live attested gateway's POST /v1/embeddings once per embedding
provider, serialized (never bursts — a parallel fan-out trips upstream 429s),
with a tiny input. Verifies a 200 + a non-empty embedding vector. Doubles as
the post-deploy embeddings smoke (run via the daily-embeddings-probe GitHub
Action's workflow_dispatch, or locally).

Pure stdlib so the daily GitHub Action needs no `uv sync` — just `python3`.

Env:
  TR_MONITOR_API_KEY   (required) the synthetic-monitor bearer.
  TR_API_BASE          base URL (default https://api.trustedrouter.com/v1).
  TR_EMBEDDINGS_INCLUDE_COHERE  set to "1" once the Cohere key is provisioned
                       in Secret Manager + wired into the enclave; until then
                       Cohere is skipped (it would 502 "missing api key").
  TR_EMBEDDINGS_PROBE_SPACING_SECONDS  delay between probes (default 3).

Exit code: 0 if all REQUIRED providers pass, 1 otherwise. A skipped provider
never fails the run.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("TR_API_BASE", "https://api.trustedrouter.com/v1").rstrip("/")
API_KEY = os.environ.get("TR_MONITOR_API_KEY", "")
INCLUDE_COHERE = os.environ.get("TR_EMBEDDINGS_INCLUDE_COHERE", "") == "1"
# New embedding providers — gated like Cohere so the probe stays green until
# the enclave re-roll that serves them is live (+ Voyage's key provisioned).
INCLUDE_VOYAGE = os.environ.get("TR_EMBEDDINGS_INCLUDE_VOYAGE", "") == "1"
INCLUDE_GEMINI = os.environ.get("TR_EMBEDDINGS_INCLUDE_GEMINI", "") == "1"
INCLUDE_DEEPINFRA = os.environ.get("TR_EMBEDDINGS_INCLUDE_DEEPINFRA", "") == "1"
SPACING_SECONDS = float(os.environ.get("TR_EMBEDDINGS_PROBE_SPACING_SECONDS", "3"))
TIMEOUT_SECONDS = float(os.environ.get("TR_EMBEDDINGS_PROBE_TIMEOUT_SECONDS", "30"))

# One representative model per embedding provider. OpenAI + Together always
# run (their keys are wired). The rest are opt-in until their enclave route +
# key are live.
PROBES = [
    {"provider": "openai", "model": "openai/text-embedding-3-large", "required": True},
    {"provider": "together", "model": "intfloat/multilingual-e5-large-instruct", "required": True},
    {"provider": "cohere", "model": "cohere/embed-v4.0", "required": INCLUDE_COHERE},
    {"provider": "voyage", "model": "voyage/voyage-3-large", "required": INCLUDE_VOYAGE},
    {"provider": "deepinfra", "model": "Qwen/Qwen3-Embedding-8B", "required": INCLUDE_DEEPINFRA},
    {"provider": "gemini", "model": "google/gemini-embedding-001", "required": INCLUDE_GEMINI},
]


def probe(model: str) -> tuple[bool, str]:
    payload = json.dumps({
        "model": model,
        "input": "trustedrouter embeddings probe",
        "metadata": {"trustedrouter_synthetic": "true"},
    }).encode()
    # API_BASE is an operator-set https endpoint (env), not user input.
    request = urllib.request.Request(  # noqa: S310 - fixed https gateway URL
        f"{API_BASE}/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "TrustedRouter-EmbeddingsProbe/1.0",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        return False, f"HTTP {exc.code} ({int((time.perf_counter() - started) * 1000)}ms): {detail}"
    except Exception as exc:  # noqa: BLE001 - any transport error is a failure
        return False, f"{type(exc).__name__}: {exc}"

    data = body.get("data") or []
    if not data:
        return False, f"200 but empty data ({elapsed_ms}ms)"
    vector = data[0].get("embedding")
    if not isinstance(vector, list) or not vector:
        return False, f"200 but no embedding vector ({elapsed_ms}ms)"
    usage = body.get("usage") or {}
    return True, f"dims={len(vector)} prompt_tokens={usage.get('prompt_tokens')} ({elapsed_ms}ms)"


def main() -> int:
    if not API_KEY:
        print("FATAL: TR_MONITOR_API_KEY is not set", file=sys.stderr)
        return 2
    print(f"Embeddings probe against {API_BASE} (cohere {'on' if INCLUDE_COHERE else 'skipped'})")
    failures = 0
    for index, spec in enumerate(PROBES):
        if not spec["required"]:
            print(f"  SKIP  {spec['provider']:9s} {spec['model']}")
            continue
        if index > 0:
            time.sleep(SPACING_SECONDS)  # serialize: never burst -> no upstream 429
        ok, detail = probe(spec["model"])
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {spec['provider']:9s} {spec['model']} -> {detail}")
        if not ok:
            failures += 1
    if failures:
        print(f"{failures} required embedding provider(s) FAILED", file=sys.stderr)
        return 1
    print("all required embedding providers OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
