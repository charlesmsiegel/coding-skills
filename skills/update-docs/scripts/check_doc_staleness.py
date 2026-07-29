#!/usr/bin/env python3
"""Find what a project's documentation skill now gets wrong.

Regenerating documentation from scratch every time is expensive and throws away
judgment that was correct. The cheaper question is *what actually went stale*, and
most of that is mechanically checkable:

  - a cited path that no longer exists (the file moved or was deleted);
  - a `path:line` citation pointing past the end of the file;
  - a cited file that has changed since the docs were last written;
  - a directory that has churned hard while the docs never mention it at all.

The last one is the interesting check: missing documentation is invisible by
construction, so it has to be inferred from where the work is happening.

Findings use the same shape as the other skills' detectors
(file/line/smell_type/description/suggestion/severity), so `--format json` is
consumable by the same tooling.

Usage:
  python check_doc_staleness.py                       # .claude/skills/documentation vs .
  python check_doc_staleness.py --docs docs/ --repo .
  python check_doc_staleness.py --format json
"""

import re
import sys
import json
import argparse
import contextlib
import subprocess
from pathlib import Path

SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".nox",
    "build", "dist", ".eggs", "site-packages", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "htmlcov", ".idea", ".vscode",
})

# A citation is a backticked span, a markdown link target, or a bare token that
# looks like a repo path — optionally with :line or :line-line. Markdown links
# matter because a directory reference, `[the skill](skills/foo/)`, carries no
# file extension and so is invisible to the bare-token pattern.
_CITATION_RE = re.compile(
    r"`([^`\n]+?)`"                                     # `src/api/client.py:88`
    r"|\]\(([^)\s]+)\)"                                 # [text](skills/foo/)
    r"|(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]\w{0,4})(?::\d+)?(?![\w/])"
)
_LINE_SUFFIX_RE = re.compile(r"^(.*?):(\d+)(?:-\d+)?$")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://|^www\.|^#|^mailto:", re.IGNORECASE)
# Placeholders, shell expansions, globs, and paths rooted outside the repo. Each
# of these is a guaranteed non-match, and a detector nobody trusts gets ignored.
_NOT_A_REPO_PATH = ("<", ">", "{", "}", "*", "$", "|")


def configure_output():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _finding(file, line, smell_type, description, suggestion, severity):
    return {"file": str(file), "line": line, "smell_type": smell_type,
            "description": description, "suggestion": suggestion, "severity": severity}


# ---- git (thin; everything below it is pure) ------------------------------- #

def _git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def docs_last_updated(repo, docs_dir):
    """ISO timestamp of the last commit touching the docs, or None."""
    out = _git(repo, "log", "-1", "--format=%cI", "--", str(docs_dir))
    return (out or "").strip() or None


def changed_since(repo, timestamp):
    """Repo-relative paths committed since `timestamp`."""
    if not timestamp:
        return set()
    out = _git(repo, "log", f"--since={timestamp}", "--name-only", "--pretty=format:")
    if out is None:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# ---- pure analysis ---------------------------------------------------------- #

def parse_citations(text):
    """Every (path, line_or_None) a doc page claims to point at."""
    found = []
    for match in _CITATION_RE.finditer(text):
        raw = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not raw or _URL_RE.match(raw):
            continue
        line = None
        if suffix := _LINE_SUFFIX_RE.match(raw):
            raw, line = suffix.group(1), int(suffix.group(2))
        # Backticks hold plenty of things that are not paths: commands, flags,
        # symbols, types. Require a separator and no whitespace.
        if "/" not in raw or any(c.isspace() for c in raw) or raw.startswith("-"):
            continue
        # Rooted outside the repo (~/.claude/skills/, /etc/hosts) or not a literal
        # path at all — checking these against the tree only manufactures noise.
        if raw.startswith(("~", "/")) or any(c in raw for c in _NOT_A_REPO_PATH):
            continue
        found.append((raw.lstrip("./"), line))
    return found


def build_path_index(repo, max_entries=200_000):
    """Every repo-relative path (files and directories), posix-style.

    Docs cite paths relative to whatever they were describing at the time —
    `scripts/` inside a section about one skill, `assets/template.html` where the
    skill directory is understood. Checking those against the repo root alone
    reports half a real README as broken. The question a reader cares about is
    "does this thing exist", so the index supports suffix resolution.
    """
    index = set()
    stack = [repo]
    while stack and len(index) < max_entries:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in EXCLUDE_DIRS:
                continue
            index.add(entry.relative_to(repo).as_posix())
            if entry.is_dir():
                stack.append(entry)
    return index


def resolve(cited, index, page_dir=None):
    """The repo-relative path a citation refers to, or None if nothing matches."""
    cited = cited.rstrip("/")
    if not cited:
        return None
    if cited in index:
        return cited
    # Relative to the page that cites it.
    if page_dir:
        candidate = f"{page_dir}/{cited}".lstrip("/")
        if candidate in index:
            return candidate
    # A unique-enough suffix: `assets/template.html` naming
    # `skills/code-visualization/assets/template.html`.
    suffix = "/" + cited
    matches = [p for p in index if p.endswith(suffix)]
    if matches:
        return min(matches, key=len)
    return None


def top_churn_dirs(changed, limit=10):
    """Directories with the most committed files, most-changed first."""
    counts = {}
    for path in changed:
        parts = Path(path).parts
        if not parts or EXCLUDE_DIRS.intersection(parts):
            continue
        # Group at two levels deep: "src" is too coarse to act on, a leaf too fine.
        key = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def analyze(repo, docs_dir, doc_pages, changed, last_updated, index=None):
    """doc_pages: [(page_path, text)]. Returns a flat findings list."""
    findings = []
    cited = set()
    index = build_path_index(repo) if index is None else index

    for page, text in doc_pages:
        page_dir = None  # a docs dir outside the repo has no page-relative root
        with contextlib.suppress(ValueError):
            page_dir = Path(page).resolve().parent.relative_to(repo).as_posix()
        for raw, line in parse_citations(text):
            resolved = resolve(raw, index, page_dir)
            if resolved is None:
                findings.append(_finding(
                    page, 1, "missing_path",
                    f"cites `{raw}`, which matches nothing in the tree",
                    "Find where it moved to and update the reference, or drop the claim.",
                    "high"))
                continue
            cited.add(resolved)
            target = repo / resolved
            if line is not None and target.is_file():
                try:
                    total = sum(1 for _ in target.open(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if line > total:
                    findings.append(_finding(
                        page, 1, "citation_past_eof",
                        f"cites `{raw}:{line}` but {resolved} has {total} lines",
                        "Re-locate the thing being cited; line numbers drift with every edit.",
                        "high"))
            if resolved in changed:
                findings.append(_finding(
                    page, 1, "changed_since_documented",
                    f"cites `{raw}`, which has been committed to since the docs were written",
                    "Re-read the file and confirm the description still holds.",
                    "medium"))

    # Churn the docs never mention at all — the gap you cannot see by reading them.
    for directory, count in top_churn_dirs(changed):
        if not any(c == directory or c.startswith(directory + "/") for c in cited):
            findings.append(_finding(
                docs_dir, 1, "undocumented_churn",
                f"`{directory}/` has {count} changed file(s) since the docs were written "
                f"and no doc page mentions it",
                "Either document it or confirm it is genuinely out of scope.",
                "medium" if count >= 5 else "low"))

    if last_updated is None:
        findings.append(_finding(
            docs_dir, 1, "never_committed",
            "no commit touches the documentation — its age cannot be established",
            "Commit the docs so staleness is measurable from here on.",
            "low"))

    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (rank[f["severity"]], f["file"], f["smell_type"]))
    return findings


def read_doc_pages(docs_dir):
    if not docs_dir.exists():
        return []
    if docs_dir.is_file():
        return [(docs_dir, docs_dir.read_text(encoding="utf-8", errors="replace"))]
    pages = []
    for page in sorted(docs_dir.rglob("*.md")):
        if EXCLUDE_DIRS.isdisjoint(page.parts):
            pages.append((page, page.read_text(encoding="utf-8", errors="replace")))
    return pages


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Report what a documentation skill now gets wrong")
    parser.add_argument("--docs", default=".claude/skills/documentation",
                        help="Documentation directory or file (default: .claude/skills/documentation)")
    parser.add_argument("--repo", default=".", help="Repository root (default: .)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    docs_dir = (repo / args.docs) if not Path(args.docs).is_absolute() else Path(args.docs)

    pages = read_doc_pages(docs_dir)
    if not pages:
        print(f"No documentation found at {docs_dir} — nothing to check against; "
              f"this is a first-time write, not a refresh.", file=sys.stderr)
        print("[]" if args.format == "json" else "")
        return 0

    last_updated = docs_last_updated(repo, docs_dir)
    findings = analyze(repo, docs_dir, pages, changed_since(repo, last_updated), last_updated)

    if args.format == "json":
        print(json.dumps(findings, indent=2))
        return 0

    print(f"\n📄 DOC STALENESS — {len(pages)} page(s) under {docs_dir}")
    print(f"last documented: {last_updated or 'never committed'}")
    print("=" * 66)
    if not findings:
        print("✅ Every citation resolves and nothing cited has changed since.")
        return 0
    for f in findings:
        page = Path(f["file"]).name
        print(f"{SEVERITY_ICONS[f['severity']]} [{f['severity'].upper()}] {page}  {f['smell_type']}")
        print(f"   {f['description']}")
        print(f"   → {f['suggestion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
