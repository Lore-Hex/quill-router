"""Live GCP enclave gateway inventory, separate from Cloud Run deployments.

Run this dependency-free module as a script to read the same inventory from
deploy shell scripts. Standalone AWS/Azure gateways have their own inventory.
"""

ENCLAVE_REGIONS = ("us-central1", "us-east4", "europe-west4")


if __name__ == "__main__":
    print(",".join(ENCLAVE_REGIONS))
