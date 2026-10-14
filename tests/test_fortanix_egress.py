"""The GCP gateways' fixed egress to Fortanix (infra/fortanix_egress.tf and
its startup script), held to what the route and the VM must agree on."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
TF = (ROOT / "infra" / "fortanix_egress.tf").read_text(encoding="utf-8")
SCRIPT = (ROOT / "infra" / "fortanix_egress_startup.sh.tftpl").read_text(encoding="utf-8")


def _block(header: str) -> str:
    """The block's text without comments, its whitespace collapsed."""
    start = TF.index(header)
    depth = 0
    for index in range(TF.index("{", start), len(TF)):
        depth += {"{": 1, "}": -1}.get(TF[index], 0)
        if depth == 0:
            text = re.sub(r"#.*", "", TF[start : index + 1])
            return re.sub(r"\s+", " ", text)
    raise AssertionError(f"unclosed block: {header}")


def test_only_the_gateways_route_their_fortanix_connections_through_the_vm() -> None:
    route = _block('resource "google_compute_route" "fortanix_egress"')
    assert 'tags = ["quill-enclave"]' in route
    assert "for_each = toset(local.fortanix_destinations)" in route
    assert "dest_range = each.value" in route
    assert "next_hop_instance = google_compute_instance.fortanix_egress.self_link" in route
    vm = _block('resource "google_compute_instance" "fortanix_egress"')
    assert "can_ip_forward = true" in vm
    # The gateways' tag on the VM would route its own forwarded packets back
    # to it.
    assert "quill-enclave" not in vm
    assert "nat_ip = google_compute_address.fortanix_egress.address" in vm
    assert "network_tier = google_compute_address.fortanix_egress.network_tier" in vm
    assert "service_account" not in vm


def test_the_vm_forwards_only_the_routed_destinations() -> None:
    vm = _block('resource "google_compute_instance" "fortanix_egress"')
    # Sorted, so reordering the list does not change the script and replace the VM.
    assert 'destinations = join(", ", sort(local.fortanix_destinations))' in vm
    rules = SCRIPT[SCRIPT.index("nft -f - <<RULES\n") : SCRIPT.index("\nRULES\n")]
    assert [line.strip() for line in rules.splitlines()[1:]] == [
        "table ip fortanix_egress",
        "delete table ip fortanix_egress",
        "table ip fortanix_egress {",
        "chain forward {",
        "type filter hook forward priority filter; policy drop;",
        "ct state established,related accept",
        'iifname "$iface" oifname "$iface" ip saddr ${source_range} ip daddr { ${destinations} } tcp dport 443'
        " ct state new counter accept",
        "}",
        "chain postrouting {",
        "type nat hook postrouting priority srcnat; policy accept;",
        'oifname "$iface" ip daddr { ${destinations} } counter masquerade',
        "}",
        "}",
    ]


def test_forwarding_turns_on_only_after_the_rules_load() -> None:
    steps = [
        "until command -v nft",
        "\nnft -f - <<RULES\n",
        'echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-fortanix-egress.conf',
        "sysctl --quiet -w net.ipv4.ip_forward=1",
    ]
    positions = [SCRIPT.index(step) for step in steps]
    assert positions == sorted(positions)
    assert SCRIPT.count("ip_forward") == 2
    # An enabled nftables.service would flush these rules whenever a package
    # upgrade restarts it; the script loads them itself on every boot.
    assert "nftables.service" not in SCRIPT and "/etc/nftables.conf" not in SCRIPT


def test_prevent_destroy_guards_the_allow_listed_address() -> None:
    address = _block('resource "google_compute_address" "fortanix_egress"')
    assert "lifecycle { prevent_destroy = true }" in address


def test_the_rendered_startup_script_parses() -> None:
    destinations = re.search(r"fortanix_destinations = \[(.*?)\]", TF)
    assert destinations is not None
    values = re.findall(r'"([^"]+)"', destinations.group(1))
    assert values and all(re.fullmatch(r"\d+\.\d+\.\d+\.\d+/32", value) for value in values)
    rendered = SCRIPT.replace("${destinations}", ", ".join(values))
    rendered = rendered.replace("${source_range}", "10.128.0.0/9")
    assert "${" not in rendered and "%{" not in rendered
    subprocess.run([BASH, "-n"], input=rendered, text=True, check=True)  # noqa: S603 - fixed script under test
