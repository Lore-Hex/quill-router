"""Guard native cost, full nonnative matrices, and the two native-only bounds."""
from __future__ import annotations

import inspect
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conformance import test_nyte_resale_ledger as contract
from tests.conformance.conftest import pytest_nyte_emulator_selection
from trusted_router.storage import InMemoryStore


def ledger_cases():
    for name, function in vars(contract).items():
        if not name.startswith("test_nyte_"):
            continue
        dimensions = [(mark.args[0], mark.args[1]) for mark in getattr(function, "pytestmark", []) if mark.name == "parametrize"]
        if "resale_enabled" in inspect.signature(function).parameters:
            dimensions.append(("resale_enabled", [True, False]))
        for values in product(*(values for _, values in dimensions)):
            yield name, dict(zip((key for key, _ in dimensions), values, strict=True))


def test_emulator_representatives_are_live_bounded_and_sales_on():
    cases = list(ledger_cases())
    assert len(cases) == 164  # Full matrices remain present, not deleted.
    kept = [(name, params) for name, params in cases
            if contract.emulator_case_reason(name, {**params, "store": "spanner-emulator"})]
    assert len(kept) == 40
    assert all(params.get("resale_enabled", True) for _, params in kept)
    for suffix, representatives in contract.EMULATOR_CASES.items():
        for expected, reason in representatives:
            assert reason
            matches = [p for n, p in kept if n == "test_nyte_" + suffix
                       and {k: v for k, v in p.items() if k != "resale_enabled"} == expected]
            assert len(matches) == 1, (suffix, expected)
    with pytest.raises(AssertionError, match="Register native representatives"):
        contract.emulator_case_reason("test_nyte_new_matrix", {"store": "spanner-emulator", "new_branch": True})


def test_collection_deselects_only_native_duplicates_before_fixtures():
    class Item(SimpleNamespace):
        __hash__ = object.__hash__

    items = [Item(path=Path("tests/conformance/test_nyte_resale_ledger.py"), module=contract,
                  originalname=name, callspec=SimpleNamespace(params={**params, "store": backend}), user_properties=[])
             for backend in ("memory", "spanner-fake", "postgres", "spanner-pg", "spanner-emulator")
             for name, params in ledger_cases()]
    # Another module's native matrix is never filtered by NYTE's policy.
    unrelated = Item(path=Path("tests/conformance/test_other.py"))
    original = [*items, unrelated]
    items.append(unrelated)
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=Mock()))
    pytest_nyte_emulator_selection(config, items)
    assert unrelated in items
    omitted = config.hook.pytest_deselected.call_args.kwargs["items"]
    assert len(omitted) == 124
    assert all(item.callspec.params["store"] == "spanner-emulator" for item in omitted)
    for backend in ("memory", "spanner-fake", "postgres", "spanner-pg"):
        before = [i for i in original if i is not unrelated and i.callspec.params["store"] == backend]
        after = [i for i in items if i is not unrelated and i.callspec.params["store"] == backend]
        assert before == after and len(after) == 164
    assert all(i.user_properties for i in items if i is not unrelated and i.callspec.params["store"] == "spanner-emulator")


def test_native_internal_ordering_subset_preserves_both_allocation_outcomes(monkeypatch):
    store = InMemoryStore()
    request = SimpleNamespace(node=SimpleNamespace(callspec=SimpleNamespace(params={"store": "spanner-emulator"})))
    contract.test_nyte_purchase_completion_ordering_matrix(store, "nativeordering", monkeypatch, request)
    buyers = [row for (kind, key), row in store.resale_entities.items() if kind == "nyte_position" and key.startswith("cs_")]
    assert len(buyers) == 2
    assert {row["allocation_status"] for row in buyers} == {"complete", "historical"}


def test_native_sweep_uses_only_its_test_owned_key_range(monkeypatch):
    store = InMemoryStore()
    page = Mock(wraps=store.reserved_resale_position_page)
    monkeypatch.setattr(store, "reserved_resale_position_page", page)
    request = SimpleNamespace(node=SimpleNamespace(callspec=SimpleNamespace(params={"store": "spanner-emulator"})))
    contract.test_nyte_interrupted_checkout_repairs_without_replay(store, "nativesweep", monkeypatch, "sweep", "healthy", request)
    page.assert_called_once_with(cursor="cs_nativesweep", limit=1)
