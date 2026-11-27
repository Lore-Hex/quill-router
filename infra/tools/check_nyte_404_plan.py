"""Refuse out-of-scope writes in a New York 404-only Terraform release."""

import json
import sys
from copy import deepcopy
from typing import Any


def _has_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_unknown(item) for item in value)
    return value is True


def _without_nyte_error(state: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    state = deepcopy(state)
    state.pop("fingerprint", None)
    matches = [m for m in state["path_matcher"] if m["name"] == "exchange-new-york"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one New York matcher")
    return state, matches[0].pop("default_custom_error_response_policy", [])


def require_nyte_404_only(changes: list[dict[str, Any]]) -> None:
    for resource in changes:
        change = resource["change"]
        if change["actions"] in (["read"], ["no-op"]):
            continue
        if resource["address"] != "google_compute_url_map.control" or change["actions"] != ["update"]:
            raise ValueError(f"Out-of-scope change: {resource['address']}")
        unknown = dict(change["after_unknown"])
        unknown.pop("fingerprint", None)
        before, _ = _without_nyte_error(change["before"])
        after, policy = _without_nyte_error(change["after"])
        if before != after or _has_unknown(unknown):
            raise ValueError("URL map change outside the New York error policy")
        if not isinstance(policy, list) or len(policy) != 1:
            raise ValueError("Expected exactly one New York error policy")
        entry = policy[0]
        # The provider can return either a project-relative or a full Compute API link.
        bucket = "projects/quill-cloud-proxy/global/backendBuckets/token-exchange-static"
        if entry["error_service"] not in (bucket, f"https://www.googleapis.com/compute/v1/{bucket}"):
            raise ValueError("Unexpected New York error service")
        if entry["error_response_rule"] != [{
            "match_response_codes": ["404"], "path": "/new-york/404.html", "override_response_code": 404,
        }]:
            raise ValueError("Expected only the reviewed 404 response rule")


if __name__ == "__main__":
    try:
        require_nyte_404_only(json.load(sys.stdin)["resource_changes"])
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(f"Refusing New York-only release: {exc}")
    print("Confirmed New York 404-only scope.")
