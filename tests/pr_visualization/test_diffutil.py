"""Unit tests for diffutil — the parser every pr-visualization tab is built on.

If parse_diff drops or mislabels a file, every downstream tab inherits the
error and the report reads as an all-clear for a change nobody analyzed.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "pr-visualization" / "scripts"

MODIFIED = """\
diff --git a/src/core.py b/src/core.py
index 1111111..2222222 100644
--- a/src/core.py
+++ b/src/core.py
@@ -1,4 +1,5 @@
 def parse(text):
-    return text
+    return text.strip()
+

 def other():
"""

ADDED = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""

DELETED = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 3333333..0000000
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""

RENAMED = """\
diff --git a/old.py b/new_name.py
similarity index 95%
rename from old.py
rename to new_name.py
"""

BINARY = """\
diff --git a/blob.bin b/blob.bin
index 1f14ada..c699c7b 100644
Binary files a/blob.bin and b/blob.bin differ
"""


@pytest.fixture
def diffutil(load_module):
    return load_module(SCRIPTS, "diffutil")


@pytest.mark.parametrize(
    "path, generated",
    [
        ("docs/codemap.html", True),
        ("docs/pr-12.html", True),
        ("docs/pr-my-branch.html", True),
        ("docs/architecture.html", False),
        ("src/docs/codemap.html", False),
        ("docs/nested/codemap.html", False),
    ],
)
def test_is_generated_doc(diffutil, path, generated):
    assert diffutil.is_generated_doc(path) is generated


def test_parse_diff_counts_adds_and_dels(diffutil):
    (fd,) = diffutil.parse_diff(MODIFIED)

    assert fd.path == "src/core.py"
    assert fd.status == "M"
    assert (fd.adds, fd.dels) == (2, 1)
    assert fd.changed == 3


def test_parse_diff_numbers_added_lines_against_the_new_file(diffutil):
    (fd,) = diffutil.parse_diff(MODIFIED)

    assert list(fd.added_lines()) == [(2, "    return text.strip()"), (3, "")]
    assert list(fd.removed_lines()) == ["    return text"]


def test_parse_diff_reports_touched_line_ranges(diffutil):
    (fd,) = diffutil.parse_diff(MODIFIED)

    assert list(fd.changed_new_line_ranges()) == [(2, 3)]


def test_pure_deletion_hunk_still_yields_a_range(diffutil):
    """A deletion changes behavior at a line even though it adds none."""
    (fd,) = diffutil.parse_diff(DELETED)

    assert fd.status == "D"
    assert fd.adds == 0 and fd.dels == 1


def test_parse_diff_labels_added_files(diffutil):
    (fd,) = diffutil.parse_diff(ADDED)

    assert (fd.path, fd.status, fd.adds) == ("new.py", "A", 2)


def test_parse_diff_keeps_the_old_path_of_a_deleted_file(diffutil):
    (fd,) = diffutil.parse_diff(DELETED)

    assert fd.path == "gone.py"
    assert fd.old_path == "gone.py"


def test_parse_diff_tracks_renames(diffutil):
    (fd,) = diffutil.parse_diff(RENAMED)

    assert fd.status == "R"
    assert fd.path == "new_name.py"
    assert fd.old_path == "old.py"


def test_parse_diff_handles_several_files_in_one_diff(diffutil):
    fds = diffutil.parse_diff(MODIFIED + ADDED + DELETED)

    assert [f.path for f in fds] == ["src/core.py", "new.py", "gone.py"]


def test_parse_diff_reports_binary_files(diffutil):
    """Binary records have no +++/--- lines; the path must be recovered from
    the 'diff --git' header so a binary-only PR doesn't parse to an empty diff."""
    fds = diffutil.parse_diff(BINARY)

    assert [f.path for f in fds] == ["blob.bin"]
    assert fds[0].binary is True


@pytest.mark.parametrize("path, has_patterns", [("a.py", True), ("a.ts", True), ("a.qqq", False)])
def test_def_patterns_are_language_aware(diffutil, path, has_patterns):
    assert bool(diffutil.def_patterns_for(path)) is has_patterns
