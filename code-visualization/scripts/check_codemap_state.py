#!/usr/bin/env python3
"""Check whether docs/codemap.html is trustworthy, and how stale it is.

Usage: python3 check_codemap_state.py REPO_DIR [--codemap docs/codemap.html]

Verdicts (JSON on stdout):
  missing                  — no codemap; nothing to update
  conflict-markers         — file contains <<<<<<< / ======= / >>>>>>> ; a merge
                             conflict was committed unresolved. The file is
                             corrupt: rebuild, do not extract-and-revise.
  merge-resolution-suspect — the last commit that touched the codemap is a merge
                             commit. Whether it was a hand-resolved conflict or a
                             silent auto-merge, the judgment tabs may be a splice
                             of two branches' revisions that nobody reviewed as a
                             whole: update with full re-verification (or rebuild).
  stale                    — meta sha resolves and commits have landed since
  current                  — meta sha == HEAD
  unknown-vintage          — no parseable sha in the meta line

Exit code 0 for current, 2 for missing, 1 for everything needing action.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import git  # noqa: E402

MARKER_RE = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--codemap", default="docs/codemap.html")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    rel = args.codemap
    path = repo / rel
    out = {"codemap": rel}

    if not path.is_file():
        out["verdict"] = "missing"
        print(json.dumps(out, indent=2))
        return 2

    html = path.read_text(encoding="utf-8", errors="replace")
    if MARKER_RE.search(html):
        out["verdict"] = "conflict-markers"
        out["detail"] = "file contains committed merge-conflict markers; extraction would recover garbage — rebuild the codemap"
        print(json.dumps(out, indent=2))
        return 1

    # last commit touching the codemap; merge commit => suspect splice
    try:
        last = git(repo, "log", "--full-history", "-1", "--format=%H %P %cs %s", "--", rel).strip()
    except Exception as e:
        last = ""
        out["git_note"] = f"history unavailable: {e}"
    merge_last = False
    if last:
        parts = last.split()
        sha, rest = parts[0], parts[1:]
        parents = [p for p in rest if re.fullmatch(r"[0-9a-f]{40}", p)]
        merge_last = len(parents) >= 2
        out["last_commit_touching_codemap"] = {
            "sha": sha[:12], "is_merge": merge_last,
            "line": last[:160],
        }
        if merge_last:
            for i, p in enumerate(parents, 1):
                try:
                    ours_theirs = git(repo, "cat-file", "-e", f"{p}:{rel}", check=False)
                    out["last_commit_touching_codemap"][f"parent{i}"] = p[:12]
                except Exception:
                    pass

    # vintage from meta line
    meta = re.search(r'<div class="doc-meta">(.*?)</div>', html, re.S)
    meta_text = re.sub(r"\s+", " ", meta.group(1)).strip() if meta else ""
    out["meta"] = meta_text[:200]
    meta_sha = None
    for cand in SHA_RE.findall(meta_text):
        try:
            git(repo, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
            meta_sha = cand
            break
        except Exception:
            continue
    head = git(repo, "rev-parse", "--short", "HEAD").strip()
    out["head"] = head
    if meta_sha:
        out["meta_sha"] = meta_sha
        behind = git(repo, "rev-list", "--count", f"{meta_sha}..HEAD").strip()
        out["commits_since_codemap"] = int(behind)
        try:
            names = [l for l in git(repo, "diff", "--name-only", f"{meta_sha}..HEAD").splitlines() if l.strip()]
            real = [n for n in names if not re.match(r"^docs/(codemap\.html|pr-[^/]+\.html)$", n)]
            out["files_changed_since"] = len(real)
            out["generated_docs_changed_since"] = len(names) - len(real)
        except Exception:
            pass

    if merge_last:
        out["verdict"] = "merge-resolution-suspect"
        out["detail"] = ("codemap was last changed by a merge commit — its judgment tabs may splice two "
                        "branches' independent revisions; update with full citation re-verification, "
                        "reconciling both parents' versions, or rebuild")
        rc = 1
    elif meta_sha is None:
        out["verdict"] = "unknown-vintage"
        rc = 1
    elif out.get("files_changed_since", out.get("commits_since_codemap", 0)) == 0:
        out["verdict"] = "current"
        rc = 0
    else:
        out["verdict"] = "stale"
        rc = 1
    print(json.dumps(out, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
