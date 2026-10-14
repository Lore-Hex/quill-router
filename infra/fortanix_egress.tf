# A fixed source address for the GCP gateways' connections to Fortanix.
#
# Fortanix's test endpoint (poc1.armet.ai, the "fortanix" provider in
# quill-cloud-proxy's enclave-go/internal/directproviders) accepts
# connections only from addresses it has allow-listed. A gateway VM's
# external address is ephemeral and changes whenever a rollout replaces the
# VM. So the route below sends only the gateways' connections to Fortanix's
# address through one VM holding a reserved address, and that address is the
# one Fortanix allow-lists. Every other destination still leaves from each
# gateway's own address, and no gateway or rollout changes.
#
# The VM forwards TLS it cannot read: the enclave runs TLS with Fortanix end
# to end and checks Fortanix's certificate itself.
#
# While the VM is not running, GCP drops the gateways' packets to Fortanix
# instead of sending them by the default route.
#
# If Fortanix's address changes, the gateways' connections leave directly
# again and Fortanix refuses them. To follow it, change fortanix_destinations
# and merge. The apply replaces the VM, because the startup script changes,
# and the routes, and keeps the reserved address; Fortanix connections fail
# until the new VM has booted.
locals {
  fortanix_egress_region = "us-west1"
  fortanix_egress_zone   = "us-west1-b"

  # poc1.armet.ai's A record on 2026-10-14: one EC2 address in AWS us-west-1.
  fortanix_destinations = ["52.53.179.221/32"]
}

# Fortanix allow-lists this address. prevent_destroy fails any plan that
# would destroy it while this block exists; deleting the block deletes the
# address.
resource "google_compute_address" "fortanix_egress" {
  project      = local.gcp_project_id
  name         = "tr-fortanix-egress"
  region       = local.fortanix_egress_region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
  description  = "Source address of the GCP gateways' connections to Fortanix, which allow-lists it (infra/fortanix_egress.tf)."

  lifecycle {
    prevent_destroy = true
  }
}

# No service account: the VM calls no Google API. It must not carry the
# gateways' tag, quill-enclave: the route would then send the VM's own
# forwarded packets back to it.
resource "google_compute_instance" "fortanix_egress" {
  project        = local.gcp_project_id
  name           = "tr-fortanix-egress"
  zone           = local.fortanix_egress_zone
  machine_type   = "e2-small"
  can_ip_forward = true

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network    = "default"
    subnetwork = "default"

    access_config {
      nat_ip       = google_compute_address.fortanix_egress.address
      network_tier = google_compute_address.fortanix_egress.network_tier
    }
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  # 10.128.0.0/9 holds every subnet of the default network, the range of
  # its default-allow-internal rule, which admits the forwarded packets.
  metadata_startup_script = templatefile("${path.module}/fortanix_egress_startup.sh.tftpl", {
    destinations = join(", ", sort(local.fortanix_destinations))
    source_range = "10.128.0.0/9"
  })

  labels = {
    service = "fortanix-egress"
  }

  allow_stopping_for_update = true
}

# Only VMs tagged quill-enclave, the gateways' instance templates' tag
# (quill-cloud-proxy tools/deploy-gcp-mig.sh), use these routes. A /32 is
# more specific than the default route, so it wins although each gateway
# has its own external address. A replacement VM with the same name and zone
# becomes the next hop without a change to the route.
resource "google_compute_route" "fortanix_egress" {
  for_each = toset(local.fortanix_destinations)

  project           = local.gcp_project_id
  name              = "tr-fortanix-egress-${replace(replace(each.value, ".", "-"), "/", "-")}"
  description       = "The gateways' connections to Fortanix leave from tr-fortanix-egress's reserved address (infra/fortanix_egress.tf)."
  network           = "default"
  dest_range        = each.value
  next_hop_instance = google_compute_instance.fortanix_egress.self_link
  tags              = ["quill-enclave"]
}

output "fortanix_egress_address" {
  description = "The address Fortanix allow-lists for the GCP gateways."
  value       = google_compute_address.fortanix_egress.address
}
