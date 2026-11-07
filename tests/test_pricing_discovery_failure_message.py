"""Discovery failures name the failed check without ever logging credentials."""
from __future__ import annotations

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import SAFE_MESSAGE_LIMIT, safe_exception_message


@pytest.mark.parametrize("message", [
    "lyceum: invalid token limits",
    "lyceum: missing or nonpositive USD token tariff",
    "vertex Claude: no complete global token prices",
    "boundless: invalid price units or currency",
    "decisionmodelsio: invalid or duplicate model ID",
])
def test_self_authored_diagnoses_survive(message: str) -> None:
    assert safe_exception_message(RuntimeError(message)) == message


@pytest.mark.parametrize(("message", "secret"), [
    ("fetch failed at https://provider.example/v1/models?key=sk-live-abc", "sk-live-abc"),
    ("fetch failed at https://user:hunter22@provider.example/v1/models", "hunter22"),
    ("upstream said Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    ("bad credentials api_key=abcdef123456 for request", "abcdef123456"),
    ("token: 'tok_live_1234567890abcdef'", "tok_live_1234567890abcdef"),
    ("x-goog-api-key=AIzaSyFixtureFixtureFixture01", "AIzaSyFixtureFixtureFixture01"),
    ("key sk-proj-ABCDEFGHIJKLMNOPQRSTUV rejected", "sk-proj-ABCDEFGHIJKLMNOPQRSTUV"),
    ("opaque " + "A1b2C3d4" * 6, "A1b2C3d4" * 6),
    ("signature=deadbeefcafe&expires=1", "deadbeefcafe"),
])
def test_secrets_and_url_queries_are_removed(message: str, secret: str) -> None:
    sanitized = safe_exception_message(RuntimeError(message))
    assert sanitized is not None and secret not in sanitized
    assert "?" not in sanitized


def test_values_of_secret_environment_variables_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYCEUM_API_KEY", "plain-words-key")
    monkeypatch.setenv("SOME_OAUTH_TOKEN", "shortpw")
    monkeypatch.setenv("UNRELATED_SETTING", "invalid")
    message = "lyceum: rejected plain-words-key and shortpw; invalid row"
    assert safe_exception_message(RuntimeError(message)) == (
        "lyceum: rejected [redacted] and [redacted]; invalid row"
    )


def test_network_exceptions_carry_no_message() -> None:
    # Their text can echo a request URL or a provider response that quotes a key.
    request = httpx.Request("GET", "https://provider.example/v1/models?signature=secret")
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("invalid api key secret-token", request=request, response=response)
    assert safe_exception_message(error) is None
    wrapped = RuntimeError("discovery failed: invalid api key secret-token")
    wrapped.__cause__ = error
    assert safe_exception_message(wrapped) is None


def test_message_is_bounded_and_single_line() -> None:
    sanitized = safe_exception_message(RuntimeError("lyceum:\n" + "row problem " * 100))
    assert sanitized is not None and "\n" not in sanitized
    assert len(sanitized) == SAFE_MESSAGE_LIMIT and sanitized.endswith("...")


def test_refresh_failure_detail_adds_the_sanitized_message() -> None:
    detail = refresh.discovery_failure_detail(
        RuntimeError("lyceum: invalid token limits at https://api.example/v1?key=sk-live-abcdef"))
    assert detail == (
        "RuntimeError message='lyceum: invalid token limits at https://api.example/v1'"
    )
    assert refresh.discovery_failure_detail(RuntimeError("")) == "RuntimeError"


def test_fetch_one_reports_the_adapter_check(monkeypatch: pytest.MonkeyPatch) -> None:
    class Adapter:
        @staticmethod
        def fetch() -> None:
            raise RuntimeError("lyceum: no valid priced models (meta-llama/x: lyceum: invalid token limits)")

    monkeypatch.setattr(refresh, "_import_provider", lambda slug: Adapter)
    slug, result, err = refresh._fetch_one("lyceum")
    assert (slug, result) == ("lyceum", None)
    assert err == ("RuntimeError message="
                   "'lyceum: no valid priced models (meta-llama/x: lyceum: invalid token limits)'")
