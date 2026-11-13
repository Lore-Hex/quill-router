"""The fast path's money settings (docs/design/fast-admission-widening.md,
(b)): the node's -required-tier, -allowance, -floor, -allowance-tier2 and
-allowance-tier1, passed by the startup script from instance metadata alone,
and the floor held against the runtime catalog's prices."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_fastpath_p1 import _command_flags, _source_at, _unit_flags
from trusted_router import catalog
from trusted_router.pricing import _flat_tier

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "fastpath_node_startup.sh.tftpl"

# The pinned-source check may fetch into the same shallow checkout.
pytestmark = pytest.mark.xdist_group("fastpath-pinned-source")

_MONEY_ATTRIBUTES = ("fastpath-required-tier", "fastpath-allowance", "fastpath-floor")
# The lower tiers' allowances: "0" or absent passes nothing, so they reach
# the unit only once set, and the pinned release must take them only then.
_TIER_ATTRIBUTES = {"fastpath-allowance-tier2": "allowance-tier2", "fastpath-allowance-tier1": "allowance-tier1"}

# WS0's values (tier 3 only), which the deploy runbook's money settings
# section names; the tier-3 allowance and the floor are unchanged since.
WS0_REQUIRED_TIER = 3
ALLOWANCE = 1_000_000_000
FLOOR = 20_000_000
# Today's: the required tier lowered to 1, with the lower tiers' allowances
# (2026-11-12), which the runbook's "Lowering the required tier" names.
REQUIRED_TIER = 1
ALLOWANCE_TIER2 = 250_000_000
ALLOWANCE_TIER1 = 100_000_000
# A hold's output budget that the floor must leave room for four times over.
HOLD_OUTPUT_TOKENS = 16_384


def _go_int(literal: str) -> int:
    literal = literal.replace("_", "").strip()
    shifted = re.fullmatch(r"(\d+)\s*<<\s*(\d+)", literal)
    return int(shifted.group(1)) << int(shifted.group(2)) if shifted else int(literal)


def _bounds() -> tuple[int, int, int]:
    """The release's bounds, read from service.go: the allowance's least
    (the owner's largest top-up), the least floor but none, and the most of
    either."""
    source = (ROOT / "fastpath" / "internal" / "service" / "service.go").read_text(encoding="utf-8")
    top_up = re.search(r"TopUps: owner\.TopUps\{[^}]*?\bMax:\s*([\d_]+)", source, re.S)
    least_floor = re.search(r"^\s*MinFloor int64 = ([\d_]+)$", source, re.M)
    most = re.search(r"^\s*MaxMoney int64 = ([\d_<\s]+)$", source, re.M)
    assert top_up and least_floor and most
    return _go_int(top_up.group(1)), _go_int(least_floor.group(1)), _go_int(most.group(1))


def _run_money_block(**attributes: str) -> subprocess.CompletedProcess[str]:
    """The startup script's money block, run by bash with the instance's
    attributes given."""
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('required_tier="$(attribute fastpath-required-tier)"')
    block = script[start:script.index("# The edge's routing snapshot and shadow comparator", start)]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = (
        'attribute() { local name="A_${1//-/_}"; printf "%s" "${!name:-}"; }\n'
        "systemd-cat() { cat >&2; }\n"
    )
    env = {"PATH": "/usr/bin:/bin", **{"A_" + name.replace("-", "_"): value for name, value in attributes.items()}}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$money_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def _flags(**attributes: str) -> list[str]:
    ran = _run_money_block(**attributes)
    assert ran.returncode == 0, (attributes, ran.stderr)
    return ran.stdout.split()


def _refused(why: str, **attributes: str) -> None:
    ran = _run_money_block(**attributes)
    assert ran.returncode == 1, (attributes, ran.stdout)
    assert why in ran.stderr, (attributes, ran.stderr)


def test_without_the_attributes_the_unit_passes_no_money_flag() -> None:
    """Absent, the node runs the release's defaults, today's values."""
    assert _flags() == []
    assert _flags(**dict.fromkeys(_MONEY_ATTRIBUTES, "")) == []
    assert _flags(**dict.fromkeys(_TIER_ATTRIBUTES, "")) == []


def test_the_tier_allowances_pass_only_when_set_and_not_zero() -> None:
    """A lower tier's allowance at "0", none, passes no flag, as absent does,
    so the release's default, none, holds; set, each follows the others in
    the unit's order."""
    assert _flags(**dict.fromkeys(_TIER_ATTRIBUTES, "0")) == []
    assert _flags(**{"fastpath-required-tier": "3", "fastpath-allowance": str(ALLOWANCE),
                     "fastpath-floor": str(FLOOR), **dict.fromkeys(_TIER_ATTRIBUTES, "0")}) == [
        "-required-tier", "3", "-allowance", "1000000000", "-floor", "20000000"]
    assert _flags(**{"fastpath-allowance": str(ALLOWANCE), "fastpath-allowance-tier2": "100000000",
                     "fastpath-allowance-tier1": "25000000"}) == [
        "-allowance", "1000000000", "-allowance-tier2", "100000000", "-allowance-tier1", "25000000"]
    assert _flags(**{"fastpath-allowance-tier2": "100000000", "fastpath-allowance-tier1": "0"}) == [
        "-allowance-tier2", "100000000"]
    # Absent, the allowance is the release's 1 << 50, which bounds tier 2's.
    assert _flags(**{"fastpath-allowance-tier2": str(1 << 50)}) == ["-allowance-tier2", str(1 << 50)]
    # Equal tiers are in order.
    assert _flags(**{"fastpath-allowance": "10000000", "fastpath-allowance-tier2": "10000000",
                     "fastpath-allowance-tier1": "10000000"}) == [
        "-allowance", "10000000", "-allowance-tier2", "10000000", "-allowance-tier1", "10000000"]


def test_the_script_holds_the_tier_allowances_to_the_releases_bounds_and_order() -> None:
    """A lower tier's allowance the release would refuse, out of the
    allowance's bounds or above the tier above's, stops the script first."""
    least_allowance, _least_floor, most = _bounds()
    for value in (str(least_allowance - 1), str(most + 1), "1000", "00", "0100000000", "1e8", "-100000000",
                  "100000000 -roles auditor", "9" * 17, "$100"):
        _refused("fastpath-allowance-tier2 is 0, 10000000 to the allowance", **{"fastpath-allowance-tier2": value})
        _refused("fastpath-allowance-tier1 is 0, 10000000 to the tier-2 allowance",
                 **{"fastpath-allowance-tier2": str(most), "fastpath-allowance-tier1": value})
    _refused("fastpath-allowance-tier2 is 0, 10000000 to the allowance",
             **{"fastpath-allowance": str(ALLOWANCE), "fastpath-allowance-tier2": str(ALLOWANCE + 1)})
    _refused("fastpath-allowance-tier1 is 0, 10000000 to the tier-2 allowance",
             **{"fastpath-allowance-tier2": "100000000", "fastpath-allowance-tier1": "100000001"})
    # Tier 1 with tier 2 at none, or absent, is above it.
    for tier2 in ("0", ""):
        _refused("fastpath-allowance-tier1 is 0, 10000000 to the tier-2 allowance",
                 **{"fastpath-allowance-tier2": tier2, "fastpath-allowance-tier1": "25000000"})


def test_terraform_sets_the_tier_allowances_with_a_release_that_takes_them() -> None:
    """Terraform lowers the required tier to 1 with both lower tiers'
    allowances set, each at most the tier above's, and pins a release whose
    command takes their flags, so the unit never passes a flag the binary
    refuses."""
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert re.search(rf'^\s*fastpath_required_tier\s*=\s*"{REQUIRED_TIER}"$', tf, re.M)
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert set(_TIER_ATTRIBUTES.values()) <= head, sorted(set(_TIER_ATTRIBUTES.values()) - head)
    script = SCRIPT.read_text(encoding="utf-8")
    pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
    assert pinned
    taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
    for attribute, flag in _TIER_ATTRIBUTES.items():
        local = attribute.replace("-", "_")
        value = re.search(rf'^\s*{local}\s*=\s*"(\d+)"$', tf, re.M)
        assert value, local
        assert re.search(rf"^\s*{attribute}\s*=\s*local\.{local}$", tf, re.M), attribute
        assert f"$(attribute {attribute})" in script, attribute
        assert flag not in _unit_flags(script)
        assert flag in taken, flag
    assert re.search(rf'^\s*fastpath_allowance_tier2\s*=\s*"{ALLOWANCE_TIER2}"$', tf, re.M)
    assert re.search(rf'^\s*fastpath_allowance_tier1\s*=\s*"{ALLOWANCE_TIER1}"$', tf, re.M)
    assert ALLOWANCE_TIER1 <= ALLOWANCE_TIER2 <= ALLOWANCE
    assert _flags(**{
        "fastpath-required-tier": str(REQUIRED_TIER),
        "fastpath-allowance": str(ALLOWANCE),
        "fastpath-floor": str(FLOOR),
        "fastpath-allowance-tier2": str(ALLOWANCE_TIER2),
        "fastpath-allowance-tier1": str(ALLOWANCE_TIER1),
    }) == ["-required-tier", "1", "-allowance", "1000000000", "-floor", "20000000",
           "-allowance-tier2", "250000000", "-allowance-tier1", "100000000"]


def test_the_attributes_render_the_flags_in_the_units_order() -> None:
    assert _flags(**{
        "fastpath-required-tier": str(WS0_REQUIRED_TIER),
        "fastpath-allowance": str(ALLOWANCE),
        "fastpath-floor": str(FLOOR),
    }) == ["-required-tier", "3", "-allowance", "1000000000", "-floor", "20000000"]
    assert _flags(**{"fastpath-required-tier": "3"}) == ["-required-tier", "3"]
    assert _flags(**{"fastpath-allowance": str(ALLOWANCE)}) == ["-allowance", "1000000000"]
    assert _flags(**{"fastpath-floor": "0"}) == ["-floor", "0"]
    assert _flags(**{"fastpath-floor": str(FLOOR)}) == ["-floor", "20000000"]


def test_the_script_holds_each_value_to_the_releases_bounds() -> None:
    """A value the release would refuse, which would stop the whole process
    restarted into the same refusal, stops the script first; each bound,
    read from the release's source, passes at its edge."""
    least_allowance, least_floor, most = _bounds()
    assert (least_allowance, least_floor, most) == (10_000_000, 1_000_000, 1 << 50)
    for tier in ("0", "1", "2", "3"):
        assert _flags(**{"fastpath-required-tier": tier}) == ["-required-tier", tier]
    for tier in ("4", "-1", "three", " 3", "03", "3 -roles auditor"):
        _refused("fastpath-required-tier is 0 to 3", **{"fastpath-required-tier": tier})
    for allowance in (least_allowance, most):
        assert _flags(**{"fastpath-allowance": str(allowance)}) == ["-allowance", str(allowance)]
    for allowance in (str(least_allowance - 1), str(most + 1), "0", "1000", "01000000000", "1e9", "-1000000000",
                      "1000000000 -roles auditor", "9" * 17, "$1000"):
        _refused("fastpath-allowance is 10000000 to 1125899906842624", **{"fastpath-allowance": allowance})
    for floor in ("0", str(least_floor), str(most)):
        assert _flags(**{"fastpath-floor": floor}) == ["-floor", floor]
    for floor in (str(least_floor - 1), str(most + 1), "20", "00", "-1", "020000000", "2e7", "9" * 17):
        _refused("fastpath-floor is 0, 1000000 to 1125899906842624", **{"fastpath-floor": floor})


def test_the_money_flags_wait_for_metadata_and_a_release_that_takes_them() -> None:
    """The money flags reach the unit only from instance metadata, after
    every other flag; HEAD's command takes them, and once Terraform sets any
    of the attributes the pinned release must take them too: a flag it does
    not take stops the node before either role starts."""
    script = SCRIPT.read_text(encoding="utf-8")
    for attribute in _MONEY_ATTRIBUTES:
        assert f"$(attribute {attribute})" in script, attribute
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$",
                          script, re.M)
    assert unit_line and "$money_flags" in unit_line.group(0).split(), unit_line
    flags = {"required-tier", "allowance", "floor"}
    assert not flags & _unit_flags(script)
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert flags <= head, sorted(flags - head)
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    if any(attribute in tf for attribute in _MONEY_ATTRIBUTES):
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert flags <= taken, sorted(flags - taken)


def test_the_runbook_names_the_designs_values_in_order() -> None:
    """The deploy runbook's steps carry the values this file holds the
    floor to, and run in the design's order: the provisioner's bypass,
    ready for each pilot workspace, LeaseReserved, then the re-pin."""
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = runbook[runbook.index("## The money settings"):]
    section = section[:section.index("\n## ")]
    order = [section.index(step) for step in (
        "--tier 3 --identity-bypass --apply",
        f"ready $WS -required-tier {WS0_REQUIRED_TIER} -floor {FLOOR}",
        "LeaseReserved",
        f'fastpath-required-tier = "{WS0_REQUIRED_TIER}"',
    )]
    assert order == sorted(order), order
    assert f'fastpath-allowance = "{ALLOWANCE}"' in section and f'fastpath-floor = "{FLOOR}"' in section
    assert f"-required-tier {WS0_REQUIRED_TIER} -allowance {ALLOWANCE} -floor {FLOOR}" in section
    assert str(ALLOWANCE // 4) in section


def _highest_credit_output_rate() -> tuple[int, str]:
    """The highest output rate, microdollars per million tokens, a credit
    request can be billed at in the runtime catalog: every model's endpoints
    as routing serves them (endpoints_for_model: current entries, retirements
    applied, announced prices in effect), credit endpoints only, the headline
    and every price tier. The catalog's prices are customer prices, the
    standard markup applied at ingest."""
    best = (0, "")
    for model_id in sorted({endpoint.model_id for endpoint in catalog.MODEL_ENDPOINTS.values()}):
        for endpoint in catalog.endpoints_for_model(model_id):
            if endpoint.usage_type != "Credits":
                continue
            rates = [endpoint.completion_price_microdollars_per_million_tokens]
            rates += [tier.completion_price_microdollars_per_million_tokens for tier in endpoint.price_tiers or ()]
            best = max(best, (max(rates), endpoint.id))
    return best


def _hold_passes_a_quarter_of_the_floor(rate: int) -> bool:
    """Whether HOLD_OUTPUT_TOKENS at the rate cost more than a quarter of
    the floor, exactly: tokens x rate / 1e6 > floor / 4."""
    return HOLD_OUTPUT_TOKENS * rate * 4 > FLOOR * 1_000_000


def test_the_floor_is_four_holds_at_the_highest_credit_output_rate() -> None:
    """A hold of 16,384 output tokens at the highest customer output rate on
    any credit endpoint fits in a quarter of the $20 floor. A price that
    passes it fails here, so a rise is reviewed, not absorbed."""
    rate, endpoint = _highest_credit_output_rate()
    assert rate > 0 and endpoint
    assert not _hold_passes_a_quarter_of_the_floor(rate), (
        f"{endpoint} bills {rate} microdollars per million output tokens: {HOLD_OUTPUT_TOKENS} of them cost "
        f"{HOLD_OUTPUT_TOKENS * rate // 1_000_000} microdollars, past a quarter of the floor {FLOOR}"
    )


def test_the_floor_check_counts_a_current_credit_endpoint_and_nothing_retired(monkeypatch: Any) -> None:
    """The control: an endpoint priced past the bound is counted, and fails
    the check, while it is current and on credits; the same price on a
    retired model (openai/o1-pro, retired 2026-10-23) or on a BYOK endpoint
    is not counted, and the highest rate stays what it was."""
    baseline = _highest_credit_output_rate()
    rate, endpoint_id = baseline
    base = catalog.MODEL_ENDPOINTS[endpoint_id]
    priced = 633_000_000
    assert _hold_passes_a_quarter_of_the_floor(priced)

    def endpoint_of(**fields: Any) -> Any:
        return replace(
            base,
            completion_price_microdollars_per_million_tokens=priced,
            published_completion_price_microdollars_per_million_tokens=priced,
            price_tiers=_flat_tier(base.prompt_price_microdollars_per_million_tokens, priced),
            published_price_tiers=_flat_tier(base.prompt_price_microdollars_per_million_tokens, priced),
            catalog_valid_until=None,
            **fields,
        )

    retired = endpoint_of(id="floor-test-retired", model_id="openai/o1-pro", provider="openai",
                          upstream_id="o1-pro", usage_type="Credits")
    byok = endpoint_of(id="floor-test-byok", model_id="floor-test/byok", provider="openai",
                       upstream_id="floor-test-byok", usage_type="BYOK")
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, retired.id, retired)
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, byok.id, byok)
    assert _highest_credit_output_rate() == baseline
    current = endpoint_of(id="floor-test-current", model_id="floor-test/current", provider="openai",
                          upstream_id="floor-test-current", usage_type="Credits")
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, current.id, current)
    assert _highest_credit_output_rate() == (priced, current.id)
    assert _hold_passes_a_quarter_of_the_floor(_highest_credit_output_rate()[0])
    assert rate < priced


def test_the_runbook_lowers_the_tier_with_todays_values() -> None:
    """The runbook's lowering section names the values Terraform sets: the
    readiness check with both lower allowances (without them a lower-tier
    workspace reads as refused), the arguments read back, and the enabler
    lowered only after the nodes."""
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = runbook[runbook.index("## Lowering the required tier"):]
    section = section[:section.index("\n## ")]
    flags = (f"-required-tier {REQUIRED_TIER} -allowance {ALLOWANCE} -floor {FLOOR} "
             f"-allowance-tier2 {ALLOWANCE_TIER2} -allowance-tier1 {ALLOWANCE_TIER1} -fleet-audit")
    ready = (f"ready $WS -required-tier {REQUIRED_TIER} -floor {FLOOR} "
             f"-allowance-tier2 {ALLOWANCE_TIER2} -allowance-tier1 {ALLOWANCE_TIER1}")
    order = [section.index(step) for step in (flags, ready, "TR_FASTPATH_ENABLER_REQUIRED_TIER")]
    assert order == sorted(order), order


def test_the_lower_allowances_reach_the_fleet_on_new_nodes() -> None:
    """A node keeps the script it was created with, so the script that reads
    the lower allowances reaches the fleet only on nodes added under new
    names; 13 and 14 (from before #2542) stay until 15 and 16 serve."""
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert "ignore_changes = [metadata_startup_script]" in tf
    for node in ("tr-fastpath-15", "tr-fastpath-16"):
        assert re.search(rf'^\s*"{node}"\s*=\s*{{ zone = "us-central1-[ab]"', tf, re.M), node
