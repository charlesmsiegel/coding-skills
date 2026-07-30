#!/usr/bin/env python3
"""Validate the structure of every skill under skills/.

The upload contract (Skills API / claude.ai) was previously only checked by the
release workflow, which runs on a tag — so a malformed description or a renamed
directory was caught after someone decided to ship, not when they wrote it. This
runs on every pull request, and the release workflow calls the same code, so the
two cannot disagree about what "valid" means.

Scope is structure. Whether a skill's *content* is any good is what tests/ and
evals/ are for; whether it works when installed alone is
tests/test_standalone_install.py.

Usage:
  python tools/validate_skills.py                  # every skill
  python tools/validate_skills.py --skill fix-pr   # just one (the release path)
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Limits the Skills API enforces on upload.
MAX_DESCRIPTION = 1024
MAX_NAME = 64

# Frontmatter is exactly these keys. An unknown key is far more likely to be a
# typo ("descriptions:") that silently drops the real field than a deliberate
# extension, and a dropped description means the skill never triggers.
ALLOWED_KEYS = {"name", "description"}

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_scalar(raw: str) -> tuple[str, str | None]:
    """Read one YAML scalar the way the upload parser will. Returns (value, error).

    This is the whole point of the check, so it has to agree with a real YAML
    parser rather than with an eyeball. An unquoted ` #` opens a comment, which
    silently truncates the value — and a description is a trigger surface, so the
    dropped half is exactly the phrases that stop matching. Rather than quietly
    apply the truncation, say how much would be lost and to quote the value.
    """
    raw = raw.strip()
    if raw[:1] in {"'", '"'}:
        quote = raw[0]
        if len(raw) < 2 or not raw.endswith(quote):
            return "", f"unterminated {quote}-quoted value"
        inner = raw[1:-1]
        # The only escape either style needs here: a doubled quote is a literal one.
        return inner.replace(quote * 2, quote), None
    if raw.startswith("#"):
        return "", "value is a YAML comment, so the key parses as null — quote it if the text is meant literally"
    head, sep, tail = raw.partition(" #")
    if sep:
        return head.rstrip(), (
            f"unquoted ` #` starts a YAML comment: {len(raw) - len(head.rstrip())} character(s) would be "
            f"dropped on upload, beginning {(sep + tail)[:40]!r} — wrap the value in single quotes"
        )
    return raw, None


def parse_frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse the flat `key: value` frontmatter block.

    Deliberately not PyYAML: the detectors are stdlib-only and so is this, so the
    check runs anywhere the skills do. The format skills actually use is flat
    scalars, and anything richer is rejected rather than half-understood.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, ["no YAML frontmatter: the file must open with a --- delimited block"]

    fields: dict[str, str] = {}
    errors: list[str] = []
    for lineno, line in enumerate(match.group(1).splitlines(), 2):
        if not line.strip():
            continue
        if line[0].isspace():
            errors.append(f"line {lineno}: indented line — frontmatter must be flat `key: value` scalars")
            continue
        if line.lstrip().startswith("#"):
            continue  # a whole-line comment, which YAML ignores
        key, sep, value = line.partition(":")
        if not sep:
            errors.append(f"line {lineno}: not a `key: value` pair: {line.strip()!r}")
            continue
        key = key.strip()
        if key in fields:
            errors.append(f"line {lineno}: duplicate key {key!r}")
        parsed, error = parse_scalar(value)
        if error:
            errors.append(f"line {lineno}: {key}: {error}")
        fields[key] = parsed
    return fields, errors


def check_skill(skill_dir: Path, evals_root: Path) -> list[str]:
    """Every structural problem with one skill, as human-readable strings."""
    errors: list[str] = []
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.is_file():
        return [f"{name}: no SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    fields, fm_errors = parse_frontmatter(text)
    errors += [f"{name}/SKILL.md: {e}" for e in fm_errors]

    unknown = sorted(set(fields) - ALLOWED_KEYS)
    if unknown:
        errors.append(f"{name}/SKILL.md: unsupported frontmatter key(s) {unknown}; allowed: {sorted(ALLOWED_KEYS)}")

    declared = fields.get("name", "")
    if not declared:
        errors.append(f"{name}/SKILL.md: frontmatter has no name")
    else:
        if declared != name:
            errors.append(
                f"{name}/SKILL.md: frontmatter name is {declared!r} but the directory is {name!r}; "
                "the release tag names the directory, so these must match"
            )
        if not NAME_RE.match(declared):
            errors.append(f"{name}/SKILL.md: name {declared!r} is not hyphen-case (lowercase, digits, single hyphens)")
        if len(declared) > MAX_NAME:
            errors.append(f"{name}/SKILL.md: name is {len(declared)} chars; the limit is {MAX_NAME}")

    description = fields.get("description", "")
    if not description:
        errors.append(f"{name}/SKILL.md: frontmatter has no description — the skill would never trigger")
    elif len(description) > MAX_DESCRIPTION:
        errors.append(f"{name}/SKILL.md: description is {len(description)} chars; the API limit is {MAX_DESCRIPTION}")

    body = text[FRONTMATTER_RE.match(text).end():] if FRONTMATTER_RE.match(text) else text
    if not body.strip():
        errors.append(f"{name}/SKILL.md: frontmatter only, no body")

    # The convention from the $SKILL preamble: an installed skill is not the cwd,
    # so a bare `python scripts/x.py` is a command that cannot run as written.
    for lineno, line in enumerate(text.splitlines(), 1):
        if re.search(r"(?<![\w/\"$])python3? scripts/", line):
            errors.append(
                f"{name}/SKILL.md:{lineno}: bare `python scripts/...` — commands run from the user's "
                f'project, not the skill dir; use `python "$SKILL/scripts/..."`'
            )

    errors += check_evals(name, evals_root / name)
    return errors


def check_evals(name: str, eval_dir: Path) -> list[str]:
    """A skill's judgment half is only regression-checkable if its evals exist."""
    evals_json = eval_dir / "evals.json"
    if not evals_json.is_file():
        return [f"{name}: no evals/{name}/evals.json — the judgment half has no regression prompts"]

    try:
        data = json.loads(evals_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"evals/{name}/evals.json: invalid JSON ({exc})"]

    # Valid JSON of the wrong shape ([] or null) would otherwise raise
    # AttributeError below and abort the whole run, hiding every other skill's
    # problems behind one traceback.
    if not isinstance(data, dict):
        return [f"evals/{name}/evals.json: top level is {type(data).__name__}, expected an object"]

    errors = []
    if data.get("skill_name") != name:
        errors.append(f"evals/{name}/evals.json: skill_name is {data.get('skill_name')!r}, expected {name!r}")

    cases = data.get("evals")
    if not isinstance(cases, list) or not cases:
        return errors + [f"evals/{name}/evals.json: 'evals' must be a non-empty list"]

    seen_ids = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"evals/{name}/evals.json: case #{index} is {type(case).__name__}, expected an object")
            continue
        label = case.get("id") or f"#{index}"
        if not case.get("id"):
            errors.append(f"evals/{name}/evals.json: case {label} has no id")
        elif case["id"] in seen_ids:
            errors.append(f"evals/{name}/evals.json: duplicate case id {case['id']!r}")
        else:
            seen_ids.add(case["id"])
        for field in ("prompt", "expected_output"):
            if not case.get(field):
                errors.append(f"evals/{name}/evals.json: case {label} has no {field}")
        for rel in case.get("files") or []:
            if not (eval_dir / rel).exists():
                errors.append(f"evals/{name}/evals.json: case {label} declares missing fixture {rel!r}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills-dir", type=Path, default=REPO_ROOT / "skills")
    ap.add_argument("--evals-dir", type=Path, default=REPO_ROOT / "evals")
    ap.add_argument("--skill", help="validate only this skill (the release workflow's path)")
    args = ap.parse_args()

    if not args.skills_dir.is_dir():
        print(f"error: {args.skills_dir} is not a directory", file=sys.stderr)
        return 2

    if args.skill:
        candidates = [args.skills_dir / args.skill]
        if not candidates[0].is_dir():
            print(f"error: no such skill: {args.skill}", file=sys.stderr)
            return 2
    else:
        candidates = sorted(p for p in args.skills_dir.iterdir() if p.is_dir())

    errors = []
    for skill_dir in candidates:
        errors += check_skill(skill_dir, args.evals_dir)

    for error in errors:
        print(f"error: {error}", file=sys.stderr)

    checked = ", ".join(p.name for p in candidates)
    if errors:
        print(f"\n{len(errors)} problem(s) across {len(candidates)} skill(s): {checked}", file=sys.stderr)
        return 1
    print(f"{len(candidates)} skill(s) valid: {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
