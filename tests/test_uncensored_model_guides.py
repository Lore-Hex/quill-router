from __future__ import annotations

import ast
import json

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


@pytest.mark.parametrize('path', ['/uncensored-ai-models', '/uncensored-deepseek-api'])
def test_uncensored_guides_are_discoverable_and_indexable(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert client.head(path).status_code == 200
    page = BeautifulSoup(response.text, 'html.parser')
    canonical = page.find('link', rel='canonical')
    assert canonical is not None and canonical['href'] == f'https://trustedrouter.com{path}'
    assert len(page.find_all('h1')) == 1
    for script in page.find_all('script', type='application/ld+json'):
        json.loads(script.string or '')
    for source in ['/sitemap-core.xml', '/llms.txt', '/resources', '/abliterated-deepseek']:
        assert path in client.get(source).text
    assert 'up to 1 million free tokens' in page.get_text()
    assert 'token volume depends on model pricing and input/output usage' in page.get_text()
    assert page.select_one('[data-signin-mode="signup"]') is not None


def test_deepseek_python_example_preserves_route_requirements(client: TestClient) -> None:
    page = BeautifulSoup(client.get('/uncensored-deepseek-api').text, 'html.parser')
    snippet = page.select_one('#python pre code')
    assert snippet is not None
    tree = ast.parse(snippet.get_text())
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'create')
    args = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
    assert args['model'] == 'dealignai/deepseek-v4.1-flash-1017-uncensored'
    assert args['extra_body']['provider'] == {
        'only': ['maple'], 'allow_fallbacks': False, 'min_privacy': 'confidential',
    }
