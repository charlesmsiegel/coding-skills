"""Shared scaffolding for the Django detector tests.

Fixtures are written to tmp_path at runtime rather than committed, so the
deliberately-bad Django code never trips the repo's own linters or this skill's
own detectors.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "django-simplifier" / "scripts"


def build_project(root: Path, files: dict[str, str]) -> Path:
    """Write a Django-shaped project (manage.py plus the given files)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text("import django\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    """Run one detector over a project and return its findings."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        # The detectors warn on stderr through the console encoding, which is cp1252
        # on Windows — decode leniently so a warning cannot fail an assertion about
        # stdout, which is always JSON.
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, script + " exited " + str(result.returncode) + ": " + result.stderr[:500]
    return json.loads(result.stdout)


def smells(findings: list[dict]) -> set[str]:
    return {f["smell_type"] for f in findings}


def severities(findings: list[dict], smell_type: str) -> list[str]:
    return [f["severity"] for f in findings if f["smell_type"] == smell_type]
