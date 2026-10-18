"""Receiver identity and extracted type evidence share lexical binding ownership."""

import ast
import math
import sys
import textwrap
import types
from fractions import Fraction
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


@pytest.mark.parametrize("declaration,pattern,value,expected,external", [
    ("", "int() as item", "3", 7, True),
    ("from fractions import Fraction", "Fraction() as item", "Fraction(3, 2)", 6.5, True),
    ("import fractions", "fractions.Fraction() as item", "fractions.Fraction(3, 2)", 6.5, True),
    ("from fractions import Fraction", "(Fraction() | int()) as item", "3", 7, True),
    ("from fractions import Fraction", "[Fraction() as item]", "[Fraction(3, 2)]", 6.5, True),
    ("from fractions import Fraction", "{'key': Fraction() as item}", "{'key': Fraction(3, 2)}", 6.5, True),
    ("", "Payload() as item", "Payload()", 6, False),
    ("int = Payload", "int() as item", "Payload()", 6, False),
    ("from fractions import Fraction", "(Fraction() | Payload()) as item", "Payload()", 6, False),
    ("from types import SimpleNamespace", "SimpleNamespace(child=item)",
     "SimpleNamespace(child=Payload())", 6, False),
])
def test_whole_class_pattern_capture_uses_only_its_proven_type(
        detector, tmp_path, declaration, pattern, value, expected, external):
    source = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
    """) + declaration + "\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(eval(value, namespace)) == expected
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external


@pytest.mark.parametrize("shadow", ["parameter", "local"])
def test_shadowed_class_pattern_cannot_prove_an_external_type(detector, tmp_path, shadow):
    source = textwrap.dedent("""
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, Fraction=Payload):
                match value:
                    case Fraction() as item:
                        return item.numerator + item.denominator + item.real
    """)
    if shadow == "local":
        source = source.replace("value, Fraction=Payload", "value").replace(
            "        match value:", "        Fraction = Payload\n        match value:")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_class_capture_followed_by_foreign_write_is_not_external(detector, tmp_path):
    source = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, supplied):
                match value:
                    case int() as item:
                        item = supplied
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(3, namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("binding", [
    "import math as item\nalias = item\nitem = alias",
    "import math as item\nalias = item\nsecond = alias\nitem = second",
    "import math as module\nitem = module",
    "import math as item\nalias = item\ndef restore():\n    nonlocal item\n    item = alias\nrestore()",
    "import math as item\nalias = item\nitem, unused = alias, None",
])
def test_grounded_module_alias_preserves_namespace_exemption(detector, tmp_path, binding):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDING
                return item.pi + item.e + item.tau
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save() == math.pi + math.e + math.tau
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("binding", [
    "import math as item\nalias = supplied\nitem = alias",
    "import math as item\nalias = item\nalias = supplied\nitem = alias",
    "import math as item\nalias = item\nif flag:\n    alias = supplied\nitem = alias",
    "import math as item\nalias: object\ndef replace():\n    nonlocal alias\n    alias = supplied\nreplace()\nitem = alias",
])
def test_possible_foreign_module_alias_write_retains_envy(detector, tmp_path, binding):
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied, flag=True):
                BINDING
                return item.pi + item.e + item.tau
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_foreign_grounded_alias_cycle_does_not_borrow_an_import(detector, tmp_path):
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied):
                import math as item
                alias = supplied
                other = alias
                alias = other
                item = alias
                return item.pi + item.e + item.tau
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern,replacement", [
    ("Fraction", "Fraction() as item", "Payload"),
    ("fractions", "fractions.Fraction() as item", "Namespace"),
])
@pytest.mark.parametrize("binding", [
    "ROOT = REPLACEMENT",
    "if True:\n    ROOT = REPLACEMENT",
    "for ROOT in (REPLACEMENT,):\n    pass",
    "with nullcontext(REPLACEMENT) as ROOT:\n    pass",
])
def test_class_pattern_module_rebinding_retains_foreign_type(
        detector, tmp_path, root, pattern, replacement, binding):
    source = textwrap.dedent("""
        import fractions
        from fractions import Fraction
        from contextlib import nullcontext
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Namespace:
            Fraction = Payload
    """) + binding.replace("ROOT", root).replace("REPLACEMENT", replacement) + "\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern,replacement", [
    ("Fraction", "Fraction() as item", "Payload"),
    ("fractions", "fractions.Fraction() as item", "Namespace"),
])
@pytest.mark.parametrize("binding", ["parameter", "local"])
def test_class_pattern_enclosing_binding_overrides_module_import(
        detector, tmp_path, root, pattern, replacement, binding):
    source = textwrap.dedent(f"""
        import fractions
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Namespace:
            Fraction = Payload
        def make({root}={replacement}):
            class Entry:
                def measure(self):
                    return self.metrics
                def save(self, value):
                    match value:
                        case {pattern}:
                            return item.numerator + item.denominator + item.real
            return Entry
        Entry = make()
    """)
    if binding == "local":
        source = source.replace(f"def make({root}={replacement}):", f"def make():\n    {root} = {replacement}")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern", [
    ("Fraction", "Fraction() as item"),
    ("fractions", "fractions.Fraction() as item"),
])
@pytest.mark.parametrize("namespace_only", [False, True])
def test_class_pattern_unshadowed_import_ignores_class_namespace(
        detector, tmp_path, root, pattern, namespace_only):
    source = textwrap.dedent(f"""
        import fractions
        from fractions import Fraction
        class Entry:
            {root} = object()
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    if not namespace_only:
        source = source.replace(f"    {root} = object()\n", "")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Fraction"](3, 2)) == 6.5
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("scope", ["method", "enclosing"])
@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("binding", ["external", "rebound", "first_party"])
def test_scoped_class_pattern_imports_use_the_nearest_proven_binding(
        detector, tmp_path, monkeypatch, scope, qualified, binding):
    payload = "class Payload:\n    numerator = 1\n    denominator = 2\n    real = 3\n"
    local = tmp_path / "cohesion_local_types.py"
    local.write_text(payload, encoding="utf-8")
    module = types.ModuleType("cohesion_local_types")
    exec(payload, module.__dict__)
    monkeypatch.setitem(sys.modules, "cohesion_local_types", module)
    package = "cohesion_local_types" if binding == "first_party" else "fractions"
    kind = "Payload" if binding == "first_party" else "Fraction"
    root = "lib" if qualified else "Kind"
    declaration = f"import {package} as lib" if qualified else f"from {package} import {kind} as Kind"
    pattern = f"lib.{kind}()" if qualified else "Kind()"
    if binding == "rebound":
        declaration += f"\n{root} = " + ("Namespace" if qualified else "Payload")
    body = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                BINDING
                match value:
                    case {pattern} as item:
                        return item.numerator + item.denominator + item.real
    """)
    if scope == "method":
        body = body.replace("        BINDING", textwrap.indent(declaration, "        "))
    else:
        body = ("def make():\n" + textwrap.indent(declaration, "    ") + "\n"
                + textwrap.indent(body.replace("        BINDING\n", ""), "    ")
                + "    return Entry\nEntry = make()\n")
    source = payload + "class Namespace:\n    Fraction = Payload\n" + body
    namespace = {}
    exec(source, namespace)
    value = module.Payload() if binding == "first_party" else namespace["Payload"]()
    if binding == "external":
        value = Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6.5 if binding == "external" else 6)
    found = findings(detector, tmp_path, source, "feature_envy")
    # A method-local replacement also reads Namespace.Fraction: four distinct
    # foreign attributes, rather than only the matched value's three.
    metric = 4 if binding == "rebound" and qualified and scope == "method" else 3
    assert [row.metric_value for row in found] == ([] if binding == "external" else [metric])
