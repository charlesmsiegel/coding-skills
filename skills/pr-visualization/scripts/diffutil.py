"""Parse `git diff` output into structured per-file records. Stdlib only."""
import re
from pathlib import Path

from common import git

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Artifacts this toolchain generates into the repo. They are committed with the
# PR by design, so the diff analysis must exclude them ("cover everything
# except itself") or every report would flag its own predecessor.
GENERATED_DOC_RE = re.compile(r"^docs/(codemap\.html|pr-[^/]+\.html)$")


def is_generated_doc(path: str) -> bool:
    return bool(GENERATED_DOC_RE.match(path))

# definition-line patterns per extension family: (regex with named group 'name')
DEF_PATTERNS = {
    "py": [re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\("),
           re.compile(r"^\s*class\s+(?P<name>\w+)\s*[(:]")],
    "js": [re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*\("),
           re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"),
           re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>\w+)"),
           re.compile(r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*(?P<name>\w+)\s*\([^;]*\)\s*[:{]")],
    "go": [re.compile(r"^func\s+(?:\([^)]+\)\s+)?(?P<name>\w+)\s*\("),
           re.compile(r"^type\s+(?P<name>\w+)\s+(?:struct|interface)\b")],
    "rs": [re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>\w+)"),
           re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>\w+)")],
    "rb": [re.compile(r"^\s*def\s+(?:self\.)?(?P<name>[\w?!]+)"),
           re.compile(r"^\s*class\s+(?P<name>\w+)")],
    "java": [re.compile(r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],\s]+\s+(?P<name>\w+)\s*\([^;]*\)\s*(?:throws [\w,\s]+)?\{?\s*$"),
             re.compile(r"^\s*(?:public\s+)?(?:abstract\s+|final\s+)?class\s+(?P<name>\w+)")],
}
EXT_FAMILY = {
    ".py": "py", ".pyi": "py",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".go": "go", ".rs": "rs", ".rb": "rb",
    ".java": "java", ".kt": "java", ".cs": "java", ".scala": "java",
}


def def_patterns_for(path: str):
    fam = EXT_FAMILY.get(Path(path).suffix.lower())
    return DEF_PATTERNS.get(fam, [])


class FileDiff:
    def __init__(self, path):
        self.path = path            # new path
        self.old_path = path
        self.status = "M"           # M/A/D/R
        self.binary = False
        self.adds = 0
        self.dels = 0
        self.hunks = []             # list of (new_start, [(tag,text,new_lineno_or_None)])

    @property
    def changed(self):
        return self.adds + self.dels

    def added_lines(self):
        for _, lines in self.hunks:
            for tag, text, ln in lines:
                if tag == "+":
                    yield ln, text

    def removed_lines(self):
        for _, lines in self.hunks:
            for tag, text, _ in lines:
                if tag == "-":
                    yield text

    def changed_new_line_ranges(self):
        """Ranges of new-file line numbers touched by hunks (for symbol overlap)."""
        for start, lines in self.hunks:
            new_lines = [ln for tag, _, ln in lines if tag in "+ " and ln is not None]
            plus = [ln for tag, _, ln in lines if tag == "+"]
            if plus:
                yield min(plus), max(plus)
            elif new_lines:  # pure deletion hunk — mark surrounding point
                yield min(new_lines), max(new_lines)


def parse_diff(text: str):
    files, cur = [], None
    new_ln = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = FileDiff("")
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("--- "):
            p = line[4:].strip()
            cur.old_path = "" if p == "/dev/null" else re.sub(r"^a/", "", p)
        elif line.startswith("+++ "):
            p = line[4:].strip()
            cur.path = cur.old_path if p == "/dev/null" else re.sub(r"^b/", "", p)
            if p == "/dev/null":
                cur.status = "D"
        elif line.startswith("new file"):
            cur.status = "A"
        elif line.startswith("deleted file"):
            cur.status = "D"
        elif line.startswith("rename to "):
            cur.status = "R"
            cur.path = line[10:].strip()
        elif line.startswith("rename from "):
            cur.old_path = line[12:].strip()
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                new_ln = int(m.group(3))
                cur.hunks.append((new_ln, []))
        elif cur.hunks and new_ln is not None:
            if line.startswith("+"):
                cur.hunks[-1][1].append(("+", line[1:], new_ln))
                cur.adds += 1
                new_ln += 1
            elif line.startswith("-"):
                cur.hunks[-1][1].append(("-", line[1:], None))
                cur.dels += 1
            elif line.startswith(" "):
                cur.hunks[-1][1].append((" ", line[1:], new_ln))
                new_ln += 1
            elif line.startswith("\\"):  # "\ No newline at end of file"
                pass
    return [f for f in files if f.path]


def resolve_base(repo: Path, base_arg=None, head="HEAD"):
    """Return (base_ref, merge_base_sha, note)."""
    if base_arg:
        mb = git(repo, "merge-base", base_arg, head).strip()
        return base_arg, mb, ""
    for cand in ("origin/main", "origin/master", "main", "master", "origin/develop", "develop"):
        try:
            git(repo, "rev-parse", "--verify", "--quiet", cand)
        except RuntimeError:
            continue
        try:
            mb = git(repo, "merge-base", cand, head).strip()
        except RuntimeError:
            continue
        head_sha = git(repo, "rev-parse", head).strip()
        if mb == head_sha:
            continue  # HEAD is the mainline (or behind it) — empty diff, keep looking
        return cand, mb, f"auto-detected base {cand}"
    raise RuntimeError(
        "could not auto-detect a base ref producing a non-empty diff; "
        "pass --base <ref> (e.g. --base origin/main or --base HEAD~5)"
    )
