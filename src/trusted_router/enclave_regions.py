"""Live GCP enclave gateway inventory, separate from Cloud Run deployments.

Run this dependency-free module as a script to read the same inventory from
deploy shell scripts. Standalone AWS/Azure gateways have their own inventory.
"""

ENCLAVE_REGIONS = ("us-central1", "us-east4", "europe-west4", "us-west1")

# Gateway regions that have NO Cloud Run control plane of their own. A gateway
# here reaches the control plane through the global load balancer, which sends
# it to the nearest control-plane region (us-central1 for us-west1).
#
# Named rather than left implicit because the deploy defaults are checked
# against this inventory: every attested region OUTSIDE this set must be a warm
# Cloud Run region, and a region INSIDE it must not appear in the Cloud Run
# lists at all. Without the set, the only way to make a gateway-only region
# pass that check is an inert warm/min-instances entry for a service that does
# not exist -- configuration that reports capacity nobody is running.
ENCLAVE_REGIONS_WITHOUT_LOCAL_CONTROL_PLANE = frozenset({"us-west1"})


if __name__ == "__main__":
    print(",".join(ENCLAVE_REGIONS))
