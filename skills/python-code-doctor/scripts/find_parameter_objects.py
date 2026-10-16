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
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from itertools import combinations
from collections import defaultdict
from common import configure_output, find_python_files, is_test_file, warn_detector_error, warn_unparseable


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


MIN_GROUP = 3        # smallest clump size to consider
MIN_FUNCS = 3        # how many functions must share the group
MAX_PARAMS = 8       # skip very long signatures (already flagged elsewhere)
_SKIP = {"self", "cls"}
_INITIALIZERS = {"__init__", "__new__", "__post_init__"}


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
    """Return list of (frozenset_of_params, file, line, funcname, identity).

    ``identity`` is what makes two definitions the same function for counting:
    a method's name (all methods named `advance` are one contract), and the
    definition's own location for anything else.
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
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if in_tests and _requests_fixtures(node):
                    continue
                names = _param_names(node.args)
                if MIN_GROUP <= len(names) <= MAX_PARAMS:
                    identity = (("method", node.name)
                                if id(node) in methods and node.name not in _INITIALIZERS
                                else ("def", str(filepath), node.lineno))
                    out.append((frozenset(names), str(filepath), node.lineno, node.name, identity))
    return out


def _families(clumps: list[dict]) -> list[dict]:
    """Join clumps that are one group of parameters seen through different subsets.

    `(name, binders, conclusion, imports)` travelling through five functions,
    some of which drop `name` and some `imports`, yields three overlapping
    clumps with three different supports — and one refactoring (a single
    parameter object) answers all of them. Two clumps are one family when
    they share at least MIN_GROUP - 1 names and at least one function.
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
            if len(first["names"] & second["names"]) >= MIN_GROUP - 1 \
                    and set(first["locs"]) & set(second["locs"]):
                parent[root(j)] = root(i)
    joined: dict[int, dict] = {}
    for i, clump in enumerate(clumps):
        family = joined.setdefault(root(i), {"names": set(), "locs": set()})
        family["names"] |= clump["names"]
        family["locs"] |= set(clump["locs"])
    return list(joined.values())


def detect(path: Path, ignore: set):
    if "data_clump" in ignore:
        return []
    functions = _collect(path)

    # Count every MIN_GROUP-sized subset of params, tracking where it occurs.
    triple_locs = defaultdict(list)   # frozenset(3 names) -> [(file, line, name)]
    identities = defaultdict(set)     # frozenset(3 names) -> distinct functions
    for names, f, ln, fn, identity in functions:
        for combo in combinations(sorted(names), MIN_GROUP):
            triple_locs[frozenset(combo)].append((f, ln, fn))
            identities[frozenset(combo)].add(identity)

    qualifying = {t: locs for t, locs in triple_locs.items() if len(identities[t]) >= MIN_FUNCS}

    # Merge triples that share the exact same support into one (maximal) clump,
    # so a recurring 4-name group is reported once rather than as 4 triples.
    by_support = defaultdict(lambda: {"names": set(), "locs": None})
    for t, locs in qualifying.items():
        key = frozenset(locs)
        by_support[key]["names"].update(t)
        by_support[key]["locs"] = locs
        by_support[key]["functions"] = len(identities[t])

    issues = []
    for data in _families(list(by_support.values())):
        names = sorted(data["names"])
        locs = data["locs"]
        f0, ln0, _ = sorted(locs)[0]
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
