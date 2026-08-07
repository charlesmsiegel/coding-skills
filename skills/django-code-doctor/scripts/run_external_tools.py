#!/usr/bin/env python3
"""Drive the real Django tools, when the project's environment already has them.

The detectors in this skill are stdlib-only on purpose: they parse source and
never start Django, so they run against any checkout with no virtualenv and no
settings module. That buys portability and costs three answers they structurally
cannot give:

- **Are there model changes with no migration?** Only Django knows, because the
  answer is a comparison against the migration graph it builds at runtime.
  `makemigrations --check --dry-run`.
- **What does Django itself think of this deployment?** `check --deploy` reads
  the *real* settings module, including whatever the environment overrides — a
  parser is guessing at which module is deployed.
- **Is the pinned Django actually vulnerable?** find_version_issues can say a
  release is past end-of-life; only an advisory database can say which
  advisories apply.

Plus `django-upgrade`, which is the tool that actually performs the rewrites
this skill only describes.

Nothing here is installed automatically and nothing mutates by default. Missing
tools are reported under `missing_tools` with an install hint, so the caller can
ask the user rather than deciding for them.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from common import configure_output
from django_context import build_context
import django_versions as V

# name -> (command builder, what it answers, install hint, mutates?)
TOOLS = {
    "django-upgrade": {
        "hint": "pip install django-upgrade",
        "answers": "rewrites the deprecated constructs find_version_issues reports",
        "mutates": True,
    },
    "check-deploy": {
        "hint": "already present — it is a Django management command",
        "answers": "Django's own deployment checks, against the real settings module",
        "mutates": False,
    },
    "makemigrations-check": {
        "hint": "already present — it is a Django management command",
        "answers": "whether any model change has no migration (unanswerable by parsing)",
        "mutates": False,
    },
    "djlint": {
        "hint": "pip install djlint",
        "answers": "template linting and formatting",
        "mutates": False,
    },
    "mypy": {
        "hint": "pip install mypy django-stubs",
        "answers": "type errors through Django's own stubs",
        "mutates": False,
    },
    "bandit": {
        "hint": "pip install bandit",
        "answers": "language-level security issues",
        "mutates": False,
    },
    "pip-audit": {
        "hint": "pip install pip-audit",
        "answers": "known advisories against the pinned Django and its dependencies",
        "mutates": False,
    },
}
# The management-command tools need manage.py rather than an executable on PATH.
MANAGEMENT_TOOLS = frozenset({"check-deploy", "makemigrations-check"})


def _finding(file, line, smell_type, description, suggestion, severity):
    return {"file": str(file), "line": line or 1, "smell_type": smell_type,
            "description": description, "suggestion": suggestion, "severity": severity}


def _manage_py(root):
    root = Path(root)
    candidate = root / "manage.py"
    if candidate.exists():
        return candidate
    for path in root.rglob("manage.py"):
        return path
    return None


def _available(name, root):
    if name in MANAGEMENT_TOOLS:
        return _manage_py(root) is not None
    return shutil.which(name) is not None


def _run(command, cwd, timeout=300):
    try:
        return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def _run_management(root, args, label, smell_type, severity):
    """Run a manage.py subcommand and turn a non-zero exit into one finding."""
    manage = _manage_py(root)
    if manage is None:
        return []
    result = _run([sys.executable, str(manage), *args], manage.parent)
    output = ((result.stdout or "") + (result.stderr or "")).strip()

    # A management command that cannot even start (no settings, no database) is
    # not a finding about the code — say so rather than reporting it as one.
    if "ImproperlyConfigured" in output or "ModuleNotFoundError" in output:
        return [_finding(
            manage, 1, "external_tool_unavailable",
            label + " could not run: " + output.splitlines()[-1][:180] if output else label,
            "It needs a working settings module and environment. Run it yourself with "
            "DJANGO_SETTINGS_MODULE set — this is one of the checks the parser cannot replace.",
            "low")]

    if result.returncode == 0:
        return []
    return [_finding(manage, 1, smell_type, label + " reported a problem:\n" + output[:1500],
                     "Read the output above; each line names its own fix.", severity)]


def run_tools(root, only=None, fix=False, run_migrations_check=False):
    root = Path(root)
    findings = []
    ran = []
    missing = {}

    wanted = set(only) if only else set(TOOLS)
    if not run_migrations_check:
        wanted.discard("makemigrations-check")

    for name in sorted(wanted):
        if name not in TOOLS:
            continue
        if not _available(name, root):
            missing[name] = TOOLS[name]["hint"] + "  (" + TOOLS[name]["answers"] + ")"
            continue

        if name == "django-upgrade":
            # Reports only, unless --fix. django-upgrade has no check mode, so
            # without --fix the honest thing is to say what it would do.
            if not fix:
                ran.append("django-upgrade (not run: it only rewrites; pass --fix)")
                continue
            version = V.CURRENT_RELEASE
            ctx = build_context(root, quiet=True)
            if ctx is not None and ctx.version:
                version = ctx.version
            target = str(version[0]) + "." + str(version[1])
            files = [str(p) for p in root.rglob("*.py")
                     if ".venv" not in p.parts and "node_modules" not in p.parts]
            result = _run(["django-upgrade", "--target-version", target, *files[:2000]], root)
            ran.append("django-upgrade --target-version " + target + " (MUTATED FILES)")
            if result.stderr.strip():
                findings.append(_finding(
                    root, 1, "external_tool_note",
                    "django-upgrade output:\n" + result.stderr[:1500],
                    "Review the diff before committing — it rewrote files in place.",
                    "low"))
            continue

        if name == "check-deploy":
            ran.append("manage.py check --deploy")
            findings.extend(_run_management(
                root, ["check", "--deploy"], "manage.py check --deploy",
                "deploy_check_failed", "high"))
            continue

        if name == "makemigrations-check":
            ran.append("manage.py makemigrations --check --dry-run")
            findings.extend(_run_management(
                root, ["makemigrations", "--check", "--dry-run"],
                "manage.py makemigrations --check", "missing_migration", "high"))
            continue

        if name == "djlint":
            ran.append("djlint")
            result = _run(["djlint", str(root), "--profile", "django"], root)
            for line in (result.stdout or "").splitlines():
                if ".html" in line and ":" in line:
                    findings.append(_finding(
                        line.split(":")[0], 1, "template_lint",
                        line.strip()[:300], "See djlint's rule documentation.", "low"))
            continue

        if name == "mypy":
            ran.append("mypy")
            result = _run(["mypy", str(root)], root)
            for line in (result.stdout or "").splitlines():
                if ": error:" in line:
                    parts = line.split(":", 3)
                    findings.append(_finding(
                        parts[0], int(parts[1]) if parts[1].isdigit() else 1,
                        "type_error", line.strip()[:300],
                        "Fix the annotation or the call.", "medium"))
            continue

        if name == "bandit":
            ran.append("bandit")
            result = _run(["bandit", "-r", str(root), "-f", "json", "-q"], root)
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            for issue in payload.get("results", [])[:200]:
                findings.append(_finding(
                    issue.get("filename", root), issue.get("line_number", 1),
                    "bandit_" + str(issue.get("test_id", "issue")).lower(),
                    issue.get("issue_text", "")[:300],
                    "See " + str(issue.get("more_info", "bandit's documentation")),
                    {"HIGH": "high", "MEDIUM": "medium"}.get(
                        issue.get("issue_severity", ""), "low")))
            continue

        if name == "pip-audit":
            ran.append("pip-audit")
            requirements = root / "requirements.txt"
            command = ["pip-audit", "-f", "json"]
            scope = "the installed environment"
            if requirements.exists():
                command += ["-r", str(requirements)]
                scope = "requirements.txt"
            result = _run(command, root, timeout=420)
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            entries = payload.get("dependencies", payload if isinstance(payload, list) else [])
            for entry in entries:
                for vuln in entry.get("vulns", []) or []:
                    findings.append(_finding(
                        requirements if requirements.exists() else root, 1,
                        "vulnerable_dependency",
                        entry.get("name", "?") + " " + entry.get("version", "?") + " — " +
                        vuln.get("id", "") + " (from " + scope + ")",
                        "Upgrade to " + ", ".join(vuln.get("fix_versions", []) or ["a fixed version"]) + ".",
                        "high"))
            continue

    return {"tools_run": ran, "missing_tools": missing, "findings": findings}


def render(report):
    lines = ["\n🎸 EXTERNAL TOOLS", "=" * 66]
    if report["tools_run"]:
        lines.append("ran: " + ", ".join(report["tools_run"]))
    else:
        lines.append("ran: nothing — none of the tools are installed")
    if report["missing_tools"]:
        lines.append("\nnot installed (this script never installs anything — ask first):")
        for name, hint in sorted(report["missing_tools"].items()):
            lines.append("  " + name + ": " + hint)
    lines.append("")
    findings = report["findings"]
    if not findings:
        lines.append("✅ No findings from the tools that ran.")
        return "\n".join(lines)
    lines.append(str(len(findings)) + " finding(s)\n")
    for f in findings[:200]:
        lines.append("[" + f["severity"].upper() + "] " + str(f["file"]) + ":" + str(f["line"]) +
                     "  " + f["smell_type"])
        lines.append("   " + f["description"])
    return "\n".join(lines)


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Run the real Django tools that are already installed")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--tools", default="",
                        help="Comma-separated subset (" + ", ".join(sorted(TOOLS)) + ")")
    parser.add_argument("--fix", action="store_true",
                        help="Let django-upgrade REWRITE files in place")
    parser.add_argument("--run-migrations-check", action="store_true",
                        help="Run makemigrations --check (imports the project and its settings)")
    args = parser.parse_args()

    only = [t.strip() for t in args.tools.split(",") if t.strip()] or None
    report = run_tools(args.path, only=only, fix=args.fix,
                       run_migrations_check=args.run_migrations_check)
    print(json.dumps(report, indent=2) if args.format == "json" else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
