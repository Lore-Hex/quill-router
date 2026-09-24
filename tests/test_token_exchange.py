from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from starlette.requests import Request

from trusted_router.config import Settings
from trusted_router.routes import public as public_routes
from trusted_router.services.email import EmailMessage


@pytest.fixture(autouse=True)
def reset_inquiry_rate_limit() -> None:
    public_routes._INQUIRY_HITS.clear()
    public_routes._INQUIRY_GLOBAL_HITS.clear()


@pytest.fixture
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    messages: list[EmailMessage] = []

    class Mail:
        def send(self, message: EmailMessage) -> bool:
            messages.append(message)
            return True

    monkeypatch.setattr(public_routes, "get_email_service", lambda _: Mail())
    return messages


def test_page_is_indexable_with_scoped_assets_and_a_real_gate(client: TestClient) -> None:
    response = client.get("/token-exchange")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select("h1")) == 1
    assert soup.select_one('link[rel="canonical"]')["href"] == "https://trustedrouter.com/token-exchange"
    assert soup.select_one('meta[property="og:image"]')["content"].endswith("/og/token-exchange.png")
    assert soup.select_one("#enterprise-brief-form")["action"] == "/token-exchange/brief"
    assert soup.select_one('input[name="email"]')["type"] == "email"
    assert not soup.select('a[href$=".pdf"]')
    assert "Gateway attestation alone" in response.text
    assert "enterprise@trustedrouter.com" in response.text
    assert "/token-exchange" in client.get("/sitemap-core.xml").text
    for path in ("/resources", "/about"):
        assert 'href="/token-exchange"' in client.get(path).text


def test_page_internal_links_resolve(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/token-exchange").text, "html.parser")
    main = soup.select_one("main")
    links = {a["href"] for a in main.select("a[href]") if a["href"].startswith("/")}
    for link in sorted(links):
        assert client.get(link).status_code == 200, link


def test_page_positioning_and_soc2_observation_status(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/token-exchange").text, "html.parser")
    assert [item.get_text(strip=True) for item in soup.select(".tm-benefits .tm-index")] == [
        "01 / SECURE", "02 / INTELLIGENT", "03 / CHEAPER",
    ]
    assert "Secure. Intelligent. Cheaper." in soup.select_one("#choice-title").get_text(" ", strip=True)
    status = soup.select_one('.tm-procurement a[href="/legal/soc2-readiness"]')
    assert "Type II observation window" in status.get_text()
    assert "SOC 2 readiness" not in soup.select_one(".tm-procurement").get_text()


def test_soc2_observation_status_does_not_claim_an_issued_report(client: TestClient) -> None:
    response = client.get("/legal/soc2-readiness")
    assert response.status_code == 200
    assert "Type II observation window is in progress" in response.text
    assert "No SOC 2 report yet" in response.text
    packet = client.get("/legal/soc2-readiness.json").json()
    assert packet["status"] == "type_2_observation_window_in_progress"
    assert packet["observation_window_status"] == "in_progress"
    assert packet["soc2_type_1_report"] == "not_obtained"
    assert packet["soc2_type_2_report"] == "not_obtained"
    assert packet["target_report"]["current_target"] == "SOC 2 Type II"


def test_enterprise_brief_is_the_approved_september_revision() -> None:
    assert hashlib.sha256(public_routes._ENTERPRISE_BRIEF.read_bytes()).hexdigest() == (
        "1d005ae234da1a68782e333023569b4f23653c78d081e05e4d2c2587ee54bbc7"
    )


def test_brochure_copy_states_the_pdf_page_count(client: TestClient) -> None:
    pdf = public_routes._ENTERPRISE_BRIEF.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", pdf))
    assert pages == 9
    soup = BeautifulSoup(client.get("/token-exchange").text, "html.parser")
    text = soup.select_one("#enterprise-brief").get_text(" ", strip=True)
    assert f"Brochure · {pages} pages" in text
    assert "nine-page Token Exchange brochure" in text


def test_original_brief_delivered_after_lead_acceptance(
    client: TestClient,
    sent_messages: list[EmailMessage],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    def event(_request: Request, name: str) -> None:
        assert len(sent_messages) == 1
        events.append(name)

    monkeypatch.setattr(public_routes, "log_browser_funnel_event", event)
    with caplog.at_level(logging.INFO):
        response = client.post("/token-exchange/brief", json={"email": "ada@example.com"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert "TrustedRouter-Token-Exchange-Brochure.pdf" in response.headers["content-disposition"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.content.startswith(b"%PDF-")
    assert hashlib.sha256(response.content).digest() == hashlib.sha256(public_routes._ENTERPRISE_BRIEF.read_bytes()).digest()
    assert len(sent_messages) == 1
    assert sent_messages[0].to == "enterprise@trustedrouter.com"
    assert sent_messages[0].reply_to == "ada@example.com"
    assert sent_messages[0].mail_class == "enterprise_brief"
    assert events == ["enterprise_brief_delivered"]
    assert "ada@example.com" not in caplog.text


@pytest.mark.parametrize("email", [None, "", "bad", "a@localhost", "a@example.com\r\nBcc: x@example.com", ["a@example.com"], 15, "a" * 321 + "@example.com"])
def test_bad_email_never_sends_or_downloads(client: TestClient, sent_messages: list[EmailMessage], email: object) -> None:
    response = client.post("/token-exchange/brief", json={"email": email})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_email"
    assert not sent_messages
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b"\xff", b"[" * 1500 + b"]" * 1500])
def test_bad_body(client: TestClient, sent_messages: list[EmailMessage], body: bytes) -> None:
    response = client.post("/token-exchange/brief", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert not sent_messages


@pytest.mark.parametrize("headers", [{"Origin": "https://other.example"}, {"Origin": "null"}, {"Origin": "https://["}, {"Sec-Fetch-Site": "cross-site"}])
def test_cross_origin_is_rejected(client: TestClient, sent_messages: list[EmailMessage], headers: dict[str, str]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com"}, headers=headers)
    assert response.status_code == 403
    assert not sent_messages


def test_same_origin_is_accepted(client: TestClient, sent_messages: list[EmailMessage]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com"}, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
    assert len(sent_messages) == 1


def test_international_domain_uses_ses_compatible_ascii(client: TestClient, sent_messages: list[EmailMessage]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@b\u00fccher.de"})
    assert response.status_code == 200
    assert sent_messages[0].reply_to == "ada@xn--bcher-kva.de"


def test_configured_enterprise_inbox_receives_the_lead(client: TestClient, test_settings: Settings, sent_messages: list[EmailMessage]) -> None:
    test_settings.partner_inquiry_email = "sales@example.com"
    assert client.post("/token-exchange/brief", json={"email": "ada@example.com"}).status_code == 200
    assert sent_messages[0].to == "sales@example.com"


def test_bounded_body_and_json_only(client: TestClient, sent_messages: list[EmailMessage]) -> None:
    assert client.post("/token-exchange/brief", json={"email": "a" * 4096}).status_code == 413
    assert client.post("/token-exchange/brief", data={"email": "ada@example.com"}).status_code == 415
    assert not sent_messages


def test_honeypot_and_rate_limit(client: TestClient, sent_messages: list[EmailMessage]) -> None:
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com", "website": "spam"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert not sent_messages
    for _ in range(5):
        assert client.post("/token-exchange/brief", json={"email": "ada@example.com"}).status_code == 200
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com"})
    assert response.status_code == 429
    assert len(sent_messages) == 5


@pytest.mark.parametrize("raises", [False, True])
def test_email_failure_is_retryable_and_redacted(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raises: bool) -> None:
    class Mail:
        def send(self, message: EmailMessage) -> bool:
            if raises:
                raise RuntimeError(f"Provider reflected {message.reply_to}")
            return False

    monkeypatch.setattr(public_routes, "get_email_service", lambda _: Mail())
    events: list[str] = []
    monkeypatch.setattr(public_routes, "log_browser_funnel_event", lambda _, name: events.append(name))
    response = client.post("/token-exchange/brief", json={"email": "ada@example.com"})
    assert response.status_code == 503
    assert response.json()["error"] == "delivery_unavailable"
    assert "ada@example.com" not in response.text + caplog.text
    assert not events


def test_missing_asset_never_captures_lead(client: TestClient, sent_messages: list[EmailMessage], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(public_routes, "_ENTERPRISE_BRIEF", tmp_path / "missing.pdf")
    assert client.post("/token-exchange/brief", json={"email": "ada@example.com"}).status_code == 503
    assert not sent_messages


def test_no_ungated_http_download(client: TestClient) -> None:
    assert client.get("/token-exchange/brief").status_code == 405
    for name in ("TrustedRouter-Token-Exchange-Brochure.pdf", "TrustedRouter-Enterprise-Brief.pdf"):
        assert client.get(f"/static/enterprise/{name}").status_code == 404
        assert client.get(f"/data/enterprise/{name}").status_code == 404
