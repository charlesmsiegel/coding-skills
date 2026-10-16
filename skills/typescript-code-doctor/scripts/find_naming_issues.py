#!/usr/bin/env python3
"""
Check naming against the conventions TypeScript codebases actually converge on,
and flag the names that shadow browser globals — which is a bug, not a style
question, because `name`, `location` and `status` already exist on `window`.
"""

import re

from common import Reporter, run_file_detector
from tsparse import TsFile

PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
CAMEL_CASE = re.compile(r"^[a-z][A-Za-z0-9]*$")
CONSTANT_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
INTERFACE_PREFIX = re.compile(r"^I[A-Z][A-Za-z0-9]*$")

# Globals on `window` that a binding of the same name shadows. `location`,
# `history`, `status` and `event` are left out on purpose: shadowing them is
# routine and deliberate (`const location = useLocation()`), so reporting them
# would bury the ones below, which nobody shadows knowingly.
BROWSER_GLOBALS = frozenset({
    "name", "length", "top", "self", "parent", "origin",
    "closed", "external", "frames", "opener",
})

BOOLEAN_PREFIXES = ("is", "has", "should", "can", "will", "did", "was", "are", "no", "allow", "enable", "disable")
# The platform's own boolean attributes (HTML and ARIA) are named as states, not
# questions, and a prop mirroring one reads correctly to anyone who has written
# `<input disabled>`.
DOM_STATE_WORDS = frozenset({
    "open", "disabled", "hidden", "checked", "selected", "required", "readonly",
    "multiple", "busy", "pressed", "expanded", "loading",
})
# A participle or adjective ending already says which way `true` points:
# `truncated`, `pending`, `available`, `visible`, `active`.
STATE_SUFFIXES = ("ed", "ing", "able", "ible", "ive")
_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")

# The platform's boolean names — HTML boolean attributes and ARIA states, in
# React's spelling, lower-cased for comparison. A component prop that mirrors
# one (`open`, `checked`, `expanded`) follows a convention every reader already
# knows; renaming it `isOpen` would be the surprise. React's uncontrolled
# `default…` twins (`defaultOpen`, `defaultChecked`) are matched by stripping
# the prefix, and `asChild` is Radix's composition prop, which shadcn/ui
# components re-export under that exact name.
DOM_BOOLEAN_ATTRIBUTES = frozenset(name.lower() for name in (
    "allowFullScreen", "async", "autoFocus", "autoPlay", "checked", "controls", "default", "defer",
    "disabled", "disablePictureInPicture", "disableRemotePlayback", "formNoValidate", "hidden",
    "indeterminate", "inert", "itemScope", "loop", "multiple", "muted", "noModule", "noValidate",
    "open", "playsInline", "readOnly", "required", "reversed", "selected",
    "draggable", "spellCheck", "contentEditable", "suppressHydrationWarning",
    "suppressContentEditableWarning", "asChild",
    # ARIA states, as a prop mirrors `aria-expanded` and its kin
    "atomic", "busy", "expanded", "grabbed", "invalid", "modal", "multiline",
    "multiselectable", "pressed",
))

# Names that state a fact about their subject without an `is`: the final word
# is a state adjective (`galleryOpen`, `loading`), an `-able`/`-ible` adjective
# (`editable`), or a past participle (`shadowed`); or the first word is a
# third-person verb (`pathExists`, `needsSearch`) or a preposition that places
# the subject (`atDefault`, `inProgress`). Each reads as "<subject> is …" or
# "<subject> …s …" — as plainly as a prefix would say which way `true` points.
STATE_ADJECTIVES = frozenset({
    "open", "closed", "visible", "hidden", "shown", "active", "inactive", "enabled", "disabled",
    "loading", "saving", "pending", "running", "available", "dirty", "pristine", "valid",
    "invalid", "ready", "busy", "idle", "empty", "full", "done", "complete", "raw", "live",
    "online", "offline", "stale", "exists",
})
FACT_VERBS = frozenset({
    "needs", "uses", "exists", "contains", "includes", "supports", "requires", "matches",
    "wants", "accepts", "expects", "owns",
})
STATE_PREPOSITIONS = frozenset({"at", "in", "on", "within", "over", "under", "above", "below", "inside", "outside"})
# Words that end in the letters of an adjective or a participle without being one.
NOT_ADJECTIVES = frozenset({"enable", "disable", "variable", "timetable", "vegetable"})
NOT_PARTICIPLES = frozenset({
    "embed", "hundred", "shed", "sled", "seed", "need", "feed", "speed", "breed", "bleed",
    "weed", "reed", "proceed", "succeed", "exceed", "indeed",
})
_CAMEL_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


def _check_declaration_case(file: TsFile, report: Reporter) -> None:
    for decl in file.types:
        if not PASCAL_CASE.match(decl.name):
            report.add(decl.line, "non_pascal_case_type",
                       f"{decl.kind} `{decl.name}` is not PascalCase",
                       "Types, interfaces, enums and classes are PascalCase; values are camelCase. "
                       "The case is how a reader tells a type from a value at a glance.", "low")
        if decl.kind == "interface" and INTERFACE_PREFIX.match(decl.name):
            report.add(decl.line, "hungarian_interface_prefix",
                       f"Interface `{decl.name}` uses the `I` prefix",
                       "Drop it. TypeScript's own style guide and the DOM typings do not use it, and "
                       "the prefix stops meaning anything the moment the interface becomes a type "
                       "alias.", "low")
    for klass in file.classes:
        if klass.name != "<anonymous>" and not PASCAL_CASE.match(klass.name):
            report.add(klass.line, "non_pascal_case_type",
                       f"class `{klass.name}` is not PascalCase",
                       "Classes are PascalCase.", "low")


def _check_function_case(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        name = func.name
        if name in ("<anonymous>", "constructor") or func.kind in ("getter", "setter"):
            continue
        if name.startswith("#") or name.startswith("["):
            continue
        if CAMEL_CASE.match(name) or PASCAL_CASE.match(name) or CONSTANT_CASE.match(name):
            continue
        report.add(func.line, "non_camel_case_function",
                   f"`{name}` is neither camelCase nor PascalCase",
                   "camelCase for functions and methods; PascalCase only for classes, components "
                   "and type-like factories.", "low")


def _check_shadowed_globals(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("const", "let", "var"):
            continue
        name_token = file.tokens[index + 1] if index + 1 < len(file) else None
        if name_token is None or name_token.kind != "name" or name_token.value not in BROWSER_GLOBALS:
            continue
        # Only module scope: a binding inside a function shadows the global for
        # a few lines, one at the top of the file shadows it for every reader.
        if not file.top_level(index):
            continue
        report.add(name_token.line, "shadows_browser_global",
                   f"`{name_token.value}` shadows the browser global of the same name",
                   f"Rename it. Inside this scope `{name_token.value}` no longer refers to "
                   f"`window.{name_token.value}`, and the two are easy to confuse when reading a "
                   "diff out of context.", "medium")


def _mirrors_a_wire_name(name: str) -> bool:
    """A member with `_` in its name spells someone else's key.

    TypeScript code names its own members in camelCase; a snake_case member of
    an interface or type literal (`cuda_available`, CouchDB's `_deleted`)
    mirrors a JSON payload, a database row or a config file the code does not
    own. Renaming it breaks the contract with the other side, so the finding
    could only be acted on by adding a mapping layer for a naming preference.
    """
    return "_" in name


def _is_dom_boolean_attribute(name: str) -> bool:
    lower = name.lower()
    return lower in DOM_BOOLEAN_ATTRIBUTES or (
        lower.startswith("default") and lower.removeprefix("default") in DOM_BOOLEAN_ATTRIBUTES)


def _reads_as_a_statement(name: str) -> bool:
    words = [word.lower() for word in _CAMEL_WORD.findall(name)]
    if not words:
        return False
    first, last = words[0], words[-1]
    if first in FACT_VERBS or (first in STATE_PREPOSITIONS and len(words) > 1):
        return True
    if last in STATE_ADJECTIVES:
        return True
    if len(last) >= 6 and last.endswith(("able", "ible")) and last not in NOT_ADJECTIVES:
        return True  # `table` and `cable` are too short to be an `-able` adjective
    return len(last) > 3 and last.endswith("ed") and last not in NOT_PARTICIPLES


def _reads_as_state(name: str) -> bool:
    """True for a name whose last word is a platform state or a participle/adjective."""
    words = _WORDS.findall(name)
    last = words[-1].lower() if words else name.lower()
    return last not in NOT_ADJECTIVES | NOT_PARTICIPLES and (
        name.lower() in DOM_STATE_WORDS or last in DOM_STATE_WORDS or last.endswith(STATE_SUFFIXES))


def _check_boolean_names(file: TsFile, report: Reporter) -> None:
    """Boolean members named neither as a question nor as a state.

    The defect is a boolean named like a bare noun (`prefill`, `grid`), where
    nothing says which way `true` points. Telling a noun from an adjective
    needs a dictionary this scanner does not carry — `ok`, `stale` and `due`
    are as clear as `isStale` — so what survives the prefix, platform and
    suffix tests is a lead, not a finding.
    """
    for decl in file.types:
        for member in decl.members:
            if member.type_text.strip() != "boolean" or member.name == "[index]":
                continue
            if member.name.lower().startswith(BOOLEAN_PREFIXES) or _reads_as_state(member.name):
                continue
            if (_mirrors_a_wire_name(member.name) or _is_dom_boolean_attribute(member.name)
                    or _reads_as_a_statement(member.name)):
                continue
            report.candidate(member.line, "boolean_without_predicate_name",
                             f"`{decl.name}.{member.name}` is a boolean whose name is neither a "
                             "question nor an obvious state",
                             ("the name is an adjective that already reads as a state (`ok`, "
                              "`stale`, `due`, `ready`)",
                              "the field mirrors a wire format or external API the TypeScript "
                              "side cannot rename",
                              "it follows a library's or the platform's own name for the flag"),
                             "low")


def _check_private_naming(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        for prop in klass.props:
            if prop.name.startswith("_") and prop.accessibility is None:
                report.add(prop.line, "underscore_without_private",
                           f"`{klass.name}.{prop.name}` is named as private but declared public",
                           "Use the `private` modifier (or a `#name` field). A naming convention "
                           "cannot be enforced; the modifier can.", "low")


def _check_file_name(file: TsFile, report: Reporter) -> None:
    """A default export whose name disagrees with its file is a grep trap."""
    default = next((e for e in file.exports if e.kind == "default"), None)
    if default is None:
        return
    stem = file.path.name.split(".")[0]
    if stem in ("index",):
        return
    named = [d for d in file.classes if d.is_default_export]
    subject = named[0].name if named else None
    if subject is None:
        for func in file.functions:
            if func.is_exported and func.name not in ("<anonymous>",) and func.line == default.line:
                subject = func.name
                break
    if subject and subject.lower() != stem.lower().replace("-", "").replace("_", ""):
        report.add(default.line, "default_export_name_mismatch",
                   f"`{file.path.name}` default-exports `{subject}`",
                   "Match the file name to the export (or use a named export). A default export can "
                   "be imported under any name, so a mismatch means the same thing appears under "
                   "several names across the codebase.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_declaration_case(file, report)
    _check_function_case(file, report)
    _check_shadowed_globals(file, report)
    _check_boolean_names(file, report)
    _check_private_naming(file, report)
    _check_file_name(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Check naming conventions and globals shadowed by locals",
        "No naming issues found!",
        analyze,
    )
