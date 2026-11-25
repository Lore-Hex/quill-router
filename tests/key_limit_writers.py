"""A structural scanner of the writers of ``tr_key_limit``.

``tr_key_limit``'s limit columns (limit_micro, day_limit_micro,
week_limit_micro, month_limit_micro) are what a strict key's admission reads:
Python's strict reserve and the fast path edge. Every write of them goes
through one guarded function per backend, which refuses in the writing
transaction a strict key's mirror going from every limit unset to any set
(storage_gcp_key_limit_mirror.write_key_limit_rows on Spanner,
PostgresStore._write_key_limit_caps_tx on Postgres). This scanner looks for
the other places that write the table, in the forms listed under "What it
covers" below, so the list of writers is not kept by hand alone. It
reports:

- each place code names ``tr_key_limit``, in any case or quoting: in Python
  a string (DML in any case, the bare table name, a piece of an f-string, a
  concatenation, a %-format or a .format), or a constant or variable holding
  one (local, module or class, and across modules by name); in a shell, SQL,
  Terraform or YAML file, or the fast path's Go, any line that is not a
  comment;
- each mutation or DML whose table it cannot resolve: a mutation call (by
  verb, through an alias, with ``**kwargs``, or any call given ``table=``)
  whose table is not a known string, and DML whose table is filled in at run
  time (an f-string, %-format, .format or concatenation, a shell variable, a
  Go variable or format).

Reads (SELECT, ``FOR UPDATE`` locks, DDL) and messages that only mention the
table are not findings. A finding is (style, path, qualified name):

  key_limit.sql        DML naming the table
  key_limit.name       the bare table name, or a constant or variable holding it
  key_limit.text       a non-Python file naming the table
  key_limit.indirect   code reached by a computed name, or an executor passed
                       on, in a scope that names the table (below)
  key_limit.escape     a value naming the table leaving its scope other than
                       by an executor, a mutation or a followed helper (below)
  mutation.unresolved  a mutation whose table is not a known string
  sql.dynamic          DML whose table is filled in at run time

limit_column_writers() narrows them to the scopes that may write a limit
column. Three layers decide it: the escape rule, the backstop and the
parser.

A scope is an outermost function with every function, lambda, comprehension
and class body nested in it (_Module.outer), or a module statement or class
body outside any function, or a text file. getattr(obj, "name") with a
literal name, where getattr is the builtin by every binding (below), and
obj.__getattribute__("name") are read as obj.name before either layer runs
(_Fetches). A function's defaults, decorators and annotations belong to the
scope around it.

Where a rule needs to know what a name or callee is (a builtin, getattr, a
plain decorator, a namespace or frame reader, the module's own helper, an
executor's alias), it asks one resolver (_Identities), which reads the
name's bindings: a name has a known identity only when each binding of it
in the module, in the forms it collects and whatever the scope (an
assignment, a walrus, annotations and defaults included, an import with
or without as, a parameter, a def or class, a loop, with, except, match,
del or type parameter target), resolves to the same thing; a builtin's
name bound anywhere in the module is not the builtin; an import of a
module's attribute (from functools import cache, a builtin) only while no
scanned module may rebind that attribute; and no name is known in a
module that imports * or may write its own namespace. A rule that clears
needs a known identity; a rule that adds findings also reads the spelling.

The escape rule (_escape_routes) decides where SQL text naming the table may
go. A value whose rendering may name the table (a string constant, f-string,
concatenation or format naming it, a name or attribute any binding of which
is one, in any module, or a list, tuple, dict or set holding one; every
string literal and f-string is followed, not only the outermost expression)
may leave its scope only by:

- a recognised executor call (any of its arguments: the statement is judged
  by the rules below, its parameters are data) or mutation call (judged by
  the mutation rules); or
- a call to a helper of the module that derived_sinks follows, at a
  parameter shown to reach only executors (clean_sinks: every use of it,
  and of each name it is bound to or put into, is one of these routes, so
  a helper that returns it, or a method result holding it, is not clean),
  the callee being that def by every binding (_helper: no other binding of
  its name in the module, no attribute of that name bound, as an attribute
  only of its class or a method's self or cls there, and undecorated or
  plainly decorated). An executor or mutation call counts only where the
  callee is settled (_settled_callee: a name bound only to such callees).

The steps it allows are chosen so that, in the code shapes it models, no
code the scanner cannot see receives it: every other operand, receiver,
callee and class it meets must be provably builtin (_builtin_value, below).
This is a best-effort guard, not a soundness guarantee for arbitrary
Python (see What it covers, below). The steps:

- built on: concatenated or %-formatted with a provably builtin operand; an
  f-string part (a format spec only for a provably builtin value); a
  container display or comprehension (a set element, a dict key or a dict
  spread only beside provably builtin keys); indexed or sliced with
  provably builtin indexes; given to str (one argument), repr, format,
  list, tuple, reversed, or set, frozenset or sorted when its elements are
  provably builtin, with every other argument provably builtin; given to a
  str method of a provably builtin str (join, format, replace, ...) with
  every other argument provably builtin;
- a method of it with every argument provably builtin: its result is
  followed (a str predicate's result is a bool). A method that compares or
  hashes its elements (sort, index, count, remove, a dict's or set's
  lookups and set algebra), or one that is not a builtin type's, only of a
  provably builtin value; encode and decode only with codecs Python ships
  and an error handler that never raises;
- bound to a name, whose uses are followed in every scope the name may
  resolve to (a closure's and a nonlocal's included), and the value it
  held before each x += other (other must be provably builtin); in a class
  body only for a plain class (_plain_class), and then every attribute of
  that name (Cls.x, self.x) is followed too;
- put into a container made in the same function with append, extend,
  insert, add, update, setdefault or += (add, update and setdefault only
  into one whose contents are provably builtin); the container and every
  name it was bound to are followed, and so is the call's result; stored
  into (container[key] = other, container[key] += other) only with
  provably builtin keys and values;
- looped over into a name;
- used in a way that keeps none of its text and calls no user code:
  compared (==, in, ...) with provably builtin operands, or by is; tested;
  negated; measured with len, bool, hash, id, type or callable; given to
  isinstance with builtin types; or a discarded expression.

Every other route is key_limit.escape, every column: given to any other
callee (a function, method, lambda, class, callable object, exception,
print or unknown name, positionally, by keyword or spread), returned,
yielded, raised, stored in an attribute, a subscript or a global,
unpacked, a key (a KeyError holds it; list[value] keeps it), a default
argument, an assert's message, or anything not listed. The routes are an
allow list, so a construct not thought of lands in the escape by default.
So is every namespace read that can see a followed name
(_namespace_escapes): locals(), vars() or dir() with no argument, eval
or exec in its scope; globals(), the module object (sys.modules,
__import__, importlib.import_module, or a getattr or setattr by a computed
name on a name an import binds), a function's __globals__, or vars(),
dir() or __dict__ of any object for a module name or a top-level class's;
and any frame read (sys._getframe, inspect's frames, f_locals, a
traceback's tb_frame or capture_locals, a closure's cells, gc's object
walks) for any name. A reader counts by every identity it may have (the
builtin of its name, an import of it under any name, a name bound to it,
an attribute of builtins, __builtins__["eval"]), passed on uncalled it is a
frame read, and a module's locals() is its globals(). (A plain class is
itself only ever read for an attribute.)

Provably builtin (_builtin_value) is syntactic and meant to fail closed:
only a
literal; an f-string with a literal part or two fields; a container
display, comprehension, concatenation, % or conditional of such; an
element or slice of such; a str method's result on such a str; str, repr,
format or len of such; or a name each scope it may resolve to binds only
to such qualifies. A module name only to an immutable one that no module
binds by another form (unpacked, a loop, with, except, import, def, class,
match, inside if or try, a function's global: _Index.opaque) and whose
module neither reads nor writes its own namespace (_Index.exposed); a
class body's name only in a class with no bases or keywords, and only to
an immutable one; a function's name only where the module reads no frame
and the function no namespace of its own, and holding a container only if
every use of it, of its elements and of what its methods return keeps
what it holds provably builtin (_inert_use). A call, an attribute, a
parameter, an unpacked target, a with, except, import, def, class or match
binding, and a builtin type's name used as a value are not provably
builtin; a builtin's name means the builtin only where nothing in the
module binds it, in any form or scope, or every binding is that builtin
(_builtin_name, through the identity resolver below).

User code an allowed step could reach, and how each is handled:

- implicit dunder calls of the other operand (__radd__, __rmod__, __eq__,
  __lt__, __contains__, __getitem__, __iadd__): every other operand of an
  operator, comparison, subscript or += must be provably builtin, and so
  must x in x += other when the value is other, and other when the value
  is x;
- an element's own dunders: a method that compares or hashes elements
  (sort, index, count, remove, lookups, set algebra), and set, frozenset
  and sorted, only for a provably builtin value (all its elements so);
- callbacks (sorted's or sort's key, max's default, map, filter, reduce,
  partial, a str.translate table, a format_map mapping): every argument
  beside the value must be provably builtin, and map, filter, reduce, min
  and max are not allowed at all;
- __format__ of a value a format spec goes to, and a lone {x} or "{}"
  whose result is x's (a str subclass's own methods perhaps): only for
  provably builtin values;
- __iter__ of what is iterated: the value is iterated only when it is
  itself the iterable (a builtin), never given to another's iteration;
- __hash__ and __eq__ of keys: a set element, a dict key, a dict spread,
  add, update and setdefault only beside provably builtin keys;
- __class_getitem__: a value is never a key, so list[value] escapes;
- streams: print (sys.stdout may be any writer, file= any stream) is an
  escape; encode and decode only with shipped codecs (the registry is
  extensible);
- __instancecheck__: isinstance only with builtin types;
- context managers, descriptors and class machinery (a metaclass
  namespace, __prepare__, __init_subclass__, __set_name__, a class
  decorator): with statements are escapes, attributes of the value are
  read only to call a method of a builtin, and a class body binds the
  value only for a plain class: no bases (but object), keywords or
  decorators, every other statement a docstring, pass, a provably builtin
  value bound to a name or an undecorated function with provably builtin
  defaults, and the class only ever read for an attribute (Cls.x) in every
  scanned module (instantiated, passed on or given to vars(), its
  namespace may be read);
- namespaces: a name read through locals(), vars(), globals(), eval,
  exec, __dict__, sys.modules or a frame instead of by name is not a use
  the rule follows, so each such read that can see a followed name is an
  escape, and a name such a read could rebind is never provably builtin;
- exceptions: raise and an assert's message hand it to a handler in
  another scope, a KeyError holds a key looked up and a UnicodeError the
  text encoded or decoded: all escapes.

The backstop (scope_marks) does not depend on parsing. A scope that holds
the key-limit table (by name or through the constant holding it), a limit
column name and a write (a DML word in a string, or a mutation or DML API
name such as update, insert_or_update, replace, execute_update, batch_update
or Mutation...) anywhere counts as writing every limit column, whatever the
parser reads. A scope that writes none is a reviewed exception, listed with
a behavioral pin test and pinned to a fingerprint of its source
(ScanDetail.fingerprints: every definition of the scope's qualified name in
the module, a conditional or repeated def or a class's duplicate method
included, and every other statement binding that name where it is defined),
so a later change to it fails the check until it is reviewed again. For a
scope the backstop sees, the parser's precision decides only how many
exceptions are listed.

The parser is meant to fail closed: in the forms it reads, a scope is
cleared only when what it writes to the table is understood.

- SQL must tokenize under a lexicon PostgreSQL and GoogleSQL read alike
  (sql_tokens) and each statement mentioning the table must be one of a
  small grammar (_cleared_columns); a ``--`` comment ends at a line feed or
  a carriage return, and any other line end inside one, any block comment,
  ``#``, a backslash in a quote, a tripled quote or a prefixed literal fails
  closed.
- Python SQL is rendered only in a plain subset (form 2 below, and
  _Module._render); ``x += piece`` is modelled as every piece in order and
  each piece alone. Everything else is a part not known, in place, the
  known text around it kept; a statement run with a part not known whose
  known text has a write verb is dynamic SQL in any scope, and any string
  holding DML whose target holds a part not known is dynamic, run or not.
  Any call given SQL text naming the table, any executor (by name, or
  through a name or attribute bound to one: run = conn.execute,
  self.run = conn.execute, setattr(obj, "run", conn.execute)), and any of
  the module's own helpers that runs a statement it is given (derived_sinks:
  its parameter reaches an executor's statement, through its local names, a
  nested function or another such helper) runs a statement: it is
  classified where it is whole, and in a scope that mentions the table a
  part not resolved to literals (a helper's result, an interpolation, a % or
  .format argument, a str method's result, a nested function's parameter)
  fails closed whatever is known around it. A statement given to a helper
  that may change it (one that builds on the parameter, is decorated other
  than by a decorator whose identity is the builtin or stdlib one
  (_plain_decorator: singledispatch imported as cache is not cache), holds
  exec, eval or compile, or calls by a computed name), or given to a callee
  that is not that helper by every binding, fails closed in such a scope or
  wherever it names the table. The one form that clears is
  f"SET LOCAL statement_timeout = {<int>}", its value provably an int.
- In a scope that mentions the table, these fail closed (key_limit.indirect,
  every column): getattr, attrgetter, methodcaller or __getattribute__ given
  a name that is not a literal, setattr given one, attrgetter or
  methodcaller naming an executor, a mutation API or such a helper; exec,
  eval and compile; and an executor, a mutation API, such a helper, or
  getattr, setattr, exec or eval itself, used other than by calling it or
  binding it to a name or attribute (passed on, returned, stored in a
  container, wrapped by functools.partial or a decorator).
- A Spanner mutation's columns must be written out at the call as a tuple or
  list of string literals (columns from a spread dict count as every
  column). Insert, Update and InsertOrUpdate keep the columns they omit, so
  only a listed limit column is written; Replace nulls what it omits, so it,
  and any other write call, counts as every column.
- Only a .sql file is read as SQL. In other text (a shell argument or
  heredoc, YAML, Terraform, Go), a DML word followed by the table before a
  ``;`` within sixty words, whitespace collapsed, counts as every column, as
  does one where a comment marker could hide the ``;``. Every check reads
  the text as written and with shell line continuations joined (as is, and
  with the next line's indentation dropped, as a YAML block scalar drops
  it): a shell joins them on a command line and in a heredoc whose
  delimiter is not quoted, and passes them on in one whose is; a finding of
  any reading counts. A line in a heredoc's body or inside a string left
  open, after a continuation, or holding another line break is never taken
  for a comment, and a table from a command substitution or a psql variable
  is one filled in at run time.

What it covers. Each form below is pinned by tests in
tests/test_key_limit_writer_inventory.py (a test, then the ids of its cases,
in brackets, split by " | "; test_the_scanners_claim_names_tests_that_exist
checks that each one exists):

1. Direct SQL in a scope: DML naming the table, its columns read by the
   grammar; reads, locks and mentions are not writes.
   test_each_way_to_write_a_limit_column_is_found[tuple assignment |
   upsert's update | a merge | a delete | a CTE before the update |
   Spanner's INSERT OR UPDATE keeps the columns it omits],
   test_reads_and_mentions_are_not_writes.
2. SQL built from literal parts, in this plain subset only, rendered
   exactly: str literals, and names bound only to renderable str values; a
   conditional expression of them; concatenation with +; f-string fields,
   and str.format or % fields, that are exactly {}, {name}, {0}, %s or
   %(name)s, with no spec and no conversion, their values str literals or
   names bound only to str values; and join, replace, strip, lstrip, rstrip,
   removeprefix, removesuffix, upper and lower with str-literal arguments
   (join's a list or tuple display of them, or a name bound once to a tuple
   display). Everything else is a part not known: any spec, conversion or
   other % code, a numeric or bytes value, a spread, a computed or duplicate
   key, a starred argument, any other str method or call, a loop's target,
   values that do not match their fields, and any bound exceeded
   (MAX_RENDERINGS renderings: the text that varies becomes not known). A
   part not known keeps the known text around it, and a statement run with
   one whose known text has a write verb is dynamic SQL (it fails closed) in
   any scope.
   test_each_way_to_write_tr_key_limit_is_found[concatenated table
   name | f-string table from a constant | format() SQL | %-formatted SQL |
   named format alternatives | str.replace SQL | % with an unknown argument |
   .format with an unknown argument | a join of non-literals],
   test_the_round_15_forms_injected_into_a_listed_scope_are_caught[4. a
   .format precision (the reviewer's) | 4. an f-string precision | 4. a
   %-format precision | 5. rstrip with a literal argument (the reviewer's) |
   5. replace and join with literal arguments | 5. rstrip with an argument
   not known],
   test_the_round_16_forms_injected_into_a_listed_scope_are_caught[1. a
   %-mapping key with punctuation (the reviewer's) | 1. an empty %-precision
   (the reviewer's) | 2. a * width before another field (the reviewer's) |
   3. 64 literal alternatives (the reviewer's) | a table with a part not
   known],
   test_the_round_17_forms_injected_into_a_listed_scope_are_caught[1. a %
   mapping overwritten by a spread (the reviewer's) | 2. a numeric branch
   given to %c (the reviewer's) | 3. a bytes value in an f-string (the
   reviewer's) | 4. replace past the bound (the reviewer's)],
   test_each_construction_is_rendered_whole_or_not_known,
   test_a_bound_on_renderings_keeps_the_known_text_around_what_varies,
   test_a_write_with_a_part_not_known_is_dynamic_in_any_scope.
3. Names and containers made in the same function holding the SQL.
   test_each_way_to_write_tr_key_limit_is_found[a walrus | an element bound
   and given to a helper | appended to a list given to a helper | put into a
   container through an alias | statements looped over].
4. Augmented assignment.
   test_each_way_to_write_tr_key_limit_is_found[augmented SQL | conditional
   += | a string built with +=, then run | added with += through an alias].
5. Helpers of the same module, the callee being the helper by every
   binding. test_each_way_to_write_tr_key_limit_is_found[a helper of the
   module that changes the statement | a chain of helpers | a helper given
   the statement by keyword | a decorated helper of the module | a helper
   rebound by another module's wrapper],
   test_the_round_14_identity_forms_injected_into_a_listed_scope_are_caught[1.
   a helper rebound by a decorator's call (the reviewer's) | 1. a class's
   method reassigned (the reviewer's) | 1. self rebound before a call of the
   helper's name].
6. Executors by name and through settled aliases, and getattr with a
   literal name. test_each_way_to_write_tr_key_limit_is_found[aliased
   executor | getattr executor (the reviewer's) | an alias of getattr |
   getattr with a default executor],
   test_the_round_15_forms_injected_into_a_listed_scope_are_caught[2. an
   executor attribute rebound (the reviewer's)].
7. Spanner mutation calls and mutation objects.
   test_each_way_to_write_tr_key_limit_is_found[positional computed table,
   limit columns | bulk **kwargs | mutable mutation columns | a REPLACE
   mutation (nulls what it omits) | insert_or_update keeps the columns it
   omits | a REPLACE payload | REST payload | the backstop through the table
   constant], test_the_scanner_finds_every_writer_the_chokepoints_replaced.
8. Batch DML. test_each_way_to_write_tr_key_limit_is_found[batch DML | a list
   literal batched].
9. Shell, SQL, YAML and Go text. test_each_text_form_is_found[shell DML in an
   argument | shell heredoc DML | the backstop in a heredoc | SQL file delete |
   SQL upsert of a limit | Go literal mutation | Go format | YAML job | a
   table split by a continuation (the reviewer's)].
10. The backstop: a scope holding the table, a limit column and a write.
    test_each_way_to_write_tr_key_limit_is_found[the backstop: table, limit
    column and a write in one scope | the backstop across a nested function |
    the backstop sees a limit column split across literals].
11. A value naming the table leaving its scope by another route (returned,
    stored, raised, given to a callee not known to be builtin, read through
    a namespace or frame). test_each_way_to_write_tr_key_limit_is_found[
    returned | stored on an attribute | raised in a message (the handler
    reads it in another scope) | logged | locals() (the reviewer's) | globals()
    (the reviewer's) | a frame's locals], and the injections of review rounds
    7 to 15 into a listed scope.
12. Identities read from bindings: builtin names, getattr, plain
    decorators, namespace readers imported under another name, a walrus in
    an annotation. test_the_identity_resolver_decides_by_bindings_not_spelling,
    test_an_imported_identity_holds_only_while_nothing_rebinds_it,
    test_a_namespace_reader_counts_by_any_identity_it_may_have.
13. Reviewed exceptions, pinned by behaviour and by a fingerprint of their
    source. test_only_the_chokepoints_write_a_limit_column, the pins in
    tests/test_key_limit_writer_exceptions.py,
    test_an_exceptions_fingerprint_covers_every_definition_of_its_name.
14. This repository, as it is. test_every_writer_of_tr_key_limit_is_known,
    test_the_writer_inventory_has_no_dead_entries.

That is the whole claim. The scanner is a regression guard for these forms
and for code shaped like this repository's. It fails closed where it sees
a value meet something it cannot resolve (an operand, a receiver, a callee
or a class it cannot show is builtin, a part of a statement it cannot
render), but it makes no promise about other Python constructs: those are
code review's job, and so are the forms below. Review rounds 2 to 14 each
found another construct, most of them in the inference of provably builtin
or in taking a name for what its spelling says; each was closed, and others
surely remain.

Not modelled (code review covers these):

- user-defined special-method adapters (__eq__, __radd__, __getitem__,
  __set_name__, a codec, a descriptor, ...) that meet the value where the
  scanner does not see the meeting, or that it wrongly takes for builtin;
- namespaces and frames reached from another module: a module's or a
  class's names read or rebound through the module object there
  (vars(module), module.__dict__, module.name = ..., a computed getattr or
  setattr, sys.modules), a builtin or a module's attribute rebound so (a
  helper's name bound as an attribute in any scanned module does count),
  a name imported from a scanned module (identities are not followed
  across modules: such a name is not known, so a namespace reader
  imported so is not seen as one), and a callee, tracer, debugger or
  error reporter reading a caller's locals (sys._getframe, inspect, the
  tb_frame of a traceback; f_locals writes through from Python 3.13);
- callees taken for executors by their names (execute, execute_sql, ...):
  what a user-defined method of that name does with the statement is
  judged only where the method is written;
- code outside the scan: tests, dependencies, C extensions, ctypes, gc,
  import and audit hooks reaching into a scanned module's objects;
- SQL whose table or limit column arrives from another module or from
  data at run time: a statement built in one module from text that does
  not name the table and completed in another, or an interpolated value
  carrying SQL. A module constant naming the table (such as
  KEY_LIMIT_TABLE) is followed into every scanned module that reads it,
  and a cross-module helper whose result runs in a scope that mentions the
  table fails closed (test_a_cross_module_helpers_statement_fails_closed);
- a statement whose known text has no write verb made into a write by a
  part not known (an interpolated value carrying SQL, or a str method whose
  arguments are not known rewriting the text): a part not known makes a
  statement dynamic only when its known text has a write verb, and a str
  method's result with arguments not known is rendered as the receiver's
  text followed by a part not known.

api_key entity writers are out of scope by design: whatever writes an entity,
the mirror changes only through the guarded functions above.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import itertools
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, cast

ROOT = Path(__file__).resolve().parents[1]

TABLE = "tr_key_limit"
LIMIT_COLUMNS = frozenset({"limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro"})

_TOKEN = re.compile(r"(?<![A-Za-z0-9_])tr_key_limit(?![A-Za-z0-9_])", re.IGNORECASE)
_LOCK = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_DML_WORD = re.compile(r"\b(?:insert|update|delete|merge|upsert|replace|truncate|copy)\b", re.IGNORECASE)
_READ_WORD = re.compile(r"\bselect\b|\b(?:create|alter|drop)\s+(?:table|index|unique|null_filtered)\b",
                        re.IGNORECASE)
_DML = (r"(?:insert\s+or\s+update(?:\s+into)?|insert\s+or\s+ignore(?:\s+into)?|insert\s+into|insert|"
        r"update|merge(?:\s+into)?|upsert(?:\s+into)?|replace\s+into|delete\s+from|delete)")
_HINT = r"(?:@\{[^}]*\}\s*)?"
_PLACEHOLDER = r"""["'`]?(?:\{[^}]*\}|%s|%\(\w+\)s|\$\{?\w+\}?|[\w.$]*\x00[\w.$\x00]*)["'`]?"""


def _dynamic_dml(placeholder: str) -> re.Pattern[str]:
    """DML whose table is filled in at run time, shaped as SQL (so a message
    such as "could not update %s" is not one)."""
    return re.compile(
        r"\b(?:update\s+" + _HINT + placeholder + r"\s+set\b"
        r"|insert(?:\s+or\s+\w+)?\s+into\s+" + _HINT + placeholder
        + r"|insert\s+or\s+update\s+" + _HINT + placeholder
        + r"|insert\s+" + _HINT + placeholder + r"\s*\("
        + r"|delete\s+from\s+" + _HINT + placeholder
        + r"|delete\s+" + _HINT + placeholder + r"\s+where\b"
        + r"|(?:merge|upsert)\s+(?:into\s+)?" + _HINT + placeholder
        + r"|replace\s+into\s+" + _HINT + placeholder + r")",
        re.IGNORECASE,
    )


_DYNAMIC = _dynamic_dml(_PLACEHOLDER)
#: In a shell, SQL, Terraform, YAML or Go file the table may also come from a
#: command substitution ($(...), or `...` holding a space or $) or a psql
#: variable (:name, :'name', :"name").
_TEXT_DYNAMIC = _dynamic_dml(r"""(?:""" + _PLACEHOLDER + r"""|\$\([^)]*\)|`[^`]*[\s$][^`]*`|:['"]?\w+['"]?)""")
# A string that is only the head of such DML ("UPDATE ", "DELETE FROM "),
# its table appended at run time.
_DYNAMIC_HEAD = re.compile(r"^\s*" + _HINT + _DML + r"""\s+["'`]?$""", re.IGNORECASE)
_GO_MUTATION = re.compile(r"spanner\.(?:Insert|Update|InsertOrUpdate|Replace|Delete)\w*\(\s*([^,\s)]+)")

MUTATION_VERBS = frozenset({"insert_or_update", "insert_or_update_map", "insert_map", "update_map", "replace_map"})
AMBIGUOUS_VERBS = {"insert": 3, "update": 3, "replace": 3, "delete": 2}
MUTATION_KEYWORDS = frozenset({"table", "columns", "values", "keyset"})
MAX_RENDERINGS = 32
#: Stands for a part of a string the scanner cannot resolve to literals (an
#: f-string interpolation, a % or .format argument, a str method's result).
UNKNOWN = "\x00"

PY_EXCLUDED = ("tests", "tests-e2e", ".venv", "node_modules", ".git", "docs", "frontend")
TEXT_SUFFIXES = (".sh", ".sql", ".tf", ".yaml", ".yml", ".go")
TEXT_EXCLUDED = (*PY_EXCLUDED, "proofs")


def _is_docstring(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    return isinstance(parent, ast.Expr)


def names_table(text: str) -> bool:
    return _TOKEN.search(text) is not None


def classify(text: str) -> str | None:
    """What a string naming the table is: DML (``sql``), the bare table name
    (``name``), or None for a read, DDL or a message that mentions it."""
    if not names_table(text):
        return None
    unlocked = _LOCK.sub(" ", text)
    if _DML_WORD.search(unlocked):
        return "sql"
    if _READ_WORD.search(unlocked):
        return None
    if text.strip(" \t\r\n'\"`").lower() == TABLE:
        return "name"
    return None


class _Token(NamedTuple):
    """One SQL token: ``word`` (lower-cased), ``ident`` (a double-quoted or
    backtick name, its quotes off, lower-cased), ``string`` (a literal, as
    written) or ``punct`` (one character), with its span in the text."""

    kind: str
    text: str
    start: int
    end: int


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_OTHER_LINE_ENDS = "\v\f\x85\u2028\u2029"
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_PUNCT = frozenset("(),;=.<>+-*/%:@{}[]!|&~^?0123456789")


def _quoted_end(sql: str, start: int, quote: str) -> int | None:
    """The index past the quote closing the one at ``start``; a doubled
    quote stays inside. None for anything PostgreSQL and GoogleSQL read
    differently: a backslash (an escape in one, a character in the other)
    or a tripled quote (GoogleSQL's triple-quoted literal); and for a quote
    that never closes."""
    if sql.startswith(quote * 3, start):
        return None
    i = start + 1
    while i < len(sql):
        char = sql[i]
        if char == "\\":
            return None
        if char == quote:
            if sql.startswith(quote * 2, i):
                i += 2
                continue
            return i + 1
        i += 1
    return None


def sql_tokens(sql: str) -> list[_Token] | None:
    """``sql`` as tokens under a small lexicon both PostgreSQL and GoogleSQL
    read alike: words, single-quoted literals ('' inside), double-quoted and
    backtick names, PostgreSQL dollar quotes, ``--`` comments, and the
    punctuation in _PUNCT. None for anything else, which the callers count
    as writing every column: any block comment (PostgreSQL's nest,
    GoogleSQL's do not), ``#`` (a GoogleSQL comment, a PostgreSQL operator),
    a backslash, a tripled quote, a prefixed literal (E'', B'', r'', ...),
    or a quote or dollar quote that never closes."""
    tokens: list[_Token] = []
    i = 0
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
        elif sql.startswith("--", i):
            # Both dialects end a line comment at \n or \r. Any other
            # line-like character in one (\v, \f, NEL, LS, PS) is not read
            # alike, so it fails closed.
            stop = len(sql)
            for terminator in "\n\r":
                found = sql.find(terminator, i)
                if found >= 0:
                    stop = min(stop, found)
            if any(char in sql[i:stop] for char in _OTHER_LINE_ENDS):
                return None
            i = stop
        elif sql.startswith("/*", i) or sql.startswith("*/", i):
            return None
        elif char in "'\"`":
            if tokens and tokens[-1].kind == "word" and tokens[-1].end == i:
                return None  # a prefixed literal
            end = _quoted_end(sql, i, char)
            if end is None:
                return None
            body = sql[i + 1:end - 1]
            tokens.append(_Token("string" if char == "'" else "ident", body if char == "'" else body.lower(), i, end))
            i = end
        elif (dollar := _DOLLAR_QUOTE.match(sql, i)) is not None:
            close = sql.find(dollar.group(0), dollar.end())
            if close < 0:
                return None
            end = close + len(dollar.group(0))
            tokens.append(_Token("string", sql[dollar.end():close], i, end))
            i = end
        elif (word := _WORD.match(sql, i)) is not None:
            tokens.append(_Token("word", word.group(0).lower(), i, word.end()))
            i = word.end()
        elif char in _PUNCT:
            tokens.append(_Token("punct", char, i, i + 1))
            i += 1
        else:
            return None
    return tokens


def _is(tokens: list[_Token], i: int, kind: str, *texts: str) -> bool:
    return 0 <= i < len(tokens) and tokens[i].kind == kind and (not texts or tokens[i].text in texts)


#: Words that make a statement a write (or a statement this grammar does not
#: clear): each must be one the grammar accounts for.
_WRITE_WORDS = frozenset({"insert", "update", "delete", "merge", "upsert", "replace", "truncate", "copy", "call",
                          "do", "execute", "with"})
_SET_END = frozenset({"where", "returning"})


def _statements(tokens: list[_Token]) -> list[list[_Token]]:
    statements: list[list[_Token]] = [[]]
    for token in tokens:
        if token.kind == "punct" and token.text == ";":
            statements.append([])
        else:
            statements[-1].append(token)
    return [statement for statement in statements if statement]


def _hint_end(tokens: list[_Token], i: int) -> int | None:
    """Past a leading Spanner statement hint ``@{NAME=VALUE, ...}`` at ``i``
    (``i`` itself when there is none); None for any other shape."""
    if not _is(tokens, i, "punct", "@"):
        return i
    j = i + 1
    if not _is(tokens, j, "punct", "{"):
        return None
    j += 1
    while True:
        if not (_is(tokens, j, "word") and _is(tokens, j + 1, "punct", "=") and _is(tokens, j + 2, "word")):
            return None
        j += 3
        if _is(tokens, j, "punct", "}"):
            return j + 1
        if not _is(tokens, j, "punct", ","):
            return None
        j += 1


def _target_end(tokens: list[_Token], i: int) -> int | None:
    """Past the table at ``i``: ``tr_key_limit`` or ``schema.tr_key_limit``,
    plain words only. None for any other target (ONLY, brackets, quotes,
    another table)."""
    if _is(tokens, i, "word", TABLE) and not _is(tokens, i + 1, "punct", "."):
        return i + 1
    if (_is(tokens, i, "word") and not _is(tokens, i, "word", "only") and _is(tokens, i + 1, "punct", ".")
            and _is(tokens, i + 2, "word", TABLE) and not _is(tokens, i + 3, "punct", ".")):
        return i + 3
    return None


def _column_list(tokens: list[_Token], i: int) -> tuple[list[str], int] | None:
    """``( word, word, ... )`` at ``i``: its names and the index past it."""
    if not _is(tokens, i, "punct", "("):
        return None
    names = []
    j = i + 1
    while True:
        if not _is(tokens, j, "word"):
            return None
        names.append(tokens[j].text)
        j += 1
        if _is(tokens, j, "punct", ")"):
            return names, j + 1
        if not _is(tokens, j, "punct", ","):
            return None
        j += 1


def _set_list(tokens: list[_Token], i: int) -> tuple[set[str], int] | None:
    """The SET list at ``i``: ``target = expression, ...``, each target a
    plain word or a bracketed list of them, up to WHERE, RETURNING, THEN
    RETURN or the end. Its targets and the index where it ends; None for
    any other shape (FROM after it, an empty expression, unbalanced
    brackets)."""
    targets: set[str] = set()
    j = i
    while True:
        if _is(tokens, j, "word") and _is(tokens, j + 1, "punct", "="):
            targets.add(tokens[j].text)
            j += 2
        elif (listed := _column_list(tokens, j)) is not None and _is(tokens, listed[1], "punct", "="):
            targets.update(listed[0])
            j = listed[1] + 1
        else:
            return None
        depth = 0
        start = j
        while j < len(tokens):
            token = tokens[j]
            if token.kind == "punct" and token.text in "([":
                depth += 1
            elif token.kind == "punct" and token.text in ")]":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and (token.kind == "punct" and token.text == ","
                                 or _is(tokens, j, "word", *_SET_END, "from")
                                 or (_is(tokens, j, "word", "then") and _is(tokens, j + 1, "word", "return"))):
                break
            j += 1
        if j == start or depth != 0:
            return None
        if _is(tokens, j, "punct", ","):
            j += 1
            continue
        if _is(tokens, j, "word", "from"):
            return None
        return targets, j


def _cleared_columns(statement: list[_Token]) -> set[str] | None:
    """The tr_key_limit columns one statement writes, when it is one this
    grammar clears; None (every column) for anything else.

    A statement with no write word is a read. Otherwise it must be exactly
    one of, after an optional leading Spanner hint:
      UPDATE <table> [[AS] alias] SET <set list> [WHERE ...]
      INSERT [OR UPDATE | OR IGNORE] [INTO] <table> (<columns>) ...
          [ON CONFLICT ... DO NOTHING | DO UPDATE SET <set list> ...]
      DELETE [FROM] <table> [WHERE ...]
    with no other write word anywhere in it (a CTE, a subquery's DML, a
    MERGE, a COPY, ...)."""
    writes = [i for i, token in enumerate(statement) if token.kind == "word" and token.text in _WRITE_WORDS]
    if not writes:
        return set()
    begin = _hint_end(statement, 0)
    if begin is None or writes[0] != begin:
        return None
    verb = statement[begin].text
    accounted = {begin}
    columns: set[str] = set()
    if verb == "update":
        j = _target_end(statement, begin + 1)
        if j is None:
            return None
        if _is(statement, j, "word", "as") and _is(statement, j + 1, "word"):
            j += 2
        elif _is(statement, j, "word") and not _is(statement, j, "word", "set"):
            j += 1
        if not _is(statement, j, "word", "set"):
            return None
        parsed = _set_list(statement, j + 1)
        if parsed is None:
            return None
        columns = parsed[0]
    elif verb == "insert":
        j = begin + 1
        if _is(statement, j, "word", "or") and _is(statement, j + 1, "word", "update", "ignore"):
            accounted.add(j + 1)
            j += 2
        if _is(statement, j, "word", "into"):
            j += 1
        j = _target_end(statement, j)
        listed = _column_list(statement, j) if j is not None else None
        if listed is None:
            return None
        columns = set(listed[0])
        for k in range(listed[1], len(statement)):
            if _is(statement, k, "word", "do") and _is(statement, k + 1, "word", "update"):
                if not (_is(statement, k + 2, "word", "set") and any(
                        _is(statement, m, "word", "on") and _is(statement, m + 1, "word", "conflict")
                        for m in range(listed[1], k))):
                    return None
                parsed = _set_list(statement, k + 3)
                if parsed is None:
                    return None
                columns |= parsed[0]
                accounted.update({k, k + 1})
            elif _is(statement, k, "word", "do") and _is(statement, k + 1, "word", "nothing"):
                accounted.add(k)
    elif verb == "delete":
        j = begin + 1
        if _is(statement, j, "word", "from"):
            j += 1
        if _target_end(statement, j) is None:
            return None
    else:
        return None
    if set(writes) - accounted:
        return None
    return columns


def written_columns(text: str) -> set[str] | None:
    """The tr_key_limit columns the statements in ``text`` may write; None
    for every column. Each statement that mentions the table must be one
    the grammar clears (_cleared_columns), whatever the other statements
    are; a text that does not tokenize under the lexicon (sql_tokens) fails
    closed whole."""
    if UNKNOWN in text:
        return None  # a part not known at scan time could be any SQL, even inside quotes
    tokens = sql_tokens(text)
    if tokens is None:
        return None
    columns: set[str] = set()
    for statement in _statements(tokens):
        if not names_table(text[statement[0].start:statement[-1].end]):
            continue
        if any(_is(statement, i, "punct", "{") and _is(statement, i + 1, "punct", "}") for i in range(len(statement))):
            return None  # a part filled in at run time could be any SQL
        cleared = _cleared_columns(statement)
        if cleared is None:
            return None
        columns |= cleared
    return columns


def assigns_limit_column(text: str) -> bool:
    """Whether DML naming the table may write a limit column: unless every
    statement that mentions the table is one the grammar clears and none of
    them writes a limit column, it may."""
    if classify(text) != "sql":
        return False
    columns = written_columns(text)
    return columns is None or bool(columns & LIMIT_COLUMNS)


def is_dynamic(text: str) -> bool:
    return _DYNAMIC.search(text) is not None


Scope = ast.AST


_UNKNOWN_BINDING = "<unknown>"


def _unknown() -> ast.Name:
    """A binding whose value is not known (an unpacked target, a with or
    except name, a def or class name, an import from a module not scanned, a
    match capture): no value, and never provably builtin."""
    return ast.Name(id=_UNKNOWN_BINDING, ctx=ast.Load())


def _stored_names(target: ast.AST) -> list[str]:
    return [node.id for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)]


class _Scope(NamedTuple):
    found: dict[str, list[ast.AST]]
    declared: dict[str, str]
    outward: dict[str, list[ast.AST]]


_TYPE_ALIAS = getattr(ast, "TypeAlias", ())
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _annotations(function: ast.AST) -> list[ast.expr]:
    """A function's parameter and return annotations (none for a lambda)."""
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    arguments = function.args
    every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
             *([arguments.vararg] if arguments.vararg else []), *([arguments.kwarg] if arguments.kwarg else [])]
    return [annotation for annotation in [*(arg.annotation for arg in every), function.returns]
            if annotation is not None]


def _scope_nodes(start: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """The nodes of one scope, from its statements: a nested function's or
    lambda's body, a class body and a comprehension are other scopes (their
    decorators, defaults, annotations, bases and first iterable are this
    one's, and so is a walrus inside a comprehension or an annotation)."""
    queue = list(start)
    while queue:
        node = queue.pop()
        yield node
        if isinstance(node, _FUNCTIONS):
            arguments = node.args
            queue += [*arguments.defaults, *(d for d in arguments.kw_defaults if d is not None)]
            if not isinstance(node, ast.Lambda):
                queue += [*node.decorator_list, *_annotations(node)]
        elif isinstance(node, ast.ClassDef):
            queue += [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]
        elif isinstance(node, _COMPREHENSIONS):
            queue.append(node.generators[0].iter)
            for sub in _scope_nodes([node.elt] if not isinstance(node, ast.DictComp) else [node.key, node.value]):
                if isinstance(sub, ast.NamedExpr):
                    queue.append(sub)
            for generator in node.generators:
                for sub in _scope_nodes([generator.iter, *generator.ifs]):
                    if isinstance(sub, ast.NamedExpr):
                        queue.append(sub)
        else:
            queue += list(ast.iter_child_nodes(node))


def _collect(node: ast.AST, found: dict[str, list[ast.AST]], appended: dict[str, list[ast.AST]],
             local: set[str]) -> None:
    """Record the binding one node of a scope makes: name -> each value it
    may be bound to (x += piece goes to appended)."""
    def bind(name: str, value: ast.AST) -> None:
        found.setdefault(name, []).append(value)

    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        if isinstance(node.op, ast.Add):
            appended.setdefault(node.target.id, []).append(node.value)
        else:
            bind(node.target.id, ast.Name(id="<parameter>"))
    elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
        for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
            if isinstance(target, ast.Name):
                bind(target.id, node.value)
            else:
                for name in _stored_names(target):
                    bind(name, _unknown())  # unpacked: not the value itself
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        for name in _stored_names(node.target):
            bind(name, ast.Starred(value=node.iter))
    elif isinstance(node, ast.NamedExpr):
        bind(node.target.id, node.value)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            for name in _stored_names(item.optional_vars) if item.optional_vars is not None else []:
                bind(name, _unknown())
    elif isinstance(node, ast.ExceptHandler) and node.name:
        bind(node.name, _unknown())
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bind(node.name, _unknown())
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        scanned = isinstance(node, ast.ImportFrom) and (
            node.level > 0 or (node.module or "").split(".")[0] in local)
        for alias in node.names:
            if alias.name != "*":
                # from a scanned module: the name it has there (its bindings
                # are in the index); a module, or a name from elsewhere: not known.
                bind(alias.asname or alias.name.split(".")[0],
                     ast.Name(id=alias.name, ctx=ast.Load()) if scanned else _unknown())
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        bind(node.name, _unknown())
    elif isinstance(node, ast.MatchMapping) and node.rest:
        bind(node.rest, _unknown())
    elif isinstance(node, _TYPE_ALIAS) and isinstance(getattr(node, "name", None), ast.Name):
        bind(node.name.id, _unknown())  # type X = ... (3.12)


#: Namespace readers: the calling scope's names (locals, vars and dir with
#: no argument), the module's (globals, and the module object by sys.modules,
#: __import__ or importlib.import_module), an object's whole namespace
#: (vars, dir, __dict__, inspect's member readers), every frame's
#: (sys._getframe, inspect's frame readers, f_locals, a closure's cells),
#: and eval and exec, which see both the calling scope and the module. A
#: getattr with a computed name reads one attribute of the object it is
#: given: it reaches this module's names only with the module object in
#: hand, which here takes one of the readers above, and from another module
#: is the limit the docstring lists. setattr with a computed name rebinds.
_FRAME_CALLS = frozenset({"_getframe", "_current_frames", "currentframe", "stack", "trace", "getouterframes",
                          "getinnerframes", "walk_stack", "walk_tb", "settrace", "setprofile", "getframeinfo",
                          "get_referrers", "get_referents", "get_objects"})
#: The modules the frame readers above come from, when one is reached by
#: identity (from inspect import stack as s) rather than as an attribute.
_FRAME_MODULES = frozenset({"sys", "inspect", "traceback", "gc"})
_FRAME_ATTRIBUTES = frozenset({"f_locals", "f_globals", "f_builtins", "f_back", "tb_frame", "gi_frame", "cr_frame",
                               "ag_frame", "__closure__", "cell_contents"})
_OBJECT_READERS = frozenset({"getmembers", "getmembers_static", "getattr_static"})
_NAMESPACE_BUILTINS = frozenset({"locals", "globals", "vars", "dir", "eval", "exec", "breakpoint"})
#: The builtins a reference to which can rebind a module's names.
_REBINDERS = frozenset({"globals", "eval", "exec", "breakpoint"})
#: The builtins' names: a name no binding in the module settles otherwise is
#: the builtin of that name.
_BUILTIN_NAMES = frozenset(dir(builtins))
#: An identity no binding settles (a parameter, a call's result, an import
#: from a scanned module, a name bound nowhere that is not a builtin).
_UNKNOWN_IDENTITY = "?"
_TYPE_PARAMS = tuple(getattr(ast, kind) for kind in ("TypeVar", "ParamSpec", "TypeVarTuple") if hasattr(ast, kind))

#: One binding of a name or an attribute: ("value", the expression it is
#: assigned), ("identity", a dotted path or a definition's) or ("unknown",
#: None).
_Binding = tuple[str, ast.AST | str | None]
_UNBOUND: _Binding = ("unknown", None)


def _assigned(target: ast.AST, parent: ast.AST | None) -> _Binding:
    """What a name or attribute target is bound to: the value of a plain
    assignment or a walrus; anything else (unpacked, a loop, with, +=, del,
    an annotation alone) is not known."""
    if isinstance(parent, ast.Assign) and any(target is other for other in parent.targets):
        return ("value", parent.value)
    if isinstance(parent, (ast.AnnAssign, ast.NamedExpr)) and parent.target is target and parent.value is not None:
        return ("value", parent.value)
    return _UNBOUND


# Where the scanner reads a name as written, and why (the identity
# decisions the docstring lists go through _Identities below):
#
# - rules that add findings read the spelling beside the identity, so a
#   name that may be a namespace or frame reader, a reflection builtin,
#   setattr, an executor or a helper counts either way (_namespace_kinds,
#   _builtin, _refers_to, _callee_names, _statements_run, scope_marks);
# - executors and mutations are recognised by their own names (the
#   documented limit: the DB-API objects are outside the scan); the escape
#   rule stops at one only when the callee is settled (_settled_callee);
# - a method of a receiver proven builtin (a str, or a container made in the
#   function) is that type's method by its name, the receiver's type fixing
#   it (_step, _argument_step, _builtin_value, _inert_use); string renderings
#   read str methods by name, and a receiver not resolved to literals
#   renders as a part not known (_Module.texts);
# - a keyword argument binds the parameter of its name (_sink_arguments),
#   an executor's statement keywords are its API's (_executed_statement),
#   and a helper _helper resolved is matched to its own sinks by its name;
# - a local name is matched to another of its spelling only where both
#   resolve to the same scope (_dict_fields, _sink_entries,
#   _parameters_reached, _bindings_within);
# - syntax and sentinels (import *, "<parameter>", "<unknown>") are not names;
# - a fingerprint holds every statement binding a name, by its spelling, on
#   purpose (_rebindings).


def _spelled(func: ast.AST) -> str:
    """A callee's own name as written: read only by rules that add findings
    (beside its identity), never by a rule that clears."""
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _definition_identity(node: ast.AST) -> str:
    return ("class:" if isinstance(node, ast.ClassDef) else "def:") + str(id(node))


class _Identities:
    """What a name or an attribute is, read from its bindings: the resolver
    the scanner asks for builtin names, the getattr rewrite, plain
    decorators, the module's helpers, namespace and frame readers, and
    setattr and executor aliases.

    An identity is a dotted path (builtins.len, functools.cache, sys, a
    module and an attribute of it) or a definition of the module (def: or
    class: and the node). Bindings are collected module wide, whatever scope
    they are in: a plain assignment or a walrus anywhere (an annotation, a
    default, a decorator or a comprehension included) binds the value; an
    import binds the module or the name it imports (followed with or without
    as); a def or class binds itself; a parameter, a loop, with, except,
    match, del, +=, unpacked or type-parameter target binds something not
    known. Attribute bindings are collected the same way (obj.x = ...,
    setattr(obj, "x", ...), del obj.x), with the receiver.

    of(node) is the identity every binding gives a name, followed through
    names and module attributes, or None (not known: every rule that relies
    on an identity then fails closed). A name no binding anywhere in the
    module touches is the builtin of its name; any binding of it, in any
    form and any scope, must resolve to the same identity; and no name is
    known in a module that imports * or may write its own namespace. A
    module attribute (functools.cache) is not known once anything in the
    module may rebind that attribute of that module.

    may(node) is every identity a name or attribute may have, for the rules
    that add findings: a builtin's name may be that builtin wherever it is
    read, bound or not, and a binding adds what it may be."""

    def __init__(self, tree: ast.Module, local: Iterable[str] = (),
                 foreign: frozenset[tuple[str, str]] = frozenset()) -> None:
        self.tree = tree
        self.local = set(local)
        #: (module, attribute) pairs some scanned module sets on that module
        #: object (functools.cache = ..., setattr(builtins, "len", ...)).
        self.foreign = foreign
        #: Every node of the tree (breadth first, as ast.walk), and each one's parent.
        self.nodes: list[ast.AST] = [tree]
        self.parents: dict[int, ast.AST] = {}
        for node in self.nodes:
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node
                self.nodes.append(child)
        self.names: dict[str, list[_Binding]] = {}
        #: Each attribute binding, by attribute name, with its receiver.
        self.attributes: dict[str, list[tuple[ast.AST, _Binding]]] = {}
        #: The module's defs and classes, by identity.
        self.definitions: dict[str, ast.AST] = {}
        #: The identities an import binds as a module (import x, import x as y).
        self.modules: set[str] = set()
        self.star = False
        self.rewrites = False
        #: Whether code may rebind any attribute of any object (setattr by a
        #: computed name, __setattr__, __dict__, vars(obj), exec, a frame).
        self.any_attribute = False
        self._may: dict[object, frozenset[str]] = {}
        self._of: dict[object, str | None] = {}
        for node in self.nodes:
            self._record(node)
        for node in self.nodes:
            if isinstance(node, ast.Call) and (self.may_builtins(node.func) | {_spelled(node.func)}) & {
                    "setattr", "delattr"}:
                if len(node.args) >= 2 and _literal_name(node.args[1]):
                    value = ("value", node.args[2]) if len(node.args) == 3 else _UNBOUND
                    name = str(cast(ast.Constant, node.args[1]).value)
                    self.attributes.setdefault(name, []).append((node.args[0], value))
                else:
                    self.any_attribute = True
        self._may.clear()  # an attribute's may reads the bindings setattr adds
        #: Every node reading a namespace (_namespace_kinds), and its kind.
        self.namespace_reads: list[tuple[ast.AST, str]] = [
            (node, kind) for node in self.nodes for kind in sorted(self._namespace_kinds(node))]
        kinds = {kind for _node, kind in self.namespace_reads}
        # A module object in hand (sys.modules, __import__, a computed getattr on
        # a module) may have any of its names set as attributes.
        self.rewrites = bool(kinds & {"globals", "scope", "frame", "rebind", "modules"})
        self.any_attribute = self.any_attribute or bool(kinds & {"rebind", "object", "scope", "frame"})
        #: Whether the module refers to a builtin that can rebind its names.
        self.rebinder = any(isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                            and self.may_builtins(node) & _REBINDERS for node in self.nodes)
        self._of.clear()

    def _bind(self, name: str, binding: _Binding) -> None:
        self.names.setdefault(name, []).append(binding)

    def _record(self, node: ast.AST) -> None:
        parent = self.parents.get(id(node))
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            self._bind(node.id, _assigned(node, parent))
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            self.attributes.setdefault(node.attr, []).append((node.value, _assigned(node, parent)))
        elif isinstance(node, ast.arg):
            self._bind(node.arg, _UNBOUND)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identity = _definition_identity(node)
            self.definitions[identity] = node
            self._bind(node.name, ("identity", identity))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name if alias.asname else alias.name.split(".")[0]
                self.modules.add(module)
                self._bind(alias.asname or module, ("identity", module))
        elif isinstance(node, ast.ImportFrom):
            # From a scanned module (relative, or a top-level name of the
            # scan): its names' identities are not followed, so not known.
            scanned = node.level > 0 or not node.module or node.module.split(".")[0] in self.local
            for alias in node.names:
                if alias.name == "*":
                    self.star = True
                else:
                    self._bind(alias.asname or alias.name,
                               _UNBOUND if scanned else ("identity", f"{node.module}.{alias.name}"))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.ExceptHandler, *_TYPE_PARAMS)):
            name = getattr(node, "name", None)
            if isinstance(name, str):
                self._bind(name, _UNBOUND)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(node.rest, _UNBOUND)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                self.names.setdefault(name, [])  # bound elsewhere, by the statements collected there

    # -- what a name may be (for rules that add findings) ----------------

    def may(self, node: ast.AST, visiting: frozenset[object] = frozenset()) -> frozenset[str]:
        if isinstance(node, ast.Name):
            key: object = ("name", node.id)
        elif isinstance(node, ast.Attribute):
            key = id(node)
        else:
            return frozenset({_UNKNOWN_IDENTITY})
        if not visiting and key in self._may:
            return self._may[key]
        if key in visiting:
            return frozenset()  # a cycle adds nothing the other bindings do not
        if len(visiting) > 12:
            return frozenset({_UNKNOWN_IDENTITY})
        found: set[str] = set()
        inner = visiting | {key}
        if isinstance(node, ast.Name):
            if node.id in _BUILTIN_NAMES:
                found.add("builtins." + node.id)
            if node.id in ("builtins", "__builtins__"):
                found.add("builtins")
            bindings = self.names.get(node.id) or []
            if self.star or not bindings:
                found.add(_UNKNOWN_IDENTITY)
            for binding in bindings:
                found |= self._may_binding(binding, inner)
        else:
            attribute = cast(ast.Attribute, node)
            for base in self.may(attribute.value, inner):
                found.add(_UNKNOWN_IDENTITY if base == _UNKNOWN_IDENTITY or base.startswith(("def:", "class:"))
                          else f"{base}.{attribute.attr}")
            for _receiver, binding in self.attributes.get(attribute.attr, []):
                found |= self._may_binding(binding, inner)
        result = frozenset(found)
        if not visiting:
            self._may[key] = result
        return result

    def _may_binding(self, binding: _Binding, visiting: frozenset[object]) -> set[str]:
        kind, payload = binding
        if kind == "identity":
            return {cast(str, payload)}
        if kind == "value" and isinstance(payload, ast.AST):
            return self._may_value(payload, visiting)
        return {_UNKNOWN_IDENTITY}

    def _may_value(self, value: ast.AST, visiting: frozenset[object]) -> set[str]:
        if isinstance(value, (ast.Name, ast.Attribute)):
            return set(self.may(value, visiting))
        if isinstance(value, ast.NamedExpr):
            return self._may_value(value.value, visiting)
        if isinstance(value, ast.IfExp):
            return self._may_value(value.body, visiting) | self._may_value(value.orelse, visiting)
        if isinstance(value, ast.BoolOp):
            return set().union(*(self._may_value(part, visiting) for part in value.values))
        return {_UNKNOWN_IDENTITY}

    def bound_may(self, node: ast.AST) -> set[str]:
        """What the bindings of a name may give it (its spelling aside)."""
        if not isinstance(node, ast.Name):
            return set(self.may(node))
        visiting = frozenset({("name", node.id)})
        return set().union(*(self._may_binding(binding, visiting) for binding in self.names.get(node.id, [])))

    def may_builtins(self, node: ast.AST) -> set[str]:
        """The builtins a name or attribute may be, by name."""
        return {identity[len("builtins."):] for identity in self.may(node)
                if identity.startswith("builtins.") and identity.count(".") == 1}

    def may_from(self, node: ast.AST, modules: Iterable[str]) -> set[str]:
        """The names, in those modules, a name or attribute may be."""
        modules = set(modules)
        return {identity.rpartition(".")[2] for identity in self.may(node) if identity.rpartition(".")[0] in modules}

    # -- what a name is (for rules that clear) ---------------------------

    def of(self, node: ast.AST, visiting: frozenset[object] = frozenset()) -> str | None:
        if isinstance(node, ast.Name):
            key: object = ("name", node.id)
        elif isinstance(node, ast.Attribute):
            key = id(node)
        else:
            return None
        if not visiting and key in self._of:
            return self._of[key]
        if key in visiting or len(visiting) > 12:
            return None  # a cycle, or too deep: not known
        result = self._of_now(node, visiting | {key})
        if not visiting:
            self._of[key] = result
        return result

    def _of_now(self, node: ast.AST, visiting: frozenset[object]) -> str | None:
        if isinstance(node, ast.Name):
            if self.star or self.rewrites or any(
                    self.may(receiver) & self.modules for receiver, _binding in self.attributes.get(node.id, [])):
                return None  # (module.len = ..., module an import, this module perhaps)
            builtin = "builtins" if node.id == "__builtins__" else (
                "builtins." + node.id if node.id in _BUILTIN_NAMES else None)
            fallback = self._trusted(builtin) if builtin is not None else None
            bindings = self.names.get(node.id)
            if bindings is None:
                return fallback
            found = {self._of_binding(binding, visiting) for binding in bindings}
            if builtin is not None:
                found.add(fallback)  # a read where no binding reaches it is the builtin
            return found.pop() if len(found) == 1 else None
        attribute = cast(ast.Attribute, node)
        base = self.of(attribute.value, visiting)
        if base is None or base.startswith(("def:", "class:")) or self._attribute_rebound(attribute.attr, base):
            return None
        return f"{base}.{attribute.attr}"

    def _of_binding(self, binding: _Binding, visiting: frozenset[object]) -> str | None:
        kind, payload = binding
        if kind == "identity":
            return self._trusted(cast(str, payload))
        value = payload
        while isinstance(value, ast.NamedExpr):
            value = value.value
        if kind == "value" and isinstance(value, (ast.Name, ast.Attribute)):
            return self.of(value, visiting)
        return None

    def _in_function(self, node: ast.AST) -> bool:
        """Whether a node runs in a function's own scope (its body; a
        default, decorator or annotation runs in the scope around it)."""
        child, parent = node, self.parents.get(id(node))
        while parent is not None:
            if isinstance(parent, _FUNCTIONS):
                body = parent.body if isinstance(parent.body, list) else [parent.body]
                if any(child is item for item in body):
                    return True
            child, parent = parent, self.parents.get(id(parent))
        return False

    def _trusted(self, identity: str) -> str | None:
        """A module attribute's identity (an import of it, or a builtin), or
        None once anything may rebind that attribute of that module
        (functools.cache = ... before from functools import cache)."""
        base, _, attr = identity.rpartition(".")
        if base and not identity.startswith(("def:", "class:")) and self._attribute_rebound(attr, base):
            return None
        return identity

    def _attribute_rebound(self, attr: str, base: str) -> bool:
        """Whether the scan may rebind an attribute of that name of the
        module base: any attribute binding of it in this module whose
        receiver may be that module or is not known, any rebinding by a
        computed name here, or a binding of it on that module elsewhere in
        the scan."""
        if self.any_attribute or (base, attr) in self.foreign:
            return True
        return any(base in may or _UNKNOWN_IDENTITY in may
                   for may in (self.may(receiver) for receiver, _binding in self.attributes.get(attr, [])))

    def builtin(self, node: ast.AST) -> str | None:
        """The builtin a name or attribute is, by every binding, else None."""
        identity = self.of(node)
        if identity is not None and identity.startswith("builtins.") and identity.count(".") == 1:
            return identity[len("builtins."):]
        return None

    # -- namespace readers -----------------------------------------------

    def _namespace_kinds(self, node: ast.AST) -> set[str]:
        """What a node reads the names of: "locals" (the calling scope's),
        "globals" (the module's), "scope" (both), "object" (an object's, a
        module or a class perhaps), "modules" (a module object by name) or
        "frame" (any scope's); "rebind" for a write by a computed name.
        These add findings, so a callee counts by every identity it may have
        (the builtin of its name wherever it is read, through an import with
        or without as, a name bound to it, an attribute of builtins) and by
        its spelling too (any object's locals(), a frame reader named as an
        attribute); a reader referred to other than by calling it is a frame
        read (it may be called anywhere)."""
        kinds: set[str] = set()
        if isinstance(node, ast.Call):
            func = node.func
            spelled = _spelled(func)
            readers = self.may_builtins(func)
            if isinstance(func, ast.Attribute) and (spelled in ("locals", "globals", "vars") or (
                    spelled in _NAMESPACE_BUILTINS and isinstance(func.value, ast.Call))):
                readers.add(spelled)  # any object's locals(), import_module("builtins").eval(...)
            if any(keyword.arg == "capture_locals" for keyword in node.keywords):
                kinds.add("frame")  # traceback's summaries keep every frame's locals
            if readers & {"getattr", "vars", "dir"} and node.args and "builtins" in self.may(node.args[0]):
                kinds.add("frame")  # any namespace reader, by a name computed at run time
            if readers & {"locals", "vars", "dir"} and not node.args and not node.keywords:
                kinds.add("locals")
                if not self._in_function(node):
                    kinds |= {"globals", "object"}  # a module's locals() is its globals(), a class body's its namespace
            if "globals" in readers:
                kinds.add("globals")
            if readers & {"vars", "dir"} and (node.args or node.keywords):
                kinds.add("object")
            if readers & {"eval", "exec"}:
                kinds.add("scope")
            if "breakpoint" in readers:
                kinds.add("frame")
            if (isinstance(func, ast.Attribute) and spelled in _FRAME_CALLS) or (
                    self.may_from(func, _FRAME_MODULES) & _FRAME_CALLS):
                kinds.add("frame")
            if spelled in _OBJECT_READERS or self.may_from(func, {"inspect"}) & _OBJECT_READERS:
                kinds.add("object")
            if spelled in ("__import__", "import_module") or "__import__" in readers or (
                    "import_module" in self.may_from(func, {"importlib"})):
                kinds.add("modules")
            setters = readers & {"setattr", "delattr"} or ({spelled} & {"setattr", "delattr"})
            computed = not (len(node.args) >= 2 and _literal_name(node.args[1]))
            if setters and computed:
                kinds.add("rebind")  # a name computed at run time (setattr by a literal one binds an attribute)
            if ((readers | {spelled}) & {"getattr", "setattr", "delattr"} and len(node.args) >= 2 and computed
                    and self.may(node.args[0]) & self.modules):
                kinds.add("modules")  # a computed name on a module object (this module itself, perhaps)
        elif isinstance(node, ast.Subscript):
            if "builtins" in self.may(node.value):
                kinds.add("frame")  # __builtins__["eval"]
        elif isinstance(node, ast.Attribute):
            if node.attr in _FRAME_ATTRIBUTES:
                kinds.add("frame")
            if node.attr == "__dict__":
                kinds.add("frame" if "builtins" in self.may(node.value) else "object")  # every reader, by name
            if node.attr in ("__setattr__", "__delattr__"):
                kinds.add("rebind")
            if node.attr == "__globals__":
                kinds.add("globals")
            if node.attr == "modules" and ("sys" in self.may(node.value) or (
                    isinstance(node.value, ast.Name) and node.value.id == "sys")):
                kinds.add("modules")
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            parent = self.parents.get(id(node))
            if not (isinstance(parent, ast.Call) and parent.func is node) and (
                    self.may_builtins(node) & _NAMESPACE_BUILTINS
                    or self.may_from(node, _FRAME_MODULES) & _FRAME_CALLS):
                kinds.add("frame")  # a namespace or frame reader passed on, to be called anywhere
            if "sys.modules" in self.may(node):
                kinds.add("modules")
        return kinds


class _Index:
    """Assignments across every scanned module, by name: module and class
    constants, ``self.x = ...`` attributes and imported names resolve through
    it; a name assigned in several places takes every value."""

    def __init__(self, trees: Iterable[ast.Module], local: Iterable[str] = (),
                 identities: dict[int, _Identities] | None = None) -> None:
        self.globals: dict[str, list[ast.AST]] = {}
        self.attribute_aliases: dict[str, set[str]] = {}
        #: The top-level names of the scanned modules (an import from one
        #: resolves through the index; from anywhere else it is not known).
        self.local: set[str] = set(local)
        #: Each module's identity resolver, by id of its tree.
        self.identities: dict[int, _Identities] = dict(identities or {})
        #: Every (module, attribute) some scanned module sets on that module.
        self.foreign: frozenset[tuple[str, str]] = frozenset()
        #: The modules (by id of the tree) that bind an attribute of each
        #: name (obj.x = ..., setattr(obj, "x", ...), del obj.x), and those
        #: whose class bodies bind it (a method, perhaps a subclass's override).
        self.attribute_stores: dict[str, set[int]] = {}
        self.class_members: dict[str, set[int]] = {}
        #: Module names bound somewhere by a form the index does not record
        #: as a value (unpacked, a loop, with, except, import, def, class,
        #: match, a statement inside if or try, a function's global): never
        #: provably builtin.
        self.opaque: set[str] = set()
        #: The module and class names of modules that read or write their
        #: namespace (globals(), vars(), __dict__, sys.modules, setattr with a
        #: computed name, eval, exec, frames): never provably builtin.
        self.exposed: set[str] = set()
        #: Every use of a name or attribute, and those that only reach an
        #: attribute of it (Cls.x), by name, across the scanned modules.
        self.uses: dict[str, int] = {}
        self.attribute_uses: dict[str, int] = {}
        #: The module each recorded value is written in (by id of the value
        #: and of the tree), and whether it may name the table (_carries).
        self.owner: dict[int, int] = {}
        self.modules: dict[int, _Module] = {}
        self.carries: dict[int, bool] = {}
        for tree in trees:
            self._tree = id(tree)
            ids = self.identities.setdefault(id(tree), _Identities(tree, self.local, self.foreign))
            parents = ids.parents
            for attr in ids.attributes:
                self.attribute_stores.setdefault(attr, set()).add(id(tree))
            for cls in ids.nodes:
                if isinstance(cls, ast.ClassDef):
                    for item in cls.body:
                        for member in _class_bindings(item):
                            self.class_members.setdefault(member, set()).add(id(tree))
            for node in ids.nodes:
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                    self.globals.setdefault(node.target.attr, []).append(ast.Name(id="<parameter>"))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute):
                            self._record(target.attr, node.value)
                elif (isinstance(node, ast.Call) and len(node.args) == 3
                      and "setattr" in ids.may_builtins(node.func) | {_spelled(node.func)}
                      and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                    # setattr(obj, "name", value), by any name setattr may have
                    # (its own spelling too), binds obj.name like an assignment.
                    self._record(node.args[1].value, node.args[2])
                elif isinstance(node, ast.alias) and node.asname and node.asname != node.name.rsplit(".", 1)[-1]:
                    # import x as y, from m import x as y: y is another name for x.
                    self.globals.setdefault(node.asname, []).append(ast.Name(id=node.name.rsplit(".", 1)[-1]))
                elif isinstance(node, ast.Global):
                    self.opaque.update(node.names)
                if isinstance(node, (ast.Name, ast.Attribute)) and not isinstance(node.ctx, ast.Store):
                    name = node.id if isinstance(node, ast.Name) else node.attr
                    self.uses[name] = self.uses.get(name, 0) + 1
                    parent = parents.get(id(node))
                    if isinstance(parent, ast.Attribute) and parent.value is node and not (
                            parent.attr.startswith("__") and parent.attr.endswith("__")):
                        self.attribute_uses[name] = self.attribute_uses.get(name, 0) + 1
            exposes = bool(ids.namespace_reads) or ids.rebinder
            names: set[str] = set()
            body = {id(node) for node in tree.body}
            for node in tree.body:
                self._module_assign(node)
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        self._module_assign(item)
                        names.update(self._recorded(item))
            for node in _scope_nodes(tree.body):
                recorded = self._recorded(node) if id(node) in body else set()
                found: dict[str, list[ast.AST]] = {}
                _collect(node, found, found, self.local)
                names.update(found)
                self.opaque.update(set(found) - recorded)
            if exposes:
                self.exposed |= names

    @staticmethod
    def _recorded(node: ast.AST) -> set[str]:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            return {node.target.id}
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            return {target.id for target in targets if isinstance(target, ast.Name)}
        return set()

    def _module_assign(self, node: ast.AST) -> None:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            self.globals.setdefault(node.target.id, []).append(ast.Name(id="<parameter>"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self._record(target.id, node.value)

    def _record(self, name: str, value: ast.AST) -> None:
        self.globals.setdefault(name, []).append(value)
        self.owner[id(value)] = self._tree


class _Module:
    def __init__(self, path: Path, tree: ast.Module, index: _Index) -> None:
        self.path = path
        self.tree = tree
        self.index = index
        #: The module's identity resolver (a builtin's name, a decorator, a
        #: helper, a namespace reader).
        self.ids = index.identities.get(id(tree)) or _Identities(tree, index.local, index.foreign)
        self.parents: dict[int, ast.AST] = self.ids.parents
        self.names: dict[int, str] = {}
        self.functions: dict[int, list[ast.AST]] = {}
        #: The outermost function around a node (a nested def, lambda,
        #: comprehension or class body belongs to it), by qualified name.
        self.outers: dict[int, str] = {}
        self.outer_of: dict[str, str] = {}
        self.outer_nodes: dict[str, ast.AST] = {}
        self._qualify(tree, "", [], "")
        # A function's defaults, decorators and annotations run in the scope
        # around it, not in its own (outer functions first: ast.walk is
        # breadth first).
        for function in self.ids.nodes:
            if isinstance(function, _FUNCTIONS):
                arguments = function.args
                outside: list[ast.AST | None] = [*arguments.defaults, *arguments.kw_defaults]
                if not isinstance(function, ast.Lambda):
                    outside += [*function.decorator_list, function.returns, *(
                        arg.annotation for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                                                   *([arguments.vararg] if arguments.vararg else []),
                                                   *([arguments.kwarg] if arguments.kwarg else [])])]
                for expression in outside:
                    for sub in ast.walk(expression) if expression is not None else []:
                        self.functions[id(sub)] = [f for f in self.functions.get(id(sub), []) if f is not function]
        self._memo: dict[int, list[str] | None] = {}
        #: Renderings by node (those not cut short by a cycle), the nodes
        #: being rendered, and whether a rendering was cut short by one.
        self._texts: dict[int, list[str]] = {}
        self._sequences: dict[int, bool] = {}
        self._sequencing: set[int] = set()
        self._sequence_cut = False
        self._sequence_pending: list[int] = []
        self._rendering: set[int] = set()
        self._cut = False
        self.callees: dict[int, set[str]] = {}
        #: The module's helpers that run a statement they are given, and
        #: those shown to only run it (clean_sinks).
        self.sinks: dict[str, list[_Sink]] = {}
        self.clean: set[tuple[str, _Sink]] = set()
        #: Every read of a name, and the target of each x += ..., which reads
        #: x too, by name.
        self.loads: dict[str, list[ast.AST]] = {}
        #: Every read of an attribute (obj.x in obj.x += ... too), by name.
        self.attribute_loads: dict[str, list[ast.AST]] = {}
        self.builtin_values: dict[int, bool] = {}
        self.carries: dict[int, bool] = {}
        self._scopes: dict[int, _Scope] = {}
        self._resolved: dict[tuple[int, str], list[tuple[int, list[ast.AST]]]] = {}
        self._classes: dict[int, dict[str, list[ast.AST]]] = {}
        #: The ClassDef and comprehension nodes, by id (their names are scopes
        #: of their own: _binding_scope).
        self.scope_nodes: dict[int, ast.AST] = {}
        self.star = self.ids.star
        #: Every node reading a namespace (_Identities._namespace_kinds), and its kind.
        self.namespace_reads: list[tuple[ast.AST, str]] = list(self.ids.namespace_reads)
        for node in self.ids.nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.loads.setdefault(node.id, []).append(node)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                self.loads.setdefault(node.target.id, []).append(node.target)
            if isinstance(node, ast.Attribute) and (isinstance(node.ctx, ast.Load) or (
                    isinstance(self.parents.get(id(node)), ast.AugAssign))):
                self.attribute_loads.setdefault(node.attr, []).append(node)
            elif isinstance(node, (ast.ClassDef, *_COMPREHENSIONS)):
                self.scope_nodes[id(node)] = node
        #: Any frame read, and the functions whose own names a read sees
        #: (locals(), vars(), eval, exec in them or in a function nested in
        #: them): their names are never provably builtin.
        self.frames = any(kind == "frame" for _node, kind in self.namespace_reads)
        self.exposed_scopes = {id(function) for node, kind in self.namespace_reads if kind in ("locals", "scope")
                               for function in self.functions.get(id(node), [])}

    def _qualify(self, node: ast.AST, prefix: str, stack: list[ast.AST], outer: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                function = not isinstance(child, ast.ClassDef)
                inner_stack = stack + [child] if function else stack
                inner_outer = outer or (inner if function else "")
                self.outer_of.setdefault(inner, inner_outer or inner)
                if inner_outer == inner:
                    self.outer_nodes.setdefault(inner, child)
                for sub in ast.walk(child):
                    self.names[id(sub)] = inner
                    self.functions[id(sub)] = inner_stack
                    self.outers[id(sub)] = inner_outer
                self._qualify(child, inner, inner_stack, inner_outer)
            elif isinstance(child, ast.Lambda):
                # A lambda's parameters are its own; its body is its scope's.
                inner_stack = stack + [child]
                for sub in ast.walk(child):
                    self.functions[id(sub)] = inner_stack
                self._qualify(child, prefix, inner_stack, outer)
            elif not prefix and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                label = next((t.id for t in targets if isinstance(t, ast.Name)), "")
                name = f"<module>.{label}" if label else "<module>"
                self.outer_of.setdefault(name, name)
                for sub in ast.walk(child):
                    self.names[id(sub)] = name
                    self.functions.setdefault(id(sub), [])
                self._qualify(child, prefix, stack, outer)
            else:
                self._qualify(child, prefix, stack, outer)

    @property
    def relative(self) -> str:
        return self.path.relative_to(ROOT).as_posix() if self.path.is_relative_to(ROOT) else self.path.name

    def where(self, node: ast.AST) -> str:
        return self.names.get(id(node), "<module>")

    def outer(self, node: ast.AST) -> str:
        """The scope the backstop and the fail-closed rules judge a node by:
        its outermost enclosing function, or its own module statement or
        class body when it is in none."""
        return self.outers.get(id(node)) or self.where(node)

    def _scope(self, function: ast.AST) -> _Scope:
        """A function's (or lambda's) own names: what each may be bound to
        (a parameter, and a nested function's rebinding of it through
        nonlocal, included), and those it declares global or nonlocal. A
        nested function's, class body's or comprehension's names are their
        own."""
        if id(function) not in self._scopes:
            self._scopes[id(function)] = _Scope({}, {}, {})  # a cycle reads as no names
            found: dict[str, list[ast.AST]] = {}
            appended: dict[str, list[ast.AST]] = {}
            declared: dict[str, str] = {}
            outward: dict[str, list[ast.AST]] = {}
            nested: list[ast.AST] = []
            body = function.body if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) else [
                cast(ast.Lambda, function).body]
            for node in _scope_nodes(body):
                _collect(node, found, appended, self.index.local)
                if isinstance(node, (ast.Global, ast.Nonlocal)):
                    for name in node.names:
                        declared[name] = "global" if isinstance(node, ast.Global) else "nonlocal"
                if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                    nested.append(node)
            if isinstance(function, _FUNCTIONS):
                arguments = function.args
                for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                            *([arguments.vararg] if arguments.vararg else []),
                            *([arguments.kwarg] if arguments.kwarg else [])]:
                    found.setdefault(arg.arg, []).append(ast.Name(id="<parameter>"))
            for appended_name, pieces in appended.items():
                tail = cast(ast.expr, pieces[0])
                for piece in pieces[1:]:
                    tail = ast.BinOp(left=tail, op=ast.Add(), right=cast(ast.expr, piece))
                tails = [tail, *(cast(ast.expr, piece) for piece in pieces)] if len(pieces) > 1 else [tail]
                bases = found.get(appended_name) or [ast.Name(id="<parameter>")]
                found[appended_name] = [
                    _augmented(ast.BinOp(left=cast(ast.expr, base), op=ast.Add(), right=one))
                    for base in bases for one in tails
                ]
            for name, kind in declared.items():
                values = found.pop(name, [])
                if kind == "nonlocal" and values:
                    outward.setdefault(name, []).extend(values)  # rebinds an enclosing function's name
            for inner in nested:
                rebound = self._class_outward(inner) if isinstance(inner, ast.ClassDef) else self._scope(inner).outward
                for name, values in rebound.items():
                    if name in found:
                        found[name].extend(values)
                    elif declared.get(name) != "global":
                        outward.setdefault(name, []).extend(values)
            self._scopes[id(function)] = _Scope(found, declared, outward)
        return self._scopes[id(function)]

    def _class_outward(self, cls: ast.ClassDef) -> dict[str, list[ast.AST]]:
        """What a class body, and every function and class in it, rebinds in
        an enclosing function through nonlocal."""
        outward: dict[str, list[ast.AST]] = {}
        found: dict[str, list[ast.AST]] = {}
        declared: set[str] = set()
        for node in _scope_nodes(cls.body):
            _collect(node, found, found, self.index.local)
            if isinstance(node, ast.Nonlocal):
                declared.update(node.names)
            inner = (self._class_outward(node) if isinstance(node, ast.ClassDef)
                     else self._scope(node).outward if isinstance(node, _FUNCTIONS) else {})
            for name, values in inner.items():
                outward.setdefault(name, []).extend(values)
        for name in declared:
            outward.setdefault(name, []).extend(found.get(name, [ast.Name(id="<parameter>")]))
        return outward

    def _class_scope(self, cls: ast.ClassDef) -> dict[str, list[ast.AST]]:
        """What each name a class body binds may be bound to."""
        if id(cls) not in self._classes:
            found: dict[str, list[ast.AST]] = {}
            for node in _scope_nodes(cls.body):
                _collect(node, found, found, self.index.local)
            self._classes[id(cls)] = found
        return self._classes[id(cls)]

    def resolutions(self, node: ast.AST, name: str) -> list[tuple[int, list[ast.AST]]]:
        """Every scope a name may resolve to where it is read, innermost
        first, with what it may be bound to there: a comprehension's target,
        a class body's name (then what is around it), a function's local
        (global and nonlocal followed), else the module's and the class
        constants of the index (scope 0)."""
        memo = (id(node), name)
        if memo in self._resolved:
            return self._resolved[memo]
        out: list[tuple[int, list[ast.AST]]] = []
        self._resolved[memo] = self._resolve(node, name, out)
        return out

    def _resolve(self, node: ast.AST, name: str, out: list[tuple[int, list[ast.AST]]]) -> list[tuple[int, list[ast.AST]]]:
        path: list[ast.AST] = [node]
        current = self.parents.get(id(node))
        while current is not None:
            child = path[-1]
            if isinstance(current, _COMPREHENSIONS):
                first = current.generators[0]
                outside = child is first and len(path) > 1 and path[-2] is first.iter
                bound = [cast(ast.AST, ast.Starred(value=generator.iter)) for generator in current.generators
                         for target in _stored_names(generator.target) if target == name]
                if bound and not outside:
                    out.append((id(current), bound))
                    return out
            elif isinstance(current, ast.ClassDef) and any(child is item for item in current.body):
                members = self._class_scope(current).get(name)
                if members:
                    out.append((id(current), members))
            elif isinstance(current, _FUNCTIONS) and any(
                    child is item for item in (current.body if isinstance(current.body, list) else [current.body])):
                scope = self._scope(current)
                kind = scope.declared.get(name)
                if kind == "global":
                    break
                if kind != "nonlocal" and name in scope.found:
                    out.append((id(current), scope.found[name]))
                    return out
            path.append(current)
            current = self.parents.get(id(current))
        out.append((0, self.index.globals.get(name) or []))
        return out

    def bindings(self, node: ast.AST, name: str) -> list[ast.AST] | None:
        """Every value a name may be bound to where it is read (None when
        none is known)."""
        values: dict[int, ast.AST] = {}
        for _scope, bound in self.resolutions(node, name):
            for value in bound:
                values.setdefault(id(value), value)  # a top-level class's names are in the index too
        return list(values.values()) or None

    # -- values --------------------------------------------------------

    def values(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> list[str] | None:
        """The strings an expression may be, flattened through tuples, lists,
        sets, dicts (their keys) and starred items, or None when one is not
        known."""
        if id(node) in self._memo:
            return self._memo[id(node)]
        if id(node) in seen:
            return None
        result = self._values(node, seen | {id(node)})
        if not seen:
            self._memo[id(node)] = result
        return result

    def _values(self, node: ast.AST, seen: frozenset[int]) -> list[str] | None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bytes):
                return [node.value.decode("utf-8", "replace")]
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            elements = node.keys if isinstance(node, ast.Dict) else node.elts
            out: list[str] = []
            for element in elements:
                inner = self.values(element, seen) if element is not None else None
                if inner is None:
                    return None
                out.extend(inner)
            return out
        if isinstance(node, ast.Starred):
            return self.values(node.value, seen)
        if isinstance(node, ast.IfExp):
            body, orelse = self.values(node.body, seen), self.values(node.orelse, seen)
            return None if body is None or orelse is None else body + orelse
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and (
                self._is_sequence(node.left, seen) or self._is_sequence(node.right, seen)):
            left, right = self.values(node.left, seen), self.values(node.right, seen)
            return None if left is None or right is None else left + right
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Call) and _callee(node) in _STRING_METHODS
                and isinstance(node.func, ast.Attribute)):
            texts = self.texts(node, seen)
            return None if any(UNKNOWN in text for text in texts) else texts
        if isinstance(node, ast.Name):
            return self._bound(self.bindings(node, node.id), seen)
        if isinstance(node, ast.Attribute):
            return self._bound(self.index.globals.get(node.attr), seen)
        return None

    def _bound(self, bound: list[ast.AST] | None, seen: frozenset[int]) -> list[str] | None:
        if not bound:
            return None
        out: list[str] = []
        for value in bound:
            if isinstance(value, ast.Name) and value.id == "<parameter>":
                return None
            inner = self.values(value, seen)
            if inner is None:
                return None
            out.extend(inner)
        return out

    def _is_sequence(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> bool:
        """Whether a value may be a tuple, list, set or dict: a display, a +
        of one, or a name any binding of which may be. Memoized by node. A
        node met again while it is being decided adds nothing, so a False
        decided under such a cut is kept only once the outermost call is
        False too (it is reachability: a node reached from a False one is
        False); a True is always kept."""
        key = id(node)
        if key in self._sequences:
            return self._sequences[key]
        if key in self._sequencing:
            self._sequence_cut = True
            return False
        outermost = not self._sequencing
        outer, self._sequence_cut = self._sequence_cut, False
        self._sequencing.add(key)
        try:
            if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                result = True
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                result = self._is_sequence(node.left) or self._is_sequence(node.right)
            elif isinstance(node, (ast.Name, ast.Attribute)):
                bound = (self.bindings(node, node.id) if isinstance(node, ast.Name)
                         else self.index.globals.get(node.attr)) or []
                result = any(self._is_sequence(value) for value in bound)
            else:
                result = False
        finally:
            self._sequencing.discard(key)
        if result or not self._sequence_cut:
            self._sequences[key] = result
        else:
            self._sequence_pending.append(key)
        if outermost:
            if not result:
                self._sequences.update(dict.fromkeys(self._sequence_pending, False))
            self._sequence_pending.clear()
        self._sequence_cut = self._sequence_cut or outer
        return result

    def texts(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> list[str]:
        """Renderings of a string expression in the plain subset, exactly;
        everything else is a part not known (UNKNOWN), in place, the known
        text around it kept (_render). At most MAX_RENDERINGS: past that,
        the parts that vary are not known instead. (seen is kept for the
        callers that pass it; cycles are cut here, a node met again while it
        is being rendered not known.)"""
        key = id(node)
        if key in self._texts:
            return self._texts[key]
        if key in self._rendering:
            self._cut = True
            return [UNKNOWN]
        outer, self._cut = self._cut, False
        self._rendering.add(key)
        try:
            result = _bounded(self._render(node, frozenset()))
        finally:
            self._rendering.discard(key)
        if not self._cut:
            self._texts[key] = result
        self._cut = self._cut or outer
        return result

    def _render(self, node: ast.AST, seen: frozenset[int]) -> list[str]:
        """The plain subset, rendered exactly:

        - a str literal; a name or attribute every binding of which renders
          (a parameter does not), with its alternatives;
        - a conditional expression of such (either branch);
        - a concatenation (+) of such;
        - an f-string, and a str.format or % template, whose fields are
          exactly {} / {name} / {0} or %s / %(name)s, with no spec and no
          conversion, each value a str literal or a name bound only to
          renderable str values (_field_values);
        - join, replace, strip, lstrip, rstrip, removeprefix, removesuffix,
          upper and lower, with str-literal arguments (join's a list or
          tuple display of them, or a name bound only, once, to a tuple
          display of them, its starred elements such tuples).

        Anything else is not known: any spec or conversion, any other % code,
        a numeric or bytes value, a spread, a computed or duplicate key, a
        starred argument, any other str method or callee, an argument that
        does not match its field, a bound exceeded. A str method that is not
        rendered keeps its receiver's text, a part not known after it."""
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else [UNKNOWN]
        if isinstance(node, ast.JoinedStr):
            pieces: list[list[str]] = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    pieces.append([part.value])
                elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
                    pieces.append(self._field_values(part.value, seen) or [UNKNOWN])
                else:
                    pieces.append([UNKNOWN])
            return _combine(pieces)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if self._is_sequence(node.left, frozenset()) or self._is_sequence(node.right, frozenset()):
                return [UNKNOWN]
            return _combine([self.texts(node.left, seen), self.texts(node.right, seen)])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self._percent_texts(node, seen)
        if isinstance(node, ast.IfExp):
            return [*self.texts(node.body), *self.texts(node.orelse)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format":
                return self._format_texts(node, seen)
            if node.func.attr in _RENDERED_METHODS:
                return self._method_texts(node, seen)
            if node.func.attr in _STRING_METHODS:
                return _bounded([text + UNKNOWN for text in self.texts(node.func.value, seen)])
        if isinstance(node, (ast.Name, ast.Attribute)):
            bound = (self.bindings(node, node.id) if isinstance(node, ast.Name)
                     else self.index.globals.get(node.attr)) or []
            if not bound:
                return [UNKNOWN]
            through: list[str] = []
            for value in bound:
                through.extend([UNKNOWN] if _is_unknown(value) else self.texts(value))
            return _bounded(through)
        return [UNKNOWN]

    def _field_values(self, node: ast.AST, seen: frozenset[int]) -> list[str] | None:
        """A field's values: a str literal, or a name bound only to
        renderable str values, with no part not known; None otherwise (a
        number, bytes, anything else; a container renders as not known)."""
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else None
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return None
        rendered = self.texts(node, seen)
        return None if any(UNKNOWN in text for text in rendered) else rendered

    def _sequence_strings(self, node: ast.AST, depth: int) -> list[list[str]] | None:
        """The elements join is given, each with its values: a list or tuple
        display of str values (a starred element a tuple of them), or a name
        bound only, once, to a tuple display of them (a tuple cannot change);
        None otherwise."""
        if depth > 4:
            return None
        if isinstance(node, (ast.Name, ast.Attribute)):
            bound = (self.bindings(node, node.id) if isinstance(node, ast.Name)
                     else self.index.globals.get(node.attr)) or []
            if len(bound) != 1 or not isinstance(bound[0], ast.Tuple):
                return None
            return self._sequence_strings(bound[0], depth + 1)
        if not isinstance(node, (ast.List, ast.Tuple)):
            return None
        out: list[list[str]] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                inner = self._sequence_strings(element.value, depth + 1)
                if inner is None or isinstance(element.value, ast.List):
                    return None
                out.extend(inner)
            else:
                values = self._field_values(element, frozenset())
                if values is None:
                    return None
                out.append(values)
        return out

    def _percent_texts(self, node: ast.BinOp, seen: frozenset[int]) -> list[str]:
        """template % values, in the subset: %% is %; %s takes the next value
        of a tuple literal (or the one value given, when it is the only
        field), %(name)s the value of that str key in a dict literal with
        only str-literal keys, each once. Any other % sequence makes the
        text from it on not known; values that do not match make every field
        not known."""
        right = node.right
        positional: list[ast.AST] | None = None
        named: dict[str, ast.AST] | None = None
        if isinstance(right, ast.Tuple) and not any(isinstance(e, ast.Starred) for e in right.elts):
            positional = list(right.elts)
        elif isinstance(right, ast.Dict):
            keys = [k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None for k in right.keys]
            if None not in keys and len(set(keys)) == len(keys):
                named = dict(zip(cast(list[str], keys), right.values, strict=True))
        out: list[str] = []
        for template in self.texts(node.left, seen):
            tokens = _percent_fields(template)
            fields = [token for token in tokens if isinstance(token, tuple)]
            values = _assign(fields, positional, named, right if positional is None and named is None else None,
                             UNKNOWN in template)
            out.extend(self._fill(tokens, values, seen))
        return out

    def _format_texts(self, node: ast.Call, seen: frozenset[int]) -> list[str]:
        """template.format(...), in the subset: {{ and }} are braces; {} takes
        the next positional value, {0} that one, {name} that keyword's.
        Any other field (a spec, a conversion, an attribute or index, a
        nested field) is not known, and then so is every field; a starred
        argument or a ** spread, numbering both automatic and manual, or a
        value that is missing makes the fields not known."""
        plain = not any(isinstance(a, ast.Starred) for a in node.args) and all(k.arg for k in node.keywords)
        positional: list[ast.AST] | None = list(node.args) if plain else None
        named: dict[str, ast.AST] | None = {cast(str, k.arg): k.value for k in node.keywords} if plain else None
        out: list[str] = []
        for template in self.texts(cast(ast.Attribute, node.func).value, seen):
            tokens = _format_fields(template)
            fields = [token for token in tokens if isinstance(token, tuple)]
            out.extend(self._fill(tokens, _assign(fields, positional, named, None, UNKNOWN in template), seen))
        return out

    def _fill(self, tokens: list[str | tuple[str, str]], values: list[ast.AST | None],
              seen: frozenset[int]) -> list[str]:
        pieces: list[list[str]] = []
        remaining = iter(values)
        for token in tokens:
            if isinstance(token, str):
                pieces.append([token])
            else:
                value = next(remaining)
                pieces.append((self._field_values(value, seen) if value is not None else None) or [UNKNOWN])
        return _combine(pieces)

    def _method_texts(self, node: ast.Call, seen: frozenset[int]) -> list[str]:
        """join, replace, strip, lstrip, rstrip, removeprefix, removesuffix,
        upper and lower with str-literal arguments, computed exactly (a
        receiver's part not known kept in place); with any other argument,
        or past the bound, the receiver's text, a part not known after it."""
        method = cast(ast.Attribute, node.func).attr
        receivers = self.texts(cast(ast.Attribute, node.func).value, seen)
        arguments: list[list[object]] | None = None
        if not node.keywords and not any(isinstance(a, ast.Starred) for a in node.args):
            arguments = []
            for argument in node.args:
                elements = self._sequence_strings(argument, 0) if method == "join" else None
                if elements is not None:
                    arguments.append([list(c) for c in itertools.islice(
                        itertools.product(*elements), MAX_RENDERINGS + 1)])
                elif method != "join":
                    values = self._field_values(argument, seen)
                    if values is None:
                        arguments = None
                        break
                    arguments.append(list(values))
                else:
                    arguments = None
                    break
        count = len(receivers)
        for alternatives in arguments or []:
            count *= len(alternatives)
        if arguments is None or count > MAX_RENDERINGS:
            return [UNKNOWN] if method == "join" else [text + UNKNOWN for text in receivers]
        rendered: list[str] = []
        for text in receivers:
            for choice in itertools.product(*arguments):
                try:
                    rendered.append(getattr(text, method)(*choice))
                except (TypeError, ValueError):
                    rendered.append(text + UNKNOWN)
        return list(dict.fromkeys(rendered))


#: The str methods rendered exactly with str-literal arguments.
_RENDERED_METHODS = frozenset({"join", "replace", "strip", "lstrip", "rstrip", "removeprefix", "removesuffix",
                               "upper", "lower"})
#: Other str methods: a string expression, its receiver's text kept, then a
#: part not known.
_STRING_METHODS = _RENDERED_METHODS | frozenset({
    "format", "casefold", "swapcase", "title", "capitalize", "zfill", "center", "ljust", "rjust", "expandtabs",
    "format_map", "translate", "partition", "rpartition", "split", "rsplit"})


def _augmented(node: ast.BinOp) -> ast.BinOp:
    """A name's value after ``+=``: its earlier value with the pieces added."""
    node.augmented = True  # type: ignore[attr-defined]
    return node


def _percent_fields(template: str) -> list[str | tuple[str, str]]:
    """A %-template's literal text and subset fields (("", "") for %s,
    ("name", "") for %(name)s); at any other % sequence, the rest of the
    template is one field not known (("?", "")), and %% is %."""
    tokens: list[str | tuple[str, str]] = []
    literal: list[str] = []
    i = 0
    while i < len(template):
        if template[i] != "%":
            literal.append(template[i])
            i += 1
            continue
        named = re.match(r"\((\w+)\)s", template[i + 1:])
        field: tuple[str, str] | None = None
        if template.startswith("%%", i):
            literal.append("%")
            i += 2
            continue
        if template.startswith("%s", i):
            field, i = ("", ""), i + 2
        elif named:
            field, i = (named.group(1), ""), i + 1 + named.end()
        if literal:
            tokens.append("".join(literal))
            literal = []
        if field is None:
            tokens.append(("?", ""))  # not the subset: from here on, not known
            return tokens
        tokens.append(field)
    if literal:
        tokens.append("".join(literal))
    return tokens


def _format_fields(template: str) -> list[str | tuple[str, str]]:
    """A .format template's literal text and subset fields (("", "auto"),
    ("0", "index"), ("name", "name")); any other field, or a brace outside
    one, is one field not known (("?", "")), its extent to the matching
    brace (or the rest of the template)."""
    tokens: list[str | tuple[str, str]] = []
    literal: list[str] = []
    i = 0
    while i < len(template):
        char = template[i]
        if template.startswith("{{", i) or template.startswith("}}", i):
            literal.append(char)
            i += 2
            continue
        if char not in "{}":
            literal.append(char)
            i += 1
            continue
        if literal:
            tokens.append("".join(literal))
            literal = []
        if char == "}":
            tokens.append(("?", ""))
            i += 1
            continue
        depth, j = 1, i + 1
        while j < len(template) and depth:
            depth += {"{": 1, "}": -1}.get(template[j], 0)
            j += 1
        content = template[i + 1:j - 1] if not depth else None
        if content == "":
            tokens.append(("", "auto"))
        elif content is not None and content.isdigit():
            tokens.append((content, "index"))
        elif content is not None and content.isidentifier():
            tokens.append((content, "name"))
        else:
            tokens.append(("?", ""))
        i = j
    if literal:
        tokens.append("".join(literal))
    return tokens


def _assign(fields: list[tuple[str, str]], positional: list[ast.AST] | None, named: dict[str, ast.AST] | None,
            single: ast.AST | None, unknown_template: bool) -> list[ast.AST | None]:
    """The value each subset field takes, None where it is not known: every
    field when any is not the subset, when automatic and manual numbering
    are mixed, or when the values do not match the fields exactly;
    automatic and %s fields when the template holds a part not known (it may
    hold fields of its own)."""
    kinds = {kind for _key, kind in fields}
    if ("?", "") in fields or {"auto", "index"} <= kinds:
        return [None] * len(fields)
    if fields and all(kind == "" for _key, kind in fields):  # % fields
        if any(key for key, _kind in fields):
            if all(key for key, _kind in fields) and named is not None and all(key in named for key, _ in fields):
                return [named[key] for key, _kind in fields]
            return [None] * len(fields)
        if unknown_template:
            return [None] * len(fields)
        if positional is not None and len(positional) == len(fields):
            return list(positional)
        if single is not None and len(fields) == 1:
            return [single]
        return [None] * len(fields)
    out: list[ast.AST | None] = []
    automatic = 0
    for key, kind in fields:
        if kind == "auto":
            index, automatic = automatic, automatic + 1
            out.append(None if unknown_template or positional is None or index >= len(positional)
                       else positional[index])
        elif kind == "index":
            out.append(positional[int(key)] if positional is not None and int(key) < len(positional) else None)
        else:
            out.append(named.get(key) if named is not None else None)
    return out


def _bounded(renderings: list[str]) -> list[str]:
    """At most MAX_RENDERINGS renderings; past the bound, one rendering with
    the text that varies not known (_common), so the known text around it
    stays."""
    distinct = list(dict.fromkeys(renderings)) or [UNKNOWN]
    return distinct if len(distinct) <= MAX_RENDERINGS else [_common(distinct)]


def _common(renderings: list[str]) -> str:
    """What every rendering holds: their common start and end around one
    part not known (or the one rendering, when they agree)."""
    distinct = list(dict.fromkeys(renderings))
    if len(distinct) == 1:
        return distinct[0]
    start = distinct[0]
    for text in distinct[1:]:
        while not text.startswith(start):
            start = start[:-1]
    rests = [text[len(start):] for text in distinct]
    end = rests[0]
    for rest in rests[1:]:
        while not rest.endswith(end):
            end = end[1:]
    return _collapse(start + UNKNOWN + end)


def _collapse(text: str) -> str:
    return re.sub(UNKNOWN + "+", UNKNOWN, text)


def _combine(pieces: list[list[str]]) -> list[str]:
    """Every rendering of a sequence of pieces; past the bound, one with each
    piece that varies not known (the known pieces around them kept)."""
    pieces = [piece or [UNKNOWN] for piece in pieces]
    count = 1
    for piece in pieces:
        count *= len(piece)
    if count > MAX_RENDERINGS:
        return [_collapse("".join(_common(piece) for piece in pieces))]
    return ["".join(choice) for choice in itertools.product(*pieces)] or [""]


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _top_level_strings(m: _Module) -> Iterator[ast.AST]:
    """Each string expression not inside a larger one, docstrings aside."""
    for node in m.ids.nodes:
        if not (isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and _callee(node) in _STRING_METHODS)):
            continue
        parent = m.parents.get(id(node))
        grandparent = m.parents.get(id(parent)) if parent is not None else None
        if isinstance(parent, (ast.JoinedStr, ast.FormattedValue)) or (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))) or (
                isinstance(parent, ast.Attribute) and isinstance(grandparent, ast.Call)
                and _callee(grandparent) in _STRING_METHODS):
            continue
        if isinstance(node, ast.BinOp) and not isinstance(node.op, (ast.Add, ast.Mod)):
            continue
        if isinstance(node, ast.Constant) and _is_docstring(node, m.parents):
            continue
        yield node


class _Finding:
    __slots__ = ("limit", "node", "style")

    def __init__(self, style: str, node: ast.AST, limit: bool) -> None:
        self.style, self.node, self.limit = style, node, limit


def _callee_names(m: _Module, func: ast.AST, seen: frozenset[int] = frozenset()) -> set[str]:
    """Every name a callee may be: its own, and through a local name bound
    to another callee (run = conn.execute, ga = getattr), that one's, or
    through an attribute bound anywhere (self.run = conn.execute,
    setattr(obj, "run", conn.execute)), the name it is bound to. A getattr
    with a literal name and a default (getattr(obj, "name", d)) may be
    either."""
    if not seen and id(func) in m.callees:
        return m.callees[id(func)]
    if id(func) in seen or len(seen) > 6:
        return set()
    inner = seen | {id(func)}
    names: set[str] = set()
    if isinstance(func, ast.Call):
        default = _getattr_default(m, func)
        if default is not None:
            names = {str(cast(ast.Constant, func.args[1]).value)} | _callee_names(m, default, inner)
    elif isinstance(func, ast.Attribute):
        names = {func.attr} | _attribute_aliases(m, func.attr)
    elif isinstance(func, ast.Name):
        names = {func.id}
        for value in m.bindings(func, func.id) or []:
            if isinstance(value, (ast.Name, ast.Attribute, ast.Call)):
                names |= _callee_names(m, value, inner)
    if not seen:
        m.callees[id(func)] = names
    return names


def _getattr_default(m: _Module, call: ast.Call) -> ast.expr | None:
    """The default of getattr(obj, "name", default), getattr being the
    builtin by every binding (_Identities.of), else None."""
    if (m.ids.of(call.func) == "builtins.getattr" and len(call.args) == 3 and _literal_name(call.args[1])
            and not call.keywords):
        return call.args[2]
    return None


def _attribute_aliases(m: _Module, attr: str) -> set[str]:
    """The names an attribute name is bound to anywhere (self.run =
    conn.execute, setattr(obj, "run", conn.execute), self.run = run_sql),
    one step, memoized by name."""
    cache = m.index.attribute_aliases
    if attr not in cache:
        found: set[str] = set()
        for value in m.index.globals.get(attr) or []:
            if isinstance(value, ast.Call):
                default = _getattr_default(m, value)
                if default is None:
                    continue
                found.add(str(cast(ast.Constant, value.args[1]).value))
                value = default
            if isinstance(value, ast.Attribute):
                found.add(value.attr)
            elif isinstance(value, ast.Name) and value.id != "<parameter>":
                found.add(value.id)
        cache[attr] = found - {attr}
    return cache[attr]


#: The modules whose attributes are the builtins below (builtins.exec,
#: operator.methodcaller).
_REFLECTION_MODULES = frozenset({"builtins", "operator"})


def _builtin(m: _Module, func: ast.AST) -> str | None:
    """Which builtin that reaches code by a name or text computed at run
    time a callee may be (getattr, setattr, exec, eval, compile, attrgetter,
    methodcaller): by any identity it may have (_Identities.may: the builtin
    of its name, an import of one under any name, a name bound to one, an
    attribute of builtins or operator) or by its own name (this adds
    findings, so spelling counts too); or any object's __getattribute__ (the
    attribute protocol's own dunder)."""
    if isinstance(func, ast.Attribute) and func.attr == "__getattribute__":
        return func.attr
    if not isinstance(func, (ast.Name, ast.Attribute)):
        return None
    names = m.ids.may_from(func, _REFLECTION_MODULES)
    if isinstance(func, ast.Name):
        names.add(func.id)
        for value in m.bindings(func, func.id) or []:
            if isinstance(value, ast.Name):
                names.add(value.id)
            elif isinstance(value, ast.Attribute):
                names.add(_builtin(m, value) or "")
    elif isinstance(func.value, ast.Name) and func.value.id in _REFLECTION_MODULES:
        names.add(func.attr)  # builtins.exec spelled so (obj.compile(...) is a method)
    hits = sorted(names & _REFLECTION)
    return hits[0] if hits else None


def _literal_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier()


def _verbs(m: _Module, call: ast.Call) -> set[str]:
    return _callee_names(m, call.func) or {_callee(call)}


#: The mutation verb a call is taken as when it may be several, the one
#: writing the most first (REPLACE nulls what it omits).
_VERB_ORDER = ("replace", "replace_map", "insert_or_update_map", "insert_map", "update_map", "insert_or_update",
               "insert", "update", "delete")


def _mutation_verb(m: _Module, call: ast.Call) -> str:
    names = _verbs(m, call)
    return next((verb for verb in _VERB_ORDER if verb in names), _callee(call))


def _mutation_parts(m: _Module, call: ast.Call) -> tuple[ast.AST | None, ast.AST | None, bool] | None:
    """(table, columns, is_delete) of a call that is a mutation, else None.
    A missing part (None) is one that is not known."""
    name = _mutation_verb(m, call)
    keywords: dict[str, ast.AST] = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    spread = [kw.value for kw in call.keywords if kw.arg is None]
    payloads = [_dict_fields(m, value) for value in spread]
    signature = AMBIGUOUS_VERBS.get(name)
    # insert/update/replace/delete are also list, dict and str methods: one
    # is a mutation when it is called like one (mutation keywords, a spread
    # dict with a table, or a mutation's positional shape: columns a
    # sequence of names, or a key set to delete).
    is_mutation = (
        name in MUTATION_VERBS
        or (signature is not None and (
            MUTATION_KEYWORDS & keywords.keys()
            or any("table" in payload for payload in payloads)
            or (len(call.args) == signature and _positional_shape(m, name, call.args))))
        or ("table" in keywords and (MUTATION_KEYWORDS - {"table"}) & keywords.keys())
    )
    first = keywords.get("table", call.args[0] if call.args else None)
    if not is_mutation and signature is not None and first is not None:
        resolved = m.values(first) or []
        is_mutation = any(names_table(value) for value in resolved)
    if not is_mutation:
        return None
    spread_columns = False
    for payload in payloads:
        for key, item in payload.items():
            if key not in keywords:
                keywords[key] = item
                spread_columns = spread_columns or key == "columns"
        if not payload:
            return None, None, name == "delete"
    table = keywords.get("table", call.args[0] if call.args else None)
    columns = keywords.get("columns", call.args[1] if len(call.args) > 1 and name != "delete" else None)
    # Columns from a spread dict are not written out at the call (the dict may
    # be changed before it is spread): every column.
    return table, None if spread_columns else columns, name == "delete"


def _positional_shape(m: _Module, name: str, args: list[ast.expr]) -> bool:
    if name == "delete":
        keys = args[1]
        label = _callee(keys) if isinstance(keys, ast.Call) else (
            keys.id if isinstance(keys, ast.Name) else keys.attr if isinstance(keys, ast.Attribute) else "")
        return "keyset" in label.lower().replace("_", "")
    return m._is_sequence(args[1], frozenset()) and bool(m.values(args[1]))


def _dict_fields(m: _Module, node: ast.AST) -> dict[str, ast.AST]:
    """The literal fields of a dict a ``**`` spreads: a dict display or
    dict(...) bound to the name, and ``name['key'] = value`` after it."""
    fields: dict[str, ast.AST] = {}
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        # payload["update"] of a dict display bound to payload.
        outer = _dict_fields(m, node.value)
        inner = outer.get(node.slice.value)
        return _dict_fields(m, inner) if inner is not None else {}
    sources = m.bindings(node, node.id) or [] if isinstance(node, ast.Name) else [node]
    for source in sources:
        if isinstance(source, ast.Dict):
            for key, value in zip(source.keys, source.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    fields[key.value] = value
        elif isinstance(source, ast.Call) and _builtin_name(m, source.func) == "dict":
            fields.update({kw.arg: kw.value for kw in source.keywords if kw.arg})
    if isinstance(node, ast.Name):
        for function in m.functions.get(id(node), [])[-1:]:
            for sub in ast.walk(function):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        # The same variable: its name resolves to the same scope
                        # there (a nested function's own name of that spelling is
                        # another dict).
                        if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                                and target.value.id == node.id and isinstance(target.slice, ast.Constant)
                                and isinstance(target.slice.value, str)
                                and _binding_scope(m, target.value, node.id) == _binding_scope(m, node, node.id)):
                            fields[target.slice.value] = sub.value
    return fields


#: Mutations that keep the columns they omit (Spanner's Update, Insert of a
#: new row, InsertOrUpdate): only a listed column is written. Replace nulls
#: every column it omits, and a _map or any other write call is not read.
_COLUMN_PRESERVING_VERBS = frozenset({"insert", "update", "insert_or_update"})


def _writes_limit_columns(m: _Module, columns: ast.AST | None) -> bool:
    """Whether a mutation's columns may include a limit column: unless they
    are written out at the call as a tuple or list of string literals (a
    name, a constant, a list later extended or anything computed counts as
    every column), they may."""
    if not isinstance(columns, (ast.Tuple, ast.List)) or not all(
            isinstance(c, ast.Constant) and isinstance(c.value, str) for c in columns.elts):
        return True
    return any(str(c.value).lower() in LIMIT_COLUMNS for c in columns.elts if isinstance(c, ast.Constant))


#: Calls that run SQL: their statement must be resolved to literals in a scope
#: that writes the table.
_EXECUTORS = frozenset({"execute", "executemany", "executescript", "execute_sql", "execute_update",
                        "execute_partitioned_dml", "batch_update", "execute_batch_dml"})
#: Builtins that reach an attribute, or run code, by a name or text computed
#: at run time.
_FETCHERS = frozenset({"getattr", "attrgetter", "methodcaller", "__getattribute__"})
_EVALUATORS = frozenset({"exec", "eval", "compile"})
_REFLECTION = _FETCHERS | _EVALUATORS | {"setattr"}


def _python_findings(m: _Module) -> Iterator[_Finding]:
    found = list(_string_findings(m))
    marks = scope_marks(m)
    mentions = {outer for outer, (seen, _anchor) in marks.items() if "table" in seen}
    found += list(_statement_calls(m, mentions))
    found += list(_indirect_uses(m, mentions))
    found += list(_escape_findings(m))
    yield from found
    # The backstop, which does not depend on parsing: a scope holding the
    # table, a limit column and a write anywhere (in the functions, lambdas,
    # comprehensions and class bodies nested in it too) writes every limit
    # column.
    by_scope: dict[str, list[_Finding]] = {}
    for finding in found:
        if finding.style.startswith("key_limit."):
            by_scope.setdefault(m.outer(finding.node), []).append(finding)
    for outer, (seen, anchor) in marks.items():
        if seen >= _BACKSTOP:
            for existing in by_scope.get(outer, []):
                yield _Finding(existing.style, existing.node, True)
            if outer not in by_scope:
                yield _Finding("key_limit.scope", anchor, True)


_LIMIT_WORD = re.compile(r"\b(?:" + "|".join(sorted(LIMIT_COLUMNS)) + r")\b", re.IGNORECASE)
#: Names of mutation and DML APIs, as identifiers or attributes.
_WRITE_APIS = frozenset({"insert", "update", "insert_or_update", "replace", "upsert", "execute_update",
                         "batch_update", "execute_partitioned_dml", "execute_batch_dml", "insert_map", "update_map",
                         "insert_or_update_map", "replace_map"})
_BACKSTOP = frozenset({"table", "limit", "write"})


def scope_marks(m: _Module) -> dict[str, tuple[set[str], ast.AST]]:
    """Per scope: which of the table, a limit column and a write it holds
    anywhere, and a node in it. A scope is an outermost function with every
    function, lambda, comprehension and class body nested in it, or a module
    statement or class body outside any function (_Module.outer).

    The table: a string piece naming it, or a name or attribute holding it (a
    module constant such as KEY_LIMIT_TABLE). A limit column: a string piece
    naming one, an identifier or attribute spelled as one, or a name holding
    one. A write: a DML word in a string piece, an identifier or attribute
    spelled as a mutation or DML API (or Mutation...), getattr(obj, "name")
    read as obj.name (_Fetches). Docstrings aside."""
    marks: dict[str, tuple[set[str], ast.AST]] = {}

    def at(node: ast.AST) -> set[str]:
        key = m.outer(node)
        return marks.setdefault(key, (set(), m.outer_nodes.get(key, node)))[0]

    for node in m.ids.nodes:
        seen = at(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_docstring(node, m.parents):
                continue
            if names_table(node.value):
                seen.add("table")
            if _LIMIT_WORD.search(node.value):
                seen.add("limit")
            if _DML_WORD.search(_LOCK.sub(" ", node.value)):
                seen.add("write")
        elif isinstance(node, ast.keyword) and node.arg and node.arg.lower() in LIMIT_COLUMNS:
            seen.add("limit")
        elif isinstance(node, (ast.Name, ast.Attribute)):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            if ident.lower() in LIMIT_COLUMNS:
                seen.add("limit")
            if ident in _WRITE_APIS or ident.startswith("Mutation"):
                seen.add("write")
            if isinstance(node.ctx, ast.Load):
                values = m.values(node) or []
                if any(classify(value) == "name" for value in values):
                    seen.add("table")
                if any(_LIMIT_WORD.search(value) for value in values):
                    seen.add("limit")
    # Rendered string expressions too, so a name split across literals
    # ('day_limit_' + 'micro') is seen whole.
    for node in _top_level_strings(m):
        if isinstance(node, ast.Constant):
            continue
        seen = at(node)
        for text in m.texts(node):
            known = text.replace(UNKNOWN, " ")
            if names_table(known):
                seen.add("table")
            if _LIMIT_WORD.search(known):
                seen.add("limit")
            if _DML_WORD.search(_LOCK.sub(" ", known)):
                seen.add("write")
    return marks


def _statement_calls(m: _Module, mentions: set[str]) -> Iterator[_Finding]:
    """Each call that runs a statement (an executor by name or alias, a
    helper that runs a statement it is given, or any call given SQL text
    naming the table): its statement is classified where it is whole; in a
    scope that mentions the table a part not resolved to literals fails
    closed, whatever is known around it; and a statement given to a helper
    that may change it before it runs fails closed in such a scope, or
    wherever it names the table."""
    for node in m.ids.nodes:
        if not isinstance(node, ast.Call):
            continue
        mentioned = m.outer(node) in mentions
        for statement, changed in _statements_run(m, node):
            texts = m.texts(statement)
            for text in texts:
                if classify(text) == "sql":
                    yield _Finding("key_limit.sql", node, changed or assigns_limit_column(text))
                known = text.replace(UNKNOWN, " ")
                if UNKNOWN in text and _DML_WORD.search(_LOCK.sub(" ", known)):
                    # A write with a part not known: dynamic SQL, in any scope.
                    yield _Finding("key_limit.sql" if names_table(known) else "sql.dynamic", node, True)
            if changed and (mentioned or any(names_table(text) for text in texts)):
                yield _Finding("key_limit.sql", node, True)
            elif mentioned and any(UNKNOWN in text for text in texts) and not _timeout_setting(m, statement):
                yield _Finding("key_limit.sql", node, True)


def _elements(node: ast.AST) -> list[ast.AST]:
    """A statement argument's statements: each of a list or tuple literal
    (the first of a nested tuple, a batch's (sql, params, types)), or itself."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [element.elts[0] if isinstance(element, (ast.List, ast.Tuple)) and element.elts else element
                for element in node.elts]
    return [node]


def _statements_run(m: _Module, call: ast.Call) -> list[tuple[ast.AST, bool]]:
    """The statements a call runs, each with whether it may be changed
    before it runs: an executor's (by name or alias), a statement-running
    helper's (_Index.sinks), and any argument that is SQL text naming the
    table."""
    statements: list[tuple[ast.AST, bool]] = []
    verbs = _verbs(m, call)
    if verbs & _EXECUTORS:
        first = _executed_statement(m, call)
        statements += [(statement, False)
                       for statement in (_elements(first) if first is not None else [ast.Name(id="<parameter>")])]
    helper = _helper(m, call.func)
    for name in sorted(verbs - _EXECUTORS):
        # A callee that is not, by every binding, the helper of that name
        # (run = with_limit(run)) may change the statement.
        changes = helper is None or helper.name != name
        for sink in m.sinks.get(name, ()):
            statements += [(statement, sink.changes or changes) for argument in _sink_arguments(call, sink)
                           for statement in _elements(argument) if _may_be_text(m, statement)]
    run = {id(statement) for statement, _changed in statements}
    for argument in [*call.args, *(kw.value for kw in call.keywords)]:
        for element in _elements(argument):
            if id(element) in run:
                continue
            texts = m.texts(element)
            if any(names_table(text) and classify(text) != "name" and (
                    classify(text) == "sql" or _READ_WORD.search(text) or _SQL_CLAUSE.search(text))
                   for text in texts):
                statements.append((element, False))
    return statements


def _indirect_uses(m: _Module, mentions: set[str]) -> Iterator[_Finding]:
    """In a scope that mentions the table, each use of code by a name or
    text computed at run time, and each use of what may run a statement or
    write a mutation other than calling it, fails closed (every column):

    - getattr, attrgetter, methodcaller or __getattribute__ given a name that
      is not a literal (getattr(obj, "name") is read as obj.name), setattr
      given one, and attrgetter or methodcaller naming an executor, a
      mutation API or a statement-running helper;
    - exec, eval and compile;
    - an executor, a mutation API, a statement-running helper, or getattr,
      setattr, exec or eval itself, passed on, stored, returned or wrapped
      (functools.partial, a decorator applied to it) instead of called or
      bound to a name or attribute (whose calls are followed)."""
    targets = _INDIRECT_TARGETS | m.sinks.keys()
    for node in m.ids.nodes:
        if m.outer(node) not in mentions:
            continue
        if isinstance(node, ast.Call):
            builtin = _builtin(m, node.func)
            if builtin in _EVALUATORS:
                yield _Finding("key_limit.indirect", node, True)
            elif builtin is not None and _computed_name(builtin, node, targets):
                yield _Finding("key_limit.indirect", node, True)
        elif (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
              and _refers_to(m, node, targets) and not _called_or_bound(m, node)):
            yield _Finding("key_limit.indirect", node, True)


#: What may run a statement or write a mutation when called.
_INDIRECT_TARGETS = _EXECUTORS | _WRITE_APIS | MUTATION_VERBS


def _computed_name(builtin: str, call: ast.Call, targets: Iterable[str]) -> bool:
    """Whether a getattr-like call reaches a name not known (or, for
    attrgetter and methodcaller, one that runs a statement or writes)."""
    if builtin in ("getattr", "setattr"):
        names = call.args[1:2]
    elif builtin == "__getattribute__":
        names = call.args[-1:]
    elif builtin == "methodcaller":
        names = call.args[:1]
    elif builtin == "attrgetter":
        names = call.args
    else:
        return False
    if not names or not all(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in names):
        return True
    return builtin in ("attrgetter", "methodcaller") and any(
        str(cast(ast.Constant, n).value).rsplit(".", 1)[-1] in targets for n in names)


def _refers_to(m: _Module, node: ast.Name | ast.Attribute, targets: Iterable[str]) -> bool:
    """Whether a name or attribute is an executor, a mutation API, a
    statement-running helper, or getattr, setattr, exec or eval: by its own
    name (a name only when no variable of that name is bound), or through
    one it is bound to."""
    reach = set(targets) | _REFLECTION
    if isinstance(node, ast.Attribute):
        return bool((_callee_names(m, node) | m.ids.may_from(node, _REFLECTION_MODULES)) & reach)
    aliased = _callee_names(m, node) - {node.id}
    # By identity too: a name an import or another name binds to one of the
    # reflection builtins (from builtins import getattr as fetch).
    bound = {identity.rpartition(".")[2] for identity in m.ids.bound_may(node)
             if identity.rpartition(".")[0] in _REFLECTION_MODULES}
    return bool((aliased | bound) & reach) or (node.id in reach and not m.bindings(node, node.id))


def _called_or_bound(m: _Module, node: ast.AST) -> bool:
    parent = m.parents.get(id(node))
    if isinstance(parent, ast.Call) and parent.func is node:
        return True
    if isinstance(parent, ast.Assign) and parent.value is node:
        return all(isinstance(target, (ast.Name, ast.Attribute)) for target in parent.targets)
    return (isinstance(parent, ast.AnnAssign) and parent.value is node
            and isinstance(parent.target, (ast.Name, ast.Attribute)))


# -- helpers that run a statement they are given ---------------------------


class _Sink(NamedTuple):
    """A helper that runs a statement it is given: where a caller passes it
    (a position among the arguments a caller gives, self or cls aside, or
    -1, and the parameter's name; rest for *args from that position or
    **kwargs), and whether it may change the statement before it runs."""

    position: int
    name: str
    changes: bool
    rest: bool = False


#: Decorators that do not change the arguments a function is called with,
#: by identity (_Identities.of: a builtin's name nothing binds, a name only
#: imported from its module under any name, an attribute of that module
#: only imported as a module).
_PLAIN_DECORATORS = frozenset({
    "builtins.staticmethod", "builtins.classmethod", "builtins.property", "functools.cached_property",
    "functools.cache", "functools.lru_cache", "abc.abstractmethod", "typing.overload",
    "typing_extensions.overload", "typing.override", "typing_extensions.override"})


def _plain_decorator(m: _Module, function: ast.AST, decorator: ast.expr) -> bool:
    """A decorator whose identity, by every binding, is one of
    _PLAIN_DECORATORS (from functools import singledispatch as cache is
    singledispatch, whatever its name), or a property's setter, getter or
    deleter in the class that defines the property: every binding of the
    property's name in the module a def of that class that is the property
    (decorated by the builtin property, or by that name's own setter,
    getter or deleter), and no attribute of the decorator's name (setter,
    getter, deleter) bound anywhere in the module."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if m.ids.of(target) in _PLAIN_DECORATORS:
        return True
    # setter, getter and deleter: the property object's own methods.
    if not (isinstance(target, ast.Attribute) and target.attr in ("setter", "getter", "deleter")
            and isinstance(target.value, ast.Name)):
        return False
    cls = m.parents.get(id(function))
    name = target.value.id
    sites = m.ids.names.get(name) or []
    definitions = [m.ids.definitions.get(cast(str, payload)) for kind, payload in sites if kind == "identity"]

    def the_property(item: ast.AST | None) -> bool:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or m.parents.get(id(item)) is not cls:
            return False
        return any(m.ids.of(d) == "builtins.property" or (
            isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter")
            and isinstance(d.value, ast.Name) and d.value.id == name) for d in item.decorator_list)

    return (isinstance(cls, ast.ClassDef) and bool(sites) and len(definitions) == len(sites)
            and all(the_property(item) for item in definitions)
            and any(m.ids.of(d) == "builtins.property" for item in definitions
                    for d in cast(ast.FunctionDef, item).decorator_list)
            and not m.ids.attributes.get(target.attr) and not m.ids.any_attribute)


def _class_bindings(item: ast.AST) -> set[str]:
    found: dict[str, list[ast.AST]] = {}
    for node in _scope_nodes([item]):
        _collect(node, found, found, set())
    return set(found)


def _decorated(m: _Module, function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return not all(_plain_decorator(m, function, decorator) for decorator in function.decorator_list)


def _parameters(m: _Module, function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, _Sink]:
    """Each parameter, as the place a caller passes it."""
    arguments = function.args
    positional = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
    if positional and isinstance(m.parents.get(id(function)), ast.ClassDef) and not any(
            m.ids.of(d) == "builtins.staticmethod" for d in function.decorator_list):
        positional = positional[1:]
    found = {name: _Sink(index, name, False) for index, name in enumerate(positional)}
    for argument in arguments.kwonlyargs:
        found[argument.arg] = _Sink(-1, argument.arg, False)
    if arguments.vararg:
        found[arguments.vararg.arg] = _Sink(len(positional), arguments.vararg.arg, False, rest=True)
    if arguments.kwarg:
        found[arguments.kwarg.arg] = _Sink(-1, arguments.kwarg.arg, False, rest=True)
    return found


def _sink_arguments(call: ast.Call, sink: _Sink) -> list[ast.AST]:
    """The arguments a call gives a helper where it takes its statement."""
    if sink.rest:
        if sink.position >= 0:
            return list(call.args[sink.position:])
        return [kw.value for kw in call.keywords]
    for keyword in call.keywords:
        if keyword.arg == sink.name:
            return [keyword.value]
    if 0 <= sink.position < len(call.args) and not any(
            isinstance(a, ast.Starred) for a in call.args[:sink.position + 1]):
        return [call.args[sink.position]]
    if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
        return [ast.Name(id="<parameter>")]  # given through a spread: not known
    return []


class _Calls(NamedTuple):
    function: ast.FunctionDef | ast.AsyncFunctionDef
    module: _Module
    calls: list[tuple[ast.Call, set[str], bool]]


def _computed_call(m: _Module, call: ast.Call) -> list[ast.AST] | None:
    """The arguments of a call whose callee is reached by a name not known
    (getattr(obj, name)(...), methodcaller(name, ...)(obj)), else None."""
    func = call.func
    if isinstance(func, ast.Call):
        builtin = _builtin(m, func.func)
        if builtin == "methodcaller":
            return [*func.args[1:], *(kw.value for kw in func.keywords)]
        if builtin in _FETCHERS:
            return [*call.args, *(kw.value for kw in call.keywords)]
    return None


def derived_sinks(m: _Module) -> dict[str, list[_Sink]]:
    """The module's helpers that run a statement they are given, by name (a
    function or method of the module, a dunder aside), for its own calls:
    one whose parameter reaches, through the names it uses, the statement
    an executor or another such helper runs, or an argument of a call by a
    computed name, directly or in a function nested in it. It may change
    the statement when the statement is not the parameter itself, it is
    decorated, the helper it gives it to may, or the call is by a computed
    name; one that holds exec, eval or compile may run any parameter,
    changed. (A helper in another module is outside the threat model.)"""
    functions: list[_Calls] = []
    for node in m.ids.nodes:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__")
                and _parameters(m, node)):
            calls = [(call, _verbs(m, call),
                      _builtin(m, call.func) in _EVALUATORS or _computed_call(m, call) is not None)
                     for call in ast.walk(node) if isinstance(call, ast.Call)]
            if calls:
                functions.append(_Calls(node, m, calls))
    sinks: dict[str, list[_Sink]] = {}
    while True:  # until nothing changes: the sinks only grow, and are finite
        reach = sinks.keys() | _EXECUTORS
        changed = False
        for entry in functions:
            if not any(special or verbs & reach for _call, verbs, special in entry.calls):
                continue
            found = _sink_entries(entry, sinks)
            known = set(sinks.get(entry.function.name, ()))
            if not found <= known:
                sinks[entry.function.name] = sorted(known | found)
                changed = True
        if not changed:
            break
    return sinks


def _sink_entries(entry: _Calls, sinks: dict[str, list[_Sink]]) -> set[_Sink]:
    m, function = entry.module, entry.function
    parameters = _parameters(m, function)
    decorated = _decorated(m, function)
    found: set[_Sink] = set()
    for call, verbs, special in entry.calls:
        if special and _builtin(m, call.func) in _EVALUATORS:
            found |= {sink._replace(changes=True) for sink in parameters.values()}
            continue
        run: list[tuple[ast.AST, bool]] = []
        if verbs & _EXECUTORS:
            first = _executed_statement(m, call)
            run += [(statement, False) for statement in (_elements(first) if first is not None else [])]
        for name in sorted(verbs - _EXECUTORS):
            for sink in sinks.get(name, ()):
                run += [(statement, sink.changes) for argument in _sink_arguments(call, sink)
                        for statement in _elements(argument)]
        if special:
            run += [(argument, True) for argument in _computed_call(m, call) or []]
        for statement, changes in run:
            for name in _parameters_reached(m, statement, parameters):
                direct = isinstance(statement, ast.Name) and statement.id == name and all(
                    _is_parameter(value) for value in m.bindings(statement, name) or [])
                found.add(parameters[name]._replace(changes=changes or decorated or not direct))
    return found


def _is_parameter(value: ast.AST) -> bool:
    return isinstance(value, ast.Name) and value.id == "<parameter>"


def _parameters_reached(m: _Module, expression: ast.AST, parameters: dict[str, _Sink]) -> set[str]:
    """The parameters a statement's text is built from: the statement
    itself, a piece of a concatenation, f-string, % or .format, a string
    method's receiver or argument, a branch of a conditional, an argument
    of a call that builds it, through the names it uses and what they are
    bound to (not a condition, a comparison or an attribute's object)."""
    reached: set[str] = set()
    visited: set[int] = set()
    queue = [expression]
    while queue and len(visited) < 256:
        node = queue.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if isinstance(node, ast.Name):
            bound = m.bindings(node, node.id) or []
            if node.id in parameters and any(_is_parameter(value) for value in bound):
                reached.add(node.id)
            queue += [value for value in bound if not _is_parameter(value)]
        elif isinstance(node, ast.JoinedStr):
            queue += [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp):
            queue += [node.left, node.right]
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            queue += node.elts
        elif isinstance(node, ast.Dict):
            queue += node.values
        elif isinstance(node, ast.IfExp):
            queue += [node.body, node.orelse]
        elif isinstance(node, ast.BoolOp):
            queue += node.values
        elif isinstance(node, (ast.Starred, ast.FormattedValue)):
            queue.append(node.value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _STRING_METHODS:
                queue.append(node.func.value)
            queue += [*node.args, *(keyword.value for keyword in node.keywords)]
    if queue:
        return set(parameters)  # past the bound: any parameter may reach it
    return reached


def _may_be_text(m: _Module, node: ast.AST, depth: int = 0) -> bool:
    """Whether an argument may carry statement text: anything but a
    non-string literal, a comparison or a negation (or a name bound only to
    those)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    if isinstance(node, (ast.Compare, ast.UnaryOp)):
        return False
    if isinstance(node, ast.Name) and depth < 4:
        bound = m.bindings(node, node.id)
        return not bound or any(_is_parameter(value) or _may_be_text(m, value, depth + 1) for value in bound)
    return True


_TIMEOUT_SETTING = re.compile(r"\s*set\s+local\s+statement_timeout\s*=\s*", re.IGNORECASE)


def _timeout_setting(m: _Module, statement: ast.AST) -> bool:
    """The one statement with a part not known that clears:
    f"SET LOCAL statement_timeout = {<int>}", its value provably an int
    (int(...), or min/max of ints)."""
    if not isinstance(statement, ast.JoinedStr) or len(statement.values) != 2:
        return False
    head, value = statement.values
    return (isinstance(head, ast.Constant) and isinstance(head.value, str)
            and _TIMEOUT_SETTING.fullmatch(head.value) is not None
            and isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None
            and _provably_int(m, value.value))


def _provably_int(m: _Module, node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Call) and not node.keywords:
        builtin = _builtin_name(m, node.func)
        if builtin == "int" and len(node.args) == 1:
            return True
        if builtin in ("min", "max") and node.args:
            return all(_provably_int(m, argument) for argument in node.args)
    return False


def _executed_statement(m: _Module, call: ast.Call) -> ast.AST | None:
    """The statement an executor runs: its first argument, a sql=/query=
    keyword, or the "sql" of a request dict (the REST client's)."""
    for keyword in call.keywords:
        if keyword.arg in ("sql", "query", "statement"):
            return keyword.value
        if keyword.arg == "request" and isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "sql":
                    return value
    return call.args[0] if call.args else None


_SQL_CLAUSE = re.compile(r"\b(?:set|into|from|values|where|using|join)\b", re.IGNORECASE)


def _unknown_could_write(text: str) -> bool:
    """Whether a string naming the table, shaped as SQL (a clause word in
    its known text), has a part not known that could supply a write: unless
    its known text is a SELECT, an unknown part counts as writing every
    column. (One with a write word is held to the grammar, which fails it
    closed on the unknown part.)"""
    if UNKNOWN not in text or not names_table(text):
        return False
    known = text.replace(UNKNOWN, " ")
    return bool(_SQL_CLAUSE.search(known)) and not known.lstrip().lower().startswith("select")


# -- the escape rule ---------------------------------------------------------
#
# A value whose rendering may name the table (a string constant, f-string,
# concatenation or format naming it, a name bound to one, or a container
# literal holding one) may leave its scope only by a recognised executor or
# mutation call (judged by the rules above), or by a followed helper of the
# module, through a parameter shown to reach only executors. Every other
# route fails closed (key_limit.escape, every column). The routes are an
# allow list: a consumer not listed, of any shape, is an escape.

#: Builtins that read a value without running user code on it, and keep none
#: of its text. (print is not one: sys.stdout may be any writer.)
_ABSORBING_BUILTINS = frozenset({"len", "bool", "hash", "id", "type", "callable"})
#: Builtins whose result is built from their argument (its text, or an
#: element of it), judged in turn; set, frozenset and sorted also hash or
#: compare its elements.
_TEXT_BUILTINS = frozenset({"str", "repr", "format", "list", "tuple", "set", "frozenset", "sorted", "reversed"})
_ELEMENT_COMPARING = frozenset({"set", "frozenset", "sorted"})
#: Types whose isinstance check runs no user code.
_BUILTIN_TYPES = frozenset({"str", "bytes", "bytearray", "int", "float", "complex", "bool", "list", "tuple", "dict",
                            "set", "frozenset", "type", "object"})
#: str methods whose result holds no text of the string.
_STR_PREDICATES = frozenset({"startswith", "endswith", "count", "find", "rfind", "index", "rindex", "isidentifier",
                             "isalnum", "isalpha", "isascii", "isdecimal", "isdigit", "islower", "isnumeric",
                             "isprintable", "isspace", "istitle", "isupper"})
#: str methods whose result is the string's text, judged in turn.
_STR_TEXT_METHODS = frozenset(name for name in dir(str) if not name.startswith("_")) - _STR_PREDICATES
#: Codecs and error handlers Python ships (encode and decode look any other
#: name up in the codec registry, which user code may extend).
_STANDARD_CODECS = frozenset({"utf-8", "utf8", "ascii", "latin-1", "latin1", "utf-16", "utf-32", "strict", "ignore",
                              "replace", "backslashreplace", "xmlcharrefreplace", "surrogateescape"})
#: str methods whose result may hold an argument object itself.
_ARGUMENT_KEEPING = frozenset({"format", "format_map", "partition", "rpartition", "maketrans"})
#: str methods whose result is mutable (a list, maketrans's dict).
_MUTABLE_RESULTS = frozenset({"split", "rsplit", "splitlines", "maketrans"})
#: Error handlers with which encode and decode never raise.
_QUIET_HANDLERS = frozenset({"ignore", "replace", "backslashreplace"})
#: Methods that put a value into the local container they are called on.
_CONTAINER_MUTATORS = frozenset({"append", "extend", "insert", "add", "update", "setdefault"})
#: Methods of the builtin types (a value naming the table is always one of
#: them: every step that yields it builds one) that compare or hash the
#: receiver's elements, with one another (sort) or with an argument (index,
#: count, remove, a dict's or set's lookups and set algebra): they run an
#: element's __lt__, __eq__ or __hash__. These, and any method not of a
#: builtin type, need a provably builtin receiver.
_ELEMENT_COMPARING_METHODS = frozenset({
    "sort", "index", "count", "remove", "get", "pop", "setdefault", "discard", "update", "fromkeys", "union",
    "intersection", "difference", "symmetric_difference", "intersection_update", "difference_update",
    "symmetric_difference_update", "isdisjoint", "issubset", "issuperset"})
_ELEMENT_FREE_METHODS = frozenset(
    name for kind in (str, bytes, bytearray, list, tuple, dict, set, frozenset) for name in dir(kind)
    if not name.startswith("_")) - _ELEMENT_COMPARING_METHODS

TaintKey = tuple[int, str]
#: The scope of a key that follows every attribute of a name (a class
#: body's value is read as Cls.x or self.x).
_ATTRIBUTE = -1
_STOP: tuple[str, ast.AST | None] = ("stop", None)
_ESCAPE: tuple[str, ast.AST | None] = ("escape", None)


def _carries_table(texts: Iterable[str]) -> bool:
    return any(names_table(text.replace(UNKNOWN, " ")) for text in texts)


def _binding_scope(m: _Module, node: ast.AST, name: str) -> int:
    """The scope a name binds or first resolves to where it appears: a
    function's, lambda's, class body's or comprehension's id, or 0 for the
    module's."""
    return m.resolutions(node, name)[0][0]


def _binding_scopes(m: _Module, node: ast.AST, name: str) -> set[int]:
    """Every scope a name may resolve to where it is read (a class body's
    name may be its enclosing scope's too)."""
    return {scope for scope, _bound in m.resolutions(node, name)}


def _declared_global(m: _Module, node: ast.AST, name: str) -> bool:
    """Whether the function a name is bound in declares it global or
    nonlocal (a binding of another scope's name)."""
    functions = m.functions.get(id(node), [])
    return bool(functions) and name in m._scope(functions[-1]).declared


def _is_unknown(value: ast.AST) -> bool:
    return isinstance(value, ast.Name) and value.id in ("<parameter>", _UNKNOWN_BINDING)


def _builtin_name(m: _Module, node: ast.AST) -> str | None:
    """The builtin a callee or value is by every binding (_Identities.of):
    a name nothing in the module binds, in any form or scope (no import *,
    and a module that never writes its own namespace), or one bound only to
    that builtin (from builtins import len as size, size = len); its name,
    else None. Every rule that clears dispatches on this name, never on the
    spelling."""
    return m.ids.builtin(node)


def _builtin_type(m: _Module, node: ast.AST) -> bool:
    """A builtin type by its builtin name, or a tuple of them."""
    if isinstance(node, ast.Tuple):
        return bool(node.elts) and all(_builtin_type(m, element) for element in node.elts)
    return _builtin_name(m, node) in _BUILTIN_TYPES


# -- provably builtin values -----------------------------------------------
#
# Python hands a value to the other operand's code in an operator (__radd__,
# __rmod__, __eq__, __contains__, __getitem__ of the container indexed with
# it, __hash__ and __eq__ of the keys of a set or dict it joins, __format__ of
# the value a format spec goes to). Such a step is allowed only when the
# other operand is provably builtin. The inference is syntactic and fails
# closed: a value is provably builtin only by one of the forms below, and
# anything else (a call, an attribute, a parameter, an unpacked target, a
# with, except, import, def, class or match binding, a builtin's name used as
# a value) is not.


def _builtin_value(m: _Module, node: ast.AST, seen: frozenset[int] = frozenset()) -> bool:
    """Whether a value is provably builtin, so its methods and those of
    everything it holds run no user code: a literal; an f-string with a
    literal part or two fields (a lone {x} is x's __format__ result, a str
    subclass perhaps); a container display, comprehension, concatenation,
    % or conditional of such; a str method's result on such a str (format
    only with such arguments); str, repr, format or len of such; an element
    or slice of such; or a name each scope it may resolve to binds only to
    such: a module name only to an immutable one that no module rebinds
    (_Index.opaque, _Index.exposed), a plain class's body name only to an
    immutable one, a function's name only where no frame or namespace read
    of the module could rebind it and, holding a container, only if every
    use of it keeps what it holds provably builtin (_inert_name)."""
    if not seen:
        if id(node) not in m.builtin_values:
            m.builtin_values[id(node)] = _builtin_value(m, node, frozenset({-1}))
        return m.builtin_values[id(node)]
    if id(node) in seen or len(seen) > 16:
        return False
    seen = seen | {id(node)}
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.JoinedStr):
        lone = len(node.values) == 1 and isinstance(node.values[0], ast.FormattedValue)
        return not lone or _builtin_value(m, cast(ast.FormattedValue, node.values[0]).value, seen)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_builtin_value(m, element, seen) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(_builtin_value(m, part, seen) for part in [*node.keys, *node.values] if part is not None) and (
            None not in node.keys or all(_builtin_value(m, value, seen)
                                         for key, value in zip(node.keys, node.values, strict=True) if key is None))
    if isinstance(node, ast.Starred):
        return _builtin_value(m, node.value, seen)  # a loop's or a spread's element
    if isinstance(node, ast.BinOp):
        return _builtin_value(m, node.left, seen) and _builtin_value(m, node.right, seen)
    if isinstance(node, ast.BoolOp):
        return all(_builtin_value(m, value, seen) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _builtin_value(m, node.body, seen) and _builtin_value(m, node.orelse, seen)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return _builtin_value(m, node.value, seen) and _builtin_value(m, node.slice, seen)
    if isinstance(node, ast.Slice):
        return all(part is None or _builtin_value(m, part, seen) for part in (node.lower, node.upper, node.step))
    if isinstance(node, ast.UnaryOp):
        return _builtin_value(m, node.operand, seen)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _builtin_value(m, node.elt, seen) and all(_builtin_value(m, g.iter, seen) for g in node.generators)
    if isinstance(node, ast.DictComp):
        return (_builtin_value(m, node.key, seen) and _builtin_value(m, node.value, seen)
                and all(_builtin_value(m, g.iter, seen) for g in node.generators))
    if isinstance(node, ast.Call):
        arguments = _arguments(node)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _STR_TEXT_METHODS | _STR_PREDICATES and _builtin_str(
                m, func.value, seen):
            if func.attr in ("encode", "decode"):
                # Another codec may return a bytes or str subclass.
                return all(isinstance(a, ast.Constant) and a.value in _STANDARD_CODECS for a in arguments)
            # partition returns its separator object, maketrans its values,
            # format and format_map their arguments' __format__ results.
            return func.attr not in _ARGUMENT_KEEPING or all(_builtin_value(m, a, seen) for a in arguments)
        if _builtin_name(m, func) in ("str", "repr", "format", "len"):
            return all(_builtin_value(m, argument, seen) for argument in arguments)
        return False
    if isinstance(node, ast.Name):
        for scope, bound in m.resolutions(node, node.id):
            if not bound or any(_is_unknown(value) for value in bound):
                return False  # nothing known (a builtin's name too: a type is not a value here), or a parameter
            if not all(_builtin_value(m, value, seen) for value in bound):
                return False
            immutable = all(_immutable(value) for value in bound)
            holder = m.scope_nodes.get(scope)
            if scope == 0:
                if node.id in m.index.opaque or node.id in m.index.exposed or m.star or not immutable:
                    return False  # a module or class constant: any code (import * too) may change or rebind it
            elif isinstance(holder, ast.ClassDef):
                if holder.keywords or holder.bases or not immutable:
                    return False  # a metaclass's namespace (__prepare__) may answer a read with anything
            elif m.frames or scope in m.exposed_scopes:
                return False  # a frame read, or the function's own namespace, may rebind it
            elif not immutable:
                # Every name the same container is bound to at once (x = y = [])
                # is another way in.
                keys = {(scope, node.id)} | {(_binding_scope(m, other, other.id), other.id)
                                             for value in bound for other in _sharing(m, value)}
                flat = all(_flat(m, value) for value in bound)
                if not all(flat and _inert_name(m, key, seen, frozenset(), True)
                           or _inert_name(m, key, seen, frozenset(), False) for key in sorted(keys)):
                    return False
        return True
    return False


def _flat(m: _Module, node: ast.AST) -> bool:
    """A container display (or list(), dict(), set()) holding only immutable
    values: its elements cannot be changed, nor change it."""
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return all(_immutable(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(part is not None and _immutable(part) for part in [*node.keys, *node.values])
    return (isinstance(node, ast.Call) and not node.args and not node.keywords
            and _builtin_name(m, node.func) in ("list", "dict", "set"))


def _immutable(node: ast.AST) -> bool:
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        return True
    if isinstance(node, ast.Tuple):
        return all(_immutable(element) for element in node.elts)
    if isinstance(node, ast.BinOp):
        return _immutable(node.left) and _immutable(node.right)
    if isinstance(node, ast.IfExp):
        return _immutable(node.body) and _immutable(node.orelse)
    if isinstance(node, ast.BoolOp):
        return all(_immutable(value) for value in node.values)
    if isinstance(node, ast.Starred):
        # An element of a display of immutable values, of a str or of a str
        # method's result (a str, bytes, or a list or tuple of str).
        value = node.value
        return (isinstance(value, (ast.Tuple, ast.List, ast.Set)) and all(_immutable(e) for e in value.elts)) or (
            isinstance(value, ast.Constant)) or (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and value.func.attr in _STR_TEXT_METHODS - {"maketrans"})
    # A str method's result (a str, bytes or tuple; split's list and
    # maketrans's dict aside).
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _STR_TEXT_METHODS - _MUTABLE_RESULTS)


def _builtin_str(m: _Module, node: ast.AST, seen: frozenset[int] = frozenset()) -> bool:
    """A provably builtin str: a str literal, an f-string, or a name bound
    only to them."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return _builtin_value(m, node, seen)
    if isinstance(node, ast.Name):
        bound = m.bindings(node, node.id) or []
        return bool(bound) and _builtin_value(m, node, seen) and all(
            not _is_unknown(value) and _builtin_str(m, value, seen | {id(node)})
            for value in bound if id(value) not in seen)
    return False


def _inert_name(m: _Module, key: TaintKey, seen: frozenset[int], visiting: frozenset[TaintKey],
                flat: bool = False) -> bool:
    """Whether every use of a function's (or comprehension's) local name
    keeps what it holds, and everything held in that, provably builtin
    (_inert_use); flat: and only immutable values ever enter it."""
    if key in visiting:
        return True  # being checked further up: its other uses decide
    if len(visiting) > 8:
        return False
    visiting = visiting | {key}
    return all(_inert_use(m, use, seen, visiting, flat=flat) for use in _uses(m, key))


def _enters(m: _Module, value: ast.AST, seen: frozenset[int], flat: bool, elements: bool = False) -> bool:
    """Whether a value put into a container keeps it provably builtin (and,
    flat, holding only immutable values: an immutable value, or, when only
    its elements enter (extend, update, +=), a flat display of them)."""
    return _builtin_value(m, value, seen) and (
        not flat or _immutable(value) or (elements and _flat(m, value)))


def _inert_use(m: _Module, node: ast.AST, seen: frozenset[int], visiting: frozenset[TaintKey],
               depth: int = 0, flat: bool = False) -> bool:
    """Whether one use of a container (or of an element of it, a copy of it
    or a method's result on it, each followed in turn) can put into it, or
    into anything it holds, nothing that is not provably builtin, and hands
    it to no code that could: indexed (stored into only with provably
    builtin keys and values, += only with a provably builtin value), given
    to its own methods with provably builtin arguments, looped over into
    names used so in turn, compared or an operand only beside provably
    builtin values, tested, negated, formatted, a discarded expression, measured with len or bool, or copied with list,
    tuple, sorted, reversed, set or frozenset. Bound to another name,
    passed on, returned or stored anywhere else, it may be changed. A flat
    container's (one only immutable values ever enter) elements, and its
    methods' results (an element, a copy, a view, never the container
    itself), may go anywhere."""
    if depth > 12:
        return False
    parent = m.parents.get(id(node))
    if isinstance(parent, ast.Subscript) and node is parent.value:
        if isinstance(parent.ctx, ast.Load):
            return flat or _inert_use(m, parent, seen, visiting, depth + 1)  # an element: a container perhaps
        if isinstance(parent.ctx, ast.Del):
            return True
        grand = m.parents.get(id(parent))
        if not _builtin_value(m, parent.slice, seen) or (flat and not _immutable(parent.slice)):
            return False
        if isinstance(grand, ast.Assign) and any(target is parent for target in grand.targets):
            return _enters(m, grand.value, seen, flat)
        if isinstance(grand, (ast.AnnAssign, ast.AugAssign)) and grand.target is parent and grand.value is not None:
            return _enters(m, grand.value, seen, flat)
        return False  # stored by unpacking, a loop, a with
    if isinstance(parent, ast.AugAssign) and node is parent.target:
        return _enters(m, parent.value, seen, flat, elements=True)
    if isinstance(parent, ast.Attribute) and node is parent.value:
        call = m.parents.get(id(parent))
        if isinstance(call, ast.Call) and call.func is parent and all(
                _enters(m, argument, seen, flat, elements=parent.attr in ("extend", "update"))
                for argument in _arguments(call)):
            return flat or _inert_use(m, call, seen, visiting, depth + 1)  # setdefault, copy, pop: it, an element
        return False
    if isinstance(parent, (ast.For, ast.comprehension)) and node is parent.iter:
        return flat or _inert_target(m, parent.target, seen, visiting)
    if isinstance(parent, (ast.List, ast.Tuple)) and not isinstance(parent.ctx, ast.Load):
        return False
    if isinstance(parent, (ast.BoolOp, ast.Starred, ast.List, ast.Tuple, ast.Set, ast.Dict)) or (
            isinstance(parent, ast.IfExp) and node is not parent.test):
        return _inert_use(m, parent, seen, visiting, depth + 1, flat=False)  # its value may be it
    if isinstance(parent, ast.BinOp):
        other = parent.right if node is parent.left else parent.left
        # Else other's __radd__ receives it, and may change it; and a result
        # that may be a container (boxes + [], boxes * 2, a set's |) holds
        # the same elements, so it is followed. A % result, or + with text,
        # is a new str.
        textual = isinstance(parent.op, ast.Mod) or (isinstance(parent.op, ast.Add) and (
            (isinstance(other, ast.Constant) and isinstance(other.value, (str, bytes))) or _builtin_str(m, other, seen)))
        return _builtin_value(m, other, seen) and (
            flat or textual or _inert_use(m, parent, seen, visiting, depth + 1))
    if isinstance(parent, ast.Compare):
        return all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops) or all(
            _builtin_value(m, other, seen) for other in [parent.left, *parent.comparators] if other is not node)
    if isinstance(parent, (ast.UnaryOp, ast.Expr, ast.IfExp)) or (
            isinstance(parent, ast.FormattedValue) and node is parent.value) or (
            isinstance(parent, (ast.If, ast.While, ast.Assert)) and node is parent.test):
        return True
    name = _builtin_name(m, parent.func) if isinstance(parent, ast.Call) else None
    if isinstance(parent, ast.Call) and node is not parent.func and not parent.keywords and name is not None:
        if name in ("len", "bool") and len(parent.args) == 1:
            return True
        if name in ("list", "tuple", "sorted", "reversed", "set", "frozenset") and len(parent.args) == 1:
            return flat or _inert_use(m, parent, seen, visiting, depth + 1)  # a copy holding the same elements
    return False


def _inert_target(m: _Module, target: ast.AST, seen: frozenset[int], visiting: frozenset[TaintKey]) -> bool:
    """Whether the names a loop binds a container's elements to are used
    only so (_inert_use)."""
    if isinstance(target, ast.Name):
        return _inert_name(m, (_binding_scope(m, target, target.id), target.id), seen, visiting)
    if isinstance(target, (ast.Tuple, ast.List)):
        return all(_inert_target(m, element, seen, visiting) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _inert_target(m, target.value, seen, visiting)
    return False


# -- routes ------------------------------------------------------------------


def _route(m: _Module, node: ast.AST, clean: set[tuple[str, _Sink]], binds: list[TaintKey]) -> ast.AST | None:
    """Follow a value naming the table up through what builds on it (a
    concatenation, an f-string, a format, a str method, a container, a
    subscript, a conditional, a method's result) to where it goes: a name it
    is bound to (added to binds, its uses judged in turn), an allowed
    consumer, or an escape (the node returned)."""
    while True:
        parent = m.parents.get(id(node))
        if parent is None:
            return None
        verdict, after = _step(m, node, parent, clean, binds)
        if verdict == "up":
            node = after or parent
            continue
        return node if verdict == "escape" else None


def _calls_nothing(node: ast.AST | None) -> bool:
    return node is None or not any(isinstance(sub, (ast.Call, ast.Lambda, ast.NamedExpr, ast.Await, ast.Yield,
                                                    ast.YieldFrom)) for sub in ast.walk(node))


def _plain_member(m: _Module, item: ast.stmt) -> bool:
    """A class body statement that hands the class to no code: a docstring,
    pass, a provably builtin value bound to a name, or an undecorated
    function with provably builtin defaults (no descriptor's __set_name__
    receives the class)."""
    if isinstance(item, ast.Pass) or (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)):
        return True
    if isinstance(item, ast.Assign):
        return all(isinstance(target, ast.Name) for target in item.targets) and _builtin_value(m, item.value)
    if isinstance(item, ast.AnnAssign):
        return isinstance(item.target, ast.Name) and _calls_nothing(item.annotation) and (
            item.value is None or _builtin_value(m, item.value))
    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = item.args
        annotations = [arg.annotation for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                                                  *([arguments.vararg] if arguments.vararg else []),
                                                  *([arguments.kwarg] if arguments.kwarg else [])]]
        return not item.decorator_list and all(
            _builtin_value(m, default) for default in [*arguments.defaults, *arguments.kw_defaults]
            if default is not None) and all(_calls_nothing(a) for a in [*annotations, item.returns])
    return False


def _plain_class(m: _Module, cls: ast.ClassDef, target: ast.AST) -> bool:
    """Whether a class body may hold the value: no bases (but object),
    keywords or decorators (a metaclass's namespace, __init_subclass__ or a
    decorator would run on it); bound by a plain assignment, and every
    other statement of the body plain (_plain_member: a descriptor's
    __set_name__ receives the class); and the class only ever read for an
    attribute (Cls.x), in every scanned module: passed on, given to vars()
    or instantiated, its namespace may be read."""
    if cls.keywords or cls.decorator_list or not all(
            _builtin_name(m, base) == "object" for base in cls.bases):
        return False
    statement = target
    while m.parents.get(id(statement)) is not cls:
        statement = m.parents[id(statement)]
    for item in cls.body:
        if item is statement:
            if not (isinstance(item, ast.Assign) or (isinstance(item, ast.AnnAssign)
                                                     and _calls_nothing(item.annotation))):
                return False
        elif not _plain_member(m, item):
            return False
    return m.index.uses.get(cls.name, 0) == m.index.attribute_uses.get(cls.name, 0)


def _bind(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """A value bound to a name: followed to the name's uses. Unpacked,
    stored in an attribute, a subscript or a global, or bound in a class
    body that is not plain (_plain_class): an escape. A class body's name is
    followed to every attribute of that name too (Cls.x, self.x)."""
    if not isinstance(target, ast.Name) or _declared_global(m, target, target.id):
        return _ESCAPE
    owner = m.parents.get(id(target))
    while owner is not None and not isinstance(owner, (*_FUNCTIONS, *_COMPREHENSIONS, ast.ClassDef, ast.Module)):
        owner = m.parents.get(id(owner))
    if isinstance(owner, ast.ClassDef):
        if not _plain_class(m, owner, target):
            return _ESCAPE
        binds.append((_ATTRIBUTE, target.id))
    binds.append((_binding_scope(m, target, target.id), target.id))
    return _STOP


def _bind_container(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """A value put into the container a name holds. The container must be
    made in this function (a list, dict or set display, comprehension, or
    list(), dict() or set()), directly or through names bound to one: then
    that name and every name it was bound to are followed (b = a;
    b.append(value) changes a too). A container from anywhere else (a
    parameter, a module or class name, a loop, an attribute, a call, an
    unpacked target) is shared with other scopes: an escape."""
    if not isinstance(target, ast.Name):
        return _ESCAPE
    keys: list[TaintKey] = []
    queue: list[ast.Name] = [target]
    while queue and len(keys) < 64:
        alias = queue.pop()
        key = (_binding_scope(m, alias, alias.id), alias.id)
        if key in keys:
            continue
        if key[0] == 0 or isinstance(m.scope_nodes.get(key[0]), ast.ClassDef) or _declared_global(
                m, alias, alias.id):
            return _ESCAPE
        keys.append(key)
        for value in m.bindings(alias, alias.id) or [ast.Name(id="<parameter>")]:
            if isinstance(value, ast.BinOp) and getattr(value, "augmented", False):
                value = value.left  # x += ...: x's value before
            if isinstance(value, ast.Name) and not _is_unknown(value):
                queue.append(value)
            elif not (isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
                    isinstance(value, ast.Call) and _builtin_name(m, value.func) in ("list", "dict", "set"))):
                return _ESCAPE
            else:
                queue += _sharing(m, value)  # x = y = []: x is the same container
    if queue:
        return _ESCAPE  # more names than followed: not every one is
    binds.extend(keys)
    return _STOP


def _sharing(m: _Module, value: ast.AST) -> list[ast.Name]:
    """Every name one container display is bound to at once (x = y = [],
    x = (y := []))."""
    names: list[ast.Name] = []
    holder = m.parents.get(id(value))
    while isinstance(holder, ast.NamedExpr):
        names.append(holder.target)
        holder = m.parents.get(id(holder))
    if isinstance(holder, (ast.Assign, ast.AnnAssign)):
        targets = holder.targets if isinstance(holder, ast.Assign) else [holder.target]
        names += [target for target in targets if isinstance(target, ast.Name)]
    return names


def _bind_augmented(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """x += value: a builtin string is rebound (x is followed, as for
    x = ...); a local container is changed in place; anything else runs its
    own __iadd__ or __add__ on the value: an escape."""
    if isinstance(target, ast.Name):
        bases = [value.left if isinstance(value, ast.BinOp) and getattr(value, "augmented", False) else value
                 for value in m.bindings(target, target.id) or []]
        if bases and all(_builtin_str(m, value) for value in bases):
            return _bind(m, target, binds)
    return _bind_container(m, target, binds)


def _arguments(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _step(m: _Module, node: ast.AST, parent: ast.AST, clean: set[tuple[str, _Sink]],
          binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """One step of a route: ("up", next) when the next node holds the
    value's text, ("stop", None) for an allowed consumer, a binding or a use
    that keeps none of the text and gives it to no user code, else
    ("escape", None)."""
    if isinstance(parent, ast.BinOp):
        other = parent.right if node is parent.left else parent.left
        return ("up", parent) if _builtin_value(m, other) else _ESCAPE  # its __radd__, __rmod__, ...
    if isinstance(parent, ast.FormattedValue):
        if node is parent.format_spec and not _builtin_value(m, parent.value):
            return _ESCAPE  # the spec goes to the formatted value's __format__
        return ("up", parent)
    if isinstance(parent, (ast.JoinedStr, ast.BoolOp, ast.Starred, ast.ListComp, ast.GeneratorExp)):
        return ("up", parent)
    if isinstance(parent, ast.SetComp):
        return ("up", parent) if _builtin_value(m, parent) else _ESCAPE
    if isinstance(parent, ast.DictComp):
        return ("up", parent) if node is parent.value or _builtin_value(m, parent.key) else _ESCAPE
    if isinstance(parent, (ast.Tuple, ast.List)):
        return ("up", parent) if isinstance(parent.ctx, ast.Load) else _ESCAPE
    if isinstance(parent, ast.Set) or (isinstance(parent, ast.Dict) and any(key is node for key in parent.keys)):
        others = [o for o in (parent.elts if isinstance(parent, ast.Set) else parent.keys) if o is not node]
        return ("up", parent) if all(o is not None and _builtin_value(m, o) for o in others) else _ESCAPE
    if isinstance(parent, ast.Dict):
        pairs = list(zip(parent.keys, parent.values, strict=True))
        keyed = [key if key is not None else value for key, value in pairs]  # each key, or each spread
        if any(key is None and value is node for key, value in pairs) and not all(
                _builtin_value(m, other) for other in keyed if other is not node):
            return _ESCAPE  # {**value, other: ...}: its keys meet the other keys' __hash__ and __eq__
        return ("up", parent)
    if isinstance(parent, ast.IfExp):
        return _STOP if node is parent.test else ("up", parent)
    if isinstance(parent, ast.comprehension):
        return _bind(m, parent.target, binds) if node is parent.iter else _STOP
    if isinstance(parent, ast.Subscript):
        if node is parent.value:
            if not _builtin_value(m, parent.slice):
                return _ESCAPE  # a key compared with its keys (__eq__), or an index's own code
            if isinstance(parent.ctx, ast.Load):
                return ("up", parent)
            grand = m.parents.get(id(parent))
            if isinstance(grand, ast.AugAssign) and grand.target is parent and not _builtin_value(m, grand.value):
                return _ESCAPE  # value[key] += other: the element meets other's __radd__
            return _STOP
        # A key: a KeyError holds it for a handler elsewhere; a container's
        # __getitem__ or a type's __class_getitem__ (list[value]) keeps it.
        return _ESCAPE
    if isinstance(parent, ast.Compare):
        operands = [o for o in [parent.left, *parent.comparators] if o is not node]
        if all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops) or all(
                _builtin_value(m, o) for o in operands):
            return _STOP
        return _ESCAPE  # the other operand's __eq__, __contains__, ...
    if isinstance(parent, ast.Expr) or (isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)):
        return _STOP
    if isinstance(parent, (ast.If, ast.While, ast.Assert)) and node is parent.test:
        return _STOP  # (an assert's message is raised with it: an escape)
    if isinstance(parent, ast.NamedExpr):
        verdict = _bind(m, parent.target, binds)
        return ("up", parent) if verdict == _STOP else verdict
    if isinstance(parent, ast.AugAssign):
        if node is parent.target:
            # Its value before, x in x += other: other's __radd__ meets it
            # (the result is x again, already followed).
            return _STOP if _builtin_value(m, parent.value) else _ESCAPE
        return _bind_augmented(m, parent.target, binds)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and node is parent.value:
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        verdicts = [_bind(m, target, binds) for target in targets]
        return _ESCAPE if _ESCAPE in verdicts else _STOP
    if isinstance(parent, ast.For) and node is parent.iter:
        return _bind(m, parent.target, binds)
    if isinstance(parent, ast.Attribute) and node is parent.value:
        call = m.parents.get(id(parent))
        if not (isinstance(call, ast.Call) and call.func is parent):
            return _ESCAPE
        if parent.attr not in _ELEMENT_FREE_METHODS and not _builtin_value(m, node):
            return _ESCAPE  # sort, index, a lookup, or a method not of a builtin: its elements meet each other
        arguments = _arguments(call)
        if parent.attr in ("encode", "decode"):
            # Only shipped codecs (the registry is extensible), and only with an
            # error handler that never raises: a UnicodeError holds the whole
            # text (.object) for a handler elsewhere.
            standard = all(isinstance(a, ast.Constant) and a.value in _STANDARD_CODECS for a in arguments)
            handler = call.args[1] if len(call.args) > 1 else next(
                (k.value for k in call.keywords if k.arg == "errors"), None)
            quiet = isinstance(handler, ast.Constant) and handler.value in _QUIET_HANDLERS
            return ("up", call) if standard and quiet else _ESCAPE
        if not all(_builtin_value(m, argument) for argument in arguments):
            return _ESCAPE  # a callback (sort's key), a mapping (format_map), a key compared with it
        if parent.attr in _STR_PREDICATES:
            return _STOP
        return ("up", call)  # any method's result may hold the value (get, setdefault, pop, copy, items, ...)
    if isinstance(parent, ast.keyword):
        call = m.parents.get(id(parent))
        return _argument_step(m, node, call, clean, binds) if isinstance(call, ast.Call) else _ESCAPE
    if isinstance(parent, ast.Call):
        # A value called is not text (a str is not callable); an argument is.
        return _STOP if node is parent.func else _argument_step(m, node, parent, clean, binds)
    return _ESCAPE


def _argument_step(m: _Module, node: ast.AST, call: ast.Call, clean: set[tuple[str, _Sink]],
                   binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """A value given to a call, positionally, by keyword or spread."""
    verbs = _verbs(m, call)
    if (verbs & _EXECUTORS or _mutation_parts(m, call) is not None) and _settled_callee(m, call.func):
        return _STOP  # judged by the executor and mutation rules
    helper = _helper(m, call.func)
    for name in sorted(verbs & m.sinks.keys()):
        for sink in m.sinks[name]:
            if (helper is not None and helper.name == name and (name, sink) in clean
                    and any(given is node for given in _sink_arguments(call, sink))):
                return _STOP  # a followed helper of the module, by every binding, that only runs it
    func = call.func
    builtin = _builtin_name(m, func)
    if (builtin == "isinstance" and len(call.args) == 2
            and call.args[0] is node and not call.keywords and _builtin_type(m, call.args[1])):
        return _STOP  # its class is a builtin type's: no __instancecheck__
    others = [argument for argument in _arguments(call) if argument is not node]
    if not all(_builtin_value(m, other) for other in others):
        return _ESCAPE  # beside a callback, a stream (file=), a key, a mapping: user code may receive it
    if builtin is not None:
        name = builtin
        if name in _ABSORBING_BUILTINS and not others:
            return _STOP
        if name in _TEXT_BUILTINS:
            if name in _ELEMENT_COMPARING and not _builtin_value(m, node):
                return _ESCAPE  # its elements' __hash__, __eq__, __lt__
            if name == "str" and others:
                return _ESCAPE  # str(data, encoding) decodes: a UnicodeDecodeError holds it
            return ("up", call)
    if isinstance(func, ast.Attribute) and func.attr in _STR_TEXT_METHODS and func.attr not in (
            "encode", "decode") and _builtin_str(m, func.value):
        return ("up", call)  # sep.join(...), template.format(...), text.replace(..., value)
    if isinstance(func, ast.Attribute) and func.attr in _CONTAINER_MUTATORS and isinstance(func.value, ast.Name):
        if func.attr in ("add", "update", "setdefault") and not _builtin_value(m, func.value):
            return _ESCAPE  # a key compared with keys already there
        verdict = _bind_container(m, func.value, binds)
        return ("up", call) if verdict == _STOP else verdict  # setdefault returns the value it holds
    return _ESCAPE


def _namespace_escapes(m: _Module, names: set[TaintKey]) -> list[ast.AST]:
    """The namespace reads of the module that can see a followed name:
    locals(), vars() or dir() with no argument, eval and exec in its scope
    (or a scope nested in it); globals(), the module object (sys.modules,
    __import__, importlib.import_module, a computed getattr on an imported
    name), a function's __globals__, eval, exec, and vars(), dir() or
    __dict__ of any object (a module, perhaps this one) for a module name or
    a top-level class's; and any frame read (sys._getframe, inspect's
    frames, f_locals, a traceback's tb_frame, a closure's cells) for any
    name. (A plain class itself is only ever read for an attribute:
    _plain_class.) Each is an escape: the names arrive there without a read
    the rule follows."""
    if not names:
        return []
    scopes: set[int] = set()
    for scope, _name in names:
        if scope == _ATTRIBUTE:
            continue
        holder = m.scope_nodes.get(scope)
        if holder is not None:  # a class body's or comprehension's: the scope around it
            around = m.functions.get(id(holder), [])
            scope = id(around[-1]) if around else 0
        scopes.add(scope)
    found: list[ast.AST] = []
    for node, kind in m.namespace_reads:
        stack = {id(function) for function in m.functions.get(id(node), [])}
        if (kind == "frame"
                or (kind in ("locals", "scope") and bool(scopes & stack if stack else 0 in scopes))
                or (kind in ("globals", "scope", "modules", "object") and 0 in scopes)):
            found.append(node)
    return found


def _escape_routes(m: _Module, seeds: Iterable[ast.AST], tainted: Iterable[TaintKey],
                   clean: set[tuple[str, _Sink]]) -> list[ast.AST]:
    """Every escape of the given values and of the names given, following
    each name a value is bound to (or put into) to its uses, and every
    namespace read that can see one of those names."""
    escapes: list[ast.AST] = []
    done: set[int] = set()
    names: set[TaintKey] = set()
    queue = list(seeds)
    for key in tainted:
        names.add(key)
        queue += _uses(m, key)
    while queue:
        node = queue.pop()
        if id(node) in done:
            continue
        done.add(id(node))
        binds: list[TaintKey] = []
        escape = _route(m, node, clean, binds)
        if escape is not None:
            escapes.append(escape)
        for key in binds:
            if key not in names:
                names.add(key)
                queue += _uses(m, key)
    return escapes + _namespace_escapes(m, names)


def _uses(m: _Module, key: TaintKey) -> list[ast.AST]:
    """The reads of a followed name: of a scope's name, each read that may
    resolve to it (x in x += ... too); of an attribute key, every attribute
    of that name."""
    scope, name = key
    if scope == _ATTRIBUTE:
        return m.attribute_loads.get(name, [])
    return [use for use in m.loads.get(name, []) if scope in _binding_scopes(m, use, name)]


def _augmented_target(m: _Module, node: ast.AST) -> bool:
    parent = m.parents.get(id(node))
    return isinstance(parent, ast.AugAssign) and parent.target is node


def _carries(m: _Module, value: ast.AST) -> bool:
    """Whether a value a name may be bound to may name the table, read in
    the module it is written in: its text resolved names it, or a piece of
    it does (a literal, or a name or attribute any binding of which may).
    Memoized; a cycle reads as not."""
    home = m.index.modules.get(m.index.owner.get(id(value), -1), m)
    memo = m.index.carries if id(value) in m.index.owner else home.carries
    if id(value) not in memo:
        memo[id(value)] = False
        memo[id(value)] = not _is_unknown(value) and _carries_now(home, value)
    return memo[id(value)]


def _carries_now(m: _Module, value: ast.AST) -> bool:
    resolved = m.values(value)
    if resolved is not None and _carries_table(resolved):
        return True  # (a dict's resolved values are its keys: its values are walked below)
    queue = [value]
    while queue:
        node = queue.pop()
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, bytes)) and _carries_table(m.values(node) or []):
                return True
        elif isinstance(node, (ast.Name, ast.Attribute)):
            bound = (m.bindings(node, node.id) if isinstance(node, ast.Name) else m.index.globals.get(node.attr)) or []
            if any(_carries(m, other) for other in bound):
                return True
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _STRING_METHODS:
                queue += [node.func.value, *_arguments(node)]
        elif isinstance(node, ast.Subscript):
            queue.append(node.value)
        elif isinstance(node, (ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.BoolOp, ast.IfExp, ast.Tuple,
                               ast.List, ast.Set, ast.Dict, ast.Starred)):
            queue += [child for child in ast.iter_child_nodes(node)
                      if not (isinstance(node, ast.IfExp) and child is node.test)]
    return False


def _table_values(m: _Module) -> list[ast.AST]:
    """The values whose rendering may name the table: string expressions
    (docstrings aside), and names and attributes any of whose bindings (in
    any module) is such text or a list, tuple, dict or set holding it."""
    seeds: list[ast.AST] = [node for node in _top_level_strings(m) if _carries_table(m.texts(node))]
    for node in m.ids.nodes:
        # Every string literal and f-string too, not only the outermost
        # expression: each operator and call it passes through is judged.
        if (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
                and not _is_docstring(node, m.parents) and _carries_table(m.values(node) or [])):
            seeds.append(node)
        elif isinstance(node, ast.JoinedStr) and _carries_table(m.texts(node)):
            seeds.append(node)
        if isinstance(node, (ast.Name, ast.Attribute)) and (
                isinstance(node.ctx, ast.Load) or _augmented_target(m, node)):
            bound = (m.bindings(node, node.id) if isinstance(node, ast.Name) else m.index.globals.get(node.attr)) or []
            if any(_carries(m, value) for value in bound):
                seeds.append(node)
    return seeds


def _holds_table(m: _Module, value: ast.AST) -> bool:
    return isinstance(value, (ast.Tuple, ast.List, ast.Set, ast.Dict)) and any(
        _carries_table(m.texts(sub)) for sub in ast.walk(value)
        if isinstance(sub, ast.JoinedStr) or (isinstance(sub, ast.Constant) and isinstance(sub.value, str)))


def _helper(m: _Module, func: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The module's own def a callee is, else None. By a name (or a name
    bound to one): every binding of it in the module, in any form and any
    scope, is that def (_Identities.of), and no attribute of that name is
    bound anywhere. As an attribute (self.run, Runner.run): of the class
    that defines the def or of a method's self or cls there (_own_receiver),
    every binding of the def's name in the module that def, and no
    attribute of that name bound in the module or, as an attribute or a
    class member (a subclass's override), in any other scanned module. Only
    a def undecorated or plainly decorated (_plain_decorator) is one."""
    ids = m.ids
    if isinstance(func, ast.Name):
        definition = ids.definitions.get(ids.of(func) or "")
        names = {func.id}
    elif isinstance(func, ast.Attribute):
        definition = ids.definitions.get(ids.of(ast.Name(id=func.attr, ctx=ast.Load())) or "")
        cls = m.parents.get(id(definition)) if definition is not None else None
        if not isinstance(cls, ast.ClassDef) or not _own_receiver(m, func.value, cls) or (
                m.index.class_members.get(func.attr, set()) - {id(m.tree)}):
            return None
        names = {func.attr}
    else:
        return None
    if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    names.add(definition.name)
    if ids.any_attribute or any(ids.attributes.get(name) or m.index.attribute_stores.get(name, set()) - {id(m.tree)}
                                for name in names):
        return None
    if not all(_plain_decorator(m, definition, decorator) for decorator in definition.decorator_list):
        return None
    return definition


def _own_receiver(m: _Module, receiver: ast.AST, cls: ast.ClassDef) -> bool:
    """Whether an attribute's receiver is a class, or its instance, whose
    attribute of a name is its own def: the class itself (a name every
    binding of which is it), or the first parameter of a method defined in
    its body (not a staticmethod) that nothing else in that method binds."""
    if not isinstance(receiver, ast.Name):
        return False
    if m.ids.of(receiver) == _definition_identity(cls):
        return True
    method = next((function for function in reversed(m.functions.get(id(receiver), []))
                   if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and m.parents.get(id(function)) is cls), None)
    if method is None or any(m.ids.of(d) == "builtins.staticmethod" for d in method.decorator_list):
        return False
    positional = [*method.args.posonlyargs, *method.args.args]
    return bool(positional) and positional[0].arg == receiver.id and _bindings_within(method, receiver.id) == [
        positional[0]]


def _bindings_within(function: ast.AST, name: str) -> list[ast.AST]:
    """Every node binding a name inside a function, its parameters and every
    nested scope included (whatever scope each binds in)."""
    found: list[ast.AST] = []
    for node in ast.walk(function):
        if node is function:
            continue
        if (isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load) and node.id == name) or (
                isinstance(node, ast.arg) and node.arg == name) or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name) or (
                isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name) or (
                isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names) or (
                getattr(node, "name", None) == name and isinstance(node, (ast.ExceptHandler, ast.MatchAs,
                                                                           ast.MatchStar, *_TYPE_PARAMS))) or (
                isinstance(node, ast.MatchMapping) and node.rest == name):
            found.append(node)
    return found


def _settled_callee(m: _Module, func: ast.AST, depth: int = 0) -> bool:
    """Whether a callee is all the names _callee_names gives it, so the
    escape rule may stop at it as an executor or a mutation. A name or an
    attribute spelled as an executor or a mutation API is taken by its name
    (the documented limit). Any other name or attribute is settled only
    when every binding of it is settled in turn: a name's in the module, an
    attribute's in every scanned module (obj.send = conn.execute is
    settled; obj.send = with_limit(obj.send) is not), a name or attribute
    bound nowhere being only what its own name says. getattr(obj, "name",
    default) is settled when its default is. Anything else (a lambda, a
    call's result, a parameter, an import, a def, a computed setattr) may be
    code those names do not describe."""
    if depth > 6:
        return False
    if isinstance(func, ast.Call):
        default = _getattr_default(m, func)
        return default is not None and _settled_value(m, default, depth + 1)
    if isinstance(func, ast.Name):
        if func.id in _INDIRECT_TARGETS:
            return True
        if m.ids.star or m.ids.rewrites:
            return False
        return all(kind == "value" and _settled_value(m, cast(ast.AST, payload), depth + 1)
                   for kind, payload in m.ids.names.get(func.id) or [])
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _INDIRECT_TARGETS:
        return True
    owners = set(m.index.attribute_stores.get(func.attr, set())) | (
        {id(m.tree)} if func.attr in m.ids.attributes else set())
    for owner in owners:
        module = m if owner == id(m.tree) else m.index.modules.get(owner)
        if module is None or module.ids.any_attribute:
            return False
        if not all(kind == "value" and _settled_value(module, cast(ast.AST, payload), depth + 1)
                   for _receiver, (kind, payload) in module.ids.attributes.get(func.attr, [])):
            return False
    return not m.ids.any_attribute


def _settled_value(m: _Module, value: ast.AST, depth: int) -> bool:
    """Whether a value bound to a callee's name is a settled callee: a name,
    an attribute or a getattr with a default, settled in turn."""
    while isinstance(value, ast.NamedExpr):
        value = value.value
    return isinstance(value, (ast.Name, ast.Attribute, ast.Call)) and _settled_callee(m, value, depth)


def clean_sinks(m: _Module) -> set[tuple[str, _Sink]]:
    """The module's helper parameters shown to reach only executors: every
    use of the parameter, and of each name it is bound to or put into, goes
    to an executor or mutation call, or to another such parameter, or keeps
    none of its text. A call reaches one only when it is that def by every
    binding (_helper), so never a def decorated other than plainly (the
    decorator's wrapper receives the statement first)."""
    entries = [(name, sink, function) for name, sinks in m.sinks.items() for sink in sinks
               for function in m.ids.nodes
               if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name == name]
    clean = {(name, sink) for name, sink, _function in entries}
    changed = True
    while changed:
        changed = False
        for name, sink, function in entries:
            if (name, sink) in clean and _escape_routes(m, [], [(id(function), sink.name)], clean):
                clean.discard((name, sink))
                changed = True
    return clean


def _escape_findings(m: _Module) -> Iterator[_Finding]:
    m.clean = clean_sinks(m)
    for node in _escape_routes(m, _table_values(m), [], m.clean):
        yield _Finding("key_limit.escape", node, True)


def _string_findings(m: _Module) -> Iterator[_Finding]:
    strings = list(_top_level_strings(m))
    for node in strings:
        for text in m.texts(node):
            kind = classify(text)
            if kind is not None:
                yield _Finding("key_limit." + kind, node, kind == "sql" and assigns_limit_column(text))
            elif _unknown_could_write(text):
                yield _Finding("key_limit.sql", node, True)
            if _DYNAMIC.search(text) or _DYNAMIC_HEAD.match(text):
                yield _Finding("sql.dynamic", node, True)

    inside_strings = {id(sub) for node in strings for sub in ast.walk(node) if sub is not node}
    for node in m.ids.nodes:
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load) and (
                id(node) not in inside_strings):
            resolved = m.values(node)
            if resolved and any(classify(value) == "name" for value in resolved):
                yield _Finding("key_limit.name", node, False)
        if isinstance(node, ast.Call):
            parts = _mutation_parts(m, node)
            if parts is not None:
                table, columns, delete = parts
                resolved = m.values(table) if table is not None else None
                limit = not delete and (_mutation_verb(m, node) not in _COLUMN_PRESERVING_VERBS
                                        or _writes_limit_columns(m, columns))
                if resolved is None:
                    yield _Finding("mutation.unresolved", node, limit)
                elif any(names_table(value) for value in resolved):
                    yield _Finding("key_limit.name", node, limit)
        if isinstance(node, ast.Dict):
            fields: dict[str | None, ast.AST] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                names = m.values(key) if key is not None else None
                fields[names[0] if names else None] = value
            table = fields.get("table")
            if table is not None and "values" in fields:
                resolved = m.values(table)
                # The payload's verb is its key in the dict around it.
                outer = m.parents.get(id(node))
                verbs = [m.values(k) for k, v in zip(outer.keys, outer.values, strict=True)
                         if v is node and k is not None] if isinstance(outer, ast.Dict) else []
                verb = verbs[0][0].lower() if verbs and verbs[0] else ""
                limit = verb not in _COLUMN_PRESERVING_VERBS or _writes_limit_columns(m, fields.get("columns"))
                if resolved is None:
                    yield _Finding("mutation.unresolved", node, limit)
                elif any(names_table(value) for value in resolved):
                    yield _Finding("key_limit.name", node, limit)


#: Line breaks other than a line feed: a reader that ends a line at one sees
#: what follows it as a line of its own.
_LINE_BREAKS = "\r\v\f\x1c\x1d\x1e\x85  "
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?\s*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([A-Za-z_]\w*))""")


def _strip_comments(path: Path, text: str) -> str:
    """Drop comment lines, but none that may not be one: in a shell, YAML or
    Terraform file a line in a heredoc's body (data, whatever it starts
    with, the delimiter quoted or not: psql reads "# 0; UPDATE ..." as an
    operator and a statement) or inside a string a line before left open,
    a line after one ending in a backslash (the shell continues that line),
    and a line holding another line break (a reader that ends the line there
    sees text after it)."""
    markers = {".go": ("//",), ".sql": ("--",), ".tf": ("#", "//")}.get(path.suffix, ("#",))
    shell = path.suffix not in (".sql", ".go")
    kept = []
    delimiters: list[str] = []
    quote: str | None = None
    continued = False
    for line in text.split("\n"):
        body = line.removesuffix("\r")
        if delimiters:
            if body.strip() == delimiters[0]:
                delimiters.pop(0)
            kept.append(line)
        elif quote or continued or not body.lstrip().startswith(markers) or any(c in body for c in _LINE_BREAKS):
            kept.append(line)
            if shell:
                if quote is None:
                    delimiters += [next(group for group in match.groups() if group)
                                   for match in _HEREDOC.finditer(body)]
                quote = _open_quote(body, quote)
        continued = body.endswith("\\")
    return "\n".join(kept)


def _open_quote(line: str, quote: str | None) -> str | None:
    """The quote a shell line leaves open, from the one it starts in (a
    comment ends the line; a backslash escapes outside single quotes)."""
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            quote = None if char == "'" else quote
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = None
        elif char == "\\":
            index += 1
        elif char == "#" and (index == 0 or line[index - 1] in " \t;|&("):
            break
        elif char in "'\"":
            quote = char
        index += 1
    return quote


def _dml_near_table(text: str) -> bool:
    """Whether a DML word is followed by the table before a ``;`` and within
    sixty words, whitespace collapsed (so blank lines, padding and
    indentation in a heredoc or a multiline string do not separate them); a
    lock's FOR UPDATE aside."""
    collapsed = " ".join(_LOCK.sub(" ", text).split())
    for match in _DML_WORD.finditer(collapsed):
        region = " ".join(collapsed[match.end():].split(" ")[:61])
        if names_table(region.split(";", 1)[0]):
            return True
        # A comment may hold the ; that seemed to end the statement.
        if names_table(region) and any(mark in region for mark in ("/*", "*/", "--", "#")):
            return True
    return False


def _readings(path: Path) -> list[str]:
    """A text file's comments stripped, read as written and with every
    shell line continuation joined (as is, and with the next line's
    indentation dropped, as a YAML block scalar drops it before the shell
    sees the script): a shell joins them on a command line and in a heredoc
    whose delimiter is not quoted, and passes them on in one whose delimiter
    is. A finding of any reading is one."""
    text = _strip_comments(path, path.read_text(encoding="utf-8", errors="replace"))
    return list(dict.fromkeys([text, re.sub(r"\\\r?\n", "", text), re.sub(r"\\\r?\n[ \t]*", "", text)]))


def _text_findings(path: Path) -> Iterator[tuple[str, bool]]:
    for text in _readings(path):
        yield from _reading_findings(path, text)


def _reading_findings(path: Path, text: str) -> Iterator[tuple[str, bool]]:
    if names_table(text):
        if path.suffix == ".sql":
            columns = written_columns(text)
            limit = columns is None or bool(columns & LIMIT_COLUMNS)
        else:
            # SQL inside another language's quoting (a shell argument or
            # heredoc, YAML, Terraform, Go) is not read as SQL: DML near
            # the table counts as writing every column.
            limit = _dml_near_table(text)
        if path.suffix == ".go" and _GO_MUTATION.search(text):
            limit = limit or any(re.search(rf'"{column}"', text) for column in LIMIT_COLUMNS)
        # The backstop, per file: the table, a limit column and a write.
        if _LIMIT_WORD.search(text) and _DML_WORD.search(_LOCK.sub(" ", text)):
            limit = True
        yield "key_limit.text", limit
    if _TEXT_DYNAMIC.search(text):
        yield "sql.dynamic", True
    if path.suffix == ".go":
        for match in _GO_MUTATION.finditer(text):
            if not match.group(1).startswith('"'):
                yield "mutation.unresolved", True


Finding = tuple[str, str, str]


class ScanDetail(dict[Finding, bool]):
    """Every finding, with whether its scope may write a limit column, and a
    fingerprint of the source of each finding's scope (a reviewed exception
    is pinned to the source it was reviewed at)."""

    fingerprints: dict[tuple[str, str], str]


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)


def _scope_fingerprint(m: _Module, where: str) -> str:
    """The source of the scope a finding is judged by (its outermost
    function, or its module statement or class body), positions aside (the
    SQL manifest's fingerprint, stable across Python versions): every
    definition of that qualified name in the module (a conditional or
    repeated def, or a class's duplicate method, included) and every other
    statement binding its name where it is defined (an assignment, import,
    loop target or del, or a function declaring it global), in source order.
    A module statement outside any definition: the whole module."""
    from tests.conformance.spanner_sql_inventory import fingerprint

    outer = m.outer_of.get(where, where)
    if outer == "<module>":
        return fingerprint(m.tree)
    statements: dict[int, ast.AST] = {
        id(node): node for node in m.ids.nodes
        if isinstance(node, _DEFINITIONS) and m.names.get(id(node)) == outer
        and m.names.get(id(m.parents.get(id(node)))) != outer}
    if not statements:
        return fingerprint(m.tree)
    for node in _rebindings(m, outer):
        statements.setdefault(id(node), node)
    ordered = sorted(statements.values(), key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
    return _fingerprint("\n".join(fingerprint(node) for node in ordered))


def _rebindings(m: _Module, outer: str) -> list[ast.AST]:
    """Each statement binding a definition's name in the module or class body
    it is defined in, and, for a method, each binding of that attribute of
    its class by the class's name (Cls.name = ..., setattr(Cls, "name", ...)).
    By spelling, deliberately: the fingerprint holds every statement that
    binds the name, whatever it binds it to."""
    if outer.startswith("<module>."):
        namespace, bare = "", outer.split(".", 1)[1]
    else:
        namespace, _, bare = outer.rpartition(".")
    owner = namespace.rpartition(".")[2]
    found: list[ast.AST] = []
    for node in m.ids.nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binds = node.id == bare
        elif isinstance(node, ast.alias):
            binds = (node.asname or node.name.split(".", 1)[0]) == bare
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds = node.name == bare
        elif owner and isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            binds = node.attr == bare and isinstance(node.value, ast.Name) and node.value.id == owner
            if binds:
                found.append(node)
                binds = False
        elif owner and isinstance(node, ast.Call) and _spelled(node.func) in ("setattr", "delattr") and len(
                node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == owner:
            binds = False
            if isinstance(node.args[1], ast.Constant) and node.args[1].value == bare:
                found.append(node)
        else:
            binds = False
        if binds and _namespace(m, node, bare) == namespace:
            statement: ast.AST = node
            while not isinstance(statement, ast.stmt) and id(statement) in m.parents:
                statement = m.parents[id(statement)]
            found.append(statement)
    for index, node in enumerate(found):
        while not isinstance(node, ast.stmt) and id(node) in m.parents:
            node = m.parents[id(node)]
        found[index] = node
    return found


def _namespace(m: _Module, node: ast.AST, bare: str) -> str | None:
    """The module ("") or class body (its qualified name) a binding is in,
    or None for a function's local. A function's decorators, defaults and
    annotations, and a class's bases, keywords and decorators, are in the
    scope around it (a walrus there binds outside)."""
    child, parent = node, m.parents.get(id(node))
    while parent is not None:
        body = parent.body if isinstance(parent, (*_FUNCTIONS, ast.ClassDef)) else None
        inside = any(child is item for item in (body if isinstance(body, list) else [body]))
        if isinstance(parent, _FUNCTIONS) and inside:
            declared = any(isinstance(sub, ast.Global) and bare in sub.names for sub in ast.walk(parent))
            return "" if declared else None
        if isinstance(parent, ast.ClassDef) and inside:
            return m.names.get(id(parent), "")
        child, parent = parent, m.parents.get(id(parent))
    return ""


def _module_attribute_stores(tree: ast.Module, local: Iterable[str]) -> set[tuple[str, str]]:
    """The (module, attribute) pairs a module sets on a module object it
    imports (functools.cache = ..., del os.path.sep, setattr(builtins, "len",
    ...), __builtins__ for builtins): an import's identity there holds
    nowhere in the scan (_Identities._trusted)."""
    local = set(local)
    imported: dict[str, str] = {"__builtins__": "builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                imported[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level and (
                node.module.split(".")[0] not in local):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def module(receiver: ast.AST) -> str | None:
        if isinstance(receiver, ast.Name):
            return imported.get(receiver.id)
        if isinstance(receiver, ast.Attribute):
            base = module(receiver.value)
            return f"{base}.{receiver.attr}" if base else None
        return None

    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            base = module(node.value)
            if base:
                found.add((base, node.attr))
        elif (isinstance(node, ast.Call) and _spelled(node.func) in ("setattr", "delattr") and len(node.args) >= 2
              and _literal_name(node.args[1])):
            base = module(node.args[0])
            if base:
                found.add((base, str(cast(ast.Constant, node.args[1]).value)))
    return found


class _Fetches(ast.NodeTransformer):
    """Reads getattr(obj, "name") (getattr being the builtin by every
    binding: _Identities.of, so through any alias of it the module binds to
    nothing else, an import of it under any name, or builtins.getattr with
    builtins the module) and obj.__getattribute__("name")
    (object.__getattribute__(obj, "name"), object the builtin) as obj.name
    before any rule runs, so every rule treats them exactly as the
    attribute. A getattr whose identity is not known is left a call: a
    value given to it escapes."""

    def __init__(self, ids: _Identities) -> None:
        self.ids = ids

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        target: ast.expr | None = None
        attribute: ast.expr | None = None
        if self.ids.of(func) == "builtins.getattr" and len(node.args) == 2 and not node.keywords:
            target, attribute = node.args
        elif isinstance(func, ast.Attribute) and func.attr == "__getattribute__" and not node.keywords:
            # The attribute protocol's own dunder, by its name: obj.name calls
            # type(obj).__getattribute__ too.
            if len(node.args) == 1:
                target, attribute = func.value, node.args[0]
            elif len(node.args) == 2 and self.ids.builtin(func.value) == "object":
                target, attribute = node.args
        if target is not None and attribute is not None and _literal_name(attribute):
            fetched = ast.Attribute(value=target, attr=str(cast(ast.Constant, attribute).value), ctx=ast.Load())
            return ast.copy_location(fetched, node)
        return node


def scan_paths_detail(python: Iterable[Path], text: Iterable[Path]) -> ScanDetail:
    """Every finding, with whether its scope may write a limit column."""
    python = list(python)
    sources = [p.read_text(encoding="utf-8") for p in python]
    local = {part.removesuffix(".py") for path in python
             for part in (path.relative_to(ROOT).parts if path.is_relative_to(ROOT) else path.parts[-2:])}
    trees = [ast.parse(source) for source in sources]
    foreign = frozenset().union(*(_module_attribute_stores(tree, local) for tree in trees))
    # Only a module that spells getattr or __getattribute__ can hold a call
    # the rewrite reads as an attribute (an import of getattr under another
    # name spells it too).
    trees = [cast(ast.Module, _Fetches(_Identities(tree, local, foreign)).visit(tree))
             if "getattr" in source or "__getattribute__" in source else tree
             for tree, source in zip(trees, sources, strict=True)]
    index = _Index(trees, local, {id(tree): _Identities(tree, local, foreign) for tree in trees})
    index.foreign = foreign
    modules = [_Module(path, tree, index) for path, tree in zip(python, trees, strict=True)]
    index.modules = {id(m.tree): m for m in modules}
    for m in modules:
        m.sinks = derived_sinks(m)
    found = ScanDetail()
    found.fingerprints = {}
    for m, source in zip(modules, sources, strict=True):
        written: _Module | None = None  # the module as written, for fingerprints
        for finding in _python_findings(m):
            where = m.where(finding.node)
            key = (finding.style, m.relative, where)
            found[key] = found.get(key, False) or finding.limit
            if (m.relative, where) not in found.fingerprints:
                written = written or _Module(m.path, ast.parse(source), index)
                found.fingerprints[(m.relative, where)] = _scope_fingerprint(written, where)
    for path in text:
        relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        raw = path.read_text(encoding="utf-8", errors="replace")
        for style, limit in _text_findings(path):
            key = (style, relative, "<file>")
            found[key] = found.get(key, False) or limit
            found.fingerprints[(relative, "<file>")] = _fingerprint(raw)
    return found


def scan_paths(python: Iterable[Path], text: Iterable[Path]) -> set[Finding]:
    return set(scan_paths_detail(python, text))


def limit_column_writers(detail: dict[Finding, bool]) -> set[tuple[str, str]]:
    """The (path, scope)s that name tr_key_limit and may write a limit
    column. An unresolved or dynamic finding is listed with the table it
    does write."""
    return {(path, where) for (style, path, where), limit in detail.items()
            if limit and style.startswith("key_limit.")}


def _excluded(path: Path, excluded: tuple[str, ...]) -> bool:
    """Tests (any tests directory), virtualenvs, dependencies and docs."""
    parts = path.relative_to(ROOT).parts
    return bool(parts) and parts[0] in excluded or any(
        p in (".venv", "node_modules", ".git", "tests") for p in parts)


def repository_paths() -> tuple[list[Path], list[Path]]:
    python = [p for p in sorted(ROOT.rglob("*.py")) if not _excluded(p, PY_EXCLUDED)]
    text = [p for p in sorted(ROOT.rglob("*")) if p.suffix in TEXT_SUFFIXES and p.is_file()
            and not _excluded(p, TEXT_EXCLUDED) and not p.name.endswith("_test.go")]
    return python, text


def scan_repository_detail() -> ScanDetail:
    return scan_paths_detail(*repository_paths())


def scan_repository() -> set[Finding]:
    return set(scan_repository_detail())


if __name__ == "__main__":
    detail = scan_repository_detail()
    for finding, limit in sorted(detail.items()):
        print(finding, "LIMIT" if limit else "")
    for scope in sorted(limit_column_writers(detail)):
        print("fingerprint", scope, detail.fingerprints[scope])
