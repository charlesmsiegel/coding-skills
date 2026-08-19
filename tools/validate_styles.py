#!/usr/bin/env python3
"""Validate the structure of every output style under styles/.

Styles are not skills: they are flat Markdown files appended to Claude Code's
system prompt, with their own frontmatter vocabulary. They share the
frontmatter *parser* with tools/validate_skills.py — the YAML-quoting rules are
the same and one careful implementation is enough — and nothing else.

The rule that earns this file is `keep-coding-instructions`. A custom style
replaces Claude Code's built-in software engineering instructions unless the
style opts back in, so a misspelled key or a non-boolean value turns a style
meant to sharpen coding behavior into one that deletes it. Nothing about that
failure is visible at runtime.

Usage:
  python tools/validate_styles.py
  python tools/validate_styles.py --styles-dir styles
"""

import argparse
import sys
from pathlib import Path

from validate_skills import (
    FRONTMATTER_RE,
    MAX_DESCRIPTION,
    MAX_NAME,
    NAME_RE,
    REPO_ROOT,
    parse_frontmatter,
)

# Claude Code reads exactly these. `force-for-plugin` is deliberately absent:
# it only applies to styles shipped inside a plugin, and this repo ships files.
ALLOWED_KEYS = {"name", "description", "keep-coding-instructions"}

# YAML has other spellings of true; Claude Code's frontmatter reader is not the
# place to find out which ones it honours. Two literals, both lowercase.
BOOLEANS = {"true", "false"}


def check_style(path: Path) -> list[str]:
    """Every structural problem with one style file, as human-readable strings."""
    errors: list[str] = []
    stem = path.stem
    text = path.read_text(encoding="utf-8")

    # The filename is the install name under .claude/output-styles/ and the
    # #style-<name> handle after install.sh translates it for Kiro.
    if not NAME_RE.match(stem):
        errors.append(f"{path.name}: filename {stem!r} is not hyphen-case (lowercase, digits, single hyphens)")

    fields, fm_errors = parse_frontmatter(text)
    errors += [f"{path.name}: {e}" for e in fm_errors]

    unknown = sorted(set(fields) - ALLOWED_KEYS)
    if unknown:
        errors.append(f"{path.name}: unsupported frontmatter key(s) {unknown}; allowed: {sorted(ALLOWED_KEYS)}")

    declared = fields.get("name", "")
    if not declared:
        errors.append(f"{path.name}: frontmatter has no name — the /config picker would show the filename")
    elif len(declared) > MAX_NAME:
        errors.append(f"{path.name}: name is {len(declared)} chars; the limit is {MAX_NAME}")

    description = fields.get("description", "")
    if not description:
        errors.append(f"{path.name}: frontmatter has no description — the /config picker would show nothing")
    elif len(description) > MAX_DESCRIPTION:
        errors.append(f"{path.name}: description is {len(description)} chars; the limit is {MAX_DESCRIPTION}")

    if "keep-coding-instructions" in fields:
        value = fields["keep-coding-instructions"]
        if value not in BOOLEANS:
            errors.append(
                f"{path.name}: keep-coding-instructions is {value!r}; it must be `true` or `false`. "
                "Anything else parses as a string, which is truthy nowhere and silently drops "
                "Claude Code's built-in software engineering instructions"
            )

    match = FRONTMATTER_RE.match(text)
    body = text[match.end():] if match else text
    if not body.strip():
        errors.append(f"{path.name}: frontmatter only, no body")
    elif declared:
        # styles/README.md promises every style opens with this marker, mirroring
        # Anthropic's built-ins. A promise CI does not check is a promise that rots.
        marker = f"Style Active: {declared}"
        if marker not in body:
            errors.append(f"{path.name}: body does not contain the marker {marker!r}")

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--styles-dir", type=Path, default=REPO_ROOT / "styles")
    args = ap.parse_args()

    if not args.styles_dir.is_dir():
        print(f"error: {args.styles_dir} is not a directory", file=sys.stderr)
        return 2

    # README.md documents the set rather than being one of them, and install.sh
    # skips it by the same rule.
    candidates = sorted(p for p in args.styles_dir.glob("*.md") if p.name != "README.md")
    if not candidates:
        print(f"error: no styles found in {args.styles_dir}", file=sys.stderr)
        return 2

    errors = []
    for path in candidates:
        errors += check_style(path)

    for error in errors:
        print(f"error: {error}", file=sys.stderr)

    checked = ", ".join(p.stem for p in candidates)
    if errors:
        print(f"\n{len(errors)} problem(s) across {len(candidates)} style(s): {checked}", file=sys.stderr)
        return 1
    print(f"{len(candidates)} style(s) valid: {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
