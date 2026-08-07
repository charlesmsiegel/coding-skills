#!/usr/bin/env python3
"""Which Django is this project on?

Every version-conditional finding rests on this answer, which is why the
function returns *where the answer came from* alongside the answer. A pin in
pyproject.toml and whatever happens to be installed in the current
interpreter are different claims, and a report that blurs them will eventually
tell someone their Django 4 project is fine because the reviewer's virtualenv
had 6.1 in it.

When nothing answers, this returns None. The detectors then stay quiet about
anything version-dependent and the report says the version is unknown —
because a version-conditional finding built on a guess is worse than no
finding at all.
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

from django_versions import parse_version

# `Django`, `django`, and the odd `Django[argon2]` extras form.
_REQUIREMENT = re.compile(r"^\s*django\s*(\[[^\]]*\])?\s*([<>=!~^ ].*)?$", re.IGNORECASE)

MANIFESTS = (
    "pyproject.toml",
    "requirements.txt", "requirements/base.txt", "requirements/production.txt",
    "requirements/prod.txt", "requirements/common.txt",
    "constraints.txt", "setup.cfg", "setup.py", "Pipfile",
)


def _from_requirement_line(line):
    """'Django>=4.2,<5.0' -> (4, 2). Returns None for any other package."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    match = _REQUIREMENT.match(line)
    if not match:
        return None
    specifier = match.group(2) or ""
    # An unpinned `Django` says nothing about the version.
    return parse_version(specifier)


def _from_requirements_text(text):
    for line in text.splitlines():
        found = _from_requirement_line(line)
        if found:
            return found
    return None


def _from_pyproject(path):
    """PEP 621 dependencies, Poetry's table, and PDM/Hatch's optional groups."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None, ""

    project = data.get("project") or {}
    for entry in project.get("dependencies") or []:
        found = _from_requirement_line(str(entry))
        if found:
            return found, "pyproject.toml [project.dependencies]"

    for group, entries in (project.get("optional-dependencies") or {}).items():
        for entry in entries or []:
            found = _from_requirement_line(str(entry))
            if found:
                return found, "pyproject.toml [project.optional-dependencies." + str(group) + "]"

    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for table in ("dependencies", "dev-dependencies"):
        for name, spec in (poetry.get(table) or {}).items():
            if name.lower() != "django":
                continue
            # Poetry allows both `django = "^5.2"` and `django = {version = "^5.2"}`.
            raw = spec.get("version") if isinstance(spec, dict) else spec
            found = parse_version(raw)
            if found:
                return found, "pyproject.toml [tool.poetry." + table + "]"
    return None, ""


def _from_setup_cfg(path):
    """install_requires in a setup.cfg, which is an indented list under a key."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _from_requirements_text(text)


def _from_pipfile(path):
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for table in ("packages", "dev-packages"):
        for name, spec in (data.get(table) or {}).items():
            if name.lower() != "django":
                continue
            raw = spec.get("version") if isinstance(spec, dict) else spec
            found = parse_version(raw)
            if found:
                return found
    return None


def _from_installed_environment():
    """What is importable right now — a different claim, and labelled as one.

    Imported rather than shelled out to, because `pip show` costs a subprocess
    and this is a fallback nobody should be relying on anyway.
    """
    try:
        from importlib.metadata import version as _version
        return parse_version(_version("django"))
    except Exception:      # not installed, or a metadata backend that disagrees
        return None


def detect_django_version(root):
    """Returns (version, source). ``version`` is None when nothing answers.

    ``source`` always describes what happened, so a caller can print it — that
    is the difference between "pinned at 4.2" and "4.2 happens to be installed
    in the interpreter running this script".
    """
    root = Path(root)
    if root.is_file():
        root = root.parent

    # Search the scanned root and its parents: a detector is often pointed at
    # `src/myapp/` while the manifest sits at the repository root.
    candidates = [root] + list(root.parents)[:3]

    for base in candidates:
        pyproject = base / "pyproject.toml"
        if pyproject.exists():
            found, source = _from_pyproject(pyproject)
            if found:
                return found, source

        for relative in MANIFESTS:
            if relative == "pyproject.toml":
                continue
            path = base / relative
            if not path.exists():
                continue
            if path.name == "Pipfile":
                found = _from_pipfile(path)
            elif path.name == "setup.cfg":
                found = _from_setup_cfg(path)
            else:
                try:
                    found = _from_requirements_text(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    found = None
            if found:
                return found, str(relative)

        # requirements/*.txt under any name, once the well-known ones miss.
        requirements_dir = base / "requirements"
        if requirements_dir.is_dir():
            for path in sorted(requirements_dir.glob("*.txt")):
                try:
                    found = _from_requirements_text(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if found:
                    return found, "requirements/" + path.name

    installed = _from_installed_environment()
    if installed:
        return installed, "the Django installed in the current interpreter (not a project pin)"

    return None, "no Django pin found in pyproject.toml, requirements*.txt, setup.cfg, or Pipfile"


def main():
    parser = argparse.ArgumentParser(
        prog="django_detect_version",
        description="Report which Django version a project pins, and where the pin was read from")
    parser.add_argument("path", nargs="?", default=".", help="Project root")
    args = parser.parse_args()

    version, source = detect_django_version(args.path)
    # "unknown" is an answer, not a failure — the caller's job is to notice the
    # word, and a non-zero exit would make an ordinary outcome look like a crash.
    if version is None:
        print("Django version: unknown - " + source)
        return 0
    print("Django version: " + str(version[0]) + "." + str(version[1]) + "  (from " + source + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
