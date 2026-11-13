"""Schema v3: component-keyed leases in the shared release journal.

Design: docs/design/component-leases-v3.md. Each rule below has a test, and
tests/cloud_rollout_v3_mutations.py removes each rule in turn and requires
the matching test to fail.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy import cloud_rollout as rollout

QR = "https://github.com/Lore-Hex/quill-router/actions/runs/{}"
QCP = "https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/{}"
# The coordinator quill-cloud-proxy runs today: tools/cloud-rollout-source.json
# pins commit 11ae458f0f833be38170a4f7044bee6d89f9c63d with this digest.
PINNED_V2_SHA256 = "0f7b668b1f359b4f49c8f468f01f6e20b4949dc01d432b8295858b985c4cf032"


class MemoryStore:
    def __init__(self):
        self.record = None
        self.generation = 0
        self.lock = threading.Lock()
        self.writes = 0

    def read(self):
        with self.lock:
            return copy.deepcopy(self.record), self.generation

    def write(self, record, generation):
        with self.lock:
            if generation != self.generation:
                raise rollout.Conflict()
            self.record = copy.deepcopy(record)
            self.generation += 1
            self.writes += 1


def make_system(version):
    store = MemoryStore()
    health = {c: "a" * 40 for c in rollout.CLOUDS}
    component_health = {(c, k): True for c in rollout.CLOUDS for k in rollout.COMPONENTS}
    probed = []
    clock = [10_000]

    def probe(cloud):
        probed.append(("cloud", cloud))
        if health[cloud] is None:
            raise rollout.Refused("unhealthy")
        return health[cloud]

    def component_probe(cloud, component):
        probed.append((cloud, component))
        if not component_health[(cloud, component)]:
            raise rollout.Refused(f"{cloud}/{component} unhealthy")

    coordinator = rollout.Coordinator(store, probe, lambda: clock[0], component_probe)
    if version == rollout.V3:
        assert coordinator.upgrade() is True
    return SimpleNamespace(store=store, health=health, component_health=component_health,
                           probed=probed, clock=clock, coordinator=coordinator)


@pytest.fixture
def v3():
    return make_system(rollout.V3)


@pytest.fixture
def v2():
    return make_system(rollout.VERSION)


def hold(system, cloud, component, run=1):
    owner = (QCP if component == "gateway" else QR).format(run)
    return system.coordinator.acquire(cloud, owner, component, 3600)


def enqueue(system, cloud, component, run=1, wait=3600):
    owner = (QCP if component == "gateway" else QR).format(run)
    return system.coordinator.enqueue(cloud, owner, component, wait)


def admit(system, ticket):
    return system.coordinator.try_admit(ticket, 3600)


# Same component, same cloud --------------------------------------------------

@pytest.mark.parametrize("cloud", rollout.CLOUDS)
@pytest.mark.parametrize("component", rollout.COMPONENTS)
def test_v3_same_component_on_one_cloud_stays_exclusive(v3, cloud, component):
    hold(v3, cloud, component)
    with pytest.raises(rollout.Busy, match=f"{cloud}/{component} already reserved"):
        hold(v3, cloud, component, run=2)


# Control plane and gateway on gcp ---------------------------------------------

@pytest.mark.parametrize("first,second", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_gcp_control_plane_and_gateway_hold_leases_at_once(v3, first, second):
    a = hold(v3, "gcp", first)
    b = hold(v3, "gcp", second, run=2)
    assert set(v3.store.record["leases"]) == {f"gcp/{first}", f"gcp/{second}"}
    assert v3.coordinator.check("gcp", a["operation_id"])["component"] == first
    assert v3.coordinator.check("gcp", b["operation_id"])["component"] == second


@pytest.mark.parametrize("held,requested", [
    ("public-surface", "control-plane"), ("public-surface", "gateway"),
    ("control-plane", "public-surface"), ("gateway", "public-surface"),
])
def test_v3_public_surface_is_exclusive_with_both_components(v3, held, requested):
    hold(v3, "gcp", held)
    with pytest.raises(rollout.Busy, match=f"gcp/{held} already reserved"):
        hold(v3, "gcp", requested, run=2)


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("held,requested", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_clouds_without_attributable_gates_stay_one_component_at_a_time(v3, cloud, held, requested):
    assert cloud not in rollout.CONCURRENT_CLOUDS
    hold(v3, cloud, held)
    with pytest.raises(rollout.Busy, match="already reserved"):
        hold(v3, cloud, requested, run=2)


def test_v3_a_blocked_lease_stops_every_other_component_on_its_cloud(v3):
    lease = hold(v3, "gcp", "control-plane")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    with pytest.raises(rollout.Busy, match="gcp/control-plane is blocked"):
        hold(v3, "gcp", "gateway", run=2)
    # Other clouds are unaffected beyond the cap.
    hold(v3, "aws", "gateway", run=3)


def test_v3_blocked_lease_is_kept_and_needs_recovery(v3):
    lease = hold(v3, "gcp", "gateway")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    assert v3.store.record["leases"]["gcp/gateway"]["state"] == "blocked"
    with pytest.raises(rollout.Refused, match="recovery"):
        v3.coordinator.release("gcp", lease["operation_id"], True)
    with pytest.raises(rollout.Refused, match="blocked or expired"):
        v3.coordinator.check("gcp", lease["operation_id"])
    v3.coordinator.recover("gcp", lease["operation_id"], lambda _: True)
    assert v3.store.record["leases"] == {}


# The two-cloud cap counts clouds -----------------------------------------------

def test_v3_cap_counts_distinct_clouds_not_leases(v3):
    hold(v3, "gcp", "control-plane")
    hold(v3, "aws", "control-plane", run=2)
    hold(v3, "gcp", "gateway", run=3)  # gcp already counted
    assert len(v3.store.record["leases"]) == 3
    with pytest.raises(rollout.Busy, match="third is protected"):
        hold(v3, "azure", "control-plane", run=4)
    with pytest.raises(rollout.Busy, match="third is protected"):
        hold(v3, "azure", "gateway", run=5)
    assert v3.store.record["holdback"]["cloud"] == "azure"


def test_v3_holdback_is_a_cloud_with_no_lease_and_is_probed_whole(v3):
    hold(v3, "gcp", "control-plane")
    hold(v3, "gcp", "gateway", run=2)
    holdback = v3.store.record["holdback"]["cloud"]
    assert holdback not in rollout.leased_clouds(v3.store.record)
    assert holdback != "gcp"
    assert ("cloud", holdback) in v3.probed  # both services, not one component


def test_v3_a_leased_cloud_never_becomes_the_holdback(v3):
    hold(v3, "gcp", "control-plane")
    assert v3.store.record["holdback"]["cloud"] == "aws"
    v3.health["aws"] = None  # the only cloud with no lease that could stand in
    with pytest.raises(rollout.Refused, match="no unchanged healthy cloud"):
        hold(v3, "azure", "gateway", run=2)
    assert set(v3.store.record["leases"]) == {"gcp/control-plane"}


def test_v3_second_component_rechecks_the_unchanged_holdback(v3):
    hold(v3, "gcp", "control-plane")
    holdback = v3.store.record["holdback"]["cloud"]
    v3.health[holdback] = "b" * 40  # changed outside any lease
    for cloud in set(rollout.CLOUDS) - {"gcp", holdback}:
        v3.health[cloud] = None  # and no other cloud can stand in
    before = v3.store.generation
    with pytest.raises(rollout.Refused, match="no unchanged healthy cloud"):
        hold(v3, "gcp", "gateway", run=2)
    assert v3.store.generation == before


# Release and recover probe only their own component (E5) ----------------------

@pytest.mark.parametrize("releasing,broken", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_release_probes_only_the_releasing_component(v3, releasing, broken):
    lease = hold(v3, "gcp", releasing)
    other = hold(v3, "gcp", broken, run=2)
    v3.component_health[("gcp", broken)] = False
    v3.health["gcp"] = None  # probe_cloud would refuse
    v3.coordinator.release("gcp", lease["operation_id"], True)
    assert f"gcp/{releasing}" not in v3.store.record["leases"]
    assert f"gcp/{broken}" in v3.store.record["leases"]
    assert ("gcp", releasing) in v3.probed and ("gcp", broken) not in v3.probed
    assert other["operation_id"] == v3.store.record["leases"][f"gcp/{broken}"]["operation_id"]


@pytest.mark.parametrize("component", ["control-plane", "gateway"])
def test_v3_release_refused_while_its_own_component_is_unhealthy(v3, component):
    lease = hold(v3, "gcp", component)
    v3.component_health[("gcp", component)] = False
    with pytest.raises(rollout.Refused, match="unhealthy"):
        v3.coordinator.release("gcp", lease["operation_id"], True)
    assert f"gcp/{component}" in v3.store.record["leases"]


def test_v3_recover_probes_only_the_recovered_component(v3):
    lease = hold(v3, "gcp", "gateway")
    hold(v3, "gcp", "control-plane", run=2)
    v3.coordinator.release("gcp", lease["operation_id"], False)
    v3.component_health[("gcp", "control-plane")] = False
    v3.coordinator.recover("gcp", lease["operation_id"], lambda _: True)
    assert set(v3.store.record["leases"]) == {"gcp/control-plane"}
    with pytest.raises(rollout.Refused, match="still be mutating"):
        other = v3.store.record["leases"]["gcp/control-plane"]
        v3.coordinator.recover("gcp", other["operation_id"], lambda _: False)


PLANE_JSON = {"release": "c" * 40, "api_base_url": "https://api.trustedrouter.com/v1"}


@pytest.mark.parametrize("component,expected", [
    ("gateway", ["https://api.trustedrouter.com/health"]),
    ("control-plane", ["https://trustedrouter.com/trust/control-plane.json",
                       "https://trustedrouter.com/health",
                       "https://trustedrouter.com/trust/control-plane.json"]),
    ("public-surface", ["https://trustedrouter.com/trust/control-plane.json",
                        "https://trustedrouter.com/health",
                        "https://trustedrouter.com/trust/control-plane.json"]),
])
def test_probe_component_reads_only_that_components_service(monkeypatch, component, expected):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PLANE_JSON if url.endswith("control-plane.json") else {"status": "ok"}

    monkeypatch.setattr(rollout, "fetch", fetch)
    rollout.probe_component("gcp", component)
    assert fetched == expected


@pytest.mark.parametrize("component,broken", [
    ("gateway", "https://api.trustedrouter.com/health"),
    ("control-plane", "https://trustedrouter.com/health"),
])
def test_probe_component_refuses_its_own_unhealthy_service(monkeypatch, component, broken):
    def fetch(url):
        if url == broken:
            return {"status": "degraded"}
        return PLANE_JSON if url.endswith("control-plane.json") else {"status": "ok"}

    monkeypatch.setattr(rollout, "fetch", fetch)
    with pytest.raises(rollout.Refused, match=f"gcp/{component} is not verified healthy"):
        rollout.probe_component("gcp", component)


def test_probe_component_requires_stable_release_evidence(monkeypatch):
    answers = iter([PLANE_JSON, {"status": "ok"}, {**PLANE_JSON, "release": "d" * 40}])
    monkeypatch.setattr(rollout, "fetch", lambda url: next(answers))
    with pytest.raises(rollout.Refused):
        rollout.probe_component("gcp", "control-plane")


def test_v2_release_still_probes_the_whole_cloud(v2):
    lease = hold(v2, "gcp", "control-plane")
    v2.health["gcp"] = None
    with pytest.raises(rollout.Refused):
        v2.coordinator.release("gcp", lease["operation_id"], True)
    assert ("gcp", "control-plane") not in v2.probed


def test_v3_holdback_cleared_only_with_the_last_lease(v3):
    a = hold(v3, "gcp", "control-plane")
    b = hold(v3, "gcp", "gateway", run=2)
    v3.coordinator.release("gcp", a["operation_id"], True)
    assert v3.store.record["holdback"] is not None
    v3.coordinator.release("gcp", b["operation_id"], True)
    assert v3.store.record["holdback"] is None


# A lease check depends on its own lease and the holdback ----------------------

class Interleaving(MemoryStore):
    """Run `between` once, after a check's first read and before its second."""

    def __init__(self, inner, between):
        self.inner, self.between, self.reads = inner, between, 0

    def read(self):
        self.reads += 1
        if self.reads == 2 and self.between:
            between, self.between = self.between, None
            between()
        return self.inner.read()

    def write(self, record, generation):
        self.inner.write(record, generation)


def test_v3_check_survives_the_other_component_coming_and_going(v3):
    lease = hold(v3, "gcp", "gateway")
    store = Interleaving(v3.store, lambda: hold(v3, "gcp", "control-plane", run=2))
    checker = rollout.Coordinator(store, v3.coordinator.probe, lambda: v3.clock[0],
                                  v3.coordinator.component_probe)
    assert checker.check("gcp", lease["operation_id"])["component"] == "gateway"
    assert store.reads == 2  # decided on the first pass, not by retrying


def test_v3_check_refuses_when_its_own_lease_changes_meanwhile(v3):
    lease = hold(v3, "gcp", "gateway")

    def block():
        v3.coordinator.release("gcp", lease["operation_id"], False)

    store = Interleaving(v3.store, block)
    checker = rollout.Coordinator(store, v3.coordinator.probe, lambda: v3.clock[0],
                                  v3.coordinator.component_probe)
    with pytest.raises(rollout.Refused, match="blocked or expired"):
        checker.check("gcp", lease["operation_id"])


def test_v3_assert_with_a_fence_from_another_cloud_or_component_is_refused(v3):
    lease = hold(v3, "gcp", "gateway")
    with pytest.raises(rollout.Refused, match="not owned"):
        v3.coordinator.check("aws", lease["operation_id"])
    with pytest.raises(rollout.Refused, match="not owned"):
        v3.coordinator.check("gcp", "f" * 32)


# Lane epochs: what a gateway gate reads ----------------------------------------

def test_v3_every_grant_moves_its_lane_epoch_and_lanes_reports_it(v3):
    lanes = v3.coordinator.lanes("gcp")
    assert lanes["schema_version"] == 3
    assert all(not lane["held"] and lane["epoch"] == 0 for lane in lanes["lanes"].values())
    a = hold(v3, "gcp", "control-plane")
    lanes = v3.coordinator.lanes("gcp")["lanes"]
    assert lanes["control-plane"] == {"held": True, "state": "active", "epoch": 1}
    assert lanes["gateway"] == {"held": False, "state": None, "epoch": 0}
    v3.coordinator.release("gcp", a["operation_id"], True)
    released = v3.coordinator.lanes("gcp")["lanes"]["control-plane"]
    assert released == {"held": False, "state": None, "epoch": 1}  # a lease existed in between
    hold(v3, "gcp", "control-plane", run=2)
    assert v3.coordinator.lanes("gcp")["lanes"]["control-plane"]["epoch"] == 2
    hold(v3, "aws", "gateway", run=3)
    assert v3.coordinator.lanes("gcp")["lanes"]["gateway"]["epoch"] == 0
    assert v3.coordinator.lanes("aws")["lanes"]["gateway"]["epoch"] == 1


def test_v3_lanes_reports_a_blocked_lane(v3):
    lease = hold(v3, "gcp", "control-plane")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    assert v3.coordinator.lanes("gcp")["lanes"]["control-plane"]["state"] == "blocked"


def test_v2_lanes_reports_the_one_lease_without_epochs(v2):
    hold(v2, "gcp", "gateway")
    lanes = v2.coordinator.lanes("gcp")
    assert lanes["schema_version"] == 2
    assert lanes["lanes"]["gateway"] == {"held": True, "state": "active", "epoch": None}
    assert lanes["lanes"]["control-plane"] == {"held": False, "state": None, "epoch": None}


def test_v2_lease_without_a_known_component_holds_every_lane(v2):
    hold(v2, "gcp", "gateway")
    record = copy.deepcopy(v2.store.record)
    del record["leases"]["gcp"]["component"]
    v2.store.record = record
    assert all(lane["held"] for lane in v2.coordinator.lanes("gcp")["lanes"].values())


def test_v2_lease_without_a_component_still_blocks_its_cloud(v2):
    hold(v2, "gcp", "gateway")
    record = copy.deepcopy(v2.store.record)
    del record["leases"]["gcp"]["component"]
    v2.store.record = record
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)


def test_lanes_cli_reads_without_retention_check(v3, monkeypatch, capsys):
    hold(v3, "gcp", "gateway")
    monkeypatch.setattr(rollout, "GCSStore", lambda: v3.store)
    monkeypatch.setattr(sys, "argv", ["cloud_rollout.py", "lanes", "--cloud", "gcp"])
    assert rollout.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lanes"]["gateway"]["held"] is True


# FIFO per lane, plus the cap across clouds --------------------------------------

def test_v3_an_earlier_conflicting_waiter_keeps_its_place(v3):
    lease = hold(v3, "gcp", "gateway")
    earlier = enqueue(v3, "gcp", "gateway", run=2)
    v3.coordinator.release("gcp", lease["operation_id"], True)  # free, not yet admitted
    for component in ("gateway", "public-surface"):
        with pytest.raises(rollout.Busy, match="earlier waiter"):
            hold(v3, "gcp", component, run=3)
    assert [e["ticket"] for e in v3.store.record["queue"]] == [earlier]
    assert admit(v3, earlier)["operation_id"] == earlier


def test_v3_an_earlier_waiter_for_the_other_lane_does_not_block(v3):
    hold(v3, "gcp", "gateway")
    earlier = enqueue(v3, "gcp", "gateway", run=2)  # waits for the gateway lane
    lease = hold(v3, "gcp", "control-plane", run=3)
    assert lease["component"] == "control-plane"
    with pytest.raises(rollout.Busy):
        admit(v3, earlier)


def test_v3_queued_waiters_in_both_lanes_are_admitted_together(v3):
    gw = hold(v3, "gcp", "gateway")
    cp = hold(v3, "gcp", "control-plane", run=2)
    next_gw = enqueue(v3, "gcp", "gateway", run=3)
    next_cp = enqueue(v3, "gcp", "control-plane", run=4)
    v3.coordinator.release("gcp", cp["operation_id"], True)
    with pytest.raises(rollout.Busy, match="gcp/gateway already reserved"):
        admit(v3, next_gw)
    assert admit(v3, next_cp)["operation_id"] == next_cp  # not stuck behind the gateway waiter
    v3.coordinator.release("gcp", gw["operation_id"], True)
    assert admit(v3, next_gw)["operation_id"] == next_gw


def test_v3_public_surface_waiter_is_not_starved_by_later_lanes(v3):
    gw = hold(v3, "gcp", "gateway")
    surface = enqueue(v3, "gcp", "public-surface", run=2)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        hold(v3, "gcp", "control-plane", run=3)
    v3.coordinator.release("gcp", gw["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        hold(v3, "gcp", "gateway", run=4)
    assert admit(v3, surface)["component"] == "public-surface"


def test_v3_cap_fifo_earlier_waiters_for_other_new_clouds_take_slots_first(v3):
    hold(v3, "gcp", "control-plane")
    enqueue(v3, "aws", "control-plane", run=2)
    with pytest.raises(rollout.Busy, match="take the free slot"):
        hold(v3, "azure", "gateway", run=3)
    # Joining a cloud that already holds a lease needs no new slot.
    assert hold(v3, "gcp", "gateway", run=4)["cloud"] == "gcp"


def test_v3_cap_fifo_a_same_cloud_waiter_shares_the_slot(v3):
    hold(v3, "aws", "control-plane")
    blocker = hold(v3, "gcp", "public-surface", run=2)
    gw = enqueue(v3, "gcp", "gateway", run=3)  # waits for public-surface
    v3.coordinator.release("gcp", blocker["operation_id"], True)
    # gcp is free again; the earlier gcp/gateway waiter does not take a second slot.
    assert hold(v3, "gcp", "control-plane", run=4)["component"] == "control-plane"
    assert admit(v3, gw)["component"] == "gateway"


# Format and cut-over -----------------------------------------------------------

def test_a_fresh_journal_starts_as_v2(v2):
    hold(v2, "gcp", "gateway")
    assert v2.store.record["schema_version"] == 2
    assert "epochs" not in v2.store.record


def test_v2_journal_keeps_one_lease_per_cloud_under_the_new_code(v2):
    hold(v2, "gcp", "gateway")
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)
    assert set(v2.store.record["leases"]) == {"gcp"}


@pytest.mark.parametrize("busy", ["lease", "waiter"])
def test_upgrade_needs_an_empty_journal(v2, busy):
    if busy == "lease":
        hold(v2, "gcp", "gateway")
    else:
        hold(v2, "gcp", "gateway")
        enqueue(v2, "gcp", "control-plane", run=2)
        v2.coordinator.release("gcp", v2.store.record["leases"]["gcp"]["operation_id"], True)
    with pytest.raises(rollout.Busy, match="empty journal"):
        v2.coordinator.upgrade()
    assert v2.store.record["schema_version"] == 2


def test_upgrade_and_downgrade_are_idempotent_and_exact(v2):
    assert v2.coordinator.upgrade() is True
    assert v2.store.record == {"schema_version": 3, "leases": {}, "holdback": None, "epochs": {}}
    writes = v2.store.writes
    assert v2.coordinator.upgrade() is False
    assert v2.store.writes == writes
    assert v2.coordinator.downgrade() is True
    assert v2.store.record == {"schema_version": 2, "leases": {}, "holdback": None}
    assert v2.coordinator.downgrade() is False


@pytest.mark.parametrize("busy", ["lease", "waiter"])
def test_downgrade_needs_an_empty_journal(v3, busy):
    lease = hold(v3, "gcp", "gateway")
    if busy == "waiter":
        enqueue(v3, "gcp", "gateway", run=2)
        v3.coordinator.release("gcp", lease["operation_id"], True)
    with pytest.raises(rollout.Busy, match="empty journal"):
        v3.coordinator.downgrade()
    assert v3.store.record["schema_version"] == 3


def load_pinned_v2(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "cloud_rollout_pinned_v2.py.txt"
    source = fixture.read_bytes()
    assert hashlib.sha256(source).hexdigest() == PINNED_V2_SHA256
    path = tmp_path / "pinned_v2_cloud_rollout.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("pinned_v2_cloud_rollout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("holding", [False, True])
def test_pinned_v2_client_refuses_a_v3_journal_before_any_write(v3, tmp_path, holding):
    """E6: the pinned client reads v3 as an unknown lock and never writes it."""
    old = load_pinned_v2(tmp_path)
    if holding:
        hold(v3, "gcp", "control-plane")
        enqueue(v3, "gcp", "control-plane", run=2)
    before = (copy.deepcopy(v3.store.record), v3.store.generation)
    legacy = old.Coordinator(v3.store, v3.coordinator.probe, lambda: v3.clock[0])
    fence = (v3.store.record["leases"]["gcp/control-plane"]["operation_id"] if holding else "a" * 32)
    attempts = [
        lambda: legacy.acquire("gcp", QCP.format(9), "gateway", 3600),
        lambda: legacy.enqueue("gcp", QCP.format(9), "gateway", 600),
        lambda: legacy.try_admit("b" * 32, 3600),
        lambda: legacy.check("gcp", fence),
        lambda: legacy.release("gcp", fence, True),
        lambda: legacy.release("gcp", fence, False),
        lambda: legacy.recover("gcp", fence, lambda _: True),
        lambda: legacy.dequeue("b" * 32),
        lambda: old.acquire_in_order(legacy, "gcp", QCP.format(9), "gateway", 3600, 600,
                                     stopped=lambda _: False, sleep=lambda _: None),
    ]
    for attempt in attempts:
        with pytest.raises(old.Refused, match="legacy or unknown"):
            attempt()
    with pytest.raises(old.Refused):
        legacy.prune(lambda _: True)
    assert (v3.store.record, v3.store.generation) == before


def test_new_client_reads_a_journal_the_pinned_v2_client_wrote(v2, tmp_path):
    old = load_pinned_v2(tmp_path)
    legacy = old.Coordinator(v2.store, v2.coordinator.probe, lambda: v2.clock[0])
    lease = legacy.acquire("gcp", QCP.format(9), "gateway", 3600)
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)
    assert v2.coordinator.lanes("gcp")["lanes"]["gateway"]["held"] is True
    with pytest.raises(rollout.Busy, match="empty journal"):
        v2.coordinator.upgrade()
    legacy.release("gcp", lease["operation_id"], True)
    assert v2.coordinator.upgrade() is True


# Validation of v3 journals ------------------------------------------------------

def v3_lease(cloud, component, fence):
    return {"cloud": cloud, "component": component, "operation_id": fence, "owner": "x",
            "state": "active", "created_at": 1, "expires_at": 2, "host": "h", "pid": 0}


def v3_record(*leases, holdback="azure", epochs=None):
    record = {"schema_version": 3, "holdback": None, "epochs": epochs or {},
              "leases": {f"{c}/{k}": v3_lease(c, k, f) for c, k, f in leases}}
    if leases:
        record["holdback"] = {"cloud": holdback, "release": "a" * 40, "checked_at": 1}
    return record


@pytest.mark.parametrize("record,match", [
    (v3_record(("gcp", "gateway", "a" * 32), ("gcp", "public-surface", "b" * 32)), "conflicting"),
    (v3_record(("aws", "gateway", "a" * 32), ("aws", "control-plane", "b" * 32), holdback="gcp"), "conflicting"),
    (v3_record(("gcp", "gateway", "a" * 32), ("gcp", "control-plane", "a" * 32)), "repeated fence"),
    (v3_record(("gcp", "gateway", "a" * 32), ("aws", "gateway", "b" * 32), ("azure", "gateway", "c" * 32),
               holdback="gcp"), "leases"),
    (v3_record(("gcp", "gateway", "a" * 32), holdback="gcp"), "protected cloud"),
    (v3_record(("gcp", "gateway", "a" * 32), epochs={"gcp/gateway": -1}), "epochs"),
    (v3_record(epochs={"gcp/gateway": "1"}), "epochs"),
])
def test_v3_journal_validation_refuses_states_no_client_writes(record, match):
    with pytest.raises(rollout.Refused, match=match):
        rollout.state_record(record)


@pytest.mark.parametrize("mutate", [
    lambda r: r["leases"].__setitem__("gcp", r["leases"].pop("gcp/gateway")),
    lambda r: r["leases"].__setitem__("gcp/router", r["leases"].pop("gcp/gateway")),
    lambda r: r["leases"]["gcp/gateway"].__setitem__("component", "control-plane"),
    lambda r: r["leases"]["gcp/gateway"].__setitem__("cloud", "aws"),
])
def test_v3_lease_key_must_match_its_cloud_and_component(mutate):
    record = v3_record(("gcp", "gateway", "a" * 32))
    mutate(record)
    with pytest.raises(rollout.Refused, match="invalid release lease"):
        rollout.state_record(record)


@pytest.mark.parametrize("version", [1, 4, "3", 3.0, None])
def test_unknown_journal_versions_stay_busy(version):
    with pytest.raises(rollout.Busy, match="legacy or unknown"):
        rollout.state_record({"schema_version": version, "leases": {}, "holdback": None})


# The interleavings table (docs/design/component-leases-v3.md) -------------------

@pytest.mark.parametrize("cp,gw,allowed", [
    ("control-plane", "gateway", True),      # canary/ramp/migrate while the gateway rolls
    ("public-surface", "gateway", False),    # a standalone public-surface change and PONG gates
    ("control-plane", "public-surface", False),
])
def test_interleavings_table_admission(v3, cp, gw, allowed):
    hold(v3, "gcp", cp)
    if allowed:
        assert hold(v3, "gcp", gw, run=2)["component"] == gw
    else:
        with pytest.raises(rollout.Busy):
            hold(v3, "gcp", gw, run=2)


def test_interleavings_table_release_while_the_other_lane_rolls(v3):
    # "verify-cloud-complete, release | gateway rolling": the control plane's
    # release must not read the gateway's state, and the gateway keeps its lease.
    cp = hold(v3, "gcp", "control-plane")
    gw = hold(v3, "gcp", "gateway", run=2)
    v3.component_health[("gcp", "gateway")] = False
    v3.coordinator.release("gcp", cp["operation_id"], True)
    assert v3.coordinator.check("gcp", gw["operation_id"])["component"] == "gateway"
