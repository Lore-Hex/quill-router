"""P1's run, held to what the nodes carry for it (the release workflow, the
startup script, the load mix)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_ships_the_load_generator_and_its_mix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "fastpath-release.yml").read_text(encoding="utf-8")
    assert "-o ../release/loadgen ./cmd/loadgen" in workflow
    assert "cp testdata/load-mix.json ../release/load-mix.json" in workflow
    assert "sha256sum fastpath fastpathctl loadgen load-mix.json > SHA256SUMS" in workflow
    assert "release/loadgen release/load-mix.json release/SHA256SUMS" in workflow


def test_node_installs_the_load_generator_when_the_release_carries_it() -> None:
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    assert "for tool in loadgen load-mix.json; do" in script
    # A release from before P1 has neither; the node still runs.
    assert "carries no $tool; the load generator is not installed" in script
    assert script.index("for tool in loadgen") < script.index('echo "$release" > /opt/fastpath/release')


def test_load_mix_is_a_histogram_the_generator_takes() -> None:
    mix = json.loads((ROOT / "fastpath" / "testdata" / "load-mix.json").read_text(encoding="utf-8"))
    assert 0 <= mix["stream_share"] <= 1 and 0 <= mix["refund_share"] <= 1
    for name in ("heartbeats", "estimates", "bill_permill"):
        weights = mix[name]
        assert weights and all(w["value"] >= 0 and w["weight"] > 0 for w in weights), name
        assert abs(sum(w["weight"] for w in weights) - 1) < 0.01, name
    assert all(w["value"] >= 1 for w in mix["estimates"])
    # The generator takes no field but the mix's, so where the numbers come
    # from, and which are assumed until measured, is the runbook's to say.
    assert set(mix) == {"stream_share", "heartbeats", "refund_share", "estimates", "bill_permill"}
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-p1.md").read_text(encoding="utf-8")
    assert "tr_reservation_by_terminal" in runbook and "assumptions" in runbook
