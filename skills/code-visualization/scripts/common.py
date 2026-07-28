"""Shared helpers for codebase/PR analyzers. Stdlib only."""
import html
import json
import os
import re
import subprocess
from pathlib import Path

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "out",
    ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".idea", ".vscode", "coverage", ".next", ".nuxt", "target",
    "bower_components", ".gradle", ".terraform", "site-packages", ".ruff_cache",
}

LANG_BY_EXT = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift",
    ".c": "C", ".h": "C/C++ header", ".cc": "C++", ".cpp": "C++", ".hpp": "C/C++ header",
    ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".clj": "Clojure",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".less": "CSS",
    ".vue": "Vue", ".svelte": "Svelte", ".lua": "Lua", ".r": "R", ".jl": "Julia",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".json": "JSON",
    ".md": "Markdown", ".rst": "reStructuredText", ".proto": "Protobuf",
    ".tf": "Terraform", ".dockerfile": "Docker",
}

CODE_LANGS = {
    "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin", "Scala",
    "Ruby", "PHP", "C#", "Swift", "C", "C++", "C/C++ header", "Elixir", "Erlang",
    "Clojure", "Shell", "Vue", "Svelte", "Lua", "R", "Julia",
}

# Branch-introducing tokens per language family; used for a cheap complexity proxy.
BRANCH_RE = re.compile(
    r"\b(if|elif|else if|for|while|case|when|catch|except|rescue|match|switch)\b"
    r"|&&|\|\||\?\s"
)

# Artifacts this toolchain generates into the repo (committed with the PR by
# design). Diff analysis must exclude them — "the report covers everything
# except itself" — and staleness checks must not count them as drift.
GENERATED_DOC_RE = re.compile(r"^docs/(codemap\.html|pr-[^/]+\.html)$")


def is_generated_doc(path: str) -> bool:
    return bool(GENERATED_DOC_RE.match(path))


def detect_lang(path: str) -> str:
    p = Path(path)
    if p.name.lower() == "dockerfile":
        return "Docker"
    if p.name.lower() == "makefile":
        return "Make"
    return LANG_BY_EXT.get(p.suffix.lower(), "Other")


def is_test_path(path: str) -> bool:
    p = path.lower()
    return bool(
        re.search(r"(^|/)(tests?|specs?|__tests__|testing)(/|$)", p)
        or re.search(r"(_test|\.test|\.spec|_spec)\.[a-z]+$", p)
        or re.search(r"(^|/)test_[^/]+\.py$", p)
        or re.search(r"(^|/)conftest\.py$", p)
    )


def walk_source(repo: Path, max_file_bytes: int = 2_000_000, skipped_large: list | None = None,
                extra_exclude: set | None = None):
    """Yield (relpath_str, Path) for tracked-looking source files.

    Excluded directories are pruned during the walk (a node_modules tree is
    never entered, not enumerated-then-discarded). Files over max_file_bytes
    are skipped; pass a list as skipped_large to learn which, so callers can
    report the omission instead of silently analyzing around a 3 MB god-file.
    extra_exclude adds user-named directory names (e.g. generated code) to the
    standard exclusion set.
    """
    exclude = EXCLUDE_DIRS | (extra_exclude or set())
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in exclude and not (d.startswith(".") and d != ".github")
        )
        at_root = Path(dirpath) == repo
        for name in sorted(filenames):
            if at_root and name.startswith("."):
                continue
            p = Path(dirpath) / name
            rel = str(p.relative_to(repo)).replace("\\", "/")
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                if skipped_large is not None:
                    skipped_large.append({"path": rel, "bytes": size})
                continue
            yield rel, p


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def loc_and_complexity(text: str):
    """Return (loc, branch_count, max_indent_depth) — cheap, language-agnostic."""
    loc = branches = max_depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        loc += 1
        if stripped.startswith(("#", "//", "*", "/*", "--", "\"\"\"", "'''")):
            continue
        branches += len(BRANCH_RE.findall(line))
        indent = len(line) - len(line.lstrip(" \t"))
        depth = indent // 4 if " " in line[:indent] or not indent else indent
        max_depth = max(max_depth, min(depth, 12))
    return loc, branches, max_depth


def git(repo: Path, *args, check: bool = True, timeout: int = 300) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:400]}")
    return r.stdout


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def json_block(data) -> str:
    # </script> inside JSON strings would break the block
    return json.dumps(data, separators=(",", ":")).replace("</", "<\\/")


def write_fragment(out_dir: Path, filename: str, tab_title: str, body: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / filename
    p.write_text(f"<!-- tab: {tab_title} -->\n{body}\n", encoding="utf-8")
    return p


def bar_cell(value: float, max_value: float, cls: str = "") -> str:
    pct = 0 if max_value <= 0 else max(1.5, 100.0 * value / max_value)
    return f'<div class="bar {cls}"><i style="width:{pct:.1f}%"></i></div>'
