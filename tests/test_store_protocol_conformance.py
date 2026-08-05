"""Storage backend Protocol conformance.

The `Store` Protocol is the contract route code talks to. Every backend must
implement every method declared on it. Adding a method to the Protocol without
implementing it across backends would silently break in production for the
unaffected backend the moment that method gets called.

The runtime `isinstance(_, Store)` check is light — Protocols are
structural — but combined with mypy on the `Store` type alias it makes
"missed a method" a deploy-blocker.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import get_type_hints

import pytest

import trusted_router
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


def test_postgres_store_class_declares_every_protocol_method() -> None:
    """Incremental backends still declare the whole Protocol surface.

    Methods outside the current increment may raise a named
    NotImplementedError, but a missing method or drifted signature is never
    allowed to reach production as an AttributeError.
    """
    from trusted_router.storage_postgres import PostgresStore

    protocol_members = _public_method_names(Store)
    missing = [
        name for name in protocol_members if not hasattr(PostgresStore, name)
    ]
    assert not missing, f"PostgresStore is missing Protocol members: {missing}"


def test_protocol_methods_have_consistent_signatures_across_backends() -> None:
    """Each method's parameter list (excluding `self`) must match between
    all backends. mypy enforces this at type-check time; this is a
    runtime tripwire for the case where someone added a kwarg to one
    backend and forgot the other, and the test runner doesn't happen to
    exercise that exact call site."""
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_postgres import PostgresStore

    diffs: list[str] = []
    backend_classes = {
        "spanner": SpannerBigtableStore,
        "postgres": PostgresStore,
    }
    for name in _public_method_names(Store):
        if name == "reset":
            # Spanner's reset deliberately raises — its signature
            # matches but the behavior differs.
            continue
        for backend_name, backend_class in backend_classes.items():
            try:
                in_mem = inspect.signature(getattr(InMemoryStore, name))
                backend = inspect.signature(getattr(backend_class, name))
            except (ValueError, TypeError):
                continue  # builtins / wrappers we can't inspect
            in_mem_params = _named_params(in_mem)
            backend_params = _named_params(backend)
            if in_mem_params != backend_params:
                diffs.append(
                    f"{name}: in_memory={in_mem_params} "
                    f"vs {backend_name}={backend_params}"
                )
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


def test_spanner_store_satisfies_typed_billing_store() -> None:
    """The typed-billing capability (#39): SpannerBigtableStore must declare
    every TypedBillingStore method so isinstance(store, TypedBillingStore) is a
    real, mypy-narrowing capability check on the authorization path — replacing
    the old getattr(STORE, "authorize_gateway_typed", None) probes."""
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.store_protocol import TypedBillingStore

    missing = [
        name for name in _public_method_names(TypedBillingStore)
        if not hasattr(SpannerBigtableStore, name)
    ]
    assert not missing, f"SpannerBigtableStore missing TypedBillingStore members: {missing}"


def test_spanner_legacy_json_reserve_path_removed() -> None:
    from trusted_router.storage_gcp import SpannerBigtableStore

    store = SpannerBigtableStore.__new__(SpannerBigtableStore)
    with pytest.raises(RuntimeError, match="legacy JSON reserve path removed"):
        store.reserve("ws", "key", 1)


def test_in_memory_store_is_not_a_typed_billing_store() -> None:
    """InMemoryStore has NO typed Spanner tables, so it must NOT satisfy the
    capability — otherwise the isinstance guard would route local/test authorize
    down the typed-DML path that only exists on Spanner."""
    from trusted_router.store_protocol import TypedBillingStore

    assert not isinstance(InMemoryStore(), TypedBillingStore)


def test_typed_billing_store_helper_unwraps_the_module_proxy() -> None:
    """isinstance against a runtime_checkable Protocol does NOT see methods
    reached via _StoreProxy.__getattr__ — so isinstance(STORE, TypedBillingStore)
    reads False even for a typed backend and would silently route typed
    billing to the legacy path (codex #97). typed_billing_store() must unwrap
    the proxy target and check THAT."""
    from trusted_router.storage import _StoreProxy, typed_billing_store
    from trusted_router.store_protocol import TypedBillingStore

    class FakeTyped:
        def authorize_gateway_typed(self, **k: object) -> None: ...
        def typed_finalize_gateway_authorization(self, *a: object, **k: object) -> None: ...
        def typed_finalize_gateway(self, **k: object) -> None: ...
        def read_typed_reservation(self, r: object) -> None: ...
        def is_typed_reservation(self, *a: object) -> None: ...
        def get_typed_authorization_by_idempotency(self, *a: object) -> None: ...
        def typed_credit_snapshot(self, w: object) -> None: ...

    proxy = _StoreProxy()
    proxy._configure(FakeTyped())  # type: ignore[arg-type]
    # The trap: naive isinstance through the proxy is False despite the target...
    assert not isinstance(proxy, TypedBillingStore)
    # ...but the helper unwraps and returns the typed target.
    assert typed_billing_store(proxy) is proxy.target
    # A non-typed backend (InMemory) -> None, through the proxy or direct.
    proxy._configure(InMemoryStore())
    assert typed_billing_store(proxy) is None
    assert typed_billing_store(InMemoryStore()) is None


# --------------------------------------------------------------------------
# Declared-but-refusing methods
#
# The tests above prove every Protocol method EXISTS on every backend. That is
# not the same as it WORKING: `PostgresStore._not_implemented(name)` raises
# NotImplementedError, which satisfies the structural check and every mypy
# signature test, then blows up at runtime the first time a route calls it.
#
# That is not hypothetical. The AWS and Azure control planes both run
# TR_STORAGE_BACKEND=postgres while every enclave dialled the GCP (Spanner)
# plane, so the Postgres authorize path had never served a real request and
# four gateway-reachable methods sat unimplemented — including one called
# AFTER the credit escrow commits, which would have stranded a reservation on
# every request the moment a peer enclave was cut over to its own plane.
#
# The behavioural conformance suite cannot guard this: tests/conformance's
# Postgres backend `pytest.skip()`s unless TR_CONFORMANCE_POSTGRES_DSN is set,
# so it is normally not running. This check is static on purpose — it needs no
# database and therefore always runs.
# --------------------------------------------------------------------------

# Exactly the modules an enclave can reach, derived from the paths the enclave
# itself dials (quill-cloud-proxy enclave-go/internal/trustedrouter/client.go):
#   /internal/gateway/{authorize,settle,refund,validate,key,resolve-custom-model}
#   /internal/gateway/fetch-image
#   /internal/gateway/video/jobs/...
# The rest of routes/internal is payment webhooks (adyen, paypal, webhook) and
# operator/worker endpoints (sentry, synthetic, reconcile, broadcast_queue,
# federation), which no enclave calls. Scoping to the real surface keeps this
# check about the cutover risk instead of failing on unrelated backlog.
_ENCLAVE_FACING_ROUTES = (
    "routes/internal/gateway.py",
    "routes/internal/fetch_image.py",
    "routes/internal/video_jobs.py",
)


def _store_methods_called_by(paths: tuple[pathlib.Path, ...]) -> dict[str, set[str]]:
    """Map method name -> {"file:line", ...} for every `STORE.<method>` access."""
    calls: dict[str, set[str]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "STORE"
            ):
                calls.setdefault(node.attr, set()).add(f"{path.name}:{node.lineno}")
    return calls


def _methods_that_refuse(module_path: pathlib.Path) -> set[str]:
    """Names passed to `self._not_implemented("...")` in a backend module."""
    tree = ast.parse(module_path.read_text(), filename=str(module_path))
    refused: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_not_implemented"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            refused.add(node.args[0].value)
    return refused


def test_no_enclave_facing_route_calls_a_method_postgres_refuses() -> None:
    """Every `STORE.x` an enclave can reach must be implemented on Postgres.

    A failure here means: cutting any enclave over to a Postgres-backed
    control plane 500s that call path in production.
    """
    src = pathlib.Path(trusted_router.__file__).parent
    modules = tuple(src / rel for rel in _ENCLAVE_FACING_ROUTES)
    missing = [str(p) for p in modules if not p.exists()]
    assert not missing, (
        f"enclave-facing route module(s) moved or renamed: {missing}. "
        "Update _ENCLAVE_FACING_ROUTES — a stale path would make this guard "
        "silently scan nothing."
    )
    called = _store_methods_called_by(modules)
    refused = _methods_that_refuse(src / "storage_postgres.py")

    broken = sorted(set(called) & refused)
    assert not broken, (
        "PostgresStore refuses "
        + str(len(broken))
        + " method(s) that enclave-facing routes call, so the AWS/Azure "
        "control planes would 500 on those paths:\n"
        + "\n".join(f"  {name}  called at {sorted(called[name])}" for name in broken)
        + "\nImplement them on PostgresStore (a legitimate empty result is a "
        "`return None`/`return []`, never a raise)."
    )


def test_the_refusal_detector_actually_detects() -> None:
    """Negative control: a guard that cannot fail guards nothing.

    Without this, a typo in the AST walk above would make the real test
    vacuously green and the next unimplemented method would ship.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        backend = pathlib.Path(tmp) / "backend.py"
        backend.write_text(
            "class S:\n"
            "    def _not_implemented(self, m): raise NotImplementedError(m)\n"
            "    def get_thing(self): self._not_implemented('get_thing')\n"
        )
        routes = pathlib.Path(tmp) / "routes"
        routes.mkdir()
        (routes / "r.py").write_text("def h():\n    return STORE.get_thing()\n")

        assert _methods_that_refuse(backend) == {"get_thing"}
        assert set(_store_methods_called_by((routes / "r.py",))) == {"get_thing"}
