import pytest

from trusted_router.schemas import CreateKeyRequest, PatchKeyRequest
from trusted_router.services.keyed_admission import KeyedConcurrencyAdmission
from trusted_router.storage_gcp_counters import key_usage_shard_count
from trusted_router.strict_budget import StrictBudgetBusy, strict_budget_slot


def test_strict_mode_is_immutable_and_single_shard():
    assert CreateKeyRequest(name="default").budget_strict is False
    assert CreateKeyRequest(name="strict", budget_strict=True).budget_strict is True
    with pytest.raises(ValueError):
        PatchKeyRequest(budget_strict=True)
    with pytest.raises(ValueError):
        PatchKeyRequest(budget_strict=False)
    with pytest.raises(ValueError, match="one key counter"):
        key_usage_shard_count({"budget_strict": True, "usage_shard_count": 16})
    assert key_usage_shard_count({"budget_strict": True}) == 1


def test_strict_admission_never_queues_and_releases_on_errors(monkeypatch):
    from trusted_router import strict_budget

    limiter = KeyedConcurrencyAdmission(max_subjects=2)
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION", limiter)
    with pytest.raises(RuntimeError):
        with strict_budget_slot("a"), strict_budget_slot("b"):
            with pytest.raises(StrictBudgetBusy):
                with strict_budget_slot("a"):
                    pytest.fail("a concurrent strict request was admitted")
            with pytest.raises(StrictBudgetBusy):
                with strict_budget_slot("c"):
                    pytest.fail("global strict capacity was exceeded")
            raise RuntimeError("database unavailable")
    assert limiter.count("a") == limiter.count("b") == 0
    with strict_budget_slot("c"):
        assert limiter.count("c") == 1
