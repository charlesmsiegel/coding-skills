"""Unit tests for the helpers every code-visualization analyzer is built on.

These decide what counts as source, what counts as a test, and how complexity
is scored — so a bug here silently skews every tab in the atlas at once.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


@pytest.mark.parametrize(
    "path, lang",
    [
        ("a/b/c.py", "Python"),
        ("x.tsx", "TypeScript"),
        ("main.go", "Go"),
        ("Dockerfile", "Docker"),
        ("deploy/Makefile", "Make"),
        ("notes.md", "Markdown"),
        ("mystery.qqq", "Other"),
        ("SHOUTY.PY", "Python"),
    ],
)
def test_detect_lang(common, path, lang):
    assert common.detect_lang(path) == lang


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "test/thing.py",
        "src/__tests__/a.js",
        "spec/models_spec.rb",
        "app/foo.test.ts",
        "app/foo.spec.js",
        "pkg/test_helper.py",
        "conftest.py",
        "testing/util.py",
    ],
)
def test_is_test_path_recognizes_test_layouts(common, path):
    assert common.is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/core.py",
        "src/latest/thing.py",  # "test" inside a longer word is not a test dir
        "protest.py",
        "src/contested.js",
    ],
)
def test_is_test_path_ignores_lookalikes(common, path):
    assert common.is_test_path(path) is False


def test_loc_and_complexity_skips_blank_lines_and_comments(common):
    loc, branches, _ = common.loc_and_complexity(
        "def f(x):\n"
        "\n"
        "    # if this counted, the comment would add a branch\n"
        "    if x:\n"
        "        return 1\n"
        "    return 0\n"
    )
    assert loc == 5  # the blank line is not counted, the comment line is
    assert branches == 1


def test_loc_and_complexity_counts_boolean_operators_as_branches(common):
    _, branches, _ = common.loc_and_complexity("if a && b || c:\n    pass\n")
    assert branches >= 2


def test_json_block_escapes_a_closing_script_tag(common):
    """Raw </script> inside embedded JSON would end the block early."""
    blob = common.json_block({"payload": "</script><script>alert(1)</script>"})
    assert "</script>" not in blob
    assert "<\\/script>" in blob


def test_esc_escapes_quotes_and_angle_brackets(common):
    assert common.esc('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"


def test_walk_source_skips_vendored_and_dot_directories(tmp_path, common):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    for skipped in ("node_modules", ".venv", "__pycache__", ".git"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "junk.py").write_text("x = 1\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")

    found = {rel for rel, _ in common.walk_source(tmp_path)}

    assert "src/a.py" in found
    assert not any(f.endswith("junk.py") for f in found)
    # .github is the one dot-directory worth reporting on.
    assert ".github/workflows/ci.yml" in found


def test_walk_source_skips_oversized_files(tmp_path, common):
    (tmp_path / "big.py").write_text("x = 1\n" * 100)
    (tmp_path / "small.py").write_text("x = 1\n")

    found = {rel for rel, _ in common.walk_source(tmp_path, max_file_bytes=50)}

    assert found == {"small.py"}


def test_bar_cell_gives_a_visible_sliver_to_nonzero_values(common):
    """A value that rounds to 0% would render as an invisible bar."""
    assert 'width:1.5%' in common.bar_cell(1, 100_000)
    assert 'width:0.0%' in common.bar_cell(0, 0)
    assert 'width:100.0%' in common.bar_cell(5, 5)
