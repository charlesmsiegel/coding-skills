#!/usr/bin/env python3
"""
Audit tsconfig.json for the settings that decide how much checking happens.

This is the highest-leverage file in a TypeScript repo and the one nobody
re-reads. `strict: false` does not make the code slightly less checked — it
turns off `strictNullChecks`, so every type silently includes `null` and
`undefined`, and the compiler stops mentioning the single most common runtime
error in the language.

Also the only detector here that reads JSON rather than TypeScript, so it
carries its own JSONC reader: tsconfig files are full of comments and trailing
commas that `json.loads` rejects.
"""

import contextlib
import json
import re
from pathlib import Path

from common import EXCLUDE_DIRS, Finding, run_tree_detector

# option -> (severity, what it costs to leave it off)
STRICT_FAMILY = {
    "strictNullChecks": ("high",
        "every type silently includes null and undefined, so the compiler cannot warn about the "
        "most common runtime error in the language"),
    "noImplicitAny": ("high",
        "any parameter or variable the compiler cannot infer becomes `any` with no diagnostic"),
    "strictFunctionTypes": ("medium", "callback parameters are checked bivariantly, so an unsound assignment passes"),
    "strictBindCallApply": ("low", "`bind`, `call` and `apply` are unchecked"),
    "strictPropertyInitialization": ("medium", "a class field can be declared non-optional and never assigned"),
    "noImplicitThis": ("medium", "`this` is `any` inside unbound functions"),
    "useUnknownInCatchVariables": ("medium", "caught values are typed `any` instead of `unknown`"),
    "alwaysStrict": ("low", "emitted files are not in strict mode"),
}

# Not implied by `strict`, but each closes a real class of bug.
RECOMMENDED_EXTRAS = {
    "noUncheckedIndexedAccess": ("medium",
        "`arr[0]` is typed `T` even when the array is empty",
        "Set it to true: indexed access then yields `T | undefined` and the check moves to compile time."),
    "noImplicitOverride": ("low",
        "a method can silently stop overriding its base when the base is renamed",
        "Set it to true and mark intentional overrides with `override`."),
    "exactOptionalPropertyTypes": ("low",
        "`{ a?: string }` also accepts `{ a: undefined }`, which is a different thing",
        "Set it to true when the codebase distinguishes 'absent' from 'present but undefined'."),
    "verbatimModuleSyntax": ("low",
        "type-only imports are elided by inference, which surprises bundlers and decorators",
        "Set it to true and write `import type` explicitly."),
    "isolatedModules": ("low",
        "constructs that a single-file transpiler (esbuild, swc, Babel) cannot handle compile here "
        "and break there",
        "Set it to true so the compiler rejects them at the source."),
    "noUnusedLocals": ("low",
        "dead local variables accumulate with no signal",
        "Set it to true, or run the equivalent lint rule."),
}

# Options whose *presence* weakens checking.
DANGEROUS_WHEN_TRUE = {
    "suppressImplicitAnyIndexErrors": ("high",
        "silences exactly the errors noImplicitAny exists to raise"),
    "suppressExcessPropertyErrors": ("high",
        "lets object literals carry properties the target type does not declare, which is how typos ship"),
    "allowUnreachableCode": ("medium", "unreachable code stops being an error"),
    "allowUnusedLabels": ("low", "a mistyped label stops being an error"),
    "noStrictGenericChecks": ("medium", "generic signatures are compared unsoundly"),
}

_LINE_COMMENT = re.compile(r"(^|[^:])//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def load_jsonc(path: Path) -> dict | None:
    """Parse a tsconfig: JSON plus comments and trailing commas.

    Strings are protected first, so a `//` inside a path value survives.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    protected: list[str] = []

    def stash(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(r'"(?:[^"\\]|\\.)*"', stash, text)
    text = _BLOCK_COMMENT.sub("", text)
    text = _LINE_COMMENT.sub(r"\1", text)
    text = _TRAILING_COMMA.sub(r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _line_of(path: Path, option: str) -> int:
    # An unreadable file simply has no better line than 1 to point at; the
    # caller has already reported the real problem.
    with contextlib.suppress(OSError):
        for number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            if f'"{option}"' in line:
                return number
    return 1


def _configs(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.name.startswith("tsconfig") else []
    found = [p for p in root.rglob("tsconfig*.json")
             if EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts)]
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found[:10]


def _has_typescript(root: Path) -> bool:
    if root.is_file():
        return root.suffix in (".ts", ".tsx")
    for candidate in root.rglob("*.ts"):
        if EXCLUDE_DIRS.isdisjoint(candidate.relative_to(root).parts):
            return True
    return False


def _finding(path: Path, line: int, smell: str, description: str, suggestion: str, severity: str) -> Finding:
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity)


def _audit(path: Path, options: dict, extends: bool, ignore: set[str]) -> list[Finding]:
    findings: list[Finding] = []

    def add(smell, line, description, suggestion, severity):
        if smell not in ignore:
            findings.append(_finding(path, line, smell, description, suggestion, severity))

    strict = options.get("strict")
    if strict is not True:
        # A config that extends a base may inherit `strict`; say so rather than
        # asserting something this file cannot know.
        inherited = " (it may be inherited from the extended config — check the resolved options "
        inherited += "with `tsc --showConfig`)" if extends else ""
        explicit_strict_flags = [k for k in STRICT_FAMILY if options.get(k) is True]
        if not explicit_strict_flags or strict is False:
            add("strict_mode_off", _line_of(path, "strict") if "strict" in options else 1,
                f"`strict` is {'false' if strict is False else 'not set'} in {path.name}{inherited}",
                "Turn it on. It is nine checks, and `strictNullChecks` alone is the difference "
                "between a compiler that knows about `undefined` and one that does not. Migrate "
                "file by file if the error count is large — see references/strictness-migration.md.",
                "high")
    for option, (severity, cost) in STRICT_FAMILY.items():
        if options.get(option) is False:
            add("strict_flag_disabled", _line_of(path, option),
                f"`{option}: false` is set explicitly — {cost}",
                "Remove the override so `strict` governs, and fix the errors it surfaces.", severity)

    for option, (severity, cost) in DANGEROUS_WHEN_TRUE.items():
        if options.get(option) is True:
            add("checking_suppressed", _line_of(path, option),
                f"`{option}: true` — {cost}",
                f"Remove `{option}` and fix what it was hiding.", severity)

    if strict is True or any(options.get(k) is True for k in STRICT_FAMILY):
        for option, (severity, cost, fix) in RECOMMENDED_EXTRAS.items():
            if option not in options:
                add("missing_strict_extra", 1,
                    f"`{option}` is not set in {path.name} — {cost}",
                    fix, severity)

    # "es5" sorts *after* "es2020" as a string, so compare the edition numerically.
    target = str(options.get("target", "")).lower()
    edition = int("".join(ch for ch in target if ch.isdigit()) or 0)
    if target and target != "esnext" and (edition < 2020 or edition < 100):
        add("outdated_target", _line_of(path, "target"),
            f"`target: {options['target']}` — below ES2020, so optional chaining and nullish "
            "coalescing are downlevelled into larger, slower output",
            "Raise it to `es2022` unless a named runtime forces otherwise.", "low")

    if options.get("allowJs") and not options.get("checkJs"):
        add("unchecked_js", _line_of(path, "allowJs"),
            "`allowJs` without `checkJs` — the .js files compile but are never checked",
            "Set `checkJs: true` (and expect errors), or migrate the remaining .js files.", "low")

    if options.get("skipLibCheck") is False and target:
        pass  # a deliberate, defensible choice — not a finding
    return findings


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    configs = _configs(root)
    if not configs:
        if not _has_typescript(root):
            return []
        return [_finding(root / "tsconfig.json", 1, "no_tsconfig",
                         "TypeScript sources but no tsconfig.json under this path",
                         "Add one. Without it every tool guesses a different set of compiler "
                         "options, and none of them is strict.", "high")]
    findings: list[Finding] = []
    for config in configs:
        data = load_jsonc(config)
        if data is None:
            findings.append(_finding(config, 1, "unparseable_tsconfig",
                                     f"{config.name} could not be parsed, so its options were not audited",
                                     "Check it with `tsc -p <file> --showConfig`.", "medium"))
            continue
        options = data.get("compilerOptions") or {}
        if not isinstance(options, dict):
            options = {}
        findings.extend(_audit(config, options, bool(data.get("extends")), ignore))
    return findings


if __name__ == "__main__":
    run_tree_detector(
        "Audit tsconfig.json strictness — the settings that decide how much checking happens",
        "tsconfig strictness looks good!",
        analyze,
    )
