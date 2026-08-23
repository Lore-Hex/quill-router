from __future__ import annotations

import pytest

from scripts.deploy.resolve_failed_revision_repair import plan_failed_revision_repair


def _service(*, spec: list[dict[str, object]], status: list[dict[str, object]]) -> dict:
    return {"spec": {"traffic": spec}, "status": {"traffic": status}}


def test_plans_repair_for_failed_desired_canary_and_dangling_tag() -> None:
    service = _service(
        spec=[
            {"revisionName": "healthy", "percent": 90},
            {"revisionName": "failed", "percent": 10},
        ],
        status=[
            {"revisionName": "healthy", "percent": 100},
            {"revisionName": "failed", "tag": "staged-probe"},
            {"revisionName": "restore", "tag": "restore-candidate"},
        ],
    )

    plan = plan_failed_revision_repair(service, ["failed", "unreferenced-failure"])

    assert plan is not None
    assert plan.active_revision == "healthy"
    assert plan.failed_revisions == ("failed",)


def test_no_plan_when_failed_revisions_are_not_traffic_referenced() -> None:
    service = _service(
        spec=[{"revisionName": "healthy", "percent": 100}],
        status=[{"revisionName": "healthy", "percent": 100}],
    )

    assert plan_failed_revision_repair(service, ["old-failed"]) is None


def test_repair_fails_closed_during_real_effective_split() -> None:
    service = _service(
        spec=[
            {"revisionName": "healthy", "percent": 90},
            {"revisionName": "failed", "percent": 10},
        ],
        status=[
            {"revisionName": "healthy", "percent": 90},
            {"revisionName": "other", "percent": 10},
            {"revisionName": "failed", "tag": "staged-probe"},
        ],
    )

    with pytest.raises(ValueError, match="exactly one 100%-traffic revision"):
        plan_failed_revision_repair(service, ["failed"])


@pytest.mark.parametrize("parent", ["spec", "status"])
def test_repair_rejects_malformed_traffic(parent: str) -> None:
    service = _service(
        spec=[{"revisionName": "healthy", "percent": 100}],
        status=[{"revisionName": "healthy", "percent": 100}],
    )
    service[parent]["traffic"] = "not-a-list"

    with pytest.raises(ValueError, match=f"service {parent} traffic is not a list"):
        plan_failed_revision_repair(service, ["failed"])
