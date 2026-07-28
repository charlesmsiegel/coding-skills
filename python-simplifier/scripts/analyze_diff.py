#!/usr/bin/env python3
"""
Review-a-change-request lens: run the file-level detectors against only the files
(and, by default, the added/modified lines) of a diff.

This is the tool for reviewing an AI-written feature or CR: you want findings about
*what changed*, not the legacy code around it. It runs the per-file detectors on
each changed .py file and keeps only findings that land on lines the diff touched.

Whole-repo / architecture detectors (find_import_cycles, find_dependency_issues,
find_untested_modules, find_duplicates, find_dead_code, find_overengineering,
find_coupling_issues) need the full tree to be correct, so they are deliberately
NOT part of the diff lens — run them with analyze_all.py against the whole repo.

Usage:
  python analyze_diff.py                 # working tree vs. the merge-base with the default branch
  python analyze_diff.py origin/main     # vs. an explicit base ref
  python analyze_diff.py HEAD~3
  python analyze_diff.py --all-lines     # every line of each changed file, not just changed lines
  python analyze_diff.py --format json | python format_findings.py
"""

import re
import ast
import sys
import json
import tempfile
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict, Counter

# Per-file detectors only. Each is meaningful on a single file and reports a line
# within that file. Whole-repo detectors are excluded on purpose (see module docstring).
DIFF_SAFE_SCRIPTS = [
    "analyze_complexity.py",
    "find_code_smells.py",
    "find_design_smells.py",
    "find_pattern_issues.py",
    "find_unpythonic.py",
    "find_mutation_hazards.py",
    "find_exception_issues.py",
    "find_global_state.py",
    "find_boolean_params.py",
    "find_return_issues.py",
    "find_loop_simplifications.py",
    "find_naming_issues.py",
    "find_comment_smells.py",
    "find_resource_leaks.py",
    "find_security_issues.py",
    "find_debug_leftovers.py",
    "find_outdated_idioms.py",
    "find_missing_docstrings.py",
    "find_type_gaps.py",
    "find_test_smells.py",
    "find_ai_scaffolding.py",
    "find_duplicate_definitions.py",
    "find_unawaited_coroutines.py",
    "find_redundant_comments.py",
    "find_local_imports.py",
]

_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git(args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_base(explicit):
    """Pick a base ref to diff against. Returns None for an invalid explicit ref."""
    if explicit:
        # A misspelled base must be an error, not an empty (falsely clean) diff.
        if _git(["rev-parse", "--verify", "--quiet", f"{explicit}^{{commit}}"]) is None:
            return None
        return explicit
    for candidate in ("origin/main", "origin/master", "main", "master"):
        mb = _git(["merge-base", "HEAD", candidate])
        if mb:
            return mb.strip()
    # Fall back to the previous commit, then the empty tree (first commit).
    if _git(["rev-parse", "HEAD~1"]) is not None:
        return "HEAD~1"
    return _git(["hash-object", "-t", "tree", "/dev/null"]).strip() if _git(["rev-parse", "HEAD"]) else None


def changed_lines(base):
    """Return ({abs_path: set_of_changed_line_numbers or None (=all lines)},
    {abs_path: base_side_relative_path or None},
    {abs_path: [(old_start, old_count, new_start, new_count), ...]}),
    or (None, None, None) if git diff itself failed (e.g. unknown base ref).
    The base-side path (the `---` line) differs from the current path for
    renames and is None for new files — it is what `git show base:path` needs
    for baseline analysis; the hunks map unchanged lines between revisions."""
    changed, rels, hunks = {}, {}, {}
    # core.quotePath=false keeps non-ASCII filenames literal instead of
    # octal-escaped+quoted, so the paths resolve on the filesystem.
    diff = _git(["-c", "core.quotePath=false", "diff", "--unified=0", "--no-color", base, "--", "*.py"])
    if diff is None:
        return None, None, None
    if diff:
        current = None
        old = None
        pending_rename_from = None
        for line in diff.splitlines():
            if line.startswith("rename from "):
                pending_rename_from = line[len("rename from "):].strip()
            elif line.startswith("rename to ") and pending_rename_from is not None:
                # A 100% rename has no ---/+++/hunk lines, but the path change
                # itself can matter (e.g. a file leaving tests/ becomes
                # production code). Register it with no changed lines: every
                # finding is then baseline-gated against the old path with an
                # identity line mapping.
                target = line[len("rename to "):].strip()
                if target.endswith(".py"):
                    key = str(Path(target).resolve())
                    changed.setdefault(key, set())
                    rels[key] = pending_rename_from
                    hunks.setdefault(key, [])
                pending_rename_from = None
            elif line.startswith("--- "):
                source = line[4:].strip()
                old = None if source == "/dev/null" else source[2:] if source.startswith("a/") else source
            elif line.startswith("+++ "):
                target = line[4:].strip()
                current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
                if current is not None:
                    key = str(Path(current).resolve())
                    changed.setdefault(key, set())
                    rels[key] = old
                    hunks.setdefault(key, [])
            elif current is not None and line.startswith("@@"):
                m = _HUNK.match(line)
                if m:
                    old_start = int(m.group(1))
                    old_count = int(m.group(2)) if m.group(2) is not None else 1
                    start = int(m.group(3))
                    count = int(m.group(4)) if m.group(4) is not None else 1
                    key = str(Path(current).resolve())
                    hunks[key].append((old_start, old_count, start, count))
                    for ln in range(start, start + count):
                        changed[key].add(ln)
    # Untracked new files: treat every line as changed.
    others = _git(["-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "--", "*.py"])
    if others:
        for rel in others.splitlines():
            rel = rel.strip()
            if rel:
                changed[str(Path(rel).resolve())] = None
                rels[str(Path(rel).resolve())] = None  # untracked: nothing at base
    # Drop files that no longer exist on disk (pure deletions).
    return {f: lines for f, lines in changed.items() if Path(f).exists()}, rels, hunks


# Definition headers whose body intersects the changed lines are accepted
# unconditionally: a finding on a def you edited is review-relevant even if it
# predates the edit. Any OTHER finding in a changed file is baseline-gated —
# reported only when absent at the base revision — because a change can
# introduce findings anchored at arbitrary unchanged lines: the enclosing `if`
# of an edited branch, a temporary field's initializer when a method changes,
# a subclass's method when its base changes.
_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _expand_to_definitions(filepath, lines):
    """Changed lines plus the header line of every definition whose body
    intersects them."""
    accepted = set(lines)
    try:
        tree = ast.parse(Path(filepath).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return accepted
    for node in ast.walk(tree):
        if isinstance(node, _DEF_NODES):
            end = getattr(node, "end_lineno", node.lineno)
            if any(node.lineno <= ln <= end for ln in lines):
                accepted.add(node.lineno)
    return accepted


def _line_mapper(hunks):
    """Map a new-side line to its base-side line (None when the line itself was
    added/modified). An unchanged line is the same construct in both revisions,
    which makes baseline matching structural rather than rank-based."""
    def to_base(n):
        delta = 0
        for old_start, old_count, new_start, new_count in hunks:
            if new_count > 0 and new_start <= n < new_start + new_count:
                return None
            if (new_count == 0 and new_start < n) or (new_count > 0 and new_start + new_count <= n):
                delta += new_count - old_count
        return n - delta
    return to_base


def _baseline_counts(base, rel):
    """Multiset of findings the detectors produce for the base revision of one
    changed file, keyed by (script, type, description, base line). Candidates
    anchored at an unchanged head line are mapped to their base line via the
    diff hunks, so a finding is suppressed only when the *same construct*
    already produced it at base. New files have an empty baseline."""
    counts = Counter()
    if not rel:
        return counts
    content = _git(["show", f"{base}:{rel}"])
    if content is None:
        return counts
    with tempfile.TemporaryDirectory() as td:
        # Recreate the base-side relative path: path-sensitive detectors (test
        # heuristics keyed on tests/ directories) must see the same context in
        # both revisions.
        snapshot = Path(td) / rel
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(content, encoding="utf-8")
        for script in DIFF_SAFE_SCRIPTS:
            issues, error = run_detector(script, str(snapshot))
            if error is not None:
                continue  # no baseline for this script → its findings stay visible (safe direction)
            for issue in issues:
                if isinstance(issue, dict):
                    counts[(script, _type_of(issue), issue.get("description"), issue.get("line"))] += 1
    return counts


def run_detector(script, filepath):
    """Run one detector. Returns (findings, error): error is a short string when
    the detector crashed/timed out/emitted bad JSON — a silent [] would let the
    diff review claim clean for a category that was never actually evaluated."""
    script_path = Path(__file__).parent / script
    if not script_path.exists():
        return [], f"script not found: {script}"
    try:
        r = subprocess.run(
            [sys.executable, str(script_path), filepath, "--format", "json"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return [], str(e)[:200] or "failed to run"
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return [], tail[-1][:200] if tail else f"exit code {r.returncode}"
    if not r.stdout.strip():
        return [], "no output"
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return [], f"bad JSON output: {e}"
    return (data if isinstance(data, list) else data.get("issues", [])), None


def collect(base, all_lines):
    files, rels, hunks_by_file = changed_lines(base)
    if files is None:
        return None, None
    findings = []
    for filepath, lines in files.items():
        file_hunks = hunks_by_file.get(filepath) or []
        # Deletion-only hunks leave nothing on the new side, but can still
        # introduce findings (e.g. removing one branch's distinct final
        # statement). Their neighboring lines discover the affected enclosing
        # definitions — but are NOT accepted themselves: findings sitting on a
        # merely-shifted line stay baseline-gated.
        seeds = set()
        for _, _, new_start, new_count in file_hunks:
            if new_count == 0:
                seeds.update({max(new_start, 1), new_start + 1})
        if lines is None:
            accepted = None
        else:
            accepted = _expand_to_definitions(filepath, lines | seeds) - (seeds - lines)
        to_base = _line_mapper(file_hunks)
        baseline = None  # computed lazily — only when a gated finding appears
        for script in DIFF_SAFE_SCRIPTS:
            issues, error = run_detector(script, filepath)
            if error is not None:
                # Detector failures bypass the changed-line filter: the reader
                # must see that this category was not evaluated for this file.
                findings.append({
                    "file": filepath, "line": 1, "smell_type": "detector_error",
                    "description": f"{script} did not complete ({error}) — its findings for this file are missing",
                    "suggestion": f"Run `python scripts/{script} {filepath}` directly to see the failure.",
                    "severity": "medium",
                })
                continue
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                ln = issue.get("line")
                # Cross-definition findings (e.g. a getter/setter pair, a
                # strategy hierarchy) anchor at one definition but list every
                # participant in related_lines; the finding belongs to the diff
                # when *any* participant changed.
                related = issue.get("related_lines") or []
                if all_lines or accepted is None \
                        or (isinstance(ln, int) and ln in accepted) \
                        or any(isinstance(r, int) and r in accepted for r in related):
                    issue.setdefault("severity", "medium")
                    findings.append(issue)
                elif isinstance(ln, int):
                    # Anywhere else in a changed file: report only what the
                    # change introduced relative to the base revision.
                    if baseline is None:
                        baseline = _baseline_counts(base, rels.get(filepath))
                    base_ln = to_base(ln)
                    key = (script, _type_of(issue), issue.get("description"), base_ln)
                    if base_ln is not None and baseline[key] > 0:
                        baseline[key] -= 1  # same construct already produced this at base
                        continue
                    issue.setdefault("severity", "medium")
                    findings.append(issue)
    # De-dupe identical findings (a line can be flagged by overlapping detectors).
    seen, unique = set(), []
    for f in findings:
        key = (f.get("file"), f.get("line"), f.get("smell_type") or f.get("issue_type"), f.get("description"))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    rank = {"high": 0, "medium": 1, "low": 2}
    unique.sort(key=lambda f: (rank.get(f.get("severity", "medium"), 1), str(f.get("file")), f.get("line", 0)))
    return files, unique


def _type_of(issue):
    for key in ("smell_type", "issue_type", "pattern_type", "type"):
        if issue.get(key):
            return issue[key]
    return "issue"


def print_text(files, findings, base):
    n_files = len(files)
    print(f"\n📋 CR REVIEW — {n_files} changed file(s) vs. {base[:12]}")
    print("=" * 60)
    if not findings:
        print("✅ No findings on the changed lines.")
        return
    by_sev = defaultdict(int)
    for f in findings:
        by_sev[f.get("severity", "medium")] += 1
    print(f"Findings on changed lines: {len(findings)}  "
          f"({_ICON['high']} {by_sev['high']}  {_ICON['medium']} {by_sev['medium']}  {_ICON['low']} {by_sev['low']})\n")
    for f in findings:
        sev = f.get("severity", "medium")
        print(f"{_ICON.get(sev, '')} [{sev.upper()}] {f.get('file', '?')}:{f.get('line', '?')}")
        print(f"   {_type_of(f)}: {f.get('description', '')}")
        if f.get("suggestion"):
            print(f"   → {f['suggestion']}")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Run the file-level detectors against only the changed lines of a diff (CR review lens)",
    )
    parser.add_argument("base", nargs="?", default=None,
                        help="Base ref to diff against (default: merge-base with origin/main, then HEAD~1)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--all-lines", action="store_true",
                        help="Report findings on every line of each changed file, not just changed lines")
    args = parser.parse_args()

    if _git(["rev-parse", "--is-inside-work-tree"]) is None:
        print("Not a git repository (or git unavailable). The diff lens needs git.", file=sys.stderr)
        sys.exit(1)

    base = resolve_base(args.base)
    if not base:
        if args.base:
            print(f"Base ref '{args.base}' does not resolve to a commit.", file=sys.stderr)
        else:
            print("Could not resolve a base ref to diff against.", file=sys.stderr)
        sys.exit(1)

    files, findings = collect(base, args.all_lines)
    if files is None:
        print(f"git diff against '{base}' failed; refusing to report a falsely clean review.", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(findings, indent=2))
    else:
        print_text(files, findings, base)


if __name__ == "__main__":
    main()
