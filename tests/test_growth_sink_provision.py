import json
import re

import pytest

from scripts.axiom_growth import model as growth_model
from scripts.axiom_growth import provision


def test_sink_includes_control_plane_without_broadening_event_allowlist():
    value = provision.source_filter()
    assert set(re.findall(r'resource.labels.service_name="([^"]+)"', value)) == {
        "trusted-router-public", "trusted-router-control", "trusted-router",
    }
    assert set(re.findall(r'jsonPayload.event="([^"]+)"', value)) == (
        growth_model.BROWSER_EVENTS | growth_model.CONVERSION_EVENTS
    )
    assert 'jsonPayload.event="acquisition.nyte.buy_completed"' in value
    assert '=~' not in value


def test_matching_sink_does_not_require_update_permission(monkeypatch, capsys):
    calls = []

    def run(args):
        calls.append(args)
        assert args[3] == "describe"
        return json.dumps({"filter": provision.source_filter()}).encode()

    monkeypatch.setattr(provision, "run", run)
    provision.update_source_filter()
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["unchanged"] == "tr-growth-source"


def test_changed_sink_updates_only_exact_filter(monkeypatch, capsys):
    calls = []

    def run(args):
        calls.append(args)
        return b'{"filter":"old"}' if args[3] == "describe" else b""

    monkeypatch.setattr(provision, "run", run)
    provision.update_source_filter()
    assert calls[1] == ["gcloud", "logging", "sinks", "update", "tr-growth-source",
                        "--project=quill-cloud-proxy", "--log-filter=" + provision.source_filter()]
    assert json.loads(capsys.readouterr().out)["updated"] == "tr-growth-source"


def test_sink_read_failure_cannot_blindly_change_filter(monkeypatch):
    calls = []

    def run(args):
        calls.append(args)
        raise RuntimeError("permission denied")

    monkeypatch.setattr(provision, "run", run)
    with pytest.raises(RuntimeError, match="permission denied"):
        provision.update_source_filter()
    assert len(calls) == 1
