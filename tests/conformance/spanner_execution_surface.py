"""Static repository execution edges for the migration carrier guard.

Never imports target code or executes commands. Unknown path expansion is an error;
stdlib/installed commands and modules are not repository files. Shell snippets in
workflow run blocks, build args, Docker directives and Python subprocess calls use
the same resolver as shell scripts.
"""
from __future__ import annotations

import ast
import re
import shlex
import sys
from pathlib import Path

import yaml


def module_file(module: str, root: Path) -> Path | None:
    for base in (root / "src", root, *sorted(root.glob("experiments/*/src")), *sorted(root.glob("experiments/*"))):
        target = base.joinpath(*module.split("."))
        for candidate in (target.with_suffix(".py"), target / "__main__.py", target / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def package_directory(path: Path, root: Path) -> Path | None:
    if path.suffix != ".py":
        return None
    parent = path.parent
    if not (parent / "__init__.py").is_file():
        # Repository namespace packages (not arbitrary sibling data directories).
        relative = path.relative_to(root)
        parts = relative.parts[1:] if relative.parts[0] == "src" else relative.parts
        if len(parts) > 1 and all(part.isidentifier() for part in parts[:-1]):
            return root.joinpath(*relative.parts[:2 if relative.parts[0] == "src" else 1])
        return None
    while parent.parent != root and (parent.parent / "__init__.py").is_file():
        parent = parent.parent
    return parent


def imported_modules(path: Path, root: Path) -> set[Path]:
    result = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                target = base.joinpath(*(node.module or "").split("."))
                if target.with_suffix(".py").is_file():
                    result.add(target.with_suffix(".py"))
                elif (target / "__init__.py").is_file():
                    result.add(target / "__init__.py")
                for alias in node.names:
                    if (target / alias.name).with_suffix(".py").is_file():
                        result.add((target / alias.name).with_suffix(".py"))
            elif node.module:
                modules = [node.module, *[node.module + "." + alias.name for alias in node.names]]
        for module in modules:
            target = module_file(module, root)
            if target is not None:
                result.add(target)
    return result


def execution_targets(path: Path, root: Path) -> set[Path]:
    from tests.conformance.spanner_schema_source import shell_argument, shell_tokens

    source = path.read_text(errors="replace")
    targets: set[Path] = set()
    bindings: dict[str, str] = {}
    copies: dict[str, str] = {}
    arrays: dict[str, list[list[str]]] = {}

    def fail(line: int, target: str) -> None:
        raise AssertionError(f"{path}:{line}: unresolved execution target: {target}; "
                             "use a statically resolvable repository file")

    def expand(value: str) -> str:
        value = shell_argument(value)
        # Recognize the conventional dirname of this very file, not arbitrary
        # command substitution. Variables are resolved only from static assignments.
        value = re.sub(r'\$\(dirname\s+["\']?\$\{?BASH_SOURCE\[0\]\}?["\']?\)',
                       str(path.parent), value)
        value = re.sub(r"\$\(command -v (python[0-9.]*)\)", r"\1", value)
        for key, replacement in bindings.items():
            value = value.replace("${" + key + "}", replacement)
            value = re.sub(r"\$" + re.escape(key) + r"\b", lambda _, replacement=replacement: replacement, value)
        return value

    # A later/different assignment must not leave a stale static path binding.
    definitions: dict[str, list[str]] = {}
    for match in re.finditer(r'(?m)^\s*(?:export\s+)?(\w+)=(.+)$', source):
        definitions.setdefault(match[1], []).append(match[2].strip())
    for _ in range(len(definitions) + 1):
        changed = False
        for key, values in definitions.items():
            resolved = set()
            for raw in values:
                value = expand(raw)
                cd = re.fullmatch(r'\$\(cd\s+"?([^"$]+)"?\s*&&\s*pwd\)', value)
                if cd:
                    value = str((root / cd[1]).resolve())
                resolved.add(value)
            if resolved <= {"true", "false"}:
                resolved = {"true"}  # Both alternatives are shell builtins.
            if len(resolved) == 1 and not re.search(r'[\s$`]', next(iter(resolved))):
                value = resolved.pop()
                changed |= bindings.get(key) != value
                bindings[key] = value
            else:
                bindings.pop(key, None)
        if not changed:
            break

    for match in re.finditer(r"(?m)^\s*(\w+)=\(([^\n]*)\)\s*$", source):
        arrays.setdefault(match[1], []).append(shlex.split(match[2]))

    def resolve(value: str, line: int, *, module: bool = False) -> None:
        value = expand(value)
        if re.search(r'[$`*{}]', value):
            fail(line, value)
        if module:
            target = module_file(value, root)
            if target is not None:
                targets.add(target)
            elif value.split(".")[0] not in sys.stdlib_module_names:
                # Installed modules have no repository source. A missing local
                # module must not silently become an installed-module assumption.
                local = any((base / value.split(".")[0]).exists() for base in (root, root / "src"))
                installed = any((Path(base) / value.replace(".", "/")).with_suffix(".py").is_file()
                                or (Path(base) / value.replace(".", "/") / "__init__.py").is_file()
                                for base in sys.path if base)
                if local or not installed:
                    fail(line, value)
            return
        value = copies.get(value, value)
        candidates = [root / value, path.parent / value]
        for directory in re.findall(r"(?m)^\s*(?:-\s*)?working-directory:\s*['\"]?([\w./-]+)", source):
            candidates.append(root / directory / value)
        # Workflows may have a working-directory; resolve an explicit relative
        # path against repository subdirectories only when it is unambiguous.
        if value.startswith("../"):
            suffix = value.lstrip("./")
            candidates.append(root / suffix)
        found = {candidate.resolve() for candidate in candidates
                 if candidate.resolve().is_relative_to(root.resolve()) and candidate.is_file()}
        if len(found) == 1:
            targets.update(found)
            return
        fail(line, value)

    def command(words: list[str], line: int) -> None:
        if not words or words[0].startswith((">", "<", "2>")):
            return
        # Wrappers do not hide their interpreter or a directly executed path.
        while words and (words[0] in {"exec", "sudo", "env", "command", "!", "then", "do", "if", "elif"}
                         or re.match(r"\w+(?:\[[^]]+\])?=", words[0])):
            words = words[1:]
        if not words:
            return
        array = re.fullmatch(r"\$\{(\w+)\[@\]\}", words[0])
        if array:
            variants = arrays.get(array[1], [])
            if not variants:
                fail(line, words[0])
            for variant in variants:
                command([*variant, *words[1:]], line)
            return
        # A forwarding entrypoint adds no target beyond the caller's argv.
        if words[0] == "$@":
            return
        if words[0].startswith("$"):
            resolved = expand(words[0])
            if resolved.startswith("$"):
                fail(line, words[0])
            words[0] = resolved
        if words[0] in copies:
            resolve(words[0], line)
            return
        program = "." if words[0] == "." else Path(words[0]).name
        if program == "uv" and "run" in words:
            words = words[words.index("run") + 1:]
            while words and words[0].startswith("-"):
                option = words.pop(0)
                if option in {"--with", "--python", "--directory", "--project", "--package", "--group"} and words:
                    words.pop(0)
            command(words, line)
            return
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program) or program in {"bash", "sh", "node", "source", "."}:
            args = words[1:]
            while args:
                arg = args.pop(0)
                if arg == "-m":
                    if not args:
                        fail(line, "missing module")
                    resolve(args[0], line, module=True)
                    return
                if arg in {"-c", "-e", "--eval"} or (program in {"bash", "sh"} and arg.startswith("-") and "c" in arg):
                    if args and program in {"bash", "sh"}:
                        shell(args[0], line - 1)
                    return
                if arg == "-" or arg.startswith(("<<", ">", "2>")):
                    return  # stdin/heredoc: its raw carrier text is in this file
                if arg in {"-W", "-X", "--check-hash-based-pycs"} and args:
                    args.pop(0)
                    continue
                if arg.startswith("-"):
                    continue
                resolve(arg, line)
                return
            return  # interactive / stdin interpreter
        if (("/" in words[0] and not words[0].startswith(("/usr/", "/bin/", "/sbin/")))
                or (root / words[0]).is_file()):
            resolve(words[0], line)

    def shell(text: str, offset: int = 0) -> None:
        # Use the offset-preserving shell lexer, including command substitutions.
        physical = text
        text = text.replace("\\\n", "  ")
        text = re.sub(r"\(\([^()]*\)\)", lambda m: re.sub(r"[^\n]", " ", m[0]), text)
        try:
            tokens = shell_tokens(re.sub(r"(?<=\d)>&(?=\d)", "> ", text), [])
        except AssertionError as exc:
            raise AssertionError(f"{path}:{offset + 1}: cannot resolve execution syntax: {text[:200]!r}: {exc}") from exc
        words: list[str] = []
        array_depth = 0
        conditional = False
        line = offset + 1
        end = -1
        for token in tokens:
            word = shell_argument(token[0])
            if token.start() < end:
                # Nested $(...) tokens also have execution positions.
                continue
            end = token.end()
            if word == "[[":
                conditional = True
                words = []
            if conditional:
                if word == "]]":
                    conditional = False
                continue
            if word == "(" and (array_depth or (words and words[-1].endswith("="))):
                array_depth += 1
                words = []
                continue
            if array_depth:
                if word == ")":
                    array_depth -= 1
                continue
            if word in {"\n", ";", "|", "&", "(", ")", "{", "}"}:
                if word != ")" or len(words) > 1 or (words and words[0].startswith(("./", "../"))):
                    command(words, line)
                words = []
            else:
                if not words:
                    line = offset + physical.count("\n", 0, token.start()) + 1
                words.append(word)
        command(words, line)
        # Match each substitution separately (a word may contain several).
        outer_end = -1
        for token in tokens:
            if token.start() < outer_end or "$(" not in token[0]:
                continue
            openings = {m.start() + token.start(): 1 for m in re.finditer(r"\$\(", token[0])}
            events = dict(openings)
            for part in tokens:
                if token.start() <= part.start() < token.end() and part[0] in {"(", ")"}:
                    events[part.start()] = 1 if part[0] == "(" else -1
            stack: list[int] = []
            for position, delta in sorted(events.items()):
                if delta == 1:
                    stack.append(position)
                elif stack:
                    start = stack.pop()
                    if not stack and start in openings and not text.startswith("$((", start):
                        shell(physical[start + 2:position], offset + physical.count("\n", 0, start))
            outer_end = token.end()

    if path.suffix == ".py":
        tree = ast.parse(source, filename=str(path))
        literals: dict[str, str] = {}
        for assignment in ast.walk(tree):
            if isinstance(assignment, ast.Assign):
                value = assignment.value
                if (isinstance(value, ast.Call) and ast.unparse(value.func) == "shutil.which"
                        and value.args):
                    value = value.args[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for target in assignment.targets:
                        if isinstance(target, ast.Name):
                            literals[target.id] = value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name not in {"subprocess.run", "subprocess.Popen", "subprocess.check_call",
                            "subprocess.check_output", "subprocess.call", "os.system", "os.execv", "os.execvp"}:
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                shell(arg.value, node.lineno - 1)
            elif isinstance(arg, (ast.List, ast.Tuple)):
                words = []
                for item in arg.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        words.append(item.value)
                    elif isinstance(item, ast.Name) and item.id in literals:
                        words.append(literals[item.id])
                    elif isinstance(item, ast.Attribute) and ast.unparse(item) == "sys.executable":
                        words.append("python")
                    else:
                        words.append("$DYNAMIC")
                command(words, node.lineno)
    elif path.suffix in {".js", ".mjs", ".cjs"}:
        for match in re.finditer(
            r"\b(?:execFileSync|execFile|spawnSync|spawn)\(\s*(['\"])(.*?)\1\s*,\s*(\[[^]]*\])", source,
        ):
            line = source.count("\n", 0, match.start()) + 1
            try:
                arguments = ast.literal_eval(match[3])
            except (ValueError, SyntaxError):
                fail(line, match[3])
            command([match[2], *arguments], line)
        for match in re.finditer(r"\b(?:execSync|exec)\(\s*(['\"])(.*?)\1", source):
            shell(match[2], source.count("\n", 0, match.start()))
    elif path.suffix in {".yaml", ".yml"} or path.parent == root / ".github/workflows":
        def visit(node):
            if isinstance(node, yaml.MappingNode):
                mapping = {key.value: value for key, value in node.value}
                entrypoint = mapping.get("entrypoint")
                arguments = mapping.get("args")
                if isinstance(entrypoint, yaml.ScalarNode) and isinstance(arguments, yaml.SequenceNode):
                    command([entrypoint.value, *[child.value for child in arguments.value]],
                            entrypoint.start_mark.line + 1)
                for key, value in node.value:
                    if key.value in {"run", "command"} and isinstance(value, yaml.ScalarNode):
                        shell(value.value, value.start_mark.line + (value.style in {"|", ">"}))
                    elif key.value == "args" and isinstance(value, yaml.SequenceNode):
                        words = [child.value for child in value.value if isinstance(child, yaml.ScalarNode)]
                        command(words, value.start_mark.line + 1)
                        for child in value.value:
                            if isinstance(child, yaml.ScalarNode) and "\n" in child.value:
                                shell(child.value, child.start_mark.line + 1)
                    else:
                        visit(value)
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    visit(child)
        visit(yaml.compose(source))
    elif path.name.startswith("Dockerfile"):
        for match in re.finditer(r"(?m)^COPY\s+(\S+)\s+(\S+)\s*$", source):
            copies[match[2]] = match[1]
        for match in re.finditer(r"(?m)^(?:RUN|CMD|ENTRYPOINT)\s+((?:[^\n]*\\\n)*[^\n]*)", source):
            text = match[1]
            line = source.count("\n", 0, match.start()) + 1
            if text.startswith("["):
                command(ast.literal_eval(text), line)
            else:
                shell(text, line - 1)
    elif path.suffix == ".tf":
        # Terraform command strings and heredocs are shell execution locations.
        for match in re.finditer(r'\bcommand\s*=\s*"((?:\\.|[^"\\])*)"', source):
            shell(match[1].replace(r'\"', '"'), source.count("\n", 0, match.start()))
        for match in re.finditer(r"\bcommand\s*=\s*<<-?(\w+)\n(.*?)^\s*\1\s*$", source, re.M | re.S):
            shell(match[2], source.count("\n", 0, match.start(2)))
    else:
        shell(source)
    return targets
