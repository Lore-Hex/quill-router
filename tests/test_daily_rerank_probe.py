import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import prod_rerank_probe as probe


def response(monkeypatch, body, status=200):
    result = Mock()
    result.status = status
    result.read.return_value = json.dumps(body).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=None)
    transport = Mock(return_value=result)
    monkeypatch.setattr(probe.urllib.request, "urlopen", transport)
    return transport


def valid_body():
    return {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}],
            "usage": {"total_tokens": 20, "cost_microdollars": 2}}


def test_daily_rerank_probe_sends_one_small_prepaid_request(monkeypatch):
    transport = response(monkeypatch, valid_body())
    assert probe.probe("private-key")[0]
    transport.assert_called_once()
    request = transport.call_args.args[0]
    assert request.full_url == "https://api.trustedrouter.com/v1/rerank"
    payload = json.loads(request.data)
    assert payload["model"] == probe.MODEL
    assert len(payload["documents"]) == payload["top_n"] == 2
    assert payload["return_documents"] is False
    assert "provider" not in payload


@pytest.mark.parametrize("case", ["status", "count", "repeat", "order", "ties", "top", "document", "cost", "shape", "score"])
def test_daily_rerank_probe_requires_complete_result(monkeypatch, case):
    body = valid_body()
    status = 200
    if case == "status":
        status = 202
    elif case == "count":
        body["results"].pop()
    elif case == "repeat":
        body["results"].append({"index": 1, "relevance_score": 0.05})
    elif case == "order":
        body["results"].reverse()
    elif case == "ties":
        body["results"] = [{"index": 1, "relevance_score": 1}, {"index": 0, "relevance_score": 1}]
    elif case == "top":
        body["results"] = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
    elif case == "document":
        body["results"][0]["document"] = {"text": "private text"}
    elif case == "cost":
        body["usage"]["cost_microdollars"] = 0
    elif case == "shape":
        body = {}
    elif case == "score":
        body["results"][0]["relevance_score"] = "private text"
    response(monkeypatch, body, status)
    assert not probe.probe("private-key")[0]


def test_daily_rerank_probe_accepts_ties_in_index_order(monkeypatch):
    body = valid_body()
    for row in body["results"]:
        row["relevance_score"] = 0.5
    response(monkeypatch, body)
    assert probe.probe("private-key")[0]


@pytest.mark.parametrize("error", [RuntimeError("private-key query documents"),
    urllib.error.HTTPError(probe.API_BASE, 500, "private-key query documents", {}, io.BytesIO(b"private-key query documents"))])
def test_daily_rerank_probe_keeps_content_out_of_logs(monkeypatch, capsys, error):
    monkeypatch.setenv("TR_MONITOR_API_KEY", "private-key")
    monkeypatch.setattr(probe.urllib.request, "urlopen", Mock(side_effect=error))
    assert probe.main() == 1
    output = capsys.readouterr()
    assert not any(value in output.out + output.err for value in ("private-key", "query", "documents"))


def test_daily_rerank_workflow_reuses_existing_identity_and_secret():
    workflow = Path(".github/workflows/daily-embeddings-probe.yml").read_text()
    assert "python3 scripts/prod_rerank_probe.py" in workflow
    assert workflow.count("--secret=trustedrouter-synthetic-monitor-api-key") == 2
    assert workflow.count("service_account:") == 1
    rerank_step = workflow.split("- name: Probe rerank on GCP", 1)[1]
    assert "if: ${{ !cancelled() }}" in rerank_step.split("run:", 1)[0]
    assert "tr-ci-probe@quill-cloud-proxy.iam.gserviceaccount.com" in workflow
