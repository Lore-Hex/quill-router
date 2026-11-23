"""The gcp-auth composite action retries transient failures and only those.

.github/actions/gcp-auth/action.yml embeds its Python in a heredoc. The test
pulls that script out of the YAML and executes it under a module name other
than __main__, which defines its functions without running the exchange, then
drives fetch_json with a fake opener: a transient GitHub 503 on the OIDC token
endpoint must not cost a job, and a 4xx denial must fail on the first answer.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/gcp-auth/action.yml"


def _script() -> str:
    action = yaml.safe_load(ACTION.read_text())
    (step,) = [step for step in action["runs"]["steps"] if "python3 - <<'PY'" in step.get("run", "")]
    return step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


@pytest.fixture(scope="module")
def gcp_auth() -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "gcp_auth_under_test"}
    exec(compile(_script(), str(ACTION), "exec"), namespace)  # noqa: S102 - our own action script
    return namespace


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid/token", code, f"status {code}", {}, io.BytesIO(body))  # type: ignore[arg-type]


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self


def _opener(outcomes: list[BaseException | dict[str, Any]]) -> tuple[Callable[..., Any], list[float]]:
    calls: list[float] = []

    def opener(request: urllib.request.Request, timeout: float) -> _Response:
        calls.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(json.dumps(outcome).encode())

    return opener, calls


def _request() -> urllib.request.Request:
    return urllib.request.Request("https://example.invalid/token", headers={"Authorization": "Bearer secret-request-token"})


def test_a_503_then_success_succeeds(gcp_auth: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    opener, calls = _opener([_http_error(503), {"value": "subject-jwt"}])
    sleeps: list[float] = []
    result = gcp_auth["fetch_json"]("GitHub OIDC token request", _request(), opener=opener, sleep=sleeps.append)
    assert result == {"value": "subject-jwt"}
    assert len(calls) == 2 and len(sleeps) == 1
    out = capsys.readouterr().out
    assert "GitHub OIDC token request failed with HTTP 503" in out
    assert "attempt 2/5" in out
    assert "secret-request-token" not in out and "subject-jwt" not in out


@pytest.mark.parametrize("error", [
    _http_error(429),
    _http_error(500),
    _http_error(502),
    urllib.error.URLError(ConnectionResetError("reset")),
    urllib.error.URLError(TimeoutError("connect timed out")),
    TimeoutError("read timed out"),
    ConnectionResetError("reset"),
])
def test_transient_failures_are_retried(gcp_auth: dict[str, Any], error: BaseException) -> None:
    opener, calls = _opener([error, {"access_token": "federated"}])
    result = gcp_auth["fetch_json"]("Google STS token exchange", _request(), opener=opener, sleep=lambda _: None)
    assert result == {"access_token": "federated"}
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_4xx_denial_fails_on_the_first_answer(gcp_auth: dict[str, Any], code: int) -> None:
    opener, calls = _opener([_http_error(code), {"value": "never reached"}])
    sleeps: list[float] = []
    with pytest.raises(urllib.error.HTTPError) as raised:
        gcp_auth["fetch_json"]("Google STS token exchange", _request(), opener=opener, sleep=sleeps.append)
    assert raised.value.code == code
    assert len(calls) == 1 and sleeps == []


def test_five_503s_give_up_with_the_last_error(gcp_auth: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    opener, calls = _opener([_http_error(503) for _ in range(5)])
    sleeps: list[float] = []
    with pytest.raises(urllib.error.HTTPError) as raised:
        gcp_auth["fetch_json"]("GitHub OIDC token request", _request(), opener=opener, sleep=sleeps.append)
    assert raised.value.code == 503
    assert len(calls) == 5 and len(sleeps) == 4
    assert "giving up after 5 attempts" in capsys.readouterr().out


def test_the_backoff_grows_with_jitter_and_stays_about_a_minute(gcp_auth: dict[str, Any]) -> None:
    backoff = gcp_auth["backoff"]
    attempts = gcp_auth["ATTEMPTS"]
    low = [backoff(n, rand=lambda: 0.0) for n in range(1, attempts)]
    high = [backoff(n, rand=lambda: 0.999999) for n in range(1, attempts)]
    assert low == sorted(low) and all(a < b for a, b in zip(low, high, strict=True))
    assert 15 <= sum(low) and sum(high) <= 75


def _run_main(gcp_auth: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
              outcomes: list[BaseException | dict[str, Any]]) -> list[float]:
    opener, calls = _opener(outcomes)
    for name, value in {
        "WIF_PROVIDER": "projects/123/locations/global/workloadIdentityPools/pool/providers/github",
        "WIF_SERVICE_ACCOUNT": "deployer@example-project.iam.gserviceaccount.com",
        "WIF_AUDIENCE": "",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example.invalid/?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "secret-request-token",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(tmp_path / "env"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr(gcp_auth["time"], "sleep", lambda _: None)
    # fetch_json's defaults were bound at definition; rebind them for the run.
    fetch_json = gcp_auth["fetch_json"]
    monkeypatch.setitem(gcp_auth, "fetch_json",
                        lambda label, request: fetch_json(label, request, opener=opener, sleep=lambda _: None))
    gcp_auth["main"]()
    return calls


def test_main_rides_out_an_oidc_503_and_writes_the_credentials(
    gcp_auth: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _run_main(gcp_auth, monkeypatch, tmp_path,
                      [_http_error(503), {"value": "subject-jwt"}, _http_error(503), {"access_token": "federated"}])
    assert len(calls) == 4
    assert "project_id=example-project" in (tmp_path / "output").read_text()
    out = capsys.readouterr().out
    assert "secret-request-token" not in out
    assert "::add-mask::subject-jwt" in out and "::add-mask::federated" in out


def test_main_fails_fast_with_googles_reason_on_an_sts_denial(
    gcp_auth: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    denial = _http_error(400, json.dumps({"error": "invalid_grant", "error_description": "condition"}).encode())
    with pytest.raises(SystemExit):
        _run_main(gcp_auth, monkeypatch, tmp_path, [{"value": "subject-jwt"}, denial])
    out = capsys.readouterr().out
    assert "::error::gcp-auth: Google STS rejected the GitHub OIDC token" in out
    assert "invalid_grant: condition" in out
    assert "retrying" not in out


def test_main_reports_the_oidc_failure_after_five_503s(
    gcp_auth: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _run_main(gcp_auth, monkeypatch, tmp_path, [_http_error(503) for _ in range(5)])
    out = capsys.readouterr().out
    assert "::error::gcp-auth: could not fetch the GitHub OIDC token: HTTP Error 503" in out
