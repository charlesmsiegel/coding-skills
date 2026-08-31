#!/usr/bin/env python3
"""
Review-a-change-request lens: run the file-level detectors against only the
files (and, by default, the added/modified lines) of a diff.

This is the tool for reviewing an AI-written feature or a pull request: you want
findings about *what changed*, not the legacy around it. It runs the per-file
detectors on each changed Rust file and keeps only findings that land on lines
the diff touched.

Whole-tree detectors (Cargo manifest, module graph, dead code, duplication,
over-engineering, untested modules) need the full project to be correct, so they
are deliberately NOT part of the diff lens — run those with analyze_all.py
against the whole repo. Neither is the compiler: `cargo check` has to see the
whole crate, so run it separately through run_external_tools.py.

Usage:
  python analyze_diff.py                 # working tree vs. the merge-base with the default branch
  python analyze_diff.py origin/main     # vs. an explicit base ref
  python analyze_diff.py HEAD~3
  python analyze_diff.py --all-lines     # every line of each changed file
  python analyze_diff.py --format json | python format_findings.py
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from common import SEVERITY_ICONS, configure_output

# Per-file detectors only. Each is meaningful on a single file and reports a
# line within that file.
DIFF_SAFE_SCRIPTS = [
    "find_error_handling.py",
    "find_unsafe_issues.py",
    "find_concurrency_issues.py",
    "find_type_issues.py",
    "find_ownership_issues.py",
    "analyze_complexity.py",
    "find_code_smells.py",
    "find_design_smells.py",
    "find_api_hygiene.py",
    "find_security_issues.py",
    "find_unrustic.py",
    "find_loop_simplifications.py",
    "find_outdated_idioms.py",
    "find_naming_issues.py",
    "find_comment_smells.py",
    "find_debug_leftovers.py",
    "find_ai_scaffolding.py",
    "find_test_smells.py",
]

_ICON = SEVERITY_ICONS
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_PATHSPECS = ("*.rs",)


def _git(args, input_text=None):
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True,
                                timeout=120, input=input_text)
        if result.returncode != 0:
            return None
        return result.stdout
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
        merge_base = _git(["merge-base", "HEAD", candidate])
        if merge_base:
            return merge_base.strip()
    if _git(["rev-parse", "HEAD~1"]) is not None:
        return "HEAD~1"
    if _git(["rev-parse", "HEAD"]) is None:
        return None
    # mktree on empty stdin yields the empty-tree id portably.
    empty_tree = _git(["mktree"], input_text="")
    return empty_tree.strip() if empty_tree else None


def changed_lines(base):
    """({abs_path: changed lines or None}, {abs_path: base-side path}, {abs_path: hunks}).

    (None, None, None) when git diff itself failed. The base-side path differs
    for renames and is None for new files; it is what `git show base:path` needs
    for the baseline pass, and the hunks map unchanged lines between revisions.
    """
    changed, base_paths, hunks = {}, {}, {}
    diff = _git(["-c", "core.quotePath=false", "diff", "--unified=0", "--no-color",
                 base, "--", *_PATHSPECS])
    if diff is None:
        return None, None, None
    current, old, pending_rename = None, None, None
    for line in diff.splitlines():
        if line.startswith("rename from "):
            pending_rename = line[len("rename from "):].strip()
        elif line.startswith("rename to ") and pending_rename is not None:
            target = line[len("rename to "):].strip()
            if target.endswith(".rs"):
                key = str(Path(target).resolve())
                changed.setdefault(key, set())
                base_paths[key] = pending_rename
                hunks.setdefault(key, [])
            pending_rename = None
        elif line.startswith("--- "):
            source = line[4:].strip()
            old = None if source == "/dev/null" else source[2:] if source.startswith("a/") else source
        elif line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if current is not None:
                key = str(Path(current).resolve())
                changed.setdefault(key, set())
                base_paths[key] = old
                hunks.setdefault(key, [])
        elif current is not None and line.startswith("@@"):
            match = _HUNK.match(line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2)) if match.group(2) is not None else 1
                start = int(match.group(3))
                count = int(match.group(4)) if match.group(4) is not None else 1
                key = str(Path(current).resolve())
                hunks[key].append((old_start, old_count, start, count))
                changed[key].update(range(start, start + count))

    others = _git(["-c", "core.quotePath=false", "ls-files", "--others",
                   "--exclude-standard", "--", *_PATHSPECS])
    if others:
        for relative in others.splitlines():
            relative = relative.strip()
            if relative:
                key = str(Path(relative).resolve())
                changed[key] = None  # untracked: treat every line as changed
                base_paths[key] = None
    return {f: lines for f, lines in changed.items() if Path(f).exists()}, base_paths, hunks


def _expand_to_definitions(filepath, lines):
    """Changed lines plus the header line of every item whose body intersects them.

    A finding on a function you edited is review-relevant even when it predates
    the edit, because you are the last person to have read it.
    """
    accepted = set(lines)
    try:
        from rsparse import RustSyntaxError, parse_file
        rsfile = parse_file(Path(filepath))
    except (OSError, ValueError, ImportError):
        return accepted
    except RustSyntaxError:
        return accepted
    spans = [(f.line, rsfile.line_of(f.body_close) if f.has_body else f.line)
             for f in rsfile.functions]
    spans += [(t.line, rsfile.line_of(t.body_close) if t.body_close > 0 else t.line)
              for t in rsfile.types]
    spans += [(t.line, rsfile.line_of(t.body_close)) for t in rsfile.traits]
    spans += [(b.line, rsfile.line_of(b.body_close)) for b in rsfile.impls]
    for start, end in spans:
        if any(start <= line <= end for line in lines):
            accepted.add(start)
    return accepted


def _line_mapper(hunks):
    """Map a new-side line to its base-side line (None when the line changed)."""
    def to_base(number):
        delta = 0
        for old_start, old_count, new_start, new_count in hunks:
            if new_count > 0 and new_start <= number < new_start + new_count:
                return None
            if (new_count == 0 and new_start < number) or (new_count > 0 and new_start + new_count <= number):
                delta += new_count - old_count
        return number - delta
    return to_base


def _baseline_counts(base, relative):
    """Findings the detectors produce for the base revision of one changed file.

    Keyed by (script, type, description, base line) so a finding is suppressed
    only when the *same construct* already produced it before the change.
    """
    counts = Counter()
    if not relative:
        return counts
    content = _git(["show", f"{base}:{relative}"])
    if content is None:
        return counts
    with tempfile.TemporaryDirectory() as directory:
        # Recreate the base-side relative path: path-sensitive detectors (the
        # test heuristics) must see the same context in both revisions.
        snapshot = Path(directory) / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(content, encoding="utf-8")
        for script in DIFF_SAFE_SCRIPTS:
            issues, error = run_detector(script, str(snapshot))
            if error is not None:
                continue  # no baseline for this script → its findings stay visible
            for issue in issues:
                if isinstance(issue, dict):
                    counts[(script, issue.get("smell_type"), issue.get("description"),
                            issue.get("line"))] += 1
    return counts


def run_detector(script, filepath):
    """Run one detector. Returns (findings, error).

    A silent [] on failure would let the review claim clean for a category that
    was never evaluated, so failures are surfaced rather than swallowed.
    """
    script_path = Path(__file__).parent / script
    if not script_path.exists():
        return [], f"script not found: {script}"
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), filepath, "--format", "json"],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return [], str(exc)[:200] or "failed to run"
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return [], tail[-1][:200] if tail else f"exit code {result.returncode}"
    if not result.stdout.strip():
        return [], "no output"
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], f"bad JSON output: {exc}"
    return (data if isinstance(data, list) else data.get("issues", [])), None


def collect(base, all_lines, scripts=None):
    scripts = scripts if scripts is not None else DIFF_SAFE_SCRIPTS
    files, base_paths, hunks_by_file = changed_lines(base)
    if files is None:
        return None, None
    findings = []
    for filepath, lines in files.items():
        file_hunks = hunks_by_file.get(filepath) or []
        # Deletion-only hunks leave nothing on the new side but can still
        # introduce findings. Their neighbouring lines discover the affected
        # items without being accepted themselves.
        seeds = set()
        for _, _, new_start, new_count in file_hunks:
            if new_count == 0:
                seeds.update({max(new_start, 1), new_start + 1})
        accepted = None if lines is None else _expand_to_definitions(filepath, lines | seeds) - (seeds - lines)
        to_base = _line_mapper(file_hunks)
        baseline = None  # computed lazily, only when a gated finding appears
        for script in scripts:
            issues, error = run_detector(script, filepath)
            if error is not None:
                findings.append({
                    "file": filepath, "line": 1, "smell_type": "detector_error",
                    "description": f"{script} did not complete ({error}) — its findings for this "
                                   "file are missing",
                    "suggestion": f"Run `python {shlex.quote(str(Path(__file__).parent / script))} "
                                  f"{shlex.quote(filepath)}` directly to see the failure.",
                    "severity": "medium",
                })
                continue
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                line = issue.get("line")
                related = issue.get("related_lines") or []
                if all_lines or accepted is None \
                        or (isinstance(line, int) and line in accepted) \
                        or any(isinstance(r, int) and r in accepted for r in related):
                    issue.setdefault("severity", "medium")
                    findings.append(issue)
                elif isinstance(line, int):
                    # Elsewhere in a changed file: report only what the change
                    # introduced relative to the base revision.
                    if baseline is None:
                        baseline = _baseline_counts(base, base_paths.get(filepath))
                    base_line = to_base(line)
                    key = (script, issue.get("smell_type"), issue.get("description"), base_line)
                    if base_line is not None and baseline[key] > 0:
                        baseline[key] -= 1
                        continue
                    issue.setdefault("severity", "medium")
                    findings.append(issue)

    seen, unique = set(), []
    for finding in findings:
        key = (finding.get("file"), finding.get("line"), finding.get("smell_type"),
               finding.get("description"))
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    rank = {"high": 0, "medium": 1, "low": 2}
    unique.sort(key=lambda f: (rank.get(f.get("severity", "medium"), 1), str(f.get("file")), f.get("line", 0)))
    return files, unique


def print_text(files, findings, base):
    print(f"\n📋 CR REVIEW — {len(files)} changed file(s) vs. {base[:12]}")
    print("=" * 60)
    if not findings:
        print("✅ No findings on the changed lines.")
        print("   The compiler was not run: `cargo check` needs the whole crate. Run")
        print("   run_external_tools.py separately before calling the change clean.")
        return
    by_severity = defaultdict(int)
    for finding in findings:
        by_severity[finding.get("severity", "medium")] += 1
    print(f"Findings on changed lines: {len(findings)}  "
          f"({_ICON['high']} {by_severity['high']}  {_ICON['medium']} {by_severity['medium']}  "
          f"{_ICON['low']} {by_severity['low']})\n")
    for finding in findings:
        severity = finding.get("severity", "medium")
        print(f"{_ICON.get(severity, '')} [{severity.upper()}] "
              f"{finding.get('file', '?')}:{finding.get('line', '?')}")
        print(f"   {finding.get('smell_type', 'issue')}: {finding.get('description', '')}")
        if finding.get("suggestion"):
            print(f"   → {finding['suggestion']}")
        print()


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Run the file-level detectors against only the changed lines of a diff",
    )
    parser.add_argument("base", nargs="?", default=None,
                        help="Base ref (default: merge-base with origin/main, then HEAD~1)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--all-lines", action="store_true",
                        help="Report findings on every line of each changed file")
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
        print(f"git diff against '{base}' failed; refusing to report a falsely clean review.",
              file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(findings, indent=2))
    else:
        print_text(files, findings, base)


if __name__ == "__main__":
    main()
