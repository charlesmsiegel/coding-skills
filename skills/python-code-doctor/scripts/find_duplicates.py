#!/usr/bin/env python3
"""
Detect duplicate code in Python using AST structural comparison.
Finds: similar functions, duplicate code blocks, copy-paste code.

Two tiers, because a hash cannot tell "same code" from "same shape":

  - A **finding**: blocks identical up to renaming. Variable, argument, function
    and class names are erased before hashing; every literal, attribute name and
    called function survives. Two such blocks compute the same thing with the
    same constants — the copy-paste the detector exists to find.
  - A **candidate** (`kind: "candidate"`): blocks that match only once literals
    are erased too. A string or number that differs is often the whole meaning
    — `_digest("environment", x)` and `_digest("procedure", x)` are two
    identities, not one — so this is a lead for a reader, never a scored defect.

Size is measured in executable lines (docstrings and comments excluded), and a
block nested inside a reported duplicate is not reported again: the same copy
seen at a smaller granularity is not a second defect.
"""

import ast
import json
import hashlib
import argparse
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict
from common import configure_output, find_python_files, warn_detector_error, warn_unparseable


# Shortest block worth reporting as a duplicate, in executable lines. Shared
# with the runner so both entry points use the same floor.
DEFAULT_MIN_LINES = 5

_BLOCK_TYPES = {
    ast.FunctionDef: 'function', ast.AsyncFunctionDef: 'function',
    ast.If: 'if_block', ast.For: 'for_loop', ast.While: 'while_loop', ast.Try: 'try_block',
}


@dataclass
class DuplicateGroup:
    hash: str
    occurrences: list[dict]
    lines: int
    # True when the members are identical up to renaming (a finding); False
    # when they match only with literals erased (a candidate).
    exact: bool


class _DocstringStripper(ast.NodeTransformer):
    """Docstrings are documentation, not code: they neither count toward a
    block's size nor distinguish two otherwise identical bodies."""

    def _strip(self, node):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:]
        self.generic_visit(node)
        return node

    visit_Module = visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _strip


class _RenameNormalizer(ast.NodeTransformer):
    """Erase what a rename changes and nothing else.

    Variable, argument, function and class names become one name. A called
    function keeps its name (copy-paste keeps its calls), and so do attribute
    names, keywords, operators and every literal.
    """

    def visit_Name(self, node):
        node.id = '_VAR_'
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            for child in ast.iter_child_nodes(node):
                if child is not node.func:
                    self.visit(child)
            return node
        self.generic_visit(node)
        return node

    def visit_arg(self, node):
        node.arg = '_ARG_'
        return node

    def visit_FunctionDef(self, node):
        node.name = '_FUNC_'
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        node.name = '_CLASS_'
        self.generic_visit(node)
        return node


class _ShapeNormalizer(ast.NodeTransformer):
    """Erase literals and callee names too, leaving only the shape.

    Strings become one string and numbers one number. True, False and None
    keep their identity: `return True` and `return 0` are not one shape, and
    a boolean is not a count.
    """

    def visit_Name(self, node):
        node.id = '_VAR_'
        return node

    def visit_Constant(self, node):
        value = node.value
        if isinstance(value, (str, bytes)):
            node.value = '_STR_'
        elif isinstance(value, bool) or value is None:
            pass
        elif isinstance(value, (int, float, complex)):
            node.value = 0
        return node


def _digest(node: ast.AST) -> str:
    dump = ast.dump(node, annotate_fields=False)
    return hashlib.sha256(dump.encode()).hexdigest()[:12]


def executable_lines(node: ast.AST) -> int:
    """Distinct source lines that carry a node. Run after docstrings are
    stripped, so a nine-line docstring over a one-line body counts as two."""
    return len({child.lineno for child in ast.walk(node)
                if getattr(child, 'lineno', None) is not None})


def get_code_preview(source_lines: list[str], start: int, end: int, max_lines: int = 3) -> str:
    preview_lines = source_lines[start-1:min(end, start-1+max_lines)]
    preview = '\n'.join(line.strip() for line in preview_lines if line.strip())
    if end - start + 1 > max_lines:
        preview += '\n...'
    return preview[:100]


def analyze_file(filepath: Path, min_lines: int) -> list[dict]:
    """Every block of at least `min_lines` executable lines, with both hashes.

    The tree is normalised in place, in two passes, and each block is hashed
    after each pass — so nested blocks cost nothing extra and no copy is made.
    """
    try:
        source = filepath.read_text(encoding='utf-8', errors='replace')
        tree = ast.parse(source, filename=str(filepath))
        source_lines = source.splitlines()
        _DocstringStripper().visit(tree)

        blocks = []
        for node in ast.walk(tree):
            block_type = _BLOCK_TYPES.get(type(node))
            if block_type is None:
                continue
            lines = executable_lines(node)
            if lines < min_lines:
                continue
            end_line = getattr(node, 'end_lineno', None) or node.lineno
            blocks.append({
                'node': node,
                'type': block_type,
                'name': node.name if block_type == 'function' else f'{block_type.split("_")[0]} at line {node.lineno}',
                'file': str(filepath),
                'line': node.lineno,
                'end_line': end_line,
                'lines': lines,
                'preview': get_code_preview(source_lines, node.lineno, end_line),
            })

        _RenameNormalizer().visit(tree)
        for block in blocks:
            block['exact_hash'] = _digest(block['node'])
        _ShapeNormalizer().visit(tree)
        for block in blocks:
            block['shape_hash'] = _digest(block['node'])
            del block['node']
        return blocks
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def _group(blocks: list[dict], key: str, exact: bool) -> list[DuplicateGroup]:
    by_hash = defaultdict(list)
    for block in blocks:
        by_hash[block[key]].append(block)
    groups = []
    for hash_val, members in by_hash.items():
        if len(members) >= 2:
            groups.append(DuplicateGroup(
                hash=hash_val,
                occurrences=[{
                    'file': b['file'], 'line': b['line'], 'end_line': b['end_line'],
                    'name': b['name'], 'type': b['type'], 'preview': b['preview'],
                } for b in members],
                lines=int(sum(b['lines'] for b in members) / len(members)),
                exact=exact,
            ))
    return groups


def _members(group: DuplicateGroup) -> frozenset:
    return frozenset((o['file'], o['line']) for o in group.occurrences)


def _collapse_nested(groups: list[DuplicateGroup]) -> list[DuplicateGroup]:
    """Drop a group whose every occurrence sits inside an occurrence of a group
    already kept: the loop inside two identical functions is the same copy,
    not a second one. Findings are kept before candidates, larger first."""
    kept: list[DuplicateGroup] = []
    claimed: dict[str, list[tuple[int, int]]] = defaultdict(list)

    def inside(occurrence: dict) -> bool:
        return any(start <= occurrence['line'] and occurrence['end_line'] <= end
                   and (start, end) != (occurrence['line'], occurrence['end_line'])
                   for start, end in claimed[occurrence['file']])

    for group in sorted(groups, key=lambda g: (not g.exact, -g.lines, -len(g.occurrences))):
        if all(inside(o) for o in group.occurrences):
            continue
        kept.append(group)
        for occurrence in group.occurrences:
            claimed[occurrence['file']].append((occurrence['line'], occurrence['end_line']))
    return kept


def find_duplicates(path: Path, min_lines: int) -> list[DuplicateGroup]:
    all_blocks = []
    for filepath in find_python_files(path):
        all_blocks.extend(analyze_file(filepath, min_lines))

    exact = _group(all_blocks, 'exact_hash', exact=True)
    exact_members = {_members(g) for g in exact}
    # A shape group with exactly the members of an exact group adds nothing;
    # one with more members names the extra look-alikes as a lead.
    loose = [g for g in _group(all_blocks, 'shape_hash', exact=False)
             if _members(g) not in exact_members]

    duplicates = _collapse_nested(exact + loose)
    duplicates.sort(key=lambda x: (not x.exact, -len(x.occurrences), -x.lines))
    return duplicates


def to_findings(duplicates: list[DuplicateGroup]) -> list[dict]:
    """Render groups in the flat findings shape every other detector emits,
    anchored at the first occurrence, so format_findings/analyze_all can show
    a real location and description instead of '?:?'."""
    findings = []
    for dup in duplicates:
        first, rest = dup.occurrences[0], dup.occurrences[1:]
        others = ", ".join(f"{o['file']}:{o['line']}" for o in rest)
        count = len(dup.occurrences)
        if dup.exact:
            description = (f"{count} {first['type']} blocks identical up to renaming "
                           f"(~{dup.lines} executable lines each); also at {others}")
            suggestion = 'Extract the shared logic into one function/class.'
            severity = 'high' if count >= 3 else 'medium'
        else:
            description = (f"{count} {first['type']} blocks with one shape but different literals "
                           f"(~{dup.lines} executable lines each); also at {others}")
            suggestion = ('Read them side by side before extracting: a literal that differs is often '
                          'the whole meaning (a tag, a message, a mode), and one function with a flag '
                          'parameter is worse than the copy.')
            severity = 'low'
        finding = {
            'file': first['file'],
            'line': first['line'],
            'smell_type': 'duplicate_code',
            'description': description,
            'suggestion': suggestion,
            'severity': severity,
            'lines': dup.lines,
            'code_snippet': first['preview'].split('\n')[0][:80],
            'occurrences': [{k: v for k, v in o.items() if k != 'end_line'} for o in dup.occurrences],
        }
        if not dup.exact:
            finding['kind'] = 'candidate'
        findings.append(finding)
    return findings


def analyze_tree(path: Path, ignore: set, min_lines: int = DEFAULT_MIN_LINES) -> list:
    """Findings for the whole tree, ordered as main() orders them.

    This detector normalises identifiers in the trees it hashes, so it parses
    for itself rather than sharing the runner's cached tree — a shared tree it
    rewrote would corrupt every other detector's view of the file.
    """
    if "duplicate_code" in ignore:
        return []
    return to_findings(find_duplicates(Path(path), min_lines))


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect duplicate code in Python")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--min-lines', type=int, default=DEFAULT_MIN_LINES,
                        help='Smallest block to report, in executable lines')
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated smell types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()
    duplicates = [] if 'duplicate_code' in ignore else find_duplicates(Path(args.path), args.min_lines)

    if args.format == 'json':
        print(json.dumps(to_findings(duplicates), indent=2))
    else:
        if not duplicates:
            print("✅ No duplicate code found!")
            return

        findings = [d for d in duplicates if d.exact]
        candidates = [d for d in duplicates if not d.exact]
        total_occurrences = sum(len(d.occurrences) for d in findings)
        total_duplicate_lines = sum(d.lines * (len(d.occurrences) - 1) for d in findings)

        print(f"Found {len(findings)} duplicate code pattern(s) and {len(candidates)} look-alike candidate(s)")
        print(f"Duplicates: {total_occurrences} occurrences, ~{total_duplicate_lines} redundant executable lines\n")

        for i, dup in enumerate(duplicates, 1):
            label = "Duplicate" if dup.exact else "Candidate (same shape, different literals)"
            print(f"{'='*60}")
            print(f"{label} #{i} ({len(dup.occurrences)} occurrences, ~{dup.lines} executable lines each)")
            print(f"{'='*60}")

            for occ in dup.occurrences:
                print(f"\n📍 {occ['file']}:{occ['line']} ({occ['type']}: {occ['name']})")
                for line in occ['preview'].split('\n'):
                    print(f"   {line}")
            print()

        print("\n💡 Suggestion: Extract duplicate code into shared functions/classes. "
              "A candidate is a lead — read both sites before merging them.")


if __name__ == '__main__':
    main()
