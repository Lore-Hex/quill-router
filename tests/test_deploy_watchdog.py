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


def test_trust_degraded_is_preserved_not_folded_into_degraded(monkeypatch) -> None:
    """An attested gateway that cannot prove what it runs is BROKEN.

    trust_degraded used to normalize to "degraded", and rollback fires
    only on "down" — so an attestation regression could ship and never
    trigger a rollback. That is the one failure this product cannot
    afford to treat as churn.
    """
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "current": {
                "checks": [
                    {"target_region": "eu-west-3", "effective_status": "trust_degraded"},
                    {"target_region": "us-central1", "effective_status": "up"},
                ]
            }
        }
    }

    def fake_urlopen(_url: str, timeout: int) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    assert watchdog.fetch_per_region(
        "https://aws.trustedrouter.com/status.json",
        ["eu-west-3", "us-central1"],
    ) == {"eu-west-3": "trust_degraded", "us-central1": "up"}


def test_rollback_statuses_cover_down_and_trust_degraded() -> None:
    watchdog = _load_watchdog()
    assert "down" in watchdog.ROLLBACK_STATUSES
    assert "trust_degraded" in watchdog.ROLLBACK_STATUSES
    # Plain degraded stays out: synthetics flap during a rolling update.
    assert "degraded" not in watchdog.ROLLBACK_STATUSES
    assert "up" not in watchdog.ROLLBACK_STATUSES
    assert "unknown" not in watchdog.ROLLBACK_STATUSES


def test_normalize_maps_routing_degraded_but_not_trust_degraded() -> None:
    watchdog = _load_watchdog()
    assert watchdog.normalize_watchdog_status("routing_degraded") == "degraded"
    assert watchdog.normalize_watchdog_status("trust_degraded") == "trust_degraded"
    assert watchdog.normalize_watchdog_status("down") == "down"
    assert watchdog.normalize_watchdog_status("anything else") == "unknown"


def test_trust_degraded_ranks_with_down_in_severity() -> None:
    watchdog = _load_watchdog()
    assert watchdog.SEVERITY["trust_degraded"] == watchdog.SEVERITY["down"]
    assert watchdog.SEVERITY["trust_degraded"] > watchdog.SEVERITY["degraded"]


def test_staged_ramp_gates_the_legacy_console_and_auth_surface() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "staged_traffic.sh"
    ).read_text(encoding="utf-8")
    assert '"${LEGACY_SURFACE_BASE_URL}/console"' in script
    assert '"${LEGACY_SURFACE_BASE_URL}/auth/session"' in script
    assert '[ "$console_code" != "302" ]' in script
    assert '[ "$session_code" != "401" ]' in script
    assert "probe_legacy_surface_or_rollback 100" in script
