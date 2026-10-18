"""Presentation protocols neither create nor hide behavioral LCOM4 groups."""

import ast
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_coupling_issues")


def cohesion(detector, tmp_path, source):
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    return [record for record in detector.coupling_issues(ast.parse(source), str(sample))
            if record.issue_type == "low_cohesion"]


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_presentation_of_an_identity_field_does_not_split_cached_behavior(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Entry:
            def __init__(self, ref, text):
                self.ref = ref
                self.text = text
                self._features = None

            def features(self):
                if self._features is None:
                    self._features = tuple(self.text.split())
                return self._features

            def {presentation}(self):
                return self.ref
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("scope", ["module", "enclosing", "method"])
@pytest.mark.parametrize("pattern", [
    "{'converter': capture}", "[capture]", "[*capture]", "{'key': _, **capture}",
])
def test_pattern_captures_shadow_implicit_representation_builtins(
        detector, tmp_path, builtin, scope, pattern):
    capture = textwrap.dedent(f"""
        match supplied:
            case {pattern.replace('capture', builtin)}:
                pass
    """)
    methods = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    if scope == "module":
        source = capture + methods
    elif scope == "enclosing":
        source = "def build():\n" + textwrap.indent(capture + methods, "    ")
    else:
        source = methods.replace(f"        return {builtin}(self)",
                                 textwrap.indent(capture.strip(), "        ")
                                 + f"\n        return {builtin}(self)")
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("pattern", [
    "{'converter': unrelated}", "[unrelated]", "[*unrelated]", "{'key': _, **unrelated}",
])
def test_unrelated_pattern_capture_does_not_hide_builtin_dispatch(detector, tmp_path, builtin, pattern):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self):
                match supplied:
                    case {pattern}:
                        pass
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "self = replacement",
    "self, unused = replacement, 0",
    "for self in replacements:\n    pass",
    "match replacement:\n    case self:\n        pass",
])
def test_rebound_receiver_cannot_certify_its_original_class_dispatch(
        detector, tmp_path, builtin, binding):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self):
                BINDING
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_rebinding_to_owned_data_does_not_dispatch_to_the_original_representation(
        detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.child
            def payload_size(self):
                return len(self.payload)
            def save(self):
                self = self.child
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure, save} and {payload_size}" in record.description


@pytest.mark.parametrize("setup,read,expected", [
    ("", "Constants", False),
    ("class Constants:\n    first = 4\n    second = 5\n    third = 6", "Constants", False),
    ("Constants = replacement", "Constants", True),
    ("if flag:\n    Constants = replacement", "Constants", True),
    ("Constants = Constants()", "Constants", True),
    ("value = Constants()", "value", True),
])
def test_local_class_namespaces_are_distinct_from_foreign_instances(
        detector, tmp_path, setup, read, expected):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Constants:
                    first = 1
                    second = 2
                    third = 3
                SETUP
                return READ.first + READ.second + READ.third
    """).replace("        SETUP", textwrap.indent(setup, "        ")).replace("READ", read)
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    found = [record for record in detector.coupling_issues(ast.parse(source), str(sample))
             if record.issue_type == "feature_envy"]
    assert bool(found) is expected


@pytest.mark.parametrize("declaration", ["@wrapper\nclass Constants:", "class Constants(Base):",
                                           "class Constants(metaclass=factory):"])
def test_unknown_class_construction_keeps_foreign_receiver_uncertainty(
        detector, tmp_path, declaration):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                DECLARATION
                    first = 1
                    second = 2
                    third = 3
                return Constants.first + Constants.second + Constants.third
    """).replace("        DECLARATION", textwrap.indent(declaration, "        "))
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    found = [record for record in detector.coupling_issues(ast.parse(source), str(sample))
             if record.issue_type == "feature_envy"]
    assert found


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("rebind", [False, True])
@pytest.mark.parametrize("intermediate", [False, True])
def test_nonlocal_receiver_writes_belong_to_the_original_binding(
        detector, tmp_path, builtin, rebind, intermediate):
    helper = "def helper():\n    nonlocal self\n    " + (
        "self = replacement" if rebind else "return self")
    if intermediate:
        helper = "def relay():\n    nonlocal self\n" + textwrap.indent(helper, "    ") + "\n    helper()"
    invoke = "relay()" if intermediate else "helper()"
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                HELPER
                {invoke}
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        HELPER", textwrap.indent(helper, "        "))
    found = cohesion(detector, tmp_path, source)
    if rebind:
        assert found == []
    else:
        [record] = found
        assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_nonlocal_write_to_a_shadowing_parameter_does_not_rebind_the_receiver(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def helper(self):
                    def nested():
                        nonlocal self
                        self = replacement
                    nested()
                helper(other)
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


@pytest.mark.parametrize("rebind", [False, True])
@pytest.mark.parametrize("intermediate", [False, True])
def test_nonlocal_class_rebinding_keeps_foreign_instance_reads(detector, tmp_path, rebind, intermediate):
    helper = "def helper():\n    nonlocal Constants\n    " + (
        "Constants = replacement" if rebind else "return Constants")
    if intermediate:
        helper = "def relay():\n    nonlocal Constants\n" + textwrap.indent(helper, "    ") + "\n    helper()"
    invoke = "relay()" if intermediate else "helper()"
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Constants:
                    first = 1
                    second = 2
                    third = 3
                HELPER
                {invoke}
                return Constants.first + Constants.second + Constants.third
    """).replace("        HELPER", textwrap.indent(helper, "        "))
    found = [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
             if record.issue_type == "feature_envy"]
    assert bool(found) is rebind


def test_nonlocal_write_to_a_shadowing_parameter_preserves_the_class_namespace(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Constants:
                    first = 1
                    second = 2
                    third = 3
                def helper(Constants):
                    def nested():
                        nonlocal Constants
                        Constants = replacement
                    nested()
                helper(other)
                return Constants.first + Constants.second + Constants.third
    """)
    assert not [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
                if record.issue_type == "feature_envy"]


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_an_annotation_only_closure_owns_its_nonlocal_receiver_write(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def relay():
                    self: object
                    def helper():
                        nonlocal self
                        self = replacement
                    helper()
                relay()
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    namespace = {"replacement": object()}
    exec(source, namespace)
    original = namespace["Entry"]()
    original.payload = "original"
    assert original.save() == "original"
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


def test_an_annotation_only_closure_does_not_rebind_the_enclosing_class_namespace(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Constants:
                    first = 1
                    second = 2
                    third = 3
                def relay():
                    Constants: object
                    def helper():
                        nonlocal Constants
                        Constants = replacement
                    helper()
                relay()
                return Constants.first + Constants.second + Constants.third
    """)
    namespace = {"replacement": object()}
    exec(source, namespace)
    assert namespace["Entry"]().save() == 6
    assert not [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
                if record.issue_type == "feature_envy"]


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_an_annotation_on_the_original_receiver_is_not_a_write(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                self: object
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


def test_a_declared_local_initialized_nonlocally_is_not_a_class_namespace(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, replacement):
                item: object
                def initialize():
                    nonlocal item
                    item = replacement
                initialize()
                return item.first + item.second + item.third
    """)
    [record] = [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
                if record.issue_type == "feature_envy"]
    assert record.metric_value == 3


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("pattern,extract", [
    ("{'item': item}", ""),
    ("[item]", ""),
    ("[*items]", "item = items[0]"),
    ("{'unused': _, **items}", "item = items['item']"),
])
def test_pattern_captures_keep_the_matched_subjects_data_ownership(detector, tmp_path, owned, pattern, extract):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.payload
            def render(self, other):
                match {'self.payload' if owned else 'other'}:
                    case {pattern}:
                        EXTRACT
                        return item.first + item.second + item.third
    """).replace("                EXTRACT", "                " + (extract or "pass"))
    item = type("Payload", (), {"first": 1, "second": 2, "third": 3})()
    payload = [item] if pattern.startswith("[") else {"item": item, "unused": None}
    namespace = {}
    exec(source, namespace)
    original = namespace["Entry"]()
    original.payload = payload
    assert original.render(payload) == 6
    found = [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
             if record.issue_type == "feature_envy"]
    assert bool(found) is not owned


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "self = self",
    "self, unused = self, other",
    "self, self = self, self",
    "def helper():\n    nonlocal self\n    self = self\nhelper()",
])
def test_identity_assignments_keep_the_original_receiver_protocol(detector, tmp_path, builtin, binding):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDING
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {"other": object()}
    exec(source, namespace)
    original = namespace["Entry"]()
    original.payload = "original"
    assert original.save() == "original"
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("bindings", ["self = self\nself = replacement", "self = replacement\nself = self"])
def test_one_foreign_assignment_still_declines_the_original_receiver_protocol(
        detector, tmp_path, builtin, bindings):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDINGS
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDINGS", textwrap.indent(bindings, "        "))
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding,elements", [
    ("for self in self:\n    pass", 2),
    ("self, unused = self", 2),
    ("self, *rest = self", 2),
    ("[self] = self", 1),
    ("def helper():\n    nonlocal self\n    self, unused = self\nhelper()", 2),
])
def test_element_extraction_from_the_receiver_is_not_identity(detector, tmp_path, builtin, binding, elements):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDING
                return {builtin}(self)
            def __iter__(self):
                return iter((replacement,) * {elements})
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {"replacement": object()}
    exec(source, namespace)
    original = namespace["Entry"]()
    original.payload = "original"
    assert original.save() != "original"
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("subject,pattern,original", [
    ("self", "self", True),
    ("self", "_ as self", True),
    ("self", "Entry(_) as self", True),
    ("self", "(Entry() as self) | (None as self)", True),
    ("replacement", "_ as self", False),
    ("self", "Entry(self)", False),
    ("self", "Entry(child=(_ as self))", False),
])
def test_whole_pattern_capture_preserves_receiver_identity_but_extraction_does_not(
        detector, tmp_path, builtin, subject, pattern, original):
    source = textwrap.dedent(f"""
        class Entry:
            __match_args__ = ('child',)
            def measure(self):
                return self.metrics
            def save(self):
                match {subject}:
                    case {pattern}:
                        pass
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    namespace = {"replacement": object()}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    entry.child = namespace["replacement"]
    assert (entry.save() == "original") is original
    found = cohesion(detector, tmp_path, source)
    if original:
        [record] = found
        assert record.metric_value == 2
    else:
        assert found == []


@pytest.mark.parametrize("binding,rebound", [
    ("[(Constants := replacement) for _ in [None]]", True),
    ("{(Constants := replacement) for _ in [None]}", True),
    ("{0: (Constants := replacement) for _ in [None]}", True),
    ("tuple((Constants := replacement) for _ in [None])", True),
    ("[[(Constants := replacement) for _ in [None]] for _ in [None]]", True),
    ("def helper():\n    nonlocal Constants\n    [(Constants := replacement) for _ in [None]]\nhelper()", True),
    ("[(lambda: (Constants := replacement))() for _ in [None]]", False),
    ("[Constants for Constants in [replacement]]", False),
    ("[(unrelated := replacement) for _ in [None]]", False),
])
def test_comprehension_walrus_writes_to_its_lexical_owner_not_the_iteration_scope(
        detector, tmp_path, binding, rebound):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, replacement):
                class Constants:
                    first = 1
                    second = 2
                    third = 3
                BINDING
                return Constants.first + Constants.second + Constants.third
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    replacement = type("Payload", (), {"first": 4, "second": 5, "third": 6})()
    assert namespace["Entry"]().save(replacement) == (15 if rebound else 6)
    found = [record for record in detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
             if record.issue_type == "feature_envy"]
    assert bool(found) is rebound


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding,rebound", [
    ("[(self := replacement) for _ in [None]]", True),
    ("[(lambda: (self := replacement))() for _ in [None]]", False),
    ("[self for self in [replacement]]", False),
    ("[(self := self) for _ in [None]]", False),
])
def test_comprehension_binding_ownership_also_controls_original_receiver_dispatch(
        detector, tmp_path, builtin, binding, rebound):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, replacement):
                BINDING
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert (entry.save(object()) != "original") is rebound
    found = cohesion(detector, tmp_path, source)
    if rebound:
        assert found == []
    else:
        [record] = found
        assert record.metric_value == 2


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
@pytest.mark.parametrize("render", [
    "str((self.left_value, self.right_value))",
    "str((self.left(), self.right()))",
])
def test_presentation_cannot_bridge_two_real_responsibilities(
        detector, tmp_path, presentation, render):
    source = textwrap.dedent(f"""
        class Split:
            def __init__(self):
                self.left_value = 'left'
                self.right_value = 'right'

            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def {presentation}(self):
                return {render}
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.kind == "finding" and record.metric_value == 2
    assert "2 stateful methods" in record.description
    assert "{left} and {right}" in record.description


def test_presentation_methods_cannot_bridge_through_each_other(detector, tmp_path):
    source = textwrap.dedent("""
        class Split:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def __repr__(self):
                return str(self.left())

            def __str__(self):
                return self.__repr__() + str(self.right())
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2 and "{left} and {right}" in record.description


def test_two_presentation_views_alone_are_not_separate_responsibilities(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def __repr__(self):
                return self.ref

            def __str__(self):
                return self.title
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("protocol,result", [
    ("__len__", "len(self.secondary)"),
    ("__iter__", "iter(self.secondary)"),
    ("__call__", "self.secondary"),
])
def test_other_protocols_still_form_real_behavioral_groups(detector, tmp_path, protocol, result):
    source = textwrap.dedent(f"""
        class Split:
            def count_primary(self):
                return len(self.primary)

            def {protocol}(self):
                return {result}
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{count_primary} and {" + protocol + "}" in record.description


@pytest.mark.parametrize("protocol,result", [
    ("__len__", "len(self.left() + self.right())"),
    ("__iter__", "iter(self.left() + self.right())"),
    ("__call__", "self.left() + self.right()"),
])
def test_other_protocols_can_connect_behavior_they_coordinate(detector, tmp_path, protocol, result):
    source = textwrap.dedent(f"""
        class Joined:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def {protocol}(self):
                return {result}
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_representation_still_participates_in_other_coupling_checks(detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class View:
            def label(self):
                return self.ref

            def {presentation}(self):
                return str(self.adapter.remote.state.value)
    """)
    found = detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
    assert any(record.issue_type == "message_chain" for record in found)


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_behavior_reaching_separate_state_through_presentation_still_counts(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Split:
            def measure(self):
                return len(self.metrics)

            def save(self):
                return self.{presentation}()

            def {presentation}(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_behavior_reaching_shared_state_through_presentation_stays_connected(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.payload)

            def save(self):
                return self.{presentation}()

            def {presentation}(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_stateless_presentation_does_not_make_its_caller_stateful(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return len(self.payload)

            def label(self):
                return self.__str__()

            def __str__(self):
                return 'Entry'
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_presentation_cycle_preserves_its_callers_reachable_state(detector, tmp_path):
    source = textwrap.dedent("""
        class Split:
            def measure(self):
                return len(self.metrics)

            def save(self):
                return self.__str__()

            def __str__(self):
                return self.__repr__()

            def __repr__(self):
                return self.payload + self.__str__()
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


def test_behavior_coordinating_groups_through_a_representation_can_join_them(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def save(self):
                return self.__repr__()

            def __repr__(self):
                return str((self.left(), self.right()))
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin,protocol", [("str", "__str__"), ("repr", "__repr__")])
def test_builtin_representation_dispatch_preserves_behavioral_state(detector, tmp_path, builtin, protocol):
    source = textwrap.dedent(f"""
        class Split:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return {builtin}(self)
            def {protocol}(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


@pytest.mark.parametrize("builtin,protocol", [("str", "__str__"), ("repr", "__repr__")])
@pytest.mark.parametrize("shadow", ["parameter", "module"])
def test_shadowed_representation_names_do_not_invent_state(detector, tmp_path, builtin, protocol, shadow):
    parameter = ", " + builtin if shadow == "parameter" else ""
    prefix = f"{builtin} = lambda obj: 'constant'\n" if shadow == "module" else ""
    source = prefix + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self{parameter}):
                return {builtin}(self)
            def {protocol}(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin,protocol", [("str", "__str__"), ("repr", "__repr__")])
def test_an_unrelated_local_import_does_not_shadow_representation_dispatch(detector, tmp_path, builtin, protocol):
    source = textwrap.dedent(f"""
        def unrelated():
            from builtins import repr as {builtin}

        class Split:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return {builtin}(self)
            def {protocol}(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


@pytest.mark.parametrize("builtin,protocol", [("str", "__str__"), ("repr", "__repr__")])
def test_a_module_import_still_shadows_representation_dispatch(detector, tmp_path, builtin, protocol):
    source = textwrap.dedent(f"""
        from external import render as {builtin}

        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return {builtin}(self)
            def {protocol}(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_builtin_str_fallback_to_repr_preserves_behavioral_state(detector, tmp_path):
    source = textwrap.dedent("""
        class Split:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return str(self)
            def __repr__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


def test_inherited_str_is_not_assumed_to_dispatch_to_local_repr(detector, tmp_path):
    source = textwrap.dedent("""
        class Base:
            def __str__(self):
                return str(self.metrics)

        class Entry(Base):
            def measure(self):
                return len(self.metrics)
            def save(self):
                return str(self)
            def __repr__(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin,presentation", [("str", "__str__"), ("repr", "__repr__"), ("str", "__repr__")])
@pytest.mark.parametrize("receiver", ["self", "instance"])
@pytest.mark.parametrize("shared", [False, True])
def test_builtin_representation_dispatch_retains_the_callers_state(
        detector, tmp_path, builtin, presentation, receiver, shared):
    source = textwrap.dedent(f"""
        class Entry:
            def measure({receiver}):
                return len({receiver}.{'payload' if shared else 'metrics'})

            def save({receiver}):
                return {builtin}({receiver})

            def {presentation}({receiver}):
                return {receiver}.payload
    """)
    found = cohesion(detector, tmp_path, source)
    if shared:
        assert found == []
    else:
        [record] = found
        assert record.metric_value == 2
        assert "{measure} and {save}" in record.description


@pytest.mark.parametrize("setup,signature,expression", [
    ("str = converter\n", "self", "str(self)"),
    ("def str(value):\n    return 'Entry'\n", "self", "str(self)"),
    ("from converter import str\n", "self", "str(self)"),
    ("if flag:\n    str = converter\n", "self", "str(self)"),
    ("", "self, str", "str(self)"),
    ("", "self, other", "str(other)"),
    ("", "self", "(lambda str: str(self))(converter)"),
    ("", "self", "(lambda self: str(self))(other)"),
    ("", "self", "str(self, 'utf-8')"),
    ("", "self", "repr(self, extra)"),
])
def test_representation_edges_require_builtin_ownership_receiver_and_arity(
        detector, tmp_path, setup, signature, expression):
    source = setup + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.metrics)

            def save({signature}):
                return {expression}

            def __str__(self):
                return self.payload

            def __repr__(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_other_method_and_class_attribute_shadows_do_not_hide_builtin_dispatch(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            str = converter
            def measure(self):
                return len(self.metrics)
            def unrelated(self, str):
                return str
            def save(self):
                return str(self)
            def __str__(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2


def test_enclosing_function_converter_is_not_builtin_dispatch(detector, tmp_path):
    source = textwrap.dedent("""
        def build(str):
            class Entry:
                def measure(self):
                    return len(self.metrics)
                def save(self):
                    return str(self)
                def __str__(self):
                    return self.payload
            return Entry
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_str_uses_an_explicit_str_before_repr_fallback(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return len(self.metrics)
            def save(self):
                return str(self)
            def __str__(self):
                return 'Entry'
            def __repr__(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_classmethod_rendering_the_class_does_not_dispatch_to_instance_repr(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return len(self.metrics)
            @classmethod
            def save(cls):
                return str(cls)
            def __str__(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("scope,binding,shadowed", [
    ("module", "BUILTIN: object", False),
    ("module", "BUILTIN = converter\nBUILTIN: object", True),
    ("module", "BUILTIN: object\nBUILTIN = converter", True),
    ("module", "BUILTIN: object = converter", True),
    ("class", "BUILTIN: object", False),
    ("class", "BUILTIN: object = converter", False),
])
def test_namespace_annotations_do_not_invent_bindings_or_erase_real_assignments(
        detector, tmp_path, builtin, scope, binding, shadowed):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    binding = binding.replace("BUILTIN", builtin)
    if scope == "module":
        source = binding + "\n" + source
    else:
        source = source.replace("class Entry:\n", "class Entry:\n" + textwrap.indent(binding, "    ") + "\n")
    namespace = {"converter": lambda value: "converted"}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == ("converted" if shadowed else "original")
    actual_namespace = namespace if scope == "module" else vars(namespace["Entry"])
    assert (builtin in actual_namespace) is ("=" in binding)
    found = cohesion(detector, tmp_path, source)
    if shadowed:
        assert found == []
    else:
        [record] = found
        assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("scope", ["method", "enclosing", "async-method"])
def test_function_only_annotations_still_own_uninitialized_lexical_bindings(
        detector, tmp_path, builtin, scope):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    if scope == "enclosing":
        source = "def build():\n" + f"    {builtin}: object\n" + textwrap.indent(source, "    ") + "    return Entry\n"
    else:
        source = source.replace("    def save(self):\n", "    def save(self):\n" + f"        {builtin}: object\n")
        if scope == "async-method":
            source = source.replace("    def save(self):", "    async def save(self):")
    namespace = {}
    exec(source, namespace)
    entry_type = namespace["build"]() if scope == "enclosing" else namespace["Entry"]
    entry = entry_type()
    entry.payload = "original"
    with pytest.raises(NameError if scope == "enclosing" else UnboundLocalError):
        if scope == "async-method":
            entry.save().send(None)
        else:
            entry.save()
    assert cohesion(detector, tmp_path, source) == []
