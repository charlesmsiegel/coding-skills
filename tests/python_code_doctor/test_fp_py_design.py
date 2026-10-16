"""False positives removed from the complexity, design and duplication detectors.

Each rule here was written against a real repository's report, where a detector
charged a defect that was not there, or charged one defect several times: an
`elif` ladder read as seventeen levels of nesting, a closure factory charged
for the branches of the closure it returns, a test's assertions counted as
branches, the same parameter list reported by two detectors. Every test that
pins a removed false positive sits next to one that pins the true positive the
same rule must keep catching — a fix that quietened both would pass the first
test alone.

Fixtures are written to tmp_path at runtime, as in test_detectors.py.
"""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def write(root: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return root


def of_type(found: list[dict], smell: str) -> list[dict]:
    return [r for r in found if (r.get("smell_type") or r.get("issue_type")) == smell]


def findings(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind", "finding") == "finding"]


def candidates(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind") == "candidate"]


def assert_candidate_shape(record: dict) -> None:
    """A candidate names its benign readings and carries no fix."""
    assert record["kind"] == "candidate"
    assert record["also_caused_by"], "a candidate names how healthy code produces it"
    assert not record["suggestion"], "a candidate carries what to confirm, not a fix"


# --------------------------------------------------------------------------- #
# analyze_complexity: the control-flow family
# --------------------------------------------------------------------------- #


@pytest.fixture
def complexity(load_module):
    return load_module(SCRIPTS_DIR, "analyze_complexity")


def measure(module, source: str, filename: str = "sample.py") -> tuple[int, int, int]:
    """(cyclomatic, cognitive, nesting) of the first function in ``source``."""
    source = textwrap.dedent(source)
    function = ast.parse(source).body[0]
    visitor = module.ComplexityVisitor(filename, source.splitlines(), module.Config())
    return visitor._cyclomatic(function), visitor._cognitive(function), visitor._max_nesting(function)


LADDER = """\
def classify(k):
    if k == 1:
        return "one"
    elif k == 2:
        return "two"
    elif k == 3:
        return "three"
    elif k == 4:
        return "four"
    elif k == 5:
        return "five"
    else:
        return "many"
"""

STAIRCASE = """\
def route(a):
    if a:
        if a > 1:
            if a > 2:
                if a > 3:
                    if a > 4:
                        return a
    return 0
"""


def test_an_elif_ladder_is_as_deep_as_one_branch(complexity):
    # An elif is a sibling of its `if`, not a block inside it: five branches
    # are one level deep, and each costs one flat point of cognitive load.
    cyclomatic, cognitive, nesting = measure(complexity, LADDER)
    assert nesting == 1
    assert cognitive == 1 + 4 + 1  # the if, four elifs, the else
    assert cyclomatic == 6


def test_an_else_holding_an_if_is_still_nesting(complexity):
    # `else:` then an indented `if` is a block inside a block — the same tree as
    # `elif`, told apart by its column, and it still costs depth.
    source = """\
    def f(a, b):
        if a:
            return 1
        else:
            if b:
                return 2
        return 3
    """
    _, cognitive, nesting = measure(complexity, source)
    assert nesting == 2
    assert cognitive == 1 + 1 + 2  # if, else, and the if nested one level in


def test_staircase_of_ifs_still_reports_nesting(tmp_path):
    write(tmp_path, {"sample.py": STAIRCASE})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [r["issue_type"] for r in found] == ["nesting_depth"]


def test_a_long_elif_ladder_is_not_reported_as_nesting(tmp_path):
    branches = "".join(f"    elif k == {n}:\n        return {n}\n" for n in range(2, 13))
    write(tmp_path, {"sample.py": "def f(k):\n    if k == 1:\n        return 1\n" + branches})
    found = run_detector("analyze_complexity.py", tmp_path)
    # Twelve branches are a cyclomatic-complexity question; they are not
    # twelve levels of nesting, and they are not quadratic cognitive load.
    assert [r["issue_type"] for r in found] == ["cyclomatic_complexity"]
    assert "nesting" not in found[0]["description"]
    assert "cognitive" not in found[0]["description"]


def test_try_and_with_add_no_cognitive_nesting(complexity):
    source = """\
    def f(path):
        with open(path) as handle:
            try:
                if handle:
                    return 1
            except OSError:
                return 2
    """
    _, cognitive, nesting = measure(complexity, source)
    # `if` at level 0 costs 1, the except clause at level 0 costs 1. Nesting
    # depth still counts the blocks a reader has to hold open.
    assert cognitive == 2
    assert nesting == 3


def test_ternaries_and_async_loops_count(complexity):
    source = """\
    async def f(items):
        async for item in items:
            value = 1 if item else 2
    """
    cyclomatic, cognitive, _ = measure(complexity, source)
    assert cyclomatic == 2
    assert cognitive == 1 + 2  # the loop, then a ternary one level in


def test_nested_function_branches_belong_to_the_nested_function(tmp_path):
    branches = "".join(f"        if x == {n}:\n            return {n}\n" for n in range(12))
    write(tmp_path, {"sample.py": "def outer():\n    def inner(x):\n" + branches
                     + "        return None\n    return inner\n"})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [(r["name"], r["issue_type"]) for r in found] == [("inner", "cyclomatic_complexity")]


def test_assertions_in_a_test_are_not_branches(tmp_path):
    asserts = "".join(f"    assert result[{n}] == {n}\n" for n in range(14))
    body = "def test_values(result):\n" + asserts
    write(tmp_path, {"tests/test_values.py": body, "checks.py": body.replace("def test_", "def check_")})
    found = run_detector("analyze_complexity.py", tmp_path)
    # In production code an assert is a conditional raise, and fourteen of them
    # are fourteen ways out; in a test they are one straight path of expectations.
    assert [(Path(r["file"]).name, r["issue_type"]) for r in found] == [
        ("checks.py", "cyclomatic_complexity")]


def test_one_control_flow_finding_names_every_measure_it_exceeds(tmp_path):
    rungs = "".join("    " * depth + f"if a > {depth}:\n" for depth in range(1, 8))
    flat = "".join(f"    if b == {n} and c:\n        return {n}\n" for n in range(12))
    write(tmp_path, {"sample.py": "def tangled(a, b, c):\n" + rungs + "    " * 8 + "return a\n" + flat})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert len(found) == 1, found
    record = found[0]
    assert record["issue_type"] == "cyclomatic_complexity"
    assert record["severity"] == "high"
    for measure_name in ("cyclomatic complexity", "cognitive complexity", "nesting depth"):
        assert measure_name in record["description"]


def test_cognitive_complexity_alone_is_reported_under_its_own_name(tmp_path):
    # Four levels of loops: within the nesting limit and with few branches,
    # but a decision four levels in costs a reader more than one at the top.
    source = """\
    def walk(grid):
        for row in grid:
            for cell in row:
                for item in cell:
                    for part in item:
                        first = part if part else None
                        yield first if first else part
    """
    write(tmp_path, {"sample.py": source})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [r["issue_type"] for r in found] == ["cognitive_complexity"]
    ignored = run_detector("analyze_complexity.py", tmp_path, "--ignore", "cognitive_complexity")
    assert ignored == []


def test_ignoring_one_measure_reports_the_next(tmp_path):
    rungs = "".join("    " * depth + f"if a > {depth}:\n" for depth in range(1, 7))
    flat = "".join(f"    if b == {n}:\n        return {n}\n" for n in range(10))
    write(tmp_path, {"sample.py": "def tangled(a, b):\n" + rungs + "    " * 7 + "return a\n" + flat})
    found = run_detector("analyze_complexity.py", tmp_path, "--ignore", "cyclomatic_complexity")
    assert [r["issue_type"] for r in found] == ["cognitive_complexity"]
    assert "cyclomatic" not in found[0]["description"]


# --------------------------------------------------------------------------- #
# analyze_complexity: length and size
# --------------------------------------------------------------------------- #


def _statements(count: int, indent: str = "    ") -> str:
    return "".join(f"{indent}value_{n} = {n}\n" for n in range(count))


def test_a_docstring_is_not_function_length(tmp_path):
    docstring = '    """Summary.\n\n' + "    Explanation line.\n" * 30 + '    """\n'
    write(tmp_path, {"sample.py": "def documented():\n" + docstring + _statements(45)})
    assert run_detector("analyze_complexity.py", tmp_path) == []


def test_nested_function_lines_are_not_the_enclosing_functions_length(tmp_path):
    inner = "    def helper():\n" + _statements(30, "        ") + "        return 0\n"
    write(tmp_path, {"sample.py": "def outer():\n" + inner + _statements(30) + "    return helper\n"})
    assert run_detector("analyze_complexity.py", tmp_path) == []


def test_a_long_function_is_still_reported(tmp_path):
    write(tmp_path, {"sample.py": 'def long():\n    """Doc."""\n' + _statements(60) + "    return 0\n"})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [r["issue_type"] for r in found] == ["function_length"]
    assert found[0]["value"] == 62  # the def line, sixty statements, the return


def _method(n: int) -> str:
    return f"    def method_{n}(self):\n        return {n}\n\n"


def test_class_size_counts_code_not_docstrings(tmp_path):
    documented = "".join(
        f'    def method_{n}(self):\n        """Doc.\n\n' + "        More.\n" * 20
        + f'        """\n        return {n}\n\n' for n in range(15))
    write(tmp_path, {"sample.py": "class Documented:\n" + documented})
    assert run_detector("analyze_complexity.py", tmp_path) == []


def test_one_size_finding_per_class(tmp_path):
    methods = "".join(
        f"    def method_{n}(self):\n" + _statements(14, "        ") + f"        return {n}\n\n"
        for n in range(25))
    write(tmp_path, {"sample.py": "class Big:\n" + methods})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [r["issue_type"] for r in found] == ["class_size"]
    assert "code lines" in found[0]["description"] and "methods" in found[0]["description"]


# --------------------------------------------------------------------------- #
# analyze_complexity: parameter_count
# --------------------------------------------------------------------------- #


def test_self_is_not_a_parameter_anybody_passes(tmp_path):
    write(tmp_path, {"sample.py": """\
        class K:
            def method(self, a, b, c, d, e):
                return a

            @classmethod
            def build(cls, a, b, c, d, e):
                return a

            def wide(self, a, b, c, d, e, f):
                return a
        """})
    found = run_detector("analyze_complexity.py", tmp_path)
    assert [(r["name"], r["value"]) for r in found] == [("K.wide", 6)]


def test_pytest_fixture_requests_are_not_a_parameter_list(tmp_path):
    write(tmp_path, {
        "tests/test_api.py": """\
            import pytest

            @pytest.fixture
            def client(app, db, cache, queue, clock, settings):
                return app

            def test_round_trip(client, db, cache, queue, clock, settings, tmp_path):
                assert client

            def build(a, b, c, d, e, f):
                return a
            """,
    })
    found = run_detector("analyze_complexity.py", tmp_path)
    # A helper in a test file is still called by somebody.
    assert [(r["name"], r["issue_type"]) for r in found] == [("build", "parameter_count")]


# --------------------------------------------------------------------------- #
# find_code_smells
# --------------------------------------------------------------------------- #


def test_a_long_signature_is_reported_once(tmp_path):
    write(tmp_path, {"sample.py": "def f(a, b, c, d, e, g, h):\n    return a\n"})
    smells = run_detector("find_code_smells.py", tmp_path)
    assert not of_type(smells, "long_parameter_list"), "parameter_count already reports it"
    complexity = run_detector("analyze_complexity.py", tmp_path)
    assert [r["issue_type"] for r in complexity] == ["parameter_count"]


def test_a_literal_in_a_test_is_the_expected_value(tmp_path):
    write(tmp_path, {
        "tests/test_totals.py": """\
            def test_total(client):
                resp = client.get("/total")
                assert resp.status_code == 200
                assert resp.json()["total"] == 10 + 100 + 5
                assert compute(7) == 49
            """,
        "totals.py": """\
            def blocks(hi):
                return [hi // 5, hi % 5]
            """,
    })
    found = of_type(run_detector("find_code_smells.py", tmp_path), "magic_number")
    assert [(Path(r["file"]).name, r["description"]) for r in found] == [
        ("totals.py", "Magic number 5"), ("totals.py", "Magic number 5")]


def test_an_http_status_where_a_status_belongs_is_not_magic(tmp_path):
    write(tmp_path, {"routes.py": """\
        import os

        def handler(resp, path):
            if resp.status_code == 409:
                raise HTTPException(404, detail="missing")
            if status == 429:
                return JSONResponse({"ok": False}, 503)
            os.chmod(path, 0o755)
            return retry(resp, 404)
        """})
    found = of_type(run_detector("find_code_smells.py", tmp_path), "magic_number")
    # A 404 handed to an arbitrary function is still an unexplained number.
    assert [r["description"] for r in found] == ["Magic number 404"]
    assert found[0]["line"] == 9


def test_type_is_comparison_is_the_exact_check_not_a_smell(tmp_path):
    write(tmp_path, {"sample.py": """\
        def kinds(x, a, b):
            if type(x) is int:
                return 1
            if type(a) is type(b):
                return 2
            if type(x) is not bool:
                return 3
            if type(x) == list:
                return 4
            return type(a) != type(b)
        """})
    found = of_type(run_detector("find_code_smells.py", tmp_path), "type_comparison")
    # Two `==`/`!=` comparisons, one finding each, even when both sides call type().
    assert [r["line"] for r in found] == [8, 10]
