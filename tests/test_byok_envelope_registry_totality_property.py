"""Proof that the BYOK AAD v2 backfill's envelope registry is total.

`byok_aad_backfill` decides what to migrate from two hand-written tables:
`_MIGRATED_KINDS` and `_fields_for_kind`. Between them they name three
locations — byok.encrypted_secret, broadcast_destination.encrypted_api_key and
broadcast_destination.encrypted_headers — and tag each with the AAD namespace
("provider" or "control") its envelope was sealed under. The law:

    for every dataclass D in storage_models, and every field f of D whose
    annotation admits an EncryptedSecretEnvelope,
        (kind(D), f) is in the registry, and the registry's family for f is
        the namespace of the encrypt_* function that actually seals f,
    and the registry names no (kind, field) pair that is not such a pair.

Both sides are derived here, never restated. The field set comes from
`dataclasses.fields` + `typing.get_type_hints` over storage_models; the entity
`kind` from an AST scan of the storage adapters' read/list call sites; the
family from an AST scan of the `encrypt_byok_secret` / `encrypt_control_secret`
call sites. Reflection over the dataclasses is the load-bearing half: a row
body is written from a dataclass, so an envelope reaching storage through any
helper still has to be a field on one of those classes.

Why this is a proof and not a test. The registry is exactly right today — every
caller of the two encrypt functions was walked by hand and maps to precisely
those three fields. The failure mode is not "the registry is wrong", it is
"someone adds a fourth place an envelope lives". `_process_row` only ever looks
at the fields `_fields_for_kind` names, so an unregistered field is never
counted, never migrated, and never reported: the audit run comes back clean and
all-v2 while v1 envelopes sit in the rows it just walked past. That clean audit
result is what signed off step 3 on AWS and Azure. A migration declaring itself
finished with v1 envelopes still in storage is the entire reason this module
exists.

The family half is the same defect class in a sharper form. A wrong family does
not skip the field, it re-seals it under the wrong namespace. The v1 unwrap
still succeeds (v1 AAD has no namespace component at all — see
`_envelope_aad`), so the backfill sees a successful decrypt, a successful
re-encrypt, and a successful verification, and reports the row migrated. The
application then reads it back through the other namespace and gets InvalidTag
for a secret that is now unrecoverable. Cross-family substitution is precisely
what this whole migration exists to remove, so writing it back in during the
migration would be the worst available outcome.

Near-miss recorded against this module's own first draft, because it is the
same mistake in miniature. `_derived_pairs` originally built its set by
iterating the derived kinds and the derived families directly. A field with
neither — exactly what a newly added, not-yet-wired envelope field looks like —
therefore contributed no pair at all, and the totality assertion passed on it.
The check was verified by adding an unregistered `EncryptedSecretEnvelope`
field to `BroadcastDestination`: two guards fired, but the law itself stayed
green. A proof whose central assertion is silent on its motivating case is not
a proof, so unresolvable halves now surface as the `UNPLACED_KIND` /
`UNSEALED_FAMILY` sentinels and read as locations the registry does not cover.

Finding recorded here because it refutes the assumption this module started
from — that `_MIGRATED_KINDS` is the one place a kind is listed. It is not, and
neither entity store reads it as a set. `SpannerEntityStore.scan` hardcodes
`kind IN ('broadcast_destination', 'byok')` into its SQL text and never
references the registry; `PostgresEntityStore.scan` binds `_MIGRATED_KINDS[0],
_MIGRATED_KINDS[1]` positionally against a two-placeholder IN list. Adding a
third kind to the registry today updates neither scan, so the backfill would
never fetch the rows it had just been taught to migrate, and would once again
report clean. The two store tests below pin scan coverage behaviourally,
against fakes that capture the statement actually issued, so that drift stops
the build. The source is deliberately left as it is: this module records the
coupling rather than burying it in a refactor.

Scope limit, stated plainly. This establishes that the registry's (kind, field,
family) set equals the set derivable from the storage dataclasses and the
sealing call sites. It does NOT establish that the derived set is the set of
envelopes that exist in production — a row written by an older schema, by a
non-Python writer, or under a kind whose dataclass has since been deleted is
invisible to this module and to the backfill alike, and no amount of reflection
over today's source can see it. It also does not check the AAD *context*
component (the provider slug, the broadcast purpose) beyond the one identity
pinned in `test_the_broadcast_context_helper_matches_the_service`; a context
mismatch is a different defect and has its own proof in
test_byok_aad_namespace_property.py.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import re
import sys
import types
import typing
from typing import Any

import pytest

import trusted_router
from tests.test_byok_aad_backfill import MemoryEntityStore, _v1_envelope
from trusted_router import storage_models
from trusted_router.byok_aad_backfill import (
    _MIGRATED_KINDS,
    BackfillRunner,
    PostgresEntityStore,
    SpannerEntityStore,
    _broadcast_context,
    _fields_for_kind,
)
from trusted_router.byok_crypto import (
    ALGORITHM_V2,
    NAMESPACE_CONTROL,
    NAMESPACE_PROVIDER,
    decrypt_byok_secret,
    decrypt_control_secret,
)
from trusted_router.config import Settings
from trusted_router.services.broadcast import broadcast_secret_context
from trusted_router.storage_models import EncryptedSecretEnvelope

SRC = pathlib.Path(trusted_router.__file__).parent

# The module under audit is excluded from every scan below. A derivation that
# reads the registry it is checking proves nothing.
BACKFILL_MODULE = "byok_aad_backfill.py"

# The entity-IO primitives that pair a literal `kind` with a storage_models
# class. Reads and lists name the class explicitly; writes pass an already-built
# instance, so they carry no class name to match on and are not scanned.
ENTITY_IO_FUNCTIONS = frozenset(
    {
        "read_entity",
        "read_entity_tx",
        "read_entity_from",
        "list_entities",
        "_read_entity",
        "_read_entity_tx",
        "_read_entity_from",
        "_list_entities",
    }
)

# The sealing functions and the AAD namespace each one writes.
SEALING_FUNCTIONS = {
    "encrypt_byok_secret": NAMESPACE_PROVIDER,
    "encrypt_control_secret": NAMESPACE_CONTROL,
}


# ---------------------------------------------------------------------------
# Derivation 1: which dataclass fields can hold an envelope.
# ---------------------------------------------------------------------------


def _admits_envelope(annotation: Any) -> bool:
    """Does this resolved annotation admit an `EncryptedSecretEnvelope`?

    Recursing through `get_args` covers `X | None` and any other generic that
    wraps the envelope. A container-wrapped envelope (a list or dict of them)
    would be reported here too, and deliberately so: the backfill handles a
    single envelope per field and would silently skip a collection, which is
    the same hole under a different shape.
    """
    if annotation is EncryptedSecretEnvelope:
        return True
    return any(_admits_envelope(arg) for arg in typing.get_args(annotation))


def envelope_fields(module: types.ModuleType) -> dict[str, tuple[str, ...]]:
    """Model name -> its envelope-bearing field names, by reflection."""
    found: dict[str, tuple[str, ...]] = {}
    for obj in vars(module).values():
        if not (isinstance(obj, type) and dataclasses.is_dataclass(obj)):
            continue
        if obj.__module__ != module.__name__:
            continue
        hints = typing.get_type_hints(obj)
        fields = tuple(
            field.name for field in dataclasses.fields(obj) if _admits_envelope(hints[field.name])
        )
        if fields:
            found[obj.__name__] = fields
    return found


# ---------------------------------------------------------------------------
# Derivation 2: the entity `kind` each model is persisted under.
# ---------------------------------------------------------------------------


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _python_sources(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if p.name != BACKFILL_MODULE)


def entity_kinds(root: pathlib.Path, model_names: frozenset[str]) -> dict[str, set[str]]:
    """Model name -> the literal entity kinds it is read back under."""
    found: dict[str, set[str]] = {}
    for path in _python_sources(root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _called_name(node) not in ENTITY_IO_FUNCTIONS:
                continue
            literals = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            named = [arg.id for arg in node.args if isinstance(arg, ast.Name)]
            named += [
                keyword.value.id
                for keyword in node.keywords
                if keyword.arg == "cls" and isinstance(keyword.value, ast.Name)
            ]
            for name in named:
                if name in model_names:
                    found.setdefault(name, set()).update(literals)
    return found


# ---------------------------------------------------------------------------
# Derivation 3: which sealing function writes each field.
# ---------------------------------------------------------------------------


def _bound_names(targets: list[ast.expr]) -> list[str]:
    """The field-ish names an assignment target binds.

    Covers the three shapes the routes actually use: a local (`encrypted_secret
    = ...`), an attribute (`destination.encrypted_api_key = ...`) and a
    string-keyed subscript (`patch["encrypted_headers"] = ...`).
    """
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
        ):
            names.append(target.slice.value)
        elif isinstance(target, ast.Tuple | ast.List):
            names.extend(_bound_names(list(target.elts)))
    return names


def _families_sealed_in(expression: ast.expr) -> set[str]:
    families: set[str] = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Call):
            family = SEALING_FUNCTIONS.get(_called_name(node) or "")
            if family is not None:
                families.add(family)
    return families


def sealed_families(root: pathlib.Path, field_names: frozenset[str]) -> dict[str, set[str]]:
    """Field name -> the AAD namespaces its value is actually sealed under.

    A value counts as sealing `f` when a call to one of the two encrypt
    functions appears anywhere inside the expression bound to `f` — which is
    what makes the `encrypt_control_secret(...) if headers else None` shape in
    the broadcast routes visible rather than a blind spot.
    """
    found: dict[str, set[str]] = {}
    for path in _python_sources(root):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            bindings: list[tuple[str, ast.expr]] = []
            if isinstance(node, ast.Assign):
                bindings = [(name, node.value) for name in _bound_names(node.targets)]
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
                bindings = [(name, node.value) for name in _bound_names([node.target])]
            elif isinstance(node, ast.keyword) and node.arg is not None:
                bindings = [(node.arg, node.value)]
            elif isinstance(node, ast.Dict):
                bindings = [
                    (key.value, value)
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                ]
            for name, expression in bindings:
                if name not in field_names:
                    continue
                families = _families_sealed_in(expression)
                if families:
                    found.setdefault(name, set()).update(families)
    return found


# ---------------------------------------------------------------------------
# The derived source of truth, and the registry restated from the module.
# ---------------------------------------------------------------------------

DERIVED_MODEL_FIELDS = envelope_fields(storage_models)
DERIVED_FIELD_NAMES = frozenset(name for fields in DERIVED_MODEL_FIELDS.values() for name in fields)
DERIVED_KINDS = entity_kinds(SRC, frozenset(DERIVED_MODEL_FIELDS))
DERIVED_FAMILIES = sealed_families(SRC, DERIVED_FIELD_NAMES)


def _registry_pairs() -> set[tuple[str, str, str]]:
    return {
        (kind, field, family)
        for kind in _MIGRATED_KINDS
        for field, family in _fields_for_kind(kind)
    }


# Sentinels for the two halves of a location the scans could not resolve. They
# exist because the first version of `_derived_pairs` iterated over the derived
# kinds and families directly, so a field with neither contributed NO pair and
# the law below passed on it — vacuously green on precisely the case the law is
# for. Emitting a sentinel instead makes an unresolvable location present itself
# as a location the registry does not cover, which is what it is.
UNPLACED_KIND = "<no entity kind derivable>"
UNSEALED_FAMILY = "<no sealing call site>"


def _derived_pairs() -> set[tuple[str, str, str]]:
    pairs: set[tuple[str, str, str]] = set()
    for model, fields in DERIVED_MODEL_FIELDS.items():
        kinds = DERIVED_KINDS.get(model) or {UNPLACED_KIND}
        for field in fields:
            families = DERIVED_FAMILIES.get(field) or {UNSEALED_FAMILY}
            for kind in kinds:
                for family in families:
                    pairs.add((kind, field, family))
    return pairs


def _resolved_pairs() -> set[tuple[str, str, str]]:
    """The derived locations that both scans could place. The round trip below
    can only exercise these; the unresolved ones fail the law instead."""
    return {
        (kind, field, family)
        for kind, field, family in _derived_pairs()
        if kind != UNPLACED_KIND and family != UNSEALED_FAMILY
    }


def _registry_kind_literals() -> set[str]:
    """The kinds `_fields_for_kind` will answer for, read off its own source.

    Calling it cannot enumerate them — it raises for everything else — so the
    "no stale kind" direction needs the literals it compares against.
    """
    source = pathlib.Path(trusted_router.__file__).parent / BACKFILL_MODULE
    tree = ast.parse(source.read_text(), filename=str(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_fields_for_kind":
            return {
                comparator.value
                for compare in ast.walk(node)
                if isinstance(compare, ast.Compare)
                and isinstance(compare.left, ast.Name)
                and compare.left.id == "kind"
                for comparator in compare.comparators
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
            }
    raise AssertionError("_fields_for_kind is no longer a module-level function")


# ---------------------------------------------------------------------------
# The law.
# ---------------------------------------------------------------------------


def test_every_envelope_bearing_model_resolves_to_exactly_one_kind() -> None:
    """A model the scan cannot place is a model the backfill cannot reach.

    Failing closed here matters more than the count: a new envelope-bearing
    dataclass that is only ever written, never read back through a typed
    `read_entity`, would otherwise contribute nothing to the derived set and
    the totality assertion below would pass while the field went unmigrated.
    """
    unplaced = sorted(name for name in DERIVED_MODEL_FIELDS if not DERIVED_KINDS.get(name))
    assert not unplaced, (
        f"envelope-bearing model(s) {unplaced} are never read back through a typed "
        f"entity-IO call, so no entity kind can be derived for them. Either the "
        f"storage adapter reads them some other way (extend ENTITY_IO_FUNCTIONS) or "
        f"they are unreachable rows — decide which before trusting any audit run."
    )
    ambiguous = {name: sorted(kinds) for name, kinds in DERIVED_KINDS.items() if len(kinds) > 1}
    assert not ambiguous, (
        f"model(s) persisted under more than one kind: {ambiguous}. `_fields_for_kind` "
        f"is keyed by a single kind, so one of them would be skipped."
    )


def test_every_envelope_field_has_exactly_one_sealing_family() -> None:
    """Every envelope field must be sealed, and by only one of the two families.

    A field with no sealing call site is either dead or written through an
    indirection this scan cannot see; either way the family in the registry is
    then an unchecked assertion rather than a derived fact, and re-encrypting
    under a guessed namespace is how a secret becomes unrecoverable.
    """
    unsealed = sorted(name for name in DERIVED_FIELD_NAMES if not DERIVED_FAMILIES.get(name))
    assert not unsealed, (
        f"envelope field(s) {unsealed} have no encrypt_byok_secret / "
        f"encrypt_control_secret call site anywhere in {SRC.name}. The backfill "
        f"would re-seal them under whichever namespace the registry happens to "
        f"claim, with nothing checking that claim."
    )
    mixed = {name: sorted(f) for name, f in DERIVED_FAMILIES.items() if len(f) > 1}
    assert not mixed, (
        f"field(s) sealed under more than one namespace: {mixed}. One AAD family per "
        f"field is what makes the backfill's re-encryption decidable."
    )


def test_the_registry_covers_every_envelope_location_exactly() -> None:
    """The law. No missing entries, no stale ones.

    This is the assertion that prevents the defect class. The rest of the
    module checks that today's three locations behave; this one checks
    tomorrow's fourth.
    """
    derived = _derived_pairs()
    registry = _registry_pairs()

    missing = sorted(derived - registry)
    assert not missing, (
        f"the backfill registry does not cover {len(missing)} envelope location(s): "
        + ", ".join(f"{kind}.{field} (family {family})" for kind, field, family in missing)
        + ". `_process_row` only walks the fields `_fields_for_kind` names, so these "
        "would keep their v1 envelopes while the audit reported clean. Add them to "
        "`_fields_for_kind` with the namespace their encrypt_* call site uses — and "
        "check `_broadcast_context` can build a purpose for any new control field."
    )

    stale = sorted(registry - derived)
    assert not stale, (
        f"the backfill registry names {len(stale)} location(s) that no storage "
        "dataclass and sealing call site support: "
        + ", ".join(f"{kind}.{field} (family {family})" for kind, field, family in stale)
        + ". A stale entry either does nothing or re-seals a field under a namespace "
        "the application no longer reads it with."
    )


def test_migrated_kinds_agrees_with_fields_for_kind_and_with_the_models() -> None:
    """The two hand-written tables must name the same kinds as each other.

    They are independent literals: `_MIGRATED_KINDS` drives the Postgres scan,
    `_fields_for_kind` drives what gets migrated. A kind in one and not the
    other is a scan that fetches rows nothing knows how to process, or a
    processor for rows nothing fetches.
    """
    derived = {kind for kinds in DERIVED_KINDS.values() for kind in kinds}
    assert set(_MIGRATED_KINDS) == derived, (
        f"_MIGRATED_KINDS is {sorted(_MIGRATED_KINDS)} but the storage dataclasses "
        f"live under {sorted(derived)}"
    )
    assert _registry_kind_literals() == set(_MIGRATED_KINDS), (
        f"_fields_for_kind answers for {sorted(_registry_kind_literals())} but "
        f"_MIGRATED_KINDS lists {sorted(_MIGRATED_KINDS)}"
    )


@pytest.mark.parametrize("kind", ["api_key", "workspace", "generation", "byok_provider"])
def test_fields_for_kind_refuses_an_unregistered_kind(kind: str) -> None:
    """Fail closed. A scan that ever widens must not silently migrate nothing
    for the new kind; it must stop."""
    with pytest.raises(ValueError, match="unsupported entity kind"):
        _fields_for_kind(kind)


# ---------------------------------------------------------------------------
# The families, checked by round trip rather than by string equality.
# ---------------------------------------------------------------------------

WORKSPACE = "workspace-registry-totality"
PROVIDER = "anthropic"


def _entity_id_for(family: str) -> str:
    return f"{WORKSPACE}#{PROVIDER}" if family == NAMESPACE_PROVIDER else "bdst_totality"


def _context_for(family: str, entity_id: str, field: str) -> str:
    if family == NAMESPACE_PROVIDER:
        return PROVIDER
    # The purpose the application uses is the field name minus its prefix; the
    # identity between that and the backfill's private copy is pinned below.
    return broadcast_secret_context(entity_id, field.removeprefix("encrypted_"))


@pytest.mark.parametrize(("kind", "field", "family"), sorted(_resolved_pairs()))
def test_each_derived_location_round_trips_through_the_backfill(
    kind: str, field: str, family: str
) -> None:
    """Seal v1 as the application would, migrate, reopen as the application would.

    The reopen uses the family derived from the *call sites*, not the family in
    the registry, which is what makes this a check on the registry rather than
    a restatement of it.

    Both family flips were run against this test. Each surfaced as a hard
    failure inside `_migrate_envelope` rather than as a silently unreadable
    envelope: the provider branch demands a `provider` field the broadcast body
    has not got, and the control branch demands a purpose suffix
    `_broadcast_context` has no entry for. So with today's two body shapes a
    wrong family fails closed, and the `envelopes_migrated` assertion is what
    actually catches it. The decrypt below is the guard for the shape that does
    not fail closed — a body carrying both a provider slug and a broadcast
    purpose, where a wrong family would migrate cleanly and report success.
    """
    settings = Settings(environment="test")
    secret = "registry-totality-probe-secret"  # noqa: S105 - synthetic crypto fixture
    entity_id = _entity_id_for(family)
    context = _context_for(family, entity_id, field)

    body: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        field: _v1_envelope(secret, settings, workspace_id=WORKSPACE, context=context),
    }
    if family == NAMESPACE_PROVIDER:
        body["provider"] = PROVIDER
    store = MemoryEntityStore({(kind, entity_id): body})

    stats = BackfillRunner(
        store,
        settings=settings,
        apply=True,
        kms_operations_per_second=1000,
        reporter=lambda _message: None,
        sleep=lambda _seconds: None,
    ).run()

    assert stats.envelopes_migrated == 1, (
        f"{kind}.{field} was not migrated (failures={stats.failures}, "
        f"unsupported={stats.unsupported_algorithms}); the registry's family for it "
        f"disagrees with the {family} namespace its call sites seal under"
    )
    raw = store.rows[(kind, entity_id)][field]
    assert raw["algorithm"] == ALGORITHM_V2

    envelope = EncryptedSecretEnvelope(**raw)
    if family == NAMESPACE_PROVIDER:
        opened = decrypt_byok_secret(envelope, settings, workspace_id=WORKSPACE, provider=context)
    else:
        opened = decrypt_control_secret(envelope, settings, workspace_id=WORKSPACE, purpose=context)
    assert opened == secret, (
        f"{kind}.{field} re-sealed under a namespace the application does not read "
        f"it with; that secret is now unrecoverable"
    )


@pytest.mark.parametrize(
    "field",
    sorted(
        name for name in DERIVED_FIELD_NAMES if DERIVED_FAMILIES.get(name) == {NAMESPACE_CONTROL}
    ),
)
def test_the_broadcast_context_helper_matches_the_service(field: str) -> None:
    """`_broadcast_context` is a copy of `broadcast_secret_context`, kept so an
    offline migration need not import the application-global STORE. The comment
    in the source claims the two are byte-identical; a copy nobody compares is
    a divergence waiting to happen, and a divergent purpose re-seals a control
    secret against an AAD the reader will never reconstruct."""
    destination_id = "bdst_context_identity"
    assert _broadcast_context(destination_id, field) == broadcast_secret_context(
        destination_id, field.removeprefix("encrypted_")
    )


# ---------------------------------------------------------------------------
# Scan coverage: a registered kind nothing fetches is not covered.
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def __enter__(self) -> _FakeSnapshot:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute_sql(self, sql: str, params: Any = None, param_types: Any = None) -> list[Any]:
        self._captured["sql"] = sql
        self._captured["params"] = params
        return []


class _FakeSpannerDatabase:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def snapshot(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._captured)


class _FakeParamTypes:
    STRING = "STRING"
    INT64 = "INT64"


class _FakeCursor:
    def fetchall(self) -> list[Any]:
        return []


class _FakeConnection:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self._captured["sql"] = sql
        self._captured["params"] = params
        return _FakeCursor()


def _kind_in_clause(sql: str) -> str:
    match = re.search(r"kind IN \(([^)]*)\)", sql)
    assert match is not None, f"no `kind IN (...)` filter in the scan statement: {sql}"
    return match.group(1)


def test_the_spanner_scan_fetches_every_registered_kind() -> None:
    """`SpannerEntityStore.scan` embeds its kind filter as SQL text and never
    reads `_MIGRATED_KINDS`. Registering a kind without editing that literal
    means the backfill never sees the rows — and reports clean, because a row
    it never scanned cannot be counted as a v1 envelope."""
    captured: dict[str, Any] = {}
    SpannerEntityStore(_FakeSpannerDatabase(captured), _FakeParamTypes()).scan(after=None, limit=10)

    filtered = set(re.findall(r"'([^']*)'", _kind_in_clause(captured["sql"])))
    assert filtered == set(_MIGRATED_KINDS), (
        f"the Spanner scan filters on {sorted(filtered)} but the registry covers "
        f"{sorted(_MIGRATED_KINDS)}"
    )


def test_the_postgres_scan_binds_every_registered_kind() -> None:
    """`PostgresEntityStore.scan` binds `_MIGRATED_KINDS[0], _MIGRATED_KINDS[1]`
    positionally against a two-placeholder IN list. It is correct only because
    the registry happens to hold exactly two kinds; a third would be dropped in
    silence. This test is what makes that arity a checked property."""
    captured: dict[str, Any] = {}
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda _dsn: _FakeConnection(captured)  # type: ignore[attr-defined]
    original = sys.modules.get("psycopg")
    sys.modules["psycopg"] = fake_psycopg
    try:
        PostgresEntityStore("dbname=totality").scan(after=None, limit=10)
    finally:
        if original is None:
            del sys.modules["psycopg"]
        else:
            sys.modules["psycopg"] = original

    placeholders = _kind_in_clause(captured["sql"]).count("%s")
    assert placeholders == len(_MIGRATED_KINDS), (
        f"the Postgres scan has {placeholders} kind placeholder(s) for a registry of "
        f"{len(_MIGRATED_KINDS)} kind(s); the surplus kinds are never fetched"
    )
    bound = set(captured["params"])
    unbound = sorted(set(_MIGRATED_KINDS) - bound)
    assert not unbound, f"registered kind(s) {unbound} are never bound into the scan"


# ---------------------------------------------------------------------------
# Negative controls. A derivation that cannot fail derives nothing.
# ---------------------------------------------------------------------------


def test_the_reflection_detects_a_new_envelope_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, a typo in `_admits_envelope` makes the whole module green
    and the next unregistered field ships."""
    module = types.ModuleType("tests._synthetic_storage_models")
    module.__dict__["EncryptedSecretEnvelope"] = EncryptedSecretEnvelope
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(  # noqa: S102 - a synthetic module is the point of the control
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Probe:\n"
        "    plain: str = ''\n"
        "    encrypted_probe: EncryptedSecretEnvelope | None = None\n"
        "    nested_probe: list[EncryptedSecretEnvelope] | None = None\n",
        module.__dict__,
    )

    assert envelope_fields(module) == {"Probe": ("encrypted_probe", "nested_probe")}


def test_the_ast_scans_detect_a_new_kind_and_a_new_sealing_site(
    tmp_path: pathlib.Path,
) -> None:
    """Negative control for both AST derivations at once."""
    (tmp_path / "adapter.py").write_text(
        "def load(io, probe_id):\n    return io.read_entity('probe_kind', probe_id, ProbeModel)\n"
    )
    (tmp_path / "route.py").write_text(
        "def seal(raw, settings, workspace_id):\n"
        "    patch = {}\n"
        "    patch['encrypted_probe'] = encrypt_control_secret(\n"
        "        raw, settings, workspace_id=workspace_id, purpose='p'\n"
        "    )\n"
        "    return patch\n"
    )

    assert entity_kinds(tmp_path, frozenset({"ProbeModel"})) == {"ProbeModel": {"probe_kind"}}
    assert sealed_families(tmp_path, frozenset({"encrypted_probe"})) == {
        "encrypted_probe": {NAMESPACE_CONTROL}
    }


def test_the_derivation_reproduces_todays_registry() -> None:
    """A pin on what the derivation currently yields.

    Not a substitute for the law above — it is a regression guard on the
    derivation itself, so a scan that quietly stops finding anything shows up
    as this test failing rather than as the law passing vacuously.
    """
    assert DERIVED_MODEL_FIELDS == {
        "ByokProviderConfig": ("encrypted_secret",),
        "BroadcastDestination": ("encrypted_api_key", "encrypted_headers"),
    }
    assert DERIVED_KINDS == {
        "ByokProviderConfig": {"byok"},
        "BroadcastDestination": {"broadcast_destination"},
    }
    assert DERIVED_FAMILIES == {
        "encrypted_secret": {NAMESPACE_PROVIDER},
        "encrypted_api_key": {NAMESPACE_CONTROL},
        "encrypted_headers": {NAMESPACE_CONTROL},
    }
