"""The repository roll-up table build_theory.py's --root mode renders.

`read_package_grade` and `render_package_table` are the produced interface a
repo-level theory.html is built from — `--package NAME:PATH` is how the
roll-up learns about each package's own document. Every assertion here is
scoped to the rendered `<h3>Packages</h3>` table, not the whole page: the
same page also embeds the package list as raw JSON in its metadata block
(`meta["packages"]`), and a substring check against the whole document would
just as happily match that JSON as the rendering — the failure shape a
sibling test file (test_summary_theory.py) was found to have shipped.

Package pages are built with the real build_theory.py wherever the scenario
allows it, rather than hand-writing `theory-meta` blocks, so these tests
exercise the actual seam between "a package's own document" and "the roll-up
that reads it back" rather than a fixture that merely agrees with this file's
assumptions about that seam's shape.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_theory.py")


def verdict_file(tmp_path, index: int, unit: str = "billing", **overrides) -> Path:
    dims = {}
    for key in ("absorption", "world_mapping", "abstraction", "justification", "honest_limits"):
        dims[key] = {"step": overrides.get(key, 1.0),
                     "rationale": f"judge {index} on {key}",
                     "evidence": [f"src/{unit}/{key}.py:{index + 1}"]}
    payload = {
        "schema": "theory-verdict/1",
        "unit": unit,
        "theory": f"{unit} models a coherent thing.",
        "instead_of": "A grab-bag of helpers, rejected because nothing ties them together.",
        "trivial": overrides.get("trivial", False),
        "trivial_reason": overrides.get("trivial_reason", ""),
        "dimensions": dims,
        "rehearsals": [
            {"requirement": "requirement A", "verdict": "extension",
             "why": "fits the existing shape", "evidence": [f"src/{unit}/a.py:1"]},
            {"requirement": "requirement B", "verdict": "extension",
             "why": "fits the existing shape", "evidence": [f"src/{unit}/b.py:1"]},
            {"requirement": "requirement C", "verdict": "patch",
             "why": "does not fit cleanly", "evidence": [f"src/{unit}/c.py:1"]},
        ],
    }
    path = tmp_path / f"verdict-{unit}-{index}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def three(tmp_path, unit: str = "billing", **overrides) -> tuple:
    return tuple(verdict_file(tmp_path, i, unit=unit, **overrides) for i in range(3))


def meta_of(page: Path) -> dict:
    match = re.search(r'id="theory-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match, "the page carries no theory-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def package_table_of(text: str) -> str:
    """Just the roll-up's `<h3>Packages</h3>` table, not the whole page.

    The same page carries the package list a second time, as raw JSON in the
    `theory-meta` script block (`meta["packages"]`) — an unscoped substring
    check would pass against that JSON even if the table itself rendered
    nothing, which is exactly the failure this file exists to avoid.
    """
    match = re.search(r"<h3>Packages</h3>.*?</table></div>", text, re.S)
    assert match, "no Packages table on the page"
    return match.group(0)


@pytest.fixture
def repo_with_code(repo):
    for i in range(12):
        repo.write(f"src/billing/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    return repo


def build_package(repo, run_script, out, name, root_dir, files) -> Path:
    """A single package's own theory.html, via a real (non-root) invocation."""
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    run_script(SCRIPT, "--out", out, "--name", name, "--repo", repo.path,
               "--root-dir", root_dir, *args)
    return out


def build_root(repo, run_script, out, files, packages=(), expect_rc=0):
    """The repository roll-up, with zero or more --package NAME:PATH entries."""
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    for spec in packages:
        args += ["--package", spec]
    return run_script(SCRIPT, "--out", out, "--name", "repo", "--repo", repo.path,
                      "--root", *args, expect_rc=expect_rc)


# --- a graded package rolls up with its own score and grade -----------------

def test_a_graded_packages_score_and_grade_are_read_back_out_of_its_own_document(
        repo_with_code, run_script, tmp_path):
    package_page = build_package(repo_with_code, run_script, tmp_path / "billing" / "theory.html",
                                 "billing", "src/billing", three(tmp_path))
    generated = meta_of(package_page)
    assert generated["score"] is not None and generated["grade"] != "—", (
        "the fixture must produce a real grade, or reading it back proves nothing"
    )

    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"billing:{package_page}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "billing" in table
    assert f'{generated["score"]:.1f}' in table, (
        "the roll-up must show the score the package's own document computed, "
        "not one this test told it to expect"
    )
    assert f">{generated['grade']}<" in table
    assert "graded" in table


# --- a missing package's document stays in the table, marked as missing -----

def test_a_missing_package_document_stays_in_the_table_marked_not_generated(
        repo_with_code, run_script, tmp_path):
    missing_path = tmp_path / "search" / "theory.html"
    assert not missing_path.exists(), "the fixture must actually be missing"

    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"search:{missing_path}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "search" in table, "a missing package must not vanish from the roll-up"
    assert "not generated" in table


# --- an exempt package reads as too small, not as a pass --------------------

def test_an_exempt_packages_document_reads_as_too_small_not_as_a_pass(
        repo_with_code, run_script, tmp_path):
    repo_with_code.write("src/tiny/a.py", "x = 1\n")
    repo_with_code.write("src/tiny/b.py", "y = 2\n")
    repo_with_code.commit()
    files = (verdict_file(tmp_path, 0, unit="tiny", trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 1, unit="tiny", trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 2, unit="tiny"))
    package_page = build_package(repo_with_code, run_script, tmp_path / "tiny" / "theory.html",
                                 "tiny", "src/tiny", files)
    generated = meta_of(package_page)
    assert generated["score"] is None and generated["exempt"] is True, (
        "the fixture must actually be exempt, or reading it back proves nothing"
    )

    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"tiny:{package_page}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "tiny" in table
    assert "<td>too small to warrant a theory</td>" in table
    assert "<td>graded</td>" not in table, "an exempt package must not also be marked graded"


# --- a disputed package is marked as such in the table -----------------------

def test_a_disputed_packages_document_is_marked_as_such_in_the_table(
        repo_with_code, run_script, tmp_path):
    files = (verdict_file(tmp_path, 0, unit="billing", abstraction=1.0),
             verdict_file(tmp_path, 1, unit="billing", abstraction=0.25),
             verdict_file(tmp_path, 2, unit="billing", abstraction=1.0))
    package_page = build_package(repo_with_code, run_script, tmp_path / "billing" / "theory.html",
                                 "billing", "src/billing", files)
    generated = meta_of(package_page)
    assert generated["disputed"], (
        "the fixture must produce a real disputed dimension, or reading it back proves nothing"
    )

    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"billing:{package_page}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "billing" in table
    assert "panel disagreed on" in table
    assert "Abstraction" in table, (
        "the dimension is named the same way here as inside theory.html — a reader "
        "cannot tell `world_mapping` and 'World-mapping' are one row"
    )


# --- exempt and disputed are not alternatives --------------------------------

def test_an_exempt_package_that_also_split_the_panel_keeps_its_disagreement(
        repo_with_code, run_script, tmp_path):
    """Two judges can call a unit trivial and the panel still split two rungs.

    Testing exemption first dropped the disagreement from the row — the one
    thing the roll-up exists to surface.
    """
    repo_with_code.write("src/tiny/a.py", "x = 1\n")
    repo_with_code.write("src/tiny/b.py", "y = 2\n")
    repo_with_code.commit()
    files = (verdict_file(tmp_path, 0, unit="tiny", trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 1, unit="tiny", trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 2, unit="tiny", abstraction=0.25))
    package_page = build_package(repo_with_code, run_script, tmp_path / "tiny" / "theory.html",
                                 "tiny", "src/tiny", files)
    generated = meta_of(package_page)
    assert generated["exempt"] is True and generated["disputed"] == ["abstraction"], (
        "the fixture must be both exempt and disputed, or reading it back proves nothing"
    )

    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"tiny:{package_page}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "<td>too small to warrant a theory; panel disagreed on Abstraction</td>" in table


def test_the_roll_up_caption_says_an_exempt_package_is_not_a_passing_one(
        repo_with_code, run_script, tmp_path):
    root_page = tmp_path / "theory.html"
    build_root(repo_with_code, run_script, root_page, three(tmp_path, unit="repo"),
               packages=[f"search:{tmp_path / 'search' / 'theory.html'}"])

    table = package_table_of(root_page.read_text(encoding="utf-8"))
    assert "too small to warrant a theory is listed as such, not as passing" in table, (
        "the caption is what stops a null being read as a pass"
    )


# --- the --root guard --------------------------------------------------------

def test_package_without_root_exits_2_naming_root(repo_with_code, run_script, tmp_path):
    out = tmp_path / "theory.html"
    args = []
    for path in three(tmp_path):
        args += ["--verdict", str(path)]
    args += ["--package", f"billing:{tmp_path / 'billing' / 'theory.html'}"]
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/billing", *args, expect_rc=2)

    assert "--root" in result.stderr
    assert not out.exists()
