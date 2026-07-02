"""Tests for the public /chat playground page.

Chunk 1 covers:
  * Page renders without any auth (anonymous visitors get the full UI)
  * Right DOM hooks present for the chunk-2 JS to find (data-action
    attributes, the marketing-chrome `data-action="open-signin"`
    button, etc.)
  * Inline `window.__TR_CHAT__` config carries the correct API base URL
    and the issue-key-endpoint path
  * Static assets `chat.js` and `chat.css` are referenced

The Send-button gating itself is a JS-runtime behavior. pytest asserts
that the right hooks and static guards are in place; browser smoke can
verify anonymous user types, clicks Send, sees the local sign-in notice,
and no inference request fires.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app


def test_chat_page_renders_without_auth() -> None:
    """No cookies, no API key — page still returns 200 with the full
    chat shell. This is the user's "no actually sending tokens until
    they've done sign-in" constraint at the page-load layer: the
    visitor must be able to load the page and explore the UI before
    any auth conversation."""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)

    resp = client.get("/chat")
    assert resp.status_code == 200
    body = resp.text

    # The chat shell + main UI elements are present
    assert "data-chat-shell" in body
    assert "data-chat-sidebar" in body
    assert "data-chat-thread" in body
    assert "data-chat-input" in body
    assert "data-chat-send" in body
    assert 'data-action="clear-chat"' in body

    # The marketing chrome's sign-in modal trigger is reachable
    # (this is what the Send-button gate fall back to when signed-out)
    assert 'data-action="open-signin"' in body

    # Inline runtime config that chat.js reads
    assert "window.__TR_CHAT__" in body
    assert "issueKeyPath" in body
    assert "/internal/chat/issue-browser-key" in body
    assert "tr_chat_key" in body  # keyCookieName

    # Static assets are referenced
    assert "/static/model_catalog.js" in body
    assert "/static/chat.js" in body
    assert "/static/chat.css" in body


def test_chat_page_appears_in_nav() -> None:
    """The /chat link belongs in the marketing nav so visitors actually
    find the page. (Without this, /chat exists but is undiscoverable
    via the site's own navigation.)"""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    # The nav lives in templates/public/_base.html which is included
    # everywhere; the home page extends a different template but the
    # marketing pages do extend _base. Pick /security (a known
    # _base.html-extending page) to check the nav.
    page = client.get("/security")
    assert page.status_code == 200
    assert '/chat' in page.text


def test_chat_page_loads_required_external_libraries() -> None:
    """The chat page renders model output with marked + highlight.js,
    sanitizes via DOMPurify. If any of these CDN tags get accidentally
    removed, the chat client falls back to plain-text rendering and
    code blocks lose highlighting / model-emitted <script> tags would
    suddenly become a real XSS vector."""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)

    resp = client.get("/chat")
    assert resp.status_code == 200
    body = resp.text
    assert "marked" in body
    assert "highlight" in body
    assert "dompurify" in body.lower() or "purify" in body.lower()


def test_chat_page_render_does_not_require_models_list() -> None:
    """The chat page is server-rendered chrome only — actual model
    population happens client-side via fetch('/v1/models'). The page
    must NOT block on having the catalog populated, so a deploy with
    an empty catalog still serves a usable page. (Future safeguard
    against accidentally coupling page render to catalog data.)"""
    settings = Settings(environment="local")
    app = create_app(settings, init_observability=False)
    client = TestClient(app)

    resp = client.get("/chat")
    assert resp.status_code == 200
    # The page should NOT enumerate models in the server-rendered
    # HTML (the chunk-2 JS will fetch them on mount). If a future
    # change starts server-rendering the model list, this assertion
    # documents the intentional client-side hydration choice.
    # (Loose check: a server-rendered model list would surface 100+
    # `<option>` or `<li data-model-id>` elements; assert this stays
    # minimal.)
    assert resp.text.count("data-model-id") == 0


def test_chat_js_supports_model_query_param() -> None:
    """Deep links like /chat?model=trustedrouter/openpatcher-s1 should
    open the playground with that model selected in the first slot."""
    js = Path("src/trusted_router/static/chat.js").read_text()

    assert 'new URLSearchParams(window.location.search).get("model")' in js
    assert "applyUrlModelOverride" in js
    assert "rememberRecentModel(URL_MODEL_ID)" in js


def test_chat_js_defaults_to_plato_alias() -> None:
    js = Path("src/trusted_router/static/chat.js").read_text()

    assert (
        'const DEFAULT_MODEL_ID = LOCKED_MODEL_ID || URL_MODEL_ID || "trustedrouter/plato";'
        in js
    )
    assert 'placeholder="trustedrouter/plato"' in js
    assert '|| "anthropic/claude-sonnet-4.6"' not in js


def test_chat_js_signed_out_send_shows_local_notice_without_request() -> None:
    """Anonymous Send should produce a pleasant in-thread notice, not
    silently fail or emit an inference request. The request-builder also
    skips local notices so they cannot leak into later model history."""
    js = Path("src/trusted_router/static/chat.js").read_text()

    assert 'local_notice: "signed_out_send"' in js
    assert "showSignedOutSendNotice(text)" in js
    assert 'Sign in to send this message.' in js
    assert 'data-action="notice-signin"' in js
    assert 'data-action="notice-new-chat"' in js
    assert "if (m.local_notice) continue;" in js
    assert "stateForStorage" in js
    assert "filter((m) => !m.local_notice)" in js


def test_synth_page_renders_raw_thinking_hooks_and_valid_defaults(client: TestClient) -> None:
    resp = client.get("/synth")
    assert resp.status_code == 200
    body = resp.text

    assert "trustedrouter/synth" in body
    assert "Synthesize many models into one perfect frontier answer." in body
    assert "raw thinking when returned" in body
    assert "data-fusion-details" in body
    assert "data-fusion-synthesis-prompt" in body
    assert '<details class="fusion-advanced">' in body
    assert "Advanced settings" in body
    assert "Synthesis instructions" in body
    assert 'data-action="toggle-fusion-detail-layout"' in body
    assert '<option value="budget" selected>Iris 1.0 &mdash; budget</option>' in body
    assert '<option value="frontier">Zeus 1.0 &mdash; frontier</option>' in body
    assert "Panel models" in body
    assert 'data-fusion-model-cards="panel"' in body
    assert body.index("data-fusion-preset") < body.index('<details class="fusion-advanced">')
    assert body.index('data-fusion-model-cards="panel"') > body.index('<details class="fusion-advanced">')
    assert "/static/model_catalog.js" in body
    assert "data-fusion-panel" not in body
    assert "deepseek/deepseek-v4-flash" in body
    assert "openai/gpt-5.5" in body
    assert "anthropic/claude-opus-4.8" in body
    assert "moonshotai/kimi-k2.6" in body
    assert "Judge and fallback judge" in body
    assert "Synthesizer and fallback synthesizer" in body
    assert "defaultJudges: [\"moonshotai/kimi-k2.6\", \"minimax/minimax-m3\"]" in body
    assert 'data-fusion-max-tokens type="number" min="64" max="8192" step="1" value="2048"' in body
