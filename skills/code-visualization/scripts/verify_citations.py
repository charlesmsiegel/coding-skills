#!/usr/bin/env python3
"""Verify file/line citations in judgment-tab fragments against the actual repo.

Usage: python3 verify_citations.py REPO_DIR --tabs-dir TABS_DIR [--fragments 01,05,06,...]

Scans fragments for <code>...</code> tokens that look like file citations:
    <code>src/click/core.py</code>
    <code>core.py:1477</code>
    <code>core.py:1528-1532</code>  (also en-dash ranges)
    <code>core.py:1422,1617,2095</code>
Checks that each cited file exists (resolving bare filenames by unique suffix
match) and that cited line numbers are in range, and prints the current content
of every cited line so the claim can be compared against reality.

Prints a JSON report to stdout. Exit code: 0 all resolved, 1 if any citation is
broken (missing file, ambiguous name, or line out of range).

This is a citation checker, not a fact checker: a citation can resolve while
the prose around it is wrong, and line content drifts when code moves. Read the
reported line content and judge whether it still supports the claim.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from common import LANG_BY_EXT, walk_source

KNOWN_EXTS = set(LANG_BY_EXT) | {".txt", ".cfg", ".ini", ".lock", ".gradle", ".mod", ".sum",
                                 ".graphql", ".env", ".properties", ".adoc", ".xml", ".sql"}

CODE_RE = re.compile(r"<code>([^<]{1,220})</code>")
CITE_RE = re.compile(
    r"^(?P<path>[\w./\-]+\.[A-Za-z][\w]{0,9})"
    r"(?::(?P<lines>\d[\d,\s]*(?:[-\u2013\u2014]\d+)?(?:,\s*\d+(?:[-\u2013\u2014]\d+)?)*))?/?$"
)
# Well-known extensionless files, citable with the same :lines syntax.
EXTLESS_RE = re.compile(
    r"^(?P<path>(?:[\w./\-]+/)?(?:Makefile|Dockerfile|Justfile|Rakefile|Gemfile|Procfile|Vagrantfile|Caddyfile|Jenkinsfile))"
    r"(?::(?P<lines>\d[\d,\s]*(?:[-\u2013\u2014]\d+)?(?:,\s*\d+(?:[-\u2013\u2014]\d+)?)*))?$"
)
# A token that was probably MEANT as a file:line citation but doesn't parse \u2014
# reported as unverified instead of silently dropped, so exit 0 can't overstate
# what was actually checked.
LOOKS_CITEY_RE = re.compile(r"^\S+\.[A-Za-z]\w{0,9}\s*(?:#L|@|:{2}|\sL)\s*\d+", re.I)


def parse_lines(spec):
    out = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)[-\u2013\u2014](\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.append((min(a, b), max(a, b)))
        elif part.isdigit():
            out.append((int(part), int(part)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--fragments", default=None,
                    help="comma-separated fragment number prefixes to check (default: all)")
    ap.add_argument("--context", type=int, default=0, help="context lines to show around each cited line")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    tabs = Path(args.tabs_dir)

    # index repo files for suffix resolution
    all_files = [rel for rel, _ in walk_source(repo)]
    by_name = defaultdict(list)
    for rel in all_files:
        parts = rel.split("/")
        for i in range(len(parts)):
            by_name["/".join(parts[i:])].append(rel)

    def resolve(path):
        if (repo / path).is_file():
            return path, "ok"
        cands = by_name.get(path, [])
        if len(cands) == 1:
            return cands[0], "resolved-by-suffix"
        if len(cands) > 1:
            # prefer real source over examples/tests/docs copies of the same name
            preferred = [c for c in cands
                         if not re.search(r"(^|/)(examples?|tests?|docs?|samples?)(/|$)", c)]
            if len(preferred) == 1:
                return preferred[0], f"resolved-by-suffix (chose over {len(cands)-1} test/example match(es))"
            shown = ", ".join(cands[:4]) + ("…" if len(cands) > 4 else "")
            return None, f"ambiguous ({len(cands)} matches: {shown})"
        return None, "missing"

    wanted = None
    if args.fragments:
        wanted = {w.strip().zfill(2) for w in args.fragments.split(",")}

    line_cache = {}

    def file_lines(rel):
        if rel not in line_cache:
            try:
                line_cache[rel] = (repo / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                line_cache[rel] = []
        return line_cache[rel]

    report, skipped, broken, ok_count = [], [], 0, 0
    frags = sorted(p for p in tabs.glob("*.html") if p.is_file())
    for frag in frags:
        if wanted and frag.name[:2] not in wanted:
            continue
        text = frag.read_text(encoding="utf-8", errors="replace")
        seen = set()
        for m in CODE_RE.finditer(text):
            token = m.group(1).strip()
            key = (frag.name, token)
            if key in seen:
                continue
            cm = CITE_RE.match(token) or EXTLESS_RE.match(token)
            if not cm:
                if LOOKS_CITEY_RE.match(token):
                    seen.add(key)
                    skipped.append({"fragment": frag.name, "citation": token,
                                    "reason": "unrecognized citation format (use path/file.ext:LINE)"})
                continue
            seen.add(key)
            path = cm.group("path").rstrip("/")
            suffix = Path(path).suffix.lower()
            if suffix and suffix not in KNOWN_EXTS:
                if "/" in path or cm.group("lines"):
                    # Meant as a citation (has a dir or line numbers) but the
                    # extension is unknown — unverified, not silently ignored.
                    skipped.append({"fragment": frag.name, "citation": token,
                                    "reason": f"unverified: extension {suffix} not in KNOWN_EXTS"})
                continue  # dotted identifier (sys.argv, Context.scope), not a file
            resolved, status = resolve(path)
            entry = {"fragment": frag.name, "citation": token, "path": path,
                     "resolved": resolved, "status": status}
            if resolved is None:
                broken += 1
                report.append(entry)
                continue
            lines_spec = cm.group("lines")
            if lines_spec:
                content = []
                lines = file_lines(resolved)
                for a, b in parse_lines(lines_spec):
                    if b > len(lines) or a < 1:
                        entry["status"] = f"line-out-of-range (file has {len(lines)} lines)"
                        broken += 1
                        break
                    lo = max(1, a - args.context)
                    hi = min(len(lines), b + args.context)
                    span = lines[lo - 1: hi]
                    shown = [ln.rstrip()[:160] for ln in span[:8]]
                    # The marker must appear whenever lines were omitted — a
                    # silently truncated span reads as the whole citation.
                    content.append({"lines": f"{a}" if a == b else f"{a}-{b}",
                                    "content": shown + (["…"] if len(span) > 8 else [])})
                entry["cited_content"] = content
            if not entry["status"].startswith("line-out"):
                ok_count += 1
            report.append(entry)

    summary = {
        "checked_fragments": [f.name for f in frags if not wanted or f.name[:2] in wanted],
        "citations_ok": ok_count,
        "citations_broken": broken,
        "citations_skipped": len(skipped),
        "note": "A resolving citation is necessary, not sufficient: read cited_content and confirm it still supports the claim. Line content shifts when code moves even if the line number stays valid. citations_skipped counts tokens that look like citations but were NOT verified — rewrite them in path/file.ext:LINE form if they are real.",
        "citations": report,
        "skipped": skipped,
    }
    print(json.dumps(summary, indent=2))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
