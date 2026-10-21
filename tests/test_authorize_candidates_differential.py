"""Authorize gives byte-identical results before and after the candidate pipeline moved.

tests/fakes/authorize_candidates_main_gateway.py.txt is the gateway route
module exactly as it was before ``trusted_router.authorize_candidates``
existed (main at bb855bdfa, pinned by hash below). It is loaded as a separate
module over the same live dependencies, and every request of a generated
matrix runs through both, each on a freshly seeded store with the same
clock, the same ids and a fresh process-local routing state. The responses,
the refusals (status, body and headers), the stored authorization and the
log lines must be identical, except for the explicitly checked reservation
guidance and diagnostic added in #1926. Reservation quotes are compared too.
"""

from __future__ import annotations

import datetime as real_dt
import hashlib
import itertools
import json
import logging
import random
import secrets
import sys
import types
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from trusted_router.catalog import META_MODEL_IDS, MODELS, NAMED_DECISION_MODEL_PROVIDERS
from trusted_router.catalog_data import PRIVATE_PROXY_MODEL_TARGETS
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.routing_state import RoutingState
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.web_fetch import FETCH_ROUTE_TYPE

FROZEN = Path(__file__).parent / "fakes" / "authorize_candidates_main_gateway.py.txt"
FROZEN_SHA256 = "58df13a794c5b22f7aa02daa89168f0bcb688b1db56119d5bfaeade9c89ca0ec"
FROZEN_NAME = "tests_frozen_authorize_candidates_main_gateway"
INSTANT = real_dt.datetime(2026, 10, 21, 9, 30, 15, 123456, tzinfo=real_dt.UTC)
DURATIONS = frozenset({"key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms", "total_ms"})
GATEWAY_LOGGERS = ("trusted_router.routes.internal.gateway", FROZEN_NAME)


def _load_frozen() -> types.ModuleType:
    source = FROZEN.read_bytes()
    assert hashlib.sha256(source).hexdigest() == FROZEN_SHA256, "the frozen route changed"
    module = types.ModuleType(FROZEN_NAME)
    module.__file__ = str(FROZEN)
    sys.modules[FROZEN_NAME] = module
    exec(compile(source, str(FROZEN), "exec"), module.__dict__)  # noqa: S102 - pinned copy of main
    return module


FROZEN_GATEWAY = _load_frozen()


def _fixed_clock() -> types.SimpleNamespace:
    class _Clock(real_dt.datetime):
        @classmethod
        def now(cls, tz: real_dt.tzinfo | None = None) -> real_dt.datetime:  # type: ignore[override]
            return INSTANT if tz is None else INSTANT.astimezone(tz)

    clock = types.SimpleNamespace(
        **{name: getattr(real_dt, name) for name in dir(real_dt) if not name.startswith("__")}
    )
    clock.datetime = _Clock
    return clock


def _model_spellings() -> list[str]:
    chat = sorted(m.id for m in MODELS.values() if m.supports_chat and m.id not in META_MODEL_IDS)
    rng = random.Random(1913)  # noqa: S311 - reproducible matrix, not a secret
    plain = rng.sample(chat, 60)
    embeddings = sorted(m.id for m in MODELS.values() if m.supports_embeddings and not m.supports_chat)[:6]
    decide = sorted(m.id for m in MODELS.values() if m.supports_decide and not m.supports_chat)[:4]
    meta = sorted(META_MODEL_IDS & set(MODELS))
    spellings = [
        *plain, *embeddings, *decide, *meta, *NAMED_DECISION_MODEL_PROVIDERS,
        *PRIVATE_PROXY_MODEL_TARGETS,
        "gpt-4.1", "gpt-4.1-2025-04-14", "openai/gpt-4.1-2025-04-14", "gpt-4o-mini",
        "nyte/auto", "trustedrouter/auto-routing", "trustedrouter/auto:nitro",
        "trustedrouter/confidential-2025-04-14", "nope/unknown-model",
        f"{plain[0]}:nitro", f"{plain[1]}:floor", f"{plain[2]}-2026-01-01",
        f"{next(iter(NAMED_DECISION_MODEL_PROVIDERS))}:nitro",
    ]
    return list(dict.fromkeys(spellings))


PROVIDERS_FOR_FILTERS = ["openai", "anthropic", "google-ai-studio", "deepseek", "together",
                         "deepinfra", "cerebras", "fireworks", "mistral", "venice", "tinfoil"]
PROVIDER_OPTIONS: list[dict[str, Any] | None] = [
    None, {}, {"order": ["deepinfra", "together"]}, {"only": ["openai", "anthropic"]},
    {"ignore": ["deepinfra"]}, {"sort": "price"}, {"sort": "latency"}, {"sort": "throughput"},
    {"sort": {"by": "price", "partition": "none"}}, {"allow_fallbacks": False},
    {"allow_fallbacks": False, "order": ["together"]}, {"data_collection": "deny"},
    {"zdr": True}, {"min_privacy": "zdr"}, {"min_privacy": "confidential"},
    {"usage": "byok"}, {"usage": "credits"}, {"jurisdiction": "us"},
    {"max_price": {"prompt": 1, "completion": 1}}, {"require_parameters": True},
    {"order": ["openrouter"]}, {"only": ["nope-provider"]}, {"only": "gemini"},
    {"preferred_max_latency": 2}, {"sort": "random"}, {"usage": "bogus"},
]
ROUTE_TYPES = [None, "chat.completions", "responses", "messages", "embeddings", "decide",
               "images", "responses.web_search.planner", "videos", "audio.speech",
               "audio.transcriptions", FETCH_ROUTE_TYPE]
REGIONS = [None, "us-central1", "europe-west4", "us-west1", "unknown-region"]
BYOK_SETS = [(), ("openai",), ("anthropic", "google-ai-studio"), ("deepseek", "mistral", "gemini"),
             tuple(PROVIDERS_FOR_FILTERS)]
SERVICE_TIERS = [None, "default", "auto", "priority", " Priority ", "flex"]
SETTINGS = [
    Settings(environment="test"),
    Settings(environment="test", stage_d_eligibility_enabled=True, stage_d_pilot_workspace_ids=""),
    Settings(environment="test", auto_model_order="anthropic/claude-haiku-4.5,openai/gpt-4.1-mini"),
    Settings(environment="test", regions="us-central1,us-east4", primary_region="us-east4"),
]


def _cases(count: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - reproducible matrix, not a secret
    models = _model_spellings()
    decide = {m.id for m in MODELS.values() if m.supports_decide and not m.supports_chat}
    decide |= set(NAMED_DECISION_MODEL_PROVIDERS)
    # Every spelling at least once on its natural route, with no options.
    cases: list[dict[str, Any]] = [
        {"model": model, "route_type": "decide" if model in decide else None} for model in models
    ]
    while len(cases) < count:
        case: dict[str, Any] = {"model": rng.choice(models), "route_type": rng.choice(ROUTE_TYPES)}
        if rng.random() < 0.15:
            case["models"] = rng.sample(models, rng.randint(1, 3))
        provider = rng.choice(PROVIDER_OPTIONS)
        if provider is not None:
            case["provider"] = provider
            if provider.get("require_parameters"):
                case["requested_parameters"] = rng.choice([["tools"], ["response_format", "reasoning"]])
        if rng.random() < 0.2:
            case["input_modalities"] = rng.choice([["text"], ["text", "image"], ["audio"]])
        case["region"] = rng.choice(REGIONS)
        case["service_tier"] = rng.choice(SERVICE_TIERS) if rng.random() < 0.3 else None
        case["estimated_input_tokens"] = rng.choice([0, 1, 1_000, 300_000, 2_000_000_000])
        case["max_tokens"] = rng.choice([None, 1, 4_096])
        case["inference_receipt"] = rng.random() < 0.15
        case["stream"] = rng.choice([None, True])
        if rng.random() < 0.1:
            case["additional_cost_reservation_microdollars"] = 1_000
        case["byok"] = rng.choice(BYOK_SETS)
        case["settings"] = rng.randrange(len(SETTINGS))
        cases.append(case)
    return cases


def _deterministic_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same ids and key material in both legs: workspace, key, authorization."""
    counter = itertools.count()
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n=32: f"test{next(counter):0{n}d}")
    monkeypatch.setattr(secrets, "token_hex", lambda n=32: f"{next(counter):0{2 * n}x}")


def _seed(backend: str, byok: tuple[str, ...]) -> tuple[Any, Any]:
    if backend == "typed":
        store, _database, key = _seed_typed_gateway_store()
        workspace_id = key.workspace_id
    else:
        store = InMemoryStore()
        configure_store(store)
        workspace = store.create_workspace("user-diff", "diff", trial_credit_microdollars=100_000_000_000)
        _raw, key = store.create_api_key(workspace_id=workspace.id, name="diff", creator_user_id="user-diff")
        workspace_id = workspace.id
    for provider in byok:
        store.upsert_byok_provider(
            workspace_id=workspace_id, provider=provider,
            secret_ref="test://byok-reference", key_hint="test",  # noqa: S106 - a reference, not a secret
        )
    return store, key


def _body_fields(case: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in case.items() if k not in {"byok", "settings"} and v is not None}


def _run(module: types.ModuleType, case: dict[str, Any], backend: str,
         monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> str:
    _deterministic_ids(monkeypatch)
    store, key = _seed(backend, case.get("byok", ()))
    monkeypatch.setattr(module, "ROUTING_STATE", RoutingState())
    body = GatewayAuthorizeRequest.model_validate({**_body_fields(case), "api_key_hash": key.hash})
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    quotes: list[dict[str, int]] = []
    method = "authorize_gateway_typed" if backend == "typed" else "reserve"
    reserve = getattr(store, method)

    def observe_reservation(_self: Any, *args: Any, **kwargs: Any) -> Any:
        quotes.append({
            "estimate": kwargs["estimate"] if backend == "typed" else args[2],
            "input": kwargs["stage_d_prompt_tokens"] if backend == "typed" else body.estimated_input_tokens,
            "output": body.output_estimate,
        })
        return reserve(*args, **kwargs)

    caplog.clear()
    with monkeypatch.context() as scope:
        scope.setattr(type(store), method, observe_reservation)
        try:
            data = module._authorize_gateway_sync(request, body, SETTINGS[case.get("settings", 0)])
        except HTTPException as exc:
            outcome: dict[str, Any] = {"status": exc.status_code, "detail": exc.detail, "headers": exc.headers}
        else:
            authorization = store.get_gateway_authorization(data["data"]["authorization_id"])
            stored = asdict(authorization) if authorization is not None else None
            outcome = {"status": 200, "data": data, "stored": stored}
    outcome["reservation_quotes"] = quotes
    outcome["logs"] = [
        (record.levelname, record.getMessage())
        for record in caplog.records if record.name in GATEWAY_LOGGERS
    ]
    error = outcome.get("detail", {}).get("error", {})
    if module is gateway and error.get("type") == "insufficient_credits":
        assert outcome["status"] == 402 and len(quotes) == 1
        quote = quotes[0]
        # Validate the precise approved delta against the ACTUAL reserve call,
        # not against the new message/log or the production error formatter.
        amount = format(Decimal(quote["estimate"]) / 1_000_000, "f")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        if "." not in amount:
            amount += ".00"
        assert error["message"] == (
            f"Insufficient credits for this request's upfront reservation of ${amount}. "
            "The reservation covers the estimated input, output-token limit, and any additional usage costs; "
            "it is not the final charge. Reduce the output-token limit (max_tokens, max_completion_tokens, "
            "or max_output_tokens) or request size, wait for in-flight requests to finish, or add credits."
        )
        diagnostic = ("INFO", (
            f"billing.insufficient_credits workspace_id={key.workspace_id} key_id={key.hash} request_id=None "
            f"model={body.model} estimated_microdollars={quote['estimate']} "
            f"estimated_input_tokens={quote['input']} output_token_budget={quote['output']}"
        ))
        assert outcome["logs"].count(diagnostic) == 1
        outcome["logs"].remove(diagnostic)
        error["message"] = "Insufficient credits"
    text = json.dumps(outcome, sort_keys=True, default=str)
    # The store's own clock and random key material differ per seeding.
    for volatile in (key.hash, key.lookup_hash, key.workspace_id):
        text = text.replace(volatile, "<volatile>")
    return text


def _compare(cases: list[dict[str, Any]], backend: str, monkeypatch: pytest.MonkeyPatch,
             caplog: pytest.LogCaptureFixture) -> dict[int, int]:
    clock = _fixed_clock()
    monkeypatch.setattr(gateway, "dt", clock)
    monkeypatch.setattr(FROZEN_GATEWAY, "dt", clock)
    statuses: dict[int, int] = {}
    messages: set[str] = set()
    mismatches = []
    with caplog.at_level(logging.DEBUG):
        for index, case in enumerate(cases):
            try:
                GatewayAuthorizeRequest.model_validate({**_body_fields(case), "api_key_hash": "h"})
            except ValidationError:
                continue
            old = _strip_store_clock(_run(FROZEN_GATEWAY, case, backend, monkeypatch, caplog))
            new = _strip_store_clock(_run(gateway, case, backend, monkeypatch, caplog))
            if old != new:
                mismatches.append((index, case, old, new))
            outcome = json.loads(new)
            statuses[outcome["status"]] = statuses.get(outcome["status"], 0) + 1
            if outcome["status"] != 200:
                messages.add(json.dumps(outcome["detail"], sort_keys=True))
    assert not mismatches, f"{len(mismatches)} differences; first: {_first_difference(mismatches[0])}"
    print(f"{backend}: {sum(statuses.values())} compared of {len(cases)}, "
          f"by status {sorted(statuses.items())}, {len(messages)} distinct refusals")
    return statuses


def _first_difference(mismatch: tuple[int, dict[str, Any], str, str]) -> str:
    import difflib

    index, case, old, new = mismatch
    lines = difflib.unified_diff(
        json.dumps(json.loads(old), indent=1, sort_keys=True).splitlines(),
        json.dumps(json.loads(new), indent=1, sort_keys=True).splitlines(),
        "main", "now", lineterm="", n=2,
    )
    return f"case {index} {case}:\n" + "\n".join(list(lines)[:40])


def _strip_store_clock(text: str) -> str:
    outcome = json.loads(text)
    stored = outcome.get("stored")
    if stored:
        # The store stamps these from its own wall clock, not the route's.
        for name in ("updated_at", "expires_at", "reservation_expires_at"):
            stored.pop(name, None)
    return json.dumps(_without_durations(outcome), sort_keys=True)


def _without_durations(value: Any) -> Any:
    """Phase timings (gateway_timing) are measured wall-clock durations."""
    if isinstance(value, dict):
        return {k: _without_durations(v) for k, v in value.items() if k not in DURATIONS}
    if isinstance(value, list):
        return [_without_durations(v) for v in value]
    return value


def test_the_memory_store_matrix_is_byte_identical(monkeypatch: pytest.MonkeyPatch,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    statuses = _compare(_cases(20_000, 1), "memory", monkeypatch, caplog)
    # Positive control: the matrix admits and refuses in volume, with several error classes.
    assert statuses.get(200, 0) > 3_000 and sum(v for k, v in statuses.items() if k != 200) > 10_000
    assert {400, 403, 501} <= set(statuses)


def test_the_typed_store_matrix_is_byte_identical(monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    statuses = _compare(_cases(6_000, 2), "typed", monkeypatch, caplog)
    assert statuses.get(200, 0) > 800 and sum(v for k, v in statuses.items() if k != 200) > 3_000
    assert {400, 402, 403, 501} <= set(statuses)


def test_the_comparison_sees_a_difference(monkeypatch: pytest.MonkeyPatch,
                                          caplog: pytest.LogCaptureFixture) -> None:
    """Positive control for the harness: a one-microdollar estimate change is caught."""
    from trusted_router import authorize_candidates

    real = authorize_candidates.finish_candidates

    def off_by_one(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        return authorize_candidates.CandidateOutcome(
            outcome.candidates, outcome.service_tier, outcome.model_estimate + 1,
        )

    monkeypatch.setattr(gateway, "finish_candidates", off_by_one)
    case = {"model": "anthropic/claude-haiku-4.5", "route_type": "chat.completions"}
    with pytest.raises(AssertionError, match="1 differences"):
        _compare([case], "memory", monkeypatch, caplog)


def test_the_comparison_rejects_incorrect_reservation_guidance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    original = gateway._insufficient_credits_error

    def off_by_one(*args: Any, **kwargs: Any) -> Exception:
        kwargs["estimate"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway, "_insufficient_credits_error", off_by_one)
    case = {"model": "anthropic/claude-haiku-4.5", "estimated_input_tokens": 2_000_000_000}
    with pytest.raises(AssertionError):
        _compare([case], "typed", monkeypatch, caplog)
