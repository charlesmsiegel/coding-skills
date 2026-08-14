#!/usr/bin/env python3
"""Download the selected candidates and record every outcome in the manifest.

Usage:
    python3 fetch_artifacts.py --out ./research/team-knowledge \
        [--repo NAME=URL ...] [--format text|json]

Reads <out>/candidates.json, fetches every candidate whose status is "selected",
and writes <out>/manifest.json.

The manifest is the skill's ground truth. A citation resolves against it, the
headline counts are computed from it, and the gaps tab is built from it — so an
artifact that could not be obtained must appear as an entry with a reason. An
absent entry would let an unreachable paper read as one nobody wanted.
"""

import argparse
import datetime as _dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from common import (
    Candidate,
    FetchError,
    Http,
    ManifestEntry,
    Reporter,
    artifact_filename,
    artifact_id,
)

# Enough to name the licence from its first lines. Anything unmatched is recorded
# as "unknown" rather than guessed — a wrong licence in the manifest is worse
# than an absent one.
LICENSE_MARKERS = (
    ("MIT", "MIT License"),
    ("Apache-2.0", "Apache License"),
    ("BSD-3-Clause", "BSD 3-Clause"),
    ("BSD-2-Clause", "BSD 2-Clause"),
    ("GPL-3.0", "GNU GENERAL PUBLIC LICENSE"),
    ("MPL-2.0", "Mozilla Public License"),
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_failure(reason: str) -> str:
    """A 401/403 is a paywall; anything else is just a failure. Both are gaps."""
    if "401" in reason or "403" in reason:
        return "paywalled"
    return "failed"


def _load_manifest(path: Path) -> dict[str, ManifestEntry]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {e["artifact_id"]: ManifestEntry.from_dict(e) for e in payload.get("artifacts", [])}


def _is_current(entry: ManifestEntry, out: Path) -> bool:
    """Whether the recorded artifact is still byte-for-byte what was recorded."""
    if entry.status != "ok":
        return False
    path = out / entry.path
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256


def _target(candidate: Candidate) -> tuple[str, str, str]:
    """(url, kind, suffix) for the best available location, or ("", "", "")."""
    if candidate.pdf_url:
        return candidate.pdf_url, "paper", ".pdf"
    if candidate.landing_url:
        return candidate.landing_url, "web", ".html"
    return "", "", ""


def _detect_license(clone: Path) -> str:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        path = clone / name
        if not path.is_file():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        for spdx, marker in LICENSE_MARKERS:
            if marker.lower() in head.lower():
                return spdx
        return "unknown"
    return "none-found"


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise OSError(result.stderr.strip()[:400] or "git failed")
    return result.stdout.strip()


def _clone_one(spec: dict, out: Path, now) -> tuple[ManifestEntry, bool]:
    """Returns (entry, cloned_now). A present clone is left alone."""
    name = spec["name"]
    url = spec["url"]
    aid = "repo:" + name
    destination = out / "docs" / "repos" / name
    if (destination / ".git").is_dir():
        head = _git("-C", str(destination), "rev-parse", "HEAD")
        return ManifestEntry(
            artifact_id=aid, kind="repo", url=url, status="ok",
            path="docs/repos/" + name, sha256=head, bytes_len=0,
            fetched_at=now(), license=_detect_license(destination),
        ), False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git("clone", "--depth", "1", "--quiet", url, str(destination))
        head = _git("-C", str(destination), "rev-parse", "HEAD")
    except OSError as exc:
        return ManifestEntry(
            artifact_id=aid, kind="repo", url=url, status="failed",
            failure_reason="clone failed: " + str(exc),
        ), False
    return ManifestEntry(
        artifact_id=aid, kind="repo", url=url, status="ok",
        path="docs/repos/" + name, sha256=head, bytes_len=0,
        fetched_at=now(), license=_detect_license(destination),
    ), True


def _write_clones_md(out: Path, entries: list[ManifestEntry]) -> None:
    lines = ["# Cloned repositories", ""]
    for entry in entries:
        name = entry.artifact_id.removeprefix("repo:")
        if entry.status == "ok":
            lines.append("- **" + name + "** — " + entry.url + " @ `" + entry.sha256[:12]
                         + "` (" + entry.license + ")")
        else:
            lines.append("- **" + name + "** — " + entry.url + " — NOT CLONED: "
                         + entry.failure_reason)
    path = out / "docs" / "repos" / "CLONES.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(out: Path, http, now=_now, repos=None) -> dict:
    out = Path(out)
    candidates_path = out / "candidates.json"
    if not candidates_path.is_file():
        raise SystemExit("no candidates.json in " + str(out) + " — run search_sources.py first")
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    selected = [Candidate.from_dict(c) for c in payload.get("candidates", [])
                if c.get("status") == "selected"]

    manifest = _load_manifest(out / "manifest.json")
    fetched = skipped = gaps = cloned = 0

    for candidate in selected:
        aid = artifact_id(candidate)
        existing = manifest.get(aid)
        if existing is not None and _is_current(existing, out):
            skipped += 1
            continue

        url, kind, suffix = _target(candidate)
        if not url:
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind="paper", url="", status="failed",
                failure_reason="no retrievable location: neither a PDF nor a landing page",
            )
            gaps += 1
            continue

        if kind == "web" and hasattr(http, "robots_allows") and not http.robots_allows(url):
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url, status="robots_blocked",
                failure_reason="robots.txt disallows this path for our user agent",
            )
            gaps += 1
            continue

        try:
            response = http.get(url)
        except (FetchError, OSError) as exc:
            reason = str(exc)
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url,
                status=_classify_failure(reason), failure_reason=reason,
            )
            gaps += 1
            continue

        subdir = "papers" if kind == "paper" else "web"
        rel = "docs/" + subdir + "/" + artifact_filename(candidate, suffix)
        destination = out / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.body)
        manifest[aid] = ManifestEntry(
            artifact_id=aid, kind=kind, url=url, status="ok", path=rel,
            sha256=hashlib.sha256(response.body).hexdigest(),
            bytes_len=len(response.body), fetched_at=now(),
            content_type=getattr(response, "content_type", ""),
        )
        fetched += 1

    repo_entries = []
    for spec in repos or []:
        entry, was_cloned = _clone_one(spec, out, now)
        manifest[entry.artifact_id] = entry
        repo_entries.append(entry)
        if was_cloned:
            cloned += 1
        elif entry.status == "ok":
            skipped += 1
        else:
            gaps += 1
    if repo_entries:
        _write_clones_md(out, repo_entries)

    (out / "manifest.json").write_text(
        json.dumps({"artifacts": [manifest[k].to_dict() for k in sorted(manifest)]}, indent=2),
        encoding="utf-8",
    )
    return {"fetched": fetched, "skipped": skipped, "gaps": gaps, "cloned": cloned,
            "selected": len(selected)}


def _parse_repo_flag(raw: str) -> dict:
    name, sep, url = raw.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError("--repo takes NAME=URL, got " + repr(raw))
    return {"name": name, "url": url}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--repo", action="append", default=[], dest="repos",
                    type=_parse_repo_flag, metavar="NAME=URL",
                    help="a repository to clone shallowly; repeatable")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.out, Http(), repos=args.repos)

    reporter = Reporter("fetch_artifacts")
    reporter.headline(
        str(result["fetched"]) + " fetched, " + str(result["cloned"]) + " cloned, "
        + str(result["skipped"]) + " already current, " + str(result["gaps"])
        + " unobtainable, of " + str(result["selected"]) + " selected"
    )
    if result["gaps"]:
        reporter.caveat(
            str(result["gaps"]) + " artifacts could not be obtained and are recorded as gaps. "
            "The report must say so: 'the literature says' and 'the reachable literature says' "
            "are different claims."
        )
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
