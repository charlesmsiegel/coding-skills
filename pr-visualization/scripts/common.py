"""Shared helpers for codebase/PR analyzers. Stdlib only."""
import html
import json
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


def walk_source(repo: Path, max_file_bytes: int = 2_000_000):
    """Yield (relpath_str, Path) for tracked-looking source files."""
    for p in sorted(repo.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(repo)
        if any(part in EXCLUDE_DIRS or part.startswith(".") and part not in {".github"} for part in rel.parts[:-1]):
            continue
        if rel.parts and rel.parts[0].startswith(".") and rel.parts[0] != ".github":
            continue
        try:
            if p.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        yield str(rel).replace("\\", "/"), p


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


def git(repo: Path, *args, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, errors="replace",
    )
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
