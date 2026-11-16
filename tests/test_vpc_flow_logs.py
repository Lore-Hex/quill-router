"""Flow logs for traffic between internal hosts (infra/vpc_flow_logs.tf).

The subnets are imported, not created: they were made by the default
network's auto mode and hold running hosts. A change that would replace one
must fail at plan time, and the filter must keep only internal traffic, or
the records follow the gateways' public request volume.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "infra/vpc_flow_logs.tf").read_text()

# The ranges auto mode assigned to the default network's regional subnets.
AUTO_MODE_RANGES = {
    "us-central1": "10.128.0.0/20",
    "us-east4": "10.150.0.0/20",
    "europe-west4": "10.164.0.0/20",
    "us-west1": "10.138.0.0/20",
}


def _subnets() -> dict[str, str]:
    block = re.search(r"flow_log_subnets\s*=\s*\{(.*?)\}", TF, re.S)
    assert block, "flow_log_subnets not found"
    return dict(re.findall(r'"([a-z0-9-]+)"\s*=\s*"([0-9./]+)"', block.group(1)))


def test_the_subnets_are_every_enclave_region_at_its_auto_mode_range() -> None:
    assert _subnets() == AUTO_MODE_RANGES
    enclave_regions = (ROOT / "src/trusted_router/enclave_regions.py").read_text()
    for region in AUTO_MODE_RANGES:
        assert f'"{region}"' in enclave_regions, region


def test_the_subnets_are_imported_and_cannot_be_replaced() -> None:
    assert re.search(
        r"import \{\s*for_each = local\.flow_log_subnets\s*"
        r"to\s*= google_compute_subnetwork\.default\[each\.key\]\s*"
        r'id\s*= "projects/\$\{local\.gcp_project_id\}/regions/\$\{each\.key\}/subnetworks/default"',
        TF,
    )
    resource = TF.split('resource "google_compute_subnetwork" "default"', 1)[1]
    assert 'name          = "default"' in resource
    assert 'network       = "default"' in resource
    assert "ip_cidr_range = each.value" in resource
    assert re.search(r"lifecycle \{\s*prevent_destroy = true\s*\}", resource)


def test_only_internal_to_internal_traffic_is_recorded_and_none_is_sampled_out() -> None:
    assert (
        "filter_expr          = \"inIpRange(connection.src_ip, '10.0.0.0/8') && "
        "inIpRange(connection.dest_ip, '10.0.0.0/8')\""
    ) in TF
    assert "flow_sampling        = 1\n" in TF
    assert 'aggregation_interval = "INTERVAL_15_MIN"' in TF
    for cidr in AUTO_MODE_RANGES.values():
        first_octet = int(cidr.split(".")[0])
        assert first_octet == 10, cidr
