from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from trusted_router.evals.draco import DRACO_EXCLUDED_SEARCH_DOMAINS, DracoTask
from trusted_router.evals.exa import (
    DEFAULT_EXA_FETCH_RESULTS,
    ExaSearchBundle,
    ExaSearchClient,
    fetch_search_result_texts,
    format_search_context,
)
from trusted_router.evals.fusion_micro import EvalConfig
from trusted_router.secrets import LocalKeyFile

DEFAULT_TR_API_BASE_URL = "https://api.quillrouter.com/v1"
DEFAULT_TR_MAX_OUTPUT_TOKENS = 1_000
DEFAULT_TR_JUDGE_MAX_OUTPUT_TOKENS = 700
DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS = 1_500
DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE = 10
DEFAULT_SEARCH_CONTEXT_CHARS_PER_RESULT = 4_000
DEFAULT_FETCH_SEARCH_RESULTS = DEFAULT_EXA_FETCH_RESULTS
DEFAULT_TOKEN_FALLBACK_MAX_TOKENS: tuple[int, ...] = (4_000, 3_000, 2_000, 1_000)
DEFAULT_LENGTH_RETRY_MAX_TOKENS: tuple[int, ...] = (8_000, 12_000)
DEFAULT_DRACO_SEARCH_QUERY_COUNT = 3
DRACO_FINANCE_INCLUDE_DOMAINS: tuple[str, ...] = (
    "sec.gov",
    "annualreports.com",
)
DRACO_FORBIDDEN_RESULT_TERMS: tuple[str, ...] = (
    "perplexity-ai/draco",
    "draco benchmark",
    "grading rubric",
    "benchmark rubric",
    "answer key",
)

ScoringMode = Literal["holistic", "criteria"]


@dataclass(frozen=True)
class SearchQuerySpec:
    query: str
    include_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatResult:
    model: str
    content: str
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    request_id: str | None
    elapsed_ms: int

    def public_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "model": self.model,
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "request_id": self.request_id,
            "elapsed_ms": self.elapsed_ms,
        }
        if include_content:
            out["content"] = self.content
        return out


@dataclass(frozen=True)
class JudgeResult:
    model: str
    score: float | None
    rationale: str
    raw: ChatResult

    def public_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        return {
            "model": self.model,
            "score": self.score,
            "rationale": self.rationale if include_content else None,
            "raw": self.raw.public_dict(include_content=include_content),
        }


@dataclass(frozen=True)
class CriterionJudgment:
    id: str
    met: bool
    weight: int
    rationale: str

    def public_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "met": self.met,
            "weight": self.weight,
        }
        if include_content:
            out["rationale"] = self.rationale
        return out


@dataclass(frozen=True)
class CriterionJudgeResult:
    model: str
    score: float | None
    criteria: tuple[CriterionJudgment, ...]
    raw: ChatResult
    raw_chunks: tuple[ChatResult, ...] = ()

    @property
    def rationale(self) -> str:
        met = sum(1 for item in self.criteria if item.met)
        return f"{met}/{len(self.criteria)} criteria marked met"

    def public_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        return {
            "model": self.model,
            "score": self.score,
            "rationale": self.rationale if include_content else None,
            "criteria": [
                item.public_dict(include_content=include_content) for item in self.criteria
            ],
            "raw": self.raw.public_dict(include_content=include_content),
            "raw_chunks": [
                item.public_dict(include_content=include_content) for item in self.raw_chunks
            ],
        }


@dataclass(frozen=True)
class FusionRunResult:
    task_id: str
    domain: str
    config_id: str
    kind: str
    search: ExaSearchBundle | None
    searches: tuple[ExaSearchBundle, ...]
    panel: tuple[ChatResult, ...]
    analysis: ChatResult | None
    final: ChatResult
    judges: tuple[JudgeResult | CriterionJudgeResult, ...]
    scoring_mode: ScoringMode = "holistic"

    @property
    def score(self) -> float | None:
        scores = [judge.score for judge in self.judges if judge.score is not None]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def public_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "domain": self.domain,
            "config_id": self.config_id,
            "kind": self.kind,
            "scoring_mode": self.scoring_mode,
            "score": self.score,
            "search": self.search.public_dict() if self.search else None,
            "searches": [item.public_dict() for item in self.searches],
            "panel": [item.public_dict(include_content=include_content) for item in self.panel],
            "analysis": self.analysis.public_dict(include_content=include_content)
            if self.analysis
            else None,
            "final": self.final.public_dict(include_content=include_content),
            "judges": [item.public_dict(include_content=include_content) for item in self.judges],
        }


class TrustedRouterChatClient:
    RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
    TOKEN_FALLBACK_STATUS_CODES = frozenset({502, 503, 504})

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_TR_API_BASE_URL,
        timeout_seconds: float = 120.0,
        stream_timeout_seconds: float = 90.0,
        retry_attempts: int = 3,
        retry_sleep_seconds: float = 0.5,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("TrustedRouter API key is required")
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be positive")
        if stream_timeout_seconds <= 0:
            raise ValueError("stream_timeout_seconds must be positive")
        if retry_sleep_seconds < 0:
            raise ValueError("retry_sleep_seconds cannot be negative")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._stream_timeout_seconds = min(timeout_seconds, stream_timeout_seconds)
        self._retry_attempts = retry_attempts
        self._retry_sleep_seconds = retry_sleep_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_TR_MAX_OUTPUT_TOKENS,
        response_format: dict[str, str] | None = None,
        token_fallback_max_tokens: tuple[int, ...] = (),
        length_retry_max_tokens: tuple[int, ...] = (),
        stream: bool = False,
    ) -> ChatResult:
        if stream:
            return self._complete_streaming(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                token_fallback_max_tokens=token_fallback_max_tokens,
                length_retry_max_tokens=length_retry_max_tokens,
            )
        started = time.perf_counter()
        request_json: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if _supports_temperature(model):
            request_json["temperature"] = temperature
        if response_format is not None:
            request_json["response_format"] = response_format
        last_result: ChatResult | None = None
        for length_attempt in _length_token_attempts(max_tokens, length_retry_max_tokens):
            response: httpx.Response | None = None
            for max_token_attempt in _max_token_attempts(
                length_attempt, token_fallback_max_tokens
            ):
                _set_max_tokens(request_json, model=model, max_tokens=max_token_attempt)
                for attempt in range(1, self._retry_attempts + 1):
                    try:
                        response = self._client.post(
                            f"{self._base_url}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self._api_key}",
                                "Content-Type": "application/json",
                            },
                            json=request_json,
                            timeout=self._timeout_seconds,
                        )
                    except (httpx.TimeoutException, httpx.NetworkError, RuntimeError):
                        if attempt == self._retry_attempts:
                            raise
                        self._sleep_before_retry(attempt)
                        continue
                    if (
                        response.status_code not in self.RETRYABLE_STATUS_CODES
                        or attempt == self._retry_attempts
                    ):
                        break
                    self._sleep_before_retry(attempt)
                if response is None:
                    continue
                if response.status_code in self.TOKEN_FALLBACK_STATUS_CODES:
                    continue
                break
            if response is None:
                continue
            result = _parse_chat_response(
                model=model,
                response=response,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            last_result = result
            if result.finish_reason != "length":
                return result
        if last_result is not None:
            return last_result
        raise RuntimeError("chat completion did not produce a response")

    def _complete_streaming(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: dict[str, str] | None,
        token_fallback_max_tokens: tuple[int, ...],
        length_retry_max_tokens: tuple[int, ...],
    ) -> ChatResult:
        started = time.perf_counter()
        request_json: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if response_format is not None:
            request_json["response_format"] = response_format
        if _supports_temperature(model):
            request_json["temperature"] = temperature
        last_result: ChatResult | None = None
        for length_attempt in _length_token_attempts(max_tokens, length_retry_max_tokens):
            max_token_attempts = _max_token_attempts(
                length_attempt, token_fallback_max_tokens
            )
            for max_token_index, max_token_attempt in enumerate(max_token_attempts):
                _set_max_tokens(request_json, model=model, max_tokens=max_token_attempt)
                for attempt in range(1, self._retry_attempts + 1):
                    try:
                        with self._client.stream(
                            "POST",
                            f"{self._base_url}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self._api_key}",
                                "Content-Type": "application/json",
                            },
                            json=request_json,
                            timeout=httpx.Timeout(
                                self._stream_timeout_seconds,
                                read=min(30.0, self._stream_timeout_seconds),
                            ),
                        ) as response:
                            if response.status_code in self.TOKEN_FALLBACK_STATUS_CODES:
                                break
                            if response.status_code in self.RETRYABLE_STATUS_CODES:
                                if attempt == self._retry_attempts:
                                    response.raise_for_status()
                                self._sleep_before_retry(attempt)
                                continue
                            result = _parse_chat_stream_response(
                                model=model,
                                response=response,
                                elapsed_ms=lambda: int(
                                    (time.perf_counter() - started) * 1000
                                ),
                                deadline_at=time.perf_counter()
                                + self._stream_timeout_seconds,
                            )
                            last_result = result
                            if result.finish_reason != "length":
                                return result
                            break
                    except (httpx.TimeoutException, httpx.NetworkError, RuntimeError):
                        if attempt == self._retry_attempts:
                            if max_token_index < len(max_token_attempts) - 1:
                                break
                            raise
                        self._sleep_before_retry(attempt)
                        continue
                if last_result is not None and last_result.finish_reason != "length":
                    return last_result
        if last_result is not None:
            return last_result
        raise RuntimeError("streaming chat completion did not produce a response")

    def _sleep_before_retry(self, attempt: int) -> None:
        if self._retry_sleep_seconds:
            time.sleep(self._retry_sleep_seconds * attempt)


class FusionLiveRunner:
    def __init__(
        self,
        *,
        tr_client: TrustedRouterChatClient,
        exa_client: ExaSearchClient | None,
        judge_passes: int = 3,
        panel_max_tokens: int = DEFAULT_TR_MAX_OUTPUT_TOKENS,
        final_max_tokens: int = DEFAULT_TR_MAX_OUTPUT_TOKENS,
        judge_max_tokens: int = DEFAULT_TR_JUDGE_MAX_OUTPUT_TOKENS,
        scoring_mode: ScoringMode = "holistic",
        criterion_chunk_size: int = DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE,
        per_generation_search: bool = True,
        fetch_search_results: bool = False,
        separate_fusion_analysis: bool = True,
        fusion_analysis_max_tokens: int = 1_200,
        length_retry_max_tokens: tuple[int, ...] = DEFAULT_LENGTH_RETRY_MAX_TOKENS,
        search_context_chars_per_result: int = DEFAULT_SEARCH_CONTEXT_CHARS_PER_RESULT,
        fetch_search_result_count: int = DEFAULT_FETCH_SEARCH_RESULTS,
        search_query_count: int = 1,
    ) -> None:
        if judge_passes < 1:
            raise ValueError("judge_passes must be positive")
        if criterion_chunk_size < 1:
            raise ValueError("criterion_chunk_size must be positive")
        for name, value in (
            ("panel_max_tokens", panel_max_tokens),
            ("final_max_tokens", final_max_tokens),
            ("judge_max_tokens", judge_max_tokens),
            ("fusion_analysis_max_tokens", fusion_analysis_max_tokens),
            ("search_context_chars_per_result", search_context_chars_per_result),
            ("fetch_search_result_count", fetch_search_result_count),
            ("search_query_count", search_query_count),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        self.tr_client = tr_client
        self.exa_client = exa_client
        self.judge_passes = judge_passes
        self.panel_max_tokens = panel_max_tokens
        self.final_max_tokens = final_max_tokens
        self.judge_max_tokens = judge_max_tokens
        self.scoring_mode = scoring_mode
        self.criterion_chunk_size = criterion_chunk_size
        self.per_generation_search = per_generation_search
        self.fetch_search_results = fetch_search_results
        self.separate_fusion_analysis = separate_fusion_analysis
        self.fusion_analysis_max_tokens = fusion_analysis_max_tokens
        self.length_retry_max_tokens = length_retry_max_tokens
        self.search_context_chars_per_result = search_context_chars_per_result
        self.fetch_search_result_count = fetch_search_result_count
        self.search_query_count = search_query_count

    def run_task_config(
        self,
        task: DracoTask,
        config: EvalConfig,
        *,
        live_search: bool,
    ) -> FusionRunResult:
        searches: list[ExaSearchBundle] = []
        shared_search_bundle: ExaSearchBundle | None = None
        if live_search and not self.per_generation_search:
            shared_search_bundle = self._search(task)
            searches.append(shared_search_bundle)

        def search_context_for_generation() -> str:
            if not live_search:
                return "Search disabled."
            if shared_search_bundle is not None:
                return format_search_context(
                    shared_search_bundle,
                    max_chars_per_result=self.search_context_chars_per_result,
                )
            search_bundles = self._searches(task)
            searches.extend(search_bundles)
            return format_search_contexts(
                search_bundles,
                max_chars_per_result=self.search_context_chars_per_result,
            )

        panel: list[ChatResult] = []
        analysis: ChatResult | None = None
        if config.kind == "fusion":
            for model in config.generation_models:
                panel.append(
                    self.tr_client.complete(
                        model=model,
                        messages=panel_messages(task, search_context_for_generation()),
                        max_tokens=self.panel_max_tokens,
                        token_fallback_max_tokens=DEFAULT_TOKEN_FALLBACK_MAX_TOKENS,
                        length_retry_max_tokens=self.length_retry_max_tokens,
                        stream=True,
                    )
                )
            if config.final_model is None:
                raise ValueError(f"fusion config {config.id} has no final model")
            if self.separate_fusion_analysis:
                analysis = self.tr_client.complete(
                    model=config.final_model,
                    messages=fusion_analysis_messages(task, tuple(panel)),
                    max_tokens=min(self.fusion_analysis_max_tokens, self.final_max_tokens),
                    response_format={"type": "json_object"},
                    length_retry_max_tokens=self.length_retry_max_tokens,
                )
            final = self.tr_client.complete(
                model=config.final_model,
                messages=synthesis_messages(
                    task, tuple(panel), analysis=analysis.content if analysis else None
                ),
                max_tokens=self.final_max_tokens,
                token_fallback_max_tokens=DEFAULT_TOKEN_FALLBACK_MAX_TOKENS,
                length_retry_max_tokens=self.length_retry_max_tokens,
                stream=True,
            )
        else:
            if len(config.generation_models) != 1:
                raise ValueError(f"solo config {config.id} must have exactly one model")
            final = self.tr_client.complete(
                model=config.generation_models[0],
                messages=panel_messages(task, search_context_for_generation()),
                max_tokens=self.final_max_tokens,
                token_fallback_max_tokens=DEFAULT_TOKEN_FALLBACK_MAX_TOKENS,
                length_retry_max_tokens=self.length_retry_max_tokens,
                stream=True,
            )
        judges: tuple[JudgeResult | CriterionJudgeResult, ...]
        if self.scoring_mode == "criteria":
            judges = tuple(
                self._judge_criteria(task, config.judge_model, final.content)
                for _index in range(self.judge_passes)
            )
        else:
            judges = tuple(
                self._judge(task, config.judge_model, final.content)
                for _index in range(self.judge_passes)
            )
        return FusionRunResult(
            task_id=task.id,
            domain=task.domain,
            config_id=config.id,
            kind=config.kind,
            search=searches[0] if searches else None,
            searches=tuple(searches),
            panel=tuple(panel),
            analysis=analysis,
            final=final,
            judges=judges,
            scoring_mode=self.scoring_mode,
        )

    def _search(self, task: DracoTask) -> ExaSearchBundle:
        return self._searches(task)[0]

    def _searches(self, task: DracoTask) -> tuple[ExaSearchBundle, ...]:
        if self.exa_client is None:
            raise ValueError("Exa client is required for live search")
        bundles: list[ExaSearchBundle] = []
        for spec in draco_search_query_specs(task, max_queries=self.search_query_count):
            bundle = self.exa_client.search_with_contents(
                spec.query,
                exclude_domains=DRACO_EXCLUDED_SEARCH_DOMAINS,
                include_domains=spec.include_domains,
            )
            if self.fetch_search_results:
                try:
                    bundle = self.exa_client.fetch_contents(
                        bundle,
                        max_results=self.fetch_search_result_count,
                        max_chars_per_result=self.search_context_chars_per_result,
                    )
                except (httpx.HTTPError, ValueError):
                    bundle = fetch_search_result_texts(
                        bundle,
                        max_results=self.fetch_search_result_count,
                        max_chars_per_result=self.search_context_chars_per_result,
                    )
            validate_draco_search_bundle(task, bundle)
            bundles.append(bundle)
        if not bundles:
            raise ValueError("no search queries were generated")
        return tuple(bundles)

    def _judge(self, task: DracoTask, judge_model: str, answer: str) -> JudgeResult:
        raw = self.tr_client.complete(
            model=judge_model,
            messages=judge_messages(task, answer),
            temperature=0.0,
            max_tokens=self.judge_max_tokens,
            response_format={"type": "json_object"},
            length_retry_max_tokens=self.length_retry_max_tokens,
        )
        score, rationale = parse_judge_json(raw.content)
        return JudgeResult(model=raw.model, score=score, rationale=rationale, raw=raw)

    def _judge_criteria(
        self, task: DracoTask, judge_model: str, answer: str
    ) -> CriterionJudgeResult:
        criteria = _flat_criteria(task.rubric)
        judgments_by_id: dict[str, CriterionJudgment] = {}
        raw_results: list[ChatResult] = []
        for chunk in _chunks(criteria, self.criterion_chunk_size):
            chunk_judgments, chunk_raw_results = self._judge_criteria_chunk(
                task,
                judge_model,
                answer,
                chunk,
            )
            raw_results.extend(chunk_raw_results)
            for judgment in chunk_judgments:
                judgments_by_id[judgment.id] = judgment
        missing = {str(criterion["id"]) for criterion in criteria} - set(judgments_by_id)
        if missing:
            raise ValueError(f"criterion judge response omitted {len(missing)} criteria")
        judgments = tuple(judgments_by_id[str(criterion["id"])] for criterion in criteria)
        score = criterion_score(task.rubric, judgments)
        first_raw = raw_results[0]
        return CriterionJudgeResult(
            model=first_raw.model,
            score=score,
            criteria=judgments,
            raw=first_raw,
            raw_chunks=tuple(raw_results),
        )

    def _judge_criteria_chunk(
        self,
        task: DracoTask,
        judge_model: str,
        answer: str,
        criteria: tuple[dict[str, str | int], ...],
    ) -> tuple[tuple[CriterionJudgment, ...], tuple[ChatResult, ...]]:
        raw = self.tr_client.complete(
            model=judge_model,
            messages=criterion_judge_messages_for_criteria(task, answer, criteria),
            temperature=0.0,
            max_tokens=max(self.judge_max_tokens, DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS),
            response_format={"type": "json_object"},
            length_retry_max_tokens=self.length_retry_max_tokens,
        )
        try:
            return parse_criterion_judge_json_for_criteria(criteria, raw.content), (raw,)
        except (json.JSONDecodeError, ValueError):
            if len(criteria) <= 1:
                raise
            midpoint = len(criteria) // 2
            left_judgments, left_raw = self._judge_criteria_chunk(
                task,
                judge_model,
                answer,
                criteria[:midpoint],
            )
            right_judgments, right_raw = self._judge_criteria_chunk(
                task,
                judge_model,
                answer,
                criteria[midpoint:],
            )
            return left_judgments + right_judgments, left_raw + right_raw


def draco_search_query(task: DracoTask) -> str:
    return (
        "Find current, primary, and authoritative sources for this research task. Task: "
        f"{task.problem[:1200]}"
    )


def draco_search_query_specs(
    task: DracoTask, *, max_queries: int = DEFAULT_DRACO_SEARCH_QUERY_COUNT
) -> tuple[SearchQuerySpec, ...]:
    if max_queries < 1:
        raise ValueError("max_queries must be positive")
    specs = [SearchQuerySpec(draco_search_query(task))]
    if max_queries == 1:
        return tuple(specs)
    problem = _compact_query_text(task.problem, max_chars=900)
    if task_requires_calculation(task) or task.domain.lower() == "finance":
        specs.append(
            SearchQuerySpec(
                "SEC EDGAR 10-Q 10-K annual report quarterly results debt notes cash flow "
                f"for: {problem}",
                include_domains=DRACO_FINANCE_INCLUDE_DOMAINS,
            )
        )
    if len(specs) < max_queries:
        specs.append(
            SearchQuerySpec(
                "official source primary filing investor relations exact figures dates "
                f"for: {problem}"
            )
        )
    return tuple(specs[:max_queries])


def format_search_contexts(
    bundles: tuple[ExaSearchBundle, ...], *, max_chars_per_result: int
) -> str:
    if not bundles:
        return "No search results were returned."
    if len(bundles) == 1:
        return format_search_context(
            bundles[0], max_chars_per_result=max_chars_per_result
        )
    parts: list[str] = []
    for index, bundle in enumerate(bundles, start=1):
        parts.append(
            "\n".join(
                (
                    f"Search pass {index}",
                    f"Query: {bundle.query}",
                    format_search_context(
                        bundle, max_chars_per_result=max_chars_per_result
                    ),
                )
            )
        )
    return "\n\n".join(parts)


def _max_token_attempts(max_tokens: int, fallback_max_tokens: tuple[int, ...]) -> tuple[int, ...]:
    attempts = [max_tokens]
    attempts.extend(value for value in fallback_max_tokens if 0 < value < max_tokens)
    return tuple(dict.fromkeys(attempts))


def _length_token_attempts(
    max_tokens: int, length_retry_max_tokens: tuple[int, ...]
) -> tuple[int, ...]:
    attempts = [max_tokens]
    attempts.extend(value for value in length_retry_max_tokens if value > max_tokens)
    return tuple(dict.fromkeys(attempts))


def _supports_temperature(model: str) -> bool:
    return not model.lower().startswith("openai/gpt-5.5")


def _uses_max_completion_tokens(model: str) -> bool:
    return model.lower().startswith("openai/gpt-5.5")


def _set_max_tokens(request_json: dict[str, Any], *, model: str, max_tokens: int) -> None:
    request_json.pop("max_tokens", None)
    request_json.pop("max_completion_tokens", None)
    if _uses_max_completion_tokens(model):
        request_json["max_completion_tokens"] = max_tokens
    else:
        request_json["max_tokens"] = max_tokens


def _parse_chat_response(*, model: str, response: httpx.Response, elapsed_ms: int) -> ChatResult:
    request_id = response.headers.get("x-request-id") or response.headers.get("x-tr-request-id")
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat completion response did not contain choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("chat completion choice had an unexpected shape")
    message = first.get("message")
    content = ""
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        content = message["content"]
    finish_reason = first.get("finish_reason")
    usage = payload.get("usage")
    input_tokens = output_tokens = None
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if isinstance(prompt_tokens, int):
            input_tokens = prompt_tokens
        if isinstance(completion_tokens, int):
            output_tokens = completion_tokens
    returned_model = payload.get("model")
    return ChatResult(
        model=returned_model if isinstance(returned_model, str) else model,
        content=content,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_id=request_id,
        elapsed_ms=elapsed_ms,
    )


def _parse_chat_stream_response(
    *,
    model: str,
    response: httpx.Response,
    elapsed_ms: Callable[[], int],
    deadline_at: float | None = None,
) -> ChatResult:
    request_id = response.headers.get("x-request-id") or response.headers.get("x-tr-request-id")
    response.raise_for_status()
    content_parts: list[str] = []
    finish_reason: str | None = None
    input_tokens = output_tokens = None
    returned_model: str | None = None
    saw_done = False
    for data in _iter_sse_data(response, deadline_at=deadline_at):
        if data == "[DONE]":
            saw_done = True
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            raise RuntimeError(message if isinstance(message, str) else "stream error")
        model_value = payload.get("model")
        if isinstance(model_value, str):
            returned_model = model_value
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if isinstance(prompt_tokens, int):
                input_tokens = prompt_tokens
            if isinstance(completion_tokens, int):
                output_tokens = completion_tokens
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            choice_finish = choice.get("finish_reason")
            if isinstance(choice_finish, str):
                finish_reason = choice_finish
            content = _stream_choice_text(choice)
            if content:
                content_parts.append(content)
    content = "".join(content_parts)
    if not saw_done and finish_reason is None:
        raise RuntimeError("stream ended without finish reason or [DONE]")
    if not content and finish_reason != "length":
        raise RuntimeError("streaming chat completion returned no content")
    return ChatResult(
        model=returned_model or model,
        content=content,
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_id=request_id,
        elapsed_ms=elapsed_ms(),
    )


def _iter_sse_data(
    response: httpx.Response, *, deadline_at: float | None = None
) -> Iterator[str]:
    data_lines: list[str] = []

    def emit_event() -> str | None:
        if not data_lines:
            return None
        event = "\n".join(data_lines).strip()
        data_lines.clear()
        return event or None

    text_buffer = ""
    for raw_chunk in response.iter_raw():
        if deadline_at is not None and time.perf_counter() > deadline_at:
            raise httpx.TimeoutException("stream deadline exceeded")
        text_buffer += raw_chunk.decode("utf-8", errors="replace")
        while "\n" in text_buffer:
            raw_line, text_buffer = text_buffer.split("\n", 1)
            line = raw_line.rstrip("\r")
            if not line:
                event = emit_event()
                if event is not None:
                    yield event
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
    if text_buffer:
        line = text_buffer.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    event = emit_event()
    if event is not None:
        if event:
            yield event


def _stream_choice_text(choice: dict[str, Any]) -> str:
    delta = choice.get("delta")
    if isinstance(delta, dict):
        for key in ("content", "reasoning_content", "reasoning", "text"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
    message = choice.get("message")
    if isinstance(message, dict):
        for key in ("content", "reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str):
                return value
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _compact_query_text(value: str, *, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def validate_draco_search_bundle(task: DracoTask, bundle: ExaSearchBundle) -> None:
    """Fail closed if search results appear to contain benchmark artifacts.

    The query itself intentionally avoids words like "rubric" and "answer key".
    This validator only inspects returned result metadata/content.
    """

    criteria = _flat_criteria(task.rubric)
    criterion_ids = {
        str(criterion["id"]).lower() for criterion in criteria if len(str(criterion["id"])) >= 16
    }
    requirement_fragments = {
        " ".join(str(criterion["requirement"]).lower().split()[:10])
        for criterion in criteria
        if len(str(criterion["requirement"]).split()) >= 10
    }
    for result in bundle.results:
        url = result.url.lower()
        haystack = "\n".join(
            (
                result.url,
                result.title,
                result.author or "",
                "\n".join(result.highlights),
                result.text or "",
                result.fetched_text or "",
            )
        ).lower()
        for domain in DRACO_EXCLUDED_SEARCH_DOMAINS:
            if domain in url:
                raise ValueError(f"Exa returned forbidden DRACO domain: {domain}")
        for term in DRACO_FORBIDDEN_RESULT_TERMS:
            if term in haystack:
                raise ValueError(f"Exa returned possible DRACO benchmark artifact: {term}")
        for criterion_id in criterion_ids:
            if criterion_id in haystack:
                raise ValueError("Exa returned possible DRACO criterion id")
        for fragment in requirement_fragments:
            if fragment and fragment in haystack:
                raise ValueError("Exa returned possible DRACO rubric requirement")


def panel_messages(task: DracoTask, search_context: str) -> list[dict[str, str]]:
    calculation_instruction = ""
    if task_requires_calculation(task):
        calculation_instruction = (
            " For numerical tasks, extract the source figures first, name the filing or page "
            "where each figure came from, show the arithmetic step by step with units, and "
            "state the final value plainly."
        )
    return [
        {
            "role": "system",
            "content": (
                "You are one member of a model panel for a deep research benchmark. "
                "Answer the task using the provided sources. Give a complete, source-grounded "
                "analysis, show quantitative calculations when the task asks for them, cite URLs "
                "inline, and explicitly call out uncertainty. Do not mention benchmark rubrics."
                f"{calculation_instruction}"
            ),
        },
        {
            "role": "user",
            "content": f"Task:\n{task.problem}\n\nSearch context:\n{search_context}",
        },
    ]


def task_requires_calculation(task: DracoTask) -> bool:
    problem = task.problem.lower()
    indicators = (
        "calculate",
        "calculation",
        "growth",
        "margin",
        "ratio",
        "operating cash flow",
        "cash generation",
        "net debt",
        "basis points",
        "year-over-year",
        "quarter-over-quarter",
        "q1",
        "q2",
        "q3",
        "q4",
    )
    return any(indicator in problem for indicator in indicators)


def synthesis_messages(
    task: DracoTask,
    panel: tuple[ChatResult, ...],
    *,
    analysis: str | None = None,
) -> list[dict[str, str]]:
    panel_text = "\n\n".join(
        f"Panel response {index} ({item.model}):\n{item.content}"
        for index, item in enumerate(panel, start=1)
    )
    analysis_text = analysis or "No separate structured analysis was produced."
    return [
        {
            "role": "system",
            "content": (
                "You synthesize a model panel into one final deep research answer. "
                "Use the structured analysis to resolve disagreements and blind spots. "
                "Write the final answer with citations. Do not invent sources."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task:\n{task.problem}\n\nStructured analysis:\n{analysis_text}\n\n"
                f"Panel outputs:\n{panel_text}"
            ),
        },
    ]


def fusion_analysis_messages(
    task: DracoTask, panel: tuple[ChatResult, ...]
) -> list[dict[str, str]]:
    panel_text = "\n\n".join(
        f"Panel response {index} ({item.model}):\n{item.content}"
        for index, item in enumerate(panel, start=1)
    )
    return [
        {
            "role": "system",
            "content": (
                "You are the analysis stage in a Fusion research pipeline. "
                "Return only JSON with keys consensus, contradictions, partial_coverage, "
                "unique_insights, and blind_spots. Each value must be an array of strings. "
                "Do not write the final answer."
            ),
        },
        {
            "role": "user",
            "content": f"Task:\n{task.problem}\n\nPanel outputs:\n{panel_text}",
        },
    ]


def judge_messages(task: DracoTask, answer: str) -> list[dict[str, str]]:
    rubric = json.dumps(task.rubric, ensure_ascii=True, sort_keys=True)
    return [
        {
            "role": "system",
            "content": (
                "You are grading a DRACO-style deep research response. Return only JSON "
                'with keys "score" (0-100 number) and "rationale" (brief string).'
            ),
        },
        {
            "role": "user",
            "content": f"Task:\n{task.problem}\n\nRubric:\n{rubric}\n\nCandidate answer:\n{answer}",
        },
    ]


def criterion_judge_messages(task: DracoTask, answer: str) -> list[dict[str, str]]:
    return criterion_judge_messages_for_criteria(task, answer, _flat_criteria(task.rubric))


def criterion_judge_messages_for_criteria(
    task: DracoTask,
    answer: str,
    criteria: tuple[dict[str, str | int], ...],
) -> list[dict[str, str]]:
    criteria_json = json.dumps(criteria, ensure_ascii=True, sort_keys=True)
    return [
        {
            "role": "system",
            "content": (
                "You are grading a DRACO deep research response criterion by criterion. "
                'Return only JSON with key "criteria". Its value must be an array of objects '
                'with keys "id" and "met" (boolean). Do not include prose or explanations. '
                "Mark met=true only when the candidate answer explicitly satisfies that criterion. "
                "For negative-weight criteria, met=true means the answer contains that error."
            ),
        },
        {
            "role": "user",
            "content": f"Task:\n{task.problem}\n\nCriteria:\n{criteria_json}\n\nCandidate answer:\n{answer}",
        },
    ]


def parse_judge_json(content: str) -> tuple[float | None, str]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = _strip_fenced_json(stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None, content[:500]
    if not isinstance(parsed, dict):
        return None, content[:500]
    raw_score = parsed.get("score")
    score = float(raw_score) if isinstance(raw_score, int | float) else None
    if score is not None:
        score = max(0.0, min(100.0, score))
    raw_rationale = parsed.get("rationale")
    rationale = raw_rationale if isinstance(raw_rationale, str) else ""
    return score, rationale


def parse_criterion_judge_json(
    rubric: dict[str, Any],
    content: str,
) -> tuple[CriterionJudgment, ...]:
    return parse_criterion_judge_json_for_criteria(_flat_criteria(rubric), content)


def parse_criterion_judge_json_for_criteria(
    criteria: tuple[dict[str, str | int], ...],
    content: str,
) -> tuple[CriterionJudgment, ...]:
    expected = {str(criterion["id"]): criterion for criterion in criteria}
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = _strip_fenced_json(stripped)
    parsed = json.loads(stripped)
    raw_items: Any
    if isinstance(parsed, list):
        raw_items = parsed
    elif isinstance(parsed, dict):
        raw_items = parsed.get("criteria")
    else:
        raise ValueError("criterion judge response must be a JSON object or array")
    if not isinstance(raw_items, list):
        raise ValueError("criterion judge response must contain a criteria list")
    by_id: dict[str, CriterionJudgment] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        criterion_id = item.get("id")
        if not isinstance(criterion_id, str) or criterion_id not in expected:
            continue
        met = item.get("met")
        rationale = item.get("rationale")
        expected_item = expected[criterion_id]
        by_id[criterion_id] = CriterionJudgment(
            id=criterion_id,
            met=met if isinstance(met, bool) else False,
            weight=int(expected_item["weight"]),
            rationale=rationale if isinstance(rationale, str) else "",
        )
    missing = set(expected) - set(by_id)
    if missing:
        raise ValueError(f"criterion judge response omitted {len(missing)} criteria")
    ordered: list[CriterionJudgment] = []
    for criterion in criteria:
        criterion_id = criterion["id"]
        if not isinstance(criterion_id, str):
            raise ValueError("rubric criterion id must be a string")
        ordered.append(by_id[criterion_id])
    return tuple(ordered)


def criterion_score(
    rubric: dict[str, Any],
    judgments: tuple[CriterionJudgment, ...],
) -> float:
    positive_total = sum(max(0, int(criterion["weight"])) for criterion in _flat_criteria(rubric))
    if positive_total <= 0:
        raise ValueError("rubric has no positive criteria weight")
    raw_score = sum(judgment.weight for judgment in judgments if judgment.met)
    return max(0.0, min(100.0, 100.0 * raw_score / positive_total))


def _flat_criteria(rubric: dict[str, Any]) -> tuple[dict[str, str | int], ...]:
    sections = rubric.get("sections")
    if not isinstance(sections, list):
        raise ValueError("rubric is missing sections")
    criteria: list[dict[str, str | int]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        raw_criteria = section.get("criteria")
        if not isinstance(raw_criteria, list):
            continue
        for item in raw_criteria:
            if not isinstance(item, dict):
                continue
            criterion_id = item.get("id")
            requirement = item.get("requirement")
            weight = item.get("weight")
            if not isinstance(criterion_id, str) or not criterion_id.strip():
                continue
            if not isinstance(requirement, str) or not requirement.strip():
                continue
            if not isinstance(weight, int):
                continue
            criteria.append(
                {
                    "id": criterion_id,
                    "requirement": requirement,
                    "weight": weight,
                }
            )
    if not criteria:
        raise ValueError("rubric contains no criteria")
    return tuple(criteria)


def _chunks(
    values: tuple[dict[str, str | int], ...],
    size: int,
) -> tuple[tuple[dict[str, str | int], ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def load_eval_key(
    name: str, *, key_file: Path = Path("~/.quill_cloud_keys.private").expanduser()
) -> str | None:
    if value := os.environ.get(name):
        return value
    value = LocalKeyFile(key_file).get(name)
    if value:
        return value
    return None


def write_fusion_run_results(
    results: tuple[FusionRunResult, ...],
    path: Path,
    *,
    include_content: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(
                json.dumps(result.public_dict(include_content=include_content), sort_keys=True)
                + "\n"
            )


def _strip_fenced_json(value: str) -> str:
    lines = value.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
