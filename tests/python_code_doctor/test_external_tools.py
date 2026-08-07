"""Tests for run_external_tools.py — the bridge to the real Python quality tools.

CI cannot assume ruff, pip-audit, or coverage are installed, so these tests split
the same way the script does: the shell-out layer is exercised structurally (each
requested tool lands in tools_run or missing_tools), and the parsing layer is fed
a *stub executable* that replays output captured from the real tools. The fixtures
below are verbatim excerpts of `pip-audit --format=json` (2.9) and
`coverage json -o -` (7.9.2 and 7.15.2), so a parser change that would break on
real output breaks here.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


@pytest.fixture
def external(load_module):
    return load_module(SCRIPTS_DIR, "run_external_tools")


def stub_tool(tmp_path: Path, stdout: str, exit_code: int = 0, name: str = "stub.py") -> list[str]:
    """An argv prefix that replays `stdout` and exits with `exit_code`.

    Substituting this for a tool's real invocation is what lets the parsers be
    tested deterministically on machines where the tool is not installed.
    """
    script = tmp_path / name
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


# ---- captured tool output -------------------------------------------------- #

PIP_AUDIT_VULNERABLE = json.dumps({
    "dependencies": [
        {"name": "jinja2", "version": "2.11.0", "vulns": [
            {"id": "PYSEC-2021-66", "fix_versions": ["2.11.3"],
             "aliases": ["CVE-2020-28493", "GHSA-g3rq-g295-4j3m"],
             "description": "This affects the package jinja2 before 2.11.3. The ReDoS "
                            "vulnerability is due to the _punctuation_re regex."},
            {"id": "PYSEC-2024-9999", "fix_versions": [],
             "aliases": [],
             "description": "An unfixed issue."},
        ]},
        {"name": "attrs", "version": "25.3.0", "vulns": []},
    ],
    "fixes": [],
})

PIP_AUDIT_CLEAN = json.dumps({"dependencies": [{"name": "attrs", "version": "25.3.0", "vulns": []}],
                              "fixes": []})

# coverage >= 7.10 — carries start_line per function.
COVERAGE_NEW = json.dumps({
    "meta": {"format": 3, "version": "7.15.2"},
    "files": {
        "mod.py": {
            "summary": {"covered_lines": 3, "num_statements": 6, "percent_covered": 50.0,
                        "missing_lines": 3},
            "missing_lines": [5, 6, 7],
            "functions": {
                "used": {"summary": {"covered_lines": 1, "num_statements": 1},
                         "missing_lines": [], "start_line": 1},
                "unused": {"summary": {"covered_lines": 0, "num_statements": 3},
                           "missing_lines": [5, 6, 7], "start_line": 4},
                "": {"summary": {"covered_lines": 2, "num_statements": 2},
                     "missing_lines": [], "start_line": 1},
            },
        },
        "orphan.py": {
            "summary": {"covered_lines": 0, "num_statements": 2, "percent_covered": 0.0,
                        "missing_lines": 2},
            "missing_lines": [1, 2],
            "functions": {"orphan": {"summary": {"covered_lines": 0, "num_statements": 1},
                                     "missing_lines": [2], "start_line": 1}},
        },
        ".venv/lib/site-packages/vendored.py": {
            "summary": {"covered_lines": 0, "num_statements": 9}, "missing_lines": [1],
            "functions": {},
        },
    },
})

# coverage 7.9.2 — same shape, but no start_line key anywhere.
COVERAGE_OLD = json.dumps({
    "meta": {"format": 3, "version": "7.9.2"},
    "files": {
        "mod.py": {
            "summary": {"covered_lines": 3, "num_statements": 6},
            "missing_lines": [5, 6, 7],
            "functions": {
                "unused": {"summary": {"covered_lines": 0, "num_statements": 3},
                           "missing_lines": [5, 6, 7]},
            },
        },
    },
})


# ---- shell-out layer (structural; assumes nothing is installed) ------------- #

def test_every_requested_tool_is_either_run_or_missing(tmp_path):
    (tmp_path / "sample.py").write_text('"""Tiny module."""\n\nX = 1\n')
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "run_external_tools.py"), str(tmp_path),
         "--format", "json", "--tools", "ruff,black,pip-audit,coverage"],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[:500]
    report = json.loads(result.stdout)
    assert {"tools_run", "missing_tools", "findings"} <= report.keys()
    ran = set(report["tools_run"])
    missing = {m["name"] for m in report["missing_tools"]}
    assert ran | missing == {"ruff", "black", "pip-audit", "coverage"}
    assert not (ran & missing)
    for m in report["missing_tools"]:
        assert m["install"].startswith("pip install ")


def test_new_tools_are_not_formatters(external):
    # Only formatters may be invoked by --fix; a --fix run must never be able to
    # trigger a network audit or execute the project's tests.
    for name in ("pip-audit", "coverage"):
        assert external.TOOLS[name][3] is False


# ---- pip-audit parsing ------------------------------------------------------ #

def test_pip_audit_reports_one_high_finding_per_vulnerability(external, tmp_path):
    (tmp_path / "requirements.txt").write_text("jinja2==2.11.0\n")
    findings = external.run_pip_audit(stub_tool(tmp_path, PIP_AUDIT_VULNERABLE, 1), str(tmp_path))

    assert len(findings) == 2
    assert all(f["severity"] == "high" for f in findings)
    # A CVE alias is more recognizable than the PYSEC id, so it wins when present.
    by_type = {f["smell_type"]: f for f in findings}
    assert "pip-audit:CVE-2020-28493" in by_type
    assert "pip-audit:PYSEC-2024-9999" in by_type
    assert "Upgrade jinja2 to 2.11.3" in by_type["pip-audit:CVE-2020-28493"]["suggestion"]
    # An advisory with no fix must not claim there is one to upgrade to.
    assert "No fixed version" in by_type["pip-audit:PYSEC-2024-9999"]["suggestion"]
    assert all(f["file"].endswith("requirements.txt") for f in findings)


def test_pip_audit_is_silent_when_nothing_is_vulnerable(external, tmp_path):
    (tmp_path / "requirements.txt").write_text("attrs==25.3.0\n")
    assert external.run_pip_audit(stub_tool(tmp_path, PIP_AUDIT_CLEAN, 0), str(tmp_path)) == []


def test_pip_audit_labels_the_environment_fallback(external, tmp_path):
    # No requirements file: pip-audit falls back to auditing the interpreter's own
    # environment, which is a different claim and has to say so.
    findings = external.run_pip_audit(stub_tool(tmp_path, PIP_AUDIT_VULNERABLE, 1), str(tmp_path))
    assert findings
    assert all("installed environment" in f["description"] for f in findings)


def test_pip_audit_crash_is_reported_not_swallowed(external, tmp_path):
    (tmp_path / "requirements.txt").write_text("attrs==25.3.0\n")
    findings = external.run_pip_audit(stub_tool(tmp_path, "boom", 2), str(tmp_path))
    assert [f["smell_type"] for f in findings] == ["pip-audit:tool-error"]


def test_requirements_files_are_found_shallowest_first_and_skip_vendor_dirs(external, tmp_path):
    (tmp_path / "requirements.txt").write_text("a\n")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "requirements-dev.txt").write_text("b\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "requirements.txt").write_text("c\n")

    found = [p.name for p in external._requirements_files(str(tmp_path))]
    assert found == ["requirements.txt", "requirements-dev.txt"]


# ---- coverage parsing ------------------------------------------------------- #

def test_coverage_reports_uncovered_modules_and_functions(external, tmp_path):
    findings = external.run_coverage(stub_tool(tmp_path, COVERAGE_NEW, 0), str(tmp_path))
    by_type = {}
    for f in findings:
        by_type.setdefault(f["smell_type"], []).append(f)

    # A module with nothing executed is one finding, not one per function —
    # the module-level fact subsumes them.
    modules = by_type["coverage:uncovered-module"]
    assert [Path(f["file"]).name for f in modules] == ["orphan.py"]
    assert modules[0]["severity"] == "medium"

    functions = by_type["coverage:uncovered-function"]
    assert len(functions) == 1
    assert "unused()" in functions[0]["description"]
    assert functions[0]["line"] == 4  # start_line, not the file top
    assert functions[0]["severity"] == "low"


def test_coverage_ignores_vendored_files(external, tmp_path):
    findings = external.run_coverage(stub_tool(tmp_path, COVERAGE_NEW, 0), str(tmp_path))
    assert not any("site-packages" in str(f["file"]) for f in findings)


def test_coverage_locates_functions_without_start_line(external, tmp_path):
    # coverage < 7.10 omits start_line. Every statement of a fully-uncovered
    # function is missing, so its lowest missing line is the first statement.
    findings = external.run_coverage(stub_tool(tmp_path, COVERAGE_OLD, 0), str(tmp_path))
    assert [f["line"] for f in findings] == [5]


def test_coverage_distinguishes_missing_data_from_a_broken_tool(external, tmp_path):
    # "No data to report." goes to STDOUT with exit 1, so exit code alone cannot
    # tell an unmeasured project from a tool that fell over.
    findings = external.run_coverage(stub_tool(tmp_path, "No data to report.", 1), str(tmp_path))
    assert [f["smell_type"] for f in findings] == ["coverage:no-data"]
    assert "coverage run -m pytest" in findings[0]["description"]

    findings = external.run_coverage(stub_tool(tmp_path, "Traceback ...", 3), str(tmp_path))
    assert [f["smell_type"] for f in findings] == ["coverage:tool-error"]


def test_coverage_data_dir_prefers_the_directory_holding_dot_coverage(external, tmp_path):
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / ".coverage").write_text("")
    assert external._coverage_data_dir(str(tmp_path)) == nested
    # Nothing to find: fall back to the path itself rather than guessing.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert external._coverage_data_dir(str(empty)) == empty
