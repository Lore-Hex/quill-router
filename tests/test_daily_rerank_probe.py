import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from scripts import prod_rerank_probe as probe

KEY = "private-key-value"


def valid_body():
    return {
        "id": "req_1",
        "model": probe.MODEL,
        "provider": probe.PROVIDER,
        "results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}],
        "usage": {"total_tokens": 20, "cost_microdollars": 2, "total_cost_microdollars": 2},
    }


def test_valid_body_passes_every_check():
    assert probe.check_body(valid_body()) is None
    extra = valid_body()
    extra["object"] = "list"
    extra["usage"]["search_units"] = 1
    extra["results"][0]["relevance_score"] = 1
    assert probe.check_body(extra) is None


def _set(path, value):
    def change(body):
        target = body
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return change


def _drop(path):
    def change(body):
        target = body
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]
    return change


CASES = {
    "body-list": (lambda body: None, "response is not an object"),
    "id-missing": (_drop(["id"]), "missing response id"),
    "id-empty": (_set(["id"], ""), "missing response id"),
    "id-number": (_set(["id"], 7), "missing response id"),
    "model-other": (_set(["model"], "voyage/rerank-2.5-lite"), "unexpected model or provider"),
    "model-missing": (_drop(["model"]), "unexpected model or provider"),
    "provider-other": (_set(["provider"], "cohere"), "unexpected model or provider"),
    "provider-missing": (_drop(["provider"]), "unexpected model or provider"),
    "results-missing": (_drop(["results"]), "incorrect result count"),
    "results-one": (_set(["results"], [{"index": 1, "relevance_score": 0.9}]), "incorrect result count"),
    "results-three": (
        _set(["results"], [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1},
                           {"index": 0, "relevance_score": 0.05}]),
        "incorrect result count",
    ),
    "results-object": (
        _set(["results"], {"1": {"index": 1, "relevance_score": 0.9}, "0": {"index": 0, "relevance_score": 0.1}}),
        "incorrect result count",
    ),
    "row-not-object": (_set(["results", 1], [0, 0.1]), "malformed result"),
    "index-bool": (_set(["results", 0, "index"], True), "invalid index"),
    "index-float": (_set(["results", 0, "index"], 1.0), "invalid index"),
    "index-missing": (_drop(["results", 0, "index"]), "invalid index"),
    "index-repeat": (_set(["results", 1, "index"], 1), "incorrect indices"),
    "index-range": (_set(["results", 1, "index"], 2), "incorrect indices"),
    "score-text": (_set(["results", 0, "relevance_score"], "0.9"), "invalid score"),
    "score-bool": (_set(["results", 0, "relevance_score"], True), "invalid score"),
    "score-nan": (_set(["results", 0, "relevance_score"], float("nan")), "invalid score"),
    "score-inf": (_set(["results", 0, "relevance_score"], float("inf")), "invalid score"),
    "score-missing": (_drop(["results", 1, "relevance_score"]), "invalid score"),
    "order-reversed": (
        _set(["results"], [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}]),
        "incorrect ordering",
    ),
    "tie-wrong-order": (
        _set(["results"], [{"index": 1, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.5}]),
        "incorrect ordering",
    ),
    "top-is-the-other-document": (
        _set(["results"], [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]),
        "unexpected top result",
    ),
    "tie-in-index-order": (
        _set(["results"], [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.5}]),
        "unexpected top result",
    ),
    "document-echoed": (_set(["results", 1, "document"], {"text": "x"}), "document returned when not requested"),
    "usage-missing": (_drop(["usage"]), "missing usage"),
    "usage-list": (_set(["usage"], [20, 2, 2]), "missing usage"),
    "tokens-bool": (_set(["usage", "total_tokens"], True), "missing token count"),
    "tokens-missing": (_drop(["usage", "total_tokens"]), "missing token count"),
    "tokens-zero": (_set(["usage", "total_tokens"], 0), "missing token count"),
    "tokens-float": (_set(["usage", "total_tokens"], 20.0), "missing token count"),
    "cost-missing": (_drop(["usage", "cost_microdollars"]), "missing positive cost"),
    "cost-zero": (_set(["usage", "cost_microdollars"], 0), "missing positive cost"),
    "cost-negative": (_set(["usage", "cost_microdollars"], -5), "missing positive cost"),
    "cost-text": (_set(["usage", "cost_microdollars"], "2"), "missing positive cost"),
    "cost-bool": (_set(["usage", "cost_microdollars"], True), "missing positive cost"),
    "cost-both-zero": (
        _set(["usage"], {"total_tokens": 20, "cost_microdollars": 0, "total_cost_microdollars": 0}),
        "missing positive cost",
    ),
    "cost-both-text": (
        _set(["usage"], {"total_tokens": 20, "cost_microdollars": "2", "total_cost_microdollars": "2"}),
        "missing positive cost",
    ),
    "cost-both-true": (
        _set(["usage"], {"total_tokens": 20, "cost_microdollars": True, "total_cost_microdollars": True}),
        "missing positive cost",
    ),
    "total-cost-missing": (_drop(["usage", "total_cost_microdollars"]), "missing positive cost"),
    "total-cost-differs": (_set(["usage", "total_cost_microdollars"], 3), "missing positive cost"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_check_names_its_own_failure(case):
    change, label = CASES[case]
    body = valid_body()
    if case == "body-list":
        body = [valid_body()]
    else:
        before = repr(body)
        change(body)
        assert repr(body) != before
    assert probe.check_body(body) == label


def test_equal_scores_cannot_pass():
    # The matching document is second in the request, so a provider that
    # returns the input order with one score for everything is refused.
    body = valid_body()
    body["results"] = [{"index": 1, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.5}]
    assert probe.check_body(body) is not None
    assert probe.DOCUMENTS[probe.RELEVANT_INDEX] == "Paris is in France."
    assert probe.RELEVANT_INDEX != 0


class _Origin:
    """A loopback stand-in for the API origin; it records what reached it."""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                origin.seen.append({
                    "method": self.command, "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length) if length else b"",
                })
                status, headers, payload = origin.answers.get(self.path, (404, {}, b"{}"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _json(body, status=200):
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def test_one_authenticated_post_with_the_small_fixed_request(monkeypatch, capsys):
    monkeypatch.setenv("TR_MONITOR_API_KEY", KEY)
    with _Origin({"/v1/rerank": _json(valid_body())}) as origin:
        monkeypatch.setattr(probe, "API_BASE", origin.base)
        assert probe.main() == 0
    output = capsys.readouterr()
    assert output.out == f"Rerank PASS: {probe.SUCCESS_DETAIL}\n"
    assert output.err == ""
    assert len(origin.seen) == 1
    sent = origin.seen[0]
    assert (sent["method"], sent["path"]) == ("POST", "/v1/rerank")
    assert sent["authorization"] == f"Bearer {KEY}"
    assert sent["content_type"] == "application/json"
    assert json.loads(sent["body"]) == {
        "model": "voyage/rerank-2.5",
        "query": "Which city is in France?",
        "documents": ["Tokyo is in Japan.", "Paris is in France."],
        "top_n": 2,
        "return_documents": False,
    }


def test_fixed_origin_is_the_gcp_api_host():
    assert probe.API_BASE == "https://api.trustedrouter.com/v1"
    assert probe.TIMEOUT_SECONDS == 30


def test_redirect_is_a_failure_and_the_credential_is_not_sent_on(monkeypatch, capsys):
    monkeypatch.setenv("TR_MONITOR_API_KEY", KEY)
    with _Origin({"/elsewhere": _json(valid_body())}) as other:
        answers = {"/v1/rerank": (302, {"Location": f"{other.base[:-3]}/elsewhere"}, b"")}
        with _Origin(answers) as origin:
            monkeypatch.setattr(probe, "API_BASE", origin.base)
            assert probe.main() == 1
    assert capsys.readouterr().out == "Rerank FAIL: HTTP 302\n"
    assert len(origin.seen) == 1
    assert other.seen == []


@pytest.mark.parametrize(("answer", "line"), [
    (_json(valid_body(), status=202), "Rerank FAIL: unexpected HTTP status\n"),
    (_json({"error": {"message": f"{KEY} Paris"}}, status=500), "Rerank FAIL: HTTP 500\n"),
    (_json({"error": {"message": f"{KEY} Paris"}}, status=401), "Rerank FAIL: HTTP 401\n"),
    ((200, {}, f"{KEY} Paris not json".encode()), "Rerank FAIL: response is not JSON\n"),
    ((200, {}, b" " * (probe.MAX_BODY_BYTES + 1)), "Rerank FAIL: response too large\n"),
    (_json({"id": f"{KEY} Paris"}), "Rerank FAIL: unexpected model or provider\n"),
])
def test_failures_exit_one_with_a_fixed_line(monkeypatch, capsys, answer, line):
    monkeypatch.setenv("TR_MONITOR_API_KEY", KEY)
    with _Origin({"/v1/rerank": answer}) as origin:
        monkeypatch.setattr(probe, "API_BASE", origin.base)
        assert probe.main() == 1
    output = capsys.readouterr()
    assert output.out == line
    assert output.err == ""


def test_unreachable_origin_exits_one_without_details(monkeypatch, capsys):
    monkeypatch.setenv("TR_MONITOR_API_KEY", KEY)
    with _Origin({}) as origin:
        closed = origin.base
    monkeypatch.setattr(probe, "API_BASE", closed)
    assert probe.main() == 1
    output = capsys.readouterr()
    assert output.out == "Rerank FAIL: request failed\n"
    assert output.err == ""


def test_silent_origin_times_out(monkeypatch, capsys):
    monkeypatch.setenv("TR_MONITOR_API_KEY", KEY)
    monkeypatch.setattr(probe, "TIMEOUT_SECONDS", 0.3)
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        monkeypatch.setattr(probe, "API_BASE", f"http://127.0.0.1:{listener.getsockname()[1]}/v1")
        started = time.monotonic()
        assert probe.main() == 1
        assert time.monotonic() - started < 10
    finally:
        listener.close()
    assert capsys.readouterr().out == "Rerank FAIL: request failed\n"


def test_unusable_key_text_stays_out_of_the_output(monkeypatch, capsys):
    # A header value with a line break is refused by the HTTP client with an
    # error that quotes the value; that text must not reach the log.
    monkeypatch.setenv("TR_MONITOR_API_KEY", f"{KEY}\nsecond-line")
    with _Origin({"/v1/rerank": _json(valid_body())}) as origin:
        monkeypatch.setattr(probe, "API_BASE", origin.base)
        assert probe.main() == 1
    output = capsys.readouterr()
    assert output.out == "Rerank FAIL: request failed\n"
    assert output.err == ""
    assert origin.seen == []


def test_missing_key_exits_two_before_any_request(monkeypatch, capsys):
    monkeypatch.delenv("TR_MONITOR_API_KEY", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("no request without a key")

    monkeypatch.setattr(probe, "probe", refuse)
    assert probe.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "FATAL: TR_MONITOR_API_KEY is not set\n"


RERANK_RUN = """set -euo pipefail
TR_MONITOR_API_KEY="$(gcloud secrets versions access latest \\
  --secret=trustedrouter-synthetic-monitor-api-key \\
  --project=quill-cloud-proxy)" \\
  python3 scripts/prod_rerank_probe.py
"""


def test_workflow_step_is_exactly_the_reviewed_one():
    text = Path(".github/workflows/daily-embeddings-probe.yml").read_text()
    workflow = yaml.safe_load(text)
    assert list(workflow["jobs"]) == ["probe"]
    job = workflow["jobs"]["probe"]
    assert "continue-on-error" not in job
    steps = job["steps"]
    step = steps[-1]
    # No key may make the step pass when the probe fails, trace its commands,
    # or skip it after an embeddings failure.
    assert step == {
        "name": "Probe rerank on GCP",
        "if": "${{ !cancelled() }}",
        "timeout-minutes": 3,
        "run": RERANK_RUN,
    }
    assert [s.get("name") for s in steps].count("Probe rerank on GCP") == 1
    assert text.count("--secret=trustedrouter-synthetic-monitor-api-key") == 2
    assert text.count("service_account:") == 1
    assert "service_account: tr-ci-probe@quill-cloud-proxy.iam.gserviceaccount.com" in text
