#!/usr/bin/env python3
"""Read a reviewed lint suppression off a source line.

A bare `# noqa: A002` says only that somebody silenced a linter. A
`# noqa: A002 -- the HTTP parameter is named id` says somebody looked at this
line, decided the rule does not apply, and wrote down why. The second is
evidence a detector can use: the observation is still true, but a reviewer has
already weighed it, so it is a lead to confirm rather than a defect to fix. The
first is not — it is as consistent with "made the gate pass" as with a decision.

Stdlib only, like every detector that imports it.
"""

import re
from typing import Iterable, Sequence

# The marker, then a code list (CODE[, CODE ...]), then a separator, then the reason. The separators
# are the ones people actually type after a code list: `--`, an em dash, a
# single hyphen, or a colon. Codes are letter(s) then digits (A002, N802,
# PLR0913), separated by commas and/or whitespace as ruff and flake8 accept.
_REASONED = re.compile(
    r"#\s*noqa\s*:\s*"
    r"(?P<codes>[A-Za-z]+[0-9]+(?:[\s,]+[A-Za-z]+[0-9]+)*)"
    r"\s*(?:--|\u2014|-|:)\s*(?P<reason>\S.*)$"
)


def _reasoned_codes(line: str) -> set[str]:
    """The codes a reasoned noqa on ``line`` names, upper-cased; empty if none."""
    match = _REASONED.search(line)
    # A reason is words. `# noqa: A001 --` with nothing after the dashes would
    # otherwise read its second dash as the reason.
    if match is None or not re.search(r"\w", match.group("reason")):
        return set()
    return {code.upper() for code in re.split(r"[\s,]+", match.group("codes")) if code}


def reasoned_noqa(lines: Sequence[str], lineno: int, codes: Iterable[str]) -> bool:
    """True when line ``lineno`` (1-based) carries a reasoned noqa for one of ``codes``.

    The marker may sit on the flagged line itself or on a comment-only line
    directly below it — where a long signature or a wrapped statement puts the
    explanation when the flagged line has no room for it. A code-bearing line
    below is a different statement, and its suppression is not this one's.
    """
    wanted = {code.upper() for code in codes}
    if 0 < lineno <= len(lines) and _reasoned_codes(lines[lineno - 1]) & wanted:
        return True
    if 0 < lineno < len(lines):
        below = lines[lineno]
        if below.lstrip().startswith("#") and _reasoned_codes(below) & wanted:
            return True
    return False
