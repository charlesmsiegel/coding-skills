#!/usr/bin/env python3
"""
Format analyzer findings into a portable artifact for review or hand-off.

Output is text only: a compact list, detailed cards, or JSON. This script does
NOT create tickets anywhere. When the user wants findings turned into real
tickets, ask which ticket software or MCP to use (Jira, Linear, GitHub Issues,
Asana, a connected MCP, ...) and create them through that tool — never assume one.

Accepts either:
  - the unified report from analyze_all.py (--format json), or
  - the flat JSON list emitted by any single detector.

Reads from a file argument or stdin.

Examples:
  python analyze_all.py . --format json | python format_findings.py            # markdown list
  python analyze_all.py . --format json | python format_findings.py --format cards
  python find_error_handling.py . --format json | python format_findings.py --format json
  python format_findings.py report.json --min-severity high
"""

import argparse
import json
import sys
from pathlib import Path

from common import SEVERITY_ICONS, SEVERITY_RANK, configure_output

_ICON = SEVERITY_ICONS


def _flatten(data):
    """Normalise either report shape into a flat list of issue dicts."""
    issues = []
    if isinstance(data, list):
        issues = [i for i in data if isinstance(i, dict)]
    elif isinstance(data, dict) and "categories" in data:
        for category, payload in data["categories"].items():
            for issue in payload.get("issues", []):
                if isinstance(issue, dict):
                    issue.setdefault("category", category)
                    issues.append(issue)
    elif isinstance(data, dict) and "issues" in data:
        issues = [i for i in data["issues"] if isinstance(i, dict)]
    elif isinstance(data, dict) and "findings" in data:
        issues = [i for i in data["findings"] if isinstance(i, dict)]
    return issues


def _type_of(issue):
    for key in ("smell_type", "issue_type", "pattern_type", "type"):
        if issue.get(key):
            return issue[key]
    return "issue"


def _suggestion(issue):
    return issue.get("suggestion") or issue.get("after") or ""


def _size_for(severity):
    return {"high": "M", "medium": "S", "low": "S"}.get(severity, "S")


def _render_list(issues):
    lines = [f"# Findings — {len(issues)} item(s)", "",
             "| Severity | Type | Location | Description |",
             "|---|---|---|---|"]
    for issue in issues:
        severity = issue.get("severity", "medium")
        location = f"{Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}"
        description = (issue.get("description", "") or "").replace("|", "\\|")
        if len(description) > 100:
            description = description[:97] + "..."
        lines.append(f"| {_ICON.get(severity, '')} {severity} | {_type_of(issue)} | "
                     f"`{location}` | {description} |")
    return "\n".join(lines)


def _render_cards(issues):
    out = [f"# Findings — {len(issues)} card(s)", ""]
    for issue in issues:
        severity = issue.get("severity", "medium")
        smell = _type_of(issue)
        category = issue.get("category", "")
        labels = ["lang:rust", f"smell:{smell}", f"size:{_size_for(severity)}",
                  f"priority:{severity}"]
        if category:
            labels.append(f"area:{category}")
        out.append(f"### [Refactor] {smell} — {Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}")
        out.append("")
        out.append(f"**Labels:** {'  '.join(labels)}")
        out.append("")
        out.append(f"**Location:** `{issue.get('file', '?')}:{issue.get('line', '?')}`")
        out.append("")
        out.append(f"**Smell:** {issue.get('description', '')}")
        if issue.get("related_lines"):
            out.append("")
            out.append(f"**Also at lines:** {', '.join(str(n) for n in issue['related_lines'])}")
        if _suggestion(issue):
            out.append("")
            out.append(f"**Proposed fix:** {_suggestion(issue)}")
        out.append("")
        out.append("**Standard:** (link the relevant coding-standard or rule)")
        out.append("")
        out.append("**Definition of Done:**")
        out.append("- [ ] Behavior unchanged (existing + new tests green)")
        out.append("- [ ] `cargo check --all-targets` and `cargo clippy` clean")
        out.append("- [ ] `cargo fmt --check` clean")
        out.append("- [ ] No new `unwrap`, `unsafe`, or `#[allow]` introduced")
        out.append("- [ ] Enforcement added if this closes a smell class (a `[lints]` entry, a CI step)")
        out.append("")
    return "\n".join(out)


def _render_json(issues):
    tickets = []
    for issue in issues:
        tickets.append({
            "title": f"[Refactor] {_type_of(issue)} in "
                     f"{Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}",
            "severity": issue.get("severity", "medium"),
            "smell": _type_of(issue),
            "location": f"{issue.get('file', '?')}:{issue.get('line', '?')}",
            "description": issue.get("description", ""),
            "proposed_fix": _suggestion(issue),
            "labels": ["lang:rust", f"smell:{_type_of(issue)}"],
        })
    return json.dumps(tickets, indent=2)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Format analyzer findings (does not create tickets)")
    parser.add_argument("input", nargs="?", help="JSON file (defaults to stdin)")
    parser.add_argument("--format", choices=["list", "cards", "json"], default="list")
    parser.add_argument("--min-severity", choices=["high", "medium", "low"], default="low")
    args = parser.parse_args()

    raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Could not parse JSON input: {exc}", file=sys.stderr)
        sys.exit(1)

    issues = _flatten(data)
    floor = SEVERITY_RANK[args.min_severity]
    issues = [i for i in issues if SEVERITY_RANK.get(i.get("severity", "medium"), 1) <= floor]
    issues.sort(key=lambda i: (SEVERITY_RANK.get(i.get("severity", "medium"), 1),
                               str(i.get("file")), i.get("line", 0)))

    if not issues:
        # A clean report still has to satisfy the format the caller asked for:
        # the JSON pipeline this script documents is broken precisely when
        # there is nothing to report, which is the common case in CI.
        print("[]" if args.format == "json" else
              "No findings at or above the requested severity.")
        return

    if args.format == "json":
        print(_render_json(issues))
    elif args.format == "cards":
        print(_render_cards(issues))
    else:
        print(_render_list(issues))


if __name__ == "__main__":
    main()
