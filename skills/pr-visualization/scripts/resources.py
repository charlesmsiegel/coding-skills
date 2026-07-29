"""Runtime resource references: the dependencies that are loaded, not imported.

An import graph built from import syntax alone understates coupling in any
codebase that assembles behavior at runtime — a rendered Jinja template, a
prompt read off disk, a SQL file, a JSON schema. Those files are dependencies by
every meaning that matters: change one and behavior changes.

Detection is textual and deliberately anchored on one rule:

    an edge is emitted only when the string resolves to a file that exists.

That is what makes a language-agnostic literal scan safe. A path assembled with
no literal segment at all (``open(cfg.path)``) is invisible here, and callers are
expected to say so rather than imply the graph is complete.

Five kinds of reference:

  literal           a quoted string in source that resolves to a repo file
  pattern           a computed path (f-string, ${}, %s, glob) expanded to matches
  template-include  {% extends %} / {% include %} / {{> partial }} between templates
  embed             //go:embed, include_str!, new URL('./x', import.meta.url)
  (loader roots)    not an edge kind — directories a loader searches, which make
                    render_template("index.html") resolve against templates/

Stdlib only; the caller supplies the file index so this module never walks a tree.
"""
import ast
import fnmatch
import posixpath
import re
from pathlib import Path
from typing import NamedTuple

from common import CODE_LANGS, detect_lang

# A pattern that matched half the repo would be noise, not a finding; a data file
# full of paths must not dominate the graph. Both caps report what they dropped.
MAX_PATTERN_TARGETS = 25
MAX_REFS_PER_FILE = 400
MAX_LITERAL_LEN = 200
BINARY_SNIFF_BYTES = 1024

CAVEAT = ("Resource edges are found textually: a quoted path is an edge only when "
          "it resolves to a file that exists. Paths assembled entirely at runtime "
          "(no literal segment) are invisible, and a resolved string is evidence of "
          "a reference, not proof the file is loaded on every code path.")

TEMPLATE_EXTS = {".j2", ".jinja", ".jinja2", ".html", ".htm", ".tmpl", ".tpl",
                 ".mustache", ".hbs", ".handlebars", ".liquid", ".twig", ".ejs"}
# Config carries real references (a compose file naming a Dockerfile, a pipeline
# naming a script). JSON is left out on purpose: lockfiles and data dumps are
# mostly paths, and they would swamp the graph without saying anything.
CONFIG_LANGS = {"YAML", "TOML"}

# Directories conventionally searched by a loader, so a bare "index.html" or
# "router.md" resolves without the loader construction being recognized.
CONVENTIONAL_ROOTS = {"templates", "template", "views", "prompts", "prompt",
                      "assets", "static", "sql", "queries", "schemas", "fixtures"}

# Constructions whose string arguments name a directory a loader will search.
LOADER_HINT_RE = re.compile(
    r"FileSystemLoader|PackageLoader|ChoiceLoader|PrefixLoader|searchpath"
    r"|template_folder|template_dir|templates_dir|prompt_dir|prompts_dir"
    r"|resource_dir|\bDIRS\b|importlib\.resources\.files|__file__",
    re.I)

STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\\n])*)\1""")
TEMPLATE_TAG_RE = re.compile(
    r"""\{%-?\s*(?:extends|include|import|from)\s+['"]([^'"]+)['"]"""
    r"""|\{\{>\s*['"]?([^\s'"}]+)['"]?\s*\}\}""")
GO_EMBED_RE = re.compile(r"^\s*//\s*go:embed\s+(.+)$", re.M)
INCLUDE_MACRO_RE = re.compile(r"""include_(?:str|bytes)!\s*\(\s*['"]([^'"]+)['"]""")
IMPORT_META_URL_RE = re.compile(r"""new\s+URL\s*\(\s*['"]([^'"]+)['"]\s*,\s*import\.meta\.url""")
# f-string / template-literal / printf holes, collapsed to a glob wildcard
HOLE_RE = re.compile(r"\{[^{}]*\}|\$\{[^{}]*\}|%[sd]|%\([^)]*\)s")
WILDCARD_RE = re.compile(r"[*?]")


class ResourceRef(NamedTuple):
    src: str
    dst: str
    line: int
    kind: str
    token: str


class Scan(NamedTuple):
    refs: list
    roots: list
    truncated: dict


class _Cand(NamedTuple):
    """A resolution candidate, before we know whether its target exists."""
    line: int
    text: str
    kind: str


def is_scannable(rel: str) -> bool:
    lang = detect_lang(rel)
    return (lang in CODE_LANGS or lang in CONFIG_LANGS
            or Path(rel).suffix.lower() in TEMPLATE_EXTS)


def _read(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        return ""
    return raw.decode("utf-8", errors="replace")


def _as_pattern(text: str) -> str | None:
    """Rewrite a computed path into a glob, or None if it is not one."""
    glob = HOLE_RE.sub("*", text)
    if glob == text and not WILDCARD_RE.search(text):
        return None
    return re.sub(r"\*+", "*", glob)


def _python_candidates(text: str) -> list:
    """String constants via ast — comments and docstrings never become edges."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return _generic_candidates(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    out = []

    def visit(node):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("*")
            out.append(_Cand(node.lineno, "".join(parts), "pattern"))
            return  # its Constant parts are fragments, not paths of their own
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append(_Cand(node.lineno, node.value, "literal"))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return out


def _strip_comment(line: str) -> str:
    """Drop a trailing line comment, but only when no quote precedes it."""
    cut = len(line)
    for marker in ("#", "//"):
        i = line.find(marker)
        if i != -1 and i < cut:
            if not any(q in line[:i] for q in "'\"`"):
                cut = i
    return line[:cut]


def _generic_candidates(text: str) -> list:
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in STRING_RE.finditer(_strip_comment(line)):
            out.append(_Cand(lineno, m.group(2), "literal"))
    return out


def _template_candidates(text: str) -> list:
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in TEMPLATE_TAG_RE.finditer(line):
            out.append(_Cand(lineno, m.group(1) or m.group(2), "template-include"))
    return out


def _embed_candidates(text: str) -> list:
    out = []
    for regex in (GO_EMBED_RE, INCLUDE_MACRO_RE, IMPORT_META_URL_RE):
        for m in regex.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            for token in m.group(1).split():
                out.append(_Cand(line, token.strip("'\""), "embed"))
    return out


def _candidates(rel: str, text: str) -> list:
    ext = Path(rel).suffix.lower()
    out = _embed_candidates(text)
    if ext in TEMPLATE_EXTS:
        out += _template_candidates(text)
    if ext in (".py", ".pyi"):
        out += _python_candidates(text)
    else:
        out += _generic_candidates(text)
    return out


def _norm(text: str) -> str:
    """Repo-shaped path: forward slashes, no ./ or leading /, .. collapsed."""
    p = text.replace("\\", "/").strip()
    p = posixpath.normpath(p) if p not in ("", ".", "/") else p
    return p.lstrip("/")


def loader_roots(files, hints_by_file) -> dict:
    """Directories a template/prompt loader searches, keyed by where they apply.

    Key "" holds the conventional roots (templates/, prompts/, ...), which are
    evidence on their own; every other key is a directory whose files declared a
    root explicitly. Roots are *not* applied repo-wide — see _Resolver._bases.
    """
    dirs = set()
    for rel in files:
        d = posixpath.dirname(rel)
        while d:
            dirs.add(d)
            d = posixpath.dirname(d)
    roots = {"": {d for d in dirs if posixpath.basename(d).lower() in CONVENTIONAL_ROOTS}}
    for rel, tokens in hints_by_file.items():
        src_dir = posixpath.dirname(rel)
        for token in tokens:
            cand = _norm(token)
            if not cand:
                continue
            for guess in (cand, _norm(posixpath.join(src_dir, cand)) if src_dir else cand):
                if guess in dirs:
                    roots.setdefault(src_dir, set()).add(guess)
    return {k: sorted(v, key=lambda d: (-len(d), d)) for k, v in roots.items()}


def _hint_tokens(text: str) -> list:
    out = []
    for line in text.splitlines():
        if LOADER_HINT_RE.search(line):
            out += [m.group(2) for m in STRING_RE.finditer(line)]
    return out


class _Resolver:
    def __init__(self, files, roots):
        self.index = set(files)
        self.roots = roots
        self.by_base = {}
        self._base_cache = {}
        for rel in files:
            self.by_base.setdefault(posixpath.basename(rel), []).append(rel)

    def _near(self, root, src_dir):
        """A root counts for this file if it is declared by, contains, or shares
        an arm with it. A root found on the far side of the repo does not: two
        skills each holding a scripts/ directory must not resolve into each
        other's copy just because one of them declared a loader."""
        return (src_dir == "" or root == src_dir or root.startswith(src_dir + "/")
                or root.split("/")[0] == src_dir.split("/")[0])

    def _bases(self, src_rel):
        src_dir = posixpath.dirname(src_rel)
        if src_dir not in self._base_cache:
            near = [r for r in self.roots.get(src_dir, [])]
            near += [r for r in self.roots.get("", []) if self._near(r, src_dir)]
            self._base_cache[src_dir] = ([src_dir] if src_dir else []) + [""] + near
        return self._base_cache[src_dir]

    def _plausible(self, cand):
        return (cand and len(cand) <= MAX_LITERAL_LEN and "://" not in cand
                and not cand.startswith(("#", "@", "<", "-"))
                and ("/" in cand or re.search(r"\.[A-Za-z0-9]{1,8}$", cand)))

    def resolve(self, src_rel, text):
        cand = _norm(text)
        if not self._plausible(cand):
            return None
        for base in self._bases(src_rel):
            target = _norm(posixpath.join(base, cand)) if base else cand
            if target in self.index and target != src_rel:
                return target
        # Unique suffix match: "config.json" with two of them stays unresolved,
        # while "partials/nav.html" under an unguessed root still lands.
        hits = [rel for rel in self.by_base.get(posixpath.basename(cand), [])
                if (rel == cand or rel.endswith("/" + cand)) and rel != src_rel]
        return hits[0] if len(hits) == 1 else None

    def expand(self, src_rel, text):
        """Every file a computed path could name, or [] if it is too loose."""
        glob = _norm(text)
        if not self._plausible(glob) or not WILDCARD_RE.search(glob):
            return []
        if "/" not in glob.split("*")[0].split("?")[0]:
            return []  # unanchored: no literal directory segment to hold it down
        out = set()
        for base in self._bases(src_rel):
            pattern = _norm(posixpath.join(base, glob)) if base else glob
            out |= {rel for rel in self.index
                    if rel != src_rel and fnmatch.fnmatchcase(rel, pattern)}
            if out:
                break
        return sorted(out)


def scan(repo: Path, files: dict) -> Scan:
    """Find every runtime resource reference among the given files.

    ``files`` maps repo-relative POSIX path -> Path, and must include non-code
    files: a template nothing imports is exactly what this looks for. Returns
    deduplicated refs (one per src/dst pair, most specific kind winning), the
    loader roots used, and what the caps truncated.
    """
    cands, hints = {}, {}
    for rel, path in files.items():
        if not is_scannable(rel):
            continue
        text = _read(path)
        if not text:
            continue
        cands[rel] = _candidates(rel, text)
        tokens = _hint_tokens(text)
        if tokens:
            hints[rel] = tokens

    roots = loader_roots(files, hints)
    resolver = _Resolver(files, roots)
    all_roots = sorted({r for group in roots.values() for r in group})
    refs, truncated = [], {"patterns": 0, "files": 0}
    # Most specific kind first, so a template include is not also reported as a
    # plain literal and a Rust include_str! keeps its embed kind.
    order = {"template-include": 0, "embed": 1, "literal": 2, "pattern": 3}

    for rel in sorted(cands):
        seen, kept, dropped = set(), [], False
        for cand in sorted(cands[rel], key=lambda c: (order[c.kind], c.line)):
            targets, kind = [], cand.kind
            if cand.kind == "pattern" or WILDCARD_RE.search(cand.text):
                pattern = _as_pattern(cand.text)
                if pattern is None:
                    continue
                targets = resolver.expand(rel, pattern)
                kind = "pattern"
                if len(targets) > MAX_PATTERN_TARGETS:
                    targets = targets[:MAX_PATTERN_TARGETS]
                    truncated["patterns"] += 1
            else:
                hit = resolver.resolve(rel, cand.text)
                targets = [hit] if hit else []
            for dst in targets:
                if dst in seen:
                    continue
                if len(kept) >= MAX_REFS_PER_FILE:
                    dropped = True
                    break
                seen.add(dst)
                kept.append(ResourceRef(rel, dst, cand.line, kind, cand.text))
            if dropped:
                break
        truncated["files"] += 1 if dropped else 0
        refs += kept

    return Scan(refs=refs, roots=all_roots, truncated=truncated)
