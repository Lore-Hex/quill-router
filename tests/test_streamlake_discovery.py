from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from scripts.pricing.base import ModelPrice, _coerce_to_model_prices, ast_whitelist_check
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.parsers.streamlake import parse
from scripts.pricing.providers import streamlake
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider


def _table(headers: list[str], rows: list[list[str]]) -> str:
    return '<table>' + ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>'
                              for row in [headers, *rows]) + '</table>'


def test_live_table_column_orders_cache_and_tiers() -> None:
    html = _table(
        ['Model', 'Input price', 'Cached Input Price', 'Output price'],
        [['GLM-5.3-Flash', '$0.15', '$0.03', '$0.50'],
         ['DeepSeek-V4.1-Flash', '$0.30', '$0.006', '$1.20']],
    ) + _table(
        ['Model', 'Input price', 'PrefixCache price', 'Output price', 'Cache Read price'],
        [['Qwen3.8-2.4T-A95B', '$2.0', '$0.25', '$6.0', '$0.17']],
    ) + '''<table><tr><th>Model</th><th>Input Length</th><th>Input price</th>
    <th>Output price</th><th>PrefixCache price</th></tr>
    <tr><td rowspan="2">MiniMax-M3</td><td>&le; 512k</td><td>$0.6</td><td>$2.4</td><td>$0.12</td></tr>
    <tr><td>&gt; 512k</td><td>$1.2</td><td>$4.8</td><td>$0.24</td></tr></table>'''
    prices, errors = _coerce_to_model_prices(parse(html))
    assert not errors
    assert prices['z-ai/glm-5.3-flash'].completion_micro_per_m == 500_000
    assert prices['deepseek/deepseek-v4.1-flash'].tiers[0].prompt_cached_micro_per_m == 6_000
    assert prices['qwen/qwen3.8-2.4t-a95b'].tiers[0].prompt_cached_micro_per_m == 250_000
    tiers = prices['minimax/minimax-m3'].tiers
    assert [t.max_prompt_tokens for t in tiers] == [524_288, None]
    assert [t.completion_micro_per_m for t in tiers] == [2_400_000, 4_800_000]


def test_conflicting_and_unlabelled_prices_are_not_guessed() -> None:
    header = ['Model', 'Input price', 'Output price']
    with pytest.raises(ValueError, match='conflicting'):
        parse(_table(header, [['GLM-5.3', '$1', '$2'], ['GLM-5.3', '$3', '$4']]))
    assert parse(_table(['GLM-5.3', '$1', '$2'], [])) == {}
    assert parse(_table(header, [['Qwen3-30B-A3B', '$0.107', 'Thinking $1.071 NonThinking $0.429']])) == {}
    with pytest.raises(ValueError, match='incomplete'):
        parse(_table(['Model', 'Input Length', 'Input price', 'Output price'],
                     [['MiniMax-M3', '\u2264 512k', '$0.6', '$2.4']]))


def test_catalog_discovers_new_families_without_reviving_retired_rows() -> None:
    rows = streamlake.parse_catalog(_table(
        ['Name', 'Category', 'Context Length'],
        [['GLM-5.3-Flash', 'Image-to-Text Reasoning', '1024K'],
         ['DeepSeek-V4.1-Flash', 'Reasoning', '1024K'],
         ['DeepSeek-V3.2-Speciale [Retired]', 'Reasoning', '160K']],
    ))
    assert [row['id'] for row in rows] == ['GLM-5.3-Flash', 'DeepSeek-V4.1-Flash']
    assert rows[0]['input_modalities'] == ['text', 'image']
    assert rows[0]['context_length'] == 1_048_576
    with pytest.raises(ValueError, match='no recognized rows'):
        streamlake.parse_catalog('<html>Maintenance</html>')


@pytest.mark.parametrize('usage,healthy', [
    (None, False), ({}, False),
    ({'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2}, False),
    ({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 4}, False),
    ({'prompt_tokens': 2, 'completion_tokens': 0, 'total_tokens': 2}, False),
    ({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}, True),
])
def test_paid_route_probe_requires_authoritative_usage(monkeypatch: pytest.MonkeyPatch, usage, healthy) -> None:
    monkeypatch.setattr('scripts.pricing.openai_catalog.httpx.post', lambda *a, **kw: httpx.Response(
        200, json={'usage': usage, 'choices': [{'message': {'content': 'PONG'}}]},
    ))
    assert probe_openai_chat(base_url='https://provider.test/v1', api_key='test', model='test',
                             require_usage=True, require_message=True) is healthy


def test_streamlake_parser_remains_sandboxable() -> None:
    source = Path('scripts/pricing/parsers/streamlake.py').read_text()
    assert ast_whitelist_check(source) == []


def test_each_streamlake_route_has_its_own_usage_canary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    checked = []

    def probe(**kwargs):
        assert kwargs['require_usage'] and kwargs['require_message']
        checked.append(kwargs['model'])
        return kwargs['model'] == 'GLM-5.3-Flash'

    monkeypatch.setenv('STREAMLAKE_API_KEY', 'test-key')
    monkeypatch.setattr('scripts.pricing.providers._direct_openai.probe_openai_chat', probe)
    spec = replace(streamlake.CATALOG.spec,
                   catalog_loader=lambda key: [{'id': 'GLM-5.3-Flash'}, {'id': 'DeepSeek-V4.1-Flash'}],
                   price_loader=lambda: {mid: ModelPrice(100_000, 500_000) for mid in streamlake.EXPECTED_MODELS})
    adapter = DirectOpenAIProvider(spec, manifest_path=tmp_path / 'models.json')
    adapter.fetch()
    assert set(checked) == {'GLM-5.3-Flash', 'DeepSeek-V4.1-Flash'}
    assert adapter.discovered_rows['z-ai/glm-5.3-flash']['routable'] is True
    assert adapter.discovered_rows['deepseek/deepseek-v4.1-flash']['routable'] is False
