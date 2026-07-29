#!/usr/bin/env python3
"""Collect every actionable piece of feedback on a pull request in one call.

`gh pr view --comments` shows review *bodies* but not the inline comments, and the
inline comments are where the actual requests live. Worse, neither view says which
threads are already resolved or have gone stale — so a naive pass re-litigates
settled discussions and burns the author's goodwill.

This fetches both halves plus CI status and normalizes them into one structure,
sorted so the threads that still need an answer come first:

  {pr, checks, reviews, threads, summary}

Each thread carries its file and line, whether it is resolved or outdated, the full
comment chain, and any GitHub ``suggestion`` block (a reviewer-authored patch that
can be applied verbatim).

Usage:
  python fetch_pr_feedback.py                     # the current branch's PR
  python fetch_pr_feedback.py 42                  # a specific PR
  python fetch_pr_feedback.py 42 --repo owner/name
  python fetch_pr_feedback.py --format json       # for tooling
  python fetch_pr_feedback.py --all               # include resolved/outdated threads

Requires the `gh` CLI, authenticated. Read-only: it never posts, resolves, or edits.
"""

import re
import sys
import json
import shutil
import argparse
import contextlib
import subprocess

PR_FIELDS = ("number,title,state,url,author,body,reviewDecision,reviews,"
             "statusCheckRollup,headRefName,baseRefName,isDraft")

# reviewThreads is GraphQL-only: the REST comments endpoint has no isResolved.
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved isOutdated path line originalLine diffSide
          comments(first: 50) {
            nodes { author { login } body createdAt url }
          }
        }
      }
    }
  }
}
"""

_SUGGESTION_RE = re.compile(r"```suggestion\r?\n(.*?)```", re.DOTALL)


def configure_output():
    """Keep non-ASCII review text from crashing a narrow console encoding."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


class GhError(RuntimeError):
    """gh could not answer — missing, unauthenticated, or the PR does not exist."""


def _gh(*args, stdin_text=None):
    if not shutil.which("gh"):
        raise GhError("the GitHub CLI (gh) is not installed — see https://cli.github.com")
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, input=stdin_text)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhError(f"gh {' '.join(args[:2])} failed to run: {e}") from e
    if r.returncode != 0:
        raise GhError((r.stderr or r.stdout or f"gh exited {r.returncode}").strip()[:400])
    return r.stdout


# ---- fetch layer (thin; everything below it is pure) ----------------------- #

def fetch(pr=None, repo=None):
    """Return the raw gh payloads: (pr_view, [thread, ...])."""
    scope = ["--repo", repo] if repo else []
    pr_view = json.loads(_gh("pr", "view", *([str(pr)] if pr else []), *scope, "--json", PR_FIELDS))

    owner_name = repo or json.loads(_gh("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
    owner, _, name = owner_name.partition("/")

    threads, cursor = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}",
                "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={pr_view['number']}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        page = json.loads(_gh(*args))["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(page["nodes"])
        # A PR with hundreds of threads is real; an unbounded loop on a bad cursor is not.
        if not page["pageInfo"]["hasNextPage"] or len(threads) >= 500:
            break
        cursor = page["pageInfo"]["endCursor"]
    return pr_view, threads


# ---- normalize layer (pure; this is what the tests pin) -------------------- #

def _login(node):
    # author is null for a deleted account, and for some bot integrations.
    return ((node or {}).get("author") or {}).get("login") or "(unknown)"


def normalize_thread(raw):
    comments = [
        {"author": _login(c), "body": (c.get("body") or "").strip(),
         "created_at": c.get("createdAt"), "url": c.get("url")}
        for c in ((raw.get("comments") or {}).get("nodes") or [])
    ]
    suggestions = [m.group(1) for c in comments for m in _SUGGESTION_RE.finditer(c["body"])]
    return {
        # line is null once a thread goes stale; originalLine still points at the
        # code the reviewer was actually looking at.
        "path": raw.get("path"),
        "line": raw.get("line") or raw.get("originalLine"),
        "resolved": bool(raw.get("isResolved")),
        "outdated": bool(raw.get("isOutdated")),
        "side": raw.get("diffSide"),
        "url": comments[0]["url"] if comments else None,
        "participants": sorted({c["author"] for c in comments}),
        "comments": comments,
        "suggestions": suggestions,
        "open": not raw.get("isResolved"),
    }


def _thread_order(t):
    # Unresolved and current first — that is the work. Outdated-but-open next,
    # because it may still be a live request against moved code. Resolved last.
    return (t["resolved"], t["outdated"], t["path"] or "", t["line"] or 0)


def normalize_checks(rollup):
    failing, pending, passing = [], [], 0
    for c in rollup or []:
        name = c.get("name") or c.get("context") or "(unnamed check)"
        url = c.get("detailsUrl") or c.get("targetUrl")
        # CheckRun uses status/conclusion; the older StatusContext uses state.
        conclusion = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if status and status != "COMPLETED" and not conclusion:
            pending.append({"name": name, "url": url})
        elif conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"):
            failing.append({"name": name, "url": url, "conclusion": conclusion})
        elif conclusion in ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"):
            pending.append({"name": name, "url": url})
        else:
            passing += 1
    return {"failing": failing, "pending": pending, "passing_count": passing}


def normalize(pr_view, threads, include_all=False):
    normalized = sorted((normalize_thread(t) for t in threads), key=_thread_order)
    actionable = [t for t in normalized if t["open"]]
    shown = normalized if include_all else actionable

    reviews = [
        {"author": _login(r), "state": r.get("state"),
         "body": (r.get("body") or "").strip(), "submitted_at": r.get("submittedAt")}
        for r in (pr_view.get("reviews") or [])
        if (r.get("body") or "").strip() or r.get("state") in ("CHANGES_REQUESTED", "APPROVED")
    ]

    return {
        "pr": {
            "number": pr_view.get("number"),
            "title": pr_view.get("title"),
            "state": pr_view.get("state"),
            "url": pr_view.get("url"),
            "author": _login(pr_view),
            "branch": pr_view.get("headRefName"),
            "base": pr_view.get("baseRefName"),
            "draft": bool(pr_view.get("isDraft")),
            "decision": pr_view.get("reviewDecision"),
        },
        "checks": normalize_checks(pr_view.get("statusCheckRollup")),
        "reviews": reviews,
        "threads": shown,
        "summary": {
            "open_threads": len(actionable),
            "resolved_threads": sum(1 for t in normalized if t["resolved"]),
            "outdated_threads": sum(1 for t in normalized if t["outdated"] and t["open"]),
            "threads_with_suggestions": sum(1 for t in actionable if t["suggestions"]),
            "hidden_threads": 0 if include_all else len(normalized) - len(actionable),
        },
    }


# ---- text rendering -------------------------------------------------------- #

def render(report):
    pr, checks, summary = report["pr"], report["checks"], report["summary"]
    out = [f"\nPR #{pr['number']}  {pr['title']}",
           f"{pr['url']}",
           f"{pr['branch']} → {pr['base']}   state: {pr['state']}"
           f"{'  [DRAFT]' if pr['draft'] else ''}   review: {pr['decision'] or 'none'}",
           "=" * 70]

    if checks["failing"]:
        out.append(f"❌ {len(checks['failing'])} failing check(s) — fix these before the review comments:")
        out += [f"   • {c['name']} ({c['conclusion']})  {c['url'] or ''}" for c in checks["failing"]]
    if checks["pending"]:
        out.append(f"⏳ {len(checks['pending'])} check(s) still running")
    if checks["passing_count"] and not checks["failing"]:
        out.append(f"✅ {checks['passing_count']} check(s) passing")

    for r in report["reviews"]:
        out.append(f"\n── review by {r['author']} [{r['state']}]")
        if r["body"]:
            out.append("   " + r["body"].replace("\n", "\n   ")[:1500])

    out.append(f"\n{summary['open_threads']} open thread(s)"
               f"  ({summary['threads_with_suggestions']} with applyable suggestions,"
               f" {summary['outdated_threads']} on moved code)")
    if summary["hidden_threads"]:
        out.append(f"({summary['hidden_threads']} resolved thread(s) hidden — pass --all to see them)")

    current_path = None
    for t in report["threads"]:
        if t["path"] != current_path:
            current_path = t["path"]
            out.append(f"\n▸ {current_path}")
        flags = " ".join(filter(None, [
            "RESOLVED" if t["resolved"] else "",
            "OUTDATED" if t["outdated"] else "",
            f"{len(t['suggestions'])} suggestion(s)" if t["suggestions"] else "",
        ]))
        out.append(f"  line {t['line']}  [{flags or 'open'}]  {t['url'] or ''}")
        for c in t["comments"]:
            body = c["body"].replace("\n", "\n      ")
            out.append(f"    {c['author']}: {body[:1200]}")
    return "\n".join(out)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Collect all review feedback on a PR")
    parser.add_argument("pr", nargs="?", help="PR number (default: the current branch's PR)")
    parser.add_argument("--repo", help="owner/name (default: the current repo)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--all", action="store_true", help="Include resolved threads")
    args = parser.parse_args()

    try:
        pr_view, threads = fetch(args.pr, args.repo)
    except GhError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    report = normalize(pr_view, threads, include_all=args.all)
    print(json.dumps(report, indent=2) if args.format == "json" else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
