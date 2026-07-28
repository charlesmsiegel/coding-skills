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
  python find_mutation_hazards.py . --format json | python format_findings.py --format json
  python format_findings.py report.json --min-severity high
"""

import sys
import json
import argparse
from pathlib import Path

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}
_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _flatten(data):
    """Normalise either report shape into a flat list of issue dicts."""
    issues = []
    if isinstance(data, list):
        issues = [i for i in data if isinstance(i, dict)]
    elif isinstance(data, dict) and "categories" in data:
        for cat, payload in data["categories"].items():
            for issue in payload.get("issues", []):
                if isinstance(issue, dict):
                    issue.setdefault("category", cat)
                    issues.append(issue)
    elif isinstance(data, dict) and "issues" in data:
        issues = [i for i in data["issues"] if isinstance(i, dict)]
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
    for i in issues:
        sev = i.get("severity", "medium")
        loc = f"{Path(str(i.get('file', '?'))).name}:{i.get('line', '?')}"
        desc = (i.get("description", "") or "").replace("|", "\\|")
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"| {_ICON.get(sev, '')} {sev} | {_type_of(i)} | `{loc}` | {desc} |")
    return "\n".join(lines)


def _render_cards(issues):
    out = [f"# Findings — {len(issues)} card(s)", ""]
    for i in issues:
        sev = i.get("severity", "medium")
        typ = _type_of(i)
        category = i.get("category", "")
        labels = ["lang:python", f"smell:{typ}", f"size:{_size_for(sev)}", f"priority:{sev}"]
        if category:
            labels.append(f"area:{category}")
        out.append(f"### [Refactor] {typ} — {Path(str(i.get('file', '?'))).name}:{i.get('line', '?')}")
        out.append("")
        out.append(f"**Labels:** {'  '.join(labels)}")
        out.append("")
        out.append(f"**Location:** `{i.get('file', '?')}:{i.get('line', '?')}`")
        out.append("")
        out.append(f"**Smell:** {i.get('description', '')}")
        if _suggestion(i):
            out.append("")
            out.append(f"**Proposed fix:** {_suggestion(i)}")
        out.append("")
        out.append("**Standard:** (link the relevant coding-standard or rule)")
        out.append("")
        out.append("**Definition of Done:**")
        out.append("- [ ] Behavior unchanged (existing + new tests green)")
        out.append("- [ ] Lint + type check clean")
        out.append("- [ ] No new duplication")
        out.append("- [ ] Enforcement rule added if this closes a smell class")
        out.append("")
    return "\n".join(out)


def _render_json(issues):
    tickets = []
    for i in issues:
        tickets.append({
            "title": f"[Refactor] {_type_of(i)} in {Path(str(i.get('file', '?'))).name}:{i.get('line', '?')}",
            "severity": i.get("severity", "medium"),
            "smell": _type_of(i),
            "location": f"{i.get('file', '?')}:{i.get('line', '?')}",
            "description": i.get("description", ""),
            "proposed_fix": _suggestion(i),
            "labels": ["lang:python", f"smell:{_type_of(i)}"],
        })
    return json.dumps(tickets, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Format analyzer findings (does not create tickets)")
    parser.add_argument("input", nargs="?", help="JSON file (defaults to stdin)")
    parser.add_argument("--format", choices=["list", "cards", "json"], default="list")
    parser.add_argument("--min-severity", choices=["high", "medium", "low"], default="low")
    args = parser.parse_args()

    raw = Path(args.input).read_text() if args.input else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Could not parse JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    issues = _flatten(data)
    floor = _SEV_RANK[args.min_severity]
    issues = [i for i in issues if _SEV_RANK.get(i.get("severity", "medium"), 1) <= floor]
    issues.sort(key=lambda i: (_SEV_RANK.get(i.get("severity", "medium"), 1), str(i.get("file")), i.get("line", 0)))

    if not issues:
        print("No findings at or above the requested severity.")
        return

    if args.format == "json":
        print(_render_json(issues))
    elif args.format == "cards":
        print(_render_cards(issues))
    else:
        print(_render_list(issues))


if __name__ == "__main__":
    main()
