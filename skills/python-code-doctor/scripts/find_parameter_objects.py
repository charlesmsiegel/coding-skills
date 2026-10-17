#!/usr/bin/env python3
"""
Detect data clumps in Python code via cross-function AST analysis.

A "data clump" is a group of parameters that keep travelling together across
many function signatures. When the same three (or more) names recur, they almost
always belong in a single object (a dataclass / value object), which shortens
signatures and centralises validation.

This analysis is whole-codebase: it collects parameter sets from every function
under the given path, then reports name-groups that recur in >= 3 functions.

Finds:
  - data_clump : a group of parameters appearing together in multiple functions

In a test file the parameters of a `test*` function or a `@pytest.fixture` are
fixture requests — pytest's dependency injection — so they are left out: a
suite whose tests share fixtures repeats their names by design.

Methods that share a name count once: `advance(self, i, x, denoised)` declared
on a Protocol and implemented by three solvers (or mirrored by a fake) is one
signature, a contract, not four functions passing the same loose values.
Initializers are the exception — unrelated classes built from the same three
values are exactly the clump this looks for.

A clump is one body of code's design, so its members are counted within one
body. Code marked `# doctor: verbatim-upstream` keeps upstream's signatures and
is a body of its own: it never makes up the numbers for a clump charged to the
project's code. A clump wholly inside it, or one that takes marked code to
reach the threshold, is reported at a marked member, where analyze_all
withholds it and counts it as not charged.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from itertools import combinations
from collections import defaultdict
from common import configure_output, find_python_files, is_test_file, upstream_marks, warn_detector_error, warn_unparseable, looks_like_test, is_route_handler


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    related_lines: list[int] = field(default_factory=list)
    occurrences: list[str] = field(default_factory=list)


MIN_GROUP = 3        # smallest clump size to consider
MIN_FUNCS = 3        # how many functions must share the group
MAX_PARAMS = 8       # skip very long signatures (already flagged elsewhere)
_SKIP = {"self", "cls"}
_INITIALIZERS = {"__init__", "__new__", "__post_init__"}



def _decorator_name(decorator: ast.AST) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return None


def _framework_called(node, in_test_file: bool) -> bool:
    """True when the framework, not a caller, supplies this function's arguments."""
    if in_test_file and node.name.startswith("test"):
        return True  # pytest test: fixtures by name
    for decorator in node.decorator_list:
        if _decorator_name(decorator) == "fixture":
            return True
    return is_route_handler(node)  # route handler: bound from the request


def _param_names(args: ast.arguments):
    names = []
    for a in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        if a.arg not in _SKIP:
            names.append(a.arg)
    return names


def _is_fixture(node) -> bool:
    """`@pytest.fixture`, `@pytest.fixture(...)` or a bare imported `@fixture`."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if getattr(target, "id", getattr(target, "attr", None)) == "fixture":
            return True
    return False


def _requests_fixtures(node) -> bool:
    """Whether pytest fills this function's parameters with fixtures."""
    return node.name.startswith("test") or _is_fixture(node)


def _collect(path: Path):
    """Return list of (frozenset_of_params, file, line, funcname, identity, body).

    ``identity`` is what makes two definitions the same function for counting:
    a method's name (all methods named `advance` are one contract), and the
    definition's own location for anything else. ``body`` is the code it is
    counted with: "upstream" for code marked as a verbatim copy, else "project".
    """
    out = []
    for filepath in find_python_files(path):
        in_tests = is_test_file(filepath)  # judged below its project, see common.project_parts
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, ValueError) as exc:
            warn_unparseable(filepath, exc)
            continue
        except Exception as exc:
            warn_detector_error(filepath, exc)
            continue
        methods = {id(item) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                   for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        marks = upstream_marks(str(filepath.resolve()))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _framework_called(node, in_tests):
                    continue
                names = _param_names(node.args)
                if MIN_GROUP <= len(names) <= MAX_PARAMS:
                    identity = (("method", node.name)
                                if id(node) in methods and node.name not in _INITIALIZERS
                                else ("def", str(filepath), node.lineno))
                    body = "upstream" if marks.covers(node.lineno) else "project"
                    out.append((frozenset(names), str(filepath), node.lineno, node.name, identity, body))
    return out


def _clumps(bodies: dict, identities: dict) -> list[dict]:
    """The clumps one group of names makes, as {locs, identities, body, marked}.

    Each body whose own functions reach MIN_FUNCS makes a clump of its members.
    When none does but marked code is among them and all of them together do,
    the clump needs the marked code: it is kept whole, as body "mixed", and
    anchored at a marked member (``marked``).
    """
    found = [{"locs": bodies[body], "identities": identities[body], "body": body, "marked": []}
             for body in bodies if len(identities[body]) >= MIN_FUNCS]
    everyone = set().union(*identities.values())
    if not found and "upstream" in bodies and len(everyone) >= MIN_FUNCS:
        found.append({"locs": [loc for locs in bodies.values() for loc in locs], "identities": everyone,
                      "body": "mixed", "marked": bodies["upstream"]})
    return found


def _families(clumps: list[dict]) -> list[dict]:
    """Join clumps that are one group of parameters seen through different subsets.

    `(name, binders, conclusion, imports)` travelling through five functions,
    some of which drop `name` and some `imports`, yields three overlapping
    clumps with three different supports — and one refactoring (a single
    parameter object) answers all of them. Two clumps are one family when
    they belong to the same body of code and share at least MIN_GROUP - 1
    names and at least one function.
    """
    parent = list(range(len(clumps)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, first in enumerate(clumps):
        for j in range(i + 1, len(clumps)):
            second = clumps[j]
            if first["body"] == second["body"] \
                    and len(first["names"] & second["names"]) >= MIN_GROUP - 1 \
                    and set(first["locs"]) & set(second["locs"]):
                parent[root(j)] = root(i)
    joined: dict[int, dict] = {}
    for i, clump in enumerate(clumps):
        family = joined.setdefault(root(i), {"names": set(), "locs": set(), "identities": set(), "marked": set()})
        family["names"] |= clump["names"]
        family["locs"] |= set(clump["locs"])
        family["identities"] |= clump["identities"]
        family["marked"] |= set(clump["marked"])
    for family in joined.values():
        family["functions"] = len(family["identities"])
    return list(joined.values())


def detect(path: Path, ignore: set):
    if "data_clump" in ignore:
        return []
    functions = _collect(path)

    # Count every MIN_GROUP-sized subset of params, by the body of code each
    # member belongs to, tracking where it occurs.
    members = defaultdict(lambda: defaultdict(list))      # 3 names -> body -> [(file, line, name)]
    identities = defaultdict(lambda: defaultdict(set))   # 3 names -> body -> distinct functions
    for names, f, ln, fn, identity, body in functions:
        for combo in combinations(sorted(names), MIN_GROUP):
            members[frozenset(combo)][body].append((f, ln, fn))
            identities[frozenset(combo)][body].add(identity)

    # Merge triples that share the exact same support into one (maximal) clump,
    # so a recurring 4-name group is reported once rather than as 4 triples.
    by_support = {}
    for triple, bodies in members.items():
        for clump in _clumps(bodies, identities[triple]):
            data = by_support.setdefault((clump["body"], frozenset(clump["locs"])),
                                         {**clump, "names": set(), "identities": set()})
            data["names"].update(triple)
            data["identities"] |= clump["identities"]

    issues = []
    for data in _families(list(by_support.values())):
        names = sorted(data["names"])
        locs = data["locs"]
        f0, ln0, _ = min(data["marked"] or locs)
        where = ", ".join(f"{Path(f).name}:{ln} {fn}()" for f, ln, fn in sorted(locs)[:5])
        more = "" if len(locs) <= 5 else f" (+{len(locs) - 5} more)"
        issues.append(CodeSmell(
            file=f0, line=ln0, smell_type="data_clump",
            description=(f"Parameters {names} appear together in {data['functions']} functions"
                         + (f" ({len(locs)} definitions; same-named methods count once)"
                            if len(locs) != data["functions"] else "")
                         + f": {where}{more}"),
            suggestion="Group them into a dataclass (e.g. a single typed object) and pass that instead of the loose parameters.",
            severity="medium", code_snippet="",
            related_lines=sorted({ln for f, ln, _ in locs if f == f0 and ln != ln0}),
            occurrences=[f"{f}:{ln} {fn}()" for f, ln, fn in sorted(locs)],
        ))
    issues.sort(key=lambda x: (x.file, x.line))
    return issues


def analyze_tree(path: Path, ignore: set) -> list:
    """Findings for the whole tree. detect() already orders them."""
    return detect(Path(path), ignore)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect data clumps (recurring parameter groups)")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = detect(Path(args.path), ignore)

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No data clumps found!")
            return
        print(f"Found {len(all_issues)} data clump(s):\n")
        for i in all_issues:
            print(f"🟡 [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
