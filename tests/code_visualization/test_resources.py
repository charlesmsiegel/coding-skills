"""Tests for resources.py — runtime resource references (templates, prompts, assets).

An import is not the only way one file depends on another: a template is
rendered, a prompt is read off disk, a schema is loaded. These tests pin the one
rule that keeps such edges honest — an edge exists only when the target file
exists — and the guards that stop a language-agnostic string scan from turning
into noise.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "scripts"


@pytest.fixture
def resources(load_module):
    return load_module(SCRIPTS, "resources")


def index(root: Path) -> dict[str, Path]:
    """The file index the caller is expected to hand in: every file, code or not."""
    return {
        str(p.relative_to(root)).replace("\\", "/"): p
        for p in root.rglob("*")
        if p.is_file()
    }


def scan(resources, root: Path):
    return resources.scan(root, index(root))


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# The core rule: resolves to a real file, or it is not an edge
# --------------------------------------------------------------------------- #


def test_a_literal_path_that_resolves_becomes_an_edge(resources, tmp_path):
    write(tmp_path, "app/loader.py", "def load():\n    return open('prompts/system.md').read()\n")
    write(tmp_path, "prompts/system.md", "You are a helpful assistant.\n")

    refs = scan(resources, tmp_path).refs

    assert [(r.src, r.dst, r.line, r.kind) for r in refs] == [
        ("app/loader.py", "prompts/system.md", 2, "literal")
    ]


def test_a_literal_that_resolves_to_nothing_is_not_an_edge(resources, tmp_path):
    write(tmp_path, "app/loader.py", "open('prompts/missing.md')\n")
    write(tmp_path, "prompts/system.md", "hi\n")

    assert scan(resources, tmp_path).refs == []


def test_a_url_is_never_a_path(resources, tmp_path):
    # The basename would otherwise match prompts/system.md.
    write(tmp_path, "app/fetch.py", "URL = 'https://example.com/prompts/system.md'\n")
    write(tmp_path, "prompts/system.md", "hi\n")

    assert scan(resources, tmp_path).refs == []


def test_a_bare_word_is_never_a_path(resources, tmp_path):
    # "system" is not a path candidate even though prompts/system.md exists.
    write(tmp_path, "app/a.py", "MODE = 'system'\n")
    write(tmp_path, "prompts/system.md", "hi\n")

    assert scan(resources, tmp_path).refs == []


def test_a_file_never_depends_on_itself(resources, tmp_path):
    write(tmp_path, "app/self.py", "NAME = 'app/self.py'\n")

    assert scan(resources, tmp_path).refs == []


def test_an_ambiguous_basename_does_not_resolve(resources, tmp_path):
    write(tmp_path, "app/a.py", "CFG = 'config.json'\n")
    write(tmp_path, "one/config.json", "{}\n")
    write(tmp_path, "two/config.json", "{}\n")

    assert scan(resources, tmp_path).refs == []


def test_a_unique_basename_resolves(resources, tmp_path):
    write(tmp_path, "app/a.py", "CFG = 'settings.json'\n")
    write(tmp_path, "conf/settings.json", "{}\n")

    assert [r.dst for r in scan(resources, tmp_path).refs] == ["conf/settings.json"]


def test_a_sibling_path_resolves_relative_to_the_citing_file(resources, tmp_path):
    write(tmp_path, "pkg/run.py", "open('data.sql')\n")
    write(tmp_path, "pkg/data.sql", "SELECT 1;\n")
    write(tmp_path, "other/data.sql", "SELECT 2;\n")  # basename is ambiguous; the sibling still wins

    assert [r.dst for r in scan(resources, tmp_path).refs] == ["pkg/data.sql"]


# --------------------------------------------------------------------------- #
# Loader roots — the jinja2 case
# --------------------------------------------------------------------------- #


def test_render_template_resolves_against_a_declared_loader_root(resources, tmp_path):
    write(tmp_path, "app/web.py",
          "from jinja2 import Environment, FileSystemLoader\n"
          "env = Environment(loader=FileSystemLoader('web/views'))\n"
          "def page():\n"
          "    return env.get_template('index.html').render()\n")
    write(tmp_path, "web/views/index.html", "<p>hi</p>\n")

    result = scan(resources, tmp_path)

    assert "web/views" in result.roots
    assert ("app/web.py", "web/views/index.html", 4) in [(r.src, r.dst, r.line) for r in result.refs]


def test_a_declared_root_does_not_leak_to_unrelated_callers(resources, tmp_path):
    """A loader root is evidence about the files near it, not about the repo.

    Two copies of the template exist, so the basename is ambiguous. Each module
    must resolve against its own neighborhood; neither may inherit the other's
    root and silently cite the wrong copy.
    """
    write(tmp_path, "one/app.py",
          "from jinja2 import FileSystemLoader\n"
          "loader = FileSystemLoader('one/views')\n"
          "page = load('card.html')\n")
    write(tmp_path, "two/app.py", "page = load('card.html')\n")
    write(tmp_path, "one/views/card.html", "<p>one</p>\n")
    write(tmp_path, "two/views/card.html", "<p>two</p>\n")

    refs = scan(resources, tmp_path).refs

    assert sorted((r.src, r.dst) for r in refs) == [
        ("one/app.py", "one/views/card.html"),
        ("two/app.py", "two/views/card.html"),
    ]


def test_a_conventional_prompts_directory_is_a_loader_root(resources, tmp_path):
    write(tmp_path, "agent/run.py", "SYSTEM = read_prompt('router.md')\n")
    write(tmp_path, "prompts/router.md", "route the request\n")

    assert [r.dst for r in scan(resources, tmp_path).refs] == ["prompts/router.md"]


# --------------------------------------------------------------------------- #
# Templates depend on templates
# --------------------------------------------------------------------------- #


def test_template_extends_and_include_link_templates(resources, tmp_path):
    write(tmp_path, "templates/page.html",
          '{% extends "base.html" %}\n{% include "partials/nav.html" %}\n')
    write(tmp_path, "templates/base.html", "<html></html>\n")
    write(tmp_path, "templates/partials/nav.html", "<nav></nav>\n")

    refs = scan(resources, tmp_path).refs

    assert {(r.dst, r.kind) for r in refs} == {
        ("templates/base.html", "template-include"),
        ("templates/partials/nav.html", "template-include"),
    }


# --------------------------------------------------------------------------- #
# Computed paths
# --------------------------------------------------------------------------- #


def test_an_fstring_pattern_fans_out_to_every_match(resources, tmp_path):
    write(tmp_path, "agent/load.py", 'def get(name):\n    return open(f"prompts/{name}.md")\n')
    write(tmp_path, "prompts/a.md", "a\n")
    write(tmp_path, "prompts/b.md", "b\n")

    refs = scan(resources, tmp_path).refs

    assert {r.dst for r in refs} == {"prompts/a.md", "prompts/b.md"}
    assert {r.kind for r in refs} == {"pattern"}


def test_an_unanchored_wildcard_is_rejected(resources, tmp_path):
    write(tmp_path, "agent/load.py", 'import glob\nfiles = glob.glob("*.md")\n')
    write(tmp_path, "prompts/a.md", "a\n")

    assert scan(resources, tmp_path).refs == []


def test_pattern_fan_out_is_capped_and_the_truncation_reported(resources, tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "MAX_PATTERN_TARGETS", 2)
    write(tmp_path, "agent/load.py", 'p = f"prompts/{n}.md"\n')
    for name in ("a", "b", "c", "d"):
        write(tmp_path, f"prompts/{name}.md", "x\n")

    result = scan(resources, tmp_path)

    assert len(result.refs) == 2
    assert result.truncated["patterns"] == 1


def test_per_file_reference_count_is_capped_and_reported(resources, tmp_path, monkeypatch):
    monkeypatch.setattr(resources, "MAX_REFS_PER_FILE", 1)
    write(tmp_path, "agent/load.py", "open('prompts/a.md')\nopen('prompts/b.md')\n")
    write(tmp_path, "prompts/a.md", "a\n")
    write(tmp_path, "prompts/b.md", "b\n")

    result = scan(resources, tmp_path)

    assert len(result.refs) == 1
    assert result.truncated["files"] == 1


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #


def test_go_embed_and_rust_include_str_are_edges(resources, tmp_path):
    write(tmp_path, "store/schema.go", '//go:embed schema.sql\nvar ddl string\n')
    write(tmp_path, "store/schema.sql", "CREATE TABLE t (id int);\n")
    write(tmp_path, "engine/q.rs", 'const Q: &str = include_str!("query.sql");\n')
    write(tmp_path, "engine/query.sql", "SELECT 1;\n")

    refs = scan(resources, tmp_path).refs

    assert {(r.src, r.dst, r.kind) for r in refs} == {
        ("store/schema.go", "store/schema.sql", "embed"),
        ("engine/q.rs", "engine/query.sql", "embed"),
    }


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def test_a_binary_file_costs_no_crash_and_no_edges(resources, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.bin").write_bytes(b"\x00\x01prompts/system.md\x00")
    write(tmp_path, "prompts/system.md", "hi\n")

    assert scan(resources, tmp_path).refs == []


def test_refs_are_json_serializable_for_the_summary(resources, tmp_path):
    write(tmp_path, "app/a.py", "open('prompts/system.md')\n")
    write(tmp_path, "prompts/system.md", "hi\n")

    refs = scan(resources, tmp_path).refs

    json.dumps([r._asdict() for r in refs])
