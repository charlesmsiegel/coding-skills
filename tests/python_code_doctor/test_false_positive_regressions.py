"""False positives found by running the doctor over a real repository.

Each test pins one shape that hardy's report charged as a defect when it was
not, beside the shape the same detector must still catch, so a fix cannot
pass by switching the detector off.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def of_type(findings: list[dict], kind: str) -> list[dict]:
    return [f for f in findings if (f.get("smell_type") or f.get("issue_type")) == kind]


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---- find_return_issues ----------------------------------------------------- #

def test_explicit_return_none_beside_a_value_is_consistent(tmp_path):
    """PEP 8 asks for exactly this, and the detector's own fix recommends it."""
    write(tmp_path, "sample.py",
          "def resolve(text, commands):\n"
          "    parts = text.split()\n"
          "    if not parts:\n"
          "        return None\n"
          "    return commands[parts[0]]\n")
    assert not of_type(run_detector("find_return_issues.py", tmp_path), "inconsistent_returns")


def test_a_bare_return_beside_a_value_is_still_inconsistent(tmp_path):
    write(tmp_path, "sample.py",
          "def resolve(text, commands):\n"
          "    if not text:\n"
          "        return\n"
          "    return commands[text]\n")
    assert of_type(run_detector("find_return_issues.py", tmp_path), "inconsistent_returns")


# ---- find_exception_issues -------------------------------------------------- #

def test_catching_baseexception_to_clean_up_and_reraise_is_not_flagged(tmp_path):
    """Cleanup that must also run on Ctrl+C is what `except BaseException` is for."""
    write(tmp_path, "sample.py",
          "import shutil\n"
          "def build(make, root):\n"
          "    try:\n"
          "        return make(root)\n"
          "    except BaseException:\n"
          "        shutil.rmtree(root, ignore_errors=True)\n"
          "        raise\n"
          "def record(run, log):\n"
          "    try:\n"
          "        return run()\n"
          "    except BaseException as error:\n"
          "        log(error)\n"
          "        raise error\n")
    assert not of_type(run_detector("find_exception_issues.py", tmp_path), "catches_baseexception")


def test_catching_baseexception_without_reraising_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py",
          "def run_cell(code, namespace):\n"
          "    try:\n"
          "        exec(code, namespace)\n"
          "    except BaseException:\n"
          "        return 'error'\n"
          "    return 'ok'\n")
    assert of_type(run_detector("find_exception_issues.py", tmp_path), "catches_baseexception")


# ---- find_mutation_hazards -------------------------------------------------- #

def test_pydantic_field_default_is_copied_not_shared(tmp_path):
    write(tmp_path, "models.py",
          "from pydantic import BaseModel\n"
          "class Scoreboard(BaseModel):\n"
          "    host: dict[str, str] = {}\n"
          "    rows: list[int] = []\n")
    assert not of_type(run_detector("find_mutation_hazards.py", tmp_path), "mutable_class_attribute")


def test_pydantic_model_through_a_first_party_base_in_another_module(tmp_path):
    """`class Baseline(FrozenModel)` where FrozenModel subclasses BaseModel elsewhere."""
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/foundation/__init__.py", "")
    write(tmp_path, "src/app/foundation/values.py",
          "from pydantic import BaseModel, ConfigDict\n"
          "class FrozenModel(BaseModel):\n"
          "    model_config = ConfigDict(frozen=True)\n")
    write(tmp_path, "src/app/evals/__init__.py", "")
    write(tmp_path, "src/app/evals/sweep.py",
          "from app.foundation.values import FrozenModel\n"
          "from ..foundation import values\n"
          "class Baseline(FrozenModel):\n"
          "    statement_digests: dict[str, str] = {}\n"
          "class Relative(values.FrozenModel):\n"
          "    tags: list[str] = []\n")
    assert not of_type(run_detector("find_mutation_hazards.py", tmp_path), "mutable_class_attribute")


def test_a_plain_class_attribute_is_still_shared(tmp_path):
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/base.py", "class Base:\n    pass\n")
    write(tmp_path, "src/app/runtime.py",
          "from app.base import Base\n"
          "class Runtime(Base):\n"
          "    enforcement = {'turns': 'sdk'}\n"
          "    environment: dict[str, str] = {}\n")
    found = of_type(run_detector("find_mutation_hazards.py", tmp_path), "mutable_class_attribute")
    assert len(found) == 2


# ---- find_ai_scaffolding ---------------------------------------------------- #

def test_a_word_containing_a_placeholder_token_is_not_a_placeholder(tmp_path):
    """"replacement" contains "replaceme"; "changement" contains "changeme"."""
    write(tmp_path, "sample.py",
          "PROMPT = 'Assess the supplied replacement argument separately.'\n"
          "NOTE = 'Le changement est fait.'\n")
    assert not of_type(run_detector("find_ai_scaffolding.py", tmp_path), "placeholder_value")


def test_a_real_placeholder_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py", "API_KEY = 'replace_me'\nTOKEN = 'changeme'\n")
    assert len(of_type(run_detector("find_ai_scaffolding.py", tmp_path), "placeholder_value")) == 2


def test_an_underscored_kwargs_declares_itself_unused(tmp_path):
    write(tmp_path, "sample.py",
          "def refuse(*_args, **_kwargs):\n"
          "    raise RuntimeError('the toolchain could not be identified')\n")
    assert not of_type(run_detector("find_ai_scaffolding.py", tmp_path), "unused_kwargs")


def test_an_unused_named_kwargs_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py",
          "def configure(name, **options):\n"
          "    return name.upper()\n")
    assert of_type(run_detector("find_ai_scaffolding.py", tmp_path), "unused_kwargs")


# ---- find_coupling_issues --------------------------------------------------- #

def test_modules_classes_and_enums_are_not_envied_objects(tmp_path):
    write(tmp_path, "sample.py",
          "import os\n"
          "from enum import Enum\n"
          "class Kind(Enum):\n"
          "    TEXT = 'text'\n"
          "    MATH = 'math'\n"
          "class Adapter:\n"
          "    def __init__(self, root):\n"
          "        self.root = root\n"
          "    def classify(self, name):\n"
          "        if os.path.isdir(os.path.join(self.root, name)):\n"
          "            return Kind.TEXT if os.sep in name else Kind.MATH\n"
          "        return Kind.MATH if os.path.exists(name) else Kind.TEXT\n"
          "    def other(self):\n"
          "        return self.root\n")
    assert not of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy")


def test_self_is_counted_the_same_way_as_the_envied_object(tmp_path):
    """Distinct attributes on both sides: three of the order's beside three of
    its own is a method working with a collaborator, not envying it. (The old
    rule set every read of the order against *distinct* self attributes.)"""
    write(tmp_path, "sample.py",
          "class Report:\n"
          "    def __init__(self):\n"
          "        self.total, self.count, self.rate = 0, 0, 1\n"
          "    def add(self, order):\n"
          "        self.total += order.subtotal * self.rate + order.tax\n"
          "        self.count += order.items + order.items + order.tax\n"
          "        return self.total\n")
    assert not of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy")


def test_calling_a_collaborator_is_delegation_not_envy(tmp_path):
    write(tmp_path, "sample.py",
          "class Workflow:\n"
          "    def __init__(self, store):\n"
          "        self.store = store\n"
          "    def run(self, request):\n"
          "        request.validate()\n"
          "        request.prepare()\n"
          "        request.submit()\n"
          "        return request.finish()\n")
    assert not of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy")


def test_a_method_named_after_a_builtin_hides_nothing(tmp_path):
    write(tmp_path, "sample.py",
          "class Representations:\n"
          "    def list(self, digest):\n"
          "        return [digest]\n"
          "    def open(self, digest):\n"
          "        return digest\n")
    assert not of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")


def test_a_module_function_named_after_a_builtin_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py", "def list(digest):\n    return [digest]\n")
    assert of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")


def test_feature_envy_on_a_parameter_is_still_flagged_at_the_method(tmp_path):
    write(tmp_path, "sample.py",
          "class Report:\n"
          "    def __init__(self):\n"
          "        self.title = ''\n"
          "\n"
          "    def summarize(self, order):\n"
          "        return order.subtotal + order.tax + order.shipping\n")
    found = of_type(run_detector("find_coupling_issues.py", tmp_path), "feature_envy")
    assert found and found[0]["line"] == 5


def test_a_workflow_that_uses_its_collaborators_is_not_a_middle_man(tmp_path):
    write(tmp_path, "sample.py",
          "import json\n"
          "class Workflow:\n"
          "    def __init__(self, store, policy):\n"
          "        self.store, self.policy = store, policy\n"
          "    def run(self, request):\n"
          "        snapshot = self.store.read()\n"
          "        plan = json.dumps(request.plan)\n"
          "        checked = [item for item in snapshot.items if item.ok]\n"
          "        return request.finish(plan, checked, snapshot.revision, snapshot.when)\n"
          "    def describe(self, request):\n"
          "        return request.name + request.kind + request.scope\n")
    assert not of_type(run_detector("find_coupling_issues.py", tmp_path), "middle_man")


def test_a_class_of_pure_forwarders_is_still_a_middle_man(tmp_path):
    write(tmp_path, "sample.py",
          "class Facade:\n"
          "    def __init__(self, inner):\n"
          "        self.inner = inner\n"
          "    def read(self, key):\n"
          "        return self.inner.read(key)\n"
          "    def write(self, key, value):\n"
          "        return self.inner.write(key, value)\n"
          "    def delete(self, key):\n"
          "        self.inner.delete(key)\n"
          "    def keys(self):\n"
          "        return self.inner.keys()\n")
    assert of_type(run_detector("find_coupling_issues.py", tmp_path), "middle_man")


# ---- find_code_smells ------------------------------------------------------- #

def test_a_long_parameter_list_is_reported_once_across_the_suite(tmp_path):
    """analyze_complexity owns parameter_count; a second detector reporting the
    same count as long_parameter_list charged one signature twice."""
    write(tmp_path, "sample.py",
          "def build(a, b, c, d, e, f, g, h):\n"
          "    return a + b + c + d + e + f + g + h\n")
    assert not of_type(run_detector("find_code_smells.py", tmp_path), "long_parameter_list")
    assert of_type(run_detector("analyze_complexity.py", tmp_path), "parameter_count")


def test_exact_type_identity_is_not_a_type_comparison_smell(tmp_path):
    """`type(x) is int` deliberately rejects bool; isinstance would accept it."""
    write(tmp_path, "sample.py",
          "def positive(value, registry, record):\n"
          "    if type(value) is not int or value < 1:\n"
          "        raise ValueError(value)\n"
          "    if registry.get(type(record).__name__) is not type(record):\n"
          "        raise ValueError(record)\n"
          "    return value\n")
    assert not of_type(run_detector("find_code_smells.py", tmp_path), "type_comparison")


def test_type_equality_is_still_a_type_comparison_smell(tmp_path):
    write(tmp_path, "sample.py", "def is_dict(x):\n    return type(x) == dict\n")
    assert of_type(run_detector("find_code_smells.py", tmp_path), "type_comparison")


def test_numbers_inside_a_named_constant_expression_are_named(tmp_path):
    write(tmp_path, "sample.py",
          "MAX_EDIT_BODY = 4 << 20\n"
          "SESSION_SECONDS = 30 * 60\n"
          "LIMIT: int = -(7 + 3)\n")
    assert not of_type(run_detector("find_code_smells.py", tmp_path), "magic_number")


def test_a_number_in_a_computed_expression_is_still_magic(tmp_path):
    write(tmp_path, "sample.py",
          "def budget(tokens):\n"
          "    reserve = tokens * 7 // 3\n"
          "    return reserve\n")
    assert of_type(run_detector("find_code_smells.py", tmp_path), "magic_number")


# ---- find_naming_issues ----------------------------------------------------- #

def test_a_keyword_only_parameter_may_share_a_builtins_name(tmp_path):
    """The name is the caller's spelling (`print(file=...)`, `sorted(key=...)`)."""
    write(tmp_path, "sample.py",
          "def restore(paths, *, id, type=None):\n"
          "    return paths[id], type\n")
    assert not of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")


def test_a_positional_parameter_shadowing_a_builtin_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py",
          "def restore(paths, id):\n"
          "    return paths[id]\n")
    assert of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")


def test_http_handler_methods_keep_the_frameworks_names(tmp_path):
    write(tmp_path, "sample.py",
          "from http.server import BaseHTTPRequestHandler\n"
          "class Handler(BaseHTTPRequestHandler):\n"
          "    def do_GET(self):\n"
          "        self.send_response(200)\n"
          "    def do_POST(self):\n"
          "        self.send_response(204)\n")
    assert not of_type(run_detector("find_naming_issues.py", tmp_path), "non_snake_case_function")


def test_a_camel_case_function_is_still_flagged(tmp_path):
    write(tmp_path, "sample.py", "def parseValue(text):\n    return int(text)\n")
    assert of_type(run_detector("find_naming_issues.py", tmp_path), "non_snake_case_function")


# ---- find_parameter_objects ------------------------------------------------- #

def test_one_parameter_group_seen_through_subsets_is_one_clump(tmp_path):
    """Five functions pass parts of one statement; some drop `name`, some
    `imports`. Three overlapping triples with three supports are one group."""
    write(tmp_path, "identity.py",
          "def digest(name, binders, conclusion, imports): ...\n"
          "def render(name, binders, conclusion, imports): ...\n"
          "def check(name, binders, conclusion, imports): ...\n"
          "def prove(binders, conclusion, imports): ...\n"
          "def show(name, binders, conclusion): ...\n")
    clumps = of_type(run_detector("find_parameter_objects.py", tmp_path), "data_clump")
    assert len(clumps) == 1
    assert "['binders', 'conclusion', 'imports', 'name']" in clumps[0]["description"]


def test_two_unrelated_groups_stay_two_clumps(tmp_path):
    write(tmp_path, "sample.py",
          "def a(host, port, user): ...\n"
          "def b(host, port, user): ...\n"
          "def c(host, port, user): ...\n"
          "def d(width, height, depth): ...\n"
          "def e(width, height, depth): ...\n"
          "def f(width, height, depth): ...\n")
    assert len(of_type(run_detector("find_parameter_objects.py", tmp_path), "data_clump")) == 2


# ---- find_coupling_issues: LCOM -------------------------------------------- #

def test_a_models_validators_do_not_make_it_incohesive(tmp_path):
    write(tmp_path, "sample.py",
          "from pydantic import BaseModel, model_validator, field_validator\n"
          "class Entry(BaseModel):\n"
          "    a: int\n    b: int\n    c: int\n    d: int\n    e: int\n    f: int\n"
          + "".join(
              f"    @model_validator(mode='after')\n"
              f"    def _rule_{name}(self):\n"
              f"        if self.{name} < 0:\n"
              f"            raise ValueError('{name}')\n"
              f"        return self\n"
              for name in "abcdef"))
    assert not of_type(run_detector("find_coupling_issues.py", tmp_path), "low_cohesion")


def test_ordinary_methods_sharing_nothing_are_still_incohesive(tmp_path):
    write(tmp_path, "sample.py",
          "class Grab:\n"
          + "".join(f"    def m_{name}(self):\n        return self.{name}\n" for name in "abcdef"))
    assert of_type(run_detector("find_coupling_issues.py", tmp_path), "low_cohesion")
