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
import os
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


def test_assertions_are_not_branches(tmp_path):
    asserts = "".join(f"    assert result[{n}] == {n}\n" for n in range(14))
    body = "def test_values(result):\n" + asserts
    write(tmp_path, {"tests/test_values.py": body, "checks.py": body.replace("def test_", "def check_")})
    found = run_detector("analyze_complexity.py", tmp_path)
    # Assertions describe invariants in both tests and production code. Their
    # conditions do not add decision points to the implementation's branches.
    assert found == []


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


def test_keyword_only_options_are_named_at_every_call_site(tmp_path):
    """The refactoring catalog's own remedy for a long parameter list is keyword
    arguments: options a caller must name cannot be lined up in the wrong order."""
    write(tmp_path, {"sample.py": """\
        def render(cid, sid, *, model="", images=0, budget=None, fence=True):
            return cid

        def lined_up(a, b, c, d, e, f):
            return a

        def everything(a, b, *, c, d, e, f, g, h, i, j, k):
            return a
        """})
    found = run_detector("analyze_complexity.py", tmp_path)
    by_name = {r["name"]: r for r in found if r["issue_type"] == "parameter_count"}
    assert set(by_name) == {"lined_up", "everything"}
    assert by_name["lined_up"]["value"] == 6, "six positional values still parade"
    assert by_name["everything"]["value"] == 11, "named or not, eleven inputs is too many"
    assert "keyword-only" in by_name["everything"]["description"]


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


# --------------------------------------------------------------------------- #
# find_parameter_objects: data_clump
# --------------------------------------------------------------------------- #


def test_overlapping_parameter_groups_are_one_clump(tmp_path):
    write(tmp_path, {"sample.py": """\
        def a(w, x, y, z):
            return w

        def b(w, x, y, z):
            return w

        def c(w, x, y, z):
            return w

        def d(x, y, z):
            return x
        """})
    found = run_detector("find_parameter_objects.py", tmp_path)
    assert len(found) == 1, [r["description"] for r in found]
    assert found[0]["line"] == 1
    assert found[0]["related_lines"] == [4, 7, 10]


def test_disjoint_groups_on_one_function_are_two_clumps(tmp_path):
    # Two parameter objects that happen to meet in one signature are two
    # objects: charging is per parameter, not per function.
    write(tmp_path, {"sample.py": """\
        def both(host, port, timeout, width, height, depth):
            return host

        def connect(host, port, timeout):
            return host

        def reconnect(host, port, timeout):
            return host

        def box(width, height, depth):
            return width

        def crate(width, height, depth):
            return width
        """})
    found = run_detector("find_parameter_objects.py", tmp_path)
    assert sorted(r["description"].split(" appear")[0] for r in found) == [
        "Parameters ['depth', 'height', 'width']", "Parameters ['host', 'port', 'timeout']"]


def test_fixture_requests_and_route_parameters_are_not_clumps(tmp_path):
    write(tmp_path, {
        "tests/test_things.py": """\
            import pytest

            @pytest.fixture
            def store(monkeypatch, tmp_path, client):
                return client

            def test_one(monkeypatch, tmp_path, client):
                assert client

            def test_two(monkeypatch, tmp_path, client):
                assert client

            def test_three(monkeypatch, tmp_path, client):
                assert client
            """,
        "routes.py": """\
            @router.get("/c/{cid}/s/{sid}")
            def read(cid, sid, body):
                return cid

            @router.put("/c/{cid}/s/{sid}")
            def write(cid, sid, body):
                return cid

            @app.route("/c/<cid>/s/<sid>")
            def legacy(cid, sid, body):
                return cid
            """,
        "budget.py": """\
            def expand(posts, radius, max_posts, max_bytes):
                return posts

            def window(posts, radius, max_posts, max_bytes):
                return posts

            def trim(posts, radius, max_posts, max_bytes):
                return posts
            """,
    })
    found = run_detector("find_parameter_objects.py", tmp_path)
    assert [(Path(r["file"]).name, r["description"].split(" appear")[0]) for r in found] == [
        ("budget.py", "Parameters ['max_bytes', 'max_posts', 'posts', 'radius']")]


# --------------------------------------------------------------------------- #
# find_duplicates: the rule of three
# --------------------------------------------------------------------------- #

DUPLICATED_BODY = (
    "    total = 0\n"
    "    for item in items:\n"
    "        total += item.price * item.quantity\n"
    "    discount = total * rate\n"
    "    return total - discount\n"
)


def test_two_identical_blocks_are_a_candidate(tmp_path):
    write(tmp_path, {"one.py": "def first(items, rate):\n" + DUPLICATED_BODY,
                     "two.py": "def second(items, rate):\n" + DUPLICATED_BODY})
    found = run_detector("find_duplicates.py", tmp_path)
    assert len(found) == 1
    assert_candidate_shape(found[0])
    assert "rule of three" in " ".join(found[0]["also_caused_by"])


def test_three_identical_blocks_are_a_finding(tmp_path):
    write(tmp_path, {"one.py": "def first(items, rate):\n" + DUPLICATED_BODY,
                     "two.py": "def second(items, rate):\n" + DUPLICATED_BODY,
                     "three.py": "def third(items, rate):\n" + DUPLICATED_BODY})
    found = run_detector("find_duplicates.py", tmp_path)
    assert [(r["kind"], r["severity"], len(r["occurrences"])) for r in found] == [("finding", "high", 3)]
    assert found[0]["suggestion"]


# --------------------------------------------------------------------------- #
# find_dead_code
# --------------------------------------------------------------------------- #


def _dead(found: list[dict], kind: str) -> list[tuple[str, str]]:
    return [(Path(r["file"]).name, r["name"]) for r in found
            if r["issue_type"] == kind and r["kind"] == "finding"]


def test_a_package_init_reexports_what_it_imports(tmp_path):
    write(tmp_path, {
        "pkg/__init__.py": """\
            import json
            from . import atomic, overlay
            from .tracker import fields as _tracker_fields
            """,
        "pkg/atomic.py": "X = 1\n", "pkg/overlay.py": "Y = 1\n",
        "pkg/tracker/__init__.py": "", "pkg/tracker/fields.py": "Z = 1\n",
        "pkg/plain.py": "import json\nfrom . import atomic\n",
    })
    found = run_detector("find_dead_code.py", tmp_path)
    # A regular module's unused imports are still provably unused.
    assert sorted(_dead(found, "unused_import")) == [("plain.py", "atomic"), ("plain.py", "json")]
    leads = [r for r in found if r["issue_type"] == "unused_import" and r["kind"] == "candidate"]
    assert [(Path(r["file"]).name, r["name"]) for r in leads] == [("__init__.py", "json")]
    assert_candidate_shape(leads[0])


def test_a_name_used_only_in_a_string_annotation_is_used(tmp_path):
    write(tmp_path, {"sample.py": """\
        from typing import TYPE_CHECKING, cast

        if TYPE_CHECKING:
            from models import Order, Invoice, Receipt, Unused

        def total(order: "Order") -> "list[Invoice]":
            receipt = cast("Receipt", order)
            return [receipt]
        """})
    assert _dead(run_detector("find_dead_code.py", tmp_path), "unused_import") == [("sample.py", "Unused")]


def test_a_yield_that_makes_a_generator_is_not_unreachable(tmp_path):
    write(tmp_path, {"sample.py": """\
        async def failing_stream():
            raise ConnectionError("down")
            yield  # unreachable; it is what makes this an async generator

        def empty():
            return
            yield

        def dead_after_yield(items):
            for item in items:
                yield item
            return
            yield None

        def leftover():
            raise ValueError("no")
            cleanup()
        """})
    found = run_detector("find_dead_code.py", tmp_path)
    assert [r["line"] for r in found if r["issue_type"] == "unreachable_code"] == [13, 17]


def test_a_function_handed_out_by_another_module_is_a_candidate(tmp_path):
    write(tmp_path, {
        "producers/__init__.py": "",
        "producers/art.py": """\
            def projection_texts(path, data, instance):
                return [data]

            def describe(path, data, verbose):
                return data
            """,
        "registry.py": """\
            \"\"\"Projections; `art.describe` is called, not registered.\"\"\"
            from producers import art as art_producer

            PROJECTIONS = {"art": art_producer.projection_texts}
            print(art_producer.describe("p", b"d", True))
            """,
        "unrelated.py": "def lease(source):\n    return source.describe, source.verbose\n",
    })
    found = run_detector("find_dead_code.py", tmp_path)
    params = {(r["name"], r["kind"]) for r in found if r["issue_type"] == "unused_parameter"}
    # Handed to a registry (through an import alias): the registry calls it
    # with its own signature. Called directly, mentioned in a docstring, or
    # sharing its name with an unrelated object's attribute: the unused
    # parameter is still provably unused.
    assert params == {("path", "candidate"), ("instance", "candidate"),
                      ("path", "finding"), ("verbose", "finding")}


# --------------------------------------------------------------------------- #
# find_coupling_issues
# --------------------------------------------------------------------------- #


def test_reading_a_module_or_a_loop_variable_is_not_feature_envy(tmp_path):
    write(tmp_path, {"sample.py": """\
        import ast
        import httpx

        class Visitor:
            def names(self, tree):
                return ast.walk(tree), ast.Name, ast.Attribute, ast.Call

            def client(self):
                return httpx.AsyncClient(timeout=httpx.Timeout(5), limits=httpx.Limits())

            def rows(self, turn):
                return [{"id": c.id, "name": c.name, "args": c.args} for c in turn.calls]

            async def cost(self, row):
                return row.get("a"), row.get("b"), row.get("c"), row.get("d")
        """})
    assert of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy") == []


def test_reading_another_objects_state_is_a_feature_envy_lead(tmp_path):
    write(tmp_path, {"sample.py": """\
        class RunView:
            def __init__(self, loop):
                self.loop = loop

            async def take_decision(self, loop):
                if loop.decisions_used < loop.budget and not loop.decisions_capped:
                    return loop.decisions_used + 1
        """})
    found = of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy")
    assert [r["name"] for r in found] == ["RunView.take_decision"]
    assert_candidate_shape(found[0])


def _disjoint_methods(count: int) -> str:
    return "".join(f"    def use_{n}(self):\n        return self.field_{n}\n\n" for n in range(count))


def _init(count: int) -> str:
    return "    def __init__(self):\n" + "".join(
        f"        self.field_{n} = {n}\n" for n in range(count)) + "\n"


def test_interfaces_value_objects_and_stateless_hooks_are_not_low_cohesion(tmp_path):
    write(tmp_path, {"sample.py": (
        "from abc import ABC\nfrom dataclasses import dataclass\nfrom typing import Protocol\n\n"
        "class Provider(ABC):\n" + _disjoint_methods(6) + "\n"
        "class View(Protocol):\n" + _disjoint_methods(6) + "\n"
        "@dataclass(frozen=True)\nclass Target:\n" + "".join(f"    field_{n}: int = 0\n" for n in range(6)) + "\n" + _disjoint_methods(6) + "\n"
        "class Hook:\n" + "".join(f"    def step_{n}(self, row):\n        return row + {n}\n\n"
                                  for n in range(6))
    )})
    assert of_type(run_detector("find_coupling_issues.py", tmp_path), "low_cohesion") == []


def test_low_cohesion_counts_only_methods_that_use_state(tmp_path):
    # Six disjoint stateful methods are low cohesion; static methods,
    # classmethods, dunders and stubs beside them do not change the verdict
    # either way, and async methods count like any other.
    extras = (
        "    @staticmethod\n    def helper(x):\n        return x\n\n"
        "    @classmethod\n    def build(cls):\n        return cls()\n\n"
        "    def __repr__(self):\n        return 'Grab'\n\n"
        "    async def fetch(self):\n        return self.field_6\n\n"
    )
    write(tmp_path, {"grab.py": "class GrabBag:\n" + _init(7) + _disjoint_methods(6) + extras,
                     "sub.py": "class Gregorian(CalendarProvider):\n" + _init(7) + _disjoint_methods(6)})
    found = of_type(run_detector("find_coupling_issues.py", tmp_path), "low_cohesion")
    by_file = {Path(r["file"]).name: r for r in found}
    assert by_file["grab.py"]["kind"] == "finding"
    assert "7 stateful methods" in by_file["grab.py"]["description"]
    # A subclass's methods may be the base's interface: a lead, not a verdict.
    assert_candidate_shape(by_file["sub.py"])


# --------------------------------------------------------------------------- #
# find_design_smells
# --------------------------------------------------------------------------- #


def _ladder(subject: str, branches: int, indent: str = "    ") -> str:
    return "".join(f"{indent}{'if' if n == 0 else 'elif'} {subject} == '{chr(97 + n)}':\n"
                   f"{indent}    return {n}\n" for n in range(branches))


def test_one_short_switch_is_a_lead_and_a_repeated_or_long_one_a_finding(tmp_path):
    write(tmp_path, {
        "single.py": "def build(kind):\n" + _ladder("kind", 4) + "    return None\n",
        "long.py": "def apply(op):\n" + _ladder("op", 7) + "    return None\n",
        "repeated.py": ("def first(operation):\n" + _ladder("operation", 4) + "    return None\n\n"
                        "def second(operation):\n" + _ladder("operation", 4) + "    return None\n"),
    })
    found = of_type(run_detector("find_design_smells.py", tmp_path), "type_switch")
    by_file: dict[str, list[str]] = {}
    for record in found:
        by_file.setdefault(Path(record["file"]).name, []).append(record["kind"])
    assert by_file == {"single.py": ["candidate"], "long.py": ["finding"],
                       "repeated.py": ["finding", "finding"]}
    assert_candidate_shape(next(r for r in found if r["kind"] == "candidate"))


def test_a_matching_local_private_name_keeps_unknown_ownership_unscored(tmp_path):
    write(tmp_path, {"loop.py": """\
        class _Loop:
            def __init__(self):
                self._trace = []

            def _usable(self):
                return True


        class RunView:
            def take(self, loop, request):
                if request._usable():
                    return loop._trace
                return request._foreign
        """})
    found = of_type(run_detector("find_design_smells.py", tmp_path), "inappropriate_intimacy")
    assert [r["description"].split("'")[1] for r in found if r["kind"] == "finding"] == ["request._foreign"]
    # A matching name is a possible local owner, not proof that every receiver
    # has that owner. Preserve these as leads without charging healthy access.
    candidates = [r for r in found if r["kind"] == "candidate"]
    assert sorted(r["description"].split("'")[1] for r in candidates) == ["loop._trace", "request._usable"]
    for record in candidates:
        assert_candidate_shape(record)


def test_a_sibling_modules_private_is_a_lead_and_a_stdlib_one_is_not(tmp_path):
    write(tmp_path, {"pkg/__init__.py": "", "pkg/sync.py": """\
        import os
        import sys
        from . import overlay

        def strict(view):
            frame = sys._getframe(0)
            if frame is None:
                os._exit(1)
            return overlay._strict_ledger(view)

        def local(view):
            from .. import characters
            return characters._card_path(view)
        """})
    found = of_type(run_detector("find_design_smells.py", tmp_path), "inappropriate_intimacy")
    # Whether `_` means module- or package-internal is the package's
    # convention, which one file cannot see: a lead, not a defect.
    assert sorted(r["description"].split("'")[1] for r in found) == [
        "characters._card_path", "overlay._strict_ledger"]
    for record in found:
        assert_candidate_shape(record)


# --------------------------------------------------------------------------- #
# find_pattern_issues: try_finally_close
# --------------------------------------------------------------------------- #


def test_a_conditional_acquire_has_no_with_form(tmp_path):
    write(tmp_path, {"sample.py": """\
        def drain(lock, no_wait, work):
            if not lock.acquire(blocking=not no_wait):
                return False
            session = None
            try:
                return work()
            finally:
                lock.release()

        def bounded(lock, work):
            if lock.acquire(timeout=5):
                try:
                    return work()
                finally:
                    lock.release()

        def blocking(lane, work):
            lane.acquire()
            try:
                return work()
            finally:
                lane.release()
        """})
    found = of_type(run_detector("find_pattern_issues.py", tmp_path), "try_finally_close")
    # A blocking acquire is the case `with lane:` replaces — still a lead.
    assert [r["line"] for r in found] == [19]
    assert_candidate_shape(found[0])


def test_a_close_in_finally_is_a_lead_naming_its_with_form(tmp_path):
    write(tmp_path, {"sample.py": """\
        import sqlite3

        def count(path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute("select 1").fetchone()
            finally:
                conn.close()
        """})
    found = of_type(run_detector("find_pattern_issues.py", tmp_path), "try_finally_close")
    assert len(found) == 1
    assert_candidate_shape(found[0])
    assert "contextlib.closing()" in found[0]["description"]
    assert any("sqlite3" in reason for reason in found[0]["also_caused_by"])


# --------------------------------------------------------------------------- #
# find_loop_simplifications: loop_to_comprehension
# --------------------------------------------------------------------------- #


def test_a_loop_that_reads_the_list_it_builds_is_not_a_comprehension(tmp_path):
    write(tmp_path, {"sample.py": """\
        def dedupe(keys):
            off = []
            for key in keys:
                if key and key not in off:
                    off.append(key)
            return off

        def doubled(items):
            out = []
            for item in items:
                out.append(item * 2)
            return out
        """})
    found = of_type(run_detector("find_loop_simplifications.py", tmp_path), "loop_to_comprehension")
    assert [r["line"] for r in found] == [10]
    assert "out = [" in found[0]["suggestion"]


def test_a_list_that_already_holds_items_is_extended_not_replaced(tmp_path):
    write(tmp_path, {"sample.py": """\
        def gather(groups, extra, seed):
            out = [seed]
            for item in extra:
                out.append(item)
            names = []
            for group in groups:
                for member in group:
                    names.append(member.name)
            for item in extra:
                seed.append(item)
            return out, names
        """})
    found = of_type(run_detector("find_loop_simplifications.py", tmp_path), "loop_to_comprehension")
    assert [(r["line"], ".extend(" in r["suggestion"]) for r in found] == [
        (3, True), (7, True), (9, True)]


# --------------------------------------------------------------------------- #
# find_local_imports
# --------------------------------------------------------------------------- #


def test_an_import_after_a_path_bootstrap_is_at_the_top(tmp_path):
    write(tmp_path, {
        "script.py": """\
            import os
            import sys
            from pathlib import Path

            REPO = Path(__file__).resolve().parents[1]
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            os.environ["APP_HOME"] = "/tmp/app"

            import app
            """,
        "late.py": """\
            import os

            def helper():
                return os.sep

            import json
            """,
    })
    found = of_type(run_detector("find_local_imports.py", tmp_path), "import_not_at_top")
    assert [(Path(r["file"]).name, r["line"], r["kind"]) for r in found] == [("late.py", 6, "finding")]


def test_a_reasoned_e402_suppression_is_a_lead(tmp_path):
    write(tmp_path, {"sample.py": """\
        import os

        def helper():
            return os.sep

        import dataclasses  # noqa: E402 - deliberate late import, after the fixtures
        import json  # noqa: E402
        """})
    found = of_type(run_detector("find_local_imports.py", tmp_path), "import_not_at_top")
    # A bare suppression says nothing about why.
    assert sorted((r["line"], r["kind"]) for r in found) == [(6, "candidate"), (7, "finding")]
    lead = next(r for r in found if r["kind"] == "candidate")
    assert_candidate_shape(lead)
    assert "deliberate late import" in lead["also_caused_by"][0]


def test_a_documented_import_order_is_a_lead(tmp_path):
    write(tmp_path, {"entry.py": '''\
        """Process entrypoint.

        Ordering matters: the socket is bound before anything of uvicorn or
        webapp is imported. The reply is json, and nothing about the order
        in which the helpers below are defined or imported matters to it.
        """
        import socket


        def start():
            sock = socket.socket()
            import uvicorn
            from webapp.main import app
            import json
            return sock, uvicorn, app, json
        '''})
    found = of_type(run_detector("find_local_imports.py", tmp_path), "local_import")
    assert sorted((r["line"], r["kind"]) for r in found) == [
        (12, "candidate"), (13, "candidate"), (14, "finding")]


# --------------------------------------------------------------------------- #
# find_boolean_params
# --------------------------------------------------------------------------- #


def test_keyword_only_flags_are_a_lead_and_positional_ones_a_finding(tmp_path):
    write(tmp_path, {"sample.py": """\
        def plan(query, *, force=False, final_check=False):
            return query

        def render(data, plain=True, compact=False):
            return data
        """})
    found = of_type(run_detector("find_boolean_params.py", tmp_path), "multiple_boolean_flags")
    by_line = {r["line"]: r for r in found}
    assert_candidate_shape(by_line[1])
    assert by_line[4]["kind"] == "finding"


def test_a_route_handlers_flags_are_bound_by_name(tmp_path):
    write(tmp_path, {"routes.py": """\
        @router.get("/scenes/{sid}")
        def read_scene(sid: str, raw: bool = False, full: bool = False):
            return sid

        def helper(sid, raw=False):
            return sid
        """})
    found = run_detector("find_boolean_params.py", tmp_path)
    assert [(r["smell_type"], r["line"]) for r in of_type(found, "boolean_positional_param")] == [
        ("boolean_positional_param", 5)]
    assert all(r["kind"] == "candidate" for r in of_type(found, "multiple_boolean_flags"))


# --------------------------------------------------------------------------- #
# find_comment_smells: commented_out_code
# --------------------------------------------------------------------------- #


def test_a_trailing_comment_annotates_code_and_is_not_dead_code(tmp_path):
    write(tmp_path, {"sample.py": """\
        def sync(overlay, cid, node):
            overlay.update(cid, body="mine")  # diverge (materializes)
            if node.level == 1 and node.module:  # from .llm import x
                return cid  # secrecy=None
            # result = compute(cid)
            # return result.value
            return None
        """})
    found = of_type(run_detector("find_comment_smells.py", tmp_path), "commented_out_code")
    assert sorted((r["line"], r["kind"]) for r in found) == [(5, "finding"), (6, "finding")]


def test_a_lone_commented_import_is_a_lead(tmp_path):
    write(tmp_path, {"sample.py": """\
        def resolve(alias, tail):
            if tail:
                # from .appearances import cast as appearances_cast
                return alias
            # import pdb
            # pdb.set_trace()
            return None
        """})
    found = of_type(run_detector("find_comment_smells.py", tmp_path), "commented_out_code")
    assert sorted((r["line"], r["kind"]) for r in found) == [
        (3, "candidate"), (5, "finding"), (6, "finding")]
    assert_candidate_shape(next(r for r in found if r["kind"] == "candidate"))


def test_a_test_module_is_not_a_god_module_for_having_many_tests(tmp_path):
    many = "".join(f"def test_case_{i}():\n    assert {i} == {i}\n\n" for i in range(45))
    write(tmp_path, {"tests/test_many.py": many, "pkg/many.py": many.replace("test_case_", "helper_")})
    found = [r for r in run_detector("find_import_cycles.py", tmp_path) if r["smell_type"] == "god_module"]
    assert [r["file"].replace(os.sep, "/").rsplit("/", 2)[-2:] for r in found] == [["pkg", "many.py"]]
