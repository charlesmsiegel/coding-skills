#!/usr/bin/env python3
"""
Drive the Rust toolchain where the project already has it, and normalise its
output into this skill's findings shape.

**This is where compilation errors come from.** The detectors in this skill read
syntax with their own scanner; they do not know a type from a lifetime and they
never will. `cargo check` does, and everything that needs a type — is this
lifetime valid, does this trait bound hold, is this `match` exhaustive, is this
`Send` — belongs to it. Run it first and believe it over anything here that
overlaps.

Four of these answer questions the detectors structurally *cannot*:

  - **cargo check** — the compiler. Borrow errors, trait resolution,
    exhaustiveness, and the type-driven half of correctness.
  - **cargo clippy** — 700+ lints with type information, which is what
    separates its `needless_clone` from this skill's syntactic guess.
  - **cargo audit / cargo deny** — published advisories. `find_cargo_issues.py`
    can say a range is unpinned; only an advisory database can say the version
    you have is vulnerable.
  - **coverage** — what actually executed. `find_untested_modules.py` answers
    "does a test import this"; coverage answers "does this code ever run".

It never installs anything, and it never runs the project's tests unless asked:
`--run-tests` is an explicit opt-in because a test suite is arbitrary code with
arbitrary side effects.

Usage:
  python run_external_tools.py .                    # every available tool, check only
  python run_external_tools.py . --format json      # {tools_run, missing_tools, findings}
  python run_external_tools.py . --tools check,clippy
  python run_external_tools.py . --fix              # cargo fmt / clippy --fix (MUTATES)
  python run_external_tools.py . --run-tests        # run the suite (SLOW, EXECUTES CODE)
"""

import argparse
import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from common import SEVERITY_ICONS, configure_output

_ICON = SEVERITY_ICONS

# rustc/clippy diagnostic level -> our severity.
_LEVEL = {"error": "high", "error: internal compiler error": "high",
          "warning": "medium", "note": "low", "help": "low"}


def _project_root(path: Path) -> Path:
    path = path if path.is_dir() else path.parent
    for candidate in [path, *path.parents]:
        if (candidate / "Cargo.toml").is_file():
            return candidate
    return path


def _run(argv, cwd, timeout=1800):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                cwd=str(cwd), shell=False, errors="replace")
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
        "suggestion": f"Resolve the {tool} finding ({code})." if code
                      else f"Resolve the {tool} finding.",
        "severity": severity,
    }


def _tool_error(tool, path, returncode, stderr):
    detail = (stderr or "").strip().splitlines()
    detail = detail[-1][:200] if detail else f"exit code {returncode}"
    return _finding(tool, path, 1, "tool-error",
                    f"{tool} did not complete ({detail}) — its results are missing from this report",
                    "medium")


def _parse_cargo_json(tool, root, stdout):
    """Cargo's `--message-format json` stream: one JSON object per line.

    `--all-targets` compiles the lib and its test harness, so the same
    diagnostic arrives once per target; identical records are collapsed.
    """
    findings = []
    seen = set()
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("reason") != "compiler-message":
            continue
        message = record.get("message") or {}
        level = message.get("level", "warning")
        if level in ("note", "help", "failure-note"):
            continue
        code = (message.get("code") or {}).get("code") or level
        primary = next((s for s in message.get("spans") or [] if s.get("is_primary")), None)
        file_name = primary.get("file_name") if primary else root
        line_number = primary.get("line_start") if primary else 1
        text = message.get("message", "")
        if primary and primary.get("label"):
            text = f"{text} ({primary['label']})"
        key = (str(file_name), line_number, code, text)
        if key in seen:
            continue
        seen.add(key)
        findings.append(_finding(tool, (root / str(file_name)).resolve() if primary else root,
                                 line_number, code, text, _LEVEL.get(level, "medium")))
    return findings


def run_check(argv, root):
    """`cargo check` — the compiler. Everything a type-aware detector could want."""
    returncode, out, err = _run([*argv, "check", "--all-targets", "--message-format", "json"], root)
    if returncode is None:
        return [_tool_error("cargo-check", root, returncode, err)]
    findings = _parse_cargo_json("cargo-check", root, out)
    if not findings and returncode != 0:
        # A non-zero exit with no diagnostic is a build that never started —
        # unresolved dependencies, a missing toolchain, no network. Reporting it
        # as "no findings" would read as a clean compile.
        return [_tool_error("cargo-check", root, returncode, err or out)]
    return findings


def run_clippy(argv, root):
    returncode, out, err = _run(
        [*argv, "clippy", "--all-targets", "--message-format", "json"], root)
    if returncode is None:
        return [_tool_error("cargo-clippy", root, returncode, err)]
    findings = _parse_cargo_json("cargo-clippy", root, out)
    if not findings and returncode != 0:
        return [_tool_error("cargo-clippy", root, returncode, err or out)]
    return findings


def run_fmt(argv, root):
    returncode, out, err = _run([*argv, "fmt", "--all", "--check"], root)
    if returncode is None:
        return [_tool_error("cargo-fmt", root, returncode, err)]
    if returncode == 0:
        return []
    files = sorted({m.group(1) for m in re.finditer(r"^Diff in (\S+)", out or "", re.MULTILINE)})
    if not files:
        return [_finding("cargo-fmt", root, 1, "unformatted",
                         "`cargo fmt --check` reports formatting differences", "low")]
    return [_finding("cargo-fmt", (root / f).resolve(), 1, "unformatted",
                     "not formatted as `rustfmt` would format it",
                     "low") for f in files]


def run_test(argv, root):
    """Compile the tests without running them: a test that no longer builds is a failure."""
    returncode, out, err = _run([*argv, "test", "--no-run", "--message-format", "json"], root)
    if returncode is None:
        return [_tool_error("cargo-test", root, returncode, err)]
    findings = _parse_cargo_json("cargo-test", root, out)
    if not findings and returncode != 0:
        return [_tool_error("cargo-test", root, returncode, err or out)]
    return findings


def run_audit(argv, root):
    returncode, out, err = _run([*argv, "audit", "--json"], root)
    if returncode is None:
        return [_tool_error("cargo-audit", root, returncode, err)]
    try:
        report = json.loads(out or "{}")
    except json.JSONDecodeError:
        return [_tool_error("cargo-audit", root, returncode, err or out)]
    findings = []
    for entry in (report.get("vulnerabilities") or {}).get("list") or []:
        advisory = entry.get("advisory") or {}
        package = entry.get("package") or {}
        patched = ", ".join((entry.get("versions") or {}).get("patched") or []) or "none published"
        findings.append({
            "tool": "cargo-audit",
            "file": str(root / "Cargo.lock"),
            "line": 1,
            "smell_type": f"cargo-audit:{advisory.get('id', 'RUSTSEC')}",
            "description": f"{package.get('name')} {package.get('version')}: "
                           f"{advisory.get('title', 'advisory')}",
            "suggestion": f"Upgrade to a patched version ({patched}). "
                          f"See {advisory.get('url') or advisory.get('id')}.",
            "severity": "high",
        })
    for entry in (report.get("warnings") or {}).values():
        for warning in entry if isinstance(entry, list) else []:
            package = warning.get("package") or {}
            findings.append(_finding(
                "cargo-audit", root / "Cargo.lock", 1, warning.get("kind", "warning"),
                f"{package.get('name')} {package.get('version')}: "
                f"{(warning.get('advisory') or {}).get('title', warning.get('kind', 'warning'))}",
                "medium"))
    return findings


def run_deny(argv, root):
    returncode, out, err = _run([*argv, "deny", "--format", "json", "check"], root)
    if returncode is None:
        return [_tool_error("cargo-deny", root, returncode, err)]
    findings = []
    for line in (err or "").splitlines() + (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        fields = record.get("fields") or {}
        severity = {"error": "high", "warning": "medium"}.get(record.get("type"), "low")
        message = fields.get("message")
        if not message:
            continue
        findings.append(_finding("cargo-deny", root / "deny.toml", 1,
                                 fields.get("code", "deny"), message, severity))
    return findings


def run_udeps(argv, root):
    returncode, out, err = _run([*argv, "+nightly", "udeps", "--all-targets",
                                 "--output", "json"], root)
    if returncode is None or returncode not in (0, 1):
        return [_tool_error("cargo-udeps", root, returncode, err or out)]
    try:
        report = json.loads(out or "{}")
    except json.JSONDecodeError:
        return []
    findings = []
    for target, entry in (report.get("unused_deps") or {}).items():
        for name in entry.get("normal", []) + entry.get("development", []):
            findings.append(_finding(
                "cargo-udeps", root / "Cargo.toml", 1, "unused-dependency",
                f"{name} is declared for {target} and never used", "low"))
    return findings


def run_tree(argv, root):
    """Duplicate versions of one crate: slower builds, and two incompatible types."""
    returncode, out, err = _run([*argv, "tree", "--duplicates"], root)
    if returncode is None or returncode != 0:
        return [_tool_error("cargo-tree", root, returncode, err or out)]
    duplicates = sorted({m.group(1) for m in
                         re.finditer(r"^(\S+) v[\d.]+", out or "", re.MULTILINE)})
    if not duplicates:
        return []
    return [_finding(
        "cargo-tree", root / "Cargo.toml", 1, "duplicate-versions",
        f"{len(duplicates)} crate(s) appear at more than one version: "
        f"{', '.join(duplicates[:8])}"
        + (f" (+{len(duplicates) - 8} more)" if len(duplicates) > 8 else ""),
        "low")]


def _coverage_file(root: Path):
    for name in ("lcov.info", "coverage/lcov.info", "target/llvm-cov/lcov.info",
                 "cobertura.xml", "target/tarpaulin/lcov.info"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def run_coverage(_argv, root):
    """Code that never executes, per coverage data already on disk."""
    data_file = _coverage_file(root)
    if data_file is None:
        return [_finding("coverage", root, 1, "no-data",
                         "No coverage data found — run `cargo llvm-cov --lcov "
                         "--output-path lcov.info` (or pass --run-tests, which does)", "low")]
    if data_file.suffix != ".info":
        return []
    findings = []
    current, hits = None, 0
    for line in data_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SF:"):
            current, hits = line[3:], 0
        elif line.startswith("DA:") and line.rsplit(",", 1)[-1].strip() not in ("0", ""):
            hits += 1
        elif line.startswith("end_of_record") and current is not None:
            if hits == 0:
                findings.append(_finding("coverage", (root / current).resolve(), 1,
                                         "uncovered-file",
                                         "no line in this file is executed by the test suite",
                                         "medium"))
            current = None
    return findings


# name -> (how to get it, check runner, mutating-fix argv or None)
TOOLS = {
    "check":    ("ships with cargo", run_check, None),
    "clippy":   ("rustup component add clippy", run_clippy, ["clippy", "--fix", "--allow-dirty"]),
    "fmt":      ("rustup component add rustfmt", run_fmt, ["fmt", "--all"]),
    "test":     ("ships with cargo", run_test, None),
    "audit":    ("cargo install cargo-audit", run_audit, None),
    "deny":     ("cargo install cargo-deny", run_deny, None),
    "udeps":    ("cargo install cargo-udeps (needs a nightly toolchain)", run_udeps, None),
    "tree":     ("ships with cargo", run_tree, None),
    "coverage": ("cargo install cargo-llvm-cov", run_coverage, None),
}

# Subcommands that come with cargo itself and need no separate probe.
_BUILTIN = {"check", "test", "tree"}


def _available(name: str, cargo: list[str], root: Path) -> bool:
    if name == "coverage":
        return True  # a reader over files on disk, not an executable
    if name in _BUILTIN:
        return True
    if name in ("clippy", "fmt"):
        returncode, _out, _err = _run([*cargo, name, "--version"], root, timeout=120)
        return returncode == 0
    if shutil.which(f"cargo-{name}"):
        return True
    returncode, _out, _err = _run([*cargo, name, "--version"], root, timeout=120)
    return returncode == 0


def apply_fixes(available, cargo, root):
    notes = []
    for tool in ("fmt", "clippy"):
        if tool not in available or TOOLS[tool][2] is None:
            continue
        returncode, _out, _err = _run([*cargo, *TOOLS[tool][2]], root)
        notes.append(f"{tool}: {'applied' if returncode == 0 else 'ran (exit %s)' % returncode}")
    return notes


def measure_coverage(cargo, root):
    """Run the suite under llvm-cov so run_coverage has data. Executes the tests."""
    returncode, _out, err = _run(
        [*cargo, "llvm-cov", "--lcov", "--output-path", "lcov.info"], root, timeout=3600)
    if returncode is None:
        return [f"coverage: could not run cargo llvm-cov ({err.strip()[:120]})"]
    return [f"coverage: ran `cargo llvm-cov` in {root} (exit {returncode})"]


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Run the installed Rust tools and normalize their output")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--tools", type=str, default="",
                        help=f"Comma-separated subset (default: all of {', '.join(TOOLS)})")
    parser.add_argument("--fix", action="store_true",
                        help="Also run `cargo fmt` and `cargo clippy --fix` (MUTATES files)")
    parser.add_argument("--run-tests", action="store_true",
                        help="Run the suite under coverage first (SLOW; EXECUTES the tests)")
    args = parser.parse_args()

    root = _project_root(Path(args.path).resolve())
    cargo_binary = shutil.which("cargo")
    if cargo_binary is None:
        print("cargo is not on PATH — none of these tools can run, and no compilation check "
              "was performed. Install Rust (https://rustup.rs) or run this on a machine that "
              "has it.")
        if args.format == "json":
            print(json.dumps({"project_root": str(root), "tools_run": [],
                              "missing_tools": [{"name": "cargo", "install": "https://rustup.rs"}],
                              "actions_taken": [], "findings": []}, indent=2))
        return
    cargo = [cargo_binary]

    wanted = set(args.tools.split(",")) if args.tools else set(TOOLS)
    wanted = {name for name in wanted if name in TOOLS}

    available, missing = {}, []
    for name in TOOLS:
        if name not in wanted:
            continue
        if _available(name, cargo, root):
            available[name] = cargo
        else:
            missing.append({"name": f"cargo {name}", "install": TOOLS[name][0]})

    # Both pre-steps run FIRST so the findings describe the post-action state.
    action_notes = apply_fixes(available, cargo, root) if args.fix else []
    if args.run_tests:
        action_notes += measure_coverage(cargo, root)

    findings = []
    for name, argv in available.items():
        runner = TOOLS[name][1]
        try:
            findings.extend(runner(argv, root))
        except Exception as exc:  # one tool failing must not sink the rest
            findings.append(_finding(name, root, 1, "tool-error",
                                     f"cargo {name} failed to run: {exc}", "medium"))

    # clippy runs the compiler, so every `cargo check` error comes back a second
    # time under its name. Keep the compiler's copy.
    from_check = {(f["file"], f["line"], f["description"]) for f in findings
                  if f["tool"] == "cargo-check"}
    findings = [f for f in findings
                if f["tool"] != "cargo-clippy"
                or (f["file"], f["line"], f["description"]) not in from_check]

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

    print(f"\n🔧 RUST TOOLCHAIN in {root} — ran: {', '.join(report['tools_run']) or '(none)'}")
    print("=" * 60)
    if missing:
        print("Not available here (ask the user before installing):")
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
