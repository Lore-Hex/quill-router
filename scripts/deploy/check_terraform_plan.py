"""Reject deletions (including replacements) before automated Terraform apply."""

import json
import sys


def main() -> int:
    try:
        plan = json.load(sys.stdin)
        if not isinstance(plan, dict) or not str(plan.get("format_version", "")).startswith("1."):
            raise ValueError("Unsupported or missing Terraform plan format")
        if plan.get("errored"):
            raise ValueError("Terraform plan contains errors")
        changes = plan.get("resource_changes", [])
        if not isinstance(changes, list):
            raise ValueError("Invalid resource_changes")
        deletions = []
        for resource in changes:
            actions = resource["change"]["actions"]
            if not isinstance(actions, list) or not actions:
                raise ValueError("Missing resource actions")
            if any(action not in {"no-op", "read", "create", "update", "delete"} for action in actions):
                raise ValueError("Unrecognized resource action")
            if "delete" in actions:
                deletions.append(resource["address"])
        if deletions:
            print("Refusing automated Terraform apply: plan deletes or replaces:", file=sys.stderr)
            for address in deletions:
                print(f"  {address}", file=sys.stderr)
            return 1
    except (ValueError, KeyError, TypeError) as exc:
        print(f"Cannot validate Terraform plan: {exc}", file=sys.stderr)
        return 1
    print("Terraform plan contains no resource deletions or replacements.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
