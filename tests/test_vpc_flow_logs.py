"""Flow logs for traffic between internal hosts (infra/vpc_flow_logs.tf).

The subnets are imported, not created: they were made by the default
network's auto mode and hold running hosts. A change that would replace one
must fail at plan time, and the filter must keep only internal traffic, or
the records follow the gateways' public request volume.
"""

from __future__ import annotations

import re
from pathlib import Path

from trusted_router.enclave_regions import ENCLAVE_REGIONS

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra/vpc_flow_logs.tf").read_text()

# The ranges auto mode assigns to the default network's regional subnets.
AUTO_MODE_RANGES = {
    "us-central1": "10.128.0.0/20",
    "us-east4": "10.150.0.0/20",
    "europe-west4": "10.164.0.0/20",
    "us-west1": "10.138.0.0/20",
    "southamerica-east1": "10.158.0.0/20",
}


def _subnets() -> dict[str, str]:
    block = re.search(r"flow_log_subnets\s*=\s*\{(.*?)\}", TF, re.S)
    assert block, "flow_log_subnets not found"
    return dict(re.findall(r'"([a-z0-9-]+)"\s*=\s*"([0-9./]+)"', block.group(1)))


def _control_plane_regions() -> set[str]:
    lib = (ROOT / "scripts/deploy/_lib.sh").read_text()
    match = re.search(r'TR_CONTROL_PLANE_REGIONS="\$\{TR_CONTROL_PLANE_REGIONS:-([a-z0-9,-]+)\}"', lib)
    assert match, "TR_CONTROL_PLANE_REGIONS default not found"
    return set(match.group(1).split(","))


def _resource() -> str:
    return TF.split('resource "google_compute_subnetwork" "default"', 1)[1]


def test_every_region_with_hosts_or_control_plane_egress_is_covered() -> None:
    # The enclave regions hold hosts; the control plane's Cloud Run services
    # send their internal traffic from the default subnet of each region they
    # run in. A region added to either list without its subnet here would be
    # a source of internal connections nothing records.
    subnets = _subnets()
    assert set(subnets) == set(ENCLAVE_REGIONS) | _control_plane_regions()
    assert subnets == AUTO_MODE_RANGES


def test_the_subnets_are_imported_and_cannot_be_replaced_or_deleted() -> None:
    assert re.search(
        r"import \{\s*for_each = local\.flow_log_subnets\s*"
        r"to\s*= google_compute_subnetwork\.default\[each\.key\]\s*"
        r'id\s*= "projects/\$\{local\.gcp_project_id\}/regions/\$\{each\.key\}/subnetworks/default"',
        TF,
    )
    resource = _resource()
    assert re.search(r"lifecycle \{\s*prevent_destroy = true\s*\}", resource)
    assert 'deletion_policy = "ABANDON"' in resource


def test_the_resource_declares_only_what_the_live_subnets_already_have() -> None:
    # Several subnet arguments force a replacement when they differ from the
    # live object (description, reserved_internal_range and others), and
    # prevent_destroy would then fail every infra apply. Only the identity,
    # the range and the flow-log settings are declared.
    resource = _resource()
    top_level = re.findall(r"^  ([a-z_]+)\s*(?:=|\{)", resource, re.M)
    assert top_level == [
        "for_each",
        "project",
        "name",
        "region",
        "network",
        "ip_cidr_range",
        "deletion_policy",
        "log_config",
        "lifecycle",
    ]
    assert 'name            = "default"' in resource
    assert 'network         = "default"' in resource
    assert "region          = each.key" in resource
    assert "ip_cidr_range   = each.value" in resource


def test_only_internal_to_internal_traffic_is_recorded() -> None:
    log_config = re.search(r"log_config \{(.*?)\n  \}", _resource(), re.S)
    assert log_config, "log_config block not found"
    settings = dict(re.findall(r"^\s+([a-z_]+)\s+= (.+)$", log_config.group(1), re.M))
    assert settings == {
        "aggregation_interval": '"INTERVAL_15_MIN"',
        "flow_sampling": "1",
        "metadata": '"INCLUDE_ALL_METADATA"',
        "filter_expr": (
            "\"inIpRange(connection.src_ip, '10.0.0.0/8') && "
            "inIpRange(connection.dest_ip, '10.0.0.0/8')\""
        ),
    }
    # Every declared range is inside the filter's 10.0.0.0/8.
    for cidr in _subnets().values():
        assert cidr.startswith("10."), cidr
