#!/usr/bin/env python3
"""Snapshot a worktree's code budget; reject net growth at the end of a run.

Git and Python's standard library suffice. Explicit, frozen path globs define
code and tests; '*' matches '/' too, so '*.py' includes nested Python files.
Physical and nonblank lines are mechanical ceilings, not semantic SLOC: review
must still reject comment deletion, minification, or weakened behavior as credit.
"""

import argparse
import fnmatch
import json
import os
import subprocess
from pathlib import Path


METRICS = ("code", "code_nonblank", "production", "production_nonblank")


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def inventory(root: Path, includes: list[str], previous: dict) -> dict:
    names = {os.fsdecode(name) for name in git(root, "ls-files", "-co", "--exclude-standard", "-z").split(b"\0") if name}
    files = {}
    # Remember baseline paths even if a later ignore rule hides an untracked file.
    for name in sorted(names | previous.keys()):
        path = root / name
        if path.is_dir():
            raise ValueError(f"cannot measure nested repository or submodule boundary: {name}")
        if not matches(name, includes):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"cannot measure linked source: {name}")
        if not path.exists():
            continue  # A deleted file contributes zero lines.
        data = path.read_bytes()
        if b"\0" in data:
            raise ValueError(f"cannot measure binary or NUL-encoded source: {name}")
        lines = data.splitlines()
        files[name] = {"lines": len(lines), "nonblank": sum(bool(line.strip()) for line in lines)}
    return files


def totals(files: dict, tests: list[str]) -> dict:
    result = dict.fromkeys(METRICS, 0)
    for name, counts in files.items():
        result["code"] += counts["lines"]
        result["code_nonblank"] += counts["nonblank"]
        if not matches(name, tests):
            result["production"] += counts["lines"]
            result["production_nonblank"] += counts["nonblank"]
    return result


def run(args) -> tuple[dict, int]:
    root = args.root.resolve(strict=True)
    if args.command == "snapshot":
        actual = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
        if actual != root:
            raise ValueError("--root must be the Git worktree root, including for a scoped refactor")
        files = inventory(root, args.include, {})
        if not files:
            raise ValueError("no code matched --include; establish a measurable scope before editing")
        baseline = {"version": 1, "root": str(root), "include": args.include, "tests": args.tests, "files": files}
        with args.baseline.open("x", encoding="utf-8") as stream:
            json.dump(baseline, stream, indent=2)
        return {"baseline": str(args.baseline), "files": len(files), **totals(files, args.tests)}, 0

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if baseline["version"] != 1 or Path(baseline["root"]) != root:
        raise ValueError("baseline version or worktree root does not match")
    before = totals(baseline["files"], baseline["tests"])
    files = inventory(root, baseline["include"], baseline["files"])
    after = totals(files, baseline["tests"])
    report = {key: {"before": before[key], "after": after[key], "delta": after[key] - before[key]} for key in METRICS}
    report["passed"] = all(after[key] <= before[key] for key in METRICS)
    report["changed_files"] = {
        name: {"before": baseline["files"].get(name), "after": files.get(name)}
        for name in sorted(baseline["files"].keys() | files.keys())
        if baseline["files"].get(name) != files.get(name)
    }
    return report, 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("snapshot", "check"):
        sub = commands.add_parser(command)
        sub.add_argument("--root", type=Path, required=True, help="Git worktree root")
        sub.add_argument("--baseline", type=Path, required=True, help="unique baseline path; snapshot never overwrites")
        if command == "snapshot":
            sub.add_argument("--include", action="append", required=True, help="repeat for every code path/type, including tests")
            sub.add_argument("--tests", action="append", default=[], help="repeat for test paths/types; remaining code is production")
    args = parser.parse_args()
    try:
        report, status = run(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        report, status = {"error": str(exc), "passed": False}, 2
    print(json.dumps(report, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
