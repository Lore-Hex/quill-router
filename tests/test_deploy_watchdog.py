from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def _cp(status: str, monitor: str = "us-central1", probe: str = "gateway_authorize", **extra: Any) -> dict:
    """A control-plane check exactly as status.json publishes it (no target_region)."""
    return {"probe_type": probe, "target": "control-plane", "target_region": None,
            "monitor_region": monitor, "status": status, "effective_status": status, **extra}


def _gw(status: str, region: str = "us-central1", probe: str = "tls_health", target: str | None = None) -> dict:
    """An enclave-only check (canonical or regional target)."""
    return {"probe_type": probe, "target": target or region, "target_region": region,
            "monitor_region": "us-central1", "status": status, "effective_status": status}


def _serve(monkeypatch, watchdog: Any, *payloads: dict) -> list[str]:
    queue = list(payloads)
    urls: list[str] = []

    def fake_urlopen(url: str, timeout: int) -> _FakeResponse:
        urls.append(url)
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)
    return urls


def test_control_plane_slo_class_still_prefers_its_published_region_status(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {
        "slo_classes": {"control_plane": {"current_by_region": {
            "us-central1": {"status": "up"}, "europe-west4": {"status": "down"}}}},
        # Fallback shape says the opposite; the SLO shape should win.
        "current": {"checks": [
            {"target_region": "us-central1", "probe_type": "control_plane_health", "effective_status": "down"},
            {"target_region": "europe-west4", "probe_type": "control_plane_health", "effective_status": "up"},
        ]},
    }})
    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json", ["us-central1", "europe-west4"], slo_class="control_plane",
    ) == {"us-central1": "up", "europe-west4": "down"}


def test_router_core_ignores_the_blended_slo_aggregate(monkeypatch) -> None:
    # The published router_core aggregate mixes enclave-only probes in, so it
    # cannot speak for the control plane; only control-plane checks do.
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {
        "slo_classes": {"router_core": {"current_by_region": {
            "us-central1": {"status": "down"}, "europe-west4": {"status": "up"}}}},
        "current": {"checks": [_cp("up", "us-central1"), _cp("down", "europe-west4")]},
    }})
    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json", ["us-central1", "europe-west4"],
    ) == {"us-central1": "up", "europe-west4": "down"}


def test_control_plane_checks_are_attributed_to_their_monitor_region_and_normalized(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {"current": {"checks": [
        _cp("routing_degraded", "us-central1", "provider_fallback"),
        _cp("up", "europe-west4"),
    ]}}})
    assert watchdog.fetch_per_region(
        "https://trustedrouter.com/status.json", ["us-central1", "europe-west4"],
    ) == {"us-central1": "degraded", "europe-west4": "up"}


@pytest.mark.parametrize("regional_status", ["up", "down", "trust_degraded", "degraded", "unknown"])
def test_an_explicit_target_region_attributes_a_control_plane_check(monkeypatch, regional_status) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {
        "slo_classes": {"router_core": {"current_by_region": {
            "global": {"status": "up"}, "us-central1": {"status": "up"}, "europe-west4": {"status": "unknown"}}}},
        "current": {"checks": [
            {"target_region": "us-east4", "probe_type": "gateway_settle", "status": regional_status},
            _cp("up", "us-central1"),
            _cp("up", "europe-west4"),
        ]},
    }})
    assert watchdog.fetch_per_region(
        "https://status.example", iter(["us-central1", "us-east4", "europe-west4", "southamerica-east1"]),
    ) == {
        "us-central1": "up",
        "us-east4": regional_status,
        "europe-west4": "up",
        # No control-plane probe reaches it: inconclusive, never green.
        "southamerica-east1": "unknown",
    }


def test_trust_degraded_is_preserved_not_folded_into_degraded(monkeypatch) -> None:
    """A probe that cannot prove what it talked to is BROKEN, not churn.

    trust_degraded used to normalize to "degraded", and rollback fires
    only on "down", so such a regression could ship and never trigger a
    rollback.
    """
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {"current": {"checks": [
        {"target_region": "eu-west-3", "probe_type": "gateway_settle", "effective_status": "trust_degraded"},
        _cp("up", "us-central1"),
    ]}}})
    assert watchdog.fetch_per_region(
        "https://aws.trustedrouter.com/status.json", ["eu-west-3", "us-central1"],
    ) == {"eu-west-3": "trust_degraded", "us-central1": "up"}


def test_rollback_statuses_cover_down_and_trust_degraded() -> None:
    watchdog = _load_watchdog()
    assert "down" in watchdog.ROLLBACK_STATUSES
    assert "trust_degraded" in watchdog.ROLLBACK_STATUSES
    # Plain degraded stays out: synthetics flap during a rolling update.
    assert "degraded" not in watchdog.ROLLBACK_STATUSES
    assert "up" not in watchdog.ROLLBACK_STATUSES
    assert "unknown" not in watchdog.ROLLBACK_STATUSES


@pytest.mark.parametrize(
    "slo_class,core_probe",
    [
        ("router_core", "gateway_authorize"),
        ("control_plane", "control_plane_health"),
    ],
)
@pytest.mark.parametrize(
    "other_probe",
    [
        "openai_sdk_pong",
        "responses_pong",
        "image_generation",
        "video_generation",
        "provider_benchmark",
        "",
        "future_probe",
    ],
)
@pytest.mark.parametrize("canonical_slo_present", [False, True])
def test_legacy_watchdog_does_not_blend_other_probes_into_release_health(
    monkeypatch,
    slo_class: str,
    core_probe: str,
    other_probe: str,
    canonical_slo_present: bool,
) -> None:
    watchdog = _load_watchdog()
    payload = {
        "data": {
            "current": {
                "checks": [
                    {"target_region": "us-central1", "probe_type": core_probe, "status": "up"},
                    {"target_region": "us-central1", "probe_type": other_probe, "status": "down"},
                    {"target_region": "europe-west4", "probe_type": other_probe, "status": "down"},
                ]
            }
        }
    }
    if canonical_slo_present:
        payload["data"]["slo_classes"] = {slo_class: {"current_by_region": {"global": {"status": "up"}}}}
    monkeypatch.setattr(
        watchdog.urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(json.dumps(payload).encode()),
    )
    assert watchdog.fetch_per_region(
        "https://status.example", ["us-central1", "europe-west4"], slo_class=slo_class
    ) == {
        "us-central1": "up",
        "europe-west4": "unknown",
    }


@pytest.mark.parametrize(
    "probe",
    ["gateway_authorize", "gateway_settle", "gateway_authorize_settle", "provider_fallback"],
)
@pytest.mark.parametrize("attribution", ["target_region", "monitor_region"])
@pytest.mark.parametrize("canonical_slo_present", [False, True])
def test_control_plane_failures_keep_blocking(monkeypatch, probe: str, attribution: str,
                                             canonical_slo_present: bool) -> None:
    watchdog = _load_watchdog()
    check = ({"target_region": "us-central1", "probe_type": probe, "status": "down"}
             if attribution == "target_region" else _cp("down", "us-central1", probe))
    payload: dict[str, Any] = {"data": {"current": {"checks": [check]}}}
    if canonical_slo_present:
        payload["data"]["slo_classes"] = {"router_core": {"current_by_region": {"global": {"status": "up"}}}}
    _serve(monkeypatch, watchdog, payload)
    assert watchdog.fetch_per_region("https://status.example", ["us-central1"]) == {"us-central1": "down"}


@pytest.mark.parametrize("probe", ["tls_health", "attestation_nonce"])
@pytest.mark.parametrize("status", ["down", "trust_degraded"])
@pytest.mark.parametrize("target", ["canonical", "us-central1"])
def test_enclave_only_probes_are_inconclusive_for_the_control_plane(
        monkeypatch, capsys, probe: str, status: str, target: str) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {"current": {"checks": [_gw(status, "us-central1", probe, target)]}}})
    assert watchdog.fetch_per_region("https://status.example", ["us-central1"]) == {"us-central1": "unknown"}
    assert "enclave-only" in capsys.readouterr().out


def test_another_regions_control_plane_is_not_blamed_on_this_one(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {"current": {"checks": [
        _cp("down", "europe-west4"), _cp("up", "us-central1"),
        # No monitor region: the serving control plane is unknown.
        {"probe_type": "gateway_settle", "target": "control-plane", "target_region": None, "status": "down"},
    ]}}})
    assert watchdog.fetch_per_region("https://status.example", ["us-central1"]) == {"us-central1": "up"}


# The live status.json shape on 2026-11-13 (one row per probe/target/monitor).
LIVE_CHECKS = [
    *[_gw("up", region, probe, target)
      for probe in ("tls_health", "attestation_nonce")
      for region, target in (("us-central1", "canonical"), ("us-central1", "us-central1"),
                             ("europe-west4", "europe-west4"), ("us-east4", "us-east4"), ("us-west1", "us-west1"))],
    *[_cp("up", monitor, probe)
      for probe in ("gateway_authorize", "gateway_settle", "provider_fallback")
      for monitor in ("us-central1", "europe-west4")],
    *[{"probe_type": pong, "target": region, "target_region": region, "monitor_region": "us-central1", "status": "up"}
      for pong in ("openai_sdk_pong", "responses_pong")
      for region in ("us-central1", "europe-west4", "us-east4", "us-west1")],
]


def test_live_shape_gives_control_plane_evidence_where_a_monitor_runs(monkeypatch) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, {"data": {
        "slo_classes": {"router_core": {"current_by_region": {"global": {"status": "up"}, "us-central1": {"status": "up"}}}},
        "current": {"checks": LIVE_CHECKS}}})
    assert watchdog.fetch_per_region(
        "https://status.example", ["us-central1", "europe-west4", "us-east4", "southamerica-east1"],
    ) == {"us-central1": "up", "europe-west4": "up", "us-east4": "unknown", "southamerica-east1": "unknown"}


LIVE_FIXTURE = ROOT / "tests" / "fixtures" / "watchdog" / "status-2026-11-13.json"


def _live(**flips: str) -> dict:
    """The real status.json of 2026-11-13, with chosen probe@monitor rows set."""
    payload = json.loads(LIVE_FIXTURE.read_text())
    for check in payload["data"]["current"]["checks"]:
        status = flips.get(f"{check['probe_type']}@{check.get('monitor_region')}")
        if status:
            check["status"] = check["effective_status"] = status
    return payload


def test_live_settle_rows_are_served_by_us_central1_terminals(monkeypatch) -> None:
    watchdog = _load_watchdog()
    payload = _live()
    settle = [c for c in payload["data"]["current"]["checks"] if c["probe_type"] == "gateway_settle"]
    assert {c["monitor_region"] for c in settle} == {"us-central1", "europe-west4"}
    assert all(c["target_url"].endswith("/v1/internal/gateway/settle") for c in settle)
    for check in settle:
        assert watchdog.control_plane_region(check, watchdog.terminal_route_region()) == "us-central1"
    authorize = [c for c in payload["data"]["current"]["checks"] if c["probe_type"] == "gateway_authorize"]
    assert {watchdog.control_plane_region(c, "us-central1") for c in authorize} == {"us-central1", "europe-west4"}


@pytest.mark.parametrize("flips", [
    {"gateway_settle@us-central1": "down", "gateway_settle@europe-west4": "down"},
    {"gateway_settle@europe-west4": "down"},
])
def test_live_shape_us_central1_settle_fault_rolls_back_us_central1_not_europe(
        monkeypatch, tmp_path: Path, flips: dict) -> None:
    # Review case: Europe's monitor settles through the us-central1 terminal
    # backend, so a settle fault there is us-central1's, never europe-west4's.
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, _live(), _live(**flips))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--regions", "us-central1,europe-west4", "--duration-min", "3",
                                      "--rollback-after", "2", "--slo-class", "router_core", "--baseline-grace-sec", "0"])
    assert watchdog.main() == 1
    assert "rollback_regions=us-central1\n" in output.read_text()


def test_live_shape_europe_authorize_fault_rolls_back_europe(monkeypatch, tmp_path: Path) -> None:
    watchdog = _load_watchdog()
    _serve(monkeypatch, watchdog, _live(), _live(**{"gateway_authorize@europe-west4": "down"}))
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--regions", "us-central1,europe-west4", "--duration-min", "3",
                                      "--rollback-after", "2", "--slo-class", "router_core", "--baseline-grace-sec", "0"])
    assert watchdog.main() == 1
    assert "rollback_regions=europe-west4\n" in output.read_text()


def test_terminal_route_is_read_from_terraform_and_matches_the_url_map_and_probes() -> None:
    watchdog = _load_watchdog()
    assert watchdog.terminal_route_region() == "us-central1"
    url_map = (ROOT / "infra" / "control_lb_url_map.tf").read_text()
    block = url_map[url_map.index("for_each = local.terminal_c1_route_enabled ? [1] : []"):]
    block = block[:block.index("service = google_compute_backend_service.control_terminal_c1.id")]
    for path in watchdog.TERMINAL_PATHS:
        assert f'"{path}"' in block and f'"/v1{path}"' in block
    # Authorize is not a terminal path: the probe's /v1 authorize falls to the
    # regional control backend.
    assert watchdog.AUTHORIZE_PATH not in block
    probes = (ROOT / "src" / "trusted_router" / "synthetic" / "probes.py").read_text()
    assert 'f"{base}/v1/internal/gateway/authorize"' in probes
    assert 'f"{base}/v1/internal/gateway/settle"' in probes


@pytest.mark.parametrize("enabled,expected", [("true", "us-central1"), ("false", None)])
def test_terminal_route_flag_controls_settle_attribution(tmp_path: Path, enabled: str, expected) -> None:
    watchdog = _load_watchdog()
    tf = tmp_path / "control_terminal_backend.tf"
    source = (ROOT / "infra" / "control_terminal_backend.tf").read_text()
    tf.write_text(source.replace("terminal_c1_route_enabled = true", f"terminal_c1_route_enabled = {enabled}"))
    terminal = watchdog.terminal_route_region(tf)
    assert terminal == expected
    settle = _cp("down", "europe-west4", "gateway_settle")
    assert watchdog.control_plane_region(settle, terminal) == (expected or "europe-west4")


def test_unreadable_terminal_route_makes_settle_inconclusive(tmp_path: Path, capsys) -> None:
    watchdog = _load_watchdog()
    assert watchdog.terminal_route_region(tmp_path / "missing.tf") == watchdog.UNREADABLE
    checks = [_cp("down", "us-central1", "gateway_settle"), _cp("up", "us-central1")]
    assert watchdog.control_plane_region(checks[0], watchdog.UNREADABLE) is None
    assert watchdog.control_plane_region(checks[1], watchdog.UNREADABLE) == "us-central1"
    assert watchdog.control_plane_per_region(checks, ["us-central1"], watchdog.UNREADABLE) == {"us-central1": "up"}
    assert "settle evidence is inconclusive" in capsys.readouterr().out


@pytest.mark.parametrize("url,expected", [
    ("https://trustedrouter.com/v1/internal/gateway/authorize", "europe-west4"),
    ("https://trustedrouter.com/v1/internal/gateway/settle", "us-central1"),
    (None, None),
])
def test_fallback_is_attributed_by_the_stage_it_reports(url, expected) -> None:
    watchdog = _load_watchdog()
    check = _cp("routing_degraded", "europe-west4", "provider_fallback", target_url=url)
    assert watchdog.control_plane_region(check, "us-central1") == expected


def test_a_sample_crossing_two_regions_is_unattributable_unless_they_coincide() -> None:
    watchdog = _load_watchdog()
    assert watchdog.control_plane_region(_cp("down", "europe-west4", "gateway_authorize_settle"), "us-central1") is None
    assert watchdog.control_plane_region(_cp("down", "us-central1", "gateway_authorize_settle"), "us-central1") == "us-central1"


def test_explicit_target_region_beats_monitor_region() -> None:
    watchdog = _load_watchdog()
    check = _cp("down", "us-central1", "gateway_authorize")
    check["target_region"] = "us-east4"
    assert watchdog.control_plane_per_region([check], ["us-central1", "us-east4"], "us-central1") == {
        "us-central1": "unknown", "us-east4": "down"}


@pytest.mark.parametrize("order", ["unknown_first", "up_first"])
def test_an_unknown_check_never_hides_an_up_one(order: str) -> None:
    watchdog = _load_watchdog()
    checks = [_cp("no_signal", "us-central1"), _cp("up", "us-central1")]
    if order == "up_first":
        checks.reverse()
    assert watchdog.control_plane_per_region(checks, ["us-central1"], "us-central1") == {"us-central1": "up"}


@pytest.mark.parametrize("target", ["canonical", "us-central1", None, "rotation"])
def test_only_control_plane_targets_are_attributed_by_monitor(target) -> None:
    watchdog = _load_watchdog()
    check = _cp("down", "us-central1", "gateway_authorize")
    check["target"] = target
    assert watchdog.control_plane_per_region([check], ["us-central1"], "us-central1") == {"us-central1": "unknown"}


def test_unknown_regions_get_one_warning_annotation_each(capsys) -> None:
    watchdog = _load_watchdog()
    checks = [_cp("up", "us-central1")]
    for _ in range(3):
        assert watchdog.control_plane_per_region(checks, ["us-central1", "us-east4", "southamerica-east1"],
                                                 "us-central1")["us-east4"] == "unknown"
    out = capsys.readouterr().out
    for region in ("us-east4", "southamerica-east1"):
        assert out.count(f"::warning::watchdog: {region}: no control-plane probe") == 1
    assert "::warning::watchdog: us-central1" not in out


def _with(checks: list[dict], **changes: str) -> dict:
    """LIVE_CHECKS with some (probe, attribution region) rows set to a status."""
    rows = []
    for check in checks:
        row = dict(check)
        region = row["target_region"] or row["monitor_region"]
        status = changes.get(f"{row['probe_type']}@{region}")
        if status:
            row["status"] = row["effective_status"] = status
        rows.append(row)
    return {"data": {"current": {"checks": rows}}}


def _run_canary(monkeypatch, tmp_path: Path, watchdog: Any, *payloads: dict,
                extra: tuple[str, ...] = ()) -> tuple[int, str]:
    _serve(monkeypatch, watchdog, *payloads)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    # The primary control-plane canary, as deploy-production.yml runs it.
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--regions", "us-central1", "--duration-min", "3",
                                      "--rollback-after", "2", "--slo-class", "router_core",
                                      "--baseline-grace-sec", "0", *extra])
    code = watchdog.main()
    return code, output.read_text()


def test_simulated_control_plane_regression_now_rolls_the_canary_back(monkeypatch, tmp_path: Path) -> None:
    # Every enclave probe stays green while the canaried control plane fails
    # authorize and settle. The old verdict read only the enclave probes and
    # passed this release.
    watchdog = _load_watchdog()
    healthy = _with(LIVE_CHECKS)
    broken = _with(LIVE_CHECKS, **{"gateway_authorize@us-central1": "down", "gateway_settle@us-central1": "down"})
    code, output = _run_canary(monkeypatch, tmp_path, watchdog, healthy, broken)
    assert code == 1
    assert "rollback_regions=us-central1" in output


def test_simulated_gateway_drain_no_longer_rolls_the_control_plane_back(monkeypatch, tmp_path: Path) -> None:
    # A gateway rollout drains and replaces us-central1's enclaves during the
    # control-plane canary; the control plane itself stays healthy.
    watchdog = _load_watchdog()
    healthy = _with(LIVE_CHECKS)
    drained = _with(LIVE_CHECKS, **{"tls_health@us-central1": "down", "attestation_nonce@us-central1": "down"})
    code, output = _run_canary(monkeypatch, tmp_path, watchdog, healthy, drained)
    assert code == 0
    assert "rollback_regions=\n" in output


def test_gateway_drain_during_the_baseline_does_not_blind_the_canary(monkeypatch, tmp_path: Path) -> None:
    # The baseline used to be captured from the blended verdict: a region that
    # read down because its enclaves were draining was excluded from rollback.
    watchdog = _load_watchdog()
    drained = _with(LIVE_CHECKS, **{"tls_health@us-central1": "down", "attestation_nonce@us-central1": "down"})
    broken = _with(LIVE_CHECKS, **{"tls_health@us-central1": "down", "attestation_nonce@us-central1": "down",
                                   "gateway_authorize@us-central1": "down"})
    code, output = _run_canary(monkeypatch, tmp_path, watchdog, drained, broken)
    assert code == 1
    assert "rollback_regions=us-central1" in output


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


def test_watchdog_baseline_artifact_round_trips_atomically(tmp_path: Path) -> None:
    watchdog = _load_watchdog()
    artifact = tmp_path / "baseline.json"

    watchdog.write_baseline(
        str(artifact),
        {"us-central1": "up", "europe-west4": "down"},
    )

    assert watchdog.read_baseline(str(artifact), ["us-central1", "europe-west4", "us-east4"]) == {
        "us-central1": "up",
        "europe-west4": "down",
        "us-east4": "unknown",
    }
    assert not list(tmp_path.glob("baseline.json.tmp.*"))


def _run_staged_probe(
    tmp_path: Path,
    *,
    console_code: str,
    session_code: str,
    resolved_tag_revision: str = "new-rev",
    remove_failures: int = 0,
    remove_always_fails: bool = False,
    remove_leaves_tag: bool = False,
    traffic_shift_failure_pct: int | None = None,
    watchdog_exit: int = 0,
    service_ingress: str = "all",
    drain_revision: str = "old-rev",
    drain_watchdog_exit: int = 0,
    old_min_instances: str = "2",
    drain_minutes: str = "15",
    existing_drain_revision: str = "",
    interrupt_drain: bool = False,
    drain_remove_fails: bool = False,
    rollback_fails: bool = False,
    region: str = "us-central1",
    mutex_assert_failure_at: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    mutex_assert_count = tmp_path / "mutex-assert-count"
    mutex_assert_count.write_text("0\n")
    tag_state = tmp_path / "tag-state"
    tag_state.write_text("")
    drain_state = tmp_path / "drain-state"
    drain_state.write_text(existing_drain_revision)
    remove_failures_state = tmp_path / "remove-failures"
    remove_failures_state.write_text(f"{remove_failures}\n")
    gcloud = stub_bin / "gcloud"
    gcloud.write_text(
        """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >>"$STAGED_CALL_LOG"
if [ -n "$STAGED_TRAFFIC_SHIFT_FAILURE_PCT" ] \
    && { [[ " $* " == *" --to-revisions=new-rev=${STAGED_TRAFFIC_SHIFT_FAILURE_PCT},"* ]] \
      || [[ " $* " == *" --to-revisions=new-rev=${STAGED_TRAFFIC_SHIFT_FAILURE_PCT} "* ]]; }; then
  exit 1
fi
if [ "$STAGED_ROLLBACK_FAILS" = "1" ] && [[ " $* " == *" --to-revisions=old-rev=100 "* ]]; then
  exit 1
fi
case " $* " in
  *" --update-tags=staged-drain="*)
    printf '%s\\n' "$STAGED_DRAIN_REVISION" >"$STAGED_DRAIN_STATE" ;;
  *" --remove-tags=staged-drain "*)
    if [ "$STAGED_DRAIN_REMOVE_FAILS" = "1" ]; then exit 1; fi
    : >"$STAGED_DRAIN_STATE" ;;
  *" --update-tags="*) printf '%s\\n' "$STAGED_RESOLVED_TAG_REV" >"$STAGED_TAG_STATE" ;;
  *" --remove-tags="*)
    remaining="$(cat "$STAGED_REMOVE_FAILURES_STATE")"
    if [ "$STAGED_REMOVE_ALWAYS_FAILS" = "1" ] || [ "$remaining" -gt 0 ]; then
      if [ "$remaining" -gt 0 ]; then
        printf '%s\\n' "$((remaining - 1))" >"$STAGED_REMOVE_FAILURES_STATE"
      fi
      exit 1
    fi
    if [ "$STAGED_REMOVE_LEAVES_TAG" != "1" ]; then
      : >"$STAGED_TAG_STATE"
    fi
    ;;
  *"status.traffic[?tag="*)
    printf '%s\\n' 'TEST ERROR: unsupported gcloud resource projection' >&2
    exit 64
    ;;
  *" --format=json"*)
    if [[ " $* " == *" run revisions describe "* ]]; then
      printf '{"metadata":{"annotations":{"autoscaling.knative.dev/minScale":"%s"}},"status":{"conditions":[{"type":"Ready","status":"True"}]}}\\n' "$STAGED_OLD_MIN_INSTANCES"
      exit 0
    fi
    tagged_revision="$(cat "$STAGED_TAG_STATE")"
    drain_revision="$(cat "$STAGED_DRAIN_STATE")"
    drain_entry=""
    if [ -n "$drain_revision" ]; then
      drain_entry=",{\\"percent\\":0,\\"revisionName\\":\\"${drain_revision}\\",\\"tag\\":\\"staged-drain\\"}"
    fi
    if [ -n "$tagged_revision" ]; then
      printf '{"metadata":{"annotations":{"run.googleapis.com/ingress":"%s"}},"status":{"traffic":[{"percent":100,"revisionName":"old-rev"},{"percent":0,"revisionName":"%s","tag":"staged-probe"}%s]}}\\n' "$STAGED_SERVICE_INGRESS" "$tagged_revision" "$drain_entry"
    else
      printf '{"metadata":{"annotations":{"run.googleapis.com/ingress":"%s"}},"status":{"traffic":[{"percent":100,"revisionName":"old-rev"}%s]}}\\n' "$STAGED_SERVICE_INGRESS" "$drain_entry"
    fi
    ;;
  *"status.url"*) printf '%s\\n' 'https://trusted-router-hash-uc.a.run.app' ;;
esac
"""
    )
    curl = stub_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
url="${*: -1}"
printf 'curl %s\\n' "$url" >>"$STAGED_CALL_LOG"
case "$url" in
  */console) code="$STAGED_CONSOLE_CODE" ;;
  */auth/session) code="$STAGED_SESSION_CODE" ;;
  *) exit 9 ;;
esac
if [ "$code" = "EMPTY" ]; then exit 7; fi
printf '%s' "$code"
[ "$code" != "000" ]
"""
    )
    sleep = stub_bin / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    python3 = stub_bin / "python3"
    python3.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  exec "$STAGED_REAL_PYTHON" "$@"
fi
printf 'python3 %s\\n' "$*" >>"$STAGED_CALL_LOG"
if [[ "$1" == */cloud_rollout.py ]] && [ "$2" = "assert" ]; then
  # Fence check: optionally lose the lease at the Nth assert (1-based).
  count="$(( $(cat "$STAGED_MUTEX_ASSERT_COUNT") + 1 ))"
  printf '%s\\n' "$count" >"$STAGED_MUTEX_ASSERT_COUNT"
  if [ -n "$STAGED_MUTEX_ASSERT_FAILURE_AT" ] \\
      && [ "$count" -ge "$STAGED_MUTEX_ASSERT_FAILURE_AT" ]; then
    echo "deploy fence lost" >&2
    exit 1
  fi
  exit 0
fi
if [[ " $* " == *" --duration-min 15 "* ]]; then
  if [ "$STAGED_INTERRUPT_DRAIN" = "1" ]; then
    kill -TERM "$PPID"
    exit 143
  fi
  exit "$STAGED_DRAIN_WATCHDOG_EXIT"
fi
previous=""
gate_file=""
for argument in "$@"; do
  if [ "$previous" = "baseline-output" ]; then
    printf '%s\n' '{"us-central1":"up"}' >"$argument"
    previous=""
    continue
  fi
  if [ "$previous" = "start-gate-file" ]; then
    gate_file="$argument"
    previous=""
    continue
  fi
  case "$argument" in
    --baseline-output) previous="baseline-output" ;;
    --start-gate-file) previous="start-gate-file" ;;
    *) previous="" ;;
  esac
done
if [ -n "$gate_file" ]; then
  # Behave like the real watchdog: block until the shell releases the gate
  # AFTER its traffic shift, record the ordering, then return the injected
  # verdict. The harness stubs `sleep` as a no-op, so the bounded wait must
  # use the REAL python interpreter for actual wall-clock waiting.
  if ! "$STAGED_REAL_PYTHON" - "$gate_file" <<'PYWAIT'
import pathlib, sys, time
gate = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(96)
    time.sleep(0.05)
PYWAIT
  then
    echo "python3 watchdog-stub: gate never released" >>"$STAGED_CALL_LOG"
    exit 96
  fi
  echo "watchdog-stub: gate-released" >>"$STAGED_CALL_LOG"
  exit "${STAGED_STUB_WATCHDOG_EXIT:-0}"
fi
exit 0
"""
    )
    python3.chmod(0o755)
    gcloud.chmod(0o755)
    curl.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy" / "staged_traffic.sh"
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:/bin:/usr/bin",
        "PROJECT_ID": "test-project",
        "SERVICE": "trusted-router",
        "STAGED_CALL_LOG": str(call_log),
        "STAGED_CONSOLE_CODE": console_code,
        "STAGED_SESSION_CODE": session_code,
        "STAGED_RESOLVED_TAG_REV": resolved_tag_revision,
        "STAGED_TAG_STATE": str(tag_state),
        "STAGED_DRAIN_STATE": str(drain_state),
        "STAGED_DRAIN_REVISION": drain_revision,
        "STAGED_DRAIN_WATCHDOG_EXIT": str(drain_watchdog_exit),
        "STAGED_OLD_MIN_INSTANCES": old_min_instances,
        "TR_REVISION_DRAIN_MINUTES": drain_minutes,
        "STAGED_INTERRUPT_DRAIN": "1" if interrupt_drain else "0",
        "STAGED_DRAIN_REMOVE_FAILS": "1" if drain_remove_fails else "0",
        "STAGED_ROLLBACK_FAILS": "1" if rollback_fails else "0",
        "STAGED_REMOVE_FAILURES_STATE": str(remove_failures_state),
        "STAGED_REMOVE_ALWAYS_FAILS": "1" if remove_always_fails else "0",
        "STAGED_REMOVE_LEAVES_TAG": "1" if remove_leaves_tag else "0",
        "STAGED_TRAFFIC_SHIFT_FAILURE_PCT": (
            "" if traffic_shift_failure_pct is None else str(traffic_shift_failure_pct)
        ),
        "STAGED_REAL_PYTHON": sys.executable,
        "STAGED_STUB_WATCHDOG_EXIT": str(watchdog_exit),
        "STAGED_SERVICE_INGRESS": service_ingress,
        "TR_LEGACY_PROBE_RETRY_SECONDS": "0",
        "TR_PROBE_TAG_REMOVE_RETRY_SECONDS": "0",
        "TR_STAGED_WATCHDOG_BASELINE_FILE": str(tmp_path / "final-baseline.json"),
        # This helper exercises a traffic ramp nested inside the workflow's
        # already-acquired deployment-mutex scope. Dedicated mutex tests cover
        # direct/manual acquisition against a generation-aware storage stub.
        "TR_DEPLOY_MUTEX_OPERATION": "watchdog-test-operation",
        "TR_DEPLOY_MUTEX_GENERATION": "1",
        "STAGED_MUTEX_ASSERT_COUNT": str(mutex_assert_count),
        "STAGED_MUTEX_ASSERT_FAILURE_AT": (
            "" if mutex_assert_failure_at is None else str(mutex_assert_failure_at)
        ),
        **(extra_env or {}),
    }
    run = subprocess.run(  # noqa: S603 - fixed local script and stubbed PATH
        ["/bin/bash", str(script), region, "new-rev", "old-rev"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    return run, call_log.read_text().splitlines()


def test_secondary_handoff_keeps_the_entire_drain_and_bound_fence(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    run, calls = _run_staged_probe(
        tmp_path, console_code="303", session_code="401", region="europe-west4",
        extra_env={"TR_SECONDARY_READY_FILE": str(ready),
                   "TR_SECONDARY_ABORT_FILE": str(tmp_path / "abort"),
                   "TR_SECONDARY_STARTED_AT": "2026-10-27T00:00:00Z",
                   "TR_BILLING_5XX_GATE_SKIP": "true"},
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert ready.read_text().splitlines() == ["europe-west4", "new-rev", "watchdog-test-operation"]
    drains = [line for line in calls if "--duration-min 15 " in line]
    assert len(drains) == 1
    assert "--rollback-after 1" in drains[0]
    assert "--baseline-input" in drains[0]
    assert calls.index(drains[0]) < next(i for i, line in enumerate(calls) if "--remove-tags=staged-drain" in line)


def test_primary_cannot_use_secondary_handoff(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    run, _ = _run_staged_probe(tmp_path, console_code="303", session_code="401",
                               extra_env={"TR_SECONDARY_READY_FILE": str(ready)})
    assert run.returncode != 0
    assert not ready.exists()
    assert (tmp_path / "drain-state").read_text().strip() == "old-rev"


def test_sibling_failure_refuses_another_traffic_shift(tmp_path: Path) -> None:
    abort = tmp_path / "abort"
    abort.touch()
    run, calls = _run_staged_probe(tmp_path, console_code="303", session_code="401",
                                 extra_env={"TR_SECONDARY_ABORT_FILE": str(abort)})
    assert run.returncode != 0
    assert not any("--to-revisions=new-rev=" in line for line in calls)
    assert any("--to-revisions=old-rev=100" in line for line in calls)


_STAGE_SHIFTS = (
    "--to-revisions=new-rev=10,old-rev=90",
    "--to-revisions=new-rev=50,old-rev=50",
    "--to-revisions=new-rev=100 ",
)


@pytest.mark.parametrize("refused_stage", [0, 1, 2], ids=["10pct", "50pct", "100pct"])
def test_lost_deploy_fence_stops_shift_traffic_before_update_traffic(
    tmp_path: Path, refused_stage: int,
) -> None:
    # shift_traffic runs under `if !`, where bash ignores errexit, so the
    # fence check must fail the shift explicitly. Assert 1 is the script-level
    # check; asserts 2, 3 and 4 open the 10%, 50% and 100% shifts.
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401",
        mutex_assert_failure_at=refused_stage + 2,
    )
    assert run.returncode != 0
    assert "deploy fence lost" in run.stderr
    traffic = [c for c in calls if c.startswith("gcloud run services update-traffic")]
    for shift in _STAGE_SHIFTS[:refused_stage]:
        assert any(shift in c for c in traffic), (shift, traffic)
    for shift in _STAGE_SHIFTS[refused_stage:]:
        assert not any(shift in c for c in traffic), (shift, traffic)
    # Without the lease this run must not move traffic at all, rollback included.
    assert not any("--to-revisions=old-rev=100" in c for c in traffic)
    if refused_stage == 0:
        assert not any("--to-revisions=" in c for c in traffic)


def test_old_revision_stays_tagged_through_post_cutover_drain(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(tmp_path, console_code="302", session_code="401")
    assert run.returncode == 0, run.stderr
    tagged = next(i for i, c in enumerate(calls) if "--update-tags=staged-drain=old-rev" in c)
    switched = next(i for i, c in enumerate(calls) if "--to-revisions=new-rev=100 " in c)
    watched = next(i for i, c in enumerate(calls) if "--duration-min 15 " in c)
    removed = next(i for i, c in enumerate(calls) if "--remove-tags=staged-drain " in c)
    assert tagged < switched < watched < removed
    assert "--baseline-input " in calls[watched]
    assert any("run revisions describe old-rev" in c for c in calls[:switched])
    assert any("run services describe" in c for c in calls[removed + 1 :])


@pytest.mark.parametrize("old_min_instances", ["0", "", "bad"])
def test_retirement_guard_refuses_an_unprotected_old_revision(
    tmp_path: Path, old_min_instances: str,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", old_min_instances=old_min_instances,
    )
    assert run.returncode != 0
    assert not any("--to-revisions=new-rev=100 " in c for c in calls)
    assert any("--to-revisions=old-rev=100 " in c for c in calls)


def test_retirement_guard_fails_closed_when_tag_readback_is_wrong(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", drain_revision="unrelated-rev",
    )
    assert run.returncode != 0
    assert not any("--to-revisions=new-rev=100 " in c for c in calls)
    assert any("--to-revisions=old-rev=100 " in c for c in calls)


def test_drain_health_failure_restores_old_traffic_before_untagging(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", drain_watchdog_exit=3,
    )
    assert run.returncode != 0
    rollback = next(i for i, c in enumerate(calls) if "--to-revisions=old-rev=100 " in c)
    removed = next(i for i, c in enumerate(calls) if "--remove-tags=staged-drain " in c)
    assert rollback < removed


@pytest.mark.parametrize("drain_minutes", ["0", "14", "21", "bad"])
def test_invalid_drain_window_cannot_silently_disable_protection(
    tmp_path: Path, drain_minutes: str,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", drain_minutes=drain_minutes,
    )
    assert run.returncode != 0
    assert not any("update-traffic" in c for c in calls)


def test_interrupted_drain_keeps_old_revision_warm_and_reports_it(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", interrupt_drain=True,
    )
    assert run.returncode != 0
    assert any("--to-revisions=new-rev=100 " in c for c in calls)
    assert not any("--remove-tags=staged-drain " in c for c in calls)
    assert "routing drain interrupted; retaining staged-drain=old-rev" in run.stdout


def test_unrelated_outstanding_drain_is_not_replaced_or_removed(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", existing_drain_revision="other-rev",
    )
    assert run.returncode != 0
    assert not any("--update-tags=staged-drain=" in c for c in calls)
    assert not any("--remove-tags=staged-drain " in c for c in calls)
    assert not any("--to-revisions=new-rev=100 " in c for c in calls)


def test_failed_drain_cleanup_cannot_report_success(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401", drain_remove_fails=True,
    )
    assert run.returncode != 0
    assert sum("--remove-tags=staged-drain " in c for c in calls) == 6


def test_ambiguous_final_shift_and_failed_rollback_keep_old_revision_warm(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path, console_code="302", session_code="401",
        traffic_shift_failure_pct=100, rollback_fails=True,
    )
    assert run.returncode != 0
    assert any("--to-revisions=old-rev=100 " in c for c in calls)
    assert not any("--remove-tags=staged-drain " in c for c in calls)


def test_private_ingress_skips_runapp_probe_and_keeps_watchdog_gate(
    tmp_path: Path,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="404",
        session_code="404",
        service_ingress="internal-and-cloud-load-balancing",
    )

    assert run.returncode == 0, run.stderr
    assert not any(call.startswith("curl ") for call in calls)
    assert "load-balancer watchdog owns HTTP validation" in run.stdout
    traffic_calls = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic") and "--to-revisions=" in call
    ]
    assert any("--to-revisions=new-rev=10,old-rev=90" in call for call in traffic_calls)
    assert any("--to-revisions=new-rev=50,old-rev=50" in call for call in traffic_calls)
    assert any("--to-revisions=new-rev=100" in call for call in traffic_calls)


def test_probe_tag_resolution_uses_json_and_unset_tag_resolves_to_empty(
    tmp_path: Path,
) -> None:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    call_log = tmp_path / "calls.log"
    service_json = tmp_path / "service.json"
    gcloud = stub_bin / "gcloud"
    gcloud.write_text(
        """#!/usr/bin/env bash
printf 'gcloud %s\\n' "$*" >>"$PROBE_CALL_LOG"
case " $* " in
  # Match real gcloud: this unsupported projection succeeds with empty output.
  # The call-log assertion below proves the implementation never takes it.
  *"status.traffic[?tag="*) exit 0 ;;
  *" --format=json"*) cat "$PROBE_SERVICE_JSON" ;;
  *) exit 9 ;;
esac
"""
    )
    gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:/bin:/usr/bin",
        "PROBE_CALL_LOG": str(call_log),
        "PROBE_SERVICE_JSON": str(service_json),
    }
    command = (
        f"source {ROOT / 'scripts/deploy/_cloud_run_revision_probe.sh'}; "
        "cloud_run_probe_tag_revision trusted-router us-central1 test-project "
        "staged-probe"
    )

    service_json.write_text(
        json.dumps(
            {
                "status": {
                    "traffic": [
                        {"percent": 100, "revisionName": "old-rev"},
                        {
                            "percent": 0,
                            "revisionName": "new-rev",
                            "tag": "staged-probe",
                        },
                    ]
                }
            }
        )
    )
    resolved = subprocess.run(  # noqa: S603 - fixed shell with stubbed gcloud
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout == "new-rev\n"
    calls = call_log.read_text().splitlines()
    assert calls == [
        "gcloud run services describe trusted-router --region=us-central1 "
        "--project=test-project --format=json"
    ]

    call_log.write_text("")
    service_json.write_text(
        json.dumps({"status": {"traffic": [{"percent": 100, "revisionName": "old-rev"}]}})
    )
    unresolved = subprocess.run(  # noqa: S603 - fixed shell with stubbed gcloud
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert unresolved.returncode == 0, unresolved.stderr
    assert unresolved.stdout == ""
    assert call_log.read_text().splitlines() == calls


def _resolve_probe_tag(service_document: dict[str, object]) -> subprocess.CompletedProcess[str]:
    command = (
        f"source {ROOT / 'scripts/deploy/_cloud_run_revision_probe.sh'}; "
        "gcloud() { printf '%s\\n' \"$PROBE_SERVICE_JSON\"; }; "
        "cloud_run_probe_tag_revision trusted-router us-central1 test-project "
        "staged-probe"
    )
    return subprocess.run(  # noqa: S603 - fixed shell with in-process gcloud stub
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        env={**os.environ, "PROBE_SERVICE_JSON": json.dumps(service_document)},
        timeout=10,
    )


def test_probe_tag_resolution_rejects_duplicate_tag_entries() -> None:
    resolved = _resolve_probe_tag(
        {
            "status": {
                "traffic": [
                    {"tag": "staged-probe", "revisionName": "first-rev"},
                    {"tag": "staged-probe", "revisionName": "second-rev"},
                ]
            }
        }
    )

    assert resolved.returncode != 0
    assert resolved.stdout == ""
    assert "more than one traffic entry" in resolved.stderr


def test_probe_tag_resolution_rejects_empty_revision_name() -> None:
    resolved = _resolve_probe_tag(
        {"status": {"traffic": [{"tag": "staged-probe", "revisionName": ""}]}}
    )

    assert resolved.returncode != 0
    assert resolved.stdout == ""
    assert "empty revisionName" in resolved.stderr


@pytest.mark.parametrize(
    ("console_code", "session_code", "expected_rc", "rolls_back"),
    (
        ("302", "401", 0, False),
        ("302", "200", 1, True),
        ("200", "401", 1, True),
        ("302", "403", 1, True),
        ("500", "401", 1, True),
        ("EMPTY", "401", 0, False),
        ("000", "401", 0, False),
    ),
)
def test_staged_legacy_probe_uses_regional_origin_and_classifies_results(
    tmp_path: Path,
    console_code: str,
    session_code: str,
    expected_rc: int,
    rolls_back: bool,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code=console_code,
        session_code=session_code,
    )

    assert run.returncode == expected_rc, run.stderr
    curl_calls = [call for call in calls if call.startswith("curl ")]
    assert curl_calls
    assert all(
        call.startswith("curl https://staged-probe---trusted-router-hash-uc.a.run.app/")
        for call in curl_calls
    )
    assert any(
        "--update-tags=staged-probe=new-rev" in call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
    )
    assert any(
        "--format=json" in call for call in calls if call.startswith("gcloud run services describe")
    )
    assert not any("status.traffic[?tag=" in call for call in calls)
    assert not any("status.traffic[?" in call for call in calls)
    assert any("--format=json" in call for call in calls)
    rollback_calls = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
        and "--to-revisions=old-rev=100" in call
    ]
    assert bool(rollback_calls) is rolls_back
    if console_code in {"EMPTY", "000"}:
        assert "inconclusive after bounded retries; continuing without rollback" in run.stdout


def test_staged_probe_reconciles_and_verifies_a_leftover_tag(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        resolved_tag_revision="old-rev",
    )

    assert run.returncode == 0, run.stderr
    assert any("--update-tags=staged-probe=new-rev" in call for call in calls)
    assert any("--format=json" in call for call in calls)
    assert not any("status.traffic[?tag=" in call for call in calls)
    assert any("--remove-tags=staged-probe" in call for call in calls)
    assert not any(call.startswith("curl ") for call in calls)
    assert "probe tag does not resolve to new-rev" in run.stdout
    assert "resolves to old-rev, expected new-rev" in run.stderr


def test_staged_probe_tag_cleanup_retries_a_transient_failure(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_failures=1,
    )

    assert run.returncode == 0, run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 2


def test_staged_probe_tag_cleanup_permanent_failure_is_nonzero(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_always_fails=True,
    )

    assert run.returncode != 0
    assert "probe tag staged-probe may still be addressable" in run.stderr
    assert (
        "gcloud run services update-traffic trusted-router --region=us-central1 "
        "--project=test-project --remove-tags=staged-probe --quiet"
    ) in run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 6


def test_staged_probe_tag_cleanup_verifies_a_successful_removal(
    tmp_path: Path,
) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        remove_leaves_tag=True,
    )

    assert run.returncode != 0
    assert "still resolves to new-rev after removal attempt" in run.stderr
    assert "probe tag staged-probe may still be addressable" in run.stderr
    assert sum("--remove-tags=staged-probe" in call for call in calls) == 6
    removal_index = next(
        index for index, call in enumerate(calls) if "--remove-tags=staged-probe" in call
    )
    assert any("--format=json" in call for call in calls[removal_index + 1 :])


def test_failed_traffic_shift_restores_the_old_desired_revision(tmp_path: Path) -> None:
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        traffic_shift_failure_pct=10,
    )

    assert run.returncode != 0
    traffic_calls = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic") and "--to-revisions=" in call
    ]
    assert any("--to-revisions=new-rev=10,old-rev=90" in call for call in traffic_calls)
    assert any("--to-revisions=old-rev=100" in call for call in traffic_calls)
    assert "ROLLBACK" in run.stdout
    assert "traffic update to 10% failed" in run.stdout


def test_staged_ramp_rolls_back_when_the_watchdog_reports_unhealthy(
    tmp_path: Path,
) -> None:
    """The backgrounded watchdog's verdict must still fail the ramp.

    Review F6: with the watchdog started before the shift (baseline overlap),
    its exit code travels through `wait "$WATCHDOG_PID"` instead of a
    synchronous invocation. If that wait were dropped or its status swallowed,
    every unhealthy deploy would ramp to 100% green. This runs the REAL
    staged_traffic.sh with a watchdog stub that honors the gate and then
    reports unhealthy.
    """
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
        watchdog_exit=3,
    )
    assert run.returncode != 0
    rollbacks = [
        call
        for call in calls
        if call.startswith("gcloud run services update-traffic")
        and "--to-revisions=old-rev=100" in call
    ]
    assert rollbacks, "unhealthy watchdog verdict must roll traffic back"


def test_watchdog_gate_releases_only_after_the_traffic_shift(
    tmp_path: Path,
) -> None:
    """Baseline overlap must not let the watch window start pre-shift.

    The stub records `watchdog-stub: gate-released` the moment the gate file
    appears. That marker must come AFTER the step's traffic shift call in the
    recorded call order — releasing the gate before the shift would let the
    watch window overlap the shift and effectively shorten the observed
    post-shift window.
    """
    run, calls = _run_staged_probe(
        tmp_path,
        console_code="302",
        session_code="401",
    )
    assert run.returncode == 0, run.stderr
    first_shift = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("gcloud run services update-traffic")
        and "--to-revisions=new-rev=10" in call
    )
    first_gate = next(index for index, call in enumerate(calls) if "gate-released" in call)
    assert first_gate > first_shift, "the watchdog gate was released before the first traffic shift"
