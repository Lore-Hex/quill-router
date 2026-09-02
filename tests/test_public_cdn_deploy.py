from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rollout_enables_origin_controlled_public_cdn() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")

    assert "--enable-cdn" in rollout
    assert "--cache-mode=USE_ORIGIN_HEADERS" in rollout
    assert "--cache-key-include-host" in rollout
    assert "--cache-key-include-protocol" in rollout
    assert "--cache-key-include-query-string" in rollout
    assert "--serve-while-stale=86400" in rollout
    assert "--no-negative-caching" in rollout


def test_regional_rollout_can_skip_shared_load_balancer_reconciliation() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")

    assert '${TR_DEPLOY_RECONCILE_LB:-1}' in rollout
    assert "skipping shared load-balancer reconciliation" in rollout
    assert 'if [ "${TR_DEPLOY_RECONCILE_LB:-1}" = "1" ]; then' in rollout
