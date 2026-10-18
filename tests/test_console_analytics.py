"""Console analytics remain coarse, consent-aware, and nonblocking."""
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from trusted_router import acquisition as analytics
from trusted_router.config import Settings


@pytest.mark.parametrize(('ua', 'expected'), [
    ('Mozilla iPhone Mobile Safari', 'mobile'), ('Android Mobile Chrome', 'mobile'),
    ('Mozilla iPad Safari', 'tablet'), ('Android Chrome', 'tablet'),
    ('Windows NT Chrome', 'desktop'), ('Macintosh Safari', 'desktop'),
    ('X11 Linux Firefox', 'desktop'), ('', 'unknown'), ('curl/8', 'unknown'),
])
def test_coarse_device_category(ua, expected):
    assert analytics.device_category(ua) == expected


def test_privacy_and_fail_open(monkeypatch, caplog):
    request = Request({'type': 'http', 'headers': [(b'user-agent', b'iPhone Mobile')],
                       'method': 'GET', 'path': '/console/api-keys'})
    context = SimpleNamespace(anonymous_id='visitor', last_touch={})
    monkeypatch.setattr(analytics, 'request_attribution', lambda _: context)
    settings = Settings(environment='test').model_copy(update={'environment': 'production'})
    with caplog.at_level('INFO', logger=analytics.__name__):
        analytics.track_console(request, settings, 'user-secret', 'console_api_keys_viewed')
    record = next(r for r in caplog.records if r.msg == 'acquisition.console_api_keys_viewed')
    assert record.device_category == 'mobile'
    assert len(record.account_fingerprint) == 64
    assert 'user-secret' not in str(record.__dict__)
    assert 'iPhone' not in str(record.__dict__)
    for headers, environment in [([(b'dnt', b'1')], 'production'),
                                  ([(b'sec-gpc', b'1')], 'production'),
                                  ([(b'user-agent', b'Googlebot')], 'production'),
                                  ([], 'local'), ([], 'test')]:
        caplog.clear()
        request.scope['headers'] = headers
        request = Request(request.scope)
        with caplog.at_level('INFO', logger=analytics.__name__):
            analytics.track_console(request, Settings(environment='test').model_copy(update={'environment': environment}), 'u', 'console_api_keys_viewed')
        assert not caplog.records
    monkeypatch.setattr(analytics, 'request_attribution', lambda _: 1 / 0)
    analytics.track_console(Request({'type': 'http', 'headers': []}), settings, 'u', 'console_api_keys_viewed')


@pytest.mark.parametrize("event", ["console_api_keys_viewed", "social_reward_clicked"])
def test_console_fields_survive_logging_and_worker_projection(monkeypatch, caplog, event):
    import json

    from scripts.axiom_growth.model import project_event
    from trusted_router.main import _ApplicationConsoleFormatter

    request = Request({"type": "http", "method": "GET", "path": "/console/api-keys",
                       "headers": [(b"user-agent", b"iPhone Mobile")]})
    monkeypatch.setattr(analytics, "request_attribution",
                        lambda _: SimpleNamespace(anonymous_id="visitor", last_touch={}))
    settings = Settings(environment="test").model_copy(update={"environment": "production"})
    observation = "84c35821-8c8f-48ea-8aba-19724a965078"
    with caplog.at_level("INFO", logger=analytics.__name__):
        analytics.track_console(request, settings, "private-user", event,
                                placement="api_key_setup", action="github", observation_id=observation)
    record = next(r for r in caplog.records if r.msg == "acquisition." + event)
    payload = json.loads(_ApplicationConsoleFormatter().format(record))
    assert payload["device_category"] == "mobile"
    assert payload["reward_action"] == "github"
    assert payload["reward_placement"] == "api_key_setup"
    assert payload["observation_id"] == observation
    assert "private-user" not in json.dumps(payload)
    assert "iPhone" not in json.dumps(payload)
    projected = project_event({**payload, "_time": "2026-10-17T00:00:00Z"}, source="cloud_logging")
    replay = project_event({**payload, "_time": "2026-10-17T00:01:00Z"}, source="cloud_logging")
    assert projected["event_id"] == replay["event_id"]
    for field in ("device_category", "reward_action", "reward_placement", "observation_id"):
        assert projected[field] == payload[field]
