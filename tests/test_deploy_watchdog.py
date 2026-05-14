from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from typing import Any


def _load_watchdog() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("trusted_router_deploy_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_watchdog_prefers_router_core_slo_region_status(monkeypatch) -> None:
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "slo_classes": {
                "router_core": {
                    "current_by_region": {
                        "us-central1": {"status": "up"},
                        "europe-west4": {"status": "down"},
                    }
                }
            },
            # Fallback shape says the opposite; the SLO shape should win.
            "current": {
                "checks": [
                    {"target_region": "us-central1", "effective_status": "down"},
                    {"target_region": "europe-west4", "effective_status": "up"},
                ]
            },
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        assert timeout == 10
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json",
        ["us-central1", "europe-west4"],
    ) == {"us-central1": "up", "europe-west4": "down"}


def test_watchdog_falls_back_to_current_checks_and_normalizes_degraded(monkeypatch) -> None:
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "current": {
                "checks": [
                    {"target_region": "us-central1", "effective_status": "routing_degraded"},
                    {"target_region": "europe-west4", "effective_status": "up"},
                ]
            }
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json",
        ["us-central1", "europe-west4"],
    ) == {"us-central1": "degraded", "europe-west4": "up"}
