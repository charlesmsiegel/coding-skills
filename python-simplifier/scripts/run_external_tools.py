#!/usr/bin/env python3
"""
Detect and run the standard Python quality tools that are installed in the CURRENT
environment, and normalize their output into this skill's findings shape.

The skill's own detectors are stdlib-only and deliberately conservative. When the
repo's environment already has the real tools (ruff, mypy, black, isort, bandit,
flake8, ...), they are stronger — so use them. This script:

  1. Detects which tools are available here (PATH, then `python -m <tool>`).
  2. Runs every available tool in NON-MUTATING check mode and merges the results.
  3. Reports which tools are MISSING, with a `pip install` hint for each.

It never installs anything and never modifies files unless you pass --fix (which
runs the autoformatters). When tools are missing, the caller (e.g. the skill) should
ASK the user whether to install them — this script only reports the gap.

Usage:
  python run_external_tools.py .                      # run all available tools (check only)
  python run_external_tools.py . --format json        # {findings, tools_run, missing_tools}
  python run_external_tools.py . --tools ruff,mypy    # only these
  python run_external_tools.py . --fix                # also run black/isort/ruff --fix (MUTATES)
"""

import re
import sys
import json
import shutil
import argparse
import contextlib
import subprocess
from collections import defaultdict

_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_MYPY_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<level>error|note|warning):\s*(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")
_FLAKE8_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>\w+)\s+(?P<msg>.*)$")


def _invocation(name, module):
    """Return the argv prefix to invoke a tool, or None if unavailable here."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    # If `python -m <tool>` can't run, the tool just isn't available here.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run([sys.executable, "-m", module, "--version"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return [sys.executable, "-m", module]
    return None


def _run(argv, timeout=300):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out"
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)


def _finding(tool, file, line, code, msg, severity):
    return {
        "tool": tool,
        "file": file,
        "line": line or 1,
        "smell_type": f"{tool}:{code}" if code else tool,
        "description": msg,
        "suggestion": f"Resolve the {tool} finding ({code})." if code else f"Resolve the {tool} finding.",
        "severity": severity,
    }


# ---- per-tool runners (check mode) --------------------------------------- #

def _tool_error(tool, path, rc, err):
    detail = (err or "").strip().splitlines()
    detail = detail[-1][:200] if detail else f"exit code {rc}"
    return _finding(tool, path, 1, "tool-error",
                    f"{tool} did not complete ({detail}) — its results are missing from this report", "medium")


def run_ruff(inv, path):
    rc, out, err = _run([*inv, "check", "--output-format", "json", "--quiet", path])
    # ruff exits 0 (clean) or 1 (findings); anything else means it didn't run.
    if rc not in (0, 1):
        return [_tool_error("ruff", path, rc, err)]
    findings = []
    try:
        for d in json.loads(out or "[]"):
            code = d.get("code") or ""
            sev = "high" if code.startswith("S") else ("medium" if code[:2] in ("E9",) or code[:1] == "F" else "low")
            loc = d.get("location") or {}
            findings.append(_finding("ruff", d.get("filename", path), loc.get("row"), code, d.get("message", ""), sev))
    except json.JSONDecodeError:
        return [_tool_error("ruff", path, rc, err or "unparseable JSON output")]
    return findings


def run_mypy(inv, path):
    rc, out, err = _run([*inv, "--no-error-summary", "--no-color-output", "--show-error-codes", path])
    # mypy exits 0 (clean) or 1 (type errors); 2/None means it failed to run.
    if rc not in (0, 1):
        return [_tool_error("mypy", path, rc, err)]
    findings = []
    for line in (out or "").splitlines():
        m = _MYPY_RE.match(line.strip())
        if not m:
            continue
        level = m.group("level")
        sev = "medium" if level == "error" else "low"
        findings.append(_finding("mypy", m.group("file"), int(m.group("line")), m.group("code") or "", m.group("msg"), sev))
    return findings


def run_bandit(inv, path):
    rc, out, err = _run([*inv, "-q", "-r", "-f", "json", path])
    # bandit exits 0 (clean) or 1 (findings); other codes mean it failed.
    if rc not in (0, 1):
        return [_tool_error("bandit", path, rc, err)]
    findings = []
    try:
        data = json.loads(out or "{}")
        for d in data.get("results", []):
            sev = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(d.get("issue_severity", "MEDIUM"), "medium")
            findings.append(_finding("bandit", d.get("filename", path), d.get("line_number"),
                                     d.get("test_id", ""), d.get("issue_text", ""), sev))
    except json.JSONDecodeError:
        return [_tool_error("bandit", path, rc, err or "unparseable JSON output")]
    return findings


def run_flake8(inv, path):
    rc, out, err = _run([*inv, path])
    # flake8 exits 0 (clean) or 1 (findings); other codes mean it failed.
    if rc not in (0, 1):
        return [_tool_error("flake8", path, rc, err)]
    findings = []
    for line in (out or "").splitlines():
        m = _FLAKE8_RE.match(line.strip())
        if not m:
            continue
        code = m.group("code")
        sev = "medium" if code[:2] == "E9" or code[:1] == "F" else "low"
        findings.append(_finding("flake8", m.group("file"), int(m.group("line")), code, m.group("msg"), sev))
    return findings


def run_black(inv, path):
    rc, out, err = _run([*inv, "--check", "--quiet", path])
    # black exits 0 (clean) or 1 (would reformat); other codes mean it failed.
    if rc not in (0, 1):
        return [_tool_error("black", path, rc, err)]
    findings = []
    if rc == 1:
        for m in re.finditer(r"would reformat (.+)", (err or "") + (out or "")):
            findings.append(_finding("black", m.group(1).strip(), 1, "format", "File is not black-formatted", "low"))
        if not findings:
            findings.append(_finding("black", path, 1, "format", "Some files are not black-formatted (run black to fix)", "low"))
    return findings


def run_isort(inv, path):
    rc, out, err = _run([*inv, "--check-only", path])
    # isort exits 0 (clean) or 1 (unsorted); other codes mean it failed.
    if rc not in (0, 1):
        return [_tool_error("isort", path, rc, err)]
    findings = []
    if rc == 1:
        for m in re.finditer(r"ERROR:\s*(.+?)\s+Imports are incorrectly sorted", (err or "") + (out or "")):
            findings.append(_finding("isort", m.group(1).strip(), 1, "imports", "Imports are not sorted/grouped", "low"))
        if not findings:
            findings.append(_finding("isort", path, 1, "imports", "Some files have unsorted imports (run isort to fix)", "low"))
    return findings


# name -> (module-for-`python -m`, pip package, check-runner, is_formatter)
TOOLS = {
    "ruff":   ("ruff", "ruff", run_ruff, True),
    "mypy":   ("mypy", "mypy", run_mypy, False),
    "bandit": ("bandit", "bandit", run_bandit, False),
    "flake8": ("flake8", "flake8", run_flake8, False),
    "black":  ("black", "black", run_black, True),
    "isort":  ("isort", "isort", run_isort, True),
}


def apply_fixes(available, path):
    """Run the autoformatters in mutating mode. Returns a list of note strings."""
    notes = []
    for tool, cmd in (("isort", ["--quiet"]), ("black", ["--quiet"]), ("ruff", ["check", "--fix", "--quiet"])):
        if tool in available:
            inv = available[tool]
            argv = [*inv, *cmd, path]
            rc, out, err = _run(argv)
            notes.append(f"{tool}: {'applied' if rc in (0, None) else 'ran (exit %s)' % rc}")
    return notes


def main():
    parser = argparse.ArgumentParser(description="Run installed Python quality tools and normalize their output")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--tools", type=str, default="", help="Comma-separated subset (default: all)")
    parser.add_argument("--fix", action="store_true", help="Also run black/isort/ruff --fix (MUTATES files)")
    args = parser.parse_args()

    wanted = set(args.tools.split(",")) if args.tools else set(TOOLS)
    wanted = {t for t in wanted if t in TOOLS}

    available, missing = {}, []
    for name in TOOLS:
        if name not in wanted:
            continue
        module, pkg, _runner, _fmt = TOOLS[name]
        inv = _invocation(name, module)
        if inv:
            available[name] = inv
        else:
            missing.append({"name": name, "install": f"pip install {pkg}"})

    # Apply fixes FIRST so the findings below describe the post-fix state —
    # otherwise the report would list issues the formatters just resolved.
    fix_notes = apply_fixes(available, args.path) if args.fix else []

    findings = []
    for name, inv in available.items():
        runner = TOOLS[name][2]
        try:
            findings.extend(runner(inv, args.path))
        except Exception as e:  # one tool failing must not sink the rest
            findings.append(_finding(name, args.path, 1, "tool-error", f"{name} failed to run: {e}", "medium"))

    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (rank.get(f["severity"], 1), str(f["file"]), f["line"]))

    report = {
        "tools_run": sorted(available),
        "missing_tools": missing,
        "fixes_applied": fix_notes,
        "findings": findings,
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return

    print(f"\n🔧 EXTERNAL TOOLS — ran: {', '.join(report['tools_run']) or '(none)'}")
    print("=" * 60)
    if missing:
        print("Not installed in this environment (ask the user before installing):")
        for m in missing:
            print(f"  • {m['name']}  →  {m['install']}")
        print()
    if fix_notes:
        print("Fixes applied: " + "; ".join(fix_notes) + "\n")
    if not findings:
        print("✅ No findings from the available tools.")
        return
    by = defaultdict(int)
    for f in findings:
        by[f["severity"]] += 1
    print(f"{len(findings)} finding(s)  "
          f"({_ICON['high']} {by['high']}  {_ICON['medium']} {by['medium']}  {_ICON['low']} {by['low']})\n")
    for f in findings[:200]:
        print(f"{_ICON[f['severity']]} [{f['severity'].upper()}] {f['file']}:{f['line']}  {f['smell_type']}")
        print(f"   {f['description']}")
    if len(findings) > 200:
        print(f"\n... and {len(findings) - 200} more")


if __name__ == "__main__":
    main()
