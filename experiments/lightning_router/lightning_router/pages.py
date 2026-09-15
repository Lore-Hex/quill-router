"""Small static documents share one shell; no account data is rendered here."""

from html import escape
from pathlib import Path

from fastapi.responses import HTMLResponse

WEB = Path(__file__).resolve().parent.parent / "web"
PAGES = {"usage": "Usage", "pricing": "Pricing", "docs": "Docs", "terms": "Terms of Service", "privacy": "Privacy Policy"}


def public_page(name: str) -> HTMLResponse:
    title = PAGES[name]
    body = (WEB / f"{name}.html").read_text()
    shell = (WEB / "page.html").read_text()
    return HTMLResponse(shell.replace("{{title}}", escape(title)).replace("{{body}}", body))
