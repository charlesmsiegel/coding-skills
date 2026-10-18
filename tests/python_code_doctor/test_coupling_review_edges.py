"""Receiver identity and extracted type evidence share lexical binding ownership."""

import ast
import math
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_coupling_issues")


def findings(detector, tmp_path, source, kind):
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    return [row for row in detector.coupling_issues(ast.parse(source), str(sample))
            if row.issue_type == kind]


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "original = self\nself = original",
    "original = self\nalias = original\nself = alias",
    "original = self\ndef restore():\n    nonlocal self\n    self = original\nrestore()",
    "original, unused = self, None\nself = original",
])
def test_grounded_alias_restore_keeps_representation_edge(detector, tmp_path, builtin, binding):
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
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    [record] = findings(detector, tmp_path, source, "low_cohesion")
    assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "original = replacement\nself = original",
    "original = self\noriginal = replacement\nself = original",
    "original = self\nfor original in self:\n    pass\nself = original",
    "original, unused = self\nself = original",
    "original = alias\nalias = original\nself = alias",
    "original: object\nself = original",
])
def test_derived_foreign_extracted_or_ungrounded_alias_is_not_identity(
        detector, tmp_path, builtin, binding):
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
    assert findings(detector, tmp_path, source, "low_cohesion") == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_child_alias_cannot_join_the_original_payload_group(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.child
            def stored(self):
                return self.payload
            def save(self):
                original = self.child
                self = original
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = findings(detector, tmp_path, source, "low_cohesion")
    assert record.metric_value == 2


@pytest.mark.parametrize("annotation,pattern", [
    ("list[Payload]", "[item]"),
    ("'list[Payload]'", "[item]"),
    ("tuple[Payload, ...]", "[item]"),
    ("dict[str, Payload]", "{'key': item}"),
    ("list[Payload]", "[(_ as item)]"),
])
@pytest.mark.parametrize("external", [False, True])
def test_simple_pattern_extraction_preserves_annotated_element_type(
        detector, tmp_path, annotation, pattern, external):
    declaration = "from external import Payload" if external else "class Payload: pass"
    source = declaration + textwrap.dedent(f"""

        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: {annotation}):
                match values:
                    case {pattern}:
                        return item.first + item.second + item.third
    """)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external


@pytest.mark.parametrize("annotation,pattern", [
    ("Payload", "Payload(item)"),
    ("list[Payload]", "[[item]]"),
    ("list[Payload]", "[Payload(item)]"),
    ("dict[str, Payload]", "{'key': {'nested': item}}"),
])
def test_external_container_does_not_prove_arbitrary_nested_or_class_fields(
        detector, tmp_path, annotation, pattern):
    source = "from external import Payload\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: {annotation}):
                match values:
                    case {pattern}:
                        return item.first + item.second + item.third
    """)
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("imported", [False, True])
def test_nonlocal_import_metadata_follows_its_binding_owner(detector, tmp_path, intermediate, imported):
    write = "import math as item" if imported else "item = supplied"
    helper = "def update():\n    nonlocal item\n    " + write + "\nupdate()"
    if intermediate:
        helper = "def relay():\n    nonlocal item\n" + textwrap.indent(helper, "    ") + "\nrelay()"
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied):
                item = self.payload
                HELPER
                return item.pi + item.e + item.tau
    """).replace("        HELPER", textwrap.indent(helper, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = object()
    supplied = type("Payload", (), {"pi": 1, "e": 2, "tau": 3})()
    actual = entry.save(supplied)
    if imported:
        assert actual == math.pi + math.e + math.tau
    else:
        assert actual == 6
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not imported


@pytest.mark.parametrize("nonlocal_write", [False, True])
@pytest.mark.parametrize("write", [
    "import math as item\nitem = supplied",
    "if flag:\n    import math as item\nelse:\n    item = supplied",
])
def test_one_possible_import_cannot_hide_a_foreign_rebinding(detector, tmp_path, nonlocal_write, write):
    body = "def update():\n    nonlocal item\n" + textwrap.indent(write, "    ") + "\nupdate()" if nonlocal_write else write
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied, flag=False):
                item = self.payload
                WRITE
                return item.pi + item.e + item.tau
    """).replace("        WRITE", textwrap.indent(body, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = object()
    supplied = type("Payload", (), {"pi": 1, "e": 2, "tau": 3})()
    assert entry.save(supplied) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_flat_alias_chain_does_not_drop_unrelated_file_findings(detector, tmp_path):
    aliases = "alias0 = self\n" + "\n".join(f"alias{i} = alias{i - 1}" for i in range(1, 500))
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                ALIASES
                self = alias499
                return str(self)
            def __str__(self):
                return self.payload
            def deep(self):
                return self.adapter.remote.state.value
    """).replace("        ALIASES", textwrap.indent(aliases, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    assert findings(detector, tmp_path, source, "low_cohesion")
    [record] = findings(detector, tmp_path, source, "message_chain")
    assert "deep" in record.name


@pytest.mark.parametrize("expression,external", [
    ("match values:\n    case [_, item]:\n        return item.numerator + item.denominator + item.real", False),
    ("match values:\n    case [item, _]:\n        return item.numerator + item.denominator + item.real", True),
    ("match values:\n    case [item, *_]:\n        return item.numerator + item.denominator + item.real", True),
    ("match values:\n    case [*_, item]:\n        return item.numerator + item.denominator + item.real", False),
    ("item = values[1]\nreturn item.numerator + item.denominator + item.real", False),
    ("item = values[0]\nreturn item.numerator + item.denominator + item.real", True),
    ("item = values[-1]\nreturn item.numerator + item.denominator + item.real", False),
    ("item = values[-2]\nreturn item.numerator + item.denominator + item.real", True),
])
def test_heterogeneous_tuple_extraction_uses_the_selected_position(detector, tmp_path, expression, external):
    source = textwrap.dedent("""
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: tuple[Fraction, Payload]):
                EXTRACT
    """).replace("        EXTRACT", textwrap.indent(expression, "        "))
    namespace = {}
    exec(source, namespace)
    values = (namespace["Fraction"](1, 2), namespace["Payload"]())
    assert namespace["Entry"]().save(values) == (namespace["Fraction"](7, 2) if external else 6)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external
