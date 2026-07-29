#!/usr/bin/env python3
"""
Detect and run the standard Python quality tools that are installed in the CURRENT
environment, and normalize their output into this skill's findings shape.

The skill's own detectors are stdlib-only and deliberately conservative. When the
repo's environment already has the real tools (ruff, mypy, black, isort, bandit,
flake8, pip-audit, coverage, ...), they are stronger — so use them. This script:

  1. Detects which tools are available here (PATH, then `python -m <tool>`).
  2. Runs every available tool in NON-MUTATING check mode and merges the results.
  3. Reports which tools are MISSING, with a `pip install` hint for each.

Two of the tools answer questions the stdlib detectors structurally cannot:

  - pip-audit knows about published advisories. `find_dependency_issues.py` reads
    the manifest and can tell you a pin is missing; only an advisory database can
    tell you the pin you have is vulnerable.
  - coverage knows what actually executed. `find_untested_modules.py` answers "does
    any test mention this module"; coverage answers "does this code ever run".

It never installs anything. It modifies nothing unless you pass --fix (runs the
autoformatters) or --run-coverage (runs the test suite). When tools are missing, the
caller (e.g. the skill) should ASK the user whether to install them — this script
only reports the gap.

Usage:
  python run_external_tools.py .                      # run all available tools (check only)
  python run_external_tools.py . --format json        # {findings, tools_run, missing_tools}
  python run_external_tools.py . --tools ruff,mypy    # only these
  python run_external_tools.py . --fix                # also run black/isort/ruff --fix (MUTATES)
  python run_external_tools.py . --run-coverage       # run the test suite first (SLOW, EXECUTES CODE)
"""

import re
import sys
import json
import shutil
import argparse
import contextlib
import subprocess
from pathlib import Path
from collections import defaultdict
from common import EXCLUDE_DIRS, SEVERITY_ICONS, configure_output

_ICON = SEVERITY_ICONS
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


def _run(argv, timeout=300, cwd=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           cwd=str(cwd) if cwd else None)
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


def _requirements_files(path):
    """Requirements files under ``path``, nearest the root first (capped).

    pip-audit audits either a requirements file or the *installed environment*.
    A requirements file is the honest target — it describes the project. The
    environment describes whatever interpreter happens to be running this script,
    which for an arbitrary target directory says nothing about that project. So
    prefer manifests, and label the fallback when there are none.
    """
    root = Path(path)
    if root.is_file():
        return [root] if root.name.startswith("requirements") else []
    if not root.is_dir():
        return []
    found = []
    for p in root.rglob("requirements*.txt"):
        if EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts):
            found.append(p)
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found[:5]


def run_pip_audit(inv, path):
    """Known advisories against the project's pinned dependencies."""
    targets = _requirements_files(path)
    # No manifest to audit — fall back to the installed environment, which is what
    # pip-audit does bare. Say so in the finding, because it is a different claim.
    jobs = [(t, ["-r", str(t)], str(t)) for t in targets] or [(None, [], path)]

    findings = []
    for target, extra, reported_file in jobs:
        rc, out, err = _run([*inv, "--format=json", "--progress-spinner=off", *extra], timeout=300)
        # pip-audit exits 0 (clean) or 1 (vulnerabilities found); else it failed.
        if rc not in (0, 1):
            findings.append(_tool_error("pip-audit", reported_file, rc, err))
            continue
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            findings.append(_tool_error("pip-audit", reported_file, rc, err or "unparseable JSON output"))
            continue
        scope = "" if target else " (installed environment — no requirements file found under the path)"
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                aliases = [a for a in vuln.get("aliases", []) if a.startswith("CVE-")]
                ident = aliases[0] if aliases else vuln.get("id", "advisory")
                fixes = vuln.get("fix_versions") or []
                summary = (vuln.get("description") or "").strip().split(". ")[0][:200]
                f = _finding("pip-audit", reported_file, 1, ident,
                             f"{dep.get('name')} {dep.get('version')} is affected by {ident}"
                             f"{scope}: {summary}", "high")
                f["suggestion"] = (f"Upgrade {dep.get('name')} to {fixes[0]} or later."
                                   if fixes else
                                   f"No fixed version is published for {ident} — pin around it or drop the dependency.")
                findings.append(f)
    return findings


def _coverage_data_dir(path):
    """Where coverage's data file lives, since `coverage json` reads it from cwd."""
    root = Path(path)
    root = root if root.is_dir() else root.parent
    if (root / ".coverage").exists():
        return root
    for p in root.rglob(".coverage"):
        if EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts):
            return p.parent
    return root


def run_coverage(inv, path):
    """Code that never executes, per the existing coverage data.

    Reports only what is *unambiguous* — a module or function with zero covered
    statements. "Under-covered" is a judgment call and belongs to a reviewer, not
    to a detector whose findings have to stay trustworthy.

    Reads existing data; it never runs anything. Note that a module absent from the
    data is not the same as a module at 0%: coverage only knows about files that
    were loaded, unless collection used --source. `find_untested_modules.py` is the
    detector that finds modules no test so much as mentions.
    """
    cwd = _coverage_data_dir(path)
    rc, out, err = _run([*inv, "json", "-o", "-"], cwd=cwd)
    if rc != 0:
        # "No data to report." goes to STDOUT, not stderr, and exits 1 — so this
        # cannot be distinguished by exit code alone. It means the measurement was
        # never taken, which is a different report than the tool failing.
        if "no data to report" in f"{out}{err}".lower():
            return [_finding("coverage", path, 1, "no-data",
                             "coverage is installed but no coverage data exists — run "
                             "`coverage run -m pytest` (or pass --run-coverage) to measure",
                             "low")]
        return [_tool_error("coverage", path, rc, err or out)]
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [_tool_error("coverage", path, rc, err or "unparseable JSON output")]

    findings = []
    for rel, info in sorted((data.get("files") or {}).items()):
        summary = info.get("summary") or {}
        if not summary.get("num_statements"):
            continue
        # Data collected without --source can include site-packages; those are
        # never the user's code and would drown the real findings.
        if not EXCLUDE_DIRS.isdisjoint(Path(rel).parts):
            continue
        resolved = cwd / rel
        reported = str(resolved) if resolved.exists() else rel
        if summary.get("covered_lines") == 0:
            findings.append(_finding("coverage", reported, 1, "uncovered-module",
                                     f"No statement in this module is executed by the test suite "
                                     f"({summary['num_statements']} statements)", "medium"))
            continue  # the module-level finding subsumes its functions
        # `functions` arrived in coverage's JSON format 3; older versions omit it.
        for name, fn in sorted((info.get("functions") or {}).items()):
            fn_summary = fn.get("summary") or {}
            if not name or not fn_summary.get("num_statements") or fn_summary.get("covered_lines"):
                continue
            # start_line only exists in coverage >= 7.10. Every statement of a
            # fully-uncovered function is missing, so its lowest missing line is
            # the function's first statement — precise on older versions too.
            missing = fn.get("missing_lines") or []
            line = fn.get("start_line") or (min(missing) if missing else 1)
            findings.append(_finding("coverage", reported, line, "uncovered-function",
                                     f"{name}() is never executed by the test suite "
                                     f"({fn_summary['num_statements']} statements)", "low"))
    return findings


# name -> (module-for-`python -m`, pip package, check-runner, is_formatter)
TOOLS = {
    "ruff":      ("ruff", "ruff", run_ruff, True),
    "mypy":      ("mypy", "mypy", run_mypy, False),
    "bandit":    ("bandit", "bandit", run_bandit, False),
    "flake8":    ("flake8", "flake8", run_flake8, False),
    "black":     ("black", "black", run_black, True),
    "isort":     ("isort", "isort", run_isort, True),
    "pip-audit": ("pip_audit", "pip-audit", run_pip_audit, False),
    "coverage":  ("coverage", "coverage", run_coverage, False),
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


def measure_coverage(available, path):
    """Run the test suite under coverage so run_coverage has data to read.

    Like apply_fixes, this is an explicit opt-in pre-step rather than something a
    runner does: it EXECUTES the project's tests, which is arbitrary code with
    arbitrary side effects. Runners stay pure readers.
    """
    if "coverage" not in available:
        return ["coverage: not installed — nothing measured"]
    cwd = _coverage_data_dir(path)
    # --source is what makes never-imported modules visible. Without it coverage
    # only reports files it actually loaded, so a module no test ever imports —
    # the most interesting gap — is absent from the report rather than shown at 0%.
    rc, _out, err = _run([*available["coverage"], "run", "--source=.", "-m", "pytest", "-q"],
                         timeout=1800, cwd=cwd)
    if rc is None:
        return [f"coverage: test run failed to start ({err.strip()[:120]})"]
    # pytest exits 1 when tests fail; coverage data is still written and still useful.
    return [f"coverage: ran the test suite in {cwd} (pytest exit {rc})"]


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Run installed Python quality tools and normalize their output")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--tools", type=str, default="", help="Comma-separated subset (default: all)")
    parser.add_argument("--fix", action="store_true", help="Also run black/isort/ruff --fix (MUTATES files)")
    parser.add_argument("--run-coverage", action="store_true",
                        help="Run the test suite under coverage first (SLOW; EXECUTES the project's tests)")
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

    # Both pre-steps run FIRST so the findings below describe the post-action state —
    # otherwise the report would list issues the formatters just resolved, or call
    # coverage data missing when we were about to generate it.
    action_notes = apply_fixes(available, args.path) if args.fix else []
    if args.run_coverage:
        action_notes += measure_coverage(available, args.path)

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
        "actions_taken": action_notes,
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
    if action_notes:
        print("Actions taken: " + "; ".join(action_notes) + "\n")
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
