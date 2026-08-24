from __future__ import annotations

import importlib.util
import json
import sys
import types

import httpx
import pytest

from trusted_router.evals.draco import (
    DRACO_CONFIG,
    DRACO_DATASET,
    DRACO_DATASETS_SERVER_URL,
    DRACO_EXCLUDED_SEARCH_DOMAINS,
    DRACO_SPLIT,
    DracoTask,
    draco_cache_artifact,
    draco_public_task_artifact,
    filter_draco_tasks,
    load_or_fetch_draco_tasks,
    parse_draco_rows,
)
from trusted_router.evals.exa import (
    DEFAULT_EXA_FETCH_RESULTS,
    EXA_CONTENTS_URL,
    EXA_SEARCH_URL,
    ExaResult,
    ExaSearchBundle,
    ExaSearchClient,
    fetch_result_text,
    fetch_search_result_texts,
    format_search_context,
)
from trusted_router.evals.fusion_live import (
    ChatResult,
    CriterionJudgment,
    FusionLiveRunner,
    TrustedRouterChatClient,
    criterion_score,
    draco_search_query,
    draco_search_query_specs,
    filter_draco_search_bundle,
    format_search_contexts,
    panel_messages,
    parse_criterion_judge_json,
    parse_judge_json,
    task_requires_calculation,
    validate_draco_search_bundle,
    write_fusion_run_results,
)
from trusted_router.evals.fusion_micro import EvalConfig


def _chat_sse(
    model: str,
    content: str,
    *,
    finish_reason: str = "stop",
    delta_key: str = "content",
) -> str:
    return (
        "data: "
        + json.dumps({"model": model, "choices": [{"delta": {delta_key: content}}]})
        + "\n\n"
        + "data: "
        + json.dumps(
            {
                "model": model,
                "choices": [{"delta": {}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        + "\n\n"
        + "data: [DONE]\n\n"
    )


def _chat_error_sse(message: str) -> str:
    return "data: " + json.dumps({"error": {"message": message}}) + "\n\n"


def _draco_payload() -> dict[str, object]:
    rubric = {
        "sections": [
            {
                "title": "Factual Accuracy",
                "criteria": [
                    {"id": "cite-primary", "weight": 10, "requirement": "Cites primary sources"}
                ],
            }
        ]
    }
    return {
        "rows": [
            {
                "row": {
                    "id": "task-1",
                    "domain": "Academic",
                    "problem": "Compare staggered adoption DiD estimators.",
                    "answer": json.dumps(rubric),
                }
            }
        ]
    }


def test_parse_draco_rows_treats_answer_as_rubric_only() -> None:
    tasks = parse_draco_rows(_draco_payload())

    assert tasks[0].id == "task-1"
    assert "DiD" in tasks[0].problem
    assert tasks[0].rubric["sections"][0]["title"] == "Factual Accuracy"
    public = draco_public_task_artifact(tasks)
    serialized = json.dumps(public)
    assert "cite-primary" not in serialized
    assert "answer" not in serialized


def test_required_extraction_dependencies_are_installed() -> None:
    assert importlib.util.find_spec("sec_parser") is not None
    assert importlib.util.find_spec("docling.document_converter") is not None


def test_draco_search_query_does_not_seed_leakage_terms() -> None:
    task = parse_draco_rows(_draco_payload())[0]

    query = draco_search_query(task).lower()

    assert "rubric" not in query
    assert "answer key" not in query
    assert "benchmark" not in query
    assert "huggingface" not in query


def test_draco_cache_refetches_when_too_small(httpx_mock, tmp_path) -> None:  # type: ignore[no-untyped-def]
    cached = parse_draco_rows(_draco_payload())
    cache_path = tmp_path / "tasks.json"
    cache_path.write_text(json.dumps(draco_cache_artifact(cached)), encoding="utf-8")
    payload = _draco_payload()
    rows = payload["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first_row = first["row"]
    assert isinstance(first_row, dict)
    second = dict(first_row)
    second["id"] = "task-2"
    rows.append({"row": second})
    httpx_mock.add_response(
        method="GET",
        url=httpx.URL(
            DRACO_DATASETS_SERVER_URL,
            params={
                "dataset": DRACO_DATASET,
                "config": DRACO_CONFIG,
                "split": DRACO_SPLIT,
                "offset": 0,
                "length": 2,
            },
        ),
        json=payload,
    )

    tasks = load_or_fetch_draco_tasks(cache_path, length=2)

    assert [task.id for task in tasks] == ["task-1", "task-2"]


def test_draco_non_financial_filter_excludes_finance_tasks() -> None:
    tasks = (
        DracoTask(
            id="finance",
            domain="Finance",
            problem="Calculate operating cash flow from a 10-Q.",
            rubric={"sections": []},
        ),
        DracoTask(
            id="academic",
            domain="Academic",
            problem="Compare staggered adoption estimators.",
            rubric={"sections": []},
        ),
    )

    filtered = filter_draco_tasks(tasks, task_filter="non-financial")

    assert [task.id for task in filtered] == ["academic"]
    assert filter_draco_tasks(tasks, task_filter="all") == tasks


def test_exa_client_sends_excluded_domains_and_parses_highlights(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url=EXA_SEARCH_URL,
        json={
            "requestId": "exa-1",
            "resolvedSearchType": "auto",
            "costDollars": {"total": 0.007},
            "results": [
                {
                    "title": "Good source",
                    "url": "https://example.com/source",
                    "publishedDate": "2026-01-01",
                    "highlights": ["Dense useful extract."],
                }
            ],
        },
    )
    client = ExaSearchClient("exa_test", client=httpx.Client())

    bundle = client.search_with_contents(
        "query",
        exclude_domains=DRACO_EXCLUDED_SEARCH_DOMAINS,
        num_results=3,
    )

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["excludeDomains"] == list(DRACO_EXCLUDED_SEARCH_DOMAINS)
    assert body["contents"]["highlights"] is True
    assert body["numResults"] == 3
    assert bundle.request_id == "exa-1"
    assert bundle.cost_dollars == 0.007
    assert "Dense useful extract." in format_search_context(bundle)


def test_exa_client_sends_include_domains_for_targeted_search(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url=EXA_SEARCH_URL,
        json={"results": []},
    )
    client = ExaSearchClient("exa_test", client=httpx.Client())

    client.search_with_contents("query", include_domains=("sec.gov",))

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["includeDomains"] == ["sec.gov"]


def test_exa_client_fetch_contents_uses_exa_text_and_highlights(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    bundle = ExaSearchBundle(
        query="Acadia margin calculation",
        request_id="exa-1",
        resolved_search_type="auto",
        cost_dollars=None,
        results=(
            ExaResult(
                title="SEC filing",
                url="https://www.sec.gov/Archives/example.htm",
                published_date=None,
                author=None,
                highlights=("search excerpt",),
                text=None,
            ),
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=EXA_CONTENTS_URL,
        json={
            "results": [
                {
                    "url": "https://www.sec.gov/Archives/example.htm",
                    "text": "Full extracted filing table text",
                    "highlights": ["margin table highlight"],
                }
            ],
            "statuses": [{"id": "https://www.sec.gov/Archives/example.htm", "status": "success"}],
        },
    )
    client = ExaSearchClient("exa_test", client=httpx.Client())

    fetched = client.fetch_contents(bundle, max_results=1, max_chars_per_result=8_000)

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["urls"] == ["https://www.sec.gov/Archives/example.htm"]
    assert body["text"]["maxCharacters"] == 8_000
    assert body["highlights"]["query"] == "Acadia margin calculation"
    assert fetched.results[0].fetched_text == "Full extracted filing table text"
    assert "margin table highlight" in fetched.results[0].highlights
    assert "Full extracted filing table text" not in json.dumps(fetched.public_dict())


def test_exa_client_fetch_contents_defaults_to_five_results(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    bundle = ExaSearchBundle(
        query="source heavy task",
        request_id="exa-1",
        resolved_search_type="auto",
        cost_dollars=None,
        results=tuple(
            ExaResult(
                title=f"Source {index}",
                url=f"https://example.com/source-{index}",
                published_date=None,
                author=None,
                highlights=(),
                text=None,
            )
            for index in range(DEFAULT_EXA_FETCH_RESULTS + 1)
        ),
    )
    httpx_mock.add_response(method="POST", url=EXA_CONTENTS_URL, json={"results": []})
    client = ExaSearchClient("exa_test", client=httpx.Client())

    client.fetch_contents(bundle)

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["urls"] == [
        f"https://example.com/source-{index}" for index in range(DEFAULT_EXA_FETCH_RESULTS)
    ]


def test_fetch_search_result_texts_adds_transient_page_context(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    bundle = ExaSearchBundle(
        query="query",
        request_id="exa-1",
        resolved_search_type="auto",
        cost_dollars=None,
        results=(
            ExaResult(
                title="Fetched source",
                url="https://example.com/source",
                published_date=None,
                author=None,
                highlights=("short highlight",),
                text=None,
            ),
        ),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://example.com/source",
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><head><style>.x{}</style></head><body><h1>Important page text</h1><script>bad()</script></body></html>",
    )

    fetched = fetch_search_result_texts(bundle, client=httpx.Client())

    assert fetched.results[0].fetched_text == "Important page text"
    assert "Important page text" in format_search_context(fetched)
    assert "Important page text" not in json.dumps(fetched.public_dict())


def test_fetch_search_result_texts_preserves_table_cell_boundaries(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    bundle = ExaSearchBundle(
        query="query",
        request_id="exa-1",
        resolved_search_type="auto",
        cost_dollars=None,
        results=(
            ExaResult(
                title="Table source",
                url="https://example.com/table",
                published_date=None,
                author=None,
                highlights=(),
                text=None,
            ),
        ),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://example.com/table",
        headers={"content-type": "text/html; charset=utf-8"},
        text="<table><tr><th>Segment</th><th>Income</th></tr><tr><td>Core</td><td>42</td></tr></table>",
    )

    fetched = fetch_search_result_texts(bundle, client=httpx.Client())

    assert fetched.results[0].fetched_text is not None
    assert "| Segment | Income" in fetched.results[0].fetched_text
    assert "| Core | 42" in fetched.results[0].fetched_text
    assert "| Segment | Income\n| Core | 42" in fetched.results[0].fetched_text


def test_format_search_context_reserves_space_for_fetched_text() -> None:
    bundle = ExaSearchBundle(
        query="query",
        request_id=None,
        resolved_search_type=None,
        cost_dollars=None,
        results=(
            ExaResult(
                title="Long source",
                url="https://example.com/source",
                published_date=None,
                author=None,
                highlights=("highlight " * 500,),
                text=None,
                fetched_text="critical fetched excerpt",
            ),
        ),
    )

    context = format_search_context(bundle, max_chars_per_result=500)

    assert "highlight" in context
    assert "critical fetched excerpt" in context


def test_format_search_contexts_labels_multiple_query_passes() -> None:
    bundles = (
        ExaSearchBundle(
            query="primary",
            request_id=None,
            resolved_search_type=None,
            cost_dollars=None,
            results=(
                ExaResult(
                    title="Primary source",
                    url="https://example.com/primary",
                    published_date=None,
                    author=None,
                    highlights=("primary text",),
                    text=None,
                ),
            ),
        ),
        ExaSearchBundle(
            query="sec",
            request_id=None,
            resolved_search_type=None,
            cost_dollars=None,
            results=(
                ExaResult(
                    title="SEC source",
                    url="https://www.sec.gov/source",
                    published_date=None,
                    author=None,
                    highlights=("sec text",),
                    text=None,
                ),
            ),
        ),
    )

    context = format_search_contexts(bundles, max_chars_per_result=500)

    assert "Search pass 1" in context
    assert "Query: primary" in context
    assert "Search pass 2" in context
    assert "Query: sec" in context


def test_fetch_result_text_extracts_pdf_with_optional_pdftotext(httpx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_pdf_text(raw: bytes, *, timeout_seconds: float) -> str:
        assert raw == b"%PDF fake"
        assert timeout_seconds == 3.0
        return "PDF extracted text"

    monkeypatch.setattr("trusted_router.evals.exa._pdf_text_from_bytes", fake_pdf_text)
    httpx_mock.add_response(
        method="GET",
        url="https://example.com/paper.pdf",
        headers={"content-type": "application/pdf"},
        content=b"%PDF fake",
    )

    text = fetch_result_text(
        "https://example.com/paper.pdf", timeout_seconds=3.0, client=httpx.Client()
    )

    assert text == "PDF extracted text"


def test_fetch_result_text_uses_docling_for_pdf_when_available(
    httpx_mock, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class FakeDocument:
        def export_to_markdown(self) -> str:
            return "# Parsed PDF\n\n| Metric | Value |\n| Revenue | 42 |"

    class FakeResult:
        document = FakeDocument()

    class FakeConverter:
        def convert(self, source: str, *, raises_on_error: bool) -> FakeResult:
            assert source.endswith(".pdf")
            assert raises_on_error is False
            return FakeResult()

    fake_module = types.SimpleNamespace(DocumentConverter=FakeConverter)
    monkeypatch.setitem(sys.modules, "docling", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_module)
    httpx_mock.add_response(
        method="GET",
        url="https://example.com/report.pdf",
        headers={"content-type": "application/pdf"},
        content=b"%PDF fake",
    )

    text = fetch_result_text("https://example.com/report.pdf", client=httpx.Client())

    assert text is not None
    assert "Parsed PDF" in text
    assert "| Metric | Value |" in text


def test_fetch_result_text_uses_sec_parser_for_sec_html_when_available(
    httpx_mock, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class FakeParser:
        def parse(self, html: str) -> list[str]:
            assert "CONDENSED CONSOLIDATED" in html
            return ["elements"]

    fake_module = types.SimpleNamespace(
        Edgar10QParser=FakeParser,
        render=lambda elements: "TableElement: Table with ~2 rows and 4 numbers.",
    )
    monkeypatch.setitem(sys.modules, "sec_parser", fake_module)
    httpx_mock.add_response(
        method="GET",
        url="https://www.sec.gov/Archives/example.htm",
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><body><h1>CONDENSED CONSOLIDATED</h1><table><tr><td>Revenue</td><td>42</td></tr></table></body></html>",
    )

    text = fetch_result_text("https://www.sec.gov/Archives/example.htm", client=httpx.Client())

    assert text is not None
    assert "SEC semantic structure:" in text
    assert "TableElement: Table with ~2 rows" in text
    assert "Visible filing text:" in text
    assert "| Revenue | 42" in text


def test_draco_search_validation_rejects_forbidden_result_domain() -> None:
    task = parse_draco_rows(_draco_payload())[0]
    bundle = ExaSearchBundle(
        query="clean query",
        request_id=None,
        resolved_search_type=None,
        cost_dollars=None,
        results=(
            ExaResult(
                title="DRACO row",
                url="https://huggingface.co/datasets/perplexity-ai/draco",
                published_date=None,
                author=None,
                highlights=(),
                text=None,
            ),
        ),
    )

    with pytest.raises(ValueError, match="forbidden DRACO domain"):
        validate_draco_search_bundle(task, bundle)


def test_draco_search_validation_rejects_rubric_content() -> None:
    task = parse_draco_rows(_draco_payload())[0]
    bundle = ExaSearchBundle(
        query="clean query",
        request_id=None,
        resolved_search_type=None,
        cost_dollars=None,
        results=(
            ExaResult(
                title="Normal source",
                url="https://example.com/source",
                published_date=None,
                author=None,
                highlights=("This page contains a benchmark rubric and answer key.",),
                text=None,
            ),
        ),
    )

    with pytest.raises(ValueError, match="benchmark artifact"):
        validate_draco_search_bundle(task, bundle)


def test_draco_search_validation_rejects_fetched_rubric_content() -> None:
    task = parse_draco_rows(_draco_payload())[0]
    bundle = ExaSearchBundle(
        query="clean query",
        request_id=None,
        resolved_search_type=None,
        cost_dollars=None,
        results=(
            ExaResult(
                title="Normal source",
                url="https://example.com/source",
                published_date=None,
                author=None,
                highlights=("Normal search snippet.",),
                text=None,
                fetched_text="This fetched body contains a DRACO benchmark grading rubric.",
            ),
        ),
    )

    with pytest.raises(ValueError, match="benchmark artifact"):
        validate_draco_search_bundle(task, bundle)


def test_draco_search_filter_drops_tainted_results_and_keeps_clean_sources() -> None:
    task = parse_draco_rows(_draco_payload())[0]
    bundle = ExaSearchBundle(
        query="clean query",
        request_id="req_1",
        resolved_search_type="auto",
        cost_dollars=0.001,
        results=(
            ExaResult(
                title="Normal source",
                url="https://example.com/source",
                published_date=None,
                author=None,
                highlights=("Normal search snippet.",),
                text="Useful public source.",
            ),
            ExaResult(
                title="DRACO leak",
                url="https://example.com/leak",
                published_date=None,
                author=None,
                highlights=("This page contains a DRACO benchmark rubric.",),
                text=None,
            ),
        ),
    )

    filtered = filter_draco_search_bundle(task, bundle)

    assert filtered.request_id == "req_1"
    assert filtered.cost_dollars == 0.001
    assert [result.url for result in filtered.results] == ["https://example.com/source"]


def test_draco_search_filter_rejects_all_tainted_results() -> None:
    task = parse_draco_rows(_draco_payload())[0]
    bundle = ExaSearchBundle(
        query="clean query",
        request_id=None,
        resolved_search_type=None,
        cost_dollars=None,
        results=(
            ExaResult(
                title="DRACO leak",
                url="https://example.com/leak",
                published_date=None,
                author=None,
                highlights=("This page contains a DRACO benchmark rubric.",),
                text=None,
            ),
        ),
    )

    with pytest.raises(ValueError, match="only forbidden"):
        filter_draco_search_bundle(task, bundle)


def test_fusion_runner_uses_search_panel_synthesis_and_judges(httpx_mock, tmp_path) -> None:  # type: ignore[no-untyped-def]
    task = DracoTask(
        id="task-1",
        domain="Academic",
        problem="Compare staggered adoption DiD estimators.",
        rubric={"sections": [{"criteria": [{"id": "a", "weight": 1, "requirement": "be right"}]}]},
    )
    config = EvalConfig(
        id="fusion_test",
        label="Fusion test",
        kind="fusion",
        generation_models=("model-a", "model-b"),
        final_model="model-final",
        judge_model="model-judge",
    )
    for request_id in ("exa-1", "exa-2"):
        httpx_mock.add_response(
            method="POST",
            url=EXA_SEARCH_URL,
            json={
                "requestId": request_id,
                "results": [
                    {"title": "Source", "url": "https://example.com", "highlights": ["source text"]}
                ],
            },
        )
    for model, content in (
        ("model-a", "panel a cites https://example.com"),
        ("model-b", "panel b cites https://example.com"),
    ):
        httpx_mock.add_response(
            method="POST",
            url="https://api.test/v1/chat/completions",
            text=_chat_sse(model, content),
            headers={"content-type": "text/event-stream"},
        )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-final",
            "choices": [
                {
                    "message": {
                        "content": '{"consensus":["both cite source"],"contradictions":[],"partial_coverage":[],"unique_insights":[],"blind_spots":[]}'
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-final", "final answer cites https://example.com"),
        headers={"content-type": "text/event-stream"},
    )
    for content in ('{"score": 82, "rationale": "solid"}', '{"score": 84, "rationale": "solid"}'):
        httpx_mock.add_response(
            method="POST",
            url="https://api.test/v1/chat/completions",
            json={
                "model": "model-judge",
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    tr_client = TrustedRouterChatClient(
        "sk-test", base_url="https://api.test/v1", client=httpx.Client()
    )
    exa_client = ExaSearchClient("exa-test", client=httpx.Client())
    runner = FusionLiveRunner(
        tr_client=tr_client,
        exa_client=exa_client,
        judge_passes=2,
        panel_concurrency=1,
    )

    result = runner.run_task_config(task, config, live_search=True)

    assert result.score == 83
    assert [item.model for item in result.panel] == ["model-a", "model-b"]
    assert result.analysis is not None
    assert result.final.model == "model-final"
    assert result.search is not None
    assert [item.request_id for item in result.searches] == ["exa-1", "exa-2"]
    output = tmp_path / "results.jsonl"
    write_fusion_run_results((result,), output, include_content=False)
    serialized = output.read_text(encoding="utf-8")
    assert "final answer cites" not in serialized
    assert "both cite source" not in serialized
    assert "sk-test" not in serialized


def test_fusion_runner_records_panel_failure_and_continues(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    task = DracoTask(
        id="task-panel-fail",
        domain="Academic",
        problem="Compare the methods.",
        rubric={"sections": [{"criteria": [{"id": "a", "weight": 1, "requirement": "be right"}]}]},
    )
    config = EvalConfig(
        id="fusion_test",
        label="Fusion test",
        kind="fusion",
        generation_models=("model-bad", "model-good"),
        final_model="model-final",
        judge_model="model-judge",
    )
    for request_id in ("exa-bad", "exa-good"):
        httpx_mock.add_response(
            method="POST",
            url=EXA_SEARCH_URL,
            json={
                "requestId": request_id,
                "results": [
                    {"title": "Source", "url": "https://example.com", "highlights": ["source text"]}
                ],
            },
        )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_error_sse("provider failed"),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-good", "usable panel answer"),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-final",
            "choices": [
                {
                    "message": {
                        "content": '{"consensus":["good panel usable"],"contradictions":[],"partial_coverage":[],"unique_insights":[],"blind_spots":[]}'
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-final", "final answer"),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-judge",
            "choices": [
                {"message": {"content": '{"score": 80, "rationale": "ok"}'}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )

    tr_client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )
    exa_client = ExaSearchClient("exa-test", client=httpx.Client())
    runner = FusionLiveRunner(
        tr_client=tr_client,
        exa_client=exa_client,
        judge_passes=1,
        panel_concurrency=1,
    )

    result = runner.run_task_config(task, config, live_search=True)

    assert [item.model for item in result.panel] == ["model-good"]
    assert len(result.panel_failures) == 1
    assert result.panel_failures[0].model == "model-bad"
    assert result.panel_failures[0].error_type == "RuntimeError"
    assert "provider failed" in result.panel_failures[0].message
    assert result.score == 80
    assert result.public_dict()["panel_failures"][0]["model"] == "model-bad"


def test_fusion_runner_concurrent_panel_preserves_config_order() -> None:
    task = DracoTask(
        id="task-parallel",
        domain="Academic",
        problem="Compare the methods.",
        rubric={"sections": [{"criteria": [{"id": "a", "weight": 1, "requirement": "be right"}]}]},
    )
    config = EvalConfig(
        id="fusion_parallel",
        label="Fusion parallel",
        kind="fusion",
        generation_models=("model-a", "model-b", "model-c"),
        final_model="model-final",
        judge_model="model-judge",
    )

    class FakeTrustedRouterClient:
        panel_timeouts: list[float | None] = []

        def complete(self, **kwargs: object) -> ChatResult:
            model = kwargs["model"]
            assert isinstance(model, str)
            response_format = kwargs.get("response_format")
            if model in {"model-a", "model-b", "model-c"}:
                stream_timeout = kwargs.get("stream_timeout_seconds")
                assert stream_timeout is None or isinstance(stream_timeout, float)
                self.panel_timeouts.append(stream_timeout)
            if model == "model-final" and response_format is not None:
                return ChatResult(
                    model=model,
                    content='{"consensus":["ok"],"contradictions":[],"partial_coverage":[],"unique_insights":[],"blind_spots":[]}',
                    finish_reason="stop",
                    input_tokens=1,
                    output_tokens=1,
                    request_id=None,
                    elapsed_ms=1,
                )
            if model == "model-final":
                return ChatResult(
                    model=model,
                    content="final answer",
                    finish_reason="stop",
                    input_tokens=1,
                    output_tokens=1,
                    request_id=None,
                    elapsed_ms=1,
                )
            if model == "model-judge":
                return ChatResult(
                    model=model,
                    content='{"score": 77, "rationale": "ok"}',
                    finish_reason="stop",
                    input_tokens=1,
                    output_tokens=1,
                    request_id=None,
                    elapsed_ms=1,
                )
            return ChatResult(
                model=model,
                content=f"{model} panel answer",
                finish_reason="stop",
                input_tokens=1,
                output_tokens=1,
                request_id=None,
                elapsed_ms=1,
            )

    runner = FusionLiveRunner(
        tr_client=FakeTrustedRouterClient(),  # type: ignore[arg-type]
        exa_client=None,
        judge_passes=1,
        panel_concurrency=3,
        panel_stream_timeout_seconds=42.0,
    )

    result = runner.run_task_config(task, config, live_search=False)

    assert [item.model for item in result.panel] == ["model-a", "model-b", "model-c"]
    assert result.panel_failures == ()
    assert result.score == 77
    fake_client = runner.tr_client
    assert isinstance(fake_client, FakeTrustedRouterClient)
    assert sorted(fake_client.panel_timeouts) == [42.0, 42.0, 42.0]


def test_trustedrouter_client_streams_chat_and_parses_reasoning_deltas(
    httpx_mock,
) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-a", "reasoned answer", delta_key="reasoning_content"),
        headers={"content-type": "text/event-stream", "x-request-id": "req-stream"},
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="model-a",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 1_000
    assert result.content == "reasoned answer"
    assert result.finish_reason == "stop"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.request_id == "req-stream"


def test_trustedrouter_client_retries_stream_provider_error(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_error_sse("provider error"),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-a", "retry ok"),
        headers={"content-type": "text/event-stream"},
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=2,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="model-a",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    assert result.content == "retry ok"
    assert len(httpx_mock.get_requests()) == 2


def test_trustedrouter_client_stream_falls_back_to_lower_token_cap(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_error_sse("provider error"),
        headers={"content-type": "text/event-stream"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("model-a", "lower cap ok"),
        headers={"content-type": "text/event-stream"},
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="model-a",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5_000,
        token_fallback_max_tokens=(1_000,),
        stream=True,
    )

    requests = httpx_mock.get_requests()
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [
        5_000,
        1_000,
    ]
    assert result.content == "lower cap ok"


def test_trustedrouter_client_shapes_gpt_5_5_requests_for_openai(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        text=_chat_sse("openai/gpt-5.5", "PONG"),
        headers={"content-type": "text/event-stream"},
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="openai/gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=16,
        stream=True,
    )

    request = httpx_mock.get_request()
    assert request is not None
    body = json.loads(request.content)
    assert body["stream"] is True
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 16
    assert result.content == "PONG"


def test_trustedrouter_client_retries_gpt_5_5_empty_length_with_larger_budgets(
    httpx_mock,
) -> None:  # type: ignore[no-untyped-def]
    for output_tokens in (16, 16_000, 32_000):
        httpx_mock.add_response(
            method="POST",
            url="https://api.test/v1/chat/completions",
            json={
                "model": "openai/gpt-5.5",
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": output_tokens},
            },
        )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )

    with pytest.raises(RuntimeError, match="empty length response"):
        client.complete(
            model="openai/gpt-5.5",
            messages=[{"role": "user", "content": "hard DRACO prompt"}],
            max_tokens=16,
        )

    requests = httpx_mock.get_requests()
    assert [
        json.loads(request.content)["max_completion_tokens"] for request in requests
    ] == [16, 16_000, 32_000]
    assert all("temperature" not in json.loads(request.content) for request in requests)


def test_trustedrouter_client_retries_transient_status(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        status_code=502,
        json={"error": {"message": "temporary"}},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-a",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=2,
        retry_sleep_seconds=0,
    )

    result = client.complete(model="model-a", messages=[{"role": "user", "content": "hi"}])

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert result.content == "ok"


def test_trustedrouter_client_reduces_tokens_after_repeated_502(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    for _ in range(2):
        httpx_mock.add_response(
            method="POST",
            url="https://api.test/v1/chat/completions",
            status_code=502,
            json={"error": {"message": "provider error"}},
        )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-a",
            "choices": [{"message": {"content": "fallback ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=2,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="model-a",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5_000,
        token_fallback_max_tokens=(3_000,),
    )

    requests = httpx_mock.get_requests()
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [
        5_000,
        5_000,
        3_000,
    ]
    assert result.content == "fallback ok"


def test_calculation_tasks_get_arithmetic_instruction() -> None:
    task = DracoTask(
        id="finance",
        domain="Finance",
        problem="Calculate Q1 operating margin and year-over-year growth.",
        rubric={"sections": [{"criteria": [{"id": "a", "weight": 1, "requirement": "math"}]}]},
    )

    messages = panel_messages(task, "source")

    assert task_requires_calculation(task)
    assert "show the arithmetic step by step" in messages[0]["content"]


def test_finance_tasks_get_sec_biased_search_query() -> None:
    task = DracoTask(
        id="finance",
        domain="Finance",
        problem="Calculate Q1 operating margin and total debt from the company's 10-Q.",
        rubric={"sections": [{"criteria": [{"id": "a", "weight": 1, "requirement": "math"}]}]},
    )

    specs = draco_search_query_specs(task, max_queries=3)

    assert len(specs) == 3
    assert specs[1].include_domains == ("sec.gov", "annualreports.com")
    assert "SEC EDGAR" in specs[1].query
    assert "official source" in specs[2].query


def test_trustedrouter_client_retries_length_with_larger_token_cap(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-a",
            "choices": [{"message": {"content": "truncated"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 5},
        },
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/v1/chat/completions",
        json={
            "model": "model-a",
            "choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 8},
        },
    )
    client = TrustedRouterChatClient(
        "sk-test",
        base_url="https://api.test/v1",
        client=httpx.Client(),
        retry_attempts=1,
        retry_sleep_seconds=0,
    )

    result = client.complete(
        model="model-a",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=2_000,
        length_retry_max_tokens=(8_000,),
    )

    requests = httpx_mock.get_requests()
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [
        2_000,
        8_000,
    ]
    assert result.content == "complete"
    assert result.finish_reason == "stop"


def test_parse_judge_json_accepts_fenced_json_and_clamps_score() -> None:
    score, rationale = parse_judge_json('```json\n{"score": 120, "rationale": "too high"}\n```')

    assert score == 100
    assert rationale == "too high"


def test_criterion_judge_parser_scores_positive_and_negative_weights() -> None:
    rubric = {
        "sections": [
            {
                "criteria": [
                    {"id": "correct-a", "weight": 10, "requirement": "States A"},
                    {"id": "correct-b", "weight": 5, "requirement": "States B"},
                    {"id": "bad-error", "weight": -5, "requirement": "Contains an error"},
                ]
            }
        ]
    }
    content = json.dumps(
        {
            "criteria": [
                {"id": "correct-a", "met": True, "rationale": "present"},
                {"id": "correct-b", "met": False, "rationale": "missing"},
                {"id": "bad-error", "met": True, "rationale": "error present"},
            ]
        }
    )

    judgments = parse_criterion_judge_json(rubric, content)

    assert judgments == (
        CriterionJudgment(id="correct-a", met=True, weight=10, rationale="present"),
        CriterionJudgment(id="correct-b", met=False, weight=5, rationale="missing"),
        CriterionJudgment(id="bad-error", met=True, weight=-5, rationale="error present"),
    )
    assert criterion_score(rubric, judgments) == pytest.approx(100 * 5 / 15)


def test_criterion_judge_parser_accepts_bare_array() -> None:
    rubric = {
        "sections": [
            {
                "criteria": [
                    {"id": "correct-a", "weight": 10, "requirement": "States A"},
                ]
            }
        ]
    }

    judgments = parse_criterion_judge_json(rubric, '[{"id": "correct-a", "met": true}]')

    assert judgments == (CriterionJudgment(id="correct-a", met=True, weight=10, rationale=""),)
