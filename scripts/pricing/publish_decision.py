"""Fail closed when deciding whether generated prices can follow a moving main.

Generation inputs are derived from the refresh workflow's Python entry points
and their repository AST import closure, including package-wide dynamic imports
and Python subprocess paths. This is deliberately a static over-approximation;
unrecognized workflow syntax/commands or unresolved imports force recomputation.
The CLI needs only the standard library (the workflow invokes plain ``python``).

The pre-commit ruff, mypy and full pytest gates also cover unrelated Python code.
Full CI repeats those gates on the *new exact SHA* after every cherry-pick, so
existing .py files outside the generation closure may rebase. Tests themselves
are not exempt: changing the validation contract requires fresh generation.

The only non-code exemption is Markdown beneath docs/: human-facing design and
operator documentation, not executable inputs or configuration. Assets, locks,
configs, CI definitions and every other non-Python path are denied by default.

Input is ``git diff --name-only --no-renames -z``: both sides of renames are
checked, including deleted dependencies. No repository code is imported/executed.
"""

from __future__ import annotations

import ast
import hashlib
import re
import shlex
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/refresh-prices.yml"
PUBLICATION_STEP = "Commit, verify, and push if changed"
# This existing, tested Bash protocol contains nested substitutions/functions.
# Recognize the reviewed body exactly instead of pretending to parse arbitrary
# Bash. Any edit (including a new generator) fails closed until reviewed again.
# This pins command classification, NOT a list of generation dependencies.
PUBLICATION_SHA256 = "56bb5497e67dc0e192ab70c8bb6ee64bf4e05f9827fe1a0cb2fb2baf5645aef0"
SETUP_ACTIONS = {
    "actions/checkout@v4",
    "actions/setup-python@v7",
    "astral-sh/setup-uv@v7",
    "google-github-actions/auth@v3",
    "google-github-actions/setup-gcloud@v3",
}


class Uncertain(ValueError):
    """The static analysis cannot prove that reusing generation is safe."""


@dataclass(frozen=True)
class Dependencies:
    entry_points: frozenset[str]
    files: frozenset[str]
    directories: frozenset[str]

    def contains(self, path: str) -> bool:
        return path in self.files or any(
            path == directory or path.startswith(directory + "/")
            for directory in self.directories
        )


def workflow_steps(source: str) -> list[dict[str, str]]:
    """Read the workflow's restricted YAML shape; unsupported shapes fail closed.

    Do not require PyYAML in the runner's system Python. Only literal run blocks
    and plain inline commands are supported. Every step field is accounted for;
    aliases, flow mappings, alternate shells and working directories are rejected.
    """
    prefix, separator, body = source.partition("    steps:\n")
    if not separator or "\t" in source or "defaults:" in prefix:
        raise Uncertain("unsupported workflow structure")
    if re.findall(r"^  ([\w-]+):", prefix.partition("jobs:\n")[2], re.M) != ["refresh"]:
        raise Uncertain("unsupported refresh job")
    steps: list[dict[str, str]] = []
    field = ""
    for line in body.splitlines():
        if line.startswith("      - "):
            if steps and steps[-1].get("name") == PUBLICATION_STEP:
                return steps
            steps.append({})
            field = ""
            line = "        " + line[8:]
        if not line.strip() or line.lstrip().startswith("#"):
            if field == "run" and (not line.strip() or line.startswith("          ")):
                steps[-1][field] += line[10:] + "\n"
            continue
        if line.startswith("        ") and not line.startswith("         "):
            match = re.fullmatch(r"        ([\w-]+):(?: (.*))?", line)
            if not steps or not match:
                raise Uncertain("unsupported step field")
            field, value = match[1], match[2] or ""
            if field not in {"name", "id", "uses", "with", "env", "if", "continue-on-error", "run"}:
                raise Uncertain(f"unsupported step field: {field}")
            if field in steps[-1] or value.startswith(("&", "*", "{", ">", "'", '"')):
                raise Uncertain("unsupported YAML value")
            if field == "run":
                if value == "|":
                    value = ""
                elif not value or value.startswith("|"):
                    raise Uncertain("unsupported run scalar")
                else:
                    value += "\n"
            steps[-1][field] = value
        elif line.startswith("          ") and field in {"run", "with", "env"}:
            if field == "run":
                steps[-1][field] += line[10:] + "\n"
        else:
            raise Uncertain("unsupported workflow indentation")
    if steps and steps[-1].get("name") == PUBLICATION_STEP:
        return steps
    raise Uncertain("missing publication step")


def shell_entry_points(source: str) -> set[str]:
    """Classify the small pre-publication shell vocabulary, without executing it."""
    entries: set[str] = set()
    source = source.replace("\\\n", "").replace("echo '```'", "echo markdown_fence")
    if "`" in source or "${{" in source:
        raise Uncertain("dynamic shell source")

    def substitution(match: re.Match[str]) -> str:
        # Current substitutions fetch secret *values*, never repository code.
        command = shlex.split(match[1])
        if command[:5] != ["gcloud", "secrets", "versions", "access", "latest"]:
            raise Uncertain("unknown command substitution")
        if not all(re.fullmatch(r"--(?:secret|project)=[\w${}-]+", word) for word in command[5:]):
            raise Uncertain("compound command substitution")
        return "secret_value"

    source = re.sub(r"\$\(([^()]*)\)", substitution, source)
    if "$(" in source:
        raise Uncertain("nested command substitution")
    for line in source.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        commands: list[list[str]] = [[]]
        for token in lexer:
            if token in {";", "|", "&&", "||"}:
                commands.append([])
            else:
                commands[-1].append(token)
        for words in commands:
            # Redirections carry data only; process substitutions/heredocs fail.
            args: list[str] = []
            i = 0
            while i < len(words):
                if words[i] in {">", ">>", "<", ">&"}:
                    if i + 1 >= len(words):
                        raise Uncertain("incomplete redirection")
                    if args and args[-1].isdigit():
                        args.pop()
                    i += 2
                else:
                    if words[i] in {"(", ")", "<<", "<<<", "&", "&>"}:
                        raise Uncertain("unsupported shell operator")
                    args.append(words[i])
                    i += 1
            while args and (args[0] in {"if", "!", "then", "else", "do", "fi", "done", "{", "}"}
                            or re.fullmatch(r"[A-Za-z_]\w*=.*", args[0])):
                args.pop(0)
            if not args:
                continue
            if args[:3] == ["for", "pair", "in"]:
                if not all(re.fullmatch(r"[A-Z_0-9]+:[a-z0-9-]+", word) for word in args[3:]):
                    raise Uncertain("dynamic loop")
                continue
            if args == ["uv", "sync", "--frozen"]:
                continue
            if args[:2] == ["uv", "run"]:
                args = args[2:]
            if not args:
                raise Uncertain("missing uv command")
            if args and args[0] in {"ruff", "mypy", "pytest"}:
                # These whole-tree validation gates repeat in exact-SHA CI.
                continue
            if args and args[0] in {"python", "python3"}:
                if len(args) > 2 and args[1] == "-m":
                    module = args[2]
                    if not re.fullmatch(r"(?:scripts|trusted_router)(?:\.\w+)+", module):
                        raise Uncertain("non-repository Python module")
                    entries.add(module)
                elif len(args) > 1 and re.fullmatch(r"scripts/[\w/]+\.py", args[1]):
                    entries.add(args[1])
                else:
                    raise Uncertain("unrecognized Python invocation")
            elif args[:3] == ["gcloud", "secrets", "describe"]:
                continue
            elif args[0] in {"set", "echo", "cat", "cp", "mkdir", "tee", "grep", "exit"}:
                # Do not silently ignore Python inputs passed to data plumbing.
                entries.update(word for word in args[1:] if word.startswith(("scripts/", "src/")) and word.endswith(".py"))
            else:
                raise Uncertain(f"unclassified workflow command: {args[0]}")
    return entries


# The self-heal runner executes newly generated source, not an importable file.
# Its checked AST whitelist excludes repository imports and execution primitives;
# Ruff only rewrites the temporary candidate. This exact module AST was reviewed
# for those two forms. Any code change invalidates the adapter, rather than
# silently trusting a new command or source producer. Parser paths are still
# resolved from PARSERS_DIR below, not duplicated in a dependency path list.
PARSER_EXECUTION_AST_SHA256 = "df9632cf7e5f1dde5bcab0e2d1a4b1504d272415fdd97eb8546c0843aaa1c9f2"


class ExecutionInputs:
    """A deliberately small resolver, never a Python interpreter.

    Unknown execution and escaped/aliased execution callables are errors. Only
    literal paths, simple Path chains, single-return path helpers, and bounded
    package patterns are understood. Ordinary strings used as prose, regexes or
    filename predicates are not execution targets.
    """

    def __init__(self, closure: ImportClosure, path: str) -> None:
        self.closure = closure
        self.path = path
        self.tree = ast.parse((closure.root / path).read_text(), filename=path)
        self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        self.names: dict[str, str] = {}
        self.conflicting_names: set[str] = set()

        def bind(name: str, qualified: str) -> None:
            if name in self.names and self.names[name] != qualified:
                self.conflicting_names.add(name)
            self.names[name] = qualified

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bind(alias.asname or alias.name.split(".")[0], alias.name if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and not node.level:
                for alias in node.names:
                    bind(alias.asname or alias.name, f"{node.module}.{alias.name}")
        self.reviewed_parser = hashlib.sha256(ast.dump(self.tree).encode()).hexdigest() == PARSER_EXECUTION_AST_SHA256

    def fail(self, node: ast.AST, message: str) -> Uncertain:
        return Uncertain(f"{self.path}:{getattr(node, 'lineno', 1)}: {message}: {ast.unparse(node)[:160]}")

    def scope(self, node: ast.AST) -> ast.AST:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return self.tree

    def qualified(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return self.qualified(node.value) + "." + node.attr
        return ""

    @staticmethod
    def sensitive(name: str) -> bool:
        return (name in {"importlib.import_module", "importlib.util.spec_from_file_location",
                         "importlib.util.module_from_spec", "__import__", "exec", "eval", "compile",
                         "builtins.__import__", "builtins.exec", "builtins.eval", "builtins.compile",
                         "os.system", "os.popen"}
                or name.startswith(("importlib.", "runpy.", "subprocess.", "os.exec", "os.spawn",
                                    "os.__", "sys.__", "builtins.__dict__", "builtins.__getattribute__")))

    def binding(self, node: ast.Name) -> ast.expr:
        for scope in (self.scope(node), self.tree):
            values: list[ast.expr] = []
            for candidate in ast.walk(scope):
                if self.scope(candidate) is not scope:
                    continue
                if isinstance(candidate, (ast.Assign, ast.AnnAssign)) and candidate.value is not None:
                    targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
                    if any(isinstance(target, ast.Name) and target.id == node.id for target in targets):
                        values.append(candidate.value)
            if len(values) == 1:
                return values[0]
            if values:
                break
        raise self.fail(node, "unresolved or multiply assigned execution value")

    def value(self, node: ast.expr, depth: int = 0) -> str:
        """Resolve a path to text; '*' means all files under a known directory."""
        if depth > 20:
            raise self.fail(node, "recursive path expression")
        def resolve(child: ast.expr) -> str:
            return self.value(child, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return str(self.closure.root / self.path)
            return resolve(self.binding(node))
        if isinstance(node, ast.JoinedStr):
            pieces = []
            for item in node.values:
                if isinstance(item, ast.Constant):
                    pieces.append(str(item.value))
                elif isinstance(item, ast.FormattedValue):
                    try:
                        pieces.append(resolve(item.value))
                    except Uncertain:
                        pieces.append("*")
            return "".join(pieces)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return str(PurePosixPath(resolve(node.left)) / resolve(node.right))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return resolve(node.left) + resolve(node.right)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            return str(PurePosixPath(resolve(node.value)).parent)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int) and node.slice.value >= 0):
            parents = PurePosixPath(resolve(node.value.value)).parents
            if node.slice.value < len(parents):
                return str(parents[node.slice.value])
            raise self.fail(node, "path parent index escapes repository")
        if isinstance(node, ast.Call):
            name = self.qualified(node.func)
            if name in {"Path", "pathlib.Path", "str"} and len(node.args) == 1 and not node.keywords:
                return resolve(node.args[0])
            if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve" and not node.args and not node.keywords:
                return str((self.closure.root / resolve(node.func.value)).resolve())
            # Path helpers such as parser_path(slug): only a single return of
            # literal Path arithmetic is supported; arbitrary calls fail closed.
            owner = self
            function_name = name
            if "." in name:
                module, _, function_name = name.rpartition(".")
                if module not in self.closure.modules:
                    raise self.fail(node, "unknown path helper")
                owner = self.closure.source(self.closure.modules[module])
            functions = [item for item in owner.tree.body if isinstance(item, ast.FunctionDef) and item.name == function_name]
            if len(functions) == 1:
                function = functions[0]
                if len(function.body) == 1 and isinstance(function.body[0], ast.Return) and function.body[0].value is not None and not function.decorator_list:
                    return owner.value(function.body[0].value, depth + 1)
        raise self.fail(node, "unresolved Python path expression")

    def include(self, node: ast.expr, *, required: bool = True) -> str:
        value = self.value(node)
        # Unified-diff labels refer to the same repository file.
        if value.startswith(("a/scripts/", "b/scripts/")):
            value = value[2:]
        absolute = (self.closure.root / value).resolve()
        if not absolute.is_relative_to(self.closure.root) or ".." in PurePosixPath(value).parts:
            raise self.fail(node, "execution path escapes repository")
        path = absolute.relative_to(self.closure.root).as_posix()
        if "*" in path:
            directory = path.split("*", 1)[0].rsplit("/", 1)[0]
            if not directory or not (self.closure.root / directory).is_dir():
                raise self.fail(node, "dynamic Python path lacks a known package directory")
            self.closure.add_directory(directory)
        elif absolute.is_dir():
            self.closure.add_directory(path)
        elif absolute.is_file():
            self.closure.add_file(path)
        elif required:
            raise self.fail(node, "missing execution target")
        else:
            # A literal filename predicate may name a currently absent file.
            self.closure.files.add(path)
        return path

    def parser_adapter(self, node: ast.AST) -> bool:
        scope = self.scope(node)
        if not self.reviewed_parser or not isinstance(scope, ast.FunctionDef) or scope.name not in {
            "sandbox_run_parser", "_normalize_and_lint_parser_source",
        }:
            return False
        assignment = next(item for item in self.tree.body if isinstance(item, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "PARSERS_DIR" for target in item.targets))
        self.include(assignment.value)
        return True

    def command(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            node = self.binding(node)
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
            raise self.fail(node, "unresolved subprocess command")
        if self.qualified(node.elts[0]) != "sys.executable":
            raise self.fail(node, "unsupported subprocess executable")
        target = self.value(node.elts[1])
        if target == "-m" and len(node.elts) > 2:
            self.closure.module(self.value(node.elts[2]), required=True)
            return False
        elif target.endswith(".py"):
            self.include(node.elts[1])
            return PurePosixPath(target).is_absolute()
        else:
            raise self.fail(node, "unresolved Python command target")

    def execution(self, call: ast.Call, name: str) -> None:
        if name in {"importlib.import_module", "__import__", "builtins.__import__"} and call.args:
            self.closure.dynamic_import(call.args[0])
        elif name == "subprocess.run" and call.args:
            if any(kw.arg in {None, "executable"} or kw.arg == "shell" and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False
            ) for kw in call.keywords):
                raise self.fail(call, "subprocess execution override")
            absolute = self.command(call.args[0])
            for kw in call.keywords:
                if kw.arg == "cwd" and not absolute and (self.closure.root / self.value(kw.value)).resolve() != self.closure.root:
                    raise self.fail(call, "relative execution target with a different working directory")
        elif name == "runpy.run_module" and call.args:
            self.closure.module(self.value(call.args[0]), required=True)
        elif name == "runpy.run_path" and call.args:
            self.include(call.args[0])
        elif name in {"exec", "builtins.exec", "compile", "builtins.compile"} and call.args:
            if call.keywords or name.endswith("exec") and len(call.args) > 2:
                raise self.fail(call, "unsupported execution options")
            if name.endswith("exec") and len(call.args) == 2:
                namespace = call.args[1]
                if isinstance(namespace, ast.Name):
                    namespace = self.binding(namespace)
                if not isinstance(namespace, ast.Dict) or namespace.keys:
                    raise self.fail(call, "unresolved execution namespace")
            compiled = call.args[0] if name.endswith("exec") else call
            if not isinstance(compiled, ast.Call) or self.qualified(compiled.func) not in {"compile", "builtins.compile"} or len(compiled.args) != 3:
                raise self.fail(call, "unresolved executed source")
            if not name.endswith("exec"):
                parent = self.parents.get(call)
                if not isinstance(parent, ast.Call) or self.qualified(parent.func) not in {"exec", "builtins.exec"}:
                    raise self.fail(call, "compiled source escapes direct exec")
            source = compiled.args[0]
            if isinstance(source, ast.Name):
                source = self.binding(source)
            if not isinstance(source, ast.Call) or not isinstance(source.func, ast.Attribute) or source.func.attr != "read_text":
                raise self.fail(call, "executed source is not a resolved repository file")
            if self.include(source.func.value) != self.include(compiled.args[1]) or self.value(compiled.args[2]) != "exec":
                raise self.fail(call, "compiled source and filename disagree")
        else:
            raise self.fail(call, "unresolved execution target")

    def path_input(self, node: ast.expr) -> None:
        parent = self.parents.get(node)
        if isinstance(parent, (ast.JoinedStr, ast.Expr)):
            return
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                return
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(str(item.value) if isinstance(item, ast.Constant) else "*" for item in node.values)
        else:
            return
        if not (text.startswith(("scripts/", "a/scripts/", "b/scripts/")) or re.fullmatch(r"[^\s]+\.py", text)
                or text == "scripts" and isinstance(parent, (ast.BinOp, ast.Call))):
            return
        if text in {"*.py", ".py"}:
            return  # Extension/glob patterns are not named execution targets.
        expression: ast.expr = node
        while isinstance(self.parents.get(expression), (ast.BinOp, ast.JoinedStr)):
            expression = self.parents[expression]  # type: ignore[assignment]
        parent = self.parents.get(expression)
        if isinstance(parent, ast.Attribute) and isinstance(self.parents.get(parent), ast.Call):
            expression = self.parents[parent]  # type: ignore[assignment]
        elif isinstance(parent, ast.Call) and (
            self.qualified(parent.func) in {"Path", "pathlib.Path", "str"}
            or isinstance(parent.func, ast.Attribute) and parent.func.attr == "joinpath"
        ):
            expression = parent
            while isinstance(self.parents.get(expression), ast.BinOp):
                expression = self.parents[expression]  # type: ignore[assignment]
        context = self.parents.get(expression)
        while isinstance(context, (ast.Set, ast.Tuple, ast.List)):
            context = self.parents.get(context)
        if isinstance(context, ast.Compare):
            return
        if isinstance(context, ast.Call) and (
            self.qualified(context.func).startswith("re.") or
            isinstance(context.func, ast.Attribute) and context.func.attr in {"startswith", "endswith", "removeprefix", "removesuffix"}
        ):
            return  # Literal predicates/regexes classify names; they do not open files.
        if not self.parser_adapter(expression):
            self.include(expression, required=False)

    def check(self) -> None:
        if self.conflicting_names:
            raise self.fail(self.tree, f"ambiguous import bindings: {sorted(self.conflicting_names)}")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.arg) and (node.arg in self.names or self.sensitive(node.arg)):
                raise self.fail(node, "import shadowed by a parameter")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*" and node.module in {"importlib", "importlib.util", "subprocess", "runpy", "os", "sys", "builtins"}:
                        raise self.fail(node, "wildcard execution import")
                    if alias.asname and alias.asname != alias.name and self.sensitive(f"{node.module}.{alias.name}"):
                        raise self.fail(node, "aliased execution callable")
            if isinstance(node, (ast.Name, ast.Attribute)):
                parent = self.parents.get(node)
                name = self.qualified(node)
                if isinstance(parent, ast.Attribute) and not (self.sensitive(name) or name == "sys.executable"):
                    continue
                if name in {"importlib", "importlib.util", "runpy", "subprocess", "os", "sys", "builtins", "pathlib"}:
                    raise self.fail(node, "execution namespace escapes attribute access")
                if isinstance(node.ctx, ast.Store) and (self.sensitive(name) or name in {"pathlib.Path", "str"}):
                    raise self.fail(node, "execution callable reassigned")
                if name == "sys.executable":
                    if not self.parser_adapter(node):
                        if not isinstance(parent, (ast.List, ast.Tuple)) or parent.elts[0] is not node:
                            raise self.fail(node, "aliased Python executable")
                        self.command(parent)
                elif self.sensitive(name):
                    if name in {"subprocess.PIPE", "subprocess.STDOUT", "subprocess.DEVNULL"}:
                        continue  # Stream constants are not execution callables.
                    if name in {"subprocess.TimeoutExpired", "subprocess.CalledProcessError"} and (
                        isinstance(parent, ast.ExceptHandler) or isinstance(parent, ast.Tuple)
                        and isinstance(self.parents.get(parent), ast.ExceptHandler)
                    ):
                        continue  # Matching an exception does not invoke it.
                    if not isinstance(parent, ast.Call) or parent.func is not node:
                        raise self.fail(node, "aliased or escaped execution callable")
                    if not self.parser_adapter(node):
                        try:
                            self.execution(parent, name)
                        except Uncertain as exc:
                            raise self.fail(parent, str(exc)) from exc
            if isinstance(node, (ast.Constant, ast.JoinedStr)):
                self.path_input(node)


class ImportClosure:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: set[str] = {WORKFLOW}
        self.directories: set[str] = set()
        self.pending: list[str] = []
        self.sources: dict[str, ExecutionInputs] = {}
        self.modules: dict[str, str] = {}
        self.packages: dict[str, str] = {}
        for base in ("scripts", "src/trusted_router"):
            for path in (root / base).rglob("*.py"):
                relative = path.relative_to(root).as_posix()
                module = relative.removeprefix("src/").removesuffix(".py").replace("/", ".")
                self.modules[module.removesuffix(".__init__")] = relative
                parent = path.parent
                while parent != root and parent != root / "src":
                    directory = parent.relative_to(root).as_posix()
                    self.packages[directory.removeprefix("src/").replace("/", ".")] = directory
                    parent = parent.parent

    def source(self, path: str) -> ExecutionInputs:
        if path not in self.sources:
            self.sources[path] = ExecutionInputs(self, path)
        return self.sources[path]

    def add_file(self, path: str) -> None:
        if not (self.root / path).is_file():
            raise Uncertain(f"missing repository source: {path}")
        if path not in self.files:
            self.files.add(path)
            if path.endswith(".py"):
                self.pending.append(path)

    def add_directory(self, directory: str) -> None:
        self.directories.add(directory)
        package = directory.removeprefix("src/").replace("/", ".")
        if package in self.packages:
            self.module(package)
        for path in self.modules.values():
            if path.startswith(directory + "/"):
                self.add_file(path)

    def module(self, name: str, *, required: bool = False, package: str = "") -> None:
        # Some entry points put scripts/ on sys.path and import bare siblings.
        candidates = [name, "scripts." + name, package + "." + name]
        found = False
        for candidate in candidates:
            if candidate not in self.modules and candidate not in self.packages:
                continue
            found = True
            if candidate in self.modules:
                self.add_file(self.modules[candidate])
            parts = candidate.split(".")
            for end in range(1, len(parts)):
                parent = ".".join(parts[:end])
                if parent in self.modules:
                    self.add_file(self.modules[parent])
        if not found and (required or name.startswith(("trusted_router.", "scripts."))):
            raise Uncertain(f"unresolved repository import: {name}")

    def dynamic_import(self, argument: ast.expr) -> None:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            if argument.value.startswith("."):
                raise Uncertain("relative dynamic import")
            self.module(argument.value, required=True)
            return
        if (isinstance(argument, ast.JoinedStr) and argument.values
                and isinstance(argument.values[0], ast.Constant)):
            # A partial module name (providers_{slug}) includes its parent,
            # not just a coincidentally existing providers_ package.
            prefix = str(argument.values[0].value).rpartition(".")[0]
            if prefix in self.packages:
                self.add_directory(self.packages[prefix])
                return
        raise Uncertain("unresolved dynamic import")

    def follow(self) -> None:
        while self.pending:
            path = self.pending.pop()
            source = self.source(path)
            source.check()
            tree = source.tree
            package = path.removeprefix("src/").rsplit("/", 1)[0].replace("/", ".")
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.module(alias.name, package=package)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        parts = package.split(".")
                        if node.level > len(parts):
                            raise Uncertain("relative import escapes repository package")
                        module = ".".join(parts[:len(parts) - node.level + 1] + ([module] if module else []))
                    self.module(module, required=bool(node.level), package=package)
                    for alias in node.names:
                        child = module + "." + alias.name
                        if child in self.modules or child in self.packages:
                            self.module(child)
                        elif alias.name == "*" and module in self.packages:
                            self.add_directory(self.packages[module])


def analyze_repository(root: Path) -> Dependencies:
    closure = ImportClosure(root)
    entries: set[str] = set()
    steps = workflow_steps((root / WORKFLOW).read_text())
    for step in steps:
        if step.get("uses"):
            if step["uses"] not in SETUP_ACTIONS or "run" in step:
                raise Uncertain("unclassified workflow action")
            continue
        if "run" not in step:
            raise Uncertain("step without a classified action or command")
        if step.get("name") == PUBLICATION_STEP:
            source = step["run"].rstrip("\n") + "\n"
            if hashlib.sha256(source.encode()).hexdigest() != PUBLICATION_SHA256:
                raise Uncertain("publication shell changed; review its command classification")
            entries.update(re.findall(r"\bpython (scripts/[\w/]+\.py)\b", source))
            staged = source.replace("\\\n", "").split("git add ", 1)[1].splitlines()[0]
            for path in shlex.split(staged):
                if (root / path).is_dir():
                    closure.add_directory(path.rstrip("/"))
                else:
                    closure.add_file(path)
        else:
            entries.update(shell_entry_points(step["run"]))
    for entry in entries:
        if entry.endswith(".py"):
            closure.add_file(entry)
        else:
            closure.module(entry, required=True)
    closure.follow()
    return Dependencies(frozenset(entries), frozenset(closure.files), frozenset(closure.directories))


def publish_decision(changed_paths: Iterable[str]) -> Literal["rebase", "recompute"]:
    """Only positively classified, unrelated paths can reuse generation."""
    try:
        dependencies = analyze_repository(ROOT)
        for path in changed_paths:
            if not path:  # A trailing NUL (or an empty diff) is not a path.
                continue
            parts = PurePosixPath(path).parts
            if path.startswith("/") or ".." in parts or "." in path.split("/"):
                return "recompute"
            if dependencies.contains(path):
                return "recompute"
            if path.startswith("docs/") and path.endswith(".md"):
                continue
            if (path.startswith(("src/trusted_router/", "scripts/")) and path.endswith(".py")
                    and PurePosixPath(path).name not in {"conftest.py", "setup.py"}
                    and (ROOT / path).is_file() and not (ROOT / path).is_symlink()):
                continue
            return "recompute"
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"Refresh dependency analysis requires recomputation: {exc}", file=sys.stderr)
        return "recompute"
    return "rebase"


def main() -> None:
    print(publish_decision(sys.stdin.read().split("\0")))


if __name__ == "__main__":
    main()
