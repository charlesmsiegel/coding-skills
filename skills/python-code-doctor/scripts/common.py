#!/usr/bin/env python3
"""Shared plumbing for the detector scripts.

Every detector needs the same four things: a way to enumerate Python files,
the severity icons, a console that can print them, and one policy for what to
do when a file will not parse or a detector crashes. These were copy-pasted
per script; this module is the single copy.
"""

import ast
import contextlib
import functools
import io
import os
import re
import sys
import tokenize
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Severity → icon, in report order.
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Directory names that are never the user's own code. Matched against path
# segments *below* the scanned root, so a repo that happens to live inside a
# directory with one of these names is still scanned.
EXCLUDE_DIRS = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".git", ".hg",
    ".tox", ".nox", "build", "dist", ".eggs", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def _decorator_target(dec):
    """The callable a decorator names: `x` for `@x`, and for `@x(...)` too."""
    return dec.func if isinstance(dec, ast.Call) else dec


def is_cli_command(func) -> bool:
    """Whether `func` is a typer/click command: `@app.command()`, `@app.callback()`,
    `@click.command` or `@click.group`.

    Such a function's signature is the command line: one parameter per option,
    and its body runs only when that command is invoked.
    """
    for dec in func.decorator_list:
        target = _decorator_target(dec)
        if not isinstance(target, ast.Attribute):
            continue
        if target.attr in ("command", "callback"):
            return True
        if target.attr in ("command", "group") and isinstance(target.value, ast.Name) and target.value.id == "click":
            return True
    return False


def registers_config(func) -> bool:
    """An `__init__` under diffusers' `@register_to_config`.

    ConfigMixin serialises the model config from exactly those flat keyword
    arguments, so the usual fix for a long signature — group them into a
    dataclass — breaks from_pretrained/save_pretrained.
    """
    if func.name != "__init__":
        return False
    for dec in func.decorator_list:
        target = _decorator_target(dec)
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        if name == "register_to_config":
            return True
    return False


# Framework hooks whose parameter list the framework fixes: the method name,
# the package whose base class declares it, and its parameters after self/cls.
# Deliberately short. An nn.Module's forward() is also an override, but its
# parameters are the author's own design, so it is not here.
FRAMEWORK_HOOKS = (
    ("settings_customise_sources", "pydantic_settings",
     ("settings_cls", "init_settings", "env_settings", "dotenv_settings", "file_secret_settings")),
    ("__get_pydantic_core_schema__", "pydantic", ("source_type", "handler")),
    ("emit", "logging", ("record",)),            # logging.Handler
    ("default", "json", ("o",)),                 # json.JSONEncoder
    ("__call__", "argparse", ("parser", "namespace", "values", "option_string")),  # argparse.Action
)


def framework_hook_overrides(tree) -> frozenset:
    """id() of every method in ``tree`` that overrides a FRAMEWORK_HOOKS entry.

    Narrow on purpose, all three must hold: the method's class has a base
    imported from the hook's package, the name is the hook's, and the parameter
    list after self/cls is exactly the framework's.
    """
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module.split(".")[0]
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        packages = set()
        for base in node.bases:
            root = base.value if isinstance(base, ast.Subscript) else base
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in imports:
                packages.add(imports[root.id])
        if not packages:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = tuple(a.arg for a in [*item.args.posonlyargs, *item.args.args,
                                           *item.args.kwonlyargs])[1:]
            if any(item.name == name and package in packages and params == expected
                   for name, package, expected in FRAMEWORK_HOOKS):
                found.add(id(item))
    return frozenset(found)


def signature_is_a_contract(func, hooks: frozenset = frozenset()) -> bool:
    """Whether something other than `func` dictates its parameter list, so its length is not a design choice.

    A `@register_to_config` `__init__` (the flat kwargs are the serialised
    config), a CLI command (each parameter is one of its flags), and a method
    in ``hooks``: an override of a framework hook (framework_hook_overrides())
    or, in a test file, a copy of a production signature (copied_signatures()).
    The parameter-count detectors exempt these; nothing else about them is
    exempt.
    """
    return registers_config(func) or is_cli_command(func) or id(func) in hooks


# The signature size a function may reach before analyze_complexity reports it.
PARAMETER_LIMIT = 5


def count_parameters(func) -> int:
    """Every parameter a caller can bind, as written: positional-only,
    positional (`self` and `cls` included), keyword-only, `*args`, `**kwargs`."""
    args = func.args
    return (len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
            + (args.vararg is not None) + (args.kwarg is not None))


def parameter_severity(count: int, limit: int = PARAMETER_LIMIT) -> str:
    """How bad a signature of ``count`` parameters is: low past the limit,
    medium from half again over it, high from double.

    analyze_complexity's parameter_count and find_code_smells'
    long_parameter_list both grade with this, so one signature cannot be a
    medium in one report and a low in the other.
    """
    ratio = count / limit
    return "high" if ratio >= 2.0 else ("medium" if ratio >= 1.5 else "low")


def _parameter_names(func) -> frozenset:
    """The parameters a caller can pass by position or keyword, self and cls aside."""
    args = func.args
    return frozenset(a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]) - {"self", "cls"}


@functools.lru_cache(maxsize=None)
def _production_signatures(root: Path) -> tuple:
    """The parameter names of every function and method in ``root``'s non-test code."""
    signatures = set()
    for path in _python_files(root):  # walking the project must not move the analysis root
        if is_test_file(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        signatures.update(_parameter_names(node) for node in ast.walk(tree)
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    signatures.discard(frozenset())
    return tuple(signatures)


def _copies_production_signature(func: ast.FunctionDef | ast.AsyncFunctionDef, filepath: Path) -> bool:
    """Whether ``func`` takes only parameters that one function or method in
    the project's own code also takes."""
    names = _parameter_names(func)
    return bool(names) and any(names <= real for real in _production_signatures(project_root(Path(filepath))))


def copied_signatures(tree: ast.AST, filepath: Path, limit: int = PARAMETER_LIMIT) -> frozenset:
    """id() of every test double in a test file, over ``limit`` parameters, that
    copies a production signature.

    A test double is a method of a class there or a function defined inside
    another: a fake pipeline's `__call__`, a stand-in transformer's `forward`,
    a closure handed to the code under test as a hook. It copies a production
    signature when every parameter it names is a parameter of one function or
    method in the project's own code. It takes what the real one takes because
    the code under test calls it that way, and the parameter names are what is
    copied, not the function's own name, since a callback can be called
    anything. Such a signature belongs to the interface it copies and is judged
    once, where that interface is defined. A module-level function in a test
    file is a helper the tests call, so it is judged on its own even when its
    parameters match.

    Only definitions over ``limit`` are looked up, so a run with none never
    reads the rest of the project.
    """
    if not is_test_file(Path(filepath)):
        return frozenset()
    nested = {id(inner): inner for node in ast.walk(tree)
              if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
              for inner in ast.walk(node)
              if inner is not node and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return frozenset(key for key, func in nested.items()
                     if count_parameters(func) > limit and _copies_production_signature(func, filepath))


# One defect that two detectors report under different names: alias -> canonical.
# A too-long signature is analyze_complexity's parameter_count and
# find_code_smells' long_parameter_list; a report shows it once.
FINDING_ALIASES = {"long_parameter_list": "parameter_count"}
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _record_type(record: dict):
    return next((record[key] for key in ("smell_type", "issue_type", "pattern_type", "type")
                 if record.get(key)), None)


def drop_aliased(records: list) -> list:
    """``records`` without each alias whose canonical twin is at the same file and line.

    The twin keeps the higher of the two severities, so dropping a duplicate
    never lowers what the report says about the code. An alias with no twin —
    its detector ran and the canonical one did not — is kept.
    """
    canonical = {(r.get("file"), r.get("line"), _record_type(r)): r
                 for r in records if isinstance(r, dict)}
    kept = []
    for record in records:
        target = FINDING_ALIASES.get(_record_type(record)) if isinstance(record, dict) else None
        twin = canonical.get((record.get("file"), record.get("line"), target)) if target else None
        if twin is None:
            kept.append(record)
        elif _SEVERITY_RANK.get(record.get("severity"), 0) > _SEVERITY_RANK.get(twin.get("severity"), 0):
            twin["severity"] = record["severity"]
    return kept


def configure_output() -> None:
    """Keep emoji output from crashing narrow console encodings.

    On Windows, stdout often defaults to cp1252 with strict error handling,
    so the first severity icon raises UnicodeEncodeError. Downgrade
    unencodable characters instead of dying.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A detached or closed stream has nothing to configure.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


def is_test_file(filepath: Path) -> bool:
    """A test module or test infrastructure: test_*.py, *_test.py, conftest.py,
    or anything under a tests/ (or test/) directory *of its project*.

    Detectors that judge design use it to stand down where a test's shape is set
    by what it tests: a literal there is an expected value, a fake mirrors the
    interface it stands in for, and each test function is a case, not a
    responsibility. The directory rule reads project_parts(), so a checkout that
    lives under a directory named `tests` is not all tests.
    """
    name = filepath.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    directories = project_parts(filepath)[:-1]
    return "tests" in directories or "test" in directories


def in_nested_repository(root: Path, path: Path, cache: dict | None = None) -> bool:
    """True when the file ``path`` lies in another repository nested below ``root``.

    A directory below the scanned root that holds a ``.git`` entry is a
    different repository: a file there for a git worktree or a submodule, a
    directory for a nested clone. A worktree checked out under the project is a
    second copy of its code, and scoring it as the project's counts every
    finding twice. ``root``'s own ``.git`` is not consulted — scanning a
    worktree is scanning a repository. ``cache`` (directory -> verdict) lets a
    caller walking many files stat each directory once.
    """
    cache = {} if cache is None else cache
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current = current / part
        nested = cache.get(current)
        if nested is None:
            nested = cache[current] = os.path.lexists(current / ".git")
        if nested:
            return True
    return False
# Installed skill payloads belong to the tooling, not to the host project.
# Match the pair, not the agent directory: hooks and other configuration there
# are still project inputs. Paths are relative to the scan root so an explicit
# scan of a skill continues to work.
def is_installed_skill(parts) -> bool:
    return any(parent in {".codex", ".claude", ".kiro", ".agents"} and child == "skills"
               for parent, child in zip(parts, parts[1:]))


def find_python_files(path: Path) -> Iterator[Path]:
    """Yield the .py files under ``path``, skipping vendored/generated dirs and
    any repository nested below it (see in_nested_repository).

    Records ``path`` as the analysis root (see set_analysis_root) before
    walking: this is where a run says what it is analysing.
    """
    set_analysis_root(path)
    return _python_files(path)


def _python_files(path: Path) -> Iterator[Path]:
    """find_python_files' walk, without recording an analysis root."""
    path = Path(path)
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        nested: dict = {}
        for p in path.rglob("*.py"):
            if (EXCLUDE_DIRS.isdisjoint(p.relative_to(path).parts)
                    and not in_nested_repository(path, p, nested)
                    and not is_installed_skill(p.relative_to(path).parts)):
                yield p


# Files whose presence marks the top of a project.
_PROJECT_MARKERS = (".git", "pyproject.toml", "setup.py", "setup.cfg")

class _Analysis:
    """The directory this run analyses, resolved; None for a single file.

    A per-file detector is handed one path and nothing else, so this is how it
    learns which part of that path is the project and which is merely where the
    project was checked out. find_python_files() records it, and the runner
    passes it to every worker as a parameter rather than trusting fork to copy
    process state. One instance, mutated in place: the state belongs to the run,
    and no caller rebinds the module's name for it.
    """

    root: Path | None = None


_ANALYSIS = _Analysis()


def set_analysis_root(path) -> None:
    """Record what this run analyses: a directory, or None for a single file."""
    candidate = Path(path) if path is not None else None
    _ANALYSIS.root = candidate.resolve() if candidate is not None and candidate.is_dir() else None


def analysis_root() -> Path | None:
    """The directory this run analyses, or None (a single file, or nothing yet)."""
    return _ANALYSIS.root


@functools.lru_cache(maxsize=4096)
def _has_marker(directory: Path) -> bool:
    return any(os.path.lexists(directory / marker) for marker in _PROJECT_MARKERS)


def _anchor(path: Path) -> Path | None:
    """The directory a file's place in its project is measured from.

    The nearest enclosing directory with a project marker, so analysing a
    project's own `tests/` directory still sees `tests` in the parts below its
    root; the analysis root when no directory above the file is marked; for a
    lone file with neither, None.
    """
    resolved = Path(path).resolve()
    root = _ANALYSIS.root
    bounded = root is not None and (resolved == root or root in resolved.parents)
    for directory in resolved.parents:
        if _has_marker(directory):
            return directory
        if bounded and directory == root and root.name.lower() not in {"test", "tests"}:
            break
    if root is not None and (resolved == root or root in resolved.parents):
        return root.parent if root.name.lower() in {"test", "tests"} else root
    return None


def project_parts(path: Path) -> tuple[str, ...]:
    """``path``'s parts below its project (see _anchor); its own parts when it
    belongs to none.

    `/home/runner/work/tests/tests/src/app.py` is ("src", "app.py"): the
    `tests` directories are where CI checked the repository out, not part of it.
    """
    anchor = _anchor(path)
    if anchor is not None:
        return Path(path).resolve().relative_to(anchor).parts
    return Path(path).parts


def project_root(path: Path) -> Path:
    """The project a file belongs to: its anchor (a project marker, bounded by
    the analysis root, or that root); failing that, the directory its top-level
    package sits in.

    A per-file detector knows only the file it was handed, so this is how one
    finds the tree that first_party_modules describes.
    """
    anchor = _anchor(path)
    if anchor is not None:
        return anchor
    path = Path(path).resolve()
    base = path if path.is_dir() else path.parent
    while (base / "__init__.py").exists() and base.parent != base:
        base = base.parent
    return base


@functools.lru_cache(maxsize=None)
def first_party_modules(root: Path) -> frozenset:
    """Top-level import names that resolve to this repository's own code.

    The stem of every module and the name of every package anywhere in the
    tree, plus each directory of modules directly under ``root`` or
    ``root/src`` (a PEP 420 namespace package, such as ``tests/``). This is
    find_dependency_issues' rule for an import no manifest has to declare, and
    the coupling detector's for a type a method could be moved onto.
    """
    names = set()
    for p in _python_files(root):  # walking another tree must not move the analysis root
        rel = p.relative_to(root).parts
        names.add(p.parent.name if p.name == "__init__.py" else p.stem)
        if len(rel) >= 2:
            names.add(rel[1] if rel[0] == "src" and len(rel) >= 3 else rel[0])
    names.discard("src")
    return frozenset(names)


def looks_like_test(path) -> bool:
    """Shared test predicate, scoped to the project rather than checkout ancestors."""
    return is_test_file(Path(path))


# The verbs a router or app object registers a handler under: FastAPI and
# Starlette (get/post/.../api_route/websocket), Flask and friends (route).
ROUTE_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options",
                         "api_route", "route", "websocket"})


def is_route_handler(node) -> bool:
    """True for a function registered by a decorator call like `@router.get(...)`.

    The framework, not the author, calls it: its parameters are bound by name from
    the request, and its return annotation becomes the response model, so several
    rules that are about a function's callers do not apply to it.
    """
    return any(isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
               and dec.func.attr in ROUTE_VERBS for dec in node.decorator_list)


# A noqa comment's code list followed by a separator and some text: the suppression
# says why. The separators are the ones people actually type after a noqa list;
# the reason must start with something other than another separator character,
# so a trailing `--` with nothing after it is still a bare suppression.
_REASONED_NOQA = re.compile(
    r"#\s*noqa:\s*(?P<codes>[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)\s*(?:--|—|-|:)\s*[^\s\-—:]",
    re.IGNORECASE,
)


def reasoned_noqa(lines: list[str], lineno: int, code: str) -> bool:
    """True when ``lineno`` carries ``# noqa: <code>`` *with a stated reason*.

    A repo that gates on Ruff has already adjudicated the line a detector is about
    to flag, and the reason it wrote down is the benign explanation a candidate
    names — so a detector that mirrors a Ruff rule reads a reasoned suppression
    of that rule as "a lead to confirm", not as a defect. A bare ``# noqa: CODE``
    states no reason and proves nothing; it stays a finding.

    The suppression may sit on the flagged line itself or on a comment-only line
    directly below it (the first line of a block body, where a long reason lands
    once the header line is full). ``lines`` is the file's text split into lines,
    ``lineno`` is 1-based.
    """
    wanted = code.upper()
    for index in (lineno, lineno + 1):
        if not 0 < index <= len(lines):
            continue
        text = lines[index - 1]
        if index != lineno and not text.lstrip().startswith("#"):
            continue
        for match in _REASONED_NOQA.finditer(text):
            codes = {c.strip().upper() for c in match.group("codes").split(",")}
            if wanted in codes:
                return True
    return False


class _ParseCache:
    """The last file read, held only until a different one is asked for.

    Every detector used to read and parse every file for itself, so running the
    whole suite parsed each file once per detector — on a large repository, most
    of the runtime. The runner instead asks every detector about one file before
    moving to the next, so the second and later callers for a file get the
    first one's work.

    Holding **one** file is the whole design, not a limitation. Keeping every
    tree would cost gigabytes on a large repository, and file-major order never
    wants an older one back. A detector run on its own — one file at a time,
    all files — is therefore no worse than it was, just no better.

    A read or parse failure is remembered alongside the text so it can be
    re-raised rather than retried, and so `source()` can still answer for a file
    that will not parse.
    """

    def __init__(self) -> None:
        self._path: str | None = None
        self._source: str | None = None
        self._tree = None
        self._error: Exception | None = None

    def _load(self, filepath: Path) -> None:
        key = str(filepath)
        if key == self._path:
            return
        self._path, self._source, self._tree, self._error = key, None, None, None
        try:
            self._source = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # re-raised by whichever accessor was asked
            self._error = exc
            return
        try:
            self._tree = ast.parse(self._source, filename=key)
        except Exception as exc:
            self._error = exc

    def parse(self, filepath: Path) -> tuple[str, "ast.AST"]:
        """``(source, tree)``, re-raising exactly what a direct parse would raise."""
        self._load(filepath)
        if self._error is not None:
            raise self._error
        return self._source, self._tree

    def source(self, filepath: Path) -> str:
        """The text alone. Raises only when the *read* failed."""
        self._load(filepath)
        if self._source is None:
            raise self._error
        return self._source


_PARSE_CACHE = _ParseCache()


def cached_parse(filepath: Path) -> tuple[str, "ast.AST"]:
    """``(source, tree)`` for ``filepath``, sharing the runner's parse of it.

    A read or parse failure is re-raised exactly as a direct ``read_text`` or
    ``ast.parse`` would have raised it, so a caller's own ``except SyntaxError``
    still classifies the file the same way.
    """
    return _PARSE_CACHE.parse(filepath)


def cached_source(filepath: Path) -> str:
    """The text of ``filepath``, for detectors that read rather than parse.

    A file that will not parse still has text, and a detector that only needs
    the text must still see it.
    """
    return _PARSE_CACHE.source(filepath)


# --------------------------------------------------------------------------- #
# Code copied verbatim from another project: the author's opt-in
# --------------------------------------------------------------------------- #

# `# doctor: verbatim-upstream <source>` — on its own line before the module's
# first statement it covers the whole file; directly above a def or class (or
# its decorators) it covers that definition. It is for code copied unchanged
# from another project, kept that way so a diff against the source stays
# readable, and it must name that source. analyze_all does not charge such code
# for its shape (UPSTREAM_EXEMPT_CATEGORIES) and reports what it withheld.
UPSTREAM_MARKER = "# doctor: verbatim-upstream"
_UPSTREAM_MARKER = re.compile(r"#\s*doctor:\s*verbatim-upstream\b(?P<source>.*)$")

# What a verbatim copy is not charged for: its shape — complexity, design,
# hygiene, duplication — which cannot change without breaking the diff it is
# kept for. Correctness and security categories are absent on purpose: a bug or
# an injection copied from upstream is still one here.
UPSTREAM_EXEMPT_CATEGORIES = frozenset({
    "complexity", "code_smells", "overengineering", "design_smells", "pattern_issues",
    "dead_code", "unpythonic", "coupling", "parameter_objects", "boolean_params",
    "return_issues", "loop_simplifications", "naming_issues", "comment_smells",
    "outdated_idioms", "missing_docstrings", "type_gaps", "test_smells", "ai_scaffolding",
    "local_imports", "duplicates",
})


@dataclass(frozen=True)
class UpstreamMarks:
    """What one file's verbatim-upstream markers declare.

    ``ranges`` are (first line, last line, source) spans the markers cover;
    ``ignored`` are (marker line, reason) for markers that cover nothing — one
    that names no source, or that sits above no definition.
    """

    ranges: tuple = ()
    ignored: tuple = ()

    def covers(self, line) -> bool:
        return isinstance(line, int) and any(start <= line <= end for start, end, _ in self.ranges)


@functools.lru_cache(maxsize=1024)
def upstream_marks(path: str) -> UpstreamMarks:
    """The verbatim-upstream markers in ``path`` (see UPSTREAM_MARKER)."""
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UpstreamMarks()
    if "verbatim-upstream" not in source:
        return UpstreamMarks()
    try:
        markers = [(tok.start[0], match.group("source").strip())
                   for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                   if tok.type == tokenize.COMMENT
                   and (match := _UPSTREAM_MARKER.match(tok.string.strip()))]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the detectors already said what they had to
            tree = ast.parse(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return UpstreamMarks()
    lines = source.splitlines()

    def start_of(node):
        return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])

    body = tree.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    first_statement = start_of(body[0]) if body else len(lines) + 1
    definitions = {start_of(node): node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    ranges, ignored = [], []
    for line, source_named in markers:
        if not source_named:
            ignored.append((line, "it names no source; say what project and file the code was copied from"))
            continue
        if line < first_statement:
            ranges.append((1, len(lines), source_named))
            continue
        following = line + 1
        while following <= len(lines) and (not lines[following - 1].strip()
                                           or lines[following - 1].lstrip().startswith("#")):
            following += 1
        node = definitions.get(following)
        if node is None:
            ignored.append((line, "it sits above no def or class, and after the module's first statement"))
            continue
        ranges.append((following, node.end_lineno, source_named))
    return UpstreamMarks(tuple(ranges), tuple(ignored))


def sort_findings(findings: list) -> list:
    """The order a report is read in: severity first, then file, then line.

    Every detector but one sorts this way, and each used to spell the key out in
    its own main(). One copy, so the runner and a detector's own CLI cannot
    drift into ordering the same findings differently.
    """
    findings.sort(key=lambda f: (f.severity != "high", f.severity != "medium",
                                 f.file, f.line))
    return findings


# --------------------------------------------------------------------------- #
# The confidence discipline, as a type
# --------------------------------------------------------------------------- #

class SchemaError(ValueError):
    """A detector tried to emit a record its evidence does not support."""


VALID_KINDS = frozenset({"finding", "candidate"})


@dataclass(frozen=True)
class Finding:
    """One output record, in one of two kinds.

    A **finding** asserts a defect. It carries a concrete fix, because a claim
    you cannot act on is not worth making.

    A **candidate** reports a lead that needs verification. It carries the
    specific ways a healthy codebase produces the same observation, and it
    carries no fix — recommending an edit on heuristic evidence is how a tool
    like this talks someone into deleting live code.

    The constructor enforces the difference. Prose in a reference file does not
    survive contact with a detector author in a hurry; a raised exception does.

    Frozen to ensure the schema enforcement holds across the lifetime of the
    object, not just at construction time.
    """

    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str = ""
    # Tuples, not lists. `frozen=True` blocks reassignment but not in-place
    # mutation, so a list here would let `candidate.also_caused_by.clear()`
    # walk a validated record into a schema-invalid state that
    # __post_init__ never re-checks — and it would then serialise and emit
    # like any other record. __post_init__ below coerces any list handed to
    # the constructor (including one that came back out of json.loads,
    # which knows nothing about tuples) into a tuple, so the type is
    # actually enforced, not just annotated.
    also_caused_by: tuple[str, ...] = ()
    severity: str = "medium"
    kind: str = "finding"
    code_snippet: str = ""
    # Other lines that participate in the same finding. analyze_diff.py uses
    # these so a cross-declaration smell surfaces when any participant changed.
    related_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "also_caused_by", tuple(self.also_caused_by))
        object.__setattr__(self, "related_lines", tuple(self.related_lines))
        if self.kind not in VALID_KINDS:
            raise SchemaError(
                f"{self.smell_type}: kind must be one of {sorted(VALID_KINDS)}, got {self.kind!r}"
            )
        if self.kind == "finding":
            if not self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a finding asserts a defect and must carry a suggestion; "
                    "if you cannot name the fix, emit a candidate instead"
                )
            if self.also_caused_by:
                raise SchemaError(
                    f"{self.smell_type}: also_caused_by belongs to candidates; a finding that has "
                    "benign explanations is a candidate"
                )
        else:
            if self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a candidate must not carry a suggestion — it is an "
                    "unverified lead, and a fix on unverified evidence is how live code gets deleted"
                )
            if not self.also_caused_by or not any(s.strip() for s in self.also_caused_by):
                raise SchemaError(
                    f"{self.smell_type}: a candidate must name the ways a healthy codebase produces "
                    "this observation in also_caused_by, so the reader can rule them out"
                )


# The keys Python detectors use for the same three ideas. `format_findings.py`
# and `analyze_all.py` already read these alternatives; the validator accepts
# the same spellings so a detector need not be rewritten to be checked.
_TYPE_KEYS = ("smell_type", "issue_type", "pattern_type", "type")


def _severity_of(record: dict) -> str:
    if record.get("severity"):
        return str(record["severity"])
    confidence = record.get("confidence")
    if isinstance(confidence, (int, float)):
        return "high" if confidence >= 90 else ("medium" if confidence >= 70 else "low")
    return "medium"


def validate_record(record: dict) -> dict:
    """Check one detector record against the finding/candidate contract.

    Raises SchemaError when a finding has no fix or a candidate has no benign
    explanation. Returns a copy of the record with `kind` made explicit, so a
    consumer never has to guess what an absent key meant. The argument is not
    mutated.
    """
    smell = next((str(record[key]) for key in _TYPE_KEYS if record.get(key)), "issue")
    suggestion = str(record.get("suggestion") or record.get("after") or "")
    kind = str(record.get("kind") or "finding")
    Finding(
        file=str(record.get("file", "")), line=int(record.get("line") or 1),
        smell_type=smell, description=str(record.get("description", "")),
        suggestion=suggestion, severity=_severity_of(record), kind=kind,
        also_caused_by=tuple(record.get("also_caused_by") or ()),
    )
    return {**record, "kind": kind}


class _SaidOnce:
    """Remembers what has already been said, so it is not said again.

    The runner asks every detector about one file, and without this each of them
    announces the same unreadable file: one broken file produced 28 identical
    lines and buried the report under them. A detector run on its own sees each
    file once, so its output is unchanged either way.
    """

    def __init__(self) -> None:
        self._said: set[str] = set()

    def first_time(self, key: str) -> bool:
        if key in self._said:
            return False
        self._said.add(key)
        return True


_UNPARSEABLE_ANNOUNCED = _SaidOnce()


def warn_unparseable(filepath: Path, exc: Exception) -> None:
    """Note a file that will not parse — expected for broken or non-Python files.

    Said once per file per process, however many detectors trip over it. Which
    detector noticed first is not information; that the file was skipped is.
    """
    if _UNPARSEABLE_ANNOUNCED.first_time(str(filepath)):
        print(f"⚠️  {filepath}: skipped, does not parse ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )
