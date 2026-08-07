#!/usr/bin/env python3
"""
Find code nothing reaches: unused imports and locals, exports no other module
imports, unreachable statements, and private class members nobody calls.

The export half needs the whole tree, which is what makes it worth having —
a linter sees one file and cannot tell an unused export from a public API.
Anything reachable from an entry point, a barrel, or a dynamic import is left
alone, and the reasons are named in each finding.
"""

import re
from pathlib import Path

from common import Finding, is_declaration_file, is_test_file, run_tree_detector
from tsparse import statement_end
from tsproject import load_project, read_package_json

# Files whose exports are consumed by something outside the TypeScript graph.
ENTRY_PATTERNS = re.compile(
    r"(?:^|/)(?:main|index|app|server|worker|setup|entry|bootstrap|cli)\.(?:ts|tsx|mts|cts)$"
    r"|\.d\.ts$|\.config\.(?:ts|mts|cts)$|(?:^|/)(?:pages|app|routes)/",
)


def _string_mentions(project, name: str) -> bool:
    """A name that appears in a string may be reached dynamically."""
    for tsfile in project.files.values():
        for token in tsfile.tokens:
            if token.kind in ("str", "template") and name in token.value:
                return True
    return False


def _check_unused_imports(tsfile, path: Path, add) -> None:
    for record in tsfile.imports:
        if record.kind != "import" or record.side_effect_only:
            continue
        bindings = list(record.names)
        if record.default_name:
            bindings.append(record.default_name)
        if record.namespace_name:
            bindings.append(record.namespace_name)
        for name in bindings:
            used = any(
                token.kind == "name" and token.value == name and token.line != record.line
                for token in tsfile.tokens
            )
            in_jsx = tsfile.is_tsx and any(name in t.value for t in tsfile.jsx_text)
            if not used and not in_jsx:
                add(path, record.line, "unused_import",
                    f"`{name}` is imported from '{record.module}' and never used",
                    "Delete it. An unused import still costs a module resolution at build time and "
                    "can keep a whole dependency in the bundle.", "low")


def _check_unreachable(tsfile, path: Path, add) -> None:
    """A statement that follows an exit *in the same block*.

    The braceless guard clause is why this has to be strict: in
    `if (done) break;` the next line is perfectly reachable, and `break` there
    looks identical to a `break` that really does end the block. So the
    terminator only counts when the token before it closes a statement (`;`) or
    opens/closes a block — never when it is the body of a control clause.
    """
    terminators = ("return", "throw", "break", "continue")
    for index, token in enumerate(tsfile.tokens):
        if token.kind != "name" or token.value not in terminators or index == 0:
            continue
        previous = tsfile.tokens[index - 1]
        if not (previous.kind == "op" and previous.value in (";", "{", "}")):
            continue  # a braceless if/else/loop body, a label, or a `case`
        # `return <div\n  attr="x" />;` — a JSX element spans lines whose ends
        # are not operators, so the statement-end heuristic stops early and the
        # next attribute looks like a new statement. Not worth guessing at.
        if tsfile.is_tsx and index + 1 < len(tsfile) and tsfile.tokens[index + 1].is_op("<", "("):
            continue
        cursor = statement_end(tsfile, index + 1)
        while cursor < len(tsfile) and tsfile.tokens[cursor].is_op(";"):
            cursor += 1
        if cursor >= len(tsfile):
            continue
        following = tsfile.tokens[cursor]
        if following.is_op("}"):
            continue
        if following.is_name("case", "default", "else", "catch", "finally", "while", "function", "class"):
            continue
        add(path, following.line, "unreachable_code",
            f"Statement after `{token.value}` on line {token.line} can never run",
            "Delete it, or move it above the exit. Unreachable code is usually a merge artefact "
            "or an exit that was added and never followed through.", "high")


def _check_private_members(tsfile, path: Path, add) -> None:
    for klass in tsfile.classes:
        for member in [*klass.methods, *klass.props]:
            accessibility = getattr(member, "accessibility", None)
            if accessibility != "private" or getattr(member, "kind", "") == "constructor":
                continue
            name = member.name.lstrip("#")
            uses = sum(
                1 for index in range(klass.body_open, klass.body_close)
                if tsfile.tokens[index].kind == "name" and tsfile.tokens[index].value.lstrip("#") == name
            )
            if uses <= 1:
                add(path, member.line, "unused_private_member",
                    f"`{klass.name}.{member.name}` is private and referenced nowhere in the class",
                    "Delete it. Private means the compiler can prove nothing outside can reach it, "
                    "so an unreferenced private member is dead with certainty.", "medium")


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    project = load_project(root)
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion, severity=severity))

    for path, tsfile in project.files.items():
        if is_declaration_file(path):
            continue
        _check_unused_imports(tsfile, path, add)
        _check_unreachable(tsfile, path, add)
        if not is_test_file(path):
            _check_private_members(tsfile, path, add)

    _check_unused_exports(project, add)
    return findings


def _check_unused_exports(project, add) -> None:
    """Exports no module in the tree imports. Entry points are exempt."""
    manifest, package = read_package_json(project.root)
    declared_entries = set()
    for key in ("main", "module", "types", "browser", "bin"):
        value = package.get(key)
        if isinstance(value, str):
            declared_entries.add(Path(value).stem)
        elif isinstance(value, dict):
            declared_entries.update(Path(str(v)).stem for v in value.values())

    imported: dict[Path, set[str]] = {}
    star_reexported: set[Path] = set()
    for path, tsfile in project.files.items():
        for record in tsfile.imports:
            target = project.resolve(path, record.module) if record.module else None
            if target is None:
                continue
            names = set(record.names)
            if record.default_name:
                names.add("default")
            if record.namespace_name or record.kind == "export-from":
                star_reexported.add(target)
            imported.setdefault(target, set()).update(names)

    for path, tsfile in project.files.items():
        posix = path.as_posix()
        if is_test_file(path) or is_declaration_file(path) or ENTRY_PATTERNS.search(posix):
            continue
        if path.stem in declared_entries or path in star_reexported:
            continue
        used = imported.get(path, set())
        exported = [e for e in tsfile.exports if e.kind in ("named", "declaration", "default")]
        if not exported:
            continue
        unused = [e for e in exported if (e.name if e.kind != "default" else "default") not in used]
        if len(unused) == len(exported) and not used:
            add(path, 1, "unreferenced_module",
                f"Nothing in the tree imports {path.name} ({len(exported)} export(s))",
                "Check whether it is reached dynamically or by a build tool; if not, delete the "
                "file. An orphaned module is still compiled, linted and reviewed.", "medium")
            continue
        for export in unused:
            name = export.name
            if name in ("default", "*") or _string_mentions(project, name):
                continue
            add(path, export.line, "unused_export",
                f"`{name}` is exported but imported by no module in this tree",
                "Make it module-private (drop `export`), or delete it. An export is a promise to "
                "keep something stable; keeping that promise for nobody is pure cost.", "low")


if __name__ == "__main__":
    run_tree_detector(
        "Find unused imports, unreachable code, dead private members and unused exports",
        "No dead code found!",
        analyze,
    )
