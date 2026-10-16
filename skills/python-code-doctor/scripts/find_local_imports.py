#!/usr/bin/env python3
"""
Detect imports that are not at the top of the module.

Imports belong at module top level. Imports buried inside functions/methods are
usually a workaround for a circular import (a design smell to fix at the source) or
plain laziness; they hide a module's real dependencies, run on every call, and make
the import graph hard to see. They should be permitted only rarely — for a genuinely
optional dependency or a deliberate lazy/heavy import — so this flags them by default.

Finds:
  - local_import       : an import inside a function/method (a deferred import)
  - import_not_at_top  : a module-level import placed after real code (PEP 8 E402)

Deliberately NOT flagged (the legitimate deferred-import patterns):
  - imports guarded by `if TYPE_CHECKING:` (type-only imports)
  - imports inside a `try: ... except ImportError/ModuleNotFoundError:` (optional dep)

Reported as candidates, not findings (deferral is the usual reason, but only
the author knows):
  - a deferred import of a known slow-to-import package (torch, transformers,
    ...), where import latency is the likely point
  - an import inside a CLI command (`@app.command()`, `@app.callback()`,
    `@click.command`, `@click.group`), which keeps `--help` and the other
    subcommands from paying for one subcommand's dependencies
  - a module-level import after a path bootstrap — statements that modify
    `sys.path` or `os.environ` (and the `X = Path(__file__)...` they are
    computed from). The import cannot move above the code that makes it
    importable; pycodestyle's E402 exempts the same statements.

Reported as candidates, because the file itself says why:
  - a module-level import after other code carrying `# noqa: E402` *with a
    stated reason* (`# noqa: E402 - deliberate late import`): the repo gates on
    the rule and adjudicated this line. A bare `# noqa: E402` states nothing.
  - a function-level import of a module the module docstring names in a
    sentence about importing ("before anything of uvicorn is imported"): a
    documented deliberate import order, such as a process entry point that must
    answer before it loads its heavy dependencies.
"""

import ast
import functools
import json
import warnings
import re
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import is_cli_command, project_root, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # A scored defect by default; "candidate" for a lead the downstream merge
    # must not score. A candidate carries no fix and names, in also_caused_by,
    # the ways a healthy codebase produces the same observation.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Top-level packages whose import alone takes on the order of a second: they
# load compiled extensions (CUDA/BLAS kernels, a C++ runtime) or eagerly import
# large submodule trees. Deferring one is a measured startup choice far more
# often than a cycle workaround. Kept short on purpose — numpy, PIL and the like
# import in tens of milliseconds and get no benefit of the doubt.
_HEAVY_MODULES = frozenset({
    "torch", "torchvision", "torchaudio", "tensorflow", "jax", "transformers",
    "diffusers", "accelerate", "ultralytics", "xformers", "triton", "onnxruntime",
    "numba", "cv2", "scipy", "sklearn", "pandas", "matplotlib", "spacy", "nltk",
    "safetensors",  # its .torch half imports torch, and it is deferred alongside it
})

# How many first-party modules deep a load is followed before giving up.
_MAX_LOAD_DEPTH = 6

_HEAVY_REASONS = (
    "deliberate deferral of a slow-to-import module to keep startup fast: a CLI's "
    "--help, or a code path that never needs it, should not pay seconds to import it",
    "an optional heavy dependency: only the code path that uses it imports it, so the "
    "rest of the program runs without it installed",
)

_CLI_REASONS = (
    "deferring a subcommand's dependencies keeps --help and the other subcommands fast: "
    "only the command that runs pays for what it imports",
    "the subcommand's dependency is an optional extra the other commands must work without",
)

def _package(path: Path) -> tuple[list, Path]:
    """A module file's package parts, and the directory its top-level package sits in."""
    parts, directory = [], path.parent
    while (directory / "__init__.py").exists() and directory.parent != directory:
        parts.insert(0, directory.name)
        directory = directory.parent
    return parts, directory


def _module_file(dotted: str, bases) -> Path | None:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return None
    for base in bases:
        module = base.joinpath(*parts[:-1], parts[-1] + ".py")
        if module.is_file():
            return module
        package = base.joinpath(*parts, "__init__.py")
        if package.is_file():
            return package
    return None


def _loaded_modules(node, path: Path) -> list:
    """Dotted names one import statement in ``path`` loads: the module and, for a
    `from X import y`, X.y in case y is a submodule. Relative names are resolved
    against the file's own package."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        parts, _ = _package(path)
        # _package already names the containing package, including for __init__.py.
        # Each dot after the first therefore removes exactly one package component.
        base = parts[:len(parts) - node.level + 1] if node.level <= len(parts) else []
        module = ".".join([*base, *(node.module.split(".") if node.module else [])])
    else:
        module = node.module or ""
    return [module, *(f"{module}.{alias.name}" if module else alias.name for alias in node.names)]


def _module_level_imports(tree):
    """Import statements that run when the module is loaded: top-level ones and
    those under module-level if/try, but not under `if TYPE_CHECKING:`."""
    stack = list(tree.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            if not _is_typechecking_test(node.test):
                stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.Try):
            stack.extend([*node.body, *node.orelse, *node.finalbody])
            for handler in node.handlers:
                stack.extend(handler.body)


def _bases(path: Path) -> tuple:
    """Where an absolute first-party module name is looked up from ``path``."""
    _, package_base = _package(path)
    root = project_root(path)
    return tuple(dict.fromkeys([package_base, root, root / "src"]))


def _heavy_chain(node, path: Path, seen: frozenset = frozenset()) -> str | None:
    """How one import statement in ``path`` reaches a heavy package, or None."""
    for dotted in _loaded_modules(node, path):
        if dotted.split(".")[0] in _HEAVY_MODULES:
            return dotted.split(".")[0]
        target = _module_file(dotted, _bases(path))
        if target is not None:
            chain = _load_cost(str(target.resolve()), seen)
            if chain:
                return f"{dotted} → {chain}"
    return None


@functools.lru_cache(maxsize=None)
def _load_cost(path_str: str, seen: frozenset = frozenset()) -> str | None:
    """The chain by which loading this module file imports a heavy package, or None.

    Followed through first-party modules to _MAX_LOAD_DEPTH; a cycle stops.
    """
    if path_str in seen or len(seen) >= _MAX_LOAD_DEPTH:
        return None
    path = Path(path_str)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in _module_level_imports(tree):
        chain = _heavy_chain(node, path, seen | {path_str})
        if chain:
            return chain
    return None


def _deferred_chain(node, path: Path) -> str | None:
    """How a deferred import statement reaches a heavy package, or None. Every
    name an `import a, b` loads must be heavy, as for the package rule; a
    `from X import y` is one load."""
    if isinstance(node, ast.Import) and len(node.names) > 1:
        chains = [_heavy_chain(ast.Import(names=[alias]), path) for alias in node.names]
        return "; ".join(chains) if all(chains) else None
    return _heavy_chain(node, path)


def _command_helpers(tree) -> set:
    """Module-level private functions that only CLI commands (or other such
    helpers) call: the pieces a command was split into.

    A helper qualifies when it is referenced at least once and every reference
    in the module is a call made from a command or from another qualifying
    helper. A function nothing calls, one also called from elsewhere or used as
    a value, and a public one (another module may call it, which this file
    cannot see) do not.
    """
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    commands = {name for name, node in functions.items() if is_cli_command(node)}
    references: dict[str, list] = defaultdict(list)  # helper -> [(caller or None, is_call)]
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def collect(node, caller):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Name) and child.id in functions:
                references[child.id].append((caller, id(child) in calls))
            collect(child, caller)

    for node in tree.body:
        collect(node, node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None)

    helpers: set = set()
    changed = True
    while changed:
        changed = False
        for name in functions:
            if name in commands or name in helpers or not name.startswith("_"):
                continue
            refs = [r for r in references.get(name, []) if r[0] != name]  # recursion is not a caller
            if refs and all(is_call and caller in commands | helpers for caller, is_call in refs):
                helpers.add(name)
                changed = True
    return helpers


def _imported_packages(node) -> list:
    """Top-level package each name of an import statement loads ('' if relative)."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if node.level:
        return [""]
    return [(node.module or "").split(".")[0]]
# A noqa comment whose code list is followed by a separator and a reason. A
# bare suppression states no reason and proves nothing.
_REASONED_NOQA = re.compile(
    r"#\s*noqa:\s*(?P<codes>[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)\s*(?:--|—|-|:)\s*(?P<reason>[^\s\-—:].*)",
    re.IGNORECASE,
)
# What a path bootstrap touches before the imports it makes possible.
_BOOTSTRAP_MARKERS = ("sys.path", "os.environ", "os.putenv", "site.addsitedir", "importorskip")
_IMPORT_WORD = re.compile(r"\bimport(?:s|ed|ing)?\b", re.IGNORECASE)


def _reasoned_noqa(lines: list[str], node: ast.stmt, code: str) -> str | None:
    """The reason a `# noqa: <code> - reason` on the statement's lines gives, if any."""
    for number in range(node.lineno, (node.end_lineno or node.lineno) + 1):
        if not 0 < number <= len(lines):
            continue
        match = _REASONED_NOQA.search(lines[number - 1])
        if match and code in {c.strip().upper() for c in match.group("codes").split(",")}:
            return match.group("reason").strip()
    return None


def _is_bootstrap(stmt) -> bool:
    """A statement that makes later imports importable: it edits sys.path or
    the environment, computes the path it puts there from `__file__`, or is an
    `if` holding nothing else."""
    if isinstance(stmt, ast.If):
        return all(_is_bootstrap(s) for s in stmt.body + stmt.orelse)
    if not isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return False
    source = ast.unparse(stmt)
    if any(marker in source for marker in _BOOTSTRAP_MARKERS):
        return True
    return isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None \
        and "__file__" in ast.unparse(stmt.value)


# How far apart, in words, a module's name and an "import" may sit and still be
# one statement about importing it ("before anything of uvicorn or grimoire is
# imported"), rather than a sentence that mentions both in passing.
_DEFERRAL_WINDOW = 6


def _docstring_deferral(tree, top: str) -> str | None:
    """The sentence of the module docstring that says something about
    importing ``top``, if there is one."""
    doc = ast.get_docstring(tree) or ""
    for sentence in re.split(r"(?<=[.!?])\s+", doc):
        words = [w.rstrip(".") for w in re.findall(r"[A-Za-z_][\w.]*", sentence)]
        named = [i for i, w in enumerate(words) if w.split(".")[0] == top]
        importing = [i for i, w in enumerate(words) if _IMPORT_WORD.fullmatch(w)]
        if any(abs(i - j) <= _DEFERRAL_WINDOW for i in named for j in importing):
            return " ".join(sentence.split())
    return None


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _is_typechecking_test(test) -> bool:
    """True for `if TYPE_CHECKING:` (as a Name or attribute like typing.TYPE_CHECKING)."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _handler_catches_importerror(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True  # bare except guarding an import — treat as optional-dep pattern
    names = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return any(n in {"ImportError", "ModuleNotFoundError"} for n in names)


def _collect_guarded(tree) -> set:
    """Import nodes that sit in a legitimate deferred-import context (by id())."""
    guarded = set()
    for node in ast.walk(tree):
        # if TYPE_CHECKING: <imports>
        if isinstance(node, ast.If) and _is_typechecking_test(node.test):
            for n in ast.walk(node):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(n))
        # try: <imports> except ImportError: ...
        if isinstance(node, ast.Try) and any(_handler_catches_importerror(h) for h in node.handlers):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(n))
    return guarded


def _is_dunder_assign(stmt) -> bool:
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    for t in targets:
        if isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__"):
            return True
    return False


def _only_imports(stmt) -> bool:
    """True if an If/Try statement contains only imports (a top-of-file guard block)."""
    if not isinstance(stmt, (ast.If, ast.Try)):
        return False
    return all(isinstance(n, (ast.Import, ast.ImportFrom, ast.Pass))
               for n in ast.walk(stmt)
               if isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr, ast.Assign,
                                 ast.AnnAssign, ast.Pass))) and any(
               isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(stmt))


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev, kind="finding", also_caused_by=(), reasons=()):
        if reasons:
            kind, also_caused_by, sug = "candidate", tuple(reasons), ""
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line),
                                kind, tuple(also_caused_by)))

    guarded = _collect_guarded(tree)
    helpers = _command_helpers(tree)

    # Case 1: imports inside functions/methods (deferred imports).
    def walk(node, in_function, in_command):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if in_function and id(child) not in guarded:
                    what = ("import " + ", ".join(a.name for a in child.names)
                            if isinstance(child, ast.Import)
                            else "from " + ("." * getattr(child, "level", 0)) + (child.module or "") + " import ...")
                    heavy = all(pkg in _HEAVY_MODULES for pkg in _imported_packages(child))
                    chain = None
                    if not heavy:  # first-party: is it slow because of what *it* loads?
                        chain = _deferred_chain(child, Path(filename))
                        heavy = chain is not None
                    documented = next(filter(None, (_docstring_deferral(tree, top)
                                       for top in _imported_packages(child))), None)
                    if heavy or in_command or documented:
                        reasons = ((f"The module docstring explains deliberate deferral: {documented}",) if documented else ()) + (_HEAVY_REASONS if heavy else ()) + (_CLI_REASONS if in_command else ())
                        where = "a CLI command" if in_command else "a function"
                        loads = f", which loads {chain} at import time" if chain else ""
                        add(child.lineno, "local_import",
                            f"Import deferred into {where} ('{what}'){loads} — usually to keep "
                            "startup fast; confirm it is not a circular-import workaround",
                            "", "low", kind="candidate", also_caused_by=reasons)
                    else:
                        add(child.lineno, "local_import",
                            f"Import inside a function ('{what}') — a deferred import, usually a circular-import workaround or laziness",
                            "Move it to the top of the module. If it is breaking a real import cycle, fix the cycle "
                            "instead (extract what both modules need into a module they can both import). Only a "
                            "dependency the program must run without belongs in try/except ImportError, at module top.",
                            "medium")
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                commandlike = is_cli_command(child) or (node is tree and child.name in helpers)
                walk(child, True, in_command or commandlike)
            elif isinstance(child, ast.Lambda):
                walk(child, True, in_command)
            else:
                walk(child, in_function, in_command)

    walk(tree, False, False)

    # Case 2: module-level import placed after real code (E402).
    seen_code = False
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if seen_code and id(stmt) not in guarded:
                reason = _reasoned_noqa(lines, stmt, "E402")
                add(stmt.lineno, "import_not_at_top",
                    "Module-level import placed after other code instead of at the top of the file",
                    "Move all imports to the top of the module (after the docstring and __future__ imports).",
                    "low",
                    reasons=[f"the line suppresses E402 with a stated reason: {reason}"] if reason else ())
            continue
        # statements allowed above imports without counting as "real code"
        if i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue  # module docstring
        if _is_dunder_assign(stmt) or _only_imports(stmt) or _is_bootstrap(stmt):
            continue
        seen_code = True

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never reads an absent key as a
    classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect imports not at the top of the module")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No non-top-level imports found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        # A candidate is a lead, not a defect, so it is counted apart.
        leads = sum(1 for i in all_issues if i.kind == "candidate")
        print(f"Found {len(all_issues) - leads} import-placement finding(s), "
              f"{leads} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
