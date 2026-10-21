"""Visible writes and subclass evidence must not manufacture external types."""

import ast
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


def metrics(detector, tmp_path, source, kind="feature_envy"):
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    return [row.metric_value for row in detector.coupling_issues(ast.parse(source), str(sample))
            if row.issue_type == kind]


@pytest.mark.parametrize("scope", ["module", "method"])
def test_qualified_annotation_without_value_does_not_replace_a_class(detector, tmp_path, scope):
    source = textwrap.dedent("""
        import fractions as library
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case library.Fraction() as item:
                        return item.numerator + item.denominator + item.real
    """)
    if scope == "module":
        source = source.replace("class Entry:", "library.Fraction: object\nclass Entry:")
    else:
        source = source.replace("        match value:", "        library.Fraction: object\n        match value:")
    namespace = {}
    exec(source, namespace)
    assert namespace["library"].Fraction is Fraction
    assert namespace["Entry"]().save(Fraction(3, 2)) == 6.5
    assert metrics(detector, tmp_path, source) == []


@pytest.mark.parametrize("body,expected,value", [
    ("nonlocal Constants\nConstants = Payload()", [3], 6),
    ("nonlocal Constants\nif True:\n    Constants = Payload()", [3], 6),
    ("nonlocal Constants\nConstants = Constants", [], 15),
    ("nonlocal Constants", [], 15),
    ("Constants = Payload()", [], 15),
])
def test_class_namespace_exemption_includes_its_actual_nonlocal_writers(
        detector, tmp_path, body, expected, value):
    source = textwrap.dedent("""
        class Payload:
            first = 1
            second = 2
            third = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Constants:
                    first = 4
                    second = 5
                    third = 6
                class Configure:
                    BODY
                return Constants.first + Constants.second + Constants.third
    """).replace("            BODY", textwrap.indent(body, "            "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save() == value
    assert metrics(detector, tmp_path, source) == expected


@pytest.mark.parametrize("base,pattern,local,body", [
    ("int", "int", True, "pass"),
    ("Fraction", "Fraction", True, "pass"),
    ("Kind", "library.Fraction", True, "pass"),
    ("int", "Fraction", False, "pass"),
    ("Fraction", "int", False, "pass"),
    ("int", "int", True, "int = object"),
])
def test_external_class_patterns_do_not_exempt_visible_local_subclasses(
        detector, tmp_path, base, pattern, local, body):
    source = textwrap.dedent(f"""
        import fractions as library
        from fractions import Fraction
        Kind = Fraction
        class Payload({base}):
            {body}
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}() as item:
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    supplied = namespace["Payload"](1) if local else (3 if pattern == "int" else Fraction(3, 2))
    assert namespace["Entry"]().save(supplied) == (6 if local else 7 if pattern == "int" else 6.5)
    assert metrics(detector, tmp_path, source) == ([3] if local else [])


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("explicit", [False, True])
def test_implicit_class_getitem_dispatches_through_the_metaclass(detector, tmp_path, builtin, explicit):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            DECORATOR
            def __class_getitem__(cls, key):
                return {builtin}(cls)
            def __{builtin}__(self):
                return self.payload
    """).replace("    DECORATOR", "    @classmethod" if explicit else "")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"][0] == "<class 'Entry'>"
    assert metrics(detector, tmp_path, source, "low_cohesion") == []


@pytest.mark.parametrize("scope", ["module", "method", "helper"])
@pytest.mark.parametrize("binding,expected", [
    ("other.Kind = Payload", [3]),
    ("other.Kind: object = Payload", [3]),
    ("other.Kind: object", []),
    ("other.Other = Payload", []),
])
def test_qualified_writes_follow_canonical_module_imports(
        detector, tmp_path, monkeypatch, scope, binding, expected):
    library = types.ModuleType("pr39_external")
    library.Kind = Fraction
    monkeypatch.setitem(sys.modules, "pr39_external", library)
    prefix = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
    """)
    declarations = "import pr39_external as library\nimport pr39_external as other\n" + binding
    entry = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                DECLARATIONS
                match value:
                    case library.Kind() as item:
                        return item.numerator + item.denominator + item.real
    """)
    if scope == "module":
        source = prefix + declarations + "\n" + entry.replace("        DECLARATIONS\n", "")
    elif scope == "method":
        source = prefix + entry.replace("        DECLARATIONS", textwrap.indent(declarations, "        "))
    else:
        declarations = "import pr39_external as library\ndef configure():\n" + textwrap.indent(
            "import pr39_external as other\n" + binding, "    ") + "\nconfigure()"
        source = prefix + declarations + "\n" + entry.replace("        DECLARATIONS\n", "")
    namespace = {}
    exec(source, namespace)
    supplied = namespace["Payload"]() if expected else Fraction(3, 2)
    assert namespace["Entry"]().save(supplied) == (6 if expected else 6.5)
    assert metrics(detector, tmp_path, source) == expected


@pytest.mark.parametrize("binding", [
    "other = library\nif flag:\n    other = object()",
    "other, unused = library, None",
    "other = library\nalias = other\nother = alias",
    "other = library\nif flag:\n    other = other.child",
])
def test_possible_module_alias_write_keeps_its_known_import_origin(detector, tmp_path, monkeypatch, binding):
    library = types.ModuleType("pr39_external")
    library.Kind = Fraction
    monkeypatch.setitem(sys.modules, "pr39_external", library)
    source = textwrap.dedent("""
        import pr39_external as library
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        def configure(flag=False):
            BINDING
            other.Kind = Payload
        configure()
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case library.Kind() as item:
                        return item.numerator + item.denominator + item.real
    """).replace("    BINDING", textwrap.indent(binding, "    "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    assert metrics(detector, tmp_path, source) == [3]


@pytest.mark.parametrize("declaration,base,pattern", [
    ("Base = int\nif False:\n    Base = Other", "Base", "int"),
    ("", "list[int]", "list"),
    ("Base = list[int]", "Base", "list"),
    ("", "object", "object"),
    ("", "", "object"),
])
def test_possible_and_generic_local_bases_keep_subclass_evidence(
        detector, tmp_path, declaration, base, pattern):
    source = textwrap.dedent("""
        class Other:
            pass
    """) + declaration + "\n" + textwrap.dedent(f"""
        class Payload{f'({base})' if base else ''}:
            first = 1
            second = 2
            third = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}() as item:
                        return item.first + item.second + item.third
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    assert metrics(detector, tmp_path, source) == [3]
