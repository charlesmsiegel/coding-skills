"""False positives removed from the tests and hygiene detectors, each beside the case it still catches.

Every rule here narrows what a detector reports, so every test that shows a
benign shape going quiet sits next to one showing the defect it was written
for still firing. A narrowing that cannot show both is a suppression.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def run_detector_text(script: str, target: Path) -> str:
    result = subprocess.run([sys.executable, str(SCRIPTS_DIR / script), str(target)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def of_type(findings: list[dict], smell: str) -> list[dict]:
    return [f for f in findings if f["smell_type"] == smell]


def lines_of(findings: list[dict], smell: str) -> list[int]:
    return sorted(f["line"] for f in of_type(findings, smell))


# --------------------------------------------------------------------------- #
# reasoned_noqa(): a suppression somebody explained
# --------------------------------------------------------------------------- #


def test_a_reasoned_noqa_is_read_on_the_line_or_the_comment_line_below(load_module):
    noqa = load_module(SCRIPTS_DIR, "noqa")
    lines = [
        "x = 1  # noqa: A001 -- the protocol names it",   # 1: reasoned
        "y = 2  # noqa: A001",                            # 2: bare
        "z = 3  # noqa: A001 --",                         # 3: separator, no reason
        "w = 4  # noqa: E501, A002: wire name",           # 4: list, colon separator
        "def f(",                                         # 5: wrapped
        "    # " + "noqa: A002 \u2014 the field is called id",  # 6: comment-only line below 5
        "v = 5",                                          # 7
        "u = 6  # noqa: A001 - fixed by the HTTP API",    # 8: code line below 7
    ]
    assert noqa.reasoned_noqa(lines, 1, ["A001"])
    assert not noqa.reasoned_noqa(lines, 1, ["A002"]), "the code must be the one asked about"
    assert not noqa.reasoned_noqa(lines, 2, ["A001"]), "a bare noqa records no decision"
    assert not noqa.reasoned_noqa(lines, 3, ["A001"]), "a separator alone is not a reason"
    assert noqa.reasoned_noqa(lines, 4, ["A002"])
    assert noqa.reasoned_noqa(lines, 5, ["A002"])
    assert not noqa.reasoned_noqa(lines, 7, ["A001"]), "a code line below is another statement"


# --------------------------------------------------------------------------- #
# shadows_builtin
# --------------------------------------------------------------------------- #


def test_file_is_not_a_python3_builtin_but_id_is(tmp_path):
    write(tmp_path, {"m.py": "def upload(file: bytes) -> None:\n    print(file)\n\n"
                             "def fetch(id: str) -> str:\n    return id\n"})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert [f["line"] for f in shadows] == [4]


def test_a_method_named_after_a_builtin_hides_it_only_from_its_own_class_body(tmp_path):
    write(tmp_path, {
        "quiet.py": (
            "class Store:\n"
            "    def set(self, value):\n"
            "        self.v = value\n"
            "    def format(self):\n"
            "        return str(self.v)\n"
            "    type = 'store'\n"
        ),
        # The rest of the class body now calls the method, not the builtin.
        "loud.py": (
            "class Registry:\n"
            "    def set(self, value):\n"
            "        self.v = value\n"
            "    seen = set()\n"
        ),
    })
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert [(Path(f["file"]).name, f["line"]) for f in shadows] == [("loud.py", 2)]


def test_a_module_level_rebinding_is_still_a_shadow(tmp_path):
    write(tmp_path, {"m.py": "list = []\n\nclass Box:\n    def put(self, input):\n        return input\n"})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert lines_of(shadows, "shadows_builtin") == [1, 4]


def test_a_reasoned_noqa_for_the_shadow_makes_a_candidate(tmp_path):
    write(tmp_path, {"m.py": (
        "def get(id: str) -> str:  # noqa: A002 -- the HTTP path parameter is named id\n"
        "    return id\n"
        "def put(id: str) -> str:  # noqa: A002\n"
        "    return id\n"
        "def post(id: str) -> str:  # noqa: A001 -- wrong code for an argument\n"
        "    return id\n"
        "def patch(\n"
        "    id: str,  # noqa: A002 -- wrapped signature, reason on the argument's line\n"
        ") -> str:\n"
        "    return id\n"
    )})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    by_line = {f["line"]: f for f in shadows}
    assert sorted(by_line) == [1, 3, 5, 8]
    for line in (1, 8):
        assert by_line[line]["kind"] == "candidate"
        assert by_line[line]["suggestion"] == "" and by_line[line]["also_caused_by"]
    for line in (3, 5):
        assert by_line[line]["kind"] == "finding" and by_line[line]["suggestion"]


def test_a_naming_candidate_prints_no_fix_arrow(tmp_path):
    write(tmp_path, {"m.py": "def get(id: str) -> str:  # noqa: A002 -- the path parameter\n"
                             "    return id\n"})
    text = run_detector_text("find_naming_issues.py", tmp_path)
    assert "[CANDIDATE]" in text and "also caused by" in text
    assert not [line for line in text.splitlines() if line.strip() == "→"]


# --------------------------------------------------------------------------- #
# os_path_join
# --------------------------------------------------------------------------- #


def test_nested_os_path_calls_are_one_finding_per_line(tmp_path):
    write(tmp_path, {"m.py": (
        "import os\n"
        "HERE = os.path.join(os.path.dirname(__file__), 'data')\n"
        "def f(p):\n"
        "    return os.path.exists(p)\n"
    )})
    assert lines_of(run_detector("find_outdated_idioms.py", tmp_path), "os_path_join") == [2, 4]


# --------------------------------------------------------------------------- #
# public_*_no_docstring
# --------------------------------------------------------------------------- #

BODY = "    a = 1\n    b = a + 1\n    return b\n"


def test_a_bare_exception_class_needs_no_docstring_but_other_classes_do(tmp_path):
    write(tmp_path, {"m.py": (
        '"""Module."""\n'
        "class StoreError(Exception):\n    pass\n"
        "class NotFound(StoreError):\n    ...\n"
        "class Shape:\n    pass\n"
        "class ParseError(ValueError):\n    def __init__(self, at):\n        self.at = at\n"
    )})
    classes = of_type(run_detector("find_missing_docstrings.py", tmp_path), "public_class_no_docstring")
    assert sorted(f["description"].split("'")[1] for f in classes) == ["ParseError", "Shape"]


def test_a_private_module_has_no_public_surface_but_init_does(tmp_path):
    for name in ("_impl.py", "impl.py", "__init__.py"):
        write(tmp_path, {name: '"""Module."""\n' f"def visible():\n{BODY}"
                               "class Thing:\n    pass\n"})
    found = run_detector("find_missing_docstrings.py", tmp_path)
    reported = {(Path(f["file"]).name, f["smell_type"]) for f in found}
    assert reported == {
        ("impl.py", "public_function_no_docstring"), ("impl.py", "public_class_no_docstring"),
        ("__init__.py", "public_function_no_docstring"), ("__init__.py", "public_class_no_docstring"),
    }


# --------------------------------------------------------------------------- #
# missing_return_annotation / missing_param_annotation
# --------------------------------------------------------------------------- #

GAPS = ("missing_return_annotation", "missing_param_annotation")


def gaps(findings: list[dict]) -> list[tuple[int, str]]:
    return sorted((f["line"], f["smell_type"]) for f in findings if f["smell_type"] in GAPS)


def test_closures_and_methods_of_private_or_local_classes_are_not_api_surface(tmp_path):
    write(tmp_path, {"m.py": (
        "def outer(x: int) -> int:\n"            # 1
        "    def helper(y):\n"                  # 2  closure
        "        return y + 1\n"
        "    class Local:\n"
        "        def run(self, z):\n"           # 5  method of a class inside a function
        "            return z\n"
        "    return helper(x)\n"
        "class _Private:\n"
        "    def run(self, z):\n"               # 9  method of a private class
        "        return z\n"
        "    class Inner:\n"
        "        def go(self, q):\n"            # 12 nested in a private class
        "            return q\n"
        "class Public:\n"
        "    def run(self, z):\n"               # 15 public method: still reported
        "        return z\n"
        "def plain(a):\n"                       # 17 module function: still reported
        "    return a\n"
    )})
    assert [line for line, _ in gaps(run_detector("find_type_gaps.py", tmp_path))] == [15, 17]


def test_route_handlers_are_exempt_and_the_same_function_undecorated_is_not(tmp_path):
    write(tmp_path, {"m.py": (
        "from fastapi import APIRouter\n"
        "from flask import Flask\n"
        "router = APIRouter()\n"
        "app = Flask(__name__)\n"
        "@router.get('/things/{tid}')\n"
        "def get_thing(tid: str):\n"           # 6
        "    return {'id': tid}\n"
        "@router.post('/things', response_model=dict)\n"
        "async def make_thing(body):\n"        # 9
        "    return body\n"
        "@app.route('/index')\n"
        "def index():\n"                       # 12
        "    return 'hi'\n"
        "def get_other(tid: str):\n"           # 14 not registered: still reported
        "    return {'id': tid}\n"
    )})
    assert gaps(run_detector("find_type_gaps.py", tmp_path)) == [(14, "missing_return_annotation")]


def test_a_bare_signature_is_one_finding_that_names_both_gaps(tmp_path):
    write(tmp_path, {"m.py": (
        "def both(a, b):\n    return a\n"
        "def params_only(a) -> int:\n    return a\n"
        "def return_only(a: int):\n    return a\n"
    )})
    found = run_detector("find_type_gaps.py", tmp_path)
    assert gaps(found) == [(1, "missing_return_annotation"), (3, "missing_param_annotation"),
                           (5, "missing_return_annotation")]
    both = next(f for f in found if f["line"] == 1)
    assert "a, b" in both["description"] and "a, b" in both["suggestion"]


def test_ignoring_the_return_gap_still_reports_the_parameters(tmp_path):
    write(tmp_path, {"m.py": "def both(a, b):\n    return a\n"})
    found = run_detector("find_type_gaps.py", tmp_path, "--ignore", "missing_return_annotation")
    assert gaps(found) == [(1, "missing_param_annotation")]
