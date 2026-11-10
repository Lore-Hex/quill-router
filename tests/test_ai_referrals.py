from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.acquisition import ATTRIBUTION_COOKIE_NAME, decode_attribution_cookie
from trusted_router.ai_referrals import ai_referral_platform


@pytest.mark.parametrize(('source', 'expected'), [
    ('ChatGPT.com', 'chatgpt'), ('www.claude.ai', 'claude'),
    ('perplexity.ai', 'perplexity'), ('chat.openai.com', 'chatgpt'),
    ('gemini.google.com', 'gemini'), ('copilot.microsoft.com', 'copilot'),
    ('chatgpt.com.evil.example', None), ('notclaude.ai', None),
    ('google', None), (None, None),
])
def test_ai_source_requires_an_exact_known_signal(source: str | None, expected: str | None) -> None:
    assert ai_referral_platform(source) == expected


def test_ai_return_visit_preserves_acquisition_and_updates_last_touch(client: TestClient) -> None:
    client.get('/openrouter-alternative?utm_source=google&utm_campaign=first')
    client.get('/deepseek-api-privacy', headers={'referer': 'https://claude.ai/chat/private-id'})
    context = decode_attribution_cookie(client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings)
    assert context is not None
    assert context.first_touch['utm_source'] == 'google'
    assert context.last_touch['utm_source'] == 'claude.ai'
    assert context.last_touch['landing_path'] == '/deepseek-api-privacy'
    assert 'private-id' not in str(context)
    # Explicit campaign tags take precedence over a referring host.
    client.get('/best-llm-router?utm_source=partner', headers={'referer': 'https://chatgpt.com/'})
    context = decode_attribution_cookie(client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings)
    assert context is not None
    assert context.last_touch['utm_source'] == 'partner'


@pytest.mark.parametrize('headers', [
    {'user-agent': 'ChatGPT-User/1.0'}, {'user-agent': 'Claude-User/1.0'},
    {'user-agent': 'Perplexity-User/1.0'}, {'sec-gpc': '1'}, {'dnt': '1'},
])
def test_ai_fetches_and_privacy_signals_do_not_create_attribution(client: TestClient, headers: dict[str, str]) -> None:
    client.get('/deepseek-api-privacy', headers={**headers, 'referer': 'https://chatgpt.com/'})
    assert client.cookies.get(ATTRIBUTION_COOKIE_NAME) is None


@pytest.mark.parametrize('path', [
    '/', '/deepseek-api-privacy', '/openrouter-alternative', '/best-llm-router',
    '/docs/agent-setup', '/us-ai-models', '/abliterated-deepseek',
    '/blog/the-ai-models-that-go-silent-on-china',
    '/blog/private-deepseek-api-evaluation-checklist',
    '/blog/choosing-an-llm-api-for-agents',
])
def test_guides_have_reciprocal_discovery_links_and_qualified_offer(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert 'href="/blog/private-deepseek-api-evaluation-checklist"' in response.text
    assert 'href="/blog/choosing-an-llm-api-for-agents"' in response.text
    assert 'up to 1 million free tokens' in response.text
    assert '$0.30 in starter credit' in response.text
    assert 'data-signin-mode="signup"' in response.text


def test_ai_report_filters_sources_without_ad_queries(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import datetime as dt
    import json
    import sys

    from scripts import marketing_funnel_report as report
    from trusted_router.marketing_funnel import FunnelRow

    monkeypatch.setattr(sys, 'argv', ['report', '--ai-only', '--format', 'json'])
    monkeypatch.setattr(report.shutil, 'which', lambda _: '/test/axiom')
    now = dt.datetime.now(dt.UTC)

    def spend(args):
        assert args.google_ads_spend == 'off'
        return None, 'disabled', now - dt.timedelta(days=28), now

    monkeypatch.setattr(report, '_google_ads_spend', spend)
    monkeypatch.setattr(report, 'fetch_axiom_cohort_records', lambda *a, **k: [])
    monkeypatch.setattr(report, '_cloud_acquisition_events', lambda *a, **k: ([], None))
    monkeypatch.setattr(report, 'aggregate_cohort_funnel_rows', lambda *a, **k: [
        FunnelRow(source=source, medium='referral', campaign='(none)', creative='(none)',
                  landing_path='/deepseek-api-privacy', experiment_id='(none)', experiment_cell_id='(none)')
        for source in ['claude.ai', 'google', 'chatgpt.com.evil.example']
    ])
    assert report.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data['citation_counts_available'] is False
    assert data['traffic_scope'] == 'ai_referral_cohorts'
    assert len(data['funnel']) == 1
    assert data['funnel'][0]['ai_referral_platform'] == 'claude'
