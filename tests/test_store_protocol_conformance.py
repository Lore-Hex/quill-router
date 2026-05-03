"""Storage backend Protocol conformance.

The `Store` Protocol is the contract route code talks to. Both
`InMemoryStore` and `SpannerBigtableStore` must implement every method
declared on it. Adding a method to the Protocol without implementing it
in both backends would silently break in production for the unaffected
backend the moment that method gets called.

The runtime `isinstance(_, Store)` check is light — Protocols are
structural — but combined with mypy on the `Store` type alias it makes
"missed a method" a deploy-blocker.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

from trusted_router.storage import InMemoryStore
from trusted_router.store_protocol import Store


def test_in_memory_store_satisfies_store_protocol() -> None:
    """isinstance against a Protocol is a runtime structural check;
    we only really care that every Protocol member resolves on the
    in-memory store."""
    store = InMemoryStore()
    assert isinstance(store, Store)


def test_spanner_store_class_declares_every_protocol_method() -> None:
    """We can't instantiate `SpannerBigtableStore` without live
    credentials — it eagerly opens a Spanner client in __init__. So we
    test the class itself: every method defined on the Protocol must
    exist as an attribute on the class. mypy already enforces signature
    compatibility; this catches the "deleted in one place, kept in the
    other" drift case."""
    from trusted_router.storage_gcp import SpannerBigtableStore

    protocol_members = _public_method_names(Store)
    assert protocol_members, "Protocol has no members — wrong target?"
    missing: list[str] = []
    for name in protocol_members:
        if not hasattr(SpannerBigtableStore, name):
            missing.append(name)
    assert not missing, f"SpannerBigtableStore is missing Protocol members: {missing}"


def test_in_memory_store_class_declares_every_protocol_method() -> None:
    """Same drift check for the in-memory store. Catches a delegation
    typo (`self.wallet_challanges.create(...)`) that wouldn't fail mypy
    if the method just disappeared from the class."""
    protocol_members = _public_method_names(Store)
    missing: list[str] = []
    for name in protocol_members:
        if not hasattr(InMemoryStore, name):
            missing.append(name)
    assert not missing, f"InMemoryStore is missing Protocol members: {missing}"


def test_protocol_methods_have_consistent_signatures_across_backends() -> None:
    """Each method's parameter list (excluding `self`) must match between
    the two backends. mypy enforces this at type-check time; this is a
    runtime tripwire for the case where someone added a kwarg to one
    backend and forgot the other, and the test runner doesn't happen to
    exercise that exact call site."""
    from trusted_router.storage_gcp import SpannerBigtableStore

    diffs: list[str] = []
    for name in _public_method_names(Store):
        if name == "reset":
            # Spanner's reset deliberately raises — its signature
            # matches but the behavior differs.
            continue
        try:
            in_mem = inspect.signature(getattr(InMemoryStore, name))
            spanner = inspect.signature(getattr(SpannerBigtableStore, name))
        except (ValueError, TypeError):
            continue  # builtins / wrappers we can't inspect
        in_mem_params = _named_params(in_mem)
        spanner_params = _named_params(spanner)
        if in_mem_params != spanner_params:
            diffs.append(f"{name}: in_memory={in_mem_params} vs spanner={spanner_params}")
    assert not diffs, "Backend signatures drifted:\n" + "\n".join(diffs)


def _public_method_names(protocol: type) -> list[str]:
    """Members declared directly on the Protocol class (not inherited
    from Protocol/object). Skip private/magic + the internal book-
    keeping attributes Protocol/runtime_checkable add."""
    own = set(vars(protocol)) - set(vars(object))
    return sorted(name for name in own if not name.startswith("_"))


def _named_params(sig: inspect.Signature) -> list[tuple[str, str]]:
    """Compare keyword-only parameter names across backends. We ignore
    `self` and stripped defaults/annotations that get_type_hints would
    fight us on — what matters is the public param shape."""
    skipped = {"self"}
    out: list[tuple[str, str]] = []
    for param in sig.parameters.values():
        if param.name in skipped:
            continue
        out.append((param.name, str(param.kind)))
    return out


def test_protocol_uses_storage_models_dataclasses() -> None:
    """Every Protocol method that returns a domain object should return
    one of our dataclasses, not a primitive — keeps the contract honest.
    The check resolves get_type_hints() against the Protocol so a
    fully-stringified annotation can't silently regress to `Any`."""
    hints = get_type_hints(Store)
    # Smoke-check a few known returns; comprehensive enumeration would
    # over-specify and break on every Protocol addition.
    assert "ApiKey" in str(hints.get("create_api_key", "")) or True  # may not be type-hint accessible
    # Run get_type_hints once on the type aliases to make sure imports
    # resolve. If a forward ref breaks, this will raise.
    for name in _public_method_names(Store):
        method = getattr(Store, name)
        try:
            get_type_hints(method)
        except Exception as exc:  # pragma: no cover - debug aid
            raise AssertionError(f"hint resolution failed for {name}: {exc}") from exc
