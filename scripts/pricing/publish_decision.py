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


class ImportClosure:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: set[str] = {WORKFLOW}
        self.directories: set[str] = set()
        self.pending: list[str] = []
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
            self.module(argument.value)
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
            tree = ast.parse((self.root / path).read_text(), filename=path)
            package = path.removeprefix("src/").rsplit("/", 1)[0].replace("/", ".")
            dynamic_names = {"__import__", "import_module"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                    dynamic_names.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
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
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    )
                    if name in dynamic_names:
                        if not node.args:
                            raise Uncertain("dynamic import without positional module")
                        self.dynamic_import(node.args[0])
                    elif name in {"exec", "eval", "compile"} and isinstance(node.func, ast.Name):
                        # Generated parsers/embedded Python can refer to any
                        # sibling: include the entire executing package.
                        self.add_directory(path.rsplit("/", 1)[0])
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Python launched by subprocess, e.g. model_change_snapshot.
                    if re.fullmatch(r"(?:scripts|src/trusted_router)/[\w/]+\.py", node.value):
                        self.add_file(node.value)


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
