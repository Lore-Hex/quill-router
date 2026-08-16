"""Proof that the BYOK AAD v2 backfill's envelope registry is total.

`byok_aad_backfill` decides what to migrate from two hand-written tables:
`_MIGRATED_KINDS` and `_fields_for_kind`. Between them they name three
locations — byok.encrypted_secret, broadcast_destination.encrypted_api_key and
broadcast_destination.encrypted_headers — and tag each with the AAD namespace
("provider" or "control") its envelope was sealed under. The law:

    for every dataclass D the package defines, and every field f of D whose
    RESOLVED annotation admits an EncryptedSecretEnvelope,
        (kind(D), f) is in the registry, and the registry's family for f is
        the namespace of the encrypt_* function that actually seals f,
    and the registry names no (kind, field) pair that is not such a pair.

The right-hand side is derived, not restated. The field set comes from
`dataclasses.fields` + `typing.get_type_hints` over every dataclass in every
module of the package; the entity `kind` from an AST scan of the typed read call
sites, with the kind argument resolved through the calling module's own
namespace; the family from an AST scan of the sealing call sites, where the
sealing functions and the namespace each one uses are themselves read off
`byok_crypto`'s AST rather than written down here.

Resolved is the load-bearing word, and it is the correction the second
adversarial review forced. The previous version reflected only storage_models
and policed that scope with a scan for the literal substring
"EncryptedSecretEnvelope" in unparsed annotation text. `SealedKey: TypeAlias =
EncryptedSecretEnvelope | None` walked past it, and so did `import ... as
_Envelope`; a complete envelope-bearing persisted model, with a real
`encrypt_control_secret` call and a real unregistered kind, passed the whole
file. Reflection over resolved annotations sees an alias, a renamed import, a
quoted forward reference and a subclass as the one type they all are, and
`envelope_fields` now covers the whole package, so where the model is declared
no longer decides whether it is seen. Both variants were re-run against this
version: each now fails the central law by name.

One shape of that evasion still WORKS, and it is the same attack with the class
moved one level in. Reflection reaches classes through `vars(module)`, so a
dataclass defined inside a FUNCTION BODY is not a module attribute and
`envelope_fields` never sees it — alias or no alias. Rebuilt and re-measured at
this commit: the `TypeAlias` model, unchanged except for being declared inside
`def load_signing(...)`, is invisible to the law. Whether anything else fires
depends on the writer, not on the model: with the `encrypt_control_secret` call
in the same function as the `write_entity`, the adjacency guard still names the
unregistered kind (2 failed); with the sealing hoisted into a helper and the
write taking the record as `Any`, the whole file passes at 25. And spelling the
envelope out in full inside that function-local class trips only the convention
check `test_every_envelope_typed_attribute_lives_in_storage_models` — never the
law. So the type-indirection class is narrowed, not eliminated: what remains is
a function-local class whose writer mentions no envelope vocabulary. The scope
limit below states the general form.

What IS restated, in full, because a hand-maintained list this file did not
disclose was itself a review finding:

  - the names of the entity-IO primitives (`ENTITY_READ_FUNCTIONS`,
    `ENTITY_IO_FUNCTIONS`) — the vocabulary the AST scans need. An adapter that
    persisted through a primitive named here would be seen; one that used some
    other name would not. On the read side that fails closed, because a model
    with no recognised read gets no kind and the law reports it as uncovered.
    On the write side it does not: see the scope limit below.
  - `NON_ENVELOPE_KINDS`, one entry today, and `LOOSELY_TYPED_FIELDS`, two.
    Both are declarations with reasons and both are asserted in both
    directions, so an entry that stops being true fails the build.
  - `_OPAQUE_CONTAINERS`, the five builtin container types `_admits_any` calls
    opaque when they carry no parameters. A container outside it is neither
    flagged as loosely typed nor reflected as an envelope: measured, both
    `_admits_any(collections.deque)` and `_admits_any(types.SimpleNamespace)`
    are False, so a persisted field annotated with either is invisible the way
    a bare `dict` was before the third review. Named here because the previous
    version of this paragraph claimed to be complete and was not.
  - the AST shapes each name scan understands, written where that scan is
    rather than gathered in one place. The two that decide what the law sees
    are `_bound_names`' assignment-target shapes and `_kind_argument`'s three
    kind-argument shapes; `_read_call_classes` has one of its own and reads a
    model class only from a bare name, which fails closed by leaving the model
    unplaced. Each is a floor on what a name scan can see and each says so
    where it is written. A target outside `_bound_names` contributes no name,
    so the field it binds is left with no sealing site and the law reports it
    as `UNSEALED_FAMILY`. An argument outside `_kind_argument` yields None, and
    what None costs depends on the caller: in `envelope_adjacent_kinds` it
    fails the build as an unresolvable call site, while in `entity_kinds` and
    `persisted_dataclasses` the read is skipped, so the model reaches the law
    as `UNPLACED_KIND` and drops out of the domain of the loosely typed check —
    narrowing that check rather than failing it.
  - the type shapes `_admits_envelope` understands: a subclass check,
    `__supertype__`, and recursion through `get_args`. Not an AST scan — it
    reads the object `typing.get_type_hints` produced and returns a bool, which
    is why an alias, a renamed import or a quoted annotation does not defeat it
    — but a wrapper that reaches the envelope through none of those three is
    reported False, and the field is invisible to the law rather than failing
    it.
  - the pin in `test_the_derivation_reproduces_todays_registry`, a guard
    against a derivation that quietly stops finding anything.

`ENVELOPE_ADAPTER_MODULES`, one more hand-maintained list, used to be here too:
two file names, so the identical write-only-kind defect in storage_postgres.py
was invisible. Review was right that this file criticised `_MIGRATED_KINDS` for
exactly that and then did it. It is gone — `envelope_adjacent_kinds` derives its
own scope, package-wide — and what replaced it is pinned rather than trusted.

That replacement is a TRADE, not a pure gain, and the first version of this
paragraph presented it as a pure gain. The old list took every kind named
anywhere in those two files; the new derivation takes only the kinds named by a
scope that mentions envelope vocabulary. It is wider in file scope — any module
in the package now qualifies — and narrower within the two original files. Two
real kinds left coverage that way: `broadcast_delivery` and
`broadcast_delivery_due`, both written by `_write_delivery` in
storage_gcp_broadcast.py, a method that names no envelope model, field, sealer
or id helper. That is why `NON_ENVELOPE_KINDS` shrank from three entries to one
— the two that went are out of scope, not proven safe — and why a new
`write_entity("broadcast_delivery_archive", ...)` beside them passes this file
today where the old list would have caught it. Measured both ways against both
versions, not reasoned about. The cost is bounded by what those functions
handle: `BroadcastDeliveryJob` has no envelope-typed field, but its `settle_body`
is the loosely typed field `LOOSELY_TYPED_FIELDS` documents, so this is the same
hole as the settle_body refutation approached from the other side.

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
not skip the field, it re-seals it under the wrong namespace: the v1 unwrap
still succeeds, because v1 AAD has no namespace component at all (see
`_envelope_aad`), so nothing inside the backfill objects and the application
later reads the row back through the other namespace and gets InvalidTag for a
secret that is now unrecoverable.

Measured rather than assumed, because the first draft of this paragraph
asserted that outcome as inevitable and it is not. Both family flips were run
against this module: with today's two body shapes each one fails CLOSED inside
`_migrate_envelope` — the provider branch demands a `provider` field the
broadcast body has not got, and the control branch demands a purpose suffix
`_broadcast_context` has no entry for. So today the `envelopes_migrated`
assertion is what catches a wrong family, and the decrypt beside it is the
guard for a future body carrying both context fields, where the silent
corruption above becomes reachable.

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
report clean. The two store tests below capture the statement each scan
actually issues and check the registry kinds against it. Calling that
"behavioural" would be an overclaim, and the first draft did: the fakes return
no rows, so nothing about retrieval, decoding or pagination is exercised. What
they establish is narrower and still worth having — adding a kind to the
registry without editing the SQL text and the placeholder count stops the
build. The source is deliberately left as it is: this module records the
coupling rather than burying it in a refactor.

Refutation, from an adversarial review of this module, of its own central
claim. Reflection follows types, and `dict[str, Any]` is the absence of one.
`BroadcastDeliveryJob.settle_body` is `dict[str, Any]`, is persisted under kind
'broadcast_delivery', and is NOT scanned by the backfill. An
`EncryptedSecretEnvelope` nested inside it serialises straight through
`storage_codec.json_body`'s recursive `asdict` into the stored row — verified
directly, not argued. So the claim "an envelope reaching storage must be a
field this reflection finds" is false, and the law proves totality over typed
fields, not over persisted bytes. `test_every_loosely_typed_persisted_field_is_classified`
holds the list of such shapes at two so it cannot grow in silence — as far as
`_OPAQUE_CONTAINERS` reaches, which is the five builtins and no further; closing
the hole properly needs the backfill to walk bodies rather than named fields,
which is a change to the migration, not to a test.

The second review sharpened that finding twice, and both corrections are in the
code rather than in this paragraph. The classifier matched only `Any` and
`object`, so `signing_material: dict` — one token shorter than the shape it did
catch, and no less opaque — was invisible to it AND to the envelope reflection,
which means the count really was wrong, not just the sentence. Unparameterized
containers now count. And the domain was `vars(storage_models)`, which is the
hand-scoping mistake again in miniature; it is now every dataclass the package
reads back under an entity kind, unioned with storage_models. That widening
resolves 26 models and still finds exactly the two fields listed, so nothing was
declared away to make it fit.

Scope limit, stated plainly. This establishes that the registry's (kind, field,
family) set equals the set derivable from the package's dataclasses and the
sealing call sites. It does NOT establish:

  - that the derived set is the set of envelopes that exist in production. A
    row written by an older schema, by a non-Python writer, or under a kind
    whose dataclass has since been deleted is invisible to this module and to
    the backfill alike, and no reflection over today's source can see it.
  - totality over loosely typed bodies, per the refutation above.
  - that every kind an envelope-bearing model can be WRITTEN to is registered.
    `envelope_adjacent_kinds` sees an entity-IO call when the SCOPE around it —
    a function, or the per-file `<module>` scope that owns class bodies and
    module-level lambdas — mentions an envelope-bearing model class, an envelope
    field name, a sealing function, or an id helper reached from one of those.
    That is enough for the three write-only-kind evasions review found: a kind
    hoisted to a module constant, the same write moved to storage_postgres.py
    (which the id helper `byok_id` pulls in), and a write inside a module-level
    `lambda` in a dispatch dict. All three were re-run against this version and
    fail with the site named. Three is the count for THIS bullet only: four
    rounds of review found six ways past earlier versions of this module
    altogether, and the other three were about the envelope-field derivation
    rather than the write-only-kind scope. All six are enumerated in the
    docstring of
    `test_every_kind_an_envelope_handling_function_names_is_registered_or_declared`.
    It is NOT enough for a scope that persists an
    envelope-bearing object while mentioning none of that vocabulary: taking the
    object as `Any` and building the row id inline still passes, verified by
    construction. That residue is a name scan's floor, not an oversight; closing
    it needs the type of each written value, which is dataflow. And per the
    trade recorded above, it is a real loss against the module-scoped list this
    replaced, which would have caught such a write inside its two files.
  - that a kind resolved from a NAME is the kind that call site really uses,
    beyond the shadowing `_shadowed_names` rejects. In a FUNCTION scope, a name
    the scope binds as a `Store` name or a parameter yields None and the site
    is reported unresolvable. In the `<module>` scope the module namespace is
    the right answer for a module-level assignment, so what shadows there is
    the scopes nested inside it — a lambda, a comprehension target, and a class
    body, the last of which the fourth review found still resolving through the
    module. `_shadowed_names` holds the full enumeration of both. Three shapes
    are still read as whatever the imported module holds at test time: a name
    bound by a `def`, `class` or `import` statement rather than by an
    assignment, a name rebound at module level between definition and call, and
    a name that arrives through `import *`.
  - that every WRITER of a field agrees on its family. `sealed_families`
    aggregates by bare field name, so if one of the two modules writing
    `encrypted_secret` began sealing through a wrapper with the other family,
    the wrapper would contribute nothing and the union would still read
    {provider}. Detecting that needs dataflow analysis, not a name scan.
  - anything about a class that is not a module attribute. Resolved reflection
    reaches classes through `vars(module)`, so a dataclass defined inside a
    function body is seen only by the text scan beside it, and therefore only
    if its annotation spells the envelope out — and even then it fails only the
    convention check, never the law. This is the surviving half of the critical
    finding review opened this rework with, described in full at the top of this
    docstring; it is the reason that finding is called narrowed rather than
    closed.
  - anything about the AAD *context* component (the provider slug, the
    broadcast purpose) beyond the three-way identity pinned in
    `test_the_broadcast_context_helper_matches_the_service`. Context
    separation is a different defect with its own partial proof in
    test_byok_aad_namespace_property.py; neither module proves a context is
    correct, only that the copies of it agree.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pathlib
import pkgutil
import re
import sys
import types
import typing
from collections.abc import Iterable
from typing import Any

import pytest

import trusted_router
from tests.test_byok_aad_backfill import MemoryEntityStore, _v1_envelope
from trusted_router import byok_crypto, storage_models
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
from trusted_router.services.broadcast_adapters import _secret_context as adapter_secret_context
from trusted_router.services.user_model_secrets import (
    USER_MODEL_ENDPOINT_KEY_PURPOSE,
    USER_MODEL_SIGNING_PURPOSE,
)
from trusted_router.storage_models import EncryptedSecretEnvelope

SRC = pathlib.Path(trusted_router.__file__).parent

# The module under audit is excluded from every scan below. A derivation that
# reads the registry it is checking proves nothing.
BACKFILL_MODULE = "byok_aad_backfill.py"

# The entity-IO primitives that pair a literal `kind` with a storage_models
# class, and the argument position the kind occupies. Reads and lists name the
# class explicitly; writes pass an already-built instance, so they carry no
# class name to match on and are not scanned here.
#
# The position matters. The first draft accepted any positional string literal
# as the kind, so `read_entity(SOME_KIND, "byok", Cls)` would have derived the
# entity *id* as a kind. Adversarial review named that; pinning the index is
# the fix.
ENTITY_READ_FUNCTIONS = {
    "read_entity": 0,
    "list_entities": 0,
    "_read_entity": 0,
    "_list_entities": 0,
    "read_entity_tx": 1,
    "read_entity_from": 1,
    "_read_entity_tx": 1,
    "_read_entity_from": 1,
}

# Every primitive that names a kind, reads and writes alike. Used only for the
# adapter-kind classification below, which needs to see a kind an envelope is
# written to even when nothing ever reads it back through a typed call.
ENTITY_IO_FUNCTIONS = ENTITY_READ_FUNCTIONS | {
    "write_entity": 0,
    "delete_entities": 0,
    "_write_entity": 0,
    "write_entity_tx": 1,
    "write_entity_batch": 1,
    "delete_entities_tx": 1,
    "_write_entity_tx": 1,
    "_write_entity_batch": 1,
}


# ---------------------------------------------------------------------------
# The package, imported. Every scan below needs the module OBJECT, not just its
# text: an annotation is only a type once it has been evaluated in the
# namespace its author wrote it in, and a kind is only a string once the name
# bound to it has been looked up. Adversarial review broke the previous,
# text-only versions of both with one token of indirection each.
# ---------------------------------------------------------------------------


def _import_package_modules() -> dict[pathlib.Path, types.ModuleType]:
    """Source path -> imported module, for every module in the package.

    A module that fails to import is a hard error, not a skip. A skipped module
    is a blind spot, and a blind spot is the exact defect this file exists to
    remove.
    """
    found: dict[pathlib.Path, types.ModuleType] = {
        pathlib.Path(trusted_router.__file__ or "").resolve(): trusted_router
    }
    failures: dict[str, str] = {}
    for info in pkgutil.walk_packages(
        trusted_router.__path__, prefix=f"{trusted_router.__name__}."
    ):
        try:
            module = importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - any failure is a blind spot
            failures[info.name] = f"{type(exc).__name__}: {exc}"
            continue
        source = getattr(module, "__file__", None)
        if source:
            found[pathlib.Path(source).resolve()] = module
    if failures:
        raise AssertionError(
            f"these package modules could not be imported, so no reflection below can "
            f"see the envelopes they declare: {failures}"
        )
    return found


MODULES_BY_PATH = _import_package_modules()


def _module_for(path: pathlib.Path) -> types.ModuleType:
    """The imported module for a source file.

    Inside the package a missing module is an error, because a file scanned
    without its namespace is a file whose kind constants and annotations cannot
    be resolved — the weakness this rework exists to remove.
    `test_every_package_source_file_is_an_imported_module` keeps that total.

    Outside the package the negative controls below build synthetic trees on
    disk that were never imported. They get an empty namespace, which resolves
    nothing; that is only safe because no path under `SRC` can reach it.
    """
    module = MODULES_BY_PATH.get(path.resolve())
    if module is not None:
        return module
    if path.resolve().is_relative_to(SRC.resolve()):
        raise AssertionError(
            f"{path} is a package source with no imported module, so its annotations "
            "and kind constants cannot be resolved"
        )
    return types.ModuleType(f"<unimported {path.name}>")


# ---------------------------------------------------------------------------
# Derivation 1: which dataclass fields can hold an envelope.
# ---------------------------------------------------------------------------


def _admits_envelope(annotation: Any) -> bool:
    """Does this RESOLVED annotation admit an `EncryptedSecretEnvelope`?

    Resolved is the load-bearing word. The caller must hand this the object
    `typing.get_type_hints` produced, never the text the author typed, because
    every cheap way to hide an envelope from a text scan — `SealedKey:
    TypeAlias = EncryptedSecretEnvelope | None`, `import ... as _Envelope`, a
    quoted annotation, a re-export — evaluates to the same object here.

    `issubclass` rather than `is`, so a subclass of the envelope counts.
    Recursing through `get_args` covers `X | None`, `Annotated[...]` and any
    other generic that wraps the envelope; `__supertype__` covers `NewType`. A
    container-wrapped envelope (a list or dict of them) is reported too, and
    deliberately so: the backfill handles a single envelope per field and would
    silently skip a collection, which is the same hole under a different shape.
    """
    if isinstance(annotation, type) and issubclass(annotation, EncryptedSecretEnvelope):
        return True
    supertype = getattr(annotation, "__supertype__", None)
    if supertype is not None and _admits_envelope(supertype):
        return True
    return any(_admits_envelope(arg) for arg in typing.get_args(annotation))


def _own_annotated_names(cls: type) -> tuple[str, ...]:
    """The attribute names annotated on this class itself, not on its bases."""
    return tuple(vars(cls).get("__annotations__") or ())


def _package_classes(modules: Iterable[types.ModuleType]) -> list[tuple[types.ModuleType, type]]:
    """Every class each module defines, as a module attribute."""
    found: list[tuple[types.ModuleType, type]] = []
    for module in modules:
        for obj in vars(module).values():
            if isinstance(obj, type) and getattr(obj, "__module__", None) == module.__name__:
                found.append((module, obj))
    return found


def envelope_typed_attributes(
    modules: Iterable[types.ModuleType],
) -> set[tuple[str, str, str]]:
    """(module, qualname, attribute) for every attribute RESOLVING to an envelope.

    Reflection, not text. This is what makes the domain claim below survive type
    indirection: the annotation is evaluated in its own module's namespace, so
    an alias, a `TypeAlias`, a renamed import, a quoted forward reference and a
    subclass all resolve to the same object and are all seen.

    An annotation that cannot be resolved raises rather than being skipped —
    an unresolvable annotation is indistinguishable from a hidden envelope.
    """
    found: set[tuple[str, str, str]] = set()
    unresolved: list[str] = []
    for module, cls in _package_classes(modules):
        own = _own_annotated_names(cls)
        if not own:
            continue
        try:
            hints = typing.get_type_hints(cls)
        except Exception as exc:  # noqa: BLE001 - unresolvable is a blind spot
            unresolved.append(f"{module.__name__}:{cls.__qualname__} ({type(exc).__name__}: {exc})")
            continue
        for name in own:
            if name in hints and _admits_envelope(hints[name]):
                found.add((module.__name__, cls.__qualname__, name))
    if unresolved:
        raise AssertionError(
            "the annotations of these classes could not be resolved, so whether they "
            f"declare an EncryptedSecretEnvelope is unknown: {unresolved}"
        )
    return found


def envelope_annotated_attributes(root: pathlib.Path) -> set[tuple[str, str, str]]:
    """(module path, class name, attribute) for every envelope-SPELLING annotation.

    A textual complement to `envelope_typed_attributes`, kept for the one thing
    reflection cannot reach: a class that is not a module attribute, such as one
    defined inside a function body. It only ever sees the literal class name, so
    on its own it is defeated by any type indirection — which is precisely how
    adversarial review defeated the previous version of the domain guard, when
    this scan was the whole of it. The two are unioned, never substituted.
    """
    found: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign):
                    continue
                if "EncryptedSecretEnvelope" in ast.unparse(statement.annotation):
                    found.add(
                        (
                            path.relative_to(root).as_posix(),
                            node.name,
                            ast.unparse(statement.target),
                        )
                    )
    return found


def envelope_fields(modules: Iterable[types.ModuleType]) -> dict[str, tuple[str, ...]]:
    """Model name -> its envelope-bearing field names, by reflection.

    Over every dataclass the package defines, not just storage_models'. The
    previous version was scoped to storage_models and leaned on a text scan to
    police that scope; adversarial review walked straight past the text scan
    with a `TypeAlias` and landed a complete envelope-bearing persisted model in
    a new adapter module that no assertion in this file could see. Widening the
    reflection is what removes that class of evasion: wherever the model is
    declared, it now owes the registry a (kind, field, family).
    """
    found: dict[str, tuple[str, ...]] = {}
    homes: dict[str, str] = {}
    collisions: list[str] = []
    for module, obj in _package_classes(modules):
        if not dataclasses.is_dataclass(obj):
            continue
        hints = typing.get_type_hints(obj)
        fields = tuple(
            field.name for field in dataclasses.fields(obj) if _admits_envelope(hints[field.name])
        )
        if not fields:
            continue
        if obj.__name__ in found:
            collisions.append(f"{obj.__name__} ({homes[obj.__name__]} and {module.__name__})")
        found[obj.__name__] = fields
        homes[obj.__name__] = module.__name__
    if collisions:
        raise AssertionError(
            "two envelope-bearing dataclasses share a name, so the kind derivation "
            f"below cannot tell them apart: {collisions}"
        )
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


def _shadowed_names(nodes: Iterable[ast.AST], *, module_level: bool) -> frozenset[str]:
    """The names in this scope that a module-namespace lookup would resolve WRONGLY.

    `_kind_argument` resolves a bare name through the module namespace, and that
    lookup is wrong whenever the name is a local. Adversarial review found the
    fail-open it opens: with a module-level `KIND = "byok"` and a
    function-local `KIND = "byok_archive"` above the write, the scan resolved
    the archive write to the registered kind 'byok' and reported nothing at all.
    A name bound in the scope is therefore treated as unresolvable, so the call
    site surfaces in `UNRESOLVABLE_KIND_CALL_SITES` and fails the build instead
    of being silently mis-attributed to whatever the module happens to hold.

    What counts as a binding here is a `Store` name or a parameter: assignments,
    `for` and `with` targets, walrus, comprehension targets, and — because a
    function scope is passed as its full walk — the same inside any nested def
    or lambda. A `def`, `class` or `import` statement also binds a name, and
    those three are NOT read, so a kind argument shadowed by one of them still
    resolves through the module.

    In the `<module>` scope most bindings ARE the module namespace and the
    lookup is right, so only a scope nested inside it shadows: a lambda, a
    comprehension target, and a CLASS BODY. The class body is the fourth
    review's finding and it is the same fail-open one level in — a class-level
    `KIND = "byok_archive"` above a class-body `write_entity(KIND, ...)` is not
    a module attribute, so the module lookup answered "byok", the registered
    kind, and the scan reported nothing. Measured before the fix, not reasoned
    about. Nested `def` bodies inside a class are NOT read here: those names are
    locals of their own scope, which gets its own `_Scope` entry, and reading
    them would shadow module constants the class body resolves correctly. A
    lambda inside a class body IS read, by the same rule that reads one at
    module level.
    """
    shadowed: set[str] = set()
    for node in nodes:
        if module_level:
            if isinstance(node, ast.Lambda):
                inner: Iterable[ast.AST] = ast.walk(node)
            elif isinstance(node, ast.comprehension):
                inner = ast.walk(node.target)
            elif isinstance(node, ast.ClassDef):
                inner = _outside_functions(node)
            else:
                continue
        else:
            inner = (node,)
        for child in inner:
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                shadowed.add(child.id)
            elif isinstance(child, ast.arg):
                shadowed.add(child.arg)
    return frozenset(shadowed)


def _kind_argument(
    node: ast.Call,
    index: int,
    module: types.ModuleType,
    shadowed: frozenset[str] = frozenset(),
) -> str | None:
    """The kind at its declared argument position, or None if it is not a constant.

    Resolved through the calling module's own namespace, not just matched as a
    literal. The previous version accepted only `ast.Constant`, so hoisting a
    repeated kind to `ARCHIVE_KIND = "byok_archive"` — ordinary practice, and
    the variant adversarial review used — made the call site invisible to every
    scan here. `getattr` on the module covers a constant defined in the module,
    one imported into it by name, and `other_module.CONST`.

    That lookup can be WRONG rather than merely absent, which is the third
    review's finding: a local of the same name resolves to the module-level
    object and the scan then reports a real write under a different, possibly
    registered, kind. `shadowed` — the names the surrounding scope binds, from
    `_shadowed_names` — is what stops that; a shadowed name resolves to None
    here whatever the module happens to hold. Omitting it restores the old,
    trusting behaviour, so every scan below passes its scope's set; the single
    call that omits it is in the negative control, where the fail-open is pinned
    so this guard cannot be dropped in silence.

    None means "not resolvable to a string constant", which callers must treat
    as unknown rather than as absent; see `envelope_adjacent_kinds`.
    """
    if len(node.args) <= index:
        return None
    argument = node.args[index]
    if isinstance(argument, ast.Constant):
        return argument.value if isinstance(argument.value, str) else None
    if isinstance(argument, ast.Name):
        if argument.id in shadowed:
            return None
        value = getattr(module, argument.id, None)
        return value if isinstance(value, str) else None
    if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name):
        if argument.value.id in shadowed:
            return None
        base = getattr(module, argument.value.id, None)
        value = getattr(base, argument.attr, None) if base is not None else None
        return value if isinstance(value, str) else None
    return None


def _read_call_classes(node: ast.Call) -> list[str]:
    """The names passed to a typed read that could be the model class."""
    named = [arg.id for arg in node.args if isinstance(arg, ast.Name)]
    named += [
        keyword.value.id
        for keyword in node.keywords
        if keyword.arg == "cls" and isinstance(keyword.value, ast.Name)
    ]
    return named


def entity_kinds(root: pathlib.Path, model_names: frozenset[str]) -> dict[str, set[str]]:
    """Model name -> the entity kinds it is read back under.

    By scope rather than by file, because the kind is resolved through a
    namespace and only the scope knows which names in it are locals.
    """
    found: dict[str, set[str]] = {}
    for scope in _package_scopes(root):
        for node, _index, kind in _entity_io_calls(
            scope.nodes, scope.module, scope.shadowed, ENTITY_READ_FUNCTIONS
        ):
            if kind is None:
                continue
            for name in _read_call_classes(node):
                if name in model_names:
                    found.setdefault(name, set()).add(kind)
    return found


def persisted_dataclasses(root: pathlib.Path) -> set[type]:
    """Every dataclass the package reads back under an entity kind.

    The derived domain for the loosely typed field check below, which used to be
    the hand-written `vars(storage_models)`. Same shape of complaint as the
    hand-written adapter module list: a persisted model declared elsewhere would
    have gone unclassified. Measured, not assumed — this resolves 26 models
    today and the two loosely typed fields it finds are both in storage_models,
    so widening the domain cost no new declarations.
    """
    found: set[type] = set()
    for scope in _package_scopes(root):
        for node, _index, kind in _entity_io_calls(
            scope.nodes, scope.module, scope.shadowed, ENTITY_READ_FUNCTIONS
        ):
            if kind is None:
                continue
            for name in _read_call_classes(node):
                obj = getattr(scope.module, name, None)
                if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                    found.add(obj)
    return found


def _names_mentioned(nodes: Iterable[ast.AST]) -> set[str]:
    """Every bare name, attribute name and parameter name appearing in `nodes`."""
    mentioned: set[str] = set()
    for child in nodes:
        if isinstance(child, ast.Name):
            mentioned.add(child.id)
        elif isinstance(child, ast.Attribute):
            mentioned.add(child.attr)
        elif isinstance(child, ast.arg):
            mentioned.add(child.arg)
    return mentioned


class _Scope(typing.NamedTuple):
    """One place an entity-IO call can live, with what it takes to read it.

    `site` is "path:scope-name", `nodes` the AST nodes the scope owns, `module`
    the imported module the names in it resolve through, and `shadowed` the
    names this scope binds, which a lookup in that module would answer wrongly.
    """

    site: str
    nodes: list[ast.AST]
    module: types.ModuleType
    shadowed: frozenset[str]


def _outside_functions(tree: ast.Module | ast.ClassDef) -> list[ast.AST]:
    """The nodes of `tree` that no function or method definition contains.

    Called on a module, it is module level, class bodies, and any lambda in
    either: a lambda is not an `ast.FunctionDef`, so the walk stops at real defs
    only and a lambda body stays in this scope. A function's decorators,
    annotations and argument defaults hang off its own `ast.FunctionDef` node
    and therefore belong to that function's scope, not to this one.

    Called on a single `ast.ClassDef` — which is what `_shadowed_names` does to
    find the names a class body binds — it is that class body without its
    methods, by the same rule.
    """
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(tree))
    while stack:
        child = stack.pop()
        nodes.append(child)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        stack.extend(ast.iter_child_nodes(child))
    return nodes


def _package_scopes(root: pathlib.Path) -> list[_Scope]:
    """Every scope in the package that an entity-IO call can appear in.

    Functions and methods, plus one synthetic `<module>` scope per file for
    everything outside them. The previous version collected only
    `ast.FunctionDef` / `ast.AsyncFunctionDef`, and adversarial review put a
    real write in the gap:

        ARCHIVERS = {"byok": lambda io, config, workspace_id, provider:
            io.write_entity("byok_archive2", byok_id(workspace_id, provider), config)}

    at module level, mentioning the full envelope vocabulary and seen by
    nothing. The `<module>` scope closes exactly that: it is the file's nodes
    minus the function bodies, so a class-body or module-level call is judged by
    the names around it the same way a function's is.
    """
    found: list[_Scope] = []
    for path in _python_sources(root):
        module = _module_for(path)
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                nodes = list(ast.walk(node))
                found.append(
                    _Scope(
                        f"{relative}:{node.name}",
                        nodes,
                        module,
                        _shadowed_names(nodes, module_level=False),
                    )
                )
        outside = _outside_functions(tree)
        found.append(
            _Scope(
                f"{relative}:<module>",
                outside,
                module,
                _shadowed_names(outside, module_level=True),
            )
        )
    return found


def _entity_io_calls(
    nodes: Iterable[ast.AST],
    module: types.ModuleType,
    shadowed: frozenset[str] = frozenset(),
    recognised: dict[str, int] | None = None,
) -> list[tuple[ast.Call, int, str | None]]:
    calls: list[tuple[ast.Call, int, str | None]] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        index = (ENTITY_IO_FUNCTIONS if recognised is None else recognised).get(
            _called_name(node) or ""
        )
        if index is None:
            continue
        calls.append((node, index, _kind_argument(node, index, module, shadowed)))
    return calls


def envelope_adjacent_kinds(
    root: pathlib.Path,
    model_names: frozenset[str],
    field_names: frozenset[str],
    sealer_names: frozenset[str],
    registry_kinds: frozenset[str],
) -> tuple[dict[str, set[str]], list[str]]:
    """Every kind named by a scope that handles envelope material.

    Returns (kind -> the call sites naming it, unresolvable call sites).

    This replaces a hand-written two-entry list of adapter modules. Adversarial
    review was right that such a list is the same maintenance hazard the module
    docstring criticises `_MIGRATED_KINDS` for, and proved it by putting the
    same defect in a third module the list did not name. The file scope is now
    derived and package-wide.

    It is not strictly wider than what it replaced, and the module docstring
    records the trade. The old list took every kind named anywhere in those two
    files; this takes only the kinds named by a scope that mentions envelope
    vocabulary, so a function in one of those same two files that mentions none
    of it is no longer covered. `broadcast_delivery` and
    `broadcast_delivery_due`, written by `_write_delivery` in
    storage_gcp_broadcast.py, are the two real kinds that left scope this way.

    A scope is envelope-adjacent when it mentions a derived envelope-bearing
    model class, a derived envelope field name, a sealing function, or an id
    helper that an already-adjacent scope uses to build the entity id of a
    registry kind. That last clause is a fixed point, not a guess: it is how
    `byok_id` enters the vocabulary, and with it every function that builds a
    byok row id — including one that takes the config as an untyped parameter.

    Scope, not function: `_package_scopes` adds a `<module>` scope per file, so
    a write inside a module-level lambda or a class body is judged too. That was
    a hole review found after the function-scoped version landed.

    Reads and writes both, because the gap this closes is a kind that is only
    ever written. A config archived under a second kind by a plain
    `write_entity` leaves `entity_kinds` unchanged and every other assertion
    green, while the archived row keeps its v1 envelope.
    """
    scopes = _package_scopes(root)
    vocabulary = set(model_names) | set(field_names) | set(sealer_names)
    for _ in range(len(scopes) + 1):
        widened = set(vocabulary)
        for scope in scopes:
            if not (_names_mentioned(scope.nodes) & vocabulary):
                continue
            for node, index, kind in _entity_io_calls(scope.nodes, scope.module, scope.shadowed):
                if kind not in registry_kinds or len(node.args) <= index + 1:
                    continue
                widened |= {
                    name
                    for call in ast.walk(node.args[index + 1])
                    if isinstance(call, ast.Call) and (name := _called_name(call)) is not None
                }
        if widened == vocabulary:
            break
        vocabulary = widened

    found: dict[str, set[str]] = {}
    unresolvable: list[str] = []
    for scope in scopes:
        if not (_names_mentioned(scope.nodes) & vocabulary):
            continue
        for node, _index, kind in _entity_io_calls(scope.nodes, scope.module, scope.shadowed):
            site = f"{scope.site}:{node.lineno}"
            if kind is None:
                unresolvable.append(f"{site} -> {ast.unparse(node)[:90]}")
            else:
                found.setdefault(kind, set()).add(site)
    return found, unresolvable


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


def sealing_functions(module: types.ModuleType) -> dict[str, str]:
    """encrypt_* function name -> the AAD namespace it passes to `_aad_v2`.

    Derived, not written down. The first draft of this module hardcoded
    {encrypt_byok_secret: provider, encrypt_control_secret: control}, which made
    the family half of the law a restatement of the very assumption it exists to
    check — if someone swapped the namespace constant inside one of these
    functions, a hand-written oracle would agree with the registry and disagree
    with reality. Adversarial review named it; reading the constant each
    function actually seals with is the fix.
    """
    source = pathlib.Path(module.__file__ or "")
    tree = ast.parse(source.read_text(), filename=str(source))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("encrypt_"):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and _called_name(call) == "_aad_v2"):
                continue
            if call.args and isinstance(call.args[0], ast.Name):
                found[node.name] = getattr(module, call.args[0].id)
    return found


def _families_sealed_in(expression: ast.expr, sealers: dict[str, str]) -> set[str]:
    families: set[str] = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Call):
            family = sealers.get(_called_name(node) or "")
            if family is not None:
                families.add(family)
    return families


def sealed_families(
    root: pathlib.Path,
    field_names: frozenset[str],
    sealers: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """Field name -> the AAD namespaces its value is actually sealed under.

    A value counts as sealing `f` when a call to one of the two encrypt
    functions appears anywhere inside the expression bound to `f` — which is
    what makes the `encrypt_control_secret(...) if headers else None` shape in
    the broadcast routes visible rather than a blind spot.

    It aggregates by bare field name across every writer, which is a real
    limit and not a rounding error: two modules already write
    `encrypted_secret`, and if one of them started sealing through a wrapper
    with the other family, the wrapper would contribute nothing to this union
    and the aggregate would still read {provider}. Detecting that needs
    dataflow, not a name scan. Stated in the module scope limit.
    """
    sealers = SEALING_FUNCTIONS if sealers is None else sealers
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
                families = _families_sealed_in(expression, sealers)
                if families:
                    found.setdefault(name, set()).update(families)
    return found


# ---------------------------------------------------------------------------
# The derived source of truth, and the registry restated from the module.
# ---------------------------------------------------------------------------

SEALING_FUNCTIONS = sealing_functions(byok_crypto)
DERIVED_MODEL_FIELDS = envelope_fields(MODULES_BY_PATH.values())
DERIVED_FIELD_NAMES = frozenset(name for fields in DERIVED_MODEL_FIELDS.values() for name in fields)
DERIVED_KINDS = entity_kinds(SRC, frozenset(DERIVED_MODEL_FIELDS))
DERIVED_FAMILIES = sealed_families(SRC, DERIVED_FIELD_NAMES)
# Seeded with the DERIVED kinds, never with `_MIGRATED_KINDS`: a scope that
# widens by reading the registry it audits would narrow again if a kind were
# dropped from the registry, which is the direction that must never fail open.
DERIVED_ADJACENT_KINDS, UNRESOLVABLE_KIND_CALL_SITES = envelope_adjacent_kinds(
    SRC,
    frozenset(DERIVED_MODEL_FIELDS),
    DERIVED_FIELD_NAMES,
    frozenset(SEALING_FUNCTIONS),
    frozenset(kind for kinds in DERIVED_KINDS.values() for kind in kinds),
)


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

STORAGE_MODELS_MODULE = "storage_models.py"
STORAGE_MODELS_NAME = storage_models.__name__


def test_every_package_source_file_is_an_imported_module() -> None:
    """Every scan below resolves names through a module object, so a source file
    with no module would be scanned with a weaker tool or not at all."""
    on_disk = {path.resolve() for path in SRC.rglob("*.py")}
    missing = sorted(str(path.relative_to(SRC)) for path in on_disk - set(MODULES_BY_PATH))
    assert not missing, (
        f"{missing} are package sources that `pkgutil.walk_packages` did not import, "
        "so annotations and kind constants in them cannot be resolved. Give the "
        "directory an __init__.py, or widen `_import_package_modules`."
    )


def test_every_envelope_typed_attribute_lives_in_storage_models() -> None:
    """A convention check, no longer the thing the law's scope rests on.

    It used to be the scope guard, and it was the weakest link in this file:
    it matched the literal substring "EncryptedSecretEnvelope" in an unparsed
    annotation, so `SealedKey: TypeAlias = EncryptedSecretEnvelope | None`
    walked past it, and `envelope_fields` — scoped to storage_models — never
    looked either. Adversarial review landed a complete envelope-bearing
    persisted model, with a real sealing call and a real unregistered kind,
    entirely inside that gap.

    The fix was not here. `envelope_fields` now reflects over every dataclass in
    the package, so an envelope-bearing model declared anywhere owes the
    registry a (kind, field, family) and the law fails without it. What remains
    here is the narrower and still useful statement that these models are all
    declared in one file, checked by the union of resolved reflection (which
    sees through aliases) and the text scan (which sees classes reflection
    cannot reach). A failure is a convention violation, not a hole.
    """
    resolved = {
        (module, cls, attribute)
        for module, cls, attribute in envelope_typed_attributes(MODULES_BY_PATH.values())
        if module != STORAGE_MODELS_NAME
    }
    spelled = {
        (module, cls, attribute)
        for module, cls, attribute in envelope_annotated_attributes(SRC)
        if module != STORAGE_MODELS_MODULE
    }
    stray = sorted(resolved | spelled)
    assert not stray, (
        "EncryptedSecretEnvelope is declared outside storage_models: "
        + ", ".join(f"{module}:{cls}.{attribute}" for module, cls, attribute in stray)
        + ". `envelope_fields` reflects the whole package, so the law covers a stray "
        "class that is a module attribute; one defined inside a function body it does "
        "NOT cover, and this message is then the only thing that fires. Either way "
        "this codebase keeps persisted secret material in one file: move the field "
        "onto a storage_models dataclass, or record here why it cannot be."
    )


# Kinds an envelope-adjacent scope names that carry no envelope. Both directions
# are asserted, so an entry that stops being named fails too.
#
# This was three entries when the scan took every kind named in two hand-listed
# adapter modules. `broadcast_delivery` and `broadcast_delivery_due` are not here
# any more because they are OUT OF SCOPE, not because they were shown to be
# safe: the method that writes them, `_write_delivery` in
# storage_gcp_broadcast.py, mentions no envelope model, field, sealer or id
# helper, so the scope-based derivation never reaches it. Read the shrink from
# three to one as a coverage trade, recorded in the module docstring, rather
# than as two fewer risks.
NON_ENVELOPE_KINDS = {
    # Index row written as a dict literal: {"destination_id": ...}. See
    # SpannerBroadcastDestinations.create in storage_gcp_broadcast.py.
    "broadcast_destination_by_workspace": "pointer row, dict literal body",
    "custom_model": "prompt-wrapper row, no encrypted envelope fields",
    "custom_model_by_user": "pointer row, dict literal body",
    "user_provided_model_by_user": "pointer row, dict literal body",
}

# Fields on a persisted dataclass whose annotation admits Any, and so could
# carry an envelope the reflection cannot see. Reflection follows types; `Any`
# is the absence of one. Verified: an EncryptedSecretEnvelope nested under
# BroadcastDeliveryJob.settle_body serialises straight through
# storage_codec.json_body's recursive asdict into the persisted row.
LOOSELY_TYPED_FIELDS = {
    ("BroadcastDeliveryJob", "settle_body"): (
        "persisted under kind 'broadcast_delivery', which the backfill does not "
        "scan. A serialised envelope echoed into a settle body would keep its v1 "
        "algorithm with nothing to report it."
    ),
    ("Generation", "tool_calls"): (
        "model-produced content, excluded from the persisted generation record "
        "by generation_record_body — see test_egress_projection_closure_property."
    ),
}


_OPAQUE_CONTAINERS = (dict, list, set, frozenset, tuple)


def _admits_any(annotation: Any) -> bool:
    """Can this resolved annotation hold an arbitrary object?

    `Any` and `object` are the obvious cases. An unparameterized container is
    the case adversarial review added: `signing_material: dict` is exactly as
    opaque as `dict[str, Any]`, and the previous version of this function saw
    only the second, so the "frozen at two" claim next door was false for the
    first. A parameterized container is judged by its parameters, so
    `dict[str, str]` is not opaque and `dict[str, Any]` is.

    `_OPAQUE_CONTAINERS` is itself a hand-written list, and the fourth one in
    this file — the module docstring names it with the other three. It holds the
    five builtins, so `_admits_any(collections.deque)` and
    `_admits_any(types.SimpleNamespace)` are both False (measured): a persisted
    field annotated with either is neither classified here nor reflected as an
    envelope, exactly as a bare `dict` was before this correction.
    """
    if annotation is Any or annotation is object:
        return True
    arguments = typing.get_args(annotation)
    if arguments:
        return any(_admits_any(argument) for argument in arguments if argument is not Ellipsis)
    origin = typing.get_origin(annotation) or annotation
    return isinstance(origin, type) and issubclass(origin, _OPAQUE_CONTAINERS)


def test_every_loosely_typed_persisted_field_is_classified() -> None:
    """Reflection follows types, and `Any` is the absence of one.

    This is the sharpest limit adversarial review found on the law, and it is
    not hypothetical: an `EncryptedSecretEnvelope` placed inside
    `BroadcastDeliveryJob.settle_body` is serialised into the row body by
    `storage_codec.json_body`, lands under kind 'broadcast_delivery', and is
    invisible both to `envelope_fields` here and to the backfill's scan. The
    module docstring's claim that any envelope reaching storage must be a
    reflected dataclass field is FALSE for that shape, and this test exists to
    stop the list of such shapes from growing silently rather than to close the
    hole — closing it needs the backfill to walk bodies, not fields.

    Two corrections from the second review. `_admits_any` matched only `Any` and
    `object`, so a bare `dict` — no less opaque, one token shorter — was neither
    flagged here nor seen by the envelope reflection, and the "frozen at two"
    claim was false for it. And the domain was `vars(storage_models)`, so a
    persisted model declared elsewhere was unclassified; it is now every
    dataclass the package reads back under an entity kind, unioned with
    storage_models so a model with no typed read is still covered.
    """
    models = persisted_dataclasses(SRC) | {
        model
        for model in vars(storage_models).values()
        if isinstance(model, type)
        and dataclasses.is_dataclass(model)
        and model.__module__ == storage_models.__name__
    }
    loose = {
        (model.__name__, field.name)
        for model in models
        for field in dataclasses.fields(model)
        if _admits_any(typing.get_type_hints(model)[field.name])
    }

    unclassified = sorted(loose - set(LOOSELY_TYPED_FIELDS))
    assert not unclassified, (
        f"persisted dataclass(es) gained loosely typed field(s) {unclassified} — the "
        "domain is every model the package reads back under an entity kind, not just "
        "storage_models, so the class named may live anywhere. An "
        "EncryptedSecretEnvelope nested inside one reaches storage without being "
        "a typed field, so neither this module's reflection nor the backfill can "
        "see it. Classify each: say why it cannot carry secret material, or make "
        "the backfill walk it."
    )
    assert not sorted(set(LOOSELY_TYPED_FIELDS) - loose), (
        f"LOOSELY_TYPED_FIELDS names fields that are no longer loosely typed: "
        f"{sorted(set(LOOSELY_TYPED_FIELDS) - loose)}"
    )


def test_every_kind_an_envelope_handling_function_names_is_registered_or_declared() -> None:
    """A kind an envelope-bearing model is WRITTEN to, that nothing reads back.

    `entity_kinds` derives from typed reads, so archiving a `ByokProviderConfig`
    under a second kind with a bare `write_entity` changes nothing it sees and
    leaves every other assertion green while the archived row keeps its v1
    envelope.

    Four rounds of adversarial review found six ways past earlier versions of
    this test, and all six are why it now looks like this. The kind had to be
    an inline string, so a module constant escaped: kinds are now resolved
    through the calling module's namespace. That resolution then read a
    function-local of the same name as the module constant, reporting a real
    write under the wrong, registered kind: a shadowed name is now unresolvable
    and the site is reported. The shadow set then missed CLASS-BODY bindings,
    which are not module attributes either, so the same mis-attribution
    survived one level in: class bodies now shadow too. The scan covered two
    hand-listed adapter modules, so the same write in storage_postgres.py
    escaped: the scope is now derived and package-wide. It collected function
    definitions only, so a write inside a module-level `lambda` escaped: there
    is now a `<module>` scope per file. And a dynamic kind used to be skipped in
    silence: it now fails.

    Scope, precisely, including what it LOST. This sees an entity-IO call when
    the scope around it mentions an envelope-bearing model, an envelope field, a
    sealing function, or an id helper reached from one of those. A scope that
    persists an envelope-bearing object while mentioning none of them — one that
    takes it as `Any` and builds the row id itself — is not seen; nor, for the
    same reason, are `broadcast_delivery` and `broadcast_delivery_due`, which
    the hand-listed-module version of this test did cover. Both residues are
    named in the module scope limit; neither is claimed to be covered.
    """
    assert not UNRESOLVABLE_KIND_CALL_SITES, (
        "these envelope-handling call sites pass a kind this scan cannot resolve to a "
        "constant, so it cannot be checked against the registry: "
        + "; ".join(sorted(UNRESOLVABLE_KIND_CALL_SITES))
        + ". Bind the kind to a module-level constant that no local in the same scope "
        "shadows, or make the helper generic enough that no envelope-bearing model "
        "reaches it."
    )
    named = set(DERIVED_ADJACENT_KINDS)
    unaccounted = sorted(named - set(_MIGRATED_KINDS) - set(NON_ENVELOPE_KINDS))
    assert not unaccounted, (
        f"envelope-handling code names kind(s) {unaccounted} that are neither in the "
        "backfill registry nor declared secret-free — at "
        + "; ".join(
            f"{kind} ({', '.join(sorted(DERIVED_ADJACENT_KINDS[kind]))})" for kind in unaccounted
        )
        + ". If such a row can hold an envelope the backfill will never scan it; if it "
        "cannot, say so in NON_ENVELOPE_KINDS with the reason."
    )
    stale = sorted(set(NON_ENVELOPE_KINDS) - named)
    assert not stale, (
        f"NON_ENVELOPE_KINDS names kinds no envelope-handling function touches: {stale}"
    )


def test_the_sealing_oracle_is_read_off_byok_crypto() -> None:
    """The family oracle must come from the crypto module, not from memory.

    A hardcoded {encrypt_byok_secret: provider} pairing would keep agreeing with
    the registry even if someone swapped the namespace constant inside
    `encrypt_byok_secret` itself — the oracle and the registry would be wrong
    together, which is the one way a two-sided equality check learns nothing.
    """
    assert SEALING_FUNCTIONS == {
        "encrypt_byok_secret": NAMESPACE_PROVIDER,
        "encrypt_control_secret": NAMESPACE_CONTROL,
    }, (
        "the namespace each encrypt_* function seals with has changed: "
        f"{SEALING_FUNCTIONS}. That is an AAD-format change, not a refactor."
    )


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
    if field == "encrypted_endpoint_api_key":
        return USER_MODEL_ENDPOINT_KEY_PURPOSE
    if field == "encrypted_signing_secret":
        return USER_MODEL_SIGNING_PURPOSE
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
        (
            "owner_workspace_id"
            if kind == "user_provided_model"
            else "workspace_id"
        ): WORKSPACE,
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
        name
        for name in DERIVED_FIELD_NAMES
        if DERIVED_FAMILIES.get(name) == {NAMESPACE_CONTROL}
        and name in {"encrypted_api_key", "encrypted_headers"}
    ),
)
def test_the_broadcast_context_helper_matches_the_service(field: str) -> None:
    """There are THREE copies of this format string, not two.

    `_broadcast_context` in the backfill, `broadcast_secret_context` on the
    write side, and `_secret_context` in broadcast_adapters on the READ side.
    The backfill's source comment claims byte-identity with the write-side copy
    only; adversarial review pointed out the read-side copy is the one that has
    to reconstruct the AAD after migration, and it was never compared. All
    three are pinned here — a divergence re-seals a control secret against a
    purpose the reader cannot rebuild.
    """
    destination_id = "bdst_context_identity"
    suffix = field.removeprefix("encrypted_")
    assert _broadcast_context(destination_id, field) == broadcast_secret_context(
        destination_id, suffix
    )
    assert _broadcast_context(destination_id, field) == adapter_secret_context(
        destination_id, suffix
    )


@pytest.mark.parametrize(
    ("field", "purpose"),
    (
        ("encrypted_endpoint_api_key", USER_MODEL_ENDPOINT_KEY_PURPOSE),
        ("encrypted_signing_secret", USER_MODEL_SIGNING_PURPOSE),
    ),
)
def test_the_user_model_context_helper_matches_the_service(
    field: str,
    purpose: str,
) -> None:
    assert _broadcast_context("ignored", field) == purpose


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

    sql = captured["sql"]
    placeholders = _kind_in_clause(sql).count("%s")
    assert placeholders == len(_MIGRATED_KINDS), (
        f"the Postgres scan has {placeholders} kind placeholder(s) for a registry of "
        f"{len(_MIGRATED_KINDS)} kind(s); the surplus kinds are never fetched"
    )
    # Positionally, not "somewhere in the tuple". The first draft asserted only
    # that each registered kind appeared among the parameters, which adversarial
    # review pointed out would still pass if the kinds were bound to the
    # pagination placeholders and the IN list got the cursor values.
    assert sql[: sql.index("kind IN (")].count("%s") == 0, (
        "the kind IN clause is no longer the first bound group, so the parameter "
        "positions checked below no longer line up with it"
    )
    bound = tuple(captured["params"][: len(_MIGRATED_KINDS)])
    assert bound == tuple(_MIGRATED_KINDS), (
        f"the Postgres scan binds {bound} into its kind filter, but the registry "
        f"covers {tuple(_MIGRATED_KINDS)}"
    )


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
        "from typing import TypeAlias\n"
        "Sealed: TypeAlias = EncryptedSecretEnvelope | None\n"
        "_Renamed = EncryptedSecretEnvelope\n"
        "class Derived(EncryptedSecretEnvelope):\n"
        "    pass\n"
        "@dataclass\n"
        "class Probe:\n"
        "    plain: str = ''\n"
        "    encrypted_probe: EncryptedSecretEnvelope | None = None\n"
        "    nested_probe: list[EncryptedSecretEnvelope] | None = None\n"
        "    aliased_probe: Sealed = None\n"
        "    renamed_probe: _Renamed | None = None\n"
        "    subclassed_probe: Derived | None = None\n"
        "    quoted_probe: 'Sealed' = None\n",
        module.__dict__,
    )

    # Each of the last four is a form of type indirection that defeated the
    # previous, text-matching version of the domain guard. They are here because
    # the reflection must see them as the same type, not because the shapes are
    # exotic.
    assert envelope_fields([module]) == {
        "Probe": (
            "encrypted_probe",
            "nested_probe",
            "aliased_probe",
            "renamed_probe",
            "subclassed_probe",
            "quoted_probe",
        )
    }
    assert envelope_typed_attributes([module]) >= {
        (module.__name__, "Probe", "aliased_probe"),
        (module.__name__, "Probe", "renamed_probe"),
        (module.__name__, "Probe", "subclassed_probe"),
        (module.__name__, "Probe", "quoted_probe"),
    }


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

    (tmp_path / "stray_model.py").write_text(
        "@dataclass\n"
        "class StrayModel:\n"
        "    encrypted_probe: EncryptedSecretEnvelope | None = None\n"
        "    plain: str = ''\n"
    )

    assert entity_kinds(tmp_path, frozenset({"ProbeModel"})) == {"ProbeModel": {"probe_kind"}}
    assert sealed_families(tmp_path, frozenset({"encrypted_probe"})) == {
        "encrypted_probe": {NAMESPACE_CONTROL}
    }
    assert envelope_annotated_attributes(tmp_path) == {
        ("stray_model.py", "StrayModel", "encrypted_probe")
    }


def test_a_kind_bound_to_a_module_constant_is_resolved(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the kind resolution, against the exact evasion.

    Adversarial review defeated the previous version by hoisting a repeated kind
    to `ARCHIVE_KIND = "byok_archive"`; `_literal_kind` required an
    `ast.Constant`, so the call site vanished from every scan. The kind is now
    looked up in the calling module's namespace, and a name that resolves to no
    string still resolves to None so the caller can fail rather than skip.

    `shadow` is the fail-open the third review found in that lookup: a local
    `PROBE_KIND = "probe_archive"` would otherwise resolve through the module to
    "probe_kind" and the archive write would be reported as the registered kind.
    It must come back None — unresolvable, therefore reported — and the read in
    `load` must keep resolving, because a file that binds the name in one
    function still has a real module constant everywhere else.

    `Archiver` is the same fail-open one level in, and it survived the fix that
    closed `shadow` because a class body is neither a function scope nor the
    module namespace. A class-level `PROBE_KIND` is not an attribute of the
    module, so the lookup answered "probe_kind" — registered — for a write that
    really goes to "probe_archive", which is exactly the shape the fourth
    review measured. `_shadowed_names` now reads class-body bindings in the
    `<module>` scope, and both halves are pinned below: guarded it is None,
    unguarded it is the mis-attribution itself.
    """
    source = tmp_path / "adapter.py"
    source.write_text(
        "def load(io, probe_id):\n"
        "    return io.read_entity(PROBE_KIND, probe_id, ProbeModel)\n"
        "def stash(io, probe_id, probe):\n"
        "    io.write_entity(runtime_kind(), probe_id, probe)\n"
        "def shadow(io, probe_id, probe):\n"
        "    PROBE_KIND = 'probe_archive'\n"
        "    io.write_entity(PROBE_KIND, probe_id, probe)\n"
        "class Archiver:\n"
        "    PROBE_KIND = 'probe_archive'\n"
        "    SEED = io.write_entity(PROBE_KIND, 'seed', ProbeModel)\n"
    )
    module = types.ModuleType("tests._synthetic_adapter")
    module.PROBE_KIND = "probe_kind"  # type: ignore[attr-defined]
    monkeypatch.setitem(MODULES_BY_PATH, source.resolve(), module)

    by_scope = {
        scope.site: [kind for _n, _i, kind in _entity_io_calls(scope.nodes, module, scope.shadowed)]
        for scope in _package_scopes(tmp_path)
    }
    assert by_scope["adapter.py:load"] == ["probe_kind"]
    assert by_scope["adapter.py:stash"] == [None]
    assert by_scope["adapter.py:shadow"] == [None]
    assert by_scope["adapter.py:<module>"] == [None]
    # Without the scope's shadow set both archive writes resolve to the module
    # constant instead — the fail-open itself, pinned so that neither half of
    # the guard can be dropped in silence.
    unguarded = _package_scopes(tmp_path)
    assert {
        scope.site: [kind for _n, _i, kind in _entity_io_calls(scope.nodes, module)]
        for scope in unguarded
        if scope.site in {"adapter.py:shadow", "adapter.py:<module>"}
    } == {"adapter.py:shadow": ["probe_kind"], "adapter.py:<module>": ["probe_kind"]}
    assert entity_kinds(tmp_path, frozenset({"ProbeModel"})) == {"ProbeModel": {"probe_kind"}}


def test_a_write_outside_any_function_is_in_scope(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the `<module>` scope.

    `_package_scopes` used to collect function definitions only, so adversarial
    review moved the write into a module-level dispatch dict — a lambda, which
    is not an `ast.FunctionDef` — and the whole adjacency scan walked past it
    while it still mentioned every envelope name. The scope that owns everything
    outside a function body is what closes it.
    """
    source = tmp_path / "adapter.py"
    source.write_text(
        "ARCHIVERS = {\n"
        "    'probe': lambda io, probe: io.write_entity(\n"
        "        'probe_archive', probe_id(probe), ProbeModel\n"
        "    ),\n"
        "}\n"
    )
    module = types.ModuleType("tests._synthetic_module_scope")
    monkeypatch.setitem(MODULES_BY_PATH, source.resolve(), module)

    kinds, unresolvable = envelope_adjacent_kinds(
        tmp_path,
        frozenset({"ProbeModel"}),
        frozenset(),
        frozenset(),
        frozenset({"probe_kind"}),
    )
    assert not unresolvable
    assert kinds == {"probe_archive": {"adapter.py:<module>:2"}}


def test_the_derivation_reproduces_todays_registry() -> None:
    """A pin on what the derivation currently yields.

    Not a substitute for the law above — it is a regression guard on the
    derivation itself, so a scan that quietly stops finding anything shows up
    as this test failing rather than as the law passing vacuously.
    """
    assert DERIVED_MODEL_FIELDS == {
        "ByokProviderConfig": ("encrypted_secret",),
        "BroadcastDestination": ("encrypted_api_key", "encrypted_headers"),
        "UserProvidedModel": (
            "encrypted_endpoint_api_key",
            "encrypted_signing_secret",
        ),
    }
    assert DERIVED_KINDS == {
        "ByokProviderConfig": {"byok"},
        "BroadcastDestination": {"broadcast_destination"},
        "UserProvidedModel": {"user_provided_model"},
    }
    assert DERIVED_FAMILIES == {
        "encrypted_secret": {NAMESPACE_PROVIDER},
        "encrypted_api_key": {NAMESPACE_CONTROL},
        "encrypted_headers": {NAMESPACE_CONTROL},
        "encrypted_endpoint_api_key": {NAMESPACE_CONTROL},
        "encrypted_signing_secret": {NAMESPACE_CONTROL},
    }
    # The derived scope of the adapter-kind guard. Pinned because it replaced a
    # hand-written module list: a fixed point that quietly stopped widening
    # would shrink this rather than fail anything, and the guard would go silent
    # the way the two-module list did.
    assert set(DERIVED_ADJACENT_KINDS) == {
        "byok",
        "broadcast_destination",
        "broadcast_destination_by_workspace",
        "custom_model",
        "custom_model_by_user",
        "user_provided_model",
        "user_provided_model_by_user",
    }
    assert {site.split(":")[0] for sites in DERIVED_ADJACENT_KINDS.values() for site in sites} == {
        "storage_gcp_byok.py",
        "storage_gcp_broadcast.py",
        "storage_gcp_custom_models.py",
        "storage_gcp_user_models.py",
        "storage_postgres.py",
    }
