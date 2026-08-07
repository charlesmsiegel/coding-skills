#!/usr/bin/env python3
"""
Detect and run the TypeScript ecosystem's real tools where the project already
has them, and normalize their output into this skill's findings shape.

The skill's own detectors read source text with their own scanner. They
cannot type-check, and everything that needs a type — "is this promise handled",
"is this condition always true", "is this cast necessary" — belongs to `tsc` and
to typescript-eslint's type-aware rules. Where the project has them installed,
they are strictly better, so use them.

Three of these answer questions the detectors structurally *cannot*:

  - tsc knows the types. `find_type_gaps.py` finds where checking was switched
    off; only the compiler finds what it would have caught.
  - npm audit knows about published advisories. `find_dependency_issues.py` can
    say a range is unpinned; only an advisory database can say the version you
    have is vulnerable.
  - coverage knows what executed. `find_untested_modules.py` answers "does a
    test import this"; coverage answers "does this code ever run".

It never installs anything. Tools are resolved from the project's own
`node_modules/.bin` first, then PATH — never with `npx` in download mode, which
would fetch packages onto a machine that did not ask for them.

Usage:
  python run_external_tools.py .                    # every available tool, check only
  python run_external_tools.py . --format json      # {tools_run, missing_tools, findings}
  python run_external_tools.py . --tools tsc,eslint
  python run_external_tools.py . --fix              # eslint --fix / prettier --write (MUTATES)
  python run_external_tools.py . --run-coverage     # run the test suite (SLOW, EXECUTES CODE)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from common import EXCLUDE_DIRS, SEVERITY_ICONS, configure_output

_ICON = SEVERITY_ICONS
_TSC_LINE = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<level>error|warning)\s+"
    r"(?P<code>TS\d+):\s*(?P<msg>.*)$"
)


def _project_root(path: Path) -> Path:
    path = path if path.is_dir() else path.parent
    for candidate in [path, *path.parents]:
        if (candidate / "package.json").is_file():
            return candidate
    return path


def _invocation(root: Path, name: str) -> list[str] | None:
    """Argv prefix for a tool, preferring the project's own node_modules."""
    for directory in [root, *root.parents]:
        for suffix in ("", ".cmd", ".ps1" if os.name == "nt" else ""):
            if not suffix and os.name == "nt":
                continue
            candidate = directory / "node_modules" / ".bin" / (name + suffix)
            if candidate.is_file():
                return [str(candidate)]
        if (directory / "node_modules").is_dir() and directory != root:
            break
    found = shutil.which(name)
    return [found] if found else None


def _run(argv, cwd, timeout=900):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                cwd=str(cwd), shell=False)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)


def _finding(tool, file, line, code, message, severity):
    return {
        "tool": tool,
        "file": str(file),
        "line": line or 1,
        "smell_type": f"{tool}:{code}" if code else tool,
        "description": message,
        "suggestion": f"Resolve the {tool} finding ({code})." if code else f"Resolve the {tool} finding.",
        "severity": severity,
    }


def _tool_error(tool, path, returncode, stderr):
    detail = (stderr or "").strip().splitlines()
    detail = detail[-1][:200] if detail else f"exit code {returncode}"
    return _finding(tool, path, 1, "tool-error",
                    f"{tool} did not complete ({detail}) — its results are missing from this report",
                    "medium")


# ---- per-tool runners (check mode) --------------------------------------- #

def run_tsc(inv, root):
    """The type checker. Everything a type-aware detector could want lives here."""
    returncode, out, err = _run([*inv, "--noEmit", "--pretty", "false"], root)
    if returncode is None:
        return [_tool_error("tsc", root, returncode, err)]
    findings = []
    for line in (out or "").splitlines():
        match = _TSC_LINE.match(line.strip())
        if not match:
            continue
        severity = "high" if match.group("level") == "error" else "medium"
        findings.append(_finding("tsc", (root / match.group("file")).resolve(),
                                 int(match.group("line")), match.group("code"),
                                 match.group("msg"), severity))
    if not findings and returncode not in (0, 1, 2):
        return [_tool_error("tsc", root, returncode, err or out)]
    return findings


def run_eslint(inv, root):
    returncode, out, err = _run([*inv, ".", "--format", "json"], root)
    if returncode not in (0, 1) or not (out or "").strip():
        return [_tool_error("eslint", root, returncode, err or out)]
    try:
        results = json.loads(out)
    except json.JSONDecodeError:
        return [_tool_error("eslint", root, returncode, "unparseable JSON output")]
    findings = []
    for result in results:
        for message in result.get("messages", []):
            rule = message.get("ruleId") or "parse-error"
            severity = "high" if message.get("severity") == 2 else "low"
            if rule.startswith(("@typescript-eslint/no-floating", "@typescript-eslint/no-misused",
                                "@typescript-eslint/no-unsafe", "security/")):
                severity = "high"
            findings.append(_finding("eslint", result.get("filePath", root),
                                     message.get("line"), rule, message.get("message", ""), severity))
    return findings


def run_biome(inv, root):
    returncode, out, err = _run([*inv, "check", "--reporter=json", "."], root)
    if returncode is None or not (out or "").strip():
        return [_tool_error("biome", root, returncode, err or out)]
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [_tool_error("biome", root, returncode, "unparseable JSON output")]
    findings = []
    for diagnostic in data.get("diagnostics", []):
        location = diagnostic.get("location") or {}
        path = (location.get("path") or {}).get("file", str(root))
        span = location.get("span") or [0]
        text = diagnostic.get("description") or diagnostic.get("message") or ""
        if isinstance(text, list):
            text = "".join(part.get("content", "") for part in text if isinstance(part, dict))
        line = 1
        source = location.get("sourceCode")
        if isinstance(source, str) and isinstance(span, list) and span:
            line = source.count("\n", 0, span[0]) + 1
        severity = {"error": "high", "warning": "medium"}.get(diagnostic.get("severity"), "low")
        findings.append(_finding("biome", path, line, diagnostic.get("category", ""), text, severity))
    return findings


def run_prettier(inv, root):
    returncode, out, err = _run([*inv, "--check", "."], root)
    if returncode not in (0, 1):
        return [_tool_error("prettier", root, returncode, err)]
    findings = []
    for line in ((err or "") + (out or "")).splitlines():
        stripped = line.strip()
        if stripped.startswith("[warn]") and "." in stripped and "Code style issues" not in stripped:
            findings.append(_finding("prettier", (root / stripped[6:].strip()).resolve(), 1,
                                     "format", "File is not prettier-formatted", "low"))
    return findings


def run_madge(inv, root):
    returncode, out, err = _run([*inv, "--circular", "--extensions", "ts,tsx", "--json", "."], root)
    if returncode not in (0, 1) or not (out or "").strip():
        return [_tool_error("madge", root, returncode, err or out)]
    try:
        cycles = json.loads(out)
    except json.JSONDecodeError:
        return [_tool_error("madge", root, returncode, "unparseable JSON output")]
    findings = []
    for cycle in cycles if isinstance(cycles, list) else []:
        if not cycle:
            continue
        findings.append(_finding("madge", (root / cycle[0]).resolve(), 1, "circular",
                                 "Import cycle: " + " → ".join(cycle) + f" → {cycle[0]}", "high"))
    return findings


def run_knip(inv, root):
    returncode, out, err = _run([*inv, "--reporter", "json", "--no-progress"], root)
    if returncode not in (0, 1) or not (out or "").strip():
        return [_tool_error("knip", root, returncode, err or out)]
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [_tool_error("knip", root, returncode, "unparseable JSON output")]
    findings = []
    for entry in data.get("files", []) if isinstance(data, dict) else []:
        findings.append(_finding("knip", (root / str(entry)).resolve(), 1, "unused-file",
                                 "Knip reports this file as unused", "medium"))
    for issue in data.get("issues", []) if isinstance(data, dict) else []:
        path = (root / str(issue.get("file", ""))).resolve()
        for kind in ("exports", "types", "dependencies", "devDependencies", "unlisted"):
            for item in issue.get(kind, []) or []:
                name = item.get("name") if isinstance(item, dict) else str(item)
                line = item.get("line", 1) if isinstance(item, dict) else 1
                findings.append(_finding("knip", path, line, f"unused-{kind}",
                                         f"`{name}` reported by knip as {kind}", "low"))
    return findings


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def run_audit(inv, root):
    """Known advisories against the installed dependency tree."""
    manager = _package_manager(root)
    if manager == "yarn":
        argv = [*inv, "npm", "audit", "--json"]
    else:
        argv = [*inv, "audit", "--json"]
    returncode, out, err = _run(argv, root, timeout=600)
    if returncode is None or not (out or "").strip():
        return [_tool_error(f"{manager}-audit", root, returncode, err or out)]
    manifest = root / "package.json"
    findings = []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # yarn emits newline-delimited JSON; take the advisory objects out of it.
        for line in out.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            advisory = (record.get("data") or {}).get("advisory")
            if advisory:
                findings.append(_advisory_finding(manager, manifest, advisory.get("module_name"),
                                                  advisory.get("severity"), advisory.get("title", ""),
                                                  advisory.get("url", "")))
        return findings or [_tool_error(f"{manager}-audit", root, returncode, "unparseable output")]

    for name, entry in (data.get("vulnerabilities") or {}).items():
        via = entry.get("via") or []
        titles = [v.get("title", "") for v in via if isinstance(v, dict)]
        urls = [v.get("url", "") for v in via if isinstance(v, dict)]
        findings.append(_advisory_finding(manager, manifest, name, entry.get("severity"),
                                          titles[0] if titles else "known advisory",
                                          urls[0] if urls else ""))
    return findings


def _advisory_finding(manager, manifest, name, severity, title, url):
    mapped = {"critical": "high", "high": "high", "moderate": "medium"}.get(severity, "low")
    finding = _finding(f"{manager}-audit", manifest, 1, severity or "advisory",
                       f"{name} is affected by a published advisory: {title}"
                       + (f" ({url})" if url else ""), mapped)
    finding["suggestion"] = (f"Upgrade {name}, or run `{manager} audit fix`. If no fixed version "
                             "exists, pin around it or drop the dependency.")
    return finding


def _coverage_files(root: Path):
    for name in ("coverage/coverage-final.json", "coverage/coverage-summary.json"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    for candidate in root.rglob("coverage-final.json"):
        if EXCLUDE_DIRS.isdisjoint(set(candidate.relative_to(root).parts) - {"coverage"}):
            return candidate
    return None


def run_coverage(_inv, root):
    """Code that never executes, per the coverage data already on disk.

    Reports only the unambiguous case — a file with zero covered statements —
    because "under-covered" is a judgment call that belongs to a reviewer.
    """
    data_file = _coverage_files(root)
    if data_file is None:
        return [_finding("coverage", root, 1, "no-data",
                         "No coverage data found — run the suite with coverage "
                         "(`vitest run --coverage` / `jest --coverage`, or pass --run-coverage)",
                         "low")]
    try:
        data = json.loads(data_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return [_tool_error("coverage", data_file, 1, "unparseable coverage data")]
    findings = []
    for path, entry in data.items():
        if path in ("total",) or not isinstance(entry, dict):
            continue
        statements = entry.get("s")
        if isinstance(statements, dict):
            total = len(statements)
            covered = sum(1 for count in statements.values() if count)
        else:
            summary = entry.get("statements") or {}
            total, covered = summary.get("total", 0), summary.get("covered", 0)
        if total and covered == 0:
            findings.append(_finding("coverage", path, 1, "uncovered-file",
                                     f"No statement in this file is executed by the test suite "
                                     f"({total} statements)", "medium"))
    return findings


# name -> (package hint, check runner, mutating-fix argv or None)
TOOLS = {
    "tsc":      ("typescript", run_tsc, None),
    "eslint":   ("eslint", run_eslint, ["--fix", "."]),
    "biome":    ("@biomejs/biome", run_biome, ["check", "--write", "."]),
    "prettier": ("prettier", run_prettier, ["--write", "."]),
    "madge":    ("madge", run_madge, None),
    "knip":     ("knip", run_knip, None),
    "npm":      ("npm (ships with node)", run_audit, None),
    "coverage": ("vitest/jest with coverage enabled", run_coverage, None),
}


def apply_fixes(available, root):
    notes = []
    for tool in ("prettier", "eslint", "biome"):
        if tool not in available or TOOLS[tool][2] is None:
            continue
        returncode, _out, _err = _run([*available[tool], *TOOLS[tool][2]], root)
        notes.append(f"{tool}: {'applied' if returncode in (0, 1) else 'ran (exit %s)' % returncode}")
    return notes


def measure_coverage(root):
    """Run the project's test script under coverage so run_coverage has data.

    An explicit opt-in, because it executes the project's tests — arbitrary code
    with arbitrary side effects. The runners themselves stay pure readers.
    """
    manager = _package_manager(root)
    runner = shutil.which(manager)
    if runner is None:
        return [f"{manager}: not on PATH — nothing measured"]
    returncode, _out, err = _run([runner, "run", "test", "--", "--coverage"], root, timeout=2400)
    if returncode is None:
        return [f"coverage: test run failed to start ({err.strip()[:120]})"]
    return [f"coverage: ran `{manager} run test -- --coverage` in {root} (exit {returncode})"]


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Run the installed TypeScript tools and normalize their output")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--tools", type=str, default="", help="Comma-separated subset (default: all)")
    parser.add_argument("--fix", action="store_true",
                        help="Also run prettier/eslint/biome in write mode (MUTATES files)")
    parser.add_argument("--run-coverage", action="store_true",
                        help="Run the project's test script with coverage first (SLOW; EXECUTES tests)")
    args = parser.parse_args()

    root = _project_root(Path(args.path).resolve())
    wanted = set(args.tools.split(",")) if args.tools else set(TOOLS)
    wanted = {name for name in wanted if name in TOOLS}

    available, missing = {}, []
    for name in TOOLS:
        if name not in wanted:
            continue
        if name == "coverage":
            available[name] = []  # a reader over files on disk, not an executable
            continue
        inv = _invocation(root, name)
        if inv:
            available[name] = inv
        else:
            missing.append({"name": name, "install": f"npm install --save-dev {TOOLS[name][0]}"})

    # Both pre-steps run FIRST so the findings describe the post-action state.
    action_notes = apply_fixes(available, root) if args.fix else []
    if args.run_coverage:
        action_notes += measure_coverage(root)

    findings = []
    for name, inv in available.items():
        runner = TOOLS[name][1]
        try:
            findings.extend(runner(inv, root))
        except Exception as exc:  # one tool failing must not sink the rest
            findings.append(_finding(name, root, 1, "tool-error", f"{name} failed to run: {exc}", "medium"))

    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (rank.get(f["severity"], 1), str(f["file"]), f["line"]))

    report = {
        "project_root": str(root),
        "tools_run": sorted(available),
        "missing_tools": missing,
        "actions_taken": action_notes,
        "findings": findings,
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return

    print(f"\n🔧 EXTERNAL TOOLS in {root} — ran: {', '.join(report['tools_run']) or '(none)'}")
    print("=" * 60)
    if missing:
        print("Not installed in this project (ask the user before installing):")
        for entry in missing:
            print(f"  • {entry['name']}  →  {entry['install']}")
        print()
    if action_notes:
        print("Actions taken: " + "; ".join(action_notes) + "\n")
    if not findings:
        print("✅ No findings from the available tools.")
        return
    counts = defaultdict(int)
    for finding in findings:
        counts[finding["severity"]] += 1
    print(f"{len(findings)} finding(s)  "
          f"({_ICON['high']} {counts['high']}  {_ICON['medium']} {counts['medium']}  "
          f"{_ICON['low']} {counts['low']})\n")
    for finding in findings[:200]:
        print(f"{_ICON[finding['severity']]} [{finding['severity'].upper()}] "
              f"{finding['file']}:{finding['line']}  {finding['smell_type']}")
        print(f"   {finding['description']}")
    if len(findings) > 200:
        print(f"\n... and {len(findings) - 200} more")


if __name__ == "__main__":
    main()
